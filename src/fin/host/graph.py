"""The v1 host graph (T6).

This module is the **only** module in ``src/fin/host/`` that knows
the host graph's topology. It is built from scratch here — no
``langgraph.prebuilt.create_react_agent``,
no vendored external agent tree (ADR 0019). The graph wires the
three nodes the v1 spec requires:

* ``model_node`` — invoke the chat model, increment ``step_count``
  once per entry, route on tool_calls.
* ``tool_node`` — execute ``list_files`` / ``read_file`` /
  ``retrieve`` via ``langgraph.prebuilt.ToolNode``, append the
  resulting ``ToolMessage``s and the corresponding ``EvidenceItem``s.
* ``citation_gate_node`` — call ``verify_citations`` and translate
  the gate result into the run-status enum.

T6 of the enterprise-kb spec (Focus 4) adds the issuer fan-out
ADR 0034 accepted, as three more nodes on the same level — never
nested inside ``model_node``:

* ``issuer_fanout_node`` — cap and normalize the roster a
  ``research_issuers`` call asked for, then ``Send`` one branch per
  approved issuer. Over the cap is the typed ``issuer_overflow``
  refusal, not a truncated roster.
* ``issuer_research_node`` — one branch, one issuer, one retrieve-port
  call; its chunks become ordinary evidence.
* ``issuer_join_node`` — fold the branches into one
  ``fin.issuer_fanout.v1`` envelope and hand control back to
  ``model_node``, so the citation verifier still runs exactly ONCE,
  on the synthesized answer.

The two fuses live in ``AgentState``:

* ``step_count`` increments once per ``model_node`` entry. When
  ``step_count >= STEP_CAP`` (default 32) the run ends with
  ``status == "step_cap"``. ``STEP_CAP`` is in fin state — NOT in
  LangGraph ``recursion_limit``. The graph factory sets
  LangGraph's recursion limit strictly higher (see
  ``RECURSION_LIMIT`` in ``state.py``, currently 105) so the
  agent-loop fuse always fires first (ADR 0008 + spec success
  criteria item 5).
* ``repair_count`` is the citation-repair counter the gate uses to
  decide bounce vs. refuse. It is independent of the step cap.

Concurrency
-----------
``compile_graph`` enforces "at most one active run per
``thread_id``" by returning a ``_LockedGraph`` proxy that acquires
a per-thread ``threading.Lock`` around every ``invoke`` /
``stream`` / ``ainvoke`` call. A second concurrent ``invoke`` on
the same ``thread_id`` raises the typed ``ConcurrentRunError``
without deadlocking and without silently serializing. The factory
itself is idempotent (LRU-cached): two calls with identical args
return the SAME proxy object — T9 will assert object identity
between the CLI helper's graph and the HTTP handler's graph.

The factory does NOT auto-ingest and does NOT read a local extracted
tree on the serve path (ADR 0046 / 0047): citation evidence and
``read_file`` bodies come from this run's retrieve / read port
payloads, and the ``read_file`` body source is the injected read port
(``adapter.read`` when the adapter offers one, an explicit
``read_port=``, otherwise a typed-miss port).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, Iterable, Literal, Mapping, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import InjectedState, ToolNode
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Send, interrupt

from fin.corpus.extracted import Locator
from fin.host.compaction import CompactionInputError
from fin.host.citation_gate import (
    HIDDEN_BOUNCE_MARKER,
    Citation,
    EvidenceItem,
    verify_citations,
)
from fin.host.evidence_admission import (
    admit_retrieved_chunks,
    evidence_from_chunks,
)
from fin.host.memory_port import MemoryPort
from fin.host.named_failures import (
    named_refusal_reason,
    retrieve_outcomes_delta,
)
from fin.host.session_compaction import (
    OccupancyCompactor,
    SessionSummaryError,
    count_tokens_approximately,
    session_budget_tokens,
)
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    UserProfileError,
    UserProfileStore,
    UserProfileUnknownLayerError,
)
from fin.host.state import (
    AgentState,
    FANOUT_RESET,
    REPAIR_CAP,
    RECURSION_LIMIT,
    STEP_CAP,
    TERMINAL_STATUSES,
)
from fin.host.agent_instructions import (
    attachment_context_message,
    messages_for_model,
)
from fin.host.degradation_notice import (
    DEGRADATION_NOTICE_FIELD,
    consume_degradation,
    degradation_notice_entry,
)
from fin.host.observability import default_collector
from fin.host.rewrite_retry import (
    RetryAttempt,
    RetrieveCallError,
    RewriteRetryError,
    RewriteRetryResult,
    retrieve_with_rewrite_retry,
)
from fin.host.stream_map import (
    assemble_final_ai_message,
    format_tool_summary,
    inline_thinking_segments,
    strip_thinking_from_answer,
)
from fin.corpus.catalog import DEFAULT_CATALOG_PATH
from fin.host.tools import list_files, read_file
from fin.host.tools.issuer_fanout import (
    ISSUER_FANOUT_TOOL,
    MAX_ISSUERS,
    STATUS_EMPTY,
    STATUS_HIT,
    STATUS_UNAVAILABLE,
    issuer_query,
    join_issuer_findings,
    plan_issuer_fanout,
)
from fin.host.track_decision import (
    decide_track,
    normalize_track,
    snapshot_delta_for_utterance,
)
from fin.host.search_mode_decision import (
    apply_operator_pick,
    assigned_retrieve_mode,
    clarify_options,
    normalize_search_mode,
    search_mode_update_for_turn,
)
from fin.host.extract_method_proposal import (
    apply_extract_method_pick,
    build_proposal_audit,
    normalize_extract_method,
    propose_window_payload,
    proposal_is_pending,
)
from fin.retrieve import MCPRetrieveAdapter, FakeRetrievePort
from fin.retrieve.degradation import (
    DegradationLedger,
    degradation_ledger_from_env,
)
from fin.retrieve.hit_mapping import MappingFailureLedger, ledger_from_env
from fin.retrieve.input_validation import InputValidationError
from fin.retrieve.port import RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY, RetrievePort
from fin.retrieve.read_port import MissReadPort, ReadPort
from fin.retrieve.tool import retrieve


# Module logger for typed, catchable failures the gate swallows (a
# summary model outage must never refuse the operator turn; the
# graph layer logs and continues un-compacted).
_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:  # ``fin.corpus.facts`` is imported lazily at call time
    from fin.corpus.facts import Fact


# ----- typed errors -----


class HostGraphError(Exception):
    """Base class for host-graph typed errors."""


class ConcurrentRunError(HostGraphError):
    """A second ``invoke`` was attempted on a ``thread_id`` while
    the first was still in flight.

    ADR 0016 / CONTEXT.md: "at most one run is active per thread."
    The graph factory raises this from its lock-acquisition path;
    callers should release their thread (or surface the error to
    the user) rather than block.
    """


class KnowledgeMCPFaultError(HostGraphError):
    """Conversation entry was refused because the knowledge MCP is not 可达.

    The published three-state snapshot (``fin.host.knowledge_mcp``) is
    缺 or 坏 while the product ``mcp`` adapter is configured: create /
    open / send cannot produce evidence this turn, so the server refuses
    rather than running a research turn with no retrieve. The HTTP
    surface translates this to 409 ``knowledge_mcp_fault`` with the
    operator copy 「知识 MCP 故障」.
    """


class RetrieveAdapterConfigError(HostGraphError):
    """The requested retrieve adapter cannot be built as configured.

    Raised for an unknown ``retrieve_adapter`` kind. (The former
    empty-``locator_map`` refusal is gone with ADR 0046 / 0047:
    canonical ``source_ref`` hits need no map, the factory no longer
    derives one from the extracted tree, and unmapped hits are dropped
    and counted on the mapping ledger instead of hiding behind a
    healthy-looking miss.)
    """


class WorkspaceRootMissingError(HostGraphError):
    """A call (model-call or memory-tool) had no usable ``workspace_root``.

    spec SC8 / ADR 0049: ``configurable.workspace_root`` is the SOLE
    carrier of the per-call 工作区. Missing or empty at tool-call time
    → this typed error. The graph NEVER falls back to ``Path.cwd()``
    or ``$HOME/.fin/default`` from inside the runtime — that fallback
    lives in the HTTP / CLI seam only, where it resolves ``$HOME``
    explicitly via :func:`fin.host.workspace_registry.default_workspace_root`.

    Callers should surface this exit to the operator rather than
    silently retrying with a default; an unset workspace is an
    operator / seam bug, not a runtime recovery point.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "configurable.workspace_root is missing or empty; "
            "the HTTP / CLI seam must inject the resolved default "
            "$HOME/.fin/default path before invoking the graph."
        )


# ----- helpers -----


def _resolve_workspace_root(config: Mapping[str, Any] | None) -> Path:
    """Return the per-call workspace_root path from a LangGraph config.

    spec SC8 / ADR 0049: the runtime reads ``configurable.workspace_root``
    only — it does NOT consult ``$HOME``, ``Path.cwd()``, or any baked
    default. Missing / empty values raise :class:`WorkspaceRootMissingError`
    so a misconfigured seam surfaces as a typed exit rather than silently
    selecting the operator's working directory.

    Non-string values (None / Path / int) are rejected as missing; the
    string ``""`` is rejected as empty. Both shapes share the same error
    because the seam contract is "non-empty string path", not "any
    truthy value".
    """
    if not config:
        raise WorkspaceRootMissingError()
    configurable = config.get("configurable") or {}
    raw = configurable.get("workspace_root")
    if raw is None:
        raise WorkspaceRootMissingError()
    if isinstance(raw, str) and raw == "":
        raise WorkspaceRootMissingError()
    return Path(raw)


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"
_DEFAULT_FACTS_ROOT = _DEFAULT_DATA_DIR / "facts"
_DEFAULT_CATALOG_PATH = DEFAULT_CATALOG_PATH

# The two retrieve adapter kinds ``compile_graph`` can auto-build.
# ``BM25Adapter`` is deliberately absent: ADR 0030 archived it from
# the product path, and offline tests pass it in as ``adapter=``.
RETRIEVE_ADAPTER_FAKE = "fake"
RETRIEVE_ADAPTER_MCP = "mcp"
RETRIEVE_ADAPTER_KINDS = (RETRIEVE_ADAPTER_FAKE, RETRIEVE_ADAPTER_MCP)

# spec SC6 / ADR 0049 (Wave A): the user-profile file lives one
# directory below ``$HOME`` at ``$HOME/.fin/user.md``. The graph's
# default ``user_profile_store`` constructs both layers pointing at
# this single path so the merged read returns the global text. The
# literal lives here (not imported from ``user_profile``) so the
# ``user_profile`` module stays free of any host-graph reference.
_USER_PROFILE_LAYER_DIR = ".fin"

# Marker the gate uses on the next pass to recognize a bounce
# template in the message history. The marker is data; CLI / HTTP
# rendering (T7, T9) strip it before printing per ADR 0021. Imported
# from ``citation_gate`` (the SSOT) so a rename touches one file.
_BOUNCE_MARKER = HIDDEN_BOUNCE_MARKER


def _resolve_path(value: Path | str | None, default: Path) -> Path:
    """Coerce ``value`` to a ``Path``; fall back to ``default``."""
    if value is None:
        return default
    return Path(value)


