"""Thread export: a comparison or citation bundle as a file (T4, #138).

Spec Focus 3 / SC6: "one authenticated-to-local GET or POST from the
thread returns a file body for citations or comparison; missing run ->
typed 404."

Binding to T3
-------------
The comparison export reads ``RunRecord.comparisons`` — the
``fin.comparison.v1`` envelopes ``compare_facts`` produced this run
(shape SSOT: ``fin.host.tools.compare_facts``). The export renders
those envelopes and NOTHING else:

* every figure in the CSV is a cell's own ``display`` string;
* every redline is a delta's own ``display`` / ``direction``;
* an envelope carrying a different ``schema`` value is refused, not
  best-effort rendered.

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
  a schema this export does not speak.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from fin.host.tools.compare_facts import COMPARISON_SCHEMA


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
    """The stored envelope is not ``fin.comparison.v1``."""

    code = "unsupported_comparison_schema"


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
    """The envelope's identity: what was compared, in what unit."""
    return [
        [field, comparison.get(field, "")]
        for field in ("schema", "concept", "axis", "unit", "scale")
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
    """The trail back to the filing pages behind the grid."""
    locators = list(comparison.get("locators") or [])
    if not locators:
        return []
    rows: list[_CsvRow] = [["document_id", "version", "page"]]
    for locator in locators:
        rows.append(
            [
                locator.get("document_id", ""),
                locator.get("version", ""),
                locator.get("page", ""),
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
    """Render ``fin.comparison.v1`` envelopes as one CSV document.

    Sections per comparison — metadata, the table, the deltas, the
    locators — because a single flat table cannot hold the redline and
    the source pages without one of them becoming a magic column. One
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


def _citations_csv(citations: Sequence[dict[str, Any]]) -> str:
    """One header row plus one row per verified citation span.

    ``csv.writer`` owns the quoting: a citation quote is prose and will
    contain commas and double quotes, and a hand-rolled join would
    corrupt exactly the evidence this file exists to carry.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["document_id", "version", "page", "quote"])
    for citation in citations:
        writer.writerow(
            [
                citation.get("document_id", ""),
                citation.get("version", ""),
                citation.get("page", ""),
                citation.get("quote", ""),
            ]
        )
    return buffer.getvalue()


# ----- payload binding and json body -----


def _bound_comparisons(
    payload: Sequence[dict[str, Any]], *, run_id: Any
) -> list[dict[str, Any]]:
    """The stored envelopes this export speaks, or a typed refusal.

    An envelope from another schema is dropped rather than
    best-effort rendered; when that leaves nothing, the caller gets
    ``unsupported_comparison_schema`` naming what was actually found.
    """
    bound = [c for c in payload if c.get("schema") == COMPARISON_SCHEMA]
    if not bound:
        raise UnsupportedComparisonSchemaError(
            "run "
            f"{run_id!r} carries no {COMPARISON_SCHEMA} comparison "
            f"(found {sorted({str(c.get('schema')) for c in payload})})"
        )
    return bound


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
    "RunNotFoundError",
    "UnsupportedComparisonSchemaError",
    "UnsupportedExportError",
    "build_export",
]
