"""Workspace HTTP routes for the T1 surface (#28).

Routes
------
* ``GET    /api/threads``               - list threads (sidebar).
* ``POST   /api/threads``               - create thread (title from
                                          first_message first line,
                                          50-char cap + ellipsis).
* ``GET    /api/threads/{id}``          - thread metadata + turn
                                          table.
* ``DELETE /api/threads/{id}``          - drop metadata row + call
                                          checkpointer.delete_thread.
* ``POST   /api/threads/{id}/messages`` - the T1 (#33) send path:
                                          ``graph.stream`` driven
                                          with ``stream_mode=
                                          ["messages", "values"]``
                                          wrapped in a
                                          ``text/event-stream``.
                                          Token deltas (``thinking``
                                          / ``answer``) flow while
                                          the model step is still
                                          running — BEFORE that
                                          node's checkpoint write;
                                          node-complete ``values``
                                          frames synthesize
                                          ``tool_call`` /
                                          ``tool_result``; terminal
                                          frames keep the fixed
                                          ``status`` -> ``answer``
                                          -> ``citations`` order
                                          (citations terminal per
                                          spec line 30).
* ``POST   /api/threads/{id}/resume``   - crash resume on the SAME
                                          delta wire (T2, #36).
* ``GET    /api/corpus``                - real ``list_filings()`` over
                                          the frozen manifest.
* ``GET    /api/status``                - the boot-scoped status
                                          surface the Sidebar strip
                                          renders (T10 / ADR 0033):
                                          retrieve reachability +
                                          adapter kind + model slug +
                                          corpus filing count. Its own
                                          handler with no dependencies
                                          — never a branch inside send
                                          or GET-thread.

Why one module, not per-route modules? Each route is small, they
share the request schema (``thread_id`` resolution, empty-question
no-op), and the alternative - one module per route - would explode
the file count for a feature this small (spec forbids new top-level
directories).

Why the same app as ``/ask``? Spec / ADR 0013 / ADR 0024: one
FastAPI process per v1 surface. The routes are added to the
existing ``fin.host.http.app`` so a single ``uvicorn`` serves the
v1 ask route AND the workspace routes AND the SPA.

Concurrent-run ordering
-----------------------
``POST /api/threads/{id}/messages`` is wrapped in
``StreamingResponse``. To satisfy the spec rule "409 decided BEFORE
SSE headers", the route probes ``graph.is_locked(thread_id)``
synchronously BEFORE returning the StreamingResponse and raises the
typed ``ConcurrentRunError`` (translated to a plain 409
``application/json`` by the shared exception handler — no headers,
no body chunk).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Final, Iterator, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

from fin.corpus.catalog import list_filings
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER, strip_hidden_bounce_marker
from fin.host.graph import ConcurrentRunError
from fin.host.operator_env import (
    probe_retrieve_reachability,
    resolve_retrieve_adapter_kind,
)
from fin.host.run_payload import RunResponsePayload, _run_to_response_payload
from fin.host.stream_map import (
    InlineFilterState,
    ThinkingClock,
    format_tool_summary,
    reasoning_delta_from_chunk,
    split_stream_delta,
    strip_thinking_from_answer,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import (
    RunRecord,
    ThreadNotFoundError,
    ThreadRecord,
    ThreadStore,
)


logger = logging.getLogger(__name__)


# ----- mid-flight stream exception classification + sanitization -----
#
# Why classify here: every model call ultimately routes through
# ``openai``'s SDK error hierarchy (or ``langchain_openai``'s
# re-exports of the same classes). Surfacing a single English
# sentence to the operator hides which axis to act on — auth,
# network, rate-limit, etc. — so the SPA cannot render actionable
# guidance. We dispatch on the exception type and pair it with a
# short Chinese ordinary-person copy the SPA can render verbatim.
#
# Why a sanitize step: the refuse ``reason`` rides the SSE wire
# (server logs + persisted RunRecord) and historically embedded
# ``str(exc)`` — which carries the provider's full error text,
# often including the API base URL with the key, the raw
# ``Authorization: Bearer ...`` header, or a long opaque token.
# A single regex pass drops anything that looks sensitive before
# the reason reaches the wire. Defense in depth: the operator
# copy is hand-written, so even an unsanitized leak can only
# land in the ``reason`` field, never in the user-facing bubble.


# Classification → user-facing Chinese copy (ordinary-person, no
# provider internals). Single source of truth so the SSE frame
# and the persisted ``RunRecord.assistant_message`` always agree.
_STREAM_FAILURE_USER_COPY: Final[dict[str, str]] = {
    "auth": "鉴权失败：服务返回 401。请检查 .env 里的 FIN_API_KEY 是否设置且有效。",
    "network": "网络错误：无法连接到模型服务。请检查网络或代理（FIN_BASE_URL）。",
    "timeout": "模型调用超时（默认 120s）。可重试，或调高 ChatOpenAI timeout。",
    "rate_limit": "模型服务限流（429）。请稍后重试。",
    "not_found": "模型或接口路径不存在（404）。检查 FIN_MODEL 和 FIN_BASE_URL 是否拼写正确。",
    "server": "模型服务内部错误（5xx）。请稍后重试。",
    "unknown": "未预期的错误。请把 server 日志贴出来排查。",
}


def _classify_exception_type(exc: BaseException) -> str:
    """Map an exception instance to a kind string.

    Dispatch by exact ``openai`` SDK class first, then by class
    name substring (defensive against ``langchain_openai``'s
    wrappers that re-export the same exceptions). Returns one of
    the keys in ``_STREAM_FAILURE_USER_COPY``; falls back to
    ``"unknown"`` for anything that does not match.
    """
    # Local imports: ``openai`` is a runtime dep (langchain-openai
    # pulls it in), but only the workspace stream loop needs the
    # error hierarchy — module-import order stays clean.
    try:
        import openai
    except ImportError:  # pragma: no cover - openai is a hard dep
        openai = None  # type: ignore[assignment]

    # Auth axis: bad / missing token. ``PermissionDeniedError``
    # (403) is a sibling failure mode with the same operator
    # action — surface it under ``auth`` so the copy still
    # points at the key.
    if openai is not None:
        # WHY this ordering: ``openai.APITimeoutError`` subclasses
        # ``APIConnectionError`` so a naive isinstance on
        # ``APIConnectionError`` would swallow every timeout. The
        # specific subclasses go first; the parent class last.
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return "auth"
        if isinstance(exc, openai.APITimeoutError):
            return "timeout"
        if isinstance(exc, openai.RateLimitError):
            return "rate_limit"
        if isinstance(exc, openai.NotFoundError):
            return "not_found"
        if isinstance(exc, openai.InternalServerError):
            return "server"
        if isinstance(exc, openai.APIConnectionError):
            return "network"

    # Defensive fallback for ``langchain_openai`` wrappers or any
    # third-party SDK that re-exports an exception under a
    # different module path. Class-name substring match is enough
    # to disambiguate the seven kinds without overreaching.
    cls_name = type(exc).__name__.lower()
    cls_module = type(exc).__module__.lower()
    combined = f"{cls_module}.{cls_name}"
    if "authentication" in cls_name or "permissiondenied" in cls_name:
        return "auth"
    if "apitimeout" in cls_name or (
        "timeout" in cls_name and "apiconnection" not in cls_name
    ):
        return "timeout"
    if "ratelimit" in cls_name:
        return "rate_limit"
    if "notfound" in cls_name:
        return "not_found"
    if "internalserver" in cls_name:
        return "server"
    if "apiconnection" in cls_name or "connectionerror" in cls_name:
        return "network"
    if "server" in cls_name:
        return "server"
    # ``combined`` kept available for future module-scoped checks;
    # the local var silences the linter without altering dispatch.
    _ = combined
    return "unknown"


def classify_stream_exception(exc: BaseException) -> tuple[str, str]:
    """Return ``(kind, user_copy)`` for a mid-flight stream exception.

    ``kind`` is one of ``auth`` / ``network`` / ``timeout`` /
    ``rate_limit`` / ``not_found`` / ``server`` / ``unknown`` —
    a closed enum the SPA keys labels off of (no free text).
    ``user_copy`` is short Chinese actionable text the SPA shows
    verbatim; never blank.
    """
    kind = _classify_exception_type(exc)
    return kind, _STREAM_FAILURE_USER_COPY[kind]


# Patterns that look sensitive: an ``Authorization: Bearer ...``
# header; ``key=...`` / ``token=...`` query-string style fragments;
# full URLs whose path or query string embeds what looks like a
# key (long alphanumeric token, hex blob). Each match collapses
# to a short tag so the operator still sees "auth failed against
# <URL redacted>" rather than losing all context.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{20,})"),
    # ``key=`` / ``token=`` / ``api_key=`` query-string fragments;
    # capture the assignment through the value delimiter.
    re.compile(r"(?i)\b(api[_-]?key|token|access[_-]?token|secret)\s*[=:]\s*[^\s,;]+"),
    # Long hex / base64url blobs (>=32 chars) — high-entropy tokens
    # the operator never needs to see on the wire.
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def _sanitize_reason_text(text: str, max_len: int = 200) -> str:
    """Redact sensitive substrings and cap length on a wire string.

    Why a small standalone helper: the refuse ``reason`` is the one
    field on the wire that may carry provider-internal text. The
    contract is "single-line, <=max_len chars, no tokens, no
    headers". One choke point keeps every future caller honest.
    """
    if not text:
        return ""
    out = text
    for pattern in _SENSITIVE_PATTERNS:
        out = pattern.sub("[redacted]", out)
    # Collapse whitespace so a multi-line provider error still
    # reads as one line on the wire (SSE is line-oriented).
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_len:
        out = out[: max_len - 3] + "..."
    return out


@dataclass(frozen=True)
class RunStreamOutcome:
    """What ``_stream_run_frames`` hands back when the run's wire ends.

    ``final_state`` is the last ``values`` frame (``None`` when the
    stream produced nothing). ``thinking_ms`` is the run's total
    streamed-thinking duration (T4, #37) — persisted on the
    ``RunRecord`` and echoed on the terminal ``status`` frame so the
    SPA's post-run collapse label matches the stored turn row.

    ``refuse_reason`` / ``refuse_copy`` are set ONLY on the
    mid-flight-raise path: the engine already emitted the terminal
    refuse frames itself, and callers persist them on a
    ``RunRecord`` so the failed exchange stays in the turn table
    (spec line 67: GET history shows empty sources plus non-empty
    assistant copy) instead of being wiped by the SPA's
    openThread reconcile.

    ``answer_reset`` is True when a bounce-marker SystemMessage set
    a pending live-view reset that was never consumed by a
    non-empty answer delta — callers attach ``reset: true`` to the
    terminal answer frame so a refuse / empty terminal still clears
    the stale draft.
    """

    final_state: dict[str, Any] | None
    thinking_ms: int
    refuse_reason: str | None = None
    refuse_copy: str | None = None
    # Mid-flight stream error taxonomy (auth / network / timeout /
    # rate_limit / not_found / server / unknown). ``None`` when
    # the run did not end on the refuse branch. The same value
    # rides the SSE ``status_classified`` field and the persisted
    # RunRecord so reload keeps the classification visible.
    refuse_kind: str | None = None
    answer_reset: bool = False


# Graph nodes whose model deltas are the operator-visible reply.
# ``model`` is the research leaf; the three Soft-Track leaves
# (specs/soft-track.md / ADR 0027) author their reply the same way,
# so their tokens paint the live turn too. Every other node stays
# off the wire — notably ``classify``, whose LLM tier streams
# routing JSON that is not prose.
_ANSWER_AUTHORING_NODES: frozenset[str] = frozenset(
    {"model", "dialogue", "clarify", "off_topic"}
)


# Maximum characters of the first message used as the thread title.
# Spec: "max 50 chars + ellipsis on overflow."
_TITLE_MAX_CHARS: int = 50

# Default title when the caller creates a thread without a first
# message (the empty corpus / empty thread copy).
_DEFAULT_TITLE: str = "New thread"


# ----- request / response schemas -----


class CreateThreadRequest(BaseModel):
    first_message: str | None = None


class CreateThreadResponse(BaseModel):
    id: str
    title: str
    created_at: str  # ISO-8601 from datetime.isoformat()


class SendMessageRequest(BaseModel):
    question: str
    # T3 (spec line 27): operator's "Think step by step" toggle.
    # ``True`` (or omitted, the default) routes to ``adaptive`` thinking;
    # ``False`` flips the model to ``disabled`` for this run. The route
    # mutates the compiled ``ChatOpenAI.model_kwargs`` for the duration
    # of the invoke and restores the original value after the run so
    # concurrent / follow-up sends on other threads see the default.
    # The proxy keeps ``reasoning_split=True`` regardless — only the
    # thinking TYPE toggles; reasoning_details round-trip remains on.
    thinking: bool = True


class CitationPayload(BaseModel):
    document_id: str
    version: str
    page: int
    quote: str


class ToolSummaryPayload(BaseModel):
    """One human-readable tool-call row for the tools region.

    ``summary`` is the formatted one-liner (``"n filings"``,
    ``"query' -> 8 hits -> AAPL-2022Q3 p.4"``, ``"<doc> p.N"``);
    raw ToolMessage JSON never reaches the wire (spec line 32).
    """

    name: str
    summary: str
    tool_call_id: str


class TurnRow(BaseModel):
    """One row in the turn table - one user send + assistant reply.

    ``assistant_message`` is the ordinary-person copy the SPA
    renders when ``status`` is ``refuse`` or ``step_cap`` (T2 /
    spec SC67). Empty on ``done``. Mirrored on the
    ``RunRecord`` and on the SSE ``status`` frame so reload and
    live surfaces agree.
    """

    run_id: str
    question: str
    status: Literal["done", "refuse", "step_cap"]
    status_reason: str = ""
    answer: str = ""
    citations: list[CitationPayload] = Field(default_factory=list)
    thinking_text: str = ""
    # T4 (#37, spec line 31): total thinking duration in ms. Drives
    # the post-run one-line collapse label ("Thought for 4.2s");
    # a reload restores it from this row. 0 when no thinking.
    thinking_duration_ms: int = 0
    # T3 (spec line 32): one dict per tool call observed this run;
    # keys are exactly ``name`` / ``summary`` / ``tool_call_id``.
    tool_summaries: list[ToolSummaryPayload] = Field(default_factory=list)
    assistant_message: str = ""
    created_at: str
    # T4 (spec line 38): the live GET-while-running signal. Stamped
    # at serialization time from ``graph.is_locked(thread_id)`` on
    # the LATEST turn row; older rows always serialize ``False``
    # because a stored RunRecord is by definition terminal.
    in_flight: bool = False
    # Soft-Track T5: auditable classify outcome for this run_id.
    track_decision: dict[str, Any] = Field(default_factory=dict)
    # Mid-flight stream error taxonomy on refuse rows
    # (``auth`` / ``network`` / ``timeout`` / ``rate_limit`` /
    # ``not_found`` / ``server`` / ``unknown``). Empty on done
    # rows. The SPA keys a small "原因" label off this field so
    # a 401 / 429 / 504 reads distinctly instead of as one opaque
    # English sentence.
    status_classified: str = ""


class WorkspaceStatusResponse(BaseModel):
    """The boot-scoped workspace status surface (T10, ADR 0033).

    Exactly four fields, and the ADR fixes each one:

    * ``retrieve_reachability`` — ``unreachable`` says every retrieve
      call answers with the typed ``retrieve_unavailable`` EXIT. It
      is payload DATA with degraded copy behind it, never an HTTP
      500 and never an empty body: a blank strip tells the operator
      nothing.
    * ``retrieve_adapter_kind`` — which port is behind the seam, so a
      healthy ``fake`` boot is distinguishable from a healthy
      product boot.
    * ``model_identity`` — the resolved ``FIN_MODEL`` slug ONLY.
      ``FIN_BASE_URL`` and key material stay off this wire.
    * ``corpus_filing_count`` — from the same in-process catalog that
      serves ``GET /api/corpus``. Zero is a valid rendered value.

    Deliberately absent (ADR 0033 OUT): ``step_budget_live``,
    ``token_usage_absolute``, ``soft_track_confidence`` — run- and
    turn-scoped values belong to the SSE contract, not to a boot
    surface — plus any "% of context window" figure and any skill
    list (plan exclusion).
    """

    retrieve_reachability: Literal["reachable", "unreachable"]
    retrieve_adapter_kind: Literal["mcp", "fake"]
    model_identity: str
    corpus_filing_count: int


class ThreadDetailResponse(BaseModel):
    id: str
    title: str
    created_at: str
    # T4: thread-level live running signal. True while the
    # per-thread lock is held - covers the window where the run is
    # executing but no RunRecord exists yet (a row is appended only
    # after its stream completes). The SPA disables the Composer on
    # this flag even before any turn row appears.
    in_flight: bool = False
    # T4 fix (code-review H1): a checkpoint with ``next`` nodes is
    # present AND the lock is NOT held — i.e. the process died
    # mid-run and the operator should see the Resume banner. The
    # previous implementation keyed off ``in_flight``, which made
    # the banner appear during a process-alive refresh (when it is
    # forbidden) and never appear after a real crash (when it is
    # needed). ``pending_checkpoint`` derives from
    # ``pending_nodes(thread_id) and not is_locked``.
    pending_checkpoint: bool = False
    turns: list[TurnRow]


# ----- helpers -----


def _derive_title(first_message: str | None) -> str:
    """Build the thread title from a first message.

    Spec: "first line, stripped, max 50 chars + ellipsis on
    overflow". When the caller passes ``None`` or an empty string
    we fall back to ``_DEFAULT_TITLE``.
    """
    if not first_message:
        return _DEFAULT_TITLE
    first_line = first_message.splitlines()[0].strip()
    if not first_line:
        return _DEFAULT_TITLE
    if len(first_line) <= _TITLE_MAX_CHARS:
        return first_line
    return first_line[:_TITLE_MAX_CHARS] + "…"


def _serialize_thread(thread: ThreadRecord) -> dict[str, str]:
    return {
        "id": thread.id,
        "title": thread.title,
        "created_at": thread.created_at.isoformat(),
    }


def _serialize_run(run: RunRecord, *, in_flight: bool = False) -> dict[str, Any]:
    """Translate a ``RunRecord`` into the turn-table JSON shape.

    T4: ``in_flight`` carries the live lock-probe result for the
    thread. Only the newest row can legitimately be ``True`` — the
    GET route passes the probe result for the last row and
    ``False`` for all earlier ones.
    """
    return {
        "run_id": run.id,
        "question": run.question,
        "status": run.status,
        "status_reason": run.status_reason,
        "answer": run.answer,
        "citations": list(run.citations),
        "thinking_text": run.thinking_text,
        "thinking_duration_ms": run.thinking_duration_ms,
        # T3 shape: one {name, summary, tool_call_id} dict per
        # tool call; deep-copied so a caller cannot mutate the
        # record's list through the serialized view.
        "tool_summaries": [dict(entry) for entry in run.tool_summaries],
        "assistant_message": run.assistant_message,
        "created_at": run.created_at.isoformat(),
        "in_flight": in_flight,
        # Soft-Track: deep-copied so callers cannot mutate the store.
        "track_decision": dict(run.track_decision or {}),
        # Mid-flight stream error taxonomy (see TurnRow). Empty on
        # rows whose refusal came from the gate rather than the
        # stream engine.
        "status_classified": run.status_classified,
    }


def _format_sse(event: str, data: Any) -> str:
    """Format a single ``text/event-stream`` frame.

    Frames are terminated by a blank line. JSON values that
    contain newlines would split across multiple ``data:`` lines
    per the SSE spec; our payloads (``status`` / ``citations`` /
    ``answer``) are newline-free by construction (no embedded
    tool JSON yet — T3 will add streaming deltas).
    """
    encoded = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {encoded}\n\n"


# ----- SSE frame builders (T1 #33 / T3) -----


def _node_frame_payloads(message: BaseMessage) -> Iterator[tuple[str, Any]]:
    """Yield the node-complete ``(event, data)`` frames for a message.

    Node-complete frames are synthesized from ``values``-mode
    emissions (each arrives AFTER the node's checkpoint write):

    * ``AIMessage``  -> one ``tool_call`` frame per parsed tool call
      (args are complete and parsed here — chunk-level
      ``tool_call_chunks`` would emit partial args). Thinking is
      NOT emitted here: the split-channel deltas already streamed
      via ``messages`` mode (whatever the assembled message carries
      on ``additional_kwargs.reasoning_details`` arrived chunk by
      chunk first).
    * ``ToolMessage`` -> one ``tool_result`` frame with the
      human-readable summary (never the raw JSON payload).
    * HumanMessage / SystemMessage / others -> no frame. The user
      side has its own UI; SystemMessages are the gate's hidden
      bounce templates and MUST NOT appear on the wire (ADR 0021).
    """
    if isinstance(message, AIMessage):
        for call in message.tool_calls or ():
            args = call.get("args") or {}
            if isinstance(args, str):
                # LangChain sometimes leaves args as a JSON string
                # when the model streamed them; parse defensively.
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {}
            yield (
                "tool_call",
                {
                    "name": call.get("name", ""),
                    "args": args,
                    "tool_call_id": call.get("id", ""),
                },
            )
        return
    if isinstance(message, ToolMessage):
        tool_name = getattr(message, "name", "") or ""
        tool_call_id = getattr(message, "tool_call_id", "") or ""
        raw_content = message.content
        parsed: Any = raw_content
        if isinstance(raw_content, str):
            try:
                parsed = json.loads(raw_content)
            except (TypeError, ValueError):
                parsed = raw_content
        # Strip the marker from the human-readable summary so a
        # bounce template embedded in tool output never escapes
        # to the SPA (ADR 0021).
        yield (
            "tool_result",
            {
                "name": tool_name,
                "summary": strip_hidden_bounce_marker(
                    format_tool_summary(tool_name, result=parsed)
                ),
                "tool_call_id": tool_call_id,
            },
        )


def _stream_run_frames(
    graph: Any,
    *,
    run_id: str,
    input_value: Any,
    config: dict[str, Any],
    seen_message_ids: set[str],
    thread_id: str | None = None,
) -> Iterator[str]:
    """Drive one run on the combined delta wire; return the outcome.

    The shared engine behind the send route (T1, #33) and the
    resume route (T2, #36) so the two surfaces cannot drift on the
    wire contract. On exception the generator still emits
    ``status`` / ``answer`` / ``citations`` before returning so the
    SPA's reader closes cleanly instead of hanging on a truncated
    stream (mid-flight failures otherwise leave the live view
    frozen in ``running``).

    Consumes ``graph.stream`` with
    ``stream_mode=["messages", "values"]``:

    * ``messages`` items arrive DURING an answer-authoring node's
      execution (``_ANSWER_AUTHORING_NODES``: the research leaf and
      the three Soft-Track leaves) — each ``AIMessageChunk`` maps to
      zero or more ``thinking`` / ``answer`` delta frames (SC60:
      reasoning deltas go to ``thinking``, inline ``<thinking>``
      prose is filtered out of ``answer`` even when a tag spans
      chunk boundaries). The aggregated end-of-call ``AIMessage``
      (not a chunk) and chunks from every other node — the
      ``classify`` tier above all — are skipped.
    * ``values`` items arrive after each node completes (post
      checkpoint) — new messages map to ``tool_call`` /
      ``tool_result`` frames, deduplicated by message id through
      the caller-owned ``seen_message_ids`` set (resume callers seed
      it with the pre-resume checkpoint's ids so checkpointed
      messages do not replay onto the wire).

    Every emitted frame ticks the :class:`ThinkingClock` (T4, #37)
    so the run's total thinking duration is known when the stream
    ends. The generator's return value carries the final state and
    ``thinking_ms``; ``None`` final state means the stream produced
    nothing. On exception the generator still emits status / answer
    / citations before returning; the send caller then persists a
    refuse row (``refuse_reason`` set), the resume caller skips.
    """
    clock = ThinkingClock()

    def _frame(event: str, data: Any) -> str:
        # T4 (#37): thinking frames open a duration segment; every
        # other frame closes it — the collapse label's data.
        if event == "thinking":
            clock.tick_thinking()
        else:
            clock.tick_other()
        return _format_sse(event, data)

    filter_state = InlineFilterState()
    final_state: dict[str, Any] | None = None
    # Bounce-marker SystemMessage arms a pending reset; the NEXT
    # non-empty answer delta carries ``reset: true`` in the same
    # frame as its text. Do NOT emit a standalone empty reset (that
    # blanks the SPA before tool_call / retry prose). tool_call does
    # not clear pending. If the run ends with pending still armed,
    # the terminal empty answer flushes it (refuse / no retry prose).
    pending_answer_reset = False

    def _answer_payload(text: str) -> dict[str, Any]:
        nonlocal pending_answer_reset
        data: dict[str, Any] = {"text": text}
        if pending_answer_reset:
            data["reset"] = True
            pending_answer_reset = False
        return data

    try:
        for mode, payload in graph.stream(
            input_value,
            config=config,
            stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                chunk, meta = payload
                # The aggregated end-of-call AIMessage is not a chunk;
                # only per-token AIMessageChunks carry new deltas.
                if not isinstance(chunk, AIMessageChunk):
                    continue
                if meta.get("langgraph_node") not in _ANSWER_AUTHORING_NODES:
                    continue
                reasoning = reasoning_delta_from_chunk(chunk)
                if reasoning:
                    yield _frame(
                        "thinking",
                        {"text": strip_hidden_bounce_marker(reasoning)},
                    )
                content = chunk.content
                if isinstance(content, str) and content:
                    split = split_stream_delta(filter_state, content)
                    filter_state = split.state
                    if split.thinking:
                        yield _frame("thinking", {"text": split.thinking})
                    if split.answer:
                        yield _frame("answer", _answer_payload(split.answer))
            else:
                final_state = payload
                for message in payload.get("messages", []) or []:
                    mid = getattr(message, "id", None)
                    if mid and mid in seen_message_ids:
                        continue
                    if mid:
                        seen_message_ids.add(mid)
                    if isinstance(message, SystemMessage):
                        # Only the gate's hidden bounce template (ADR
                        # 0021 marker) arms pending reset. Other
                        # SystemMessages are ignored. The template
                        # itself never reaches the wire.
                        raw = message.content
                        if (
                            isinstance(raw, str)
                            and HIDDEN_BOUNCE_MARKER in raw
                        ):
                            pending_answer_reset = True
                        continue
                    for event, data in _node_frame_payloads(message):
                        yield _frame(event, data)

        # Stream end: release any held-back inline-filter fragment so
        # the live view never silently loses trailing text.
        tail = split_stream_delta(filter_state, "", final=True)
        if tail.thinking:
            yield _frame("thinking", {"text": tail.thinking})
        if tail.answer:
            yield _frame("answer", _answer_payload(tail.answer))
    except Exception as exc:
        # Mid-flight failure (model raise, node crash): without this
        # boundary the exception escapes the generator, FastAPI kills
        # the SSE body before any terminal frame, and the SPA's reader
        # hangs waiting for ``citations`` (spec line 96: terminal
        # statuses carry explicit copy — never swallowed blanks).
        #
        # Classify the exception so the SPA can render an actionable
        # label instead of one opaque English sentence. The
        # classification ``kind`` is a closed enum (auth / network /
        # timeout / rate_limit / not_found / server / unknown); the
        # user-facing copy is hand-written Chinese keyed off that
        # kind. The exception type name rides ``reason`` for operator
        # debuggability, with ``str(exc)`` appended only AFTER the
        # sanitize pass — provider error text commonly embeds the
        # auth header, the key, or a long opaque token, none of which
        # belong on the wire.
        kind, user_copy = classify_stream_exception(exc)
        raw_reason = f"stream raised mid-flight: {type(exc).__name__}: {exc}"
        sanitized = _sanitize_reason_text(raw_reason)
        reason = f"{sanitized} (kind={kind})" if sanitized else f"stream raised mid-flight: {type(exc).__name__} (kind={kind})"
        logger.exception(
            "run stream raised mid-flight",
            extra={
                "run_id": run_id,
                "thread_id": thread_id,
                "kind": kind,
            },
        )
        yield _format_sse(
            "status",
            {
                "run_id": run_id,
                "status": "refuse",
                "reason": reason,
                "assistant_message": user_copy,
                "status_classified": kind,
                "thinking_ms": clock.total_ms,
            },
        )
        # Flush pending bounce reset on the terminal empty answer
        # when the raise happened before any retry prose.
        yield _format_sse(
            "answer", _answer_payload(strip_thinking_from_answer(""))
        )
        yield _format_sse("citations", _citations_wire_payload([]))
        clock.close()
        return RunStreamOutcome(
            final_state=None,
            thinking_ms=clock.total_ms,
            refuse_reason=reason,
            refuse_copy=user_copy,
            refuse_kind=kind,
        )
    clock.close()
    return RunStreamOutcome(
        final_state=final_state,
        thinking_ms=clock.total_ms,
        answer_reset=pending_answer_reset,
    )


def _citations_wire_payload(citations: list[Any]) -> list[dict[str, Any]]:
    """Normalise ``payload["citations"]`` into the wire dict shape.

    ``_run_to_response_payload`` emits plain dicts; older fixtures /
    callers may carry ``Citation`` dataclasses. Both collapse to the
    ``CitationPayload`` model-dump shape here so every emitter sends
    one identical shape (Standards: single source of truth for the
    wire).
    """
    out: list[dict[str, Any]] = []
    for c in citations:
        if isinstance(c, dict):
            out.append(
                CitationPayload(
                    document_id=c["document_id"],
                    version=c["version"],
                    page=c["page"],
                    quote=c["quote"],
                ).model_dump()
            )
        else:
            out.append(
                CitationPayload(
                    document_id=c.document_id,
                    version=c.version,
                    page=c.page,
                    quote=c.quote,
                ).model_dump()
            )
    return out


def _terminal_sse_frames(
    *,
    run_id: str,
    payload: RunResponsePayload,
    thinking_type: str | None = None,
    thinking_ms: int | None = None,
    answer_reset: bool = False,
) -> Iterator[str]:
    """Yield the fixed-order terminal frames: ``status`` -> ``answer``
    -> ``citations`` (spec line 30; citations is TERMINAL so the SPA
    closes its reader).

    Shared by the send and resume routes so the two surfaces cannot
    drift on order / marker-stripping / citation shaping. Optional
    extras on the status frame: ``thinking_type`` (send route echo)
    and ``thinking_ms`` (T4, #37 — the collapse label's duration,
    known by stream end).

    ``answer_reset`` attaches ``reset: true`` to the terminal answer
    when a bounce left pending reset unconsumed (typical: refuse
    with empty text and no retry prose). Mid-run empty
    ``{text:"", reset:true}`` is forbidden; this terminal flush is
    the sole empty+reset exception.
    """
    safe_answer = strip_thinking_from_answer(
        payload["answer"] if payload["status"] == "done" else ""
    )
    status_body: dict[str, Any] = {
        "run_id": run_id,
        "status": payload["status"],
        "reason": payload["reason"] or None,
        "assistant_message": payload["assistant_message"],
    }
    # Soft-Track: expose the graph track so the SPA can render a
    # light research hint without Chat|Research chrome.
    if payload.get("track"):
        status_body["track"] = payload["track"]
    if thinking_type is not None:
        status_body["thinking_type"] = thinking_type
    if thinking_ms is not None:
        status_body["thinking_ms"] = thinking_ms
    answer_body: dict[str, Any] = {"text": safe_answer}
    if answer_reset:
        answer_body["reset"] = True
    yield _format_sse("status", status_body)
    yield _format_sse("answer", answer_body)
    yield _format_sse("citations", _citations_wire_payload(payload["citations"]))


def _model_identity() -> str:
    """The resolved ``FIN_MODEL`` slug, or the ADR 0022 default.

    Same resolution ``fin.host.compile._build_default_model``
    performs, minus everything else it reads: the endpoint and the
    auth token never reach this payload (ADR 0033).
    """
    import os

    from fin.host.compile import DEFAULT_MODEL

    return (os.environ.get("FIN_MODEL") or "").strip() or DEFAULT_MODEL


def _corpus_filing_count() -> int:
    """Filing count from the catalog that serves ``GET /api/corpus``.

    A corpus that was never ingested raises ``IngestMissingError``
    here. On this route that is a zero count, not a failure: ADR
    0033 makes zero a valid rendered value, and a 500 would blank a
    strip whose other three fields are still true. The warning keeps
    the state out of the silent-swallow class — ``GET /api/corpus``
    itself still surfaces the typed error to whoever asks for the
    filings.
    """
    from fin.corpus.errors import IngestMissingError

    try:
        return len(list_filings()["filings"])
    except IngestMissingError:
        logger.warning(
            "status: the corpus catalog is unreadable; reporting "
            "corpus_filing_count=0",
            exc_info=True,
        )
        return 0


# ----- route factory -----


def build_router(*, get_graph, get_thread_store) -> APIRouter:
    """Build the workspace APIRouter.

    The two dependencies are passed in so the router can be wired
    against any FastAPI app (the prod app in
    ``fin.host.http``, and any test app).
    """
    router = APIRouter(prefix="/api")

    @router.get("/threads")
    def list_threads(store: ThreadStore = Depends(get_thread_store)) -> list[dict]:
        return [_serialize_thread(t) for t in store.list_threads()]

    @router.post("/threads", response_model=CreateThreadResponse, status_code=200)
    def create_thread(
        body: CreateThreadRequest,
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        # WHY the route does NOT mint the id: the store owns id
        # allocation (uuid4 inside ``create_thread``), matching the
        # checkpointer's own auto-id pattern and keeping the HTTP
        # layer free of persistence concerns. ``record.id`` is
        # exposed on the response so the client can navigate to
        # the new thread.
        title = _derive_title(body.first_message)
        record = store.create_thread(title=title)
        return _serialize_thread(record)

    @router.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
    def get_thread(
        thread_id: str,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        runs = store.list_runs(thread_id)
        # T4 (spec line 38): GET while a lock is held reports
        # running. The probe is the live signal; only the newest
        # row can be in flight because a stored RunRecord is
        # appended only after its stream completed. Defensive
        # ``hasattr`` mirrors ``delete_thread``'s wiring-bug
        # tolerance for graph stand-ins without the seam.
        is_locked = False
        if hasattr(graph, "is_locked"):
            is_locked = bool(graph.is_locked(thread_id))
        # T4 fix (code-review H1): ``pending_checkpoint`` is true
        # exactly when a checkpoint exists with non-empty ``next``
        # nodes AND the lock is NOT held — i.e. the prior run died
        # before reaching a terminal node. The Resume banner keys
        # off this flag (NOT ``in_flight``, which is the live-lock
        # signal and must NOT trigger a Resume button).
        pending_checkpoint = False
        if (
            not is_locked
            and hasattr(graph, "pending_nodes")
            and bool(graph.pending_nodes(thread_id))
        ):
            pending_checkpoint = True
        return {
            **_serialize_thread(thread),
            "in_flight": is_locked,
            "pending_checkpoint": pending_checkpoint,
            "turns": [
                _serialize_run(r, in_flight=(is_locked and i == len(runs) - 1))
                for i, r in enumerate(runs)
            ],
        }

    @router.delete("/threads/{thread_id}")
    def delete_thread(
        thread_id: str,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        existing = store.get_thread(thread_id)
        if existing is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        # Metadata first, then checkpointer. If the checkpointer
        # delete raises we keep the metadata gone - that's the
        # user-visible intent, and the next ``GET`` will not
        # resurrect the turn table.
        store.delete_thread(thread_id)

        # WHY the narrow excepts: the previous ``except Exception:
        # pass`` swallowed EVERY failure mode — including a saver
        # outage that leaves orphaned checkpoint tuples behind —
        # with no log trail. Now:
        #
        # * ``AttributeError`` = the graph proxy has neither a
        #   ``delete_thread`` seam nor a reachable checkpointer.
        #   That is a wiring bug, not a runtime failure; warn and
        #   still return 200 (metadata row is already gone, which
        #   is what the user asked for).
        # * any OTHER exception (saver outage, DB down) means the
        #   checkpoint tuples may STILL EXIST while metadata is
        #   gone — an inconsistent state the operator must see.
        #   Log with traceback AND surface 500 so a silent orphan
        #   cannot hide behind a green UI.
        try:
            graph.delete_thread(thread_id)
        except AttributeError:
            logger.warning(
                "delete_thread: no checkpointer on graph for thread_id=%s",
                thread_id,
            )
        except Exception:
            logger.exception(
                "delete_thread: saver failed for thread_id=%s", thread_id
            )
            raise HTTPException(status_code=500, detail="delete failed") from None
        return {"ok": True}

    @router.post("/threads/{thread_id}/messages")
    def send_message(
        thread_id: str,
        body: SendMessageRequest,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """Run one v1 host-graph run on the thread over SSE (T1 #33).

        Wire shape (``_stream_run_frames`` drives the run):

        * ``thinking`` / ``answer`` delta frames flow DURING each
          model step (``messages`` mode), before that step's
          checkpoint write. Multiple frames per channel when the
          model emits multiple chunks.
        * ``tool_call`` / ``tool_result`` frames are synthesized
          from node-complete ``values`` frames (parsed args /
          human-readable summaries).
        * ``status`` -> ``answer`` (authoritative full text) ->
          ``citations`` (TERMINAL per spec line 30) close the run.

        Concurrency: the lock probe runs SYNCHRONOUSLY before the
        ``StreamingResponse`` is returned, so a second concurrent
        request raises ``ConcurrentRunError`` -> plain 409
        ``application/json`` BEFORE any event-stream headers.
        """
        # Empty question is a no-op per spec (composer guard):
        # 204 with no body, no graph invoke, no SSE.
        if not body.question or not body.question.strip():
            return Response(status_code=204)

        # Unknown thread -> 404 BEFORE any graph invoke (the graph
        # would happily create state for a new thread_id).
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )

        # T2: title update on the first real send. The T1 path
        # set the title from ``first_message`` at create time;
        # when the user created the thread empty (corpus-card
        # click) the title was the default placeholder. We
        # rewrite it from the question here, using the SAME
        # first-line / 50-char / ellipsis rule ``create_thread``
        # uses. Only when the current title is the default
        # placeholder — a real first_message would have set a
        # non-default title and we never overwrite user intent.
        #
        # Done BEFORE the graph invoke: if the run raises a
        # ``ConcurrentRunError`` (409) the title still updates
        # — the user's question is real intent, the 409 just
        # means another run is in flight.
        if thread.title == _DEFAULT_TITLE:
            derived = _derive_title(body.question)
            if derived != _DEFAULT_TITLE:
                try:
                    thread = store.update_thread_title(
                        thread_id, title=derived
                    )
                except ThreadNotFoundError:
                    # Race: thread was deleted between the
                    # 404-check above and this update. Surface
                    # 404 - same shape as the unknown-thread
                    # guard above. Sidebar stays clean (the
                    # deleted thread was never on it).
                    return JSONResponse(
                        status_code=404,
                        content={"error": "thread_not_found"},
                    )

        # Mint the run_id up front so the SSE ``status`` event can
        # carry it before any graph work happens. The same id is
        # persisted on the RunRecord, so the optimistic turn the
        # client renders matches the stored turn row exactly.
        run_id = str(uuid.uuid4())

        # 409-before-headers (spec line 26): probe the lock HERE,
        # synchronously. The typed exception handler translates it
        # to 409 ``application/json``; a probe inside the
        # StreamingResponse generator would surface mid-body once
        # event-stream headers were already flushed.
        if hasattr(graph, "is_locked") and graph.is_locked(thread_id):
            raise ConcurrentRunError(
                f"another run is already in flight on thread_id={thread_id!r}"
            )

        # T2: the input state ALWAYS resets the run-scoped fields
        # (``reset_input_state`` clears ``evidence`` — impossible
        # with ``operator.add + []`` — and zeroes ``step_count`` /
        # ``repair_count``) so a follow-up post starts a fresh run,
        # not a continuation. ``messages`` keeps accumulating via
        # ``add_messages``; the helper clears the six run-scoped
        # fields per ADR 0016 / spec line 34.
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        thinking_type = "adaptive" if body.thinking else "disabled"
        if not body.thinking:
            # T3 (#34): the operator's thinking preference rides the
            # run config — ``model_node`` swaps to the pre-built
            # no-thinking binding (``extra_body`` thinking.type=
            # disabled, reasoning_split stays on). Only the OFF
            # preference is stamped; the default (on) keeps the boot
            # binding untouched.
            config["configurable"]["thinking_type"] = thinking_type

        def event_stream() -> Iterator[str]:
            """Yield the delta wire for this run, then persist + close.

            ``_stream_run_frames`` owns the frame mapping and
            returns the final state via ``yield from``. RunRecord
            persistence is best-effort — if the thread was deleted
            mid-stream the persistence is skipped rather than
            resurrected (a row for a deleted thread is never
            wanted) and a single refuse-status frame explains it.
            """
            seen_message_ids: set[str] = set()
            # Seed the dedup set with the pre-run checkpoint's
            # message ids: a follow-up send's first ``values`` frame
            # replays the whole accumulated list, and prior runs'
            # tool frames must not re-emit onto this stream
            # (spec line 34).
            if hasattr(graph, "checkpoint_message_ids"):
                seen_message_ids |= graph.checkpoint_message_ids(thread_id)
            outcome = yield from _stream_run_frames(
                graph,
                run_id=run_id,
                input_value=reset_input_state(body.question),
                config=config,
                seen_message_ids=seen_message_ids,
                thread_id=thread_id,
            )

            if outcome.final_state is None:
                if outcome.refuse_reason is None:
                    # Exit condition: empty stream WITHOUT an
                    # exception — the engine emitted no values frame
                    # and no terminal frames; there is no failure to
                    # persist, so exit without touching the store.
                    return
                # Mid-flight raise: the engine already emitted the
                # terminal refuse frames itself. Persist the refusal
                # so the turn table keeps the user's question and the
                # ordinary-person copy (spec line 67: GET history
                # shows empty sources plus non-empty assistant copy)
                # — without the row, the SPA's openThread reconcile
                # replaces its optimistic turn with the server's
                # empty list and the failed exchange vanishes.
                try:
                    store.append_run(
                        RunRecord(
                            id=run_id,
                            thread_id=thread_id,
                            question=body.question,
                            status="refuse",
                            status_reason=outcome.refuse_reason,
                            answer="",
                            citations=[],
                            thinking_text="",
                            thinking_duration_ms=outcome.thinking_ms,
                            tool_summaries=[],
                            assistant_message=outcome.refuse_copy or "",
                            # Stream error taxonomy (auth / network /
                            # ...). ``None`` when the run did not end
                            # on the refuse branch; the field default
                            # (``""``) carries the same meaning so the
                            # SPA never sees an unexpected ``None``.
                            status_classified=outcome.refuse_kind or "",
                        )
                    )
                except ThreadNotFoundError:
                    # No extra frame here — the engine's terminal
                    # frames already closed the wire (the normal
                    # branch below can still add one because its
                    # frames have not been emitted yet).
                    return
                # Mid-flight refuse persisted a terminal row but the
                # LangGraph checkpoint may still list pending next
                # nodes. Finalize via the graph seam so GET does not
                # report ``pending_checkpoint: true`` above a terminal
                # turn (the tracked change). Defensive ``hasattr`` mirrors
                # ``delete_thread`` / ``pending_nodes`` wiring tolerance.
                if hasattr(graph, "finalize_abandoned_run"):
                    graph.finalize_abandoned_run(
                        thread_id,
                        status="refuse",
                        status_reason=outcome.refuse_reason,
                    )
                return
            payload = _run_to_response_payload(outcome.final_state)
            # Persist the run record so the GET turn table surfaces
            # it without re-invoking the graph. T4 (#37): the
            # thinking duration rides the row so a reload restores
            # the collapsed one-line duration label.
            try:
                store.append_run(
                    RunRecord(
                        id=run_id,
                        thread_id=thread_id,
                        question=body.question,
                        status=payload["status"],
                        status_reason=payload["reason"],
                        answer=payload["answer"],
                        citations=payload["citations"],
                        thinking_text=payload["thinking_text"],
                        thinking_duration_ms=outcome.thinking_ms,
                        tool_summaries=payload["tool_summaries"],
                        assistant_message=payload["assistant_message"],
                        track_decision=dict(payload.get("track_decision") or {}),
                    )
                )
            except ThreadNotFoundError:
                # The thread was deleted mid-stream; do NOT
                # resurrect metadata. Yield a single error frame
                # so the SPA surfaces the state, then stop.
                yield _format_sse("status", {
                    "run_id": run_id,
                    "status": "refuse",
                    "reason": "thread_not_found",
                    "assistant_message": "Thread was deleted while the run was in flight.",
                })
                return
            # Terminal frames per spec line 30: status -> answer ->
            # citations. Shared with the T4 resume route so the two
            # emitters cannot drift. T4 (#37): the status frame
            # carries the thinking duration for the live collapse
            # label.
            yield from _terminal_sse_frames(
                run_id=run_id,
                payload=payload,
                thinking_type=thinking_type,
                thinking_ms=outcome.thinking_ms,
                answer_reset=outcome.answer_reset,
            )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/threads/{thread_id}/resume")
    def resume_thread(
        thread_id: str,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """T4 crash resume (spec line 39 / ADR 0026 / ADR 0018).

        Continues an interrupted run from the LAST COMPLETE
        host-graph node via ``graph.stream(None, config=...)`` —
        LangGraph's ``input=None`` contract means "resume from the
        last checkpoint". No new ``HumanMessage`` is appended (the
        user turn is never deleted nor duplicated), and a completed
        ``model_node`` message (full content + parsed ``tool_calls``,
        per spec line 37) is NOT regenerated: the checkpointer
        resumes at the pending node (``tools`` after a model-complete
        crash).

        Rejection order mirrors the send path so every failure mode
        resolves BEFORE any SSE header:

        * 404 - unknown thread metadata row;
        * 409 - a run is currently in flight on this thread
          (lock probe; ``application/json`` body, no stream);
        * 204 - nothing to resume: the prior run reached a terminal
          node (LangGraph's ``stream(None)`` on a terminal
          checkpoint is a documented no-op and the spec forbids
          calling it);
        * 503 - ingest missing (shared handler, raised by the graph
          dependency itself).
        """
        # Unknown thread -> 404 BEFORE any probe or stream.
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )

        # Concurrent-run guard BEFORE StreamingResponse headers, same
        # shape as send_message's T3 fix: raising here lets the typed
        # exception handler emit 409 with application/json.
        if hasattr(graph, "is_locked") and graph.is_locked(thread_id):
            raise ConcurrentRunError(
                f"another run is already in flight on thread_id={thread_id!r}"
            )

        # Nothing to resume -> 204 No Content. A terminal checkpoint
        # (``pending_nodes() == ()``) means the prior run finished;
        # LangGraph's ``stream(None)`` would be a no-op and the spec
        # forbids that path. Also covers a never-sent thread (no
        # checkpoint at all).
        if not hasattr(graph, "pending_nodes") or not graph.pending_nodes(thread_id):
            return Response(status_code=204)

        # Mint a NEW run id for the resumed remainder. The prior
        # run's turn row already exists in the store; this row holds
        # whatever the remainder produces. The SSE ``status`` frame
        # carries this id so the SPA can reconcile its optimistic
        # turn with the persisted rows.
        run_id = str(uuid.uuid4())

        def event_stream() -> Iterator[str]:
            """Yield the resumed remainder on the SAME delta wire.

            Drives ``graph.stream(None, ...)`` through the shared
            ``_stream_run_frames`` engine, so the remainder's
            thinking / answer deltas stream live with the same
            per-chunk granularity as the send path (T2, #36) — not
            one dump after the remainder finishes.

            The dedup set is seeded with the pre-resume checkpoint's
            message ids: the first ``values`` frame replays the
            checkpointed messages (the crashed run's HumanMessage +
            complete ``AIMessage``), and re-emitting their frames
            would duplicate what the pre-crash stream already sent.
            """
            seen_message_ids: set[str] = set()
            if hasattr(graph, "checkpoint_message_ids"):
                seen_message_ids |= graph.checkpoint_message_ids(thread_id)
            outcome = yield from _stream_run_frames(
                graph,
                run_id=run_id,
                input_value=None,
                config={"configurable": {"thread_id": thread_id}},
                seen_message_ids=seen_message_ids,
                thread_id=thread_id,
            )

            if outcome.final_state is None:
                # Mid-flight raise on a RESUME: unlike the send path,
                # persist nothing — when the original send row exists
                # it already tells the story, and appending a refuse
                # row per failed Resume click would spam the turn
                # table. KNOWN GAP (follow-up): after a process death
                # there is no send row at all, so a resume that also
                # raises leaves the question invisible in history;
                # fixing that needs the checkpointed HumanMessage,
                # which this route does not carry.
                return
            payload = _run_to_response_payload(outcome.final_state)
            try:
                store.append_run(
                    RunRecord(
                        id=run_id,
                        thread_id=thread_id,
                        question=f"[resumed] {thread.title}"[:_TITLE_MAX_CHARS],
                        status=payload["status"],
                        status_reason=payload["reason"],
                        answer=payload["answer"],
                        citations=payload["citations"],
                        thinking_text=payload["thinking_text"],
                        thinking_duration_ms=outcome.thinking_ms,
                        tool_summaries=payload["tool_summaries"],
                        assistant_message=payload["assistant_message"],
                        track_decision=dict(payload.get("track_decision") or {}),
                    )
                )
            except ThreadNotFoundError:
                yield _format_sse("status", {
                    "run_id": run_id,
                    "status": "refuse",
                    "reason": "thread_not_found",
                    "assistant_message": "Thread was deleted while the run was resuming.",
                })
                return
            # Terminal frames per spec line 30 (shared emitter). The
            # resume's thinking duration is the REMAINDER's, not the
            # crashed prefix's (that time is unknowable post-crash).
            yield from _terminal_sse_frames(
                run_id=run_id,
                payload=payload,
                thinking_ms=outcome.thinking_ms,
                answer_reset=outcome.answer_reset,
            )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/corpus")
    def corpus() -> dict:
        """List the frozen 12 filings from the real catalog."""
        return list_filings()

    @router.get("/status", response_model=WorkspaceStatusResponse)
    def workspace_status() -> dict:
        """The Sidebar strip's boot-scoped surface (T10, ADR 0033).

        No ``Depends``: the read seam is fin-local config plus the
        in-process catalog, so the handler needs neither the
        compiled graph nor the thread store. It builds no MCP
        client, issues no health-check RPC, and makes no cross-repo
        call — the reachability value comes from the same probe the
        boot warning logs (``fin.host.operator_env``).

        Every state is a 200 with all four fields populated. The
        route has no failure branch to hide a blank strip behind.
        """
        return {
            "retrieve_reachability": probe_retrieve_reachability(),
            "retrieve_adapter_kind": resolve_retrieve_adapter_kind(),
            "model_identity": _model_identity(),
            "corpus_filing_count": _corpus_filing_count(),
        }

    return router


# ----- mount helper -----


def mount(app: FastAPI, *, get_graph, get_thread_store) -> None:
    """Mount the workspace routes onto an existing FastAPI app."""
    app.include_router(
        build_router(get_graph=get_graph, get_thread_store=get_thread_store)
    )


__all__ = [
    "build_router",
    "mount",
]
