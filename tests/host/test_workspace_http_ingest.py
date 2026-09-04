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
    # The field is always present; an overridden hook-less service has
    # no catalog concern, so a clean ingest stays warning-free.
    assert body["catalog_warning"] is None
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


def test_oversize_content_length_refused_before_the_service_runs(
    http_app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input-boundary fix (route layer): a DECLARED oversize
    Content-Length is the named 413 BEFORE the spooled upload is read
    into memory and BEFORE the service runs — the ingest tripwire
    below must never fire."""
    fake = FakePushPort()
    service = _service(fake, max_bytes=16)

    def _tripwire(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "service.ingest ran despite oversize Content-Length"
        )

    monkeypatch.setattr(service, "ingest", _tripwire)
    _install(http_app, service)
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("big.txt", b"x" * 512, "text/plain")},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "ingest_upload_too_large"
    assert fake.block_total() == 0


def test_ingest_bounded_read_stops_at_the_cap() -> None:
    """Input-boundary fix (chunked uploads, no Content-Length): the
    route reads the spooled upload in bounded segments and refuses at
    cap+1 bytes — never one full ``read()`` of an unbounded body."""
    import io

    from fin.host.workspace_http import _read_bounded_upload

    assert _read_bounded_upload(io.BytesIO(b"x" * 17), 16) is None
    assert _read_bounded_upload(io.BytesIO(b"x" * 16), 16) == b"x" * 16
    assert _read_bounded_upload(io.BytesIO(b"x" * 4096), 0) == b"x" * 4096


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


def test_default_ingest_service_wires_the_catalog_hook_in_both_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product assembly wires ``on_ingested`` -> ``record_filings``
    for BOTH adapter kinds — a fake boot registers into the same
    catalog the ``GET /api/corpus`` route reads."""
    from fin.host.workspace_http import _build_default_ingest_service

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    fake_service = _build_default_ingest_service()
    assert fake_service._on_ingested is not None

    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")
    mcp_service = _build_default_ingest_service()
    assert mcp_service._on_ingested is not None


# ----- T2 x T3 wiring: the real product dependency registers the catalog
#
# These tests drive the route WITHOUT a dependency override, so the
# request flows through ``get_ingest_service`` -> the product assembly
# -> the ``on_ingested`` hook -> ``record_filings``. The catalog module
# default is pointed at a temp table so the committed
# ``data/corpus/catalog.json`` is never touched.


@pytest.fixture
def product_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the product ingest wiring at a temp catalog table.

    Patches the catalog module default (both the hook's write path and
    ``GET /api/corpus``'s read path read it at call time), selects the
    fake adapter boot (no MCP subprocess), and starts from a fresh
    ingest-service singleton so the test owns its own service.
    """
    catalog_path = tmp_path / "corpus" / "catalog.json"
    monkeypatch.setattr(
        "fin.corpus.catalog.DEFAULT_CATALOG_PATH", catalog_path
    )
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.setattr(
        "fin.host.workspace_http._ingest_service_singleton", None
    )
    return catalog_path


def test_product_ingest_registers_the_filing_in_the_catalog(
    client: TestClient, product_catalog: Path
) -> None:
    """A metadata-complete upload through the REAL product wiring
    registers the pushed filing: ``GET /api/corpus`` then lists the
    row with the matching (document_id, version) pair."""
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["catalog_warning"] is None  # clean success stays clean

    rows = [
        f
        for f in client.get("/api/corpus").json()["filings"]
        if f["document_id"] == "AAPL-2023Q3"
    ]
    assert len(rows) == 1
    assert rows[0]["version"] == body["version"]
    assert rows[0]["company"] == "AAPL"
    assert rows[0]["period"] == "2023Q3"
    assert rows[0]["page_count"] == body["unit_count"]


def test_product_ingest_push_survives_catalog_write_failure(
    client: TestClient, product_catalog: Path
) -> None:
    """Push success + catalog write failure is neither a failed ingest
    nor a silent success: the answer stays 200 ``ingested`` carrying
    the named ``catalog_warning``, the pushed blocks stand, and the
    corrupt table is left untouched."""
    product_catalog.parent.mkdir(parents=True, exist_ok=True)
    product_catalog.write_text("{ not json")  # corrupt table

    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["catalog_warning"].startswith("catalog_record_failed")
    # The push landed and was not rolled back (blocks observable on
    # the fake port behind the product singleton).
    fake = get_ingest_service().push_port
    assert fake.block_total() == body["block_count"] > 0
    # The upsert refused to build on the unreadable table: no partial
    # rewrite of the corrupt file.
    assert product_catalog.read_text() == "{ not json"


def test_product_ingest_without_metadata_warns_row_contract(
    client: TestClient, product_catalog: Path
) -> None:
    """A metadata-less upload cannot satisfy the catalog row contract
    (``company`` / ``period`` are required non-empty strings): the
    push still lands and the response carries the named warning —
    never a fabricated row and never a silent skip-as-success."""
    response = client.post(
        "/api/corpus/ingest",
        files={"file": ("solo.md", _MD_BYTES, "text/markdown")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["catalog_warning"].startswith("catalog_record_failed")
    assert "company" in body["catalog_warning"]
    # Nothing was registered: the catalog stays empty.
    assert client.get("/api/corpus").json()["filings"] == []


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
