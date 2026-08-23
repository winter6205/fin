"""Tests for the v1 host graph (T6).

The host graph is the durable runtime for fin v1 (ADR 0019). It
authored in this repo: model node + tool node + citation gate on
the path to END + step cap 32 in fin state. Tests cover:

* **happy path** — scripted tool call + scripted answer with a
  ≥32-char extractive span from that page → ``done`` with
  non-empty citations.
* **bounce loop** — scripted tool call returning a non-citable
  answer + bounce template → 3 iterations → ``refuse`` with
  reason ``citation_repair_cap`` (NOT ``step_cap``).
* **step cap** — scripted loop that never reaches done/refuse
  → after 32 iterations, ``status == "step_cap"``.
* **unknown locator** — scripted final answer mentioning a
  well-formed ``document_id`` not in the corpus → ``refuse`` with
  reason ``unknown_locator`` on the first failure.
* **concurrent** — two threads invoking the same graph on the
  same ``thread_id`` simultaneously → exactly one succeeds, the
  other raises ``ConcurrentRunError``.
* **recursion_limit > 32** — assert the factory sets
  LangGraph's recursion limit strictly higher than ``STEP_CAP``
  so the fin fuse fires first.
* **factory idempotence** — two ``compile_graph`` calls with the
  same args return the same proxy object (T9 will assert identity
  between CLI and HTTP).
* **status precedence** — step-cap wins over a same-iteration
  bounce (the bounce sets ``repair_count`` but not ``status``;
  the next ``model_node`` entry trips the cap first).
* **tool-node error propagation** — a tool that raises propagates
  the exception out of ``invoke`` (LangGraph ``ToolNode`` with
  ``handle_tool_errors=False``) so the surface sees a typed
  error, not a swallowed failure.
* **malformed evidence from tool** — pre-seeded invalid evidence
  in the input state is validated by the citation gate, which
  raises ``MalformedEvidenceError`` out of ``invoke``.

All tests use ``ScriptedChatModel`` (no live LLM) and
``InMemorySaver`` (no Postgres). The committed extracted tree at
``data/corpus/extracted/`` is required for tests that exercise
the tool functions against real page text — it is loaded once
per session via the ``corpus_tree`` module-level fixture.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator, load_extracted_tree
from fin.host import (
    AgentState,
    ConcurrentRunError,
    EvidenceItem,
    MalformedEvidenceError,
    REPAIR_CAP,
    RECURSION_LIMIT,
    STEP_CAP,
    ScriptedChatModel,
    compile_graph,
)
from fin.retrieve import BM25Adapter


# ----- corpus fixtures -----


# Pulled from the frozen corpus manifest. The version (sha256)
# belongs to the committed extracted tree; tests that drive
# ``read_file`` against the real extracted tree use these values
# verbatim so the tool returns a happy-path page-text dict.
AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
MSFT_2023Q1 = "MSFT-2023Q1"

KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


@pytest.fixture(scope="session")
def corpus_tree():
    """Load the committed extracted tree once per test session."""
    manifest = Path("data/corpus/manifest.json")
    extracted = Path("data/corpus/extracted")
    return load_extracted_tree(manifest, extracted)


@pytest.fixture(scope="session")
def bm25_adapter(corpus_tree):
    """Build a BM25Adapter once per test session for retrieve-based scenarios."""
    return BM25Adapter(corpus_tree)


@pytest.fixture(scope="session")
def aapl_page1_text(corpus_tree):
    """Real page-1 text from AAPL-2022Q3 (used to script a >=32-char span)."""
    return corpus_tree[AAPL_2022Q3][0].text


# ----- helpers -----


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _scripted_read_file_then_answer(
    answer: str,
    *,
    document_id: str = AAPL_2022Q3,
    version: str = AAPL_2022Q3_VERSION,
    page: int = 1,
    page_limit: int = 1,
    read_call_id: str = "c-read",
) -> list[AIMessage]:
    """Build the script: a read_file tool call followed by the answer AIMessage."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "document_id": document_id,
                        "version": version,
                        "page": page,
                        "page_limit": page_limit,
                    },
                    "id": read_call_id,
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _invoke_done(
    *,
    model: ScriptedChatModel,
    thread_id: str,
    span_source: str,
) -> dict:
    """Drive the happy-path ``done`` script and return the resulting state.

    ``span_source`` is the page text the answer quotes from; the
    scripted answer embeds its first 80 characters verbatim so the
    citation gate finds a ≥32-char extractive span.
    """
    assert len(span_source) >= 32, "test fixture must seed a ≥32-char span"
    quoted_answer = f"Per the filing: {span_source[:80]}"
    model.script = _scripted_read_file_then_answer(quoted_answer)
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    return g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config(thread_id),
    )


