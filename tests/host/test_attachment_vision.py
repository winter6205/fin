"""Contract tests for fin's dedicated vision port (ADR 0089).

The port is one call over one rendered image. These tests pin the outward
surface the attachment-preparation and observation-persistence sides depend
on: the typed failure codes, provider-reported usage, the representation of a
cost the provider did not report, and where the model identity comes from.

Everything here is hermetic: a fake client stands in for the provider, so no
test in the default run opens a socket or needs a credential. The single
``live_round_trip`` case is opt-in (see its docstring) and reports a missing
credential as a named failure rather than a pass.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from fin.host import attachment_vision
from fin.host import compile as host_compile
from fin.host.attachment_vision import (
    VISION_CALL_FAILED,
    VISION_EMPTY_TEXT,
    ChatModelVisionAdapter,
    VisionCallFailedError,
    VisionCallResult,
    VisionCrop,
    VisionEmptyTextError,
    VisionErrorClass,
    VisionModelConfig,
    VisionPort,
    VisionTarget,
    resolve_vision_model_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_SOURCE = Path(attachment_vision.__file__).read_text(encoding="utf-8")

# A committed G4 gold fixture: a real chart page, so the live case asks the
# question ADR 0089 actually assigns the provider.
GOLD_IMAGE = REPO_ROOT / "data/corpus/g4_attachment_gold/g4-image-chart-001.png"

CONFIG_ENV: Final[dict[str, str]] = {
    "FIN_MODEL": "configured-vision-model",
    "FIN_BASE_URL": "https://configured.invalid/v1",
    "FIN_API_KEY": "test-only-not-a-real-secret",
}


def target(*, crop: VisionCrop | None = None) -> VisionTarget:
    return VisionTarget(
        attachment_id="att-1", version="v3", page=2, crop=crop
    )


class FakeClient:
    """A stand-in provider: returns one prepared message, or raises."""

    def __init__(self, *, message: Any = None, error: Exception | None = None) -> None:
        self.message = message
        self.error = error
        self.calls: list[Any] = []

    def invoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.message


class FakeMessage:
    """The shape of a langchain response, without importing langchain."""

    def __init__(
        self,
        content: Any,
        *,
        usage_metadata: Any = None,
        response_metadata: Any = None,
    ) -> None:
        self.content = content
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


def adapter_for(client: Any) -> ChatModelVisionAdapter:
    return ChatModelVisionAdapter(
        env=CONFIG_ENV, client_factory=lambda _config: client
    )


def result_text(message_content: Any, **kwargs: Any) -> VisionCallResult:
    client = FakeClient(message=FakeMessage(message_content, **kwargs))
    return adapter_for(client).inspect(image=PNG_BYTES, target=target())


PNG_BYTES: Final[bytes] = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63600000020001e221bc330000000049454e44ae426082"
)


# ----- port shape -----------------------------------------------------


def test_port_is_a_single_method_protocol() -> None:
    """The one-call surface is the contract; a second method would let the
    retry reserve and the budget check drift out of the caller's sight."""
    methods = [
        name
        for name in vars(VisionPort)
        if not name.startswith("_") and callable(getattr(VisionPort, name))
    ]
    assert methods == ["inspect"]
    assert isinstance(adapter_for(FakeClient(message=FakeMessage("ok"))), VisionPort)


# ----- success --------------------------------------------------------


def test_inspect_returns_text_target_and_reported_usage() -> None:
    result = result_text(
        "Page 2 carries one bar chart; Q3 revenue is 41.2.",
        usage_metadata={
            "input_tokens": 1200,
            "output_tokens": 34,
            "total_tokens": 1234,
        },
    )

    assert result.text == "Page 2 carries one bar chart; Q3 revenue is 41.2."
    # Provenance identity travels back with the answer, unprompted.
    assert result.target == target()
    assert result.model_id == CONFIG_ENV["FIN_MODEL"]
    assert result.media_type == "image/png"
    assert result.usage is not None
    assert result.usage.input_tokens == 1200
    assert result.usage.output_tokens == 34
    assert result.usage.total_tokens == 1234


