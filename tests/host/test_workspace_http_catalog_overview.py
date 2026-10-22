"""The corpus OVERVIEW seam (T6): one route that carries the generic read
model and its presentation grouping.

``GET /api/corpus`` stays the FINANCIAL compatibility projection with its
exact six-key shape (spec "Catalog fields and compatibility projection"),
so the generic surface needs its own mouth: an ordinary document has no
``company`` / ``period`` / ``form`` at all, and a route that dropped those
keys would break the existing financial callers.

The overview carries the read model plus the grouping the operator reads:
only dimensions with at least two distinct non-empty values, missing values
under 未分类, and a flat title list when no dimension qualifies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.corpus.catalog import record_filings


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "document_id": "doc_" + "a" * 64,
        "version": "b" * 64,
        "title": "Team Handbook",
        "doc_type": "narrative",
        "page_count": 3,
    }
    row.update(overrides)
    return row


def _write(catalog_path: Path, rows: list[dict[str, Any]]) -> None:
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps({"filings": rows}), encoding="utf-8"
    )


def _point_at(monkeypatch: pytest.MonkeyPatch, catalog_path: Path) -> None:
    monkeypatch.setattr(
        "fin.corpus.catalog.DEFAULT_CATALOG_PATH", catalog_path
    )


def test_a_missing_table_is_an_empty_overview_not_an_error(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "absent" / "catalog.json")

    response = client.get("/api/corpus/overview")

    assert response.status_code == 200
    assert response.json() == {
        "documents": [],
        "dimensions": [],
        "flat_titles": [],
    }


def test_the_overview_carries_a_document_with_no_financial_metadata(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "catalog.json")
    _write(tmp_path / "catalog.json", [_row()])

    documents = client.get("/api/corpus/overview").json()["documents"]

    assert documents == [
        {
            "document_id": "doc_" + "a" * 64,
            "version": "b" * 64,
            "title": "Team Handbook",
            "doc_type": "narrative",
            "page_count": 3,
        }
    ]
    # Absent is OMITTED, never reported as null or "" — a consumer must
    # be able to tell "claims no company" from "company is blank".
    assert "company" not in documents[0]
    assert "period" not in documents[0]


def test_present_company_and_period_travel_with_the_document(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "catalog.json")
    _write(
        tmp_path / "catalog.json",
        [_row(company="Acme", period="2024Q1")],
    )

    document = client.get("/api/corpus/overview").json()["documents"][0]

    assert document["company"] == "Acme"
    assert document["period"] == "2024Q1"


def test_only_a_dimension_with_two_values_is_offered_as_a_grouping(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "catalog.json")
    _write(
        tmp_path / "catalog.json",
        [
            _row(document_id="doc_1", title="Handbook", doc_type="narrative"),
            _row(document_id="doc_2", title="Runbook", doc_type="manual"),
        ],
    )

    body = client.get("/api/corpus/overview").json()

    keys = [d["key"] for d in body["dimensions"]]
    # Two distinct doc_type values qualify; company and period are absent
    # on every row, so each is ONE 未分类 bucket and never a grouping.
    assert keys == ["doc_type"]
    assert body["dimensions"][0]["groups"] == [
        {"value": "manual", "document_ids": ["doc_2"]},
        {"value": "narrative", "document_ids": ["doc_1"]},
    ]


def test_a_row_missing_the_dimension_lands_under_unclassified(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "catalog.json")
    _write(
        tmp_path / "catalog.json",
        [
            _row(document_id="doc_1", title="Handbook", company="Acme"),
            _row(document_id="doc_2", title="Runbook"),
        ],
    )

    body = client.get("/api/corpus/overview").json()

    company = next(d for d in body["dimensions"] if d["key"] == "company")
    assert company["groups"] == [
        {"value": "Acme", "document_ids": ["doc_1"]},
        {"value": "未分类", "document_ids": ["doc_2"]},
    ]


def test_a_corpus_with_no_qualifying_dimension_falls_back_to_titles(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(monkeypatch, tmp_path / "catalog.json")
    _write(
        tmp_path / "catalog.json",
        [
            _row(document_id="doc_1", title="Handbook"),
            _row(document_id="doc_2", title="Runbook"),
        ],
    )

    body = client.get("/api/corpus/overview").json()

    assert body["dimensions"] == []
    assert body["flat_titles"] == ["Handbook", "Runbook"]


def test_a_corrupt_table_is_the_same_typed_503_as_the_financial_read(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt = tmp_path / "catalog.json"
    corrupt.write_text("{ not json", encoding="utf-8")
    _point_at(monkeypatch, corrupt)

    response = client.get("/api/corpus/overview")

    assert response.status_code == 503
    assert response.json() == {"error": "ingest_missing"}


def test_the_financial_projection_keeps_its_exact_key_set(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding the generic mouth must not widen the legacy one."""
    _point_at(monkeypatch, tmp_path / "catalog.json")
    record_filings(
        [_row(company="Acme", period="2024Q1", form="10-Q")],
        catalog_path=tmp_path / "catalog.json",
    )

    filings = client.get("/api/corpus").json()["filings"]

    assert filings == [
        {
            "document_id": "doc_" + "a" * 64,
            "company": "Acme",
            "period": "2024Q1",
            "version": "b" * 64,
            "page_count": 3,
            "form": "10-Q",
        }
    ]
