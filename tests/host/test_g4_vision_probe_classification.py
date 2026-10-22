"""The vision probe's error classes, which decide what the next call does.

``classify_error`` output is control flow in ``scripts/g4_vision_probe.py``: one
class aborts the run, one authorises the single permitted retry, and
``image_input_rejected`` is what sets ``supports_image_input`` — the verdict the
per-run visual-call budget cites. These tests pin the boundary that a refusal
has to name both a modality and a refusal, so an unrelated 400 cannot be
recorded as the provider being blind, and the same single-spelling rule for the
prompt kinds that decide which answer carrier a reply is recorded in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = PROJECT_ROOT / "scripts" / "g4_vision_probe.py"
GOLD_CHART_PNG = (
    PROJECT_ROOT / "data" / "corpus" / "g4_attachment_gold" / "g4-image-chart-001.png"
)


def _probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g4_vision_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Statused(RuntimeError):
    """A provider exception that carries its own status code."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (_Statused("Invalid API key passed in the request", 401), "auth_or_permission"),
        (_Statused("Forbidden: workspace lacks the model", 403), "auth_or_permission"),
        (TimeoutError("read timed out"), "transport"),
        (_Statused("Connection error.", 0), "transport"),
        (
            _Statused("This model does not support image inputs", 400),
            "image_input_rejected",
        ),
        (_Statused("image_url is not supported by this endpoint", 400), "image_input_rejected"),
        (_Statused("multimodal content is not supported here", 400), "image_input_rejected"),
        (_Statused("Your input was flagged by content policy", 400), "provider_refusal"),
        (_Statused("Too many requests", 429), "rate_limited"),
        (_Statused("unknown parameter: response_format", 400), "bad_request"),
        (RuntimeError("something the provider never documented"), "provider_error"),
    ],
    ids=[
        "401",
        "403",
        "timeout",
        "connection",
        "refuses_images",
        "refuses_image_url",
        "refuses_multimodal",
        "content_policy",
        "429",
        "400_without_modality",
        "undocumented",
    ],
)
def test_one_error_class_per_failure(exception: BaseException, expected: str) -> None:
    assert _probe().classify_error(exception) == expected


def test_a_400_about_something_else_is_not_read_as_a_blind_provider() -> None:
    """The rejection has to name the modality, not merely contain "image".

    A request that mentions an image field and fails for another reason is a
    bad request, and recording it as a modality rejection would set
    ``supports_image_input`` false against a provider that answers image calls
    correctly.
    """
    probe = _probe()

    for message in (
        "image payload exceeds the request size limit of 1 byte",
        "image field must be base64, not a path",
        "does not support response_format on this route",
    ):
        assert probe.classify_error(_Statused(message, 400)) == "bad_request"


def test_the_verdict_bearing_class_is_named_once() -> None:
    """No call site may spell the class out again.

    ``verdicts`` and the retry decision both branch on these values, so a second
    spelling of one of them would silently stop matching.
    """
    probe = _probe()
    assert probe.IMAGE_INPUT_REJECTED == probe.classify_error(
        _Statused("the model does not accept images", 400)
    )
    source = PROBE_PATH.read_text()
    for literal in (
        '"image_input_rejected"',
        '"auth_or_permission"',
        '"transport"',
        '"provider_refusal"',
        '"rate_limited"',
        '"bad_request"',
        '"provider_error"',
    ):
        assert source.count(literal) == 1, f"{literal} is spelled more than once"


def test_live_probe_loads_operator_model_settings_before_rendering_or_building_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _probe()
    events: list[str] = []
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.setattr(
        probe,
        "apply_operator_settings",
        lambda: events.append("settings"),
    )
    monkeypatch.setattr(
        probe,
        "render_probe_page",
        lambda *_args: events.append("render"),
    )
    monkeypatch.setattr(
        probe,
        "build_client",
        lambda: pytest.fail("the probe must reject a missing model before a call"),
    )

    assert probe.main([]) == 2
    assert events == ["settings"]
    assert "FIN_MODEL" in capsys.readouterr().err


def test_the_prompt_kind_is_named_once() -> None:
    """Each of the four kinds is spelled once, at its own constant.

    The prompt table keys the question, ``_record`` keys which answer carrier a
    response lands in, and the control call keys whether an image went out. A
    second spelling that drifted would leave a carrier null on a call that
    succeeded, which reads exactly like a provider that answered nothing.
    """
    source = PROBE_PATH.read_text()
    for literal in ('"presence"', '"meaning"', '"structured_json"', '"text_control"'):
        assert source.count(literal) == 1, f"{literal} is spelled more than once"


class _Reply:
    """A provider answer with text and nothing else."""

    content = "ok"
    usage_metadata = None
    response_metadata = None


class _Client:
    def invoke(self, messages: Any) -> _Reply:
        return _Reply()


def _probe_on_the_gold_chart() -> Any:
    probe = _probe()
    return probe.Probe(
        _Client(),
        GOLD_CHART_PNG,
        probe.Scrubber(("placeholder-secret-value",)),
        probe.Target(),
    )


@pytest.mark.parametrize(
    ("kind_attr", "presence", "meaning", "image_sent"),
    [
        ("KIND_PRESENCE", True, False, True),
        ("KIND_MEANING", False, True, True),
        ("KIND_STRUCTURED", False, True, True),
        ("KIND_TEXT_CONTROL", False, False, False),
    ],
)
def test_a_chart_answer_is_recorded_only_for_its_own_question(
    kind_attr: str, presence: bool, meaning: bool, image_sent: bool
) -> None:
    """Which carrier a reply fills is decided by the kind, not by exclusion.

    Gating the meaning carrier on "anything but presence" wrote the control's
    "ok" - a reply to a request that carried no image - into the same field as a
    chart reading, and put a witness in the artifact the provider never saw.
    """
    probe = _probe()
    subject = _probe_on_the_gold_chart()

    record = subject.invoke(getattr(probe, kind_attr), [])

    assert record["chart_presence_answer"] == ("ok" if presence else None)
    assert record["chart_meaning_answer"] == ("ok" if meaning else None)
    assert (record["image_bytes"] is not None) is image_sent


def test_the_control_call_is_the_only_one_that_sends_no_image() -> None:
    """The control separates "cannot see" from "cannot answer at all".

    Its verdict value depends on it being the one call with no image in the
    request, so its dimensions must be null while every chart question carries
    the bytes the provider was given.
    """
    probe = _probe()
    kinds = [kind for kind, _builder in probe.PROMPTS] + [probe.KIND_TEXT_CONTROL]

    records = [_probe_on_the_gold_chart().invoke(kind, []) for kind in kinds]

    assert [r["image_width"] is None for r in records] == [False, False, False, True]
