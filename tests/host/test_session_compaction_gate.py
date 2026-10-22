"""Tests for ADR 0044 occupancy-based session compaction (plan T1).

Contract under test (docs/adr/0044-session-compaction-token-occupancy.md):

* Occupancy = ``count_tokens_approximately`` of the model-facing
  payload vs a 256_000 estimated-token session budget (one constant,
  operator-overridable per call via the ``session_token_budget``
  context manager).
* At >= 90% occupancy the gate summarizes the OLDEST COMPLETE tool
  pairs (never splitting a pair), writes the summary plus the latest
  status-snapshot slots, and ``RemoveMessage``s the evicted ids so the
  ``add_messages`` reducer actually shrinks the checkpointer.
* Compaction stops near 40% occupancy.
* The gate runs before EVERY LLM call and is NOT nested inside
  ``model_node`` (separate graph node ``compaction_gate``).
* The summarizer is injectable; the graph default is the v1 model.
* Summary failure is typed and must not refuse the operator turn.

Tests override the session budget to 1_600 estimated tokens
(``session_token_budget(1_600)`` = 6_400 chars model-facing) so a
scripted fixture can cross the 90% trigger without a 230k-char
history. All the fractional math below is written out in comments
next to the assertions that depend on it.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from fin.corpus.extracted import ExtractedPage, Locator, load_extracted_tree
from fin.host.session.compaction import _SUMMARY_MARKER, render_summary_message
from fin.host.graph import compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.session.session_compaction import (
    SESSION_TOKEN_BUDGET,
    OccupancyCompactor,
    SessionSummaryError,
    count_tokens_approximately,
    session_token_budget,
    summary_max_chars,
)
from fin.retrieve import FakeRetrievePort

# Bound applied to the session-compaction summary text (mirrors the
# ``fin.host.session.compaction`` bounded-summary pattern).
MAX_SUMMARY_CHARS_BOUND = summary_max_chars()

# Repo root for the corpus fixture (``aapl_page1_text`` is a
# session-scoped fixture in ``test_cli.py`` but fixtures don't leak
# across files; reload it locally so the gate tests don't need
# ``test_cli.py``).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Same fixture ids as tests/host/test_graph.py (that file is not a
# package, so the constants are re-declared here by intent).
AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Test budget: 1_600 estimated tokens == 6_400 model-facing chars.
TEST_BUDGET = 1_600
MODEL_TEST_BUDGET = 10_000


@contextmanager
def _budget(tokens: int = TEST_BUDGET) -> Iterator[None]:
    with session_token_budget(tokens):
        yield


@pytest.fixture(autouse=True)
def _isolated_fin_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FIN_HOME", str(tmp_path / ".fin"))


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    """Real page-1 text from AAPL-2022Q3 — matches ``test_cli``."""
    manifest = _REPO_ROOT / "data" / "corpus" / "manifest.json"
    extracted = _REPO_ROOT / "data" / "corpus" / "extracted"
    tree = load_extracted_tree(manifest, extracted)
    return tree[AAPL_2022Q3][0].text


def _serving_adapter(aapl_page1_text: str) -> FakeRetrievePort:
    """Fake retrieve port whose ``pages_by_doc`` manifest serves the
    AAPL-2022Q3 unit 1 as THIS-RUN ``read_source`` evidence.

    ADR 0046 / 0047: no compile path boots a tree-backed read port —
    the read body comes from the bound adapter's own ``read_slice``.
    These gate tests target compaction semantics, not the citation
    contract, so the scripted research turn carries its own payload
    source (the same pattern as ``test_serve_path_no_tree_ssot``)
    and the scripted answer cites what the run actually read.
    """
    page = ExtractedPage(
        locator=Locator(
            document_id=AAPL_2022Q3,
            version=AAPL_2022Q3_VERSION,
            unit=1,
        ),
        text=aapl_page1_text,
        passages=(),
    )
    return FakeRetrievePort(pages_by_doc={AAPL_2022Q3: [page]})


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ----- scripted-model summarizer -----


class StubSummarizer:
    """Deterministic injectable summarizer recording its prompts."""

    def __init__(self, text: str = "Summary of the evicted research history.") -> None:
        self.text = text
        self.prompts: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.prompts.append(list(messages))
        return AIMessage(content=self.text)


class ExplodingSummarizer:
    """Summarizer that always fails, like a misconfigured v1 model."""

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("model backend unavailable")


# ----- seeded-history builders -----


_TOOL_PAYLOAD = "p" * 12_000  # 3_000 estimated tokens per big ToolMessage


def _tool_pair(
    ai_text: str, tool_body: str, call_id: str
) -> list[BaseMessage]:
    return [
        AIMessage(
            content=ai_text,
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        }
                    },
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=tool_body, tool_call_id=call_id),
    ]


def _seed_messages() -> list[BaseMessage]:
    """A fat research history whose occupancy is far above 90%.

    With the 1_600-token override: pairs 1-3 each carry a 12_000-char
    ToolMessage (3_000 estimated tokens under the chars/4 estimator),
    pair 4 is small, the final AIMessage is 240 chars and the seeded
    question is 25 chars. The raw history is far above the 90% trigger.
    Graph tests use a larger budget so its real prompt and tool-schema
    overhead still leaves a feasible 40% floor for the newest complete
    unit.
    """
    messages: list[BaseMessage] = []
    for index in range(3):
        messages += _tool_pair(
            f"calling read_source {index}", _TOOL_PAYLOAD, f"c-seed-{index}"
        )
    messages += _tool_pair(
        "calling read_source 3",
        json.dumps({"source_ref": {"document_id": AAPL_2022Q3, "unit": 1}}),
        "c-seed-3",
    )
    messages.append(AIMessage(content="x" * 240))
    messages.append(HumanMessage(content="Break down AAPL 2023 Q3 revenue"))
    return messages


def _seed_checkpoint(
    g: Any,
    thread_id: str,
    messages: list[BaseMessage],
    *,
    status_snapshots: list[dict[str, Any]] | None = None,
    track: str | None = None,
) -> None:
    """Write a checkpoint directly so a test starts mid-thread.

    ``as_node="model"`` keeps the compiled graph's pending-node view
    pointing at the gate chain, exactly like a run that stopped right
    before an LLM call would.
    """
    state: dict[str, Any] = {"messages": messages}
    if status_snapshots is not None:
        state["status_snapshots"] = status_snapshots
    if track is not None:
        state["track"] = track
    g._graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        state,
        as_node="model",
    )


def _snapshot_messages(g: Any, thread_id: str) -> list[BaseMessage]:
    values = g._graph.get_state(_thread_config(thread_id)).values or {}
    return list(values.get("messages") or [])


def _checkpoint_ids(messages: list[BaseMessage]) -> list[str | None]:
    return [getattr(m, "id", None) for m in messages]


def _research_graph(
    thread_script: list[AIMessage],
    *,
    summarizer: Any = None,
    status_snapshots: list[dict[str, Any]] | None = None,
) -> Any:
    """Compile a research-capable graph with an explicit summarizer seam."""
    model = ScriptedChatModel(script=thread_script)
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )


# ===== module surface =====


def test_default_session_budget_is_256_000_tokens() -> None:
    """ADR 0044: the session budget is ONE constant, 256_000 tokens."""
    assert SESSION_TOKEN_BUDGET == 256_000


def test_budget_reader_tracks_module_constant_mutation() -> None:
    """Spec-M1: with NO override active, ``session_budget_tokens()``
    falls back to the LIVE module attribute — an operator editing
    ``SESSION_TOKEN_BUDGET`` takes effect without touching the
    ``ContextVar`` (single source of truth = the constant)."""
    import fin.host.session.session_compaction as sc

    original = sc.SESSION_TOKEN_BUDGET
    try:
        sc.SESSION_TOKEN_BUDGET = 999_000
        assert sc.session_budget_tokens() == 999_000
        # And the computed occupancy moves with the constant.
        compactor = sc.OccupancyCompactor()
        tokens = 40_000
        messages = [HumanMessage(content="x" * (tokens * sc.CHARS_PER_TOKEN))]
        ratio = compactor.occupancy(messages)
        assert ratio == pytest.approx(tokens / 999_000)
    finally:
        sc.SESSION_TOKEN_BUDGET = original
    assert sc.session_budget_tokens() == 256_000


def test_budget_override_wins_over_module_constant() -> None:
    """Spec-M1: an active ``session_token_budget`` override still
    wins over a mutated module constant (the contextmanager stays
    the explicit override seam)."""
    import fin.host.session.session_compaction as sc

    original = sc.SESSION_TOKEN_BUDGET
    try:
        sc.SESSION_TOKEN_BUDGET = 999_000
        with session_token_budget(1_600):
            assert sc.session_budget_tokens() == 1_600
    finally:
        sc.SESSION_TOKEN_BUDGET = original


def test_budget_override_is_honored_per_call() -> None:
    """The operator-facing override changes the budget for the
    scoped calls and restores the default afterwards."""
    from fin.host.session.session_compaction import session_budget_tokens

    assert count_tokens_approximately([]) is not None  # import sanity
    with _budget():
        assert SESSION_TOKEN_BUDGET == 256_000  # constant untouched
        # The override must be observable through the budget reader
        # the compactor uses, not by mutating the constant.
        assert session_budget_tokens() == TEST_BUDGET
    assert session_budget_tokens() == SESSION_TOKEN_BUDGET


def test_count_tokens_approximately_is_a_chars_over_four_heuristic() -> None:
    """The estimator is the documented chars/4 heuristic summed over
    each message's text content, and counts the payload it is given
    (not the call-scoped prompt)."""
    assert count_tokens_approximately([]) == 0
    one = [HumanMessage(content="a" * 400)]
    assert count_tokens_approximately(one) == 100
    two = one + [AIMessage(content="b" * 400)]
    assert count_tokens_approximately(two) == 200


# ===== occupancy compactor (pure unit level) =====


def test_no_compaction_below_trigger() -> None:
    """A history far below 90% occupancy produces NO update — the
    gate must be a no-op under budget."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    with _budget():
        update = compactor.compact_update(
            [HumanMessage(content="hello"), AIMessage(content="hi")]
        )
    assert update == []


