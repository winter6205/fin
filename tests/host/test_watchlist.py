"""Tests for the workspace watchlist + in-app notice store (T4, #138).

Spec Focus 3 / SC4: "Watchlist: create / list / delete via workspace
API; after process restart ... list matches; empty list is ``[]`` not
an error." plus "In-app notice when a watched filing appears in the
local catalog (no outbound email/Slack)."

The store is the persistence seam behind those two sentences. The
restart case is exercised the only way a store can be: build a SECOND
store instance over the SAME backing file and read it back — that is
what a `fin-serve` restart does to the process.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from fin.host.watchlist import (
    InvalidWatchError,
    JsonFileWatchlistStore,
    NoticeNotFoundError,
    WatchNotFoundError,
    WatchlistError,
    default_watchlist_store,
    matches_watch,
    refresh_notices,
)


def _filing(document_id: str, *, form: str, company: str | None = None) -> dict:
    """One catalog row in the ``list_filings`` shape (T2 adds ``form``)."""
    ticker = document_id.split("-")[0]
    return {
        "document_id": document_id,
        "company": company or ticker,
        "period": document_id.split("-")[1],
        "version": "v" + document_id,
        "page_count": 3,
        "form": form,
    }


@pytest.fixture
def store(tmp_path: Path) -> JsonFileWatchlistStore:
    return JsonFileWatchlistStore(tmp_path / "watchlist.json")


# ----- watchlist CRUD -----


def test_empty_watchlist_is_an_empty_list_not_an_error(store) -> None:
    """SC4: "empty list is ``[]`` not an error"."""
    assert store.list_watches() == []


def test_add_then_list_returns_the_issuer_and_its_form_types(store) -> None:
    entry = store.add_watch(issuer="aapl", forms=["10-k", "8-K"])

    listed = store.list_watches()
    assert [e.id for e in listed] == [entry.id]
    # Issuer and form types are normalized to the catalog's casing so
    # "aapl" and "AAPL" are one watch, not two.
    assert listed[0].issuer == "AAPL"
    assert listed[0].forms == ("10-K", "8-K")


def test_add_watch_with_blank_issuer_is_a_typed_error(store) -> None:
    """Empty input is a typed rejection, never a row nobody can match."""
    with pytest.raises(InvalidWatchError):
        store.add_watch(issuer="   ", forms=["10-K"])


def test_add_watch_with_no_forms_watches_every_form(store) -> None:
    entry = store.add_watch(issuer="MSFT", forms=[])
    assert entry.forms == ()
    assert matches_watch(entry, _filing("MSFT-2023Q1", form="8-K")) is True


def test_adding_the_same_issuer_and_forms_twice_returns_the_same_row(store) -> None:
    """A duplicate add is idempotent — the operator clicking twice must
    not produce two rows that both fire the same notice."""
    first = store.add_watch(issuer="AAPL", forms=["10-K"])
    second = store.add_watch(issuer="aapl", forms=["10-k"])

    assert second.id == first.id
    assert len(store.list_watches()) == 1


def test_remove_watch_drops_the_row(store) -> None:
    entry = store.add_watch(issuer="AAPL", forms=["10-K"])
    store.remove_watch(entry.id)
    assert store.list_watches() == []


def test_remove_unknown_watch_is_a_typed_error(store) -> None:
    with pytest.raises(WatchNotFoundError):
        store.remove_watch("does-not-exist")


def test_watchlist_survives_a_process_restart(tmp_path: Path) -> None:
    """SC4 restart clause: a NEW store over the same backing file — what
    a ``fin-serve`` restart does — lists the same rows."""
    path = tmp_path / "watchlist.json"
    before = JsonFileWatchlistStore(path)
    entry = before.add_watch(issuer="AAPL", forms=["10-K", "8-K"])

    after = JsonFileWatchlistStore(path)
    restored = after.list_watches()

    assert [e.id for e in restored] == [entry.id]
    assert restored[0].issuer == "AAPL"
    assert restored[0].forms == ("10-K", "8-K")


def test_a_store_over_a_missing_file_starts_empty(tmp_path: Path) -> None:
    """First boot has no file yet; that is an empty watchlist, not an
    exception the workspace has to catch."""
    assert JsonFileWatchlistStore(tmp_path / "never-written.json").list_watches() == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        42,
        {"schema": "fin.watchlist.v1", "watches": []},
        {"schema": "fin.watchlist.v1", "notices": []},
        {"schema": "fin.watchlist.v1", "watches": {}, "notices": []},
        {"schema": "fin.watchlist.v1", "watches": [], "notices": {}},
        {
            "schema": "fin.watchlist.v1",
            "watches": [{"id": "watch-1", "issuer": "AAPL", "forms": []}],
            "notices": [],
        },
        {
            "schema": "fin.watchlist.v1",
            "watches": [
                {
                    "id": "watch-1",
                    "issuer": "AAPL",
                    "forms": "10-K",
                    "created_at": "2024-01-01T00:00:00+00:00",
                }
            ],
            "notices": [],
        },
    ],
)
def test_invalid_watchlist_shapes_raise_typed_error_without_rewriting_file(
    tmp_path: Path, payload
) -> None:
    path = tmp_path / "watchlist.json"
    original = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(original)
    store = JsonFileWatchlistStore(path)

    with pytest.raises(WatchlistError):
        store.list_watches()

    assert path.read_bytes() == original


def test_empty_watchlist_file_starts_with_the_empty_table_default(tmp_path: Path) -> None:
    path = tmp_path / "empty-watchlist.json"
    path.write_bytes(b"")

    assert JsonFileWatchlistStore(path).list_watches() == []


def _add_watch_in_process(path: str, issuer: str, ready) -> None:
    store = JsonFileWatchlistStore(path)
    ready.wait()
    store.add_watch(issuer=issuer)


def test_independent_process_stores_preserve_concurrent_adds(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    context = multiprocessing.get_context("fork")
    ready = context.Barrier(2)
    processes = [
        context.Process(
            target=_add_watch_in_process,
            args=(str(path), issuer, ready),
        )
        for issuer in ("AAPL", "MSFT")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join()

    assert all(process.exitcode == 0 for process in processes)
    restored = JsonFileWatchlistStore(path)
    assert {entry.issuer for entry in restored.list_watches()} == {"AAPL", "MSFT"}
    assert len(json.loads(path.read_text(encoding="utf-8"))["watches"]) == 2


def test_duplicate_add_is_idempotent_across_independent_stores(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    first = JsonFileWatchlistStore(path).add_watch(issuer="AAPL")
    second = JsonFileWatchlistStore(path).add_watch(issuer="aapl")

    assert second.id == first.id
    assert [entry.issuer for entry in JsonFileWatchlistStore(path).list_watches()] == [
        "AAPL"
    ]


# ----- matching -----


def test_matching_is_by_issuer_and_form(store) -> None:
    entry = store.add_watch(issuer="AAPL", forms=["10-K"])

    assert matches_watch(entry, _filing("AAPL-2023FY", form="10-K")) is True
    # Right issuer, wrong form.
    assert matches_watch(entry, _filing("AAPL-2023Q1", form="10-Q")) is False
    # Right form, wrong issuer.
    assert matches_watch(entry, _filing("MSFT-2023FY", form="10-K")) is False


def test_matching_accepts_the_company_key_when_it_is_not_the_ticker(store) -> None:
    """The manifest keys ``document_id`` by ticker but ``company`` may
    carry a different string; either one identifies the issuer."""
    entry = store.add_watch(issuer="APPLE INC", forms=[])
    filing = _filing("AAPL-2023Q1", form="10-Q", company="Apple Inc")
    assert matches_watch(entry, filing) is True


# ----- notices -----


def test_no_notice_until_a_watched_filing_enters_the_catalog(store) -> None:
    store.add_watch(issuer="AAPL", forms=["10-K"])

    created = refresh_notices(store, filings=[_filing("MSFT-2023FY", form="10-K")])

    assert created == []
    assert store.list_notices() == []


def test_a_watched_filing_entering_the_catalog_creates_one_notice(store) -> None:
    """The T4 acceptance line: watched filing lands in the local catalog
    -> the workspace has an in-app notice for it."""
    store.add_watch(issuer="AAPL", forms=["10-K"])
    catalog = [_filing("MSFT-2023FY", form="10-K")]
    refresh_notices(store, filings=catalog)

    catalog.append(_filing("AAPL-2023FY", form="10-K"))
    created = refresh_notices(store, filings=catalog)

    assert [n.document_id for n in created] == ["AAPL-2023FY"]
    stored = store.list_notices()
    assert [n.document_id for n in stored] == ["AAPL-2023FY"]
    assert stored[0].issuer == "AAPL"
    assert stored[0].form == "10-K"
    assert stored[0].read is False


def test_refreshing_twice_does_not_duplicate_the_notice(store) -> None:
    store.add_watch(issuer="AAPL", forms=["10-K"])
    catalog = [_filing("AAPL-2023FY", form="10-K")]

    refresh_notices(store, filings=catalog)
    second = refresh_notices(store, filings=catalog)

    assert second == []
    assert len(store.list_notices()) == 1


def test_notices_survive_a_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    before = JsonFileWatchlistStore(path)
    before.add_watch(issuer="AAPL", forms=["10-K"])
    refresh_notices(before, filings=[_filing("AAPL-2023FY", form="10-K")])

    after = JsonFileWatchlistStore(path)
    assert [n.document_id for n in after.list_notices()] == ["AAPL-2023FY"]
    # The dedup ledger persists too: a restart must not re-announce a
    # filing the operator already saw.
    assert refresh_notices(after, filings=[_filing("AAPL-2023FY", form="10-K")]) == []


def test_marking_a_notice_read_keeps_it_out_of_the_unread_view(store) -> None:
    store.add_watch(issuer="AAPL", forms=["10-K"])
    (notice,) = refresh_notices(store, filings=[_filing("AAPL-2023FY", form="10-K")])

    store.mark_notice_read(notice.id)

    assert [n.read for n in store.list_notices()] == [True]
    assert store.list_notices(unread_only=True) == []


def test_marking_an_unknown_notice_read_is_a_typed_error(store) -> None:
    with pytest.raises(NoticeNotFoundError):
        store.mark_notice_read("does-not-exist")


def test_removing_a_watch_drops_its_notices(store) -> None:
    """A notice with no watch behind it is an orphan the operator can
    never act on — delete cascades, same rule the thread store uses."""
    entry = store.add_watch(issuer="AAPL", forms=["10-K"])
    refresh_notices(store, filings=[_filing("AAPL-2023FY", form="10-K")])

    store.remove_watch(entry.id)

    assert store.list_notices() == []


# ----- default selection -----


def test_default_store_path_comes_from_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "watchlist.json"
    store = default_watchlist_store({"FIN_WATCHLIST_PATH": str(path)})
    store.add_watch(issuer="AAPL", forms=["10-K"])

    assert path.exists()
    # Cached per path so two requests in one process share the state.
    assert default_watchlist_store({"FIN_WATCHLIST_PATH": str(path)}) is store