def test_image_bytes_and_identity_reach_the_provider_once() -> None:
    import base64

    crop = VisionCrop(left=0.1, top=0.2, right=0.4, bottom=0.6)
    client = FakeClient(message=FakeMessage("41.2", usage_metadata={"total_tokens": 3}))
    adapter_for(client).inspect(
        image=PNG_BYTES, target=target(crop=crop), media_type="image/jpeg"
    )

    assert len(client.calls) == 1
    message = client.calls[0][0]
    parts = message.content
    text_part = next(p for p in parts if p["type"] == "text")
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    payload = image_part["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(payload) == PNG_BYTES
    # The crop is named to the model, so a crop answer cannot be mistaken
    # for whole-page coverage.
    assert "0.1" in text_part["text"] and "0.6" in text_part["text"]


def test_multimodal_parts_content_is_flattened_to_text() -> None:
    result = result_text(
        [{"type": "text", "text": "chart "}, {"type": "text", "text": "meaning"}]
    )
    assert result.text == "chart meaning"


def test_token_usage_spelling_is_also_read() -> None:
    result = result_text(
        "ok",
        response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 5}},
    )
    assert result.usage is not None
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 5
    # An absent field stays absent rather than becoming a zero.
    assert result.usage.total_tokens is None


# ----- typed failure exits -------------------------------------------


def test_provider_transport_fault_is_vision_call_failed() -> None:
    client = FakeClient(error=RuntimeError("connection reset by peer"))
    with pytest.raises(VisionCallFailedError) as excinfo:
        adapter_for(client).inspect(image=PNG_BYTES, target=target())

    assert excinfo.value.code == VISION_CALL_FAILED == "vision_call_failed"
    assert excinfo.value.__cause__ is client.error
    # Class name only: a provider message can echo a request line.
    assert "RuntimeError" in str(excinfo.value)
    assert "connection reset" not in str(excinfo.value)


def test_empty_output_is_vision_empty_text_not_a_blank_success() -> None:
    for content in ("", "   \n ", [], None, 17):
        with pytest.raises(VisionEmptyTextError) as excinfo:
            result_text(content)
        assert excinfo.value.code == VISION_EMPTY_TEXT == "vision_empty_text"


def test_the_two_failure_conditions_stay_distinct() -> None:
    assert VISION_CALL_FAILED != VISION_EMPTY_TEXT
    assert not issubclass(VisionEmptyTextError, VisionCallFailedError)
    assert not issubclass(VisionCallFailedError, VisionEmptyTextError)


# ----- retry classification ------------------------------------------
#
# The run's single reserved full-page retry (attachment_budgets.
# VisionCallLedger.charge_full_page_attempt(retry=True)) may only be spent on
# an adapter-classified retryable transport failure, so the adapter — not the
# caller reading exception prose — owns the verdict. These cases pin the
# taxonomy the G4 probe already proved against the real provider.


