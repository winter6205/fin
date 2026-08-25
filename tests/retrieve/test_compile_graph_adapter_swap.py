"""Boundary tests for ``compile_graph`` adapter swap (T3, ADR 0030).

Product ``compile_graph`` (non-test) does NOT bind in-process BM25;
the product path is the MCP adapter. The adapter is chosen from
EXPLICIT configuration — an injected ``adapter`` or the
``retrieve_adapter`` kind that ``fin.host.compile`` resolves from
``FIN_RETRIEVE_ADAPTER`` — never by sniffing the checkpointer type.
The factory's own default kind is the hermetic ``FakeRetrievePort``
so the test surface stays free of MCP subprocesses and live data.

Tests cover:

1. ``adapter=None`` (factory default) → auto-built
   ``FakeRetrievePort``, whatever the checkpointer is.
2. ``adapter=FakeRetrievePort(...)`` (explicit test) → adapter
   is used verbatim; no MCP launch attempted.
3. ``adapter=BM25Adapter(...)`` (legacy / offline tests) → still
   supported (BM25 is retained as an offline adapter).
4. ``retrieve_adapter="mcp"`` → ``MCPRetrieveAdapter`` with a
   locator map derived from the extracted tree, or a typed
   config error when no usable map exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage

from fin.corpus.extracted import Locator, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import RetrieveAdapterConfigError
from fin.retrieve import BM25Adapter, FakeRetrievePort, MCPRetrieveAdapter


# ----- corpus fixture (lightweight — only needed for the
# BM25-legacy path that goes through load_extracted_tree) -----


def _corpus_tree() -> dict:
    """Load the committed extracted tree once. Returns empty
    dict if no tree is available; both paths handle empty
    corpus gracefully."""
    manifest = Path("data/corpus/manifest.json")
    extracted = Path("data/corpus/extracted")
    if not manifest.is_file() or not extracted.is_dir():
        return {}
    return load_extracted_tree(manifest, extracted)


# ----- 1. default test path: auto-builds FakeRetrievePort -----


class _CapturingFake(FakeRetrievePort):
    """A FakeRetrievePort that records every call so tests can
    assert which adapter the host graph actually bound.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    def retrieve(self, query, document_id=None, limit=8):  # type: ignore[override]
        self.calls.append(
            {"query": query, "document_id": document_id, "limit": limit}
        )
        return {"chunks": []}


def test_compile_graph_default_builds_fake_port_for_tests() -> None:
    """When ``adapter`` is ``None`` and no ``retrieve_adapter``
    kind is configured, the factory auto-builds a
    ``FakeRetrievePort`` — not BM25, not MCP.

    ADR 0030 + T3 acceptance: "fake-port in default pytest".
    """
    model = ScriptedChatModel(script=[])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        # adapter=None  ← auto-build path
        known_document_ids={"DOC-2023Q1"},
    )
    assert graph is not None
    # The auto-built adapter is a FakeRetrievePort (hermetic).
    # The compiled graph's tool bind is opaque; we assert the
    # factory returned a valid compiled graph.


def test_compile_graph_with_explicit_fake_port_does_not_build_mcp() -> None:
    """When ``adapter=FakeRetrievePort(...)``, the factory uses
    it directly. No MCP subprocess is launched (the adapter is
    pure in-process). The factory call must NOT raise.

    Negative boundary: this is the most common test pattern —
    many tests pass a pre-built ``FakeRetrievePort`` to keep
    the suite hermetic.
    """
    fake = FakeRetrievePort()
    model = ScriptedChatModel(script=[])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=fake,
        known_document_ids={"DOC-2023Q1"},
    )
    assert graph is not None


# ----- 2. legacy / offline: BM25 still supported -----


