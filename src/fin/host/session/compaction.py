"""Bounded session history for long-lived host conversations.

The compactor is deliberately a host-side value transformation.  A caller
can put ``CompactionResult.summary`` and ``CompactionResult.messages`` into
the existing checkpoint state, while Postgres remains the authority store.
This module does not import the graph, thread store, memory MCP, or Redis.

Compaction is incremental: once an earlier summary is supplied, the summary
model receives that summary plus only the messages evicted by the newest
window operation.  Recent messages are never sent to the summary model.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import BaseMessage, SystemMessage


DEFAULT_WINDOW_SIZE = 24
DEFAULT_KEEP_LAST = 8
DEFAULT_MAX_SUMMARY_CHARS = 4_000
MAX_WINDOW_SIZE = 100_000
MAX_SUMMARY_CHARS = 1_000_000
_SUMMARY_MARKER = "[fin.session.summary.v1]"


class SessionCompactionError(Exception):
    """Base class for typed session-compaction failures."""


class CompactionConfigError(SessionCompactionError):
    """The compaction limits are invalid."""


class CompactionInputError(SessionCompactionError):
    """The supplied history or status snapshot has an invalid shape."""


class SummaryModelError(SessionCompactionError):
    """The injected summary model failed or returned invalid output."""


class SummaryModel(Protocol):
    """Small injected seam for a synchronous incremental summarizer."""

    def invoke(self, messages: list[BaseMessage]) -> Any:
        """Return a summary response for the supplied evicted messages."""


@dataclass(frozen=True)
class CompactionResult:
    """The model-facing history and summary produced by one compaction pass."""

    messages: list[BaseMessage]
    summary: str = ""
    compacted: bool = False
    evicted_count: int = 0
    active_snapshot: dict[str, Any] = field(default_factory=dict)


def _validate_limits(
    window_size: int,
    keep_last: int,
    max_summary_chars: int,
) -> None:
    if (
        isinstance(window_size, bool)
        or not isinstance(window_size, int)
        or window_size < 1
        or window_size > MAX_WINDOW_SIZE
    ):
        raise CompactionConfigError(
            f"window_size must be an integer from 1 to {MAX_WINDOW_SIZE}"
        )
    if (
        isinstance(keep_last, bool)
        or not isinstance(keep_last, int)
        or keep_last < 1
        or keep_last > window_size
    ):
        raise CompactionConfigError(
            "keep_last must be a positive integer no greater than window_size"
        )
    if (
        isinstance(max_summary_chars, bool)
        or not isinstance(max_summary_chars, int)
        or max_summary_chars < 4
        or max_summary_chars > MAX_SUMMARY_CHARS
    ):
        raise CompactionConfigError(
            f"max_summary_chars must be an integer from 4 to {MAX_SUMMARY_CHARS}"
        )


def copy_snapshot(
    status_snapshots: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Public copy of the latest status snapshot's known slots.

    Shared with ``fin.host.session.session_compaction`` (Std-M3). Raises
    :class:`CompactionInputError` on malformed input so callers can
    catch it typed.
    """
    if status_snapshots is None or len(status_snapshots) == 0:
        return {}
    latest = status_snapshots[-1]
    if not isinstance(latest, Mapping):
        raise CompactionInputError("status_snapshots must contain mappings")
    snapshot: dict[str, Any] = {}
    for key in ("company", "period", "document_ids"):
        if key not in latest:
            continue
        value = latest[key]
        if key == "document_ids":
            if not isinstance(value, (list, tuple)):
                raise CompactionInputError("status snapshot document_ids must be a list")
            snapshot[key] = [str(item) for item in value]
        else:
            snapshot[key] = str(value)
    return snapshot


# Backward-compat alias (existing call sites inside this module and
# potential external users of the underscore name).
_copy_snapshot = copy_snapshot


def _validate_messages(messages: Sequence[BaseMessage] | None) -> list[BaseMessage]:
    if messages is None:
        raise CompactionInputError("messages must be a sequence, not None")
    copied = list(messages)
    if any(not isinstance(message, BaseMessage) for message in copied):
        raise CompactionInputError("messages must contain LangChain BaseMessage values")
    return copied


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return str(content)


