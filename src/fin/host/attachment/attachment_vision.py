"""fin's dedicated vision port: one provider call over one rendered image.

ADR 0089 assigns the configured provider — not a local VLM and not a picture
classifier — the job of deciding what a rendered page or crop shows. This
module owns exactly that one call and nothing else:

* :class:`VisionPort` is the single-method seam the attachment-preparation and
  observation sides inject, so a run never reaches for the conversational
  model port when it needs a page inspected;
* :class:`ChatModelVisionAdapter` is the default adapter, built from the same
  three settings the host already uses for its chat model;
* the two outward failures are typed and distinct — a provider that could not
  be reached (:data:`VISION_CALL_FAILED`) is not a provider that answered with
  nothing usable (:data:`VISION_EMPTY_TEXT`), and neither becomes an empty
  success.

Deliberately absent: the per-run call budget and its retry reserve (owned by
`fin.host.attachment.attachment_budgets` and the preparation job), the coverage
comparison and observation persistence, and any monetary ceiling. The product
decision defers the ceiling, so this port records what the provider reported
and enforces nothing.

Persistence contract: :class:`VisionCallResult` echoes the
:class:`VisionTarget` it was called with, so a caller can bind an observation
to its original attachment version, physical page, and crop bounds without
re-deriving them, and carries the model id that actually answered.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Final, Mapping, Protocol, runtime_checkable

from fin.host.compile import DEFAULT_BASE_URL, FIN_API_KEY_ENV


# ----- configuration (the host's own settings, reused) ----------------


# The env-var NAMES. Only the three settings the host's chat model already
# reads: no vision-specific credential, model, or endpoint setting exists.
FIN_MODEL_ENV: Final[str] = "FIN_MODEL"
FIN_BASE_URL_ENV: Final[str] = "FIN_BASE_URL"


VISION_CALL_FAILED: Final[str] = "vision_call_failed"
VISION_EMPTY_TEXT: Final[str] = "vision_empty_text"

DEFAULT_MEDIA_TYPE: Final[str] = "image/png"
SUPPORTED_MEDIA_TYPES: Final[frozenset[str]] = frozenset({"image/png", "image/jpeg"})

# The first-preparation scan asks ADR 0089's two questions in one call: is a
# chart or table present, and what does it say. A crop inspection overrides it
# with the caller's question.
PAGE_SCAN_PROMPT: Final[str] = (
    "Inspect this rendered page of an uploaded document. State whether any "
    "chart, plot, or table is present. For each one, report its title, axes "
    "and units, every series, every category, and each plotted value. Name "
    "what is not legible instead of guessing at it."
)

_TOKEN_KEY_ALIASES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("input_tokens", ("input_tokens", "prompt_tokens")),
    ("output_tokens", ("output_tokens", "completion_tokens")),
    ("total_tokens", ("total_tokens",)),
)

_COST_KEYS: Final[tuple[str, ...]] = ("cost", "total_cost")
_CURRENCY_KEYS: Final[tuple[str, ...]] = ("currency", "cost_currency")


class VisionPortError(Exception):
    """Base class for vision-port failures."""


class VisionErrorClass(StrEnum):
    """The adapter's own verdict on one provider failure.

    The preparation side holds exactly one reserved full-page retry per run,
    and the spec lets only an adapter-classified retryable transport failure
    spend it. The adapter, not the caller's prose-reading, owns that call — so
    the verdict travels as data on the error. This mirrors the taxonomy the
    G4 probe proved against the live provider (`scripts/g4_vision_probe.py`).
    """

    TRANSPORT = "transport"
    PERMANENT = "permanent"


# Auth and permission are adjudicated before the transport cues: a refusal
# must never be re-read as a dropped packet, and a retry cannot fix it.
_REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_REFUSAL_CUES: Final[tuple[str, ...]] = (
    "invalid_api_key",
    "invalid api key",
    "unauthor",
    "authentication",
    "permission",
    "forbidden",
    "quota",
)
# Substrings matched against the exception class name; "connect" covers
# ConnectionError, httpx ConnectError, openai APIConnectionError, and
# httpcore RemoteDisconnected (spec 57's retryable-transport shapes).
_TRANSPORT_NAME_CUES: Final[tuple[str, ...]] = (
    "timeout",
    "connect",
    "socket",
    "dns",
    "gaierror",
    "network",
)
_TRANSPORT_TEXT_CUES: Final[tuple[str, ...]] = (
    "timeout",
    "timed out",
    "connect",
    "getaddrinfo",
    "name resolution",
    "network",
    "stream was reset",
    "stream ended",
)


def _error_signals(exc: BaseException) -> tuple[str, str, Any]:
    """The three readings a provider failure can be recognised from.

    Some SDKs put the HTTP status on the exception, some only on a wrapped
    ``response``; either way the prose never leaves the classifier.
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return type(exc).__name__.lower(), str(exc).lower(), status