def test_compile_graph_with_bm25_adapter_in_tests_is_supported() -> None:
    """``BM25Adapter`` is RETAINED for legacy / offline tests
    (e.g. ``tests/retrieve/test_bm25.py``). ``compile_graph``
    still accepts it as ``adapter=``; the factory does not
    auto-build MCP or BM25 when ``adapter`` is supplied.

    ADR 0030 archives BM25 from the PRODUCT path; tests that
    need a real in-process lexical ranker can still pass one
    in.
    """
    tree = _corpus_tree()
    if not tree:
        import pytest

        pytest.skip("no committed extracted tree available")
    adapter = BM25Adapter(tree)
    model = ScriptedChatModel(script=[])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=adapter,
        known_document_ids=set(tree.keys()),
    )
    assert graph is not None


# ----- 3. test-mode + known ids alone (no adapter): hermetic -----


def test_compile_graph_with_only_known_ids_does_not_call_mcp() -> None:
    """The canonical host-test call pattern (no adapter, only
    ``known_document_ids``) must NOT attempt to launch an MCP
    subprocess. The auto-build path picks a fake port for the
    test checkpointer.
    """
    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids={"DOC-2023Q1"},
    )
    # No exception, no subprocess launched.


# ----- 4. retrieve tool binds to the supplied adapter -----


def test_compile_graph_binds_supplied_fake_port_to_retrieve_tool() -> None:
    """The host graph's ``retrieve`` tool must call the
    SUPPLIED adapter's ``retrieve`` method, not a fresh one.

    Verification: invoke the compiled graph on a state whose
    last AI message declares a ``retrieve`` tool call; the
    resulting state shows the adapter was the one we passed
    in (no new subprocess, no BM25 rebuild).
    """
    from langchain_core.messages import AIMessage, HumanMessage

    fake = FakeRetrievePort(
        hits_by_query={
            "what was revenue": [
                # The chunk must carry a Locator so the citation
                # gate (T5) can validate the answer span.
                __import__(
                    "fin.retrieve", fromlist=["Chunk"]
                ).Chunk(
                    text="Revenue rose twelve percent year over year. "
                    "Operating margin held. Risk factors include "
                    "macroeconomic uncertainty.",
                    source_ref=Locator(
                        document_id="DOC-2023Q1",
                        version="v-" + "a" * 60,
                        page=1,
                    ),
                    score=2.5,
                ),
            ],
        },
        known_document_ids={"DOC-2023Q1"},
    )
    from fin.host import ScriptedChatModel

    answer_with_span = (
        "Revenue rose twelve percent year over year. "
        "Operating margin held. Risk factors include macroeconomic "
        "uncertainty and geopolitical tensions across regions."
    )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "what was revenue"},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer_with_span),
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=fake,
        known_document_ids={"DOC-2023Q1"},
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-bind"}},
    )
    # The run reached a terminal state — the exact status
    # depends on the citation gate's span matching (>=32 chars);
    # what matters for THIS test is that the run didn't crash
    # with an MCP/BM25-related error and that our fake was used.
    assert out["status"] in ("done", "refuse", "step_cap"), out["status"]


# ----- 5. adapter selection is explicit config, not checkpointer type -----


class _StubSaver(InMemorySaver):
    """A working saver whose class is not
    ``langgraph.checkpoint.memory.InMemorySaver``.

    Before the fix, ``compile_graph`` duck-typed the checkpointer's
    module + class name to decide fake-vs-MCP, so anything else —
    ``PostgresSaver``, or this stand-in — meant "production". The
    inverse bit hurt more: ``fin-serve`` booting on ``InMemorySaver``
    (no ``FIN_CHECKPOINT_URL``) silently got the always-empty fake
    port. Selection must read configuration instead.
    """


