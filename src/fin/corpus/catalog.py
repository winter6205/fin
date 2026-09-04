"""Read-side catalog for the v1 agent's ``list_files`` tool.

The catalog is a host metadata table at ``data/corpus/catalog.json``
(ADR 0047): ingested/pushed filings are registered here as rows of the
cross-ticket contract ``{document_id, company, period, version,
page_count}`` (plus the optional ``source_sha256``). ``list_filings``
reads the table and never opens ``manifest.json`` or the extracted
tree — the serve-time HTML tree is not the catalog SSOT (ADR 0046 /
0047).

Rows are written through :func:`record_filings` (upsert keyed by
``document_id`` + ``version``), the seam the ingest endpoint (T3)
calls to register pushed filings. Rows may additionally carry the
EDGAR-shaped fields ``cik`` / ``form`` / ``ticker`` / ``filing_date``
(ADR 0031): ``form`` is emitted on every hit (spec SC3 of
``specs/enterprise-kb-agent.md``), the others stay filter inputs only.

No LangChain / LangGraph imports on this path. Returns the ADR 0009
contract shape (``{"filings": [...]}`` / ``{"filings": []}``). A
missing or empty table is the empty catalog — ``{"filings": []}``,
never a 503 and never a tree scan. A table file that exists but
cannot be parsed as the catalog schema is the typed
``CatalogCorruptError`` (same known-bad-corpus-state family as
``IngestMissingError``, so the host's 503 mapping and the status
strip's zero-count fallback keep working).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fin.corpus.errors import CatalogCorruptError, CatalogRowError


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
DEFAULT_CATALOG_PATH = _DEFAULT_DATA_DIR / "catalog.json"

# The cross-ticket row contract. ``page_count`` is handled separately
# (int, not bool, >= 0); the string fields must be non-empty strings.
_REQUIRED_ROW_STRING_FIELDS = (
    "document_id",
    "company",
    "period",
    "version",
)

# The one EDGAR-shaped field emitted on a hit (spec SC3). ``cik`` /
# ``ticker`` / ``filing_date`` / ``accession`` / ``source_sha256``
# stay stored-only.
_OUTPUT_KEYS = (
    "document_id",
    "company",
    "period",
    "version",
    "page_count",
    "form",
)


def list_filings(
    company: str | None = None,
    period: str | None = None,
    *,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    catalog_path: Path | str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the ADR 0009 catalog shape for the ingested filings.

    Each hit is ``{document_id, company, period, version, page_count,
    form}``. Filings are sorted by ``(document_id, version)`` for
    deterministic output. Unknown filters yield ``{"filings": []}``;
    so do an empty table and a missing table file (ADR 0047 — the
    catalog is the metadata table, not a serve-time extracted tree).

    Additive EDGAR-shaped optional filters per ADR 0031: ``cik``,
    ``form``, ``ticker``, exact ``filing_date``. Filters are
    AND-combined. Empty filter values (``cik=""`` etc.) behave as
    empty hits — NOT "match anything" and NOT an exception, per
    spec "empty hits not exceptions".

    ``form`` is the one EDGAR-shaped field that is also emitted, per
    spec SC3: the operator reads the Corpus 首页 card to see what kind
    of filing a row is, and the model needs the same answer from the
    catalog tool. ``cik`` / ``accession`` / ``filing_date`` / ``ticker``
    stay filter inputs only.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    rows = _load_rows(Path(catalog_path))

    filings: list[dict[str, Any]] = []
    for row in sorted(rows, key=_row_key):
        if company is not None and row.get("company") != company:
            continue
        if period is not None and row.get("period") != period:
            continue
        # EDGAR-shaped optional filters (ADR 0031). Empty-string
        # filter values behave as empty hits — short-circuit before
        # any row passes the row loop.
        if cik is not None:
            if cik == "" or row.get("cik") != cik:
                continue
        if form is not None:
            if form == "" or row.get("form") != form:
                continue
        if ticker is not None:
            if ticker == "" or row.get("ticker") != ticker:
                continue
        if filing_date is not None:
            if filing_date == "" or row.get("filing_date") != filing_date:
                continue
        hit: dict[str, Any] = {key: row.get(key) for key in _OUTPUT_KEYS}
        filings.append(hit)
    return {"filings": filings}


def record_filings(
    rows: Iterable[Mapping[str, Any]],
    *,
    catalog_path: Path | str | None = None,
) -> int:
    """Upsert rows into the catalog metadata table; return the count
    of recorded rows.

    The upsert key is ``(document_id, version)``: the same version of
    the same document is replaced in place; a new version is a second
    row. Rows are the cross-ticket contract ``{document_id, company,
    period, version, page_count}`` plus the optional ``source_sha256``;
    extra fields are tolerated and carried inert (the emitted hit
    shape is fixed by ``list_filings``, so extra storage fields never
    leak onto the wire).

    A row violating the contract raises the typed ``CatalogRowError``
    and the table is left untouched — a bad row is a named failure,
    never a silent skip-as-success. A corrupt existing table raises
    ``CatalogCorruptError``: the upsert refuses to build on a table
    it cannot read.

    Concurrency: the read-modify-write is serialized by an exclusive
    ``flock`` on a sibling lock file and the rewrite is atomic
    (temp file + ``os.replace``), so concurrent upserts neither lose
    rows nor leave a torn file for readers.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    catalog_path = Path(catalog_path)

    validated = [
        _validated_row(row, index) for index, row in enumerate(rows)
    ]
    if not validated:
        return 0

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with _table_lock(catalog_path):
        merged = {_row_key(row): row for row in _load_rows(catalog_path)}
        for row in validated:
            merged[_row_key(row)] = row
        _write_rows(catalog_path, sorted(merged.values(), key=_row_key))
    return len(validated)


