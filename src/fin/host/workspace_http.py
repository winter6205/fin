"""Workspace HTTP routes for the T1 surface.

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
* ``POST   /api/threads/{id}/messages`` - the T1 send path:
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
                                          delta wire (T2).
* ``POST   /api/threads/{id}/stop``     - cooperative stop of the
                                          in-flight run (T4 workflow):
                                          flips the run's stop
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
                                          download (T4 workflow).
                                          Comparison exports bind to
                                          ``fin.comparison``; a
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
                                          removes (T4 workflow).
* ``GET    /api/notices``               - in-app notices for watched
                                          filings in the LOCAL catalog
                                          and configured filing source; ``POST
                                          /{notice_id}/read`` dismisses
                                          one. Workspace-only — nothing
                                          is emailed or posted out.
* ``GET    /api/corpus``                - real ``list_filings()`` over
                                          the catalog metadata table
                                          (ADR 0047) — the FINANCIAL
                                          compatibility projection, whose
                                          six-key shape is unchanged.
* ``GET    /api/corpus/overview``        - the GENERIC catalog surface
                                          (T6): one entry per row with its
                                          title, type and any present
                                          company / period, plus the
                                          presentation grouping (only
                                          dimensions with two distinct
                                          non-empty values, missing values
                                          under 未分类, flat titles when no
                                          dimension qualifies). A separate
                                          mouth because ``/api/corpus`` is
                                          frozen for its financial
                                          callers.
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
                                          ``document_id_not_allowed`` /
                                          ``invalid_upload_metadata``
                                          (422),
                                          push/invalidation outage
                                          codes (503). The 200 is an
                                          ACCEPT only; a catalog
                                          registration failure after
                                          the push lands is a durable
                                          ``catalog_publication_failed``
                                          audit event read back as the
                                          ``catalog_warning`` field on the
                                          STATUS poll — never rolled
                                          back, never silent, never raw
                                          exception text. The ONLY product
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
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator, Literal, Protocol, cast

import anyio
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
from starlette.requests import ClientDisconnect
from langgraph.types import Command
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from fin.corpus.catalog import (
    catalog_grouping,
    list_catalog_entries,
    list_filings,
    record_filings,
)
from fin.corpus.catalog_publication import PublicationWarning
from fin.corpus.errors import (
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.ingest_endpoint import (
    PUBLIC_DOCUMENT_ID_REFUSAL,
    IngestConcurrentRunError,
    IngestDocumentIdNotAllowedError,
    IngestDocumentIdMissingError,
    IngestDurableCommitError,
    IngestEndpoint,
    IngestedDocument,
    IngestFormatUnspecifiedError,
    IngestInvalidDocTypeError,
    IngestInvalidUploadMetadataError,
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
from fin.host.attachment.attachment_budgets import AttachmentBudgets
from fin.host.attachment.attachment_layout import (
    HeronLayoutConfig,
    HeronPageLayout,
    LayoutRecordRunner,
)
from fin.host.attachment.attachment_prep_worker import (
    ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS,
    AttachmentPrepWorker,
    DrainCadence,
    PeriodicDrainWorker,
    drain_periodically,
)
from fin.host.attachment.attachment_prepare import (
    ATTACHMENT_PAGE_LANE_UNWIRED,
    AttachmentCleanupError,
    AttachmentNotFoundError,
    AttachmentNotReadyError,
    AttachmentPreparationService,
)
from fin.host.attachment.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    StoredPageRegion,
)
from fin.host.attachment.attachment_vision import ChatModelVisionAdapter
from fin.host.grounded.citation_gate import (
    COMPANY_KIND,
    DATABASE_ROW_KIND,
    DATABASE_ROW_SNAPSHOT_KEY,
    database_row_snapshot,
)
from fin.host.collection_bind import resolve_bound_collection_name
from fin.host.question.document_choice_admission import (
    accepted_document_reply,
    document_choice_resume_value,
    resolved_document_choice_pick,
)
from fin.host.evidence.evidence_contract import (
    CITATION_CHANNEL_SCHEMA,
    DATABASE_ROW_SNAPSHOT_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_FIELD,
    MALFORMED_EVIDENCE_CONTRACT,
    MAX_CITATIONS_PER_RUN,
    UNSUPPORTED_EVIDENCE_CONTRACT,
    AdmissionContext,
    seed_context_from_rows,
    validate_evidence,
)
from fin.host.export import (
    ExportError,
    MalformedCitationRowError,
    UnsupportedComparisonSchemaError,
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
    DOCUMENT_CHOICE_INTERRUPT_KIND,
    ConcurrentRunError,
    KnowledgeMCPFaultError,
    PendingCheckpointProjectionError,
)
from fin.host.knowledge_mcp import (
    knowledge_mcp_gate_closed,
    knowledge_mcp_state,
    knowledge_mcp_status_value,
    resolve_bound_retrieve_adapter_kind,
)
from fin.host.memory_dream import run_dream_after_turn
from fin.host.memory_port import build_memory_port
from fin.host.named_failures import (
    TASK_INPUT_INVALID,
    TASK_REPLY_EXPIRED,
    TASK_REPLY_UNKNOWN,
    TIER_UNAVAILABLE_CODE,
    RetrieveTier,
    unavailable_tier,
)
from fin.host.observability import ObservabilityCollector
from fin.host.operator_env import resolve_retrieve_adapter_kind
from fin.host.run_payload import (
    RunResponsePayload,
    _run_to_response_payload,
    citation_sections_from_evidence,
)
from fin.host.stream_map import (
    strip_thinking_from_answer,
)
from fin.host.grounded.route_intent import (
    normalize_route_intent,
    normalize_route_source,
)
from fin.host.run_control import RunAdmission, RunControl, StopToken
from fin.host.turn_receipts import TurnReceiptProjector, default_receipts
from fin.host.run_execution import (
    ResumeRunRequest,
    RunEvent,
    RunEventKind,
    RunExecutor,
    RunRequest,
    RunTerminal,
    # WHY re-exported: the classifier and the visual-claim projection are
    # execution facts other modules and tests read off this surface; they
    # live with the run that produces them, and their old spelling here
    # keeps every existing caller on one name.
    _STREAM_FAILURE_USER_COPY,
    _node_frame_events,
    _sanitize_reason_text,
    _visual_claims_wire_payload,
    classify_stream_exception,
    ensure_thread_occupancy,
)
from fin.host.state import reset_input_state
from fin.host.evidence.structured_evidence_drain_worker import (
    INVALIDATION_DRAIN_CONSUMER_ID,
    InvalidationDrainTransport,
    StructuredInvalidationDrainWorker,
)
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    INPUT_REPLY,
    INPUT_RESTORE,
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    PURPOSE_TASK_CLARIFICATION,
    AcceptedInput,
    PendingQuestion,
    ReconciliationProposal,
    TaskRuntimeError,
    TaskRuntimeState,
)
from fin.host.task.task_runtime import (
    accept_input as accept_task_input,
    archived_task_summaries,
    bounded_input_history,
    current_task_summary,
    decide as decide_task_runtime,
    is_reconciled,
    pending_question_payload,
    reconcile_marker,
)
from fin.host.thread_store import (
    PersistenceUnavailableError,
    RunRecord,
    ThreadNotFoundError,
    ThreadRecord,
    ThreadStore,
    project_stored_citation,
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
from fin.host.fin_home import resolve_fin_home
from fin.host.workspace_registry import workspace_root_for_fin_home
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.invalidation_port.errors import InvalidationError
from fin.retrieve.invalidation_port.ledger import InvalidationLedger
from fin.retrieve.push_batching import PushJournal
from fin.retrieve.push_port import PushError
from fin.retrieve.source_read import ATTACHMENT_KIND


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
    """Resolve the process-default 工作区 from the fin home.

    ``<FIN_HOME>/default`` is ``$HOME/.fin/default`` on a primary
    checkout with ``FIN_HOME`` unset. A linked worktree must not
    mkdir the operator's ``~/.fin``. Never ``Path.cwd()``.
    """
    return workspace_root_for_fin_home(resolve_fin_home())


def get_workspace_root() -> Path:
    """FastAPI dependency returning the resolved default 工作区 root.

    Production resolves the fin home per request (cheap; the mkdir
    is idempotent). Tests override via
    ``app.dependency_overrides[get_workspace_root]`` to inject a
    ``tmp_path``-rooted directory.
    """
    return default_workspace_root_for_process()



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







# Maximum characters of the first message used as the thread title.
# Spec: "max 50 chars + ellipsis on overflow."
_TITLE_MAX_CHARS: int = 50

# Default title when the caller creates a thread without a first
# message (the empty corpus / empty thread copy).
_DEFAULT_TITLE: str = "New thread"


# The scope label one attach carries; the byte cap is NOT here — it is
# the deployed :class:`~fin.host.attachment.attachment_budgets.AttachmentBudgets`
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
    # Question/task runtime T3: answer the thread's still-pending
    # clarification. ``question_id`` names WHICH question is being
    # answered, so a reply to a superseded question is refused as
    # ``task_reply_expired`` instead of answering whatever is current.
    # ``choice_id`` is the submitted choice — the reserved ``other`` token
    # is a real choice, not a sentinel — and ``free_text`` carries what
    # the user typed alongside it, preserved verbatim.
    question_id: str | None = None
    choice_id: str | None = None
    free_text: str | None = None
    # The run the caller believes it is answering. Optional: a client
    # that does not track run ids sends nothing and the door admits the
    # reply against the thread's paused run. When it IS named it must
    # be that run — a reply aimed at a turn that is no longer the one
    # in flight is refused rather than applied to whatever paused last.
    origin_run_id: str | None = None
    # Question/task runtime T4: an EXPLICIT restore of a listed archived
    # task. Restore is never inferred from an unrelated message, and the
    # archived results stay historical until the current recheck passes.
    restore_archive_id: str | None = None
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

    ``free_text`` rides the reserved ``other`` pick (QA-chain T9 /
    contract line 90). ``pickChoice`` reaches this route too, and the
    paused graph node accepts the reply from the resume value ALONE — so
    without the field here, an Other submitted over ``/resume`` would
    answer the literal word "other". A 检索档 pick names no question and
    carries no free text, so the field is inert on that lane. Omitted on
    every body-less resume, which stays a transport control that creates
    no user input (contract line 96).
    """

    pick: str | None = None
    free_text: str | None = None


class CreateWatchRequest(BaseModel):
    """T4 workflow: one watched issuer, optionally narrowed.

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
    """One citation on the workspace wire.

    Mirrors ``fin.host.http.CitationSchema`` field for field: exactly one
    source id is set (``document_id`` for a company row, ``attachment_id``
    for an attachment row whose ``filename`` is the safe display name),
    and ``display`` says how ``unit`` is presented.

    A ``database-row`` citation sets NEITHER document id: a ledger row
    has no filing behind it, and naming one would point the citation at a
    document the query never read. Its own id rides ``database_row``
    (``source`` plus ``record_key``), its ``version`` is its
    ``queried_at``, and the retained snapshot it opens is the same key.

    WHY no field default is allowed on the eight slots: stored rows span
    three writer generations in ``fin_run.citations`` JSONB, and permissive
    per-field defaults used to paper over a corrupt row (one naming both a
    ``page`` and a ``unit`` decoded as if nothing was wrong). A row is now
    read through :func:`fin.host.thread_store.project_stored_citation`, the
    frozen legacy-shape matcher: it fills only the defaults that are facts
    of that shape (absent ``kind`` -> the document kind while no lane
    marker rides the row, a ``page`` read as ``unit``, absent ``display``
    -> ``page``), and any row that matches no frozen shape is refused here.
    The history read surfaces such a row as a per-record
    ``TurnRow.rejected_citations`` entry, and the SSE wire names the
    same per-row outcome through :func:`_citations_wire_projection` —
    no silent drops on either surface.
    """

    kind: str
    document_id: str
    attachment_id: str
    version: str
    unit: int
    display: str
    filename: str
    quote: str
    # The ledger row this citation opens, or ``None`` for a document
    # kind. Optional and dumped with ``exclude_none`` so a company or
    # attachment row's wire dict keeps EXACTLY the keys it had before the
    # third kind existed — the snapshot is what a row needs, and asking
    # every stored filing citation to carry one would be a migration
    # nothing here can do.
    database_row: dict[str, Any] | None = None

    # ONE mechanism for the omit-null rule, shared by BOTH doors (the
    # same field-level seam ``fin.host.http.CitationSchema`` uses after
    # repair 016c342): the SSE dump, the turn-table dict, and — crucially
    # — FastAPI's ``response_model`` re-encode of ``ThreadDetailResponse``
    # all serialize through the model layer, so the key is dropped in
    # exactly one place. Without it the response model re-added
    # ``"database_row": null`` to every company / attachment row on
    # history GET while the stream omitted the key, and the web decoder
    # (spec §2: the sibling is present ONLY for a database-row) refused
    # a perfectly good citation after reload.
    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> Any:
        out = handler(self)
        if isinstance(out, dict) and out.get("database_row") is None:
            out.pop("database_row", None)
        return out

    @model_validator(mode="before")
    @classmethod
    def _match_frozen_stored_shape(cls, data: Any) -> Any:
        projected = project_stored_citation(data)
        if projected is None:
            raise ValueError(UNSUPPORTED_EVIDENCE_CONTRACT)
        return projected


class RejectedCitationPayload(BaseModel):
    """One stored citation the history read refused, named per record.

    ``citation_index`` is the row's position in the run's stored list.
    The refusal is visible and typed — the corrupt record can neither
    blank the turn nor vanish from it, and the rest of the history
    still serves.
    """

    citation_index: int
    error: str


class ToolSummaryPayload(BaseModel):
    """One human-readable tool-call row for the tools region.

    ``summary`` is the formatted one-liner (``"n filings"``,
    ``"query' -> 8 hits -> AAPL-2022Q3 u.4"``, ``"<doc> u.N"``);
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
    # Stored rows that match no frozen shape arrive here, named
    # per-record with their original index: a corrupt row is visible
    # and typed instead of blanking the turn (or being silently
    # dropped from it).
    rejected_citations: list[RejectedCitationPayload] = Field(
        default_factory=list
    )
    thinking_text: str = ""
    # T4 (spec line 31): total thinking duration in ms. Drives
    # the post-run one-line collapse label ("Thought for 4.2s");
    # a reload restores it from this row. 0 when no thinking.
    thinking_duration_ms: int = 0
    # T3 (spec line 32): one dict per tool call observed this run;
    # keys are exactly ``name`` / ``summary`` / ``tool_call_id``.
    tool_summaries: list[ToolSummaryPayload] = Field(default_factory=list)
    assistant_message: str = ""
    # A5: NULL is unknown. ``insufficient_evidence`` is the only token,
    # and a done turn that carries it is not a complete answer.
    semantic_outcome: str | None = None
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
    # T3 (enterprise-kb Focus 1 / SC2): the ``fin.comparison``
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
    # Trust (spec SC8): the locators this run retrieved or
    # read (``{document_id, version, unit}``), copied from the
    # run's evidence before the next run resets it. Wider than
    # ``citations``: a refused turn still shows what it looked at.
    evidence_locators: list[dict[str, Any]] = Field(default_factory=list)
    # T6: structured host analysis envelopes persisted for reload.
    analysis_results: list[dict[str, Any]] = Field(default_factory=list)
    # ADR 0089 / spec thread-attachment-source-read: the visual claims this
    # run read, one provenance row per observation (key set SSOT:
    # ``fin.host.evidence.visual_evidence.VISUAL_CLAIM_FIELDS``). It is a SEPARATE
    # channel from ``citations`` — a generated visual observation is never a
    # source quote — and travels the SSE ``visual_claims`` frame, the
    # persisted ``RunRecord``, and this row so the live wire, a reload, and
    # the non-streaming door all show the same link to the original page.
    # ``[]`` when the run produced none (never absent), mirroring
    # ``evidence_locators``.
    visual_claims: list[dict[str, Any]] = Field(default_factory=list)
    # EP6: this run's admitted structured snapshots, on their own field for
    # the same reason as ``visual_claims`` — a structured value grounds a
    # claim, it is never a quotation, so it must not ride ``citations``.
    # Stored as the frozen record (snapshot + digest + selector); EP7's
    # replay reads it back against the original snapshot (T30).
    structured_evidence: list[dict[str, Any]] = Field(default_factory=list)
    # iknow retrieve window for this run. ``{}`` when the run did not
    # retrieve. Keys are only the strings iknow returned.
    retrieval_window: dict[str, Any] = Field(default_factory=dict)
    # T8: the operator's latest thumb for this run (``"up"`` /
    # ``"down"``). ``None`` when no thumb was ever set — additive and
    # nullable, so pre-T8 clients that ignore the field keep working.
    # Side record keyed by ``run_id`` (``fin.host.feedback``): it is
    # never part of the run's citations or evidence, so setting or
    # flipping a thumb cannot move the verified answer (ADR 0035/0036).
    feedback: str | None = None


class RunCountsView(BaseModel):
    """How many runs this process is in each state right now.

    Three aggregates and no identity: no thread, no run id, no elapsed
    figure, nothing about one turn. That is what keeps the counts on a
    boot-scoped surface — the run-scoped facts each one summarizes stay on
    the SSE contract and on the thread's own receipt view.
    """

    active: int
    paused: int
    suspected_stuck: int


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

    knowledge_mcp: Literal["missing", "fault", "reachable", "not_probed"]
    retrieve_adapter_kind: Literal["mcp", "fake"]
    model_identity: str
    corpus_filing_count: int
    bound_collection_name: str
    run_counts: RunCountsView