def extract_existing_compaction(
    messages: list[BaseMessage],
) -> tuple[list[BaseMessage], str, dict[str, Any]]:
    """Public form of :func:`_extract_existing_compaction` — remove a
    leading summary marker and recover state for the next pass.

    Shared with ``fin.host.session.session_compaction`` (Std-M3).
    """
    if not messages or not isinstance(messages[0], SystemMessage):
        return messages, "", {}
    message = messages[0]
    content = _message_text(message)
    marker = message.additional_kwargs.get("fin_compaction_summary")
    if marker is not True and not content.startswith(_SUMMARY_MARKER):
        return messages, "", {}
    summary = message.additional_kwargs.get("fin_compaction_summary_text", "")
    if not isinstance(summary, str):
        summary = ""
    if not summary:
        body = content[len(_SUMMARY_MARKER) :].lstrip("\n")
        summary = body.split("\nActive status snapshot:", 1)[0]
    embedded_snapshot = message.additional_kwargs.get("status_snapshot", {})
    if not isinstance(embedded_snapshot, Mapping):
        embedded_snapshot = {}
    return messages[1:], summary, dict(embedded_snapshot)


# Backward-compat alias.
_extract_existing_compaction = extract_existing_compaction


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def snapshot_text(snapshot: Mapping[str, Any]) -> str:
    """Public form of :func:`_snapshot_text` — render the snapshot
    slots as ``key=value`` text (Std-M3: shared with
    ``fin.host.session.session_compaction``)."""
    if not snapshot:
        return ""
    fields: list[str] = []
    for key in ("company", "period", "document_ids"):
        value = snapshot.get(key)
        if value:
            rendered = ",".join(value) if isinstance(value, list) else str(value)
            fields.append(f"{key}={rendered}")
    return "; ".join(fields)


# Backward-compat alias.
_snapshot_text = snapshot_text


def render_summary_message(
    summary: str, snapshot: Mapping[str, Any]
) -> SystemMessage:
    """Public form of :func:`_render_summary_message` — build the
    leading ``SystemMessage`` carrying the summary + snapshot slots
    (Std-M3: shared with ``fin.host.session.session_compaction``)."""
    lines = [_SUMMARY_MARKER, summary or "(earlier conversation omitted)"]
    slots = snapshot_text(snapshot)
    if slots:
        lines.append(f"Active status snapshot: {slots}")
    return SystemMessage(
        content="\n".join(lines),
        additional_kwargs={
            "fin_compaction_summary": True,
            "fin_compaction_summary_text": summary,
            "status_snapshot": dict(snapshot),
        },
    )


# Backward-compat alias.
_render_summary_message = render_summary_message


def default_summary(
    evicted: Sequence[BaseMessage],
    previous_summary: str,
) -> str:
    """Public form of :func:`_default_summary` — the no-model
    fallback summary (Std-M3: shared with
    ``fin.host.session.session_compaction``)."""
    pieces: list[str] = []
    if previous_summary:
        pieces.append(f"Earlier summary: {previous_summary}")
    for message in evicted:
        text = _message_text(message).strip()
        if text:
            pieces.append(text)
    return " | ".join(pieces) or "(earlier conversation omitted)"


# Backward-compat alias.
_default_summary = default_summary


def response_text(
    response: Any,
    *,
    error: type[Exception] = SummaryModelError,
) -> str:
    """Extract stripped text from a summarizer response.

    Shared by ``fin.host.session.compaction`` and
    ``fin.host.session.session_compaction`` (Std-M4: one copy of the
    response-shape handling). Accepts a plain string or a message
    with a string ``content``; anything else raises ``error`` (each
    module passes its own typed failure so the seam stays typed).
    """
    if isinstance(response, str):
        return response.strip()
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content.strip()
    raise error("summary model must return text or a message with text")


def _response_text(response: Any) -> str:
    """Backward-compat alias for :func:`response_text`."""
    return response_text(response)