# ---------- row contract ----------


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (row["document_id"], row["version"])


def _validated_row(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Validate one row against the catalog row contract.

    Returns a plain-dict copy. Raises ``CatalogRowError`` naming the
    row index and the offending field — the error message is part of
    the contract, so the ingest side can surface which row drifted.
    """
    where = f"row #{index}"
    if not isinstance(row, Mapping):
        raise CatalogRowError(
            f"{where} must be a mapping, got {type(row).__name__}"
        )
    validated = dict(row)
    for field in _REQUIRED_ROW_STRING_FIELDS:
        value = validated.get(field)
        if not isinstance(value, str) or not value:
            raise CatalogRowError(
                f"{where} field {field!r} must be a non-empty string, "
                f"got {value!r}"
            )
    page_count = validated.get("page_count")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
    ):
        raise CatalogRowError(
            f"{where} field 'page_count' must be a non-negative int, "
            f"got {page_count!r}"
        )
    source_sha256 = validated.get("source_sha256")
    if source_sha256 is not None and not isinstance(source_sha256, str):
        raise CatalogRowError(
            f"{where} field 'source_sha256' must be a string or None, "
            f"got {source_sha256!r}"
        )
    return validated


# ---------- table I/O ----------


def _load_rows(catalog_path: Path) -> list[dict[str, Any]]:
    """Read the table's rows. Missing file → ``[]`` (the empty
    catalog); existing-but-broken → ``CatalogCorruptError``."""
    try:
        raw = catalog_path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CatalogCorruptError(
            f"catalog table at {catalog_path} is unreadable: {exc}"
        ) from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise CatalogCorruptError(
            f"catalog table at {catalog_path} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("filings"), list
    ):
        raise CatalogCorruptError(
            f"catalog table at {catalog_path} is not the "
            "{'filings': [...]} shape"
        )
    for index, row in enumerate(payload["filings"]):
        try:
            _validated_row(row, index)
        except CatalogRowError as exc:
            raise CatalogCorruptError(
                f"catalog table at {catalog_path} holds an invalid row: {exc}"
            ) from exc
    return payload["filings"]


def _write_rows(catalog_path: Path, rows: list[dict[str, Any]]) -> None:
    """Atomically rewrite the table: temp file in the same directory,
    fsync, ``os.replace``. Readers never observe a torn file."""
    payload = (
        json.dumps({"filings": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=str(catalog_path.parent), prefix=".catalog-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, catalog_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _table_lock(catalog_path: Path):
    """Serialize read-modify-write cycles across processes and
    threads via an exclusive ``flock`` on a sibling lock file."""
    lock_path = catalog_path.with_name(catalog_path.name + ".lock")
    with open(lock_path, "a+") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


__all__ = ["DEFAULT_CATALOG_PATH", "list_filings", "record_filings"]