class InvalidationCheckpointResponse(BaseModel):
    """One consumer's durable position in the invalidation stream (ADR 0097 §2.4).

    The two checkpoints stay separate fields because they are separate
    facts, and collapsing them on the wire is the silent-error this
    table exists to prevent: ``applied_checkpoint`` covers events whose
    local scrub COMMITTED and moves only with that commit, while
    ``acked_checkpoint`` is the cursor the backend ACCEPTED. The gap
    between them is precisely the "scrubbed but not yet acknowledged"
    window a crash redelivers from, so a reader that cannot tell them
    apart cannot tell a finished drain from a half-finished one.

    ``last_seq`` is the orderable half (ADR 0097 §2.4): the opaque
    checkpoint cannot be compared, so a skipped window is only decidable
    against the gapless per-consumer sequence. It moves with
    ``applied_checkpoint`` and NEVER with ``acked_checkpoint``, and is
    exposed beside both where a reader could plausibly pair it with the
    wrong one. Empty strings and zero mean "never drained", never a
    fabricated position — which is why they are rendered, not omitted,
    and not turned into a 404.
    """

    consumer_id: str
    acked_checkpoint: str
    applied_checkpoint: str
    last_seq: int


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


_PENDING_QUESTION_UNAVAILABLE_DETAIL = (
    "Pending question is temporarily unavailable. Retry opening the thread."
)


class TaskRuntimeView(BaseModel):
    """The thread's task state as readers see it (question/task runtime
    T4), kept SEPARATE from the turn table and from ``status``.

    ``current_task`` and ``archived_tasks`` are the task/turn split the
    spec requires: a turn can end with an answer while its task stays
    active, so neither is derivable from the other's row. ``None`` /
    ``[]`` means taskless, never "unknown".
    """

    current_task: dict[str, Any] | None = None
    archived_tasks: list[dict[str, Any]] = Field(default_factory=list)
    pending_question: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None
    task_outcome: str | None = None
    input_version: int = 0
    # The spec requires readers to be able to obtain bounded ORIGINAL
    # input history, not only the current task. Bounded at the reader so
    # asking for more cannot widen the window; an unrecoverable original
    # reads as ``payload: null`` rather than being reconstructed.
    input_history: list[dict[str, Any]] = Field(default_factory=list)


def _task_runtime_wire_projection(graph: Any, thread_id: str) -> dict[str, Any]:
    """Project the checkpointed task envelope onto the wire.

    Returns the EMPTY view when the graph exposes no ``task_runtime_state``
    seam: a stand-in or an unwired graph must not make a taskless thread
    look like a broken one, and an absent envelope is genuinely empty
    rather than unknown.
    """
    reader = getattr(graph, "task_runtime_state", None)
    if not callable(reader):
        return _empty_task_runtime_view()
    state = cast(TaskRuntimeState, reader(thread_id))
    return {
        "current_task": current_task_summary(state),
        "archived_tasks": archived_task_summaries(state),
        "pending_question": pending_question_payload(state),
        "reconciliation": reconcile_marker(state),
        "task_outcome": state.task_outcome,
        "input_version": state.input_version,
        "input_history": bounded_input_history(state),
    }


def _empty_task_runtime_view() -> dict[str, Any]:
    return {
        "current_task": None,
        "archived_tasks": [],
        "pending_question": None,
        "reconciliation": None,
        "task_outcome": None,
        "input_version": 0,
        "input_history": [],
    }


def _source_scope_wire_projection(
    graph: Any, thread_id: str
) -> dict[str, Any] | None:
    """The thread's accepted source-scope snapshot, or ``None``.

    The projection, not a second store: the value is read off the SAME
    checkpoint channel the resume commits it to. A thread with no accepted
    restriction — and a stand-in graph with no seam — both read as
    ``None``, which the wire contract distinguishes from an explicit
    ``mode="unrestricted"`` clear.

    A stored snapshot is user INTENT, never a current grant, so this is
    published as the saved selection only: Source Scope rechecks current
    permission, identity and version on every read path, and the reader
    must not present the saved value as a live authorization.
    """
    reader = getattr(graph, "source_scope", None)
    if not callable(reader):
        return None
    raw = reader(thread_id)
    return cast(dict[str, Any], raw) if isinstance(raw, Mapping) and raw else None


def _reply_answers_pending_choice(graph: Any, thread_id: str) -> bool:
    """Whether this send is the operator's pick answering a paused
    DOCUMENT CHOICE.

    Read off the checkpoint's pending interrupt, narrowed to the pinned
    ``document_choice`` kind: a ``task_clarification`` pause and a
    search-mode ``choice_window`` keep the pre-existing 409, because a
    reply to either of those is still refused at admission rather than
    treated as the pick. Only the document-choice interrupt releases the
    send door, and only for a send that NAMES a question.
    """
    probe = getattr(graph, "interrupt_pending", None)
    if not callable(probe):
        return False
    pending = probe(thread_id)
    return isinstance(pending, Mapping) and pending.get("kind") == (
        DOCUMENT_CHOICE_INTERRUPT_KIND
    )


def _accepted_input_kind(body: SendMessageRequest) -> str:
    """Which of the three accepted-input doors this send uses.

    Restore wins over reply because it names a concrete archive to adopt,
    and a reply only makes sense against a pending question. Both are
    EXPLICIT — neither is ever inferred from the message text, so an
    ordinary message can never be mistaken for a restore.
    """
    if body.restore_archive_id:
        return INPUT_RESTORE
    if body.question_id:
        return INPUT_REPLY
    return INPUT_MESSAGE


def _question_hint_is_ordinary(
    runtime_state: TaskRuntimeState, body: SendMessageRequest
) -> bool:
    """Whether a ``question_id`` names no answer this send can be.

    An ANSWER carries a choice or free text. A bare id — neither — can
    only claim which question the message belongs to, so when no question
    owns that id the message is an ordinary follow-up in the same thread,
    never a refused answer. A matching terminal clarification is NOT this
    case: that reply must consume the question and open the next turn.

    ``choice_id`` and non-blank ``free_text`` are answer values. A
    free-text-only clarification reply travels as ``free_text`` with no
    ``choice_id``, so a stale or duplicate delivery of it must keep the
    reply vocabulary — the typed refusal or the recorded retry replay —
    instead of being admitted as a second new turn.

    A question id that already has an accepted reply is an answer attempt
    even with no answer value on this delivery: it can only be a repeat
    or a conflicting answer, and both are decided in the reply path.
    """
    question_id = (body.question_id or "").strip()
    if not question_id:
        return False
    question = runtime_state.pending_question
    if question is not None and question.question_id == question_id:
        return False
    if (body.choice_id or "").strip() or (body.free_text or "").strip():
        return False
    return not any(
        row.kind == INPUT_REPLY and row.question_id == question_id
        for row in runtime_state.input_history
    )


def _pending_terminal_clarification(
    runtime_state: TaskRuntimeState, body: SendMessageRequest
) -> PendingQuestion | None:
    """The task clarification this send answers, if it names one.

    The terminal clarification publishes its question on the thread and
    ends its turn, so there is no paused run to continue: the reply is a
    NEW turn that must consume this question. A mismatched id is not this
    path — ``_plan_reply`` already judges reply admission in the runtime
    vocabulary, and its refusal stays authoritative.
    """
    question_id = (body.question_id or "").strip()
    if not question_id:
        return None
    question = runtime_state.pending_question
    if question is None or question.purpose != PURPOSE_TASK_CLARIFICATION:
        return None
    if question.question_id != question_id:
        return None
    return question


def _clarify_reply_turn_question(
    question: PendingQuestion,
    accepted: AcceptedInput,
    runtime_state: TaskRuntimeState,
) -> str:
    """The new turn's user message: the original question plus the answer.

    The follow-up that opened the task ("ZEUS 的年假政策是什么") is the
    context this reply continues; the reply alone ("AAPL") names no
    subject. The original comes from the committed task goal, never from
    prose the host invents, and the operator's own answer is preserved
    verbatim beside it.

    This is the ONE value the reply turn carries: the same string is the
    run's input message, the persisted ``RunRecord.question`` and the GET
    history row, so a live optimistic row and a reload cannot disagree
    about what was asked. The web mirror in ``store.ts`` reuses the same
    shape and fallback because it renders before any host frame arrives.
    """
    original = (
        runtime_state.current_task.goal
        if runtime_state.current_task is not None
        else question.prompt
    )
    answer = (accepted.payload or accepted.choice_id or "").strip()
    return f"原问题：{original}\n补充回答：{answer}"


def _accepted_payload(body: SendMessageRequest) -> str | None:
    """The original text one accepted input stores, or ``None``.

    A reply can be a CLICK: the choice is the answer and no prose was
    authored. Storing ``""`` for it would refuse the input in Task
    Runtime ("an accepted input must carry its original payload"), so a
    blank message is the absent original — exactly the shape a document
    choice click already stores. Free text and an ordinary message keep
    their bytes.
    """
    text = body.question or body.free_text
    return text if isinstance(text, str) and text.strip() else None


def _accept_at_send_door(
    runtime_state: TaskRuntimeState,
    body: SendMessageRequest,
    *,
    kind: str,
    run_id: str,
    graph: Any = None,
    thread_id: str = "",
) -> tuple[TaskRuntimeState, AcceptedInput]:
    """Accept this send as the input kind it names, or refuse it typed.

    A refusal is a typed 409/400 BEFORE any run starts, not a silent
    drop: an expired reply must not look like a message that was merely
    ignored. The envelope is left exactly as it was, so the last
    consistent task state stays authoritative.

    A document reply passes :func:`_precheck_document_reply` first, so
    the refusal happens before :func:`accept_task_input` — nothing is
    written and no run starts. A duplicate pick returns the FIRST
    accepted record, which is how ``input_version`` survives a double
    click.
    """
    duplicate = _precheck_document_reply(
        graph, thread_id, runtime_state, body, kind=kind
    )
    if duplicate is not None:
        return runtime_state, duplicate
    try:
        return accept_task_input(
            runtime_state,
            kind=kind,
            payload=_accepted_payload(body),
            provenance="workspace_send",
            # Only a reply ANSWERS a question. An ordinary message that
            # carried a stale association hint is not recorded as having
            # answered it.
            question_id=body.question_id if kind == INPUT_REPLY else None,
            choice_id=body.choice_id if kind == INPUT_REPLY else None,
            archive_id=body.restore_archive_id,
            run_id=run_id,
        )
    except TaskRuntimeError as exc:
        raise _task_input_http_error(exc) from exc


def _checkpoint_origin_run_id(graph: Any, thread_id: str) -> str | None:
    """The canonical run id of the checkpoint a reply would land on.

    Read through the same ``checkpoint_run_id`` seam the resume route
    uses, because a send and a resume address the SAME paused run: a
    freshly minted send run id would name no question's origin and every
    legitimate reply would look foreign. ``None`` when the seam is absent
    or the checkpoint carries none, which is a thread with no run identity
    to judge a reply against.
    """
    reader = getattr(graph, "checkpoint_run_id", None)
    if not callable(reader):
        return None
    value = reader(thread_id)
    return value if isinstance(value, str) and value else None


def _accepted_document_reply(
    runtime: TaskRuntimeState, *, question_id: str, choice_id: str
) -> AcceptedInput | None:
    """The FIRST accepted record for this exact document pick, if any.

    Idempotency read, delegated to the public admission seam so the send
    door and the resume node cannot disagree about what "already
    accepted" means. The COMPOSITE key is what is matched, never the bare
    candidate id: an ordinary ``task_clarification`` reply stores the bare
    id on the accept path this work does not own, and short-circuiting
    that one would change a door the pre-check was never asked about.

    Returning the stored record — rather than refusing — is what keeps a
    double click from advancing ``input_version`` twice: the record names
    the input version the FIRST pick already reconciled, so the runtime
    node recognizes it as reconciled and no-ops.
    """
    return accepted_document_reply(
        runtime, question_id=question_id, choice_id=choice_id
    )


def _precheck_document_reply(
    graph: Any,
    thread_id: str,
    runtime_state: TaskRuntimeState,
    body: SendMessageRequest,
    *,
    kind: str,
) -> AcceptedInput | None:
    """Refuse a document reply that cannot possibly be valid — EARLY.

    The QUESTION capability's ``document_choice_interrupt_node`` owns the decision and
    keeps its validate-then-reopen loop: this is the backstop and the
    only path that can reopen a question. What it cannot do is refuse an
    invalid reply before the send door has already ACCEPTED it, because
    acceptance is what advances ``input_version``, and an accepted input
    is exactly the write the contract forbids for an invalid, foreign or
    expired reply.

    So the checks here are the same primitives the node consults, asked
    one layer earlier and nothing more:

    * :func:`accepted_document_reply` — a pick already in accepted
      history returns its first outcome instead of a second input;
    * the question id this reply NAMES must still be the pending one,
      which is the ownership half of ``_decide_reply``'s reconciliation
      check;
    * :func:`resolved_document_choice_pick` — the node's own membership
      and originating-run validation, run against the checkpoint's run id
      read through the ``checkpoint_run_id`` seam rather than a run id
      minted here.

    Everything else is deliberately out of scope. A send with no question
    named is an ordinary message; a thread with no pending question, a
    thread whose pending question is a ``task_clarification`` (answered
    through the ordinary reply path), and a graph with no run-identity
    seam all pass through untouched — a pre-check that guessed on those
    would be the second decision authority this is not allowed to be.
    """
    if kind != INPUT_REPLY:
        return None
    question_id = (body.question_id or "").strip()
    choice_id = (body.choice_id or "").strip()
    if not question_id or not choice_id:
        return None

    duplicate = _accepted_document_reply(
        runtime_state, question_id=question_id, choice_id=choice_id
    )
    if duplicate is not None:
        return duplicate

    question = runtime_state.pending_question
    if question is None or question.purpose != PURPOSE_DOCUMENT_CHOICE:
        return None
    if question.question_id != question_id:
        raise _task_input_http_error(
            TaskRuntimeError(
                TASK_REPLY_EXPIRED,
                "the document choice this reply names is no longer pending",
                question_id=question_id,
                pending_question_id=question.question_id,
            )
        )

    origin_run_id = _checkpoint_origin_run_id(graph, thread_id)
    if origin_run_id is None:
        return None
    if resolved_document_choice_pick(
        runtime_state,
        origin_run_id=origin_run_id,
        raw_pick=choice_id,
    ) is None:
        raise _task_input_http_error(
            TaskRuntimeError(
                TASK_INPUT_INVALID,
                "the pick names no candidate of this thread's pending "
                "document choice",
                question_id=question_id,
                choice_id=choice_id,
            )
        )
    return None


def _task_input_http_error(exc: TaskRuntimeError) -> HTTPException:
    """The typed refusal as an HTTP status the SPA can branch on.

    ``400`` for a malformed or stale input (the caller sent something
    this thread cannot accept) and ``409`` for a state conflict (a reply
    naming no pending question). The named failure rides the body so a
    client distinguishes them without parsing prose.
    """
    conflict = exc.kind in (TASK_REPLY_UNKNOWN, TASK_REPLY_EXPIRED)
    return HTTPException(
        status_code=409 if conflict else 400,
        detail={"error": exc.kind},
    )


def _reply_refusal(
    runtime_state: TaskRuntimeState,
    question_id: str,
) -> TaskRuntimeError | None:
    """Whether the question this reply names can still be answered, in
    the SAME vocabulary the reconciliation boundary decides in.

    Nothing pending at all is ``task_reply_unknown`` — the reply names a
    clarification this thread never had. A question that is no longer
    the pending one, or one whose task is no longer current, is
    ``task_reply_expired``: the reply is real, but the question it
    answers has been superseded or cancelled. Collapsing the two would
    tell an operator to check for a pending question that does not
    exist, and tell a surface that a live question was replaced when
    none ever was.
    """
    question = runtime_state.pending_question
    if question is None:
        return TaskRuntimeError(
            TASK_REPLY_UNKNOWN,
            "no clarification is pending on this thread",
            question_id=question_id,
        )
    if question.question_id != question_id:
        return TaskRuntimeError(
            TASK_REPLY_EXPIRED,
            "the clarification this reply names is no longer pending",
            question_id=question_id,
            pending_question_id=question.question_id,
        )
    if question.task_id and runtime_state.current_task is None:
        return TaskRuntimeError(
            TASK_REPLY_EXPIRED,
            "the task this clarification belongs to is no longer current",
            question_id=question.question_id,
        )
    return None


def _accepted_task_runtime_state(graph: Any, thread_id: str) -> TaskRuntimeState:
    """Read the committed task envelope to accept the next input against.

    Falls back to the EMPTY envelope when the graph exposes no
    ``task_runtime_state`` seam, so a stand-in accepts input 1 rather
    than refusing every send on an unwired graph.
    """
    reader = getattr(graph, "task_runtime_state", None)
    if not callable(reader):
        return TaskRuntimeState()
    return cast(TaskRuntimeState, reader(thread_id))


@dataclass(frozen=True)
class _ReplyPlan:
    """What a clarification reply is, decided before any run starts.

    ``retry`` is set when the (question_id, choice_id, free_text) tuple
    is already in accepted history — the reply is a repeat delivery and
    must not become a second accepted input. ``continued`` is set when
    the thread still holds a paused run the reply answers; that run's
    own identity is reused, so the reply cannot split one turn into
    two. Both unset means the thread has no run to continue, and the
    reply keeps the ordinary send door's existing behaviour.
    """

    retry: AcceptedInput | None = None
    continued: "_ContinuedReply | None" = None


