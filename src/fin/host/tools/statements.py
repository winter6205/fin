"""Three-year financial statement host tool backed by XBRL facts.

This module only arranges facts already written by the corpus ingest
sidecar. It performs no year-over-year arithmetic and does not ask the
model to author a table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin.corpus.facts import Fact, list_fact_documents, read_facts


STATEMENT_SCHEMA = "fin.statement.v1"
MAX_STATEMENT_CELLS = 64
_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "corpus"
_DEFAULT_FACTS = _DATA_DIR / "facts"


def _clean(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _empty_statement(
    issuer: str, periods: list[str], concepts: list[str]
) -> dict[str, Any]:
    return {
        "schema": STATEMENT_SCHEMA,
        "kind": "three_year",
        "issuer": issuer,
        "periods": periods,
        "columns": [{"key": period, "label": period} for period in periods],
        "rows": [],
        "missing": [],
        "hit_count": 0,
        "cell_count": 0,
        "locators": [],
    }


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,}"


def _locator(fact: Fact) -> dict[str, Any]:
    return {
        "document_id": fact.source_locator.document_id,
        "version": fact.source_locator.version,
        "unit": fact.source_locator.unit,
    }


def _statement_row(
    *,
    concept: str,
    periods: list[str],
    facts: dict[tuple[str, str], Fact],
    missing: list[dict[str, str]],
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for period in periods:
        fact = facts.get((concept, period))
        if fact is None:
            missing.append(
                {"concept": concept, "period": period, "reason": "fact_not_found"}
            )
            cells.append(
                {
                    "concept": concept,
                    "period": period,
                    "status": "missing",
                    "value": None,
                    "display": "—",
                    "unit": "",
                    "scale": "",
                    "locator": None,
                }
            )
            continue
        source = _locator(fact)
        if source not in locators:
            locators.append(source)
        cells.append(
            {
                "concept": concept,
                "period": period,
                "status": "hit",
                "value": fact.value,
                "display": _display(fact.value),
                "unit": fact.unit,
                "scale": fact.scale,
                "locator": source,
            }
        )
    return {"key": concept, "label": concept, "concept": concept, "cells": cells}


def three_year_statement(
    issuer: str,
    periods: list[str],
    concepts: list[str] | None = None,
    *,
    facts_root: Path | None = None,
) -> dict[str, Any]:
    """Render exactly three requested periods from an issuer's facts.

    Missing documents or cells stay visible as typed ``missing`` cells.
    Invalid request sizes and oversized tables are typed exits with an
    empty, renderable statement skeleton.
    """
    issuer_value = str(issuer or "").strip().upper()
    period_values = _clean(periods)
    concept_values = _clean(concepts)
    skeleton = _empty_statement(issuer_value, period_values, concept_values)
    if not issuer_value or not period_values or not concept_values:
        return {"error": "empty_statement", "statement": skeleton}
    if len(period_values) != 3:
        return {
            "error": "statement_requires_three_years",
            "requested": len(period_values),
            "statement": skeleton,
        }
    if len(period_values) * len(concept_values) > MAX_STATEMENT_CELLS:
        return {
            "error": "statement_overflow",
            "limit": MAX_STATEMENT_CELLS,
            "requested": len(period_values) * len(concept_values),
            "statement": skeleton,
        }

    root = facts_root or _DEFAULT_FACTS
    candidates = [
        document_id
        for document_id in list_fact_documents(facts_root=root)
        if document_id.upper().startswith(f"{issuer_value}-")
    ]
    indexed: dict[tuple[str, str], Fact] = {}
    for document_id in candidates:
        for fact in read_facts(document_id=document_id, facts_root=root):
            indexed.setdefault((fact.concept, fact.period), fact)

    missing: list[dict[str, str]] = []
    locators: list[dict[str, Any]] = []
    rows = [
        _statement_row(
            concept=concept,
            periods=period_values,
            facts=indexed,
            missing=missing,
            locators=locators,
        )
        for concept in concept_values
    ]
    return {
        "statement": {
            "schema": STATEMENT_SCHEMA,
            "kind": "three_year",
            "issuer": issuer_value,
            "periods": period_values,
            "columns": [
                {"key": period, "label": period} for period in period_values
            ],
            "rows": rows,
            "missing": missing,
            "hit_count": sum(
                1
                for row in rows
                for cell in row["cells"]
                if cell["status"] == "hit"
            ),
            "cell_count": len(period_values) * len(concept_values),
            "locators": locators,
        }
    }


__all__ = ["MAX_STATEMENT_CELLS", "STATEMENT_SCHEMA", "three_year_statement"]
