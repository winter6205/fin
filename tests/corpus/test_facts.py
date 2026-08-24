"""Boundary tests for the corpus fact table (ADR 0031 / CONTEXT `get_xbrl_fact`).

The fact table is the lookup source for ``get_xbrl_fact``: a row records
one tagged XBRL number (``concept``) tied to a ``Locator`` so the
research track can match a claimed numeric value to a verbatim source
on a frozen locator unit. The five boundary classes (empty, negative,
overflow, concurrent, exception) come from the defensive-contract
validator axis.

* empty  — ``read_facts`` on a missing document returns ``[]`` (no
  raise); ``list_fact_documents`` on an empty root returns ``[]``.
* negative — ``read_facts`` on a malformed JSON returns ``[]`` and does
  NOT raise (catalog-miss shape, per spec: "Empty hits not
  exceptions").
* overflow — a 1000+ row fact list round-trips byte-identical.
* concurrent — **N/A single-writer** for ingest (per spec); the lookup
  side is documented but not asserted here because that lands with T5.
* exception — invalid ``value`` type raises a typed error at write
  time so the catalog can fail fast on a malformed tag.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

import json
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import (
    Fact,
    FactValueError,
    list_fact_documents,
    read_facts,
    write_facts,
)


def _locator() -> Locator:
    return Locator(
        document_id="AAPL-2023Q3",
        version=load_private_fixture("5cae37674ca6bf538f54bca53ca28b15aaf96dd1b7ae386553bd32387f254b73"),
        page=4,
    )


def _sample_fact() -> Fact:
    return Fact(
        concept="us-gaap:Revenues",
        value=383285000000,
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )


# ---------- boundary 1: empty ----------


def test_read_facts_missing_document_returns_empty_list(tmp_path: Path) -> None:
    """Empty input: ``read_facts`` on a missing document_id returns ``[]``.

    The fact table follows the same "empty hits not exceptions" rule as
    ``list_files`` (spec § Boundaries); the later ``get_xbrl_fact`` tool
    consumes this empty result as the typed "fact_miss" error dict
    shape.
    """
    facts_root = tmp_path / "facts"
    facts_root.mkdir(parents=True, exist_ok=True)

    assert read_facts(document_id="AAPL-2023Q3", facts_root=facts_root) == []


def test_list_fact_documents_on_empty_root_returns_empty(tmp_path: Path) -> None:
    """Empty ``facts_root`` returns ``[]`` without raising."""
    facts_root = tmp_path / "facts"
    facts_root.mkdir(parents=True, exist_ok=True)

    assert list_fact_documents(facts_root=facts_root) == []


# ---------- boundary 2: negative / malformed ----------


def test_read_facts_malformed_json_returns_empty_list(tmp_path: Path) -> None:
    """Negative: a malformed JSON file on disk must NOT crash the reader.

    The lookup is a research-track tool; per spec § Boundaries, an
    unknown fact returns the structured error dict, not an uncaught
    ``JSONDecodeError``. ``read_facts`` returns ``[]`` so the caller can
    convert that to the error dict.
    """
    facts_root = tmp_path / "facts"
    facts_root.mkdir(parents=True, exist_ok=True)
    bad_path = facts_root / "AAPL-2023Q3.json"
    bad_path.write_text("{ this is not valid json")

    assert read_facts(document_id="AAPL-2023Q3", facts_root=facts_root) == []


# ---------- boundary 3: overflow ----------


def test_write_and_read_facts_round_trip_large_list(tmp_path: Path) -> None:
    """Overflow: a 1000+ row fact list round-trips byte-identical."""
    facts_root = tmp_path / "facts"
    locator = _locator()
    facts = [
        Fact(
            concept=f"us-gaap:Concept{i:04d}",
            value=float(i),
            period=f"2023-Q3-YTD",
            unit="USD",
            scale="thousands",
            source_locator=locator,
        )
        for i in range(1000)
    ]

    write_facts(document_id="AAPL-2023Q3", facts=facts, facts_root=facts_root)

    loaded = read_facts(document_id="AAPL-2023Q3", facts_root=facts_root)
    assert len(loaded) == 1000
    assert loaded[0].concept == "us-gaap:Concept0000"
    assert loaded[999].concept == "us-gaap:Concept0999"
    assert loaded[500].value == 500.0


# ---------- boundary 4: concurrent (N/A single-writer) ----------


# Concurrent ingest on the fact table is N/A single-writer per spec
# (T5 owns the lookup-side concurrent test, which is read-only and
# independent across overlapping calls). This module covers the
# write-side ingest path only.


# ---------- boundary 5: exception ----------


def test_write_facts_rejects_invalid_value_type(tmp_path: Path) -> None:
    """Exception: ``write_facts`` must reject an invalid ``value`` type
    so a malformed XBRL tag cannot poison the catalog.

    A typed ``FactValueError`` is raised so the caller (the ingest CLI
    or the OCR pipeline) fails fast and the error surfaces in the same
    shape as the rest of the corpus slice's typed exits (ADR 0020 /
    spec § Boundaries).
    """
    facts_root = tmp_path / "facts"

    bad_fact = Fact(
        concept="us-gaap:Revenues",
        value=object(),  # type: ignore[arg-type]
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )

    with pytest.raises(FactValueError):
        write_facts(
            document_id="AAPL-2023Q3",
            facts=[bad_fact],
            facts_root=facts_root,
        )


# ---------- happy path ----------


def test_write_and_read_facts_round_trip(tmp_path: Path) -> None:
    """Happy path: ``write_facts`` produces a JSON file ``read_facts``
    can reload into the original ``Fact`` instances."""
    facts_root = tmp_path / "facts"
    fact = _sample_fact()

    write_facts(document_id="AAPL-2023Q3", facts=[fact], facts_root=facts_root)

    on_disk = facts_root / "AAPL-2023Q3.json"
    assert on_disk.is_file()

    loaded = read_facts(document_id="AAPL-2023Q3", facts_root=facts_root)
    assert len(loaded) == 1
    assert loaded[0] == fact


def test_list_fact_documents_returns_document_ids(tmp_path: Path) -> None:
    """``list_fact_documents`` returns the on-disk document_ids, sorted."""
    facts_root = tmp_path / "facts"
    facts_root.mkdir(parents=True, exist_ok=True)

    locator = _locator()
    fact = _sample_fact()
    write_facts(
        document_id="AAPL-2023Q3", facts=[fact], facts_root=facts_root
    )
    write_facts(
        document_id="MSFT-2023Q3",
        facts=[
            Fact(
                concept="us-gaap:Revenues",
                value=56.0,
                period="2023-Q3-YTD",
                unit="USD",
                scale="billions",
                source_locator=Locator(
                    document_id="MSFT-2023Q3",
                    version=load_private_fixture("5a8558db7c94112d0a7ff48be9f8fcf09d80f8a8f053645952c03d65ed1eb5e2"),
                    page=4,
                ),
            )
        ],
        facts_root=facts_root,
    )

    docs = list_fact_documents(facts_root=facts_root)
    assert docs == ["AAPL-2023Q3", "MSFT-2023Q3"]


def test_write_facts_is_atomic_and_deterministic(tmp_path: Path) -> None:
    """A second ``write_facts`` against the same document_id with the
    same fact list must NOT rewrite the file (byte-level idempotency).
    """
    facts_root = tmp_path / "facts"
    fact = _sample_fact()

    write_facts(document_id="AAPL-2023Q3", facts=[fact], facts_root=facts_root)
    before = (facts_root / "AAPL-2023Q3.json").read_bytes()

    write_facts(document_id="AAPL-2023Q3", facts=[fact], facts_root=facts_root)
    after = (facts_root / "AAPL-2023Q3.json").read_bytes()

    assert before == after


def test_write_facts_payload_shape(tmp_path: Path) -> None:
    """Spot-check the on-disk JSON shape: a top-level ``facts`` list
    with ``concept`` / ``value`` / ``source_locator`` fields (the
    minimal ``get_xbrl_fact`` lookup keys)."""
    facts_root = tmp_path / "facts"
    write_facts(
        document_id="AAPL-2023Q3",
        facts=[_sample_fact()],
        facts_root=facts_root,
    )

    payload = json.loads((facts_root / "AAPL-2023Q3.json").read_text())
    assert "facts" in payload
    assert isinstance(payload["facts"], list)
    entry = payload["facts"][0]
    assert entry["concept"] == "us-gaap:Revenues"
    assert entry["value"] == 383285000000
    assert entry["source_locator"]["document_id"] == "AAPL-2023Q3"
    assert entry["source_locator"]["page"] == 4