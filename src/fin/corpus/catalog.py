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
calls to register pushed filings. A writer that owns a whole
document -- the demo seed, whose rows are generated from the fixture --
writes through :func:`replace_filings`, which replaces that document's
rows instead of adding a second row per version. Each version records
exactly one extract method (ADR 0079, stored-only): new rows default to
``standard`` and :func:`switch_extract_method` is the operator's
change path. Rows may additionally carry the EDGAR-shaped fields
``cik`` / ``form`` / ``ticker`` / ``filing_date`` (ADR 0031): ``form``
is emitted on every hit (spec SC3 of ``specs/enterprise-kb-agent.md``),
the others stay filter inputs only.

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

from fin.corpus.errors import (
    CatalogCorruptError,
    CatalogRowError,
    ExtractMethodConflictError,
    ExtractMethodTargetMissingError,
)
from fin.corpus.extract_method import (
    DEFAULT_EXTRACT_METHOD,
    EXTRACT_METHODS,
)
from fin.corpus.params import DOC_TYPES


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
DEFAULT_CATALOG_PATH = _DEFAULT_DATA_DIR / "catalog.json"


def default_corpus_ledger_path(
    filename: str,
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    user_home: Path | None = None,
) -> Path:
    """Ledger path for ``filename``.

    A primary checkout with ``FIN_HOME`` unset uses
    ``<user_home>/.fin/corpus``. A linked worktree uses
    ``<worktree>/.fin-home/corpus``. An explicit absolute ``FIN_HOME``
    wins. Resolving does not create the path or copy bytes. An explicit
    caller path is not decided here.
    """
    from fin.host.fin_home import resolve_fin_home

    home = resolve_fin_home(
        environ=environ, repo_root=repo_root, user_home=user_home
    )
    return home / "corpus" / filename

# The physical row contract. ``document_id`` / ``version`` / ``title``
# / ``doc_type`` must be non-empty strings; ``page_count`` is handled
# separately (int, not bool, >= 0).
#
# ``company`` and ``period`` are OPTIONAL (T6): an ordinary enterprise
# document has no financial identity, and a metadata-free upload has to
# be catalog-visible anyway. A present value must still be a string, and
# a missing one is OMITTED — never invented as ``""``, which would read
# as "a company that is the empty string" to every consumer.
_REQUIRED_ROW_STRING_FIELDS = (
    "document_id",
    "version",
    "title",
    "doc_type",
)

#: Fields a WRITE requires that a READ tolerates missing on a row the
#: table's earlier writers (the financial ingest) never recorded. The
#: read is the compatibility surface for those existing rows — spec
#: "Preserve existing ... legacy financial fields when present" — so a
#: row the old writer stored stays readable. Absence only: a present
#: value still must be a non-empty string.
_WRITE_ONLY_ROW_STRING_FIELDS = (
    "title",
    "doc_type",
)

#: Optional string fields: absent is fine, present must be a string.
#: They are NOT checked for emptiness — an existing financial writer
#: that stored ``""`` keeps its value rather than failing a read of the
#: table it wrote itself.
_OPTIONAL_ROW_STRING_FIELDS = (
    "company",
    "period",
)

# The one EDGAR-shaped field emitted on a hit (spec SC3). ``cik`` /
# ``ticker`` / ``filing_date`` / ``accession`` / ``source_sha256``
# stay stored-only.
#
# This is the FINANCIAL compatibility projection for the existing
# ``list_files`` callers, unchanged by T6: same six keys, same AND
# combination, same empty-hits-not-exceptions rule. The generic read
# model is :func:`list_catalog_entries` — the two are deliberately
# separate so a financial read cannot grow ``title`` / ``doc_type`` and
# a generic read cannot inherit the four legacy filter arguments.
_OUTPUT_KEYS = (
    "document_id",
    "company",
    "period",
    "version",
    "page_count",
    "form",
)

#: The generic read model's always-present keys. ``company`` /
#: ``period`` join it only when the row carries them.
_GENERIC_OUTPUT_KEYS = (
    "document_id",
    "title",
    "doc_type",
    "version",
    "page_count",
)

#: The doc_type reported for a legacy row that never recorded one: the
#: closed-vocabulary member meaning "no reliable type recorded"
#: (:data:`fin.corpus.params.DOC_TYPES`). The membership check pins the
#: literal to the vocabulary — removing ``unknown`` from DOC_TYPES
#: fails here instead of drifting silently.
_LEGACY_DOC_TYPE = "unknown"
assert _LEGACY_DOC_TYPE in DOC_TYPES