@dataclass(frozen=True)
class _ContinuedReply:
    """A paused run, continued under the identity it already has.

    ``accepted_input_version`` is the version Task Runtime's acceptance
    actually reached, carried out of the planning section so the turn's
    receipt records the authoritative one rather than counting its own.
    """

    run_id: str
    resume_input: Any
    resume_config: dict[str, Any]
    accepted_input_version: int
    #: QA-chain T9: a grounded document reply is accepted by the
    #: ``document_choice_interrupt_node``, not by this door, so this
    #: section has no acceptance of its own to project onto the receipt.
    #: Counting the version it read would record an input it never
    #: accepted and make a duplicate retry look like a second one.
    accepted_by_this_door: bool = True


def _reply_pick(body: SendMessageRequest) -> str:
    """The value a continued run is handed as its resume value.

    ``Command(resume=None)`` trips an ``UnboundLocalError`` in this
    langgraph version, so a continuation always carries a value. The
    operator's own submitted text is it: for a clarification that IS
    the answer, and for any other paused node the value is ignored by
    the node that receives it.
    """
    return body.free_text or body.choice_id or ""


def _pending_document_choice(
    runtime_state: TaskRuntimeState, body: SendMessageRequest
) -> bool:
    """Does this reply answer a pending GROUNDED DOCUMENT choice?

    Checked against the committed envelope rather than only the live
    interrupt, because by the time an identical retry arrives the pause
    is already resolved: a reply NAMING a document question is that kind
    even when nothing is pending any more. A reply with no ``choice_id``
    is an ordinary message and never this kind.
    """
    if not (body.question_id or "").strip() or not (
        body.choice_id or ""
    ).strip():
        return False
    question = runtime_state.pending_question
    if question is not None and question.purpose == PURPOSE_DOCUMENT_CHOICE:
        return True
    return _accepted_document_reply(
        runtime_state,
        question_id=(body.question_id or "").strip(),
        choice_id=(body.choice_id or "").strip(),
    ) is not None


def _plan_document_reply(
    graph: Any,
    thread_id: str,
    runtime_state: TaskRuntimeState,
    body: SendMessageRequest,
    workspace_root: Path,
) -> _ReplyPlan:
    """Classify a grounded document reply against the document seam.

    QA-chain T9. Two facts separate this from the clarification path
    above, and both are why the decision lives here rather than being
    folded into it:

    * the ``document_choice_interrupt_node`` is the ONE accept/reconcile
      authority for this kind — on ``Command(resume=pick)`` it calls
      ``accept_input`` with the COMPOSITE ``{question_id}/{choice_id}``
      key, commits the checked scope and re-retrieves. Accepting here as
      well would advance ``input_version`` a SECOND time and store the
      BARE ``choice_id`` the node's duplicate guard does not match, so a
      click would stop storing ``payload=null`` under its composite key
      and an identical retry would stop being idempotent;
    * the node reads the pick from the resume value ALONE, so the value
      must be the structured shape it expects — a bare ``choice_id``
      would discard the operator's rephrase and answer the literal word
      "other" (contract line 90).

    Refusals still happen EARLY, through
    :func:`_precheck_document_reply`, before any wire is opened.
    """
    _precheck_document_reply(
        graph, thread_id, runtime_state, body, kind=INPUT_REPLY
    )
    run_id = (
        graph.checkpoint_run_id(thread_id)
        if hasattr(graph, "checkpoint_run_id")
        else None
    )
    if not run_id:
        # No run identity to judge this reply against; fall through to
        # the ordinary send door rather than continuing a run that
        # cannot be identified.
        return _ReplyPlan()
    return _ReplyPlan(
        continued=_ContinuedReply(
            run_id=str(run_id),
            resume_input=Command(
                resume=document_choice_resume_value(
                    (body.choice_id or "").strip(),
                    body.free_text or body.question,
                )
            ),
            resume_config={
                "configurable": {
                    "thread_id": thread_id,
                    "run_id": str(run_id),
                    "workspace_root": str(workspace_root),
                }
            },
            # The node owns this acceptance, so this door holds no
            # authoritative version of its own: the receipt records the
            # one Task Runtime actually reached.
            accepted_input_version=runtime_state.input_version,
            accepted_by_this_door=False,
        )
    )


def _plan_reply(
    graph: Any,
    thread_id: str,
    runtime_state: TaskRuntimeState,
    body: SendMessageRequest,
    *,
    workspace_root: Path,
) -> _ReplyPlan:
    """Classify a clarification reply against the committed envelope.

    Every check here runs inside the door's admission section, so a
    refusal is a plain 4xx and a continuation is admitted before any
    other caller can read the same state.

    The retry key is the tuple the first delivery recorded: question,
    choice, and the payload the door accepted. An exact repeat is
    idempotent; a DIFFERENT reply to a question that already has an
    accepted answer is a conflict; a choice that is not on the current
    question is not a candidate answer to anything.
    """
    # QA-chain T9: a grounded document reply is classified by the
    # DOCUMENT seam, not by the clarification keys below. The node accepts
    # it under the COMPOSITE ``{question_id}/{choice_id}`` key, so the
    # bare-``choice_id`` ``prior`` lookup never matches it and the
    # "already has an accepted answer" guard below would refuse every
    # legitimate pick as a second answer. Decide the document kind first,
    # then let the clarification rules handle only their own.
    document_reply = _reply_answers_pending_choice(graph, thread_id)
    if document_reply or _pending_document_choice(
        runtime_state, body
    ):
        return _plan_document_reply(
            graph, thread_id, runtime_state, body, workspace_root
        )

    if _question_hint_is_ordinary(runtime_state, body):
        # No answer value traveled with the id, and no pending
        # clarification owns it: this is a follow-up, not a stale reply.
        return _ReplyPlan()

    payload = _accepted_payload(body)
    prior = next(
        (
            row
            for row in runtime_state.input_history
            if row.kind == INPUT_REPLY
            and row.question_id == body.question_id
            and row.choice_id == body.choice_id
            and row.payload == payload
        ),
        None,
    )
    if prior is not None and is_reconciled(runtime_state, prior.input_version):
        # The repeat of a reply that already has its outcome. Checked
        # FIRST, before pending-ness: the question a reply answers is
        # cleared by answering it, so a settled retry never matches a
        # pending question again.
        return _ReplyPlan(retry=prior)

    if prior is None and any(
        row.kind == INPUT_REPLY and row.question_id == body.question_id
        for row in runtime_state.input_history
    ):
        # An accepted answer already exists for this question and this
        # delivery is not it, so the reply would be a second answer to a
        # question that has one. Checked before pending-ness: answering
        # CLEARS the question, so the conflict outlives it.
        raise _task_input_http_error(
            TaskRuntimeError(
                TASK_INPUT_INVALID,
                "this clarification already has an accepted answer",
                question_id=body.question_id,
            )
        )
    refusal = _reply_refusal(runtime_state, str(body.question_id))
    if refusal is not None:
        raise _task_input_http_error(refusal)
    question = runtime_state.pending_question
    if question is not None and question.question_id == str(body.question_id).strip():
        # The association is right but there is no answer value: a listed
        # choice, the Other token's free text, or the message text the
        # free-text-only card sends under its association. Accepting an
        # empty reply would open a turn that answers nothing and show an
        # empty "补充回答" line, so it is refused while the question stays
        # pending and answerable.
        if not (body.choice_id or body.free_text or body.question or "").strip():
            raise _task_input_http_error(
                TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    "a clarification reply must carry a choice or free text",
                    question_id=body.question_id,
                )
            )
    if question is not None and body.choice_id is not None:
        choice = body.choice_id.strip()
        # The reserved Other token is always an offered option — it is the
        # trailing token the wire adds, never a stored listed choice — and
        # it is answerable only with the operator's own text.
        if choice == OTHER_CHOICE_ID:
            if not (body.free_text or body.question or "").strip():
                raise _task_input_http_error(
                    TaskRuntimeError(
                        TASK_INPUT_INVALID,
                        "the Other answer must carry its free text",
                        question_id=body.question_id,
                        choice_id=body.choice_id,
                    )
                )
        elif choice not in question.choices:
            raise _task_input_http_error(
                TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    "the chosen answer is not one of the offered choices",
                    question_id=body.question_id,
                    choice_id=body.choice_id,
                )
            )

    paused_run_id = (
        graph.checkpoint_run_id(thread_id)
        if hasattr(graph, "checkpoint_run_id")
        else None
    )
    if body.origin_run_id and body.origin_run_id != paused_run_id:
        # The caller named the run it believes it is answering. Naming
        # one that is not the thread's paused run means the client is
        # answering a turn that is no longer the one in flight.
        raise _task_input_http_error(
            TaskRuntimeError(
                TASK_INPUT_INVALID,
                "the named origin run is not this thread's paused run",
                origin_run_id=body.origin_run_id,
                paused_run_id=paused_run_id,
            )
        )

    recoverable = bool(
        paused_run_id
        and hasattr(graph, "pending_nodes")
        and graph.pending_nodes(thread_id)
    )
    if not recoverable:
        if prior is not None:
            # Its input was accepted but nothing decided it and there is
            # no paused run left to decide it in. Accepting the delivery
            # again would be a second input for the same answer.
            raise _task_input_http_error(
                TaskRuntimeError(
                    TASK_REPLY_EXPIRED,
                    "this reply was accepted but its run is no longer resumable",
                    question_id=body.question_id,
                )
            )
        # A pending question that still MATCHES is this fallthrough: the
        # question survives but the run that paused on it did not, so its
        # clarification is answered by whatever run the delivery starts.
        # Nothing is pending and nothing can be continued, though, and
        # that case already refused above — falling through to the
        # ordinary send door admitted a stale draft as a SECOND reply
        # under a brand-new run id, so it became a turn, advanced
        # ``input_version``, and only reached the reconciliation boundary
        # — which refuses mid-run, long after the operator saw a 200
        # stream.
        return _ReplyPlan()

    run_id = str(paused_run_id)
    if prior is None:
        runtime_state, accepted = _accept_at_send_door(
            runtime_state,
            body,
            kind=INPUT_REPLY,
            run_id=run_id,
            graph=graph,
            thread_id=thread_id,
        )
        decided = _decide_continued_reply(runtime_state, accepted)
    else:
        # An exact retry whose reconciliation never landed: continue the
        # run with the acceptance already recorded, never a second one.
        accepted = prior
        decided = None
    resume_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "run_id": run_id,
            "workspace_root": str(workspace_root),
            "accepted_task_input": accepted.to_state_dict(),
        }
    }
    # The reconciled envelope rides the SAME ``Command`` that continues
    # the run, so the accepted input and its decision become durable in
    # one checkpoint write and the reconcile node downstream finds the
    # version already reconciled. An acceptance that is still waiting for
    # its decision carries no envelope: the node that owes the decision
    # is the one that will make it.
    resume_input: Any = Command(resume=_reply_pick(body))
    if decided is not None:
        resume_input = Command(
            resume=_reply_pick(body),
            update={"task_runtime": decided.to_state_dict()},
        )
    return _ReplyPlan(
        continued=_ContinuedReply(
            run_id=run_id,
            resume_input=resume_input,
            resume_config=resume_config,
            accepted_input_version=accepted.input_version,
        )
    )


def _decide_continued_reply(
    runtime_state: TaskRuntimeState, accepted: AcceptedInput
) -> TaskRuntimeState:
    """Commit this reply's decision before the continued run starts.

    Same pure decision the reconcile node would make, applied one step
    earlier: a paused run resumes at whatever node it stopped on, which
    is usually past ``reconcile_task``, so leaving the node to decide
    would leave the accepted input permanently unreconciled.
    """
    try:
        return decide_task_runtime(
            runtime_state,
            accepted,
            ReconciliationProposal(
                kind=INPUT_REPLY, reason="clarification reply"
            ),
        )
    except TaskRuntimeError as exc:
        raise _task_input_http_error(exc) from exc


def _retry_frames(
    store: ThreadStore, thread_id: str, prior: AcceptedInput
) -> Iterator[str]:
    """The terminal frames of the run a repeated reply already produced.

    A repeat delivery is a no-op for everything durable — no second
    accepted input, no second run, no second turn row — so the only
    honest thing left to send is the outcome the first delivery
    already published, read back off the row that published it. A run
    that never wrote a row (stopped, or a process that died before
    finalizing) has no outcome to return, so the retry says so with the
    typed token instead of inventing one.

    The replay is the turn's OUTCOME, not a byte-for-byte repeat of the
    live wire: ``fin_run`` is the durable record of what the turn
    produced, and a live channel it never persisted cannot be recovered
    from it. The visual-claim lane and the semantic verdict ARE persisted
    columns, so they ride the replay too — a duplicate delivery that
    dropped them would show a turn with no claims and no
    ``insufficient_evidence`` verdict while ``GET`` history showed both.
    """
    row = next(
        (run for run in store.list_runs(thread_id) if run.id == prior.run_id),
        None,
    )
    if row is None:
        payload = _run_to_response_payload(
            {"status": "refuse", "status_reason": TASK_REPLY_EXPIRED}
        )
    else:
        payload = _run_to_response_payload(
            {
                "status": row.status,
                "answer": row.answer,
                "status_reason": row.status_reason,
                "citations": row.citations,
                "thinking_text": row.thinking_text,
                "tool_summaries": row.tool_summaries,
                "track_decision": dict(row.track_decision or {}),
                "comparisons": list(row.comparisons or []),
                "analysis_results": list(row.analysis_results or []),
                "evidence_locators": list(row.evidence_locators or []),
                "retrieval_window": dict(row.retrieval_window or {}),
            }
        )
    # Re-normalised from the persisted rows, not lifted verbatim: the row
    # and the frame are the SAME rows through the SAME projector, so a
    # duplicate delivery cannot mint a second shape for them.
    claim_rows, claim_refusals = (
        _visual_claims_wire_payload(row.visual_claims)
        if row is not None
        else ([], [])
    )
    return _terminal_sse_frames(
        run_id=prior.run_id or "",
        payload=payload,
        thinking_ms=row.thinking_duration_ms if row is not None else None,
        claim_rows=claim_rows,
        visual_refusals=claim_refusals,
        semantic_outcome=row.semantic_outcome if row is not None else None,
    )


class PendingTurnResponse(BaseModel):
    run_id: str
    question: str
    choice_window: dict[str, Any]


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
    # T4 fix (code-review H1): the graph projection includes a checkpoint
    # with pending nodes only when the thread lock is not held.
    pending_checkpoint: bool = False
    # T2f: WHAT a paused run waits for while ``pending_checkpoint`` is
    # true — ``"attachment_preparation"`` (durable prep job owns the
    # resume) or ``"choice_window"`` (the operator's pick owns it);
    # ``None`` for a plain crash checkpoint. The route computes it into
    # the payload dict, so the response model must declare it or
    # FastAPI filters the key off the wire.
    pending_interrupt_kind: str | None = None
    # A choice-window turn is not a completed RunRecord. Carry its exact
    # checkpoint identity, original question, and choices so a reload can
    # restore the paused prompt without writing synthetic history.
    pending_turn: PendingTurnResponse | None = None
    # Spec 2.2 rule 2 (E5): the history envelope declares its citation
    # channel once, so a client decodes the turns' citations against a
    # named version rather than a guess. The token names the shape; the
    # version is its explicit integer sibling. Both are frozen
    # constants; undeclared here, FastAPI would filter them off the wire.
    # WHY the field is named ``schema_version`` and NOT
    # ``evidence_schema_version``: the WEB KEY IS THE CONTRACT, and one
    # key name per fact is the whole point of the split (ADR 0093
    # decision 2). Every other carrier of this same pair — the SSE status
    # frame (:1685), the terminal frames, and every JSON Schema
    # ``required`` list in the evidence home — spells the version
    # ``schema_version`` beside whatever ``schema``/``evidence_schema``
    # names the shape, so a differently-spelled field here would make the
    # history envelope the one carrier the web decoder cannot read, and
    # the version check would never fire on ``GET /api/threads/{id}``.
    evidence_schema: str = CITATION_CHANNEL_SCHEMA
    schema_version: int = EVIDENCE_SCHEMA_VERSION
    # T3 (ADR 0044): token occupancy from the SAME figure the
    # compaction gate computes (``graph.occupancy(thread_id)`` ->
    # ``OccupancyCompactor.occupancy`` over the checkpointed
    # messages vs the session budget). ``None`` for a thread with
    # no checkpoint (never sent on) — the UI stays neutral rather
    # than showing a fabricated 0%. This is the thread-scoped
    # surface ONLY: ADR 0033 keeps ``GET /api/status`` free of any
    # occupancy / context-window figure.
    occupancy: Occupancy | None = None
    # Question/task runtime T4: the thread-scoped task envelope. Declared
    # here (not only in the route dict) because FastAPI filters any key the
    # response model does not declare — the same reason ``pending_turn`` and
    # ``pending_interrupt_kind`` are declared.
    task_runtime: TaskRuntimeView = Field(default_factory=TaskRuntimeView)
    # QA-chain T9: the thread's ACCEPTED source-scope snapshot, exactly as
    # ``fin.host.source_scope`` freezes it
    # (``{schema_version, mode, sources[], accepted_input_version}``).
    #
    # ``None`` means no accepted restriction exists yet — the
    # whole-corpus default — and is NOT the same as an unrestricted
    # snapshot, which is an explicit accepted clear. Declared here because
    # FastAPI filters any key the response model does not declare.
    source_scope: dict[str, Any] | None = None
    turns: list[TurnRow]


