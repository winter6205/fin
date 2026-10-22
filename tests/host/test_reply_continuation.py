"""A clarification reply continues the run it answers.

A reply used to be a fresh send: the door minted a new ``run_id``
unconditionally, so the operator's answer to "请补充：公司" started a
second run on a checkpoint that already held the first one's identity.
The SSE ``status`` frame, the persisted turn row and the in-flight turn
the client is painting could then all disagree about which turn the
answer belonged to.

These tests pin the continued run: one ``run_id`` from the pause to the
persisted row, one accepted input for one reply, one turn row — plus the
admission rules that decide whether a second caller gets in at all.

Hermetic: ScriptedChatModel + InMemorySaver + the committed corpus tree.
No live model, network, or iknow-memory backend.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.run_control import RunAdmissionError, RunControl
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input, decide
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    AcceptedInput,
    PendingQuestion,
    ReconciliationProposal,
    TaskRuntimeState,
)

KNOWN_IDS = {"AAPL-2022Q3"}
AAPL_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
RESEARCH = "What was AAPL net sales in 2023 Q3?"
QUESTION_ID = "q-seeded"
CHOICES = ("公司", "期间")


# ----- fixtures (same shape as test_task_runtime_host.py; tests are not a package) -----


@pytest.fixture
def http_app() -> FastAPI:
    """Fresh FastAPI app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_graph] = lambda: _graph()
    return store


def _read_source_call(call_id: str = "c-read") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": "AAPL-2022Q3",
                        "version": AAPL_VERSION,
                        "unit": 1,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _graph(
    script: list[AIMessage] | None = None, model: Any | None = None
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model
        or ScriptedChatModel(
            script=script or [AIMessage(content="Net sales grew year over year.")]
        ),
        known_document_ids=KNOWN_IDS,
    )


# ----- wire + state helpers -------------------------------------------------


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                events.append((current["event"], json.loads(current["data"])))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:") :].strip()
    if "event" in current and "data" in current:
        events.append((current["event"], json.loads(current["data"])))
    return events


def _status(body: str) -> dict[str, Any]:
    for name, data in _parse_sse_events(body):
        if name == "status":
            return data
    raise AssertionError(f"no status frame in: {body[:400]}")


def _cfg(thread_id: str, **configurable: Any) -> dict:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _send(client: TestClient, thread_id: str, question: str, **door: Any) -> Any:
    return client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": question, **door},
    )


def _seed_pending_question(
    g: Any, tid: str, *, run_id: str | None = None
) -> TaskRuntimeState:
    """Commit a clarification the way the reconcile node does.

    Written back through the checkpointer because the door reads the
    COMMITTED envelope — a question built only in memory is not there
    to answer. ``run_id`` rides the write's config because a checkpoint
    written without it loses the run identity the continuation must
    reuse.
    """
    state = g.task_runtime_state(tid)
    state, accepted = accept_input(
        state, kind=INPUT_MESSAGE, payload=RESEARCH, provenance="workspace_send"
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            goal=RESEARCH,
            question=PendingQuestion(
                question_id=QUESTION_ID,
                task_id="",
                task_version=1,
                prompt="请补充：公司",
                choices=CHOICES,
                input_version=0,
            ),
        ),
    )
    g._graph.update_state(
        _cfg(tid, **({"run_id": run_id} if run_id else {})),
        {"task_runtime": committed.to_state_dict()},
    )
    return g.task_runtime_state(tid)


def _pause_run(g: Any, tid: str, *, run_id: str, question: str = "q1") -> str:
    """Leave ``tid`` mid-run: a recoverable checkpoint under ``run_id``.

    Streaming is abandoned right after the first committed node, so the
    checkpoint keeps pending nodes — the state the crash resume and a
    continued reply both address. Driven at the graph proxy directly:
    ``TestClient`` drains a response body, so a route can never park a
    thread mid-graph on its own.
    """
    cfg = _cfg(tid, run_id=run_id)
    stream = g.stream(reset_input_state(question), config=cfg, stream_mode="values")
    for _ in stream:
        break
    stream.close()
    assert g.pending_nodes(tid), "the run did not pause"
    return run_id


# ===========================================================================
# Admission: one serialized section per thread
# ===========================================================================


