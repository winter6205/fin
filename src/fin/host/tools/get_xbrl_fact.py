"""Host tool wrapper for ADR 0031 / CONTEXT ``get_xbrl_fact``.

Pure function over the ingest-built fact table (``fin.corpus.facts``).
Returns the contract dict the research track consumes:

* hit  -> ``{"concept", "value", "period", "unit", "scale",
  "source_locator": {"document_id", "version", "page"}}``
  (no ``error`` key).
* miss -> ``{"error": "fact_not_found", "concept", "period",
  "document_id"}`` (no exception, no raise — the agent loop must
  never see a typed error out of this tool).

Per spec § Boundaries ("empty hits not exceptions") and T5
acceptance ("miss returns error dict without throwing out of the
loop"), the miss path is a structured dict, never an exception.

The wrapper does NOT contribute ``EvidenceItem`` rows to
``AgentState`` — that is reserved for ``read_file`` /
``retrieve``. The numeric gate (T5) consumes the ``ToolMessage``
JSON content directly when verifying the model's claimed values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin.corpus.facts import Fact, FactLookupError, FactResult, lookup_fact


def get_xbrl_fact(
    concept: str,
    period: str,
    *,
    document_id: str | None = None,
    issuer: str | None = None,
    facts_root: Path | None = None,
) -> dict[str, Any]:
    """ADR 0031 ``get_xbrl_fact`` host tool.

    Returns one of:

    * hit dict ``{"concept", "value", "period", "unit", "scale",
      "source_locator": {"document_id", "version", "page"}}``;
    * miss dict ``{"error": "fact_not_found", "concept", "period",
      "document_id"}``.

    Never raises. The miss dict is the typed contract — the model
    can see ``error`` and pick a recovery path.
    """
    result: FactResult = lookup_fact(
        concept=concept,
        period=period,
        document_id=document_id,
        issuer=issuer,
        facts_root=facts_root,
    )
    if isinstance(result, Fact):
        return {
            "concept": result.concept,
            "value": result.value,
            "period": result.period,
            "unit": result.unit,
            "scale": result.scale,
            "source_locator": {
                "document_id": result.source_locator.document_id,
                "version": result.source_locator.version,
                "page": result.source_locator.page,
            },
        }
    # ``result`` is ``FactLookupError``; the typed-miss shape.
    return {
        "error": result.error,
        "concept": result.concept,
        "period": result.period,
        "document_id": result.document_id,
    }


__all__ = ["get_xbrl_fact"]
