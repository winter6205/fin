"""Thread export: a comparison or citation bundle as a file (T4, #138).

Spec Focus 3 / SC6: "one authenticated-to-local GET or POST from the
thread returns a file body for citations or comparison; missing run ->
typed 404."

Binding to T3
-------------
The comparison export reads ``RunRecord.comparisons`` — the
``fin.comparison`` envelopes ``compare_facts`` produced this run
(shape SSOT: ``fin.host.tools.compare_facts``). The export renders
those envelopes and NOTHING else:

* every figure in the CSV is a cell's own ``display`` string;
* every redline is a delta's own ``display`` / ``direction``;
* an envelope carrying a different ``schema`` value is refused, not
  best-effort rendered;
* so is a recognized token whose ``schema_version`` is present and
  wrong (ADR 0093 decisions 6 and 7) — and so is a recognized token
  whose ``schema_version`` is ABSENT, as ``malformed_evidence_contract``
  (decision 12).

CONTEXT (``compare_facts`` entry) forbids "computing the delta in the
SPA or the export": this module therefore has no arithmetic at all, and
the delta section reproduces the tool's rows verbatim rather than
picking one delta per table row.

Typed failures (the HTTP layer maps ``code`` to the status):

* ``run_not_found`` (404) — the thread has no runs, or the caller named
  a ``run_id`` that is not on this thread;
* ``export_empty`` (404) — the thread has runs but none carries the
  requested kind;
* ``unsupported_comparison_schema`` (422) — the stored envelope is from
  a schema this export does not speak;
* ``malformed_citation_row`` — a stored citation row names no source
  this export can open, refused like ``unsupported_comparison_schema``
  rather than rendered with an empty identity column.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from fin.host.citation_gate import (
    COMPANY_KIND,
    DATABASE_ROW_KIND,
    DATABASE_ROW_SNAPSHOT_KEY,
    QUOTABLE_EVIDENCE_KINDS,
    format_database_row_source_id,
)
from fin.host.evidence_contract import _is_integer_value
from fin.host.tools.compare_facts import (
    COMPARISON_SCHEMA,
    COMPARISON_SCHEMA_VERSION,
)
from fin.retrieve.source_read import ATTACHMENT_KIND


ExportKind = Literal["comparison", "citations"]
ExportFormat = Literal["csv", "json"]

EXPORT_KINDS: tuple[str, ...] = ("comparison", "citations")
EXPORT_FORMATS: tuple[str, ...] = ("csv", "json")

_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
_JSON_MEDIA_TYPE = "application/json; charset=utf-8"


# ----- typed errors -----


class ExportError(Exception):
    """Base class carrying the wire ``code`` the route echoes."""

    code = "export_failed"


class RunNotFoundError(ExportError):
    """No run to export from (empty thread, or unknown ``run_id``)."""

    code = "run_not_found"


class ExportEmptyError(ExportError):
    """Runs exist, but none carries the requested kind."""

    code = "export_empty"


class UnsupportedComparisonSchemaError(ExportError):
    """The stored envelope is not ``fin.comparison``."""

    code = "unsupported_comparison_schema"


class MalformedCitationRowError(ExportError):
    """A stored citation row names no source this export can open."""

    code = "malformed_citation_row"


class UnsupportedExportError(ExportError):
    """Unknown ``kind`` / ``format`` query value."""

    code = "unsupported_export"


@dataclass(frozen=True)
class ExportFile:
    """One downloadable file: name, media type, and the whole body."""

    filename: str
    media_type: str
    body: str
    run_id: str


# ----- run selection -----


def _run_payload(run: Any, kind: str) -> list[dict[str, Any]]:
    if kind == "comparison":
        return list(getattr(run, "comparisons", None) or [])
    return list(getattr(run, "citations", None) or [])


def _select_run(runs: Sequence[Any], *, kind: str, run_id: str | None) -> Any:
    """The run this export renders.

    Explicit ``run_id`` wins (and a miss is ``run_not_found``, never a
    silent fallback onto a different turn). Otherwise the LATEST run
    that actually carries the kind — ``list_runs`` is oldest-first, so
    the operator's most recent comparison is the default download.
    """
    if not runs:
        raise RunNotFoundError("this thread has no runs to export")
    if run_id is not None:
        for run in runs:
            if run.id == run_id:
                if not _run_payload(run, kind):
                    raise ExportEmptyError(
                        f"run {run_id!r} has no {kind} to export"
                    )
                return run
        raise RunNotFoundError(f"unknown run {run_id!r} on this thread")
    for run in reversed(list(runs)):
        if _run_payload(run, kind):
            return run
    raise ExportEmptyError(f"no run on this thread has a {kind} to export")


# ----- csv bodies -----


_CsvRow = list[Any]


def _meta_rows(comparison: dict[str, Any]) -> list[_CsvRow]:
    """The envelope's identity: what was compared, in what unit, and
    under which declared shape version (the token names the shape; the
    version numbers it, so a reader of the CSV can tell two otherwise
    identical tables apart).

    WHY the version cell here is never empty: ADR 0093 decision 12
    refuses a recognized token whose ``schema_version`` is ABSENT, so by
    the time a row is rendered the version is always the current one and
    the cell always carries it. Decision 10's empty-cell rendering still
    describes the WRITER's default for an absent identity value (that is
    what ``concept``/``axis``/``unit``/``scale`` do below), but no
    comparison envelope reaches this function with an absent version —
    see :func:`_schema_version_problem`, which refuses one first. A
    record carrying NO ``schema`` at all never reaches here either: it is
    not ``fin.comparison`` and :func:`_bound_comparisons` drops it.
    """
    return [
        [field, comparison.get(field, "")]
        for field in ("schema", "schema_version", "concept", "axis", "unit", "scale")
    ]


def _table_rows(comparison: dict[str, Any]) -> list[_CsvRow]:
    """The grid: a header of column labels, then one row per fact row.

    Always non-empty — an envelope with no rows still declares its
    header, so the reader can tell "compared, found nothing" from
    "never compared".
    """
    columns = list(comparison.get("columns") or [])
    rows: list[_CsvRow] = [
        ["row"] + [c.get("label", c.get("key", "")) for c in columns]
    ]
    for row in comparison.get("rows") or []:
        cells = list(row.get("cells") or [])
        rows.append(
            [row.get("label", row.get("key", ""))]
            + [cell.get("display", "") for cell in cells]
        )
    return rows


def _delta_rows(comparison: dict[str, Any]) -> list[_CsvRow]:
    """The redline, verbatim: the tool's own ``direction`` / ``display``."""
    deltas = list(comparison.get("deltas") or [])
    if not deltas:
        return []
    rows: list[_CsvRow] = [
        ["from_row", "from_column", "to_row", "to_column", "direction", "delta"]
    ]
    for delta in deltas:
        start = delta.get("from") or {}
        end = delta.get("to") or {}
        rows.append(
            [
                start.get("row", ""),
                start.get("column", ""),
                end.get("row", ""),
                end.get("column", ""),
                delta.get("direction", ""),
                delta.get("display", ""),
            ]
        )
    return rows


