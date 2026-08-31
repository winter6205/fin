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
  ``RECURSION_LIMIT`` in ``state.py``, currently 70) so the
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

The factory propagates ``IngestMissingError`` from
``load_extracted_tree`` (ADR 0020): if the committed extracted tree
is missing or incomplete the operator must restore the frozen PDF
(or refresh the manifest) and re-run ingest. ``compile_graph``
does NOT auto-ingest.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Iterable, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Send

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import Locator, load_extracted_tree
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
from fin.host.state import (
    AgentState,
    FANOUT_RESET,
    REPAIR_CAP,
    RECURSION_LIMIT,
    STEP_CAP,
    TERMINAL_STATUSES,
)
from fin.host.agent_instructions import messages_for_model
from fin.host.degradation_notice import (
    DEGRADATION_NOTICE_FIELD,
    consume_degradation,
    degradation_notice_entry,
)
from fin.host.stream_map import (
    assemble_final_ai_message,
    format_tool_summary,
    inline_thinking_segments,
    strip_thinking_from_answer,
)
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
from fin.host.track_decision import decide_track, snapshot_delta_for_utterance
from fin.retrieve import MCPRetrieveAdapter, FakeRetrievePort
from fin.retrieve.degradation import (
    DegradationLedger,
    degradation_ledger_from_env,
)
from fin.retrieve.hit_mapping import MappingFailureLedger, ledger_from_env
from fin.retrieve.tool import retrieve
from fin.retrieve.port import RetrievePort

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


class RetrieveAdapterConfigError(HostGraphError):
    """The requested retrieve adapter cannot be built as configured.

    Raised for an unknown ``retrieve_adapter`` kind, and for a
    product MCP adapter that would be built with no usable
    ``locator_map`` — a map-less MCP adapter drops every hit
    (ADR 0030 mapper-drop), so it would answer every query with
    ``{"chunks": []}`` while looking perfectly healthy. Refusing
    to build it turns a silent always-empty retrieve into a boot
    failure the operator can act on.
    """


# ----- helpers -----


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"
_DEFAULT_FACTS_ROOT = _DEFAULT_DATA_DIR / "facts"

# The two retrieve adapter kinds ``compile_graph`` can auto-build.
# ``BM25Adapter`` is deliberately absent: ADR 0030 archived it from
# the product path, and offline tests pass it in as ``adapter=``.
RETRIEVE_ADAPTER_FAKE = "fake"
RETRIEVE_ADAPTER_MCP = "mcp"
RETRIEVE_ADAPTER_KINDS = (RETRIEVE_ADAPTER_FAKE, RETRIEVE_ADAPTER_MCP)

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


def _locator_map_from_tree(
    tree: dict[str, tuple[Any, ...]],
) -> dict[str, Locator]:
    """Publish one ``iknow-memory id -> fin Locator`` entry per
    frozen locator unit in the extracted ``tree``.

    ``MCPRetrieveAdapter`` maps each hit's ``source_ref`` through
    this dict and DROPS whatever is missing, so the map is the
    only thing standing between a healthy MCP server and a
    retrieve path that returns nothing.

    The published id spelling is ``"{document_id}#{page}"``: the
    identity fin can address without reading iknow-memory's
    storage. An operator whose iknow-memory corpus was ingested
    under different ids passes an explicit ``locator_map`` to
    ``compile_graph``; a stale mapping degrades safely because the
    citation verifier still matches the claimed span against the
    frozen unit's text.
    """
    return {
        f"{document_id}#{page.locator.page}": page.locator
        for document_id, pages in tree.items()
        for page in pages
    }


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


