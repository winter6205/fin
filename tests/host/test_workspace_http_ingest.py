"""Tests for the ``POST /api/corpus/ingest`` route (corpus-serve-ingest T3).

The route is the workspace's corpus-ingest door (T5 will trigger it
from the web). It appends to the existing router — every other route
and request model is untouched. Coverage follows the five boundary
classes on this surface:

* happy path — a supported upload is observable as a push record on
  the ``FakePushPort`` behind the overridden dependency (SC5);
* empty class — missing file part / zero-byte body are named 4xx;
* negative class — unsupported format is a named 415, never an empty
  200 with a silent skip (SC7);
* overflow class — an oversize upload is a named 413;
* exception class — MCP/push down and invalidation-down are typed
  503s carrying the stable error ``code`` (SC8).

The concurrent class on this surface is the existing
``concurrent_run`` 409 on the send path — untouched here by
construction (this file does not modify any existing route).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.corpus.ingest_endpoint import IngestEndpoint, IngestedDocument
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.invalidation_port.errors import (
    InvalidationServiceUnavailableError,
)
from fin.retrieve.mcp_push_port import MCPPushPort
from fin.host.workspace_http import get_ingest_service

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MD_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)

_MD_BYTES = (
    b"# Heading\n\nA first paragraph with enough body text to chunk.\n"
)


class _DownInvalidationPort:
    """Invalidation seam that answers with the typed unavailable EXIT."""

    def invalidate(self, targets: Any) -> dict[str, Any]:
        raise InvalidationServiceUnavailableError(
            "invalidation service is down"
        )


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _install(http_app: FastAPI, service: IngestEndpoint) -> None:
    http_app.dependency_overrides[get_ingest_service] = lambda: service


def _service(push_port: Any = None, **kwargs: Any) -> IngestEndpoint:
    push_port = push_port if push_port is not None else FakePushPort()
    kwargs.setdefault("invalidation_port", push_port)
    return IngestEndpoint(push_port=push_port, **kwargs)


# ----- SC5: supported fixture ingest observable as push ----------------


def test_ingest_route_pushes_and_answers_the_outcome(
    http_app: FastAPI, client: TestClient
) -> None:
    fake = FakePushPort()
    _install(http_app, _service(fake))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["fmt"] == "md"
    assert body["document_id"] == "AAPL-2023Q3"  # company + period
    assert body["version"]  # sha256 of the source bytes
    assert body["block_count"] == len(body["block_ids"]) > 0
    assert body["invalidation"] is None
    assert fake.block_total() == body["block_count"]
    recalled = fake.recall(body["block_ids"][0])
    assert recalled["text"]
    assert recalled["metadata"]["fmt"] == "md"


def test_ingest_route_with_real_md_fixture_lands_every_block(
    http_app: FastAPI, client: TestClient
) -> None:
    fake = FakePushPort()
    _install(http_app, _service(fake))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("quarterly.md", _MD_SAMPLE.read_bytes(), "text/markdown")},
    )
    assert response.status_code == 200
    body = response.json()
    assert fake.block_total() == body["block_count"]


def test_second_identical_upload_reports_duplicates(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app, _service(FakePushPort()))
    files = {"file": ("sample.md", _MD_BYTES, "text/markdown")}
    first = client.post("/api/corpus/ingest", files=files)
    second = client.post("/api/corpus/ingest", files=files)

    assert first.json()["written"] > 0
    assert second.json()["written"] == 0
    assert second.json()["duplicates"] == first.json()["block_count"]
    assert second.json()["block_ids"] == first.json()["block_ids"]


# ----- empty class ------------------------------------------------------


def test_missing_file_part_is_a_named_4xx(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app, _service(FakePushPort()))
    response = client.post("/api/corpus/ingest")
    assert response.status_code == 422
    assert response.json()["error"] == "missing_file"


def test_zero_byte_upload_is_a_named_400(
    http_app: FastAPI, client: TestClient
) -> None:
    fake = FakePushPort()
    _install(http_app, _service(fake))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "empty_source"
    assert fake.block_total() == 0


# ----- negative class (SC7) ---------------------------------------------


def test_unsupported_format_is_a_named_415_not_a_silent_200(
    http_app: FastAPI, client: TestClient
) -> None:
    fake = FakePushPort()
    _install(http_app, _service(fake))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("report.rtf", b"some bytes", "application/rtf")},
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    assert fake.block_total() == 0


def test_unsupported_declared_format_is_a_named_415(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app, _service(FakePushPort()))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("blob.bin", b"some bytes", "application/octet-stream")},
        data={"format": "rtf"},
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"


def test_unknown_doc_type_is_a_named_4xx(
    http_app: FastAPI, client: TestClient
) -> None:
    _install(http_app, _service(FakePushPort()))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"doc_type": "bogus"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_doc_type"


# ----- overflow class ---------------------------------------------------


def test_oversize_upload_is_a_named_413(
    http_app: FastAPI, client: TestClient
) -> None:
    fake = FakePushPort()
    _install(http_app, _service(fake, max_bytes=16))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("big.txt", b"x" * 64, "text/plain")},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "ingest_upload_too_large"
    assert fake.block_total() == 0


# ----- exception class (SC8: MCP down typed) ----------------------------


def test_push_unavailable_is_a_typed_503(
    http_app: FastAPI, client: TestClient
) -> None:
    # command=None is the documented MCP-down shape: the transport
    # raises OSError and the adapter names it push_service_unavailable.
    endpoint = IngestEndpoint(push_port=MCPPushPort(command=None))
    _install(http_app, endpoint)
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.txt", b"body text\n", "text/plain")},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "push_service_unavailable"


def test_invalidation_failure_after_push_is_a_typed_503(
    http_app: FastAPI, client: TestClient
) -> None:
    captured: list[Any] = []
    fake = FakePushPort()
    service = IngestEndpoint(
        push_port=fake,
        invalidation_port=_DownInvalidationPort(),
        # constructor-level provider: the seam the leader wires to the
        # T2 catalog after the merge
        previous_records=lambda: captured,
    )
    _install(http_app, service)

    # Seed the "previous" records directly through the service: an
    # ingest whose provider is still empty plans no invalidation.
    first = service.ingest(b"first version body\n", filename="DOC-1.txt")
    captured.extend(first.records)

    # Re-ingest different bytes under the same document_id via HTTP:
    # push lands on the fake, then the invalidation stub answers with
    # the typed unavailable failure.
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("DOC-1.txt", b"second version body\n", "text/plain")},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "invalidation_service_unavailable"
    # The push itself landed (idempotent retry is the recovery).
    assert fake.block_total() > first.block_count


def test_ingest_service_dependency_defaults_to_env_kind(
    http_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The un-overridden dependency builds the env-selected service."""
    from fin.host.workspace_http import _build_default_ingest_service

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    service = _build_default_ingest_service()
    assert isinstance(service, IngestEndpoint)
    assert isinstance(service._push_port, FakePushPort)

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")
    service = _build_default_ingest_service()
    assert isinstance(service._push_port, MCPPushPort)


# ----- on_ingested hook wiring survives the route -----------------------


def test_route_still_reports_ingested_with_hook(
    http_app: FastAPI, client: TestClient
) -> None:
    seen: list[IngestedDocument] = []
    _install(http_app, _service(FakePushPort(), on_ingested=seen.append))
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )
    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].company == "AAPL"
    assert seen[0].period == "2023Q3"
    assert seen[0].page_count == response.json()["unit_count"]
