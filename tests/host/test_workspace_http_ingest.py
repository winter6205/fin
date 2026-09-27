"""Tests for the durable corpus-ingest door (POST/GET ``/api/corpus/ingest``).

The route is the workspace's corpus-ingest door. As of the permanent-upload
amendment it is **accept-and-persist**: a 200 means the four accept-time items
are durable (raw object, metadata pointer, ``pending`` job record,
``(document_id, version)`` identity) and nothing was parsed, 切分 or pushed.
The outcome is polled at ``GET /api/corpus/ingest/{job_id}``, which returns the
persisted state, never a constant. Coverage follows the boundary classes:

* accept happy path — supported upload -> 200 accepted, state pending, no push;
* identity — ``company``+``period`` derive ``{TICKER}-{FYQ}``; missing both is
  the named ``document_id_missing`` 422; the filename never names the document;
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
from fin.retrieve.fake_push_port import FakePushPort
from fin.host.workspace_http import get_ingest_service

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
    ) -> None:
        self.endpoint = endpoint
        self.objects = objects
        self.pointers = pointers
        self.states = states
        self.push = push


def _install(
    http_app: FastAPI,
    *,
    objects: Any = None,
    pointers: Any = None,
    states: Any = None,
    max_bytes: int | None = None,
) -> _Stack:
    objects = objects if objects is not None else _RecordingObjectStore()
    pointers = (
        pointers if pointers is not None else InMemoryMetadataStore()
    )
    states = (
        states if states is not None else InMemoryIngestStateStore()
    )
    push = FakePushPort()
    kwargs: dict[str, Any] = {}
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    endpoint = IngestEndpoint(
        push_port=push,
        invalidation_port=push,
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
        **kwargs,
    )
    http_app.dependency_overrides[get_ingest_service] = lambda: endpoint
    return _Stack(endpoint, objects, pointers, states, push)


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
        data=form if form is not None else {"company": "AAPL", "period": "2023Q3"},
    )


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
    assert body["document_id"] == "AAPL-2023Q3"
    assert body["job_id"] == body["document_id"]
    assert body["version"]

    assert len(stack.objects.puts) == 1
    key = stack.objects.puts[0]
    assert key.endswith(f"/{body['version']}")
    assert stack.push.written_total() == 0
    assert stack.states.get_job(body["document_id"]).state is IngestState.PENDING


def test_document_id_is_ticker_plus_fiscal_period_and_case_normalizing(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = _post(
        client, form={"company": "aapl", "period": "2022q3"}
    )
    assert response.status_code == 200
    assert response.json()["document_id"] == "AAPL-2022Q3"


def test_explicit_document_id_wins_over_metadata(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app)
    response = _post(
        client,
        form={"document_id": "0000950123-22-000100", "company": "AAPL", "period": "2022Q3"},
    )
    assert response.status_code == 200
    assert response.json()["document_id"] == "0000950123-22-000100"


# ----- identity failures (nothing persisted) ------------------------------


def test_missing_metadata_pair_is_document_id_missing_422(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(client, form={})

    assert response.status_code == 422
    assert response.json()["error"] == "document_id_missing"
    assert stack.objects.puts == []
    assert stack.push.written_total() == 0


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
        form={"company": "AAPL", "period": "2023Q3", "format": "rtf"},
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    assert stack.objects.puts == []


def test_unknown_doc_type_is_a_named_422(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    response = _post(
        client, form={"company": "AAPL", "period": "2023Q3", "doc_type": "bogus"}
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


def test_different_version_while_in_flight_is_a_typed_conflict(
    http_app: FastAPI, client: TestClient
) -> None:
    stack = _install(http_app)
    accepted = _post(client)
    doc_id = accepted.json()["document_id"]
    before = list(stack.objects.puts)

    conflict = _post(client, data=b"different bytes entirely for the door\n")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "concurrent_run"
    assert stack.objects.puts == before
    assert stack.states.get_job(doc_id).state is IngestState.PENDING


def test_replacing_a_ready_document_is_accepted_and_reopens_the_job(
    http_app: FastAPI, client: TestClient
) -> None:
    # The settled replacement: a different version of a ``ready`` source is
    # accepted, the document's one job reopens as a fresh ``pending``
    # generation, and the status poll reports the new version — never a
    # second logical version coexisting with the replaced one.
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
    assert body["job_id"] == accepted.json()["job_id"] == doc_id
    assert body["state"] == "pending"
    assert body["version"] != accepted.json()["version"]
    assert stack.states.get_job(doc_id).state is IngestState.PENDING
    polled = client.get(f"/api/corpus/ingest/{doc_id}")
    assert polled.status_code == 200
    assert polled.json()["version"] == body["version"]
    assert polled.json()["state"] == "pending"
    assert polled.json()["text_ready"] is False


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
        states.get_job("AAPL-2023Q3")


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
        states.get_job("AAPL-2023Q3")


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
    assert ready["graph"] is None


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