def _locator_rows(comparison: dict[str, Any]) -> list[_CsvRow]:
    """The trail back to the filing units behind the grid."""
    locators = list(comparison.get("locators") or [])
    if not locators:
        return []
    rows: list[_CsvRow] = [["document_id", "version", "unit"]]
    for locator in locators:
        rows.append(
            [
                locator.get("document_id", ""),
                locator.get("version", ""),
                locator.get("unit", ""),
            ]
        )
    return rows


def _comparison_sections(comparison: dict[str, Any]) -> list[list[_CsvRow]]:
    """The sections one comparison contributes, empty ones dropped.

    A section that produced no rows contributes no blank separator
    either, so an envelope without deltas does not trail padding.
    """
    sections = (
        _meta_rows(comparison),
        _table_rows(comparison),
        _delta_rows(comparison),
        _locator_rows(comparison),
    )
    return [section for section in sections if section]


def _comparison_csv(comparisons: Sequence[dict[str, Any]]) -> str:
    """Render ``fin.comparison`` envelopes as one CSV document.

    Sections per comparison — metadata, the table, the deltas, the
    locators — because a single flat table cannot hold the redline and
    the source units without one of them becoming a magic column. One
    blank row separates sections, and comparisons from each other.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for index, comparison in enumerate(comparisons):
        if index:
            writer.writerow([])
        for section_index, section in enumerate(_comparison_sections(comparison)):
            if section_index:
                writer.writerow([])
            writer.writerows(section)
    return buffer.getvalue()


def _source_id_key(kind: str) -> str | None:
    """The stored citation field that opens a unit of ``kind``.

    Mirrors the gate's own identity rule (``citation_gate._citation_from``):
    the attachment leg is addressed by its attachment, the company leg by
    the corpus document, and a ledger row by NEITHER — its identity is
    its ``record_key`` under the snapshot key, which is read on its own
    below.

    ``None`` is a real answer, not a fallback: a kind outside the closed
    set has no id slot at all, and defaulting it to ``document_id`` would
    export an unknown kind as if it named a filing (ADR 0091).
    """
    if kind == COMPANY_KIND:
        return "document_id"
    if kind == ATTACHMENT_KIND:
        return "attachment_id"
    if kind == DATABASE_ROW_KIND:
        return DATABASE_ROW_SNAPSHOT_KEY
    return None


def _database_row_source_id(citation: Mapping[str, Any]) -> str:
    """The id that opens a stored ledger citation row, or ``""``.

    Read off the row's own retained snapshot — the permitted view plus
    the record key the query returned — so the exported trail names
    which view answered and which record, not a fabricated filing id.
    """
    snapshot = citation.get(DATABASE_ROW_SNAPSHOT_KEY)
    if not isinstance(snapshot, Mapping):
        return ""
    source = str(snapshot.get("source") or "")
    record_key = snapshot.get("record_key")
    if not source or not isinstance(record_key, Mapping) or not record_key:
        return ""
    return format_database_row_source_id(source, record_key)


def _row_source_id(citation: Mapping[str, Any], kind: str) -> str:
    """The one id that opens this citation row, read by its own kind."""
    key = _source_id_key(kind)
    if key == DATABASE_ROW_SNAPSHOT_KEY:
        return _database_row_source_id(citation)
    if key is None:
        return ""
    return str(citation.get(key) or "")


def _citations_csv(citations: Sequence[dict[str, Any]]) -> str:
    """One header row plus one row per verified citation span.

    ``source_id`` is the ONE id that opens the cited unit for that row's
    kind, because since the unit-citation contract the identity is
    kind-dependent (``document_id`` for a company row, ``attachment_id``
    for an attachment row, the view plus record key for a ledger row); a
    fixed ``document_id`` column exported an empty first cell for every
    attachment row, which no reader can open. A row that names no source
    at all is refused by :func:`_bound_citations` before this renders, so
    the column is never blank here.

    ``csv.writer`` owns the quoting: a citation quote is prose and will
    contain commas and double quotes, and a hand-rolled join would
    corrupt exactly the evidence this file exists to carry.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["kind", "source_id", "version", "unit", "quote"])
    for citation in citations:
        # A pre-migration row carries no ``kind``; it renders as the
        # company row it was written as, or its source_id cell would be
        # blank — the exact silent-empty-column failure this door exists
        # to refuse.
        kind = str(citation.get("kind") or "")
        if not kind and _is_pre_locator_rename_row(citation):
            kind = COMPANY_KIND
        writer.writerow(
            [
                kind,
                _row_source_id(citation, kind),
                citation.get("version", ""),
                citation.get("unit", ""),
                citation.get("quote", ""),
            ]
        )
    return buffer.getvalue()