#: Grouping candidates, in presentation order. ``title`` is a
#: display/filter value and is NEVER a grouping dimension; department and
#: time-range dimensions are outside this contract.
GROUPING_KEYS = ("doc_type", "company", "period")

#: The public metadata-filter allowlist (spec qa-chain-corpus-retrieval
#: §"Retrieve request and metadata filters"): the only keys a generic
#: metadata filter may name, in contract order. Catalog-owned so the
#: filter contract and the catalog's own row fields cannot drift apart.
METADATA_FILTER_KEYS: tuple[str, ...] = (
    "title",
    "doc_type",
    "company",
    "period",
)

#: Where a grouping bucket puts a row with no value for the dimension.
UNCLASSIFIED_LABEL = "未分类"


def _legacy_display_title(row: Mapping[str, Any]) -> str:
    """Display title for a legacy row that never recorded one.

    Built from the financial identity the row does carry — company,
    period, form — so the operator sees the same identity the
    financial projection shows. Deterministic; degrades to the
    document id when even the identity is absent.
    """
    company = row.get("company")
    period = row.get("period")
    form = row.get("form")
    identity = " ".join(
        str(part)
        for part in (company, period, form)
        if isinstance(part, str) and part
    )
    return identity if identity else str(row.get("document_id", ""))


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
    period, version, page_count}`` plus the optional ``source_sha256``
    and the optional ``extract_method``; extra fields are tolerated
    and carried inert (the emitted hit shape is fixed by
    ``list_filings``, so extra storage fields never leak onto the
    wire).

    Exactly one extract method is recorded per version (ADR 0079). A
    row without ``extract_method`` keeps what the version already
    records — a plain re-push never flips an operator's fast version
    back to standard — and defaults to standard for a new version
    (ADR 0068). An explicit method that differs from the stored one is
    the typed ``ExtractMethodConflictError``, not a dual graph and not
    a silent overwrite; changing the method is the operator switch's
    job (:func:`switch_extract_method`).

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
        merged = _upsert_rows(_load_rows(catalog_path), validated)
        _write_rows(catalog_path, sorted(merged.values(), key=_row_key))
    return len(validated)


