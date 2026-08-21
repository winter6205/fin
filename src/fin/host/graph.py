"""The v1 host graph (T6).

This module is the **only** module in ``src/fin/host/`` that knows
the host graph's topology. It is built from scratch here — no
``deer_flow`` import, no ``langgraph.prebuilt.create_react_agent``,
no vendored external agent tree (ADR 0019). The graph wires the
three nodes the v1 spec requires:

* ``model_node`` — invoke the chat model, increment ``step_count``
  once per entry, route on tool_calls.
* ``tool_node`` — execute ``list_files`` / ``read_file`` /
  ``retrieve`` via ``langgraph.prebuilt.ToolNode``, append the
  resulting ``ToolMessage``s and the corresponding ``EvidenceItem``s.
* ``citation_gate_node`` — call ``verify_citations`` and translate
  the gate result into the run-status enum.

The two fuses live in ``AgentState``:

* ``step_count`` increments once per ``model_node`` entry. When
  ``step_count >= STEP_CAP`` (default 32) the run ends with
  ``status == "step_cap"``. ``STEP_CAP`` is in fin state — NOT in
  LangGraph ``recursion_limit``. The graph factory sets
  LangGraph's recursion limit strictly higher (``RECURSION_LIMIT``
  = 50) so the agent-loop fuse always fires first (ADR 0008 +
  spec success criteria item 5).
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
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import Locator, load_extracted_tree
from fin.host.citation_gate import (
    Citation,
    EvidenceItem,
    verify_citations,
)
from fin.host.state import (
    AgentState,
    REPAIR_CAP,
    RECURSION_LIMIT,
    STEP_CAP,
)
from fin.host.tools import list_files, read_file
from fin.retrieve import BM25Adapter
from fin.retrieve.tool import retrieve


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


# ----- helpers -----


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"

# Marker the gate uses on the next pass to recognize a bounce
# template in the message history. The marker is data; CLI / HTTP
# rendering (T7, T9) strip it before printing per ADR 0021.
_BOUNCE_MARKER = "[FIN_HIDDEN_BOUNCE]"


def _resolve_path(value: Path | str | None, default: Path) -> Path:
    """Coerce ``value`` to a ``Path``; fall back to ``default``."""
    if value is None:
        return default
    return Path(value)


def _last_ai_text(messages: list[BaseMessage]) -> str:
    """Return the textual content of the last ``AIMessage`` in
    ``messages``. Empty string if there is no ``AIMessage`` or the
    last ``AIMessage`` is empty (model returned only tool_calls)."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str):
                return content
            # Some providers return content as a list of blocks;
            # the citation gate treats the raw string, so coerce.
            return "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
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
    def list_files_tool(company: str | None = None, period: str | None = None) -> dict:
        """List available 10-Q filings in the corpus.

        Optional ``company`` and ``period`` filters match the
        manifest's ``company`` and ``period`` fields exactly.
        Returns ``{"filings": [...]}``; unknown filters return
        ``{"filings": []}``.
        """
        return list_files(
            company=company,
            period=period,
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
            # faithful to the source page (ADR 0021).
            try:
                page_count_value = int(result["page_count"])
            except (KeyError, TypeError, ValueError):
                page_count_value = page
            end_page = min(page + page_limit - 1, page_count_value)
            for page_num in range(page, end_page + 1):
                locator = Locator(
                    document_id=result["document_id"],
                    version=result["version"],
                    page=page_num,
                )
                new_evidence.append(
                    EvidenceItem(locator=locator, page_text=result["text"])
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


def _make_retrieve_tool(adapter: BM25Adapter):
    """Build the ``retrieve`` host tool.

    The tool wraps T4's ``retrieve`` (which is the port-seam) with
    the factory's shared ``BM25Adapter``. On a hit, each chunk
    contributes one ``EvidenceItem`` to ``AgentState``. An empty
    contract (``{"chunks": []}``) contributes nothing.

    The tool's registered name is ``retrieve`` so the model can
    call it by name.
    """

    @tool("retrieve")
    def retrieve_tool(
        query: str,
        document_id: str | None = None,
        limit: int = 8,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Lexical BM25 retrieval over page-passage chunks.

        ``query`` is the lexical query. ``document_id`` is an
        optional corpus filter; unknown ids return empty. ``limit``
        defaults to 8 (ADR 0009 cap = 20). On a hit, each chunk
        contributes one ``EvidenceItem``; misses contribute none.
        """
        result = retrieve(
            query=query,
            document_id=document_id,
            limit=limit,
            adapter=adapter,
        )
        new_evidence: list[EvidenceItem] = []
        chunks = result.get("chunks", []) if isinstance(result, dict) else []
        for chunk in chunks:
            new_evidence.append(
                EvidenceItem(
                    locator=chunk.source_ref,
                    page_text=chunk.text,
                )
            )
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
        tool_message = ToolMessage(
            content=json.dumps({"chunks": serialized_chunks}, default=str),
            tool_call_id=tool_call_id,
            name="retrieve",
        )
        return Command(
            update={"messages": [tool_message], "evidence": new_evidence}
        )

    return retrieve_tool


# ----- the graph factory -----


def compile_graph(
    *,
    checkpointer: Any,
    model: Any,
    adapter: BM25Adapter | None = None,
    manifest_path: Path | str | None = None,
    extracted_root: Path | str | None = None,
    known_document_ids: set[str] | None = None,
    step_cap: int = STEP_CAP,
    recursion_limit: int = RECURSION_LIMIT,
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
        ``ScriptedChatModel``; production (T7 / T9) passes
        ``init_chat_model(os.environ["FIN_MODEL"])``. The factory
        calls ``bind_tools`` on it to register the three host
        tools.
    adapter
        An already-built ``BM25Adapter`` (T4). If ``None``, the
        factory builds one from ``manifest_path`` + ``extracted_root``
        so tests do not need to construct it manually. Production
        (T7) builds one at boot and passes it in.
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
    recursion_limit
        LangGraph's recursion limit. Default ``RECURSION_LIMIT``
        (50). Strictly greater than ``step_cap`` so the agent-loop
        fuse fires first (spec success criteria item 5).

    Returns
    -------
    _LockedGraph
        A proxy object whose ``invoke`` / ``stream`` / ``ainvoke``
        enforce one-at-a-time execution per ``thread_id``.

    Raises
    ------
    IngestMissingError
        If ``adapter`` is ``None`` and ``load_extracted_tree``
        cannot find the committed extracted tree. The factory
        does NOT swallow this — the operator must restore the
        tree and re-run ingest.
    """
    # Build adapter if not supplied. We propagate IngestMissingError
    # explicitly (the loader raises it; we let it propagate instead
    # of swallowing as a miss dict).
    if adapter is None:
        mpath = _resolve_path(manifest_path, _DEFAULT_MANIFEST_PATH)
        rpath = _resolve_path(extracted_root, _DEFAULT_EXTRACTED_ROOT)
        tree = load_extracted_tree(mpath, rpath)
        adapter = BM25Adapter(tree)
        if known_document_ids is None:
            known_document_ids = set(tree.keys())

    if known_document_ids is None:
        # Defensive: a caller passing an external adapter but no
        # known_document_ids set must declare the set explicitly.
        raise ValueError(
            "compile_graph requires either `adapter` or "
            "`known_document_ids` (or `manifest_path` + "
            "`extracted_root` so the factory can derive it)."
        )

    mpath = _resolve_path(manifest_path, _DEFAULT_MANIFEST_PATH)
    rpath = _resolve_path(extracted_root, _DEFAULT_EXTRACTED_ROOT)

    list_files_tool = _make_list_files_tool(mpath, rpath)
    read_file_tool = _make_read_file_tool()
    retrieve_tool = _make_retrieve_tool(adapter)

    bound_model = model.bind_tools(
        [list_files_tool, read_file_tool, retrieve_tool]
    )

    tool_dispatcher = ToolNode(
        [list_files_tool, read_file_tool, retrieve_tool],
        name="tool_node",
        handle_tool_errors=True,
    )

    # ----- node functions -----

    def model_node(state: AgentState) -> dict[str, Any]:
        # Step 1: step-cap fuse. Increment ``step_count`` and check
        # BEFORE invoking the model — when the fuse fires we never
        # call the LLM again (spec success criteria item 5).
        previous = int(state.get("step_count", 0))
        next_step = previous + 1
        if next_step >= step_cap:
            # Step cap wins on the next entry: we have already
            # spent ``step_cap`` agent-loop ticks without reaching
            # done / refuse. ADR 0008 — this is the runaway fuse,
            # NOT a citation failure.
            return {
                "messages": [],
                "step_count": next_step,
                "status": "step_cap",
                "status_reason": "step_cap",
            }
        # Step 2: invoke the model. The script is the source of
        # truth — this is the only place we call the LLM in v1.
        answer_message: BaseMessage = bound_model.invoke(state["messages"])
        # NOTE: ``evidence`` is intentionally omitted from this return.
        # The ``operator.add`` reducer in ``AgentState`` only fires when
        # a node actually returns the key; omitting it leaves the prior
        # evidence list untouched (tool_node returns the new items).
        return {
            "messages": [answer_message],
            "step_count": next_step,
            "status": "running",
            "repair_count": int(state.get("repair_count", 0)),
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

    def from_model(state: AgentState) -> Literal["tools", "citation_gate", "__end__"]:
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
            return "tools"
        return "citation_gate"

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
    graph.add_node("model", model_node)
    graph.add_node("tools", tool_dispatcher)
    graph.add_node("citation_gate", citation_gate_node)

    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", from_model)
    graph.add_edge("tools", "model")
    graph.add_conditional_edges("citation_gate", from_gate)

    compiled = graph.compile(checkpointer=checkpointer)

    return _make_locked_graph(
        checkpointer=checkpointer,
        model=model,
        step_cap=step_cap,
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
) -> "_LockedGraph":
    key = (
        id(checkpointer),
        id(model),
        int(step_cap),
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
            yield from self._graph.stream(
                input,
                self._apply_recursion_limit(config),
                **kwargs,
            )
        finally:
            self._release_thread(lock)


__all__ = [
    "ConcurrentRunError",
    "HostGraphError",
    "compile_graph",
]
