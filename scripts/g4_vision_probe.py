"""G4 T2a probe: what can fin's CONFIGURED vision provider actually see?

ADR 0089 assigns the configured provider — not a local VLM and not a
picture classifier — the job of deciding, for every rendered full page,
both whether a chart is present and what it says. fin has no vision
client yet (T2e owns that production seam), so the per-run visual-call
budget T2a must decide rests on evidence nobody has collected: does the
configured model accept an image at all, does it answer correctly, and
what does one call cost in latency and tokens?

This script is that evidence. It is a probe, not a seam:

* by default it builds ONE throwaway chart image with PIL in
  ``/tmp/g4_vision_probe/``, so the recorded measurement is independent of the
  suites that later consume the decision; ``--image`` points the same questions
  at a file on disk instead, which is how a committed fixture or a figure crop
  gets priced;
* provider config comes from fin's own helper
  (:func:`fin.host.compile._build_default_model`), so the probe cannot
  drift from the product boot path and re-implements no provider
  construction or base URL selection; the model itself must be configured;
* it asks the two distinct ADR 0089 questions (presence, meaning) plus a
  third structured call, and records provider-reported usage, latency,
  response length, and the image dimensions actually sent, per call;
* it never prints or writes a key, a full base URL, or any query
  component — env var NAMES, the model slug, and ``scheme://host`` only.

The meaning question carries a checkable witness: the mean of a series
whose values only the image knows. A provider that silently drops an
unsupported image part cannot produce it, which is why "answered, with
the right mean" — not "answered" — is the bar for
``verdicts.supports_image_input``.

Hard budget: at most ``MAX_REAL_CALLS`` (8) real API calls, one retry per
call and only on a transport error. An auth/permission failure stops the
run at once; this script never loops against an endpoint that said no.
Empty or malformed provider output is recorded as an empty result, never
retried into a pass.

Usage::

    uv run python <path>/scripts/g4_vision_probe.py
    uv run python <path>/scripts/g4_vision_probe.py --render-only
    uv run python <path>/scripts/g4_vision_probe.py --image <file> \
        --series "Total net sales" --mean 94186.5 --out <json>

Credentials come from ``~/.fin/settings.yaml`` (``llm.apiKey``) or from
``FIN_API_KEY`` already in the process. There is no ``.env`` search.

``--image`` points the same three questions at a file on disk instead of the
throwaway page; ``--series``, ``--mean`` and ``--out`` are then required, because
a witness that belongs to another file proves nothing and a record written over
the default artifact would mislabel its subject.

Exit codes: ``0`` the probe ran and image input is confirmed; ``1`` the
probe ran but a verdict is negative (that finding IS a result, not a
crash); ``2`` usage error — bad arguments, an unreadable ``--image``, or one
whose suffix this script cannot type — where no request is built at all;
``3`` blocked on an auth/permission failure, where no further call is made.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin.host import compile as host_compile  # noqa: E402
from fin.host.operator_settings import apply_operator_settings  # noqa: E402

# ----- measured inputs ------------------------------------------------


MAX_REAL_CALLS = 8  # hard cap for this whole ticket
SCRATCH_DIR = Path("/tmp/g4_vision_probe")
ARTIFACT_PATH = PROJECT_ROOT / "data" / "corpus" / "g4_vision_probe.json"

# A US-Letter page at 96 dpi: the shape a rendered full page has in T2e.
PAGE_SIZE_PX = (816, 1056)

# The witness. Only the image carries these numbers, so a correct mean is
# positive evidence the bytes reached the model rather than being dropped.
SERIES_NAME = "Revenue"
BAR_LABELS = ["Q1", "Q2", "Q3", "Q4"]
BAR_VALUES = [12, 17, 23, 31]
EXPECTED_MEAN = sum(BAR_VALUES) / len(BAR_VALUES)  # 20.75

# How the sentence describes the bytes sent. ``--image`` cannot claim to be a
# page, and a page render is not a chart-only crop either.
PAGE_MEDIUM = "one rendered page of a financial document"
ATTACHED_MEDIUM = "one image attached to a financial document"

ENV_NAMES_READ = ("FIN_MODEL", "FIN_BASE_URL", host_compile.FIN_API_KEY_ENV)


@dataclass(frozen=True)
class Target:
    """The thing under probe and the one answer that proves it was seen.

    ``expected_mean`` is never stated in a prompt and never sent as text, so
    a correct mean can only come from the image bytes reaching the model.
    """

    series_name: str = SERIES_NAME
    medium: str = PAGE_MEDIUM
    expected_mean: float = EXPECTED_MEAN


# ----- the output contract --------------------------------------------


STRUCTURED_KEYS = (
    "has_chart",
    "chart_type",
    "series_name",
    "category_labels",
    "bar_values",
    "mean_of_series",
    "description",
)


class BudgetExhausted(RuntimeError):
    """Raised when the hard real-call cap would be exceeded."""


class Blocked(Exception):
    """Raised on an auth/permission failure: stop, do not retry."""


# ----- provider config (fin's own helper) -----------------------------


def build_client() -> tuple[Any, str]:
    """fin's configured ``ChatOpenAI`` and the key value it resolved.

    Private-symbol probe seam: ``fin.host.compile`` exports no model factory,
    so this is the one place that touches ``_build_default_model`` and
    ``_read_env``. The probe measures the product's own model, base URL, key
    resolution (including the loud ``sk-no-key-required`` placeholder),
    ``timeout=120.0`` and ``max_retries=0``, and re-implements none of them.
    The key value is for the scrubber only and is never recorded.
    """
    client = host_compile._build_default_model(model_name=None, base_url=None, env=None)
    return client, host_compile._read_env(None, host_compile.FIN_API_KEY_ENV) or ""


def host_only(url: str) -> str:
    """Reduce a base URL to ``scheme://host`` — never path or query."""
    parts = urlsplit(str(url))
    if not parts.scheme or not parts.netloc:
        return "unknown"
    return f"{parts.scheme}://{parts.hostname or parts.netloc}"


