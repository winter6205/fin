"""Idempotent push against the contract fake (plan T14, SC18 / SC20, §B4;
spec `operator-console-host-contract` T1).

Default-suite tests: the retrieval service is a **contract fake**
(`FakePushPort`) that uses the §B4 field names verbatim — no service
process, no network, no large corpus. Line B (the iknow-memory repo)
is not touched by this ticket; the real integration lands later.

Covered, one test per T14 acceptance clause:

* pushing the same envelope batch `IDEMPOTENCY_BASELINE_PUSHES` times
  (the §B1「幂等基准」row = 3) leaves the block total increment at 0
  and the block id set unchanged;
* replaying one envelope idempotency key returns the same block ids
  and writes no second copy;
* every chunk of one frozen unit carries the same `source_ref`, and
  `source_ref` never acts as block identity;
* the recalled chunk text is byte-identical to the pushed slice
  (§B4「不二次切分」);
* a push whose metadata misses a SC20 field is rejected with a typed
  error and writes nothing;
* the failed push does not count towards the idempotency assertion;
* the wire envelope carries `source_ref` + body + `chunks[]` with
  monotonic `index` starting at 0 (T1 acceptance);
* slice texts are contiguous substrings of the body (SC11 / SC16).
"""

from __future__ import annotations

import pytest

from fin.corpus.chunking import (
    DocumentChunking,
    lookup_chunk_params,
    split_document,
    split_unit,
)
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import UnitLocator, build_unit
from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    envelope_of_unit,
    envelopes_of_document,
    envelopes_of_unit,
)
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.iknow_identity import compute_iknow_content_identity
from fin.retrieve.push_port import (
    IDEMPOTENCY_BASELINE_PUSHES,
    PushEmptyBatchError,
    PushEmptyDocumentError,
    PushError,
    PushMetadataIncompleteError,
    PushProtocolError,
    UnknownPushedBlockError,
    push_envelopes,
    validate_envelope_chunks,
)

DOCUMENT_ID = "TEST-2023Q1"
VERSION = "a" * 64
VALID_FROM = "2026-01-01T00:00:00.000Z"
VALID_UNTIL = "9999-12-31T23:59:59.999Z"


def _sentences(n: int, start: int = 0) -> str:
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _unit_envelope(*, unit: int = 1, n: int = 40):
    chunking = split_unit(
        build_unit(
            fmt="txt",
            source_sha256=VERSION,
            document_id=DOCUMENT_ID,
            version=VERSION,
            unit=unit,
            text=_sentences(n),
        ),
        doc_type="narrative",
    )
    return envelopes_of_unit(
        chunking, valid_from=VALID_FROM, valid_until=VALID_UNTIL
    )


