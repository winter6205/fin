"""The demo source viewer exposes only exact synthetic fixture locators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fin.corpus.demo_seed import DEMO_FIXTURE_DIR, load_demo_documents
from fin.corpus.push import deserialize_source_ref


@pytest.fixture
def demo_source_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from fin.host.http import app

    monkeypatch.setenv("FIN_DEMO_MODE", "1")
    return TestClient(app)


def test_demo_source_index_lists_fixture_locator_metadata_without_chunks(
    demo_source_client: TestClient,
) -> None:
    response = demo_source_client.get("/api/demo/sources")

    assert response.status_code == 200
    sources = response.json()["sources"]
    handbook = next(
        source
        for source in sources
        if source["document_id"] == "zeus-handbook" and source["unit"] == 2
    )
    assert handbook["version"]
    assert handbook["page"] == 2
    assert handbook["title"] == "ZEUS Employee Handbook"
    assert handbook["section"] == "Section 2"
    assert all("content" not in source for source in sources)
    assert len({(row["document_id"], row["version"], row["unit"]) for row in sources}) == len(sources)


def test_demo_source_returns_the_exact_allowlisted_chunk(
    demo_source_client: TestClient,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "demo-corpus"
        / "zeus-handbook.json"
    )
    envelope = json.loads(fixture_path.read_text(encoding="utf-8"))
    version_response = demo_source_client.get("/api/demo/sources")
    source = next(
        row
        for row in version_response.json()["sources"]
        if row["document_id"] == "zeus-handbook" and row["unit"] == 1
    )

    response = demo_source_client.get(
        "/api/demo/source",
        params={
            "document_id": source["document_id"],
            "version": source["version"],
            "unit": source["unit"],
            "page": source["page"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        **source,
        "content": envelope["chunks"][0]["content"],
    }


def test_demo_source_resolves_a_pushed_locator_to_its_own_page(
    demo_source_client: TestClient,
) -> None:
    """A hit that came back from a real push carries the seed's own triple,
    so the allowlist has to be keyed by the same version."""
    document = next(
        document
        for document in load_demo_documents(DEMO_FIXTURE_DIR)
        if document.document_id == "zeus-handbook"
    )
    unit = document.units[1]
    source = next(
        row
        for row in demo_source_client.get("/api/demo/sources").json()["sources"]
        if row["document_id"] == document.document_id and row["unit"] == unit.unit
    )

    assert source["version"] == unit.locator.version == document.source_sha256
    assert deserialize_source_ref(unit.source_ref) == unit.locator

    response = demo_source_client.get(
        "/api/demo/source",
        params={
            "document_id": unit.locator.document_id,
            "version": unit.locator.version,
            "unit": unit.locator.unit,
            "page": unit.unit,
        },
    )

    assert response.status_code == 200
    assert response.json()["content"] == unit.content
    assert response.json()["page"] == unit.unit


def test_every_allowlisted_unit_is_its_own_page(
    demo_source_client: TestClient,
) -> None:
    """The web only resolves a citation when the source's page is the
    citation's unit, so the corpus has to keep them equal."""
    sources = demo_source_client.get("/api/demo/sources").json()["sources"]

    assert sources
    assert all(source["unit"] == source["page"] for source in sources)


def test_the_allowlist_is_the_seed_own_locator_set(
    demo_source_client: TestClient,
) -> None:
    """The allowlist and the pushed locators are one identity.

    The host reads the seed's own units, so every allowlisted row is a
    locator the seed pushed and every pushed locator is allowlisted.
    """
    sources = demo_source_client.get("/api/demo/sources").json()["sources"]
    pushed = {
        (unit.locator.document_id, unit.locator.version, unit.locator.unit)
        for document in load_demo_documents(DEMO_FIXTURE_DIR)
        for unit in document.units
    }

    assert {
        (source["document_id"], source["version"], source["unit"])
        for source in sources
    } == pushed


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "0" * 64},
        {"unit": 99},
        {"page": 2},
    ],
)
def test_demo_source_rejects_stale_or_mismatched_locator(
    demo_source_client: TestClient,
    overrides: dict[str, str | int],
) -> None:
    source = next(
        row
        for row in demo_source_client.get("/api/demo/sources").json()["sources"]
        if row["document_id"] == "zeus-handbook" and row["unit"] == 1
    )
    params = {**source, **overrides}
    params.pop("title")
    params.pop("section")

    response = demo_source_client.get("/api/demo/source", params=params)

    assert response.status_code == 404
    assert response.json() == {"error": "demo_source_not_found"}


@pytest.mark.parametrize(
    "params",
    [
        {"document_id": "", "version": "v", "unit": 1, "page": 1},
        {"document_id": "x" * 201, "version": "0" * 64, "unit": 1, "page": 1},
        {"document_id": "zeus-handbook", "version": "0" * 64, "unit": 0, "page": 1},
        {"document_id": "zeus-handbook", "version": "0" * 64, "unit": 1, "page": 0},
    ],
)
def test_demo_source_rejects_malformed_locator_before_reading_a_source(
    demo_source_client: TestClient,
    params: dict[str, str | int],
) -> None:
    response = demo_source_client.get("/api/demo/source", params=params)

    assert response.status_code == 422


def test_demo_source_routes_are_closed_outside_demo_mode(
    demo_source_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host import demo_http

    monkeypatch.setenv("FIN_DEMO_MODE", "0")

    def forbidden_load() -> object:
        raise AssertionError("fixture loader must not run outside demo mode")

    monkeypatch.setattr(demo_http, "load_demo_documents", forbidden_load)

    assert demo_source_client.get("/api/demo/sources").status_code == 404
    assert (
        demo_source_client.get(
            "/api/demo/source",
            params={
                "document_id": "zeus-handbook",
                "version": "0" * 64,
                "unit": 1,
                "page": 1,
            },
        ).status_code
        == 404
    )
