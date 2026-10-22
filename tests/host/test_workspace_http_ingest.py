"""Tests for the durable corpus-ingest door (POST/GET ``/api/corpus/ingest``).

The route is the workspace's corpus-ingest door. As of the permanent-upload
amendment it is **accept-and-persist**: a 200 means the four accept-time items
are durable (raw object, metadata pointer, ``pending`` job record,
``(document_id, version)`` identity) and nothing was parsed, 切分 or pushed.
The outcome is polled at ``GET /api/corpus/ingest/{job_id}``, which returns the
persisted state, never a constant. Coverage follows the boundary classes:

* accept happy path — a file-only upload -> 200 accepted, state pending, no
  parse or push;
* identity — the trusted scope plus the content digest derive the id; optional
  ``title``/``company``/``period`` metadata never changes it; a caller-supplied
  ``document_id`` is the named ``document_id_not_allowed`` 422;
* empty class — missing file part is a named 4xx;
* negative class — unsupported format is a named 415 refused BEFORE any write;
* overflow class — one byte over the pinned 50 MiB cap is a named 413, exact
  cap is accepted;
* idempotency/conflict — the same ``(document_id, version)`` joins the job with
  no second write; a different version while in flight is ``concurrent_run``;
  replacing a ``ready`` document is accepted and reopens the one job as a
  fresh ``pending`` generation naming only the new version;
* exception class — an accept-time persistence outage is a typed 5xx with no
  accepted job; a failed compensating delete is observable;
* status read — GET returns the real persisted state, ``unknown_job`` for an
  unknown id, and never renders a push success as graph/C ready.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.corpus.index_job import IndexJobLedger, IndexJobTracker
from fin.corpus.ingest_endpoint import IngestEndpoint, MAX_INGEST_BYTES
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.ingest_state_errors import IngestStepError
from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataStoreError,
)
from fin.corpus.object_store import (
    InMemoryObjectStore,
    ObjectPointer,
    ObjectStoreUnavailableError,
)
from fin.corpus.upload_metadata import derive_document_identity
from fin.retrieve.fake_index_job_port import FakeIndexJobPort
from fin.retrieve.fake_push_port import FakePushPort
from fin.host.workspace_http import get_index_job_tracker, get_ingest_service

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MD_BYTES = (
    b"# Heading\n\nA first paragraph with enough body text to chunk.\n"
)


class _RecordingObjectStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def put(
        self, key: str, payload: bytes, *, content_type: str | None = None
    ) -> ObjectPointer:
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)

    def delete(self, key: str) -> None:
        self.deletes.append(key)
        super().delete(key)


class _FailingPutObjectStore(InMemoryObjectStore):
    def put(
        self, key: str, payload: bytes, *, content_type: str | None = None
    ) -> ObjectPointer:
        raise ObjectStoreUnavailableError("object store is down")


class _FailingDeleteObjectStore(InMemoryObjectStore):
    def delete(self, key: str) -> None:
        raise ObjectStoreUnavailableError("delete is down")


class _FailingPointerStore(InMemoryMetadataStore):
    def put_pointer(self, document_id: str, pointer: Any) -> None:
        raise MetadataStoreError("pointer table is unavailable")


class _FailingStateStore(InMemoryIngestStateStore):
    def create_job(
        self, document_id: str, *, actor: str | None = None
    ) -> Any:
        raise RuntimeError("job table is unavailable")


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


class _Stack:
    def __init__(
        self,
        endpoint: IngestEndpoint,
        objects: Any,
        pointers: Any,
        states: InMemoryIngestStateStore,
        push: FakePushPort,
        index_port: FakeIndexJobPort,
        index_tracker: IndexJobTracker,
    ) -> None:
        self.endpoint = endpoint
        self.objects = objects
        self.pointers = pointers
        self.states = states
        self.push = push
        self.index_port = index_port
        self.index_tracker = index_tracker


def _install(
    http_app: FastAPI,
    *,
    objects: Any = None,
    pointers: Any = None,
    states: Any = None,
    max_bytes: int | None = None,
    on_ingested: Any = None,
) -> _Stack:
    objects = objects if objects is not None else _RecordingObjectStore()
    pointers = (
        pointers if pointers is not None else InMemoryMetadataStore()
    )
    states = (
        states if states is not None else InMemoryIngestStateStore()
    )
    push = FakePushPort()
    kwargs: dict[str, Any] = {"corpus_scope_id": "test-scope"}
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if on_ingested is not None:
        kwargs["on_ingested"] = on_ingested
    endpoint = IngestEndpoint(
        push_port=push,
        invalidation_port=push,
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
        **kwargs,
    )
    # The status route's graph face is the same process singleton the
    # worker would take; here it is the contract fake over an in-
    # process ledger, so route tests never reach a live service.
    index_port = FakeIndexJobPort()
    index_tracker = IndexJobTracker(index_port, IndexJobLedger())
    http_app.dependency_overrides[get_ingest_service] = lambda: endpoint
    http_app.dependency_overrides[get_index_job_tracker] = (
        lambda: index_tracker
    )
    return _Stack(
        endpoint, objects, pointers, states, push, index_port, index_tracker
    )


def _post(
    client: TestClient,
    *,
    name: str = "sample.md",
    data: bytes = _MD_BYTES,
    content_type: str = "text/markdown",
    form: dict[str, str] | None = None,
):
    return client.post(
        "/api/corpus/ingest",
        files={"file": (name, data, content_type)},
        data=form if form is not None else {},
    )


def _expected_id(data: bytes = _MD_BYTES) -> str:
    return derive_document_identity(
        data, corpus_scope_id="test-scope", filename=None
    ).document_id


# ----- accept happy path --------------------------------------------------


def test_accepted_upload_persists_the_four_items_and_pushes_nothing(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["state"] == "pending"
    assert body["document_id"] == _expected_id()
    assert body["document_id"].startswith("doc_")
    assert body["job_id"] == body["document_id"]
    assert body["version"]

    assert len(stack.objects.puts) == 1
    key = stack.objects.puts[0]
    assert key.endswith(f"/{body['version']}")
    assert stack.push.written_total() == 0
    assert stack.states.get_job(body["document_id"]).state is IngestState.PENDING


def test_optional_metadata_never_changes_the_identity(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = _post(
        client,
        form={
            "company": "aapl",
            "period": "2022q3",
            "title": "Quarterly report",
            "doc_type": "narrative",
        },
    )
    assert response.status_code == 200
    assert response.json()["document_id"] == _expected_id()


def test_explicit_document_id_is_rejected_before_any_write(
    http_app: FastAPI, client: TestClient
) -> None:
    """A caller-chosen id is refused before writes, legacy flag or not."""
    stack = _install(http_app)
    for legacy in ({}, {"generic": "true"}, {"generic": "false"}):
        response = _post(
            client,
            form={
                "document_id": "0000950123-22-000100",
                "company": "AAPL",
                "period": "2022Q3",
                **legacy,
            },
        )

        assert response.status_code == 422
        assert response.json()["error"] == "document_id_not_allowed"
        assert stack.objects.puts == []
        assert stack.push.written_total() == 0


# ----- identity failures (nothing persisted) ------------------------------


def test_the_legacy_generic_field_cannot_select_another_path(
    http_app: FastAPI, client: TestClient
) -> None:
    """An old client's ``generic`` field is ignored: both values (and its
    absence) accept the same bytes under the same content-derived id."""
    _install(http_app)
    absent = _post(client)
    for value in ("true", "false"):
        response = _post(client, form={"generic": value})
        assert response.status_code == 200
        assert response.json()["document_id"] == absent.json()["document_id"]
        assert response.json()["version"] == absent.json()["version"]


# ----- empty class --------------------------------------------------------


def test_missing_file_part_is_a_named_4xx(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = client.post("/api/corpus/ingest")
    assert response.status_code == 422
    assert response.json()["error"] == "missing_file"


def test_zero_byte_upload_is_accepted_durably_and_parses_later(
    http_app: FastAPI, client: TestClient
) -> None:
    """Empty-content detection is a byte-dependent parse outcome (T3); the
    accept door persists the bytes and defers the failure to the job."""
    stack = _install(http_app)
    response = _post(client, name="empty.txt", data=b"", content_type="text/plain")

    assert response.status_code == 200
    assert response.json()["state"] == "pending"
    assert len(stack.objects.puts) == 1
    assert stack.push.written_total() == 0


# ----- negative class (refused before acceptance) -------------------------


def test_unsupported_extension_is_a_named_415_not_a_silent_200(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(
        client, name="report.rtf", data=b"some bytes", content_type="application/rtf"
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    assert stack.objects.puts == []
    assert stack.push.written_total() == 0


def test_unsupported_declared_format_is_a_named_415(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(
        client,
        name="blob.bin",
        data=b"some bytes",
        content_type="application/octet-stream",
        form={"format": "rtf"},
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    assert stack.objects.puts == []


def test_unknown_doc_type_is_a_named_422(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(
        client, form={"doc_type": "bogus"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_doc_type"
    assert stack.objects.puts == []


# ----- overflow class -----------------------------------------------------


def test_exact_cap_is_accepted_and_one_byte_over_is_413(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    assert MAX_INGEST_BYTES == 52_428_800

    ok = _post(client, name="exact.txt", data=b"x" * MAX_INGEST_BYTES, content_type="text/plain")
    assert ok.status_code == 200
    assert ok.json()["state"] == "pending"

    over = _post(
        client, name="over.txt", data=b"x" * (MAX_INGEST_BYTES + 1), content_type="text/plain"
    )
    assert over.status_code == 413
    assert over.json()["error"] == "ingest_upload_too_large"
    assert len(stack.objects.puts) == 1


def test_declared_oversize_content_length_refused_before_the_service_runs(
    http_app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = _install(http_app, max_bytes=16)

    def _tripwire(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("submit_durable ran despite oversize body")

    monkeypatch.setattr(stack.endpoint, "submit_durable", _tripwire)
    response = _post(client, name="big.txt", data=b"x" * 512, content_type="text/plain")
    assert response.status_code == 413
    assert response.json()["error"] == "ingest_upload_too_large"
    assert stack.objects.puts == []


def test_ingest_bounded_read_stops_at_the_cap() -> None:
    import io

    from fin.host.workspace_http import _read_bounded_upload

    assert _read_bounded_upload(io.BytesIO(b"x" * 17), 16) is None
    assert _read_bounded_upload(io.BytesIO(b"x" * 16), 16) == b"x" * 16
    assert _read_bounded_upload(io.BytesIO(b"x" * 4096), 0) == b"x" * 4096


# ----- idempotency / conflict --------------------------------------------


def test_same_version_resubmission_joins_the_job_without_a_second_write(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    first = _post(client)
    second = _post(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["version"] == first.json()["version"]
    assert len(stack.objects.puts) == 1


def test_different_bytes_are_a_separate_document_not_a_conflict(
    http_app: FastAPI, client: TestClient
) -> None:
    """Content identity makes different bytes a different document: the
    public door has no way to claim a second version of one document."""
    stack = _install(http_app)
    first = _post(client)
    second = _post(client, data=b"different bytes entirely for the door\n")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["document_id"] != first.json()["document_id"]
    assert second.json()["version"] != first.json()["version"]
    assert len(stack.objects.puts) == 2
    assert stack.states.get_job(first.json()["document_id"]).state is (
        IngestState.PENDING
    )
    assert stack.states.get_job(second.json()["document_id"]).state is (
        IngestState.PENDING
    )


def test_a_ready_document_and_a_new_version_coexist_as_separate_documents(
    http_app: FastAPI, client: TestClient
) -> None:
    """Existing records are never renamed or merged: new bytes land under
    their own content-derived id while the ready document stays ready."""
    stack = _install(http_app)
    accepted = _post(client)
    doc_id = accepted.json()["document_id"]
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(doc_id, state)

    response = _post(client, data=b"a wholly new version of the ready doc\n")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] != doc_id
    assert body["state"] == "pending"
    assert body["version"] != accepted.json()["version"]
    polled = client.get(f"/api/corpus/ingest/{doc_id}")
    assert polled.status_code == 200
    assert polled.json()["state"] == "ready"
    assert polled.json()["text_ready"] is True


# ----- accept-time persistence outages -----------------------------------


def test_object_store_outage_is_a_typed_503_with_no_accepted_job(
    http_app: FastAPI, client: TestClient
) -> None:
    states = InMemoryIngestStateStore()
    _install(http_app, objects=_FailingPutObjectStore(), states=states)
    response = _post(client)
    assert response.status_code == 503
    assert response.json()["error"] == "durable_commit_failed"
    with pytest.raises(Exception):
        states.get_job(_expected_id())


def test_pointer_commit_failure_leaves_no_accepted_job_and_deletes_the_object(
    http_app: FastAPI, client: TestClient
) -> None:
    objects = _RecordingObjectStore()
    states = InMemoryIngestStateStore()
    _install(http_app, objects=objects, pointers=_FailingPointerStore(), states=states)
    response = _post(client)
    assert response.status_code == 503
    assert response.json()["error"] == "durable_commit_failed"
    assert len(objects.puts) == 1
    assert objects.deletes == objects.puts
    with pytest.raises(Exception):
        states.get_job(_expected_id())


def test_state_record_failure_rolls_back_and_deletes_the_object(
    http_app: FastAPI, client: TestClient
) -> None:
    objects = _RecordingObjectStore()
    _install(
        http_app,
        objects=objects,
        pointers=InMemoryMetadataStore(),
        states=_FailingStateStore(),
    )
    response = _post(client)
    assert response.status_code == 503
    assert response.json()["error"] == "durable_commit_failed"
    assert objects.deletes == objects.puts


def test_a_failing_compensating_delete_is_observable(
    http_app: FastAPI, client: TestClient
) -> None:
    objects = _FailingDeleteObjectStore()
    _install(http_app, objects=objects, pointers=_FailingPointerStore())
    response = _post(client)
    assert response.status_code == 500
    assert response.json()["error"] == "object_cleanup_failed"


# ----- status read -------------------------------------------------------


def test_status_returns_the_real_persisted_state_not_a_constant(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    accepted = _post(client).json()
    doc_id = accepted["document_id"]

    poll = client.get(f"/api/corpus/ingest/{doc_id}")
    assert poll.status_code == 200
    body = poll.json()
    assert body["state"] == "pending"
    assert body["job_id"] == doc_id
    assert body["version"] == accepted["version"]
    assert body["text_ready"] is False
    assert body["graph"] is None

    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(doc_id, state)

    ready = client.get(f"/api/corpus/ingest/{doc_id}").json()
    assert ready["state"] == "ready"
    assert ready["text_ready"] is True
    # Null graph on a ready job with no index job ever submitted: the
    # pass-through says "nothing claimed", never a graph/C readiness.
    assert ready["graph"] is None


def test_status_returns_the_graph_pass_through_once_jobs_exist(
    http_app: FastAPI, client: TestClient
) -> None:
    """After the handoff submitted, the route reports the polled job
    rows and the separate corpus-wide ``community_index_state`` — pure
    pass-through, re-polled on every GET, never derived by fin."""
    stack = _install(http_app)
    doc_id = _post(client).json()["document_id"]

    # The handoff the worker performs on a ready landing, done directly
    # here so this test is about the route's read face only.
    stack.index_tracker.submit_after_push(
        doc_id, {"src-a": "ident-a", "src-b": "ident-b"}
    )

    poll = client.get(f"/api/corpus/ingest/{doc_id}").json()
    assert poll["state"] == "pending"
    # Non-ready jobs keep graph null even with ledger entries: text
    # readiness gates the graph face, not the other way around.
    assert poll["graph"] is None

    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(doc_id, state)
    polls_before = stack.index_port.poll_calls

    body = client.get(f"/api/corpus/ingest/{doc_id}").json()
    graph = body["graph"]
    assert graph is not None
    assert sorted(job["source_ref"] for job in graph["jobs"]) == [
        "src-a",
        "src-b",
    ]
    assert all(job["job_id"] for job in graph["jobs"])
    assert graph["community_index_state"] == {
        "status": "in_progress",
        "failure_code": None,
    }
    assert graph["error"] is None
    # The GET is the tracker's designed pure-read poll.
    assert stack.index_port.poll_calls == polls_before + 2

    # Service-side progress rides straight through the next poll.
    job_id = graph["jobs"][0]["job_id"]
    stack.index_port.advance_job(job_id, "ready")
    stack.index_port.set_community_index_state("ready")
    advanced = client.get(f"/api/corpus/ingest/{doc_id}").json()["graph"]
    assert advanced["community_index_state"] == {
        "status": "ready",
        "failure_code": None,
    }
    assert {job["status"] for job in advanced["jobs"]} == {
        "ready",
        "in_progress",
    }


def test_status_survives_a_poll_failure_with_stale_state_and_error(
    http_app: FastAPI, client: TestClient
) -> None:
    """An unreachable index-job service never 500s the status door: the
    persisted (stale) state stays visible beside the named error."""
    stack = _install(http_app)
    doc_id = _post(client).json()["document_id"]
    stack.index_tracker.submit_after_push(doc_id, {"src-a": "ident-a"})
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(doc_id, state)

    # One successful poll first: the state it confirms is what the
    # door must keep visible when polling later breaks.
    client.get(f"/api/corpus/ingest/{doc_id}")

    stack.index_port.inject_poll_failure("index_job_unavailable", sticky=True)

    response = client.get(f"/api/corpus/ingest/{doc_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["text_ready"] is True
    graph = body["graph"]
    assert graph["error"]["code"] == "index_job_unavailable"
    # Stale-visible: the last persisted poll, never an empty dict
    # presented as ready and never a fin-derived claim.
    assert graph["community_index_state"] == {
        "status": "in_progress",
        "failure_code": None,
    }


def test_status_marks_a_crashing_graph_poll_with_a_named_error(
    http_app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph_status that raises is visible as graph_status_failed —
    never a silent null that reads like "nothing ever submitted"."""
    stack = _install(http_app)
    doc_id = _post(client).json()["document_id"]
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(doc_id, state)

    def _boom(document_id: str) -> None:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(stack.index_tracker, "graph_status", _boom)

    response = client.get(f"/api/corpus/ingest/{doc_id}")
    assert response.status_code == 200
    graph = response.json()["graph"]
    assert graph["error"]["code"] == "graph_status_failed"
    assert graph["jobs"] == []
    assert graph["community_index_state"] is None