def _document_envelopes():
    result = parse_document(
        (_sentences(20) + "\n\n" + _sentences(20, start=20)).encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    return envelopes_of_document(
        split_document(result, doc_type="narrative"),
        valid_from=VALID_FROM,
        valid_until=VALID_UNTIL,
    )


# ---------------------------------------------------------------------------
# T1 acceptance: wire envelope carries source_ref + body + chunks (monotonic)
# ---------------------------------------------------------------------------


def test_envelope_carries_source_ref_body_and_monotonic_chunks() -> None:
    envelope = _unit_envelope()[0]

    assert envelope.source_ref
    assert envelope.body
    assert envelope.chunks
    indexes = [chunk.index for chunk in envelope.chunks]
    assert indexes == list(range(len(envelope.chunks)))


def test_chunk_contents_are_contiguous_substrings_of_body() -> None:
    envelope = _unit_envelope(n=10)[0]

    for chunk in envelope.chunks:
        assert chunk.content in envelope.body


def test_envelope_wire_shape_matches_the_spec() -> None:
    envelope = _unit_envelope()[0]
    wire = envelope.to_wire()

    assert "source_ref" in wire
    assert "body" in wire
    assert "chunks" in wire
    assert "metadata" in wire
    assert "valid_from" in wire
    assert "valid_until" in wire
    for required in REQUIRED_PUSH_METADATA_FIELDS:
        assert wire["metadata"][required]


# ---------------------------------------------------------------------------
# SC18 / §B1「幂等基准」: three consecutive pushes of the same batch
# ---------------------------------------------------------------------------


def test_baseline_push_count_is_the_b1_value() -> None:
    assert IDEMPOTENCY_BASELINE_PUSHES == 3


def test_repeated_pushes_keep_block_total_and_block_id_set() -> None:
    port = FakePushPort()
    envelopes = _document_envelopes()
    assert len(envelopes) > 1

    first = push_envelopes(port, envelopes)
    total_after_first = port.block_total()
    ids_after_first = port.block_ids()
    chunk_count = sum(len(envelope.chunks) for envelope in envelopes)
    assert total_after_first == chunk_count
    assert first.written == total_after_first

    for _ in range(IDEMPOTENCY_BASELINE_PUSHES - 1):
        outcome = push_envelopes(port, envelopes)
        assert outcome.written == 0
        assert outcome.duplicates == chunk_count
        assert set(outcome.block_ids) == set(first.block_ids)

    assert port.block_total() - total_after_first == 0
    assert port.block_ids() == ids_after_first


def test_replaying_one_idempotency_key_returns_the_same_block_ids() -> None:
    port = FakePushPort()
    envelopes = _unit_envelope()
    first = push_envelopes(port, envelopes)
    before = port.block_total()

    replay = push_envelopes(port, [envelopes[0]])

    assert replay.block_ids == first.block_ids
    assert replay.written == 0
    assert port.block_total() == before


# ---------------------------------------------------------------------------
# §B4: `source_ref` granularity and non-identity, on the wire
# ---------------------------------------------------------------------------


def test_pushed_chunks_of_one_unit_share_one_source_ref_on_the_wire() -> None:
    port = FakePushPort()
    envelope = _unit_envelope()[0]

    push_envelopes(port, [envelope])

    stored = [port.recall(block_id) for block_id in port.block_ids()]
    assert len({row["source_ref"] for row in stored}) == 1
    # Every chunk the envelope carried has its own block id, and
    # every block id answers to the same envelope row.
    chunk_ids_per_envelope = {
        chunk["id"]
        for row in stored
        for chunk in row["chunks"]
    }
    assert chunk_ids_per_envelope == set(port.block_ids())
    assert len(chunk_ids_per_envelope) == len(envelope.chunks)
    assert len(stored) == len(envelope.chunks)


def test_source_ref_never_acts_as_block_identity_on_the_wire() -> None:
    port = FakePushPort()
    envelope = _unit_envelope()[0]

    push_envelopes(port, [envelope])

    assert envelope.source_ref not in port.block_ids()
    with pytest.raises(UnknownPushedBlockError) as excinfo:
        port.recall(envelope.source_ref)
    assert excinfo.value.code == "unknown_pushed_block"


# ---------------------------------------------------------------------------
# §B4「不二次切分」: recalled chunk text is byte-identical to pushed text
# ---------------------------------------------------------------------------


def test_recalled_chunk_text_is_byte_identical_to_pushed_text() -> None:
    port = FakePushPort()
    envelope = _unit_envelope()[0]

    push_envelopes(port, [envelope])

    stored = port.recall(port.block_ids().__iter__().__next__())
    # The stored row is the envelope shape; the body is byte-identical
    # to the pushed unit text and every chunk content is the pushed
    # slice verbatim.
    assert stored["body"].encode("utf-8") == envelope.body.encode("utf-8")
    pushed_chunks = [chunk.content for chunk in envelope.chunks]
    stored_chunks = [chunk["content"] for chunk in stored["chunks"]]
    assert stored_chunks == pushed_chunks


# ---------------------------------------------------------------------------
# SC20: missing metadata is rejected with a named failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_PUSH_METADATA_FIELDS)
def test_push_missing_a_metadata_field_is_rejected_with_a_named_failure(
    field: str,
) -> None:
    port = FakePushPort()
    envelopes = list(_unit_envelope())
    crippled = envelopes[0]
    metadata = dict(crippled.metadata)
    metadata.pop(field)
    from dataclasses import replace
    from types import MappingProxyType
    crippled = replace(crippled, metadata=MappingProxyType(metadata))

    with pytest.raises(PushMetadataIncompleteError) as excinfo:
        push_envelopes(port, [crippled, *envelopes[1:]])

    assert excinfo.value.code == "push_metadata_incomplete"
    assert field in str(excinfo.value)
    assert isinstance(excinfo.value, PushError)
    # the whole batch is refused: nothing is written
    assert port.block_total() == 0


def test_the_contract_fake_itself_refuses_an_incomplete_wire_record() -> None:
    """The fake enforces the wire contract independently of the adapter's
    client-side validation, so a future transport cannot smuggle an
    incomplete record past it."""
    port = FakePushPort()
    wire = dict(_unit_envelope()[0].to_wire())
    del wire["metadata"]["section"]

    with pytest.raises(PushMetadataIncompleteError):
        port.push([wire])

    assert port.block_total() == 0


def test_failed_push_does_not_count_towards_the_idempotency_assertion() -> None:
    port = FakePushPort()
    envelopes = list(_unit_envelope())

    from dataclasses import replace
    from types import MappingProxyType
    crippled_metadata = dict(envelopes[0].metadata)
    crippled_metadata.pop("fmt")
    crippled = replace(
        envelopes[0], metadata=MappingProxyType(crippled_metadata)
    )

    with pytest.raises(PushMetadataIncompleteError):
        push_envelopes(port, [crippled])
    assert port.block_total() == 0
    assert port.rejected_batches() == 1

    first = push_envelopes(port, envelopes)
    baseline_total = port.block_total()
    for _ in range(IDEMPOTENCY_BASELINE_PUSHES - 1):
        push_envelopes(port, envelopes)

    assert port.block_total() - baseline_total == 0
    assert port.block_ids() == set(first.block_ids)
    # the refused batch left a countable trace, not a silent success
    assert port.rejected_batches() == 1
    # ``written_total`` is per-envelope (the wire-level idempotency
    # key is envelope-level); the failed push did not write a
    # single envelope.
    assert port.written_total() == len(envelopes)


# ---------------------------------------------------------------------------
# T1 acceptance: chunk monotonic + substring invariants, on the wire
# ---------------------------------------------------------------------------


def test_chunk_indexes_on_the_wire_are_monotonic_from_zero() -> None:
    port = FakePushPort()
    envelopes = _unit_envelope(n=80)
    push_envelopes(port, envelopes)

    stored = [port.recall(block_id) for block_id in port.block_ids()]
    for row in stored:
        indexes = [chunk["index"] for chunk in row["chunks"]]
        assert indexes == sorted(indexes)
        assert indexes[0] == 0


def test_chunk_texts_on_the_wire_are_substrings_of_the_body() -> None:
    port = FakePushPort()
    envelopes = _unit_envelope(n=80)
    push_envelopes(port, envelopes)

    stored = [port.recall(block_id) for block_id in port.block_ids()]
    for row in stored:
        body = row["body"]
        for chunk in row["chunks"]:
            assert chunk["content"] in body


# ---------------------------------------------------------------------------
# T1 boundary: empty unit / empty document on the envelope path
# ---------------------------------------------------------------------------


def _empty_unit_chunking():
    """A :class:`UnitChunking` whose frozen text is empty (no parents,
    no children — the shape an empty frozen unit would carry)."""
    from fin.corpus.chunking import UnitChunking

    return UnitChunking(
        locator=UnitLocator(DOCUMENT_ID, VERSION, 1),
        unit_text="",
        fmt="txt",
        doc_type="narrative",
        section="body",
        density=0,
        params=lookup_chunk_params("narrative", "body", 0),
        parents=(),
        children=(),
        source_sha256=VERSION,
        parse_rule_version="parse-1",
        params_table_version="params-1",
    )


def test_envelope_of_empty_unit_is_well_defined_with_no_chunks() -> None:
    """An empty unit produces a well-defined envelope — empty body, zero
    chunks — rather than raising at construction time; the refusal is
    the push side's named ``PushEmptyDocumentError`` (invalid input),
    not a construction-time crash."""
    envelope = envelope_of_unit(
        _empty_unit_chunking(),
        valid_from=VALID_FROM,
        valid_until=VALID_UNTIL,
    )

    assert envelope.body == ""
    assert envelope.chunks == ()
    assert envelope.source_ref == _unit_envelope(unit=1)[0].source_ref


def test_pushing_the_empty_unit_envelope_is_a_named_empty_document() -> None:
    """Pushing the empty-unit envelope is invalid input per §B2 第 5 条
    (「空文档 / 空内容」): refused by name with nothing written — never a
    zero-chunk silent success and never a half-write."""
    port = FakePushPort()
    envelope = envelope_of_unit(
        _empty_unit_chunking(),
        valid_from=VALID_FROM,
        valid_until=VALID_UNTIL,
    )

    with pytest.raises(PushEmptyDocumentError) as excinfo:
        push_envelopes(port, [envelope])

    assert excinfo.value.code == "push_empty_document"
    assert excinfo.value.degraded is False
    assert port.block_total() == 0


def test_envelopes_of_document_with_no_units_is_empty_not_an_error() -> None:
    """A document with zero frozen units yields a zero-length envelope
    tuple — a well-defined empty input, not an exception. The empty
    batch itself is refused later, at the push entry point."""
    document = DocumentChunking(
        document_id=DOCUMENT_ID,
        version=VERSION,
        doc_type="narrative",
        units=(),
    )

    assert (
        envelopes_of_document(
            document, valid_from=VALID_FROM, valid_until=VALID_UNTIL
        )
        == ()
    )


# ---------------------------------------------------------------------------
# T1 acceptance: chunk index density + substring invariants (validator)
# ---------------------------------------------------------------------------


def _wire_with_chunks(chunks: list[dict]) -> dict:
    """A minimal on-contract wire envelope with ``chunks`` replaced."""
    return {
        "source_ref": '{"document_id":"D","unit":1,"version":"v"}',
        "body": "alpha beta",
        "chunks": chunks,
        "metadata": {field: "x" for field in REQUIRED_PUSH_METADATA_FIELDS},
        "valid_from": VALID_FROM,
        "valid_until": VALID_UNTIL,
    }


def test_chunk_index_not_dense_from_zero_is_a_named_protocol_error() -> None:
    """A ``chunks`` index sequence of ``0, 2`` (gap at 1) violates the
    monotonic-from-0 invariant and is refused by name — the wire
    declares ``0..N-1`` in order, not merely "increasing"."""
    wire = _wire_with_chunks(
        [{"index": 0, "content": "alpha"}, {"index": 2, "content": "beta"}]
    )

    with pytest.raises(PushProtocolError) as excinfo:
        validate_envelope_chunks(wire)

    assert excinfo.value.code == "push_protocol_error"
    assert "not monotonic" in str(excinfo.value)


def test_chunk_content_not_contiguous_substring_of_body_is_refused() -> None:
    """A chunk whose content is not a contiguous substring of the body
    (SC11 / SC16「连续子串」) is refused by name."""
    wire = _wire_with_chunks([{"index": 0, "content": "gamma delta"}])

    with pytest.raises(PushProtocolError) as excinfo:
        validate_envelope_chunks(wire)

    assert excinfo.value.code == "push_protocol_error"
    assert "contiguous substring" in str(excinfo.value)


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param([], id="empty-chunks"),
        pytest.param(
            [{"index": 0, "content": "alpha"}, "stray string"],
            id="non-mapping-chunk",
        ),
        pytest.param([{"index": "0", "content": "alpha"}], id="str-index"),
        pytest.param([{"index": True, "content": "alpha"}], id="bool-index"),
        pytest.param([{"index": 0, "content": ""}], id="empty-content"),
        pytest.param([{"index": 0, "content": None}], id="null-content"),
    ],
)
def test_off_contract_chunk_shapes_are_refused_by_name(chunks: list) -> None:
    """The chunk validator refuses every off-contract ``chunks`` shape
    with the same named protocol error — a malformed chunk is never
    coerced into a plausible one."""
    with pytest.raises(PushProtocolError) as excinfo:
        validate_envelope_chunks(_wire_with_chunks(chunks))

    assert excinfo.value.code == "push_protocol_error"