class _PausedStartGraph:
    """Delegates to the real graph, but parks before each ``stream``.

    The graph takes its per-thread lock LAZILY, inside ``stream``, so a
    start delayed here is exactly the window an advisory "is it locked?"
    probe cannot close: both callers have answered the probe and neither
    holds the lock yet. Everything else — the lock, the checkpoint, the
    streaming semantics — is the real seam, not a stand-in for it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._gate = threading.Event()
        self._gate.set()
        self._lock = threading.Lock()
        self._arrivals = 0
        self.arrived = threading.Event()

    def arm(self) -> None:
        """Open the window: park the next starts and count them."""
        with self._lock:
            self._arrivals = 0
            self.arrived = threading.Event()
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    @property
    def arrivals(self) -> int:
        with self._lock:
            return self._arrivals

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._arrivals += 1
            self.arrived.set()
        # Bounded wait: a caller the door already refused never arrives,
        # and a test must not hang waiting for it.
        self._gate.wait(15.0)
        return self._inner.stream(*args, **kwargs)


def test_a_reply_while_another_run_occupies_the_thread_is_refused_before_headers(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The second reply is a 409 with no stream and no accepted input.

    Before the serialized claim, both replies passed the advisory
    occupancy probe, both computed an acceptance from the same committed
    snapshot, and only then did one lose the graph lock — arriving to
    the client as a 200 whose frames refuse a run it had already been
    admitted for, with an ``input_version`` advance no checkpoint ever
    carried. Losing at the door is the whole point: the loser must not
    have accepted anything at all.
    """
    from fin.host.http import get_graph

    inner = _graph()
    g = _PausedStartGraph(inner)
    http_app.dependency_overrides[get_graph] = lambda: g

    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    _seed_pending_question(inner, tid)
    before = inner.task_runtime_state(tid)
    rows_before = len(fresh_store.list_runs(tid))

    results: dict[str, Any] = {}

    def reply(label: str) -> Any:
        response = _send(
            client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
        )
        results[label] = response
        return response

    g.arm()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reply, "first")
        assert g.arrived.wait(10.0), "the first reply never reached the graph"
        second = pool.submit(reply, "second")
        # Hold the window until the second reply has been answered by the
        # door or has parked at the graph — whichever the code under test
        # does. Releasing before the door has had its say would let the
        # first run take the graph lock first, and the second would be
        # refused by the lock it can actually see.
        deadline = time.monotonic() + 10.0
        while not (second.done() or g.arrivals >= 2):
            if time.monotonic() > deadline:
                raise AssertionError("the second reply never reached the door")
            time.sleep(0.02)
        g.release()
        first.result(timeout=30)
        second.result(timeout=30)

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    refused = next(r for r in results.values() if r.status_code == 409)
    assert refused.json() == {"error": "concurrent_run"}
    # The loser never reached the graph at all — it was refused at the
    # door, not after its acceptance had been computed and abandoned.
    assert g.arrivals == 1, "the refused reply still started a run"

    after = inner.task_runtime_state(tid)
    # One accepted input for one admitted reply, and the loser
    # contributed nothing: no phantom reply, no second advance.
    assert after.input_version == before.input_version + 1
    assert [(row.input_version, row.choice_id) for row in after.input_history] == [
        (row.input_version, row.choice_id) for row in before.input_history
    ] + [(before.input_version + 1, CHOICES[0])]
    assert len(fresh_store.list_runs(tid)) == rows_before + 1


def test_two_application_instances_do_not_admit_each_others_threads() -> None:
    """Admission, like the stop registry, is per mounted application.

    A shared registry would make one process's two apps refuse each
    other's runs on a colliding ``thread_id``, which is the isolation
    the two-app contract exists to keep — so a second app runs normally
    on the same id while the first is mid-run, and a finished run frees
    its own thread for whoever comes next.

    Isolation is proven by ABSENCE: the second ``admit`` not raising is
    the whole assertion, so the claims below are read for what they
    leave behind rather than to check a return value.
    """
    first = RunControl()
    second = RunControl()

    first_claim = first.admit("t-1")
    second_claim = second.admit("t-1")

    assert {first_claim.thread_id, second_claim.thread_id} == {"t-1"}
    assert first_claim is not second_claim
    # Each app owns its own lock for the shared id, so neither claim can
    # have released the other's.
    assert first._admissions is not second._admissions

    first_claim.release()
    second_claim.release()

    # A finished run frees its own thread for whoever comes next, and a
    # third registry on the same id is likewise unaffected.
    fresh = RunControl().admit("t-1")
    assert fresh.thread_id == "t-1"
    fresh.release()
    assert first.admit("t-1").thread_id == "t-1"