def _last_ai_text(messages: list[BaseMessage]) -> str:
    """Return the THINKING-STRIPPED textual content of the last
    ``AIMessage`` in ``messages``. Empty string if there is no
    ``AIMessage`` or the last ``AIMessage`` is empty (model returned
    only tool_calls).

    Why strip here (T3 / SC60): the citation gate and every
    user-visible answer surface read this function's output. When
    the proxy inlines ``<thinking>...</thinking>`` into content
    (fallback shape; ADR 0022 normally routes thinking through
    ``additional_kwargs.reasoning_details``, which this function
    never reads), the private scratchpad must not pass as the
    answer. Stripping at THIS seam means the gate, ``/ask``, and
    the workspace SSE all agree on one definition of answer prose.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str):
                return strip_thinking_from_answer(content)
            # Some providers return content as a list of blocks;
            # the citation gate treats the raw string, so coerce.
            return strip_thinking_from_answer(
                "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            )
    return ""


def _format_bounce_template(template: str) -> str:
    """Wrap the gate's hidden template with the marker so T7 / T9
    can find and strip it before user-visible rendering."""
    return f"{_BOUNCE_MARKER} {template}"


# ----- tool wrappers -----


def _make_list_files_tool(catalog_path: Path):
    """Build the ``list_files`` host tool.

    Returns a LangChain ``BaseTool`` whose function calls T3's
    ``list_files`` with the factory's injected catalog path (ADR
    0047: the tool catalogs the metadata table over ingested/pushed
    filings — never a serve-time extracted tree). The tool does NOT
    contribute evidence — it returns catalog metadata only.
    The tool's registered name is ``list_files`` (matching the
    ADR 0009 contract) so the model can call it by name.
    """

    @tool("list_files")
    def list_files_tool(
        company: str | None = None,
        period: str | None = None,
        *,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
    ) -> dict:
        """List available 10-Q filings in the corpus.

        Optional ``company`` and ``period`` filters match the
        catalog row's ``company`` and ``period`` fields exactly.
        Additive EDGAR-shaped filters (``cik``, ``form``,
        ``ticker``, exact ``filing_date``) per ADR 0031 are
        forwarded through. Returns ``{"filings": [...]}`; unknown
        filters, conflicting filters, empty filter values, and an
        empty or missing catalog table all return
        ``{"filings": []}``.
        """
        return list_files(
            company=company,
            period=period,
            cik=cik,
            form=form,
            ticker=ticker,
            filing_date=filing_date,
            catalog_path=catalog_path,
        )

    return list_files_tool


def _make_read_file_tool(read_port: ReadPort):
    """Build the ``read_file`` host tool.

    The tool wraps T3's ``read_file`` with the factory's injected read
    port (ADR 0046 / 0047: the body fetch is a port call — MCP when the
    bytes live there, the fake manifest on the fake path, never a local
    extracted tree) and, on a successful page read, contributes one
    ``EvidenceItem`` to ``AgentState`` per page in the joined ``text``
    slice. A miss dict (``{"error": "unknown_locator"}`` or the named
    ``read_unavailable`` failure) contributes nothing.

    The tool's registered name is ``read_file`` so the model can call
    it by name.
    """

    @tool("read_file")
    def read_file_tool(
        document_id: str,
        version: str,
        page: int = 1,
        page_limit: int = 1,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Read a page from a known locator.

        ``document_id`` matches the manifest's ``document_id``
        (e.g. ``AAPL-2022Q3``). ``version`` is the manifest
        sha256. ``page`` is 1-based; ``page_limit`` is clamped to
        3 (ADR 0009). Misses return ``{"error": "unknown_locator"}``
        (or ``{"error": "read_unavailable"}`` when the corpus
        backend exposes no read tool) and contribute no evidence.
        """
        result = read_file(
            document_id=document_id,
            version=version,
            page=page,
            page_limit=page_limit,
            reader=read_port,
        )
        new_evidence: list[EvidenceItem] = []
        if isinstance(result, dict) and "text" in result:
            # Successful page read: emit one EvidenceItem per page
            # in the joined ``text`` slice. ``page_limit`` is
            # clamped to 3, so the loop is bounded. The gate
            # matches spans anywhere in ``page_text``; emitting
            # one item per page keeps each EvidenceItem's locator
            # faithful to the source page (ADR 0021). We slice the
            # joined text by the ``\n\n`` separator the tool uses
            # so the gate cannot over-attribute a span to multiple
            # pages when ``page_limit > 1``.
            try:
                page_count_value = int(result["page_count"])
            except (KeyError, TypeError, ValueError):
                page_count_value = page
            end_page = min(page + page_limit - 1, page_count_value)
            page_text_parts = result["text"].split("\n\n")
            for offset, page_num in enumerate(range(page, end_page + 1)):
                locator = Locator(
                    document_id=result["document_id"],
                    version=result["version"],
                    page=page_num,
                )
                page_text = (
                    page_text_parts[offset]
                    if offset < len(page_text_parts)
                    else result["text"]
                )
                new_evidence.append(
                    EvidenceItem(locator=locator, page_text=page_text)
                )
        tool_message = ToolMessage(
            content=json.dumps(result, default=str),
            tool_call_id=tool_call_id,
            name="read_file",
        )
        return Command(
            update={"messages": [tool_message], "evidence": new_evidence}
        )

    return read_file_tool


def _default_empty_hit_rewrite(query: str) -> str:
    """Deterministic lightweight rewrite for an exact empty retrieve hit.

    T9 headroom: a model-backed rewriter can replace this via
    ``_make_retrieve_tool(rewriter=...)`` without touching the retry
    contract — the helper only requires a non-empty string different
    from the input. Keeping the original terms and appending one
    broadening marker is cheap, deterministic, and never collapses to
    the original query, so the lexical port can re-score the same
    terms with the retry visibly bounded at one attempt.
    """
    return f"{query} (broadened)"


def _make_retrieve_tool(
    adapter: RetrievePort,
    *,
    mapping_ledger: MappingFailureLedger | None = None,
    degradation_ledger: DegradationLedger | None = None,
    rewriter: Callable[[str], str] | None = None,
    retry_observer: Callable[[RewriteRetryResult], None] | None = None,
):
    """Build the ``retrieve`` host tool.

    The tool wraps T4's ``retrieve`` (which is the port-seam)
    with the factory's shared adapter. On a hit, each chunk
    contributes one ``EvidenceItem`` to ``AgentState``. An empty
    contract (``{"chunks": []}``) contributes nothing.

    Evidence admission (T17 / SC22): a chunk whose ``source_ref``
    is not a fin ``Locator`` never mapped back to one, so it is
    dropped before it can become evidence or reach the serialized
    envelope, and the drop is counted on ``mapping_ledger``. The
    citation gate's own rules are unchanged — admission decides
    what evidence exists, not how the gate rules on it.

    Invalid input (T21 / SC42 / SC43): the port raises a named
    ``InputValidationError`` for an empty query or a provably
    conflicting filter set, and the tool serializes that code as
    ``{"error": "<code>"}``. It is NOT rewritten to
    ``{"chunks": []}`` — an empty result would answer a question
    that was never asked — and it contributes no evidence. A
    legal empty result (unknown filter, no hits) keeps the
    ``{"chunks": []}`` shape it always had.

    Typed EXIT (spec s1-corpus-retrieve § Boundaries): when the
    port returns ``{"error": "retrieve_unavailable"}`` (product
    MCP transport / process down, ADR 0030) the tool serializes
    THAT dict as the ``ToolMessage`` content. It must not be
    rewritten as ``{"chunks": []}`` — the miss shape would tell
    the model the corpus has nothing when retrieval never ran.
    The error contributes no evidence and triggers no BM25
    fallback; the model can retry or say retrieval is down.

    Empty-hit rewrite retry (T9): an exact miss shape
    (``{"chunks": []}``) is retried ONCE through
    ``retrieve_with_rewrite_retry`` with a rewritten query —
    never a fixed pipeline. Hits, memory recall shapes, and
    typed error dicts pass through untouched (no rewrite), the
    retry is bounded at one rewrite (``max_retries=1``), and the
    port's ``InputValidationError`` envelope (``{"error": code}``)
    is likewise never rewritten. The rewrite strategy is
    deterministic by default (``_default_empty_hit_rewrite``); a
    model-backed rewriter can be injected via ``rewriter`` without
    touching this contract. Every PERFORMED rewrite is recorded on
    the process default observability collector (or the injected
    ``retry_observer``) — including one whose follow-up retrieve
    raised, so the error path cannot un-count a performed rewrite.
    A typed retry failure raised by the helper itself (e.g. an
    injected rewriter that throws) does NOT escape the tool: the
    tool keeps its envelope contract and degrades to the exact
    ``{"chunks": []}`` envelope it would have returned for a miss.

    Degradation (T20 / SC39 / §B3): a response carrying
    ``degraded`` has every one of its ``degraded_reasons``
    recorded on ``degradation_ledger``, and the notice rides
    onto the tool envelope so the run's tool trace tells the
    user this answer is partial. The flag changes neither the
    evidence a hit contributes nor the citation gate's rules —
    a degraded result is usable but must be visible.

    The tool's registered name is ``retrieve`` so the model can
    call it by name.
    """
    ledger = (
        mapping_ledger if mapping_ledger is not None else ledger_from_env()
    )
    degradations = (
        degradation_ledger
        if degradation_ledger is not None
        else degradation_ledger_from_env()
    )
    rewrite = rewriter if rewriter is not None else _default_empty_hit_rewrite

    @tool("retrieve")
    def retrieve_tool(
        query: str,
        document_id: str | None = None,
        limit: int = 8,
        *,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
        state: Annotated[Any, InjectedState()] = None,
    ) -> Command:
        """Narrative retrieval over page-passage chunks.

        ``query`` is the lexical query. ``document_id`` is an
        optional corpus filter; unknown ids return empty.
        Additive EDGAR-shaped filters (``cik``, ``form``,
        ``ticker``, exact ``filing_date``) per ADR 0031 are
        forwarded to the port. ``limit`` defaults to 8 (ADR 0009
        cap = 20). On a hit, each chunk contributes one
        ``EvidenceItem``; misses contribute none. When retrieval
        is unavailable the tool returns
        ``{"error": "retrieve_unavailable"}``. The 检索档 is NOT an
        argument: the host cascade assigns it and this call carries it
        (spec search-mode SC2), so the model cannot pick or lie about a
        档.
        """
        def _port_call(retry_query: str) -> dict[str, Any]:
            """One adapter-bound retrieve call for the retry helper.

            The ``InputValidationError`` envelope is built HERE, before
            the retry helper can see it: a named input error is NOT an
            empty hit, so it must flow through as an error dict and
            never trigger a rewrite (T21 contract, unchanged).
            """
            try:
                return retrieve(
                    query=retry_query,
                    document_id=document_id,
                    cik=cik,
                    form=form,
                    ticker=ticker,
                    filing_date=filing_date,
                    limit=limit,
                    # SC2: the 档 this run's cascade assigned rides the
                    # existing single port call. Resolved here (per
                    # invocation), not at compile time — the graph is
                    # cached and shared across runs.
                    search_mode=assigned_retrieve_mode(state),
                    adapter=adapter,
                )
            except InputValidationError as exc:
                # Invalid input (T21 / §B2 第 5 条). The port already named
                # and counted it; the tool's contract is an envelope, so the
                # code travels as ``{"error": <code>}`` — the same named-EXIT
                # shape as an unavailable transport, and deliberately NOT
                # ``{"chunks": []}``, which would tell the model the corpus
                # is empty for a call that was never askable. The model can
                # fix its own arguments and retry; the run does not die on a
                # blank ``query``.
                return {"error": exc.code}

        # Every port call is counted so "a rewrite was PERFORMED"
        # is observable even on the paths where the helper raises
        # and never returns its attempts: the port is called a
        # second time exactly when one rewrite was performed.
        port_calls = 0

        def _counted_port_call(retry_query: str) -> dict[str, Any]:
            nonlocal port_calls
            port_calls += 1
            return _port_call(retry_query)

        def _record_retry(outcome: RewriteRetryResult) -> None:
            """Record one performed rewrite on the retry observer.

            Default: the process observability singleton (``fin.host.http``
            exposes it on GET /api/observability). Resolved lazily so a
            collector built later in the process is still the one that
            counts.
            """
            if retry_observer is not None:
                retry_observer(outcome)
            else:
                default_collector().record_rewrite_retry(
                    exhausted=outcome.exhausted
                )

        # T3 (spec SC4–SC5): the drops this call caused are measured on
        # the ledger's PER-THREAD counter, read before and after the
        # retrieve. Both halves matter:
        #
        # * Before, not after: the product adapter records its
        #   mapper-drops into the same shared ledger from INSIDE
        #   ``retrieve``, so a baseline taken after the transport returns
        #   already contains this call's drops and the delta is
        #   structurally zero — which mislabels a real mapping drop as
        #   ``corpus_not_ready``.
        # * Per-thread, not process-wide: ONE ledger serves every run in
        #   the process (``compile_graph`` injects it into the adapter and
        #   the tool alike), while the concurrent-run guard is per
        #   ``thread_id``. A process-wide delta would charge a concurrent
        #   run's drop to this one and refuse a genuinely empty collection
        #   as ``mapping_drop`` — the same false accusation, one race
        #   later. A retrieve call runs on one thread, so the per-thread
        #   counter needs no window at all.
        #
        # The retry helper may call the port twice (one rewrite); both
        # attempts belong to this call, and both are counted here.
        failures_before = ledger.count_on_this_thread()

        retry_outcome: RewriteRetryResult | None = None
        degraded_to_empty_hit = False
        try:
            retry_outcome = retrieve_with_rewrite_retry(
                query, _counted_port_call, rewrite, max_retries=1
            )
        except RewriteRetryError as exc:
            # A rewrite that actually happened must be counted even on
            # the error path: the recording can never sit after the
            # re-raise. The synthesized outcome marks the retry
            # exhausted — the rewritten attempt produced no hit, so it
            # definitively did not turn the miss into one.
            if port_calls > 1:
                _record_retry(
                    RewriteRetryResult(
                        result={},
                        attempts=tuple(
                            RetryAttempt(query=query, empty_hit=True)
                            for _ in range(port_calls)
                        ),
                    )
                )
            if isinstance(exc, RetrieveCallError):
                # Pre-retry behavior (T6): an unexpected exception from the
                # port propagated out of the tool unchanged. The retry helper
                # types it as ``RetrieveCallError``; unwrap the cause so the
                # run's error surface stays exactly what it was before T9.
                cause = exc.__cause__
                if cause is not None:
                    raise cause from exc
                raise
            # Typed retry failure with no port exception behind it
            # (e.g. an injected rewriter that throws): the tool keeps
            # its envelope contract and degrades to the exact shape it
            # would have returned for ``{"chunks": []}`` instead of
            # refusing the whole run. The port's typed error dict
            # never takes this path (it is not an empty hit).
            degraded_to_empty_hit = True
        if retry_outcome is not None and retry_outcome.retry_count:
            _record_retry(retry_outcome)
        result = (
            {"chunks": []}
            if degraded_to_empty_hit or retry_outcome is None
            else dict(retry_outcome.result)
        )
        if isinstance(result, dict) and "error" in result:
            # Typed EXIT: surface the port's error dict verbatim.
            # Building evidence / a chunks envelope here would
            # destroy the distinction the spec requires. A degraded
            # failure keeps its reasons on the same dict (§B2 第 2
            # 条: 具名失败, not an empty result), and consuming it
            # here is what puts them in front of the user.
            envelope = dict(result)
            _consume_into_envelope(envelope, result, ledger=degradations)
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=json.dumps(envelope, default=str),
                            tool_call_id=tool_call_id,
                            name="retrieve",
                        )
                    ]
                }
            )
        raw_chunks = result.get("chunks", []) if isinstance(result, dict) else []
        # SC22: hits that did not map back to a locator are dropped and
        # counted here, so neither the evidence list nor the serialized
        # envelope below can carry a retrieval-side identifier.
        #
        # T3 (spec SC4–SC5): the record this call contributes is
        # measured against ``failures_before`` (read before the port
        # ran, above), so it covers every drop this call caused — the
        # adapter's mapper-drops, the retry's second attempt, and the
        # ``admit_retrieved_chunks`` drop below. The counter is
        # per-thread, so a concurrent run's drop is never charged here
        # and the delta cannot go negative; ``retrieve_outcomes_delta``
        # still clamps it defensively. The run-scoped record lets the
        # citation gate tell an empty-bound 资料集合 from a mapping
        # drop, and it changes neither admission nor the envelope below.
        chunks = admit_retrieved_chunks(raw_chunks, ledger=ledger)
        new_evidence: list[EvidenceItem] = evidence_from_chunks(chunks)
        # Serialize the chunks with their Locator as a dict so the
        # ToolMessage is JSON-friendly. The original ``Chunk`` value
        # objects live on ``page_text`` (string) for evidence;
        # ToolMessage content is the contract-shape dict.
        serialized_chunks = [
            {
                "text": chunk.text,
                "source_ref": {
                    "document_id": chunk.source_ref.document_id,
                    "version": chunk.source_ref.version,
                    "page": chunk.source_ref.page,
                },
                "score": chunk.score,
            }
            for chunk in chunks
        ]
        envelope: dict[str, Any] = {"chunks": serialized_chunks}
        _consume_into_envelope(envelope, result, ledger=degradations)
        tool_message = ToolMessage(
            content=json.dumps(envelope, default=str),
            tool_call_id=tool_call_id,
            name="retrieve",
        )
        call_outcome: dict[str, Any] = {
            "chunks": len(serialized_chunks),
            "mapping_failures": ledger.count_on_this_thread(),
        }
        if result.get(RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY) is True:
            # A RECORDED Fast job fact rides the run record so the
            # citation gate can name the method, not just the empty set.
            call_outcome[RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY] = True
        return Command(
            update={
                "messages": [tool_message],
                "evidence": new_evidence,
                # T3 (SC4–SC5): the run-scoped outcome record. The
                # envelope the MODEL reads keeps the miss shape —
                # fin's internal map is not the model's business —
                # and the failure is named on the run, where the
                # operator reads it. ``retrieve_outcomes_delta``
                # guards the negative-delta case defensively.
                "retrieve_outcomes": retrieve_outcomes_delta(
                    [call_outcome],
                    baseline=failures_before,
                ),
            }
        )

    return retrieve_tool


def _consume_into_envelope(
    envelope: dict[str, Any],
    result: Any,
    *,
    ledger: DegradationLedger,
) -> None:
    """Record ``result``'s degradation and publish it on ``envelope``.

    Publishing IS the exposure: the two verbatim §B3 fields and the
    rendered notice ride on the envelope the tool trace summarizes, so
    the reasons reach the user on the same row as the call. The notice
    record is written only after they are on the envelope.
    """
    consume_degradation(
        result,
        ledger=ledger,
        expose=lambda signal, line: envelope.update(
            {
                **signal.as_envelope_fields(),
                DEGRADATION_NOTICE_FIELD: line,
            }
        ),
    )


def _make_get_xbrl_fact_tool(facts_root: Path):
    """Build the ``get_xbrl_fact`` host tool (T5 / ADR 0031).

    Returns a LangChain ``BaseTool`` whose function calls T5's
    ``get_xbrl_fact`` wrapper with the factory's resolved
    ``facts_root``. The tool does NOT contribute ``EvidenceItem``s:
    ``get_xbrl_fact`` returns ToolMessage JSON carrying the
    fact-row dict (or the typed ``fact_not_found`` miss shape),
    but those rows are NOT page text — the citation verifier's
    evidence match is a separate concern. The numeric gate
    (T5 / ADR 0031) consumes the ToolMessage JSON directly when
    verifying the model's claimed values.

    The tool's registered name is ``get_xbrl_fact`` so the model
    can call it by name (matches the ADR 0031 contract). Misses
    return the typed ``{"error": "fact_not_found", ...}`` dict so
    the agent loop never sees a typed exception out of the
    ToolNode (spec § Boundaries: "empty hits not exceptions").
    """

    @tool("get_xbrl_fact")
    def get_xbrl_fact_tool(
        concept: str,
        period: str,
        document_id: str | None = None,
        issuer: str | None = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> dict:
        """Look up one tagged XBRL fact from the ingest-built fact table.

        ``concept`` is the XBRL tag (e.g., ``us-gaap:Revenues``).
        ``period`` is the reporting period (e.g., ``2023-Q3-YTD``).
        ``document_id`` is the optional locator filter (e.g.,
        ``AAPL-2023Q3``). Returns the hit dict on success or the
        typed ``{"error": "fact_not_found"}`` dict on miss. Never
        raises.
        """
        from fin.host.tools.get_xbrl_fact import get_xbrl_fact

        result = get_xbrl_fact(
            concept=concept,
            period=period,
            document_id=document_id,
            issuer=issuer,
            facts_root=facts_root,
        )
        tool_message = ToolMessage(
            content=json.dumps(result, default=str),
            tool_call_id=tool_call_id,
            name="get_xbrl_fact",
        )
        # We return a ``Command`` with the new ToolMessage; the
        # numeric gate (T5) consumes this JSON to extract the
        # claimed (concept, period) -> fact-value mapping on the
        # ``done`` path. No ``EvidenceItem`` is contributed because
        # the fact row is NOT page text — the citation gate's
        # evidence shape is reserved for ``read_file`` /
        # ``retrieve`` page / chunk content.
        return Command(update={"messages": [tool_message]})

    return get_xbrl_fact_tool


# ----- locator-analysis tool consolidation -----
#
# The four plain-dict locator-analysis host tools
# (``compare_facts`` / ``redline`` / ``extract_tables`` /
# ``three_year_statement``) all share the same shape — ``@tool(name) +
# inner closure + lazy import + kwargs passthrough`` — so the per-tool
# duplication collapses into a single ``_make_locator_tool`` factory
# driven by a ``_LocatorToolSpec`` record. ``get_xbrl_fact`` keeps its
# own factory because its return shape (Command + ToolMessage, not a
# plain dict) diverges from the template.
#
# Tool NAMES are part of the model-binding contract (the model calls
# by name; ``_ANALYSIS_PAYLOAD_KEYS`` in ``stream_map`` maps them to
# payload keys). Renaming any of them would silently drop a tool from
# the prompt.


# ----- memory-tool factories (T6 / spec SC12-SC13) -----
#
# Three explicit memory tools surface only when the injected memory port
# is enabled. The OFF branch (port is ``None`` or ``enabled is False``)
# binds NONE of them — the compiled graph's tool set is the existing
# eight ToolNode tools, identical to the pre-memory surface (T3 / SC3).
#
# The ``dream`` tool is NEVER created in any branch (map G4). No code
# path here references a dream tool name, so the wiring test that
# asserts ``"dream" not in compile_graph signature params`` stays green
# regardless of which branch compiled the graph.
#
# ``user_profile_store`` is injectable so tests can use ``tmp_path``
# roots; the production entry point builds one with both layers at
# ``$HOME/.fin/user.md`` (spec SC6 / ADR 0049 — no workspace overlay).


def _make_save_memory_tool(
    memory_port: MemoryPort,
) -> Any:
    """Build the explicit ``save_memory`` tool (spec SC12).

    Title + body text are forwarded to ``memory_port.extract`` so the
    operator's hand-curated row lives in the same store the auto-extract
    path writes to. The workspace key is resolved PER CALL from
    ``configurable.workspace_root`` (spec SC8 / ADR 0049) — the tool
    does NOT close over a compile-time path. A missing or empty
    ``workspace_root`` raises :class:`WorkspaceRootMissingError`; the
    graph never silently defaults to ``Path.cwd()`` or ``$HOME``.

    The tool returns a dict the ToolNode serializes as ToolMessage JSON
    so the model can confirm the row landed (and the operator can see
    the advisory marker).
    """

    @tool("save_memory")
    def save_memory_tool(
        title: str,
        body: str,
        config: Annotated[RunnableConfig, InjectedToolArg()],
    ) -> dict:
        """Persist one cross-session memory row (title + body).

        ``title`` is a short label; ``body`` is the full prose (the
        first non-empty line becomes the memory hook — ≤80 chars). The
        row is workspace-scoped (resolved from
        ``configurable.workspace_root`` at call time) and is advisory
        context, never citation evidence.
        """
        workspace = str(_resolve_workspace_root(config))
        result = memory_port.extract(
            body,
            metadata={"title": title},
            workspace=workspace,
        )
        return result

    return save_memory_tool


def _make_recall_memory_tool(
    memory_port: MemoryPort,
) -> Any:
    """Build the explicit ``recall_memory`` tool (spec SC12).

    The returned payload carries the same ``advisory: True`` and
    ``citation_eligible: False`` markers the host prefetch uses so the
    citation verifier refuses memory rows as evidence (ADR 0035 /
    0046). The workspace key is resolved PER CALL from
    ``configurable.workspace_root`` (spec SC8 / ADR 0049) — the tool
    does NOT close over a compile-time path.
    """

    @tool("recall_memory")
    def recall_memory_tool(
        query: str,
        limit: int = 8,
        config: Annotated[RunnableConfig, InjectedToolArg()] = None,
    ) -> dict:
        """Return advisory memory rows that lexically hit ``query``.

        ``query`` is the operator's recall cue; ``limit`` is the cap
        (default 8). The payload is advisory context — it cannot
        satisfy the citation verifier. Rows are workspace-scoped
        (resolved from ``configurable.workspace_root`` at call time).
        """
        workspace = str(_resolve_workspace_root(config))
        return memory_port.recall(query, limit=limit, workspace=workspace)

    return recall_memory_tool


def _make_edit_user_profile_tool(
    user_profile_store: UserProfileStore,
) -> Any:
    """Build the layered ``edit_user_profile`` tool (spec SC13).

    ``layer`` selects which file the tool writes:

    * ``"workspace"`` — ``<workspace>/.fin/user.md`` (overlay)
    * ``"global"`` — ``$HOME/.fin/user.md``

    Writing one layer NEVER rewrites the other; the contract is
    file-isolated on disk. ``UserProfileStore.write_layer`` raises
    :class:`UserProfileUnknownLayerError` for any other name, and the
    error rides on the ToolMessage so the model sees a readable refusal
    instead of a crashed run.
    """

    @tool("edit_user_profile")
    def edit_user_profile_tool(layer: str, text: str) -> dict:
        """Write ``text`` to one user-profile layer.

        ``layer`` is ``"workspace"`` or ``"global"``; an unknown name
        returns the typed ``user_profile_unknown_layer`` dict without
        raising. Writing workspace does NOT rewrite global (SC13).
        """
        try:
            user_profile_store.write_layer(layer, text)  # type: ignore[arg-type]
        except UserProfileUnknownLayerError as exc:
            return {"error": "user_profile_unknown_layer", "layer": exc.layer}
        return {"status": "ok", "layer": layer}

    return edit_user_profile_tool


@dataclass(frozen=True)
class _LocatorToolSpec:
    """One locator-analysis tool the graph binds (T6).

    Bundles the public name (the model binds by it), the lazily
    imported implementation it delegates to, and the factory kwargs
    the graph resolved once (``manifest_path`` / ``extracted_root`` /
    ``facts_root``). ``signature`` is the Python parameter source the
    wrapped tool exposes — kept as a string so
    ``_make_locator_tool`` can build the wrapper via ``exec`` and
    preserve the live Pydantic-compatible annotations LangChain's
    schema inference needs at @tool-decoration time. ``coerce_fn``
    is the per-impl shim that mirrors the pre-refactor list / None
    coercions (``list(x or [])`` etc.) so each tool's kwarg map
    stays exactly what it was before the consolidation.
    """

    name: str
    description: str
    signature: str
    impl_module: str
    impl_attr: str
    factory_kwargs: Mapping[str, Any]
    coerce_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _extract_param_names(signature: str) -> list[str]:
    """Parse parameter names out of a Python signature fragment.

    Handles the forms the locator-analysis tools actually use:
    ``name``, ``name: type``, ``name=default``, ``name: type = default``.
    ``*args`` / ``**kwargs`` are skipped — none of the consolidated
    tools use them. The names are returned in declaration order so
    the wrapper body can mirror the source 1-to-1.
    """
    names: list[str] = []
    for part in signature.split(","):
        piece = part.strip()
        if not piece or piece.startswith("*"):
            continue
        # ``name: type`` or ``name: type = default``
        name = piece.split(":", 1)[0].strip()
        name = name.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _make_locator_tool(spec: _LocatorToolSpec) -> Any:
    """Build one locator-analysis host tool from a spec.

    The wrapper is built via ``exec`` so its parameter types stay
    live Pydantic-compatible annotations (LangChain's schema
    inference calls ``validate_arguments(func)``, which reads
    ``__annotations__`` directly — copying ``__signature__`` onto a
    ``**kwargs`` shim would silently drop the types and produce an
    empty schema).

    The body lazy-imports ``spec.impl_attr`` from
    ``spec.impl_module`` on first call and forwards the wrapper's
    kwargs merged with ``spec.factory_kwargs`` (the paths the graph
    resolved once). ``spec.coerce_fn`` runs first when present so
    the per-impl ``list(x or [])`` shims apply before the invoke.

    Returns the same ``StructuredTool`` shape the previous
    per-tool factories produced — same name, same schema, same
    return payload shape. The four consolidated factories below
    are now thin spec bindings.
    """
    param_names = _extract_param_names(spec.signature)
    forward_lines = "".join(
        f"    forwarded[{name!r}] = {name}\n" for name in param_names
    )
    coerce_line = (
        "    forwarded = coerce_fn(forwarded)\n"
        if spec.coerce_fn is not None
        else ""
    )
    source = (
        f"def _wrapped({spec.signature}) -> dict:\n"
        "    forwarded = dict(factory_kwargs)\n"
        f"{forward_lines}"
        f"{coerce_line}"
        "    module = importlib.import_module(impl_module)\n"
        "    impl = getattr(module, impl_attr)\n"
        "    return impl(**forwarded)\n"
    )
    namespace = {
        "importlib": importlib,
        "impl_module": spec.impl_module,
        "impl_attr": spec.impl_attr,
        "factory_kwargs": dict(spec.factory_kwargs),
        "coerce_fn": spec.coerce_fn,
    }
    exec(compile(source, f"<locator-tool:{spec.name}>", "exec"), namespace)
    wrapped = namespace["_wrapped"]
    wrapped.__doc__ = spec.description
    wrapped.__name__ = f"{spec.name}_tool"
    return tool(spec.name)(wrapped)


# Per-impl coerce shims. Match the pre-refactor coercions exactly so
# each tool's kwarg map stays bit-for-bit equivalent (T3 / spec Focus
# 1 numeric gate reads ``compare_facts`` columns; the empty /
# non-empty split is load-bearing).


def _coerce_compare_facts(forwarded: dict[str, Any]) -> dict[str, Any]:
    """Match ``compare_facts``'s pre-refactor list / None coercion."""
    forwarded["periods"] = list(forwarded.get("periods") or [])
    document_ids = forwarded.get("document_ids")
    forwarded["document_ids"] = (
        list(document_ids) if document_ids else None
    )
    issuers = forwarded.get("issuers")
    forwarded["issuers"] = list(issuers) if issuers else None
    return forwarded


def _coerce_statement(forwarded: dict[str, Any]) -> dict[str, Any]:
    """Match ``three_year_statement``'s pre-refactor list coercion."""
    forwarded["periods"] = list(forwarded.get("periods") or [])
    forwarded["concepts"] = list(forwarded.get("concepts") or [])
    return forwarded


# The four consolidated factories. Each is now a one-line spec
# binding; the @tool + lazy-import + kwargs-passthrough template is
# in ``_make_locator_tool``.


_COMPARE_FACTS_DESCRIPTION = (
    "Compare one XBRL concept across periods and/or issuers.\n"
    "\n"
    "``concept`` is the XBRL tag (e.g. ``us-gaap:Revenues``).\n"
    "``periods`` are the reporting periods to use as columns\n"
    "(e.g. ``[\"2022-Q3-YTD\", \"2023-Q3-YTD\"]``). Rows come from\n"
    "``issuers`` (tickers, resolved across that issuer's filings —\n"
    "use this for cross-period) or ``document_ids`` (exact\n"
    "filings); with neither, every filing holding facts is a row.\n"
    "Returns a structured comparison table with per-cell locators\n"
    "and code-computed deltas. Cells with no fact row are marked\n"
    "``missing``; an unresolvable request returns the typed\n"
    "``empty_comparison`` / ``comparison_overflow`` dict. Never\n"
    "raises. Quote only the values this table reports."
)

_REDLINE_DESCRIPTION = "Compare paragraph text between two frozen filings."

_EXTRACT_TABLES_DESCRIPTION = (
    "Extract pipe-delimited tables from one frozen page."
)

_STATEMENT_DESCRIPTION = (
    "Render three requested years from XBRL fact sidecars."
)


def _make_compare_facts_tool(facts_root: Path):
    """Build the ``compare_facts`` host tool (T3 / spec Focus 1).

    Cross-period and peer comparison is a HOST TOOL result, not a
    prose table the model writes: the tool resolves each cell
    against the ingest-built fact table (locator per cell) and
    computes the deltas itself. Like ``get_xbrl_fact`` it
    contributes NO ``EvidenceItem`` — a fact row is not page text,
    so the citation verifier still requires a read / retrieve span
    before any prose answer passes.

    Returns a plain dict so the prebuilt ``ToolNode`` serializes
    it as ToolMessage JSON; ``tool_node_with_summaries`` then
    lifts the ``comparison`` envelope onto ``AgentState`` for the
    turn row, and the numeric gate reads its columns as
    ``(concept, period)`` claims.
    """
    return _make_locator_tool(
        _LocatorToolSpec(
            name="compare_facts",
            description=_COMPARE_FACTS_DESCRIPTION,
            signature=(
                "concept: str, "
                "periods: list[str], "
                "document_ids: list[str] | None = None, "
                "issuers: list[str] | None = None"
            ),
            impl_module="fin.host.tools.compare_facts",
            impl_attr="compare_facts",
            factory_kwargs={"facts_root": facts_root},
            coerce_fn=_coerce_compare_facts,
        )
    )


def _make_redline_tool(manifest_path: Path, extracted_root: Path):
    """Build the locator-bound paragraph redline host tool."""
    return _make_locator_tool(
        _LocatorToolSpec(
            name="redline",
            description=_REDLINE_DESCRIPTION,
            signature=(
                "before_document_id: str, "
                "before_version: str, "
                "after_document_id: str, "
                "after_version: str"
            ),
            impl_module="fin.host.tools.redline",
            impl_attr="redline",
            factory_kwargs={
                "manifest_path": manifest_path,
                "extracted_root": extracted_root,
            },
        )
    )


def _make_table_extract_tool(manifest_path: Path, extracted_root: Path):
    """Build the locator-bound page table extraction tool."""
    return _make_locator_tool(
        _LocatorToolSpec(
            name="extract_tables",
            description=_EXTRACT_TABLES_DESCRIPTION,
            signature=(
                "document_id: str, "
                "version: str, "
                "page: int = 1"
            ),
            impl_module="fin.host.tools.table_extract",
            impl_attr="extract_tables",
            factory_kwargs={
                "manifest_path": manifest_path,
                "extracted_root": extracted_root,
            },
        )
    )


def _make_statement_tool(facts_root: Path):
    """Build the XBRL-backed three-year statement tool."""
    return _make_locator_tool(
        _LocatorToolSpec(
            name="three_year_statement",
            description=_STATEMENT_DESCRIPTION,
            signature=(
                "issuer: str, "
                "periods: list[str], "
                "concepts: list[str]"
            ),
            impl_module="fin.host.tools.statements",
            impl_attr="three_year_statement",
            factory_kwargs={"facts_root": facts_root},
            coerce_fn=_coerce_statement,
        )
    )


class IssuerTask(TypedDict, total=False):
    """The payload one ``Send`` carries to a fan-out worker (T6).

    Declared as the worker node's input schema so a branch reads its
    own issuer instead of the whole ``AgentState``: a worker that could
    see the accumulated evidence of its siblings would be a per-issuer
    node that quietly depends on join order.
    """

    issuer: str
    query: str
    # SC2: the 档 this run's cascade assigned, stamped by the router off
    # the run's OWN state (never by the model, whose tool args only carry
    # the roster + query). ``None`` means no assignment → no key sent.
    search_mode: str | None


def _make_research_issuers_tool(issuer_cap: int):
    """Build the ``research_issuers`` schema the model binds against
    (T6 / ADR 0034 / spec Focus 4).

    This is the one bound tool the prebuilt ``ToolNode`` does NOT
    execute: a call by this name is intercepted in ``from_model`` and
    served by the fan-out planner / worker / join nodes, because ADR
    0034 makes the fan-out **graph topology** rather than a tool body.
    Running it as an ordinary tool would put the per-issuer work back
    inside one serial node — exactly what Focus 4 asks the host graph
    to stop doing. The body below is therefore unreachable in the
    compiled graph; it returns a typed marker instead of raising so a
    future rewiring that DID hand it to the ToolNode surfaces as a
    readable tool result rather than a crashed run.
    """

    @tool(ISSUER_FANOUT_TOOL)
    def research_issuers_tool(issuers: list[str], query: str) -> dict:
        """Research one question across several issuers at once.

        ``issuers`` are tickers (e.g. ``["AAPL", "MSFT"]``), at most
        {cap} of them; ``query`` is the single question asked of each.
        The host graph researches every issuer, joins the results, and
        returns one envelope naming which issuers had supporting
        passages. More than {cap} issuers returns the typed
        ``issuer_overflow`` dict — ask about fewer issuers instead.
        Cite at least one verbatim passage for every issuer that came
        back with evidence.
        """
        return {
            "error": "fanout_not_dispatched",
            "limit": issuer_cap,
            "issuers": list(issuers or []),
        }

    # ``@tool`` freezes the docstring as the model-visible description,
    # so the cap is substituted after the fact rather than f-stringed
    # into a docstring literal (which Python would not accept).
    research_issuers_tool.description = (
        research_issuers_tool.description or ""
    ).format(cap=issuer_cap)
    return research_issuers_tool


def _first_fanout_call(state: AgentState) -> tuple[dict[str, Any], str]:
    """The ``(args, tool_call_id)`` of this turn's fan-out request.

    Only the FIRST such call is served: two fan-outs in one AIMessage
    would be two rosters racing for the same run-scoped channels, and
    the router only sends here when every pending call is a fan-out,
    so ignoring the rest cannot strand an unrelated tool.
    """
    for name, args, call_id in _pending_tool_calls(state):
        if name == ISSUER_FANOUT_TOOL:
            return dict(args or {}), call_id
    return {}, ""


def _issuer_of(document_id: str) -> str:
    """The issuer key behind a ``document_id``.

    ``document_id`` is ``TICKER-YYYYQn`` (ADR 0006 / manifest), the
    same prefix convention ``compare_facts`` uses to resolve an issuer
    row across that issuer's filings.
    """
    return str(document_id or "").split("-", 1)[0].strip().upper()


def _issuer_finding(issuer: str, status: str, **extra: Any) -> dict[str, Any]:
    """The state update carrying ONE branch's non-hit finding row.

    Every branch reports, so ``empty`` and ``unavailable`` travel on
    the same additive channel as a hit and reach the join's envelope
    instead of being swallowed (spec SC7).
    """
    return {
        "issuer_findings": [
            {
                "issuer": issuer,
                "status": status,
                "chunk_count": 0,
                "locators": [],
                "error": "",
                **extra,
            }
        ]
    }


def _issuer_evidence(
    result: Any, *, ledger: MappingFailureLedger
) -> list[EvidenceItem]:
    """The retrieve result's chunks as ordinary ``EvidenceItem``s.

    Fan-out evidence is deliberately indistinguishable from a
    ``read_file`` page here, so the ONE citation verifier after the
    join treats both alike (ADR 0034). Text-less chunks are dropped:
    they can carry no citable span. Mapping-failed chunks go through
    the same admission as the single-issuer path (T17 / SC22), so a
    fan-out branch cannot be the way a retrieval-side identifier
    reaches the citation gate.
    """
    chunks = result.get("chunks", []) if isinstance(result, dict) else []
    admitted = admit_retrieved_chunks(chunks, ledger=ledger)
    return evidence_from_chunks(
        [chunk for chunk in admitted if getattr(chunk, "text", "")]
    )


def _issuer_hit_update(
    issuer: str, evidence: list[EvidenceItem]
) -> dict[str, Any]:
    """The state update for a branch that found citable passages."""
    return {
        "evidence": evidence,
        "issuer_findings": [
            {
                "issuer": issuer,
                "status": STATUS_HIT,
                "chunk_count": len(evidence),
                "error": "",
                "locators": [
                    {
                        "document_id": item.locator.document_id,
                        "version": item.locator.version,
                        "page": item.locator.page,
                    }
                    for item in evidence
                ],
            }
        ],
    }


def _uncited_contributors(
    fanout: dict[str, Any], citations: Iterable[Citation]
) -> list[str]:
    """Issuers that produced evidence but hold no citation (SC7).

    Spec Focus 4 wants the synthesis to keep "≥1 citation per issuer
    that contributed". An issuer whose fan-out returned passages and
    whose evidence the answer then quoted nowhere was dropped from the
    synthesis — the citation verifier alone cannot see that, because
    the OTHER issuers' spans already passed it. Empty and unavailable
    issuers are not contributors and are not required to be cited;
    they are reported in the envelope instead.
    """
    contributing = [
        str(issuer)
        for issuer in (fanout.get("contributing") or ())
        if str(issuer)
    ]
    if not contributing:
        return []
    cited = {_issuer_of(citation.document_id) for citation in citations}
    return [issuer for issuer in contributing if issuer not in cited]


def _clarify_missing_slots(state: AgentState) -> list[str]:
    """Return the slots the clarify leaf must still ask for.

    First source is this turn's ``track_decision.signals``, which
    the clarify tier fills from the utterance. When clarify was
    reached some other way (the LLM tier records no slots), fall
    back to the gaps in the latest status snapshot — the active
    routing view, whose dependent fields may be empty (CONTEXT
    *status snapshot*).
    """
    signals = dict((state.get("track_decision") or {}).get("signals") or {})
    recorded = signals.get("missing_slots")
    if isinstance(recorded, (list, tuple)):
        named = [str(slot) for slot in recorded if slot]
        if named:
            return named

    snapshots = list(state.get("status_snapshots") or [])
    latest = snapshots[-1] if snapshots else {}
    if not isinstance(latest, dict):
        latest = {}
    return [
        slot for slot in ("company", "period") if not latest.get(slot)
    ]


# ----- T3 helpers: tool-summary + thinking extraction -----


def _pending_tool_calls(state: AgentState) -> list[tuple[str, dict, str]]:
    """Return the (name, args, tool_call_id) tuples the last
    ``AIMessage`` declared.

    The prebuilt ``ToolNode`` consumes them but does not surface
    ``args`` back to the caller; we read it from the message list
    so the summary formatter can render
    ``"company=AAPL"`` / ``"p.4"`` / etc. without re-parsing raw
    ``ToolMessage`` JSON.
    """
    messages = state.get("messages", [])
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            return [
                (
                    call.get("name", ""),
                    call.get("args") or {},
                    call.get("id", ""),
                )
                for call in message.tool_calls
            ]
    return []


def _tool_result_for_call(call_id: str, tool_messages: list[BaseMessage]) -> dict | None:
    """Pick the ``ToolMessage`` matching ``call_id``.

    The result is JSON in ``ToolMessage.content``; the summarizer
    prefers parsed dicts (``{"filings": [...]}`` /
    ``{"chunks": [...]}`` / ``{"text": ..., "document_id": ...}``)
    and falls back to ``None`` for unparseable payloads (the
    formatter can still render ``args`` even when the result is
    missing - e.g. an error ToolMessage).
    """
    import json

    for tm in tool_messages:
        if not isinstance(tm, ToolMessage):
            continue
        if getattr(tm, "tool_call_id", "") != call_id:
            continue
        try:
            return json.loads(tm.content)
        except (TypeError, ValueError):
            return None
    return None


def _build_tool_summaries(
    pending: list[tuple[str, dict, str]],
    tool_messages: list[BaseMessage],
) -> list[dict[str, str]]:
    """Render one ``{name, summary, tool_call_id}`` dict per call.

    The summary text is the human one-liner the SPA renders in the
    ``#F5F5F4`` tools region (spec line 32); the ``tool_call_id``
    correlates with the ``tool_call`` SSE frame so the SPA can
    pair the live "running" indicator with the post-run trace.
    """
    rendered: list[dict[str, str]] = []
    for name, args, call_id in pending:
        # Empty tool_call_id still renders a row (turn-trace-folds
        # spec: GET must not drop those calls; the SPA keys rows with
        # an index fallback so empty ids stay stable).
        result = _tool_result_for_call(call_id, tool_messages)
        rendered.append(
            {
                "name": name,
                "summary": format_tool_summary(name, result=result, args=args),
                "tool_call_id": call_id,
            }
        )
    return rendered


def comparison_from_tool_message(message: BaseMessage) -> dict | None:
    """The ``fin.comparison.v1`` envelope a ``compare_facts``
    ``ToolMessage`` carries, stamped with its ``tool_call_id``.

    ``None`` for every other message, for a typed EXIT whose skeleton
    holds no columns (``empty_comparison`` / ``comparison_overflow``
    — nothing to render and nothing to gate), and for content that
    does not parse. The parse is defensive because this runs inside
    the graph: a malformed payload must not kill the run.
    """
    if not isinstance(message, ToolMessage):
        return None
    if getattr(message, "name", "") != "compare_facts":
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    comparison = payload.get("comparison")
    if not isinstance(comparison, dict) or not comparison.get("columns"):
        return None
    return {
        **comparison,
        "tool_call_id": getattr(message, "tool_call_id", "") or "",
    }


_ANALYSIS_PAYLOAD_KEYS = {
    "redline": "redline",
    "extract_tables": "table_extract",
    "three_year_statement": "statement",
}


def analysis_from_tool_message(message: BaseMessage) -> dict | None:
    """Extract a structured T6 analysis result from a ToolMessage."""
    if not isinstance(message, ToolMessage):
        return None
    payload_key = _ANALYSIS_PAYLOAD_KEYS.get(getattr(message, "name", ""))
    if payload_key is None:
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    analysis = payload.get(payload_key)
    if not isinstance(analysis, dict) or not analysis.get("schema"):
        return None
    return {
        **analysis,
        "tool_call_id": getattr(message, "tool_call_id", "") or "",
    }


def _fold_tool_update(raw_update: Any) -> dict[str, list[Any]]:
    """The unwrapped ``ToolNode`` result as ONE flat state update.

    Contract: when ANY wrapped tool returns a ``Command``, the prebuilt
    ``ToolNode._func`` answers a LIST of per-call entries — each either
    a ``Command(update={...})``, a plain ``{"messages": [...]}`` dict,
    or a LIST of those. LangGraph merges that list itself when a node
    returns it verbatim; the summarising wrapper must merge it manually
    and must keep every channel the tools wrote.

    Only ``messages`` / ``evidence`` / ``retrieve_outcomes`` are copied,
    which is exactly the set the wrapped tools (``read_file``,
    ``retrieve``) write and the wrapper re-emits. An unknown key would
    be silently dropped, so a tool that grows a new state channel must
    be added here too — the fold is the single place that knows the
    ToolNode return shape, so the node body does not restate it per
    branch.
    """
    merged: dict[str, list[Any]] = {
        "messages": [],
        "evidence": [],
        "retrieve_outcomes": [],
    }
    if isinstance(raw_update, dict):
        merged["messages"].extend(raw_update.get("messages", []))
        return merged
    if not isinstance(raw_update, list):
        return merged
    for entry in raw_update:
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            if isinstance(item, Command):
                payload = item.update or {}
                merged["messages"].extend(payload.get("messages", []))
                merged["evidence"].extend(payload.get("evidence", []))
                merged["retrieve_outcomes"].extend(
                    payload.get("retrieve_outcomes", [])
                )
            elif isinstance(item, dict):
                merged["messages"].extend(item.get("messages", []))
            elif isinstance(item, BaseMessage):
                merged["messages"].append(item)
    return merged


def _collect_comparisons(messages: list[BaseMessage]) -> list[dict]:
    """Every renderable comparison in ``messages``, in call order."""
    found: list[dict] = []
    for message in messages:
        comparison = comparison_from_tool_message(message)
        if comparison is not None:
            found.append(comparison)
    return found


def _collect_analysis_results(messages: list[BaseMessage]) -> list[dict]:
    """Every structured T6 analysis result, in tool-call order."""
    found: list[dict] = []
    for message in messages:
        analysis = analysis_from_tool_message(message)
        if analysis is not None:
            found.append(analysis)
    return found


def _extract_thinking_text(message: AIMessage) -> str:
    """Pull the thinking prose from an ``AIMessage`` for the turn row.

    Sources (in order):
    1. ``additional_kwargs.reasoning_details`` list — each entry's
       ``text`` field concatenated. This is the
       ``reasoning_split=true`` shape the MiniMax proxy emits
       (ADR 0022).
    2. Inline ``<thinking>...</thinking>`` blocks already stripped
       from ``message.content`` by ``_last_ai_text`` — we read
       ``content`` here so a proxy that ONLY inlined thinking (no
       reasoning_details) still surfaces the prose to the turn row.
    """
    pieces: list[str] = []
    details = message.additional_kwargs.get("reasoning_details")
    if isinstance(details, list):
        for entry in details:
            if isinstance(entry, dict):
                text = entry.get("text")
                if isinstance(text, str):
                    pieces.append(text)
    # If no reasoning_details, fall back to stripping any inline
    # ``<thinking>`` block from content. The block is already
    # absent from the message's content in the split-channel case;
    # here we surface it for the persisted turn row.
    content = message.content
    if isinstance(content, str):
        pieces.extend(inline_thinking_segments(content))
    return "\n".join(piece for piece in pieces if piece)




# ----- the graph factory -----


def _locator_from_payload(payload: Any) -> Locator | None:
    """Decode an evidence-locator dict into a ``Locator``.

    Both the ``get_xbrl_fact`` row (``source_locator``) and a
    ``compare_facts`` cell (``locator``) carry the same three-field
    shape. A payload that is not a dict, is missing a field, or
    carries an unparseable page decodes to ``None`` — the caller
    drops the row rather than registering a fact the numeric gate
    could not trace back to a page.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return Locator(
            document_id=payload["document_id"],
            version=payload["version"],
            page=int(payload["page"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _collect_fact_tool_calls(
    messages: list[BaseMessage],
) -> tuple[list[tuple[str, str]], list[Fact]]:
    """Walk ``messages`` for ``get_xbrl_fact`` tool calls and the
    corresponding fact rows.

    Each ``AIMessage`` may carry one or more ``get_xbrl_fact``
    tool calls. The matching ``ToolMessage`` (same ``tool_call_id``)
    carries the JSON content with the fact row, or the
    ``fact_not_found`` miss shape. We pair them up so the numeric
    gate sees ``(concept, period)`` claims alongside the available
    fact rows.

    Returns
    -------
    claimed_concepts
        The ``(concept, period)`` tuples from each
        ``get_xbrl_fact`` tool call's args.
    available_facts
        The ``Fact`` rows whose ``ToolMessage`` content decoded to
        a hit dict (no ``error`` key). Miss shapes are dropped —
        the model's claim was already a no-hit, so the numeric
        gate's "no matching fact row" branch will fire.
    """
    from fin.corpus.facts import Fact

    claimed: list[tuple[str, str]] = []
    facts: list[Fact] = []
    for message in messages:
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if call.get("name") != "get_xbrl_fact":
                continue
            args = call.get("args") or {}
            concept = args.get("concept", "")
            period = args.get("period", "")
            if not isinstance(concept, str) or not isinstance(period, str):
                continue
            if not concept or not period:
                continue
            claimed.append((concept, period))
            call_id = call.get("id", "")
            for tm in messages:
                if not isinstance(tm, ToolMessage):
                    continue
                if getattr(tm, "tool_call_id", "") != call_id:
                    continue
                try:
                    payload = json.loads(tm.content)
                except (TypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict) or "error" in payload:
                    continue
                locator = _locator_from_payload(payload.get("source_locator"))
                if locator is None:
                    continue
                facts.append(
                    Fact(
                        concept=payload.get("concept", concept),
                        value=payload.get("value"),
                        period=payload.get("period", period),
                        unit=payload.get("unit", ""),
                        scale=payload.get("scale", ""),
                        source_locator=locator,
                    )
                )
                break
    return claimed, facts


def _comparison_column_claims(
    concept: str, comparison: dict
) -> list[tuple[str, str]]:
    """The ``(concept, period)`` claims one comparison table makes.

    Each COLUMN key is a period the model asked about, so it is an
    implicit numeric claim even when every cell under it missed.
    """
    claims: list[tuple[str, str]] = []
    for column in comparison.get("columns") or ():
        key = str((column or {}).get("key") or "")
        if key and (concept, key) not in claims:
            claims.append((concept, key))
    return claims


def _comparison_cell_fact(concept: str, cell: dict) -> "Fact | None":
    """Convert one HIT cell of a comparison table into a ``Fact``.

    A cell with no value (the ``missing`` shape) or an undecodable
    locator yields ``None``: the column's claim still stands, but
    there is no evidenced row behind it.
    """
    from fin.corpus.facts import Fact

    if cell.get("value") is None:
        return None
    locator = _locator_from_payload(cell.get("locator"))
    if locator is None:
        return None
    return Fact(
        concept=concept,
        value=cell["value"],
        period=str(cell.get("column") or ""),
        unit=str(cell.get("unit") or ""),
        scale=str(cell.get("scale") or ""),
        source_locator=locator,
    )


def _comparison_cell_facts(concept: str, comparison: dict) -> list["Fact"]:
    """Every evidenced cell of one comparison table, as ``Fact`` rows."""
    facts: list[Fact] = []
    for row in comparison.get("rows") or ():
        for cell in (row or {}).get("cells") or ():
            fact = _comparison_cell_fact(concept, cell)
            if fact is not None:
                facts.append(fact)
    return facts


def _collect_comparison_claims(
    messages: list[BaseMessage],
) -> tuple[list[tuple[str, str]], list["Fact"]]:
    """Read every ``compare_facts`` table as numeric claims + facts.

    T3 / spec SC2: a comparison is the model asking for numbers, so
    each COLUMN of a table is an implicit ``(concept, period)`` claim
    and each HIT cell is the evidenced row that claim must match. A
    ``missing`` cell contributes a claim with no fact, which is what
    makes "the comparison reported nothing, the answer reported a
    number" fail instead of passing silently.

    The delta rows are deliberately NOT registered as facts: they are
    arithmetic the tool performed over cells the gate already checks,
    and the gate's per-claim rule ("some fact matches some number in
    the answer") already tolerates a quoted delta.
    """
    claimed: list[tuple[str, str]] = []
    facts: list[Fact] = []
    for message in messages:
        comparison = comparison_from_tool_message(message)
        if comparison is None:
            continue
        concept = str(comparison.get("concept") or "")
        if not concept:
            continue
        for pair in _comparison_column_claims(concept, comparison):
            if pair not in claimed:
                claimed.append(pair)
        facts.extend(_comparison_cell_facts(concept, comparison))
    return claimed, facts


def _collect_statement_claims(
    messages: list[BaseMessage],
) -> tuple[list[tuple[str, str]], list["Fact"]]:
    """Treat XBRL statement cells as numeric claims for this run."""
    from fin.corpus.facts import Fact

    claimed: list[tuple[str, str]] = []
    facts: list[Fact] = []
    for message in messages:
        statement = analysis_from_tool_message(message)
        if not statement or statement.get("schema") != "fin.statement.v1":
            continue
        periods = {
            str((column or {}).get("key") or "")
            for column in statement.get("columns") or ()
            if isinstance(column, dict)
        }
        for row in statement.get("rows") or ():
            if not isinstance(row, dict):
                continue
            concept = str(row.get("concept") or row.get("key") or "")
            if not concept:
                continue
            for cell in row.get("cells") or ():
                if not isinstance(cell, dict):
                    continue
                period = str(cell.get("period") or "")
                if not period or (periods and period not in periods):
                    continue
                pair = (concept, period)
                if pair not in claimed:
                    claimed.append(pair)
                if cell.get("status") != "hit" or cell.get("value") is None:
                    continue
                locator = _locator_from_payload(cell.get("locator"))
                if locator is None:
                    continue
                facts.append(
                    Fact(
                        concept=concept,
                        value=cell["value"],
                        period=period,
                        unit=str(cell.get("unit") or ""),
                        scale=str(cell.get("scale") or ""),
                        source_locator=locator,
                    )
                )
    return claimed, facts


def _evaluate_numeric_gate_in_node(messages: list[BaseMessage]):
    """Run the numeric gate on the run's accumulated messages.

    Returns the ``NumericGateResult`` from
    :func:`evaluate_numeric_gate`, or ``None`` if the model made no
    numeric lookup this run — neither ``get_xbrl_fact`` (T5) nor
    ``compare_facts`` (T3). The gate is skipped in that case per T5
    spec acceptance.
    """
    from fin.host.numeric_gate import evaluate_numeric_gate

    fact_claims, fact_rows = _collect_fact_tool_calls(messages)
    compare_claims, compare_rows = _collect_comparison_claims(messages)
    statement_claims, statement_rows = _collect_statement_claims(messages)
    claimed = fact_claims + [
        pair for pair in compare_claims if pair not in fact_claims
    ]
    claimed.extend(
        pair
        for pair in statement_claims
        if pair not in claimed
    )
    if not claimed:
        # No numeric lookup -> gate is skipped. We return ``None`` so
        # the caller can distinguish "no claim" from "claim, gate
        # passed" (which would be ``outcome="pass"``).
        return None
    answer = _last_ai_text(messages)
    return evaluate_numeric_gate(
        answer=answer,
        claimed_concepts=claimed,
        available_facts=fact_rows + compare_rows + statement_rows,
    )


def compile_graph(
    *,
    checkpointer: Any,
    model: Any,
    adapter: RetrievePort | None = None,
    manifest_path: Path | str | None = None,
    extracted_root: Path | str | None = None,
    catalog_path: Path | str | None = None,
    facts_root: Path | str | None = None,
    known_document_ids: set[str] | None = None,
    step_cap: int = STEP_CAP,
    issuer_cap: int = MAX_ISSUERS,
    recursion_limit: int = RECURSION_LIMIT,
    jev_client: Any | None = None,
    retrieve_adapter: str = RETRIEVE_ADAPTER_FAKE,
    mcp_command: list[str] | None = None,
    locator_map: dict[str, "Locator"] | None = None,
    read_port: ReadPort | None = None,
    mapping_failure_ledger: MappingFailureLedger | None = None,
    degradation_ledger: DegradationLedger | None = None,
    summarizer: Any | None = None,
    memory_port: MemoryPort | None = None,
    user_profile_store: UserProfileStore | None = None,
    enable_choice_window: bool = False,
) -> "_LockedGraph":
    """Build the v1 host graph (T6).

    Parameters
    ----------
    checkpointer
        A LangGraph ``Checkpointer``. Tests pass
        ``InMemorySaver()``; production (T8) passes
        ``PostgresSaver.from_conn_string(os.environ[
        "FIN_CHECKPOINT_URL"])``. The factory does NOT instantiate
        the checkpointer itself (ADR 0015).
    model
        A LangChain ``BaseChatModel``. Tests pass
        ``ScriptedChatModel``; production (T7 / T9) passes the
        ``ChatOpenAI`` instance built by T8's
        ``compile_for_runtime`` from ``FIN_MODEL`` + ``FIN_BASE_URL``.
        The factory calls ``bind_tools`` on it to register the
        host tools (T5: ``get_xbrl_fact`` is the fourth tool,
        bound AFTER ``retrieve`` to preserve insertion order;
        LangChain's tool binding is order-sensitive).
    adapter
        An already-built retrieve adapter (``FakeRetrievePort`` /
        ``MCPRetrieveAdapter`` / ``BM25Adapter``). This is the
        most explicit configuration there is, so it wins over
        ``retrieve_adapter``. If ``None``, the factory
        auto-builds the kind named by ``retrieve_adapter``.

        ``BM25Adapter`` is no longer the product default
        (ADR 0030); it remains importable for legacy / offline
        tests but the factory does not auto-build it.

        When the adapter offers a ``read`` method (both
        ``FakeRetrievePort`` and ``MCPRetrieveAdapter`` do, ADR
        0047), it is the default ``read_file`` body source unless
        ``read_port`` is supplied explicitly.
    manifest_path
        Path to ``data/corpus/manifest.json``. Defaults to the
        repo's canonical location. Used by the ``list_files`` /
        ``redline`` / ``table_extract`` tool wiring only — the
        retrieve and ``read_file`` evidence paths never touch it
        (ADR 0046 / 0047).
    extracted_root
        Path to ``data/corpus/extracted/``. Defaults to the
        repo's canonical location. Same scope as
        ``manifest_path``: legacy tool wiring only.
    catalog_path
        T2 (ADR 0047): path to the catalog metadata table
        ``data/corpus/catalog.json`` over ingested/pushed filings.
        Defaults to the repo's canonical location. The
        ``list_files`` tool reads ONLY this table — a missing or
        empty table is the empty catalog ``{"filings": []}``, never
        a tree scan. Tests inject a ``tmp_path``-rooted table
        (built via ``fin.corpus.catalog.record_filings``).
    known_document_ids
        Optional static locator allow-list for the citation
        verifier. ``None`` (the default, the product boot shape)
        means the verifier's allow-list is THIS run's evidence
        set (ADR 0046: cite only from this-run payloads). When
        supplied, it is unioned with the run's evidence — never
        read from a manifest or extracted tree.
    step_cap
        The agent-loop step fuse. Default ``STEP_CAP`` (32).
    issuer_cap
        T6 (spec Focus 4 / ADR 0034): the most issuers one
        ``research_issuers`` fan-out may cover. Default
        ``MAX_ISSUERS`` (5, the ADR's recommendation). A roster over
        this cap ends the run with the typed ``issuer_overflow``
        reason rather than fanning out and dropping the remainder.
    recursion_limit
        LangGraph's recursion limit. Default ``RECURSION_LIMIT``
        from ``fin.host.state`` (currently ``3 * STEP_CAP + 9 = 105``).
        Strictly greater than ``step_cap`` so the agent-loop fuse
        fires first (spec success criteria item 5).
    facts_root
        Path to ``data/corpus/facts/`` (T5). Defaults to the repo's
        canonical location. The factory wires the
        ``get_xbrl_fact`` host tool to read from this root.
        Tests inject a synthetic ``tmp_path``-rooted tree.
    jev_client
        Soft-Track gray zone: a Jev closed-set Choice provider
        (``JevChoiceProvider``) for ambiguous utterances, used twice in
        a turn — once for the four tracks in ``decide_track`` and again
        for the 检索档 of a grounded turn in
        ``search_mode_update_for_turn``. When ``None`` (missing
        ``FIN_JEV_API_KEY``) each gray zone fail-closes to its own
        residual — ``track="clarify"``, or the 档 choice window — and
        never a silent default.
    retrieve_adapter
        T3 (#76): which adapter to auto-build when ``adapter`` is
        ``None`` — ``"fake"`` (the factory default: hermetic
        ``FakeRetrievePort``, no MCP subprocess, no live data) or
        ``"mcp"`` (the product ``MCPRetrieveAdapter``, ADR 0030).
        The production entry points in ``fin.host.compile``
        resolve this from ``FIN_RETRIEVE_ADAPTER`` and pass it
        explicitly. The choice is NEVER inferred from the
        checkpointer type: which saver holds state says nothing
        about where retrieve comes from, and inferring it meant
        ``fin-serve`` without ``FIN_CHECKPOINT_URL`` silently
        served an always-empty fake port.
    mcp_command
        T3 (#76): launch command for the MCP server used when
        ``retrieve_adapter`` is ``"mcp"`` and no ``adapter`` was
        supplied. Defaults to ``["iknow-memory"]`` (兜底非契约).
    locator_map
        T3 (#76): explicit compatibility map from ``retrieval_id``
        (or a legacy ``source_ref`` key) to ``fin.Locator``, passed
        to the auto-built ``MCPRetrieveAdapter``. Canonical
        ``source_ref`` hits (ADR 0047's wire contract) need no map,
        so ``None`` (the default) builds the adapter with an empty
        map and never reads the extracted tree; hits that cannot
        map are dropped and counted on the mapping ledger —
        visible, never silent.
    read_port
        T1 (ADR 0046 / 0047): the ``read_file`` body source.
        ``None`` (the default) resolves in order: the bound
        adapter's own ``read`` when it has one (``FakeRetrievePort``
        serves its ``pages_by_doc`` manifest, ``MCPRetrieveAdapter``
        answers the named ``read_unavailable`` EXIT until iknow
        grows a read tool), otherwise a typed-miss port. An
        explicit port wins over both. The factory never builds a
        tree-backed read port itself — the local extracted tree is
        not a serve-path SSOT.
    mapping_failure_ledger
        T17 (SC22): where hits that fail to map back to a locator
        are counted. ``None`` (the default) resolves the ledger
        from ``FIN_MAPPING_FAILURE_LOG`` — a path persists the
        count, unset keeps it in memory. The same ledger is shared
        by the auto-built MCP adapter and the host's evidence
        admission, so one number covers both drop sites.
    degradation_ledger
        T20 (SC39 / §B3): where a retrieval response's
        ``degraded_reasons`` are recorded, and where the matching
        user-facing notice is recorded against them. ``None`` (the
        default) resolves the ledger from ``FIN_DEGRADATION_LOG``.
        Shared by the ``retrieve`` tool and the fan-out workers, so
        「记录原因且暴露信号」is one count over every consumption
        point rather than one per call site.
    summarizer
        ADR 0044 / plan T1: the injectable seam for the session-
        compaction gate. ``None`` (the default) uses the v1 chat
        model — the same ``model`` object this function received —
        so the live host compacts with the production model. Tests
        inject a stub. Mirrors ``fin.host.compaction.SummaryModel``
        Protocol DI shape (``.invoke`` or callable). The gate
        catches ``SessionSummaryError`` so a model outage cannot
        refuse the operator turn (ADR 0044: "summary failure is
        typed and must not refuse the operator turn").
    memory_port
        T6 / spec SC12: the explicit memory-port seam. ``None`` (the
        default, the boot shape per T3 / SC2 / SC3) keeps the graph
        OFF — the compiled graph exposes no memory tool, and the
        ToolNode's tool list is exactly the existing eight tools.
        When supplied AND ``memory_port.enabled is True``, three
        tools surface: ``save_memory`` (title + body → extract),
        ``recall_memory`` (query → advisory payload), and
        ``edit_user_profile`` (layer + text → layered file write).
        There is NO dream tool in any branch (map G4). When
        ``memory_port`` is supplied but ``enabled`` is ``False``,
        the same OFF branch as ``None`` applies: the three memory
        tools are NOT bound.
    workspace_root
        **REMOVED in Wave A (ADR 0049).** The per-call 工作区 is now
        carried in ``configurable.workspace_root`` (injected by HTTP
        / CLI from :func:`fin.host.workspace_registry.default_workspace_root`).
        ``compile_graph`` no longer bakes any path — neither
        ``Path.cwd()`` nor ``$HOME/.fin/default`` — so the graph
        reads the seam-injected value at every tool call and raises
        :class:`WorkspaceRootMissingError` when the seam forgot it.
        Keep this parameter out of the signature: SC8 forbids silent
        defaults inside the runtime.
    user_profile_store
        T6 / spec SC13 + that pull request operator decision: the two-layer
        profile file store. ``None`` (the default) builds one rooted
        at ``$HOME/.fin/user.md`` (both layers point at the same file
        — spec SC6: product reads ``$HOME/.fin/user.md`` only, no
        workspace overlay). that pull request made this unconditional — the
        store is built regardless of ``memory_port`` because the user
        profile is operator-profile machinery, NOT memory-fact
        machinery, and the merged profile text surfaces into the
        system prefix on every leaf (research AND non-research)
        regardless of memory_on. The WRITE path (``edit_user_profile``
        tool) stays ON-gated per spec SC12. Tests inject an explicit
        store rooted at a ``tmp_path`` so the suite never touches the
        real ``$HOME``.
    enable_choice_window
        search-mode host T2 / ADR 0062 HITL: when ``True``, a grounded
        turn whose 检索档 cascade resolves to ``clarify`` pauses the run
        on a LangGraph ``interrupt`` (the choice window) BEFORE the
        grounded leaf executes, and ``POST /threads/{id}/resume`` (or
        ``Command(resume=pick)``) continues it with the operator's 档.
        Default ``False`` keeps the pre-T2 flow intact — the opt-in
        suites and the durable production boot (``compile_for_runtime``)
        are what turn the HITL lane on. Requires a checkpointer (the
        factory always has one; the interrupt's durability IS the
        checkpoint).

    Returns
    -------
    _LockedGraph
        A proxy object whose ``invoke`` / ``stream`` / ``ainvoke``
        enforce one-at-a-time execution per ``thread_id``.

    Raises
    ------
    RetrieveAdapterConfigError
        If ``retrieve_adapter`` names an unknown kind.
    """
    # One mapping-failure counter per graph, shared by the retrieve
    # adapter's mapper-drop and the host's evidence admission (T17):
    # both are the same SC22 failure, so they must not be two numbers.
    if mapping_failure_ledger is None:
        mapping_failure_ledger = ledger_from_env()

    # One degradation ledger per graph for the same reason (T20): the
    # single-issuer tool and every fan-out worker consume the same §B3
    # signal, and SC39 counts reasons across the run, not per seam.
    if degradation_ledger is None:
        degradation_ledger = degradation_ledger_from_env()

    # Build the adapter if the caller did not inject one. The kind
    # comes from configuration only (``retrieve_adapter``), so a
    # test checkpointer cannot force a fake port on a production
    # boot and a Postgres saver cannot force MCP on a test.
    if adapter is None:
        if retrieve_adapter not in RETRIEVE_ADAPTER_KINDS:
            raise RetrieveAdapterConfigError(
                f"unknown retrieve adapter kind {retrieve_adapter!r}; "
                f"expected one of {list(RETRIEVE_ADAPTER_KINDS)}"
            )
        if retrieve_adapter == RETRIEVE_ADAPTER_FAKE:
            # Hermetic fake port (ADR 0030 / spec success-criteria
            # item 1: default pytest talks to no MCP process).
            if known_document_ids is None:
                known_document_ids = set()
            adapter = FakeRetrievePort(known_document_ids=known_document_ids)
        else:
            # Product MCP adapter (ADR 0030), built WITHOUT the
            # extracted tree (ADR 0046 / 0047): canonical
            # ``source_ref`` hits need no locator map, the client-side
            # document membership test defers to the MCP server when
            # no allow-list is configured, and unmapped hits are
            # dropped and counted on the mapping ledger — visible,
            # never silent.
            adapter = MCPRetrieveAdapter(
                command=mcp_command or ["iknow-memory"],  # 兜底非契约
                locator_map=locator_map or {},
                known_document_ids=known_document_ids,
                timeout_seconds=5.0,
                mapping_ledger=mapping_failure_ledger,
            )

    # Resolve the read_file body source (ADR 0046 / 0047): the most
    # explicit port wins; otherwise the adapter's own ``read`` (the
    # fake's pages manifest / the MCP named-exit); otherwise a typed
    # -miss port for adapters without a read face (legacy BM25). The
    # factory never reads the extracted tree here — the tree is not a
    # serve-path SSOT.
    if read_port is None and hasattr(adapter, "read"):
        read_port = adapter  # type: ignore[assignment]
    if read_port is None:
        read_port = MissReadPort()

    mpath = _resolve_path(manifest_path, _DEFAULT_MANIFEST_PATH)
    rpath = _resolve_path(extracted_root, _DEFAULT_EXTRACTED_ROOT)
    fpath = _resolve_path(facts_root, _DEFAULT_FACTS_ROOT)
    cpath = _resolve_path(catalog_path, _DEFAULT_CATALOG_PATH)

    list_files_tool = _make_list_files_tool(cpath)
    read_file_tool = _make_read_file_tool(read_port)
    retrieve_tool = _make_retrieve_tool(
        adapter,
        mapping_ledger=mapping_failure_ledger,
        degradation_ledger=degradation_ledger,
    )
    get_xbrl_fact_tool = _make_get_xbrl_fact_tool(fpath)
    # T3 (spec Focus 1): bound LAST so the four prior tools keep
    # their positions — LangChain's tool binding is order-sensitive
    # and existing tests assert those indices.
    compare_facts_tool = _make_compare_facts_tool(fpath)
    # T6 (spec Focus 4): bound last, and bound ONLY to the model — the
    # ToolNode below deliberately does not receive it (see
    # ``_make_research_issuers_tool``).
    research_issuers_tool = _make_research_issuers_tool(issuer_cap)
    redline_tool = _make_redline_tool(mpath, rpath)
    table_extract_tool = _make_table_extract_tool(mpath, rpath)
    statement_tool = _make_statement_tool(fpath)

    # T6 (spec SC12 / SC13 / map G4): when memory is ON, expose exactly
    # three tools — save / recall / edit_user_profile. OFF keeps the
    # tool list at the existing eight ToolNode tools. No dream tool is
    # EVER created in any branch.
    memory_enabled = bool(memory_port is not None and memory_port.enabled)
    save_memory_tool: Any | None = None
    recall_memory_tool: Any | None = None
    edit_user_profile_tool: Any | None = None
    # that pull request operator decision: the user profile is operator-profile
    # machinery, NOT memory-fact machinery. Its READ path (merged
    # profile text into the system prefix) is unconditional — both
    # ON and OFF graphs need a store to call ``read_merged()`` at
    # model-call time. The write path (edit_user_profile tool)
    # remains ON-gated per spec SC12.
    #
    # spec SC6 (ADR 0049, Wave A): the product path reads
    # ``$HOME/.fin/user.md`` ONLY — there is no workspace overlay
    # in production. We construct a store with BOTH layers pointed at
    # the same global path so ``read_merged()`` returns the global
    # text (workspace layer is empty, so the overlay falls through).
    # Tests inject an explicit ``user_profile_store`` rooted at a
    # ``tmp_path``-built home so the suite never touches real ``$HOME``.
    if user_profile_store is None:
        home = os.environ.get("HOME") or str(Path.home())
        global_md = Path(home) / _USER_PROFILE_LAYER_DIR / GLOBAL_FILENAME
        user_profile_store = UserProfileStore(
            global_path=global_md,
            workspace_path=global_md,
        )
    # Closure: read the merged profile text at model-call time so a
    # file edit between calls is visible on the next turn (no caching
    # at compile time). Used by ``_non_research_leaf`` and
    # ``model_node`` to pass ``user_profile=`` into ``messages_for_model``.
    def _read_profile_text() -> str:
        return user_profile_store.read_merged()

    if memory_enabled:
        save_memory_tool = _make_save_memory_tool(
            memory_port,  # type: ignore[arg-type]
        )
        recall_memory_tool = _make_recall_memory_tool(
            memory_port,  # type: ignore[arg-type]
        )
        edit_user_profile_tool = _make_edit_user_profile_tool(user_profile_store)

    model_bound_tools: list[Any] = [
        list_files_tool,
        read_file_tool,
        retrieve_tool,
        get_xbrl_fact_tool,
        compare_facts_tool,
        research_issuers_tool,
        redline_tool,
        table_extract_tool,
        statement_tool,
    ]
    tool_node_tools: list[Any] = [
        list_files_tool,
        read_file_tool,
        retrieve_tool,
        get_xbrl_fact_tool,
        compare_facts_tool,
        redline_tool,
        table_extract_tool,
        statement_tool,
    ]
    if memory_enabled:
        # The three memory tools bind to BOTH the model and the ToolNode
        # so a call the model emits reaches the dispatcher and the
        # payload lands as a ToolMessage. Order matches the spec's
        # "save / recall / edit-user-profile" listing.
        model_bound_tools.extend(
            [save_memory_tool, recall_memory_tool, edit_user_profile_tool]
        )
        tool_node_tools.extend(
            [save_memory_tool, recall_memory_tool, edit_user_profile_tool]
        )

    bound_model = model.bind_tools(model_bound_tools)
    # Operator thinking off: OpenAI-compatible / 方舟 extra_body
    # ``thinking.type=disabled`` (docs: enabled | disabled | auto).
    # Do NOT send MiniMax ``adaptive`` or ``reasoning_split``.
    _thinking_off_body = {"thinking": {"type": "disabled"}}
    bound_model_no_thinking = bound_model.bind(extra_body=_thinking_off_body)
    # Soft-Track T1: non-research leaves call the UNBOUND model
    # (no bind_tools) with call-scoped per-track prompts.
    unbound_model_no_thinking = model.bind(extra_body=_thinking_off_body)

    tool_dispatcher = ToolNode(
        tool_node_tools,
        name="tool_node",
        handle_tool_errors=True,
    )

    # ----- node functions -----

    def classify_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """Soft-Track: tiered track decision before leaves.

        Passes ``status_snapshots`` and the optional ``jev_client``
        into ``decide_track`` (gray-zone Choice) and, for a grounded
        turn, into the 档 cascade's own judge call below. Appends
        snapshots when the utterance
        sets or changes research context. When the track is
        ``grounded``, also runs the 检索档 cascade
        (``fin.host.search_mode_decision``) — graph routing is
        unchanged: retrieve stays ONE tool.

        search-mode host T2 (SC4): an optional documented pin rides the
        run config (``configurable.pinned_search_mode``, stamped by the
        workspace send's ``tiers`` selector) — a pin assigns its 档 in
        the rules tier and never opens the choice window. The pin is
        run-scoped CONFIG, never checkpointed state, so a follow-up send
        cannot inherit a stale pin.
        """
        configurable = dict((config or {}).get("configurable") or {})
        pinned = configurable.get("pinned_search_mode")
        messages = list(state.get("messages") or [])
        prior_snapshots = list(state.get("status_snapshots") or [])
        decision = decide_track(
            messages,
            snapshots=prior_snapshots,
            jev_client=jev_client,
        )
        update: dict[str, Any] = {
            "track": decision.track,
            "track_decision": {
                "track": decision.track,
                "confidence": decision.confidence,
                "decision_tier": decision.decision_tier,
                "signals": dict(decision.signals),
            },
        }
        # Dialogue / off_topic need not append every turn; only when
        # the utterance carries research-context slots.
        text = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                content = message.content
                text = content.strip() if isinstance(content, str) else str(content)
                break
        delta = snapshot_delta_for_utterance(text, prior_snapshots)
        if delta and decision.track not in ("dialogue", "off_topic"):
            update["status_snapshots"] = delta
        if decision.track == "grounded":
            # The assigned 档 rides the append-only snapshot history —
            # the inherit source next turn; a company-change wipe
            # carries none and blocks cross-job inheritance.
            merged = [*prior_snapshots, *(update.get("status_snapshots") or [])]
            sm_update = search_mode_update_for_turn(
                messages,
                snapshots=merged,
                jev_client=jev_client,
                pinned_mode=str(pinned) if pinned else None,
            )
            update["search_mode_decision"] = sm_update["audit"]
            if sm_update["snapshot_patch"]:
                update["status_snapshots"] = [
                    *(update.get("status_snapshots") or []),
                    *sm_update["snapshot_patch"],
                ]
        return update

    def _select_unbound_model(config: RunnableConfig) -> Any:
        configurable = dict((config or {}).get("configurable") or {})
        if configurable.get("thinking_type") == "disabled":
            return unbound_model_no_thinking
        return model

    def _with_turn_attachments(
        state: AgentState, model_messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        """Prepend the turn-scoped attachment block, or return as-is.

        corpus-serve-ingest T4 (ADR 0047 thread attach): attachment
        context is injected CALL-SCOPED like the track prompt —
        prepended at model time and never written back to the
        checkpoint. ``reset_input_state`` clears the ``attachments``
        channel on the next send, so the text is visible on this
        run's model calls only. The block goes right after the track
        prompt (position 1) so the model reads the instructions
        before the attachment body.

        ADR 0047's visibility contract is unconditional for the turn
        ("gives the model that file for this turn only"), so EVERY
        leaf's model call gets it — the research ``model_node`` and
        the ``_non_research_leaf`` tracks alike; an attach turn that
        Soft-Track routes to dialogue must still expose the file.

        The attachment text never enters the ``evidence`` channel
        (tool_node owns it), so an attachment-only span cannot pass
        the citation gate (ADR 0046 / SC9).
        """
        attachment_message = attachment_context_message(
            state.get("attachments")
        )
        if attachment_message is None:
            return model_messages
        if not model_messages:
            return [attachment_message]
        return [
            model_messages[0],
            attachment_message,
            *model_messages[1:],
        ]

    def _non_research_leaf(
        state: AgentState,
        config: RunnableConfig,
        *,
        track: Literal["dialogue", "clarify", "off_topic"],
    ) -> dict[str, Any]:
        """Call unbound model with track prompt; done + empty citations."""
        selected = _select_unbound_model(config)
        missing_slots = (
            _clarify_missing_slots(state) if track == "clarify" else None
        )
        # that pull request: the user profile is operator-profile machinery,
        # NOT memory-fact machinery. Surface the merged profile text
        # into the system prefix regardless of memory_on. Read at
        # call time (not compile-time) so file edits between calls
        # take effect on the next turn.
        stream = selected.stream(
            _with_turn_attachments(
                state,
                messages_for_model(
                    state["messages"],
                    track=track,
                    missing_slots=missing_slots,
                    user_profile=_read_profile_text(),
                ),
            )
        )
        answer_message = assemble_final_ai_message(stream)
        this_call_thinking = _extract_thinking_text(answer_message)
        prior_thinking = str(state.get("thinking_text", "") or "")
        if prior_thinking and this_call_thinking:
            thinking_text = prior_thinking + "\n" + this_call_thinking
        else:
            thinking_text = prior_thinking or this_call_thinking
        answer = _last_ai_text([answer_message])
        previous = int(state.get("step_count", 0))
        return {
            "messages": [answer_message],
            "step_count": previous + 1,
            "answer": answer,
            "citations": (),
            "status": "done",
            "status_reason": "done",
            "thinking_text": thinking_text,
            "track": track,
        }

    def dialogue_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        return _non_research_leaf(state, config, track="dialogue")

    def clarify_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        return _non_research_leaf(state, config, track="clarify")

    def off_topic_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        return _non_research_leaf(state, config, track="off_topic")

    def mode_choice_node(state: AgentState) -> dict[str, Any]:
        """检索档 choice window (search-mode host T2 / ADR 0062 HITL).

        Reached ONLY on ``from_classify`` when the grounded cascade
        resolved to ``clarify`` — the run pauses here on ``interrupt``
        until the operator picks a 档, then proceeds to the SAME
        compaction-gate → grounded leaf path (retrieve stays ONE tool).
        The loop is the documented validate-then-reopen shape: an
        out-of-list pick reopens the window (never a silent C), and a
        pick of an UNOPENED option is merged as the 具名失败 typed_fail
        by ``apply_operator_pick``. No LLM call happens in this node, so
        LangGraph's replay-on-resume of the node body is side-effect
        free.
        """
        decision = dict(state.get("search_mode_decision") or {})
        options = list(decision.get("options") or []) or clarify_options()
        rejected: list[str] = []
        while True:
            window: dict[str, Any] = {
                "kind": "choice_window",
                "title": "选择检索档",
                "prompt": "为本次 grounded 提问选择检索档后继续。",
                "options": options,
            }
            if rejected:
                window["rejected_picks"] = list(rejected)
            raw = str(interrupt(window) or "").strip()
            # The option ids are lasting tokens and the pick is resolved
            # against them through the alias map — upper-casing a pick
            # here would destroy the lowercase ones and reject a client
            # that still sends a retired letter.
            picked = normalize_search_mode(raw)
            if picked is not None and any(
                str(option.get("id")) == picked for option in options
            ):
                break
            rejected.append(raw)
        assert picked is not None  # reached only through the match above
        merged = apply_operator_pick(
            decision,
            pick=picked,
            snapshots=state.get("status_snapshots"),
            rejected=rejected,
        )
        update: dict[str, Any] = {"search_mode_decision": merged["audit"]}
        if merged["snapshot_patch"]:
            update["status_snapshots"] = merged["snapshot_patch"]
        return update

    def extract_method_choice_node(state: AgentState) -> dict[str, Any]:
        """Fast 抽图 propose window (search-mode host T4 / ADR 0068 / 0079).

        Reached ONLY on ``from_classify`` when the run input carries an
        explicit ``{"proposed": "fast"}`` seed — the ordinary grounded
        flow never opens it. Same validate-then-reopen interrupt loop as
        ``mode_choice`` (no LLM call), and the payload stays
        ``kind="choice_window"`` so the SPA renders it unchanged.

        A resolved pick routes onto the corpus version switch through
        ``apply_extract_method_pick``: only a valid ``fast`` pick on a request
        naming a recorded ``(document_id, version)`` flips that version
        (the catalog then marks the rebuild intent and the audit reads
        ``applied is True``). Every other outcome applies nothing —
        the switch call is idempotent, so LangGraph's replay-on-resume
        of the node body cannot double-apply. No snapshot channel is
        touched: the extract method never rides the 档 audit.
        """
        request = dict(state.get("extract_method_proposal") or {})
        rejected: list[str] = []
        while True:
            raw = str(interrupt(propose_window_payload(rejected)) or "").strip()
            picked = normalize_extract_method(raw)
            if picked is not None:
                break
            rejected.append(raw)
        applied, apply_error = apply_extract_method_pick(
            request, picked, catalog_path=cpath
        )
        return {
            "extract_method_proposal": build_proposal_audit(
                request,
                picked=picked,
                rejected=rejected,
                applied=applied,
                apply_error=apply_error,
            )
        }

    def model_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        # Step 1: step-cap fuse. Increment ``step_count`` and check
        # BEFORE invoking the model — when the fuse fires we never
        # call the LLM again (spec success criteria item 5).
        previous = int(state.get("step_count", 0))
        next_step = previous + 1
        if next_step >= step_cap:
            # Step cap wins on the next entry: we have already
            # spent ``step_cap`` agent-loop ticks without reaching
            # done / refuse. ADR 0008 — this is the runaway fuse,
            # NOT a citation failure. No model ran on this entry,
            # so the run's accumulated thinking survives verbatim
            # (T4 #37: the turn row keeps what streamed so far).
            return {
                "messages": [],
                "step_count": next_step,
                "status": "step_cap",
                "status_reason": "step_cap",
                "thinking_text": str(state.get("thinking_text", "") or ""),
            }
        # Step 1b: T3 (#34) — pick the model binding for this run's
        # thinking preference. ``configurable.thinking_type ==
        # "disabled"`` (the operator's global off preference) swaps
        # to the no-thinking binding for EVERY model call in the
        # run (including bounce retries); absent / any other value
        # keeps the boot-time binding (adaptive, ADR 0022). Selecting
        # a pre-built binding — instead of mutating the shared
        # model's kwargs — keeps concurrent runs on other threads
        # isolated.
        configurable = dict((config or {}).get("configurable") or {})
        selected_model = bound_model
        if configurable.get("thinking_type") == "disabled":
            selected_model = bound_model_no_thinking
        # Step 2: STREAM the model (T3 / spec line 27 / ADR 0026).
        # Token deltas are observed by the workspace SSE layer via
        # LangGraph's ``stream_mode="messages"``; THIS node still
        # returns exactly one state update containing ONE complete
        # ``AIMessage`` — the durable contract (spec line 37: a
        # checkpoint after model_node holds full content AND full
        # parsed ``tool_calls`` so T4 resume never regenerates the
        # turn). ``assemble_final_ai_message`` joins chunks into
        # that message, preserving ``tool_calls`` and
        # ``additional_kwargs.reasoning_details`` (ADR 0022
        # round-trip: the next model call sees prior reasoning).
        #
        # ``thinking_text`` accumulates ONLY thinking prose from
        # ``reasoning_details`` (the split-channel shape) plus any
        # inline ``<thinking>`` block stripped out of content. It is
        # stored on state for the turn row; it NEVER merges into the
        # answer content (SC60).
        # corpus-serve-ingest T4 (ADR 0047 thread attach): turn-scoped
        # attachment context, injected CALL-SCOPED like the track
        # prompt — see ``_with_turn_attachments`` for the full
        # contract (this-turn-only, checkpoint-clean, never evidence).
        # that pull request: read the merged profile text at call time (NOT at
        # compile time) so a file edit between calls is visible on
        # the next turn. The user profile is operator-profile
        # machinery and surfaces regardless of memory_on.
        # ADR 0061 (search-mode host T3): the 档 usage copy rides this
        # call's head SystemMessage, read from the run's audit — an
        # unassigned turn (clarify awaiting a pick, typed fail) carries
        # ``search_mode=None`` and injects nothing. Because the copy is
        # call-time only, the compaction gate's history rebuild needs no
        # extra seam: the next call re-adds it, and checkpointed
        # messages are never rewritten when the mode changes.
        model_messages = _with_turn_attachments(
            state,
            messages_for_model(
                state["messages"],
                track="grounded",
                memory_on=memory_enabled,
                user_profile=_read_profile_text(),
                # The same single owner of「本次跑的是哪个档」the retrieve
                # hop reads, so the copy and the wire call can never
                # disagree about the run's assignment.
                search_mode=assigned_retrieve_mode(state),
            ),
        )
        stream = selected_model.stream(model_messages)
        answer_message = assemble_final_ai_message(stream)
        # T4 (#37): ``thinking_text`` ACCUMULATES across this run's
        # model calls (each call's prose joined with a newline) so
        # the persisted turn row matches what the delta wire
        # streamed — the reload view and the live view agree.
        # ``reset_input_state`` clears the field at run start, and
        # a resume continues accumulating onto the checkpointed
        # value (same run, spec line 39).
        this_call_thinking = _extract_thinking_text(answer_message)
        prior_thinking = str(state.get("thinking_text", "") or "")
        if prior_thinking and this_call_thinking:
            thinking_text = prior_thinking + "\n" + this_call_thinking
        else:
            thinking_text = prior_thinking or this_call_thinking
        # NOTE: ``evidence`` is intentionally omitted from this return.
        # The reducer in ``AgentState`` only fires when a node actually
        # returns the key; omitting it leaves the prior evidence list
        # untouched (tool_node returns the new items).
        return {
            "messages": [answer_message],
            "step_count": next_step,
            "status": "running",
            "repair_count": int(state.get("repair_count", 0)),
            "thinking_text": thinking_text,
            "track": "grounded",
        }

    def tool_node_with_summaries(state: AgentState) -> dict[str, Any]:
        """Run the prebuilt ToolNode, then append human-readable
        one-liners to ``state['tool_summaries']``.

        Why a wrapper (vs. registering ToolNode directly): the
        summaries are presentation state derived from the SAME
        ``(name, args, result)`` triples the tool functions saw.
        Deriving them inside the tools would couple ADR 0009 tool
        contracts to UI copy; deriving them here keeps ToolNode as
        the executor and this wrapper as the only summarizer.

        Raw ``ToolMessage.content`` JSON never enters a summary -
        the formatter renders name + short human phrase only
        (spec line 32).

        Return-shape note: when ANY wrapped tool returns a
        ``Command`` (``read_file`` / ``retrieve`` do), the prebuilt
        ``ToolNode._func`` answers a list instead of one flat dict.
        LangGraph merges that list itself when a node returns it
        verbatim, but this wrapper must merge it manually to attach
        ``tool_summaries``. ``_fold_tool_update`` owns that shape (see
        its docstring); this node keeps only the channel semantics.
        ``retrieve_outcomes`` is folded here (over the prior channel)
        because it is overwrite-semantics: a channel this wrapper did
        not copy would be silently discarded, and the citation gate's
        具名失败 (T3) reads it.
        """
        pending = list(_pending_tool_calls(state))
        raw_update: Any = tool_dispatcher.invoke(state)

        merged = _fold_tool_update(raw_update)
        merged_messages = merged["messages"]
        merged_evidence = merged["evidence"]
        merged_outcomes = merged["retrieve_outcomes"]

        new_summaries = _build_tool_summaries(pending, merged_messages)
        update: dict[str, Any] = {"messages": merged_messages}
        if merged_evidence:
            update["evidence"] = merged_evidence
        update["tool_summaries"] = new_summaries
        # T3 (spec Focus 1): lift any ``compare_facts`` table onto the
        # state so the turn row persists STRUCTURE, not prose. The
        # channel is overwrite-semantics, so this node folds the prior
        # list itself (see ``AgentState.comparisons``). Written on
        # EVERY tool step, including the empty fold: a run whose only
        # comparison was a typed EXIT must still leave the reader a
        # ``[]``, not an absent key.
        update["comparisons"] = list(state.get("comparisons") or []) + (
            _collect_comparisons(merged_messages)
        )
        update["analysis_results"] = list(
            state.get("analysis_results") or []
        ) + _collect_analysis_results(merged_messages)
        # T3 (SC4–SC5): the retrieve outcomes accumulate WITHIN the
        # run (several retrieve calls are several records) and the
        # channel is overwrite-semantics, so the fold is explicit —
        # same shape as the comparison / analysis channels above.
        update["retrieve_outcomes"] = list(
            state.get("retrieve_outcomes") or []
        ) + merged_outcomes
        return update

    def _fanout_tool_update(
        payload: dict[str, Any],
        tool_call_id: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The ``ToolMessage`` + tool-trace half of a fan-out result.

        Both the planner's typed EXITs and the join's envelope answer
        the SAME model tool call, so they share one renderer: the
        message history never carries a dangling ``research_issuers``
        call, and the workspace tools region gets the same
        human-readable one-liner either way (spec line 32).
        """
        return {
            "messages": [
                ToolMessage(
                    content=json.dumps(payload, default=str),
                    tool_call_id=tool_call_id,
                    name=ISSUER_FANOUT_TOOL,
                )
            ],
            "tool_summaries": [
                {
                    "name": ISSUER_FANOUT_TOOL,
                    "summary": format_tool_summary(
                        ISSUER_FANOUT_TOOL, result=payload, args=args
                    ),
                    "tool_call_id": tool_call_id,
                }
            ],
        }

    def issuer_fanout_node(state: AgentState) -> dict[str, Any]:
        """Plan the fan-out: normalize the roster and apply the cap.

        The cap is enforced HERE, on the request, before any branch
        runs — "over-cap issuer lists are a typed overflow, not a
        hang" (spec Focus 4). An over-cap roster ends the run with
        ``status_reason == "issuer_overflow"`` instead of quietly
        researching the first ``issuer_cap`` issuers, because a
        ``done`` built on a truncated roster is the silent drop SC7
        forbids; the typed EXIT still reaches the message history so
        the operator can re-ask with fewer issuers.

        ``empty_fanout`` is NOT terminal: no issuers to research is an
        empty-hit shape like ``{"filings": []}``, so the model gets the
        typed dict back and may continue with the ordinary tools.
        """
        args, tool_call_id = _first_fanout_call(state)
        question = ""
        for message in reversed(state.get("messages") or []):
            if isinstance(message, HumanMessage):
                content = message.content
                question = (
                    content if isinstance(content, str) else str(content)
                )
                break
        plan = plan_issuer_fanout(
            issuers=args.get("issuers"),
            # The model normally restates the question per fan-out; a
            # call that omitted it falls back to the operator's own
            # utterance rather than fanning out an empty query.
            query=str(args.get("query") or question or ""),
            cap=issuer_cap,
        )
        update: dict[str, Any] = {
            "issuer_plan": {**plan, "tool_call_id": tool_call_id},
            "issuer_fanout": plan.get("fanout") or {},
            # Clear any earlier fan-out's findings before this one's
            # branches start writing to the additive channel.
            "issuer_findings": FANOUT_RESET,
        }
        if not plan.get("error"):
            # The accepted path answers the model's call from the JOIN,
            # once the branches have reported. Answering it here too
            # would put two ``ToolMessage``s on one ``tool_call_id``.
            return update
        update.update(_fanout_tool_update(plan, tool_call_id, args=args))
        if plan["error"] == "issuer_overflow":
            update["status"] = "refuse"
            update["status_reason"] = "issuer_overflow"
        return update

    def issuer_research_node(task: IssuerTask) -> dict[str, Any]:
        """Research ONE issuer through the retrieve port.

        This is the fan-out branch ADR 0034 permits. It calls the port
        once and no more: ranking stays behind the port and there is no
        hybrid / RRF pass in here (ADR 0032 + ADR 0034's rejected
        list). Its chunks become ordinary ``EvidenceItem``s, so the one
        citation verifier after the join treats fan-out evidence
        exactly like a ``read_file`` page.
        """
        issuer = str(task.get("issuer") or "").strip().upper()
        query = str(task.get("query") or "")
        if not issuer or not query:
            return {}

        try:
            result = retrieve(
                query=issuer_query(issuer, query),
                ticker=issuer,
                # SC2: the branch answers under the 档 the cascade
                # assigned for this run — carried on the payload the
                # router stamped, not re-read from anywhere else.
                search_mode=task.get("search_mode"),
                adapter=adapter,
            )
        except InputValidationError as exc:
            # Invalid input on one branch (T21). The blank-query guard
            # above means this is a filter-level refusal; it is reported
            # under the port's own code rather than as a transport
            # problem, and it does not take the join down.
            return _issuer_finding(issuer, STATUS_UNAVAILABLE, error=exc.code)
        except Exception as exc:  # noqa: BLE001 — one branch, not the run
            # A port that raises anything else breaks its own contract
            # (a legal empty result is a value, not an exception).
            # Letting it propagate would kill the join and take the
            # other issuers' work with it, so the branch degrades to the
            # typed unavailable row — reported, never swallowed.
            return _issuer_finding(
                issuer,
                STATUS_UNAVAILABLE,
                error=f"retrieve_raised:{type(exc).__name__}",
            )

        # T20 / SC39: a fan-out worker is a retrieval consumption point
        # like any other, so a degraded answer here is recorded and
        # shown too — otherwise the branch would be the one place a
        # degradation could arrive silently. The notice rides on the
        # run's tool trace, which is additive across branches.
        notices: list[dict[str, str]] = []
        consume_degradation(
            result,
            ledger=degradation_ledger,
            expose=lambda signal, _line: notices.append(
                degradation_notice_entry(
                    signal, tool_call_id=str(task.get("issuer") or "")
                )
            ),
            detail=f"issuer={issuer}",
        )
        summaries: dict[str, Any] = (
            {"tool_summaries": notices} if notices else {}
        )

        if isinstance(result, dict) and "error" in result:
            # Typed EXIT (ADR 0030): "retrieve is down" must not read
            # as "the corpus has nothing about this issuer".
            return {
                **summaries,
                **_issuer_finding(
                    issuer, STATUS_UNAVAILABLE, error=str(result["error"])
                ),
            }

        new_evidence = _issuer_evidence(result, ledger=mapping_failure_ledger)
        if not new_evidence:
            return {**summaries, **_issuer_finding(issuer, STATUS_EMPTY)}
        return {**summaries, **_issuer_hit_update(issuer, new_evidence)}

    def issuer_join_node(state: AgentState) -> dict[str, Any]:
        """Join the branches into one ``fin.issuer_fanout.v1`` envelope.

        The join exists so the citation verifier runs ONCE, on the
        synthesized answer, instead of once per issuer (ADR 0034's
        rejected "verifier-per-issuer with no join"). It answers the
        single ``research_issuers`` call and hands control back to
        ``model``, which writes the synthesis.
        """
        plan = dict(state.get("issuer_plan") or {})
        result = join_issuer_findings(
            plan=plan, findings=list(state.get("issuer_findings") or [])
        )
        tool_call_id = str(plan.get("tool_call_id") or "")
        # The planner stored the call's args into ``issuer_plan``'s
        # query, so the join's one-liner keeps the same query prefix
        # the live wire shows (turn-trace-folds T1).
        join_args = {"query": plan.get("query")} if plan.get("query") else None
        return {
            **_fanout_tool_update(result, tool_call_id, args=join_args),
            "issuer_fanout": result["fanout"],
        }

    def citation_gate_node(state: AgentState) -> dict[str, Any]:
        # Pull the last ``AIMessage``'s text as the candidate
        # answer. Empty content (model returned only tool_calls)
        # falls through to the bounce path so the model can retry
        # with prose.
        messages = state["messages"]
        candidate_answer = _last_ai_text(messages)
        evidence = list(state.get("evidence", []))
        repair_count = int(state.get("repair_count", 0))

        # ADR 0046: the verifier's locator allow-list is THIS run's
        # evidence set — the document ids the run actually retrieved
        # or read — unioned with any static allow-list the caller
        # configured. It is never derived from a manifest or a local
        # extracted tree, so a locator claim nothing brought in this
        # run refuses immediately (SC9).
        known_ids = set(known_document_ids or ())
        known_ids.update(
            item.locator.document_id for item in evidence
        )

        gate_result = verify_citations(
            answer=candidate_answer,
            evidence=evidence,
            repair_count=repair_count,
            known_document_ids=known_ids,
            repair_cap=REPAIR_CAP,
        )

        if gate_result.outcome == "done":
            # T5: numeric gate. Walk the AIMessage history for
            # ``get_xbrl_fact`` tool calls this run; each call is
            # an implicit (concept, period) claim. The matching
            # ToolMessage JSON carries the fact value (or the
            # ``fact_not_found`` miss shape). If the model claimed
            # a number that doesn't match an evidenced fact row,
            # the run refuses immediately — T5 spec ("research
            # done does not pass when a claimed numeric fact has
            # no matching evidenced row"). The numeric gate is
            # SKIPPED when the model made no ``get_xbrl_fact`` call
            # (CONTEXT ``get_xbrl_fact``: "non-research skips the
            # fact gate"; here, research without a fact call also
            # skips).
            numeric_outcome = _evaluate_numeric_gate_in_node(messages)
            if numeric_outcome is not None and numeric_outcome.outcome == "fail":
                return {
                    "messages": [],
                    "status": "refuse",
                    "status_reason": "numeric_gate_fail",
                    "repair_count": repair_count,
                }
            # T6 (spec Focus 4 / SC7): a fan-out run must keep every
            # issuer that contributed evidence in the synthesis. The
            # verifier above already passed — on the OTHER issuers'
            # spans — so without this check a five-issuer ask could
            # ``done`` on four. Runs with no fan-out skip it entirely.
            dropped = _uncited_contributors(
                dict(state.get("issuer_fanout") or {}), gate_result.citations
            )
            if dropped:
                return {
                    "messages": [],
                    "status": "refuse",
                    "status_reason": "issuer_coverage_fail",
                    "repair_count": repair_count,
                }
            return {
                "messages": [],
                "answer": gate_result.answer,
                "citations": tuple(gate_result.citations),
                "status": "done",
                "status_reason": "done",
                "repair_count": repair_count,
            }

        if gate_result.outcome == "refuse":
            # T3: the run had NOTHING to cite and retrieve is the
            # reason. The verifier's own vocabulary is untouched
            # (ADR 0008) — the refusal it produced is RE-LABELLED
            # with the more specific retrieve-side fact, which is
            # the whole point of 「具名失败」: an operator must not
            # have to read three different product facts as one
            # ``unknown_locator``. With evidence present, or with no
            # retrieve at all, the gate's verdict stands unchanged.
            return {
                "messages": [],
                "status": "refuse",
                "status_reason": named_refusal_reason(
                    evidence=evidence,
                    outcomes=state.get("retrieve_outcomes"),
                    verifier_reason=gate_result.reason,
                ),
                "repair_count": repair_count,
            }

        # Bounce: append the hidden template as a SystemMessage at
        # the end of the message list (ADR 0021). The template is
        # data; CLI / HTTP rendering (T7 / T9) strips the marker
        # before printing.
        hidden = SystemMessage(
            content=_format_bounce_template(gate_result.hidden_template)
        )
        return {
            "messages": [hidden],
            "status": "running",
            "status_reason": "",
            "repair_count": gate_result.repair_count,
        }

    # ----- session compaction gate (ADR 0044 / plan T1) -----

    # The injectable seam: ``None`` -> the v1 chat model. Mirrors
    # ``fin.host.compaction.SummaryModel`` Protocol DI shape; tests
    # pass a stub. Held in a closure so the gate node picks up the
    # operator override (``session_token_budget`` ContextVar) at
    # call time.
    _effective_summarizer = summarizer if summarizer is not None else model
    _compactor = OccupancyCompactor(summarizer=_effective_summarizer)

    def compaction_gate_node(state: AgentState) -> dict[str, Any]:
        """Compact before the next LLM call (ADR 0044).

        The gate runs on EVERY entry path that would otherwise call
        the model: ``tools -> gate``, ``issuer_join -> gate``,
        ``from_classify -> gate``, ``from_issuer_fanout (fallback)
        -> gate``, and ``from_gate (bounce) -> gate``. It is NOT
        nested inside ``model_node`` (separate graph node).

        On a summary failure, the typed :class:`SessionSummaryError`
        is logged and the node returns ``{}`` so the operator turn
        continues un-compacted (ADR 0044: typed, not silent empty
        catch; must not refuse the operator turn). A malformed
        snapshot (``CompactionInputError`` from ``copy_snapshot``)
        is handled the same way — logged and skipped, never a
        blown-up run.
        """
        messages = list(state.get("messages") or [])
        snapshots = list(state.get("status_snapshots") or [])
        try:
            update = _compactor.compact_update(
                messages, status_snapshots=snapshots
            )
        except (SessionSummaryError, CompactionInputError) as exc:
            _LOG.warning("session compaction skipped: %s", exc)
            return {}
        if not update:
            return {}
        # ``compact_update`` returns ``[RemoveMessage(REMOVE_ALL),
        # summary, *kept]`` so the ``add_messages`` reducer shrinks
        # the checkpointer. We pass the SAME ``messages`` list back
        # to the channel so the rebuild pattern actually replaces
        # history rather than appending duplicates.
        return {"messages": update}

    # ----- conditional edges -----

    def _clarify_window_pending(state: AgentState) -> bool:
        """True while a grounded turn's 档 cascade still waits on the
        clarify choice window (T2; reused by the T4 post-propose route
        so the two window lanes cannot drift)."""
        audit = state.get("search_mode_decision") or {}
        return bool(
            enable_choice_window
            and normalize_track(state.get("track")) == "grounded"
            and audit.get("decision_tier") == "clarify"
            and audit.get("options")
        )

    def from_classify(
        state: AgentState,
    ) -> Literal["extract_method_choice", "mode_choice", "compaction_gate"]:
        """Route EVERY track through the compaction gate (ADR 0044).

        The gate must run before EVERY LLM call — the research
        ``model`` AND the non-research leaves (dialogue / clarify /
        off_topic are LLM calls too). The gate is a no-op under
        budget; :func:`from_compaction_gate` then applies the track
        decision.

        Recorded deviation: ``decide_track``'s own Jev gray-zone tier
        (the ambiguous ``choose`` call) runs BEFORE this gate. It is
        safe because the judge never sees the fat history —
        ``decide_track`` sends only the LAST human utterance to the
        Jev Choice client, so no evictable context reaches that call
        and there is nothing for the gate to compact first.

        search-mode host T2: with the HITL lane on, a grounded turn
        whose 档 cascade resolved to ``clarify`` first visits
        ``mode_choice`` — the window pauses the run BEFORE any model
        or retrieve call (pin / rules / inherit / llm assignments never
        route here; there is no required per-turn picker).

        search-mode host T4: an explicit Fast propose seed runs the
        extract-method window FIRST (still before any model call), then
        normal 档 routing resumes — a clarify turn sees both windows in
        sequence. Without the seed this lane does not exist.
        """
        if (
            enable_choice_window
            and normalize_track(state.get("track")) == "grounded"
            and proposal_is_pending(state.get("extract_method_proposal"))
        ):
            return "extract_method_choice"
        if _clarify_window_pending(state):
            return "mode_choice"
        return "compaction_gate"

    def from_extract_method(
        state: AgentState,
    ) -> Literal["mode_choice", "compaction_gate"]:
        """Continue normal classify-time routing after the propose
        window resolved: a clarify 档 still owed opens ``mode_choice``
        next; otherwise proceed to the leaf path. The just-resolved
        proposal is no longer pending, so this edge cannot loop."""
        if _clarify_window_pending(state):
            return "mode_choice"
        return "compaction_gate"

    def from_compaction_gate(
        state: AgentState,
    ) -> Literal["dialogue", "clarify", "off_topic", "model"]:
        """Apply the Soft-Track track decision after the gate.

        Mirrors the residual branch in ``decide_track``: a missing
        track defaults to the dialogue leaf rather than the research
        model (specs/soft-track.md — no single global default track).
        The gray-zone judge itself fail-closes to clarify (ADR 0080),
        so this default only covers pre-classify state.
        """
        track = state.get("track", "dialogue")
        if track == "dialogue":
            return "dialogue"
        if track == "clarify":
            return "clarify"
        if track == "off_topic":
            return "off_topic"
        return "model"

    def from_model(
        state: AgentState,
    ) -> Literal["tools", "issuer_fanout", "citation_gate", "__end__"]:
        # Step-cap fires here: the node already set ``status`` to
        # ``step_cap`` and we route to END. Status-vs-tool_call
        # precedence: terminal status wins, even if the AIMessage
        # has tool_calls (defensive — the model_node does not
        # produce a message in the step-cap branch).
        if state.get("status") == "step_cap":
            return END
        messages = state["messages"]
        last = messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            # T6: ``research_issuers`` is served by the fan-out
            # topology, not by the ToolNode. A MIXED batch still goes
            # to the ToolNode so the ordinary tools in it are not
            # stranded; the fan-out call then comes back as ToolNode's
            # invalid-tool-name result, which is a readable answer
            # rather than a lost tool call.
            if all(
                call.get("name") == ISSUER_FANOUT_TOOL
                for call in last.tool_calls
            ):
                return "issuer_fanout"
            return "tools"
        # Soft-Track: citation verifier is research-only.
        if normalize_track(state.get("track")) == "grounded":
            return "citation_gate"
        return END

    def from_issuer_fanout(state: AgentState) -> Any:
        """Fan out one branch per approved issuer, else fall through.

        Returning a list of ``Send`` is what makes the branches
        parallel AND bounded: the list is exactly the roster the cap
        already approved, so the graph never opens an unbounded set of
        agents (ADR 0034's first rejected option). A typed EXIT routes
        back to ``model`` (``empty_fanout``) or to END (the refused
        ``issuer_overflow``) without opening any branch at all.
        """
        if state.get("status") in TERMINAL_STATUSES:
            return END
        plan = dict(state.get("issuer_plan") or {})
        issuers = [str(issuer) for issuer in (plan.get("issuers") or ())]
        query = str(plan.get("query") or "")
        if plan.get("error") or not issuers or not query:
            return "compaction_gate"
        # Every branch of THIS run retrieves under THIS run's assigned
        # 档 (SC2): read once off the state here, at dispatch time, so a
        # cached graph cannot carry a previous run's token into a new
        # fan-out. The model never supplies it — the payload's 档 key is
        # written by the host from the cascade's audit.
        mode = assigned_retrieve_mode(state)
        return [
            Send(
                "issuer_research",
                {"issuer": issuer, "query": query, "search_mode": mode},
            )
            for issuer in issuers
        ]

    def from_gate(state: AgentState) -> Literal["compaction_gate", "__end__"]:
        # Status-based routing out of the citation gate. Done /
        # refuse / step_cap all END; ``"running"`` (after a bounce)
        # routes back through the compaction gate so the next LLM
        # call sees a possibly-compacted history.
        status = state.get("status", "running")
        if status in ("done", "refuse", "step_cap"):
            return END
        return "compaction_gate"

    # ----- assemble the graph -----

    graph = StateGraph(AgentState)
    graph.add_node("classify", classify_node)
    graph.add_node("dialogue", dialogue_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("off_topic", off_topic_node)
    if enable_choice_window:
        # search-mode host T2: the HITL lane adds a PAUSE node, not a
        # retrieve node — the graph stays one compiled graph with ONE
        # retrieve tool (spec complexity ACR).
        # search-mode host T4: the Fast propose window is a second
        # PAUSE node on the same lane (ADR 0068: propose only, never
        # apply), reached only from an explicit seed.
        graph.add_node("extract_method_choice", extract_method_choice_node)
        graph.add_conditional_edges(
            "extract_method_choice",
            from_extract_method,
            ["mode_choice", "compaction_gate"],
        )
        graph.add_node("mode_choice", mode_choice_node)
        graph.add_edge("mode_choice", "compaction_gate")
    graph.add_node("model", model_node)
    # ADR 0044 / plan T1: the compaction gate is a SEPARATE graph
    # node (NOT nested inside ``model_node``). It runs before every
    # entry that would otherwise call the model: tools, issuer_join,
    # classify's research branch, the citation-gate bounce, and
    # the issuer_fanout typed-fallback.
    graph.add_node("compaction_gate", compaction_gate_node)
    # ``tool_node_with_summaries`` wraps ``tool_dispatcher`` so the
    # graph executor still emits ``ToolMessage``s unchanged; the
    # wrapper only ADDS the ``tool_summaries`` field to the state
    # update (T3). The forward edge ``tools -> model`` still routes
    # back to model_node for the next agent-loop iteration.
    graph.add_node("tools", tool_node_with_summaries)
    # T6 (ADR 0034): plan -> Send per issuer -> join -> back to the
    # model, and only THEN the one citation gate. The three nodes are
    # siblings of ``tools``, not nested inside ``model_node``, so the
    # fan-out is topology the checkpointer can resume like any other
    # node (spec ACR: "fan-out is a graph join node").
    graph.add_node("issuer_fanout", issuer_fanout_node)
    graph.add_node("issuer_research", issuer_research_node, input_schema=IssuerTask)
    graph.add_node("issuer_join", issuer_join_node)
    graph.add_node("citation_gate", citation_gate_node)

    graph.add_edge(START, "classify")
    # Explicit path map: with the HITL lane OFF ``mode_choice`` (T2) and
    # ``extract_method_choice`` (T4) are not nodes on this graph, and
    # LangGraph would reject the branch return annotation's unknown
    # target at compile time.
    graph.add_conditional_edges(
        "classify",
        from_classify,
        ["extract_method_choice", "mode_choice", "compaction_gate"]
        if enable_choice_window
        else ["compaction_gate"],
    )
    graph.add_edge("dialogue", END)
    graph.add_edge("clarify", END)
    graph.add_edge("off_topic", END)
    graph.add_conditional_edges("model", from_model)
    # ADR 0044: every LLM entry goes through the gate — tools and
    # issuer_join enter it for the next agent-loop iteration, and
    # classify routes ALL tracks (research AND non-research leaves)
    # through it so the gate runs before EVERY LLM call. The gate
    # is a no-op under budget; it never refuses a turn.
    graph.add_edge("tools", "compaction_gate")
    graph.add_conditional_edges(
        "issuer_fanout",
        from_issuer_fanout,
        ["issuer_research", "compaction_gate", END],
    )
    graph.add_edge("issuer_research", "issuer_join")
    graph.add_edge("issuer_join", "compaction_gate")
    graph.add_conditional_edges("citation_gate", from_gate)
    # The gate applies the track decision AFTER compacting, so the
    # non-research leaves are also reached post-gate.
    graph.add_conditional_edges("compaction_gate", from_compaction_gate)

    compiled = graph.compile(checkpointer=checkpointer)

    return _make_locked_graph(
        checkpointer=checkpointer,
        model=model,
        jev_client=jev_client,
        step_cap=step_cap,
        issuer_cap=issuer_cap,
        recursion_limit=recursion_limit,
        summarizer=summarizer,
        compiled_graph=compiled,
        enable_choice_window=enable_choice_window,
        # T2 (ADR 0044): the SAME compactor instance the graph gate
        # uses, so ``/compact`` and the >=90% auto path share one
        # eviction unit walker, one summarizer seam, one floor.
        compactor=_compactor,
        # T6: surface the bound tool list (save / recall / profile on
        # the ON branch) so the test introspection path can assert
        # the spec-mandated tool surface without reaching through the
        # compiled-graph internals.
        bound_tools=model_bound_tools,
        # T6: surface the ToolNode so the test introspection path
        # can drive save / recall / edit_user_profile without going
        # through ``tool_node_with_summaries`` (a wrapper whose name
        # shadows the actual executor in the compiled graph).
        tool_node=tool_dispatcher,
        # T6: memory_port changes the bound tool set (ON adds three
        # tools; OFF keeps the eight ToolNode tools), so the
        # idempotence cache must distinguish graphs by memory
        # surface. ``id(memory_port)`` is stable across two calls with
        # the same injected port object — keying on the bound tool
        # LIST directly would create a fresh list per call and break
        # the OFF/idempotence contract that the existing wiring tests
        # pin.
        #
        # spec SC8 / ADR 0049 (Wave A): the per-call workspace
        # cache slot is GONE. The per-call 工作区 rides the run's
        # ``configurable.workspace_root``, not the compile-time graph
        # identity — two compiles with identical config but different
        # run-time workspaces must share the same cached proxy.
        memory_port_id=id(memory_port) if memory_port is not None else 0,
    )


# ----- concurrency proxy -----


# Idempotence cache. Two ``compile_graph`` calls with the SAME
# checkpointer + model objects return the SAME proxy object so
# T9 can assert identity between the CLI helper's graph and the
# HTTP handler's graph (both share the process-level checkpointer
# and model).
#
# We use a dict keyed on ``(id(checkpointer), id(model),
# step_cap, recursion_limit)`` because most LangGraph objects
# (``InMemorySaver``, ``CompiledStateGraph``) do not implement a
# hash-friendly ``__eq__`` — ``lru_cache`` keyed on the objects
# directly would create a fresh cache entry per call. Keying on
# ``id`` is the documented escape hatch for "same object reused"
# idempotence.
#
# spec SC8 / ADR 0049 (Wave A): the key DROPS the per-call
# workspace cache slot. The per-call 工作区 rides the run's
# ``configurable.workspace_root``
# (injected by HTTP / CLI), not the compile-time graph identity. Two
# compiles that differ ONLY in the workspace they will be invoked
# against must share one cache entry — there is exactly one graph
# object per (checkpointer, model) pair.
_IDEMPOTENCE_CACHE: dict[tuple, "_LockedGraph"] = {}


def _make_locked_graph(
    *,
    compiled_graph: Any,
    recursion_limit: int,
    checkpointer: Any,
    model: Any,
    step_cap: int,
    jev_client: Any | None = None,
    issuer_cap: int = MAX_ISSUERS,
    summarizer: Any | None = None,
    compactor: OccupancyCompactor | None = None,
    bound_tools: list[Any] | None = None,
    tool_node: Any | None = None,
    memory_port_id: int = 0,
    enable_choice_window: bool = False,
) -> "_LockedGraph":
    key = (
        id(checkpointer),
        id(model),
        # The gray-zone judge is part of the compiled graph's behavior:
        # two graphs differing only in their judge are two different
        # graphs, so the id belongs in the key.
        id(jev_client) if jev_client is not None else 0,
        int(step_cap),
        # T6: two graphs that differ ONLY in the issuer cap are two
        # different graphs; without this the cache would hand a test
        # tuning the cap the previously-compiled default.
        int(issuer_cap),
        int(recursion_limit),
        # T1 (ADR 0044): the summarizer seam affects the compiled
        # graph's ``compaction_gate`` node, so two graphs that differ
        # only in ``summarizer`` are two different graphs.
        id(summarizer) if summarizer is not None else 0,
        # T6: ON vs OFF adds three memory tools to the bound set;
        # without this key the first OFF compile would hand a later
        # ON compile a cached OFF proxy.
        int(memory_port_id),
        # search-mode host T2: the HITL flag adds the ``mode_choice``
        # node + routing — same reasoning as ``memory_port_id``, an OFF
        # compile must never be served to an ON caller from cache.
        int(enable_choice_window),
    )
    cached = _IDEMPOTENCE_CACHE.get(key)
    if cached is not None:
        return cached
    proxy = _LockedGraph(
        compiled_graph=compiled_graph,
        recursion_limit=recursion_limit,
        compactor=compactor,
        bound_tools=bound_tools or [],
        tool_node=tool_node,
    )
    _IDEMPOTENCE_CACHE[key] = proxy
    return proxy


class _LockedGraph:
    """Wraps a compiled ``StateGraph`` with per-thread concurrency.

    The factory returns this object instead of the raw compiled
    graph so callers cannot bypass the "one active run per
    ``thread_id``" rule. The wrapper is intentionally minimal — it
    forwards ``invoke``, ``ainvoke``, and ``stream`` to the inner
    graph after acquiring the thread lock and applying the
    default ``recursion_limit``.

    Implementation choice: a per-thread ``threading.Lock`` dict
    guarded by a meta-lock, plus ``threading.Lock.acquire(
    blocking=False)`` so a second concurrent ``invoke`` raises
    ``ConcurrentRunError`` immediately instead of deadlocking.
    """

    def __init__(
        self,
        *,
        compiled_graph: Any,
        recursion_limit: int,
        compactor: OccupancyCompactor | None = None,
        bound_tools: list[Any] | None = None,
        tool_node: Any | None = None,
    ) -> None:
        self._graph = compiled_graph
        # ``recursion_limit`` is exposed via the public attribute
        # ``recursion_limit`` so tests can assert it directly.
        self.recursion_limit = int(recursion_limit)
        # T2 (ADR 0044): the compactor shared with the graph's
        # ``compaction_gate`` node; ``/compact`` runs the same gate.
        self._compactor = compactor
        # T6: the bound tool list captured at factory time so the
        # test surface can assert the spec-mandated save / recall /
        # edit_user_profile presence without reaching through the
        # compiled-graph internals.
        self._bound_tools: list[Any] = list(bound_tools or [])
        # T6: the executor-side ToolNode — exposed so tests can drive
        # the spec tools (save / recall / edit_user_profile) directly
        # without going through the ``tool_node_with_summaries``
        # wrapper that shadows the registered executor name in the
        # compiled graph.
        self._tool_node = tool_node
        self._thread_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    # ---- lock management ----

    def _lock_for(self, thread_id: str) -> threading.Lock:
        """Return the per-thread lock, creating it on first use.

        Held briefly under ``_meta_lock``; the per-thread lock
        itself is what callers hold for the duration of ``invoke``.
        """
        with self._meta_lock:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[thread_id] = lock
            return lock

    def _acquire_thread(self, thread_id: str) -> threading.Lock:
        """Try to acquire the per-thread lock.

        Raises ``ConcurrentRunError`` if another ``invoke`` is
        already in flight on ``thread_id``. Returns the lock on
        success; caller is responsible for releasing it.
        """
        lock = self._lock_for(thread_id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            raise ConcurrentRunError(
                f"another invoke is already in flight on thread_id={thread_id!r}"
            )
        return lock

    def is_locked(self, thread_id: str) -> bool:
        """T3 helper for the workspace route: non-blocking probe
        of the per-thread lock. ``True`` when another run is in
        flight. The workspace send path uses this to decide
        ``409`` BEFORE returning the StreamingResponse so the 409
        body carries ``application/json`` (spec line 26 + T2).
        """
        with self._meta_lock:
            lock = self._thread_locks.get(thread_id)
        if lock is None:
            return False
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        """T4: return LangGraph's pending-node tuple for ``thread_id``'s
        latest checkpoint. Empty tuple when the last run reached a
        terminal node (done / refuse / step_cap) OR when the thread
        has no checkpoint at all (a freshly-created thread that has
        never been sent on).

        The workspace ``POST /resume`` route uses this seam to
        decide whether ``stream(None)`` will actually do work — if
        the tuple is empty, the route returns ``204`` (no run to
        resume). Reaching through ``_graph.checkpointer.get_state``
        would couple the HTTP layer to the compiled graph's
        internals; this method is the documented seam mirroring
        :meth:`delete_thread`'s rationale.

        Read-only path: does NOT acquire the per-thread lock. The
        route probes ``is_locked`` first, so under normal use no
        concurrent write is in flight; even if one were, reading a
        checkpoint while another run is mid-superstep yields the
        most-recently-committed snapshot — still well-defined.
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        return tuple(snapshot.next or ())

    def interrupt_pending(self, thread_id: str) -> dict[str, Any] | None:
        """The payload of the choice window pending on ``thread_id``, or
        ``None`` when no HITL interrupt is pending (search-mode host T2).

        Why a separate probe from ``is_locked``: a paused ``interrupt``
        RELEASES the per-thread lock (the run's stream generator has
        ended), so the send route's 409 guard for "a choice window is
        waiting for the operator" must read the checkpoint's pending
        task interrupts, not the lock. Typed-fail / crash mid-run
        checkpoints carry pending NODES but no INTERRUPTS, so a plain
        crash resume is never mistaken for a pending window.

        Read-only path (mirrors :meth:`pending_nodes`): does NOT
        acquire the per-thread lock.
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        for task in snapshot.tasks or ():
            for pending in getattr(task, "interrupts", ()) or ():
                value = getattr(pending, "value", None)
                if isinstance(value, dict):
                    return value
        return None

    def checkpoint_run_id(self, thread_id: str) -> str | None:
        """Return the canonical run id stored on the latest checkpoint.

        LangGraph records ``configurable.run_id`` as checkpoint metadata.
        The workspace resume route uses this seam instead of minting a
        second id or reaching through the compiled graph internals.
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        metadata = snapshot.metadata or {}
        run_id = metadata.get("run_id")
        return run_id if isinstance(run_id, str) and run_id else None

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        """Return the message ids on ``thread_id``'s latest checkpoint.

        The workspace send / resume routes seed their SSE dedup set
        with these ids so a follow-up send (whose first ``values``
        frame replays the whole accumulated message list) and a
        crash resume (whose first frame replays the pre-crash
        checkpoint) never re-emit run-1's ``tool_call`` /
        ``tool_result`` frames onto the new stream (spec line 34).
        Empty set for a thread with no checkpoint.

        Read-only path: does NOT acquire the per-thread lock; the
        routes call it before starting the run, when no writer is
        in flight (they probe ``is_locked`` first).
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        messages = (snapshot.values or {}).get("messages") or []
        return {
            mid
            for mid in (getattr(m, "id", None) for m in messages)
            if mid
        }

    def checkpoint_last_human_text(self, thread_id: str) -> str | None:
        """Return the latest HumanMessage text on the thread checkpoint.

        Workspace resume persists a RunRecord for the remainder; the
        question must match the crashed run's user text so optimistic
        turns and history can reconcile. Empty / missing → ``None``.
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        messages = (snapshot.values or {}).get("messages") or []
        for message in reversed(messages):
            if not isinstance(message, HumanMessage):
                continue
            content = message.content
            if isinstance(content, str):
                text = content.strip()
            else:
                text = str(content).strip()
            return text or None
        return None

    def occupancy(self, thread_id: str) -> dict[str, float | int] | None:
        """T3 (ADR 0044): token occupancy for ``thread_id``'s latest
        checkpoint, from the SAME figure the compaction gate computes.

        ``self._compactor.occupancy`` over the checkpointed
        messages (the model-facing base the gate measures) vs the
        active session budget — one shared estimator, no second
        number, and the SAME compactor instance the gate node uses
        (no throwaway per-call construction). ``None`` when the
        thread has no checkpoint (a
        freshly created thread has no model-facing payload, so the
        UI stays neutral rather than showing a fabricated 0%).

        Read-only path: does NOT acquire the per-thread lock,
        mirroring :meth:`pending_nodes` / :meth:`checkpoint_message_ids`.
        """
        snapshot = self._graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        messages = (snapshot.values or {}).get("messages") or []
        if not messages:
            return None
        return {
            "estimated_tokens": count_tokens_approximately(messages),
            "budget_tokens": session_budget_tokens(),
            "ratio": self._compactor.occupancy(messages),
        }

    @staticmethod
    def _release_thread(lock: threading.Lock) -> None:
        lock.release()

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        """Run the ADR 0044 compaction gate against ``thread_id``'s
        checkpoint — the ``/compact`` command path (plan T2).

        WHY a proxy method (mirrors ``finalize_abandoned_run`` /
        ``delete_thread``): the graph proxy must remain the ONLY
        writer to the checkpointer, and the write-back needs
        ``update_state`` — reaching into ``self._graph`` from the
        HTTP layer would couple the route to compiled-graph
        internals.

        Semantics:

        * Acquires the SAME per-thread lock as ``invoke`` / ``stream``
          (raises ``ConcurrentRunError`` when a run is in flight — the
          409 contract is unchanged; the route probes ``is_locked``
          first for the before-headers 409, this acquire is the
          belt-and-suspenders writer lock).
        * ``force=True`` on the compactor: the gate runs WITHOUT the
          90% trigger (a thread under 90% may still compact when the
          operator asked), with the same eviction units, summary
          write, and ``RemoveMessage`` write-back as the auto path.
        * Returns a receipt dict:
          ``{"outcome": "compacted", "before_tokens", "after_tokens",
          "messages_removed"}`` when the gate produced an update;
          ``{"outcome": "pending_run"}`` when the thread has a
          resumable mid-run checkpoint (never clobbered — resume or
          stop the run first); ``{"outcome": "noop"}`` when the
          thread has no checkpoint or the gate produced no update.
        * Write-back is ``update_state(..., as_node="dialogue")``:
          the ``dialogue`` leaf routes straight to END, so the
          checkpoint stays terminal (``pending_nodes() == ()``) —
          verified against the compiled graph (a pending checkpoint
          is rejected before this point by the ``pending_nodes``
          guard).
        * Summary failure is typed (:class:`SessionSummaryError`) and
          surfaces to the caller as ``{"outcome": "summary_failed"}``
          — the caller turns it into a graceful receipt, never a 500
          (ADR 0044: must not refuse the operator turn). The
          checkpoint is untouched on that path.
        """
        lock = self._acquire_thread(thread_id)
        try:
            if self._compactor is None:
                # ``compactor`` is optional only for direct
                # ``_LockedGraph`` construction outside the factory;
                # ``/compact`` cannot run without the shared gate
                # compactor (ADR 0044 T2), so fail typed and clear.
                raise ConcurrentRunError(
                    "this graph has no session compactor (compile_graph "
                    "always provides one); /compact is unavailable"
                )
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = self._graph.get_state(config)
            values = snapshot.values or {}
            messages = list(values.get("messages") or [])
            if not messages:
                # No checkpoint (never-sent thread) — nothing to compact.
                return {"outcome": "noop"}
            if tuple(snapshot.next or ()):
                # Mid-run (resumable) checkpoint — never clobbered.
                return {"outcome": "pending_run"}
            before_tokens = count_tokens_approximately(messages)
            try:
                update = self._compactor.compact_update(
                    messages,
                    status_snapshots=list(values.get("status_snapshots") or []),
                    force=True,
                )
            except SessionSummaryError:
                # Typed summary failure BEFORE any write: the
                # checkpoint is untouched, the turn continues
                # (ADR 0044: must not refuse the operator turn).
                return {"outcome": "summary_failed"}
            if not update:
                # Under the floor already (nothing evictable) — no-op.
                return {"outcome": "noop"}
            self._graph.update_state(config, {"messages": update}, as_node="dialogue")
            return {
                "outcome": "compacted",
                "before_tokens": before_tokens,
                "after_tokens": count_tokens_approximately(
                    [m for m in update if not isinstance(m, RemoveMessage)]
                ),
                "messages_removed": max(
                    0,
                    len(messages)
                    - len([m for m in update if not isinstance(m, RemoveMessage)]),
                ),
            }
        finally:
            self._release_thread(lock)

    # ---- public surface ----

    @staticmethod
    def _extract_thread_id(config: dict | None) -> str:
        if not isinstance(config, dict):
            raise ConcurrentRunError(
                "compile_graph requires a config dict with "
                "configurable.thread_id"
            )
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ConcurrentRunError(
                "compile_graph requires config['configurable']"
            )
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ConcurrentRunError(
                "compile_graph requires a non-empty configurable.thread_id"
            )
        return thread_id

    def _apply_recursion_limit(self, config: dict | None) -> dict:
        """Return ``config`` with ``recursion_limit`` set to the
        factory default if absent. Does not override an explicit
        caller value."""
        merged: dict = dict(config or {})
        merged.setdefault("recursion_limit", self.recursion_limit)
        return merged

    def invoke(self, input: Any, config: dict | None = None, **kwargs: Any) -> Any:
        thread_id = self._extract_thread_id(config)
        lock = self._acquire_thread(thread_id)
        try:
            return self._graph.invoke(
                input,
                self._apply_recursion_limit(config),
                **kwargs,
            )
        finally:
            self._release_thread(lock)

    async def ainvoke(self, input: Any, config: dict | None = None, **kwargs: Any) -> Any:
        thread_id = self._extract_thread_id(config)
        lock = self._acquire_thread(thread_id)
        try:
            return await self._graph.ainvoke(
                input,
                self._apply_recursion_limit(config),
                **kwargs,
            )
        finally:
            self._release_thread(lock)

    def stream(self, input: Any, config: dict | None = None, **kwargs: Any) -> Any:
        thread_id = self._extract_thread_id(config)
        lock = self._acquire_thread(thread_id)
        try:
            effective_config = self._apply_recursion_limit(config)
            if input is not None:
                configurable = dict(
                    effective_config.get("configurable") or {}
                )
                # Direct graph callers may omit the HTTP-owned id; mint
                # it at the start of a fresh run so its checkpoint can
                # still be resumed without minting on the resume path.
                configurable.setdefault("run_id", str(uuid.uuid4()))
                effective_config["configurable"] = configurable
            yield from self._graph.stream(
                input,
                effective_config,
                **kwargs,
            )
        finally:
            self._release_thread(lock)

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        """Clear pending checkpoint after a mid-flight terminal outcome.

        When a run dies mid-graph (model raise, empty stream with
        refuse frames, etc.) the HTTP layer persists a terminal
        ``RunRecord`` but LangGraph's checkpoint may still list
        pending ``next`` nodes. ``GET /threads/{id}`` then reports
        ``pending_checkpoint: true`` and the Resume banner appears
        above an already-terminal turn.

        Uses ``update_state(..., as_node="citation_gate")`` with the
        terminal ``status`` / ``status_reason`` so ``from_gate``
        routes to END. Messages are preserved — unlike
        ``delete_thread``. No-op when ``pending_nodes()`` is already
        empty (normal gate refuse / done paths). Acquires the
        per-thread lock (mirrors ``delete_thread``).
        """
        lock = self._acquire_thread(thread_id)
        try:
            if not self.pending_nodes(thread_id):
                return
            config = {"configurable": {"thread_id": thread_id}}
            self._graph.update_state(
                config,
                {"status": status, "status_reason": status_reason},
                as_node="citation_gate",
            )
        finally:
            self._release_thread(lock)

    def delete_thread(self, thread_id: str) -> None:
        """Forward ``thread_id`` to the inner graph's
        ``checkpointer.delete_thread`` under the SAME per-thread
        lock used by ``invoke`` / ``stream``.

        Why the lock: deleting a thread while a ``graph.invoke``
        is mid-flight on the same ``thread_id`` would race the
        saver's pending writes. The lock mirrors ``invoke``'s
        critical-section so delete serializes against in-flight
        runs (the second ``invoke`` would have raised
        ``ConcurrentRunError`` already, but T4 will stream
        longer-running runs that must not see a delete mid-flight).

        Why a public method (not ``graph._graph.checkpointer.delete_thread``
        from the route): the workspace HTTP layer deletes the
        metadata row FIRST and then needs to drop the
        checkpoint tuples. Reaching through ``_graph`` to
        ``checkpointer`` couples the route to the compiled
        graph's internals; this method is the documented seam.
        """
        lock = self._acquire_thread(thread_id)
        try:
            checkpointer = getattr(self._graph, "checkpointer", None)
            delete_thread_fn = getattr(checkpointer, "delete_thread", None)
            # WHY ``AttributeError`` (not a typed error): the HTTP
            # delete route distinguishes "graph wired without a
            # checkpointer" (a boot/wiring bug -> warn, metadata
            # row is already gone, return 200) from "the saver
            # itself failed" (runtime outage -> 500). Raising the
            # same exception type ``getattr`` would raise keeps
            # that split structural instead of string-matching on
            # messages.
            if not callable(delete_thread_fn):
                raise AttributeError(
                    "underlying graph has no checkpointer.delete_thread"
                )
            delete_thread_fn(thread_id)
        finally:
            self._release_thread(lock)


__all__ = [
    "RETRIEVE_ADAPTER_FAKE",
    "RETRIEVE_ADAPTER_KINDS",
    "RETRIEVE_ADAPTER_MCP",
    "ConcurrentRunError",
    "HostGraphError",
    "KnowledgeMCPFaultError",
    "RetrieveAdapterConfigError",
    "compile_graph",
    "analysis_from_tool_message",
    "comparison_from_tool_message",
]