def config_block(client: Any) -> dict[str, Any]:
    """Recorded config: env var names, model slug, host only. No secrets."""
    return {
        "env_names_read": list(ENV_NAMES_READ),
        "model_slug": str(getattr(client, "model_name", "unknown")),
        "base_url_host_only": host_only(
            getattr(client, "openai_api_base", "") or ""
        ),
    }


# ----- the throwaway chart page ---------------------------------------


def _font(size: int) -> Any:
    from PIL import ImageFont

    return ImageFont.load_default(size=size)


def render_probe_page(path: Path, target: Target) -> None:
    """Draw a page-like PNG with one bar chart.

    The chart is deliberately the one thing a text-only model cannot
    guess: labelled axes, four bars, known values, stated mean. The mean
    printed on the page is taken from ``target``, so the page cannot be
    drawn for one witness and judged against another.
    """
    from PIL import Image, ImageDraw

    width, height = PAGE_SIZE_PX
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(30)
    label_font = _font(24)
    small_font = _font(20)

    left = 120
    right = width - 80
    top = 150
    bottom = 640
    plot_left = left + 60
    scale_max = 35.0

    draw.text(
        (left, 70),
        "ACME Industrial Holdings - Exhibit 4",
        fill="black",
        font=title_font,
    )
    draw.text(
        (left, 112),
        f"Quarterly {target.series_name.lower()} by segment "
        "(USD millions, unaudited)",
        fill="black",
        font=small_font,
    )
    draw.line([(plot_left, top), (plot_left, bottom)], fill="black", width=2)
    draw.line([(plot_left, bottom), (right, bottom)], fill="black", width=2)

    for tick in range(0, int(scale_max) + 1, 5):
        y = bottom - (tick / scale_max) * (bottom - top)
        draw.text((left, y - 10), str(tick), fill="black", font=small_font)
        draw.line([(plot_left - 6, y), (plot_left, y)], fill="black", width=1)

    span = (right - plot_left) / len(BAR_LABELS)
    bar_width = span * 0.5
    for index, (label, value) in enumerate(
        zip(BAR_LABELS, BAR_VALUES, strict=True)
    ):
        bar_left = plot_left + index * span + (span - bar_width) / 2
        bar_top = bottom - (value / scale_max) * (bottom - top)
        draw.rectangle(
            [bar_left, bar_top, bar_left + bar_width, bottom], fill="#3b6ea5"
        )
        centre = bar_left + bar_width / 2
        draw.text((centre - 12, bottom + 10), label, fill="black", font=label_font)
        draw.text((centre - 14, bar_top - 28), str(value), fill="black", font=small_font)

    draw.rectangle([left, 690, left + 26, 716], fill="#3b6ea5")
    draw.text(
        (left + 38, 690),
        f"{target.series_name} (USD millions)",
        fill="black",
        font=small_font,
    )
    draw.text(
        (left, 740),
        f"Mean of the {target.series_name} series across the four quarters: "
        f"{target.expected_mean:.2f}",
        fill="black",
        font=small_font,
    )
    draw.text(
        (left, 790),
        "Source: management presentation, prepared for the G4 vision probe.",
        fill="black",
        font=small_font,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_data_url(path: Path) -> str:
    """The data URL a multimodal endpoint receives for this image.

    The declared type follows the file suffix: a provider that trusts it
    must not be handed JPEG bytes labelled as PNG, which would turn an
    input-path mistake into a negative capability verdict.  ``main`` refuses
    a suffix absent from ``IMAGE_MIME`` before a client exists, so the one
    way this lookup can fail is a suffix admitted there without a declared
    type.
    """
    mime = IMAGE_MIME[path.suffix.lower()]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode(
        "ascii"
    )


# ----- prompts: the ADR 0089 split ------------------------------------


def _human(text: str, data_url: str | None) -> list[Any]:
    """One HumanMessage; multimodal when a data URL is given."""
    from langchain_core.messages import HumanMessage

    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if data_url is not None:
        parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return [HumanMessage(content=parts)]


# The kinds a call may be sent as. ``PROMPTS`` keys the questions, ``_record``
# keys which answer carrier a response lands in, and the control is decided by
# ``image_sent``; each spelling is therefore load-bearing in three places and is
# named once here.
KIND_PRESENCE: Final[str] = "presence"
KIND_MEANING: Final[str] = "meaning"
KIND_STRUCTURED: Final[str] = "structured_json"
KIND_TEXT_CONTROL: Final[str] = "text_control"

# The two kinds that ask about the chart *in the image*, so their answer is what
# ``chart_meaning_answer`` carries. The text-only control answers "ok" with no
# image in the request, and a control reply recorded as a chart answer would put
# a witness in the artifact that no image could have produced.
_CHART_ANSWER_KINDS: Final[tuple[str, ...]] = (KIND_MEANING, KIND_STRUCTURED)


def presence_prompt(target: Target, data_url: str) -> list[Any]:
    """Question (a): is a chart on this page at all?"""
    return _human(
        f"This image is {target.medium}. Does it contain a chart? "
        "Reply with exactly one word: yes or no.",
        data_url,
    )


def meaning_prompt(target: Target, data_url: str) -> list[Any]:
    """Question (b): what does the chart say, including the checkable mean?"""
    return _human(
        f"This image is {target.medium}. Read the chart on it and answer in "
        "a few sentences: the chart type, the name of the series it plots, "
        "the axis or category labels, the value of every bar, and the "
        f"arithmetic mean of the {target.series_name} series across all of "
        "its categories, stated to two decimal places.",
        data_url,
    )


def structured_prompt(target: Target, data_url: str) -> list[Any]:
    """Can T2e get a structured observation out of this provider?"""
    schema = ", ".join(
        [
            '"has_chart": true or false',
            '"chart_type": string',
            f'"series_name": string (the series is {target.series_name})',
            '"category_labels": array of strings',
            '"bar_values": array of numbers',
            '"mean_of_series": number',
            '"description": string',
        ]
    )
    return _human(
        f"This image is {target.medium}. Reply with ONLY one JSON object and "
        "nothing else - no prose, no code fence - with exactly these keys: "
        + schema
        + ". Read every value from the chart in the image itself.",
        data_url,
    )


PROMPTS: tuple[tuple[str, Any], ...] = (
    (KIND_PRESENCE, presence_prompt),
    (KIND_MEANING, meaning_prompt),
    (KIND_STRUCTURED, structured_prompt),
)


# ----- output handling (scrub first, always) --------------------------


class Scrubber:
    """Removes any credential value that could ride back in an error."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = tuple(sorted((s for s in secrets if len(s) >= 6), reverse=True))

    def clean(self, text: str) -> str:
        out = str(text)
        for secret in self._secrets:
            out = out.replace(secret, "[REDACTED]")
        # A URL that carried a token in its query must not survive either.
        return re.sub(r"\?[^\s\"']*", "?[REDACTED]", out)


def message_text(message: Any) -> str:
    """Flatten a langchain response to text, parts-list included."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = [
            item if isinstance(item, str) else str(item.get("text") or "")
            for item in content
            if isinstance(item, (str, dict))
        ]
        return "".join(chunks).strip()
    return str(content).strip()


