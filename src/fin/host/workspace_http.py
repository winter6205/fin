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
                                          spec line 30). T2f graph
                                          gate: a send whose named
                                          attachment is not ready yet
                                          opens the run, the gate
                                          node starts-or-joins the
                                          durable job and PAUSES the
                                          run on an ``interrupt``;
                                          the wire closes after the
                                          ``attachment_wait`` frame
                                          (it carries the run id),
                                          and the boot auto-resumer
                                          drives that same checkpoint
                                          run id when the job
                                          settles — no operator
                                          resend.
* ``POST   /api/threads/{id}/resume``   - crash resume on the SAME
                                          delta wire (T2, #36).
* ``POST   /api/threads/{id}/stop``     - cooperative stop of the
                                          in-flight run (T4 workflow,
                                          the tracked change): flips the run's stop
                                          token, the SSE generator
                                          closes the graph stream and
                                          writes a terminal ``refuse``
                                          / ``stopped`` turn row. No
                                          HumanMessage is appended and
                                          the per-thread lock is
                                          released, so a later send is
                                          accepted while a CONCURRENT
                                          one is still 409.
* ``GET    /api/threads/{id}/export``   - the thread's comparison or
                                          citation bundle as a file
                                          download (T4 workflow, the tracked change).
                                          Comparison exports bind to
                                          ``fin.comparison.v1``; a
                                          missing run is a typed 404.
* ``POST   /api/threads/{id}/runs/{run_id}/feedback``
                                       - the operator's thumbs
                                          up/down for one run (T8).
                                          A side record keyed by
                                          ``run_id``; flipping a thumb
                                          never touches the run's
                                          citations (ADR 0035/0036).
* ``GET    /api/watchlist``             - watched issuers (+ form
                                          types); ``POST`` adds,
                                          ``DELETE /{watch_id}``
                                          removes (T4 workflow, the tracked change).
* ``GET    /api/notices``               - in-app notices for watched
                                          filings in the LOCAL catalog
                                          and configured filing source; ``POST
                                          /{notice_id}/read`` dismisses
                                          one. Workspace-only — nothing
                                          is emailed or posted out.
* ``GET    /api/corpus``                - real ``list_filings()`` over
                                          the catalog metadata table
                                          (ADR 0047).
* ``POST   /api/corpus/ingest``         - the corpus-ingest door
                                          (corpus-serve-ingest T3 /
                                          ADR 0047): multipart upload
                                          -> parse -> 切分 (params
                                          table) -> push via the
                                          PushPort -> invalidate stale
                                          blocks -> register the
                                          filing in the T2 catalog
                                          (``record_filings``). Named
                                          failures:
                                          ``missing_file`` /
                                          ``empty_source`` (4xx empty),
                                          ``format_unspecified`` (422),
                                          ``unsupported_format`` (415),
                                          ``ingest_upload_too_large``
                                          (413), ``invalid_doc_type`` /
                                          ``document_id_missing`` (422),
                                          push/invalidation outage
                                          codes (503). A catalog
                                          registration failure after
                                          the push lands is the named
                                          ``catalog_warning`` field on
                                          the 200 — never rolled back,
                                          never silent. The ONLY product
                                          surface that calls 切分;
                                          never a research tool.
* ``GET    /api/status``                - the boot-scoped status
                                          surface the left corner
                                          renders (ADR 0033, amended):
                                          knowledge MCP three-state
                                          (缺/坏/可达) + adapter kind +
                                          model slug + corpus filing
                                          count + the bound 资料集合
                                          name (T4 mirror of iknow's
                                          constant; ``unbound`` when
                                          not bound). Its own handler
                                          with no dependencies — never
                                          a branch inside send or
                                          GET-thread.
* ``GET    /api/observability``         - the per-process counters
                                          surface (T9): terminal-status
                                          counts (``done`` / ``refuse``
                                          / ``step_cap``), latency
                                          aggregates, and empty-hit
                                          rewrite-retry counters.
                                          Counters only — never per-run
                                          data, never question / answer
                                          text, never credentials
                                          (ADR 0033 spirit).

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

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.types import Command
from pydantic import BaseModel, Field

from fin.corpus.catalog import list_filings, record_filings
from fin.corpus.errors import (
    CatalogCorruptError,
    CatalogRowError,
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.ingest_endpoint import (
    IngestConcurrentRunError,
    IngestDocumentIdMissingError,
    IngestDurableCommitError,
    IngestEndpoint,
    IngestedDocument,
    IngestFormatUnspecifiedError,
    IngestInvalidDocTypeError,
    IngestObjectCleanupError,
    IngestUploadTooLargeError,
)
from fin.corpus.ingest_state import IngestState
from fin.corpus.ingest_state_errors import UnknownJobError
from fin.corpus.ingest_worker import IngestWorker
from fin.corpus.index_job import INDEX_JOBS_FILE, IndexJobLedger, IndexJobTracker
from fin.corpus.ocr import TesseractEngine
from fin.corpus.parsers import (
    NullLayout,
    PageParseResult,
    PageRegionParser,
)
from fin.corpus.pushed_records import PushedRecordsStore
from fin.host.attachment_budgets import AttachmentBudgets
from fin.host.attachment_layout import (
    HeronLayoutConfig,
    HeronPageLayout,
    LayoutRecordRunner,
)
from fin.host.attachment_prep_worker import (
    ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS,
    AttachmentPrepWorker,
    PeriodicDrainWorker,
    drain_periodically,
)
from fin.host.attachment_prepare import (
    ATTACHMENT_PAGE_LANE_UNWIRED,
    AttachmentCleanupError,
    AttachmentNotFoundError,
    AttachmentNotReadyError,
    AttachmentPreparationService,
)
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
)
from fin.host.attachment_vision import ChatModelVisionAdapter
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER, strip_hidden_bounce_marker
from fin.host.collection_bind import resolve_bound_collection_name
from fin.host.export import (
    ExportError,
    UnsupportedExportError,
    build_export,
)
from fin.host.feedback import (
    FeedbackPersistenceError,
    InvalidFeedbackError,
    MAX_RUN_ID_LENGTH,
    default_feedback_store,
)
from fin.host.filing_monitor import FilingMonitor, default_filing_monitor
from fin.host.filing_monitor_errors import FilingSourceUnavailableError
from fin.host.graph import (
    ATTACHMENT_PREPARATION_INTERRUPT_KIND,
    ConcurrentRunError,
    KnowledgeMCPFaultError,
    analysis_from_tool_message,
    comparison_from_tool_message,
)
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_STATES,
    knowledge_mcp_gate_closed,
    knowledge_mcp_state,
    resolve_bound_retrieve_adapter_kind,
)
from fin.host.memory_dream import run_dream_after_turn
from fin.host.memory_port import build_memory_port
from fin.host.named_failures import (
    TIER_UNAVAILABLE_CODE,
    RetrieveTier,
    unavailable_tier,
)
from fin.host.observability import (
    ObservabilityCollector,
    ObservabilityError,
)
from fin.host.operator_env import resolve_retrieve_adapter_kind
from fin.host.run_payload import RunResponsePayload, _run_to_response_payload
from fin.host.stream_map import (
    InlineFilterState,
    ThinkingClock,
    format_tool_summary,
    reasoning_delta_from_chunk,
    split_stream_delta,
    strip_thinking_from_answer,
)
from fin.host.run_control import RunControl, StopToken
from fin.host.state import reset_input_state
from fin.host.thread_store import (
    RunRecord,
    ThreadNotFoundError,
    ThreadRecord,
    ThreadStore,
)
from fin.host.watchlist import (
    InvalidWatchError,
    JsonFileWatchlistStore,
    Notice,
    NoticeNotFoundError,
    WatchEntry,
    WatchNotFoundError,
    default_watchlist_store,
    refresh_notices,
)
from fin.host.workspace_registry import default_workspace_root
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.invalidation_port.errors import InvalidationError
from fin.retrieve.invalidation_port.ledger import InvalidationLedger
from fin.retrieve.push_batching import PushJournal
from fin.retrieve.push_port import PushError


logger = logging.getLogger(__name__)


# ----- default 工作区 resolution (Focus 2 / SC4, single default) -----


# The one default 工作区 root, resolved per request. spec Focus 3 /
# SC8 / ADR 0049: HTTP is the workspace-inject seam — every send /
# resume / ask invoke carries ``configurable.workspace_root`` =
# ``$HOME/.fin/default``. ``default_workspace_root`` mkdir's the
# directory on first resolve (spec Focus 2). Tests override this
# dependency to point at a ``tmp_path`` home so the suite never
# touches the real ``$HOME``.


def default_workspace_root_for_process() -> Path:
    """Resolve the process-default 工作区 from ``$HOME``.

    Reads ``$HOME`` at call time (NEVER ``Path.cwd()`` — spec SC3)
    and delegates to
    :func:`fin.host.workspace_registry.default_workspace_root` so
    the SSOT for the ``<home>/.fin/default`` shape stays in the
    registry module.
    """
    home = os.environ.get("HOME") or str(Path.home())
    return default_workspace_root(home)


def get_workspace_root() -> Path:
    """FastAPI dependency returning the resolved default 工作区 root.

    Production resolves from ``$HOME`` per request (cheap; the mkdir
    is idempotent). Tests override via
    ``app.dependency_overrides[get_workspace_root]`` to inject a
    ``tmp_path``-rooted directory.
    """
    return default_workspace_root_for_process()


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


def _dream_hook_kwargs(port: Any) -> dict[str, Any]:
    """Probe ``port`` for the dual-gate wiring the per-turn hook needs.

    ADR 0048 2026-09-06 (SC21): the SSE routes must not unconditionally
    enqueue ``port.dream()``; they call :func:`run_dream_after_turn`
    with the gate wiring the runtime port exposes. A port that does
    not expose any of the three required callables (gate-state store,
    active-facts counter, dream factory) yields an empty kwargs dict,
    and the hook falls back to a safe no-op — the dream is not fired.
    Tests' recording port exposes all three so the wiring pins the
    gated behaviour end-to-end.
    """
    gate_state = getattr(port, "gate_state_store", None)
    if gate_state is None:
        return {}
    facts_count = getattr(port, "_facts_count", None)
    if not callable(facts_count):
        return {}
    dream_factory = getattr(port, "_build_dream", None)
    if not callable(dream_factory):
        return {}
    return {
        "gate_state_store": gate_state,
        "active_facts_provider": facts_count,
        "dream_factory": dream_factory,
    }


def _register_dream_background(
    background: BackgroundTasks,
    memory_port: Any,
    thread_id: str,
) -> None:
    """Schedule the per-turn dream hook on ``background`` (SC21).

    Dream runs AFTER the SSE stream is exhausted, never inside a graph
    node (CONTEXT.md:46). Registering before the response is returned
    means every exit path from the generator still gets its chance;
    the dual gate inside the hook decides whether it actually runs
    (ADR 0048 2026-09-06 / SC21).
    """
    background.add_task(
        run_dream_after_turn,
        memory_port,
        thread_id=thread_id,
        **_dream_hook_kwargs(memory_port),
    )


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
    # T4 workflow (the tracked change): the operator pressed Stop and the engine
    # closed the graph stream between frames. Distinct from
    # ``refuse_reason`` (a failure) — a stop is a deliberate end, so
    # the caller writes the ``stopped`` turn row and its own terminal
    # frames rather than a stream-error refusal.
    stopped: bool = False
    # search-mode host T2: the run PAUSED on a 检索档 choice window
    # (LangGraph HITL interrupt, ADR 0062). The engine already emitted
    # the ``choice_window`` SSE frame; the caller persists NOTHING (a
    # paused run is not a finished turn — the resumed remainder's
    # completion writes the row) and closes the stream.
    choice_window: dict[str, Any] | None = None
    # T2f durable graph gate: the run PAUSED on the attachment
    # preparation interrupt (payload kind
    # ``attachment_preparation``). The engine already emitted the
    # ``attachment_wait`` SSE frame carrying the run id; the caller
    # persists NOTHING (same pause posture as ``choice_window``) — the
    # auto-resumer drives the SAME checkpoint run id to completion once
    # the durable preparation job settles.
    attachment_wait: dict[str, Any] | None = None


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


# The scope label one attach carries; the byte cap is NOT here — it is
# the deployed :class:`~fin.host.attachment_budgets.AttachmentBudgets`
# value the route reads through its dependency.
ATTACHMENT_SCOPE: Final[str] = "thread_attach"


def _attachment_wire_record(record: AttachmentRecord) -> dict[str, Any]:
    """Serialize one attachment row for the workspace surface.

    Every attachment door answers with this shape, so a client that polled
    the upload response and then read the status cannot see two different
    field sets for the same row.
    """
    return {
        "attachment_id": record.attachment_id,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "scope": ATTACHMENT_SCOPE,
        "status": record.status,
        "version": record.version,
        "unit_count": record.unit_count,
        "searchable": record.searchable,
        "failure_reason": record.failure_reason,
    }


# A locator is one opaque path segment: bounded, and free of the whitespace
# and control characters a client can only have produced by accident. The
# bound is generous next to a uuid, tight enough that a garbage path cannot
# reach a database query carrying megabytes it will never match.
_MAX_LOCATOR_CHARS: Final[int] = 512
_LOCATOR_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"[\s\x00-\x1f\x7f]")


def _locator_is_wellformed(value: str) -> bool:
    """Whether a path segment can name an attachment at all."""
    return (
        bool(value.strip())
        and len(value) <= _MAX_LOCATOR_CHARS
        and _LOCATOR_SHAPE_RE.search(value) is None
    )


def _locator_refusal(*locators: str) -> JSONResponse | None:
    """The refusal every ``{attachment_id}`` door shares for a bad identity.

    A malformed path identity is a client mistake, not an identity miss, so
    every door judges it the same way and before any lookup or write: 400
    ``invalid_locator``, never a query the durable store can never match.
    ``None`` means the segments are well formed and the door may continue.
    """
    if all(_locator_is_wellformed(locator) for locator in locators):
        return None
    return JSONResponse(status_code=400, content={"error": "invalid_locator"})


def _attachment_delete_path(thread_id: str, attachment_id: str) -> str:
    """The one URL of a single attachment's delete door."""
    return f"/api/threads/{thread_id}/attachments/{attachment_id}"


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
    # ADR 0047 **thread attach**: ids returned by
    # ``POST /threads/{id}/attachments``. An id stays referenceable for the
    # whole thread lifetime, so a later turn reuses it; an unknown id or
    # another thread's id is the named 400 ``attachment_not_found`` BEFORE
    # any graph invoke. A stored ``uploaded`` / ``parsing`` id is accepted
    # (T2f): it rides the run as an empty-text item and the graph's
    # preparation gate suspends the run on the durable job and hydrates
    # the text on resume; a ``failed`` id is the named 400
    # ``attachment_not_ready`` until the retry door re-spends it. Attach
    # never pushes: the resolved text rides ``reset_input_state(
    # attachments=...)`` as turn-scoped context and never enters the
    # corpus/ingest path.
    attachment_ids: list[str] = Field(default_factory=list)
    # The explicit 检索档 selector. Empty (the default) is the ordinary
    # send — the model resolves the track as it always has. A caller that
    # NAMES a still-unimplemented 档 (sql 真数据库) gets the named 400
    # BEFORE any run starts; the product never opens a document
    # retrieve dressed as SQL, and never guesses a 档 from the question
    # prose (the wayfinder doc forbids an open-ended 业务意图桶). The enum
    # is derived from the SSOT closed set (:mod:`fin.retrieve.tiers`)
    # rather than re-spelled here, so a 档 added there cannot leave this
    # schema silently accepting the old set — and a bogus token is a 422
    # input error rather than a silently ignored field.
    #
    # What the door accepts IS what the port forwards: the retired letters
    # left the vocabulary on both wires in one stroke (pre-release, no live
    # client), so a ``tiers: ["A"]`` send is a typed 422 that names the bad
    # selector instead of a 200 whose 档 was guessed from a spelling
    # nothing writes anymore.
    tiers: list[RetrieveTier] = Field(default_factory=list)
    # search-mode host T4 (HTTP surface): an explicit Fast 抽图 PROPOSE
    # request. The send seeds the run's ``extract_method_proposal``
    # STATE field with ``{"proposed": "fast"}`` (the graph-side trigger
    # locked in tests/host/test_search_mode_fast_propose.py) so a
    # grounded run pauses on the propose window — the workbench has no
    # other channel to reach it. Omitted (the default) injects nothing:
    # the wire stays byte-identical to the pre-T4 send. Typed as a
    # closed Literal (proposing anything but Fast is not a request the
    # host makes, per ``PROPOSED_EXTRACT_METHOD``) so a bogus token is
    # a 422 input error, not a silently ignored field — same style as
    # ``tiers`` above.
    propose_extract_method: Literal["fast"] | None = None
    # The SWITCH TARGET. The operator pick on the propose
    # window only reaches ``switch_extract_method`` when the request
    # names the exact ``(document_id, version)`` row; both fields are
    # optional and half a target stays a proposal record (the graph
    # gate records ``missing_version_context``), never a guessed
    # apply. Free strings on purpose: the row's existence — not a
    # client-side format — is what the catalog checks.
    propose_document_id: str | None = None
    propose_version: str | None = None


class ResumeRequest(BaseModel):
    """Optional body for ``POST /threads/{id}/resume`` (search-mode host T2).

    ``pick`` answers a pending 检索档 choice window: the run continues
    with ``Command(resume=pick)`` on the SAME SSE wire. The body stays
    OPTIONAL on purpose — a body-less resume keeps the pre-T2 crash-
    resume contract (``graph.stream(None)``) untouched, and against a
    pending window it REDRAWS the ``choice_window`` frame (the SPA's
    reload path).
    """

    pick: str | None = None


