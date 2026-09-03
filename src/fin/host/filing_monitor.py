"""Polling adapter for new filings on the workspace watchlist.

The monitor owns source polling only. ``watchlist.refresh_notices`` remains
the single durable dedup and reload seam, so a source result is safe to
replay after a process restart.

The default source is disabled unless an operator explicitly configures a
CIK map. Tests inject a scripted source and therefore never contact SEC.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

from fin.host.filing_monitor_errors import FilingSourceUnavailableError
from fin.host.watchlist import Notice, WatchEntry, JsonFileWatchlistStore, refresh_notices


class FilingSource(Protocol):
    """Source boundary used by the monitor and deterministic test fixtures."""

    def fetch(self, watches: Sequence[WatchEntry]) -> Iterable[dict[str, Any]]:
        """Return current filings for the requested watches."""


@dataclass(frozen=True)
class FilingMonitorResult:
    """One successful poll and the notices it newly created."""

    status: str
    created: list[Notice]


class FilingMonitor:
    """Poll a source and persist matching filing notices."""

    def __init__(self, source: FilingSource) -> None:
        self._source = source

    def poll(self, store: JsonFileWatchlistStore) -> FilingMonitorResult:
        watches = store.list_watches()
        if not watches:
            return FilingMonitorResult(status="healthy", created=[])
        try:
            filings = list(self._source.fetch(watches))
        except FilingSourceUnavailableError:
            raise
        except Exception as exc:
            raise FilingSourceUnavailableError(
                "filing source did not return a poll result"
            ) from exc
        return FilingMonitorResult(
            status="healthy",
            created=refresh_notices(store, filings=filings),
        )


class DisabledFilingSource:
    """Explicit no-op source for boots without monitor configuration."""

    def fetch(self, watches: Sequence[WatchEntry]) -> Iterable[dict[str, Any]]:
        return []


class SecFilingSource:
    """Read recent filings from SEC submissions JSON.

    ``cik_by_issuer`` is explicit configuration rather than an implicit
    ticker lookup. This keeps the source deterministic and avoids an extra
    network dependency. The source is only selected by
    ``FIN_EDGAR_MONITOR_CIKS``.
    """

    def __init__(
        self,
        cik_by_issuer: dict[str, str],
        *,
        user_agent: str | None = None,
    ) -> None:
        self._cik_by_issuer = {
            issuer.strip().upper(): cik.strip().zfill(10)
            for issuer, cik in cik_by_issuer.items()
            if issuer.strip() and cik.strip()
        }
        self._user_agent = (
            user_agent if user_agent is not None else os.environ.get(
                "FIN_EDGAR_USER_AGENT", ""
            )
        ).strip()

    def fetch(self, watches: Sequence[WatchEntry]) -> list[dict[str, Any]]:
        if not self._user_agent:
            raise FilingSourceUnavailableError(
                "FIN_EDGAR_USER_AGENT is required for SEC monitor polling"
            )
        filings: list[dict[str, Any]] = []
        seen_issuers: set[str] = set()
        for watch in watches:
            if watch.issuer in seen_issuers:
                continue
            seen_issuers.add(watch.issuer)
            cik = self._cik_by_issuer.get(watch.issuer)
            if cik is None:
                continue
            payload = self._get_submissions(cik)
            filings.extend(self._recent_filings(payload, issuer=watch.issuer))
        return filings

    def _get_submissions(self, cik: str) -> dict[str, Any]:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise FilingSourceUnavailableError(
                "SEC submissions source is unavailable"
            ) from exc
        if not isinstance(payload, dict):
            raise FilingSourceUnavailableError(
                "SEC submissions source returned an invalid payload"
            )
        return payload

    @staticmethod
    def _recent_filings(
        payload: dict[str, Any], *, issuer: str
    ) -> list[dict[str, Any]]:
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        out: list[dict[str, Any]] = []
        for index, accession in enumerate(accessions):
            form = forms[index] if index < len(forms) else ""
            date = dates[index] if index < len(dates) else ""
            out.append(
                {
                    "document_id": str(accession),
                    "company": issuer,
                    "ticker": issuer,
                    "form": str(form),
                    "period": str(date),
                    "filing_date": str(date),
                }
            )
        return out


def _configured_ciks() -> dict[str, str]:
    raw = os.environ.get("FIN_EDGAR_MONITOR_CIKS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(issuer): str(cik)
        for issuer, cik in value.items()
        if isinstance(issuer, str) and isinstance(cik, (str, int))
    }


def default_filing_monitor() -> FilingMonitor:
    """Build the process default without making a network call at import."""

    ciks = _configured_ciks()
    source: FilingSource = (
        SecFilingSource(ciks) if ciks else DisabledFilingSource()
    )
    return FilingMonitor(source)


__all__ = [
    "DisabledFilingSource",
    "FilingMonitor",
    "FilingMonitorResult",
    "FilingSource",
    "SecFilingSource",
    "default_filing_monitor",
]