class _ClientGoneSend:
    """The transport uvicorn hands a response once the peer is gone.

    ``OSError`` at ``http.response.start`` is that transport's own
    answer, and it lands BEFORE the body iterator is advanced — so the
    run generator has not started, and anything its ``finally`` would
    release has not been released.
    """

    def __init__(self) -> None:
        self.starts = 0

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.starts += 1
            raise OSError("client is gone")


def _disconnect_scope(thread_id: str, body: bytes) -> dict[str, Any]:
    """One ASGI HTTP scope at the spec version the server advertises.

    Starlette branches on it: below 2.4 the response runs inside a
    collapsing task group, where the transport's ``OSError`` escapes
    before ``background`` is ever awaited — the deployed shape, and the
    one a ``BackgroundTasks`` release net cannot survive.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/threads/{thread_id}/messages",
        "raw_path": f"/api/threads/{thread_id}/messages".encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def _send_into_a_disconnect(http_app: FastAPI, thread_id: str, body: bytes) -> int:
    """Drive one send whose client leaves before the body is iterated.

    Returns how many times the response tried to start, so a caller can
    tell "the door never got that far" from "the door ran and the wire
    died under it".
    """
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # A peer that walked away never answers again. Returning
        # ``http.disconnect`` here would let the collapsing task group
        # cancel the response before its ``send`` ever failed, which is
        # a different failure than the deployed one.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport = _ClientGoneSend()
    with pytest.raises(BaseException):  # the disconnect is the transport's
        asyncio.run(http_app(_disconnect_scope(thread_id, body), receive, transport))
    return transport.starts


def test_a_disconnect_before_the_stream_starts_does_not_strand_the_claim(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The claim is the RESPONSE's to release, so a lost client frees it.

    The claim is taken at the door and used to be released only by the
    run generator's ``finally`` — which never runs when the client is
    gone before the first body chunk, because that generator has not
    started. The thread then refused every later send with 409
    ``concurrent_run`` while ``GET thread`` reported nothing in flight:
    a permanent, unexplainable lock. Nothing else frees it: the graph
    was never reached, and on this transport shape the ``OSError``
    escapes before ``background`` runs.
    """
    tid = _new_thread(client)

    starts = _send_into_a_disconnect(
        http_app, tid, json.dumps({"question": RESEARCH}).encode()
    )
    assert starts == 1, "the door never reached the wire"

    # The stranded claim answers with a run nobody is in: ``in_flight``
    # is false, so the 409 would have nothing to point at.
    assert client.get(f"/api/threads/{tid}").json()["in_flight"] is False
    second = _send(client, tid, RESEARCH)
    assert second.status_code == 200, second.text
    # Admitted for real, not merely answered: the second send ran under
    # its own identity and wrote the one turn the disconnected one
    # never got to.
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert [turn["run_id"] for turn in turns] == [_status(second.text)["run_id"]]