# ----- payload binding and json body -----


def _schema_version_problem(comparison: Mapping[str, Any]) -> str | None:
    """Why this recognized-token envelope's ``schema_version`` is refused,
    or ``None`` when the export speaks it.

    The rule is the operator's frozen split (ADR 0093 decisions 6, 7 and
    12, spec §2.1 rules 1 and 3, and the same ladder
    ``evidence_contract._check_schema_version`` runs at every other
    boundary) so the export cannot drift from the validator that owns the
    fact:

    * ABSENT key — ``malformed_evidence_contract``. This is decision 12's
      ruling, not decision 9's: a token and its version are minted
      together, so an envelope that names ``fin.comparison`` and omits the
      number is a producer type bug, and NO component may infer, default,
      or back-fill it. Decision 9's absent-accepts half was explicitly
      "Superseded in part by decision 12"; the surviving half is the v0
      rule (below), and this export was re-synced to that supersession.
      An earlier version of this function read decision 9's PRE-ruling
      text and accepted the absent case, which was the one boundary on
      which this export disagreed with the validator and with the web.
    * PRESENT but not a finite integer BY VALUE (``1.5``, ``NaN``/``inf``,
      string, bool, null, list, object) — ``malformed_evidence_contract``: a
      producer's type bug, not a contract skew. ``bool`` is excluded
      explicitly because ``isinstance(True, int)`` is true in Python and
      ``True`` would otherwise pass as version ``1``.
    * PRESENT finite integer other than
      :data:`COMPARISON_SCHEMA_VERSION` — ``unsupported_evidence_contract``:
      a genuine version skew, refused rather than parsed by the nearest
      older reader.

    WHY the rule is over the DECODED VALUE and not the Python type: this
    module reads what the store already decoded, and Python's ``json.loads``
    gives the JSON text ``1.0`` the float ``1.0``. The operator's ruling is
    that BOTH ends validate a version as a finite integer value, so ``1.0``
    is the same version ``1`` and binds here exactly as it does in the
    validator (a JavaScript decoder turns the same ``1.0`` text into the
    integer ``1`` outright). The predicate is IMPORTED from
    ``evidence_contract`` rather than restated, so there is exactly one
    implementation of the rule on the Python side and the two boundaries
    cannot drift.

    Both classes name ``UnsupportedComparisonSchemaError`` on the export
    surface — the module's one comparison-schema refusal, whose ``code``
    string is frozen — and the message carries which of the two it was.

    WHAT STILL RENDERS AN EMPTY VERSION CELL, so decision 10 is not dead:
    nothing in this module's CSV path. Decision 10's "an absent version
    renders as an empty cell" was written to describe a case that reaches
    the CSV; decision 12 removed that case from this boundary. The
    ``comparison.get(field, "")`` default in :func:`_meta_rows` still
    renders an empty cell for every OTHER absent identity value, which is
    what decision 10's "exactly like the other absent identity values"
    describes.

    WHY decision 9's v0 half cannot reach this function at all: this
    function has exactly one caller, :func:`_bound_comparisons`, and that
    caller filters on ``c.get("schema") == COMPARISON_SCHEMA`` BEFORE
    calling. A record with NO ``schema`` field at all — the v0 shape
    decision 12 explicitly left untouched — is therefore dropped as
    another schema, and a run carrying only those raises
    ``unsupported_comparison_schema``. Decision 9's leniency half was
    never load-bearing here; it was unreachable in this module even before
    decision 12 withdrew it. This is the durable fact behind the absent
    branch's wording: the branch can only be reached by an envelope that
    already IS ``fin.comparison``, so the refusal describes a missing
    version beside a recognized token and nothing else.
    """
    if "schema_version" not in comparison:
        # WHY the message names no token: this function is only reached for
        # an envelope already bound as ``fin.comparison``, and the
        # interpolation below would otherwise read "a recognized None
        # token" or "a recognized 'fin.other' token" for the inputs the
        # caller filters out — describing as recognized a token this
        # module never recognized, and describing decision 9's v0 half
        # (no ``schema`` at all) as a missing version. State the actual
        # precondition instead.
        return (
            "malformed_evidence_contract: schema_version is absent beside "
            f"the {COMPARISON_SCHEMA} token (ADR 0093 decision 12: the token "
            "and its version are minted together and nothing back-fills it)"
        )
    version = comparison["schema_version"]
    if not _is_integer_value(version):
        return (
            "malformed_evidence_contract: schema_version "
            f"{version!r} is not a finite integer"
        )
    if version != COMPARISON_SCHEMA_VERSION:
        return (
            f"unsupported_evidence_contract: schema_version {version!r} "
            f"is not the current version {COMPARISON_SCHEMA_VERSION}"
        )
    return None