def test_compact_update_returns_remove_rebuild_plus_summary() -> None:
    """At >= 90% the update list starts with the ``RemoveMessage``
    rebuild sentinel so the ``add_messages`` reducer shrinks the
    checkpointer, followed by the summary SystemMessage and the kept
    tail."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    messages = _seed_messages()
    with _budget():
        update = compactor.compact_update(messages)
    assert update, "occupancy far above 90% must compact"
    assert isinstance(update[0], RemoveMessage)
    assert update[0].id == REMOVE_ALL_MESSAGES
    summary = update[1]
    assert isinstance(summary, SystemMessage)
    assert summary.additional_kwargs.get("fin_compaction_summary") is True
    # The summary carries the fin_compaction marker so
    # messages_for_model does not stack a second copy.
    assert _SUMMARY_MARKER in summary.content


def test_compact_update_stops_near_forty_percent() -> None:
    """After compaction the model-facing occupancy is at or below
    the 40% floor (with the host prompt as overhead)."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    messages = _seed_messages()
    from fin.host.agent_instructions import HOST_SYSTEM_PROMPT

    overhead = count_tokens_approximately(
        [SystemMessage(content=HOST_SYSTEM_PROMPT)]
    )
    # The real prompt is graph overhead, so the budget is the one
    # ``_seed_messages`` reserves for it: a 1_600-token budget leaves
    # 640 floor tokens against a prompt that alone is far larger, and
    # no compaction could ever satisfy the floor.
    with _budget(MODEL_TEST_BUDGET):
        update = compactor.compact_update(messages, overhead_tokens=overhead)
    kept = [m for m in update if not isinstance(m, RemoveMessage)]
    with _budget(MODEL_TEST_BUDGET):
        occupancy = compactor.occupancy(kept, overhead_tokens=overhead)
    assert occupancy <= 0.40 + 1e-9, (
        f"post-compaction occupancy {occupancy:.3f} must be <= 0.40"
    )


