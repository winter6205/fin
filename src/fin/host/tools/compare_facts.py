"""Host tool for cross-period / peer comparison (T3, spec Focus 1).

Spec Focus 1: "Cross-period and peer comparison as **host tools**
(not free-prose tables). Workspace renders the tool result on the
turn (table or redline), then the **citation verifier** still gates
any prose answer." This module is that tool.

Contract
--------
``compare_facts`` resolves one XBRL ``concept`` over a grid of
**rows** (issuers or documents) x **columns** (periods) against the
ingest-built fact table (``fin.corpus.facts``), and returns the
``fin.comparison.v1`` envelope described below. Numbers come from
fact rows with a ``locator`` each; the deltas are computed HERE, so
the model never authors the arithmetic (CONTEXT ``get_xbrl_fact``).

The envelope is the shape the workspace turn row persists and the
T4 export binds to::

    {
      "comparison": {
        "schema": "fin.comparison.v1",
        "kind": "table",                  # rendering hint; v1 = table
        "axis": "period"|"peer"|"matrix"|"single",
        "concept": "us-gaap:Revenues",
        "unit": "USD",                    # first hit's unit ("" if none)
        "scale": "millions",              # first hit's scale ("" if none)
        "columns": [{"key": "2023-Q3-YTD", "label": "2023-Q3-YTD"}],
        "rows": [
          {
            "key": "AAPL",
            "label": "AAPL",
            "kind": "issuer"|"document",
            "cells": [
              {
                "row": "AAPL",
                "column": "2023-Q3-YTD",
                "status": "hit"|"missing",
                "value": [private source value removed].0|null,   # null when missing OR textual
                "display": "[private source value removed]",     # "—" when missing
                "unit": "USD",
                "scale": "millions",
                "locator": {"document_id","version","page"}|null
              }
            ]
          }
        ],
        "deltas": [
          {
            "from": {"row": "AAPL", "column": "2022-Q3-YTD"},
            "to":   {"row": "AAPL", "column": "2023-Q3-YTD"},
            "absolute": 99647.0,
            "percent": 51.33|null,        # null at a zero baseline
            "direction": "up"|"down"|"flat",
            "display": "+99,647 (+51.33%)"
          }
        ],
        "missing": [{"row","column","reason"}],
        "hit_count": 2,
        "cell_count": 2,
        "locators": [{"document_id","version","page"}]
      }
    }

Typed EXITs (dict values, never exceptions — spec § Boundaries
"empty hits not exceptions"):

* ``{"error": "empty_comparison", "comparison": <empty skeleton>}``
  — no concept, no periods, or no rows to scope. The skeleton is
  always present so the SPA and the export never branch on a
  missing key.
* ``{"error": "comparison_overflow", "limit", "requested",
  "comparison": <empty skeleton>}`` — the requested grid exceeds
  :data:`MAX_CELLS`. A comparison the operator cannot read is a
  refusal, not a multi-thousand-cell payload on the SSE wire.

Like ``get_xbrl_fact``, this tool contributes NO ``EvidenceItem``:
a fact row is not page text, so the citation verifier still needs a
``read_file`` / ``retrieve`` span before the prose answer can pass.
The numeric gate is what binds the comparison's numbers to the
answer (see ``fin.host.graph``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from fin.corpus.facts import Fact, lookup_fact, list_fact_documents


COMPARISON_SCHEMA = "fin.comparison.v1"

# Upper bound on ``len(rows) * len(columns)``. 64 cells is already a
# table no operator reads in one glance; beyond it the payload is
# the problem, not the answer. The cap is on the REQUEST so the tool
# never walks the fact table for a grid it would refuse anyway.
MAX_CELLS = 64

# Rendered placeholder for a cell with no fact row. A blank cell
# reads as "zero"; the em dash reads as "not reported".
MISSING_DISPLAY = "—"


# ----- small pure helpers -----


def _clean(values: Iterable[str] | None) -> list[str]:
    """Drop blanks and duplicates, preserving first-seen order."""
    out: list[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def format_comparison_value(value: Any) -> str:
    """Render a cell value the way the filing prints it.

    Integral floats lose the trailing ``.0`` a JSON round-trip adds,
    so ``[private source value removed].0`` still reads as ``[private source value removed]``. Textual XBRL values
    pass through verbatim; ``None`` is the missing placeholder.
    """
    if value is None:
        return MISSING_DISPLAY
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,}"


def _numeric(value: Any) -> float | None:
    """The comparable float behind a fact value, else ``None``.

    Booleans and strings are not arithmetic: a string-tagged fact is
    reported in the cell but never feeds a delta.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _locator_dict(fact: Fact) -> dict[str, Any]:
    return {
        "document_id": fact.source_locator.document_id,
        "version": fact.source_locator.version,
        "page": fact.source_locator.page,
    }


