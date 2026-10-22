"""The one run lifecycle HTTP and background callers share.

Every answer turn used to be driven from two modules that each knew how to
consume the graph, close its iterator, register a stop token, and write the
turn row. That is four responsibilities duplicated per caller, and the
duplication is invisible until one of them drifts: a background resume that
forgets ``run_control.end`` leaves a thread that can never be stopped again.

This module is the single owner of those four things. :class:`RunExecutor`
consumes the graph, releases the iterator, holds the stop token for the whole
run, and writes exactly one terminal ``fin_run`` row. Callers hand it a
:func:`RunRequest` or :func:`ResumeRunRequest` and receive typed events; the HTTP
adapter in ``fin.host.workspace_http`` encodes those events as SSE and is the
only place that knows the wire exists. The attachment auto-resumer drives the
same executor and discards the events, which is why its resume cannot drift
from an operator's.

Why the event carries its JSON body instead of named fields: the body key
order is part of the observed wire contract (a client diffs a frame, and the
committed conformance example is compared field by field). Rebuilding a body
from fields on the far side of the seam would make the adapter the one place a
byte could quietly change, for no gain — the tag is what lets a caller narrow
an event without parsing a string, and that is what the background caller and
the tests need.

Why the executor owns persistence rather than yielding "you may persist now":
a turn that ends without its row is invisible to history, and a turn that
gets two rows shows the operator the same answer twice. The finalization
branches and the ``append_run`` calls live here together so the count cannot
disagree with the outcome.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import nullcontext
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, cast

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

from fin.host.grounded.citation_gate import HIDDEN_BOUNCE_MARKER, strip_hidden_bounce_marker
from fin.host.evidence.evidence_contract import (
    CITATION_CHANNEL_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_FIELD,
    MALFORMED_EVIDENCE_CONTRACT,
    VISUAL_CLAIM_SCHEMA,
    seed_context_from_rows,
    validate_evidence,
)
from fin.host.graph import (
    ATTACHMENT_PREPARATION_INTERRUPT_KIND,
    DOCUMENT_CHOICE_INTERRUPT_KIND,
    ConcurrentRunError,
    analysis_from_tool_message,
    comparison_from_tool_message,
)
from fin.host.named_failures import (
    TASK_ALREADY_RECONCILED,
    TASK_APPLICABILITY_UNRESOLVED,
    TASK_CAPABILITY_UNAVAILABLE,
    TASK_CAPABILITY_UNSUPPORTED,
    TASK_CHECKPOINT_FAILED,
    TASK_DEPENDENCY_FAILURE,
    TASK_EXECUTION_LIMIT,
    TASK_FAILURE_KINDS,
    TASK_HISTORY_FAILED,
    TASK_INPUT_INVALID,
    TASK_INPUT_UNRECONCILED,
    TASK_INSUFFICIENT_EVIDENCE,
    TASK_INTENT_MISSING,
    TASK_PERMISSION_DENIED,
    TASK_REPLY_EXPIRED,
    TASK_REPLY_UNKNOWN,
    TASK_RESTORE_NOT_ADOPTED,
    TASK_RESTORE_UNKNOWN,
)
from fin.host.observability import ObservabilityCollector, ObservabilityError
from fin.host.run_budgets import (
    STAGE_CLEANUP,
    STAGE_EXECUTE,
    RunBudgets,
    StageLedger,
    StageOutcome,
)
from fin.host.run_call_supervisor import (
    RunCallCancelled,
    RunCallSupervisor,
    run_call_context,
)
from fin.host.run_control import RunAdmission, RunAdmissionError, RunControl, StopToken
from fin.host.span_seams import stream_in_turn_segment
from fin.host.run_payload import RunResponsePayload, _run_to_response_payload
from fin.host.stream_map import (
    InlineFilterState,
    ThinkingClock,
    format_tool_summary,
    reasoning_delta_from_chunk,
    split_stream_delta,
    strip_thinking_from_answer,
)
from fin.host.task.task_runtime_types import TaskRuntimeState
from fin.host.thread_store import RunRecord, ThreadNotFoundError, ThreadStore
from fin.host.turn_receipt_store import CancellationReason, TechnicalResult
from fin.host.turn_receipts import (
    TurnReceiptProjector,
    cancellation_for,
    failure_token,
)
from fin.host.evidence.visual_evidence import VISUAL_CLAIM_FIELDS


logger = logging.getLogger(__name__)


class RunEventKind(str, Enum):
    """The channels one run speaks on, and the one non-wire marker.

    Every member except ``TERMINAL`` names an SSE event; the value is that
    name, so the adapter maps a kind to a frame without a second table. A
    ``TERMINAL`` event carries the run's final projection instead of a body:
    deciding how a finished turn is written to the wire is the adapter's
    job, while deciding that the turn ended is this module's.
    """

    STATUS = "status"
    THINKING = "thinking"
    ANSWER = "answer"
    CITATIONS = "citations"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMPARISON = "comparison"
    ANALYSIS = "analysis"
    CHOICE_WINDOW = "choice_window"
    DOCUMENT_CHOICE = "document_choice"
    ATTACHMENT_WAIT = "attachment_wait"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class RunTerminal:
    """How a finished turn presents itself, before any encoding.

    ``visual_claims`` is already normalised (the executor needs the same rows
    for the persisted ``fin_run`` column), so the adapter must not normalise
    it a second time — a second pass would be a second chance to disagree
    with the row.
    """

    run_id: str
    payload: RunResponsePayload
    claim_rows: list[dict[str, Any]]
    claim_refusals: list[dict[str, Any]]
    evidence: Any
    semantic_outcome: str | None
    thinking_type: str | None
    thinking_ms: int
    answer_reset: bool


@dataclass(frozen=True)
class RunEvent:
    """One emission of one run.

    ``data`` is the channel body for every kind but ``TERMINAL``, which
    carries a :class:`RunTerminal`. It is typed as ``Any`` on purpose: the
    bodies are per-emission JSON projections whose shape the producer owns,
    and ``TERMINAL``'s payload is a different type again — one field, two
    kinds, both narrowed by ``kind``.
    """

    kind: RunEventKind
    data: Any


class RunTechnicalResult(str, Enum):
    """How a run ended, in the vocabulary the receipt projection uses.

    ``None`` on :class:`RunOutcome` means the run established no technical
    result: it paused on a window, or its stream produced nothing at all.
    Neither is a completed turn, so neither may claim ``done``.
    """

    DONE = "done"
    STOPPED = "stopped"
    TIMEOUT = "timeout"
    FAILED = "failed"


# The kind a mid-flight failure reports when the classification is a timeout.
# The stream taxonomy is a closed enum of its own; the technical result
# reuses its ``timeout`` token rather than inventing a fourth vocabulary.
_TIMEOUT_CLASSIFICATION: Final[str] = "timeout"


@dataclass(frozen=True)
class RunOutcome:
    """What one run hands back when its event stream ends.

    ``technical_result`` is the one answer to "how did this end"; callers
    branch on it instead of re-deriving an end from ``final_state`` and
    ``refuse_reason`` together. It is ``None`` while nothing terminal was
    established — a paused choice window, an attachment gate, or a stream
    that produced no state and raised nothing.

    ``final_state`` is the last ``values`` frame (``None`` when the stream
    produced nothing). ``thinking_ms`` is the run's total streamed-thinking
    duration, persisted on the ``RunRecord`` and echoed on the terminal
    frame so the SPA's post-run collapse label matches the stored row.

    ``refuse_reason`` / ``refuse_copy`` / ``refuse_kind`` are set ONLY when
    the run raised mid-flight: the engine already emitted the terminal
    refuse events itself, and the executor persists them so the failed
    exchange stays in the turn table instead of being wiped by the SPA's
    ``openThread`` reconcile. ``refuse_kind`` is the closed stream-error
    taxonomy (auth / network / timeout / rate_limit / not_found / server /
    unknown, or a named task-runtime failure token) and rides both the status
    frame and the persisted row.

    ``answer_reset`` marks a terminal reset for an answer already visible to
    the client. Bounced draft prose is buffered until admission, so a hidden
    draft cannot require an empty terminal answer to clear it.

    ``choice_window`` / ``attachment_wait`` are the two pause payloads: the
    run is still in flight on its checkpoint and writes no row until the
    resumed remainder finishes.

    ``stage_metrics`` is what the run's own clock observed about the two
    stages the executor owns — the streamed execution and the release that
    follows it. It is a property of this object and never of an event: the
    receipt owner projects it, and the wire does not carry it.
    """

    thread_id: str
    run_id: str
    technical_result: RunTechnicalResult | None
    final_state: dict[str, Any] | None
    thinking_ms: int
    refuse_reason: str | None = None
    refuse_copy: str | None = None
    refuse_kind: str | None = None
    answer_reset: bool = False
    choice_window: dict[str, Any] | None = None
    document_choice: dict[str, Any] | None = None
    attachment_wait: dict[str, Any] | None = None
    stage_metrics: tuple[StageOutcome, ...] = ()
    #: The model calls this stretch watched go past, by the receipt's
    #: breakdown keys. It is what makes ``model`` an inclusive total of
    #: invocations rather than a restatement of the streamed stretch, and
    #: it rides on this object for the same reason ``stage_metrics``
    #: does: it belongs to the run's observation, never to the wire.
    model_calls: dict[str, int] = field(default_factory=dict)
    cancellation_reason: CancellationReason | None = None


# Graph nodes whose model deltas are the operator-visible reply. ``model`` is
# the research leaf; the three Soft-Track leaves author their reply the same
# way, so their tokens paint the live turn too. Every other node stays off
# the wire — notably ``classify``, whose LLM tier streams routing JSON that
# is not prose.
_ANSWER_AUTHORING_NODES: frozenset[str] = frozenset(
    {"model", "dialogue", "clarify", "off_topic"}
)

#: The node whose model calls reconcile this turn's accepted input against
#: the current task. Counted apart from the answer author's calls because it
#: is a separate obligation on the same model, not a step toward an answer.
_RUNTIME_RECONCILIATION_NODE: Final[str] = "reconcile_task"


def _model_call_bucket(node: Any, message: Any) -> str:
    """Which of the receipt's model breakdowns one invocation belongs to.

    The graph reports a chat-model invocation the same way everywhere:
    one aggregated ``AIMessage`` at the end of the call, tagged with the
    node that made it. The node therefore IS the classification, and a
    call that came back asking for tools is counted as tool planning
    rather than as an answer synthesis, so the two never describe the
    same invocation twice.

    Everything else is ``other_model``: a model call that is neither the
    reconciliation nor the answer author still counts once and lands in
    the residual bucket rather than being dropped. An untagged call is
    counted there too — the inclusive total is every invocation the
    boundary saw, and a call nothing could attribute is still a call.
    """
    if node == _RUNTIME_RECONCILIATION_NODE:
        return "runtime_reconciliation"
    if node == "model":
        if getattr(message, "tool_calls", None):
            return "tool_planning"
        return "grounded_decision"
    return "other_model"


# T4 workflow: the ordinary-person copy a stopped run leaves on its
# turn row. A stop is the operator's own action, so the copy says what
# happened and what is possible next — never an error sentence.
STOPPED_ASSISTANT_MESSAGE: Final[str] = "本轮已停止。可以直接再发一条。"

# ``RunRecord.status`` is a closed enum (``done`` / ``refuse`` /
# ``step_cap``); a stop is not a completed answer, so it lands on ``refuse``
# with ``status_reason="stopped"`` as the discriminator the SPA reads. Adding
# a fourth status would ripple through the gate, the ask payload, and the
# persisted enum for one presentation difference.
STOPPED_STATUS_REASON: Final[str] = "stopped"

THREAD_DELETED_IN_FLIGHT_COPY: Final[str] = (
    "Thread was deleted while the run was in flight."
)
THREAD_DELETED_RESUMING_COPY: Final[str] = (
    "Thread was deleted while the run was resuming."
)


# ----- mid-flight stream exception classification + sanitization -----
#
# Why classify here: every model call ultimately routes through ``openai``'s
# SDK error hierarchy (or ``langchain_openai``'s re-exports of the same
# classes). Surfacing a single English sentence to the operator hides which
# axis to act on — auth, network, rate-limit, etc. — so the SPA cannot render
# actionable guidance. We dispatch on the exception type and pair it with a
# short Chinese ordinary-person copy the SPA can render verbatim.
#
# Why a sanitize step: the refuse ``reason`` rides the wire (server logs +
# persisted RunRecord) and historically embedded ``str(exc)`` — which carries
# the provider's full error text, often including the API base URL with the
# key, the raw ``Authorization: Bearer ...`` header, or a long opaque token. A
# single regex pass drops anything that looks sensitive before the reason
# reaches the wire. Defense in depth: the operator copy is hand-written, so
# even an unsanitized leak can only land in the ``reason`` field, never in the
# user-facing bubble.


# Classification → user-facing Chinese copy (ordinary-person, no provider
# internals). Single source of truth so the SSE frame and the persisted
# ``RunRecord.assistant_message`` always agree.
_STREAM_FAILURE_USER_COPY: Final[dict[str, str]] = {
    "auth": "鉴权失败：服务返回 401。请检查 ~/.fin/settings.yaml 里的 llm.apiKey（或进程环境里的 FIN_API_KEY）是否设置且有效。",
    "network": "网络错误：无法连接到模型服务。请检查网络或代理（FIN_BASE_URL）。",
    "timeout": "模型调用超时（默认 120s）。可重试，或调高 ChatOpenAI timeout。",
    "rate_limit": "模型服务限流（429）。请稍后重试。",
    "not_found": "模型或接口路径不存在（404）。检查 FIN_MODEL 和 FIN_BASE_URL 是否拼写正确。",
    "server": "模型服务内部错误（5xx）。请稍后重试。",
    "unknown": "未预期的错误。请把 server 日志贴出来排查。",
}

# Question/task runtime T2: ordinary-person copy for a refused task
# reconciliation. Grouped by the FACT each failure names rather than listed
# one per token — the operator cannot act on most of them, so telling them
# which one it was would be noise. None of these asks the user a question:
# an operational failure is a system outcome, never a clarification.
_TASK_REFUSAL_USER_COPY: Final[dict[str, str]] = {
    TASK_INPUT_INVALID: "这次输入无法处理，当前任务未改变。请重试。",
    TASK_INPUT_UNRECONCILED: "这次输入尚未确认，当前任务未改变。请重试。",
    TASK_ALREADY_RECONCILED: "这次输入已经处理过了。",
    TASK_PERMISSION_DENIED: "当前没有执行这项操作的权限。",
    TASK_DEPENDENCY_FAILURE: "依赖服务出错，当前任务未改变。请稍后重试。",
    TASK_CAPABILITY_UNAVAILABLE: "所需能力当前不可用。",
    TASK_CAPABILITY_UNSUPPORTED: "当前来源版本不支持这项操作。",
    TASK_INSUFFICIENT_EVIDENCE: "证据不足，任务保持进行中。",
    TASK_APPLICABILITY_UNRESOLVED: "适用条件尚未确认，任务保持进行中。",
    TASK_INTENT_MISSING: "缺少可确认的用户意图，任务未改变。",
    TASK_EXECUTION_LIMIT: "已达到执行上限，任务保持进行中。",
    TASK_CHECKPOINT_FAILED: "保存进度失败，当前任务保持上一个一致状态。",
    TASK_HISTORY_FAILED: "读取历史失败，当前任务保持上一个一致状态。",
    TASK_REPLY_EXPIRED: "这条回复对应的提问已经过期，当前任务未改变。",
    TASK_REPLY_UNKNOWN: "当前没有待回答的提问。",
    TASK_RESTORE_UNKNOWN: "找不到对应的历史任务，当前任务未改变。",
    TASK_RESTORE_NOT_ADOPTED: "按当前条件不能恢复该历史任务，当前任务未改变。",
}


def _classify_exception_type(exc: BaseException) -> str:
    """Map an exception instance to a kind string.

    Dispatch by exact ``openai`` SDK class first, then by class name
    substring (defensive against ``langchain_openai``'s wrappers that
    re-export the same exceptions). Returns one of the keys in
    ``_STREAM_FAILURE_USER_COPY``; falls back to ``"unknown"`` for
    anything that does not match.
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
        if isinstance(
            exc, (openai.AuthenticationError, openai.PermissionDeniedError)
        ):
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
    # third-party SDK that re-exports an exception under a different
    # module path. Class-name substring match is enough to disambiguate
    # the seven kinds without overreaching.
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
    # ``combined`` kept available for future module-scoped checks; the
    # local var silences the linter without altering dispatch.
    _ = combined
    return "unknown"