def test_an_abandoned_stream_records_the_disconnect_on_its_receipt(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A client that walks away is an OUTCOME, and the receipt says so.

    The one-receipt invariant already held — a disconnect projects no
    second row — but no path recorded the END. The turn was therefore
    left ``running`` with no terminal instant and its accounting open
    forever, so an operator could not tell an abandoned turn from one
    still in flight, and nothing ever retired it.

    Recovering the RUN after a disconnect is a different question and is
    left alone here: what is pinned is that the outcome is recorded.
    """
    from fin.host.turn_receipts import default_receipts

    receipts = default_receipts(fresh_store)
    tid = _new_thread(client)

    starts = _send_into_a_disconnect(
        http_app, tid, json.dumps({"question": RESEARCH}).encode()
    )
    assert starts == 1, "the door never reached the wire"

    stored = receipts.store.list_receipts(tid)
    assert len(stored) == 1, "the disconnect minted more than one receipt"
    receipt = stored[0]
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "stopped"
    assert receipt.cancellation_reason == "client_disconnect"
    assert receipt.terminal_at is not None
    # An abandoned turn ended without finishing, not with a failure.
    assert receipt.failure_kind is None
    assert receipt.accounting_complete is False


def test_a_finished_run_is_never_retroactively_a_disconnect(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The signal is the ABSENCE of an end, so a real end is left alone.

    Every stream ends through the same ``finally``. Without the guard
    this one would land on top of the turn's own terminal projection
    and relabel a answered turn as a lost client.
    """
    from fin.host.turn_receipts import default_receipts

    receipts = default_receipts(fresh_store)
    tid = _new_thread(client)

    response = _send(client, tid, RESEARCH)
    assert response.status_code == 200

    run_id = _status(response.text)["run_id"]
    receipt = receipts.read(tid, run_id)
    assert receipt is not None
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "done"
    assert receipt.cancellation_reason is None


# ===========================================================================
# The reply continues the run it answers
# ===========================================================================


PAUSED_RUN_ID = "7f4a8c2e-2c4e-4f7e-9e47-8d8f8c6b1a2d"


def _paused_thread(client: TestClient) -> tuple[str, Any]:
    """A thread whose run is paused at ``PAUSED_RUN_ID`` with a question open.

    The pause is a real mid-run checkpoint on the real graph — abandoned
    after its first committed node — so ``pending_nodes`` is non-empty
    and the checkpoint carries the canonical run id a continuation has
    to reuse.
    """
    from fin.host.http import get_graph

    g = _graph()
    fresh_store = client.app.dependency_overrides[get_graph]
    fresh_store()
    client.app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _pause_run(g, tid, run_id=PAUSED_RUN_ID, question=RESEARCH)
    _seed_pending_question(g, tid, run_id=PAUSED_RUN_ID)
    assert g.checkpoint_run_id(tid) == PAUSED_RUN_ID
    return tid, g


def test_a_reply_continues_the_same_run(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """One ``run_id`` from the pause to the persisted turn row.

    Minting a fresh id for a reply split one logical turn in two: the
    SSE status frame, the persisted row and the in-flight turn the
    client is painting could each name a different run. The checkpoint's
    own id is the run's identity, so the reply reuses it exactly as the
    crash resume already does.
    """
    tid, g = _paused_thread(client)

    body = _send(
        client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
    ).text

    assert _status(body)["run_id"] == PAUSED_RUN_ID
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert [turn["run_id"] for turn in turns] == [PAUSED_RUN_ID]


def test_a_continued_reply_accepts_exactly_one_input(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """One accepted input, one ``input_version`` step, one decision.

    The paused run resumes at whatever node it stopped on — usually past
    ``reconcile_task`` — so the door commits the decision with the same
    ``Command`` that continues the run. Left to the node, the accepted
    input would sit in history forever with nothing decided about it.
    """
    tid, g = _paused_thread(client)
    before = g.task_runtime_state(tid)

    _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])

    after = g.task_runtime_state(tid)
    assert after.input_version == before.input_version + 1
    replies = [row for row in after.input_history if row.kind == "reply"]
    assert [(row.input_version, row.choice_id) for row in replies] == [
        (before.input_version + 1, CHOICES[0])
    ]
    assert replies[0].run_id == PAUSED_RUN_ID, (
        "the accepted reply is not attached to the run it continued"
    )
    assert after.pending_question is None
    assert after.reconciliation is not None
    assert after.reconciliation.reconciled_input_version == after.input_version


def _outcome_frames(body: str) -> list[tuple[str, Any]]:
    """The turn's TERMINAL frames, in order, as the replay must reproduce them.

    Every terminal channel is compared, not four status keys: the lanes
    ``fin_run`` does persist — the visual claims and the semantic verdict
    — are exactly the ones a partial projection silently drops, and a
    duplicate delivery that dropped them would show a turn with no claims
    and no ``insufficient_evidence`` verdict while ``GET`` history showed
    both. The run's delta and tool frames stay out: ``fin_run`` is the
    durable record of the turn's OUTCOME, not of its narration.

    ``search_mode_decision`` is the one status key no row stores, so it is
    lifted off both sides rather than compared — a live channel with
    nothing to replay from.
    """
    frames = _parse_sse_events(body)
    first_terminal = next(
        index for index, (name, _) in enumerate(frames) if name == "status"
    )
    return [
        (
            name,
            {
                key: value
                for key, value in data.items()
                if key != "search_mode_decision"
            }
            if name == "status"
            else data,
        )
        for name, data in frames[first_terminal:]
    ]


def test_an_identical_retry_is_a_no_op(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A repeated delivery returns the first outcome and changes nothing.

    Duplicate delivery is idempotent: the retry key is the tuple the
    first delivery recorded, so a second POST of the same reply must not
    accept a second input, advance the version again, or append a second
    turn row under a run id that is already a primary key.
    """
    tid, g = _paused_thread(client)

    first = _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])
    after_first = g.task_runtime_state(tid)
    rows_after_first = client.get(f"/api/threads/{tid}").json()["turns"]

    retry = _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])
    assert retry.status_code == 200
    assert _outcome_frames(retry.text) == _outcome_frames(first.text)

    after_retry = g.task_runtime_state(tid)
    assert after_retry.input_version == after_first.input_version
    assert after_retry.input_history == after_first.input_history
    assert client.get(f"/api/threads/{tid}").json()["turns"] == rows_after_first


def test_a_retry_replays_the_persisted_claim_and_semantic_channels() -> None:
    """The replay carries every lane the row persisted, not four keys.

    Driven at the projection rather than through a run, because the two
    lanes here are produced by gates this file's scripted model cannot
    reach — a claim needs a visual read and the verdict needs a host
    gate. What is under test is the replay itself: a row holding both
    must come back out on the wire with both, so a duplicate delivery
    cannot show a turn the ``GET`` history contradicts.
    """
    from fin.host.thread_store import InMemoryThreadStore, RunRecord
    from fin.host.evidence.visual_evidence import VisualEvidence
    from fin.host.workspace_http import _retry_frames

    store = InMemoryThreadStore()
    tid = store.create_thread(id="t-claims", title="研究").id
    # The PRODUCER's own row, so this pins the replay against the shape
    # the live wire emits rather than against a hand-written guess.
    claim = VisualEvidence(
        attachment_id="att-visual-1",
        version="b" * 64,
        version_int=7,
        unit=3,
        page=3,
        observation_version=2,
        scope=None,
        provider="stub-vision",
        model_version="stub-vision-1",
        page_image_sha256="c" * 64,
        detail="gross margin row, right column",
        filename="page-3.png",
        reused=False,
        outbound_calls=1,
    ).as_claim_row()
    store.append_run(
        RunRecord(
            id="run-claims",
            thread_id=tid,
            question=RESEARCH,
            status="refuse",
            status_reason="insufficient_evidence",
            answer="",
            citations=[],
            visual_claims=[claim],
            semantic_outcome="insufficient_evidence",
        )
    )
    prior = AcceptedInput(
        input_version=1,
        kind="reply",
        payload=CHOICES[0],
        provenance="workspace_send",
        question_id=QUESTION_ID,
        choice_id=CHOICES[0],
        run_id="run-claims",
    )

    frames = list(_retry_frames(store, tid, prior))
    events = _parse_sse_events("".join(frames))
    names = [name for name, _ in events]

    assert names == ["status", "answer", "visual_claims", "citations"]
    assert dict(events)["status"]["semantic_outcome"] == "insufficient_evidence"
    replayed = dict(events)["visual_claims"]["visual_claims"]
    assert replayed == [
        {**claim, "schema": "fin.evidence.visual_claim", "schema_version": 1}
    ]


def test_a_conflicting_reply_to_an_answered_question_is_refused(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A DIFFERENT answer to the same question does not overwrite one.

    The operator already answered; a second, different answer to the
    same question is a conflict about history, not a fresh input to
    reconcile — and it must not leave a second accepted reply behind.
    """
    tid, g = _paused_thread(client)
    _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])
    settled = g.task_runtime_state(tid)

    refused = _send(
        client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[1]
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"] == "task_input_invalid"
    assert g.task_runtime_state(tid) == settled


def test_a_choice_outside_the_offered_candidates_is_refused(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A reply naming an answer nobody offered is not reconciled.

    Candidate membership is a fact about the CURRENT question, and only
    the door is close enough to it to check: the reconcile boundary sees
    a pending question and a reply, not the two lists beside them.
    """
    tid, g = _paused_thread(client)
    before = g.task_runtime_state(tid)

    refused = _send(
        client, tid, "", question_id=QUESTION_ID, choice_id="not-offered"
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"] == "task_input_invalid"
    assert g.task_runtime_state(tid) == before


def test_a_reply_to_a_superseded_question_is_expired(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A reply naming a question that is no longer current is expired.

    ``question_id`` names WHICH question is being answered, so a stale
    draft must not be applied to whatever question the thread holds now.
    """
    tid, g = _paused_thread(client)
    before = g.task_runtime_state(tid)

    refused = _send(
        client, tid, "", question_id="q-superseded", choice_id=CHOICES[0]
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "task_reply_expired"
    assert g.task_runtime_state(tid) == before


def _evicted_acceptance(g: Any, tid: str) -> TaskRuntimeState:
    """Commit an envelope whose accepted records no longer include a reply.

    ``accept_input`` keeps only the most recent ``INPUT_HISTORY_LIMIT``
    accepted inputs, so on a long-lived thread the record that proves a
    reply was admitted has genuinely fallen outside what the door reads
    back. Written with the real acceptance function rather than a
    hand-built tuple, so the bound this leaves is the bound the product
    has.
    """
    from fin.host.task.task_runtime_types import INPUT_HISTORY_LIMIT

    state = TaskRuntimeState()
    for index in range(INPUT_HISTORY_LIMIT + 5):
        state, _ = accept_input(
            state,
            kind=INPUT_MESSAGE,
            payload=f"历史提问 {index}",
            provenance="workspace_send",
        )
    assert all(
        row.kind != "reply" for row in state.input_history
    ), "the bounded history kept a reply, so nothing was evicted"
    g._graph.update_state(
        _cfg(tid), {"task_runtime": state.to_state_dict()}
    )
    return g.task_runtime_state(tid)


def _assert_a_refused_stale_reply(client: TestClient, tid: str, g: Any) -> None:
    """A stale reply changes nothing the door could have moved.

    Read as the durable facts an operator can see afterwards — the turn
    table and the committed envelope — because that is all a rejected
    reply is allowed to leave behind.

    The token is ``task_reply_unknown`` in both callers, and that is the
    point: neither thread holds a pending question, and a record whose
    eviction removed the proof of an earlier acceptance leaves the door
    with no more to go on than the reconciliation boundary would have.
    ``task_reply_expired`` would claim this thread had once asked
    something, which the committed envelope does not say.
    """
    before = g.task_runtime_state(tid)
    turns_before = client.get(f"/api/threads/{tid}").json()["turns"]

    stale = _send(
        client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
    )

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["error"] == "task_reply_unknown"
    assert client.get(f"/api/threads/{tid}").json()["turns"] == turns_before
    assert g.task_runtime_state(tid) == before


def test_a_reply_naming_no_clarification_is_refused_at_the_door(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A reply with nothing to answer is refused before it starts a run.

    Nothing about the thread is recoverable here — the prior run finished
    and no clarification is pending — so the reply used to fall through
    to the ordinary send door and be admitted as a SECOND ``INPUT_REPLY``
    under a brand-new run id. The operator saw a 200 stream and a new
    turn row for a draft to a question that never existed, and the
    ``input_version`` advanced for it. Only the reconciliation boundary
    downstream noticed, by refusing inside a run that had already
    started.
    """
    from fin.host.http import get_graph

    g = _graph()
    http_app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)

    _assert_a_refused_stale_reply(client, tid, g)


def test_a_reply_whose_acceptance_left_retained_history_is_refused(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The same refusal once the acceptance record is no longer retained.

    Eviction is not an edge case: the envelope keeps a bounded number of
    accepted inputs, so a reply admitted long enough ago on a busy
    thread reads back as never admitted at all. Treating "cannot prove
    it was accepted" as "therefore it is a fresh send" is how a stale
    draft becomes a new turn — the proof being absent is a reason to
    refuse, never a reason to run.
    """
    from fin.host.http import get_graph

    g = _graph()
    http_app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    _evicted_acceptance(g, tid)

    _assert_a_refused_stale_reply(client, tid, g)


def test_origin_run_id_must_name_the_paused_run(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The optional origin names the turn being answered, or is refused.

    Optional because a client that does not track run ids still gets a
    working reply door; once a client DOES name one, naming a run that
    is not the thread's paused run means it is answering a turn that is
    no longer the one in flight.
    """
    tid, g = _paused_thread(client)

    wrong = _send(
        client,
        tid,
        "",
        question_id=QUESTION_ID,
        choice_id=CHOICES[0],
        origin_run_id="some-other-run",
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["detail"]["error"] == "task_input_invalid"
    assert g.task_runtime_state(tid).pending_question is not None

    named = _send(
        client,
        tid,
        "",
        question_id=QUESTION_ID,
        choice_id=CHOICES[0],
        origin_run_id=PAUSED_RUN_ID,
    )
    assert named.status_code == 200
    assert _status(named.text)["run_id"] == PAUSED_RUN_ID


# ===========================================================================
# Concurrent replies converge; a stop still reaches a continued run
# ===========================================================================


def test_two_concurrent_identical_replies_converge_on_one_acceptance(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The same reply twice at once is one accepted input and one run.

    Both callers pass a read of the same committed envelope, so the
    serialized admission section — not a lucky ordering — is what makes
    the second one recognize the first's work instead of duplicating it.
    """
    from fin.host.http import get_graph

    inner = _graph()
    g = _PausedStartGraph(inner)
    http_app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _pause_run(inner, tid, run_id=PAUSED_RUN_ID, question=RESEARCH)
    _seed_pending_question(inner, tid, run_id=PAUSED_RUN_ID)

    results: dict[str, Any] = {}

    def reply(label: str) -> Any:
        response = _send(
            client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
        )
        results[label] = response
        return response

    g.arm()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reply, "first")
        assert g.arrived.wait(10.0), "the first reply never reached the graph"
        second = pool.submit(reply, "second")
        deadline = time.monotonic() + 10.0
        while not (second.done() or g.arrivals >= 2):
            if time.monotonic() > deadline:
                raise AssertionError("the second reply never reached the door")
            time.sleep(0.02)
        g.release()
        first.result(timeout=30)
        second.result(timeout=30)

    assert sorted(r.status_code for r in results.values()) == [200, 409]
    assert g.arrivals == 1

    after = inner.task_runtime_state(tid)
    replies = [row for row in after.input_history if row.kind == "reply"]
    assert len(replies) == 1, [row.input_version for row in replies]
    assert (
        replies[0].input_version
        == max(row.input_version for row in after.input_history)
    )
    assert [t["run_id"] for t in client.get(f"/api/threads/{tid}").json()["turns"]] == [
        PAUSED_RUN_ID
    ]


def test_two_concurrent_different_replies_leave_exactly_one_winner(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Two different answers to one question cannot both land.

    The conflict rule is the second half of the same admission section:
    whichever caller arrives second reads an envelope that already
    carries an accepted answer and is refused, so the question never
    ends up with two.
    """
    from fin.host.http import get_graph

    inner = _graph()
    g = _PausedStartGraph(inner)
    http_app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _pause_run(inner, tid, run_id=PAUSED_RUN_ID, question=RESEARCH)
    _seed_pending_question(inner, tid, run_id=PAUSED_RUN_ID)

    results: dict[str, Any] = {}

    def reply(label: str, choice: str) -> Any:
        response = _send(
            client, tid, "", question_id=QUESTION_ID, choice_id=choice
        )
        results[label] = response
        return response

    g.arm()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reply, "first", CHOICES[0])
        assert g.arrived.wait(10.0), "the first reply never reached the graph"
        second = pool.submit(reply, "second", CHOICES[1])
        deadline = time.monotonic() + 10.0
        while not (second.done() or g.arrivals >= 2):
            if time.monotonic() > deadline:
                raise AssertionError("the second reply never reached the door")
            time.sleep(0.02)
        g.release()
        first.result(timeout=30)
        second.result(timeout=30)

    assert sorted(r.status_code for r in results.values()) == [200, 409]
    after = inner.task_runtime_state(tid)
    assert [row.choice_id for row in after.input_history if row.kind == "reply"] == [
        CHOICES[0]
    ]
    assert after.pending_question is None


def test_stop_still_reaches_a_continued_run(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The claim a continued run holds does not lock Stop out.

    Stop flips the token the run's stream polls and never touches the
    door's admission claim — if it had to take that claim it could only
    fire once the run had already finished, which is exactly when a stop
    matters most.
    """
    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel as SCM

    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=10.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    inner = _graph(model=BlockingModel(script=[AIMessage(content="answered anyway")]))
    http_app.dependency_overrides[get_graph] = lambda: inner
    tid = _new_thread(client)
    _pause_run(inner, tid, run_id=PAUSED_RUN_ID, question=RESEARCH)
    _seed_pending_question(inner, tid, run_id=PAUSED_RUN_ID)

    bodies: dict[str, str] = {}

    def reply() -> None:
        with client.stream(
            "POST",
            f"/api/threads/{tid}/messages",
            json={
                "question": "",
                "question_id": QUESTION_ID,
                "choice_id": CHOICES[0],
            },
        ) as response:
            bodies["continued"] = "".join(response.iter_text())

    worker = threading.Thread(target=reply)
    worker.start()
    try:
        assert entered.wait(10.0), "the continued run never reached the model"
        stopped = client.post(f"/api/threads/{tid}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json() == {"stopped": True, "run_id": PAUSED_RUN_ID}
    finally:
        release.set()
        worker.join(20)
        assert worker.is_alive() is False

    assert _status(bodies["continued"])["run_id"] == PAUSED_RUN_ID
    # The resume column's rule, unchanged by the continuation: a stopped
    # run writes NO turn row.
    assert client.get(f"/api/threads/{tid}").json()["turns"] == []


def test_two_applications_cannot_cancel_each_others_continued_runs(
    fresh_store,
) -> None:
    """A continued run belongs to the application that continued it.

    The continuation reuses another application's run identity, so the
    only thing keeping the two runs apart is the registry they stop and
    admit through. A shared one would let one app's Stop reach the
    other's continued run — the exact confusion the two-app contract
    exists to prevent.
    """
    from fin.host.run_execution import ResumeRunRequest, RunExecutor

    first_graph = _graph()
    second_graph = _graph()
    first_thread = fresh_store.create_thread(id="thread-first", title="first").id
    second_thread = fresh_store.create_thread(id="thread-second", title="second").id
    _pause_run(first_graph, first_thread, run_id="run-first")
    _pause_run(second_graph, second_thread, run_id="run-second")

    first_control = RunControl()
    second_control = RunControl()
    first = RunExecutor(
        graph=first_graph, store=fresh_store, run_control=first_control
    )
    second = RunExecutor(
        graph=second_graph, store=fresh_store, run_control=second_control
    )

    events = list(
        first.resume(
            ResumeRunRequest(
                thread_id=first_thread,
                run_id="run-first",
                resume_input=None,
                resume_config=_cfg(first_thread, run_id="run-first"),
                admission=first_control.admit(first_thread),
            )
        )
    )
    assert events, "the continued run produced no events"

    # The other application can neither reach the run that just finished
    # nor its thread's admission claim.
    assert second_control.request_stop(first_thread) is None
    assert second_control.admit(first_thread).thread_id == first_thread
    assert first_control.request_stop(first_thread) is None
    assert [row.id for row in fresh_store.list_runs(first_thread)] == ["run-first"]


def test_a_retry_of_an_undecided_reply_continues_that_acceptance(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A repeat of an accepted-but-undecided reply continues, not re-accepts.

    The first delivery advanced ``input_version`` and then lost its run
    before anything decided it. The repeat carries the same tuple, so it
    is the same input: continuing the paused run is what finishes it, and
    accepting it a second time would count one operator answer twice.
    """
    tid, g = _paused_thread(client)
    settled = g.task_runtime_state(tid)
    # The acceptance without its decision: what a run that died between
    # the two leaves behind.
    accepted_only, accepted = accept_input(
        settled,
        kind="reply",
        payload=None,
        provenance="workspace_send",
        question_id=QUESTION_ID,
        choice_id=CHOICES[0],
        run_id=PAUSED_RUN_ID,
    )
    g._graph.update_state(
        _cfg(tid, run_id=PAUSED_RUN_ID),
        {"task_runtime": accepted_only.to_state_dict()},
    )
    before = g.task_runtime_state(tid)
    assert before.input_version == accepted.input_version

    response = _send(
        client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
    )

    assert response.status_code == 200, response.text
    assert _status(response.text)["run_id"] == PAUSED_RUN_ID
    after = g.task_runtime_state(tid)
    # One acceptance for one operator answer, whichever delivery carried it.
    assert after.input_version == before.input_version
    assert [
        (row.input_version, row.choice_id)
        for row in after.input_history
        if row.kind == "reply"
    ] == [(accepted.input_version, CHOICES[0])]


# ===========================================================================
# The durable store: one turn row for one continued run
# ===========================================================================


def test_a_continued_reply_writes_one_turn_row_in_postgres(
    client: TestClient, http_app: FastAPI
) -> None:
    """``fin_run.id`` is a primary key, so a second row is not a detail.

    The in-memory store appends without a uniqueness check, which would
    let a retried or doubled reply leave two rows behind and hide the
    defect this slice exists to close. Against a real Postgres the same
    reply is a primary-key violation, so the run's identity — one run,
    one row — is enforced where the schema can actually enforce it.

    Env-gated: without a ``_test`` DSN the durable store is untested in
    this environment and the in-memory assertions above still hold.
    """
    import os

    url = os.environ.get("FIN_CHECKPOINT_URL", "").strip()
    if not url:
        pytest.skip("FIN_CHECKPOINT_URL is not set")

    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import PostgresThreadStore

    store = PostgresThreadStore(url)
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_graph] = lambda: _graph()
    try:
        g = _graph()
        http_app.dependency_overrides[get_graph] = lambda: g
        tid = _new_thread(client)
        _pause_run(g, tid, run_id=PAUSED_RUN_ID, question=RESEARCH)
        _seed_pending_question(g, tid, run_id=PAUSED_RUN_ID)

        body = _send(
            client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
        ).text
        assert _status(body)["run_id"] == PAUSED_RUN_ID
        # The repeat is the case the primary key exists for: a second row
        # under a run id already stored raises rather than duplicating.
        retry = _send(
            client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0]
        )
        assert retry.status_code == 200, retry.text

        rows = [row.id for row in store.list_runs(tid)]
        assert rows == [PAUSED_RUN_ID], rows
    finally:
        import psycopg

        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM fin_thread")
                cur.execute("DELETE FROM fin_run")
            conn.commit()
