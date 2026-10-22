"""T2f durable graph gate: the send/pause/retry/re round trip (HTTP surface).

What this file pins (spec thread-attachment-source-read, ADR 0089):

* a send naming a stored ``uploaded`` row is ACCEPTED (no 400): the id
  rides the run state with an empty ``text`` while a ``ready`` row still
  carries its frozen text;
* the grounded run then pauses on the preparation gate and the wire
  closes on the ``attachment_wait`` frame carrying the server-minted
  run id and the pending attachment ids — with nothing persisted;
* the thread detail answers ``pending_interrupt_kind ==
  "attachment_preparation"`` while that pause is pending;
* the hermetic round trip: send -> durable job (row ``parsing``) ->
  ``drain_attachment_jobs`` -> the shared in-process resume drives the
  SAME run id to completion with the hydrated text in the model prompt;
* a preparation that fails AFTER the run started reaches the turn row as
  the gate's typed refusal — never an empty successful turn, never a
  stale ``ready``;
* the retry door answers immediately with the wire record, enqueues or
  joins exactly ONE durable job (two concurrent retries included), and
  never parses inside the request;
* the named send refusals are unchanged: unknown / cross-thread ->
  400 ``attachment_not_found``, a ``failed`` row -> 400
  ``attachment_not_ready``, an in-flight upload -> 409
  ``attachment_upload_in_progress``.

Hermetic by construction: in-memory thread / attachment / job stores,
``ScriptedChatModel`` + ``InMemorySaver``, no Postgres, no network, no
worker threads — the drains and the auto-resumer are driven directly
through the same internal functions the boot sweeps use.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.corpus.extracted import load_extracted_tree
from fin.corpus.object_store import InMemoryObjectStore
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_budgets import AttachmentBudgets
from fin.host.attachment.attachment_prepare import AttachmentPreparationService
from fin.host.attachment.attachment_store import InMemoryAttachmentStore
from fin.host.run_control import RunControl
from fin.host.thread_store import InMemoryThreadStore
from fin.host.workspace_http import _resume_one_attachment_wait
from fin.retrieve import FakeRetrievePort


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# The committed corpus units behind the graph's ``read_source`` company leg;
# the serve path itself never walks the tree (ADR 0046 / 0047).
_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        AAPL_2022Q3: load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )[AAPL_2022Q3]
    }
)

# A unique sentinel no corpus fixture contains; the recording model
# proves hydrated attachment text (or its absence) reached the prompt.
ATTACH_SENTINEL = "T2F-ROUNDTRIP-SENTINEL net sales were 89,493 million"

# Grounded by the Soft-Track rules tier (mirrors test_thread_attach.py):
# naming "retrieve" routes the turn to the research leaf, which is the
# only route that passes through the attachment preparation gate.
GROUNDED_QUESTION = "retrieve a summary of the attachment"

DEFAULT_BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=8,
    max_vision_calls_per_run=16,
    max_vision_crop_calls_per_run=8,
)


class RecordingScriptedModel(ScriptedChatModel):
    """Scripted model that records the FULL prompt of every call.

    ``ScriptedChatModel._stream`` with neither ``stream_responses`` nor
    ``stream_script`` set delegates to ``_generate``, so overriding
    ``_generate`` records both the sync and the streamed path — and the
    workspace graph always streams.
    """

    seen_calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_calls.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


# ----- fixtures / harness -----


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    tree = load_extracted_tree(
        Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
    )
    return tree[AAPL_2022Q3][0].text


def _read_then_answer_script(answer: str) -> list[AIMessage]:
    """One ``read_source`` hop, then an extractive answer the citation gate
    admits — the proven done shape at the HTTP surface."""
    return [
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
                        },
                        "position": 0,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


@pytest.fixture
def env(http_app: FastAPI, aapl_page1_text: str):
    """One in-memory attachment stack + graph, with a per-test builder.

    ``build(wire_gate=True)`` compiles the graph WITH the attachment
    service (the T2f gate node exists); ``wire_gate=False`` keeps today's
    topology so a plain send's run state can be read without a pause.
    The same service instance answers the HTTP doors and the graph gate,
    which is exactly what the production composition root does.
    """
    from fin.host.http import (
        get_attachment_budgets,
        get_attachment_service,
        get_graph,
        get_thread_store,
    )

    def build(*, wire_gate: bool = True) -> dict[str, Any]:
        store = InMemoryThreadStore()
        objects = InMemoryObjectStore()
        service = AttachmentPreparationService(
            store=InMemoryAttachmentStore(store), objects=objects
        )
        answer = f"Per the filing: {aapl_page1_text[:80]}"
        model = RecordingScriptedModel(
            script=_read_then_answer_script(answer)
        )
        kwargs: dict[str, Any] = {
            "checkpointer": InMemorySaver(),
            "model": model,
            "known_document_ids": KNOWN_IDS,
            "adapter": _READ_ADAPTER,
        }
        if wire_gate:
            kwargs["attachment_service"] = service
        graph = compile_graph(**kwargs)
        http_app.dependency_overrides[get_thread_store] = lambda: store
        http_app.dependency_overrides[get_attachment_service] = lambda: service
        http_app.dependency_overrides[get_attachment_budgets] = (
            lambda: DEFAULT_BUDGETS
        )
        http_app.dependency_overrides[get_graph] = lambda: graph
        return {
            "store": store,
            "service": service,
            "model": model,
            "graph": graph,
            "objects": objects,
            "answer": answer,
        }

    return build


# ----- helpers (mirrors test_thread_attach.py / test_workspace_http.py) -----


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "q"}).json()["id"]


def _attach(
    client: TestClient, thread_id: str, content: bytes, filename: str = "note.md"
):
    return client.post(
        f"/api/threads/{thread_id}/attachments",
        content=content,
        params={"filename": filename},
    )


def _send(
    client: TestClient,
    thread_id: str,
    question: str = GROUNDED_QUESTION,
    attachment_ids: list[str] | None = None,
):
    payload: dict[str, Any] = {"question": question}
    if attachment_ids is not None:
        payload["attachment_ids"] = attachment_ids
    return client.post(f"/api/threads/{thread_id}/messages", json=payload)


def _retry(client: TestClient, thread_id: str, attachment_id: str):
    return client.post(
        f"/api/threads/{thread_id}/attachments/{attachment_id}/retry"
    )


def _retire(client: TestClient, thread_id: str) -> None:
    """Retire a run parked on the attachment gate, through the real door.

    A pause is a state and not an end, so ``_drive`` deliberately keeps the
    run's live entry open in the mounted app's ``RunControl`` — that is what
    makes ``/api/status`` able to publish a non-zero ``paused`` at all. The
    one production event that retires it is ``DELETE /api/threads/{id}``,
    which calls ``run_control.forget`` for exactly this reason.

    Every case here parks a run and then walks away, so without this the
    shared registry carries the pause into every later file in the process
    and ``/api/status`` publishes a run count nobody in the suite created.
    The status code is asserted so a refused delete (a second pause, an
    attachment cleanup failure) cannot hide a leak behind a green test.
    """
    response = client.delete(f"/api/threads/{thread_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
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
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _checkpoint_attachments(env: dict[str, Any], thread_id: str) -> list[dict[str, str]]:
    """The run-state ``attachments`` channel on the latest checkpoint.

    Reaching through ``_LockedGraph._graph.get_state`` is the documented
    test-surface read the state-persistence pins already use.
    """
    values = (
        env["graph"]._graph.get_state(  # noqa: SLF001 -- test state read
            {"configurable": {"thread_id": thread_id}}
        ).values
        or {}
    )
    return [dict(item) for item in (values.get("attachments") or [])]


def _model_saw(env: dict[str, Any], needle: str) -> bool:
    return any(
        needle in str(getattr(message, "content", ""))
        for call in env["model"].seen_calls
        for message in call
    )


def _pause_one_run(
    client: TestClient, env: dict[str, Any], thread_id: str
) -> tuple[str, str, list[tuple[str, Any]]]:
    """Upload one stored ``uploaded`` row and send a grounded turn that
    names it; return ``(run_id, attachment_id, events)`` after the wire
    closed on the preparation pause."""
    attachment_id = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()[
        "attachment_id"
    ]
    response = _send(client, thread_id, attachment_ids=[attachment_id])
    assert response.status_code == 200, response.text
    events = _parse_sse_events(response.text)
    wait = [data for name, data in events if name == "attachment_wait"]
    assert wait, f"no attachment_wait frame on the wire: {[n for n, _ in events]}"
    return str(wait[0]["run_id"]), attachment_id, events


# ----- 1. accepted send: run state shapes -----


def test_send_with_uploaded_and_ready_rows_carries_empty_and_frozen_text(
    client: TestClient, env
) -> None:
    """Normal path: a stored ``uploaded`` id is accepted with an empty
    ``text`` on the run state, and a ``ready`` id in the SAME send still
    carries its frozen text (the gate unwired: the send itself decides
    the ride, the refusal is gone)."""
    built = env(wire_gate=False)
    thread_id = _new_thread(client)
    uploaded = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    ready = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    built["service"].prepare(ready["attachment_id"])

    response = _send(
        client,
        thread_id,
        attachment_ids=[uploaded["attachment_id"], ready["attachment_id"]],
    )
    assert response.status_code == 200, response.text

    items = _checkpoint_attachments(built, thread_id)
    assert [item["attachment_id"] for item in items] == [
        uploaded["attachment_id"],
        ready["attachment_id"],
    ]
    assert items[0]["text"] == ""
    assert items[0]["filename"] == "note.md"
    assert items[1]["text"] == ATTACH_SENTINEL


# ----- 2. the pause: frame + detail -----


def test_paused_run_closes_stream_on_attachment_wait_with_minted_run_id(
    client: TestClient, env
) -> None:
    """Failure/normal boundary of the pause wire: the stream CLOSES on
    the ``attachment_wait`` frame — no terminal status/answer/citations,
    no turn row persisted — and the frame's payload is exactly the
    server-minted run id plus the pinned kind plus the pending ids. The
    run id is the checkpoint's own, which is what the resumer will
    drive."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    run_id, attachment_id, events = _pause_one_run(client, built, thread_id)

    assert [name for name, _ in events][-1] == "attachment_wait"
    for name, _ in events[:-1]:
        assert name not in {"status", "citations", "answer"}

    frame = events[-1][1]
    assert frame == {
        "run_id": run_id,
        "kind": "attachment_preparation",
        "attachment_ids": [attachment_id],
    }
    assert len(run_id) == 36
    assert built["graph"].checkpoint_run_id(thread_id) == run_id
    assert built["store"].list_runs(thread_id) == []
    # The gate asked for the work: the row left ``uploaded`` and one
    # durable job exists.
    assert built["service"].status(attachment_id).status == "parsing"
    assert len(built["service"].jobs.incomplete()) == 1
    _retire(client, thread_id)