def classify_stream_exception(exc: BaseException) -> tuple[str, str]:
    """Return ``(kind, user_copy)`` for a mid-flight stream exception.

    ``kind`` is one of ``auth`` / ``network`` / ``timeout`` /
    ``rate_limit`` / ``not_found`` / ``server`` / ``unknown`` — a closed
    enum the SPA keys labels off of (no free text). ``user_copy`` is short
    Chinese actionable text the SPA shows verbatim; never blank.
    """
    kind = _classify_exception_type(exc)
    return kind, _STREAM_FAILURE_USER_COPY[kind]


def _technical_result_for_failure(kind: str) -> RunTechnicalResult:
    """The technical result one mid-flight classification selects.

    A timeout is the only classification that names its own end; every other
    failure is a failure, and collapsing the timeout into ``failed`` is what
    makes a deadline indistinguishable from a crash downstream.
    """
    if kind == _TIMEOUT_CLASSIFICATION:
        return RunTechnicalResult.TIMEOUT
    return RunTechnicalResult.FAILED


# Patterns that look sensitive: an ``Authorization: Bearer ...`` header;
# ``key=...`` / ``token=...`` query-string style fragments; full URLs whose
# path or query string embeds what looks like a key (long alphanumeric token,
# hex blob). Each match collapses to a short tag so the operator still sees
# "auth failed against <URL redacted>" rather than losing all context.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{20,})"),
    # ``key=`` / ``token=`` / ``api_key=`` query-string fragments;
    # capture the assignment through the value delimiter.
    re.compile(
        r"(?i)\b(api[_-]?key|token|access[_-]?token|secret)\s*[=:]\s*[^\s,;]+"
    ),
    # Long hex / base64url blobs (>=32 chars) — high-entropy tokens the
    # operator never needs to see on the wire.
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def _sanitize_reason_text(text: str, max_len: int = 200) -> str:
    """Redact sensitive substrings and cap length on a wire string.

    Why a small standalone helper: the refuse ``reason`` is the one field on
    the wire that may carry provider-internal text. The contract is
    "single-line, <=max_len chars, no tokens, no headers". One choke point
    keeps every future caller honest.
    """
    if not text:
        return ""
    out = text
    for pattern in _SENSITIVE_PATTERNS:
        out = pattern.sub("[redacted]", out)
    # Collapse whitespace so a multi-line provider error still reads as one
    # line on the wire (SSE is line-oriented).
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_len:
        out = out[: max_len - 3] + "..."
    return out