def test_call_scoped_overhead_can_trigger_compaction() -> None:
    """Call-scoped tokens can cross 90% while messages alone stay below it."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    messages = [HumanMessage(content="x" * 400) for _ in range(80)]
    with _budget(MODEL_TEST_BUDGET):
        assert compactor.occupancy(messages) < 0.90
        update = compactor.compact_update(messages, overhead_tokens=2_000)
    assert update, "message payload plus call overhead must cross 90%"


@pytest.mark.parametrize("overhead", [-1, True, 1.5, "1"])
def test_compaction_rejects_invalid_overhead_before_summary(overhead: Any) -> None:
    summarizer = StubSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    with _budget(MODEL_TEST_BUDGET):
        with pytest.raises(ValueError, match="overhead_tokens"):
            compactor.compact_update(_seed_messages(), overhead_tokens=overhead)
        with pytest.raises(ValueError, match="overhead_tokens"):
            compactor.occupancy(_seed_messages(), overhead_tokens=overhead)
    assert summarizer.prompts == []


def test_1600_budget_names_unreachable_model_facing_floor() -> None:
    """The actual grounded prompt and schemas cannot fit the 640-token floor."""
    summarizer = StubSummarizer()
    model = ScriptedChatModel(script=[AIMessage(content="")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )
    messages = _seed_messages()
    overhead = graph._compaction_overhead_tokens(
        {"messages": messages, "track": "grounded"}
    )
    assert overhead > TEST_BUDGET * 0.40
    with _budget():
        with pytest.raises(SessionSummaryError, match="40% floor"):
            graph._compactor.compact_update(
                messages,
                overhead_tokens=overhead,
            )
    assert summarizer.prompts == []


def test_summary_wrapper_and_overhead_fit_with_visible_summary_truncation() -> None:
    """The summary body is visibly shortened when the wrapper and host
    overhead would otherwise push the model-facing result above 40%."""
    compactor = OccupancyCompactor(
        summarizer=StubSummarizer(text="important context " * 1_000)
    )
    messages = _seed_messages()
    from fin.host.agent_instructions import HOST_SYSTEM_PROMPT

    overhead = count_tokens_approximately(
        [SystemMessage(content=HOST_SYSTEM_PROMPT)]
    ) + 3_000
    with _budget(MODEL_TEST_BUDGET):
        update = compactor.compact_update(
            messages,
            overhead_tokens=overhead,
            status_snapshots=[{"company": "ZEUS", "period": "FY2026"}],
        )
    kept = [m for m in update if not isinstance(m, RemoveMessage)]
    summary = kept[0]
    assert isinstance(summary, SystemMessage)
    assert summary.additional_kwargs["fin_compaction_summary_truncated"] is True
    assert summary.additional_kwargs["fin_compaction_summary_text"]
    with _budget(MODEL_TEST_BUDGET):
        occupancy = compactor.occupancy(kept, overhead_tokens=overhead)
    assert occupancy <= 0.40


def test_too_small_summary_body_fails_before_calling_summarizer() -> None:
    """A wrapper that leaves less than two summary chars is named early."""
    snapshot = None
    wrapper_tokens = 0
    for suffix_length in range(4):
        candidate = {
            "company": "ZEUS",
            "period": "x" * suffix_length,
            "document_ids": [],
        }
        wrapper = render_summary_message("x", candidate)
        fixed_chars = len(str(wrapper.content)) - 1
        tokens = count_tokens_approximately(
            [render_summary_message("x", candidate)]
        )
        if 4 * tokens + 3 - fixed_chars < 2:
            snapshot = candidate
            wrapper_tokens = tokens
            break
    assert snapshot is not None, "test precondition: find tight wrapper metadata"

    summarizer = StubSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    messages = [
        HumanMessage(content="older context " * 20),
        HumanMessage(content="newest!"),
    ]
    floor_tokens = int(TEST_BUDGET * 0.40)
    overhead = floor_tokens - wrapper_tokens - 1
    with _budget():
        with pytest.raises(SessionSummaryError, match="summary body"):
            compactor.compact_update(
                messages,
                status_snapshots=[snapshot],
                force=True,
                overhead_tokens=overhead,
            )
    assert summarizer.prompts == []


def test_python_large_overhead_is_named_as_unreachable_not_overflow() -> None:
    summarizer = StubSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    with _budget(MODEL_TEST_BUDGET):
        assert compactor.occupancy(
            _seed_messages(), overhead_tokens=10**1000
        ) == 1.0
        with pytest.raises(SessionSummaryError, match="40% floor"):
            compactor.compact_update(_seed_messages(), overhead_tokens=10**1000)
    assert summarizer.prompts == []


def test_eviction_never_splits_a_tool_pair() -> None:
    """Evicted ids always cover WHOLE pairs: for every seeded pair,
    either both ids are removed or neither is."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    messages = _seed_messages()
    # Map each AIMessage's tool-call id to the paired ToolMessage id.
    # ToolMessage ids are NOT auto-derived from ``tool_call_id`` by
    # langchain_core — they are uuids — so we look them up by
    # ``tool_call_id`` rather than guessing the id shape.
    tool_by_call: dict[str, ToolMessage] = {
        m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)
    }
    call_ids: list[str] = [
        call["id"]
        for message in messages
        if isinstance(message, AIMessage) and message.tool_calls
        for call in message.tool_calls
    ]
    with _budget():
        update = compactor.compact_update(messages)
    removed = {
        m.id for m in update if isinstance(m, RemoveMessage)
    }
    assert removed, "seeded history must trigger compaction"
    for call_id in call_ids:
        tool_message = tool_by_call.get(call_id)
        assert tool_message is not None
        ai_in_removed = call_id in removed
        tool_in_removed = getattr(tool_message, "id", None) in removed
        # Whole-pair invariant: either both removed or neither.
        assert ai_in_removed == tool_in_removed, (
            f"split pair for {call_id}: ai={ai_in_removed} tool={tool_in_removed}"
        )