def _usage_mapping(message: Any) -> dict[str, Any]:
    """Where a provider put its token counts, in whichever of its shapes.

    Three spellings are read - ``usage_metadata``, a nested
    ``response_metadata["token_usage"]``, and a named-tuple-like object with
    ``_asdict`` - and each miss falls to the next. An object that is none of
    those yields ``{}``, which the caller records as "no usage reported" rather
    than as a row of zeros.
    """
    raw: Any = getattr(message, "usage_metadata", None) or {}
    if not raw:
        metadata = getattr(message, "response_metadata", None) or {}
        raw = metadata.get("token_usage") or {}
    if not isinstance(raw, dict):
        as_dict = getattr(raw, "_asdict", None)
        raw = as_dict() if callable(as_dict) else {}
    return raw if isinstance(raw, dict) else {}


def message_usage(message: Any) -> dict[str, int | None] | None:
    """Provider-reported usage, or ``None`` when the provider reported none.

    Recorded as-is: a provider that omits usage leaves a visible null
    rather than an invented number, because that token cost is exactly
    what the budget decision is made from.
    """
    raw = _usage_mapping(message)

    def _num(*keys: str) -> int | None:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    usage = {
        "input_tokens": _num("input_tokens", "prompt_tokens"),
        "output_tokens": _num("output_tokens", "completion_tokens"),
        "total_tokens": _num("total_tokens"),
    }
    if all(value is None for value in usage.values()):
        return None
    return usage