class CreateWatchRequest(BaseModel):
    """T4 workflow (the tracked change): one watched issuer, optionally narrowed.

    ``forms`` empty (or omitted) watches every form type — the
    operator asked about the issuer, not about a filing kind. The
    store normalizes casing, so the SPA does not have to.
    """

    issuer: str
    forms: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    """T8 thumbs feedback body: ``{"value": "up" | "down"}``.

    ``value`` is typed as ``str`` and validated IN the route (not by
    Pydantic) so an out-of-enum value answers the typed 400
    ``{"error": "invalid_feedback"}`` the SPA's error path renders —
    the same shape every other workspace route answers with — instead
    of FastAPI's generic 422 envelope.
    """

    value: str


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
    # T3 (enterprise-kb Focus 1 / SC2): the ``fin.comparison.v1``
    # envelopes this run's ``compare_facts`` calls produced. The
    # turn renders a TABLE off this field rather than re-parsing a
    # prose table out of ``answer``. Untyped nested JSON on purpose
    # — the producing tool
    # (``fin.host.tools.compare_facts``) owns and validates the
    # shape, and a read path that 500s on an unexpected key would
    # lose the whole turn. Same precedent as ``track_decision``.
    # Present (and non-empty) on ``refuse`` rows too: on a
    # ``numeric_gate_fail`` the table is the evidence FOR the
    # refusal.
    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    # Trust (the tracked change, spec SC8): the locators this run retrieved or
    # read (``{document_id, version, page}``), copied from the
    # run's evidence before the next run resets it. Wider than
    # ``citations``: a refused turn still shows what it looked at.
    evidence_locators: list[dict[str, Any]] = Field(default_factory=list)
    # T6: structured host analysis envelopes persisted for reload.
    analysis_results: list[dict[str, Any]] = Field(default_factory=list)
    # T8: the operator's latest thumb for this run (``"up"`` /
    # ``"down"``). ``None`` when no thumb was ever set — additive and
    # nullable, so pre-T8 clients that ignore the field keep working.
    # Side record keyed by ``run_id`` (``fin.host.feedback``): it is
    # never part of the run's citations or evidence, so setting or
    # flipping a thumb cannot move the verified answer (ADR 0035/0036).
    feedback: str | None = None


class WorkspaceStatusResponse(BaseModel):
    """The boot-scoped workspace status surface (ADR 0033, amended; T4).

    Five fields, each fixed by an ADR or a plan ticket:

    * ``knowledge_mcp`` — the three-state knowledge-MCP field
      (缺 ``missing`` / 坏 ``fault`` / 可达 ``reachable``) from the
      SAME in-process snapshot the conversation gate reads. It
      REPLACES the former PATH-only ``retrieve_reachability``: the
      ADR 0033 amendment makes the real ``initialize`` + ``tools/list``
      handshake the product truth. It is payload DATA with degraded
      copy behind it, never an HTTP 500 and never an empty body.
    * ``retrieve_adapter_kind`` — which port is behind the seam, so a
      ``fake`` boot is distinguishable from a product boot. It is the
      BOUND port (``fin.host.knowledge_mcp``'s
      ``resolve_bound_retrieve_adapter_kind``), never the raw
      ``FIN_RETRIEVE_ADAPTER`` string: a scripted boot compiles the
      fake port whatever that string names, so publishing the
      configured kind made the operator-facing classifier report
      GATED — "create / open / send will 409" — for a process whose
      gate is open (ADR 0033 amendment 2026-09-20). This field is the
      adapter half of the same predicate the conversation gate uses.
    * ``model_identity`` — the resolved ``FIN_MODEL`` slug ONLY.
      ``FIN_BASE_URL`` and key material stay off this wire.
    * ``corpus_filing_count`` — from the same in-process catalog that
      serves ``GET /api/corpus``. Zero is a valid rendered value.
    * ``bound_collection_name`` — T4 (``specs/first-closed-loop.md``
      "工作台 shows the bound 资料集合 identity"): the 资料集合 this
      process is bound to, or ``unbound`` when it is bound to none.
      The v1 bind is the one process-wide collection ADR 0049 settles
      (spec Open Question 2 → yes), and the name is a documented
      MIRROR of iknow-memory's ``SOURCE_DOC_COLLECTION_NAME`` because
      no MCP tool returns a collection identity — see
      :mod:`fin.host.collection_bind`. It is NOT a 就绪 signal and is
      never derived from ``corpus_filing_count`` (SC7). The ``unbound``
      sentinel is itself hand-mirrored in the SPA
      (``web/src/knowledgeMcp.ts``), which carries no copy of the
      host's name space by design — a rename here must move both.

    Deliberately absent (ADR 0033 OUT): ``step_budget_live``,
    ``token_usage_absolute``, ``soft_track_confidence`` — run- and
    turn-scoped values belong to the SSE contract, not to a boot
    surface — plus any "% of context window" figure and any skill
    list (plan exclusion).
    """

    knowledge_mcp: Literal["missing", "fault", "reachable"]
    retrieve_adapter_kind: Literal["mcp", "fake"]
    model_identity: str
    corpus_filing_count: int
    bound_collection_name: str


class Occupancy(BaseModel):
    """Thread-detail token occupancy (ADR 0044 T3).

    ``ratio`` is the shared compactor figure (the compactor clamps
    to [0, 1]); ``estimated_tokens`` and ``budget_tokens`` are
    exposed so the UI can render an absolute figure without
    re-deriving the budget.
    """

    estimated_tokens: int
    budget_tokens: int
    ratio: float


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
    # T2f: WHAT a paused run waits for while ``pending_checkpoint`` is
    # true — ``"attachment_preparation"`` (durable prep job owns the
    # resume) or ``"choice_window"`` (the operator's pick owns it);
    # ``None`` for a plain crash checkpoint. The route computes it into
    # the payload dict, so the response model must declare it or
    # FastAPI filters the key off the wire.
    pending_interrupt_kind: str | None = None
    # T3 (ADR 0044): token occupancy from the SAME figure the
    # compaction gate computes (``graph.occupancy(thread_id)`` ->
    # ``OccupancyCompactor.occupancy`` over the checkpointed
    # messages vs the session budget). ``None`` for a thread with
    # no checkpoint (never sent on) — the UI stays neutral rather
    # than showing a fabricated 0%. This is the thread-scoped
    # surface ONLY: ADR 0033 keeps ``GET /api/status`` free of any
    # occupancy / context-window figure.
    occupancy: Occupancy | None = None
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


def _serialize_run(
    run: RunRecord,
    *,
    in_flight: bool = False,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Translate a ``RunRecord`` into the turn-table JSON shape.

    T4: ``in_flight`` carries the live lock-probe result for the
    thread. Only the newest row can legitimately be ``True`` — the
    GET route passes the probe result for the last row and
    ``False`` for all earlier ones.

    T8: ``feedback`` is the latest thumb for ``run.id`` looked up
    from the feedback store (one ``list_feedback()`` for the whole
    turn table, not a query per row).
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
        # T3: structured comparison envelopes. Deep-copied because
        # the entries nest (rows -> cells), so a shallow ``dict()``
        # would still share the inner lists with the store.
        "comparisons": copy.deepcopy(list(run.comparisons or [])),
        # Trust (the tracked change): copied so a caller cannot mutate the store's
        # list through the serialized view.
        "evidence_locators": [dict(loc) for loc in run.evidence_locators],
        "analysis_results": copy.deepcopy(list(run.analysis_results or [])),
        # T8: the latest thumb for this run (``None`` when unset).
        "feedback": feedback,
    }


def _serialize_watch(entry: WatchEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "issuer": entry.issuer,
        "forms": list(entry.forms),
        "created_at": entry.created_at.isoformat(),
    }