def _classify_provider_error(exc: BaseException) -> VisionErrorClass:
    """One class per failure; unclassifiable is PERMANENT, never a retry.

    The reserve is scarce, so a nameless fault stays non-retryable
    (conservative default) rather than being guessed into TRANSPORT. 429 is
    deliberately not retryable here: a rate limit is the provider refusing on
    load, not a transport fault, and the run's reserve does not buy a wait.
    """
    name, text, status = _error_signals(exc)
    if status in _REFUSAL_STATUSES or any(
        cue in name or cue in text for cue in _REFUSAL_CUES
    ):
        return VisionErrorClass.PERMANENT
    if any(cue in name for cue in _TRANSPORT_NAME_CUES) or any(
        cue in text for cue in _TRANSPORT_TEXT_CUES
    ):
        return VisionErrorClass.TRANSPORT
    if isinstance(status, int) and not isinstance(status, bool) and 400 <= status < 500:
        # Any other 4xx: the provider understood and rejected the request;
        # an identical retry answers the same way.
        return VisionErrorClass.PERMANENT
    return VisionErrorClass.PERMANENT


class VisionCallFailedError(VisionPortError):
    """The provider could not be reached or refused the call.

    Carries the exception class name only: a provider message can echo a
    request line, and a credential never belongs in an outward message. The
    original stays available as ``__cause__`` for the caller's own logging.

    ``error_class`` is this adapter's own retry verdict, set at the raise
    site from the underlying exception — it is what authorises (or forbids)
    ``retry=True`` against the run's reserved full-page retry.
    """

    code = VISION_CALL_FAILED

    def __init__(
        self,
        message: str,
        *,
        error_class: VisionErrorClass = VisionErrorClass.PERMANENT,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class

    @property
    def retryable(self) -> bool:
        return self.error_class is VisionErrorClass.TRANSPORT


class VisionEmptyTextError(VisionPortError):
    """The provider answered with nothing usable — empty, whitespace-only,
    or a response shape this port cannot read as text.

    Separate from a transport fault on purpose: one is retriable under the
    run's reserved full-page retry, the other is malformed output that must
    not be retried into a pass.
    """

    code = VISION_EMPTY_TEXT
    retryable: bool = False


# ----- value types ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisionModelConfig:
    """The resolved model identity the adapter was configured with."""

    model_id: str
    base_url: str


@dataclass(frozen=True, slots=True)
class VisionCrop:
    """Normalized page-image bounds of a crop inspection.

    Normalized (0..1) rather than pixels: the observation must restate the
    same region against a stored page image that may have been re-rendered at
    another resolution.
    """

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        for name, value in (
            ("left", self.left),
            ("top", self.top),
            ("right", self.right),
            ("bottom", self.bottom),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"crop {name} must be a number")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"crop {name} must be within 0 and 1")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("crop bounds must increase")


@dataclass(frozen=True, slots=True)
class VisionTarget:
    """What is being looked at: the identity a caller builds provenance from.

    ``crop is None`` means the whole rendered page. This carries identity
    only — the stored page-image hash and observation version belong to the
    persistence side.
    """

    attachment_id: str
    version: str
    page: int
    crop: VisionCrop | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("attachment_id", self.attachment_id),
            ("version", self.version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.page, int) or isinstance(self.page, bool):
            raise ValueError("page must be an integer")
        if self.page < 1:
            raise ValueError("page is 1-based and must be positive")