# ---------------------------------------------------------------------------
# T4 handoff: the fake answers with the identity field the real service does
# ---------------------------------------------------------------------------
#
# ``compute_iknow_content_identity`` is the one mirror of iknow's formula
# that both the fake and these expectations read, so what a test re-derives
# here is that mirror rather than an independent observation of the
# service: it pins that the fake answers with the mirrored value and not
# with fin's own ``body_content_id``. Agreement with what iknow actually
# stores is the live cross-repo driver's claim
# (``tests/retrieve/test_live_content_identity.py``, opt-in ``-m mcp``).


def test_fake_answers_a_single_document_push_with_content_identity() -> None:
    """One 资料 in a batch answers with a top-level ``content_identity``
    (iknow ``src/tools/push.ts`` singles shape), not an array."""
    port = FakePushPort()
    envelope = _unit_envelope(n=6)[0]

    response = port.push([envelope.to_wire()])

    assert response["content_identity"] == compute_iknow_content_identity(
        envelope.body
    )
    assert "content_identities" not in response


def test_fake_answers_a_multi_document_batch_with_ordered_identities() -> None:
    port = FakePushPort()
    envelopes = _document_envelopes()
    assert len(envelopes) > 1

    response = port.push([envelope.to_wire() for envelope in envelopes])

    assert response["content_identities"] == [
        compute_iknow_content_identity(envelope.body) for envelope in envelopes
    ]
    assert "content_identity" not in response