def _empty_comparison(concept: str) -> dict[str, Any]:
    """The skeleton every typed EXIT still carries."""
    return {
        "schema": COMPARISON_SCHEMA,
        "kind": "table",
        "axis": "single",
        "concept": concept,
        "unit": "",
        "scale": "",
        "columns": [],
        "rows": [],
        "deltas": [],
        "missing": [],
        "hit_count": 0,
        "cell_count": 0,
        "locators": [],
    }


def _axis(row_count: int, column_count: int) -> str:
    if row_count > 1 and column_count > 1:
        return "matrix"
    if column_count > 1:
        return "period"
    if row_count > 1:
        return "peer"
    return "single"


# ----- row scoping -----


def _issuer_documents(issuer: str, documents: list[str]) -> list[str]:
    """Documents whose ``document_id`` belongs to ``issuer``.

    ``document_id`` is ``TICKER-YYYYQn`` (ADR 0006 / manifest), so the
    ticker prefix is the issuer key. An issuer with no fact file
    resolves to no candidates and its row is all misses — a dropped
    row would hide the gap the operator asked about.
    """
    prefix = f"{issuer.upper()}-"
    return [doc for doc in documents if doc.upper().startswith(prefix)]


def _resolve_rows(
    *,
    document_ids: list[str],
    issuers: list[str],
    facts_root: Path | None,
) -> list[tuple[str, str, list[str]]]:
    """Return ``(key, kind, candidate document_ids)`` per row.

    Precedence: explicit ``document_ids`` (document rows) beat
    ``issuers`` (issuer rows, resolved across that issuer's
    documents); with neither, every document holding facts becomes a
    row so an unscoped compare still shows the library.
    """
    if document_ids:
        return [(doc, "document", [doc]) for doc in document_ids]
    known = list_fact_documents(facts_root=facts_root) if facts_root else []
    if issuers:
        return [
            (issuer, "issuer", _issuer_documents(issuer, known))
            for issuer in issuers
        ]
    return [(doc, "document", [doc]) for doc in known]


def _lookup_cell_fact(
    *,
    concept: str,
    period: str,
    candidates: list[str],
    facts_root: Path | None,
) -> Fact | None:
    """First candidate document holding ``(concept, period)``.

    Read-only: ``lookup_fact`` opens and closes its own file per
    call, so concurrent comparisons cannot see a torn table.
    """
    for document_id in candidates:
        result = lookup_fact(
            concept=concept,
            period=period,
            document_id=document_id,
            facts_root=facts_root,
        )
        if isinstance(result, Fact):
            return result
    return None


# ----- delta computation (the arithmetic the model must not author) -----


def _format_delta(absolute: float, percent: float | None) -> str:
    sign = "+" if absolute >= 0 else "-"
    head = f"{sign}{format_comparison_value(abs(absolute))}"
    if percent is None:
        return head
    return f"{head} ({sign}{abs(percent):.2f}%)"


def _delta_entry(
    base: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any] | None:
    """One redline row between two cells, or ``None`` when either end
    has no comparable number (a delta off a missing cell would be an
    invented figure)."""
    base_value = base["value"]
    target_value = target["value"]
    if base_value is None or target_value is None:
        return None
    absolute = float(target_value) - float(base_value)
    percent = (
        None if base_value == 0 else absolute / abs(float(base_value)) * 100.0
    )
    direction = "flat" if absolute == 0 else ("up" if absolute > 0 else "down")
    return {
        "from": {"row": base["row"], "column": base["column"]},
        "to": {"row": target["row"], "column": target["column"]},
        "absolute": absolute,
        "percent": None if percent is None else round(percent, 4),
        "direction": direction,
        "display": _format_delta(
            absolute, None if percent is None else round(percent, 2)
        ),
    }


def _compute_deltas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redline rows for the comparison's dominant axis.

    * Several columns -> consecutive period-over-period deltas within
      each row (cross-period, and each row of a matrix).
    * One column, several rows -> every row against the FIRST row,
      which is the baseline the operator named first (peer).
    """
    deltas: list[dict[str, Any]] = []
    column_count = len(rows[0]["cells"]) if rows else 0
    if column_count > 1:
        for row in rows:
            cells = row["cells"]
            for index in range(1, len(cells)):
                entry = _delta_entry(cells[index - 1], cells[index])
                if entry is not None:
                    deltas.append(entry)
        return deltas
    if column_count == 1 and len(rows) > 1:
        baseline = rows[0]["cells"][0]
        for row in rows[1:]:
            entry = _delta_entry(baseline, row["cells"][0])
            if entry is not None:
                deltas.append(entry)
    return deltas


# ----- the tool -----


def _build_cell(
    *, row_key: str, period: str, fact: Fact | None
) -> dict[str, Any]:
    if fact is None:
        return {
            "row": row_key,
            "column": period,
            "status": "missing",
            "value": None,
            "display": MISSING_DISPLAY,
            "unit": "",
            "scale": "",
            "locator": None,
        }
    return {
        "row": row_key,
        "column": period,
        "status": "hit",
        "value": _numeric(fact.value),
        "display": format_comparison_value(fact.value),
        "unit": fact.unit,
        "scale": fact.scale,
        "locator": _locator_dict(fact),
    }


@dataclass(frozen=True)
class _GridRequest:
    """One normalized comparison request — the parameter object the
    phases below share, so ``concept`` / ``columns`` / ``facts_root``
    are not re-threaded through every helper signature."""

    concept: str
    columns: list[str]
    row_specs: list[tuple[str, str, list[str]]]
    facts_root: Path | None

    @property
    def cell_count(self) -> int:
        """Cells asked for — what :data:`MAX_CELLS` caps."""
        return len(self.row_specs) * len(self.columns)


@dataclass
class _Grid:
    """A walked grid: the cell rows plus the tallies no cell can see.

    ``unit`` / ``scale`` are the FIRST hit's, because the envelope
    reports one of each; ``locators`` de-duplicates in first-seen
    order so the citation list matches reading order."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    locators: list[dict[str, Any]] = field(default_factory=list)
    unit: str = ""
    scale: str = ""
    hit_count: int = 0

    def record(self, *, row_key: str, period: str, fact: Fact | None) -> None:
        if fact is None:
            self.missing.append(
                {"row": row_key, "column": period, "reason": "fact_not_found"}
            )
            return
        self.hit_count += 1
        self.unit = self.unit or fact.unit
        self.scale = self.scale or fact.scale
        locator = _locator_dict(fact)
        if locator not in self.locators:
            self.locators.append(locator)