def test_thread_detail_during_pause_names_the_pending_interrupt_kind(
    client: TestClient, env
) -> None:
    """Observability of the pause: while it waits, the thread detail
    reports ``pending_checkpoint`` true with kind
    ``attachment_preparation`` and the thread NOT in flight (the pause
    released the lock — that is what lets the resumer back in)."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    _pause_one_run(client, built, thread_id)

    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["pending_checkpoint"] is True
    assert detail["pending_interrupt_kind"] == "attachment_preparation"
    assert detail["in_flight"] is False
    assert detail["turns"] == []
    _retire(client, thread_id)


# ----- 3. end-to-end round trip -----


def test_round_trip_send_drain_resume_completes_same_run_with_hydrated_text(
    client: TestClient, env
) -> None:
    """The whole durable loop, hermetic: uploaded attachment + grounded
    send -> the job is enqueued (row ``parsing``) -> the drain runs it
    (row ``ready``, job ``succeeded``) -> the shared in-process resume
    drives the SAME checkpoint run id to completion, the hydrated text
    reaches the model prompt, and the one turn row is written under the
    paused run id."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    run_id, attachment_id, _events = _pause_one_run(client, built, thread_id)

    assert built["service"].drain_attachment_jobs() == 1
    record = built["service"].status(attachment_id)
    assert record.status == "ready" and record.searchable
    assert built["service"].jobs.incomplete() == ()

    resumed = _resume_one_attachment_wait(
        built["graph"],
        built["store"],
        built["service"],
        RunControl(),
        thread_id,
    )
    assert resumed is True

    # The hydrated attachment text reached the model on the SAME run.
    assert _model_saw(built, ATTACH_SENTINEL)
    turns = built["store"].list_runs(thread_id)
    assert [row.id for row in turns] == [run_id]
    assert turns[0].status == "done"
    assert turns[0].answer == built["answer"]
    assert turns[0].citations

    # The pause is over: nothing pending, no interrupt kind left to name.
    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["pending_checkpoint"] is False
    assert detail["pending_interrupt_kind"] is None
    # The resume above drove a THROWAWAY ``RunControl``, so the mounted
    # app's registry still holds the pause this send left. Only the thread
    # going away retires it.
    _retire(client, thread_id)