def test_fake_identity_reaches_the_caller_through_the_push_seam() -> None:
    """The pair ``submit_index_job`` needs is readable off the outcome,
    keyed by the very ``source_ref`` fin sent."""
    port = FakePushPort()
    envelopes = _document_envelopes()

    outcome = push_envelopes(port, envelopes)

    assert dict(outcome.content_identities) == {
        envelope.source_ref: compute_iknow_content_identity(envelope.body)
        for envelope in envelopes
    }


def test_fake_replay_echoes_the_stored_identity() -> None:
    """An idempotent re-push reports the identity of the row that is
    actually stored, so the handoff never depends on which of the two
    calls a caller happened to read."""
    port = FakePushPort()
    envelopes = _unit_envelope(n=6)

    first = push_envelopes(port, envelopes)
    replay = push_envelopes(port, envelopes)

    assert replay.content_identities == first.content_identities


def test_fake_identity_is_the_remote_value_not_fins_body_hash() -> None:
    """fin's ``body_content_id`` and iknow's ``content_identity`` are two
    different constructions over two different owners; the fake must not
    collapse them, or T4 would submit a pair the real service rejects."""
    port = FakePushPort()
    envelope = _unit_envelope(n=6)[0]

    response = port.push([envelope.to_wire()])

    assert response["content_identity"] != envelope.body_content_id
