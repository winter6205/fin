"""Fact table for the research track's ``get_xbrl_fact`` lookup.

A fact table row records one tagged XBRL number tied to a
``Locator`` (ADR 0006) so the research track can match a claimed
numeric value to a verbatim source on a frozen locator unit. The
table is an ingest-built sidecar under
``data/corpus/facts/<document_id>.json``; ``get_xbrl_fact`` (T5)
consumes it as a lookup, NOT as a retriever (CONTEXT ``get_xbrl_fact``:
"XBRL is the reporting format, not a website ... a `verify` tool,
using retrieve/embeddings as the fact SSOT" is forbidden).

Schema
------

The on-disk shape is a JSON object with a ``facts`` list:

.. code-block:: json

    {
      "facts": [
        {
          "concept": "us-gaap:Revenues",
          "value": 383285000000,
          "period": "2023-Q3-YTD",
          "unit": "USD",
          "scale": "thousands",
          "source_locator": {
            "document_id": "AAPL-2023Q3",
            "version": "[private source excerpt removed]",
            "page": 4
          }
        }
      ]
    }

Atomicity
---------

``write_facts`` writes via a same-directory temp file + ``os.replace``,
mirroring the ingest idempotency rule (ADR 0020). A second call with
the same payload is a no-op (byte-identical on-disk file).

Empty / miss shape
------------------

``read_facts`` returns ``[]`` (NOT a typed error) for missing or
malformed files; the caller (``get_xbrl_fact``) converts that into the
typed "fact_miss" error dict per spec § Boundaries ("empty hits not
exceptions"). ``FactValueError`` is the typed EXIT for malformed tag
data at write time.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from fin.corpus.extracted import Locator


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_FACTS_ROOT = _DEFAULT_DATA_DIR / "facts"


_ALLOWED_VALUE_TYPES = (int, float, str)


@dataclass(frozen=True)
class Fact:
    """One tagged financial fact keyed to a frozen locator unit.

    ``value`` is intentionally loose (numeric OR string) because XBRL
    tagged values may be either; the research track's numeric gate
    (T5) applies the strict numeric check downstream.
    """

    concept: str
    value: float | int | str
    period: str
    unit: str
    scale: str
    source_locator: Locator


class FactValueError(ValueError):
    """Typed EXIT: ``write_facts`` was given a fact with an unsupported
    ``value`` type (neither number nor string).

    Per spec § Boundaries, the corpus slice raises typed errors so the
    caller can fail fast at the boundary (ADR 0020 family). This is a
    ``ValueError`` so the rest of the corpus's typed-EXIT shape stays
    consistent (IngestMissingError is also a ``RuntimeError``).
    """


def _validate_fact(fact: Fact) -> None:
    if not isinstance(fact.value, _ALLOWED_VALUE_TYPES) or isinstance(
        fact.value, bool
    ):
        raise FactValueError(
            f"Fact.value must be int|float|str (got {type(fact.value).__name__})"
        )


def _payload(facts: list[Fact]) -> bytes:
    return json.dumps(
        {"facts": [asdict(f) for f in facts]},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def write_facts(
    *,
    document_id: str,
    facts: list[Fact],
    facts_root: Path,
) -> None:
    """Write ``facts`` for ``document_id`` under ``facts_root``.

    Writes are atomic (temp + ``os.replace``) and idempotent: a second
    call with the same payload is a no-op.

    Raises
    ------
    FactValueError
        If any ``Fact.value`` is not a number or string.
    """
    for fact in facts:
        _validate_fact(fact)

    facts_root.mkdir(parents=True, exist_ok=True)
    payload = _payload(facts)
    target = facts_root / f"{document_id}.json"

    if target.exists() and target.read_bytes() == payload:
        return

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)


def read_facts(
    *,
    document_id: str,
    facts_root: Path,
) -> list[Fact]:
    """Read the fact list for ``document_id`` from ``facts_root``.

    Returns ``[]`` if the file is absent or malformed (spec §
    Boundaries: "empty hits not exceptions"); the caller (``T5
    get_xbrl_fact``) converts that to the typed fact-miss error
    dict.
    """
    path = facts_root / f"{document_id}.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[Fact] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        locator_payload = entry.get("source_locator")
        if not isinstance(locator_payload, dict):
            continue
        try:
            locator = Locator(
                document_id=locator_payload["document_id"],
                version=locator_payload["version"],
                page=int(locator_payload["page"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            Fact(
                concept=entry["concept"],
                value=entry["value"],
                period=entry["period"],
                unit=entry["unit"],
                scale=entry["scale"],
                source_locator=locator,
            )
        )
    return out


def list_fact_documents(*, facts_root: Path) -> list[str]:
    """Return the sorted ``document_id`` list with a fact file on disk.

    Mirrors the catalog shape ``list_filings`` exposes for the
    extracted tree: deterministic order, no raise on missing root.
    """
    if not facts_root.is_dir():
        return []
    return sorted(p.stem for p in facts_root.glob("*.json") if p.is_file())


__all__ = [
    "Fact",
    "FactValueError",
    "_DEFAULT_FACTS_ROOT",
    "list_fact_documents",
    "read_facts",
    "write_facts",
]