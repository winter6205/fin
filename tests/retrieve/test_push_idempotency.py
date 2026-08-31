"""Idempotent push against the contract fake (plan T14, SC18 / SC20, §B4).

Default-suite tests: the retrieval service is a **contract fake**
(`FakePushPort`) that uses the §B4 field names verbatim — no service
process, no network, no large corpus. Line B (the iknow-memory repo)
is not touched by this ticket; the real integration lands later.

Covered, one test per T14 acceptance clause:

* pushing the same batch `IDEMPOTENCY_BASELINE_PUSHES` times (the §B1
  「幂等基准」 row = 3) leaves the block total increment at 0 and the
  block id set unchanged;
* replaying one idempotency key returns the same block ids and writes
  no second copy;
* every block of one frozen unit carries the same `source_ref`, and
  `source_ref` never acts as block identity;
* the recalled block text is byte-identical to the pushed text (§B4
  「不二次切分」);
* a push whose metadata misses a SC20 field is rejected with a typed
  error and writes nothing;
* the failed push does not count towards the idempotency assertion.
"""

from __future__ import annotations

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    document_push_records,
    unit_push_records,
)
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import build_unit
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import (
    IDEMPOTENCY_BASELINE_PUSHES,
    PushError,
    PushMetadataIncompleteError,
    UnknownPushedBlockError,
    push_records,
)

DOCUMENT_ID = "TEST-2023Q1"
VERSION = "a" * 64


def _sentences(n: int, start: int = 0) -> str:
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _unit_records(*, unit: int = 1, n: int = 40):
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
    return unit_push_records(chunking)


def _document_records():
    result = parse_document(
        (_sentences(20) + "\n\n" + _sentences(20, start=20)).encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    from fin.corpus.chunking import split_document

    return document_push_records(split_document(result, doc_type="narrative"))


# ---------------------------------------------------------------------------
# SC18 / §B1「幂等基准」: three consecutive pushes of the same batch
# ---------------------------------------------------------------------------


def test_baseline_push_count_is_the_b1_value() -> None:
    assert IDEMPOTENCY_BASELINE_PUSHES == 3


def test_repeated_pushes_keep_block_total_and_block_id_set() -> None:
    port = FakePushPort()
    records = _document_records()
    assert len(records) > 1

    first = push_records(port, records)
    total_after_first = port.block_total()
    ids_after_first = port.block_ids()
    assert total_after_first == len({record.idempotency_key for record in records})
    assert first.written == total_after_first

    for _ in range(IDEMPOTENCY_BASELINE_PUSHES - 1):
        outcome = push_records(port, records)
        assert outcome.written == 0
        assert outcome.duplicates == len(records)
        assert set(outcome.block_ids) == set(first.block_ids)

    assert port.block_total() - total_after_first == 0
    assert port.block_ids() == ids_after_first


def test_replaying_one_idempotency_key_returns_the_same_block_ids() -> None:
    port = FakePushPort()
    records = _unit_records()
    push_records(port, records)
    before = port.block_total()

    replay = push_records(port, [records[0]])

    assert replay.block_ids == (records[0].block_id,)
    assert replay.written == 0
    assert port.block_total() == before


# ---------------------------------------------------------------------------
# §B4: `source_ref` granularity and non-identity, on the wire
# ---------------------------------------------------------------------------


def test_pushed_blocks_of_one_unit_share_one_source_ref_on_the_wire() -> None:
    port = FakePushPort()
    records = _unit_records()

    push_records(port, records)

    stored = [port.recall(record.block_id) for record in records]
    assert len({row["source_ref"] for row in stored}) == 1
    assert len({row["block_id"] for row in stored}) == len(records)


def test_source_ref_never_acts_as_block_identity_on_the_wire() -> None:
    port = FakePushPort()
    records = _unit_records()
    push_records(port, records)

    source_ref = records[0].source_ref

    assert source_ref not in port.block_ids()
    with pytest.raises(UnknownPushedBlockError) as excinfo:
        port.recall(source_ref)
    assert excinfo.value.code == "unknown_pushed_block"


# ---------------------------------------------------------------------------
# §B4「不二次切分」: recalled text is byte-identical to pushed text
# ---------------------------------------------------------------------------


def test_recalled_block_text_is_byte_identical_to_pushed_text() -> None:
    port = FakePushPort()
    records = _unit_records()

    push_records(port, records)

    for record in records:
        row = port.recall(record.block_id)
        assert row["text"].encode("utf-8") == record.text.encode("utf-8")
        assert row["parent_text"].encode("utf-8") == record.parent_text.encode("utf-8")


# ---------------------------------------------------------------------------
# SC20: missing metadata is rejected with a named failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_PUSH_METADATA_FIELDS)
def test_push_missing_a_metadata_field_is_rejected_with_a_named_failure(
    field: str,
) -> None:
    port = FakePushPort()
    records = _unit_records()
    crippled = records[0].without_metadata_field(field)

    with pytest.raises(PushMetadataIncompleteError) as excinfo:
        push_records(port, [crippled, *records[1:]])

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
    wire = _unit_records()[0].to_wire()
    del wire["metadata"]["section"]

    with pytest.raises(PushMetadataIncompleteError):
        port.push([wire])

    assert port.block_total() == 0


def test_failed_push_does_not_count_towards_the_idempotency_assertion() -> None:
    port = FakePushPort()
    records = _unit_records()

    with pytest.raises(PushMetadataIncompleteError):
        push_records(port, [records[0].without_metadata_field("fmt")])
    assert port.block_total() == 0
    assert port.rejected_batches() == 1

    first = push_records(port, records)
    baseline_total = port.block_total()
    for _ in range(IDEMPOTENCY_BASELINE_PUSHES - 1):
        push_records(port, records)

    assert port.block_total() - baseline_total == 0
    assert port.block_ids() == set(first.block_ids)
    # the refused batch left a countable trace, not a silent success
    assert port.rejected_batches() == 1
    assert port.written_total() == baseline_total