def _make_list_files_tool(manifest_path: Path, extracted_root: Path):
    """Build the ``list_files`` host tool.

    Returns a LangChain ``BaseTool`` whose function calls T3's
    ``list_files`` with the factory's resolved paths. The tool does
    NOT contribute evidence — it returns catalog metadata only.
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
        manifest's ``company`` and ``period`` fields exactly.
        Additive EDGAR-shaped filters (``cik``, ``form``,
        ``ticker``, exact ``filing_date``) per ADR 0031 are
        forwarded through. Returns ``{"filings": [...]}`; unknown
        filters, conflicting filters, and empty filter values
        all return ``{"filings": []}``.
        """
        return list_files(
            company=company,
            period=period,
            cik=cik,
            form=form,
            ticker=ticker,
            filing_date=filing_date,
            manifest_path=manifest_path,
            extracted_root=extracted_root,
        )

    return list_files_tool


def _make_read_file_tool():
    """Build the ``read_file`` host tool.

    The tool wraps T3's ``read_file`` and, on a successful page
    read, contributes one ``EvidenceItem`` to ``AgentState`` per
    page in the joined ``text`` slice. A miss dict
    (``{"error": "unknown_locator"}``) contributes nothing.

    The tool's registered name is ``read_file`` so the model can
    call it by name.
    """

    @tool("read_file")
    def read_file_tool(
        document_id: str,
        version: str,
        page: int = 1,
        page_limit: int = 1,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Read a page from a frozen 10-Q filing.

        ``document_id`` matches the manifest's ``document_id``
        (e.g. ``AAPL-2022Q3``). ``version`` is the manifest
        sha256. ``page`` is 1-based; ``page_limit`` is clamped to
        3 (ADR 0009). Misses return ``{"error": "unknown_locator"}``
        and contribute no evidence.
        """
        result = read_file(
            document_id=document_id,
            version=version,
            page=page,
            page_limit=page_limit,
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


def _make_retrieve_tool(
    adapter: RetrievePort,
    *,
    mapping_ledger: MappingFailureLedger | None = None,
    degradation_ledger: DegradationLedger | None = None,
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

    Typed EXIT (spec s1-corpus-retrieve § Boundaries): when the
    port returns ``{"error": "retrieve_unavailable"}`` (product
    MCP transport / process down, ADR 0030) the tool serializes
    THAT dict as the ``ToolMessage`` content. It must not be
    rewritten as ``{"chunks": []}`` — the miss shape would tell
    the model the corpus has nothing when retrieval never ran.
    The error contributes no evidence and triggers no BM25
    fallback; the model can retry or say retrieval is down.

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
        ``{"error": "retrieve_unavailable"}``.
        """
        result = retrieve(
            query=query,
            document_id=document_id,
            cik=cik,
            form=form,
            ticker=ticker,
            filing_date=filing_date,
            limit=limit,
            adapter=adapter,
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
        return Command(
            update={"messages": [tool_message], "evidence": new_evidence}
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


def _make_compare_facts_tool(facts_root: Path):
    """Build the ``compare_facts`` host tool (T3 / spec Focus 1).

    Cross-period and peer comparison is a HOST TOOL result, not a
    prose table the model writes: the tool resolves each cell against
    the ingest-built fact table (locator per cell) and computes the
    deltas itself. Like ``get_xbrl_fact`` it contributes NO
    ``EvidenceItem`` — a fact row is not page text, so the citation
    verifier still requires a read / retrieve span before any prose
    answer passes.

    Returns a plain dict so the prebuilt ``ToolNode`` serializes it as
    ToolMessage JSON; ``tool_node_with_summaries`` then lifts the
    ``comparison`` envelope onto ``AgentState`` for the turn row, and
    the numeric gate reads its columns as ``(concept, period)``
    claims.
    """

    @tool("compare_facts")
    def compare_facts_tool(
        concept: str,
        periods: list[str],
        document_ids: list[str] | None = None,
        issuers: list[str] | None = None,
    ) -> dict:
        """Compare one XBRL concept across periods and/or issuers.

        ``concept`` is the XBRL tag (e.g. ``us-gaap:Revenues``).
        ``periods`` are the reporting periods to use as columns
        (e.g. ``["2022-Q3-YTD", "2023-Q3-YTD"]``). Rows come from
        ``issuers`` (tickers, resolved across that issuer's filings —
        use this for cross-period) or ``document_ids`` (exact
        filings); with neither, every filing holding facts is a row.
        Returns a structured comparison table with per-cell locators
        and code-computed deltas. Cells with no fact row are marked
        ``missing``; an unresolvable request returns the typed
        ``empty_comparison`` / ``comparison_overflow`` dict. Never
        raises. Quote only the values this table reports.
        """
        from fin.host.tools.compare_facts import compare_facts

        return compare_facts(
            concept=concept,
            periods=list(periods or []),
            document_ids=list(document_ids) if document_ids else None,
            issuers=list(issuers) if issuers else None,
            facts_root=facts_root,
        )

    return compare_facts_tool


class IssuerTask(TypedDict, total=False):
    """The payload one ``Send`` carries to a fan-out worker (T6).

    Declared as the worker node's input schema so a branch reads its
    own issuer instead of the whole ``AgentState``: a worker that could
    see the accumulated evidence of its siblings would be a per-issuer
    node that quietly depends on join order.
    """

    issuer: str
    query: str


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
        if not call_id:
            continue
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


def _collect_comparisons(messages: list[BaseMessage]) -> list[dict]:
    """Every renderable comparison in ``messages``, in call order."""
    found: list[dict] = []
    for message in messages:
        comparison = comparison_from_tool_message(message)
        if comparison is not None:
            found.append(comparison)
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
    claimed = fact_claims + [
        pair for pair in compare_claims if pair not in fact_claims
    ]
    if not claimed:
        # No numeric lookup -> gate is skipped. We return ``None`` so
        # the caller can distinguish "no claim" from "claim, gate
        # passed" (which would be ``outcome="pass"``).
        return None
    answer = _last_ai_text(messages)
    return evaluate_numeric_gate(
        answer=answer,
        claimed_concepts=claimed,
        available_facts=fact_rows + compare_rows,
    )


def compile_graph(
    *,
    checkpointer: Any,
    model: Any,
    adapter: RetrievePort | None = None,
    manifest_path: Path | str | None = None,
    extracted_root: Path | str | None = None,
    facts_root: Path | str | None = None,
    known_document_ids: set[str] | None = None,
    step_cap: int = STEP_CAP,
    issuer_cap: int = MAX_ISSUERS,
    recursion_limit: int = RECURSION_LIMIT,
    classify_model: Any | None = None,
    retrieve_adapter: str = RETRIEVE_ADAPTER_FAKE,
    mcp_command: list[str] | None = None,
    locator_map: dict[str, "Locator"] | None = None,
    mapping_failure_ledger: MappingFailureLedger | None = None,
    degradation_ledger: DegradationLedger | None = None,
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
    manifest_path
        Path to ``data/corpus/manifest.json``. Defaults to the
        repo's canonical location.
    extracted_root
        Path to ``data/corpus/extracted/``. Defaults to the
        repo's canonical location. ``IngestMissingError`` from
        ``load_extracted_tree`` propagates to the caller
        (ADR 0020).
    known_document_ids
        The corpus manifest's ``document_id`` set. ``None`` (the
        default) reads it from the manifest on disk; tests inject a
        smaller fixture set.
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
        from ``fin.host.state`` (currently ``2 * STEP_CAP + 6 = 70``).
        Strictly greater than ``step_cap`` so the agent-loop fuse
        fires first (spec success criteria item 5).
    facts_root
        Path to ``data/corpus/facts/`` (T5). Defaults to the repo's
        canonical location. The factory wires the
        ``get_xbrl_fact`` host tool to read from this root.
        Tests inject a synthetic ``tmp_path``-rooted tree.
    classify_model
        Soft-Track T4: optional model for ambiguous LLM classify.
        When ``None`` (default), the LLM tier is skipped. The
        residual branch in ``decide_track`` then resolves the
        bare-``q`` cascade to ``track="dialogue"`` (not research),
        so the model leaf is not the silent global default.
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
        supplied. Defaults to ``["iknow-memory", "serve"]``.
    locator_map
        T3 (#76): iknow-memory id → ``fin.Locator`` map passed
        to the auto-built ``MCPRetrieveAdapter``. ``None`` (the
        default) derives the map from the extracted tree via
        :func:`_locator_map_from_tree`; an explicit map overrides
        that derivation. An empty effective map is refused —
        see ``RetrieveAdapterConfigError``.
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

    Returns
    -------
    _LockedGraph
        A proxy object whose ``invoke`` / ``stream`` / ``ainvoke``
        enforce one-at-a-time execution per ``thread_id``.

    Raises
    ------
    IngestMissingError
        If the ``"mcp"`` auto-build needs the extracted tree and
        ``load_extracted_tree`` cannot find it. The factory does
        NOT swallow this — the operator must restore the tree and
        re-run ingest.
    RetrieveAdapterConfigError
        If ``retrieve_adapter`` names an unknown kind, or the
        ``"mcp"`` auto-build would produce an adapter with an
        empty ``locator_map``.
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
            mpath = _resolve_path(manifest_path, _DEFAULT_MANIFEST_PATH)
            rpath = _resolve_path(extracted_root, _DEFAULT_EXTRACTED_ROOT)
            # The tree is the SSOT for BOTH the client-side
            # document membership test and the iknow-id → locator
            # map; load it once when either is missing.
            if known_document_ids is None or locator_map is None:
                tree = load_extracted_tree(mpath, rpath)
                if known_document_ids is None:
                    known_document_ids = set(tree.keys())
                if locator_map is None:
                    locator_map = _locator_map_from_tree(tree)
            if not locator_map:
                raise RetrieveAdapterConfigError(
                    "the product MCP retrieve adapter needs a non-empty "
                    "locator_map: unmapped hits are dropped, so a "
                    "map-less adapter answers every query with no "
                    "chunks. Pass locator_map= or point "
                    "manifest_path / extracted_root at an ingested "
                    "corpus."
                )
            adapter = MCPRetrieveAdapter(
                command=mcp_command or ["iknow-memory", "serve"],
                locator_map=locator_map,
                known_document_ids=known_document_ids,
                timeout_seconds=5.0,
                mapping_ledger=mapping_failure_ledger,
            )

    if known_document_ids is None:
        # Defensive: a caller passing an external adapter but no
        # known_document_ids set must declare the set explicitly.
        # The auto-build path always sets this above, so a None
        # reaching this branch means the caller passed an external
        # adapter without declaring the corpus.
        raise ValueError(
            "compile_graph requires either `adapter` or "
            "`known_document_ids` (or `manifest_path` + "
            "`extracted_root` so the factory can derive it)."
        )

    mpath = _resolve_path(manifest_path, _DEFAULT_MANIFEST_PATH)
    rpath = _resolve_path(extracted_root, _DEFAULT_EXTRACTED_ROOT)
    fpath = _resolve_path(facts_root, _DEFAULT_FACTS_ROOT)

    list_files_tool = _make_list_files_tool(mpath, rpath)
    read_file_tool = _make_read_file_tool()
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

    bound_model = model.bind_tools(
        [
            list_files_tool,
            read_file_tool,
            retrieve_tool,
            get_xbrl_fact_tool,
            compare_facts_tool,
            research_issuers_tool,
        ]
    )
    # T3 (#34): the off-preference binding layers the MiniMax
    # ``extra_body`` (``thinking.type=disabled``) on top of the same
    # bound tools. ``reasoning_split`` STAYS ``True`` so
    # ``reasoning_details`` round-trip / replay is unaffected by the
    # preference (spec line 28). ``model_node`` selects per run via
    # ``configurable.thinking_type`` — no shared model mutation, so
    # concurrent runs on other threads cannot leak the preference.
    _thinking_off_body = {
        "thinking": {"type": "disabled"},
        "reasoning_split": True,
    }
    bound_model_no_thinking = bound_model.bind(extra_body=_thinking_off_body)
    # Soft-Track T1: non-research leaves call the UNBOUND model
    # (no bind_tools) with call-scoped per-track prompts.
    unbound_model_no_thinking = model.bind(extra_body=_thinking_off_body)

    tool_dispatcher = ToolNode(
        [
            list_files_tool,
            read_file_tool,
            retrieve_tool,
            get_xbrl_fact_tool,
            compare_facts_tool,
        ],
        name="tool_node",
        handle_tool_errors=True,
    )

    # ----- node functions -----

    def classify_node(state: AgentState) -> dict[str, Any]:
        """Soft-Track: tiered track decision before leaves.

        Passes ``status_snapshots`` and optional ``classify_model``
        into ``decide_track``. Appends snapshots when the utterance
        sets or changes research context.
        """
        messages = list(state.get("messages") or [])
        prior_snapshots = list(state.get("status_snapshots") or [])
        decision = decide_track(
            messages,
            snapshots=prior_snapshots,
            classify_model=classify_model,
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
        return update

    def _select_unbound_model(config: RunnableConfig) -> Any:
        configurable = dict((config or {}).get("configurable") or {})
        if configurable.get("thinking_type") == "disabled":
            return unbound_model_no_thinking
        return model

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
        stream = selected.stream(
            messages_for_model(
                state["messages"], track=track, missing_slots=missing_slots
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
        stream = selected_model.stream(
            messages_for_model(state["messages"], track="research")
        )
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
            "track": "research",
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
        ``ToolNode._func`` returns a LIST of per-call updates —
        each either a ``Command(update={...})`` or a plain
        ``{messages: [ToolMessage]}`` dict — instead of one flat
        dict. LangGraph merges that list itself when a node returns
        it verbatim, but this wrapper must merge it manually to
        attach ``tool_summaries``. We fold every entry's
        ``messages`` / ``evidence`` into ONE state update so the
        reducer semantics are identical to the unwrapped node.
        """
        pending = list(_pending_tool_calls(state))
        raw_update: Any = tool_dispatcher.invoke(state)

        merged_messages: list[Any] = []
        merged_evidence: list[Any] = []
        if isinstance(raw_update, dict):
            merged_messages.extend(raw_update.get("messages", []))
        elif isinstance(raw_update, list):
            # Per-call entries in tool-call order; flatten.
            for entry in raw_update:
                if isinstance(entry, Command):
                    payload = entry.update or {}
                    merged_messages.extend(payload.get("messages", []))
                    merged_evidence.extend(payload.get("evidence", []))
                elif isinstance(entry, dict):
                    merged_messages.extend(entry.get("messages", []))
                elif isinstance(entry, list):
                    for item in entry:
                        if isinstance(item, Command):
                            payload = item.update or {}
                            merged_messages.extend(payload.get("messages", []))
                            merged_evidence.extend(payload.get("evidence", []))
                        elif isinstance(item, BaseMessage):
                            merged_messages.append(item)
                elif isinstance(entry, BaseMessage):
                    merged_messages.append(entry)

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
        return update

    def _fanout_tool_update(
        payload: dict[str, Any], tool_call_id: str
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
                        ISSUER_FANOUT_TOOL, result=payload
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
        update.update(_fanout_tool_update(plan, tool_call_id))
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
                adapter=adapter,
            )
        except Exception as exc:  # noqa: BLE001 — one branch, not the run
            # A port that raises breaks its own contract ("empty hits,
            # not exceptions"). Letting it propagate would kill the
            # join and take the other issuers' work with it, so the
            # branch degrades to the typed unavailable row — which is
            # reported in the envelope, never swallowed.
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
        return {
            **_fanout_tool_update(result, tool_call_id),
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

        gate_result = verify_citations(
            answer=candidate_answer,
            evidence=evidence,
            repair_count=repair_count,
            known_document_ids=set(known_document_ids or ()),
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
            return {
                "messages": [],
                "status": "refuse",
                "status_reason": gate_result.reason,
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

    # ----- conditional edges -----

    def from_classify(
        state: AgentState,
    ) -> Literal["dialogue", "clarify", "off_topic", "model"]:
        """Route Soft-Track leaves after classify."""
        # No single global default track (specs/soft-track.md):
        # missing track defaults to the dialogue leaf rather than
        # the research model so the conditional edge mirrors the
        # residual branch in ``decide_track``.
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
        if state.get("track") == "research":
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
            return "model"
        return [
            Send("issuer_research", {"issuer": issuer, "query": query})
            for issuer in issuers
        ]

    def from_gate(state: AgentState) -> Literal["model", "__end__"]:
        # Status-based routing out of the citation gate. Done /
        # refuse / step_cap all END; ``"running"`` (after a bounce)
        # routes back to the model with the hidden template
        # already injected.
        status = state.get("status", "running")
        if status in ("done", "refuse", "step_cap"):
            return END
        return "model"

    # ----- assemble the graph -----

    graph = StateGraph(AgentState)
    graph.add_node("classify", classify_node)
    graph.add_node("dialogue", dialogue_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("off_topic", off_topic_node)
    graph.add_node("model", model_node)
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
    graph.add_conditional_edges("classify", from_classify)
    graph.add_edge("dialogue", END)
    graph.add_edge("clarify", END)
    graph.add_edge("off_topic", END)
    graph.add_conditional_edges("model", from_model)
    graph.add_edge("tools", "model")
    graph.add_conditional_edges(
        "issuer_fanout", from_issuer_fanout, ["issuer_research", "model", END]
    )
    graph.add_edge("issuer_research", "issuer_join")
    graph.add_edge("issuer_join", "model")
    graph.add_conditional_edges("citation_gate", from_gate)

    compiled = graph.compile(checkpointer=checkpointer)

    return _make_locked_graph(
        checkpointer=checkpointer,
        model=model,
        classify_model=classify_model,
        step_cap=step_cap,
        issuer_cap=issuer_cap,
        recursion_limit=recursion_limit,
        compiled_graph=compiled,
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
_IDEMPOTENCE_CACHE: dict[tuple, "_LockedGraph"] = {}


def _make_locked_graph(
    *,
    compiled_graph: Any,
    recursion_limit: int,
    checkpointer: Any,
    model: Any,
    step_cap: int,
    classify_model: Any | None = None,
    issuer_cap: int = MAX_ISSUERS,
) -> "_LockedGraph":
    key = (
        id(checkpointer),
        id(model),
        id(classify_model) if classify_model is not None else 0,
        int(step_cap),
        # T6: two graphs that differ ONLY in the issuer cap are two
        # different graphs; without this the cache would hand a test
        # tuning the cap the previously-compiled default.
        int(issuer_cap),
        int(recursion_limit),
    )
    cached = _IDEMPOTENCE_CACHE.get(key)
    if cached is not None:
        return cached
    proxy = _LockedGraph(
        compiled_graph=compiled_graph, recursion_limit=recursion_limit
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

    def __init__(self, *, compiled_graph: Any, recursion_limit: int) -> None:
        self._graph = compiled_graph
        # ``recursion_limit`` is exposed via the public attribute
        # ``recursion_limit`` so tests can assert it directly.
        self.recursion_limit = int(recursion_limit)
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

    @staticmethod
    def _release_thread(lock: threading.Lock) -> None:
        lock.release()

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
    "RetrieveAdapterConfigError",
    "compile_graph",
    "comparison_from_tool_message",
]