class _StatusedError(RuntimeError):
    """A provider exception that carries its own status code."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _failure_for(error: Exception) -> VisionCallFailedError:
    client = FakeClient(error=error)
    with pytest.raises(VisionCallFailedError) as excinfo:
        adapter_for(client).inspect(image=PNG_BYTES, target=target())
    return excinfo.value


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("read timed out"),
        ConnectionError("connection reset by peer"),
        _StatusedError("Connection error.", 0),
        _StatusedError("Server disconnected without sending a response", 0),
        RuntimeError("stream was reset: INTERNAL_ERROR"),
    ],
    ids=[
        "timeout",
        "connection_reset",
        "openai_connection_error",
        "server_disconnected",
        "stream_reset",
    ],
)
def test_transport_faults_are_classified_retryable(error: Exception) -> None:
    exc = _failure_for(error)
    assert exc.code == VISION_CALL_FAILED  # wire code unchanged by classification
    assert exc.error_class is VisionErrorClass.TRANSPORT
    assert exc.retryable is True


def test_dns_gaierror_shape_is_retryable_transport() -> None:
    import socket

    exc = _failure_for(socket.gaierror("[Errno -3] Temporary failure in name resolution"))
    assert exc.error_class is VisionErrorClass.TRANSPORT
    assert exc.retryable is True


@pytest.mark.parametrize(
    "error",
    [
        _StatusedError("Invalid API key passed in the request", 401),
        _StatusedError("Forbidden: workspace lacks the model", 403),
        _StatusedError("Too many requests", 429),
        _StatusedError("Bad request: unknown parameter", 400),
        RuntimeError("Quota exceeded for this billing account"),
        _StatusedError("This model does not support image inputs", 400),
        _StatusedError("Your input was flagged by content policy", 400),
    ],
    ids=[
        "401",
        "403",
        "429_rate_limit",
        "400",
        "quota_text_only",
        "modality_refusal",
        "content_policy",
    ],
)
def test_provider_refusals_are_classified_non_retryable(error: Exception) -> None:
    exc = _failure_for(error)
    assert exc.code == VISION_CALL_FAILED
    assert exc.error_class is VisionErrorClass.PERMANENT
    assert exc.retryable is False


def test_unclassifiable_fault_is_conservatively_non_retryable() -> None:
    """A failure the adapter cannot name must not spend the reserved retry;
    the reserve is only for faults proven to be transport."""
    exc = _failure_for(RuntimeError("something the provider never documented"))
    assert exc.error_class is VisionErrorClass.PERMANENT
    assert exc.retryable is False


def test_classification_leaves_the_message_class_name_only() -> None:
    """The retry verdict is data on the error, not prose in its message: a
    provider message can echo a request line and never belongs outward."""
    exc = _failure_for(TimeoutError("read timed out to https://secret.host"))
    assert "secret.host" not in str(exc)
    assert "TimeoutError" in str(exc)


def test_empty_output_error_is_never_retryable() -> None:
    """Malformed output retried is a second chance at a pass, which the spec
    forbids; the type alone says so without consulting the ledger."""
    for content in ("", "   \n ", [], None, 17):
        with pytest.raises(VisionEmptyTextError) as excinfo:
            result_text(content)
        assert excinfo.value.retryable is False
        assert excinfo.value.code == VISION_EMPTY_TEXT


def test_unset_credential_failure_is_not_a_transport_fault() -> None:
    """A configuration refusal looks raisable like any other call failure,
    but retrying it can never make the key appear."""
    adapter = ChatModelVisionAdapter(
        env={"FIN_MODEL": "m", "FIN_BASE_URL": "https://x.invalid/v1"}
    )
    with pytest.raises(VisionCallFailedError) as excinfo:
        adapter.inspect(image=PNG_BYTES, target=target())
    assert excinfo.value.error_class is VisionErrorClass.PERMANENT
    assert excinfo.value.retryable is False


def test_successful_call_is_unaffected_by_classification() -> None:
    result = result_text("one bar chart on page 2")
    assert result.text == "one bar chart on page 2"


def test_unusable_input_is_refused_before_any_call() -> None:
    client = FakeClient(message=FakeMessage("ok"))
    adapter = adapter_for(client)
    with pytest.raises(ValueError):
        adapter.inspect(image=b"", target=target())
    with pytest.raises(ValueError):
        adapter.inspect(image=PNG_BYTES, target=target(), media_type="image/svg+xml")
    with pytest.raises(ValueError):
        adapter.inspect(image=PNG_BYTES, target=replace(target(), page=0))
    with pytest.raises(ValueError):
        adapter.inspect(image=PNG_BYTES, target=replace(target(), version="  "))
    with pytest.raises(ValueError):
        adapter.inspect(
            image=PNG_BYTES,
            target=target(crop=VisionCrop(left=0.8, top=0.1, right=0.2, bottom=0.5)),
        )
    assert client.calls == []


# ----- usage and cost recording --------------------------------------


def test_unreported_cost_is_unavailable_never_zero() -> None:
    result = result_text("ok", usage_metadata={"input_tokens": 10, "output_tokens": 2})
    assert result.cost is None


def test_reported_cost_is_kept_including_a_reported_zero() -> None:
    reported = result_text(
        "ok", usage_metadata={"total_tokens": 3, "cost": "0.00042", "currency": "USD"}
    )
    assert reported.cost is not None
    assert reported.cost.amount == Decimal("0.00042")
    assert reported.cost.currency == "USD"

    free = result_text("ok", usage_metadata={"total_tokens": 3, "cost": 0})
    assert free.cost is not None
    assert free.cost.amount == Decimal("0")

    # A cost the provider could not express is still unavailable, not a
    # number invented from a price table.
    unparsable = result_text("ok", usage_metadata={"total_tokens": 3, "cost": "n/a"})
    assert unparsable.cost is None


def test_usage_absent_entirely_is_none_not_zeros() -> None:
    result = result_text("ok")
    assert result.usage is None
    assert result.cost is None


def test_no_ceiling_is_enforced_here() -> None:
    """The monetary ceiling is deferred by product decision: recording must
    not silently grow into enforcement."""
    result = result_text("ok", usage_metadata={"total_tokens": 999_999, "cost": 500})
    assert result.usage is not None and result.usage.total_tokens == 999_999
    assert result.cost is not None and result.cost.amount == Decimal("500")


# ----- configuration, not literals ------------------------------------


def test_model_and_base_url_come_from_configuration() -> None:
    config = resolve_vision_model_config(env=CONFIG_ENV)
    assert config == VisionModelConfig(
        model_id="configured-vision-model",
        base_url="https://configured.invalid/v1",
    )
    assert resolve_vision_model_config(env={"FIN_MODEL": "  m2  "}).model_id == "m2"


def test_unset_configuration_falls_back_to_the_hosts_defaults() -> None:
    """The vision port reuses the host's own model settings, so an operator
    who configured only the chat model gets the same model here."""
    config = resolve_vision_model_config(env={})
    assert config.model_id == host_compile.DEFAULT_MODEL
    assert config.base_url == host_compile.DEFAULT_BASE_URL


def test_no_model_or_endpoint_literal_and_no_extra_credential() -> None:
    assert "MiniMax" not in MODULE_SOURCE
    assert re.search(r"https?://", MODULE_SOURCE) is None
    assert "sk-no-key-required" not in MODULE_SOURCE
    # The three host settings and nothing else: no vision-specific key, model,
    # or endpoint setting is introduced.
    named = set(re.findall(r"FIN_[A-Z_]+", MODULE_SOURCE))
    assert named == {
        "FIN_MODEL",
        "FIN_MODEL_ENV",
        "FIN_BASE_URL",
        "FIN_BASE_URL_ENV",
        "FIN_API_KEY_ENV",
    }
    assert attachment_vision.FIN_API_KEY_ENV is host_compile.FIN_API_KEY_ENV


def test_missing_credential_is_a_typed_call_failure_naming_the_env_var() -> None:
    """Refusing beats inventing a placeholder key: a placeholder value is
    ``compile``'s private detail and a silent ``OPENAI_API_KEY`` fallback
    would spend a credential this port was never granted."""
    adapter = ChatModelVisionAdapter(
        env={"FIN_MODEL": "m", "FIN_BASE_URL": "https://x.invalid/v1"}
    )
    with pytest.raises(VisionCallFailedError) as excinfo:
        adapter.inspect(image=PNG_BYTES, target=target())
    assert "FIN_API_KEY" in str(excinfo.value)
    assert excinfo.value.code == VISION_CALL_FAILED


def test_default_client_is_built_from_the_resolved_configuration() -> None:
    seen: list[VisionModelConfig] = []

    def factory(config: VisionModelConfig) -> FakeClient:
        seen.append(config)
        return FakeClient(message=FakeMessage("ok"))

    ChatModelVisionAdapter(env=CONFIG_ENV, client_factory=factory).inspect(
        image=PNG_BYTES, target=target()
    )
    assert seen == [
        VisionModelConfig(
            model_id="configured-vision-model",
            base_url="https://configured.invalid/v1",
        )
    ]


def test_real_client_import_is_lazy() -> None:
    """Construction must not need the heavy provider package, so a boot path
    can hold the port without paying for the runtime import."""
    ChatModelVisionAdapter(env=CONFIG_ENV)


# ----- opt-in live round trip ----------------------------------------

LIVE_OPT_IN_ENV: Final[str] = "FIN_VISION_LIVE_ROUNDTRIP"


def _live_credentials() -> dict[str, str]:
    """Shell values first, then the operator ``.env``.

    A git worktree has no ``.env`` of its own and ``find_env_file`` stops at
    its ``pyproject.toml``, so the main checkout — the directory the worktree's
    git common dir sits in — is the second place to look. Values are never
    logged.
    """
    env = {
        key: os.environ[key]
        for key in ("FIN_MODEL", "FIN_BASE_URL", attachment_vision.FIN_API_KEY_ENV)
        if key in os.environ
    }
    candidates = [REPO_ROOT / ".env"]
    common = _git_common_dir()
    if common is not None:
        candidates.append(common.parent / ".env")
    for path in candidates:
        if path.is_file():
            from fin.host.operator_env import parse_env_file

            for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
                env.setdefault(key, value)
            break
    return env


def _git_common_dir() -> Path | None:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def test_live_round_trip_against_the_configured_provider() -> None:
    """One real call, opt-in::

        FIN_VISION_LIVE_ROUNDTRIP=1 uv run pytest -q \
            tests/host/test_attachment_vision.py -k live_round_trip

    Not selected unless the switch is set — this task may not register a new
    pytest marker. Selected but without a credential is a NAMED FAILURE
    (blocked evidence), never a green skip or a Fake substitute.
    """
    if os.environ.get(LIVE_OPT_IN_ENV) != "1":
        pytest.skip(f"{LIVE_OPT_IN_ENV}=1 not set: live case not selected")
    env = _live_credentials()
    if not (env.get(attachment_vision.FIN_API_KEY_ENV) or "").strip():
        pytest.fail(
            "vision_live_blocked: no credential — set "
            f"{attachment_vision.FIN_API_KEY_ENV} in the repo .env, then run "
            f"`{LIVE_OPT_IN_ENV}=1 uv run pytest -q "
            "tests/host/test_attachment_vision.py -k live_round_trip`",
            pytrace=False,
        )
    if not GOLD_IMAGE.is_file():
        pytest.fail(
            f"vision_live_blocked: gold image absent: {GOLD_IMAGE}", pytrace=False
        )

    from fin.host.compile import DEFAULT_MODEL

    adapter = ChatModelVisionAdapter(env=env)
    result = adapter.inspect(
        image=GOLD_IMAGE.read_bytes(),
        target=VisionTarget(attachment_id="g4-image-chart-001", version="t2e", page=1),
        media_type="image/png",
    )

    # A model slug is configuration, not a secret; the endpoint path and any
    # key value never appear here.
    suffix = " (=host default)" if result.model_id == DEFAULT_MODEL else ""
    print(
        "\n[vision-live] model="
        f"{result.model_id}{suffix} "
        f"chars={len(result.text)} usage={result.usage} cost={result.cost}"
    )
    assert result.text
    assert result.model_id