# ----- 4. failed preparation lands as a typed refusal -----


def test_preparation_failing_mid_run_reaches_row_as_typed_refusal(
    client: TestClient, env
) -> None:
    """Exception path across the pause: the send is accepted (the row is
    ``uploaded`` at send time), the drain's preparation FAILS through the
    real door (non-UTF-8 bytes -> ``attachment_not_text``), and the
    settled pause is still driven — the run record carries the typed
    refusal on the same run id, the model is never reached, and no stale
    ``ready`` or empty successful turn exists."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    attachment_id = _attach(
        client, thread_id, b"\xff\xfe\x00\x9f", filename="x.bin"
    ).json()["attachment_id"]
    response = _send(client, thread_id, attachment_ids=[attachment_id])
    events = _parse_sse_events(response.text)
    run_id = next(
        data["run_id"] for name, data in events if name == "attachment_wait"
    )

    assert built["service"].drain_attachment_jobs() == 1
    assert built["service"].status(attachment_id).status == "failed"

    assert (
        _resume_one_attachment_wait(
            built["graph"],
            built["store"],
            built["service"],
            RunControl(),
            thread_id,
        )
        is True
    )

    turns = built["store"].list_runs(thread_id)
    assert [row.id for row in turns] == [run_id]
    assert turns[0].status == "refuse"
    assert turns[0].status_reason == "attachment_not_text"
    assert turns[0].answer == ""
    assert turns[0].citations == []
    # Never an empty successful turn: the gate refused before the leaf.
    assert built["model"].seen_calls == []
    # No stale ready anywhere: the row and the job both say failed.
    record = built["service"].status(attachment_id)
    assert record.searchable is False
    assert [job.state for job in built["service"].jobs.incomplete()] == []
    _retire(client, thread_id)


# ----- 5. the retry door: ask only, never parse in-request -----


def test_retry_of_uploaded_row_answers_parsing_and_enqueues_one_job(
    client: TestClient, env
) -> None:
    """Normal path of the retry door: an ``uploaded`` row re-asks through
    the durable job and the door answers IMMEDIATELY with the wire
    record — status ``parsing``, still no version, no searchable text.
    One job exists; nothing was prepared inside the request."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    attachment_id = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()[
        "attachment_id"
    ]

    response = _retry(client, thread_id, attachment_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attachment_id"] == attachment_id
    assert body["status"] == "parsing"
    assert body["searchable"] is False
    assert body["version"] == 0
    assert body["failure_reason"] is None

    jobs = built["service"].jobs.incomplete()
    assert len(jobs) == 1
    assert jobs[0].attachment_id == attachment_id
    assert jobs[0].state == "pending"
    # No synchronous preparation: the row is not ready and no model or
    # drain ran.
    assert built["service"].status(attachment_id).status == "parsing"
    assert built["model"].seen_calls == []


def test_concurrent_double_retry_joins_the_one_durable_job(
    client: TestClient, env
) -> None:
    """Concurrency: two retry requests racing the same ``uploaded`` row
    start-or-join ONE job — the join is decided by the job store's key,
    not by read-then-write luck, so the incomplete count stays 1."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    attachment_id = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()[
        "attachment_id"
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_retry, client, thread_id, attachment_id)
        second = pool.submit(_retry, client, thread_id, attachment_id)
        responses = [first.result(timeout=10.0), second.result(timeout=10.0)]

    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "parsing"
    assert len(built["service"].jobs.incomplete()) == 1


def test_retry_of_failed_row_reopens_work_and_clears_the_reason(
    client: TestClient, env
) -> None:
    """The retry door's own state: a named ``failed`` row re-enqueues
    (the job store reopens the failed job) and the row leaves ``failed``
    with its retired reason cleared — the retried row never reads as
    still failed over freshly queued work."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    attachment_id = _attach(
        client, thread_id, b"\xff\xfe\x00\x9f", filename="x.bin"
    ).json()["attachment_id"]
    assert built["service"].prepare(attachment_id).status == "failed"

    response = _retry(client, thread_id, attachment_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "parsing"
    assert body["failure_reason"] is None

    jobs = built["service"].jobs.incomplete()
    assert len(jobs) == 1
    assert jobs[0].state == "pending"


# ----- 6. named reference refusals on send -----


def test_send_with_unknown_attachment_id_named_400(client: TestClient, env) -> None:
    """Invalid input: an id the store never held -> 400
    ``attachment_not_found`` before any graph work (no model call, no
    run row)."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    response = _send(client, thread_id, attachment_ids=["phantom-id"])
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_found"}
    assert built["model"].seen_calls == []
    assert built["store"].list_runs(thread_id) == []


def test_send_cannot_reference_another_threads_attachment(
    client: TestClient, env
) -> None:
    """Permission/identity across threads: a sibling thread naming the
    id gets the same reveal-nothing 400 ``attachment_not_found``."""
    env(wire_gate=True)
    thread_a = _new_thread(client)
    thread_b = _new_thread(client)
    attached = _attach(client, thread_a, ATTACH_SENTINEL.encode()).json()
    response = _send(client, thread_b, attachment_ids=[attached["attachment_id"]])
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_found"}


def test_send_naming_a_failed_row_is_attachment_not_ready(
    client: TestClient, env
) -> None:
    """Failure path at the door: a ``failed`` row in a NEW send is the
    named 400 ``attachment_not_ready`` — a repeated turn must not
    re-spend a named failure; only the retry door re-asks."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    attachment_id = _attach(
        client, thread_id, b"\xff\xfe\x00\x9f", filename="x.bin"
    ).json()["attachment_id"]
    assert built["service"].prepare(attachment_id).status == "failed"

    response = _send(client, thread_id, attachment_ids=[attachment_id])
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_ready"}
    assert built["model"].seen_calls == []
    # The refusal consumed nothing: the row is still the named failure.
    assert built["service"].status(attachment_id).status == "failed"