def _legacy_pending_projection(graph: Any, thread_id: str) -> dict[str, Any]:
    """Pre-projection pending probes, for graph stand-ins without the seam.

    The single-snapshot ``pending_checkpoint_projection`` seam is a
    compiled-graph contract; test doubles and older wiring expose only the
    granular ``pending_nodes`` / ``interrupt_pending`` probes. Reading those
    keeps the Resume banner working for a stand-in instead of silently
    reporting nothing pending. The pending-turn provenance
    (``run_id`` / ``question`` / ``choices``) is available only through the
    projection seam, so a stand-in degrades to ``pending_turn=None``.
    """
    pending_checkpoint = bool(
        hasattr(graph, "pending_nodes") and graph.pending_nodes(thread_id)
    )
    pending_interrupt_kind: str | None = None
    if pending_checkpoint and hasattr(graph, "interrupt_pending"):
        payload = graph.interrupt_pending(thread_id)
        if isinstance(payload, dict):
            raw_kind = payload.get("kind")
            if isinstance(raw_kind, str) and raw_kind:
                pending_interrupt_kind = raw_kind
    return {
        "pending_checkpoint": pending_checkpoint,
        "pending_interrupt_kind": pending_interrupt_kind,
        "pending_turn": None,
    }


def _pending_checkpoint_wire_projection(
    graph: Any, thread_id: str, *, is_locked: bool
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "pending_checkpoint": False,
        "pending_interrupt_kind": None,
        "pending_turn": None,
    }
    if is_locked:
        return projection
    reader = getattr(graph, "pending_checkpoint_projection", None)
    if not callable(reader):
        # No projection seam: a graph stand-in (test double / older wiring).
        # Fall back to the granular probes so the Resume signal survives,
        # mirroring the ``is_locked`` / ``occupancy`` ``hasattr`` tolerance.
        return _legacy_pending_projection(graph, thread_id)
    try:
        return cast(dict[str, Any], reader(thread_id))
    except PendingCheckpointProjectionError as exc:
        # The typed cause (WHY the checkpoint could not be projected) is
        # logged with its traceback for the operator; the client still gets
        # only the fixed detail, so no checkpoint / exception text leaks.
        logger.warning(
            "get_thread: pending question projection unavailable",
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail=_PENDING_QUESTION_UNAVAILABLE_DETAIL,
        ) from exc


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


