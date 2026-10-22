"""The delete door: refused while a run is live, whole once it is not.

A delete and a run are both writers of the same thread, so the door claims
the thread before it removes anything and refuses the request outright when
the claim is held — the run is not raced and its turn is not half removed.
Once the claim is held, the retirement is one transaction that records why
the receipts are going and takes the history with them.

The late-write case is the other half of the same promise, and it is
asserted here against the door's own store as well as in the store's own
suite: a run that finished after the delete must not bring a receipt back.
"""

from __future__ import annotations

import concurrent.futures
import threading

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.run_execution import THREAD_DELETED_IN_FLIGHT_COPY
from fin.host.thread_store import InMemoryThreadStore, ThreadStore
from fin.host.turn_receipts import TurnReceiptProjector, default_receipts

KNOWN_IDS = {"AAPL-2022Q3"}


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI) -> ThreadStore:
    from fin.host.http import get_graph, get_thread_store

    memory: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: memory
    http_app.dependency_overrides[get_graph] = lambda: _graph()
    return memory


def _graph(model: ScriptedChatModel | None = None) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model
        or ScriptedChatModel(script=[AIMessage(content="Net sales grew.")]),
        known_document_ids=KNOWN_IDS,
    )


def _receipts(store: ThreadStore) -> TurnReceiptProjector:
    """The projection the mounted app derives from this store."""
    return default_receipts(store)


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _send(client: TestClient, thread_id: str, question: str = "q") -> Any:
    return client.post(
        f"/api/threads/{thread_id}/messages", json={"question": question}
    )


def _turn_ids(turns: list[Any]) -> list[str]:
    return [turn.id for turn in turns]


def _blocking_model(
    entered: threading.Event, release: threading.Event
) -> ScriptedChatModel:
    """A model that holds its run open until the test lets it finish.

    The events live in the caller's scope rather than on the instance:
    the model is a pydantic object, which refuses an attribute it was not
    declared with.
    """

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


# ----- the refusal while a run is live -----

def test_deleting_a_thread_mid_run_is_refused_and_the_turn_survives(
    client: TestClient, http_app: FastAPI, store: ThreadStore
) -> None:
    """A live run holds the thread, so the delete is a 409 and a no-op.

    The send is driven to completion by a model that blocks inside it, so
    the claim this door refuses on is one a real run took at a real moment
    — not a claim a test pretended into existence.
    """
    from fin.host.http import get_graph

    entered = threading.Event()
    release = threading.Event()
    model = _blocking_model(entered, release)
    http_app.dependency_overrides[get_graph] = lambda: _graph(model)
    thread_id = _new_thread(client)

    statuses: dict[str, int] = {}
    bodies: dict[str, dict[str, Any]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(lambda: _send(client, thread_id))
        assert entered.wait(timeout=10.0), "the run never reached the model"
        deleting = pool.submit(lambda: client.delete(f"/api/threads/{thread_id}"))
        refused = deleting.result(timeout=10.0)
        statuses["delete"] = refused.status_code
        bodies["delete"] = refused.json()
        release.set()
        statuses["send"] = sending.result(timeout=20.0).status_code

    assert statuses["delete"] == 409
    assert bodies["delete"] == {"error": "concurrent_run"}
    assert statuses["send"] == 200
    # Refused means refused: nothing was removed, including the turn the
    # run was still writing.
    assert store.get_thread(thread_id) is not None
    assert len(_turn_ids(store.list_runs(thread_id))) == 1


def test_the_delete_is_admitted_once_the_run_has_ended(
    client: TestClient, store: ThreadStore
) -> None:
    """The same thread deletes cleanly after the run released its claim."""
    thread_id = _new_thread(client)
    assert _send(client, thread_id).status_code == 200

    assert client.delete(f"/api/threads/{thread_id}").status_code == 200

    assert store.get_thread(thread_id) is None


# ----- the retirement -----

def test_deleting_a_thread_takes_its_history_and_its_receipts(
    client: TestClient, store: ThreadStore
) -> None:
    receipts = _receipts(store)
    thread_id = _new_thread(client)
    _send(client, thread_id)
    (turn_id,) = _turn_ids(store.list_runs(thread_id))
    assert receipts.read(thread_id, turn_id) is not None

    assert client.delete(f"/api/threads/{thread_id}").status_code == 200

    assert store.get_thread(thread_id) is None
    assert receipts.read(thread_id, turn_id) is None
    assert client.get(f"/api/threads/{thread_id}").status_code == 404


def test_a_finished_run_cannot_recreate_the_receipt_of_a_deleted_thread(
    client: TestClient, http_app: FastAPI, store: ThreadStore
) -> None:
    """The late writer meets the fence on both the receipt and the history.

    The run is inside the graph and holding its claim when the delete is
    refused, so the retirement below happens through a store whose fence is
    already proven by the refusal above; what this pins is the second half
    — that the writer which then finishes finds nothing to write onto.
    """
    from fin.host.http import get_graph

    receipts = _receipts(store)
    entered = threading.Event()
    release = threading.Event()
    model = _blocking_model(entered, release)
    http_app.dependency_overrides[get_graph] = lambda: _graph(model)
    thread_id = _new_thread(client)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(lambda: _send(client, thread_id))
        assert entered.wait(timeout=10.0), "the run never reached the model"
        # Retire the thread through the store the door would use, with the
        # live run still fenced out by its claim.
        retired = receipts.delete_thread(thread_id)
        release.set()
        body = sending.result(timeout=20.0)

    assert [r.cancellation_reason for r in retired] == ["thread_deleted"]
    assert store.get_thread(thread_id) is None
    assert receipts.read(thread_id, retired[0].turn_id) is None
    # The run finished anyway, and its own terminal write is what the fence
    # refused: the wire closes on the deleted-thread refuse instead of a
    # turn row and a receipt nobody can read.
    assert THREAD_DELETED_IN_FLIGHT_COPY in body.text


def test_deleting_an_unknown_thread_is_still_a_404(
    client: TestClient, store: ThreadStore
) -> None:
    assert client.delete("/api/threads/does-not-exist").status_code == 404