def _tool_result_events(
    message: ToolMessage,
    pending_args: dict[str, dict[str, Any]] | None = None,
) -> Iterator[RunEvent]:
    """Yield the ``tool_result`` (plus ``comparison``/``analysis``) events
    for one ``ToolMessage``.

    Split from :func:`_node_frame_events` to keep each function under the
    complexity budget: the args-consuming summary belongs to the tool
    contract, the structured envelopes to the comparison wire.
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
    # Strip the marker from the human-readable summary so a bounce template
    # embedded in tool output never escapes to the SPA (ADR 0021).
    call_args = (
        pending_args.get(tool_call_id) if pending_args is not None else None
    )
    if pending_args is not None and tool_call_id:
        pending_args.pop(tool_call_id, None)
    yield RunEvent(
        RunEventKind.TOOL_RESULT,
        {
            "name": tool_name,
            "summary": strip_hidden_bounce_marker(
                format_tool_summary(tool_name, result=parsed, args=call_args)
            ),
            "tool_call_id": tool_call_id,
        },
    )
    # The comparison table rides its own channel so the SPA does not have to
    # re-parse ``tool_result``. Reuses the graph's extractor so the wire
    # entry and the persisted ``RunRecord.comparisons`` entry are
    # byte-identical.
    comparison = comparison_from_tool_message(message)
    if comparison is not None:
        yield RunEvent(RunEventKind.COMPARISON, comparison)
    analysis = analysis_from_tool_message(message)
    if analysis is not None:
        yield RunEvent(RunEventKind.ANALYSIS, analysis)


def _node_frame_events(
    message: Any,
    pending_args: dict[str, dict[str, Any]] | None = None,
) -> Iterator[RunEvent]:
    """Yield the node-complete events for a message.

    Node-complete events are synthesized from ``values``-mode emissions
    (each arrives AFTER the node's checkpoint write):

    * ``AIMessage``  -> one ``tool_call`` event per parsed tool call
      (args are complete and parsed here — chunk-level
      ``tool_call_chunks`` would emit partial args). Thinking is NOT emitted
      here: the split-channel deltas already streamed via ``messages`` mode
      (whatever the assembled message carries on
      ``additional_kwargs.reasoning_details`` arrived chunk by chunk first).
    * ``ToolMessage`` -> one ``tool_result`` event with the human-readable
      summary (never the raw JSON payload), plus — for ``compare_facts``
      only — one ``comparison`` event carrying the ``fin.comparison``
      envelope. That envelope is the sole structured payload on this wire:
      it is tool-computed data, not model prose, and the live view renders it
      as a table before the answer arrives. A typed EXIT (no columns) emits
      no event.
    * HumanMessage / SystemMessage / others -> no event. The user side has
      its own UI; SystemMessages are the gate's hidden bounce templates and
      MUST NOT appear on the wire (ADR 0021).
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
            # Register id -> args so the matching ToolMessage event can
            # summarize with the same args the persist path uses (live
            # one-liners match GET history).
            if pending_args is not None and call_id:
                pending_args[str(call_id)] = dict(args)
            yield RunEvent(
                RunEventKind.TOOL_CALL,
                {
                    "name": call.get("name", ""),
                    "args": args,
                    "tool_call_id": call_id,
                },
            )
        return
    if isinstance(message, ToolMessage):
        yield from _tool_result_events(message, pending_args)


def _close_stages(
    stages: StageLedger,
    *,
    interrupted: bool,
    stream: Any,
) -> None:
    """Close the graph iterator and time the release against its grace.

    The close is what returns the per-thread ownership, so it is not
    abandoned when it runs long: work that survives the grace stays
    supervised rather than being released early, and the ledger records the
    stage as incomplete so nobody reads the wait as a finished cleanup.
    """
    started = time.perf_counter()
    close = getattr(stream, "close", None)
    if callable(close):
        close()
    stages.record_cleanup(time.perf_counter() - started)
    stages.finish_execute(interrupted=interrupted)


def _split_admitted_answer(answer: str, source_chunk_lengths: list[int]) -> list[str]:
    """Split the admitted answer proportionally to its model chunk lengths."""
    if not answer:
        return []
    lengths = [length for length in source_chunk_lengths if length > 0]
    if not lengths:
        return [answer]
    chunk_count = min(len(lengths), len(answer))
    lengths = lengths[:chunk_count]
    total_length = sum(lengths)
    chunks: list[str] = []
    start = 0
    cumulative_length = 0
    for index in range(chunk_count):
        cumulative_length += lengths[index]
        if index == chunk_count - 1:
            end = len(answer)
        else:
            boundary = round(len(answer) * cumulative_length / total_length)
            end = max(start + 1, min(boundary, len(answer) - (chunk_count - index - 1)))
        chunks.append(answer[start:end])
        start = end
    return chunks


def _stream_run_events(
    graph: Any,
    *,
    run_id: str,
    input_value: Any,
    config: dict[str, Any],
    seen_message_ids: set[str],
    thread_id: str | None = None,
    stop_token: StopToken | None = None,
    run_call_supervisor: RunCallSupervisor | None = None,
    budgets: RunBudgets | None = None,
    lifecycle: RunControl | None = None,
) -> Generator[RunEvent, None, RunOutcome]:
    """Drive one run on the combined event stream; return the outcome.

    The shared engine behind send and resume so the two surfaces cannot
    drift. On exception the generator still emits ``status`` / ``answer`` /
    ``citations`` before returning, because those three are what close a
    reader: a run that raised mid-flight must end its stream the way any
    other end does, not leave the caller holding a half-open one.

    Consumes ``graph.stream`` with ``stream_mode=["messages", "values"]``:

    * ``messages`` items arrive DURING an answer-authoring node's execution
      (``_ANSWER_AUTHORING_NODES``). ``AIMessageChunk`` reasoning and inline
      ``<thinking>`` prose stream on the thinking channel; answer prose is
      buffered until the completed graph state provides its admitted,
      projected answer. The aggregated end-of-call ``AIMessage`` (not a
      chunk) and chunks from every other node — the ``classify`` tier above
      all — are skipped.
    * ``values`` items arrive after each node completes (post checkpoint) —
      new messages map to ``tool_call`` / ``tool_result`` events,
      deduplicated by message id through the caller-owned
      ``seen_message_ids`` set (a resume seeds it with the pre-resume
      checkpoint's ids so checkpointed messages do not re-emit).

    Every emitted event ticks the :class:`ThinkingClock` so the run's total
    thinking duration is known when the stream ends. The generator's return
    value carries the final state and ``thinking_ms``; ``None`` final state
    means the stream produced nothing. On exception the generator still
    emits status / answer / citations before returning; send persists a
    refuse row, resume skips.

    ``stop_token`` is scoped to each graph ``next`` and ``close``. A stop
    cancels run-owned external calls and returns ``stopped`` without
    classifying cancellation as a provider failure. The graph iterator is
    closed before the executor releases admission.

    ``budgets`` is where the deadline is enforced, and this loop is the only
    place it can be: the executor holds the stream, so every check here is
    made between two events of the run it is already consuming — no second
    graph call, and nothing taken while the per-thread lock is held. A run
    that stops making progress past the execute budget ends as the timeout
    classification instead of consuming the turn indefinitely, and the
    release that follows is timed against its own cleanup grace.
    """
    clock = ThinkingClock()
    stages = StageLedger(budgets)
    stages.begin_execute()

    filter_state = InlineFilterState()
    # Answer prose is buffered until the graph has completed admission. The
    # count retains the model's stream shape so the admitted answer can still
    # arrive in several frames without exposing draft prose.
    answer_chunk_lengths: list[int] = []
    current_call_chunk_lengths: list[int] = []
    final_state: dict[str, Any] | None = None
    # A bounce-marker SystemMessage arms a reset for the first non-empty
    # admitted answer delta. Draft prose stays buffered until admission, so an
    # empty refusal/error/timeout answer never needs to clear hidden text.
    pending_answer_reset = False
    # tool_call_id -> args, registered when the AIMessage's ``tool_call``
    # events are emitted and consumed by the matching ``tool_result`` event
    # so its summary carries the same args the graph persist path passes
    # (live one-liners match GET history, e.g. retrieve shows its query).
    pending_args: dict[str, dict[str, Any]] = {}

    def _event(kind: RunEventKind, data: Any) -> RunEvent:
        # Thinking events open a duration segment; every other event closes
        # it — the collapse label's data.
        if kind is RunEventKind.THINKING:
            clock.tick_thinking()
        else:
            clock.tick_other()
        return RunEvent(kind, data)

    def _answer_payload(text: str) -> dict[str, Any]:
        nonlocal pending_answer_reset
        data: dict[str, Any] = {"text": text}
        if text and pending_answer_reset:
            data["reset"] = True
            pending_answer_reset = False
        return data

    stopped = False
    # Set when the execute budget was passed: the run made no progress for
    # longer than its ceiling, and it ends on the timeout classification
    # rather than staying on the turn.
    timed_out = False
    # Set when the run stopped without finishing its work — a raise or a
    # deadline. The stage's elapsed time is then a lower bound on what the
    # graph actually did, never a total.
    interrupted = False
    # The mid-flight raise's classified outcome, built after the ``finally``
    # so the cleanup stage is measured on the way out.
    raise_kind: str | None = None
    # Every model call this stretch watched go past, by breakdown. Counted
    # here rather than inferred afterwards from the step counter, which
    # counts node entries: a step-cap refusal and a catalog tool hop are
    # entries that never called the model at all.
    model_calls: dict[str, int] = {}
    raise_reason: str | None = None
    raise_copy: str | None = None
    # Set when the run paused on a choice window (the final ``values`` frame
    # carries ``__interrupt__``).
    choice_window: dict[str, Any] | None = None
    # Set when the run paused on the grounded document choice (QA-chain T9).
    document_choice: dict[str, Any] | None = None
    # Set when the run paused on the attachment preparation gate.
    attachment_wait: dict[str, Any] | None = None
    graph_stream = graph.stream(
        input_value,
        config=config,
        stream_mode=["messages", "values"],
    )
    try:
        while stop_token is None or not stop_token.is_set():
            try:
                call_context = (
                    run_call_context(stop_token, run_call_supervisor)
                    if stop_token is not None
                    else nullcontext()
                )
                with call_context:
                    mode, payload = next(graph_stream)
            except StopIteration:
                break
            except RunCallCancelled:
                if stop_token is None or not stop_token.is_set():
                    raise
                stopped = True
                break
            # Progress is marked BEFORE the stop check so the time spent
            # waiting on the operator is never read as time the run spent
            # failing to progress.
            stages.observe_progress()
            if lifecycle is not None and thread_id is not None:
                # The same instant the ledger's no-progress clock starts
                # over, so a run that is producing events is never counted
                # as stuck however long it has been going.
                lifecycle.progress(thread_id)
            if stop_token is not None and stop_token.is_set():
                stopped = True
                break
            if stages.execute_exceeded():
                timed_out = True
                interrupted = True
                break
            if mode == "messages":
                chunk, meta = payload
                # The aggregated end-of-call AIMessage is not a chunk;
                # only per-token AIMessageChunks carry new deltas. It is
                # also the ONLY thing on this stream that marks a model
                # call having happened, which is why it is counted here
                # and then dropped: the aggregated message is the
                # checkpointed proof that one call ran.
                if not isinstance(chunk, AIMessage):
                    continue
                if not isinstance(chunk, AIMessageChunk):
                    bucket = _model_call_bucket(meta.get("langgraph_node"), chunk)
                    model_calls[bucket] = model_calls.get(bucket, 0) + 1
                    if meta.get("langgraph_node") in _ANSWER_AUTHORING_NODES:
                        answer_chunk_lengths = current_call_chunk_lengths
                        current_call_chunk_lengths = []
                    continue
                if meta.get("langgraph_node") not in _ANSWER_AUTHORING_NODES:
                    continue
                reasoning = reasoning_delta_from_chunk(chunk)
                if reasoning:
                    yield _event(
                        RunEventKind.THINKING,
                        {"text": strip_hidden_bounce_marker(reasoning)},
                    )
                content = chunk.content
                if isinstance(content, str) and content:
                    split = split_stream_delta(filter_state, content)
                    filter_state = split.state
                    if split.thinking:
                        yield _event(
                            RunEventKind.THINKING, {"text": split.thinking}
                        )
                    if split.answer:
                        current_call_chunk_lengths.append(len(split.answer))
            else:
                final_state = payload
                for message in payload.get("messages", []) or []:
                    mid = getattr(message, "id", None)
                    if mid and mid in seen_message_ids:
                        continue
                    if mid:
                        seen_message_ids.add(mid)
                    if isinstance(message, SystemMessage):
                        # Only the gate's hidden bounce template (ADR 0021
                        # marker) arms pending reset. Other SystemMessages
                        # are ignored. The template itself never reaches the
                        # wire.
                        raw = message.content
                        if (
                            isinstance(raw, str)
                            and HIDDEN_BOUNCE_MARKER in raw
                        ):
                            pending_answer_reset = True
                            # The discarded model attempt must not shape the
                            # admitted answer's frame boundaries.
                            answer_chunk_lengths = []
                            current_call_chunk_lengths = []
                        continue
                    for event in _node_frame_events(message, pending_args):
                        yield _event(event.kind, event.data)
                # A paused HITL interrupt surfaces on the LAST ``values``
                # frame under the reserved ``__interrupt__`` key (LangGraph
                # contract). The choice-window payload is the event's body
                # verbatim — the host/HTTP contract the SPA paints. The
                # attachment-preparation pause shares this loop but not this
                # channel — the SPA does not answer it, so it rides a
                # distinct ``attachment_wait`` event carrying the run id the
                # auto-resumer will drive.
                for pending in payload.get("__interrupt__") or ():
                    value = getattr(pending, "value", None)
                    if not isinstance(value, dict):
                        continue
                    if value.get("kind") == "choice_window":
                        choice_window = value
                        yield _event(RunEventKind.CHOICE_WINDOW, value)
                    elif (
                        value.get("kind")
                        == DOCUMENT_CHOICE_INTERRUPT_KIND
                    ):
                        # QA-chain T9: the grounded document choice. Its
                        # payload REFERENCES the committed question — the
                        # frame carries the same frozen field spellings
                        # ``pending_question`` uses, so the SPA renders both
                        # surfaces from one shape and the pause carries no
                        # second candidate list.
                        document_choice = value
                        yield _event(RunEventKind.DOCUMENT_CHOICE, value)
                    elif (
                        value.get("kind")
                        == ATTACHMENT_PREPARATION_INTERRUPT_KIND
                    ):
                        attachment_wait = value
                        yield _event(
                            RunEventKind.ATTACHMENT_WAIT,
                            {"run_id": run_id, **value},
                        )

        if stop_token is not None and stop_token.is_set():
            stopped = True

        # Finalize inline thinking tags after the graph has ended. Answer
        # prose remains buffered until the completed state says it passed
        # admission; a stop, deadline, pause, refusal or exception cannot
        # publish the model's draft.
        if not stopped and not timed_out:
            tail = split_stream_delta(filter_state, "", final=True)
            if tail.thinking:
                yield _event(RunEventKind.THINKING, {"text": tail.thinking})
            if tail.answer:
                answer_chunk_lengths.extend(current_call_chunk_lengths)
                current_call_chunk_lengths = []
                answer_chunk_lengths.append(len(tail.answer))
            if final_state is not None:
                final_payload = _run_to_response_payload(final_state)
                admitted_answer = ""
                if final_payload["status"] == "done":
                    admitted_answer = final_payload["answer"]
                elif (
                    document_choice is not None
                    and isinstance(final_state.get("grounded_decision"), dict)
                    and final_state["grounded_decision"].get("outcome") == "choice"
                ):
                    # The explicit choice admission commits only the parsed
                    # prose before its candidate terminator. Reuse the shared
                    # projection to remove that payload and any unsafe labels.
                    messages = final_state.get("messages") or []
                    last_ai = next(
                        (
                            message
                            for message in reversed(messages)
                            if isinstance(message, AIMessage)
                        ),
                        None,
                    )
                    raw_content = getattr(last_ai, "content", "")
                    if isinstance(raw_content, str):
                        raw_answer = strip_thinking_from_answer(raw_content)
                    elif isinstance(raw_content, list):
                        raw_answer = strip_thinking_from_answer(
                            "".join(
                                block.get("text", "")
                                if isinstance(block, dict)
                                else str(block)
                                for block in raw_content
                            )
                        )
                    else:
                        raw_answer = ""
                    admitted_answer = _run_to_response_payload(
                        {
                            **final_state,
                            "status": "done",
                            "status_reason": "done",
                            "answer": raw_answer,
                        }
                    )["answer"]
                if admitted_answer:
                    for answer_piece in _split_admitted_answer(
                        admitted_answer, answer_chunk_lengths
                    ):
                        yield _event(
                            RunEventKind.ANSWER,
                            _answer_payload(answer_piece),
                        )
    except Exception as exc:
        # Mid-flight failure (model raise, node crash): without this
        # boundary the exception escapes the generator, FastAPI kills the
        # SSE body before any terminal event, and the SPA's reader hangs
        # waiting for ``citations`` (terminal statuses carry explicit copy —
        # never swallowed blanks).
        #
        # Classify the exception so the SPA can render an actionable label
        # instead of one opaque English sentence. The classification ``kind``
        # is a closed enum (auth / network / timeout / rate_limit / not_found
        # / server / unknown); the user-facing copy is hand-written Chinese
        # keyed off that kind. The exception type name rides ``reason`` for
        # operator debuggability, with ``str(exc)`` appended only AFTER the
        # sanitize pass — provider error text commonly embeds the auth
        # header, the key, or a long opaque token, none of which belong on
        # the wire.
        kind, user_copy = classify_stream_exception(exc)
        raw_reason = f"stream raised mid-flight: {type(exc).__name__}: {exc}"
        sanitized = _sanitize_reason_text(raw_reason)
        reason = (
            f"{sanitized} (kind={kind})"
            if sanitized
            else f"stream raised mid-flight: {type(exc).__name__} (kind={kind})"
        )
        # A refused reconciliation names a real product fact, so it must not
        # collapse into the generic "server" bucket. Its named failure IS the
        # reason, and the copy says the run could not continue — never a
        # question to the user, because an operational failure is not
        # something an answer can fix.
        refusal = getattr(exc, "kind", None)
        if isinstance(refusal, str) and refusal in TASK_FAILURE_KINDS:
            kind = refusal
            user_copy = _TASK_REFUSAL_USER_COPY.get(
                refusal, _STREAM_FAILURE_USER_COPY["server"]
            )
            reason = refusal
        logger.exception(
            "run stream raised mid-flight",
            extra={
                "run_id": run_id,
                "thread_id": thread_id,
                "kind": kind,
            },
        )
        yield RunEvent(
            RunEventKind.STATUS,
            {
                "run_id": run_id,
                "status": "refuse",
                "reason": reason,
                "assistant_message": user_copy,
                "status_classified": kind,
                "thinking_ms": clock.total_ms,
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
            },
        )
        # Flush pending bounce reset on the terminal empty answer when the
        # raise happened before any retry prose.
        yield RunEvent(
            RunEventKind.ANSWER,
            _answer_payload(strip_thinking_from_answer("")),
        )
        yield RunEvent(RunEventKind.CITATIONS, [])
        # No return here: the outcome is built once the ``finally`` below has
        # timed the release, so the cleanup stage is reported from the run
        # that actually ended rather than from the moment it raised.
        raise_kind, raise_reason, raise_copy = kind, reason, user_copy
        interrupted = True
    finally:
        # Close the graph iterator explicitly. On the stop and timeout paths
        # the ``break`` above leaves it suspended, and only ``close()`` runs
        # ``_LockedGraph.stream``'s ``finally`` — i.e. releases the per-thread
        # lock. Without it the composer would stay disabled until the
        # generator was garbage-collected.
        call_context = (
            run_call_context(stop_token, run_call_supervisor)
            if stop_token is not None
            else nullcontext()
        )
        with call_context:
            _close_stages(stages, interrupted=interrupted, stream=graph_stream)
    clock.close()
    if raise_kind is not None:
        return RunOutcome(
            thread_id=thread_id or "",
            run_id=run_id,
            technical_result=_technical_result_for_failure(raise_kind),
            final_state=None,
            thinking_ms=clock.total_ms,
            refuse_reason=raise_reason,
            refuse_copy=raise_copy,
            refuse_kind=raise_kind,
            stage_metrics=stages.outcomes(),
            model_calls=model_calls,
        )
    if timed_out:
        # The budget was passed, so the run ends the way a provider timeout
        # already ends: the same refuse triple on the same ``timeout``
        # classification, because a deadline the operator cannot see is a
        # run that simply stopped.
        reason = (
            f"{STAGE_EXECUTE} stage exceeded its "
            f"{stages.budgets.execute_stage_s:g}s budget without progress"
        )
        user_copy = _STREAM_FAILURE_USER_COPY[_TIMEOUT_CLASSIFICATION]
        logger.warning(
            "run stream exceeded its execution budget",
            extra={"run_id": run_id, "thread_id": thread_id},
        )
        yield RunEvent(
            RunEventKind.STATUS,
            {
                "run_id": run_id,
                "status": "refuse",
                "reason": reason,
                "assistant_message": user_copy,
                "status_classified": _TIMEOUT_CLASSIFICATION,
                "thinking_ms": clock.total_ms,
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
            },
        )
        yield RunEvent(
            RunEventKind.ANSWER,
            _answer_payload(strip_thinking_from_answer("")),
        )
        yield RunEvent(RunEventKind.CITATIONS, [])
        return RunOutcome(
            thread_id=thread_id or "",
            run_id=run_id,
            technical_result=RunTechnicalResult.TIMEOUT,
            final_state=None,
            thinking_ms=clock.total_ms,
            refuse_reason=reason,
            refuse_copy=user_copy,
            refuse_kind=_TIMEOUT_CLASSIFICATION,
            stage_metrics=stages.outcomes(),
            model_calls=model_calls,
        )
    return RunOutcome(
        thread_id=thread_id or "",
        run_id=run_id,
        technical_result=(
            RunTechnicalResult.STOPPED
            if stopped
            # A paused window is still in flight on its checkpoint: it has
            # established no end yet, so it may not claim ``done`` — the
            # resumed remainder is what finishes the turn.
            else None
            if choice_window is not None or attachment_wait is not None
            else RunTechnicalResult.DONE
            if final_state is not None
            # An empty stream with no raise established nothing: it is not a
            # completed turn and must not claim ``done``.
            else None
        ),
        final_state=final_state,
        thinking_ms=clock.total_ms,
        # Any pending reset belongs to a bounced draft that never reached
        # the client. The first admitted non-empty answer consumed the reset
        # above; without such an answer there is nothing visible to clear.
        answer_reset=False,
        choice_window=choice_window,
        document_choice=document_choice,
        attachment_wait=attachment_wait,
        stage_metrics=stages.outcomes(),
        model_calls=model_calls,
        cancellation_reason=(
            cast(CancellationReason, stop_token.reason)
            if stopped and stop_token is not None
            else None
        ),
    )


def _visual_claims_wire_payload(
    claims: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalise the run's visual-claim channel into the wire / row shape,
    returning ``(rows, refusals)``.

    A visual claim is NOT a citation (ADR 0089): it rides its own
    ``visual_claims`` channel and its own ``fin_run`` column, never the
    ``citations`` channel, so the quote the verifier passed and the
    provenance of a generated observation cannot be confused for each other.
    The row's key set IS :data:`VISUAL_CLAIM_FIELDS` in that fixed order plus
    the one frozen ``fin.evidence.visual_claim`` token and its
    ``schema_version`` sibling appended at the E5 versioning cutover, so
    neither the live wire nor a reload can drift from the SSOT the evidence
    token mints. Claims stored before the cutover replay byte-identically as
    legacy v0 — the token is minted on this projection, never back-filled
    into a stored row.

    A malformed live row is NAMED, never dropped: the minted candidate is
    judged by the validator home and the exact code rides the
    ``evidence_refusals`` channel beside the row's index. Only the code and
    field names are logged, never a row's content (filename / image hash are
    thread-private material).
    """
    rows: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    claim_rows = list(claims or ())
    # WHY seeded from the rows' own identities: thread trust and the stored
    # observation version are admission facts the transport cannot see; the
    # posture and its never-hash rule live with the home's
    # ``seed_context_from_rows``.
    seeded = seed_context_from_rows(visual_claim_rows=claim_rows)
    for index, row in enumerate(claim_rows):
        candidate: Any = row
        if isinstance(row, Mapping) and all(
            name in row for name in VISUAL_CLAIM_FIELDS
        ):
            # The projector is the single normaliser: onto the frozen keys IN
            # ORDER, extra keys off, then the E5 cutover mint. A row MISSING
            # any field is passed to the home verbatim so the refusal names
            # the missing keys instead of a generic one here.
            candidate = {name: row[name] for name in VISUAL_CLAIM_FIELDS}
            candidate["schema"] = VISUAL_CLAIM_SCHEMA
            candidate[EVIDENCE_SCHEMA_VERSION_FIELD] = EVIDENCE_SCHEMA_VERSION
        outcome = validate_evidence({"visual_claims": [candidate]}, seeded)
        if outcome.accepted:
            projected = {name: candidate[name] for name in VISUAL_CLAIM_FIELDS}
            projected["schema"] = VISUAL_CLAIM_SCHEMA
            projected[EVIDENCE_SCHEMA_VERSION_FIELD] = EVIDENCE_SCHEMA_VERSION
            rows.append(projected)
            continue
        code = (
            outcome.refusal.code
            if outcome.refusal is not None
            else MALFORMED_EVIDENCE_CONTRACT
        )
        logger.warning(
            "visual_claims wire: refused row %d as %s", index, code
        )
        refusals.append({"citation_index": index, "error": code})
    return rows, refusals


def stopped_run_events(run_id: str, *, thinking_ms: int) -> Iterator[RunEvent]:
    """Terminal ``status`` -> ``answer`` -> ``citations`` for a stop.

    Same fixed order (and same terminal ``citations`` event) as every other
    run end, so the SPA's reader closes on a stop exactly as it does on a
    refuse. The empty answer clears whatever partial draft was on screen when
    the operator pressed Stop.
    """
    yield RunEvent(
        RunEventKind.STATUS,
        {
            "run_id": run_id,
            "status": "refuse",
            "reason": STOPPED_STATUS_REASON,
            "assistant_message": STOPPED_ASSISTANT_MESSAGE,
            "thinking_ms": thinking_ms,
            "evidence_schema": CITATION_CHANNEL_SCHEMA,
            EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
        },
    )
    yield RunEvent(RunEventKind.ANSWER, {"text": "", "reset": True})
    yield RunEvent(RunEventKind.CITATIONS, [])


def thread_deleted_event(run_id: str, assistant_message: str) -> RunEvent:
    """The single refuse a run emits when its thread disappeared mid-flight.

    The turn still ended on a terminal refuse; this event only tells the SPA
    why there is no row to reload.
    """
    return RunEvent(
        RunEventKind.STATUS,
        {
            "run_id": run_id,
            "status": "refuse",
            "reason": "thread_not_found",
            "assistant_message": assistant_message,
            "evidence_schema": CITATION_CHANNEL_SCHEMA,
            EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
        },
    )


@dataclass(frozen=True)
class RunRequest:
    """One new question turn: what the send door already decided.

    Everything here is established before the run starts — the accepted
    input, the run-scoped reset, the config pin. The executor adds the
    execution.
    """

    thread_id: str
    run_id: str
    question: str
    input_value: Any
    config: dict[str, Any]
    # The run's thinking projection, echoed on the terminal status frame. It
    # is a run fact (the same value rides the run config), not a wire
    # choice, so it is read here rather than re-decided by the adapter.
    thinking_type: str | None = None
    # The admission the door claimed for this thread before it read any
    # thread state. Handed back so the claim outlives the header write and
    # is released when the run's stream ends — a claim released when the
    # response object is built would reopen the very window it closed.
    admission: RunAdmission | None = None
    stop_token: StopToken | None = None


@dataclass(frozen=True)
class ResumeRunRequest:
    """One continuation of an existing checkpoint run.

    ``resume_input`` is LangGraph's ``Command(resume=pick)`` for an operator
    answer, a ``Command(resume=<string>)`` for the auto-resumer (the gate
    node ignores the value, and ``Command(resume=None)`` trips an
    ``UnboundLocalError`` in this langgraph version), or ``None`` for the
    plain crash resume.
    """

    thread_id: str
    run_id: str
    resume_input: Any
    resume_config: dict[str, Any]
    # As on ``RunRequest``: the door's claim, released with the stream.
    admission: RunAdmission | None = None
    stop_token: StopToken | None = None


def ensure_thread_occupancy(graph: Any, thread_id: str) -> None:
    """Refuse a run on a thread whose previous run still holds the lock.

    Read-only and lock-free on purpose: the stop door must stay reachable
    while a run is in flight, and this probe runs before the response
    headers are flushed so the refusal is a plain 409 rather than a broken
    stream.

    A free function rather than a method because the resume door probes
    before it has anything else: two call sites, one refusal.
    """
    if hasattr(graph, "is_locked") and graph.is_locked(thread_id):
        raise ConcurrentRunError(
            f"another run is already in flight on thread_id={thread_id!r}"
        )


def ensure_no_pending_choice(graph: Any, thread_id: str) -> None:
    """Refuse a new send while a choice window is still pending.

    A paused window releases the per-thread lock, so the occupancy probe
    cannot see it; the run is still in flight on the checkpoint, waiting
    for the operator's pick. Only the send door asks this: a resume is
    exactly what answers a pending window.
    """
    if (
        hasattr(graph, "interrupt_pending")
        and graph.interrupt_pending(thread_id) is not None
    ):
        raise ConcurrentRunError(
            f"a choice window is pending on thread_id={thread_id!r}"
        )


class RunExecutor:
    """The one owner of a run's graph consumption, cleanup and finalization.

    Construct one per run-driving caller with the graph it may run on, the
    store its rows go to, and the stop registry its run must be reachable
    through. Two executors over two control registries cannot signal each
    other's runs — the isolation is in the injected registry, not in this
    class.

    ``send`` and ``resume`` are generators because the run streams: the
    caller pulls events until the turn ends, and the ``finally`` around the
    graph consumption is what releases the iterator when the caller stops
    pulling early.

    ``budgets`` is the deadline this executor holds every run to. It
    defaults to the spec's maxima rather than to no bound at all: an
    executor with no ceiling is an unbounded run waiting for the ceiling to
    be configured, and a run must be bounded before anyone remembers to ask
    for it.
    """

    def __init__(
        self,
        *,
        graph: Any,
        store: ThreadStore,
        run_control: RunControl,
        collector: ObservabilityCollector | None = None,
        budgets: RunBudgets | None = None,
        receipts: TurnReceiptProjector | None = None,
        publication_warnings: Callable[[str], int] | None = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._run_control = run_control
        self._collector = collector
        self._budgets = budgets
        self._receipts = receipts
        self._publication_warnings = publication_warnings

    # ----- admission -----

    def admit(self, thread_id: str) -> RunAdmission:
        """Claim ``thread_id`` for one run, or raise the typed refusal.

        The claim is this registry's, not the graph's lock: the graph
        takes its lock lazily on the first ``next()`` of the stream, so
        probing it lets two callers read thread state and compute an
        acceptance before either of them is actually serialized. The
        door holds this claim from its first read until the run's
        stream ends, which is what makes "read state, decide, run" one
        section instead of three racing steps.
        """
        return self._run_control.admit(thread_id)

    def assert_thread_occupancy(self, thread_id: str) -> None:
        ensure_thread_occupancy(self._graph, thread_id)

    def assert_no_pending_choice(self, thread_id: str) -> None:
        ensure_no_pending_choice(self._graph, thread_id)

    # ----- execution -----

    def _claim(self, thread_id: str, admission: RunAdmission | None) -> RunAdmission:
        """The claim this run runs under, taking one if the caller had none.

        A door that already claimed the thread hands its claim in, so the
        executor never blocks on a lock its own caller holds; a background
        caller with no door takes and releases its own, which is what
        keeps "one run per thread" true of the auto-resumer too.
        """
        return self._run_control.admit(thread_id) if admission is None else admission

    def _finish_run(
        self,
        request: RunRequest | ResumeRunRequest,
        claim: RunAdmission,
        stop_token: StopToken | None,
        outcome: RunOutcome | None,
    ) -> None:
        """Release run control and admission even if an earlier release fails."""
        try:
            if stop_token is not None:
                self._run_control.end(stop_token)
        finally:
            try:
                if (
                    outcome is not None
                    and outcome.choice_window is None
                    and outcome.document_choice is None
                    and outcome.attachment_wait is None
                ):
                    self._run_control.finished(request.thread_id, request.run_id)
            finally:
                claim.release()

    def send(self, request: RunRequest) -> Generator[RunEvent, None, RunOutcome]:
        """Run one new question turn and finalize it."""
        turn_started = time.perf_counter()
        claim = self._claim(request.thread_id, request.admission)
        stop_token: StopToken | None = None
        outcome: RunOutcome | None = None
        try:
            stop_token = request.stop_token or self._run_control.begin(
                request.thread_id, request.run_id
            )
            outcome = yield from self._drive(
                request.thread_id,
                request.run_id,
                request.input_value,
                request.config,
                stop_token=stop_token,
            )
            if outcome.technical_result is RunTechnicalResult.STOPPED:
                # A stop is a terminal turn (persisted as ``refuse`` /
                # stopped), so it counts on the observability wire too.
                self._record_terminal(
                    turn_started, "refuse", run_id=request.run_id
                )
                yield from self._persist_stopped_run(request, outcome)
                return outcome

            if outcome.choice_window is not None:
                # The run PAUSED on the choice window — the engine already
                # emitted the event. Persist NOTHING and emit no terminal
                # events: a paused run is not a finished turn (the resumed
                # remainder's completion writes the row under the SAME
                # checkpoint run_id).
                self._record_pause(
                    request.thread_id, request.run_id, "clarify", outcome
                )
                return outcome

            if outcome.document_choice is not None:
                # QA-chain T9: the run PAUSED on the grounded document
                # choice. Same pause posture as the choice window — the
                # event is out, NOTHING is persisted, and the resumed
                # remainder's completion writes the single turn row under
                # the SAME checkpoint run id. The committed question lives
                # on ``task_runtime.pending_question``; this frame only
                # REFERENCES it, never a second question authority.
                self._record_pause(
                    request.thread_id, request.run_id, "clarify", outcome
                )
                return outcome

            if outcome.attachment_wait is not None:
                # The run PAUSED on the attachment preparation gate — same
                # pause posture as the choice window: persist NOTHING and
                # close; the auto-resumer drives the SAME checkpoint run_id
                # to completion once the durable job settles.
                self._record_pause(request.thread_id, request.run_id, None, outcome)
                return outcome

            if outcome.final_state is None:
                return self._finalize_emptied_run(
                    request, outcome, turn_started
                )

            yield from self._finalize(
                request.thread_id,
                request.run_id,
                request.question,
                outcome,
                thinking_type=request.thinking_type,
                deleted_copy=THREAD_DELETED_IN_FLIGHT_COPY,
                turn_started=turn_started,
            )
            return outcome
        finally:
            self._finish_run(request, claim, stop_token, outcome)

    def resume(
        self, request: ResumeRunRequest
    ) -> Generator[RunEvent, None, RunOutcome]:
        """Continue a paused or crashed run and finalize the remainder.

        THE one resume path, so an operator resume and the attachment
        auto-resumer cannot drift: the auto-resumer consumes this to
        exhaustion in the background and DISCARDS the events — the resumed
        turn row is the half it needs, the live wire is the half only an
        HTTP caller reads.
        """
        claim = self._claim(request.thread_id, request.admission)
        stop_token: StopToken | None = None
        outcome: RunOutcome | None = None
        try:
            stop_token = request.stop_token or self._run_control.begin(
                request.thread_id, request.run_id
            )
            if self._receipts is not None:
                self._receipts.resume_started(
                    thread_id=request.thread_id, turn_id=request.run_id
                )
            self._reconcile_receipt_input_history(
                request.thread_id, request.run_id
            )
            outcome = yield from self._drive(
                request.thread_id,
                request.run_id,
                request.resume_input,
                request.resume_config,
                stop_token=stop_token,
            )
            if outcome.technical_result is RunTechnicalResult.STOPPED:
                if self._receipts is not None:
                    self._receipts.stopped(
                        thread_id=request.thread_id,
                        turn_id=request.run_id,
                        cancellation_reason=outcome.cancellation_reason,
                    )
                # No row: the original send's row already tells the story, and
                # one row per stopped Resume click would spam the turn table
                # (same policy as this path's mid-flight refuse branch).
                yield from stopped_run_events(
                    request.run_id, thinking_ms=outcome.thinking_ms
                )
                return outcome
            self._reconcile_receipt_input_history(
                request.thread_id, request.run_id
            )

            if outcome.choice_window is not None:
                # Re-draw (body-less resume onto a pending window) or an invalid
                # pick that REOPENED the window: the event is out, the run is
                # still paused, so persist nothing and close — the operator picks
                # again.
                self._record_pause(
                    request.thread_id, request.run_id, "clarify", outcome
                )
                return outcome

            if outcome.document_choice is not None:
                # The resumed remainder REPAUSED on the SAME document
                # question — an invalid, stale, foreign or duplicate pick
                # reopens the question rather than accepting it, and the
                # re-raised interrupt redraws the event. Persist nothing:
                # the run is still paused, the operator picks again, and
                # the SAME checkpoint run id carries the whole exchange.
                self._record_pause(
                    request.thread_id, request.run_id, "clarify", outcome
                )
                return outcome

            if outcome.attachment_wait is not None:
                # The resumed remainder paused on the attachment gate AGAIN
                # (e.g. a re-upload changed the content version mid-resume).
                # Same posture as the send route's pause: persist nothing and
                # close; the next settled sweep drives this run.
                self._record_pause(request.thread_id, request.run_id, None, outcome)
                return outcome

            if outcome.final_state is None:
                # Mid-flight raise on a RESUME: unlike the send path, persist
                # nothing — when the original send row exists it already tells
                # the story, and appending a refuse row per failed Resume click
                # would spam the turn table.
                return outcome

            resume_question = (
                self._graph.checkpoint_last_human_text(request.thread_id)
                if hasattr(self._graph, "checkpoint_last_human_text")
                else None
            ) or ""
            yield from self._finalize(
                request.thread_id,
                request.run_id,
                resume_question,
                outcome,
                thinking_type=None,
                deleted_copy=THREAD_DELETED_RESUMING_COPY,
            )
            return outcome
        finally:
            self._finish_run(request, claim, stop_token, outcome)

    # ----- internals -----

    def _reconcile_receipt_input_history(
        self, thread_id: str, turn_id: str
    ) -> None:
        """Calibrate the receipt from committed same-turn checkpoint inputs."""
        receipts = self._receipts
        reader = getattr(self._graph, "task_runtime_state", None)
        if receipts is None or not callable(reader):
            return
        task_state = cast(TaskRuntimeState, reader(thread_id))
        receipts.reconcile_accepted_inputs(
            thread_id=thread_id,
            turn_id=turn_id,
            input_history=task_state.input_history,
        )

    def _finalize(
        self,
        thread_id: str,
        run_id: str,
        question: str,
        outcome: RunOutcome,
        *,
        thinking_type: str | None,
        deleted_copy: str,
        turn_started: float | None = None,
    ) -> Generator[RunEvent, None, None]:
        """Persist a finished remainder and emit its one terminal event.

        A send and a resume finish a run the same way: the same payload
        projection, the same visual-claim normalisation, the same
        persisted fields, the same terminal event. Written out twice they
        are a drift waiting to happen, and the drift stays invisible until
        a lane is added to one copy only — a persisted channel the other
        never writes is exactly what a duplicate delivery then replays
        short.

        ``turn_started`` is the send door's latency clock, and ``None`` on
        the resume path because that run's collector record was already
        written by the send that started it. ``thinking_type`` is the
        send's own preference; a resumed remainder has none to carry.
        ``deleted_copy`` names which of the two surfaces lost its thread,
        so the operator sees where the turn went.
        """
        final_state = cast(dict[str, Any], outcome.final_state)
        payload = _run_to_response_payload(final_state)
        # The visual-claim channel is read straight off the final state (it
        # is NOT part of the citation payload), normalised ONCE here so the
        # persisted row and the terminal event carry the identical rows.
        # Run-scoped, copied before the next send resets the state channel.
        claim_rows, claim_refusals = _visual_claims_wire_payload(
            final_state.get("visual_claims")
        )
        if turn_started is not None:
            # The turn's terminal status (done / refuse / step_cap) and
            # wall-clock latency land on the observability collector
            # exactly where the turn row is built.
            self._record_terminal(
                turn_started, payload["status"], run_id=run_id
            )
        try:
            self._commit_turn(
                RunRecord(
                    id=run_id,
                    thread_id=thread_id,
                    question=question,
                    status=payload["status"],
                    status_reason=payload["reason"],
                    answer=payload["answer"],
                    citations=payload["citations"],
                    thinking_text=payload["thinking_text"],
                    thinking_duration_ms=outcome.thinking_ms,
                    tool_summaries=payload["tool_summaries"],
                    assistant_message=payload["assistant_message"],
                    track_decision=dict(payload.get("track_decision") or {}),
                    # The structured table the turn renders. Persisted on
                    # refuse rows too — see TurnRow.
                    comparisons=list(payload.get("comparisons") or []),
                    analysis_results=list(payload.get("analysis_results") or []),
                    # This run's retrieved / read locators, taken from the
                    # final state BEFORE the next send resets the evidence
                    # channel. On a resume they are the remainder's own; the
                    # crashed prefix's row keeps its own.
                    evidence_locators=list(payload["evidence_locators"]),
                    # This run's visual claims, their own channel (never
                    # merged into ``citations``).
                    visual_claims=claim_rows,
                    # This turn's admitted structured snapshots, read from
                    # the final state BEFORE the next send resets the
                    # additive channel.
                    structured_evidence=list(
                        final_state.get("structured_evidence") or []
                    ),
                    retrieval_window=dict(payload["retrieval_window"]),
                    semantic_outcome=final_state.get("semantic_outcome"),
                ),
                technical_result=RunTechnicalResult.DONE.value,
                semantic_result=(
                    "answered" if payload["status"] == "done" else None
                ),
                stage_metrics=outcome.stage_metrics,
                model_calls=outcome.model_calls,
            )
        except ThreadNotFoundError:
            # The thread was deleted mid-stream; do NOT resurrect metadata.
            # Emit a single error event so the SPA surfaces the state, then
            # stop. The turn still ended on a terminal refuse.
            if turn_started is not None:
                self._record_terminal(
                    turn_started, "refuse", run_id=run_id
                )
            yield thread_deleted_event(run_id, deleted_copy)
            return
        yield RunEvent(
            RunEventKind.TERMINAL,
            RunTerminal(
                run_id=run_id,
                payload=payload,
                claim_rows=claim_rows,
                claim_refusals=claim_refusals,
                evidence=final_state.get("evidence"),
                semantic_outcome=final_state.get("semantic_outcome"),
                thinking_type=thinking_type,
                thinking_ms=outcome.thinking_ms,
                answer_reset=outcome.answer_reset,
            ),
        )

    def _drive(
        self,
        thread_id: str,
        run_id: str,
        input_value: Any,
        config: dict[str, Any],
        *,
        stop_token: StopToken,
    ) -> Generator[RunEvent, None, RunOutcome]:
        """Consume the graph under one stop token.

        The dedup set is seeded with the pre-run checkpoint's message ids: a
        later run's first ``values`` frame replays the whole accumulated
        list, and prior runs' tool events must not re-emit (the crashed run's
        messages were already on the pre-crash wire).
        """
        seen_message_ids: set[str] = set()
        if hasattr(self._graph, "checkpoint_message_ids"):
            seen_message_ids |= self._graph.checkpoint_message_ids(thread_id)
        # The status counts read the registry this opens and closes, so the
        # window is opened around the consumption itself rather than
        # around the door — a caller that abandons the generator closes it
        # with the run.
        self._run_control.executing(
            thread_id,
            run_id,
            stuck_after_s=self._stuck_after_s(),
        )
        try:
            events = _stream_run_events(
                self._graph,
                run_id=run_id,
                input_value=input_value,
                config=config,
                seen_message_ids=seen_message_ids,
                thread_id=thread_id,
                stop_token=stop_token,
                run_call_supervisor=getattr(
                    self._graph, "run_call_supervisor", None
                ),
                budgets=self._budgets,
                lifecycle=self._run_control,
            )
            if self._receipts is not None:
                events = stream_in_turn_segment(
                    self._receipts, thread_id, run_id, events
                )
            outcome = yield from events
        except BaseException:
            # WHY the raise branch and not a plain ``finally``: a stream
            # that raised or was abandoned never reported an outcome, so
            # there is nothing to read and the window has to close here.
            self._run_control.finished(thread_id, run_id)
            raise
        if (
            outcome.choice_window is not None
            or outcome.document_choice is not None
            or outcome.attachment_wait is not None
        ):
            # A pause is a state, not an end, so the window stays open and
            # is marked waiting. Closing it here is what made the published
            # ``paused`` count structurally zero: the generator's own
            # teardown ran before the caller could record the pause, so
            # every pause found no live entry to mark.
            self._run_control.paused(thread_id, run_id)
        return outcome

    def _stuck_after_s(self) -> float:
        """The no-progress window a run is suspected stuck past.

        The executing phase's deadline plus the cleanup grace: a run that
        has gone that long without producing anything has not been
        cleaned up either, so the grace is what makes the bound mean
        "still working on it" rather than "should be over".
        """
        budgets = self._budgets if self._budgets is not None else RunBudgets()
        return budgets.execute_stage_s + budgets.cleanup_grace_s

    def _finalize_emptied_run(
        self,
        request: RunRequest,
        outcome: RunOutcome,
        turn_started: float,
    ) -> RunOutcome:
        """Finalize a run whose stream produced no state.

        Two different runs reach here and they are NOT interchangeable: an
        empty stream WITHOUT a raise has no failure to persist and exits
        without touching the store, while a mid-flight raise already emitted
        its own terminal events and owes the turn table a refuse row —
        without it, the SPA's ``openThread`` reconcile replaces the
        optimistic turn with the server's empty list and the failed exchange
        vanishes.
        """
        if outcome.refuse_reason is None or outcome.technical_result is None:
            # A stream that raised classified itself, so a refuse triple
            # and its technical result always arrive together. A stream
            # that produced nothing established no end to report, and
            # naming one here would invent the failure it never had.
            return outcome
        self._record_terminal(
            turn_started, "refuse", run_id=request.run_id
        )
        try:
            self._commit_turn(
                RunRecord(
                    id=request.run_id,
                    thread_id=request.thread_id,
                    question=request.question,
                    status="refuse",
                    status_reason=outcome.refuse_reason,
                    answer="",
                    citations=[],
                    thinking_text="",
                    thinking_duration_ms=outcome.thinking_ms,
                    tool_summaries=[],
                    assistant_message=outcome.refuse_copy or "",
                    # Stream error taxonomy (auth / network / ...). ``None``
                    # when the run did not end on the refuse branch; the
                    # field default (``""``) carries the same meaning so the
                    # SPA never sees an unexpected ``None``.
                    status_classified=outcome.refuse_kind or "",
                ),
                # A stream that raised has established its own end, so
                # the receipt takes the result the classification selected
                # rather than the empty stream's "nothing happened".
                technical_result=outcome.technical_result.value,
                failure_kind=failure_token(outcome.refuse_kind),
                stage_metrics=outcome.stage_metrics,
                model_calls=outcome.model_calls,
            )
        except ThreadNotFoundError:
            # No extra event here — the engine's terminal events already
            # closed the wire (the normal branch can still add one because
            # its events have not been emitted yet).
            return outcome
        # A mid-flight refuse persisted a terminal row but the LangGraph
        # checkpoint may still list pending next nodes. Finalize via the
        # graph seam so GET does not report ``pending_checkpoint: true``
        # above a terminal turn. Defensive ``hasattr`` mirrors the
        # ``delete_thread`` / ``pending_nodes`` wiring tolerance.
        self._finalize_abandoned(
            request.thread_id,
            status="refuse",
            status_reason=outcome.refuse_reason,
        )
        return outcome

    def _persist_stopped_run(
        self, request: RunRequest, outcome: RunOutcome
    ) -> Iterator[RunEvent]:
        """Write the stopped turn row, clear the checkpoint, close the wire.

        A stopped run is still a turn the operator can see: without the row
        the SPA's ``openThread`` reconcile would replace its optimistic turn
        with the server's list and the question would vanish (same reasoning
        as the mid-flight refuse row).

        ``finalize_abandoned_run`` mirrors the mid-flight refuse path: the
        LangGraph checkpoint may still list pending nodes after the stream
        was closed, and without this a Resume banner would appear above a
        turn the operator deliberately ended.
        """
        thinking_ms = outcome.thinking_ms
        try:
            self._commit_turn(
                RunRecord(
                    id=request.run_id,
                    thread_id=request.thread_id,
                    question=request.question,
                    status="refuse",
                    status_reason=STOPPED_STATUS_REASON,
                    answer="",
                    citations=[],
                    thinking_text="",
                    thinking_duration_ms=thinking_ms,
                    tool_summaries=[],
                    assistant_message=STOPPED_ASSISTANT_MESSAGE,
                ),
                technical_result=RunTechnicalResult.STOPPED.value,
                cancellation_reason=outcome.cancellation_reason,
                stage_metrics=outcome.stage_metrics,
                model_calls=outcome.model_calls,
            )
        except ThreadNotFoundError:
            # The thread was deleted while the run was being stopped; do not
            # resurrect it. The events below still close the wire so the
            # SPA's reader does not hang.
            logger.debug(
                "run %s stop finalized after thread %s was deleted",
                request.run_id,
                request.thread_id,
            )
        self._finalize_abandoned(
            request.thread_id,
            status="refuse",
            status_reason=STOPPED_STATUS_REASON,
        )
        yield from stopped_run_events(request.run_id, thinking_ms=thinking_ms)

    def _commit_turn(
        self,
        run: RunRecord,
        *,
        technical_result: str,
        semantic_result: str | None = None,
        failure_kind: str | None = None,
        cancellation_reason: CancellationReason | None = None,
        stage_metrics: tuple[StageOutcome, ...] = (),
        model_calls: dict[str, int] | None = None,
    ) -> None:
        """Persist one finished turn's transcript row and its receipt.

        The receipt and the row commit together in one store transaction
        whenever the turn has a receipt, so a reader never finds one
        half of the turn without the other. A turn with no receipt — one
        started before receipts existed — still gets its row: the
        transcript is the turn's history, and losing it to make room for
        a projection would be the wrong trade.

        ``ThreadNotFoundError`` still escapes to the caller's own handler
        so a thread deleted mid-run is not resurrected.
        """
        receipts = self._receipts
        if receipts is not None:
            committed = receipts.commit_terminal(
                thread_id=run.thread_id,
                turn_id=run.id,
                technical_result=cast(TechnicalResult, technical_result),
                semantic_result=cast("Any", semantic_result),
                failure_kind=failure_kind,
                cancellation_reason=(
                    cancellation_reason or cancellation_for(technical_result)
                ),
                stage_metrics=stage_metrics,
                model_calls=model_calls,
                run=run,
                catalog_warning_count=self._catalog_warning_count(run.thread_id),
            )
            if committed is not None:
                return
        self._store.append_run(run)

    def _catalog_warning_count(self, thread_id: str) -> int | None:
        """How many of the thread's documents are still unsearchable.

        ``None`` when no attachment store is wired to this executor, so
        the receipt keeps whatever the turn already recorded instead of
        claiming a clean thread. The count is asked at the commit, not
        at the run's start: a publication that settles mid-turn is
        exactly the case the receipt should not warn about.
        """
        if self._publication_warnings is None:
            return None
        return self._publication_warnings(thread_id)

    def _record_pause(
        self,
        thread_id: str,
        turn_id: str,
        semantic_result: str | None,
        outcome: RunOutcome,
    ) -> None:
        """Report the deliberate wait a paused run is now in.

        A pause is a state, not an outcome, so it writes no transcript
        row; what it records is the prefix's observed work, which the
        continued run's own terminal commit later adds to.
        """
        receipts = self._receipts
        if receipts is not None:
            final_state = outcome.final_state
            track_decision = (
                final_state.get("track_decision")
                if isinstance(final_state, dict)
                else None
            )
            receipts.paused(
                thread_id=thread_id,
                turn_id=turn_id,
                semantic_result=cast("Any", semantic_result),
                track_decision=track_decision,
                stage_metrics=outcome.stage_metrics,
                model_calls=outcome.model_calls,
                catalog_warning_count=self._catalog_warning_count(thread_id),
            )
        # The execution window is still open on this run — ``_drive`` marks
        # a pause rather than closing it — so an operator reading the counts
        # during a clarification sees a paused turn, not a run that stopped
        # reporting progress and is therefore suspected stuck. Marking it
        # again here is a no-op, and keeps a pause reported from anywhere
        # but the stream's own teardown reaching the counts at all.
        self._run_control.paused(thread_id, turn_id)

    def _finalize_abandoned(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        """Tell the graph seam a run that will never continue is finished."""
        if hasattr(self._graph, "finalize_abandoned_run"):
            self._graph.finalize_abandoned_run(
                thread_id, status=status, status_reason=status_reason
            )

    def _record_terminal(
        self, turn_started: float, status: str, *, run_id: str
    ) -> None:
        """Record one terminal turn on the observability collector.

        Counters only — the status enum plus elapsed milliseconds; no
        question / answer / URL text ever reaches the collector. A recording
        failure is a WARNING, never a broken wire or a lost turn row (same
        tolerance as the feedback store on GET-thread).
        """
        collector = self._collector
        if collector is None:
            return
        elapsed_ms = (time.perf_counter() - turn_started) * 1000.0
        try:
            collector.record(status, latency_ms=elapsed_ms)
        except ObservabilityError:
            logger.warning(
                "run %s: failed to record terminal status %r on the "
                "observability collector",
                run_id,
                status,
                exc_info=True,
            )


__all__ = [
    "STAGE_CLEANUP",
    "STAGE_EXECUTE",
    "STOPPED_ASSISTANT_MESSAGE",
    "ensure_no_pending_choice",
    "ensure_thread_occupancy",
    "StageOutcome",
    "STOPPED_STATUS_REASON",
    "THREAD_DELETED_IN_FLIGHT_COPY",
    "THREAD_DELETED_RESUMING_COPY",
    "ResumeRunRequest",
    "RunBudgets",
    "RunEvent",
    "RunEventKind",
    "RunExecutor",
    "RunOutcome",
    "RunRequest",
    "RunTechnicalResult",
    "RunTerminal",
]