def _structured_evidence_wire_payload(rows: Any) -> list[dict[str, Any]]:
    """Project stored snapshots for the turn row, withholding expired ones.

    The answer stays in history after expiry; the citation is marked
    unavailable (spec§"Retention contract"). The deadline is fin's own and
    is read off the frozen snapshot, so it needs no backend outcome --
    MP15 must never become an input to it (docs/decisions/p2-d2).
    """
    from fin.host.evidence.evidence_contract import EVIDENCE_EXPIRED

    now = datetime.now(timezone.utc)
    payload: list[dict[str, Any]] = []
    for row in rows or []:
        expires_at = ((row.get("snapshot") or {}) if isinstance(row, dict) else {}).get(
            "expires_at"
        )
        try:
            expired = expires_at is not None and datetime.fromisoformat(
                str(expires_at).replace("Z", "+00:00")
            ) <= now
        except ValueError:
            # An unparseable deadline is not a licence to serve the payload.
            expired = True
        if expired:
            payload.append(
                {
                    "snapshot": {},
                    "digest": row.get("digest"),
                    "selector": row.get("selector") or {},
                    "error": EVIDENCE_EXPIRED,
                }
            )
            continue
        payload.append(copy.deepcopy(row))
    return payload


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
    # WHY the accepted/rejected split happens here, and WHY every
    # refusal is named by the decision the row actually failed (review
    # item: one authority): the history read re-uses the SAME two
    # authorities the write and wire boundaries use — the frozen v0
    # projection (shape mismatch = ``unsupported``) and the validator
    # home (content/serialization = its exact §6 code) — so a reload
    # and a live stream can never report two different codes for one
    # corrupt row and the web's ``(index, code)`` refusal fold dedups
    # them into one visible outcome.
    accepted_citations: list[dict[str, Any]] = []
    rejected_citations: list[dict[str, Any]] = []
    for index, row in enumerate(run.citations):
        projected = project_stored_citation(row)
        if projected is None:
            logger.warning(
                "turn table: refused stored citation row %d of run %s "
                "(unsupported_evidence_contract: matches no frozen shape)",
                index,
                run.id,
            )
            rejected_citations.append(
                {"citation_index": index, "error": UNSUPPORTED_EVIDENCE_CONTRACT}
            )
            continue
        try:
            dumped = CitationPayload.model_validate(projected).model_dump(
                exclude_none=True
            )
        except ValidationError:
            # The projection passed but the payload cannot be built: the
            # same serialization-failure class the SSE wire names
            # ``malformed``, never a second vocabulary.
            logger.warning(
                "turn table: refused stored citation row %d of run %s "
                "(malformed_evidence_contract on serialization)",
                index,
                run.id,
            )
            rejected_citations.append(
                {"citation_index": index, "error": MALFORMED_EVIDENCE_CONTRACT}
            )
            continue
        if dumped.get("kind") == DATABASE_ROW_KIND:
            # Replay applies the v0 KEY-SET projection only; a
            # lane-marked v1 row's CONTENT (bad snapshot timestamp,
            # mirror mismatch, §3 overflow) stays unverified without
            # this re-run. The wire candidate mints the frozen snapshot
            # token the same way the live frame does, so a pre-E5
            # stored snapshot replays instead of being refused for a
            # token its generation never owed.
            candidate = _citation_row_candidate(row)
            outcome = validate_evidence(
                {
                    "evidence_schema": CITATION_CHANNEL_SCHEMA,
                    EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
                    "citations": [candidate],
                },
                _wire_admission_context([candidate]),
            )
            if not outcome.accepted and outcome.refusal is not None:
                logger.warning(
                    "turn table: refused stored ledger citation row %d "
                    "of run %s (%s)",
                    index,
                    run.id,
                    outcome.refusal.code,
                )
                rejected_citations.append(
                    {"citation_index": index, "error": outcome.refusal.code}
                )
                continue
        accepted_citations.append(dumped)
    return {
        "run_id": run.id,
        "question": run.question,
        "status": run.status,
        "status_reason": run.status_reason,
        "answer": run.answer,
        "citations": accepted_citations,
        "rejected_citations": rejected_citations,
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
        # Trust: copied so a caller cannot mutate the store's
        # list through the serialized view.
        "evidence_locators": [dict(loc) for loc in run.evidence_locators],
        "analysis_results": copy.deepcopy(list(run.analysis_results or [])),
        # ADR 0089: the run's visual-claim rows (deep-copied — the rows are
        # flat but the ``scope`` region nests a list, so a shallow copy
        # would still share it with the store). Kept off ``citations``.
        "visual_claims": copy.deepcopy(list(run.visual_claims or [])),
        # EP6: the admitted structured snapshots, deep-copied for the same
        # reason — each row nests (snapshot + selector), so a shallow copy
        # would share the inner objects with the store.
        "structured_evidence": _structured_evidence_wire_payload(
            run.structured_evidence
        ),
        "retrieval_window": copy.deepcopy(dict(run.retrieval_window or {})),
        # T8: the latest thumb for this run (``None`` when unset).
        "feedback": feedback,
        # A5: NULL is unknown. A done turn with insufficient_evidence
        # is a partial answer, and this is the field that survives
        # after the checkpoint is cleared.
        "semantic_outcome": run.semantic_outcome,
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





def _resume_run_stream(
    graph: Any,
    store: ThreadStore,
    *,
    run_control: RunControl,
    thread_id: str,
    run_id: str,
    resume_input: Any,
    resume_config: dict[str, Any],
    receipts: TurnReceiptProjector | None = None,
    attachments: AttachmentPreparationService | None = None,
) -> Iterator[str]:
    """Drive one background resumed run to its turn row, discarding the wire.

    The attachment auto-resumer's only path: it consumes the frames to
    exhaustion and throws them away, because the resumed turn row is the
    half it needs and no operator is reading a stream. The operator's
    ``POST .../resume`` does NOT come through here — it builds its own
    executor so it can hand that executor the claim its route already
    took. What keeps the two surfaces from drifting is that both go
    through :class:`~fin.host.run_execution.RunExecutor`, which owns the
    graph consumption, the stop token, the iterator release and the one
    row; a background resume that drove the graph itself is how they used
    to diverge. A background resume ends on the same receipt as an
    operator's: it continues that turn's run, so it reports on that
    turn's receipt rather than starting a projection of its own.
    """

    executor = RunExecutor(
        graph=graph,
        store=store,
        run_control=run_control,
        receipts=receipts,
        publication_warnings=_publication_warnings(attachments),
    )
    return _sse_frames(
        executor.resume(
            ResumeRunRequest(
                thread_id=thread_id,
                run_id=run_id,
                resume_input=resume_input,
                resume_config=resume_config,
            )
        )
    )



def _citations_wire_projection(
    citations: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project ``payload["citations"]`` onto the wire, naming every refusal.

    Returns ``(rows, refusals)``. ``rows`` is the closed wire shape, and a
    dict or a ``Citation`` dataclass answers through one rule so the live
    stream and the history serialization cannot emit two different frames.
    ``refusals`` mirrors the history read's ``rejected_citations`` entry
    exactly — original ``citation_index`` plus the §6 code — so a row that
    cannot be published is a visible, typed, per-record outcome. The old
    silent drop-with-warning is closed here: the row's identity checks live
    in the validator home, this boundary only calls it.

    The frame-level overflow bound is the home's channel check, evaluated
    before any row is looked at: an oversize frame yields one named
    ``citation_overflow`` refusal, never a truncated publish.

    WHY the context is seeded from the rows themselves: thread trust,
    resolvable locators, and received texts are admission-time facts (E3),
    and by the time the transport re-emits a row those lanes have already
    run. A transport re-check cannot see the thread, so it asserts no
    denial from an empty view of it — seeding the row's own identity keeps
    those context checks inert while every shape check stays active.

    THIS IS THE EMISSION DOOR, and the distinction is load-bearing rather
    than descriptive. It has exactly one production caller: the terminal
    SSE ``citations`` frame in :func:`_terminal_sse_frames`. A frame
    published right now is an ACTIVE emission, so §2.1's token/version
    pairing is enforced in full — the mint in
    :func:`_citation_row_candidate` gives a pre-E5 keyless stored snapshot
    its frozen pair on the way OUT, and a row that states half a contract
    (a ``schema_version`` with no ``schema``, or the reverse) is NOT
    repaired: it reaches ``_validate_snapshot`` as stored and is refused
    there as ``malformed_evidence_contract`` ("database_row snapshot:
    missing ['schema']"). Spec §7.5 and ADR 0093 decision 12 say this in
    the spec's words: the grace applies to stored records, not to fresh
    producers, and an active frame may not borrow the legacy rule to skip
    version validation.

    The OTHER door is :func:`_serialize_run`, the history read, and it
    draws the line elsewhere on purpose: it serves STORED records, which
    spec §7.1 leaves "exactly as they are", so a pre-E5 keyless snapshot
    is published verbatim there and the web's replay decode accepts it. The
    two doors therefore REFUSE different shapes, and the web's
    ``decodeDatabaseRowSnapshot`` mirrors that split by taking an explicit
    ``SnapshotReadMode`` (its default is ``"emission"``, this door's mode)
    rather than sniffing the payload. One input makes the two ends
    disagree if that split is ever collapsed: a snapshot carrying
    ``schema_version`` with no ``schema`` is accepted on replay and
    refused here, and the two ends of the wire must answer alike
    (operator ruling R5).
    """
    rows: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    candidates = [_citation_row_candidate(source) for source in citations or ()]
    if len(candidates) > MAX_CITATIONS_PER_RUN:
        whole = validate_evidence(
            {
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
                "citations": candidates,
            },
            _wire_admission_context(candidates),
        )
        if not whole.accepted and whole.refusal is not None and whole.refusal.item_index is None:
            logger.warning(
                "citations wire: refused the frame as %s: %s",
                whole.refusal.code,
                whole.refusal.message,
            )
            return [], [{"citation_index": 0, "error": whole.refusal.code}]
    for index, candidate in enumerate(candidates):
        outcome = validate_evidence(
            {
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
                "citations": [candidate],
            },
            _wire_admission_context([candidate]),
        )
        code: str | None = None
        detail = ""
        if not outcome.accepted and outcome.refusal is not None:
            code, detail = outcome.refusal.code, outcome.refusal.message
        elif _row_names_no_record(candidate):
            # The record key plus the source are where a ledger citation's
            # own id lives, and this boundary has always required both: a
            # snapshot without them names a view, not a record, so the
            # opened citation would show nothing (spec 8.1).
            code, detail = MALFORMED_EVIDENCE_CONTRACT, "snapshot names no record"
        if code is not None:
            # The home's detail echoes field names, kind values, and schema
            # tokens — never a row's quote or field contents.
            logger.warning(
                "citations wire: refused row %d as %s: %s", index, code, detail
            )
            refusals.append({"citation_index": index, "error": code})
            continue
        try:
            # ``CitationPayload`` is the frozen-shape matcher: every stored
            # generation is read through ``project_stored_citation`` here,
            # so legacy defaults stay the home's decision, not this loop's.
            rows.append(CitationPayload(**candidate).model_dump(exclude_none=True))
        except ValidationError:
            # Safety net for the serialization-failure class: a row that
            # survived the home but cannot be built is a named contract
            # error, visible in the refusals list — never a silently
            # shorter frame and never a lost stream.
            logger.warning(
                "citations wire: refused row %d as %s on serialization",
                index,
                MALFORMED_EVIDENCE_CONTRACT,
            )
            refusals.append(
                {"citation_index": index, "error": MALFORMED_EVIDENCE_CONTRACT}
            )
    return rows, refusals


# The eight slots a wire row states its identity through, plus the one
# optional ledger sibling. Read off every source — a missing key stays
# absent so the validator home can name it, never defaulted here.
_CITATION_REQUIRED_FIELDS: Final[tuple[str, ...]] = tuple(
    name
    for name in CitationPayload.model_fields
    if name != DATABASE_ROW_SNAPSHOT_KEY
)


_ABSENT: Final[object] = object()  # private "key not on the row" sentinel


def _read_present(source: Any, name: str) -> Any:
    """One field of a wire row off a dict OR a dataclass, distinguishing
    an absent key from a present ``None`` (both read as ``None`` before,
    which let a corruption look like an older generation)."""
    if isinstance(source, dict):
        return source.get(name, _ABSENT)
    return getattr(source, name, _ABSENT)


def _citation_row_candidate(source: Any) -> dict[str, Any]:
    """Read one row's own fields into the frozen key set for the home.

    This is a projection, not a validation: values pass through as the row
    states them (wrong types included — the home names them), keys the row
    does not carry stay absent, and a ledger snapshot gains the §4.4
    ``schema`` token a fresh E5 frame owes an unversioned stored snapshot.
    A stored row is never rewritten — the mint is on this emission only.
    """
    candidate: dict[str, Any] = {}
    for name in _CITATION_REQUIRED_FIELDS:
        value = _read_present(source, name)
        if value is not _ABSENT:
            candidate[name] = value
    kind = candidate.get("kind")
    raw = _read_present(source, DATABASE_ROW_SNAPSHOT_KEY)
    if kind == DATABASE_ROW_KIND and (raw is _ABSENT or raw is None):
        # The live stream's ``Citation`` dataclass holds the row's identity
        # in flat gate slots, not a nested snapshot; rebuilding the spec's
        # shape here (the gate family's single assembly owner) is what lets
        # the same validator read both sources.
        rebuilt = database_row_snapshot(source)
        if rebuilt is not None:
            candidate[DATABASE_ROW_SNAPSHOT_KEY] = _mint_snapshot_schema(rebuilt)
    elif raw is not _ABSENT and raw is not None:
        candidate[DATABASE_ROW_SNAPSHOT_KEY] = (
            _mint_snapshot_schema(dict(raw))
            if isinstance(raw, Mapping)
            else raw
        )
    return candidate


def _mint_snapshot_schema(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Stamp the frozen E5 token and version pair onto a stored snapshot
    that carries NEITHER marker — the true pre-E5 keyless shape, whose
    generation owed no marker at all (spec §7.1, §7.2).

    A record that states one marker without the other is NOT repaired.
    §7.2 is a non-rewrite rule ("legacy rows are never rewritten,
    back-filled, or migrated in place"), and §7.5 says a record that
    carries ``schema`` without the ``schema_version`` §2.1 requires
    beside it is ``malformed_evidence_contract``; §7.3 says a refused
    record is NAMED per-record, not dropped. So half a contract reaches
    the validator home exactly as stored and is refused there under the
    code the home assigns — the mint never launders a broken emission
    into a wire that looks conforming, and an activity frame cannot
    borrow the legacy grace to skip a check its own generation owes.
    The v0 path is left exactly as strict as it was: a record with no
    ``schema`` at all is the only shape this touches, and it is accepted
    as legacy v0 with its stored values intact (§7.1, ADR 0093
    decision 8).

    Both markers present is likewise returned untouched, so an
    unrecognized token or an unsupported version integer reaches the
    home and is named ``unsupported_evidence_contract`` there rather
    than being normalized into an accepted row (ADR 0093 decisions 6
    and 7).
    """
    if "schema" in snapshot or EVIDENCE_SCHEMA_VERSION_FIELD in snapshot:
        return snapshot
    return {
        "schema": DATABASE_ROW_SNAPSHOT_SCHEMA,
        EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
        **snapshot,
    }


def _wire_admission_context(candidates: list[dict[str, Any]]) -> AdmissionContext:
    """The rows' own identities as validator context. One home for the
    posture and its never-hash rule: :func:`seed_context_from_rows` (see
    there for WHY self-seeding keeps the transport re-check shape-only).
    """
    return seed_context_from_rows(citation_rows=candidates)


def _row_names_no_record(candidate: dict[str, Any]) -> bool:
    """True iff a ledger candidate's snapshot names no view or no record."""
    if candidate.get("kind") != DATABASE_ROW_KIND:
        return False
    snapshot = candidate.get(DATABASE_ROW_SNAPSHOT_KEY)
    if not isinstance(snapshot, Mapping):
        return False
    return not str(snapshot.get("source") or "") or not snapshot.get("record_key")


def _reraise_single(group: BaseExceptionGroup) -> None:
    """Re-raise one task failure unwrapped; re-raise a group of several.

    A task group always collects its tasks' failures into a group. The
    transport above this response understands a bare ``OSError`` or
    ``ClientDisconnect`` and nothing else, so the single-exception case —
    the only one a disconnect produces — has to reach it unwrapped. Two
    unrelated failures stay a group, because dropping one of them would
    hide a second defect behind a first.
    """
    if len(group.exceptions) != 1:
        raise group
    exception = group.exceptions[0]
    context = None if exception.__suppress_context__ else exception.__context__
    raise exception from group.__cause__ or context


class _ClaimedStreamingResponse(StreamingResponse):
    """A run stream that hands the door's claim back however the wire ends.

    The claim is taken at the door, before the first byte, because a
    refusal has to be a 409 with no stream at all. Releasing it from the
    run generator's ``finally`` was the one place it could NOT be
    released: a client that disconnects before the first body chunk
    means that generator never starts, so the claim outlived every
    caller and the thread answered 409 ``concurrent_run`` to
    everything after it — while ``GET thread`` reported nothing in
    flight, because no run ever held anything but the claim. The graph
    was never reached, so nothing inside the graph could free it
    either, and on this transport shape the failure escapes before
    ``background`` is awaited.

    The response is the one object that always runs: its ``finally``
    fires on the normal path, on a mid-stream transport failure, and on
    a disconnect before iteration. ``RunAdmission.release`` is
    idempotent, so the executor's own release stays a no-op.

    That same ``finally`` is also where a turn whose stream was
    abandoned gets its outcome recorded, and the ORDER is the point:
    the claim comes back first, so a receipt write that itself fails
    cannot leave the thread locked, and the recorder decides for itself
    whether the run had already reported an end.
    """

    def __init__(
        self,
        *args: Any,
        admission: RunAdmission,
        on_disconnect: Callable[[], None] | None = None,
        on_end: Callable[[], None] | None = None,
        abandonment: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._admission = admission
        self._on_disconnect = on_disconnect
        self._on_end = on_end
        self._abandonment = abandonment

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        disconnect_reported = False
        stream_completed = False

        def report_disconnect() -> None:
            nonlocal disconnect_reported
            if disconnect_reported:
                return
            disconnect_reported = True
            if self._on_disconnect is not None:
                self._on_disconnect()

        async def observed_receive() -> Any:
            message = await receive()
            if message.get("type") == "http.disconnect":
                report_disconnect()
            return message

        async def observed_send(message: Any) -> None:
            try:
                await send(message)
            except OSError:
                report_disconnect()
                raise

        try:
            spec_version = tuple(
                map(
                    int,
                    scope.get("asgi", {}).get("spec_version", "2.0").split("."),
                )
            )
            # WHY one task group for both: the two ASGI spec versions differ
            # only in WHO swallows the transport's OSError. The disconnect
            # itself has to be observed on both, because a run blocked in
            # synchronous work never touches ``receive`` again, so a
            # listener that does not run alongside the stream reports a
            # lost client only once that work finishes on its own.
            try:
                async with anyio.create_task_group() as task_group:

                    async def stream_response() -> None:
                        nonlocal stream_completed
                        try:
                            await self.stream_response(observed_send)
                            stream_completed = True
                        except OSError as exc:
                            report_disconnect()
                            if spec_version < (2, 4):
                                raise ClientDisconnect() from exc
                            raise
                        finally:
                            task_group.cancel_scope.cancel()

                    async def listen_for_disconnect() -> None:
                        await self.listen_for_disconnect(observed_receive)
                        report_disconnect()
                        task_group.cancel_scope.cancel()

                    task_group.start_soon(stream_response)
                    await listen_for_disconnect()
            except BaseExceptionGroup as group:
                _reraise_single(group)
            if self.background is not None:
                await self.background()
        finally:
            try:
                if self._on_end is not None:
                    self._on_end()
            finally:
                try:
                    self._admission.release()
                finally:
                    if not stream_completed:
                        self._record_abandonment()

    def _record_abandonment(self) -> None:
        """Tell the receipt its stream ended without an outcome.

        Swallowed on purpose: this runs in the response's teardown, where
        an exception would replace whatever the wire was already
        reporting with a failure nobody asked about. A receipt that
        cannot be written is logged, not raised.
        """
        if self._abandonment is None:
            return
        try:
            self._abandonment()
        except Exception:
            logger.exception(
                "the abandoned turn's receipt could not be recorded"
            )


def _publication_warnings(
    attachments: AttachmentPreparationService | None,
) -> Callable[[str], int] | None:
    """The receipt's warning counter for whichever attachment store exists.

    ``None`` when no preparation service is wired — a deployment with no
    attachment lane has no documents to be unsearchable, and passing the
    counter anyway would report a clean thread on the strength of a
    measurement nobody could have made.
    """
    if attachments is None:
        return None
    return attachments.unresolved_publication_count


def _abandoned_stream(
    receipts: TurnReceiptProjector, *, thread_id: str, turn_id: str
) -> Callable[[], None]:
    """The recorder one run stream hands its outcome to on teardown.

    Bound here rather than at each call site so every run stream asks
    the same question of the same receipt, and so the projector — not
    the transport — decides whether an ending wire was a lost client or
    a run that had already reported its own end.
    """

    def record() -> None:
        receipts.stream_abandoned(thread_id=thread_id, turn_id=turn_id)

    return record


def _disconnect_stop(stop_token: StopToken) -> Callable[[], None]:
    """The one call a lost client makes on the run it left behind.

    Capturing the token fences even a resumed segment that reuses the
    same ``run_id``: a delayed disconnect cannot stop its newer token.
    ``client_disconnect`` is a receipt reason, so the abandoned run says
    why it stopped rather than reporting a timeout.
    """

    def stop() -> None:
        stop_token.stop(reason="client_disconnect")

    return stop


def _sse_frames(events: Iterable[RunEvent]) -> Iterator[str]:
    """Encode typed run events as the SSE frames the SPA reads.

    The ONE place a run's events become bytes. The event name is the kind, so
    no second table can disagree about a channel name, and a body rides
    through untouched: its key order is part of the observed wire contract,
    which is why the engine builds it and this only encodes it.
    """
    for event in events:
        if event.kind is RunEventKind.TERMINAL:
            yield from _terminal_frames(event.data)
            continue
        yield _format_sse(event.kind.value, event.data)


def _terminal_frames(terminal: RunTerminal) -> Iterator[str]:
    """The fixed-order terminal frames for one finished run.

    A finished turn reaches the wire as a projection, not a fixed body: how
    many frames it takes (the visual-claim channel only speaks when the run
    has claims) and which envelope the citations carry are properties of the
    run, so the run decides and this orders them.
    """
    yield from _terminal_sse_frames(
        run_id=terminal.run_id,
        payload=terminal.payload,
        thinking_type=terminal.thinking_type,
        thinking_ms=terminal.thinking_ms,
        answer_reset=terminal.answer_reset,
        claim_rows=terminal.claim_rows,
        visual_refusals=terminal.claim_refusals,
        semantic_outcome=terminal.semantic_outcome,
        evidence=terminal.evidence,
    )


def _node_frame_payloads(
    message: Any,
    pending_args: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, Any]]:
    """The node-complete ``(event, data)`` pairs for one message.

    The engine consumes typed events; this pair form is the shape a caller
    reading a single message's events off a run script expects.
    """
    return [
        (event.kind.value, event.data)
        for event in _node_frame_events(message, pending_args)
    ]


def _terminal_sse_frames(
    *,
    run_id: str,
    payload: RunResponsePayload,
    thinking_type: str | None = None,
    thinking_ms: int | None = None,
    answer_reset: bool = False,
    claim_rows: list[dict[str, Any]] | None = None,
    visual_refusals: list[dict[str, Any]] | None = None,
    semantic_outcome: str | None = None,
    evidence: Any = None,
) -> Iterator[str]:
    """Yield the fixed-order terminal frames: ``status`` -> ``answer``
    -> ``visual_claims`` (only when non-empty) -> ``citations``.

    ``citations`` is TERMINAL so the SPA closes its reader, so anything after
    it is unreachable; the visual-claim lane therefore rides BETWEEN the
    answer frames and the citations frame, and only when non-empty — a run
    with no claim leaves no frame, matching the persisted ``[]`` (consumers
    never branch on absence).

    ``claim_rows`` arrives already normalised by the run that owns them: the
    same rows are the persisted ``fin_run`` column, and normalising twice
    would be a second chance for the row and the frame to disagree.
    """
    # The answer was already projected when the run state was mapped into
    # this payload, so the terminal frame publishes the SAME string the
    # history row and the export carry (GE-5).
    safe_answer = strip_thinking_from_answer(
        payload["answer"] if payload["status"] == "done" else ""
    )
    status_body: dict[str, Any] = {
        "run_id": run_id,
        "status": payload["status"],
        "reason": payload["reason"] or None,
        "assistant_message": payload["assistant_message"],
        # The terminal frame declares the citation channel once here, so
        # the citations array keeps its exact eight-key row shape and a
        # client decodes against a named version instead of a guess.
        "evidence_schema": CITATION_CHANNEL_SCHEMA,
        EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
    }
    # Soft-Track: expose the graph track so the SPA can render a light
    # research hint without Chat|Research chrome.
    if payload.get("track"):
        status_body["track"] = payload["track"]
    # QA-chain T8: the product route intent/source ride the terminal
    # frame so the live receipt reports the same value as the persisted
    # turn row's ``track_decision`` (which carries them nested).
    #
    # THIS is the emission door for the closed vocabulary. T8 fixes four
    # wire intents (``retrieve`` / ``overview`` / ``chat`` /
    # ``policy_refuse``) and three sources (``rule`` / ``jev`` /
    # ``fallback``), and the contract lives in
    # :mod:`fin.host.grounded.route_intent` — so the two ``normalize_*`` readers
    # that own it are called HERE rather than the strings being
    # republished on truthiness. ``classify_node`` is the only writer of
    # these values and it projects through :func:`decide_route_intent`,
    # which is total, so nothing in the production path can produce an
    # out-of-vocabulary value; the check is the wire boundary's own.
    #
    # WHY the emitter rather than the graph writer: the graph owns the
    # DECISION and its audit, and a persisted audit should record what
    # the run actually decided even while that record is malformed. The
    # wire owns the CONTRACT, and a client that switches on these two
    # fields must be able to assume the vocabulary is closed. The same
    # reasoning splits the source: it rides only beside an intent that
    # survived, because 「how was this turn routed」 is not a standalone
    # fact — without the disposition it describes, it is unattributed.
    route_decision = dict(payload.get("track_decision") or {})
    route_intent = normalize_route_intent(route_decision.get("route_intent"))
    if route_intent is not None:
        status_body["route_intent"] = route_intent
        route_source = normalize_route_source(route_decision.get("route_source"))
        if route_source is not None:
            status_body["route_source"] = route_source
    status_body["search_mode_decision"] = dict(
        payload.get("search_mode_decision") or {}
    )
    if thinking_type is not None:
        status_body["thinking_type"] = thinking_type
    if thinking_ms is not None:
        status_body["thinking_ms"] = thinking_ms
    if semantic_outcome == "insufficient_evidence":
        status_body["semantic_outcome"] = semantic_outcome
    answer_body: dict[str, Any] = {"text": safe_answer}
    if answer_reset:
        answer_body["reset"] = True
    yield _format_sse("status", status_body)
    yield _format_sse("answer", answer_body)
    if claim_rows:
        # Emitted between the answer frames and the terminal ``citations``
        # frame: a visual claim is its own channel, and the citations frame
        # closes the reader so this is the last point the SPA can still see.
        yield _format_sse("visual_claims", {"visual_claims": claim_rows})
    citation_rows, citation_refusals = _citations_wire_projection(
        payload["citations"]
    )
    display_sections = citation_sections_from_evidence(citation_rows, evidence)
    for row, section in zip(citation_rows, display_sections, strict=True):
        if section is not None:
            row["section"] = section
    # The visual lane's named refusals ride the SAME ``evidence_refusals``
    # frame as the citation lane's (per-row ``{citation_index, error}``
    # entries; the index addresses the row's position in its own channel) —
    # a dropped malformed live row is closed there too.
    refusal_entries = [
        *(visual_refusals or ()),
        *citation_refusals,
    ]
    if refusal_entries:
        # A refused row rides its own frame BEFORE the terminal
        # ``citations`` frame (which closes the reader): the entry shape
        # mirrors the history read's ``rejected_citations`` so one refusal
        # vocabulary serves both surfaces, and a mid-stream loss is named
        # instead of silent.
        yield _format_sse(
            "evidence_refusals", {"refusals": refusal_entries}
        )
    yield _format_sse("citations", citation_rows)



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
    run_control: RunControl,
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

    Admission: the command claims the thread for exactly the compaction,
    the one part that writes. ``compact_thread``'s own per-thread lock
    only covers a run that has REACHED the graph, so without the claim a
    compact could land in the window where a reply has been admitted and
    decided but its stream has not started — writing the checkpoint out
    from under an acceptance computed against the pre-compaction
    envelope. The claim ends with the compaction, before the receipt
    frames: those read only the receipt already in hand, so holding it
    through the stream would buy nothing and would strand a client that
    disconnected. A refusal keeps the operator's 409 either way — the
    claim refuses as ``concurrent_run`` and the lock refuses as
    ``concurrent_run``.
    """
    run_id = str(uuid.uuid4())
    claim = run_control.admit(thread_id)
    try:
        receipt = graph.compact_thread(thread_id)
    finally:
        claim.release()

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
            # WHY no receipt: a command is not an accepted answer turn. It
            # accepts no input, answers no question, and runs no model on
            # a turn identity, so there is no (thread_id, turn_id) turn to
            # project — this row stands in ``fin_run`` alone, under its own
            # minted run id, and the operator reads it as a command result.
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
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
            },
        )
        yield _format_sse("answer", {"text": copy})
        yield _format_sse("citations", [])

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _model_identity() -> str:
    """The configured ``FIN_MODEL`` slug, or empty when unset.

    The endpoint and auth token never reach this payload (ADR 0033).
    """
    import os

    return (os.environ.get("FIN_MODEL") or "").strip()


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


class _CarriesErrorCode(Protocol):
    # The table dispatch calls a row's resolver with the matched exception
    # family, and pyright cannot correlate row-to-resolver per iteration, so
    # every resolver parameter must accept any table family, not just PushError.
    code: str


def _push_error_status(exc: _CarriesErrorCode) -> int:
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
    # The public upload identity refuses a caller-chosen id, and explicit
    # upload metadata is validated (not edited) against the shared
    # bounds. Both are caller-input refusals before any write, so both
    # answer 422 with their named code.
    (IngestDocumentIdNotAllowedError, 422, False),
    (IngestInvalidUploadMetadataError, 422, False),
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


# ----- ingest -> catalog registration (T6) -----
#
# The catalog write is no longer a side effect the sync route observes
# through a thread-local slot. It is a RECOVERABLE step of the durable
# run: the endpoint persists ``catalog_publication_pending`` with the
# complete row, performs the upsert through this hook, and records a
# typed success or failure in the audit trail. Warning state is read
# back from that trail on poll, so it survives a restart and no
# thread-local transport is involved.
#
# The hook therefore PROPAGATES the catalog's own failures instead of
# swallowing them: the endpoint is the layer that knows how to type a
# ``CatalogRowError`` / ``CatalogCorruptError`` / ``OSError`` into a
# ``catalog_publication_failed`` event and keep the job in
# ``cataloging``. A hook that swallowed here would make every catalog
# outage look like a success.


def _record_ingested_filing(doc: IngestedDocument) -> None:
    """Product ``on_ingested`` hook: register the pushed filing.

    Maps the :class:`IngestedDocument` onto the T6 row contract and
    upserts it into the default catalog table (``record_filings``'
    own default — the ``data/corpus/catalog.json`` the ``GET
    /api/corpus`` route reads). ``title`` and ``doc_type`` are the
    display fields the row contract now requires; ``source_sha256`` is
    carried only when the run set it (public uploads), so one value is
    never stored under two keys for the trusted replay leg.

    A catalog-side failure propagates: the durable run converts it to
    the typed ``catalog_publication_failed`` audit event, keeps the job
    in ``cataloging``, and surfaces the warning through polling. The
    push itself already landed and is never rolled back.
    """
    row: dict[str, object] = {
        "document_id": doc.document_id,
        "version": doc.version,
        "page_count": doc.page_count,
        "title": doc.title,
        "doc_type": doc.doc_type,
    }
    # Optional fields are OMITTED when absent, never written as ``None``:
    # the row contract refuses a non-string and a missing company is not
    # a company named "None".
    if doc.company:
        row["company"] = doc.company
    if doc.period:
        row["period"] = doc.period
    if doc.source_sha256:
        row["source_sha256"] = doc.source_sha256
    # One row: ``record_filings`` takes an iterable of rows.
    record_filings([row])


def _catalog_warning_body(
    warning: PublicationWarning | None,
) -> dict[str, Any] | None:
    """The pollable warning shape, or ``None`` when the publication settled.

    Only typed, already-safe values cross this boundary: the code, the
    typed failure classification and the 1-based attempt number — never
    an exception message, a path or a DSN. A poller renders a fixed
    安全 copy from ``code``; the detail it needs to route the operator is
    ``failure_code`` / ``failure_stage``, which are closed vocabularies.
    """
    if warning is None:
        return None
    body: dict[str, Any] = {
        "code": warning.code,
        "attempts": warning.attempts,
    }
    if warning.failure_code is not None:
        body["failure_code"] = warning.failure_code
    if warning.failure_stage is not None:
        body["failure_stage"] = warning.failure_stage
    if warning.stage is not None:
        body["stage"] = warning.stage
    return body


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

    An explicit catalog keeps every journal beside that file. With no
    catalog path, the two ledger files follow the fin home when that
    home is active, and the push journal stays next to the repo catalog.
    An env var overrides one journal without moving the others.
    """
    from fin.corpus.catalog import DEFAULT_CATALOG_PATH, default_corpus_ledger_path

    if catalog_path is not None:
        data_dir = Path(catalog_path).parent
        pushed_default = data_dir / "pushed_records.jsonl"
        index_default = data_dir / INDEX_JOBS_FILE
    else:
        data_dir = Path(DEFAULT_CATALOG_PATH).parent
        pushed_default = default_corpus_ledger_path("pushed_records.jsonl")
        index_default = default_corpus_ledger_path(INDEX_JOBS_FILE)
    return {
        "push_journal_path": _configured_journal_path(
            _PUSH_JOURNAL_PATH_ENV, data_dir / "push_journal.jsonl"
        ),
        "invalidation_ledger_path": _configured_journal_path(
            _INVALIDATION_LEDGER_PATH_ENV, data_dir / "invalidation_ledger.jsonl"
        ),
        "pushed_records_path": _configured_journal_path(
            _PUSHED_RECORDS_PATH_ENV, pushed_default
        ),
        "index_job_ledger_path": _configured_journal_path(
            _INDEX_JOB_LEDGER_PATH_ENV, index_default
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
    from fin.host.operator_env import (
        resolve_corpus_scope_id,
        resolve_retrieve_adapter_kind,
    )

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
    corpus_scope_id = resolve_corpus_scope_id()
    if resolve_retrieve_adapter_kind() == "fake":
        shared: FakePushPort = FakePushPort()
        return _JournaledIngestEndpoint(
            push_port=shared,
            invalidation_port=shared,
            on_ingested=_record_ingested_filing,
            object_store=objects,
            metadata_store=pointers,
            state_store=states,
            corpus_scope_id=corpus_scope_id,
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
        corpus_scope_id=corpus_scope_id,
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


def _build_ingest_worker(
    *,
    endpoint: IngestEndpoint | None,
    journal_dir: Path | None,
    index_job_tracker: IndexJobTracker | None,
    index_job_ledger: IndexJobLedger | None,
) -> IngestWorker:
    if index_job_tracker is None and index_job_ledger is None:
        index_job_tracker, index_job_ledger = _get_index_job_components()
    return IngestWorker(
        endpoint if endpoint is not None else get_ingest_service(),
        journal_dir=(
            journal_dir if journal_dir is not None else _ingest_worker_journal_dir()
        ),
        index_job_tracker=index_job_tracker,
        index_job_ledger=index_job_ledger,
    )


def _build_ingest_drain_thread(
    worker: IngestWorker,
    check_interval_seconds: float | None,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_drain_ingest_queue,
        args=(
            worker,
            stop_event,
            check_interval_seconds
            if check_interval_seconds is not None
            else INGEST_WORKER_CHECK_INTERVAL_SECONDS,
        ),
        name=INGEST_WORKER_THREAD_NAME,
        daemon=True,
    )
    return stop_event, thread


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
        worker = _build_ingest_worker(
            endpoint=endpoint,
            journal_dir=journal_dir,
            index_job_tracker=index_job_tracker,
            index_job_ledger=index_job_ledger,
        )
        stop_event, thread = _build_ingest_drain_thread(
            worker, check_interval_seconds
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


def start_application_ingest_worker(
    *,
    endpoint: IngestEndpoint | None = None,
    journal_dir: Path | None = None,
    check_interval_seconds: float | None = None,
    index_job_tracker: IndexJobTracker | None = None,
    index_job_ledger: IndexJobLedger | None = None,
) -> Callable[[], None]:
    """Start one application's ingest drain and return its exact stop.

    The durable service and index-job projection remain process-wide, while
    the loop and its stop event belong to this application. This lets two
    mounted apps drain the shared durable queue without either app's shutdown
    stopping the other's thread.
    """
    worker = _build_ingest_worker(
        endpoint=endpoint,
        journal_dir=journal_dir,
        index_job_tracker=index_job_tracker,
        index_job_ledger=index_job_ledger,
    )
    stop_event, thread = _build_ingest_drain_thread(
        worker, check_interval_seconds
    )
    thread.start()

    def _stop() -> None:
        stop_event.set()
        if thread is not threading.current_thread():
            thread.join(timeout=60.0)

    return _stop


# One attachment service per process, over the env-selected thread store
# and object store: attachment rows are durable product state, so they
# follow the same singletons the thread sidebar and corpus ingest use.
_attachment_service_singleton: AttachmentPreparationService | None = None
_attachment_service_lock = threading.Lock()


# Where a layout pass leaves the boxes it found, so a later question's crop
# inspection can be taken without re-running Heron. A callable rather than
# the store itself: the parser is built before the store that owns the row
# exists in a test, and the sink keeps that order from mattering.
RegionSink = Callable[..., int]


class HeronPageLaneParser(PageRegionParser):
    """The deployed pages port: one pinned Heron pass per PDF document.

    The core binds its layout at construction and never hands a document
    to it, while :class:`~fin.host.attachment.attachment_layout.HeronPageLayout` is
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
        # WHY the sink is not a constructor argument: it is bound
        # post-construction, because the page lane is built from
        # environment bounds before the store those boxes belong to is
        # chosen. A region sink is additive metadata, so a lane deployed
        # without one still parses, renders, and scans — it simply leaves
        # no croppable box behind, which the crop lane reads as the
        # whole-page fallback.
        self._region_sink: RegionSink | None = None

    @property
    def heron_config(self) -> HeronLayoutConfig:
        """The boot-resolved harness knobs every document's pass is taken with."""
        return self._heron_config

    @property
    def region_sink(self) -> RegionSink | None:
        return self._region_sink

    @region_sink.setter
    def region_sink(self, sink: RegionSink | None) -> None:
        """Bind the store that keeps the boxes this pass found.

        Settable because the composition order is real: the page lane is
        built from environment bounds before the store those boxes belong to
        is chosen, and rebinding after the fact is cheaper — and less
        fragile — than threading a not-yet-built store through the lane.
        """
        self._region_sink = sink

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
            result = document_parser.extract_pdf(
                source_bytes=source_bytes,
                source_sha256=source_sha256,
                document_id=document_id,
                version=version,
                max_pages=max_pages,
            )
            self._persist_regions(
                document_id=document_id, version=version, result=result
            )
            return result
        finally:
            # A copy this process wrote for one preparation is removed even
            # when that preparation failed: the row already carries the
            # typed exit, and a private filing must not leak into tmp.
            try:
                staged.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.debug("staged copy %s not removed: %s", staged, cleanup_exc)

    def _persist_regions(
        self, *, document_id: str, version: str, result: PageParseResult
    ) -> None:
        """Leave one version's page boxes where the crop lane will find them.

        The layout pass already produced this geometry; persisting it is what
        makes a later question's crop inspection possible at all. The write
        is best-effort beside the units it describes — a sink failure is
        logged and the boxes are lost rather than failing a preparation whose
        text and index are already sound, because regions are additive
        metadata (the same posture the core takes: an unanswerable page is
        reported, never fatal).
        """
        if self._region_sink is None:
            return
        regions = tuple(
            StoredPageRegion(
                page=page.page_no,
                kind=box.kind,
                left_pt=box.left_pt,
                top_pt=box.top_pt,
                right_pt=box.right_pt,
                bottom_pt=box.bottom_pt,
            )
            for page in result.pages
            for box in page.regions
        )
        try:
            written = self._region_sink(
                attachment_id=document_id, version=int(version), regions=regions
            )
        except Exception as exc:  # noqa: BLE001 - additive metadata, never fatal
            logger.warning(
                "attachment region boxes could not be stored: attachment_id=%s (%s)",
                document_id,
                exc,
            )
            return
        logger.debug(
            "stored %d region boxes for attachment_id=%s version=%s",
            written,
            document_id,
            version,
        )


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
    :class:`~fin.host.attachment.attachment_prepare.AttachmentPageLaneUnwiredError`
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
    from fin.host.attachment.attachment_job_store import (
        InMemoryThreadAttachmentJobStore,
        PostgresThreadAttachmentJobStore,
    )
    from fin.host.attachment.attachment_store import (
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
    from fin.host.attachment.attachment_budgets import BOUND_ENV_KEYS

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
    # The boxes a page lane located belong to the same store that owns the
    # row they describe; the sink is bound here, once both exist, because
    # ``pages`` was built from environment bounds before the store was
    # chosen. A lane with no sink (tests, unwired deployments) leaves no
    # boxes, which the crop lane reads as the whole-page fallback.
    if pages is not None:
        pages.region_sink = store.record_regions
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


def _build_attachment_prep_worker(
    service: AttachmentPreparationService | None,
    interval_s: float,
) -> AttachmentPrepWorker:
    return AttachmentPrepWorker(
        service if service is not None else default_attachment_service(),
        interval_s=interval_s,
    )


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
            _attachment_prep_worker = _build_attachment_prep_worker(
                service, interval_s
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


def start_application_attachment_prep_worker(
    service: AttachmentPreparationService,
    *,
    interval_s: float = ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS,
) -> Callable[[], None]:
    """Start an app-owned prep drain and return its exact stop callback."""
    worker = _build_attachment_prep_worker(service, interval_s)
    worker.start()
    return worker.stop


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
    receipts: TurnReceiptProjector | None = None,
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
        receipts=receipts,
        attachments=service,
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
    receipts: TurnReceiptProjector | None = None,
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
                receipts=receipts,
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
    receipts: TurnReceiptProjector | None = None,
) -> None:
    """Startup sweep, then a bounded periodic re-check, until stopped.

    The loop body is kept separate from the thread plumbing (the shared
    :func:`~fin.host.attachment.attachment_prep_worker.drain_periodically`, the
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
            receipts=receipts,
        ),
        stop_event,
        DrainCadence(check_interval_seconds),
        worker_logger=logger,
        log_label="attachment resume worker",
    )


def _build_attachment_resume_worker(
    *,
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    workspace_root: Path | None,
    run_control: RunControl | None,
    check_interval_seconds: float | None,
    receipts: TurnReceiptProjector | None,
) -> PeriodicDrainWorker:
    control = run_control if run_control is not None else RunControl()
    return PeriodicDrainWorker(
        lambda: _resume_attachment_waits_once(
            graph,
            store,
            service,
            control,
            workspace_root=workspace_root,
            receipts=receipts,
        ),
        DrainCadence(
            check_interval_seconds
            if check_interval_seconds is not None
            else ATTACHMENT_RESUME_WORKER_CHECK_INTERVAL_SECONDS
        ),
        thread_name=ATTACHMENT_RESUME_WORKER_THREAD_NAME,
        stopping_error=AttachmentResumeWorkerStoppingError,
        log_label="attachment resume worker",
        worker_logger=logger,
    )


def start_attachment_resume_worker(
    *,
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    workspace_root: Path | None = None,
    run_control: RunControl | None = None,
    check_interval_seconds: float | None = None,
    receipts: TurnReceiptProjector | None = None,
) -> threading.Thread:
    """Start the process's single attachment auto-resumer (idempotent).

    The boot half of the durable gate's resume clause: runs paused on
    the preparation interrupt are driven by this sweep, never by an
    operator resend. The singleton guard is the shared
    :class:`~fin.host.attachment.attachment_prep_worker.PeriodicDrainWorker`, the
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
            worker = _build_attachment_resume_worker(
                graph=graph,
                store=store,
                service=service,
                workspace_root=workspace_root,
                run_control=run_control,
                check_interval_seconds=check_interval_seconds,
                receipts=receipts,
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


def start_application_attachment_resume_worker(
    *,
    graph: Any,
    store: ThreadStore,
    service: AttachmentPreparationService,
    workspace_root: Path | None = None,
    run_control: RunControl | None = None,
    check_interval_seconds: float | None = None,
    receipts: TurnReceiptProjector | None = None,
) -> Callable[[], None]:
    """Start an app-owned auto-resumer bound to this app's graph/control."""
    worker = _build_attachment_resume_worker(
        graph=graph,
        store=store,
        service=service,
        workspace_root=workspace_root,
        run_control=run_control,
        check_interval_seconds=check_interval_seconds,
        receipts=receipts,
    )
    worker.start()
    return worker.stop


# The invalidation drain's cadence: the spec's "drains at least once per
# minute while online" clause finally has a caller. Same posture as the three
# workers above — one process-wide executor, started only from the boot path,
# daemon thread, stopped in the boot ``finally``. The transport pair is bound
# by P3's producer (the IK invalidations channel is not answerable from this
# repo yet), so the boot start is a no-op until that binding exists: a drain
# with no transport would poll a channel that answers nothing and log a
# refusal every interval forever. The pair's type is declared beside the
# worker that calls it (structured_evidence_drain_worker) and imported here
# rather than redeclared, so the binder and the drain name one type.
_invalidation_drain_worker: StructuredInvalidationDrainWorker | None = None
_invalidation_drain_transport: InvalidationDrainTransport | None = None
_invalidation_drain_worker_lock = threading.Lock()


def bind_invalidation_drain_transport(
    fetch: Callable[[str | None], object],
    acknowledge: Callable[[str], None],
) -> None:
    """Bind the ``(fetch, acknowledge)`` pair the invalidation drain calls.

    The one place the drain's transport enters the process, so P3's producer
    binds here rather than the boot path growing a second lifecycle. A start
    already reading a live worker keeps the seams it was built with — the
    binding takes effect at the next start, never by mutating a running
    drain's callables underneath it.
    """
    global _invalidation_drain_transport
    with _invalidation_drain_worker_lock:
        _invalidation_drain_transport = (fetch, acknowledge)


def default_invalidation_drain_transport() -> InvalidationDrainTransport | None:
    """The bound invalidation transport, or ``None`` when none is bound.

    ``None`` is the honest answer on this build: the backend half of the
    invalidations channel lands with P3, so until it is bound there is
    nothing for the drain to call.
    """
    with _invalidation_drain_worker_lock:
        return _invalidation_drain_transport


def start_invalidation_drain_worker(
    *,
    store: ThreadStore | None = None,
    consumer_id: str = INVALIDATION_DRAIN_CONSUMER_ID,
    transport: InvalidationDrainTransport | None = None,
    cadence: DrainCadence | None = None,
) -> threading.Thread | None:
    """Start the process's single invalidation drain, or return the live one.

    Returns ``None`` when no transport is bound — the boot path calls this
    unconditionally, and a build without P3's producer must not acquire a
    thread whose every interval ends in the same refusal.

    ``cadence`` carries the interval and the loop's clock together, so a test
    drives the whole cadence contract — the period, not just the sleep — on a
    fake clock instead of a real minute. It defaults to
    :func:`~fin.host.evidence.structured_evidence_drain_worker.default_cadence`: the
    spec's minute on the real clock.

    Rebuilt — honoring freshly passed seams — only once the previous thread's
    exit is confirmed, the same restart-after-clean-stop rule the resume
    sweep uses.
    """
    global _invalidation_drain_worker
    pair = (
        transport if transport is not None else default_invalidation_drain_transport()
    )
    if pair is None:
        return None
    # WHY lazy: a caller with no bound transport returns above without ever
    # calling this, so a build whose drain never runs never opens the
    # Postgres pool behind ``default_thread_store``. It is a lazy CALL, not a
    # lazy import — this module already imports ``thread_store`` at its top.
    from fin.host.thread_store import default_thread_store

    with _invalidation_drain_worker_lock:
        worker = _invalidation_drain_worker
        if worker is None or not worker.is_live():
            worker = StructuredInvalidationDrainWorker(
                store if store is not None else default_thread_store(),
                pair,
                consumer_id=consumer_id,
                cadence=cadence,
            )
            _invalidation_drain_worker = worker
        return worker.start()


def stop_invalidation_drain_worker(*, timeout: float = 60.0) -> None:
    """Stop the process's invalidation drain. Idempotent.

    The in-flight batch finishes its current phase first: a scrub that
    committed without its acknowledgement is exactly the state the next
    process's startup sweep reconciles, so a half-run drain is recoverable
    and must not be cut mid-transaction.
    """
    with _invalidation_drain_worker_lock:
        worker = _invalidation_drain_worker
    if worker is not None:
        worker.stop(timeout=timeout)


def start_application_invalidation_drain_worker(
    *,
    store: ThreadStore,
    consumer_id: str = INVALIDATION_DRAIN_CONSUMER_ID,
    transport: InvalidationDrainTransport | None = None,
    cadence: DrainCadence | None = None,
) -> Callable[[], None]:
    """Start this application's invalidation drain when transport exists."""
    pair = transport if transport is not None else default_invalidation_drain_transport()
    if pair is None:
        # No transport means the legacy boot starts no polling worker either.
        return lambda: None
    worker = StructuredInvalidationDrainWorker(
        store,
        pair,
        consumer_id=consumer_id,
        cadence=cadence,
    )
    worker.start()
    return worker.stop


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
    run_control: RunControl | None = None,
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

    ``run_control`` is the stop registry this router answers Stop
    against. It is created HERE when absent, so a caller that mounts
    directly keeps its own isolation; an application that ALSO runs
    background work passes the one instance it owns, so the Stop door
    and attachment auto-resume signal the same runs. The send gate's
    in-flight counter (:class:`AttachmentUploadInFlight`) is created HERE
    for the same reason.
    """
    router = APIRouter(prefix="/api")
    if run_control is None:
        run_control = RunControl()

    def get_turn_receipts(
        store: ThreadStore = Depends(get_thread_store),
    ) -> TurnReceiptProjector:
        """The turn-receipt projection paired with this router's store.

        Derived from the same dependency the routes write their turns to
        rather than selected again: a receipt beside a different thread
        store would split one turn across two histories, and the
        background auto-resumer — which has no route to ask — would be
        reporting turns it could not read back.
        """
        return default_receipts(store)

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
        # WHY the outage is caught around both store reads: a thread
        # store that cannot be reached is the named
        # ``persistence_unavailable`` outcome — a typed 503 the SPA can
        # show — never a crash, and never the empty turn table that
        # swallowing the failure would masquerade as.
        try:
            thread = store.get_thread(thread_id)
        except PersistenceUnavailableError:
            logger.warning("get_thread: thread store unreachable", exc_info=True)
            return cast(dict, JSONResponse(
                status_code=503, content={"error": "persistence_unavailable"}
            ))
        if thread is None:
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
        # T2: existence check wins (404 above); the MCP gate runs right
        # after it, so an unknown id is never masked by this 409.
        if knowledge_mcp_gate_closed():
            raise KnowledgeMCPFaultError()
        try:
            runs = store.list_runs(thread_id)
        except PersistenceUnavailableError:
            logger.warning("get_thread: run list unreachable", exc_info=True)
            return cast(dict, JSONResponse(
                status_code=503, content={"error": "persistence_unavailable"}
            ))
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
        # The projection reads checkpoint status, interrupt kind, and
        # pending-turn provenance from one snapshot after the lock probe.
        pending_projection = _pending_checkpoint_wire_projection(
            graph, thread_id, is_locked=is_locked
        )
        # T3 (ADR 0044): occupancy from the same compactor figure the
        # gate uses. Defensive ``hasattr`` mirrors the ``is_locked``
        # wiring-bug tolerance for graph stand-ins without that seam.
        occupancy = None
        if hasattr(graph, "occupancy"):
            occupancy = graph.occupancy(thread_id)
        return {
            **_serialize_thread(thread),
            "in_flight": is_locked,
            "pending_checkpoint": pending_projection["pending_checkpoint"],
            "pending_interrupt_kind": pending_projection[
                "pending_interrupt_kind"
            ],
            "pending_turn": pending_projection["pending_turn"],
            "occupancy": occupancy,
            "task_runtime": _task_runtime_wire_projection(graph, thread_id),
            "source_scope": _source_scope_wire_projection(graph, thread_id),
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
        receipts: TurnReceiptProjector = Depends(get_turn_receipts),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
    ) -> dict:
        existing = store.get_thread(thread_id)
        if existing is None:
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
        # A run in flight owns this thread's checkpoints and receipts, and
        # it will write to them after this request returns. Deleting under
        # it would be the race this claim exists to prevent, so the delete
        # is refused outright rather than admitted and raced — the same 409
        # a second send gets. The claim is the door's own registry, never
        # the graph's non-reentrant lock; the occupancy probe below is what
        # sees a run the graph is executing for a caller holding no claim.
        admission = run_control.admit(thread_id)
        try:
            ensure_thread_occupancy(graph, thread_id)
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
                return cast(dict, JSONResponse(
                    status_code=500,
                    content={"error": "attachment_cleanup_failed"},
                ))

            # Metadata, history and receipts next, in the ONE transaction
            # that records why they are going: the cause is written while
            # the receipts still exist, and the thread's removal takes them
            # with it.
            try:
                receipts.delete_thread(thread_id)
            except ThreadNotFoundError:
                logger.warning(
                    "delete_thread: thread %s was already gone", thread_id
                )
                return cast(dict, JSONResponse(
                    status_code=404, content={"error": "thread_not_found"}
                ))
            # The thread this registry was counting is gone, so its run —
            # a paused one nobody will ever resume — leaves the counts with
            # it instead of reading as work still in flight.
            run_control.forget(thread_id)

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
            except ConcurrentRunError:
                # A run the graph is executing for a caller holding no
                # claim took the per-thread lock between the probe above
                # and this call. The thread is already gone, so this is
                # the inconsistent state below rather than a refusal: the
                # caller would retry and be told 404 about a thread it had
                # in fact just deleted.
                logger.exception(
                    "delete_thread: saver contended for thread_id=%s", thread_id
                )
                raise HTTPException(status_code=500, detail="delete failed") from None
            except Exception:
                logger.exception(
                    "delete_thread: saver failed for thread_id=%s", thread_id
                )
                raise HTTPException(status_code=500, detail="delete failed") from None
        finally:
            # Released here and not by the graph: this claim guards the
            # retirement itself, and a late release would let a second
            # delete or a send walk past a thread that is still going.
            admission.release()
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
        receipts: TurnReceiptProjector = Depends(get_turn_receipts),
    ) -> Response:
        """Run one v1 host-graph run on the thread over SSE (T1).

        Wire shape (the run is driven by ``RunExecutor``; this route
        only encodes what it emits):

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
        #
        # The two EXPLICIT task doors are exempt. A restore names a
        # concrete archive and a reply names a concrete question; neither
        # carries message text, so requiring one would silently drop the
        # operator's request as an empty submit — a restore would look
        # like it had done nothing, which is the false impression W5
        # forbids. A door that names nothing at all is still an empty
        # submit and still answers 204.
        if not (
            body.question
            and body.question.strip()
            or body.restore_archive_id
            or body.question_id
        ):
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
        # code is named per 档 off the one routing table
        # (``TIER_UNAVAILABLE_CODE``), never collapsed into one token.
        # That table is EMPTY since A2 opened the sql 档 (spec
        # search-mode-business-sql-contract §4: fin's change is one
        # table row), so every closed-set 档 starts its run and this
        # door only fires again when a future plan slot closes one.
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
                run_control=run_control,
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


        # 409-before-headers: RunControl owns admission and this claim is
        # acquired HERE, synchronously, because the typed exception handler
        # translates the refusal to 409 ``application/json``; a check
        # inside the StreamingResponse generator would surface mid-body
        # once event-stream headers were already flushed.
        executor = RunExecutor(
            graph=graph,
            store=store,
            run_control=run_control,
            collector=collector,
            receipts=receipts,
            publication_warnings=_publication_warnings(attachments),

        )
        # ONE serialized admission section per thread: read pending state,
        # decide what this send is, accept its input, and run — with no
        # other caller able to read that state in between. The previous
        # occupancy probe could not promise that: it sampled the graph's
        # lock, which is taken LAZILY inside ``graph.stream``, so two
        # callers could both pass it and both compute an acceptance from
        # the same snapshot before either lost the race. The loser then
        # discovered it one frame into its own stream, having already
        # advanced an ``input_version`` nothing ever committed.
        #

        # NOT the graph's lock: that lock is non-reentrant and is taken
        # exactly once, inside the single ``graph.stream`` call below.
        # This claim is the door's own; Stop records its reason here until
        # the token exists, without waiting on either lock.
        admission = run_control.admit(thread_id, stop_pending=True)
        try:
            # The graph's own lock is the FOREIGN run detector: the
            # attachment auto-resumer and the compaction command drive
            # that same per-thread lock from registries this door holds
            # no claim on, so their runs are visible only here. It stays
            # a fast path on top of the claim, never a substitute for it —
            # a probe answers "is anyone in the graph", while the claim is
            # what makes the read-decide-run section below indivisible.
            executor.assert_thread_occupancy(thread_id)
            # A clarification reply is classified against the COMMITTED
            # envelope before anything is minted: an exact repeat
            # delivery is a no-op, and a reply that answers a paused run
            # continues THAT run rather than starting a second one under
            # a fresh identity.
            runtime_state = _accepted_task_runtime_state(graph, thread_id)
            reply_plan = (
                _plan_reply(

                    graph,
                    thread_id,
                    runtime_state,
                    body,
                    workspace_root=workspace_root,
                )
                if body.question_id
                else _ReplyPlan()
            )
            if reply_plan.retry is not None:
                admission.release()
                return StreamingResponse(
                    _retry_frames(store, thread_id, reply_plan.retry),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            # A terminal clarification has no paused run: its reply is a
            # NEW turn that consumes the pending question. Captured before
            # acceptance because acceptance advances the envelope and the
            # question is the context this turn must answer under.
            pending_clarify = _pending_terminal_clarification(
                runtime_state, body
            )
            continued = reply_plan.continued
            if continued is not None:
                # The reply is already accepted by Task Runtime; this is
                # the receipt's projection of that one acceptance, on the
                # SAME turn the paused run already owns.
                if continued.accepted_by_this_door:
                    receipts.accepted_reply(
                        thread_id=thread_id,
                        turn_id=continued.run_id,
                        input_version=continued.accepted_input_version,
                    )

                # Minted at the door for the same reason as the send
                # below: a disconnect observed before the first frame must
                # still reach the run it started.
                continued_token = run_control.begin(
                    thread_id, continued.run_id, admission=admission
                )

                def event_stream() -> Iterator[str]:
                    """The answered run's remainder on the SAME wire.


                    A continuation keeps the resume column's history
                    rules verbatim: a stop or a mid-flight failure
                    writes no turn row, and a completed remainder writes
                    exactly one — under the ORIGINAL run id.
                    """
                    yield from _sse_frames(
                        executor.resume(
                            ResumeRunRequest(
                                thread_id=thread_id,
                                run_id=continued.run_id,
                                resume_input=continued.resume_input,
                                resume_config=continued.resume_config,
                                admission=admission,
                                stop_token=continued_token,
                            )

                        )
                    )

                _register_dream_background(background, memory_port, thread_id)
                return _ClaimedStreamingResponse(
                    event_stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                    admission=admission,
                    on_disconnect=_disconnect_stop(continued_token),
                    abandonment=_abandoned_stream(
                        receipts, thread_id=thread_id, turn_id=continued.run_id
                    ),
                )

            # Mint the run_id up front so the SSE ``status`` event can
            # carry it before any graph work happens. The same id is
            # persisted on the RunRecord, so the optimistic turn the
            # client renders matches the stored turn row exactly.
            run_id = str(uuid.uuid4())

            # A PAUSED choice window releases the per-thread lock (the run's
            # stream ended on the interrupt), so the same-thread send needs this
            # second probe to keep the 409 semantics: the run is still in flight
            # on the checkpoint, waiting for the operator's pick. Same typed 409
            # ``concurrent_run`` answer — placed BEFORE the attachment
            # consumption below, so a rejected send keeps its attachments.
            executor.assert_no_pending_choice(thread_id)

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
                # T3: the operator's thinking preference rides the
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
            # Question/task runtime T2: the send door is the only place an
            # ordinary message is durably accepted, and acceptance happens
            # BEFORE the graph runs, so the original payload and provenance
            # are already recoverable when any dependant work starts.
            #
            # The accepted record rides the run CONFIG and the committed
            # envelope rides the input state, because the two have opposite
            # lifetimes: the config is per-invocation (a resume carries none,
            # so a resumed execution never manufactures another accepted
            # input), while the envelope is thread-scoped and must survive
            # the run-scoped reset below.
            stale_hint = _question_hint_is_ordinary(runtime_state, body)
            accepted_kind = (
                INPUT_MESSAGE
                if stale_hint
                else _accepted_input_kind(body)
            )
            runtime_state, accepted_task_input = _accept_at_send_door(
                runtime_state,
                body,
                kind=accepted_kind,
                run_id=run_id,
                graph=graph,
                thread_id=thread_id,
            )
            committed_runtime = runtime_state
            # A stale hint never contributes prose: the message is the
            # operator's own text, which for a free-text-only send can
            # live in ``free_text`` rather than ``question``.
            turn_question = (
                (body.question or body.free_text or "")
                if stale_hint
                else body.question
            )
            if (
                pending_clarify is not None
                and accepted_task_input.kind == INPUT_REPLY
            ):
                # Consume the question on THIS checkpoint write and carry
                # the original task question into the new turn's message.
                # Decided BEFORE the receipt exists, so a refused reply
                # leaves no begun turn behind.
                committed_runtime = _decide_continued_reply(
                    runtime_state, accepted_task_input
                )
                turn_question = _clarify_reply_turn_question(
                    pending_clarify, accepted_task_input, committed_runtime
                )
            receipts.begin(
                thread_id=thread_id,
                turn_id=run_id,
                # Task Runtime's own version, not a count this door keeps:
                # the receipt projects it and Task Runtime stays the one
                # authority for which input was accepted.
                accepted_input_version=accepted_task_input.input_version,
            )
            config["configurable"]["accepted_task_input"] = (
                accepted_task_input.to_state_dict()
            )
            if body.restore_archive_id:
                # The graph node reads the archive from run config, so an
                # explicit restore reaches the same reconciliation boundary
                # an ordinary message does.
                config["configurable"]["restore_archive_id"] = body.restore_archive_id
            input_state = reset_input_state(
                turn_question,
                attachments=resolved_attachments,
            )
            input_state["task_runtime"] = committed_runtime.to_state_dict()
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
                """Encode this run's events as the SSE wire.

                The run itself — the graph consumption, the stop token, the
                iterator release and the one turn row — belongs to the
                executor, so a RunRecord is best-effort by construction: if the
                thread was deleted mid-stream the row is skipped rather than
                resurrected (a row for a deleted thread is never wanted) and a
                single refuse status says why.
                """
                yield from _sse_frames(
                    executor.send(
                        RunRequest(
                            thread_id=thread_id,
                            run_id=run_id,
                            # The turn's ONE question, exactly what the
                            # checkpoint input state carries: a reply turn
                            # persists the composed original+answer text,
                            # never the raw answer (or "" for a click).
                            question=turn_question,
                            input_value=input_state,
                            config=config,
                            thinking_type=thinking_type,
                            admission=admission,
                            stop_token=stop_token,
                        )
                    )
                )

            # The stop token is minted AT THE DOOR, before the first byte,
            # and handed to the request. Minted here it exists before the
            # stream starts, so a client that disconnects between the
            # headers and the first frame still has something to stop;
            # minted inside the executor it would not. The executor
            # reuses this exact token rather than beginning a second one.
            stop_token: StopToken = run_control.begin(
                thread_id, run_id, admission=admission
            )
            # Dream runs AFTER the stream is exhausted, never inside a graph
            # node (CONTEXT.md:46); see ``_register_dream_background``.
            _register_dream_background(background, memory_port, thread_id)
            return _ClaimedStreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
                admission=admission,
                on_disconnect=_disconnect_stop(stop_token),
                abandonment=_abandoned_stream(
                    receipts, thread_id=thread_id, turn_id=run_id
                ),
            )
        except BaseException:
            # Every refusal between here and the stream is a claim nobody
            # will ever release: no run started, so no ``finally`` frees it,
            # and the thread would refuse every later send forever.
            admission.release()
            raise

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
        :attr:`~fin.host.attachment.attachment_budgets.AttachmentBudgets.max_upload_bytes`,
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
                return cast(dict, JSONResponse(
                    status_code=404, content={"error": "thread_not_found"}
                ))
            if _declared_upload_over(request, budgets.max_upload_bytes):
                return cast(dict, JSONResponse(
                    status_code=413, content={"error": "attachment_too_large"}
                ))
            body = await _read_bounded_body(
                request, budgets.max_upload_bytes
            )
            if body is None:
                return cast(dict, JSONResponse(
                    status_code=413, content={"error": "attachment_too_large"}
                ))
            if not body:
                return cast(dict, JSONResponse(
                    status_code=400, content={"error": "empty_attachment"}
                ))
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
                return cast(dict, JSONResponse(
                    status_code=500,
                    content={"error": "attachment_store_failed"},
                ))
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
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
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
            return cast(dict, refusal)
        if store.get_thread(thread_id) is None:
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
        record = attachments.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            return cast(dict, JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            ))
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
            return cast(dict, refusal)
        if store.get_thread(thread_id) is None:
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
        record = attachments.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            return cast(dict, JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            ))
        try:
            request = attachments.retry_preparation(thread_id, attachment_id)
            if request.state == "unknown":
                # EXIT: the row lost a deletion race between the guard
                # above and the ask; the identity answer is the same
                # one every other door gives.
                return cast(dict, JSONResponse(
                    status_code=400, content={"error": "attachment_not_found"}
                ))
            fresh = attachments.status(attachment_id)
            if fresh is None:
                return cast(dict, JSONResponse(
                    status_code=400, content={"error": "attachment_not_found"}
                ))
            return _attachment_wire_record(fresh)
        except AttachmentThreadGoneError:
            # EXIT: the thread was deleted between the check above and the
            # write; the attachment is gone, not the store broken.
            return cast(dict, JSONResponse(
                status_code=400, content={"error": "attachment_not_found"}
            ))
        except AttachmentStoreError:
            logger.exception(
                "retry_thread_attachment: state write failed for attachment_id=%s",
                attachment_id,
            )
            return cast(dict, JSONResponse(
                status_code=500, content={"error": "attachment_store_failed"}
            ))

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
            return cast(dict, refusal)
        if store.get_thread(thread_id) is None:
            return cast(dict, JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            ))
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
            return cast(dict, JSONResponse(
                status_code=400, content={"error": "unknown_locator"}
            ))
        except AttachmentCleanupError:
            logger.exception(
                "delete_thread_attachment: cleanup failed for thread_id=%s "
                "attachment_id=%s",
                thread_id,
                attachment_id,
            )
            return cast(dict, JSONResponse(
                status_code=500, content={"error": "attachment_cleanup_failed"}
            ))
        except AttachmentStoreError:
            logger.exception(
                "delete_thread_attachment: state write failed for "
                "thread_id=%s attachment_id=%s",
                thread_id,
                attachment_id,
            )
            return cast(dict, JSONResponse(
                status_code=500, content={"error": "attachment_store_failed"}
            ))
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
        receipts: TurnReceiptProjector = Depends(get_turn_receipts),
        attachments: AttachmentPreparationService = Depends(
            get_attachment_service
        ),
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
        # exception handler emit 409 with application/json. The same
        # serialized claim the send door takes, so a resume cannot be
        # admitted where a send would be refused.
        executor = RunExecutor(
            graph=graph,
            store=store,
            run_control=run_control,
            receipts=receipts,
            publication_warnings=_publication_warnings(attachments),
        )
        admission = run_control.admit(thread_id, stop_pending=True)
        try:
            # As on the send door: the graph lock still reports runs this
            # registry did not admit (the auto-resumer, compaction), and
            # the claim is what makes the decision below indivisible.
            executor.assert_thread_occupancy(thread_id)
            return _resume_route(
                thread_id=thread_id,
                background=background,
                graph=graph,
                store=store,
                memory_port=memory_port,
                workspace_root=workspace_root,
                body=body,
                executor=executor,
                admission=admission,
                receipts=receipts,
            )
        except BaseException:
            admission.release()
            raise

    def _resume_route(
        *,
        thread_id: str,
        background: BackgroundTasks,
        graph: Any,
        store: ThreadStore,
        memory_port: Any,
        workspace_root: Path,
        body: ResumeRequest | None,
        executor: RunExecutor,
        admission: RunAdmission,
        receipts: TurnReceiptProjector,
    ) -> Response:
        """The resumed run itself, under the claim the route already took."""

        def refuse(response: Response) -> Response:
            """Answer without a run — so nothing else will release the claim."""
            admission.release()
            return response

        # search-mode host T2: a pick answers a PENDING window — with
        # none open it is a typed 400 (never a hijacked crash resume,
        # never a silently dropped field). A body-less resume is the
        # pre-T2 crash path unchanged.
        pick = body.pick if body is not None else None
        document_choice_pending = False
        if pick is not None:
            pending_choice = (
                graph.interrupt_pending(thread_id)
                if hasattr(graph, "interrupt_pending")
                else None
            )
            if pending_choice is None:
                return refuse(
                    JSONResponse(
                        status_code=400, content={"error": "choice_not_pending"}
                    )
                )
            document_choice_pending = (
                isinstance(pending_choice, Mapping)
                and pending_choice.get("kind") == DOCUMENT_CHOICE_INTERRUPT_KIND
            )

        # Nothing to resume -> 204 No Content. A terminal checkpoint
        # (``pending_nodes() == ()``) means the prior run finished;
        # LangGraph's ``stream(None)`` would be a no-op and the spec
        # forbids that path. Also covers a never-sent thread (no
        # checkpoint at all).
        if not hasattr(graph, "pending_nodes") or not graph.pending_nodes(thread_id):
            return refuse(Response(status_code=204))

        # Reuse the id attached to the recoverable checkpoint. A resume
        # continues the in-flight run; minting here would split the SSE
        # turn from the canonical checkpoint identity.
        run_id = (
            graph.checkpoint_run_id(thread_id)
            if hasattr(graph, "checkpoint_run_id")
            else None
        )
        if not run_id:
            return refuse(
                JSONResponse(
                    status_code=500,
                    content={"error": "resume_run_id_unavailable"},
                )
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
        #
        # QA-chain T9: on the DOCUMENT CHOICE interrupt the resume value
        # carries the pick AND the operator's ``other`` free text (contract
        # line 90) — the paused node accepts the reply from the resume
        # value alone, so this route would otherwise discard the rephrase
        # exactly as the send door did. A 检索档 pick has no question and
        # no free text, so it keeps the plain string it always sent.
        resume_input: Any = (
            Command(
                resume=document_choice_resume_value(
                    pick, body.free_text if body is not None else None
                )
            )
            if document_choice_pending and pick is not None
            else (Command(resume=pick) if pick is not None else None)
        )

        resume_token = run_control.begin(
            thread_id, run_id, admission=admission
        )

        def event_stream() -> Iterator[str]:
            """Yield the resumed remainder on the SAME delta wire.

            Drives :meth:`RunExecutor.resume` — the one resume path the
            attachment auto-resumer also uses — so the two surfaces
            cannot drift on persistence or terminal frames. This route
            builds its own executor rather than calling
            :func:`_resume_run_stream`, because it already holds the
            admission claim and the claim has to reach the executor that
            releases it.
            """
            yield from _sse_frames(
                executor.resume(
                    ResumeRunRequest(
                        thread_id=thread_id,
                        run_id=run_id,
                        resume_input=resume_input,
                        resume_config=resume_config,
                        admission=admission,
                        stop_token=resume_token,
                    )
                )
            )

        _register_dream_background(background, memory_port, thread_id)
        return _ClaimedStreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
            admission=admission,
            on_disconnect=_disconnect_stop(resume_token),
            abandonment=_abandoned_stream(
                receipts, thread_id=thread_id, turn_id=run_id
            ),
        )

    @router.post("/threads/{thread_id}/stop")
    def stop_thread(
        thread_id: str,
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """Stop the in-flight run on this thread (T4 workflow).

        Rejection order mirrors send / resume:

        * 404 — unknown thread metadata row;
        * 204 — no run or send/resume admission is in flight;
        * 202 ``{"stopped": true, "pending": true, "run_id": null}``
          — an admitted send/resume has not created its token yet;
        * 200 ``{"stopped": true, "run_id": ...}`` — a live token is set.

        This route deliberately does NOT touch the graph: the run owns
        the per-thread lock, so acquiring it here would deadlock the
        one moment a stop matters. It stops the current token or latches
        onto its admission and returns; the SSE generator does the ending.

        Target selection and stop are one registry operation, so a run
        cannot end between observation and cancellation and leave a stale
        snapshot to target its successor. ``user_stop`` is stated rather
        than defaulted so the receipt preserves the operator's cause.
        """
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        stop = run_control.stop_current(thread_id, reason="user_stop")
        if stop is None:
            return Response(status_code=204)
        if stop.pending:
            return JSONResponse(
                status_code=202,
                content={
                    "stopped": True,
                    "pending": True,
                    "run_id": stop.run_id,
                },
            )
        return JSONResponse(
            status_code=200,
            content={"stopped": True, "run_id": stop.run_id},
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

        Comparison exports bind to ``fin.comparison`` — the same
        envelopes the turn row persists (``TurnRow.comparisons``) and
        the SSE ``comparison`` frame carried. ``fin.host.export`` owns
        the rendering; this route owns only the HTTP shape.

        Typed failures, all with a stable ``error`` code:
        ``thread_not_found`` / ``run_not_found`` / ``export_empty``
        (404), ``unsupported_export`` (400),
        ``unsupported_comparison_schema`` / ``malformed_citation_row``
        (422 — the data is there and cannot be exported without
        losing the identity a citation owes).
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
        except (
            MalformedCitationRowError,
            UnsupportedComparisonSchemaError,
        ) as exc:
            return JSONResponse(status_code=422, content={"error": exc.code})
        except ExportError as exc:
            return JSONResponse(status_code=404, content={"error": exc.code})
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
        return cast(list[dict], JSONResponse(content=notices, headers=headers))

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

    @router.get("/corpus/overview")
    def corpus_overview() -> dict:
        """The GENERIC catalog surface: documents plus their presentation grouping.

        ``GET /api/corpus`` is deliberately NOT reused: that route is the
        FINANCIAL compatibility projection with a fixed six-key shape, and
        an ordinary document has no ``company`` / ``period`` / ``form`` at
        all (T6). This mouth carries the generic read model — one entry per
        physical row with its title, type and any PRESENT company / period
        — plus the grouping the operator actually reads: only dimensions
        with at least two distinct non-empty values, missing values under
        未分类, and a flat title list when no dimension qualifies.

        Both projections read the same table, so a corrupt table is the
        same typed ``CatalogCorruptError`` (→ 503 ``ingest_missing``) and a
        missing one is the same empty success. The grouping lives in
        :func:`fin.corpus.catalog.catalog_grouping` so this route stays a
        projection and the rule stays unit-tested beside the row contract.
        """
        entries = list_catalog_entries()
        grouping = catalog_grouping(entries)
        return {
            # The read model already omits absent company / period, so the
            # documents list is that projection verbatim: "claims no
            # company" and "company is blank" stay different facts.
            "documents": entries,
            "dimensions": grouping["dimensions"],
            "flat_titles": grouping["flat_titles"],
        }

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
        that never probed (plain ``TestClient`` tests) reports 未探测
        (``not_probed``), NOT 缺 — 缺 asserts a probe established the
        absence, and this process never looked — while the conversation
        gate stays open for that same hermetic case (see
        ``fin.host.knowledge_mcp``). The three probed states are echoed
        verbatim; ``not_probed`` is display-only, so no probe can
        publish it.

        ``knowledge_mcp`` is resolved by the ONE function
        ``fin.host.knowledge_mcp.knowledge_mcp_status_value`` owns, so
        this route cannot drift back to coercing ``None`` into 缺 and so
        a value outside the probed set still fails closed (→ 坏, logged)
        instead of being echoed unvouched.

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

        ``run_counts`` is the one run-aware field here, and it is counts
        alone: this router's own run registry, read without touching the
        graph, the MCP client or the checkpoint. A status answer that had
        to wait for any of those is the one surface that must stay
        answerable during the stall it exists to report — and a
        suspected-stuck count is only honest if answering it costs
        nothing that can itself be stuck. ``active`` counts the runs this
        process is executing, ``paused`` the ones waiting on the operator,
        and ``suspected_stuck`` the active runs whose executing phase has
        gone past its deadline plus the cleanup grace without producing
        anything. A deliberate clarification or attachment wait is never
        suspected-stuck: nobody bounds the operator's thinking time.
        """
        published = knowledge_mcp_state()
        knowledge_mcp = knowledge_mcp_status_value(published)
        counts = run_control.counts()
        return {
            "knowledge_mcp": knowledge_mcp,
            "retrieve_adapter_kind": resolve_bound_retrieve_adapter_kind(),
            "model_identity": _model_identity(),
            "corpus_filing_count": _corpus_filing_count(),
            "bound_collection_name": resolve_bound_collection_name(),
            "run_counts": {
                "active": counts.active,
                "paused": counts.paused,
                "suspected_stuck": counts.suspected_stuck,
            },
        }

    @router.post("/corpus/ingest")
    def corpus_ingest(
        request: Request,
        file: UploadFile | None = File(default=None),
        document_id: str | None = Form(default=None),
        company: str | None = Form(default=None),
        period: str | None = Form(default=None),
        doc_type: str | None = Form(default=None),
        title: str | None = Form(default=None),
        format: str | None = Form(default=None),
        service: IngestEndpoint = Depends(get_ingest_service),
    ) -> Response:
        """Corpus-ingest door: accept one upload durably and return its job.

        The ONLY product surface that takes a document into the permanent
        corpus (ADR 0047). It is **accept-and-persist**: parse, 切分 and
        push run asynchronously off the request (drained by the boot
        worker, :func:`start_ingest_worker`), so a 200 here says the upload
        is durable and queued, never that anything was ingested —
        ``accepted`` replaced the old ``ingested`` token, and the outcome
        is polled at ``GET /api/corpus/ingest/{job_id}``.

        Multipart upload; the file is the only required input. The
        document identity is derived from the trusted corpus scope and the
        content digest, so ``document_id`` is refused. ``title``,
        ``company``, ``period`` and ``doc_type`` are optional display
        metadata subject to their existing validation; without a title the
        sanitized filename basename names the document. ``format`` is an
        optional explicit declaration (otherwise the filename extension
        names the format). An old client's multipart ``generic`` field is
        ignored for compatibility.

        Accepted means every accept-time item is durable: the raw object,
        its metadata pointer, the ``pending`` job record, the
        ``(document_id, version)`` identity and the accepted-metadata
        audit row. Every validation that needs no bytes runs before the
        first write, so a refused upload leaves nothing behind; a
        persistence failure is a typed 5xx with no accepted job. The
        exception -> answer mapping lives in :func:`_ingest_error_response`
        (table-driven).
        """
        if file is None:
            return JSONResponse(
                status_code=422, content={"error": "missing_file"}
            )
        # The public door never lets a caller choose identity. The
        # module keeps a trusted explicit-ID seam for internal imports;
        # this field is refused here, before any store access.
        if document_id is not None and document_id.strip():
            return _ingest_error_response(
                IngestDocumentIdNotAllowedError(PUBLIC_DOCUMENT_ID_REFUSAL)
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
                title=title,
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
                "catalog_warning": _catalog_warning_body(status.catalog_warning),
                "catalog_warning_count": status.catalog_warning_count,
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

    @router.get(
        "/structured-evidence/invalidation-checkpoint",
        response_model=InvalidationCheckpointResponse,
    )
    def structured_invalidation_checkpoint(
        consumer_id: str,
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        """This consumer's durable position in the invalidation stream.

        The checkpoint is written by the drain worker, which runs on a
        cadence inside one process; until a reader can cross that
        boundary the only way to learn the drain's position is to be
        the drain, so a stalled drain and a drain that never started are
        the same 200 everywhere else. Read it, do not infer it.

        ``consumer_id`` is REQUIRED rather than defaulted to
        ``INVALIDATION_DRAIN_CONSUMER_ID``. That constant is a contract,
        not a configuration surface (see
        :mod:`fin.host.evidence.structured_evidence_drain_worker`), so a copy
        of it here would be a second identity that goes quietly stale
        and answers for a consumer nothing drains.

        What requiring it does NOT do is protect against a caller naming the
        wrong one: the store answers a miss with empty strings and zero, so a
        misspelled or renamed consumer gets that same "never drained" body
        back. The parameter forces the caller to state which identity it means,
        which makes a rename a mismatch somebody can see — it is not a guard.

        A lookup miss is NOT a 404 — "no checkpoint yet" is the normal
        first-drain state, and an unknown consumer is indistinguishable
        from one that has never drained, so both render the empty
        position. A blank or whitespace-only id is a client error
        (400) rather than a silent normalisation: an all-whitespace id
        trimmed to empty is an operator's typo, and answering it about
        the empty consumer would look like a real answer.

        A store that cannot be reached is the same typed
        ``persistence_unavailable`` 503 ``GET /api/threads/{thread_id}``
        answers with — never a bare 500, and never the empty position,
        which would read as "never drained" for a deployment that has
        been draining for days.
        """
        normalized_consumer_id = consumer_id.strip()
        if not normalized_consumer_id:
            return cast(dict, JSONResponse(
                status_code=400, content={"error": "invalid_consumer_id"}
            ))
        try:
            checkpoint = store.read_invalidation_checkpoint(normalized_consumer_id)
        except PersistenceUnavailableError:
            logger.warning(
                "structured_invalidation_checkpoint: thread store unreachable",
                exc_info=True,
            )
            return cast(dict, JSONResponse(
                status_code=503, content={"error": "persistence_unavailable"}
            ))
        # WHY the four fields are spelled out instead of dumped: the two
        # checkpoints are only distinguishable if a reader is told which
        # is which, and ``dataclasses.asdict`` would publish the store's
        # internal names as a wire contract without that framing.
        return {
            "consumer_id": checkpoint.consumer_id,
            "acked_checkpoint": checkpoint.acked_checkpoint,
            "applied_checkpoint": checkpoint.applied_checkpoint,
            "last_seq": checkpoint.last_seq,
        }

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
    run_control: RunControl | None = None,
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
            run_control=run_control,
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