class SessionCompactor:
    """Apply a sliding window and incrementally update its summary.

    The object has no persisted mutable session state.  ``previous_summary``
    is explicit so callers can checkpoint it alongside the returned history.
    The lock only serializes an injected model that may itself be stateful;
    concurrent calls with the default summarizer remain independent.
    """

    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        keep_last: int = DEFAULT_KEEP_LAST,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        summary_model: SummaryModel | Callable[[list[BaseMessage]], Any] | None = None,
    ) -> None:
        _validate_limits(window_size, keep_last, max_summary_chars)
        self.window_size = window_size
        self.keep_last = keep_last
        self.max_summary_chars = max_summary_chars
        self.summary_model = summary_model
        self._summary_lock = threading.Lock()

    def _make_summary(
        self,
        evicted: Sequence[BaseMessage],
        previous_summary: str,
        snapshot: Mapping[str, Any],
    ) -> str:
        if self.summary_model is None:
            raw = _default_summary(evicted, previous_summary)
        else:
            prompt: list[BaseMessage] = [
                SystemMessage(
                    content=(
                        "Create a concise incremental conversation summary. "
                        "The messages after this instruction are the newly "
                        "evicted history; do not summarize recent messages. "
                        f"Previous summary: {previous_summary or '(none)'}"
                    )
                )
            ]
            slots = _snapshot_text(snapshot)
            if slots:
                prompt[0] = SystemMessage(
                    content=f"{prompt[0].content} Active status snapshot: {slots}"
                )
            prompt.extend(evicted)
            try:
                with self._summary_lock:
                    if hasattr(self.summary_model, "invoke"):
                        response = self.summary_model.invoke(prompt)  # type: ignore[union-attr]
                    elif callable(self.summary_model):
                        response = self.summary_model(prompt)
                    else:
                        raise SummaryModelError(
                            "summary_model must be callable or provide invoke"
                        )
                raw = _response_text(response)
            except SummaryModelError:
                raise
            except Exception as exc:  # noqa: BLE001 - convert dependency failure
                raise SummaryModelError(
                    "could not create session summary"
                ) from exc
            if not raw:
                raise SummaryModelError("summary model returned empty text")
        return _bounded_text(raw, self.max_summary_chars)

    def compact(
        self,
        messages: Sequence[BaseMessage] | None,
        *,
        previous_summary: str = "",
        status_snapshots: Sequence[Mapping[str, Any]] | None = None,
    ) -> CompactionResult:
        """Compact history only when it exceeds ``window_size`` messages."""
        if not isinstance(previous_summary, str):
            raise CompactionInputError("previous_summary must be text")
        original_messages = _validate_messages(messages)
        history, embedded_summary, embedded_snapshot = _extract_existing_compaction(
            original_messages
        )
        if not previous_summary:
            previous_summary = embedded_summary
        snapshot = _copy_snapshot(status_snapshots)
        if status_snapshots is None and embedded_snapshot:
            snapshot = embedded_snapshot
        if len(history) <= self.window_size:
            return CompactionResult(
                messages=original_messages,
                summary=previous_summary,
                active_snapshot=snapshot,
            )

        evicted_count = len(history) - self.keep_last
        evicted = history[:evicted_count]
        retained = history[evicted_count:]
        summary = self._make_summary(evicted, previous_summary, snapshot)
        return CompactionResult(
            messages=[_render_summary_message(summary, snapshot), *retained],
            summary=summary,
            compacted=True,
            evicted_count=evicted_count,
            active_snapshot=snapshot,
        )


def compact_session(
    messages: Sequence[BaseMessage] | None,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    keep_last: int = DEFAULT_KEEP_LAST,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    previous_summary: str = "",
    status_snapshots: Sequence[Mapping[str, Any]] | None = None,
    summary_model: SummaryModel | Callable[[list[BaseMessage]], Any] | None = None,
) -> CompactionResult:
    """Functional entry point for callers that do not retain a compactor."""
    return SessionCompactor(
        window_size=window_size,
        keep_last=keep_last,
        max_summary_chars=max_summary_chars,
        summary_model=summary_model,
    ).compact(
        messages,
        previous_summary=previous_summary,
        status_snapshots=status_snapshots,
    )


compact_messages = compact_session
SlidingWindowCompactor = SessionCompactor


__all__ = [
    "CompactionConfigError",
    "CompactionInputError",
    "CompactionResult",
    "DEFAULT_KEEP_LAST",
    "DEFAULT_MAX_SUMMARY_CHARS",
    "DEFAULT_WINDOW_SIZE",
    "MAX_SUMMARY_CHARS",
    "SessionCompactionError",
    "SessionCompactor",
    "SlidingWindowCompactor",
    "SummaryModelError",
    "compact_messages",
    "compact_session",
    "copy_snapshot",
    "default_summary",
    "extract_existing_compaction",
    "render_summary_message",
    "response_text",
    "snapshot_text",
]