def list_catalog_entries(
    *, catalog_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """The GENERIC read model: every row as a catalog document.

    One entry per physical row, sorted by ``(document_id, version)`` for
    deterministic output. Each entry carries ``document_id``, ``title``,
    ``doc_type``, ``version`` and ``page_count``, plus ``company`` and
    ``period`` ONLY when the row has them — an absent value is omitted
    rather than reported as ``None`` or ``""``, so a consumer can tell
    "this document claims no company" from "this document's company is
    blank".

    Legacy financial fields (``cik`` / ``form`` / ``ticker`` /
    ``filing_date`` / ``source_sha256`` / ``extract_method``) stay
    stored-only: they exist for :func:`list_filings`, not for the generic
    surface, and the four old filter arguments are never generic metadata
    keys. A missing table is the empty list; a broken table is the typed
    ``CatalogCorruptError``, exactly as for the financial read.

    A legacy row that never recorded ``title`` / ``doc_type`` — the rows
    the pre-generic financial writer stored — still yields a complete
    generic entry: the title is derived from the row's financial
    identity (company / period / form, falling back to the document id)
    and ``doc_type`` is the closed-vocabulary member ``unknown``. The
    synthesized values exist for this projection only; the physical row
    on disk is never rewritten with them.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    entries: list[dict[str, Any]] = []
    for row in sorted(_load_rows(Path(catalog_path)), key=_row_key):
        entries.append(_generic_entry(row))
    return entries


def catalog_document_titles(
    *, catalog_path: Path | str | None = None
) -> dict[str, str | None]:
    """Each catalog document's STORED title, keyed by ``document_id``.

    Every document id in the table is a key. A document whose rows never
    recorded a title maps to ``None`` rather than to the generic read
    model's synthesized display value: that value is derived from
    company/period identity, which a caller may not depend on. An
    untitled document's caller is expected to fall back to the document
    id, and an id absent from this mapping is not in the catalog at all.

    A missing table is the empty mapping; a broken table is the typed
    ``CatalogCorruptError`` exactly as for the other reads.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    titles: dict[str, str | None] = {}
    for row in _load_rows(Path(catalog_path)):
        document_id = row.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            continue
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            titles.setdefault(document_id, title.strip())
        else:
            titles.setdefault(document_id, None)
    return titles


def _generic_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """One physical row as a generic read-model entry.

    Legacy synthesis happens here — and only here, on the projection:
    a row that never recorded ``title`` / ``doc_type`` gets the derived
    display title and the ``unknown`` doc_type, so every generic
    consumer (read model, grouping) sees the same complete entry while
    the physical row on disk is never rewritten with the synthesized
    values.
    """
    entry = {key: row.get(key) for key in _GENERIC_OUTPUT_KEYS}
    if "title" not in row:
        entry["title"] = _legacy_display_title(row)
    if "doc_type" not in row:
        entry["doc_type"] = _LEGACY_DOC_TYPE
    for field in _OPTIONAL_ROW_STRING_FIELDS:
        if field in row:
            entry[field] = row[field]
    return entry


def catalog_grouping(
    rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    catalog_path: Path | str | None = None,
) -> dict[str, Any]:
    """Group the catalog for presentation.

    Returns ``{"dimensions": [...], "flat_titles": [...]}``:

    * ``dimensions`` — one entry per QUALIFYING candidate, in the fixed
      :data:`GROUPING_KEYS` order. A candidate qualifies only with at
      least two distinct non-empty values, so a corpus where every
      document shares one ``doc_type`` offers no ``doc_type`` grouping
      rather than a single-item one. Each entry is
      ``{"key", "label", "groups": [{"value", "document_ids"}]}`` with
      values sorted, and rows with no value for that dimension grouped
      under :data:`UNCLASSIFIED_LABEL`;
    * ``flat_titles`` — every row's ``title``, sorted and de-duplicated,
      used only when NO dimension qualifies.

    ``title`` is never a dimension: it is a display/filter value, and a
    per-title grouping would be the identity function over the corpus.

    Rows are the physical rows (pass them) or the table's rows (name
    ``catalog_path``); both forms read the same fields. The table form
    projects through the generic read model first, so a legacy row
    groups by its synthesized ``doc_type`` (``unknown``) exactly as it
    would when the caller passes :func:`list_catalog_entries` output —
    one corpus, one presentation.
    """
    if rows is None:
        if catalog_path is None:
            catalog_path = DEFAULT_CATALOG_PATH
        rows = [_generic_entry(row) for row in _load_rows(Path(catalog_path))]
    ordered = sorted(rows, key=_row_key)
    dimensions: list[dict[str, Any]] = []
    for key in GROUPING_KEYS:
        groups = _groups_for(ordered, key)
        if len(groups) < 2:
            continue  # EXIT: one bucket is not a grouping
        dimensions.append(
            {
                "key": key,
                "label": GROUPING_LABELS[key],
                "groups": groups,
            }
        )
    return {
        "dimensions": dimensions,
        "flat_titles": _flat_titles(ordered),
    }


#: Operator-facing names for the grouping candidates. Pinned here so the
#: label and the key cannot drift apart between the host route and the
#: SPA, which both render from this projection.
GROUPING_LABELS: dict[str, str] = {
    "doc_type": "文档类型",
    "company": "公司",
    "period": "期间",
}


def _groups_for(
    rows: list[Mapping[str, Any]], key: str
) -> list[dict[str, Any]]:
    """One dimension's buckets, sorted by value with 未分类 last.

    A blank / non-string value is "missing", not a bucket of its own:
    ``""`` and ``None`` both land under 未分类, so the operator sees one
    unclassified bucket instead of several indistinguishable empties.
    """
    buckets: dict[str, list[str]] = {}
    for row in rows:
        value = row.get(key)
        label = value if isinstance(value, str) and value else UNCLASSIFIED_LABEL
        buckets.setdefault(label, []).append(str(row.get("document_id", "")))
    ordered = sorted(
        (label for label in buckets if label != UNCLASSIFIED_LABEL)
    )
    if UNCLASSIFIED_LABEL in buckets:
        ordered.append(UNCLASSIFIED_LABEL)
    return [
        {"value": label, "document_ids": sorted(buckets[label])}
        for label in ordered
    ]


def _flat_titles(rows: list[Mapping[str, Any]]) -> list[str]:
    """The sorted, de-duplicated titles of rows with one."""
    titles = {
        title
        for title in (row.get("title") for row in rows)
        if isinstance(title, str) and title
    }
    return sorted(titles)
def replace_filings(
    rows: Iterable[Mapping[str, Any]],
    *,
    document_ids: Iterable[str],
    catalog_path: Path | str | None = None,
) -> int:
    """Replace the rows of ``document_ids`` with ``rows``; return the count written.

    :func:`record_filings` upserts on ``(document_id, version)``, so a new
    version of a document is a *second* row. A writer that owns a whole
    document -- the demo seed, whose rows are generated from the fixture --
    must not leave the previous version's row behind, or one document shows
    up twice in :func:`list_filings`. This replaces every row of
    ``document_ids`` with the given rows; the rows of other documents are
    carried through untouched.

    A row whose ``(document_id, version)`` is already in the table keeps the
    extract method the table records (ADR 0068): replacing a row is not a
    method switch. A row of a document outside ``document_ids`` is a named
    ``CatalogRowError``, not a silent write the caller did not ask for.

    Concurrency follows the same ``flock`` + atomic-rewrite discipline as
    :func:`record_filings`.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    catalog_path = Path(catalog_path)

    validated = [
        _validated_row(row, index) for index, row in enumerate(rows)
    ]
    if not validated:
        return 0

    replaced = set(document_ids)
    outside = sorted({row["document_id"] for row in validated} - replaced)
    if outside:
        raise CatalogRowError(
            f"rows for documents {outside} are outside the replaced set "
            f"{sorted(replaced)}; replace_filings only replaces the "
            "documents it is given"
        )
    incoming = {_row_key(row) for row in validated}

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with _table_lock(catalog_path):
        merged = _upsert_rows(_load_rows(catalog_path), validated)
        kept = [
            row
            for key, row in merged.items()
            if key[0] not in replaced or key in incoming
        ]
        _write_rows(catalog_path, sorted(kept, key=_row_key))
    return len(validated)


def switch_extract_method(
    document_id: str,
    version: str,
    method: str,
    *,
    catalog_path: Path | str | None = None,
) -> None:
    """Operator switch (ADR 0068): record ``method`` on one version.

    This is the only sanctioned path that changes a version's extract
    method after it is recorded. A real change also marks the row
    ``rebuild_pending`` — switching means the version must be
    re-extracted (ADR 0079), and that job belongs to iknow-memory, not
    to fin. Re-switching to the already-recorded method is a no-op: no
    spurious rebuild intent.

    Named failures, table untouched: an out-of-set ``method`` is
    ``CatalogRowError``; a ``(document_id, version)`` with no row is
    ``ExtractMethodTargetMissingError`` (the switch never creates a
    version). Concurrency follows the same ``flock`` + atomic-rewrite
    discipline as :func:`record_filings`.
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH
    catalog_path = Path(catalog_path)
    if method not in EXTRACT_METHODS:
        raise CatalogRowError(
            f"extract method {method!r} is not one of "
            f"{sorted(EXTRACT_METHODS)}"
        )

    with _table_lock(catalog_path):
        by_key = {_row_key(row): row for row in _load_rows(catalog_path)}
        key = (document_id, version)
        stored = by_key.get(key)
        if stored is None:
            raise ExtractMethodTargetMissingError(
                f"no catalog row for (document_id={document_id!r}, "
                f"version={version!r}); the switch changes a recorded "
                "version, it never creates one"
            )
        current = stored.get("extract_method") or DEFAULT_EXTRACT_METHOD
        if current == method:
            return
        by_key[key] = {
            **stored,
            "extract_method": method,
            "rebuild_pending": True,
        }
        _write_rows(catalog_path, sorted(by_key.values(), key=_row_key))


# ---------- row contract ----------


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (row["document_id"], row["version"])


def _upsert_rows(
    existing: list[dict[str, Any]],
    validated: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Merge ``validated`` into ``existing`` on the ``(document_id, version)`` key.

    One merge for every writer, so the upsert key and the extract-method
    resolution stay single-sourced in this module.
    """
    merged = {_row_key(row): row for row in existing}
    for row in validated:
        merged[_row_key(row)] = _with_recorded_method(row, merged)
    return merged


def _with_recorded_method(
    row: dict[str, Any],
    merged: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve the one extract method a version records on write.

    New versions default to standard (ADR 0068); a re-push without a
    method inherits what the version already records, so a plain
    ingest never silently flips an operator's pick. An explicit
    method that differs from the recorded one is the typed
    ``ExtractMethodConflictError`` — checked against ``merged`` so a
    second method inside the same batch fails too, before any write.
    """
    key = _row_key(row)
    incoming = row.get("extract_method")
    stored = merged.get(key)
    if stored is None:
        return {
            **row,
            "extract_method": incoming or DEFAULT_EXTRACT_METHOD,
        }
    stored_method = stored.get("extract_method") or DEFAULT_EXTRACT_METHOD
    if incoming is None:
        return {**row, "extract_method": stored_method}
    if incoming != stored_method:
        raise ExtractMethodConflictError(
            f"(document_id={key[0]!r}, version={key[1]!r}) already "
            f"records extract method {stored_method!r}; a second, "
            "different method on one version is rejected (ADR 0079) "
            "— use switch_extract_method to change it"
        )
    return row


def _validated_row(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Validate one row against the catalog row contract.

    Returns a plain-dict copy. Raises ``CatalogRowError`` naming the
    row index and the offending field — the error message is part of
    the contract, so the ingest side can surface which row drifted.

    The ``source_sha256 == version`` equality is enforced HERE and not
    on the read path: the spec's rule is about what a NEW row writes
    (qa-chain-corpus-retrieval §"Catalog fields and compatibility
    projection"), and the read path already tolerates the rows earlier
    writers stored. A row whose two digests disagree is one the writer
    must not create; an existing one stays readable so a compatibility
    read cannot be broken by a rule aimed at new writes.
    """
    validated = _validated_row_where(
        row,
        index,
        tolerated_absent=(),
    )
    source_sha256 = validated.get("source_sha256")
    if source_sha256 is not None and source_sha256 != validated.get("version"):
        raise CatalogRowError(
            f"row #{index} field 'source_sha256' must equal 'version' when "
            f"present, got {source_sha256!r} against version "
            f"{validated.get('version')!r}"
        )
    return validated


def _validated_read_row(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Validate one row as it may exist ON DISK, across writers.

    The write path (:func:`_validated_row`) demands every field the
    current writer records; the read path also serves the rows that
    writer's predecessors stored, which never carried ``title`` /
    ``doc_type``. Only ABSENCE is forgiven: every other constraint,
    and a present-but-invalid value for these fields too, stays at
    write strength, so a genuinely corrupt row still reads as the
    typed ``CatalogRowError`` (wrapped as ``CatalogCorruptError`` by
    the loader).
    """
    return _validated_row_where(
        row,
        index,
        tolerated_absent=_WRITE_ONLY_ROW_STRING_FIELDS,
    )


def _validated_row_where(
    row: Mapping[str, Any],
    index: int,
    *,
    tolerated_absent: tuple[str, ...],
) -> dict[str, Any]:
    where = f"row #{index}"
    if not isinstance(row, Mapping):
        raise CatalogRowError(
            f"{where} must be a mapping, got {type(row).__name__}"
        )
    validated = dict(row)
    for field in _REQUIRED_ROW_STRING_FIELDS:
        if field in tolerated_absent and field not in validated:
            continue
        value = validated.get(field)
        if not isinstance(value, str) or not value:
            raise CatalogRowError(
                f"{where} field {field!r} must be a non-empty string, "
                f"got {value!r}"
            )
    for field in _OPTIONAL_ROW_STRING_FIELDS:
        if field in validated and not isinstance(validated[field], str):
            raise CatalogRowError(
                f"{where} field {field!r} must be a string or absent, "
                f"got {validated[field]!r}"
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
    extract_method = validated.get("extract_method")
    if extract_method is not None and extract_method not in EXTRACT_METHODS:
        raise CatalogRowError(
            f"{where} field 'extract_method' must be one of "
            f"{sorted(EXTRACT_METHODS)}, got {extract_method!r}"
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
            _validated_read_row(row, index)
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


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "GROUPING_KEYS",
    "GROUPING_LABELS",
    "METADATA_FILTER_KEYS",
    "UNCLASSIFIED_LABEL",
    "catalog_document_titles",
    "catalog_grouping",
    "default_corpus_ledger_path",
    "list_catalog_entries",
    "list_filings",
    "record_filings",
    "replace_filings",
    "switch_extract_method",
]