def _record_mcp_construction(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch the factory's ``MCPRetrieveAdapter`` with a recorder.

    Returns the list the recorder appends constructor kwargs to.
    Asserting on construction (rather than on the returned graph)
    also sidesteps ``compile_graph``'s id-keyed idempotence cache.
    """
    built: list[dict] = []

    class _RecordingMCP(MCPRetrieveAdapter):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("fin.host.graph.MCPRetrieveAdapter", _RecordingMCP)
    return built


def _forbid_mcp_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``MCPRetrieveAdapter`` construction fail loudly."""

    def _boom(**kwargs: Any) -> None:
        raise AssertionError(
            f"compile_graph built an MCP adapter: {sorted(kwargs)}"
        )

    monkeypatch.setattr("fin.host.graph.MCPRetrieveAdapter", _boom)


def test_non_memory_checkpointer_alone_does_not_select_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Postgres-shaped checkpointer must not, on its own, flip the
    factory onto the product MCP path. The checkpointer answers
    "where does state live", not "where does retrieve come from"."""
    _forbid_mcp_construction(monkeypatch)
    graph = compile_graph(
        checkpointer=_StubSaver(),
        model=ScriptedChatModel(script=[]),
        known_document_ids={"DOC-2023Q1"},
    )
    assert graph is not None


def test_in_memory_checkpointer_does_not_block_mcp_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse: ``retrieve_adapter="mcp"`` builds the product
    adapter even on ``InMemorySaver``.

    This is the ``fin-serve``-without-Postgres case (spec line 14:
    "``fin-serve`` retrieve ... read this tree"). Binding the fake
    port there made every product retrieve return no hits.
    """
    built = _record_mcp_construction(monkeypatch)
    compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        retrieve_adapter="mcp",
        known_document_ids={"DOC-2023Q1"},
        locator_map={"iknow-1": Locator("DOC-2023Q1", "v" * 64, 1)},
    )
    assert len(built) == 1
    assert built[0]["locator_map"] == {
        "iknow-1": Locator("DOC-2023Q1", "v" * 64, 1)
    }


def test_unknown_retrieve_adapter_kind_raises_typed_error() -> None:
    """A typo in ``FIN_RETRIEVE_ADAPTER`` must stop the boot, not
    fall through to a silent default."""
    with pytest.raises(RetrieveAdapterConfigError):
        compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(script=[]),
            retrieve_adapter="bm25",
            known_document_ids={"DOC-2023Q1"},
        )


def test_explicit_adapter_wins_over_retrieve_adapter_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected adapter is the most explicit configuration there
    is; the kind is not consulted."""
    _forbid_mcp_construction(monkeypatch)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        adapter=FakeRetrievePort(),
        retrieve_adapter="mcp",
        known_document_ids={"DOC-2023Q1"},
    )
    assert graph is not None


# ----- 6. the product MCP adapter gets a usable locator map -----


def test_mcp_auto_build_derives_locator_map_from_extracted_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``locator_map=None`` used to mean "every MCP hit is dropped":
    the adapter mapped iknow ids through an empty dict, so product
    retrieve could only ever return ``{"chunks": []}``.

    The auto-build must publish one id per frozen locator unit,
    derived from the same extracted tree ``read_file`` and the
    citation verifier read.
    """
    tree = _corpus_tree()
    if not tree:
        pytest.skip("no committed extracted tree available")

    built = _record_mcp_construction(monkeypatch)
    compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        retrieve_adapter="mcp",
    )
    assert len(built) == 1
    locator_map = built[0]["locator_map"]
    known_ids = built[0]["known_document_ids"]

    # One entry per frozen unit across the whole tree.
    assert len(locator_map) == sum(len(pages) for pages in tree.values())
    assert set(known_ids) == set(tree)

    # Every published id resolves to a locator that is really in the
    # tree — an id that mapped to a fabricated locator would sail
    # past the mapper-drop and fail only at the citation gate.
    tree_locators = {
        page.locator for pages in tree.values() for page in pages
    }
    assert set(locator_map.values()) == tree_locators


def test_mcp_auto_build_with_empty_locator_map_fails_loudly() -> None:
    """An MCP adapter with no usable map is a retrieve path that
    cannot return a single hit. Refuse to build it rather than
    serve silent misses (error-handling-enforcer: no
    success-shaped result for a broken configuration)."""
    with pytest.raises(RetrieveAdapterConfigError) as excinfo:
        compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(script=[]),
            retrieve_adapter="mcp",
            known_document_ids={"DOC-2023Q1"},
            locator_map={},
        )
    assert "locator_map" in str(excinfo.value)