@dataclass(frozen=True, slots=True)
class VisionUsage:
    """Provider-reported token usage; an unreported field stays ``None``."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class VisionCost:
    """Provider-reported cost. Constructed only from a reported value."""

    amount: Decimal
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class VisionCallResult:
    """One vision call's outcome, ready to become a persisted observation.

    ``cost is None`` means the provider reported no cost — unpriced, not
    free. A reported zero arrives as ``VisionCost(amount=Decimal("0"))`` and
    is a different fact.
    """

    text: str
    target: VisionTarget
    model_id: str
    media_type: str
    usage: VisionUsage | None = None
    cost: VisionCost | None = None


# ----- the port -------------------------------------------------------


@runtime_checkable
class VisionPort(Protocol):
    """One vision call over one rendered image."""

    def inspect(
        self,
        *,
        image: bytes,
        target: VisionTarget,
        media_type: str = DEFAULT_MEDIA_TYPE,
        question: str | None = None,
    ) -> VisionCallResult: ...


# ----- configuration resolution ---------------------------------------


def _trimmed(raw: str | None) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def resolve_vision_model_config(
    *, env: Mapping[str, str] | None = None
) -> VisionModelConfig:
    """The host's chat-model settings, resolved when the port is used.

    ``env is None`` reads the process environment. A missing model is a
    permanent vision-call failure; the port does not invent a model identity.
    """
    import os

    source: Mapping[str, str] = os.environ if env is None else env
    model_id = _trimmed(source.get(FIN_MODEL_ENV))
    if not model_id:
        raise VisionCallFailedError(
            f"vision client is unavailable: {FIN_MODEL_ENV} is unset"
        )
    return VisionModelConfig(
        model_id=model_id,
        base_url=_trimmed(source.get(FIN_BASE_URL_ENV)) or DEFAULT_BASE_URL,
    )


# ----- the default adapter -------------------------------------------


class ChatModelVisionAdapter:
    """The production :class:`VisionPort` over the configured chat model.

    Configuration and the client are resolved lazily on first inspection, so
    booting a graph without invoking vision does not require vision settings
    or pay for the provider import. One adapter never holds two identities.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        client_factory: Callable[[VisionModelConfig], Any] | None = None,
    ) -> None:
        self._config: VisionModelConfig | None = None
        self._env = env
        self._client_factory = client_factory
        self._client: Any = None

    def inspect(
        self,
        *,
        image: bytes,
        target: VisionTarget,
        media_type: str = DEFAULT_MEDIA_TYPE,
        question: str | None = None,
    ) -> VisionCallResult:
        parts = _content_parts(
            image=image, target=target, media_type=media_type, question=question
        )
        config = self._resolve_config()
        message = self._request(parts)
        text = _response_text(message)
        usage = _response_usage(message)
        return VisionCallResult(
            text=text,
            target=target,
            model_id=config.model_id,
            media_type=media_type,
            usage=usage,
            cost=_response_cost(message),
        )

    # -- provider leg

    def _request(self, parts: list[str | dict[str, Any]]) -> Any:
        try:
            client = self._ensure_client()
            return client.invoke([_human_message(parts)])
        except VisionPortError:
            raise
        except Exception as exc:  # any provider fault becomes one typed exit
            raise VisionCallFailedError(
                f"vision provider call failed: {type(exc).__name__}",
                error_class=_classify_provider_error(exc),
            ) from exc

    def _ensure_client(self) -> Any:
        if self._client is None:
            config = self._resolve_config()
            if self._client_factory is not None:
                self._client = self._client_factory(config)
            else:
                self._client = _build_chat_model_client(config, self._env)
        return self._client

    def _resolve_config(self) -> VisionModelConfig:
        if self._config is None:
            self._config = resolve_vision_model_config(env=self._env)
        return self._config


def _build_chat_model_client(
    config: VisionModelConfig, env: Mapping[str, str] | None
) -> Any:
    """The provider client, from :func:`resolve_vision_model_config` only.

    The timeout and retry count match the host's own model wiring; the rest
    of that wiring stays there, because the reasoning-preserve channel a chat
    reply needs is not part of a single-image inspection.

    ``VisionCallFailedError`` on an unset key rather than a placeholder value
    or a silent third-party fallback: the placeholder the host's chat model
    uses is that module's private detail, and reading an unrelated
    provider-side variable would spend a credential this port was never
    granted. A keyless local gateway sets an explicit dummy value instead.
    """
    import os

    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    source: Mapping[str, str] = os.environ if env is None else env
    api_key = _trimmed(source.get(FIN_API_KEY_ENV))
    if not api_key:
        raise VisionCallFailedError(
            f"vision client is unavailable: {FIN_API_KEY_ENV} is unset"
        )
    return ChatOpenAI(
        model=config.model_id,
        base_url=config.base_url,
        api_key=SecretStr(api_key),
        timeout=120.0,
        max_retries=0,
    )


# ----- request and response mapping -----------------------------------


