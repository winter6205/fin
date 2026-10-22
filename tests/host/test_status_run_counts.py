"""The status surface answers from local run state, and only counts.

``/api/status`` is the one surface an operator reads during the incident
it exists to report, so the three lifecycle counts it carries have to be
answerable without the graph, the MCP client or the checkpoint — which is
also why it carries counts and nothing about an individual turn. These
cases pin both halves: the registry's own arithmetic against a clock the
test owns, and the wire during a run that is holding the graph lock.

The existing forbidden-field pins in ``test_workspace_http.py`` stay the
authority on which keys may appear; what is asserted here is that the new
field is three integers and nothing more.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.run_control import RunControl

KNOWN_IDS = {"AAPL-2022Q3"}
COUNT_KEYS = {"active", "paused", "suspected_stuck"}

#: Grounded by the tier rules, so the run reaches the grounded lane. Since
#: ADR 0104 the 检索档 picker is retired: a routing residual is assigned
#: host-side (``graph`` / ``decision_tier="fallback"``) instead of opening
#: a window, so this question no longer pauses anything by itself.
GROUNDED_QUESTION = "What was AAPL net sales in 2023 Q3?"

#: The options the Fast propose window publishes. They are the extract
#: METHODS, never 检索档 tokens: the mode vocabulary left the window
#: entirely with ADR 0104, so a 档 option here would mean the retired
#: picker came back.
PROPOSE_WINDOW_OPTION_IDS = {"fast", "standard"}

#: The executing deadline plus the cleanup grace, which is the window a
#: run is suspected stuck past. Matched here rather than imported so a
#: change to the published budgets has to be a deliberate edit.
STUCK_AFTER_S = 121.0


@pytest.fixture
def clock() -> Iterator[list[float]]:
    """A monotonic clock the test advances by hand."""
    now = [1000.0]

    def read() -> float:
        return now[0]

    read.now = now  # type: ignore[attr-defined]
    yield now


@pytest.fixture
def control(clock: list[float]) -> Iterator[RunControl]:
    yield RunControl(clock=lambda: clock[0])


# ----- the registry's own arithmetic -----

def test_an_idle_process_reports_no_runs(control: RunControl) -> None:
    assert control.counts() == type(control.counts())(0, 0, 0)


def test_a_run_is_active_while_it_is_executing(control: RunControl) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)

    assert control.counts().active == 1
    assert control.counts().paused == 0
    assert control.counts().suspected_stuck == 0


def test_a_run_is_only_suspected_stuck_past_its_deadline_and_grace(
    control: RunControl, clock: list[float]
) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)

    clock[0] += STUCK_AFTER_S
    assert control.counts().suspected_stuck == 0, "the boundary is not 'past' it"

    clock[0] += 0.001
    assert control.counts().suspected_stuck == 1


def test_progress_puts_the_no_progress_clock_back_to_zero(
    control: RunControl, clock: list[float]
) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    clock[0] += STUCK_AFTER_S + 10.0

    control.progress("t1")

    assert control.counts().suspected_stuck == 0


def test_a_pause_for_the_operator_is_never_suspected_stuck(
    control: RunControl, clock: list[float]
) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.paused("t1", "run-1")

    clock[0] += 10 * STUCK_AFTER_S

    counts = control.counts()
    assert counts.paused == 1
    assert counts.active == 0
    assert counts.suspected_stuck == 0


def test_progress_on_a_paused_run_does_not_resume_it(
    control: RunControl, clock: list[float]
) -> None:
    """Only the executor decides a run runs again, by opening a window."""
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.paused("t1", "run-1")
    clock[0] += STUCK_AFTER_S
    control.progress("t1")

    assert control.counts().paused == 1


def test_a_pause_naming_another_run_leaves_the_live_one_alone(
    control: RunControl
) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)

    control.paused("t1", "run-other")

    assert control.counts().active == 1
    assert control.counts().paused == 0


def test_a_finished_run_leaves_the_counts(control: RunControl) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)

    control.finished("t1", "run-1")

    assert control.counts().active == 0


def test_a_late_finish_cannot_drop_the_run_that_replaced_it(
    control: RunControl
) -> None:
    """The same identity check the stop registry keeps for its tokens."""
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.executing("t1", "run-2", stuck_after_s=STUCK_AFTER_S)

    control.finished("t1", "run-1")

    assert control.counts().active == 1


def test_a_continued_run_replaces_the_paused_one_rather_than_adding(
    control: RunControl
) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.paused("t1", "run-1")

    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)

    counts = control.counts()
    assert (counts.active, counts.paused) == (1, 0)


def test_a_deleted_thread_stops_being_counted(control: RunControl) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.paused("t1", "run-1")

    control.forget("t1")

    assert control.counts().paused == 0


def test_several_threads_are_counted_separately(control: RunControl) -> None:
    control.executing("t1", "run-1", stuck_after_s=STUCK_AFTER_S)
    control.executing("t2", "run-2", stuck_after_s=STUCK_AFTER_S)
    control.executing("t3", "run-3", stuck_after_s=STUCK_AFTER_S)
    control.paused("t3", "run-3")

    assert control.counts() == type(control.counts())(2, 1, 0)


# ----- the wire -----

@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def _workspace(http_app: FastAPI) -> Iterator[Any]:
    """One thread store for the whole test, as the mounted app derives it.

    A fresh store per dependency call would answer every later request
    from an empty one, so the fixture holds the single instance the
    routes share.
    """
    from fin.host.http import get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    yield store


def _blocking_model(
    entered: threading.Event, release: threading.Event
) -> ScriptedChatModel:
    class _Blocking(ScriptedChatModel):
        def _generate(  # type: ignore[no-untyped-def]
            self, messages, stop=None, run_manager=None, **kwargs
        ):
            entered.set()
            release.wait(timeout=10.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    return _Blocking(script=[AIMessage(content="Net sales grew.")])


def _graph(model: ScriptedChatModel) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(), model=model, known_document_ids=KNOWN_IDS
    )


def _propose_fast_send() -> dict[str, Any]:
    """The send that opens one of the windows that DID survive the QA
    design correction: a proposed Fast 抽图 extraction still pauses the
    run for an operator pick.

    The request field is the only channel that reaches it — the send
    door seeds the run's ``extract_method_proposal`` state
    (``fin.host.workspace_http``) and the graph opens the propose window
    before any model call (``fin.host.graph``).
    """
    return {
        "question": GROUNDED_QUESTION,
        "propose_extract_method": "fast",
    }


def _fast_propose_window_graph() -> Any:
    """A graph whose remaining choice window is reachable.

    ``enable_choice_window`` stays on because it gates the windows that
    SURVIVED the QA design correction (ADR 0104): a Fast 抽图 proposal and
    genuine content clarification. The 检索档 picker it used to gate is
    retired — the routing residual is assigned host-side now — so this
    graph opens no mode window at all.
    """
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="Net sales grew.")]),
        known_document_ids=KNOWN_IDS,
        enable_choice_window=True,
    )


def _sse_events(body: str) -> list[tuple[str, Any]]:
    """The ``(event, data)`` pairs of one SSE body, in wire order.

    Private to this file on purpose: the paused-window contract is
    asserted on the event NAME, not on a substring, so the run cannot
    pass by naming a window in some other frame.
    """
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:") :].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def test_an_idle_process_publishes_zero_counts(
    client: TestClient, _workspace: Any
) -> None:
    body = client.get("/api/status")

    assert body.status_code == 200
    assert body.json()["run_counts"] == {
        "active": 0,
        "paused": 0,
        "suspected_stuck": 0,
    }


def test_the_counts_carry_no_identity_about_a_run(
    client: TestClient, _workspace: Any
) -> None:
    body = client.get("/api/status").json()

    assert set(body["run_counts"]) == COUNT_KEYS
    assert all(isinstance(value, int) for value in body["run_counts"].values())
    # No per-run or per-thread telemetry anywhere in the payload: an
    # aggregate that leaks one run's identity is the boot surface
    # widening by a key rather than by a value.
    for key, value in body.items():
        assert not isinstance(value, dict) or set(value) == COUNT_KEYS, key


def test_status_answers_while_a_run_holds_the_graph_lock(
    client: TestClient, http_app: FastAPI, _workspace: None
) -> None:
    """The count is read from local state, never from what the run holds.

    The run below is inside the model with the per-thread graph lock
    taken, so a status route that reached for the graph, the checkpoint or
    the store would block here — the one moment its answer is worth
    anything.
    """
    from fin.host.http import get_graph

    entered = threading.Event()
    release = threading.Event()
    http_app.dependency_overrides[get_graph] = lambda: _graph(
        _blocking_model(entered, release)
    )
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(
            lambda: client.post(
                f"/api/threads/{thread_id}/messages", json={"question": "q"}
            )
        )
        assert entered.wait(timeout=10.0), "the run never reached the model"
        body = client.get("/api/status")
        release.set()
        assert sending.result(timeout=20.0).status_code == 200

    assert body.status_code == 200
    assert body.json()["run_counts"]["active"] == 1


def test_the_counts_are_empty_again_once_the_run_has_ended(
    client: TestClient, http_app: FastAPI, _workspace: None
) -> None:
    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = lambda: _graph(
        ScriptedChatModel(script=[AIMessage(content="Net sales grew.")])
    )
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]
    client.post(f"/api/threads/{thread_id}/messages", json={"question": "q"})

    body = client.get("/api/status")

    assert body.json()["run_counts"] == {
        "active": 0,
        "paused": 0,
        "suspected_stuck": 0,
    }


def test_a_run_that_paused_on_the_wire_is_published_as_paused(
    client: TestClient, http_app: FastAPI, _workspace: Any
) -> None:
    """The count is read after a pause that really happened.

    Every other ``paused`` case above drives ``RunControl`` directly, so
    they all pass even if the executor stopped reporting pauses entirely —
    which is how a published field can never be non-zero while its own
    suite is green. This one crosses the executor seam: a send whose
    cascade pauses on a choice window leaves the turn waiting for an
    operator pick, and the count has to say "paused" rather than report
    that nothing happened.

    The vehicle is the Fast 抽图 propose window — one of the two windows
    that survived the QA design correction (ADR 0104 retired the 检索档
    picker, so it can no longer produce a paused run).
    """
    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = _fast_propose_window_graph
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]

    try:
        sent = client.post(
            f"/api/threads/{thread_id}/messages", json=_propose_fast_send()
        )

        assert sent.status_code == 200
        assert "choice_window" in sent.text, "the run did not pause on the window"
        events = _sse_events(sent.text)
        assert events[-1][0] == "choice_window", (
            "the run did not pause on the window"
        )
        window = events[-1][1]
        assert window["kind"] == "choice_window"
        # The surviving window is the extract-method one, so its options
        # are the METHODS — a 档 option would mean the retired picker came
        # back and this test would be passing on a window that no longer
        # exists in the product.
        assert {
            str(option.get("id")) for option in window.get("options") or []
        } == PROPOSE_WINDOW_OPTION_IDS
        # A paused run is still in flight on its checkpoint: it published
        # no terminal frame, so the counts below describe a live wait and
        # not a finished turn.
        assert "status" not in [name for name, _ in events]
        assert client.get("/api/status").json()["run_counts"] == {
            "active": 0,
            "paused": 1,
            "suspected_stuck": 0,
        }
    finally:
        # The registry is the mounted app's, so a pause left behind here is
        # still there for the next case in this process — and for every
        # later file, which is how this case ends up reading a paused count
        # it did not create. In a ``finally`` so a FAILED count assertion
        # cannot leak its own pause into the rest of the sweep: that is the
        # one way this case turns a real regression into four unrelated
        # failures elsewhere.
        client.delete(f"/api/threads/{thread_id}")


def test_a_paused_run_stops_being_counted_when_its_thread_is_deleted(
    client: TestClient, http_app: FastAPI, _workspace: Any
) -> None:
    """A wait the operator never returns from must not outlive the thread.

    The paused entry is local state, so the one event that can retire it
    is the thread going away — otherwise a process that once served one
    abandoned clarification would publish a paused turn for the rest of
    its life.

    The pause is the same surviving window the case above drives (the
    Fast 抽图 propose window, ADR 0104): a retired 检索档 window would
    make this case assert on a product path that no longer exists.
    """
    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = _fast_propose_window_graph
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]
    try:
        sent = client.post(
            f"/api/threads/{thread_id}/messages", json=_propose_fast_send()
        )
        assert "choice_window" in sent.text, "the run did not pause on the window"
        assert client.get("/api/status").json()["run_counts"]["paused"] == 1

        client.delete(f"/api/threads/{thread_id}")

        assert client.get("/api/status").json()["run_counts"] == {
            "active": 0,
            "paused": 0,
            "suspected_stuck": 0,
        }
    finally:
        # Same reason as the case above: a failing assertion part-way through
        # must not leave this pause in the registry for the rest of the
        # process. The delete is idempotent — a thread already gone answers
        # 404 and the entry is already retired, so the repeat is harmless.
        client.delete(f"/api/threads/{thread_id}")