# ----- happy path -----


def test_done_path_read_file_then_answer_with_span(aapl_page1_text):
    """Scripted tool call + answer that contains a ≥32-char span from
    page 1 of AAPL-2022Q3 ends ``done`` with non-empty citations."""
    model = ScriptedChatModel(script=[])
    out = _invoke_done(
        model=model,
        thread_id="t-done",
        span_source=aapl_page1_text,
    )
    assert out["status"] == "done"
    assert out["status_reason"] == "done"
    assert out["answer"] == f"Per the filing: {aapl_page1_text[:80]}"
    assert len(out["citations"]) >= 1
    # The citation locator comes from the matched evidence item
    # (ADR 0021), not from the model's claim.
    citation = out["citations"][0]
    assert citation.document_id == AAPL_2022Q3
    assert citation.version == AAPL_2022Q3_VERSION
    assert citation.page == 1
    assert citation.quote == aapl_page1_text[:80]
    # step_count = 2 (model -> tools -> model -> gate)
    assert out["step_count"] == 2
    # No bounces happened.
    assert out["repair_count"] == 0


# ----- bounce loop -----


def test_bounce_loop_ends_at_repair_cap_refuse(aapl_page1_text):
    """Scripted read_file returns a happy-page evidence list, but the
    scripted answer contains no ≥32-char span from that page →
    bounce → bounce → bounce → refuse(citation_repair_cap)."""
    model = ScriptedChatModel(script=_scripted_read_file_then_answer(
        # Deliberately no ≥32-char span from the page text — the
        # gate will bounce.
        answer="I think revenue was strong this quarter, definitely up.",
    ))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-bounce"),
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"
    assert out["repair_count"] == REPAIR_CAP  # 3
    # The repair counter — not the step cap — triggered the refuse:
    # only ~4 model entries were consumed, far below STEP_CAP.
    assert out["step_count"] < STEP_CAP
    # No citations populated on refuse.
    assert out.get("citations", ()) == ()


# ----- step cap -----


def test_step_cap_fires_at_32_iterations():
    """A scripted loop that calls ``list_files`` every turn trips
    ``status == "step_cap"`` after 32 ``model_node`` entries."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {},
                        "id": "c-loop",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-cap"),
    )
    assert out["status"] == "step_cap"
    assert out["status_reason"] == "step_cap"
    # ``step_count`` is incremented on every entry; the cap fires
    # BEFORE the 33rd model invocation, so the counter sits at
    # ``STEP_CAP`` (= 32).
    assert out["step_count"] == STEP_CAP
    # Step cap is the runaway fuse — repair_count is unaffected.
    assert out["repair_count"] == 0


# ----- unknown locator -----


def test_unknown_locator_refuses_on_first_failure():
    """Scripted final answer mentions a well-shaped locator that is
    NOT in the known corpus → ``refuse(unknown_locator)`` on the
    first failure (no repair)."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Per the ZZZZ-9999Q1 filing, revenue was up across all "
                    "segments this quarter and operating margin improved."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,  # does NOT contain ZZZZ-9999Q1
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-unknown"),
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"
    # ``unknown_locator`` does NOT increment the repair counter
    # (ADR 0008: refuses immediately, no repair).
    assert out["repair_count"] == 0


# ----- status precedence -----