def test_summary_failure_is_typed_not_silent() -> None:
    """A failing summarizer raises the typed
    ``SessionSummaryError`` — no empty catch, no silent no-op at the
    seam level (the graph layer decides to continue un-compacted)."""
    compactor = OccupancyCompactor(summarizer=ExplodingSummarizer())
    with _budget():
        with pytest.raises(SessionSummaryError):
            compactor.compact_update(_seed_messages())


def test_trigger_measurement_counts_existing_summary_weight() -> None:
    """Spec-M2a: the trigger measurement must see the tokens of the
    existing compaction summary SystemMessage — the model actually
    faces it, so a thread whose history is mostly one huge summary
    must trigger compaction (the old behavior stripped the summary
    before measuring and never compacted)."""
    compactor = OccupancyCompactor(summarizer=StubSummarizer())
    # A huge summary (~ 23_000 estimated tokens) + a tiny live tail:
    # without counting the summary the tail is far below the trigger.
    summary_text = "s" * 92_000
    messages: list[BaseMessage] = [
        render_summary_message(summary_text, {}),
        HumanMessage(content="hello"),
        AIMessage(content="hi"),
    ]
    with _budget():
        update = compactor.compact_update(messages)
    assert update, (
        "a huge existing summary must be measured as model-facing "
        "weight and trigger compaction"
    )


