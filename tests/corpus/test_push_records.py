"""Push record construction: `source_ref`, content identity, idempotency
key and the SC20 metadata surface (plan T14, SC18 / SC20, §B4).

Default-suite tests: small scripted fixtures only — no retrieval
service process, no large corpus, no network. What is covered here:

* `source_ref` is the stable serialization of the fin locator triple
  `{document_id, version, unit}` and its granularity is the frozen
  unit — every block of one unit carries the same value (§B4);
* `source_ref` is NOT block identity — block ids are the fin-side
  deterministic sha256 from `fin.corpus.chunking`;
* the content identity is the deterministic hash of (block text +
  normalization rule version + parse rule version + params table
  version), so it moves when any of the three versions moves;
* the idempotency key is the (`source_ref`, content identity) pair;
* every record carries the SC20 metadata filter fields.
"""

from __future__ import annotations

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.determinism import canonical_json_bytes
from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    body_content_id,
    content_identity,
    document_push_records,
    serialize_source_ref,
    unit_push_records,
)
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import UnitLocator, build_unit

DOCUMENT_ID = "TEST-2023Q1"
VERSION = "a" * 64


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _unit(text: str, *, fmt: str = "txt", unit: int = 1):
    return build_unit(
        fmt=fmt,
        source_sha256=VERSION,
        document_id=DOCUMENT_ID,
        version=VERSION,
        unit=unit,
        text=text,
    )


def _records(text: str | None = None, *, unit: int = 1):
    chunking = split_unit(_unit(text or _sentences(40), unit=unit), doc_type="narrative")
    return unit_push_records(chunking)


# ---------------------------------------------------------------------------
# §B4: `source_ref` spelling, content and granularity
# ---------------------------------------------------------------------------


def test_source_ref_is_the_stable_serialization_of_the_locator_triple() -> None:
    locator = UnitLocator(document_id=DOCUMENT_ID, version=VERSION, unit=7)
    serialized = serialize_source_ref(locator)

    assert serialized == canonical_json_bytes(locator.to_dict()).decode("utf-8")
    # stable: the same locator always serializes to the same bytes
    assert serialized == serialize_source_ref(
        UnitLocator(document_id=DOCUMENT_ID, version=VERSION, unit=7)
    )
    # the triple is fully recoverable from the serialization
    assert DOCUMENT_ID in serialized and VERSION in serialized and '"unit":7' in serialized


def test_all_blocks_of_one_frozen_unit_share_one_source_ref() -> None:
    records = _records()

    assert len(records) > 1  # the fixture really is multi-block
    assert len({record.source_ref for record in records}) == 1