def _bound_comparisons(
    payload: Sequence[dict[str, Any]], *, run_id: Any
) -> list[dict[str, Any]]:
    """The stored envelopes this export speaks, or a typed refusal.

    An envelope from another schema is dropped rather than
    best-effort rendered; when that leaves nothing, the caller gets
    ``unsupported_comparison_schema`` naming what was actually found.

    A RECOGNIZED token carrying an unsupported ``schema_version`` is not
    dropped — it is refused. The CSV renders the declared version as its
    own identity row, so silently shipping a version skew would write the
    wrong number straight into a file an operator downloads, and a run
    that carries one has no envelope this export honestly speaks. The same
    holds for a recognized token with NO version at all, which decision 12
    classes as ``malformed_evidence_contract``; see
    :func:`_schema_version_problem`.
    """
    bound = [c for c in payload if c.get("schema") == COMPARISON_SCHEMA]
    for index, comparison in enumerate(bound):
        problem = _schema_version_problem(comparison)
        if problem is not None:
            raise UnsupportedComparisonSchemaError(
                f"run {run_id!r} comparison {index} carries an "
                f"unsupported {COMPARISON_SCHEMA} version: {problem}"
            )
    if not bound:
        raise UnsupportedComparisonSchemaError(
            "run "
            f"{run_id!r} carries no {COMPARISON_SCHEMA} comparison "
            f"(found {sorted({str(c.get('schema')) for c in payload})})"
        )
    return bound