def test_summary_output_is_bounded() -> None:
    """Spec-M2b: repeated compactions cannot grow the summary
    unboundedly — the produced summary text is capped (same
    ``_bounded_text`` pattern as ``fin.host.session.compaction``)."""
    compactor = OccupancyCompactor(
        summarizer=StubSummarizer(text="y" * 50_000)
    )
    with _budget():
        update = compactor.compact_update(_seed_messages())
    assert update, "seeded history must trigger compaction"
    summary = update[1]
    assert isinstance(summary, SystemMessage)
    # The rendered message = marker line + summary + slots; the
    # summary BODY must not exceed the session-compaction bound.
    body = summary.additional_kwargs["fin_compaction_summary_text"]
    assert len(body) <= MAX_SUMMARY_CHARS_BOUND
    assert body.endswith("…")


# ===== a stopped run does not wait on the summary =====


class BlockingSummarizer:
    """A summarizer that never returns until released.

    Stands in for the real blocking model call the compaction gate makes
    on the run's own thread: nothing observes it, so a stop can only be
    honoured by this module rather than by the graph.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.entered.set()
        self.release.wait(timeout=10)
        return AIMessage(content="summary that arrives too late")


def test_a_stopped_run_abandons_the_summary_instead_of_waiting_for_it() -> None:
    """The blocking summary must not outlive the stop that asked it to end.

    Compaction runs on the run's own thread inside ``graph.stream``, so a
    stop that cannot abandon this call cannot end the turn at all: the
    operator's Stop, a lost client and a server shutdown would all wait
    the summarizer out.
    """
    from fin.host.run_call_supervisor import run_call_context
    from fin.host.run_control import StopToken

    summarizer = BlockingSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    token = StopToken(thread_id="t-1", run_id="run-1")

    finished: list[str] = []

    def _compact() -> None:
        try:
            with _budget():
                with run_call_context(token, None):
                    compactor.compact_update(_seed_messages())
        except SessionSummaryError:
            finished.append("typed")
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            finished.append(type(exc).__name__)

    worker = threading.Thread(target=_compact)
    worker.start()
    try:
        assert summarizer.entered.wait(timeout=5)
        token.stop(reason="client_disconnect")
        worker.join(timeout=5)
        assert finished == ["typed"], finished
        assert not worker.is_alive()
    finally:
        summarizer.release.set()
        worker.join(timeout=5)


def test_an_unstopped_summary_is_waited_for_normally() -> None:
    """A run nobody stopped still gets its summary: the abandonment is
    bounded by the stop token, not a timeout that fires on its own."""
    summarizer = BlockingSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    finished: list[str] = []

    def _compact() -> None:
        try:
            with _budget():
                update = compactor.compact_update(_seed_messages())
        finally:
            summarizer.release.set()
        finished.append("compacted" if update else "noop")

    worker = threading.Thread(target=_compact)
    worker.start()
    assert summarizer.entered.wait(timeout=5)
    assert not finished, "the summary was not waited for"
    summarizer.release.set()
    worker.join(timeout=5)

    assert finished == ["compacted"]


def test_a_summary_that_raises_a_cancellation_before_the_call_is_not_invoked() -> None:
    """A run already stopped must not spend a model call it cannot use."""
    from fin.host.run_call_supervisor import run_call_context
    from fin.host.run_control import StopToken

    summarizer = StubSummarizer()
    compactor = OccupancyCompactor(summarizer=summarizer)
    token = StopToken(thread_id="t-2", run_id="run-2")
    token.stop(reason="user_stop")

    with _budget():
        with pytest.raises(SessionSummaryError):
            with run_call_context(token, None):
                compactor.compact_update(_seed_messages())

    assert summarizer.prompts == []


# ===== graph gate integration =====


def test_below_threshold_run_does_not_compact() -> None:
    """A short thread under 90% occupancy: the gate is a no-op and
    the checkpoint holds no compaction summary."""
    model = ScriptedChatModel(script=[AIMessage(content="")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=StubSummarizer(),
    )
    with _budget(MODEL_TEST_BUDGET):
        g.invoke(
            {
                "messages": [HumanMessage("hello there")],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-gate-below"),
        )
    messages = _snapshot_messages(g, "t-gate-below")
    assert not any(
        isinstance(m, SystemMessage)
        and m.additional_kwargs.get("fin_compaction_summary") is True
        for m in messages
    )


# ----- the grounded prompt is model-facing OVERHEAD -----
#
# ADR 0044 stops the automatic gate at 40% occupancy, and the overhead
# the gate counts for the next model call is the grounded prompt PLUS
# the bound tool schemas (plus the 档 copy the turn's mode adds). Every
# token of prompt text therefore shrinks the room the 40% floor has
# left for history — a longer prompt is a smaller history budget.
#
# The 2026-10-20 enterprise-neutral rewrite of the track prompts grew
# the grounded prompt by ~80 estimated tokens and pushed the
# end-to-end gate to 0.4006 occupancy (over the 0.40 floor). The
# wording budget below is the EARLY WARNING for that failure class:
# the end-to-end assertion above is the contract, this is the cheap
# signal that names the cause.
GROUNDED_PROMPT_TOKEN_BOUND = 780
"""Estimated-token budget for the grounded system prompt.