def test_status_of_a_failed_job_carries_the_typed_error(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    doc_id = _post(client).json()["document_id"]
    stack.states.transition(doc_id, IngestState.FETCHING)
    with pytest.raises(IngestStepError):
        stack.states.fail_step(
            doc_id, step="extracting", cause=ValueError("bad bytes")
        )

    body = client.get(f"/api/corpus/ingest/{doc_id}").json()
    assert body["state"] == "failed"
    assert body["error_type"]
    assert body["error_message"] == "bad bytes"
    assert body["text_ready"] is False


def test_status_of_an_unknown_job_is_a_named_404(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = client.get("/api/corpus/ingest/NOPE-2099Q9")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_job"


# ----- product dependency wiring ----------------------------------------


def test_ingest_service_dependency_defaults_to_env_kind(
    http_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host.workspace_http import _build_default_ingest_service
    from fin.retrieve.mcp_push_port import MCPPushPort

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    service = _build_default_ingest_service()
    assert isinstance(service, IngestEndpoint)
    assert isinstance(service._push_port, FakePushPort)

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")
    service = _build_default_ingest_service()
    assert isinstance(service._push_port, MCPPushPort)


def test_default_ingest_service_wires_the_durable_ports_in_both_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product assembly wires object/pointer/state ports for BOTH
    adapter kinds so the durable accept runs against the real port contracts,
    not a second weaker one, and keeps ``on_ingested`` for the deferred chain."""
    from fin.host.workspace_http import _build_default_ingest_service

    for kind in ("fake", "mcp"):
        monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", kind)
        service = _build_default_ingest_service()
        assert service.state_store is not None
        assert service._on_ingested is not None


def test_the_default_service_injects_a_nonempty_corpus_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity needs a scope; the product assembly never leaves it blank."""
    from fin.host.workspace_http import _build_default_ingest_service

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.delenv("FIN_CORPUS_SCOPE_ID", raising=False)
    service = _build_default_ingest_service()

    assert service._corpus_scope_id
    assert service._corpus_scope_id == "fin-default"


# ----- single public flow -------------------------------------------------


def test_a_file_only_upload_needs_no_metadata(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(client, name="Team Handbook.md")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["document_id"] == _expected_id()
    assert "team" not in body["document_id"]
    assert len(stack.objects.puts) == 1


def test_blank_optional_metadata_is_absent_not_an_error(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = _post(
        client,
        form={"title": "", "company": "   ", "period": ""},
    )

    assert response.status_code == 200
    assert response.json()["document_id"] == _expected_id()


def test_wildcard_metadata_is_refused_before_any_write(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(client, name="a.md", form={"company": "Acme*"})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_upload_metadata"
    assert stack.objects.puts == []


def test_acceptance_returns_before_execution_and_status_reads_do_not_execute(
    http_app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request only accepts: parse/chunk/push run off it, and a status
    poll is observational rather than the thing that starts the work."""
    stack = _install(http_app)
    ran: list[str] = []
    monkeypatch.setattr(
        stack.endpoint, "run_durable", lambda doc_id: ran.append(doc_id)
    )

    accepted = _post(client)
    assert accepted.status_code == 200
    assert ran == []
    assert stack.push.written_total() == 0

    polled = client.get(f"/api/corpus/ingest/{accepted.json()['document_id']}")
    assert polled.status_code == 200
    assert ran == []
    assert stack.push.written_total() == 0


def test_an_unconfigured_scope_refuses_rather_than_minting(
    http_app: FastAPI, client: TestClient
) -> None:
    """The route's service is always built with a scope; a service built
    without one refuses the upload rather than minting an identity.

    The refusal is a ``ValueError`` from the identity derivation, which is
    a wiring bug rather than a named upload failure, so it is an unhandled
    500 — the assertion is that it never becomes a silent 200 with an
    unscoped identity."""
    push = FakePushPort()
    endpoint = IngestEndpoint(
        push_port=push,
        object_store=InMemoryObjectStore(),
        metadata_store=InMemoryMetadataStore(),
        state_store=InMemoryIngestStateStore(),
    )
    http_app.dependency_overrides[get_ingest_service] = lambda: endpoint

    with pytest.raises(ValueError):
        _post(client, name="a.md")


# ----- warning on poll ----------------------------------------------------


def test_a_catalog_failure_polls_a_typed_warning_over_http(
    http_app: FastAPI, client: TestClient
) -> None:
    from fin.corpus.errors import CatalogRowError

    def boom(doc: Any) -> None:
        raise CatalogRowError("title is empty")

    stack = _install(http_app, on_ingested=boom)
    accepted = _post(client).json()
    doc_id = accepted["document_id"]
    stack.endpoint.run_durable(doc_id)

    body = client.get(f"/api/corpus/ingest/{doc_id}").json()
    assert body["state"] == "cataloging"
    assert body["catalog_warning"] == {
        "code": "catalog_publication_failed",
        "attempts": 1,
        "failure_code": "catalog_row_invalid",
        "failure_stage": "validate",
    }
    assert body["catalog_warning_count"] == 1


def test_a_healthy_poll_carries_no_catalog_warning(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    accepted = _post(client).json()

    body = client.get(f"/api/corpus/ingest/{accepted['document_id']}").json()

    assert body["catalog_warning"] is None
    assert body["catalog_warning_count"] == 0