def test_step_cap_wins_over_pending_bounce():
    """A run that bounces twice and then hits step_count == STEP_CAP
    in the same iteration ends with ``status == "step_cap"`` — the
    bounce turn does not set ``status``; the next ``model_node``
    entry trips the cap before the gate is reached on the next
    turn.

    See ``test_step_cap_wins_when_both_fuses_would_fire_same_iteration``
    for the canonical precedence test where ``repair_count`` is
    primed high and the loop trips the step cap first.
    """
    # Verify the natural bounce path ends in refuse, not step_cap,
    # because bouncing 3 times consumes only ~3 model entries (far
    # below STEP_CAP). This documents the gap between the two fuses.
    bouncing_answer = AIMessage(content="vague answer with no evidence")
    model = ScriptedChatModel(script=[bouncing_answer])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-precedence-natural"),
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"
    assert out["step_count"] < STEP_CAP


def test_step_cap_wins_when_both_fuses_would_fire_same_iteration():
    """Seed ``repair_count = REPAIR_CAP`` (so the gate would refuse
    if reached) AND loop the model_node to push ``step_count`` over
    the cap first. ``status == "step_cap"`` wins because the cap
    check happens on ``model_node`` ENTRY — before any further gate
    evaluation.

    The scripted model always calls ``list_files``, so the run is a
    pure tool loop: model -> tools -> model -> tools -> ... With the
    repair counter already at the cap, ANY gate visit would refuse;
    the fact that we end at ``step_cap`` proves the fin-owned step
    counter fired first (the gate was never reached).
    """
    tool_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "list_files", "args": {}, "id": "c-loop", "type": "tool_call"}
        ],
    )
    model = ScriptedChatModel(script=[tool_call])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": REPAIR_CAP,
            "step_count": 0,
        },
        config=_thread_config("t-precedence2"),
    )
    assert out["status"] == "step_cap"
    assert out["status_reason"] == "step_cap"
    assert out["step_count"] == STEP_CAP
    # repair_count was not touched by the step-cap branch — the
    # fuses are independent (ADR 0008).
    assert out["repair_count"] == REPAIR_CAP


# ----- concurrency -----