# ----- 7. the unchanged upload gate -----


class _BlockingPutStore:
    """Delegates to the real byte store; ``put`` waits for the test.

    The window the send gate must cover is exactly "``put`` has not
    returned yet", so the test has to hold that window open from the
    inside and be told when it is open (mirrors test_thread_attach.py).

    ``delete`` is delegated too, so the proxy is a whole stand-in for the
    seam it replaces rather than a partial one: the thread delete retires
    a paused run through ``attachments.delete_thread``, and a proxy missing
    this method answers ``attachment_cleanup_failed`` — a 500 raised
    BEFORE ``run_control.forget`` is reached, which leaves the pause in the
    registry and leaks it into every later file in the process.
    """

    def __init__(self, inner: InMemoryObjectStore) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def put(self, key, payload, *, content_type=None):  # type: ignore[no-untyped-def]
        self.entered.set()
        assert self.release.wait(
            timeout=5.0
        ), "the upload gate was never released; the upload is stuck"
        return self.inner.put(key, payload, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def get_pointer(self, key: str):
        return self.inner.get_pointer(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)


def test_send_still_409_while_an_upload_is_in_flight(
    client: TestClient, env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression pin of the unchanged gate: while this thread's bytes
    are still being stored, a send is the named 409 with NO run started
    — and the same send proceeds once the upload lands."""
    built = env(wire_gate=True)
    thread_id = _new_thread(client)
    gate = _BlockingPutStore(built["objects"])
    monkeypatch.setattr(built["service"], "objects", gate)

    in_flight = ThreadPoolExecutor(max_workers=1)
    started = in_flight.submit(
        _attach, client, thread_id, ATTACH_SENTINEL.encode()
    )
    assert gate.entered.wait(timeout=5.0), "upload never reached the byte store"

    blocked = _send(client, thread_id)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json() == {"error": "attachment_upload_in_progress"}
    assert built["model"].seen_calls == []
    assert built["store"].list_runs(thread_id) == []

    gate.release.set()
    landed = started.result(timeout=10.0)
    in_flight.shutdown(wait=True)
    assert landed.status_code == 200

    # T2f interplay: the released upload's stored row is then accepted
    # by the send (no 400) and pauses on the gate like any uploaded id.
    released = _send(client, thread_id, attachment_ids=[landed.json()["attachment_id"]])
    assert released.status_code == 200, released.text
    assert any(name == "attachment_wait" for name, _ in _parse_sse_events(released.text))
    _retire(client, thread_id)
