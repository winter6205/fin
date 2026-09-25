"""G4 T2a probe: what can fin's CONFIGURED vision provider actually see?

ADR 0089 assigns the configured provider — not a local VLM and not a
picture classifier — the job of deciding, for every rendered full page,
both whether a chart is present and what it says. fin has no vision
client yet (T2e owns that production seam), so the per-run visual-call
budget T2a must decide rests on evidence nobody has collected: does the
configured model accept an image at all, does it answer correctly, and
what does one call cost in latency and tokens?

This script is that evidence. It is a probe, not a seam:

* it builds ONE throwaway chart image with PIL in ``/tmp/g4_vision_probe/``
  (never a gold fixture — the measurement must be independent of the
  suites that later consume the decision);
* provider config comes from fin's own helper
  (:func:`fin.host.compile._build_default_model`), so the probe cannot
  drift from the product boot path and re-implements no provider
  selection, no hardcoded model or base URL;
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

    cd <dir whose .env holds FIN_MODEL / FIN_BASE_URL / FIN_API_KEY>
    uv run python <path>/scripts/g4_vision_probe.py
    uv run python <path>/scripts/g4_vision_probe.py --render-only

Exit codes: ``0`` the probe ran and image input is confirmed; ``1`` the
probe ran but a verdict is negative (that finding IS a result, not a
crash); ``2`` argparse usage error; ``3`` blocked on an auth/permission
failure, where no further call is made.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin.host import compile as host_compile  # noqa: E402
from fin.host.operator_env import apply_dotenv, find_env_file  # noqa: E402

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
MEAN_TEXT = f"{EXPECTED_MEAN:.2f}"

ENV_NAMES_READ = ("FIN_MODEL", "FIN_BASE_URL", host_compile.FIN_API_KEY_ENV)

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


def build_client() -> Any:
    """Return fin's configured ``ChatOpenAI`` for this machine.

    Delegates entirely to ``_build_default_model`` so the probe measures
    the same model, base URL, key resolution (including the loud
    ``sk-no-key-required`` placeholder), ``timeout=120.0`` and
    ``max_retries=0`` the product boots with.
    """
    return host_compile._build_default_model(
        model_name=None, base_url=None, env=None
    )


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


def render_probe_page(path: Path) -> tuple[int, int]:
    """Draw a page-like PNG with one bar chart; return its dimensions.

    The chart is deliberately the one thing a text-only model cannot
    guess: labelled axes, four bars, known values, stated mean.
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
        f"Quarterly {SERIES_NAME.lower()} by segment (USD millions, unaudited)",
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
        f"{SERIES_NAME} (USD millions)",
        fill="black",
        font=small_font,
    )
    draw.text(
        (left, 740),
        f"Mean of the {SERIES_NAME} series across the four quarters: {MEAN_TEXT}",
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
    with Image.open(path) as saved:
        return int(saved.width), int(saved.height)


def image_data_url(path: Path) -> str:
    """The data URL a multimodal endpoint receives for this image."""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(
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


def presence_prompt(data_url: str) -> list[Any]:
    """Question (a): is a chart on this page at all?"""
    return _human(
        "This image is one rendered page of a financial document. Does "
        "this page contain a chart? Reply with exactly one word: yes or "
        "no.",
        data_url,
    )


def meaning_prompt(data_url: str) -> list[Any]:
    """Question (b): what does the chart say, including the checkable mean?"""
    return _human(
        "This image is one rendered page of a financial document. Read "
        "the chart on it and answer in a few sentences: the chart type, "
        "the name of the series it plots, the axis or category labels, "
        f"the value of every bar, and the arithmetic mean of the "
        f"{SERIES_NAME} series across all quarters stated to two decimal "
        "places.",
        data_url,
    )


def structured_prompt(data_url: str) -> list[Any]:
    """Can T2e get a structured observation out of this provider?"""
    schema = ", ".join(
        [
            '"has_chart": true or false',
            '"chart_type": string',
            f'"series_name": string (the series is {SERIES_NAME})',
            '"category_labels": array of strings',
            '"bar_values": array of numbers',
            '"mean_of_series": number',
            '"description": string',
        ]
    )
    return _human(
        "This image is one rendered page of a financial document. Reply "
        "with ONLY one JSON object and nothing else - no prose, no code "
        "fence - with exactly these keys: "
        + schema
        + ". Read every value from the chart in the image itself.",
        data_url,
    )


PROMPTS: tuple[tuple[str, Any], ...] = (
    ("presence", presence_prompt),
    ("meaning", meaning_prompt),
    ("structured_json", structured_prompt),
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


def message_usage(message: Any) -> dict[str, int | None] | None:
    """Provider-reported usage, or ``None`` when the provider reported none.

    Recorded as-is: a provider that omits usage leaves a visible null
    rather than an invented number, because that token cost is exactly
    what the budget decision is made from.
    """
    raw: Any = getattr(message, "usage_metadata", None) or {}
    if not raw:
        metadata = getattr(message, "response_metadata", None) or {}
        raw = metadata.get("token_usage") or {}
    if not isinstance(raw, dict):
        as_dict = getattr(raw, "_asdict", None)
        raw = as_dict() if callable(as_dict) else {}
    if not isinstance(raw, dict):
        return None

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


def classify_error(exc: BaseException) -> str:
    """One error class per failure, so a verdict never rests on prose."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (401, 403) or "invalid_api_key" in text or "unauthor" in text:
        return "auth_or_permission"
    if "timeout" in name or "timeout" in text:
        return "transport"
    if "connection" in name or "connect" in text or "remotedisconnected" in name:
        return "transport"
    if any(
        marker in text
        for marker in (
            "image",
            "vision",
            "multimodal",
            "modalit",
            "unsupported file",
            "invalid url",
        )
    ):
        return "image_input_rejected"
    if "content policy" in text or "safety" in text:
        return "provider_refusal"
    if status == 429 or "rate limit" in text:
        return "rate_limited"
    if status == 400 or "bad request" in text:
        return "bad_request"
    return "provider_error"


# ----- the probe ------------------------------------------------------


class Probe:
    """Sends the calls under the hard cap and records what came back."""

    def __init__(self, client: Any, image: Path, scrubber: Scrubber) -> None:
        from PIL import Image

        self._client = client
        self.data_url = image_data_url(image)
        self._scrubber = scrubber
        with Image.open(image) as opened:
            self.dims = (int(opened.width), int(opened.height))
        self.image_bytes = image.stat().st_size
        self.calls: list[dict[str, Any]] = []

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
            if record["error_class"] == "auth_or_permission":
                raise Blocked(self._scrubber.clean(str(exc))) from exc
            if record["error_class"] == "transport" and self.has_budget():
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
        image_sent = kind != "text_control"
        width, height = self.dims
        return {
            "seq": len(self.calls) + 1,
            "prompt_kind": kind,
            "image_width": width if image_sent else None,
            "image_height": height if image_sent else None,
            "image_bytes": self.image_bytes if image_sent else None,
            "latency_ms": latency_ms,
            "response_char_count": response_char_count,
            "chart_presence_answer": text if kind == "presence" else None,
            "chart_meaning_answer": text if kind != "presence" else None,
            "usage": usage,
            "error_class": error_class,
        }


# ----- verdicts -------------------------------------------------------


def saw_the_image(text: str | None) -> bool:
    """The witness: only the rendered page carries the mean this number."""
    return bool(text) and MEAN_TEXT in text


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


def verdicts(calls: list[dict[str, Any]]) -> dict[str, bool]:
    meaning_text = next(
        (c["chart_meaning_answer"] for c in calls if c["prompt_kind"] == "meaning"),
        None,
    )
    image_rejected = any(
        c["error_class"] == "image_input_rejected" for c in calls
    )
    structured_text = next(
        (
            c["chart_meaning_answer"]
            for c in calls
            if c["prompt_kind"] == "structured_json"
        ),
        None,
    )
    return {
        "supports_image_input": (not image_rejected) and saw_the_image(meaning_text),
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
            probe.invoke(kind, builder(probe.data_url))
        except BudgetExhausted:
            return
    image_calls = [c for c in probe.calls if c["prompt_kind"] != "text_control"]
    all_failed = bool(image_calls) and all(c["error_class"] for c in image_calls)
    if all_failed and probe.has_budget():
        # Separates "cannot see" from "cannot answer at all" - the budget
        # decision must not conflate them.
        probe.invoke("text_control", _human("Reply with exactly one word: ok", None))


# ----- entry point -----------------------------------------------------


def write_artifact(payload: dict[str, Any]) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(
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
        "--dotenv-start",
        type=Path,
        default=Path.cwd(),
        help="directory to search for the operator .env (default: cwd), "
        "resolved by fin.host.operator_env.find_env_file",
    )
    return parser.parse_args(argv)


def summary_line(
    config: dict[str, Any], calls: list[dict[str, Any]], verdict: dict[str, bool]
) -> str:
    """One grep-able line: the three verdicts plus the witness result."""
    meaning_text = next(
        (c["chart_meaning_answer"] for c in calls if c["prompt_kind"] == "meaning"),
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
        f"mean_expected={MEAN_TEXT} "
        f"mean_answered={str(saw_the_image(meaning_text)).lower()} "
        f"artifact={ARTIFACT_PATH}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    image_path = SCRATCH_DIR / "probe_page.png"
    width, height = render_probe_page(image_path)
    if args.render_only:
        print(
            f"G4_VISION_PROBE result=render_only image={image_path} "
            f"width={width} height={height} "
            f"bytes={image_path.stat().st_size} expected_mean={MEAN_TEXT} calls=0"
        )
        return 0

    if find_env_file(start=args.dotenv_start) is None:
        print(
            "probe: warning: no operator .env found from "
            f"{args.dotenv_start}; the configured key env name "
            f"({host_compile.FIN_API_KEY_ENV}) may be unset and "
            "fin's placeholder key will be rejected by the provider",
            file=sys.stderr,
        )
    apply_dotenv(start=args.dotenv_start)
    client = build_client()
    config = config_block(client)
    scrubber = Scrubber(
        (
            host_compile._read_env(None, host_compile.FIN_API_KEY_ENV) or "",
            "sk-no-key-required",
        )
    )

    probe = Probe(client, image_path, scrubber)
    try:
        run_probe(probe)
        status = 0 if verdicts(probe.calls)["supports_image_input"] else 1
    except Blocked as exc:
        print(
            f"probe: BLOCKED on auth/permission, no further calls: {exc}",
            file=sys.stderr,
        )
        status = 3

    calls = probe.calls
    verdict = verdicts(calls)
    write_artifact(
        {
            "config": config,
            "calls": calls,
            "total_calls": len(calls),
            "verdicts": verdict,
        }
    )
    print(summary_line(config, calls, verdict))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