def _content_parts(
    *,
    image: bytes,
    target: VisionTarget,
    media_type: str,
    question: str | None,
) -> list[str | dict[str, Any]]:
    """Validated input becomes the multimodal parts one call carries."""
    if not isinstance(image, (bytes, bytearray)) or not image:
        raise ValueError("image must be non-empty rendered bytes")
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(
            f"unsupported media type {media_type!r}; "
            f"expected one of {sorted(SUPPORTED_MEDIA_TYPES)}"
        )
    prompt = question.strip() if isinstance(question, str) else ""
    if not prompt:
        prompt = PAGE_SCAN_PROMPT
    url = "data:" + media_type + ";base64," + base64.b64encode(bytes(image)).decode(
        "ascii"
    )
    instruction = f"{prompt}\n\nInspection scope: {_scope_phrase(target)}."
    return [
        {"type": "text", "text": instruction},
        {"type": "image_url", "image_url": {"url": url}},
    ]


def _scope_phrase(target: VisionTarget) -> str:
    """The inspected region, named to the model.

    A crop answer must not be readable as whole-page coverage, so the exact
    normalized bounds travel in the request rather than only in the result.
    """
    if target.crop is None:
        return f"whole rendered page {target.page}"
    crop = target.crop
    return (
        f"page {target.page} region left={crop.left:g}, top={crop.top:g}, "
        f"right={crop.right:g}, bottom={crop.bottom:g}"
    )


def _human_message(parts: list[str | dict[str, Any]]) -> Any:
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=parts)


def _response_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            item if isinstance(item, str) else str(item.get("text") or "")
            for item in content
            if isinstance(item, (str, dict))
        )
    else:
        text = ""
    text = text.strip()
    if not text:
        raise VisionEmptyTextError("vision provider returned no readable text")
    return text


def _metadata_mapping(message: Any) -> dict[str, Any]:
    raw: Any = getattr(message, "response_metadata", None) or {}
    return raw if isinstance(raw, dict) else {}


def _usage_mapping(message: Any) -> dict[str, Any]:
    """Where the provider put its numbers, in whichever of its shapes.

    ``usage_metadata`` first, then a nested ``response_metadata`` token-usage
    block, then a named-tuple-like object. A miss yields ``{}`` — read as
    "nothing reported", never as zeros.
    """
    raw: Any = getattr(message, "usage_metadata", None) or {}
    if not raw:
        raw = _metadata_mapping(message).get("token_usage") or {}
    if not isinstance(raw, dict):
        as_dict = getattr(raw, "_asdict", None)
        raw = as_dict() if callable(as_dict) else {}
    return raw if isinstance(raw, dict) else {}


def _number(raw: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _response_usage(message: Any) -> VisionUsage | None:
    raw = _usage_mapping(message)
    fields = {
        name: _number(raw, keys) for name, keys in _TOKEN_KEY_ALIASES
    }
    if all(value is None for value in fields.values()):
        return None
    return VisionUsage(**fields)


def _response_cost(message: Any) -> VisionCost | None:
    """A reported cost, or ``None`` when the provider reported none.

    Absence is the whole point: no price table, no token-count arithmetic,
    and a reported zero stays a reported zero. An amount the provider sent in
    a form that is not a number counts as not reported.
    """
    usage = _usage_mapping(message)
    metadata = _metadata_mapping(message)
    for carrier in (usage, metadata):
        for key in _COST_KEYS:
            amount = _decimal(carrier.get(key)) if key in carrier else None
            if amount is None:
                continue
            return VisionCost(amount=amount, currency=_currency(carrier, metadata))
    return None


def _currency(carrier: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    """Currency from the carrier that reported the cost, else from metadata."""
    for source in (carrier, metadata):
        for key in _CURRENCY_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal, str)):
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    return None


__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "FIN_API_KEY_ENV",
    "FIN_BASE_URL_ENV",
    "FIN_MODEL_ENV",
    "PAGE_SCAN_PROMPT",
    "SUPPORTED_MEDIA_TYPES",
    "VISION_CALL_FAILED",
    "VISION_EMPTY_TEXT",
    "ChatModelVisionAdapter",
    "VisionCallFailedError",
    "VisionCallResult",
    "VisionCost",
    "VisionEmptyTextError",
    "VisionErrorClass",
    "VisionModelConfig",
    "VisionPort",
    "VisionPortError",
    "VisionTarget",
    "VisionUsage",
    "resolve_vision_model_config",
]