def _serialize_notice(notice: Notice) -> dict[str, Any]:
    return {
        "id": notice.id,
        "watch_id": notice.watch_id,
        "issuer": notice.issuer,
        "form": notice.form,
        "document_id": notice.document_id,
        "period": notice.period,
        "created_at": notice.created_at.isoformat(),
        "read": notice.read,
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


def _tool_result_frames(
    message: ToolMessage,
    pending_args: dict[str, dict[str, Any]] | None = None,
) -> Iterator[tuple[str, Any]]:
    """Yield the ``tool_result`` (plus ``comparison``/``analysis``)
    frames for one ``ToolMessage``.

    Split from :func:`_node_frame_payloads` to keep each function
    under the complexity budget (code-review T2 finding): the
    args-consuming summary belongs to the T1 contract, the
    structured envelopes to the T3 comparison wire.
    """
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
    call_args = (
        pending_args.get(tool_call_id) if pending_args is not None else None
    )
    if pending_args is not None and tool_call_id:
        pending_args.pop(tool_call_id, None)
    yield (
        "tool_result",
        {
            "name": tool_name,
            "summary": strip_hidden_bounce_marker(
                format_tool_summary(tool_name, result=parsed, args=call_args)
            ),
            "tool_call_id": tool_call_id,
        },
    )
    # T3: the comparison table rides its own frame so the SPA
    # does not have to re-parse ``tool_result``. Reuses the
    # graph's extractor so the wire entry and the persisted
    # ``RunRecord.comparisons`` entry are byte-identical.
    comparison = comparison_from_tool_message(message)
    if comparison is not None:
        yield ("comparison", comparison)
    analysis = analysis_from_tool_message(message)
    if analysis is not None:
        yield ("analysis", analysis)


def _node_frame_payloads(
    message: BaseMessage,
    pending_args: dict[str, dict[str, Any]] | None = None,
) -> Iterator[tuple[str, Any]]:
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
      human-readable summary (never the raw JSON payload), plus —
      for ``compare_facts`` only — one ``comparison`` frame
      carrying the ``fin.comparison.v1`` envelope (T3). That
      envelope is the sole structured payload on this wire: it is
      tool-computed data, not model prose, and the live view
      renders it as a table before the answer arrives. A typed
      EXIT (no columns) emits no frame.
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
            call_id = call.get("id", "")
            # Register id -> args so the matching ToolMessage frame
            # can summarize with the same args the persist path uses
            # (turn-trace-folds: live one-liners match GET history).
            if pending_args is not None and call_id:
                pending_args[str(call_id)] = dict(args)
            yield (
                "tool_call",
                {
                    "name": call.get("name", ""),
                    "args": args,
                    "tool_call_id": call_id,
                },
            )
        return
    if isinstance(message, ToolMessage):
        yield from _tool_result_frames(message, pending_args)


def _stream_run_frames(
    graph: Any,
    *,
    run_id: str,
    input_value: Any,
    config: dict[str, Any],
    seen_message_ids: set[str],
    thread_id: str | None = None,
    stop_token: StopToken | None = None,
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

    ``stop_token`` (T4 workflow, the tracked change) is checked between frames: a
    set token stops consuming the graph stream and returns
    ``stopped=True`` WITHOUT emitting terminal frames, because a stop
    is not a stream failure and the caller owns the ``stopped`` copy.
    The graph iterator is closed explicitly in a ``finally`` so
    ``_LockedGraph.stream`` runs its own ``finally`` and releases the
    per-thread lock right away — the composer must be usable the
    moment the wire closes, not whenever the iterator is collected.
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
    # tool_call_id -> args, registered when the AIMessage's
    # ``tool_call`` frames are emitted and consumed by the matching
    # ``tool_result`` frame so its summary carries the same args the
    # graph persist path passes (turn-trace-folds T1: live one-liners
    # match GET history, e.g. retrieve shows its query).
    pending_args: dict[str, dict[str, Any]] = {}

    def _answer_payload(text: str) -> dict[str, Any]:
        nonlocal pending_answer_reset
        data: dict[str, Any] = {"text": text}
        if pending_answer_reset:
            data["reset"] = True
            pending_answer_reset = False
        return data

    stopped = False
    # search-mode host T2: set when the run paused on a choice window
    # (the final ``values`` frame carries ``__interrupt__``).
    choice_window: dict[str, Any] | None = None
    # T2f: set when the run paused on the attachment preparation gate.
    attachment_wait: dict[str, Any] | None = None
    graph_stream = graph.stream(
        input_value,
        config=config,
        stream_mode=["messages", "values"],
    )
    try:
        for mode, payload in graph_stream:
            if stop_token is not None and stop_token.is_set():
                stopped = True
                break
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
                    for event, data in _node_frame_payloads(
                        message, pending_args
                    ):
                        yield _frame(event, data)
                # A paused HITL interrupt surfaces on the LAST
                # ``values`` frame under the reserved ``__interrupt__``
                # key (LangGraph contract). The choice-window payload is
                # the frame's data verbatim — the host/HTTP contract the
                # SPA paints (spec search-mode-host SC2, stub allowed).
                # T2f: the attachment-preparation pause shares this loop
                # but not this frame — the SPA does not answer it, so it
                # rides a distinct ``attachment_wait`` event carrying
                # the run id the auto-resumer will drive.
                for pending in payload.get("__interrupt__") or ():
                    value = getattr(pending, "value", None)
                    if not isinstance(value, dict):
                        continue
                    if value.get("kind") == "choice_window":
                        choice_window = value
                        yield _frame("choice_window", value)
                    elif (
                        value.get("kind")
                        == ATTACHMENT_PREPARATION_INTERRUPT_KIND
                    ):
                        attachment_wait = value
                        yield _frame(
                            "attachment_wait", {"run_id": run_id, **value}
                        )

        # Stream end: release any held-back inline-filter fragment so
        # the live view never silently loses trailing text. Skipped on
        # a stop — the operator asked for the run to end, and the
        # caller's terminal empty answer frame clears the draft.
        if not stopped:
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
    finally:
        # Close the graph iterator explicitly. On the stop path the
        # ``break`` above leaves it suspended, and only ``close()``
        # runs ``_LockedGraph.stream``'s ``finally`` — i.e. releases
        # the per-thread lock. Without it the composer would stay
        # disabled until the generator was garbage-collected.
        close = getattr(graph_stream, "close", None)
        if callable(close):
            close()
    clock.close()
    return RunStreamOutcome(
        final_state=final_state,
        thinking_ms=clock.total_ms,
        answer_reset=pending_answer_reset,
        stopped=stopped,
        choice_window=choice_window,
        attachment_wait=attachment_wait,
    )


def _resume_run_stream(
    graph: Any,
    store: ThreadStore,
    *,
    run_control: RunControl,
    thread_id: str,
    run_id: str,
    resume_input: Any,
    resume_config: dict[str, Any],
) -> Iterator[str]:
    """Drive one resumed run on the shared delta wire; persist on finish.

    THE one resume path, extracted from ``POST /threads/{id}/resume`` so
    an operator resume and the T2f attachment auto-resumer cannot drift:
    the auto-resumer consumes the generator to exhaustion in the
    background and DISCARDS the frames — the resumed turn row is the
    half it needs, the live wire is the half only an HTTP caller reads.

    ``resume_input`` is LangGraph's ``Command(resume=pick)`` for an
    operator answer, a ``Command(resume=<string>)`` for the
    auto-resumer (the gate node ignores the value, and
    ``Command(resume=None)`` trips an ``UnboundLocalError`` in this
    langgraph version), or ``None`` for the plain crash resume.

    The dedup set is seeded with the pre-resume checkpoint's message
    ids: the first ``values`` frame replays the checkpointed messages
    (the crashed run's HumanMessage + complete ``AIMessage``), and
    re-emitting their frames would duplicate what the pre-crash stream
    already sent.
    """
    seen_message_ids: set[str] = set()
    if hasattr(graph, "checkpoint_message_ids"):
        seen_message_ids |= graph.checkpoint_message_ids(thread_id)
    # A resumed remainder is an in-flight run on this thread too, so
    # Stop must reach it (the composer is disabled for its whole
    # duration).
    stop_token = run_control.begin(thread_id, run_id)
    try:
        outcome = yield from _stream_run_frames(
            graph,
            run_id=run_id,
            input_value=resume_input,
            config=resume_config,
            seen_message_ids=seen_message_ids,
            thread_id=thread_id,
            stop_token=stop_token,
        )
    finally:
        run_control.end(stop_token)

    if outcome.stopped:
        # No row: the original send's row already tells the story, and
        # one row per stopped Resume click would spam the turn table
        # (same policy as this path's mid-flight refuse branch).
        yield from _stopped_terminal_frames(
            run_id, thinking_ms=outcome.thinking_ms
        )
        return

    if outcome.choice_window is not None:
        # Re-draw (body-less resume onto a pending window) or an invalid
        # pick that REOPENED the window: the frame is out, the run is
        # still paused, so persist nothing and close — the operator
        # picks again.
        return

    if outcome.attachment_wait is not None:
        # T2f: the resumed remainder paused on the attachment gate
        # AGAIN (e.g. a re-upload changed the content version
        # mid-resume). Same posture as the send route's pause: persist
        # nothing and close; the next settled sweep drives this run.
        return

    if outcome.final_state is None:
        # Mid-flight raise on a RESUME: unlike the send path, persist
        # nothing — when the original send row exists it already tells
        # the story, and appending a refuse row per failed Resume click
        # would spam the turn table. After process death with no send
        # row, a resume that also raises still leaves history without a
        # row (success-path resume below does load the checkpoint
        # HumanMessage when final_state is present).
        return
    payload = _run_to_response_payload(outcome.final_state)
    resume_question = (
        graph.checkpoint_last_human_text(thread_id)
        if hasattr(graph, "checkpoint_last_human_text")
        else None
    ) or ""
    try:
        store.append_run(
            RunRecord(
                id=run_id,
                thread_id=thread_id,
                question=resume_question,
                status=payload["status"],
                status_reason=payload["reason"],
                answer=payload["answer"],
                citations=payload["citations"],
                thinking_text=payload["thinking_text"],
                thinking_duration_ms=outcome.thinking_ms,
                tool_summaries=payload["tool_summaries"],
                assistant_message=payload["assistant_message"],
                track_decision=dict(payload.get("track_decision") or {}),
                comparisons=list(payload.get("comparisons") or []),
                analysis_results=list(payload.get("analysis_results") or []),
                # SC8: the RESUMED remainder's locators. The crashed
                # prefix's row (when one exists) keeps its own.
                evidence_locators=list(payload["evidence_locators"]),
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
    # Terminal frames per spec line 30 (shared emitter). The resume's
    # thinking duration is the REMAINDER's, not the crashed prefix's
    # (that time is unknowable post-crash).
    yield from _terminal_sse_frames(
        run_id=run_id,
        payload=payload,
        thinking_ms=outcome.thinking_ms,
        answer_reset=outcome.answer_reset,
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


# T4 workflow (the tracked change): the ordinary-person copy a stopped run leaves on
# its turn row. A stop is the operator's own action, so the copy says
# what happened and what is possible next — never an error sentence.
STOPPED_ASSISTANT_MESSAGE: Final[str] = "本轮已停止。可以直接再发一条。"

# ``RunRecord.status`` is a closed enum (``done`` / ``refuse`` /
# ``step_cap``); a stop is not a completed answer, so it lands on
# ``refuse`` with ``status_reason="stopped"`` as the discriminator the
# SPA reads. Adding a fourth status would ripple through the gate, the
# ask payload, and the persisted enum for one presentation difference.
STOPPED_STATUS_REASON: Final[str] = "stopped"


def _stopped_terminal_frames(run_id: str, *, thinking_ms: int) -> Iterator[str]:
    """Terminal ``status`` -> ``answer`` -> ``citations`` for a stop.

    Same fixed order (and same terminal ``citations`` frame) as every
    other run end, so the SPA's reader closes on a stop exactly as it
    does on a refuse. The empty answer frame clears whatever partial
    draft was on screen when the operator pressed Stop.
    """
    yield _format_sse(
        "status",
        {
            "run_id": run_id,
            "status": "refuse",
            "reason": STOPPED_STATUS_REASON,
            "assistant_message": STOPPED_ASSISTANT_MESSAGE,
            "thinking_ms": thinking_ms,
        },
    )
    yield _format_sse("answer", {"text": "", "reset": True})
    yield _format_sse("citations", _citations_wire_payload([]))


def _persist_and_close_stopped_run(
    graph: Any,
    *,
    store: ThreadStore,
    thread_id: str,
    run_id: str,
    question: str,
    thinking_ms: int,
) -> Iterator[str]:
    """Write the stopped turn row, clear the checkpoint, close the wire.

    A stopped run is still a turn the operator can see: without the row
    the SPA's ``openThread`` reconcile would replace its optimistic turn
    with the server's list and the question would vanish (same
    reasoning as the mid-flight refuse row).

    ``finalize_abandoned_run`` mirrors the mid-flight refuse path: the
    LangGraph checkpoint may still list pending nodes after the stream
    was closed, and without this a Resume banner would appear above a
    turn the operator deliberately ended.
    """
    try:
        store.append_run(
            RunRecord(
                id=run_id,
                thread_id=thread_id,
                question=question,
                status="refuse",
                status_reason=STOPPED_STATUS_REASON,
                answer="",
                citations=[],
                thinking_text="",
                thinking_duration_ms=thinking_ms,
                tool_summaries=[],
                assistant_message=STOPPED_ASSISTANT_MESSAGE,
            )
        )
    except ThreadNotFoundError:
        # The thread was deleted while the run was being stopped; do
        # not resurrect it. The frames below still close the wire so
        # the SPA's reader does not hang.
        logger.debug(
            "run %s stop finalized after thread %s was deleted",
            run_id,
            thread_id,
        )
    if hasattr(graph, "finalize_abandoned_run"):
        graph.finalize_abandoned_run(
            thread_id,
            status="refuse",
            status_reason=STOPPED_STATUS_REASON,
        )
    yield from _stopped_terminal_frames(run_id, thinking_ms=thinking_ms)


# T2 (ADR 0044 / plan session-compaction T2): operator receipt copy for
# the ``/compact`` command. Plain-prose Chinese like every other
# operator-facing copy in this file; states what happened and what is
# possible next — never an error sentence on the summary-failure path
# (ADR 0044: the failure must not refuse the operator turn).
COMPACT_DONE_COPY: Final[str] = (
    "已压缩会话历史，较早的检索过程已归入摘要。"
    " 可以继续提问，摘要会保留此前的工作脉络。"
)
COMPACT_NOOP_COPY: Final[str] = (
    "当前会话暂不需要压缩，历史保持不变。"
)
COMPACT_PENDING_COPY: Final[str] = (
    "本线程有一个未完成的回合，暂不压缩。"
    " 可以先恢复或停止该回合，再重试压缩。"
)
COMPACT_SUMMARY_FAILED_COPY: Final[str] = (
    "本次压缩未完成：生成会话摘要时出现问题。"
    " 会话未受影响，可以直接重试或继续提问。"
)


def _run_compact_command(
    *,
    graph: Any,
    store: ThreadStore,
    thread_id: str,
) -> Response:
    """Run the ``/compact`` command and stream its receipt (T2).

    Placement: called from ``send_message`` AFTER the unknown-thread
    404 and BEFORE any graph stream — Soft-Track classify is the
    first graph node (START -> classify), so a command handled here
    is never classified as a user question and never reaches the
    research tools (ADR 0044).

    Concurrency: ``graph.compact_thread`` acquires the SAME
    per-thread lock as ``invoke`` / ``stream`` (raises
    ``ConcurrentRunError`` -> 409 ``application/json`` — but the
    synchronously-raised exception from ``compact_thread`` here
    happens BEFORE the StreamingResponse is returned, so the 409
    body keeps the plain ``application/json`` shape, same as the
    ``is_locked`` probe on the ordinary send path).

    Wire: the SAME fixed-order terminal triple as every other run
    end (``status`` -> ``answer`` -> ``citations``) so the SPA's
    reader closes on ``citations`` with no client-side changes and
    no research-run frames (``tool_call`` / ``tool_result`` /
    streamed ``answer`` deltas) ever appear. The status frame
    carries ``status="done"``, no ``track`` — this turn never went
    through classify.

    Receipt outcomes (copy in this module's constants):

    * ``compacted`` — eviction happened; the answer copy says so.
    * ``noop`` — empty payload or nothing evictable (gate produced
      no update); the checkpoint is untouched.
    * ``summary_failed`` — typed :class:`SessionSummaryError` from
      the gate; graceful receipt, NOT a 500, NOT a refuse; the
      checkpoint is untouched and the thread stays usable.

    Persistence: one ``RunRecord`` row (``status="done"``, empty
    ``track_decision`` — the command never reached classify) so GET
    history shows the receipt turn exactly like any other turn.

    Resume path (``POST .../resume``) deliberately does NOT handle
    ``/compact``: a resume continues an already-pending run whose
    own compaction gate fires before its next LLM call, and a
    crashed run should be resumed or stopped, not compacted under
    its pending checkpoint.

    ``ConcurrentRunError`` from ``compact_thread`` (a run in flight)
    propagates untouched — the plain 409 contract, identical to the
    ``is_locked`` probe on the ordinary send path.
    """
    run_id = str(uuid.uuid4())
    receipt = graph.compact_thread(thread_id)

    if receipt.get("outcome") == "compacted":
        copy = COMPACT_DONE_COPY
    elif receipt.get("outcome") == "summary_failed":
        copy = COMPACT_SUMMARY_FAILED_COPY
        logger.warning(
            "compact command: summary failed on thread_id=%s", thread_id
        )
    elif receipt.get("outcome") == "pending_run":
        copy = COMPACT_PENDING_COPY
    else:
        copy = COMPACT_NOOP_COPY

    def event_stream() -> Iterator[str]:
        try:
            store.append_run(
                RunRecord(
                    id=run_id,
                    thread_id=thread_id,
                    question="/compact",
                    status="done",
                    answer=copy,
                    citations=[],
                    thinking_text="",
                    assistant_message=copy,
                    track_decision={},
                )
            )
        except ThreadNotFoundError:
            # Thread deleted mid-command; the frames below still
            # close the wire (mirrors the stopped-run path).
            logger.debug(
                "compact command on thread %s raced a delete", thread_id
            )
        yield _format_sse(
            "status",
            {
                "run_id": run_id,
                "status": "done",
                "reason": None,
                "assistant_message": copy,
            },
        )
        yield _format_sse("answer", {"text": copy})
        yield _format_sse("citations", _citations_wire_payload([]))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


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

    An empty or missing catalog table is a zero count by construction
    (ADR 0047: the empty catalog is ``{"filings": []}``, not a
    failure). A table file that exists but is corrupt raises
    ``CatalogCorruptError`` — an ``IngestMissingError`` subclass. On
    this route that is still a zero count, not a failure: ADR 0033
    makes zero a valid rendered value, and a 500 would blank a strip
    whose other three fields are still true. The warning keeps the
    state out of the silent-swallow class — ``GET /api/corpus``
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


# Push-failure codes that mean the retrieval backend was unreachable or
# answered off-contract — a dependency outage (503). The remaining
# ``PushError`` codes (empty batch / empty document / incomplete
# metadata) cannot be induced by an uploader: they mean the endpoint's
# own pipeline produced an invalid batch, which is a 500, never a
# client-facing 4xx.
_PUSH_UNAVAILABLE_CODES: Final[frozenset[str]] = frozenset(
    {
        "push_service_unavailable",
        "push_protocol_error",
        "push_batch_failed",
        "push_journal_unwritable",
        "push_journal_unreadable",
        "push_journal_corrupt",
    }
)


def _push_error_status(exc: PushError) -> int:
    """Push outages are dependency failures (503); the rest are 500."""
    return 503 if exc.code in _PUSH_UNAVAILABLE_CODES else 500


# ----- ingest route: exception -> named HTTP answer (table-driven) -----
#
# Complexity-gate remediation: the ``corpus_ingest`` handler kept one
# ``except`` branch per failure family. The mapping is data, not
# control flow — each row is (exception family, status, detail flag)
# and :func:`_ingest_error_response` dispatches ONCE, isinstance-style,
# in table order. Order matters: a family must precede a base it must
# shadow (``UnsupportedFormatParseError`` before ``ParseError``).
# Every row's family carries a stable ``code`` class attribute; the
# body is always ``{"error": code}``, plus ``detail`` (str(exc)) only
# on the 415 that names the received format (SC7).
_INGEST_ERROR_TABLE: Final[
    tuple[tuple[type[BaseException], Any, bool], ...]
] = (
    (UnsupportedFormatParseError, 415, True),
    (IngestUploadTooLargeError, 413, False),
    (IngestFormatUnspecifiedError, 422, False),
    (IngestDocumentIdMissingError, 422, False),
    (IngestInvalidDocTypeError, 422, False),
    # Accept-time conflict families: the upload is refused, nothing was
    # written, and the caller's next move differs (wait vs. give up).
    (IngestConcurrentRunError, 409, False),
    (ParseError, 400, False),
    (InvalidationError, 503, False),
    # A durable item could not be persisted: no job was accepted, so the
    # caller retries the same upload. The compensating delete failing on
    # top of that leaves an orphan object, which is its own named 500.
    (IngestDurableCommitError, 503, False),
    (IngestObjectCleanupError, 500, False),
    # PushError's status depends on the code: resolver, not a constant.
    (PushError, _push_error_status, False),
)


def _ingest_error_response(exc: Exception) -> JSONResponse:
    """Answer an ingest failure with its named HTTP response.

    One dispatch replaces the ingest handler's per-family ``except``
    branches: walk :data:`_INGEST_ERROR_TABLE` in order, take the
    row's status (a plain int, or a resolver for families whose status
    depends on the error code) and answer ``{"error": exc.code}`` —
    every table family declares ``code``. A family OUTSIDE the table
    is not this route's named contract: re-raise typed so it stays an
    unhandled 500, never a fabricated answer.
    """
    for family, status, with_detail in _INGEST_ERROR_TABLE:
        if not isinstance(exc, family):
            continue
        resolved = status(exc) if callable(status) else status
        content: dict[str, Any] = {"error": exc.code}
        if with_detail:
            content["detail"] = str(exc)
        return JSONResponse(status_code=resolved, content=content)
    raise exc


# ----- route-layer upload gate (input boundary) -----
#
# Both upload doors (thread attach, corpus ingest) used to buffer the
# WHOLE body before consulting the byte cap — an unbounded allocation
# on a lying or chunked uploader. Now the route consults the declared
# Content-Length first and, when the length is unknown, reads in
# bounded segments and refuses at cap+1 bytes. The service-level cap
# checks stay (defense in depth); this is the route's own gate.

# Segment size of the bounded read for the multipart ingest door.
_INGEST_READ_SEGMENT: Final[int] = 1024 * 1024

# Framing headroom for the pre-buffer declared gate on the multipart ingest
# door. A multipart request's Content-Length is the file bytes PLUS part
# boundaries and the metadata fields, so comparing the whole request length
# to the per-file cap would refuse an exactly-at-cap upload. The authoritative
# per-file boundary stays in the bounded read and the endpoint check, which
# both read the same MAX_INGEST_BYTES; this allowance only covers framing so the
# coarse memory guard never false-rejects a file that actually fits.
_INGEST_MULTIPART_FRAMING_ALLOWANCE: Final[int] = 1024


def _declared_upload_over(request: Request, cap: int) -> bool:
    """True when the request's declared Content-Length exceeds ``cap``.

    The pre-buffer gate: a declared oversize body is refused before a
    single byte is read. A missing (chunked) or unparseable header is
    NOT oversize by declaration — the bounded read decides. A
    ``cap <= 0`` means unlimited (the service cap's own convention).
    """
    if cap <= 0:
        return False
    declared = request.headers.get("content-length")
    if declared is None:
        return False
    try:
        return int(declared) > cap
    except ValueError:
        return False


async def _read_bounded_body(request: Request, cap: int) -> bytes | None:
    """Read the attach body in segments; ``None`` once over ``cap``.

    The no-Content-Length (chunked) leg of the attach gate: segments
    accumulate only while the running total fits ``cap``, so an
    unbounded stream can never balloon process memory past cap+1
    bytes. ``cap <= 0`` (unlimited) degrades to the plain full read.
    """
    if cap <= 0:
        return await request.body()
    chunks: list[bytes] = []
    total = 0
    async for segment in request.stream():
        total += len(segment)
        if total > cap:
            return None
        chunks.append(segment)
    return b"".join(chunks)


def _read_bounded_upload(file: Any, cap: int) -> bytes | None:
    """Read the spooled multipart upload in segments; ``None`` over cap.

    The ingest leg of the gate (no Content-Length, or a declared one
    that already passed): the sync SpooledTemporaryFile is read segment
    by segment and the read stops at cap+1 bytes — the route never
    materializes an oversized upload in memory. ``cap <= 0`` means
    unlimited.
    """
    if cap <= 0:
        return file.read()
    chunks: list[bytes] = []
    total = 0
    while True:
        segment = file.read(_INGEST_READ_SEGMENT)
        if not segment:
            break
        total += len(segment)
        if total > cap:
            return None
        chunks.append(segment)
    return b"".join(chunks)


# ----- send gate: uploads this process is still storing -----


class AttachmentUploadInFlight:
    """Per-thread count of attachment uploads whose bytes are still landing.

    The gate's truth is server-side and narrow: an upload is in flight
    from the moment the attach door starts reading its body until the
    record exists. Counted per thread, so a pending file the
    message never names still holds its own thread. The counts live in
    this process only, which is what the single-operator 工作台 on one
    server process needs; a multi-worker deployment would need shared
    state.

    Why the lock: an upload can be mid-body on one request thread while
    a send is being checked on another.

    EXIT: one process holds the counts, so a send that lands on a second
    worker would not see an upload the first is still storing. The
    工作台 runs one uvicorn process; sharing state is a prerequisite of
    adding a second worker, not a gap in this gate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    @contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        """Close ``thread_id``'s send gate for the duration of one upload.

        A context manager rather than public ``enter`` / ``exit`` calls:
        every door path — stored, refused, failed, client gone — must
        release, and ``with`` is the only spelling that cannot forget one.
        """
        self._enter(thread_id)
        try:
            yield
        finally:
            self._exit(thread_id)

    def _enter(self, thread_id: str) -> None:
        """Open the gate for ``thread_id`` for the duration of one upload."""
        with self._lock:
            self._counts[thread_id] = self._counts.get(thread_id, 0) + 1

    def _exit(self, thread_id: str) -> None:
        """Hand the thread back; the last upload out clears its entry."""
        with self._lock:
            left = self._counts.get(thread_id, 0) - 1
            if left > 0:
                self._counts[thread_id] = left
            else:
                self._counts.pop(thread_id, None)

    def pending(self, thread_id: str) -> bool:
        """True while ``thread_id`` has an upload this process is storing."""
        with self._lock:
            return self._counts.get(thread_id, 0) > 0


# One ingest service per process: the fake boot shares one observable
# store across requests; the MCP boot's adapters are stateless enough
# (one subprocess per push call) that sharing changes nothing.
_ingest_service_singleton: IngestEndpoint | None = None
_ingest_service_lock = threading.Lock()


# ----- ingest -> catalog registration (T2 x T3 wiring) -----
#
# Why a thread-local slot: the ``on_ingested`` hook runs INSIDE
# ``service.ingest`` (after the push landed), so a hook failure cannot
# become the route's error answer without denying an already-successful
# push — and it must not vanish either (error-handling-enforcer: no
# silent skip-as-success). The hook records the named failure on this
# thread's slot; the ingest route resets the slot before the call,
# reads it after, and surfaces it as the response's named
# ``catalog_warning`` field. Thread-local because the service is a
# process-wide singleton while sync routes run on a threadpool:
# concurrent ingests must never read each other's warnings.
_catalog_hook_slot = threading.local()

# The named warning code the response carries when the catalog
# registration failed after a successful push. The detail (str(exc))
# follows the code and names the drifted field / broken table, so the
# operator can act without reading server logs.
CATALOG_RECORD_FAILED: Final[str] = "catalog_record_failed"

# The failure families the hook converts into the named warning:
# ``CatalogRowError`` / ``CatalogCorruptError`` are the catalog's own
# contract failures (bad row / unreadable table); ``OSError`` covers
# the filesystem outages ``record_filings`` cannot type (mkdir, lock,
# fsync, replace). Anything else is a bug and keeps propagating typed
# (T3 contract: the hook's own failures propagate).
_CATALOG_FAILURES: Final[tuple[type[BaseException], ...]] = (
    CatalogRowError,
    CatalogCorruptError,
    OSError,
)


def _record_ingested_filing(doc: IngestedDocument) -> None:
    """Product ``on_ingested`` hook: register the pushed filing.

    Maps the :class:`IngestedDocument` onto the T2 row contract and
    upserts it into the default catalog table (``record_filings``'
    own default — the ``data/corpus/catalog.json`` the ``GET
    /api/corpus`` route reads). ``source_sha256`` is left out: the
    ingest version IS the sha256 of the source bytes, so storing it
    twice would duplicate one value under two keys.

    A catalog-side failure never rolls the push back and never fails
    the request: it is logged with traceback and recorded on the
    thread slot so the ingest route answers 200 with the named
    ``catalog_warning``.
    """
    try:
        # One row: ``record_filings`` takes an iterable of rows.
        record_filings(
            [
                {
                    "document_id": doc.document_id,
                    "company": doc.company,
                    "period": doc.period,
                    "version": doc.version,
                    "page_count": doc.page_count,
                }
            ]
        )
    except _CATALOG_FAILURES as exc:
        _catalog_hook_slot.warning = f"{CATALOG_RECORD_FAILED}: {exc}"
        logger.warning(
            "ingest: catalog registration failed for %s "
            "(the push stands)",
            doc.document_id,
            exc_info=True,
        )


# The durable append-only journals the ingest service owns between
# pushes: the push batch journal (resume across processes), the
# invalidation ledger (settled closures), the pushed-record journal
# (what a replacement upload must close), and the index-job ledger (the
# submitted iknow job per pushed pair, the restart handle for polling).
# Each defaults to the catalog's
# own data dir — the visible ``data/corpus`` the ``GET /api/corpus``
# route reads — and each is individually relocatable by env var, so a
# deployment moves the ingest journals as typed config, never as a
# silent temp dir. Constructing the service never writes these files;
# the first append does.
_PUSH_JOURNAL_PATH_ENV: Final[str] = "FIN_CORPUS_PUSH_JOURNAL_PATH"
_INVALIDATION_LEDGER_PATH_ENV: Final[str] = "FIN_CORPUS_INVALIDATION_LEDGER_PATH"
_PUSHED_RECORDS_PATH_ENV: Final[str] = "FIN_CORPUS_PUSHED_RECORDS_PATH"
_INDEX_JOB_LEDGER_PATH_ENV: Final[str] = "FIN_CORPUS_INDEX_JOB_LEDGER_PATH"


def _configured_journal_path(env_var: str, default: Path) -> Path:
    configured = (os.environ.get(env_var) or "").strip()
    return Path(configured) if configured else default


def _ingest_components(*, catalog_path: Path | None = None) -> dict[str, Path]:
    """Where the four durable ingest journals live.

    The default sits beside the catalog (``DEFAULT_CATALOG_PATH``'s
    parent) so one visible data dir holds all ingest state; an env var
    overrides one journal without moving the others.
    """
    from fin.corpus.catalog import DEFAULT_CATALOG_PATH

    data_dir = Path(catalog_path or DEFAULT_CATALOG_PATH).parent
    return {
        "push_journal_path": _configured_journal_path(
            _PUSH_JOURNAL_PATH_ENV, data_dir / "push_journal.jsonl"
        ),
        "invalidation_ledger_path": _configured_journal_path(
            _INVALIDATION_LEDGER_PATH_ENV, data_dir / "invalidation_ledger.jsonl"
        ),
        "pushed_records_path": _configured_journal_path(
            _PUSHED_RECORDS_PATH_ENV, data_dir / "pushed_records.jsonl"
        ),
        "index_job_ledger_path": _configured_journal_path(
            _INDEX_JOB_LEDGER_PATH_ENV, data_dir / INDEX_JOBS_FILE
        ),
    }


def _ingest_ports_from_env() -> tuple[Any, Any, Any]:
    """The three accept-time ports of the durable ingest door.

    Resolved through :func:`fin.corpus.ingest_storage.preflight_ingest_storage`,
    which is the process's single answer to "what storage can promise a
    durable accept". There is no fallback here on purpose: a deployment that
    cannot store raw bytes, pointers and job rows durably is refused at boot
    rather than handed a door that answers ``accepted`` and forgets on
    restart.
    """
    from fin.corpus.ingest_storage import preflight_ingest_storage

    ports = preflight_ingest_storage()
    return ports.objects, ports.pointers, ports.states


class _JournaledIngestEndpoint(IngestEndpoint):
    """The product endpoint: every successful push also journals its records.

    ``IngestedDocument`` carries no records, so the ``on_ingested`` hook
    cannot persist them; this subclass journals the full ``PushRecord``
    rows after ``ingest`` returns, keyed ``(document_id, version)``. The
    journal is what makes the replacement closure real: the accept door's
    ``previous_records`` provider reads it, so a version's stale blocks
    are still closable after the process that pushed them is gone.

    Journaling failure warns with the same posture as catalog failure —
    the push already stands — and never fails the ingest.
    """

    def __init__(
        self, *, pushed_records_store: PushedRecordsStore, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._pushed_records_store = pushed_records_store

    def ingest(self, data: bytes, **kwargs: Any) -> Any:
        result = super().ingest(data, **kwargs)
        try:
            self._pushed_records_store.replace(
                result.document_id, result.version, result.records
            )
        except Exception as exc:
            logger.warning(
                "ingest: pushed-record journaling failed for %s "
                "(the push stands): %s",
                result.document_id,
                exc,
            )
        return result


def _build_default_ingest_service() -> IngestEndpoint:
    """Build the env-selected ingest service (``get_ingest_service``).

    The backend selection mirrors the retrieve adapter's
    (``FIN_RETRIEVE_ADAPTER``, via
    :func:`fin.host.operator_env.resolve_retrieve_adapter_kind`): a
    fake retrieve boot gets a process-local ``FakePushPort`` serving
    both the push and invalidation faces (observable, no network); the
    product default gets the MCP ``push`` / ``invalidation`` adapters
    (ADR 0047: ingest enters search only through MCP). The MCP
    adapters are imported lazily so the workspace import surface stays
    free of the stdio transport (and its psycopg-heavy compile import)
    until an ingest actually happens.

    Both kinds get the same three accept-time ports (object / pointer /
    job state), so the durable accept of a fake boot exercises the real
    port contracts rather than a second, weaker contract.

    Both kinds also get the three durable journals from
    :func:`_ingest_components`: a path-backed ``PushJournal`` (batch
    resume outlives the process), a path-backed ``InvalidationLedger``
    (settled closures stay queryable), and a path-backed
    ``PushedRecordsStore`` whose zero-argument reader is the
    ``previous_records`` provider. Without that provider the
    replacement stale-block closure at the accept door is the
    documented no-op; with it a shipped boot closes the superseded
    version's blocks on every replacement upload.

    The index-job half of the handoff is deliberately NOT built here:
    the tracker and its ledger are the process singleton behind
    :func:`get_index_job_tracker`, shared by the boot worker's submit
    hook and the status route's graph pass-through, so one round of
    ledger writes answers both doors.

    Both kinds wire ``on_ingested`` to :func:`_record_ingested_filing`
    so a pushed filing is registered in the same catalog table the
    ``GET /api/corpus`` route lists — a fake boot is a real product
    mode, not a second contract. The hook belongs to the synchronous
    chain: the HTTP accept door does not parse or push, so it never
    reaches the hook.
    """
    from fin.host.operator_env import resolve_retrieve_adapter_kind

    components = _ingest_components()
    pushed_records_store = PushedRecordsStore(
        path=components["pushed_records_path"]
    )
    journal_wiring: dict[str, Any] = {
        "journal": PushJournal(path=components["push_journal_path"]),
        "invalidation_ledger": InvalidationLedger(
            path=components["invalidation_ledger_path"]
        ),
        # zero-arg provider shape the endpoint seam documents; the
        # planner filters this view by document and skips the incoming
        # version, so a same-version re-push plans no targets.
        "previous_records": pushed_records_store.all_records,
        "pushed_records_store": pushed_records_store,
    }
    objects, pointers, states = _ingest_ports_from_env()
    if resolve_retrieve_adapter_kind() == "fake":
        shared: FakePushPort = FakePushPort()
        return _JournaledIngestEndpoint(
            push_port=shared,
            invalidation_port=shared,
            on_ingested=_record_ingested_filing,
            object_store=objects,
            metadata_store=pointers,
            state_store=states,
            **journal_wiring,
        )
    from fin.retrieve.mcp_invalidation_port import MCPInvalidationPort
    from fin.retrieve.mcp_push_port import MCPPushPort

    return _JournaledIngestEndpoint(
        push_port=MCPPushPort(),
        invalidation_port=MCPInvalidationPort(),
        on_ingested=_record_ingested_filing,
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
        **journal_wiring,
    )


def get_ingest_service() -> IngestEndpoint:
    """FastAPI dependency returning the corpus ingest endpoint service.

    Tests override this via
    ``app.dependency_overrides[get_ingest_service]`` to inject a
    service built on ``FakePushPort`` (no live MCP). Production gets
    the env-selected ports from ``_build_default_ingest_service``,
    built once per process.
    """
    global _ingest_service_singleton
    with _ingest_service_lock:
        if _ingest_service_singleton is None:
            _ingest_service_singleton = _build_default_ingest_service()
        return _ingest_service_singleton


# The one index-job tracker per process: the worker's submit hook and
# the status route's graph pass-through must read and write the same
# durable ledger, so both doors reach it through this singleton the
# same way they reach the ingest endpoint service.
_index_job_singleton: tuple[IndexJobTracker, IndexJobLedger] | None = None
_index_job_lock = threading.Lock()


def _build_default_index_job_components() -> tuple[IndexJobTracker, IndexJobLedger]:
    """Build the env-selected index-job tracker and its ledger.

    The port selection mirrors both the retrieve adapter's and the
    ingest service's (fake boot = observable in-process contract fake,
    product boot = the MCP stdio adapter on the same
    ``DEFAULT_MCP_COMMAND`` configuration the push and invalidation
    adapters use). The ledger is path-backed beside the other durable
    ingest journals — the submitted ``job_id``s are exactly what a
    restart re-polls instead of re-pushing. The MCP adapter import is
    lazy for the same reason the push adapters are: the workspace
    import surface stays free of the stdio transport until an
    index-job call actually happens. Building writes nothing.
    """
    components = _ingest_components()
    ledger = IndexJobLedger(path=components["index_job_ledger_path"])
    if resolve_retrieve_adapter_kind() == "fake":
        from fin.retrieve.fake_index_job_port import FakeIndexJobPort

        port: Any = FakeIndexJobPort()
    else:
        from fin.retrieve.mcp_index_job_port import MCPIndexJobPort

        port = MCPIndexJobPort()
    return IndexJobTracker(port, ledger), ledger


def _get_index_job_components() -> tuple[IndexJobTracker, IndexJobLedger]:
    global _index_job_singleton
    with _index_job_lock:
        if _index_job_singleton is None:
            _index_job_singleton = _build_default_index_job_components()
        return _index_job_singleton


def get_index_job_tracker() -> IndexJobTracker:
    """FastAPI dependency returning the process's index-job tracker.

    Tests override this via
    ``app.dependency_overrides[get_index_job_tracker]`` to inject a
    tracker over ``FakeIndexJobPort``. Production gets the env-selected
    tracker from ``_build_default_index_job_components``, built once
    per process and shared with the ingest worker (the tracker is
    read-only for the route: its poll is the designed pure-read path).
    """
    tracker, _ledger = _get_index_job_components()
    return tracker


# The durable ingest queue's execution side: one drain loop per process,
# started only from the boot path (``fin.host.http.serve``). Nothing here
# runs at import — the suite imports this module heavily and must never
# acquire a background thread unless a test opts in by calling
# ``start_ingest_worker`` itself.
_ingest_worker_thread: threading.Thread | None = None
_ingest_worker_stop_event: threading.Event | None = None
_ingest_worker_lock = threading.Lock()


class IngestWorkerStoppingError(RuntimeError):
    """Start refused while a timed-out stop's thread is still draining.

    The singleton guard of :func:`start_ingest_worker`: a stop whose
    join times out keeps the live thread registered, and a start over it
    would run a second drain loop in one process. Callers retry once the
    old sweep finishes (the durable store makes waiting cheap; the loop
    exits through its stop_event on its own).
    """

    code = "ingest_worker_stopping"

#: Seconds between queue re-checks after the startup drain. Bounded so
#: a submit landing between two checks still runs within one interval.
INGEST_WORKER_CHECK_INTERVAL_SECONDS: Final[float] = 30.0

INGEST_WORKER_THREAD_NAME: Final[str] = "fin-ingest-worker"


def _ingest_worker_journal_dir() -> Path:
    """The durable directory for the boot worker's per-document journals.

    Sits beside the configured push journal (same data dir, env override
    honored), so restart-replayed runs skip exactly the batches that
    already landed instead of starting from a blank journal.
    """
    components = _ingest_components()
    return Path(components["push_journal_path"]).parent / "ingest_worker_journals"


def _drain_ingest_queue(
    worker: IngestWorker,
    stop_event: threading.Event,
    check_interval_seconds: float,
) -> None:
    """Startup drain, then a bounded periodic re-check, until stopped.

    The chosen boot posture is startup drain + periodic re-check (not
    event-on-submit): it satisfies "resume after restart" from the
    durable store alone, and a job submitted later still runs within one
    interval, without the HTTP accept door gaining any execution
    coupling. Posture on a failing drain: log with exception context
    and keep the loop — the durable store plus the journals make the
    next sweep the recovery, so a transient store outage must not kill
    the only executor in the process. Silent thread death is the failure
    mode this rule excludes; the loop exits only through ``stop_event``.
    """
    while True:
        try:
            worker.run_pending()
        except Exception:
            logger.exception(
                "ingest worker: drain failed; continuing to the next check"
            )
        if stop_event.wait(check_interval_seconds):
            return


def start_ingest_worker(
    *,
    endpoint: IngestEndpoint | None = None,
    journal_dir: Path | None = None,
    check_interval_seconds: float | None = None,
    index_job_tracker: IndexJobTracker | None = None,
    index_job_ledger: IndexJobLedger | None = None,
) -> threading.Thread:
    """Start the process's single durable ingest drain (idempotent).

    The boot path for the durable resume clause: a fresh
    :class:`IngestWorker` over ``get_ingest_service()`` (unless an
    endpoint is injected)
    discovers every resumable job from the durable state store and
    drives it — queued, failed and interrupted jobs alike, with no
    re-upload. The worker takes the same process-wide index-job tracker
    the status route reads (unless one is injected), so a submit it
    makes is immediately observable through ``GET
    /api/corpus/ingest/{job_id}``. Calling this again while the loop is
    alive returns the
    existing thread without building a second worker (one executor per
    process; the claim CAS still guards two processes). A live thread
    whose stop_event is already set is a timed-out stop still draining:
    this start raises :class:`IngestWorkerStoppingError` rather than
    replace it, because the dying loop would leave the process with no
    executor and (if a second were spawned) a window of two. The thread
    is a daemon so a host that forgets to stop never blocks interpreter
    exit; ``stop_ingest_worker`` is the clean shutdown.
    """
    global _ingest_worker_thread, _ingest_worker_stop_event
    with _ingest_worker_lock:
        current = _ingest_worker_thread
        if current is not None and current.is_alive():
            if (
                _ingest_worker_stop_event is not None
                and _ingest_worker_stop_event.is_set()
            ):
                raise IngestWorkerStoppingError(
                    "the ingest worker is stopping (a prior stop join "
                    "timed out); start again once its thread exits"
                )
            return current
        if index_job_tracker is None and index_job_ledger is None:
            index_job_tracker, index_job_ledger = _get_index_job_components()
        service = endpoint if endpoint is not None else get_ingest_service()
        worker = IngestWorker(
            service,
            journal_dir=(
                journal_dir
                if journal_dir is not None
                else _ingest_worker_journal_dir()
            ),
            index_job_tracker=index_job_tracker,
            index_job_ledger=index_job_ledger,
        )
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_drain_ingest_queue,
            args=(
                worker,
                stop_event,
                (
                    check_interval_seconds
                    if check_interval_seconds is not None
                    else INGEST_WORKER_CHECK_INTERVAL_SECONDS
                ),
            ),
            name=INGEST_WORKER_THREAD_NAME,
            daemon=True,
        )
        _ingest_worker_stop_event = stop_event
        _ingest_worker_thread = thread
        thread.start()
        return thread


def stop_ingest_worker(*, timeout: float = 60.0) -> None:
    """Signal the drain loop to exit and join it. Idempotent.

    The in-flight sweep finishes its current job first (a half-run job
    is exactly what the next boot's startup drain resumes), then the
    loop returns. Stopping a worker that was never started is a no-op.
    The globals are cleared only after the join confirms the thread
    exited; on a join timeout the live thread stays registered — the
    stopped tombstone — so ``start_ingest_worker`` refuses it
    (:class:`IngestWorkerStoppingError`) instead of letting two drain
    loops share the process.
    """
    global _ingest_worker_thread, _ingest_worker_stop_event
    with _ingest_worker_lock:
        if _ingest_worker_stop_event is not None:
            _ingest_worker_stop_event.set()
        thread = _ingest_worker_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    if thread is not None and not thread.is_alive():
        with _ingest_worker_lock:
            if _ingest_worker_thread is thread:
                _ingest_worker_thread = None
                _ingest_worker_stop_event = None


# One attachment service per process, over the env-selected thread store
# and object store: attachment rows are durable product state, so they
# follow the same singletons the thread sidebar and corpus ingest use.
_attachment_service_singleton: AttachmentPreparationService | None = None
_attachment_service_lock = threading.Lock()


class HeronPageLaneParser(PageRegionParser):
    """The deployed pages port: one pinned Heron pass per PDF document.

    The core binds its layout at construction and never hands a document
    to it, while :class:`~fin.host.attachment_layout.HeronPageLayout` is
    built over one file on disk; uploads, though, are bytes the door has
    already stored. So this composition-root port owns the per-document
    meeting of the two shapes: a PDF extract stages the bytes as a
    private 0600 file, runs the core with one fresh Heron layout over
    it, and destroys the copy on the way out — a thread's filing does
    not survive the preparation that read it.

    The image lane keeps the base behavior over
    :class:`~fin.corpus.parsers.NullLayout`: the pinned harness takes
    PDF input only (its one attempt at a bare raster is a committed
    refusal), and a standalone image's page is its own raster, so its
    region and text need no box to address.

    Nothing here is a container dependency at boot: the pass is taken
    lazily by the layout port on the first page that asks, and every
    host-side absence it can meet — no harness, no container, no
    configured image — is its own named pass state answered with no
    regions, never a raise (regions are additive metadata).
    """

    def __init__(
        self,
        *,
        config: HeronLayoutConfig,
        runner: LayoutRecordRunner | None = None,
    ) -> None:
        # The OCR port is the module's own production engine: lazy on the
        # optional ``[ocr]`` extra, so a host without it ends a page that
        # needed a read at the typed ``ocr_required_unavailable`` exit
        # rather than at boot. One engine serves every document: it holds
        # no per-document state.
        ocr = TesseractEngine()
        super().__init__(layout=NullLayout(), ocr=ocr)
        self._ocr_engine = ocr
        self._heron_config = config
        # ``None`` keeps the adapter's own measured-harness default; a
        # caller may hand in another runner, and this is the seam the
        # lane's boot tests take so a wiring check never crosses a
        # container.
        self._runner = runner

    @property
    def heron_config(self) -> HeronLayoutConfig:
        """The boot-resolved harness knobs every document's pass is taken with."""
        return self._heron_config

    def extract_pdf(
        self,
        *,
        source_bytes: bytes,
        source_sha256: str,
        document_id: str,
        version: str,
        max_pages: int | None = None,
    ) -> PageParseResult:
        handle, staged_name = tempfile.mkstemp(
            prefix="fin-attachment-", suffix=".pdf"
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(handle, "wb") as staged_file:
                staged_file.write(source_bytes)
            document_parser = PageRegionParser(
                layout=HeronPageLayout(
                    pdf_path=staged,
                    runner=self._runner,
                    config=self._heron_config,
                ),
                ocr=self._ocr_engine,
            )
            return document_parser.extract_pdf(
                source_bytes=source_bytes,
                source_sha256=source_sha256,
                document_id=document_id,
                version=version,
                max_pages=max_pages,
            )
        finally:
            # A copy this process wrote for one preparation is removed even
            # when that preparation failed: the row already carries the
            # typed exit, and a private filing must not leak into tmp.
            try:
                staged.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.debug("staged copy %s not removed: %s", staged, cleanup_exc)


def _build_default_attachment_service() -> AttachmentPreparationService:
    """Build the default attachment service and resume interrupted work.

    Recovery runs here rather than on a request path: a restart must
    restore the derived index before any ``ready`` is reported, and
    construction is the one point certain to precede the first read.

    The byte store follows the corpus object-store configuration. With no
    MinIO/S3 configured the service degrades to process-local bytes and
    says so — the same logged degradation the unconfigured checkpointer
    uses — because attachment rows outlive the blobs then, and recovery
    reports them as ``source_unavailable`` rather than as empty text.

    This root deploys the page lane whenever the run has declared its
    bounds: every wired service carries the run's own bounds, a
    per-document Heron pages port, and the host's vision port. Once the
    lane is deployed, two config facts can stop the build from succeeding
    — the four ``FIN_ATTACHMENT_*`` bounds read through the boot singleton
    (:func:`fin.host.http.get_attachment_budgets`, the same read
    ``serve()`` makes before binding, so a missing or non-positive bound
    is a startup failure here too) and a malformed
    ``FIN_ATTACHMENT_HERON_*`` value, which is a named boot failure
    (:meth:`HeronLayoutConfig.from_environ`) rather than a fallback. No
    optional knob has to be present: with no harness image configured the
    pass answers its own named state per document and the lane still
    serves native text, rendering, OCR, and the page scan. The vision
    port is constructed without a credential — its client is built
    lazily on the first scan — so boot never pays a provider import, and
    a keyless scan at preparation time is the typed
    ``vision_call_failed`` exit, never a silent skip.

    WHY the bounds gate the lane: declaring any one of the four
    ``FIN_ATTACHMENT_*`` keys is the operator's word that this process
    prices pages, and then every sibling bound and every Heron knob is
    validated strictly at build time. A process that declares none has
    not opted in, and this root does not crash it: it builds T2d's
    shipped text-only door, where a page document answers the pinned
    :class:`~fin.host.attachment_prepare.AttachmentPageLaneUnwiredError`
    and text keeps working. That is not an operator escape hatch —
    ``serve()`` reads the boot singleton before binding regardless, so a
    server without its bounds still fails at startup; only non-``serve``
    entry points (tests, embedded doors) ever meet the unwired lane.
    """
    import os

    from fin.corpus.object_store import (
        InMemoryObjectStore,
        object_store_from_env,
    )
    from fin.corpus.object_store_errors import ObjectStoreNotConfiguredError
    from fin.host.attachment_job_store import (
        InMemoryThreadAttachmentJobStore,
        PostgresThreadAttachmentJobStore,
    )
    from fin.host.attachment_store import (
        InMemoryAttachmentStore,
        PostgresAttachmentStore,
    )
    from fin.host.thread_store import default_thread_store

    # WHY the lane config first: both reads are boot facts, and a
    # deployment that declared its bounds but cannot state them legally,
    # or misspelled a harness knob, must fail before it opens a pool,
    # stores a byte, or resumes work. Lazily imported: ``fin.host.http``
    # owns the deployed-budget singleton and imports this module at its
    # bottom, so a module-level import here would close the cycle — the
    # same seam ``build_router`` reads the door's copy of these bounds
    # through. ``BOUND_ENV_KEYS`` is imported for the key NAMES only: the
    # module that owns the bounds owns how they are spelled, so the gate
    # here can never drift from what ``from_environ`` reads.
    from fin.host.attachment_budgets import BOUND_ENV_KEYS

    lane_declared = any(key in os.environ for key, _ in BOUND_ENV_KEYS)
    budgets: AttachmentBudgets | None = None
    pages: HeronPageLaneParser | None = None
    vision: ChatModelVisionAdapter | None = None
    if lane_declared:
        from fin.host.http import get_attachment_budgets

        budgets = get_attachment_budgets()
        pages = HeronPageLaneParser(
            config=HeronLayoutConfig.from_environ(environ=os.environ)
        )
        # The one production vision port: the host's own chat-model
        # settings, no separate credentials, client built lazily at the
        # first scan.
        vision = ChatModelVisionAdapter()
    else:
        # EXIT: no bounds declared — the unwired text-only door, not a
        # crash. See the docstring: serve() still gates a boundless
        # server at boot.
        logger.info(
            "no FIN_ATTACHMENT_* bounds are set; the page lane stays "
            "unwired and page documents will answer %s",
            ATTACHMENT_PAGE_LANE_UNWIRED,
        )

    try:
        objects: Any = object_store_from_env()
    except ObjectStoreNotConfiguredError:
        logger.warning(
            "FIN_OBJECT_STORE_URL/FIN_OBJECT_STORE_BUCKET are not set; "
            "thread attachments keep their raw bytes in process memory and "
            "will report source_unavailable after a restart"
        )
        objects = InMemoryObjectStore()

    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if url:
        # WHY the thread store first: every attachment row references the
        # thread row, so the thread schema has to exist before the
        # attachment tables are created.
        default_thread_store()
        store: Any = PostgresAttachmentStore(url)
        # WHY the durable job twin here: the graph gate enqueues
        # preparation work that must outlive this process (the auto-
        # resumer and the restart sweep both read it), so a Postgres
        # attachment store gets the Postgres job store over the SAME
        # url — one durable database owns the row and its work.
        jobs: Any = PostgresThreadAttachmentJobStore(url)
    else:
        store = InMemoryAttachmentStore(default_thread_store())
        jobs = InMemoryThreadAttachmentJobStore()
    service = AttachmentPreparationService(
        store=store,
        objects=objects,
        budgets=budgets,
        pages=pages,
        vision=vision,
        jobs=jobs,
    )
    service.recover()
    return service


def default_attachment_service() -> AttachmentPreparationService:
    """Process singleton for the thread attachment service.

    ``fin.host.http`` wraps this as its ``get_attachment_service``
    dependency so tests can override the seam in one place.
    """
    global _attachment_service_singleton
    with _attachment_service_lock:
        if _attachment_service_singleton is None:
            _attachment_service_singleton = _build_default_attachment_service()
        return _attachment_service_singleton


# The boot-owned execution side of the durable graph gate (T2f): one
# prep drain that runs the queued preparation jobs, and one auto-resumer
# that drives runs paused on the preparation interrupt. Both start only
# from the boot path (``fin.host.http.serve``) — nothing here acquires a
# thread at import, the same posture as the ingest worker above.


_attachment_prep_worker: AttachmentPrepWorker | None = None
_attachment_prep_worker_lock = threading.Lock()


def start_attachment_prep_worker(
    service: AttachmentPreparationService | None = None,
    *,
    interval_s: float = ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS,
) -> threading.Thread:
    """Start the process's single attachment prep drain (idempotent).

    Boot glue over :class:`AttachmentPrepWorker`, which owns the
    singleton guard itself: a second call while the loop is alive
    returns the live thread, and a start over a timed-out stop's dying
    thread raises ``AttachmentPrepWorkerStoppingError`` rather than
    sharing the process with two drains. The worker object stays the
    module global after a confirmed stop (the class is restartable), so
    a restart re-``start``\\ s the same drain posture over the same
    service. ``service`` is honored only for the first construction in
    the process — after that the documented singleton answers, same as
    the HTTP dependency.
    """
    global _attachment_prep_worker
    with _attachment_prep_worker_lock:
        if _attachment_prep_worker is None:
            _attachment_prep_worker = AttachmentPrepWorker(
                service if service is not None else default_attachment_service(),
                interval_s=interval_s,
            )
        return _attachment_prep_worker.start()


def stop_attachment_prep_worker(*, timeout: float = 60.0) -> None:
    """Stop the process's attachment prep drain. Idempotent.

    Delegates the join to the worker: the in-flight job finishes its
    current claim first (a half-run job is exactly what the boot sweep
    re-adopts), and stopping a never-started worker is a no-op.
    """
    with _attachment_prep_worker_lock:
        worker = _attachment_prep_worker
    if worker is not None:
        worker.stop(timeout=timeout)


# The resume sweep is a PeriodicDrainWorker over the shared guard; it is
# rebound (honoring freshly passed seams) only once the previous thread's
# exit is confirmed — see start_attachment_resume_worker.
_attachment_resume_worker: PeriodicDrainWorker | None = None
_attachment_resume_worker_lock = threading.Lock()


class AttachmentResumeWorkerStoppingError(RuntimeError):
    """Start refused while a timed-out stop's sweep thread is still alive.

    The singleton guard of :func:`start_attachment_resume_worker`, one
    per process, mirroring :class:`IngestWorkerStoppingError`: a stop
    whose join times out keeps the live thread registered, and a start
    over it would run two resume sweeps in one process.
    """

    code = "attachment_resume_worker_stopping"


#: Seconds between pause sweeps after the startup sweep. Shorter than
#: the prep drain: the pause is operator-visible (the thread sits
#: waiting), while a missed prep job re-runs on the next claim anyway.
ATTACHMENT_RESUME_WORKER_CHECK_INTERVAL_SECONDS: Final[float] = 10.0

ATTACHMENT_RESUME_WORKER_THREAD_NAME: Final[str] = "fin-attachment-resume-worker"


def _resume_one_attachment_wait(
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    run_control: RunControl,
    thread_id: str,
    *,
    workspace_root: Path | None = None,
) -> bool:
    """Drive ONE thread's settled attachment pause to its turn row.

    Returns whether a resume was driven. The guards are the contract:

    * only the ``attachment_preparation`` interrupt kind is ever
      auto-resumed — a ``choice_window`` pause waits on the operator's
      pick and a plain crash checkpoint waits on the Resume button, so
      this sweep cannot answer for either (the exact-kind compare, not
      a deny-list, keeps a future kind from being auto-resumed by
      default);
    * the lock and pending-node probes keep a live run and a finalized
      terminal row out of reach (same read-only seams the routes use);
    * the work must be SETTLED: ``request_preparation`` is asked through
      the same idempotent start-or-join the gate node uses, and any
      ``pending`` answer means job queued or running — wait for the
      next check. ``ready`` / ``failed`` / ``unknown`` all let the run
      go: the gate re-evaluates on its own and refuses or hydrates
      accordingly, so a failed preparation reaches the turn row as the
      gate's typed refuse instead of hanging the pause forever.

    The resume VALUE is a string sentinel: ``Command(resume=None)``
    trips an ``UnboundLocalError`` in this langgraph version, and the
    gate node ignores the value — the re-evaluation is the whole ask.
    """
    if not hasattr(graph, "interrupt_pending") or not hasattr(
        graph, "pending_nodes"
    ):
        return False
    if hasattr(graph, "is_locked") and graph.is_locked(thread_id):
        return False
    payload = graph.interrupt_pending(thread_id)
    if not isinstance(payload, dict):
        return False
    if payload.get("kind") != ATTACHMENT_PREPARATION_INTERRUPT_KIND:
        return False
    if not graph.pending_nodes(thread_id):
        return False
    for attachment_id in payload.get("attachment_ids") or ():
        request = service.request_preparation(thread_id, str(attachment_id))
        if request.state == "pending":
            return False
    run_id = (
        graph.checkpoint_run_id(thread_id)
        if hasattr(graph, "checkpoint_run_id")
        else None
    )
    if not run_id:
        logger.warning(
            "attachment auto-resume: thread %s pauses on an attachment gate "
            "with no checkpoint run_id; leaving it for an operator resume",
            thread_id,
        )
        return False
    resume_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "run_id": run_id,
            "workspace_root": str(
                workspace_root
                if workspace_root is not None
                else default_workspace_root_for_process()
            ),
        }
    }
    # THE same path POST /resume drives, frames discarded: the resumed
    # remainder's turn row is written inside the generator, so consuming
    # it to exhaustion is the whole resume.
    for _frame in _resume_run_stream(
        graph,
        store,
        run_control=run_control,
        thread_id=thread_id,
        run_id=run_id,
        resume_input=Command(resume="attachment_preparation_settled"),
        resume_config=resume_config,
    ):
        pass
    return True


def _resume_attachment_waits_once(
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    run_control: RunControl,
    *,
    workspace_root: Path | None = None,
) -> int:
    """One sweep over every thread; resume each settled attachment pause.

    Returns how many runs were driven. A thread whose resume raises is
    logged and skipped: one poisoned checkpoint cannot starve the rest
    of the sweep (the same log-and-continue posture as the drains).
    """
    resumed = 0
    for thread in store.list_threads():
        try:
            if _resume_one_attachment_wait(
                graph,
                store,
                service,
                run_control,
                thread.id,
                workspace_root=workspace_root,
            ):
                resumed += 1
        except Exception:
            # EXIT: one poisoned checkpoint must not starve the rest of
            # the sweep — the failure exits THIS thread's attempt only;
            # log with context and continue to the next thread.
            logger.exception(
                "attachment resume sweep: thread %s failed; continuing",
                thread.id,
            )
    return resumed


def _check_attachment_resume_waits(
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    run_control: RunControl,
    workspace_root: Path | None,
    stop_event: threading.Event,
    check_interval_seconds: float,
) -> None:
    """Startup sweep, then a bounded periodic re-check, until stopped.

    The loop body is kept separate from the thread plumbing (the shared
    :func:`~fin.host.attachment_prep_worker.drain_periodically`, the
    same posture as the prep module's ``_drain_attachment_queue'') so a
    test can drive the exact loop without spawning anything.

    The startup sweep is what makes a process restart resume waiting
    runs without an operator resend: the pause and its run id live in
    the durable checkpointer, so the first pass over the thread list
    finds every settled gate.
    """
    drain_periodically(
        lambda: _resume_attachment_waits_once(
            graph,
            store,
            service,
            run_control,
            workspace_root=workspace_root,
        ),
        stop_event,
        check_interval_seconds,
        worker_logger=logger,
        log_label="attachment resume worker",
    )


def start_attachment_resume_worker(
    *,
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    workspace_root: Path | None = None,
    run_control: RunControl | None = None,
    check_interval_seconds: float | None = None,
) -> threading.Thread:
    """Start the process's single attachment auto-resumer (idempotent).

    The boot half of the durable gate's resume clause: runs paused on
    the preparation interrupt are driven by this sweep, never by an
    operator resend. The singleton guard is the shared
    :class:`~fin.host.attachment_prep_worker.PeriodicDrainWorker`, the
    same posture as :func:`start_attachment_prep_worker` above: a live
    thread is returned as-is, a start over a timed-out stop's dying
    thread raises :class:`AttachmentResumeWorkerStoppingError`, and the
    worker is rebound — honoring the freshly passed seams — only once
    the previous thread's exit is confirmed (the restart-after-clean-
    stop clause). The thread is a daemon so a host that forgets to stop
    never blocks interpreter exit; ``stop_attachment_resume_worker`` is
    the clean shutdown.

    ``run_control`` defaults to a fresh registry: a background resume
    is not addressable by the HTTP Stop door (that registry belongs to
    one mounted router), and registering there would claim a stop can
    reach a run the operator never saw. ``workspace_root`` defaults to
    resolving the process default at resume time — the same value the
    HTTP seam injects — so a boot that never pauses never pays the
    directory resolution.
    """
    global _attachment_resume_worker
    with _attachment_resume_worker_lock:
        worker = _attachment_resume_worker
        if worker is None or not worker.is_live():
            control = run_control if run_control is not None else RunControl()
            worker = PeriodicDrainWorker(
                lambda: _resume_attachment_waits_once(
                    graph,
                    store,
                    service,
                    control,
                    workspace_root=workspace_root,
                ),
                interval_s=(
                    check_interval_seconds
                    if check_interval_seconds is not None
                    else ATTACHMENT_RESUME_WORKER_CHECK_INTERVAL_SECONDS
                ),
                thread_name=ATTACHMENT_RESUME_WORKER_THREAD_NAME,
                stopping_error=AttachmentResumeWorkerStoppingError,
                log_label="attachment resume worker",
                worker_logger=logger,
            )
            _attachment_resume_worker = worker
        return worker.start()


def stop_attachment_resume_worker(*, timeout: float = 60.0) -> None:
    """Signal the resume sweep to exit and join it. Idempotent.

    Delegates the join to the shared guard: the in-flight sweep
    finishes first, stopping a never-started worker is a no-op, and the
    thread is cleared only after the join confirms the exit — on a join
    timeout the live thread stays registered (the stopping tombstone),
    so ``start_attachment_resume_worker`` refuses to rebind over it
    (:class:`AttachmentResumeWorkerStoppingError`) instead of letting
    two sweeps share the process.
    """
    with _attachment_resume_worker_lock:
        worker = _attachment_resume_worker
    if worker is not None:
        worker.stop(timeout=timeout)


def build_router(
    *,
    get_graph,
    get_thread_store,
    get_attachment_service=None,
    get_attachment_budgets=None,
    get_watchlist_store=None,
    get_memory_port=None,
    get_filing_monitor=None,
    get_feedback_store=None,
    get_observability_collector=None,
    get_workspace_root=None,
) -> APIRouter:
    """Build the workspace APIRouter.

    The dependencies are passed in so the router can be wired against
    any FastAPI app (the prod app in ``fin.host.http``, and any test
    app). ``get_watchlist_store`` defaults to the env-selected store so
    an existing caller that predates T4 still mounts; ``get_memory_port``
    does the same for callers that predate dream; ``get_filing_monitor``
    defaults to the process default monitor; ``get_feedback_store``
    (T8) does the same for callers that predate thumbs feedback;
    ``get_observability_collector`` (T9) defaults to a collector built
    HERE — one per mounted router, mirroring the ``RunControl`` registry
    so two apps in one test session cannot record into each other's
    counters. The prod app injects the per-process singleton.
    ``get_workspace_root`` (Wave A, spec SC8 / ADR 0049) defaults to
    the ``$HOME``-resolved default 工作区 and is the seam that
    injects ``configurable.workspace_root`` on every send / resume.

    The stop registry (:class:`RunControl`) is created HERE rather than
    at module level: one registry per mounted router means two apps in
    one test session cannot stop each other's runs. The send gate's
    in-flight counter (:class:`AttachmentUploadInFlight`) is created HERE
    for the same reason.
    """
    router = APIRouter(prefix="/api")
    run_control = RunControl()
    upload_in_flight = AttachmentUploadInFlight()
    if get_attachment_service is None:
        get_attachment_service = default_attachment_service
    if get_attachment_budgets is None:
        # Lazily imported: ``fin.host.http`` owns the deployed-budget
        # dependency and imports this module at its bottom, so a
        # module-level import here would close the cycle.
        from fin.host.http import get_attachment_budgets
    if get_watchlist_store is None:
        get_watchlist_store = default_watchlist_store
    if get_memory_port is None:
        get_memory_port = build_memory_port
    if get_filing_monitor is None:
        get_filing_monitor = default_filing_monitor
    if get_feedback_store is None:
        get_feedback_store = default_feedback_store
    if get_observability_collector is None:
        router_collector = ObservabilityCollector()
        get_observability_collector = lambda: router_collector  # noqa: E731
    if get_workspace_root is None:
        get_workspace_root = default_workspace_root_for_process

    @router.get("/threads")
    def list_threads(store: ThreadStore = Depends(get_thread_store)) -> list[dict]:
        return [_serialize_thread(t) for t in store.list_threads()]

    @router.post("/threads", response_model=CreateThreadResponse, status_code=200)
    def create_thread(
        body: CreateThreadRequest,
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        # T2 (workbench retrieve seam): a new research thread cannot be
        # opened while the product knowledge MCP is not 可达 — there is
        # no thread to 404 on, so the gate is the first check.
        if knowledge_mcp_gate_closed():
            raise KnowledgeMCPFaultError()
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
        feedback_store: Any = Depends(get_feedback_store),
    ) -> dict:
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        # T2: existence check wins (404 above); the MCP gate runs right
        # after it, so an unknown id is never masked by this 409.
        if knowledge_mcp_gate_closed():
            raise KnowledgeMCPFaultError()
        runs = store.list_runs(thread_id)
        # T8: ONE ``list_feedback()`` for the whole turn table (never a
        # query per row), then matched by run id at serialization time.
        # A thumbs outage must not blank the operator's history: the
        # table still serves, with ``feedback`` simply null on every
        # row and a WARNING left in the log.
        feedback_by_run: dict[str, str] = {}
        try:
            for record in feedback_store.list_feedback():
                feedback_by_run[record.run_id] = record.value
        except Exception:
            logger.warning(
                "get_thread: feedback store unreadable; history serves "
                "without thumbs",
                exc_info=True,
            )
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
        # T2f: WHAT a paused run is waiting for, when anything is
        # pending: ``"attachment_preparation"`` (the durable prep job +
        # auto-resumer own the resume — no operator action) or
        # ``"choice_window"`` (the operator's pick resumes). ``null``
        # when a plain crash checkpoint has no interrupt at all. Read
        # only while a pause can be observed (same lock probe as the
        # flag above), through the same defensive ``hasattr`` seam.
        pending_interrupt_kind: str | None = None
        if pending_checkpoint and hasattr(graph, "interrupt_pending"):
            pending_payload = graph.interrupt_pending(thread_id)
            if isinstance(pending_payload, dict):
                raw_kind = pending_payload.get("kind")
                if isinstance(raw_kind, str) and raw_kind:
                    pending_interrupt_kind = raw_kind
        # T3 (ADR 0044): occupancy from the same compactor figure the
        # gate uses. Defensive ``hasattr`` mirrors the ``is_locked`` /
        # ``pending_nodes`` wiring-bug tolerance for graph stand-ins
        # without the seam.
        occupancy = None
        if hasattr(graph, "occupancy"):
            occupancy = graph.occupancy(thread_id)
        return {
            **_serialize_thread(thread),
            "in_flight": is_locked,
            "pending_checkpoint": pending_checkpoint,
            "pending_interrupt_kind": pending_interrupt_kind,
            "occupancy": occupancy,
            "turns": [
                _serialize_run(
                    r,
                    in_flight=(is_locked and i == len(runs) - 1),
                    feedback=feedback_by_run.get(r.id),
                )
                for i, r in enumerate(runs)
            ],
        }

    @router.delete("/threads/{thread_id}")
    def delete_thread(
        thread_id: str,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        existing = store.get_thread(thread_id)
        if existing is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        # The thread's attachments are voided first, and their bytes are
        # removed only behind that tombstone. An attachment that outlived its
        # thread would keep searchable text a deleted research thread owned,
        # so a cleanup failure stops the delete here — named, logged, and
        # retryable — instead of acknowledging a thread whose bytes remain.
        try:
            attachments.delete_thread(thread_id)
        except AttachmentCleanupError:
            logger.exception(
                "delete_thread: attachment cleanup failed for thread_id=%s",
                thread_id,
            )
            return JSONResponse(
                status_code=500,
                content={"error": "attachment_cleanup_failed"},
            )

        # Metadata next, then checkpointer. If the checkpointer
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
        background: BackgroundTasks,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
        memory_port: Any = Depends(get_memory_port),
        collector: ObservabilityCollector = Depends(
            get_observability_collector
        ),
        workspace_root: Path = Depends(get_workspace_root),
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

        # ADR 0083/0089 send gate, host half: while this thread has an
        # upload still writing its bytes, no run starts here — a second
        # client cannot slip past an in-flight attach. The pending file
        # needs no mention in the message: its record does not exist yet,
        # so the reference check below could never see it. Decided before
        # the concurrency probe, the title rewrite, and any graph work, so
        # the refusal leaves the thread's rows, title, lock, and run record
        # untouched (the same zero-side-effect ordering as that check).
        if upload_in_flight.pending(thread_id):
            return JSONResponse(
                status_code=409,
                content={"error": "attachment_upload_in_progress"},
            )

        # T6 (spec SC9): an explicitly named 检索档 that this product
        # does not implement refuses HERE — before the concurrency
        # probe, before any side effect, and long before a retrieve
        # could be dressed up as the tier the operator asked for. The
        # code is named per 档 (综述未开 / 真库未接), never collapsed
        # into one token; lexical/graph fall through to the ordinary run
        # because 查找 is openable on the default stack today.
        requested_tier = unavailable_tier(body.tiers)
        if requested_tier is not None:
            return JSONResponse(
                status_code=400,
                content={"error": TIER_UNAVAILABLE_CODE[requested_tier]},
            )

        # T2 (workbench retrieve seam): existence is settled (404 above),
        # so the knowledge-MCP gate runs HERE — before the concurrency
        # probe and before any side effect. A gated send must not consume
        # attachments, rewrite the title, or start a run.
        if knowledge_mcp_gate_closed():
            raise KnowledgeMCPFaultError()

        # T2 (ADR 0044 / plan session-compaction T2): ``/compact`` is
        # intercepted BEFORE Soft-Track classify — Soft-Track classify
        # is the first graph node (START -> classify), so this branch
        # runs before any stream run; the string is never classified
        # as a user question and never a research tool. Match is exact
        # on the stripped, case-folded text: ``" /Compact "`` is the
        # command, ``"/compact now"`` is an ordinary question. The
        # unknown-thread 404 above still wins (contract preserved), and
        # the title rewrite below is deliberately skipped — the command
        # must not become the thread title.
        if body.question.strip().casefold() == "/compact":
            return _run_compact_command(
                graph=graph,
                store=store,
                thread_id=thread_id,
            )

        # ADR 0083/0088 + T2f: referenced attachments are resolved from this
        # thread's durable store BEFORE the lock probe and BEFORE the title
        # rewrite, so a bad reference is a named 400 with zero side effects.
        # Unknown OR other-thread ids share one error (no existence leak
        # across threads). A stored ``uploaded`` / ``parsing`` row is NOT a
        # refusal any more: the id rides the run state with an empty
        # ``text`` and the graph's preparation gate starts-or-joins the
        # durable job, suspends the run (releasing the lock), and hydrates
        # the text when it resumes (spec: accept a stored uploaded
        # attachment without waiting). A ``failed`` row keeps the named 400
        # because a repeated turn must not re-spend a named failure — the
        # retry door owns that decision — while a failure that lands AFTER
        # the run started still reaches the turn row as the gate's typed
        # refusal, never as an empty success.
        # Resolution is read-only — an attachment stays referenceable for
        # the rest of the thread's life.
        referenced_ids = list(dict.fromkeys(body.attachment_ids or []))
        resolved_attachments: list[dict[str, str]] = []
        for attachment_id in referenced_ids:
            record = attachments.status(attachment_id)
            if record is None or record.thread_id != thread_id:
                return JSONResponse(
                    status_code=400,
                    content={"error": "attachment_not_found"},
                )
            if record.status == "failed":
                return JSONResponse(
                    status_code=400,
                    content={"error": "attachment_not_ready"},
                )
            text = ""
            if record.searchable:
                try:
                    resolved = attachments.resolve_for_turn(
                        thread_id=thread_id, attachment_id=attachment_id
                    )
                    text = resolved.text
                except (AttachmentNotReadyError, AttachmentNotFoundError):
                    # EXIT: the derived data went unreadable between the
                    # two reads. Empty text is the not-ready shape the
                    # gate already handles — ask and wait — so this door
                    # never guesses a failure the durable job may cure.
                    text = ""
            resolved_attachments.append(
                {
                    "attachment_id": record.attachment_id,
                    "filename": record.filename,
                    "text": text,
                }
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

        # search-mode host T2: a PAUSED choice window releases the
        # per-thread lock (the run's stream ended on the interrupt), so
        # the same-thread send needs this second probe to keep the 409
        # semantics: the run is still in flight on the checkpoint,
        # waiting for the operator's pick. Same typed 409
        # ``concurrent_run`` answer — placed BEFORE the attachment
        # consumption below, so a rejected send keeps its attachments.
        if (
            hasattr(graph, "interrupt_pending")
            and graph.interrupt_pending(thread_id) is not None
        ):
            raise ConcurrentRunError(
                f"a choice window is pending on thread_id={thread_id!r}"
            )

        # T2: the input state ALWAYS resets the run-scoped fields
        # (``reset_input_state`` clears ``evidence`` — impossible
        # with ``operator.add + []`` — and zeroes ``step_count`` /
        # ``repair_count``) so a follow-up post starts a fresh run,
        # not a continuation. ``messages`` keeps accumulating via
        # ``add_messages``; the helper clears the six run-scoped
        # fields per ADR 0016 / spec line 34.
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread_id,
                # LangGraph persists configurable.run_id in checkpoint
                # metadata. Keeping the HTTP run id there makes a later
                # crash resume address the same canonical run.
                "run_id": run_id,
                # Wave A (spec SC8 / ADR 0049): the seam injects the
                # one default 工作区 on every send — the graph reads
                # it PER CALL and raises WorkspaceRootMissingError if
                # a seam forgets.
                "workspace_root": str(workspace_root),
            }
        }
        # The existing ``tiers`` selector is the documented pin field —
        # naming EXACTLY ONE open 档 pins it for this run (the rules tier
        # assigns without opening the window; the audit records the pin). An
        # unopened 档 was already the named 400 above; a multi-select names
        # nothing (the ordinary cascade decides, same as an empty selector
        # today). The pin rides the run CONFIG, never the checkpointed
        # state, so a follow-up send cannot inherit it. The value is copied
        # straight: the door's enum is the SSOT closed set, so what arrives
        # here is already the exact token the port validates and there is no
        # legacy spelling left to resolve on the way.
        if len(body.tiers) == 1:
            config["configurable"]["pinned_search_mode"] = body.tiers[0]
        thinking_type = "adaptive" if body.thinking else "disabled"
        if not body.thinking:
            # T3 (#34): the operator's thinking preference rides the
            # run config — ``model_node`` swaps to the pre-built
            # no-thinking binding (``extra_body`` thinking.type=
            # disabled, OpenAI/方舟; no MiniMax reasoning_split). Only the OFF
            # preference is stamped; the default (on) keeps the boot
            # binding untouched.
            config["configurable"]["thinking_type"] = thinking_type

        # search-mode host T4: the propose seed is a run STATE field, not
        # a configurable (unlike the T2 pin above) — ``reset_input_state``
        # zeroes ``extract_method_proposal`` on every run, so the seed is
        # stamped ON TOP of the fresh reset dict here: the overwrite
        # channel takes the input value, the next send's reset clears it
        # (no checkpoint leak). Without the field the key stays the
        # reset's ``{}`` — ``proposal_is_pending`` never fires.
        input_state = reset_input_state(
            body.question,
            attachments=resolved_attachments,
        )
        if body.propose_extract_method is not None:
            seed: dict[str, Any] = {"proposed": body.propose_extract_method}
            # The apply target rides the SAME run-scoped channel; a
            # named half is carried verbatim so the audit can say WHY
            # nothing applied (missing_version_context).
            if body.propose_document_id is not None:
                seed["document_id"] = body.propose_document_id
            if body.propose_version is not None:
                seed["version"] = body.propose_version
            input_state["extract_method_proposal"] = seed

        def event_stream() -> Iterator[str]:
            """Yield the delta wire for this run, then persist + close.

            ``_stream_run_frames`` owns the frame mapping and
            returns the final state via ``yield from``. RunRecord
            persistence is best-effort — if the thread was deleted
            mid-stream the persistence is skipped rather than
            resurrected (a row for a deleted thread is never
            wanted) and a single refuse-status frame explains it.
            """
            # T9: the turn's wall-clock latency starts when the wire
            # starts carrying the run (same anchor as thinking_ms —
            # the streaming window IS the turn the operator sees).
            turn_started = time.perf_counter()

            def record_terminal(status: str) -> None:
                """Record one terminal turn on the observability collector.

                Counters only — the status enum plus elapsed
                milliseconds; no question / answer / URL text ever
                reaches the collector. A recording failure is a
                WARNING, never a broken wire or a lost turn row
                (same tolerance as the feedback store on GET-thread).
                """
                elapsed_ms = (time.perf_counter() - turn_started) * 1000.0
                try:
                    collector.record(status, latency_ms=elapsed_ms)
                except ObservabilityError:
                    logger.warning(
                        "send: failed to record terminal status %r on the "
                        "observability collector",
                        status,
                        exc_info=True,
                    )

            seen_message_ids: set[str] = set()
            # Seed the dedup set with the pre-run checkpoint's
            # message ids: a follow-up send's first ``values`` frame
            # replays the whole accumulated list, and prior runs'
            # tool frames must not re-emit onto this stream
            # (spec line 34).
            if hasattr(graph, "checkpoint_message_ids"):
                seen_message_ids |= graph.checkpoint_message_ids(thread_id)
            # T4 workflow (the tracked change): register the run as stoppable BEFORE
            # the first frame so a Stop pressed during the very first
            # model call still lands. ``finally`` deregisters so a
            # later Stop on an idle thread answers 204.
            stop_token = run_control.begin(thread_id, run_id)
            try:
                outcome = yield from _stream_run_frames(
                    graph,
                    run_id=run_id,
                    input_value=input_state,
                    config=config,
                    seen_message_ids=seen_message_ids,
                    thread_id=thread_id,
                    stop_token=stop_token,
                )
            finally:
                run_control.end(stop_token)

            if outcome.stopped:
                # A stop is a terminal turn (persisted as ``refuse`` /
                # stopped), so it counts on the observability wire too.
                record_terminal("refuse")
                yield from _persist_and_close_stopped_run(
                    graph,
                    store=store,
                    thread_id=thread_id,
                    run_id=run_id,
                    question=body.question,
                    thinking_ms=outcome.thinking_ms,
                )
                return

            if outcome.choice_window is not None:
                # search-mode host T2: the run PAUSED on the choice
                # window — the engine already emitted the frame. Persist
                # NOTHING and emit no terminal frames: a paused run is
                # not a finished turn (the resumed remainder's
                # completion writes the row under the SAME checkpoint
                # run_id). The stream close is the SPA's cue to paint
                # the window.
                return

            if outcome.attachment_wait is not None:
                # T2f: the run PAUSED on the attachment preparation gate
                # — the engine already emitted the ``attachment_wait``
                # frame with the run id. Same pause posture as the
                # choice window: persist NOTHING and close; the
                # auto-resumer drives the SAME checkpoint run_id to
                # completion once the durable job settles, and that
                # resumed remainder writes the one turn row.
                return

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
                record_terminal("refuse")
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
            # T9: the turn's terminal status (done / refuse / step_cap)
            # and wall-clock latency land on the observability collector
            # exactly where the turn row is built.
            record_terminal(payload["status"])
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
                        # T3: the structured table the turn renders.
                        # Persisted on refuse rows too — see TurnRow.
                        comparisons=list(payload.get("comparisons") or []),
                        analysis_results=list(
                            payload.get("analysis_results") or []
                        ),
                        # SC8: this run's retrieved / read locators,
                        # taken from the final state BEFORE the next
                        # send resets the evidence channel.
                        evidence_locators=list(payload["evidence_locators"]),
                    )
                )
            except ThreadNotFoundError:
                # The thread was deleted mid-stream; do NOT
                # resurrect metadata. Yield a single error frame
                # so the SPA surfaces the state, then stop. The
                # turn still ended on a terminal refuse.
                record_terminal("refuse")
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

        # Dream runs AFTER the stream is exhausted, never inside a graph
        # node (CONTEXT.md:46); see ``_register_dream_background``.
        _register_dream_background(background, memory_port, thread_id)
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/threads/{thread_id}/attachments")
    async def attach_to_thread(
        thread_id: str,
        request: Request,
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
        budgets: AttachmentBudgets = Depends(get_attachment_budgets),
    ) -> dict:
        """ADR 0083/0086/0089 **thread attach**: the durable upload door.

        A file dropped into the open research thread is stored as raw
        bytes under this thread's ownership and answered with a logical
        ``attachment_id`` plus ``uploaded``. Nothing else happens on this
        path: no parsing, OCR, layout, vision, or indexing, and no
        background task behind the response. Preparation is a separate,
        requested step (ADR 0089) and is what later makes the attachment
        ``ready`` for every turn of this thread. Nothing is pushed and
        nothing enters the permanent company corpus (ADR 0047 keeps those
        doors separate).

        Wire shape: raw bytes body with the filename as the
        ``filename`` query parameter. Deliberately NOT multipart
        ``UploadFile``: this door takes one file and no form fields, so
        the web client's plain ``fetch`` body is the entire contract and
        neither side pays for multipart handling (the ingest route is the
        only multipart surface). The model gets the logical id, never a
        path.

        Named boundaries (no silent success, and every failure key is
        attach-namespaced, never ingest vocabulary):

        * unknown thread            -> 404 ``thread_not_found``
        * empty body                -> 400 ``empty_attachment``
        * body over the byte cap    -> 413 ``attachment_too_large``
        * durable write fails       -> 500 ``attachment_store_failed``
          (the exception boundary: never a half-stored 200 whose id the
          next send could reference, and never ingest vocabulary here)

        The byte cap is the deployed
        :attr:`~fin.host.attachment_budgets.AttachmentBudgets.max_upload_bytes`,
        read through the ``get_attachment_budgets`` dependency — not a
        constant this module carries, so the door cannot drift from the
        bound the operator configured. The 413 is decided at the ROUTE
        layer before the body is buffered: a declared oversize
        Content-Length is refused unread, and a chunked body is read in
        bounded segments (:func:`_read_bounded_body`) — the cap is never
        discovered after a full-buffer allocation. Either way the refusal
        stores no object and no attachment row.

        Bytes this door cannot classify are still accepted: judging the
        content is preparation's job, which names the rejection there.

        While this route is reading and storing the body the thread's send
        gate is closed (:meth:`AttachmentUploadInFlight.hold`), so no refusal
        can strand it: the handle releases on every return AND every raise,
        including a client that walks away mid-body.
        """
        with upload_in_flight.hold(thread_id):
            if store.get_thread(thread_id) is None:
                return JSONResponse(
                    status_code=404, content={"error": "thread_not_found"}
                )
            if _declared_upload_over(request, budgets.max_upload_bytes):
                return JSONResponse(
                    status_code=413, content={"error": "attachment_too_large"}
                )
            body = await _read_bounded_body(
                request, budgets.max_upload_bytes
            )
            if body is None:
                return JSONResponse(
                    status_code=413, content={"error": "attachment_too_large"}
                )
            if not body:
                return JSONResponse(
                    status_code=400, content={"error": "empty_attachment"}
                )
            try:
                record = attachments.accept(
                    thread_id=thread_id,
                    filename=request.query_params.get("filename") or "",
                    payload=body,
                )
            except AttachmentStoreError:
                logger.exception(
                    "attach_to_thread: durable write failed for thread_id=%s",
                    thread_id,
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": "attachment_store_failed"},
                )
            return _attachment_wire_record(record)

    @router.get("/threads/{thread_id}/attachments")
    def list_thread_attachments(
        thread_id: str,
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        """Every attachment this thread owns, with its preparation state."""
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        return {
            "attachments": [
                _attachment_wire_record(record)
                for record in attachments.list_for_thread(thread_id)
            ]
        }

    @router.get("/threads/{thread_id}/attachments/{attachment_id}")
    def read_thread_attachment(
        thread_id: str,
        attachment_id: str,
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        """Status of one attachment on a later turn.

        An unknown id and another thread's id share ``attachment_not_found``
        so this read cannot probe what a different thread holds. A malformed
        path identity is refused earlier and separately, as the delete door
        does, so garbage text never reaches the store as a query:

        * malformed path identity  -> 400 ``invalid_locator``
        """
        refusal = _locator_refusal(thread_id, attachment_id)
        if refusal is not None:
            return refusal
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        record = attachments.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            return JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            )
        return _attachment_wire_record(record)

    @router.post("/threads/{thread_id}/attachments/{attachment_id}/retry")
    def retry_thread_attachment(
        thread_id: str,
        attachment_id: str,
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        """Re-drive a failed attachment through the durable job: no in-request parse.

        T2f: preparation is durable background work, so this door only
        re-asks — :meth:`AttachmentPreparationService.retry_preparation`
        re-enqueues a failed row's job (the job store reopens ``failed``
        jobs on a fresh enqueue) and answers with the resulting wire
        record immediately. A retried row therefore leaves this door as
        ``parsing`` (work queued), never ``ready``: the prep worker
        drains the job and the row reaches ``ready`` on a later status
        read. The door deliberately does NOT wait, and a document this
        deployment cannot read is still a 200 carrying its failed
        reason eventually, never a 500: the preparation service owns
        the whole refusal ladder, including a page document arriving at
        a deployment with no page lane.

        Retry writes state, so a malformed path identity is refused before any
        lookup or preparation, exactly as the delete door refuses it:

        * malformed path identity  -> 400 ``invalid_locator``
        * identity gone mid-flight -> 400 ``attachment_not_found``
        """
        refusal = _locator_refusal(thread_id, attachment_id)
        if refusal is not None:
            return refusal
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        record = attachments.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            return JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            )
        try:
            request = attachments.retry_preparation(thread_id, attachment_id)
            if request.state == "unknown":
                # EXIT: the row lost a deletion race between the guard
                # above and the ask; the identity answer is the same
                # one every other door gives.
                return JSONResponse(
                    status_code=400, content={"error": "attachment_not_found"}
                )
            fresh = attachments.status(attachment_id)
            if fresh is None:
                return JSONResponse(
                    status_code=400, content={"error": "attachment_not_found"}
                )
            return _attachment_wire_record(fresh)
        except AttachmentThreadGoneError:
            # EXIT: the thread was deleted between the check above and the
            # write; the attachment is gone, not the store broken.
            return JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            )
        except AttachmentStoreError:
            logger.exception(
                "retry_thread_attachment: state write failed for attachment_id=%s",
                attachment_id,
            )
            return JSONResponse(
                status_code=500, content={"error": "attachment_store_failed"}
            )

    @router.delete("/threads/{thread_id}/attachments/{attachment_id}")
    def delete_thread_attachment(
        thread_id: str,
        attachment_id: str,
        store: ThreadStore = Depends(get_thread_store),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        """Remove one attachment through the door that voids it first.

        :meth:`AttachmentPreparationService.delete_attachment` owns why the
        order is the guarantee; this door owns the named answers:

        * malformed path identity  -> 400 ``invalid_locator``
        * unknown thread           -> 404 ``thread_not_found``
        * unknown, other thread's,
          or already-deleted id    -> 400 ``unknown_locator``
        * bytes not removable      -> 500 ``attachment_cleanup_failed``
          (logged with the owed key; tombstone and obligation retained)
        * the durable store refuses
          the void                 -> 500 ``attachment_store_failed``

        A DELETE carries no input but these two path identities, so a request
        body — well formed or not — is ignored rather than parsed.
        """
        refusal = _locator_refusal(thread_id, attachment_id)
        if refusal is not None:
            return refusal
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        try:
            attachments.delete_attachment(
                thread_id=thread_id, attachment_id=attachment_id
            )
        except (AttachmentNotFoundError, AttachmentThreadGoneError) as exc:
            # EXIT: a missing thread took its attachments with it, which is
            # the same answer as an id that never existed — and the same one
            # another thread's id gets, so this door cannot be used to probe
            # who holds a given attachment.
            logger.info(
                "delete_thread_attachment: nothing to void: %s", exc
            )
            return JSONResponse(
                status_code=400, content={"error": "unknown_locator"}
            )
        except AttachmentCleanupError:
            logger.exception(
                "delete_thread_attachment: cleanup failed for thread_id=%s "
                "attachment_id=%s",
                thread_id,
                attachment_id,
            )
            return JSONResponse(
                status_code=500, content={"error": "attachment_cleanup_failed"}
            )
        except AttachmentStoreError:
            logger.exception(
                "delete_thread_attachment: state write failed for "
                "thread_id=%s attachment_id=%s",
                thread_id,
                attachment_id,
            )
            return JSONResponse(
                status_code=500, content={"error": "attachment_store_failed"}
            )
        return {"ok": True, "attachment_id": attachment_id}

    @router.post("/threads/{thread_id}/resume")
    def resume_thread(
        thread_id: str,
        background: BackgroundTasks,
        body: ResumeRequest | None = Body(default=None),
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
        memory_port: Any = Depends(get_memory_port),
        workspace_root: Path = Depends(get_workspace_root),
    ) -> Response:
        """T4 crash resume (spec line 39 / ADR 0026 / ADR 0018) +
        search-mode host T2 choice-window pick.

        Continues an interrupted run from the LAST COMPLETE
        host-graph node via ``graph.stream(None, config=...)`` —
        LangGraph's ``input=None`` contract means "resume from the
        last checkpoint". No new ``HumanMessage`` is appended (the
        user turn is never deleted nor duplicated), and a completed
        ``model_node`` message (full content + parsed ``tool_calls``,
        per spec line 37) is NOT regenerated: the checkpointer
        resumes at the pending node (``tools`` after a model-complete
        crash).

        ``{"pick": ...}`` body (T2): answers a pending 检索档 choice
        window via ``Command(resume=pick)`` on the SAME delta wire.
        Rejection order keeps every failure mode before any SSE
        header:

        * 404 - unknown thread metadata row;
        * 409 - a run is currently in flight on this thread
          (lock probe; ``application/json`` body, no stream);
        * 400 ``choice_not_pending`` - a pick arrived while NO window
          is pending (typed, never a silently ignored field nor a
          crash-resume hijack);
        * 204 - nothing to resume: the prior run reached a terminal
          node (LangGraph's ``stream(None)`` on a terminal
          checkpoint is a documented no-op and the spec forbids
          calling it);
        * 503 - ingest missing (shared handler, raised by the graph
          dependency itself).

        A body-less resume against a pending window REDRAWS the
        ``choice_window`` frame (``stream(None)`` re-raises the
        interrupt) — the SPA's reload path, with no new endpoint to
        learn.
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

        # search-mode host T2: a pick answers a PENDING window — with
        # none open it is a typed 400 (never a hijacked crash resume,
        # never a silently dropped field). A body-less resume is the
        # pre-T2 crash path unchanged.
        pick = body.pick if body is not None else None
        if pick is not None:
            pending_choice = (
                graph.interrupt_pending(thread_id)
                if hasattr(graph, "interrupt_pending")
                else None
            )
            if pending_choice is None:
                return JSONResponse(
                    status_code=400, content={"error": "choice_not_pending"}
                )

        # Nothing to resume -> 204 No Content. A terminal checkpoint
        # (``pending_nodes() == ()``) means the prior run finished;
        # LangGraph's ``stream(None)`` would be a no-op and the spec
        # forbids that path. Also covers a never-sent thread (no
        # checkpoint at all).
        if not hasattr(graph, "pending_nodes") or not graph.pending_nodes(thread_id):
            return Response(status_code=204)

        # Reuse the id attached to the recoverable checkpoint. A resume
        # continues the in-flight run; minting here would split the SSE
        # turn from the canonical checkpoint identity.
        run_id = (
            graph.checkpoint_run_id(thread_id)
            if hasattr(graph, "checkpoint_run_id")
            else None
        )
        if not run_id:
            return JSONResponse(
                status_code=500,
                content={"error": "resume_run_id_unavailable"},
            )
        resume_config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread_id,
                "run_id": run_id,
                # Wave A (spec SC8 / ADR 0049): resume re-injects the
                # one default 工作区 so the remainder of the run
                # resolves memory/profile seams from the same root.
                "workspace_root": str(workspace_root),
            }
        }

        # search-mode host T2: with a validated pick the resume INPUT is
        # LangGraph's ``Command(resume=pick)``; without one it stays the
        # crash-resume ``None`` (or, against a pending window, redraws
        # the window frame).
        resume_input: Any = Command(resume=pick) if pick is not None else None

        def event_stream() -> Iterator[str]:
            """Yield the resumed remainder on the SAME delta wire.

            Delegates to :func:`_resume_run_stream` — the one resume
            path shared with the T2f auto-resumer, so the two surfaces
            cannot drift on persistence or terminal frames.
            """
            yield from _resume_run_stream(
                graph,
                store,
                run_control=run_control,
                thread_id=thread_id,
                run_id=run_id,
                resume_input=resume_input,
                resume_config=resume_config,
            )

        _register_dream_background(background, memory_port, thread_id)
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/threads/{thread_id}/stop")
    def stop_thread(
        thread_id: str,
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """Stop the in-flight run on this thread (T4 workflow, the tracked change).

        Rejection order mirrors send / resume:

        * 404 — unknown thread metadata row;
        * 204 — nothing in flight (a Stop the operator pressed just
          after the run ended is a no-op, not a failure);
        * 200 ``{"stopped": true, "run_id": ...}`` — the flag is set.

        This route deliberately does NOT touch the graph: the run owns
        the per-thread lock, so acquiring it here would deadlock the
        one moment a stop matters. It flips the token and returns; the
        SSE generator does the ending.
        """
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        stopped_run_id = run_control.request_stop(thread_id)
        if stopped_run_id is None:
            return Response(status_code=204)
        return JSONResponse(
            status_code=200,
            content={"stopped": True, "run_id": stopped_run_id},
        )

    @router.get("/threads/{thread_id}/export")
    def export_thread(
        thread_id: str,
        kind: str = "comparison",
        format: str = "csv",
        run_id: str | None = None,
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """Download this thread's comparison or citation bundle.

        Comparison exports bind to ``fin.comparison.v1`` — the same
        envelopes the turn row persists (``TurnRow.comparisons``) and
        the SSE ``comparison`` frame carried. ``fin.host.export`` owns
        the rendering; this route owns only the HTTP shape.

        Typed failures, all with a stable ``error`` code:
        ``thread_not_found`` / ``run_not_found`` / ``export_empty``
        (404), ``unsupported_export`` (400),
        ``unsupported_comparison_schema`` (422).
        """
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        try:
            export = build_export(
                runs=store.list_runs(thread_id),
                kind=kind,
                thread_id=thread_id,
                run_id=run_id,
                fmt=format,
            )
        except UnsupportedExportError as exc:
            return JSONResponse(status_code=400, content={"error": exc.code})
        except ExportError as exc:
            status = 422 if exc.code == "unsupported_comparison_schema" else 404
            return JSONResponse(status_code=status, content={"error": exc.code})
        return Response(
            content=export.body,
            media_type=export.media_type,
            headers={
                # ``attachment`` is what makes the browser save a file
                # rather than render the CSV as a page of text.
                "Content-Disposition": f'attachment; filename="{export.filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/threads/{thread_id}/runs/{run_id}/feedback")
    def set_run_feedback(
        thread_id: str,
        run_id: str,
        body: FeedbackRequest,
        store: ThreadStore = Depends(get_thread_store),
        feedback_store: Any = Depends(get_feedback_store),
    ) -> Response:
        """Record (or flip) the operator's thumb for one run (T8).

        Feedback is a SIDE record keyed by ``run_id``
        (``fin.host.feedback``, table ``fin_run_feedback``): it carries
        no answer, citation, or evidence fields, so setting or flipping
        a thumb can never mutate the run's persisted citations
        (ADR 0035/0036, spec SC10 — pinned by
        ``tests/host/test_feedback_route.py``).

        Typed failures, all with a stable ``error`` code:

        * ``invalid_feedback`` (400) — value outside ``up``/``down``,
          or an empty / oversized run id (``MAX_RUN_ID_LENGTH``);
        * ``thread_not_found`` / ``run_not_found`` (404) — same shapes
          the export route answers with;
        * ``feedback_persistence_failed`` (503) — the store could not
          complete the write (a storage outage, not a client error).
        """
        # Boundary classes first (400 before 404): a malformed id is a
        # client error regardless of whether the thread exists, and the
        # store would normalize/validate the same values a second time.
        normalized_run_id = run_id.strip()
        if (
            not normalized_run_id
            or len(normalized_run_id) > MAX_RUN_ID_LENGTH
        ):
            return JSONResponse(
                status_code=400, content={"error": "invalid_feedback"}
            )
        if body.value not in ("up", "down"):
            return JSONResponse(
                status_code=400, content={"error": "invalid_feedback"}
            )
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        if not any(r.id == run_id for r in store.list_runs(thread_id)):
            return JSONResponse(
                status_code=404, content={"error": "run_not_found"}
            )
        try:
            record = feedback_store.set_feedback(run_id, body.value)
        except InvalidFeedbackError:
            # Defense in depth: the store re-validates what the route
            # already checked.
            return JSONResponse(
                status_code=400, content={"error": "invalid_feedback"}
            )
        except FeedbackPersistenceError:
            logger.exception(
                "set_run_feedback: persistence failed for run_id=%s", run_id
            )
            return JSONResponse(
                status_code=503,
                content={"error": "feedback_persistence_failed"},
            )
        return JSONResponse(
            status_code=200,
            content={
                # Snake_case exactly as web/src/feedback.ts parses.
                "run_id": record.run_id,
                "value": record.value,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            },
        )

    @router.get("/watchlist")
    def list_watchlist(
        watchlist: JsonFileWatchlistStore = Depends(get_watchlist_store),
    ) -> list[dict]:
        """Watched issuers. Empty is ``[]``, never an error (SC4)."""
        return [_serialize_watch(w) for w in watchlist.list_watches()]

    @router.post("/watchlist")
    def create_watch(
        body: CreateWatchRequest,
        watchlist: JsonFileWatchlistStore = Depends(get_watchlist_store),
    ) -> Response:
        try:
            entry = watchlist.add_watch(issuer=body.issuer, forms=body.forms)
        except InvalidWatchError:
            return JSONResponse(status_code=400, content={"error": "invalid_watch"})
        return JSONResponse(status_code=200, content=_serialize_watch(entry))

    @router.delete("/watchlist/{watch_id}")
    def delete_watch(
        watch_id: str,
        watchlist: JsonFileWatchlistStore = Depends(get_watchlist_store),
    ) -> Response:
        try:
            watchlist.remove_watch(watch_id)
        except WatchNotFoundError:
            return JSONResponse(
                status_code=404, content={"error": "watch_not_found"}
            )
        return JSONResponse(status_code=200, content={"ok": True})

    @router.get("/notices")
    def list_notices(
        unread_only: bool = False,
        watchlist: JsonFileWatchlistStore = Depends(get_watchlist_store),
        monitor: FilingMonitor = Depends(get_filing_monitor),
    ) -> list[dict]:
        """In-app notices for watched filings in the LOCAL catalog and
        configured real filing source.

        Reconcile-on-read: the catalog metadata table is not an event
        source, so the read IS the delivery. Nothing leaves the
        process — spec Focus 3 forbids outbound email / Slack.

        A missing or empty catalog table refreshes against no local
        filings (ADR 0047: the empty catalog is ``{"filings": []}``,
        not a failure). A corrupt table file raises the typed
        ``CatalogCorruptError``, which the shared handler turns into
        the typed 503 the rest of the corpus surface already uses. A
        monitor outage leaves the persisted rows in the response and
        adds typed degraded headers.
        """
        refresh_notices(watchlist, filings=list_filings()["filings"])
        headers = {"X-Filing-Monitor-Status": "healthy"}
        try:
            monitor.poll(watchlist)
        except FilingSourceUnavailableError as exc:
            headers["X-Filing-Monitor-Status"] = "degraded"
            headers["X-Filing-Monitor-Error"] = exc.code
        notices = [
            _serialize_notice(n)
            for n in watchlist.list_notices(unread_only=unread_only)
        ]
        return JSONResponse(content=notices, headers=headers)

    @router.post("/notices/{notice_id}/read")
    def mark_notice_read(
        notice_id: str,
        watchlist: JsonFileWatchlistStore = Depends(get_watchlist_store),
    ) -> Response:
        try:
            notice = watchlist.mark_notice_read(notice_id)
        except NoticeNotFoundError:
            return JSONResponse(
                status_code=404, content={"error": "notice_not_found"}
            )
        return JSONResponse(status_code=200, content=_serialize_notice(notice))

    @router.get("/corpus")
    def corpus() -> dict:
        """List the catalog's ingested filings (metadata table, ADR 0047).

        An empty or missing table is ``{"filings": []}`` — the empty
        catalog, not an error. A corrupt table file raises the typed
        ``CatalogCorruptError`` (→ 503 ``ingest_missing`` via the
        shared handler).
        """
        return list_filings()

    @router.get("/status", response_model=WorkspaceStatusResponse)
    def workspace_status() -> dict:
        """The left corner's boot-scoped surface (ADR 0033, amended).

        No ``Depends``: the read seam is the in-process knowledge-MCP
        snapshot plus fin-local config and the catalog, so the handler
        needs neither the compiled graph nor the thread store. It
        builds no MCP client and issues no health-check RPC — the
        snapshot is filled once by ``publish_knowledge_mcp()`` at
        ``fin-serve`` boot.

        Every state is a 200 with all five fields populated. A process
        that never probed (plain ``TestClient`` tests) reports 缺 —
        this process has no confirmed reachable MCP — while the
        conversation gate stays open for that same hermetic case (see
        ``fin.host.knowledge_mcp``).

        ``retrieve_adapter_kind`` publishes the BOUND port, not the
        configured string: the (adapter, state) pair this payload
        carries is then read by the same predicate the conversation
        gate answers with, which is what
        ``scripts/lib/readiness.sh`` classifies on. Publishing the
        configured kind made a scripted boot (fake port, gate open)
        read as the product port and therefore as GATED.

        ``bound_collection_name`` reads config + that same snapshot (no
        extra call, no catalog) and reports ``unbound`` whenever the
        process is not bound to a live iknow 资料集合; see
        :mod:`fin.host.collection_bind`.
        """
        published = knowledge_mcp_state()
        knowledge_mcp = (
            published if published in KNOWLEDGE_MCP_STATES else "missing"
        )
        return {
            "knowledge_mcp": knowledge_mcp,
            "retrieve_adapter_kind": resolve_bound_retrieve_adapter_kind(),
            "model_identity": _model_identity(),
            "corpus_filing_count": _corpus_filing_count(),
            "bound_collection_name": resolve_bound_collection_name(),
        }

    @router.post("/corpus/ingest")
    def corpus_ingest(
        request: Request,
        file: UploadFile | None = File(default=None),
        document_id: str | None = Form(default=None),
        company: str | None = Form(default=None),
        period: str | None = Form(default=None),
        doc_type: str | None = Form(default=None),
        format: str | None = Form(default=None),
        service: IngestEndpoint = Depends(get_ingest_service),
    ) -> Response:
        """Corpus-ingest door: accept one upload durably and return its job.

        The ONLY product surface that takes a company document into the
        permanent corpus (ADR 0047). It is **accept-and-persist**: parse,
        切分 and push run asynchronously off the request (drained by the
        boot worker, :func:`start_ingest_worker`), so a 200 here says the
        upload is durable and queued,
        never that anything was ingested — ``accepted`` replaced the old
        ``ingested`` token, and the outcome is polled at
        ``GET /api/corpus/ingest/{job_id}``.

        Multipart upload + optional metadata form fields: ``format``
        (explicit declaration; otherwise the filename extension names the
        format), ``document_id`` (wins verbatim) or ``company`` +
        ``period`` (the ``"{TICKER}-{FYQ}"`` identity; the filename is
        metadata only and never names the document), ``doc_type`` (optional
        params-table scene override).

        Accepted means all four accept-time items are durable: the raw
        object, its metadata pointer, the ``pending`` job record and the
        ``(document_id, version)`` identity. Every validation that needs no
        bytes runs before the first write, so a refused upload leaves
        nothing behind; a persistence failure is a typed 5xx with no
        accepted job. The exception -> answer mapping lives in
        :func:`_ingest_error_response` (table-driven).
        """
        if file is None:
            return JSONResponse(
                status_code=422, content={"error": "missing_file"}
            )
        # Route-layer upload gate (input boundary): refuse a declared
        # oversize body BEFORE the spooled upload is read into memory,
        # and bound the read when no Content-Length is declared. The
        # cap is the service's own (single source of truth; the
        # service cap check stays as defense in depth).
        cap = service.max_bytes
        if _declared_upload_over(request, cap + _INGEST_MULTIPART_FRAMING_ALLOWANCE):
            return JSONResponse(
                status_code=413,
                content={"error": "ingest_upload_too_large"},
            )
        # Sync handler: read the underlying SpooledTemporaryFile (the
        # async ``file.read()`` coroutine would need an ``async def``
        # route, putting the whole accept on the event loop) — bounded, so
        # an unbounded upload never materializes here.
        data = _read_bounded_upload(file.file, cap)
        if data is None:
            return JSONResponse(
                status_code=413,
                content={"error": "ingest_upload_too_large"},
            )
        try:
            submission = service.submit_durable(
                data,
                filename=file.filename,
                declared_format=format,
                document_id=document_id,
                company=company,
                period=period,
                doc_type=doc_type,
            )
        except Exception as exc:
            return _ingest_error_response(exc)
        return JSONResponse(
            status_code=200,
            content={
                "status": "accepted",
                "job_id": submission.job_id,
                "document_id": submission.document_id,
                "version": submission.version,
                "state": submission.state.value,
            },
        )

    @router.get("/corpus/ingest/{job_id}")
    def corpus_ingest_status(
        job_id: str,
        service: IngestEndpoint = Depends(get_ingest_service),
        index_job_tracker: IndexJobTracker = Depends(get_index_job_tracker),
    ) -> Response:
        """Poll one ingest job: the PERSISTED state, never a constant.

        ``job_id`` is the state store's key, i.e. the ``document_id``, so
        the handle returned at accept time is the one polled here. An
        unknown id is the named ``unknown_job`` 404 — never an empty
        success body that a poller would read as "still running".

        ``text_ready`` is the chunk/text side of the pipeline (the job
        reached ``ready``). ``graph`` is the iknow-memory index-job side,
        pass-through only: once the job is text-ready it carries the
        tracker's polled graph (``null`` before the first submit ever
        ran, a refused poll keeping the last persisted state beside an
        ``error`` — never a claim of graph/C readiness derived from
        anything fin computed). Non-ready jobs carry ``graph: null``.
        The poll is the tracker's designed pure-read path; a poll that
        fails outright is reported as ``graph_status_failed`` beside an
        empty job list, it never turns the status read itself into a 500.
        """
        try:
            status = service.job_status(job_id)
        except UnknownJobError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": "unknown_job", "job_id": exc.document_id},
            )
        except Exception as exc:
            return _ingest_error_response(exc)
        graph: Any = None
        if status.state is IngestState.READY:
            try:
                graph = index_job_tracker.graph_status(status.document_id)
            except Exception as exc:
                logger.exception(
                    "ingest status: graph status poll failed for %s",
                    status.document_id,
                )
                graph = {
                    "jobs": [],
                    "community_index_state": None,
                    "error": {
                        "code": "graph_status_failed",
                        "message": str(exc),
                    },
                }
        return JSONResponse(
            status_code=200,
            content={
                "job_id": status.job_id,
                "document_id": status.document_id,
                "version": status.version,
                "state": status.state.value,
                "error_type": status.error_type,
                "error_message": status.error_message,
                "text_ready": status.text_ready,
                "graph": graph,
            },
        )

    @router.get("/observability")
    def observability(
        collector: ObservabilityCollector = Depends(
            get_observability_collector
        ),
    ) -> dict:
        """The per-process counters surface (T9).

        Terminal-status counts (``done`` / ``refuse`` / ``step_cap``),
        latency aggregates over those turns, and empty-hit
        rewrite-retry counters — nothing else. Deliberately NOT on
        ``GET /api/status`` (boot-scoped per ADR 0033) and NOT on
        ``GET /health`` (process-scoped): those surfaces grow no
        run-scoped counters, so observability gets its own route.

        The payload is counters only by construction: the collector's
        contract rejects anything that is not a typed status plus a
        latency number, and no question, answer, URL, error text, or
        credential ever enters it. Every state is a 200 — an empty
        collector is a valid answer, not a failure.
        """
        payload = collector.snapshot()
        payload["rewrite_retry"] = collector.retry_snapshot()
        return payload

    return router


# ----- mount helper -----


def mount(
    app: FastAPI,
    *,
    get_graph,
    get_thread_store,
    get_attachment_service=None,
    get_attachment_budgets=None,
    get_watchlist_store=None,
    get_memory_port=None,
    get_filing_monitor=None,
    get_feedback_store=None,
    get_observability_collector=None,
    get_workspace_root=None,
) -> None:
    """Mount the workspace routes onto an existing FastAPI app."""
    app.include_router(
        build_router(
            get_graph=get_graph,
            get_thread_store=get_thread_store,
            get_attachment_service=get_attachment_service,
            get_attachment_budgets=get_attachment_budgets,
            get_watchlist_store=get_watchlist_store,
            get_memory_port=get_memory_port,
            get_filing_monitor=get_filing_monitor,
            get_feedback_store=get_feedback_store,
            get_observability_collector=get_observability_collector,
            get_workspace_root=get_workspace_root,
        )
    )


__all__ = [
    "AttachmentResumeWorkerStoppingError",
    "IngestWorkerStoppingError",
    "build_router",
    "default_attachment_service",
    "default_workspace_root_for_process",
    "get_ingest_service",
    "get_workspace_root",
    "mount",
    "start_attachment_prep_worker",
    "start_attachment_resume_worker",
    "start_ingest_worker",
    "stop_attachment_prep_worker",
    "stop_attachment_resume_worker",
    "stop_ingest_worker",
]