def _is_pre_locator_rename_row(citation: Mapping[str, Any]) -> bool:
    """Whether this row is one the locator rename left in the database.

    A row written before the rename is ``{document_id, version, page,
    quote}``: no ``kind``, and a ``page`` where the current shape has
    ``unit``. Both halves matter. A row the CURRENT code wrote that merely
    lost ``kind`` still carries ``unit`` / ``display`` / ``filename``, and
    reading it would be guessing an identity the row does not state — which
    is the refusal :func:`_bound_citations` owes. So the test is the old
    shape itself, not merely the absence of ``kind``.
    """
    if str(citation.get("document_id") or "") == "":
        return False
    if "unit" in citation or "display" in citation or "filename" in citation:
        return False
    return "page" in citation


def _bound_citations(
    payload: Sequence[dict[str, Any]], *, run_id: Any
) -> list[dict[str, Any]]:
    """The stored citation rows this export can name a source for.

    Same posture as :func:`_bound_comparisons`: a row this export does
    not speak is refused, not best-effort rendered — and here the cost of
    rendering it is silent, since an empty ``source_id`` still downloads
    as a clean-looking file. Every row is checked before a single cell is
    written. The row's ``quote`` is user prose, so the refusal names the
    row index and the observed ``kind`` only.
    """
    for index, citation in enumerate(payload):
        kind = str(citation.get("kind") or "")
        if not kind and _is_pre_locator_rename_row(citation):
            # WHY a stored row is read rather than refused: this column is
            # JSONB and a citation row written before the locator rename is
            # {document_id, version, page, quote}. That is exactly what a
            # company citation was, and exactly what this door exported
            # before the rename — refusing it makes the whole citations
            # export unreachable for every thread created before the
            # rename. A row that is NOT that shape (one written by the
            # current code with only ``kind`` missing) is still refused
            # below: its identity cannot be read, only guessed.
            kind = COMPANY_KIND
        if kind not in QUOTABLE_EVIDENCE_KINDS:
            raise MalformedCitationRowError(
                f"run {run_id!r} citation row {index} has kind {kind!r}, "
                f"outside the quotable kinds {sorted(QUOTABLE_EVIDENCE_KINDS)}"
            )
        source_key = _source_id_key(kind)
        if source_key is None:
            # A kind outside the closed set has no id slot at all, and
            # the check above already refused it. Reaching here would
            # mean the closed set and this switch disagree, so it is
            # answered as the refusal it is rather than defaulted into
            # the document column.
            raise MalformedCitationRowError(
                f"run {run_id!r} citation row {index} has kind {kind!r}, "
                f"which names no source id slot"
            )
        if not _row_source_id(citation, kind):
            raise MalformedCitationRowError(
                f"run {run_id!r} citation row {index} (kind {kind!r}) "
                f"names no {source_key}"
            )
        if not str(citation.get("version") or ""):
            raise MalformedCitationRowError(
                f"run {run_id!r} citation row {index} (kind {kind!r}) "
                "names no version"
            )
    return list(payload)