def _build_row(
    *,
    request: _GridRequest,
    spec: tuple[str, str, list[str]],
    grid: _Grid,
) -> dict[str, Any]:
    """One grid row: a cell per column, tallied as each cell resolves."""
    row_key, row_kind, candidates = spec
    cells: list[dict[str, Any]] = []
    for period in request.columns:
        fact = _lookup_cell_fact(
            concept=request.concept,
            period=period,
            candidates=candidates,
            facts_root=request.facts_root,
        )
        grid.record(row_key=row_key, period=period, fact=fact)
        cells.append(_build_cell(row_key=row_key, period=period, fact=fact))
    return {"key": row_key, "label": row_key, "kind": row_kind, "cells": cells}


def _build_grid(request: _GridRequest) -> _Grid:
    """Walk the row x column grid against the fact table, row-major.

    The request is capped before this runs (:func:`_refusal`), so the
    walk carries no bound of its own."""
    grid = _Grid()
    for spec in request.row_specs:
        grid.rows.append(_build_row(request=request, spec=spec, grid=grid))
    return grid


def _refusal(request: _GridRequest) -> dict[str, Any] | None:
    """The typed EXIT this request earns, or ``None`` to walk it."""
    if not request.concept or not request.columns or not request.row_specs:
        return {
            "error": "empty_comparison",
            "comparison": _empty_comparison(request.concept),
        }
    if request.cell_count > MAX_CELLS:
        return {
            "error": "comparison_overflow",
            "limit": MAX_CELLS,
            "requested": request.cell_count,
            "comparison": _empty_comparison(request.concept),
        }
    return None


def _envelope(request: _GridRequest, grid: _Grid) -> dict[str, Any]:
    """The ``fin.comparison.v1`` envelope around a walked grid.

    Key order is part of the wire shape the export and the persisted
    turn row round-trip: keep it."""
    return {
        "comparison": {
            "schema": COMPARISON_SCHEMA,
            "kind": "table",
            "axis": _axis(len(grid.rows), len(request.columns)),
            "concept": request.concept,
            "unit": grid.unit,
            "scale": grid.scale,
            "columns": [{"key": key, "label": key} for key in request.columns],
            "rows": grid.rows,
            "deltas": _compute_deltas(grid.rows),
            "missing": grid.missing,
            "hit_count": grid.hit_count,
            "cell_count": request.cell_count,
            "locators": grid.locators,
        }
    }


def compare_facts(
    concept: str,
    periods: list[str],
    *,
    document_ids: list[str] | None = None,
    issuers: list[str] | None = None,
    facts_root: Path | None = None,
) -> dict[str, Any]:
    """Compare one XBRL ``concept`` across periods and/or issuers.

    Returns the ``fin.comparison.v1`` envelope documented at the top
    of this module. Never raises: an unresolvable request is a typed
    ``empty_comparison`` / ``comparison_overflow`` dict, and an
    unresolvable CELL is a ``missing`` cell with reason
    ``fact_not_found``.

    These five arguments are the ADR 0009 tool schema the model binds
    to, so they stay flat here; the refuse / walk / envelope phases
    behind them take a single :class:`_GridRequest`."""
    request = _GridRequest(
        concept=(concept or "").strip(),
        columns=_clean(periods),
        row_specs=_resolve_rows(
            document_ids=_clean(document_ids),
            issuers=_clean(issuers),
            facts_root=facts_root,
        ),
        facts_root=facts_root,
    )
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    return _envelope(request, _build_grid(request))


__all__ = [
    "COMPARISON_SCHEMA",
    "MAX_CELLS",
    "MISSING_DISPLAY",
    "compare_facts",
    "format_comparison_value",
]