# The classes are compared at the call sites that decide what happens next, so
# they are named once here rather than spelled at each branch.
AUTH_OR_PERMISSION = "auth_or_permission"
TRANSPORT = "transport"
IMAGE_INPUT_REJECTED = "image_input_rejected"
PROVIDER_REFUSAL = "provider_refusal"
RATE_LIMITED = "rate_limited"
BAD_REQUEST = "bad_request"
PROVIDER_ERROR = "provider_error"

# A modality rejection has to name both a modality and a refusal. Matching on
# the bare word "image" would classify "this endpoint does not support
# response_format" as an image rejection, and ``supports_image_input`` is the
# verdict the per-run vision budget is set from: a loose match here turns an
# unrelated 400 into a claim about what the provider can see.
_MODALITY_CUES = ("image", "vision", "multimodal", "modalit", "picture", "photo")
_REFUSAL_CUES = (
    "does not support",
    "do not support",
    "not supported",
    "not accept",
    "unsupported",
    "cannot process",
    "unable to",
    "rejected",
    "invalid url",
)


def _refuses_image_input(text: str) -> bool:
    """Whether provider text positively refuses an image input."""
    return any(cue in text for cue in _MODALITY_CUES) and any(
        cue in text for cue in _REFUSAL_CUES
    )


@dataclass(frozen=True, slots=True)
class _ErrorRule:
    """One row of the classification table: a class and the evidence for it.

    A status code is the provider's own answer and is tried before the prose,
    but the two are alternatives rather than stages: an ``Unauthorized`` 401 and
    an ``Invalid API key`` without a status are the same failure, and a chain of
    ``if`` statements kept them in one order only by accident.
    """

    kind: str
    statuses: frozenset[int] = frozenset()
    name_cues: tuple[str, ...] = ()
    text_cues: tuple[str, ...] = ()
    modality: bool = False

    def matches(self, name: str, text: str, status: Any) -> bool:
        if self.modality:
            return _refuses_image_input(text)
        return (
            status in self.statuses
            or any(cue in name for cue in self.name_cues)
            or any(cue in text for cue in self.text_cues)
        )