def _json_body(
    *,
    thread_id: str,
    run_id: Any,
    kind: str,
    payload: Sequence[dict[str, Any]],
) -> str:
    """The JSON export document: the envelopes under a kind-named key."""
    return json.dumps(
        {
            "thread_id": thread_id,
            "run_id": run_id,
            "kind": kind,
            ("comparisons" if kind == "comparison" else "citations"): payload,
        },
        ensure_ascii=False,
        indent=2,
    )


# ----- the builder -----


def build_export(
    *,
    runs: Sequence[Any],
    kind: str,
    thread_id: str,
    run_id: str | None = None,
    fmt: str = "csv",
) -> ExportFile:
    """Build the downloadable file for one thread + kind.

    ``runs`` is the thread's ``RunRecord`` list oldest-first (exactly
    what ``ThreadStore.list_runs`` returns). Raises the typed errors
    documented at the top of this module; never returns an empty body
    for a "successful" export.
    """
    if kind not in EXPORT_KINDS:
        raise UnsupportedExportError(f"unknown export kind {kind!r}")
    if fmt not in EXPORT_FORMATS:
        raise UnsupportedExportError(f"unknown export format {fmt!r}")

    run = _select_run(runs, kind=kind, run_id=run_id)
    payload = _run_payload(run, kind)

    if kind == "comparison":
        payload = _bound_comparisons(payload, run_id=run.id)
    else:
        payload = _bound_citations(payload, run_id=run.id)

    stem = f"fin-{kind}-{str(run.id)[:8].rstrip('-_')}"
    if fmt == "json":
        return ExportFile(
            filename=f"{stem}.json",
            media_type=_JSON_MEDIA_TYPE,
            body=_json_body(
                thread_id=thread_id, run_id=run.id, kind=kind, payload=payload
            ),
            run_id=run.id,
        )

    body = (
        _comparison_csv(payload) if kind == "comparison" else _citations_csv(payload)
    )
    return ExportFile(
        filename=f"{stem}.csv",
        media_type=_CSV_MEDIA_TYPE,
        body=body,
        run_id=run.id,
    )


__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_KINDS",
    "ExportEmptyError",
    "ExportError",
    "ExportFile",
    "ExportFormat",
    "ExportKind",
    "MalformedCitationRowError",
    "RunNotFoundError",
    "UnsupportedComparisonSchemaError",
    "UnsupportedExportError",
    "build_export",
]