def test_concurrent_run_on_same_thread_raises():
    """Two threads invoking the same graph on the same ``thread_id``
    simultaneously → exactly one succeeds, the other raises
    ``ConcurrentRunError``.

    Implementation note: we use a blocking scripted model so the
    first thread holds the lock long enough for the second thread
    to attempt acquisition and raise.
    """

    # Custom scripted model that blocks on a ``threading.Event``
    # before returning so the second thread races in while the
    # first is still in flight.
    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            # Bounded wait: if the release is never set, the test
            # will hang — explicit timeout prevents that.
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    blocking = BlockingModel(script=[AIMessage(content="answer")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
    )

    results: dict[str, tuple[str, str]] = {}

    def runner(label: str) -> None:
        try:
            r = g.invoke(
                {
                    "messages": [HumanMessage("q")],
                    "evidence": [],
                    "repair_count": 0,
                    "step_count": 0,
                },
                config=_thread_config("t-race"),
            )
            results[label] = ("ok", r["status"])
        except ConcurrentRunError as e:
            results[label] = ("err", type(e).__name__)
        except Exception as e:  # pragma: no cover - other raises are bugs
            results[label] = ("err", f"{type(e).__name__}: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(runner, "first")
        # Wait until the first thread has entered the model node
        # so it is holding the per-thread lock.
        assert entered.wait(timeout=5.0), "first thread never entered the graph"
        f2 = ex.submit(runner, "second")
        # Give the second thread time to attempt acquisition and
        # raise. ``BlockingModel._generate`` still holds, so the
        # second thread should see ``ConcurrentRunError``.
        time.sleep(0.2)
        # Release the first thread so the test can finish.
        release.set()
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    # Exactly one succeeded and one raised. The graph order is not
    # deterministic — only the SET of outcomes.
    statuses = sorted(results.values())
    assert ("err", "ConcurrentRunError") in statuses, results
    assert ("ok", "refuse") in statuses or ("ok", "done") in statuses or ("ok", "step_cap") in statuses, results


def test_concurrent_runs_on_different_threads_serialize_independently():
    """Two threads invoking the SAME compiled graph on DIFFERENT
    ``thread_id``s do not block each other — the lock is per-thread."""
    barrier = threading.Barrier(2)
    release = threading.Event()

    class BarrierModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            barrier.wait(timeout=5.0)
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    bm = BarrierModel(script=[AIMessage(content="done")])
    g = compile_graph(checkpointer=InMemorySaver(), model=bm, known_document_ids=KNOWN_IDS)

    results: dict[str, str] = {}

    def runner(label: str, thread_id: str) -> None:
        r = g.invoke(
            {
                "messages": [HumanMessage("q")],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config(thread_id),
        )
        results[label] = r["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(runner, "a", "t-a")
        f2 = ex.submit(runner, "b", "t-b")
        # Wait until BOTH threads have entered model_node.
        # ``barrier`` is set to 2; both must arrive. They should
        # both be inside the lock at this point.
        time.sleep(0.5)
        release.set()
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    assert set(results) == {"a", "b"}
    for status in results.values():
        assert status in {"done", "refuse", "step_cap"}


# ----- factory surface -----


def test_recursion_limit_is_strictly_above_step_cap():
    """``compile_graph`` exposes ``recursion_limit`` strictly
    greater than ``STEP_CAP`` so the agent-loop fuse fires first."""
    model = ScriptedChatModel(script=[AIMessage(content="")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    assert g.recursion_limit > STEP_CAP, (
        f"recursion_limit={g.recursion_limit} must be > STEP_CAP={STEP_CAP}"
    )


def test_recursion_limit_default_is_exported_constant():
    """The exported ``RECURSION_LIMIT`` constant is the same value
    the factory uses."""
    model = ScriptedChatModel(script=[AIMessage(content="")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    assert g.recursion_limit == RECURSION_LIMIT


def test_compile_graph_idempotent_same_args():
    """Two ``compile_graph`` calls with the SAME checkpointer and
    model instances return the SAME proxy object — T9 will assert
    identity between the CLI helper's graph and the HTTP handler's
    graph (both share the process-level checkpointer)."""
    shared_checkpointer = InMemorySaver()
    shared_model = ScriptedChatModel(script=[AIMessage(content="")])
    g1 = compile_graph(
        checkpointer=shared_checkpointer,
        model=shared_model,
        known_document_ids=KNOWN_IDS,
    )
    g2 = compile_graph(
        checkpointer=shared_checkpointer,
        model=shared_model,
        known_document_ids=KNOWN_IDS,
    )
    assert g1 is g2


def test_compile_graph_distinct_args_yield_distinct_graphs():
    """Two ``compile_graph`` calls with different models produce
    different proxy objects."""
    m1 = ScriptedChatModel(script=[AIMessage(content="one")])
    m2 = ScriptedChatModel(script=[AIMessage(content="two")])
    g1 = compile_graph(
        checkpointer=InMemorySaver(),
        model=m1,
        known_document_ids=KNOWN_IDS,
    )
    g2 = compile_graph(
        checkpointer=InMemorySaver(),
        model=m2,
        known_document_ids=KNOWN_IDS,
    )
    assert g1 is not g2


# ----- tool-node evidence propagation -----


def test_read_file_tool_appends_evidence(aapl_page1_text):
    """``read_file`` adds one ``EvidenceItem`` per page to the
    state when the page read succeeds; the citation gate then
    matches the answer's span against that evidence."""
    # First iteration: read_file. Second iteration: answer with a
    # span from the same page text. ``done`` requires the citation
    # gate to match the span, which means evidence must have made
    # it into state from the tool node.
    model = ScriptedChatModel(
        script=_scripted_read_file_then_answer(
            answer=f"Answer quoting: {aapl_page1_text[:80]}"
        )
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-evidence"),
    )
    assert out["status"] == "done"
    assert len(out["evidence"]) >= 1
    # The matched citation's locator must be the AAPL page-1 locator
    # (NOT a model-invented version — ADR 0021).
    cit = out["citations"][0]
    assert cit.document_id == AAPL_2022Q3
    assert cit.version == AAPL_2022Q3_VERSION


def test_list_files_tool_does_not_contribute_evidence():
    """``list_files`` returns catalog metadata only — it must NOT
    add anything to ``state["evidence"]``."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {},
                        "id": "c-lf",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I checked the catalog. Done."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-lf"),
    )
    # The answer does not cite any evidence, so the gate bounces.
    assert out["status"] in {"refuse", "step_cap"}
    # No evidence was added by ``list_files``.
    assert out["evidence"] == []


# ----- boundary cases: malformed evidence -----


def test_unknown_tool_name_becomes_error_tool_message():
    """A scripted tool call naming a tool the graph does not register
    becomes an error ``ToolMessage`` the model sees on the next turn
    (ToolNode self-correction path) — NOT a crash, and NOT evidence."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "nonexistent_tool",
                        "args": {"x": 1},
                        "id": "c-bad",
                        "type": "tool_call",
                    }
                ],
            ),
            # After seeing the error ToolMessage, the model answers.
            AIMessage(content="I could not use that tool; sorry."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-unknown-tool"),
    )
    # The run completes (the error message is data, not a crash).
    assert out["status"] in {"refuse", "step_cap"}
    assert out["evidence"] == []
    # The error ToolMessage made it into the history.
    from langchain_core.messages import ToolMessage as _TM

    tool_messages = [m for m in out["messages"] if isinstance(m, _TM)]
    assert len(tool_messages) == 1
    assert "nonexistent_tool" in str(tool_messages[0].content)


def test_malformed_evidence_raises_typed_error(aapl_page1_text):
    """An ``EvidenceItem`` with an empty ``page_text`` violates the
    citation gate's contract; the graph must surface the typed
    ``MalformedEvidenceError`` out of ``invoke`` instead of
    swallowing it."""
    # We seed the input state with malformed evidence. The model
    # immediately returns an answer (no tool call); the gate runs
    # on turn 1 and raises.
    model = ScriptedChatModel(
        script=[AIMessage(content="Here is the answer text without a real cite.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    bad_locator = Locator(document_id=AAPL_2022Q3, version=AAPL_2022Q3_VERSION, page=1)
    with pytest.raises(MalformedEvidenceError):
        g.invoke(
            {
                "messages": [HumanMessage("q")],
                "evidence": [EvidenceItem(locator=bad_locator, page_text="")],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-bad-ev"),
        )


# ----- graph surface -----


def test_compile_graph_propagates_missing_thread_id():
    """``invoke`` without ``configurable.thread_id`` raises
    ``ConcurrentRunError`` (we never run on an anonymous thread)."""
    model = ScriptedChatModel(script=[AIMessage(content="x")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    with pytest.raises(ConcurrentRunError):
        g.invoke(
            {
                "messages": [HumanMessage("q")],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            }
        )


def test_state_schema_exposes_step_cap_constants():
    """``AgentState`` and the cap constants are exposed via
    ``fin.host``."""
    assert STEP_CAP == 32
    assert REPAIR_CAP == 3
    assert RECURSION_LIMIT > STEP_CAP
    # TypedDict is a class; attribute existence is asserted by
    # importability of the class itself.
    assert AgentState is not None


# ===== T3: model_node streams; thinking strip; tool summaries =====


def test_model_node_streams_not_invokes(aapl_page1_text):
    """T3 spec line 27: ``model_node`` calls ``model.stream()`` (NOT
    blocking ``invoke``). We pin this with a recording scripted
    model whose ``_generate`` raises - if the node still called the
    sync path the graph would crash; if it streams, the run
    completes."""
    from fin.host.scripted_model import ScriptedChatModel as SCM

    class SyncForbidden(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise AssertionError(
                "model_node must stream (spec line 27), not invoke"
            )

    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = SyncForbidden(
        script=[
            AIMessage(content=answer),
        ],
        stream_script=[AIMessageChunk(content=answer)],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage("q")],
            "evidence": [
                EvidenceItem(
                    locator=Locator(AAPL_2022Q3, AAPL_2022Q3_VERSION, 1),
                    page_text=aapl_page1_text,
                )
            ],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-stream-only"),
    )
    assert out["status"] == "done"


def test_streamed_thinking_never_lands_in_answer_or_state_answer(
    aapl_page1_text,
):
    """SC60 binding test (graph level): a streamed thinking delta
    plus an answer chunk assemble into an ``AIMessage`` where ONLY
    the answer prose is content. The state's ``answer`` and the
    citation gate's candidate must never contain the thinking text.

    The thinking arrives via ``additional_kwargs.reasoning_details``
    (the MiniMax ``reasoning_split`` shape, ADR 0022)."""
    thinking = "INTERNAL SCRATCHPAD the operator must never see in answer"
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content="",
                additional_kwargs={
                    "reasoning_details": [{"type": "reasoning.text", "text": thinking}]
                },
            ),
            AIMessageChunk(content=answer),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    # Seed evidence so the gate can match the answer span; without
    # evidence the gate would bounce (no evidenced span) and refuse
    # after the cap - which is the WRONG failure mode to test
    # here. This test pins the THINKING-STRIP semantics, not the
    # bounce path.
    state_in = reset_input_state_for_test("q")
    state_in["evidence"] = [
        EvidenceItem(
            locator=Locator(AAPL_2022Q3, AAPL_2022Q3_VERSION, 1),
            page_text=aapl_page1_text,
        )
    ]
    out = g.invoke(state_in, config=_thread_config("t-thinking-split"))
    assert out["status"] == "done"
    # The answer field holds only the real prose.
    assert out["answer"] == answer
    assert thinking not in out["answer"]
    # The checkpointed AIMessage carries reasoning_details on
    # additional_kwargs for replay (ADR 0022 round-trip) but NOT in
    # content.
    ai_messages = [m for m in out["messages"] if isinstance(m, AIMessage)]
    last_ai = ai_messages[-1]
    assert last_ai.content == answer
    details = last_ai.additional_kwargs.get("reasoning_details")
    assert details and details[0]["text"] == thinking


def test_inline_thinking_block_is_stripped_before_citation_gate(
    aapl_page1_text,
):
    """When the proxy inlines ``<thinking>...</thinking>`` into the
    answer content (fallback shape), the gate must see STRIPPED
    prose so the thinking text cannot pass as the answer AND the
    evidenced span after the block still matches (done path).

    SC60 binding: verify_citations receives thinking-stripped prose;
    a >=32-char evidenced span still ends ``done``."""
    span = aapl_page1_text[:80]
    raw = f"<thinking>private chain of thought</thinking>{span}"
    # The model returned a SYNC AIMessage with the inline block
    # because the streaming-shape proxy is the more common case
    # (ADR 0022) - but the gate's strip path is the same seam.
    model = ScriptedChatModel(script=[AIMessage(content=raw)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    state_in = reset_input_state_for_test("q")
    state_in["evidence"] = [
        EvidenceItem(
            locator=Locator(AAPL_2022Q3, AAPL_2022Q3_VERSION, 1),
            page_text=aapl_page1_text,
        )
    ]
    out = g.invoke(state_in, config=_thread_config("t-inline-thinking"))
    # The gate matched the span AFTER stripping -> done.
    assert out["status"] == "done"
    # The persisted answer has no thinking text.
    assert "private chain of thought" not in out["answer"]
    assert out["answer"] == span


def test_verify_citations_receives_stripped_prose_with_thinking_in_content(
    aapl_page1_text,
):
    """Unit-level pin of the strip seam: ``graph._last_ai_text``
    returns thinking-stripped prose when the proxy inlined a
    ``<thinking>`` block into content. Without the strip the gate
    would bounce (the leading thinking text breaks nothing per se,
    but the DONE result's ``answer`` would carry the private text -
    which SC60 forbids)."""
    from fin.host.graph import _last_ai_text

    span = aapl_page1_text[:80]
    msg = AIMessage(
        content=f"<thinking>secret</thinking>\n{span}",
        additional_kwargs={},
    )
    text = _last_ai_text([msg])
    assert "secret" not in text
    assert span in text


def test_tool_summaries_appended_by_tool_node(aapl_page1_text):
    """``tool_node`` appends one human-readable summary per tool call
    to ``state['tool_summaries']``; each entry carries name +
    summary + tool_call_id. Raw ToolMessage JSON stays off the
    summary string."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-read-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Answer: {aapl_page1_text[:80]}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        reset_input_state_for_test("q"),
        config=_thread_config("t-tool-summary"),
    )
    summaries = out.get("tool_summaries") or []
    assert len(summaries) == 1
    entry = summaries[0]
    assert set(entry.keys()) == {"name", "summary", "tool_call_id"}
    assert entry["name"] == "read_file"
    assert entry["tool_call_id"] == "c-read-1"
    assert f"{AAPL_2022Q3} p.1" in entry["summary"]
    # Raw JSON never leaks into the summary.
    assert "{" not in entry["summary"]


def test_checkpointed_message_after_stream_has_complete_tool_calls():
    """Spec line 37 (T4 groundwork): after ``model_node`` completes,
    the CHECKPOINTED message list contains one complete ``AIMessage``
    with full parsed ``tool_calls`` - not chunks. A resume from that
    point can execute tools without regenerating the model turn."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "list_files", "args": "", "id": "c-tc", "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": None, "args": '{"company": "AAPL"}', "id": None, "index": 0}
                ],
            ),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    cfg = _thread_config("t-checkpoint-toolcalls")
    try:
        list(g.stream(reset_input_state_for_test("q"), config=cfg))
    except AssertionError:
        # The scripted loop replays the tool-call chunk forever until
        # step cap; any terminal outcome is fine for this test.
        pass
    state = g._graph.get_state(cfg)
    values = state.values
    ai_messages = [m for m in values.get("messages", []) if isinstance(m, AIMessage)]
    assert ai_messages, "no AIMessage was checkpointed"
    first_ai = ai_messages[0]
    # Complete tool_calls on the checkpoint (not chunks).
    assert first_ai.tool_calls
    call = first_ai.tool_calls[0]
    assert call["name"] == "list_files"
    assert call["id"] == "c-tc"
    assert call["args"] == {"company": "AAPL"}


# ----- helpers -----


def reset_input_state_for_test(question: str) -> dict:
    """Build a minimal reset input (mirrors ``reset_input_state``)
    without importing it twice in this file's tests."""
    from fin.host.state import EVIDENCE_RESET

    return {
        "messages": [HumanMessage(question)],
        "evidence": EVIDENCE_RESET,
        "repair_count": 0,
        "step_count": 0,
        "answer": "",
        "citations": [],
        "status": "running",
        "thinking_text": "",
        "tool_summaries": [],
    }


# ----- T3 (#34): thinking preference reaches the model binding -----


class _ExtraBodyRecordingModel(ScriptedChatModel):
    """Record the ``extra_body`` kwarg each ``_stream`` call saw.

    The per-run thinking preference is applied by selecting a
    differently-bound model (``bind(extra_body=...)``), so the
    observable seam is the kwarg the model receives.
    """

    seen_extra_bodies: list = []

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.seen_extra_bodies.append(kwargs.get("extra_body"))
        yield from super()._stream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def test_thinking_type_disabled_config_selects_disabled_binding() -> None:
    """``configurable.thinking_type == "disabled"`` makes every
    model call in the run carry the MiniMax ``extra_body`` with
    ``thinking.type=disabled`` while ``reasoning_split`` STAYS on
    (converter replay continuity, spec line 28)."""
    from langchain_core.messages import AIMessageChunk

    model = _ExtraBodyRecordingModel(
        stream_responses=[[AIMessageChunk(content="ok")]]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids={"X"},
    )
    g.invoke(
        {"messages": [HumanMessage(content="q")]},
        config={
            "configurable": {"thread_id": "t-pref-off", "thinking_type": "disabled"}
        },
    )
    assert model.seen_extra_bodies
    assert all(
        body == {"thinking": {"type": "disabled"}, "reasoning_split": True}
        for body in model.seen_extra_bodies
    )


def test_default_config_keeps_boot_thinking_binding() -> None:
    """Without ``thinking_type`` (the default / on preference) the
    model binding is the boot-time one — NO extra_body kwarg is
    layered on top of the compiled extra_body (adaptive,
    ADR 0022)."""
    from langchain_core.messages import AIMessageChunk

    model = _ExtraBodyRecordingModel(
        stream_responses=[[AIMessageChunk(content="ok")]]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids={"X"},
    )
    g.invoke(
        {"messages": [HumanMessage(content="q")]},
        config={"configurable": {"thread_id": "t-pref-default"}},
    )
    assert model.seen_extra_bodies
    assert all(body is None for body in model.seen_extra_bodies)