# Order is the adjudication: the modality question is asked only after auth and
# transport had their turn, and it is answered before 429/400 can absorb a
# provider that refuses images with a plain 400.
_ERROR_RULES: Final[tuple[_ErrorRule, ...]] = (
    _ErrorRule(
        AUTH_OR_PERMISSION,
        frozenset({401, 403}),
        text_cues=("invalid_api_key", "unauthor"),
    ),
    _ErrorRule(TRANSPORT, name_cues=("timeout",), text_cues=("timeout",)),
    _ErrorRule(
        TRANSPORT,
        name_cues=("connection", "remotedisconnected"),
        text_cues=("connect",),
    ),
    _ErrorRule(IMAGE_INPUT_REJECTED, modality=True),
    _ErrorRule(PROVIDER_REFUSAL, text_cues=("content policy", "safety")),
    _ErrorRule(RATE_LIMITED, frozenset({429}), text_cues=("rate limit",)),
    _ErrorRule(BAD_REQUEST, frozenset({400}), text_cues=("bad request",)),
)


def _error_signals(exc: BaseException) -> tuple[str, str, Any]:
    """The three readings a provider failure can be recognised from."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return type(exc).__name__.lower(), str(exc).lower(), status


def classify_error(exc: BaseException) -> str:
    """One error class per failure, so a verdict never rests on prose."""
    name, text, status = _error_signals(exc)
    for rule in _ERROR_RULES:
        if rule.matches(name, text, status):
            return rule.kind
    # EXIT: unclassified is its own named class, so a caller never reads an
    # unknown failure as one of the verdict-bearing ones above.
    return PROVIDER_ERROR


# ----- the probe ------------------------------------------------------


class Probe:
    """Sends the calls under the hard cap and records what came back."""

    def __init__(
        self, client: Any, image: Path, scrubber: Scrubber, target: Target
    ) -> None:
        from PIL import Image

        self._client = client
        self._target = target
        self.data_url = image_data_url(image)
        self._scrubber = scrubber
        with Image.open(image) as opened:
            self.dims = (int(opened.width), int(opened.height))
        self.image_bytes = image.stat().st_size
        self.calls: list[dict[str, Any]] = []

    @property
    def target(self) -> Target:
        return self._target

    @property
    def used(self) -> int:
        return len(self.calls)

    def has_budget(self) -> bool:
        return self.used < MAX_REAL_CALLS

    def invoke(self, kind: str, messages: list[Any]) -> dict[str, Any]:
        """One real API call, with at most one retry and only on transport."""
        if not self.has_budget():
            raise BudgetExhausted(
                f"hard cap of {MAX_REAL_CALLS} real API calls reached"
            )
        started = time.monotonic()
        try:
            message = self._client.invoke(messages)
        except Exception as exc:  # provider boundary: classify, never swallow
            record = self._record(
                kind,
                latency_ms=round((time.monotonic() - started) * 1000),
                response_char_count=0,
                text=None,
                usage=None,
                error_class=classify_error(exc),
            )
            self.calls.append(record)
            print(
                f"probe: call {record['seq']} ({kind}) failed "
                f"{record['error_class']}: "
                f"{self._scrubber.clean(str(exc))[:300]}",
                file=sys.stderr,
            )
            if record["error_class"] == AUTH_OR_PERMISSION:
                raise Blocked(self._scrubber.clean(str(exc))) from exc
            if record["error_class"] == TRANSPORT and self.has_budget():
                # The single permitted retry, for a transport error only.
                print(f"probe: retrying {kind} once (transport)", file=sys.stderr)
                return self.invoke(kind, messages)
            return record

        text = self._scrubber.clean(message_text(message))
        record = self._record(
            kind,
            latency_ms=round((time.monotonic() - started) * 1000),
            response_char_count=len(text),
            text=text,
            usage=message_usage(message),
            error_class=None,
        )
        self.calls.append(record)
        return record

    def _record(
        self,
        kind: str,
        *,
        latency_ms: int | None,
        response_char_count: int,
        text: str | None,
        usage: dict[str, int | None] | None,
        error_class: str | None,
    ) -> dict[str, Any]:
        image_sent = kind != KIND_TEXT_CONTROL
        width, height = self.dims
        return {
            "seq": len(self.calls) + 1,
            "prompt_kind": kind,
            "image_width": width if image_sent else None,
            "image_height": height if image_sent else None,
            "image_bytes": self.image_bytes if image_sent else None,
            "latency_ms": latency_ms,
            "response_char_count": response_char_count,
            "chart_presence_answer": text if kind == KIND_PRESENCE else None,
            "chart_meaning_answer": text if kind in _CHART_ANSWER_KINDS else None,
            "usage": usage,
            "error_class": error_class,
        }


# ----- verdicts -------------------------------------------------------


def saw_the_image(text: str | None, expected_mean: float) -> bool:
    """The witness: only the image carries the numbers this mean averages.

    Compared with a half-unit tolerance rather than a string match, so an
    answer that rounds the mean correctly still counts while no value plotted
    on the chart can be mistaken for it.
    """
    if not text:
        return False
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    return any(
        abs(float(raw.replace(",", "")) - expected_mean) <= 0.5 for raw in numbers
    )  # EXIT: no number in the answer, so the image was never read


def structured_ok(text: str | None) -> bool:
    """Is a structured observation obtainable from this provider?

    Bare JSON, fenced JSON, or an object embedded in prose all count,
    because T2e can ask for any of them; a missing key does not, because
    the observation contract needs every field. The wider match is not
    padding: a model with an inline reasoning channel prefixes its
    answer with that channel, so a strict ``json.loads(content)`` would
    report a perfectly good observation as malformed — which is exactly
    the `vision_empty_text` trap T2e has to avoid.
    """
    if not text:
        return False
    candidates = [text]
    fenced = re.search(r"\{.*\}", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and all(k in parsed for k in STRUCTURED_KEYS):
            return True
    return False


def verdicts(calls: list[dict[str, Any]], target: Target) -> dict[str, bool]:
    meaning_text = next(
        (c["chart_meaning_answer"] for c in calls if c["prompt_kind"] == KIND_MEANING),
        None,
    )
    image_rejected = any(
        c["error_class"] == IMAGE_INPUT_REJECTED for c in calls
    )
    structured_text = next(
        (
            c["chart_meaning_answer"]
            for c in calls
            if c["prompt_kind"] == KIND_STRUCTURED
        ),
        None,
    )
    return {
        "supports_image_input": (not image_rejected)
        and saw_the_image(meaning_text, target.expected_mean),
        "structured_output_ok": structured_ok(structured_text),
        "usage_reported": any(c["usage"] for c in calls),
    }


def run_probe(probe: Probe) -> None:
    """Walk the three prompts once each, then one control if useful.

    A negative answer is data, so a failed call never aborts the plan;
    only an auth/permission failure (``Blocked``) or the hard cap does.
    """
    for kind, builder in PROMPTS:
        try:
            probe.invoke(kind, builder(probe.target, probe.data_url))
        except BudgetExhausted:
            return
    image_calls = [c for c in probe.calls if c["prompt_kind"] != KIND_TEXT_CONTROL]
    all_failed = bool(image_calls) and all(c["error_class"] for c in image_calls)
    if all_failed and probe.has_budget():
        # Separates "cannot see" from "cannot answer at all" - the budget
        # decision must not conflate them.
        probe.invoke(KIND_TEXT_CONTROL, _human("Reply with exactly one word: ok", None))


# ----- entry point -----------------------------------------------------


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe fin's configured vision provider for ADR 0089 "
        "chart presence/meaning ability and per-call cost.",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="build the throwaway page image and exit; makes no API call",
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help="probe this image file instead of the throwaway page; needs "
        "--series and --mean, because a witness that does not belong to this "
        "file proves nothing",
    )
    parser.add_argument(
        "--series",
        default=None,
        help="series name read out of --image, used in the meaning prompts",
    )
    parser.add_argument(
        "--mean",
        type=float,
        default=None,
        help="arithmetic mean of that series, computed outside this script: "
        "an answer counts as having seen the image only if it carries it",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"artifact path (default: {ARTIFACT_PATH}, and required with "
        "--image so a file probe cannot overwrite the page record",
    )
    args = parser.parse_args(argv)
    if args.image is not None and (args.series is None or args.mean is None):
        parser.error("--image needs --series and --mean to carry a witness")
    if args.image is None and (args.series is not None or args.mean is not None):
        parser.error("--series and --mean describe --image; pass them together")
    if args.image is not None and args.out is None:
        parser.error(
            "--image needs --out: a probe of some other file must not "
            f"overwrite the recorded page artifact at {ARTIFACT_PATH}"
        )
    if args.out is None:
        args.out = ARTIFACT_PATH
    return args


def target_for(args: argparse.Namespace) -> Target:
    """The subject under probe: the throwaway page, or the named file."""
    if args.image is None:
        return Target()
    return Target(
        series_name=args.series,
        medium=ATTACHED_MEDIUM,
        expected_mean=args.mean,
    )


def summary_line(
    config: dict[str, Any],
    calls: list[dict[str, Any]],
    verdict: dict[str, bool],
    target: Target,
    artifact: Path,
) -> str:
    """One grep-able line: the three verdicts plus the witness result."""
    meaning_text = next(
        (c["chart_meaning_answer"] for c in calls if c["prompt_kind"] == KIND_MEANING),
        None,
    )
    result = "ok" if verdict["supports_image_input"] else "negative"
    return (
        f"G4_VISION_PROBE result={result} "
        f"model={config['model_slug']} "
        f"base_host={config['base_url_host_only']} calls={len(calls)} "
        "supports_image_input="
        f"{str(verdict['supports_image_input']).lower()} "
        "structured_output_ok="
        f"{str(verdict['structured_output_ok']).lower()} "
        "usage_reported="
        f"{str(verdict['usage_reported']).lower()} "
        f"mean_expected={target.expected_mean:.2f} "
        "mean_answered="
        f"{str(saw_the_image(meaning_text, target.expected_mean)).lower()} "
        f"artifact={artifact}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    loaded = None
    if not args.render_only:
        loaded = apply_operator_settings()
        if not os.environ.get("FIN_MODEL", "").strip():
            print(
                "probe: FIN_MODEL is required; set it in the process environment "
                "or configure llm.model in ~/.fin/settings.yaml",
                file=sys.stderr,
            )
            return 2
    target = target_for(args)
    if args.image is None:
        image_path = SCRATCH_DIR / "probe_page.png"
        render_probe_page(image_path, target)
    else:
        image_path = args.image
        if not image_path.is_file():
            print(f"probe: no such image: {image_path}", file=sys.stderr)
            return 2  # EXIT: a missing file is a usage error, not a verdict
        if image_path.suffix.lower() not in IMAGE_MIME:
            print(
                f"probe: unsupported image suffix "
                f"{image_path.suffix or '(none)'} on {image_path}; this probe "
                f"declares a type for {', '.join(sorted(IMAGE_MIME))} only",
                file=sys.stderr,
            )
            # EXIT: bytes with no honest declared type must never reach the
            # provider, where a rejection of this script's own labelling
            # mistake would be recorded as supports_image_input=false.
            return 2

    if args.render_only:
        print(
            f"G4_VISION_PROBE result=render_only image={image_path} "
            f"bytes={image_path.stat().st_size} "
            f"expected_mean={target.expected_mean:.2f} calls=0"
        )
        return 0

    if loaded is None and not os.environ.get(host_compile.FIN_API_KEY_ENV, "").strip():
        print(
            "probe: warning: ~/.fin/settings.yaml is missing and "
            f"{host_compile.FIN_API_KEY_ENV} is unset; "
            "fin's placeholder key will be rejected by the provider",
            file=sys.stderr,
        )
    client, api_key = build_client()
    config = config_block(client)
    scrubber = Scrubber((api_key, "sk-no-key-required"))

    probe = Probe(client, image_path, scrubber, target)
    blocked = False
    try:
        run_probe(probe)
    except Blocked as exc:
        print(
            f"probe: BLOCKED on auth/permission, no further calls: {exc}",
            file=sys.stderr,
        )
        blocked = True

    calls = probe.calls
    verdict = verdicts(calls, target)
    if blocked:
        status = 3  # EXIT: a run stopped on auth yields no capability verdict
    else:
        status = 0 if verdict["supports_image_input"] else 1
    payload = {
        "config": config,
        "image": {
            "path": str(image_path),
            "width": probe.dims[0],
            "height": probe.dims[1],
            "bytes": probe.image_bytes,
        },
        "series_name": target.series_name,
        "expected_mean": target.expected_mean,
        "calls": calls,
        "total_calls": len(calls),
        "verdicts": verdict,
    }
    write_artifact(args.out, payload)
    print(summary_line(config, calls, verdict, target, args.out))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