Today's wording costs 759 estimated tokens; the 804 of the
pre-correction wording is what broke the 40% floor. The bound leaves
~20 tokens of slack for a wording tweak while still catching a
growth of the size that regressed.
"""


def test_the_grounded_prompt_stays_inside_its_overhead_budget() -> None:
    """The grounded prompt may not grow past the room the compaction
    floor leaves for it.

    The prompt is counted as model-facing overhead, so a longer prompt
    is a smaller history budget: past the bound the automatic gate
    cannot land under the 40% floor (the end-to-end check in
    ``test_graph_gate_counts_tool_schemas_and_meets_total_floor``).
    """
    from fin.host.agent_instructions import HOST_SYSTEM_PROMPT

    prompt_tokens = count_tokens_approximately(
        [SystemMessage(content=HOST_SYSTEM_PROMPT)]
    )
    assert prompt_tokens <= GROUNDED_PROMPT_TOKEN_BOUND, (
        f"the grounded prompt costs {prompt_tokens} estimated tokens, over "
        f"the {GROUNDED_PROMPT_TOKEN_BOUND}-token budget: it is counted as "
        "model-facing overhead, so a longer prompt pushes the automatic "
        "compaction gate past the 40% floor"
    )


@pytest.mark.soft_track
def test_the_grounded_ship_overhead_leaves_room_under_the_floor() -> None:
    """Soft-Track ship gate: the model-facing overhead of a grounded turn
    leaves room under ADR 0044's 40% floor for the history the gate
    keeps.

    The overhead is the grounded prompt plus the bound tool schemas; the
    turn's 档 copy rides on top of it at model-call time. The floor is a
    product budget: when the overhead fills it, the automatic gate can
    no longer land under 40% and the thread stays over budget — the
    failure the end-to-end gate check in this file exists to catch.
    """
    from fin.host.agent_instructions import SEARCH_MODE_USAGE_COPY

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="")]),
        known_document_ids=KNOWN_IDS,
        summarizer=StubSummarizer(),
    )
    overhead = graph._compaction_overhead_tokens(
        {"messages": _seed_messages(), "track": "grounded"}
    )
    mode_copy_tokens = max(
        count_tokens_approximately([SystemMessage(content=copy)])
        for copy in SEARCH_MODE_USAGE_COPY.values()
    )
    # The history the gate must be able to keep: the summary wrapper plus
    # the newest complete unit of the seeded history.
    history_tokens = count_tokens_approximately(
        [render_summary_message("x", {}), _seed_messages()[-1]]
    )
    floor_tokens = MODEL_TEST_BUDGET * 0.40
    assert (
        overhead + mode_copy_tokens + history_tokens <= floor_tokens
    ), (
        f"grounded overhead {overhead} + 档 copy {mode_copy_tokens} + "
        f"history {history_tokens} does not fit the {floor_tokens}-token "
        "floor: the automatic gate could not land under 40% occupancy"
    )


def test_graph_gate_counts_tool_schemas_and_meets_total_floor() -> None:
    """The automatic gate includes the grounded prompt and bound schemas."""
    summarizer = StubSummarizer()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="")]),
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )
    messages = _seed_messages()
    thread_id = "t-gate-full-overhead"
    _seed_checkpoint(graph, thread_id, messages, track="grounded")
    state = {"messages": messages, "track": "grounded"}
    overhead = graph._compaction_overhead_tokens(state)
    assert overhead > 1_000, "grounded schemas and prompt must be counted"
    with _budget(MODEL_TEST_BUDGET):
        assert graph._compactor.occupancy(
            messages,
            overhead_tokens=overhead,
        ) >= 0.90
        graph.invoke(
            {
                "messages": [],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config(thread_id),
        )
        checkpoint = _snapshot_messages(graph, thread_id)
        assert isinstance(checkpoint[0], SystemMessage)
        assert checkpoint[0].additional_kwargs.get("fin_compaction_summary") is True
        occupancy = graph.occupancy(thread_id)
    assert occupancy is not None
    assert occupancy["ratio"] <= 0.40
    assert len(summarizer.prompts) == 1


def test_gate_compacts_before_next_llm_call(aapl_page1_text: str) -> None:
    """A thread seeded at >= 90% occupancy is compacted BEFORE the
    first model call of the run: the model's prompt opens with the
    summary, the checkpoint shrank to summary + 4 kept messages +
    this turn's pair, and no pre-summary message id survived."""
    summarizer = StubSummarizer()
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": AAPL_2022Q3,
                                "version": AAPL_2022Q3_VERSION,
                                "unit": 1,
                            }
                        },
                        "id": "c-live",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_serving_adapter(aapl_page1_text),
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )
    seeded = _seed_messages()
    _seed_checkpoint(g, "t-gate-90", seeded, track="grounded")
    initial_overhead = g._compaction_overhead_tokens(
        {"messages": seeded, "track": "grounded"}
    )
    with _budget(MODEL_TEST_BUDGET):
        assert g._compactor.occupancy(
            seeded, overhead_tokens=initial_overhead
        ) >= 0.90
    # The evicted ids are the AIMessage + ToolMessage of the oldest
    # three fat pairs (call ids ``c-seed-0..2``). Pair 4 (c-seed-3)
    # and the trailing AIMessage + HumanMessage are kept and re-
    # emitted in the rebuild, so their ids survive.
    evicted_call_ids = [f"c-seed-{i}" for i in range(3)]
    expected_evicted: set[str] = set()
    for message in seeded:
        if isinstance(message, AIMessage) and message.tool_calls:
            if any(call["id"] in evicted_call_ids for call in message.tool_calls):
                mid = getattr(message, "id", None)
                if mid is not None:
                    expected_evicted.add(mid)
        elif isinstance(message, ToolMessage) and message.tool_call_id in evicted_call_ids:
            mid = getattr(message, "id", None)
            if mid is not None:
                expected_evicted.add(mid)

    with _budget(MODEL_TEST_BUDGET):
        out = g.invoke(
            {
                "messages": [],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-gate-90"),
        )

    assert out["status"] == "done", out.get("status_reason")

    checkpoint = _snapshot_messages(g, "t-gate-90")
    values = g._graph.get_state(_thread_config("t-gate-90")).values or {}
    compacted_history = checkpoint[:-3]
    call_state = {**values, "messages": compacted_history}
    call_overhead = g._compaction_overhead_tokens(call_state)
    with _budget(MODEL_TEST_BUDGET):
        assert g._compactor.occupancy(
            compacted_history,
            overhead_tokens=call_overhead,
        ) <= 0.40
    # 1) The checkpointer actually shrank: summary + 4 kept
    #    (pair 4 = AIMessage + ToolMessage, final AIMessage) + this
    #    turn's HumanMessage is empty (invoke input) + the live pair
    #    (AIMessage with tool_calls + ToolMessage) + the final
    #    answer AIMessage = 9 messages total, versus 9 seeded + 3
    #    appended — far fewer than an uncompacted run.
    assert len(checkpoint) < len(seeded) + 3
    # 2) The summary opens the checkpoint.
    assert isinstance(checkpoint[0], SystemMessage)
    assert checkpoint[0].additional_kwargs.get("fin_compaction_summary") is True
    assert "AAPL" in checkpoint[0].content  # snapshot slot embedded
    # 3) The evicted ids are GONE: ``RemoveMessage(REMOVE_ALL)`` plus
    #    the rebuild pattern strips every pre-summary message from the
    #    checkpointer, including the AIMessage + ToolMessage of the
    #    three oldest fat pairs.
    post_ids = _checkpoint_ids(checkpoint)
    surviving_evicted = [mid for mid in expected_evicted if mid in post_ids]
    assert surviving_evicted == [], (
        f"evicted ids survived: {surviving_evicted}"
    )
    # 4) The history is still a VALID tool-pair history: every
    #    tool_call is answered by a ToolMessage in the checkpoint.
    open_calls: set[str] = set()
    for message in checkpoint:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                open_calls.add(call["id"])
        elif isinstance(message, ToolMessage):
            open_calls.discard(message.tool_call_id)
    assert not open_calls, f"unanswered tool calls after compaction: {open_calls}"
    # 5) The summarizer was actually used exactly once this run.
    assert len(summarizer.prompts) == 1
    # 6) The model's first call saw the summary, not the fat
    #    history: the stub prompt is the evicted payload; assert the
    #    live model call happened (done) and the checkpoint carries
    #    the compacted shape above — the model-facing payload check
    #    is the open_calls + summary-first assertions.


def test_snapshot_slots_survive_for_classify_inherit(
    aapl_page1_text: str,
) -> None:
    """After compaction, classify still sees the latest company /
    period snapshot: a short follow-up inherits the research track
    with the snapshot's company in its signals."""
    summarizer = StubSummarizer()
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": AAPL_2022Q3,
                                "version": AAPL_2022Q3_VERSION,
                                "unit": 1,
                            }
                        },
                        "id": "c-live",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_serving_adapter(aapl_page1_text),
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )
    seeded = _seed_messages()
    _seed_checkpoint(
        g,
        "t-gate-inherit",
        seeded,
        track="grounded",
        status_snapshots=[
            {"company": "MSFT", "period": "2023 Q1", "document_ids": []},
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": [AAPL_2022Q3],
            },
        ],
    )
    with _budget(MODEL_TEST_BUDGET):
        first = g.invoke(
            {
                "messages": [],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-gate-inherit"),
        )
    assert first["status"] == "done"

    checkpoint = _snapshot_messages(g, "t-gate-inherit")
    summaries = [
        m
        for m in checkpoint
        if isinstance(m, SystemMessage)
        and m.additional_kwargs.get("fin_compaction_summary") is True
    ]
    assert len(summaries) == 1
    # The summary embeds the LATEST snapshot (AAPL, not the older MSFT row).
    assert "AAPL" in summaries[0].content
    assert "2023 Q3" in summaries[0].content

    # A short follow-up with no slots must inherit the research
    # track from the snapshot that survived compaction.
    with _budget(MODEL_TEST_BUDGET):
        follow = g.invoke(
            {
                "messages": [HumanMessage("More detail please")],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-gate-inherit"),
        )
    assert follow["track"] == "grounded"
    assert follow["track_decision"]["decision_tier"] == "inherit"
    assert follow["track_decision"]["signals"].get("snapshot_company") == "AAPL"
    # No re-compaction on the follow-up (still under 90%).
    checkpoint = _snapshot_messages(g, "t-gate-inherit")
    summaries = [
        m
        for m in checkpoint
        if isinstance(m, SystemMessage)
        and m.additional_kwargs.get("fin_compaction_summary") is True
    ]
    assert len(summaries) == 1


def test_summary_failure_leaves_turn_uncompacted_and_continues(
    aapl_page1_text: str,
) -> None:
    """A typed summary failure must NOT refuse the turn: the run
    continues un-compacted and the checkpoint keeps every pre-run
    message id (no trim, no delete, no empty catch)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": AAPL_2022Q3,
                                "version": AAPL_2022Q3_VERSION,
                                "unit": 1,
                            }
                        },
                        "id": "c-live",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_serving_adapter(aapl_page1_text),
        known_document_ids=KNOWN_IDS,
        summarizer=ExplodingSummarizer(),
    )
    seeded = _seed_messages()
    _seed_checkpoint(g, "t-gate-fail", seeded, track="grounded")
    pre_ids = _checkpoint_ids(seeded)

    with _budget(MODEL_TEST_BUDGET):
        out = g.invoke(
            {
                "messages": [],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config=_thread_config("t-gate-fail"),
        )

    # The turn completed normally — summary failure must not refuse.
    assert out["status"] == "done", out.get("status_reason")
    checkpoint = _snapshot_messages(g, "t-gate-fail")
    # Checkpoint unchanged apart from this run's own appended
    # messages: every pre-run id survives, no summary was written.
    post_ids = _checkpoint_ids(checkpoint)
    for mid in pre_ids:
        assert mid in post_ids
    assert not any(
        isinstance(m, SystemMessage)
        and m.additional_kwargs.get("fin_compaction_summary") is True
        for m in checkpoint
    )


def test_gate_is_a_standalone_node_not_nested_in_model_node() -> None:
    """ACR: the gate must be a separate graph node wired BEFORE the
    LLM entries — not a call inside ``model_node``."""
    model = ScriptedChatModel(script=[AIMessage(content="")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=StubSummarizer(),
    )
    drawable = g._graph.get_graph()
    nodes = set(drawable.nodes)
    assert "compaction_gate" in nodes
    # The direct LLM-entry edges are replaced: tools / issuer_join
    # now enter through the gate, and classify's research branch
    # does too (no direct edge into "model" from those sources).
    direct_entries = {
        (edge.source, edge.target)
        for edge in drawable.edges
        if edge.target == "model"
    }
    assert ("tools", "model") not in direct_entries
    assert ("issuer_join", "model") not in direct_entries
    assert ("compaction_gate", "model") in direct_entries
