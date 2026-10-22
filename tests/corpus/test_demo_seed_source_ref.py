"""The demo seed's wire ``source_ref`` is the §B4 canonical serialization.

The retrieval service echoes back the ``source_ref`` it was pushed, and fin's
hit mapper only accepts the canonical serialization of the locator triple
(§B4). A producer that pushes a bare document id therefore makes every hit
unmappable: the demo answers with zero chunks and a ledger full of
``unmapped_source_ref``.

§B4 granularity is the frozen unit, so the seed pushes one envelope per unit
(one page) and every pushed source ref names the page it was pushed under.
These tests pin the producer's contract -- the canonical form, the per-unit
split, the round trip and the mapper that reads the value back -- against the
shipped demo corpus.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.demo_seed import (
    DEMO_FIXTURE_DIR,
    DemoDocument,
    DemoFixtureError,
    content_identity_of,
    load_demo_documents,
)
from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import (
    content_identity,
    deserialize_source_ref,
    serialize_source_ref,
)
from fin.retrieve.hit_mapping import MappingFailureLedger, map_hits_to_chunks


def _documents() -> tuple[DemoDocument, ...]:
    return load_demo_documents(DEMO_FIXTURE_DIR)


def _document(document_id: str) -> DemoDocument:
    for document in _documents():
        if document.document_id == document_id:
            return document
    raise AssertionError(f"the demo corpus holds no document {document_id!r}")


def test_every_demo_unit_pushes_the_canonical_locator_serialization() -> None:
    for document in _documents():
        for unit in document.units:
            assert unit.source_ref == serialize_source_ref(
                UnitLocator(
                    document_id=document.document_id,
                    version=document.source_sha256,
                    unit=unit.unit,
                )
            )
            assert deserialize_source_ref(unit.source_ref) == unit.locator


def test_the_pushed_envelope_carries_the_canonical_source_ref() -> None:
    for document in _documents():
        for unit in document.units:
            envelope = unit.push_envelope()

            assert envelope["source_ref"] == unit.source_ref
            assert (
                deserialize_source_ref(envelope["source_ref"]).document_id
                == document.document_id
            )


def test_a_multi_page_document_pushes_one_source_ref_per_page() -> None:
    """One envelope per document can only name one unit, so a hit on the
    second page would cite the first."""
    document = _document("zeus-annual-results-summary")

    assert len(document.units) > 1
    source_refs = [unit.source_ref for unit in document.units]
    assert len(set(source_refs)) == len(source_refs)
    for unit in document.units:
        locator = deserialize_source_ref(unit.source_ref)
        assert locator.document_id == document.document_id
        assert locator.version == document.source_sha256
        assert locator.unit == unit.unit


def test_the_pushed_envelope_carries_the_unit_body_and_its_locator() -> None:
    """The envelope of one unit: that page's body and that page's locator."""
    document = _document("zeus-annual-results-summary")
    unit = document.units[1]
    envelope = unit.push_envelope()

    assert envelope["content"] == str(document.chunks[1]["content"])
    assert envelope["chunks"] == [{"content": envelope["content"], "index": 0}]
    assert envelope["metadata"]["locator"] == {
        "document_id": document.document_id,
        "version": document.source_sha256,
        "unit": unit.unit,
    }
    assert envelope["body_content_id"] == content_identity_of(unit.content)


def test_the_pushed_body_content_id_is_the_service_formula_over_the_pushed_body() -> None:
    """The demo pins the service's identity, not fin's own derivation.

    ``fin.corpus.push`` derives a body identity from the envelope's declared
    rule versions; the demo declares the fixture's (``verbatim-v1`` /
    ``demo-fixture-v1``) while the service keys its row by its own defaults.
    The index job is addressed by the pair the service resolves, so the demo
    uses the service's formula -- the two disagree for this envelope, on
    purpose, and changing the value would change the port's idempotency key.
    """
    for document in _documents():
        for unit in document.units:
            envelope = unit.push_envelope()
            metadata = envelope["metadata"]

            assert envelope["body_content_id"] == content_identity_of(
                envelope["content"]
            )
            assert envelope["body_content_id"] != content_identity(
                envelope["content"],
                normalization_rule_version=metadata["normalization_rule_version"],
                parse_rule_version=metadata["parse_rule_version"],
                params_table_version=metadata["params_table_version"],
            )


def test_the_hit_mapper_keeps_the_hits_of_the_demo_documents() -> None:
    """The service echoes the pushed ``source_ref`` back on every hit, so the
    pushed value has to survive fin's own mapper."""
    for document in _documents():
        for unit in document.units:
            ledger = MappingFailureLedger()

            chunks = map_hits_to_chunks(
                [{"text": "kept", "source_ref": unit.source_ref, "score": 1.0}],
                locator_map={},
                limit=10,
                ledger=ledger,
            )

            assert len(chunks) == 1, unit.source_ref
            assert ledger.count() == 0, unit.source_ref
            assert ledger.count_by_code() == {}, unit.source_ref
            assert chunks[0].source_ref == Locator(
                document_id=document.document_id,
                version=document.source_sha256,
                unit=unit.unit,
            )


def test_the_aegis_installation_document_pushes_its_own_triple() -> None:
    """The version is the sha256 of the fixture file, so the expectation
    follows the document's own bytes instead of freezing a hash that rots."""
    path = DEMO_FIXTURE_DIR / "zeus-aegis-installation.json"
    version = hashlib.sha256(path.read_bytes()).hexdigest()
    document = _document("zeus-aegis-installation")
    unit = document.units[0]

    assert unit.source_ref == (
        '{"document_id":"zeus-aegis-installation","unit":1,'
        f'"version":"{version}"}}'
    )
    assert unit.push_envelope()["source_ref"] == unit.source_ref


def test_the_source_ref_is_stable_across_loads() -> None:
    """The version is the document's own content hash, so the same bytes
    always serialize to the same locator triple."""
    first = load_demo_documents(DEMO_FIXTURE_DIR)
    second = load_demo_documents(DEMO_FIXTURE_DIR)

    assert [
        unit.source_ref for document in first for unit in document.units
    ] == [unit.source_ref for document in second for unit in document.units]


def test_a_fixture_naming_another_document_is_refused(tmp_path: Path) -> None:
    """The fixture keeps declaring its own ``source_ref``; the wire form is
    what changed, not how fixtures are named."""
    directory = tmp_path / "demo-corpus"
    directory.mkdir()
    envelope: dict[str, Any] = {
        "source_ref": "zeus-handbook",
        "title": "ZEUS Handbook",
        "content": "ZEUS Handbook · Page 1",
        "chunks": [{"content": "ZEUS Handbook · Page 1", "index": 0}],
        "metadata": {"document_type": "handbook", "page_count": 1, "form": "10-K"},
        "valid_from": "2026-01-05T00:00:00.000Z",
    }
    (directory / "zeus-other.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )

    with pytest.raises(DemoFixtureError, match="the file name is the document id"):
        load_demo_documents(directory)