def test_source_ref_differs_across_frozen_units_of_one_document() -> None:
    result = parse_document(
        (_sentences(20) + "\n\n" + _sentences(20, start=20)).encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    document = split_document(result, doc_type="narrative")
    per_unit = [
        {record.source_ref for record in unit_push_records(unit)}
        for unit in document.units
    ]

    assert len(per_unit) >= 2
    assert len({next(iter(refs)) for refs in per_unit}) == len(per_unit)


def test_source_ref_is_not_block_identity() -> None:
    records = _records()

    block_ids = {record.block_id for record in records}
    # distinct blocks under one shared source_ref
    assert len(block_ids) == len(records)
    assert records[0].source_ref not in block_ids
    # block identity is the chunking-side deterministic sha256
    chunking = split_unit(_unit(_sentences(40)), doc_type="narrative")
    assert block_ids == {child.block_id for child in chunking.hittable_blocks()}


# ---------------------------------------------------------------------------
# §B4: content identity and the idempotency key
# ---------------------------------------------------------------------------


def test_content_identity_is_deterministic_for_the_same_text_and_versions() -> None:
    args = dict(
        normalization_rule_version=NORMALIZATION_RULE_VERSION,
        parse_rule_version="parse-1",
        params_table_version="params-1",
    )

    assert content_identity("revenue grew", **args) == content_identity(
        "revenue grew", **args
    )
    assert content_identity("revenue grew", **args) != content_identity(
        "revenue fell", **args
    )


def test_content_identity_moves_with_each_of_the_three_rule_versions() -> None:
    base = dict(
        normalization_rule_version="norm-1",
        parse_rule_version="parse-1",
        params_table_version="params-1",
    )
    baseline = content_identity("revenue grew", **base)

    for field in base:
        bumped = dict(base)
        bumped[field] = base[field] + "x"
        assert content_identity("revenue grew", **bumped) != baseline, field


def test_idempotency_key_is_the_source_ref_and_content_identity_pair() -> None:
    records = _records()
    record = records[0]

    assert record.idempotency_key == (record.source_ref, record.content_id)
    # same text in a different frozen unit: same content identity,
    # different key (the key is unit-scoped, the content id is not)
    other = _records(unit=2)[0]
    assert other.content_id == record.content_id
    assert other.idempotency_key != record.idempotency_key


# ---------------------------------------------------------------------------
# SC20: structured metadata rides along
# ---------------------------------------------------------------------------


def test_every_record_carries_the_sc20_metadata_filter_fields() -> None:
    records = _records()

    for record in records:
        for field in REQUIRED_PUSH_METADATA_FIELDS:
            assert record.metadata.get(field), (record.block_id, field)


def test_records_are_built_for_children_only_and_carry_parent_text() -> None:
    chunking = split_unit(_unit(_sentences(40)), doc_type="narrative")
    records = unit_push_records(chunking)

    assert {record.block_id for record in records} == {
        child.block_id for child in chunking.children
    }
    for record in records:
        assert record.parent_text
        assert record.text in record.parent_text


def test_document_push_records_cover_every_hittable_block() -> None:
    result = parse_document(
        (_sentences(20) + "\n\n" + _sentences(20, start=20)).encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    document = split_document(result, doc_type="narrative")

    records = document_push_records(document)

    assert {record.block_id for record in records} == {
        block.block_id for block in document.hittable_blocks()
    }


# ---------------------------------------------------------------------------
# T1: body_content_id — the wire-level idempotency key component (SSOT)
# ---------------------------------------------------------------------------


_META = {
    "normalization_rule_version": "norm-1",
    "parse_rule_version": "parse-1",
    "params_table_version": "params-1",
}


def test_body_content_id_is_derived_like_content_identity_from_the_body() -> None:
    """A hand-rolled wire envelope without ``body_content_id`` hashes
    through the SAME construction ``content_identity`` uses — the wire
    key and the corpus-side content identity can never drift."""
    wire = {"source_ref": "s", "body": "revenue grew", "metadata": _META}

    assert body_content_id(wire) == content_identity(
        "revenue grew",
        normalization_rule_version="norm-1",
        parse_rule_version="parse-1",
        params_table_version="params-1",
    )


def test_body_content_id_accepts_content_spelling_of_the_body() -> None:
    """Envelopes spelling the body ``content`` (the direct-push wire
    name) hash identically to ones spelling it ``body`` — the two
    spellings are one body for identity purposes."""
    assert body_content_id({"content": "revenue grew", "metadata": _META}) == (
        body_content_id({"body": "revenue grew", "metadata": _META})
    )


def test_body_content_id_moves_with_each_rule_version() -> None:
    """The derived id is version-scoped: bumping any of the three rule
    versions moves it, so a re-parse under new rules is a new write."""
    baseline = body_content_id({"body": "t", "metadata": _META})

    for field in _META:
        bumped = dict(_META)
        bumped[field] = _META[field] + "x"
        assert (
            body_content_id({"body": "t", "metadata": bumped}) != baseline
        ), field


def test_body_content_id_is_stable_for_the_same_body_and_versions() -> None:
    """Same body + same versions → same id, independent of dict
    insertion order or envelope construction path."""
    first = body_content_id({"body": "t", "metadata": dict(_META)})
    second = body_content_id(
        {
            "metadata": {"params_table_version": "params-1", "parse_rule_version": "parse-1", "normalization_rule_version": "norm-1"},
            "body": "t",
        }
    )
    assert first == second


def test_explicit_body_content_id_is_trusted_verbatim() -> None:
    """When the corpus side pre-computes ``body_content_id``, the SSOT
    trusts it — a hand-edited body with a pinned id keys on the pinned
    id, not a re-derived hash (record_to_envelope's contract)."""
    wire = {"body": "t", "metadata": _META, "body_content_id": "pinned-id"}

    assert body_content_id(wire) == "pinned-id"


def test_body_content_id_of_an_empty_body_is_stable_and_named() -> None:
    """An empty / absent body has a well-defined id (hash of ``""``) —
    it does not raise, so the empty-document refusal stays on the push
    path's named check, not on the hashing helper."""
    empty = body_content_id({"body": "", "metadata": _META})
    absent = body_content_id({"metadata": _META})

    assert empty == absent
    assert empty == content_identity(
        "",
        normalization_rule_version="norm-1",
        parse_rule_version="parse-1",
        params_table_version="params-1",
    )
