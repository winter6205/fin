"""T7 filing monitor tests.

The source is deliberately scripted: default pytest must never contact SEC.
The HTTP assertions also pin the reload-safe notice ledger and the degraded
wire signal when the source becomes unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.filing_monitor import FilingMonitor, SecFilingSource
from fin.host.filing_monitor_errors import FilingSourceUnavailableError
from fin.host.watchlist import JsonFileWatchlistStore, WatchEntry


def _filing(document_id: str = "AAPL-2026Q2") -> dict[str, str]:
    return {
        "document_id": document_id,
        "company": "AAPL",
        "ticker": "AAPL",
        "form": "10-Q",
        "period": "2026 Q2",
        "filing_date": "2026-08-28",
    }


class ScriptedFilingSource:
    def __init__(self, filings: list[dict[str, str]]) -> None:
        self.filings = filings

    def fetch(self, watches):
        return list(self.filings)


class DownFilingSource:
    def fetch(self, watches):
        raise OSError("fixture source is offline")


def test_scripted_filing_creates_notice_that_a_new_store_can_reload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.json"
    store = JsonFileWatchlistStore(path)
    store.add_watch(issuer="AAPL", forms=["10-Q"])

    result = FilingMonitor(ScriptedFilingSource([_filing()])).poll(store)

    assert result.status == "healthy"
    assert [notice.document_id for notice in result.created] == ["AAPL-2026Q2"]

    reloaded = JsonFileWatchlistStore(path)
    assert [notice.document_id for notice in reloaded.list_notices()] == [
        "AAPL-2026Q2"
    ]


def test_source_down_is_a_typed_failure_and_does_not_erase_persisted_notice(
    tmp_path: Path,
) -> None:
    store = JsonFileWatchlistStore(tmp_path / "watchlist.json")
    store.add_watch(issuer="AAPL", forms=["10-Q"])
    FilingMonitor(ScriptedFilingSource([_filing()])).poll(store)

    with pytest.raises(FilingSourceUnavailableError) as caught:
        FilingMonitor(DownFilingSource()).poll(store)

    assert caught.value.code == "filing_source_unavailable"
    assert [notice.document_id for notice in store.list_notices()] == [
        "AAPL-2026Q2"
    ]


def test_sec_source_maps_recent_submission_rows_without_live_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                b'{"filings":{"recent":{"form":["10-Q"],'
                b'"filingDate":["2026-08-28"],'
                b'"accessionNumber":["0000320193-26-000001"]}}}'
            )

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(
        "fin.host.filing_monitor.urllib.request.urlopen",
        fake_urlopen,
    )

    source = SecFilingSource({"AAPL": "320193"}, user_agent="fixture")
    entries = source.fetch(
        [
            WatchEntry(
                id="w1",
                issuer="AAPL",
                forms=("10-Q",),
                created_at=datetime.now(timezone.utc),
            )
        ]
    )

    assert entries[0]["document_id"] == "0000320193-26-000001"
    assert entries[0]["form"] == "10-Q"
    assert requests[0][1] == 20
    assert requests[0][0].full_url.endswith("CIK0000320193.json")


def test_notices_route_returns_persisted_rows_with_typed_degraded_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host import workspace_http
    from fin.host.workspace_http import build_router

    path = tmp_path / "watchlist.json"
    store = JsonFileWatchlistStore(path)
    store.add_watch(issuer="AAPL", forms=["10-Q"])
    FilingMonitor(ScriptedFilingSource([_filing()])).poll(store)

    app = FastAPI()
    app.include_router(
        build_router(
            get_graph=lambda: None,
            get_thread_store=lambda: None,
            get_watchlist_store=lambda: store,
            get_filing_monitor=lambda: FilingMonitor(DownFilingSource()),
        )
    )
    monkeypatch.setattr(
        workspace_http,
        "list_filings",
        lambda: {"filings": []},
    )

    response = TestClient(app).get("/api/notices")

    assert response.status_code == 200
    assert response.json()[0]["document_id"] == "AAPL-2026Q2"
    assert response.headers["x-filing-monitor-status"] == "degraded"
    assert response.headers["x-filing-monitor-error"] == (
        "filing_source_unavailable"
    )
