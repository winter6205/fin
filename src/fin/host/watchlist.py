"""Watchlist + in-app notice storage for the workspace (T4, the tracked change).

Spec Focus 3 / SC4: the operator watches an issuer (optionally
narrowed to form types) from the workspace, the list survives a
restart, and a watched filing appearing in the LOCAL catalog raises an
in-app notice. Nothing leaves the process — no email, no Slack, no
outbound webhook (spec Focus 3: "no outbound email/Slack").

Why a side file rather than a ``fin_run``-style table: the plan leaves
storage open ("Postgres 表 vs 侧文件，只要 reload 后仍在"). A watchlist
is operator preference, not graph state and not turn presentation
state, so it has neither the checkpointer's lifecycle nor the thread
store's cascade rules. One JSON document keeps the durable boot and
the no-Postgres dev boot on the SAME code path, which is what the
restart clause actually asks for.

Shape on disk (``fin.watchlist.v1``)::

    {
      "schema": "fin.watchlist.v1",
      "watches": [{"id","issuer","forms":[...],"created_at"}],
      "notices": [{"id","watch_id","issuer","form","document_id",
                   "period","created_at","read"}]
    }

The notice list is also the dedup ledger: a filing already announced
for a watch never announces again, including after a restart.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


WATCHLIST_SCHEMA = "fin.watchlist.v1"


# ----- typed errors -----


class WatchlistError(Exception):
    """Base class for watchlist typed errors."""


class InvalidWatchError(WatchlistError):
    """The requested watch has no issuer to match on.

    A row with a blank issuer would match nothing forever while
    occupying the operator's list — a typed rejection at the seam is
    louder than a silently useless row. The HTTP layer maps this to
    400 ``{"error": "invalid_watch"}``.
    """


class WatchNotFoundError(WatchlistError):
    """A caller addressed a watch id the store does not know (404)."""


class NoticeNotFoundError(WatchlistError):
    """A caller addressed a notice id the store does not know (404)."""


# ----- records -----


@dataclass(frozen=True)
class WatchEntry:
    """One watched issuer, optionally narrowed to form types.

    ``forms`` empty means every form type: the operator asked about the
    issuer, not about a filing kind.
    """

    id: str
    issuer: str
    forms: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class Notice:
    """One in-app notice: a watched filing that is now in the catalog.

    ``read`` is the operator's dismissal, not a delivery receipt — the
    notice never leaves the workspace, so there is nothing else to
    track.
    """

    id: str
    watch_id: str
    issuer: str
    form: str
    document_id: str
    period: str
    created_at: datetime
    read: bool = False


# ----- normalization + matching (pure) -----


def normalize_issuer(issuer: str) -> str:
    """Upper-cased, whitespace-stripped issuer key.

    The manifest keys ``document_id`` / ``ticker`` / ``company`` in
    upper case for the frozen 12; folding here means "aapl" and "AAPL"
    are one watch instead of two rows firing the same notice.
    """
    return (issuer or "").strip().upper()


def normalize_forms(forms: Iterable[str] | None) -> tuple[str, ...]:
    """Upper-cased form types, blanks dropped, first-seen order kept."""
    out: list[str] = []
    for form in forms or ():
        text = str(form).strip().upper()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _filing_issuer_keys(filing: dict[str, Any]) -> set[str]:
    """Every string that identifies the issuer behind a catalog row.

    ``document_id`` is ``TICKER-PERIOD`` (ADR 0006), and the manifest
    carries ``company`` / ``ticker`` separately — CONTEXT warns against
    "treating company as forever equal to ticker", so all three are
    accepted rather than picking one.
    """
    keys = {
        normalize_issuer(str(filing.get("company") or "")),
        normalize_issuer(str(filing.get("ticker") or "")),
        normalize_issuer(str(filing.get("document_id") or "").split("-")[0]),
    }
    keys.discard("")
    return keys


def matches_watch(entry: WatchEntry, filing: dict[str, Any]) -> bool:
    """True when ``filing`` is what ``entry`` is watching for.

    Form filtering is exact against the catalog's ``form`` value; a
    catalog row with no form can only match a watch that named no
    forms (an unlabelled row is not evidence of any particular kind).
    """
    if entry.issuer not in _filing_issuer_keys(filing):
        return False
    if not entry.forms:
        return True
    form = normalize_issuer(str(filing.get("form") or ""))
    return form in entry.forms


# ----- store -----


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JsonFileWatchlistStore:
    """A watchlist + notice ledger persisted as one JSON document.

    Every public call re-reads the file under a process lock so a
    second `uvicorn` worker (or a test's second store instance) sees
    what the first wrote. The write is atomic (temp file + ``replace``)
    so a crash mid-write cannot leave a truncated document that would
    read back as "the operator never watched anything".
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    # -- disk seam --

    def _read(self) -> dict[str, Any]:
        """Load the document; a missing file is an empty watchlist.

        A malformed document is NOT silently reset: losing the
        operator's watchlist to a stray character is worse than a loud
        failure, and the HTTP layer has no repair to offer.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema": WATCHLIST_SCHEMA, "watches": [], "notices": []}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WatchlistError(
                f"watchlist document at {self._path} is not valid JSON"
            ) from exc
        payload.setdefault("watches", [])
        payload.setdefault("notices", [])
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema"] = WATCHLIST_SCHEMA
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)

    # -- watches --

    def add_watch(self, *, issuer: str, forms: Iterable[str] | None = None) -> WatchEntry:
        """Add (or return the existing) watch for ``issuer`` + ``forms``.

        Idempotent on an identical issuer + form set: the operator
        clicking twice must not produce two rows that both announce the
        same filing.
        """
        key = normalize_issuer(issuer)
        if not key:
            raise InvalidWatchError("a watch needs a non-empty issuer")
        form_tuple = normalize_forms(forms)
        with self._lock:
            payload = self._read()
            for row in payload["watches"]:
                if row["issuer"] == key and tuple(row.get("forms") or ()) == form_tuple:
                    return _row_to_watch(row)
            entry = WatchEntry(
                id=str(uuid.uuid4()),
                issuer=key,
                forms=form_tuple,
                created_at=_now(),
            )
            payload["watches"].append(_watch_to_row(entry))
            self._write(payload)
            return entry

    def list_watches(self) -> list[WatchEntry]:
        """Watches newest-first (same ordering rule as the sidebar's
        thread list). Empty is ``[]``, never an error (SC4)."""
        with self._lock:
            rows = self._read()["watches"]
        entries = [_row_to_watch(row) for row in rows]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def remove_watch(self, watch_id: str) -> None:
        """Drop the watch AND the notices it raised.

        Cascade rationale mirrors ``fin_thread`` -> ``fin_run``: a
        notice whose watch is gone is an orphan the operator can no
        longer act on or explain.
        """
        with self._lock:
            payload = self._read()
            remaining = [r for r in payload["watches"] if r["id"] != watch_id]
            if len(remaining) == len(payload["watches"]):
                raise WatchNotFoundError(f"unknown watch {watch_id!r}")
            payload["watches"] = remaining
            payload["notices"] = [
                n for n in payload["notices"] if n.get("watch_id") != watch_id
            ]
            self._write(payload)

    # -- notices --

    def list_notices(self, *, unread_only: bool = False) -> list[Notice]:
        """Notices newest-first; ``unread_only`` hides dismissed rows."""
        with self._lock:
            rows = self._read()["notices"]
        notices = [_row_to_notice(row) for row in rows]
        if unread_only:
            notices = [n for n in notices if not n.read]
        return sorted(notices, key=lambda n: n.created_at, reverse=True)

    def record_notices(self, notices: list[Notice]) -> list[Notice]:
        """Append notices that are not already on the ledger.

        The ``(watch_id, document_id)`` pair is the dedup key, and it
        lives on the persisted rows — so a restart cannot re-announce a
        filing the operator already saw.
        """
        if not notices:
            return []
        with self._lock:
            payload = self._read()
            seen = {
                (row.get("watch_id"), row.get("document_id"))
                for row in payload["notices"]
            }
            created: list[Notice] = []
            for notice in notices:
                key = (notice.watch_id, notice.document_id)
                if key in seen:
                    continue
                seen.add(key)
                created.append(notice)
                payload["notices"].append(_notice_to_row(notice))
            if created:
                self._write(payload)
            return created

    def mark_notice_read(self, notice_id: str) -> Notice:
        with self._lock:
            payload = self._read()
            for row in payload["notices"]:
                if row["id"] == notice_id:
                    row["read"] = True
                    self._write(payload)
                    return _row_to_notice(row)
            raise NoticeNotFoundError(f"unknown notice {notice_id!r}")


def refresh_notices(
    store: JsonFileWatchlistStore, *, filings: Iterable[dict[str, Any]]
) -> list[Notice]:
    """Reconcile the local catalog against the watchlist.

    Returns only the notices this call CREATED, so a caller can tell
    "a watched filing just entered the catalog" from "the operator has
    unread notices". Called by ``GET /api/notices`` — the local catalog
    is a committed tree, not an event source, so reconcile-on-read is
    the whole delivery mechanism (and keeps the workspace free of a
    background poller nobody asked for).
    """
    watches = store.list_watches()
    if not watches:
        return []
    candidates: list[Notice] = []
    for filing in filings:
        for watch in watches:
            if not matches_watch(watch, filing):
                continue
            candidates.append(
                Notice(
                    id=str(uuid.uuid4()),
                    watch_id=watch.id,
                    issuer=watch.issuer,
                    form=str(filing.get("form") or ""),
                    document_id=str(filing.get("document_id") or ""),
                    period=str(filing.get("period") or ""),
                    created_at=_now(),
                )
            )
    return store.record_notices(candidates)


# ----- row codecs -----


def _watch_to_row(entry: WatchEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "issuer": entry.issuer,
        "forms": list(entry.forms),
        "created_at": entry.created_at.isoformat(),
    }


def _row_to_watch(row: dict[str, Any]) -> WatchEntry:
    return WatchEntry(
        id=row["id"],
        issuer=row["issuer"],
        forms=tuple(row.get("forms") or ()),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _notice_to_row(notice: Notice) -> dict[str, Any]:
    return {
        "id": notice.id,
        "watch_id": notice.watch_id,
        "issuer": notice.issuer,
        "form": notice.form,
        "document_id": notice.document_id,
        "period": notice.period,
        "created_at": notice.created_at.isoformat(),
        "read": notice.read,
    }


def _row_to_notice(row: dict[str, Any]) -> Notice:
    return Notice(
        id=row["id"],
        watch_id=row["watch_id"],
        issuer=row["issuer"],
        form=row.get("form", ""),
        document_id=row["document_id"],
        period=row.get("period", ""),
        created_at=datetime.fromisoformat(row["created_at"]),
        read=bool(row.get("read", False)),
    )


# ----- env-based default selection -----


# ``src/fin/host/watchlist.py`` -> ``src`` (parents[2]) -> repo root.
_DEFAULT_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "workspace" / "watchlist.json"
)

# Same rationale as ``default_thread_store``'s cache: one store per
# path per process so a write from one request is visible to the next.
_DEFAULT_STORE_CACHE: dict[str, JsonFileWatchlistStore] = {}


def default_watchlist_store(env: Any = None) -> JsonFileWatchlistStore:
    """The store the workspace routes use.

    ``FIN_WATCHLIST_PATH`` overrides the location (tests, or an
    operator keeping workspace state outside the repo); unset uses
    ``data/workspace/watchlist.json`` next to the corpus data.
    """
    source = env if env is not None else os.environ
    raw = (source.get("FIN_WATCHLIST_PATH") or "").strip()
    path = Path(raw) if raw else _DEFAULT_PATH
    key = str(path)
    cached = _DEFAULT_STORE_CACHE.get(key)
    if cached is None:
        cached = JsonFileWatchlistStore(path)
        _DEFAULT_STORE_CACHE[key] = cached
    return cached


__all__ = [
    "InvalidWatchError",
    "JsonFileWatchlistStore",
    "Notice",
    "NoticeNotFoundError",
    "WATCHLIST_SCHEMA",
    "WatchEntry",
    "WatchNotFoundError",
    "WatchlistError",
    "default_watchlist_store",
    "matches_watch",
    "normalize_forms",
    "normalize_issuer",
    "refresh_notices",
]
