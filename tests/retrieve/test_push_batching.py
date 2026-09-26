"""Batched, resumable push (plan T15, SC23 / SC44, §B4 后台路径失败契约).

Default-suite tests: the retrieval service is the T14 contract fake
(`FakePushPort`) — no service process, no network, no large corpus.

Covered, one test per T15 acceptance clause:

* a document whose block count exceeds the single-push cap is **split**,
  not truncated: after the run the stored block total equals the
  expected total (no lost tail);
* a single push batch above the cap is refused with a typed overflow
  error instead of being silently truncated;
* a run interrupted mid-way and pushed again does not re-write the
  batches that already succeeded (block total increment = 0) and fills
  the unfinished ones up to the expected total;
* a failed background push leaves an enumerable, replayable named
  failure record; the replay is idempotent and nothing is dropped
  silently.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from types import MappingProxyType

from fin.corpus.push import PushRecord, unit_push_records
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_batching import (
    BatchedPushOutcome,
    PushJournal,
    PushJournalUnwritableError,
    expected_block_total,
    plan_batches,
    push_in_batches,
)
from fin.retrieve.push_port import (
    MAX_PUSH_BATCH_RECORDS,
    PushBatchFailedError,
    PushBatchOverflowError,
    PushBatchSizeInvalidError,
    PushError,
    PushProtocolError,
)

DOCUMENT_ID = "TEST-2024Q2"
VERSION = "b" * 64


def _sentences(n: int, start: int = 0) -> str:
    return "".join(
        f"Sentence {i:04d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _records(units: int = 4, sentences: int = 30) -> tuple[PushRecord, ...]:
    out: list[PushRecord] = []
    for unit in range(1, units + 1):
        chunking = split_unit(
            build_unit(
                fmt="txt",
                source_sha256=VERSION,
                document_id=DOCUMENT_ID,
                version=VERSION,
                unit=unit,
                text=_sentences(sentences, start=unit * 1000),
            ),
            doc_type="narrative",
        )
        out.extend(unit_push_records(chunking))
    return tuple(out)


class FlakyPushPort:
    """A `PushPort` that fails one push call — the scripted interruption.

    Wraps the T14 contract fake so the batches that succeed land in a
    real store, and records the idempotency keys of every batch it
    actually forwarded so a test can assert that a resumed run does not
    re-push an already-completed batch.
    """

    def __init__(
        self,
        inner: FakePushPort,
        *,
        fail_on_call: int,
        error: BaseException | None = None,
    ) -> None:
        self.inner = inner
        self.fail_on_call = fail_on_call
        self.error = error or PushError("simulated interruption")
        self.calls = 0
        self.forwarded_keys: list[tuple[str, str]] = []

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise self.error
        for record in records:
            # After T1 the wire envelope's body identity is
            # ``body_content_id`` (the per-unit sha256 over the body +
            # rule versions), not the per-child ``content_id``. Older
            # per-child wire records may still carry ``content_id``;
            # accept either so the flaky port is a faithful interceptor
            # for both shapes.
            content_id = record.get("body_content_id") or record.get(
                "content_id"
            )
            self.forwarded_keys.append(
                (str(record["source_ref"]), str(content_id))
            )
        return self.inner.push(records)


# ---------------------------------------------------------------------------
# SC44: over-limit block count is split, never truncated
# ---------------------------------------------------------------------------


def test_plan_batches_splits_without_losing_the_tail() -> None:
    records = _records()
    assert len(records) > 5

    batches = plan_batches(records, batch_size=3)

    # After T1 the plan holds one envelope per PushRecord (each
    # record coerces into a single-chunk envelope); the count of
    # pushed items is preserved across the split.
    assert sum(len(batch.records) for batch in batches) == len(records)
    flattened = [record for batch in batches for record in batch.records]
    assert len(flattened) == len(records)
    assert [batch.index for batch in batches] == list(range(len(batches)))
    assert len({batch.key for batch in batches}) == len(batches)
    # the last batch is the remainder, not a dropped tail
    assert len(batches[-1].records) == len(records) - 3 * (len(batches) - 1)


def test_batch_key_is_derived_from_the_t14_idempotency_keys() -> None:
    records = _records()

    first = plan_batches(records, batch_size=3)
    again = plan_batches(records, batch_size=3)

    assert [batch.key for batch in first] == [batch.key for batch in again]
    # a different batch composition is a different key
    other = plan_batches(records, batch_size=4)
    assert first[0].key != other[0].key


def test_over_limit_document_is_pushed_in_batches_up_to_the_expected_total() -> None:
    port = FakePushPort()
    journal = PushJournal()
    records = _records()
    expected = expected_block_total(records)
    assert expected == len({record.idempotency_key for record in records})

    outcome = push_in_batches(port, records, journal=journal, batch_size=3)

    assert outcome.batches_planned == len(plan_batches(records, batch_size=3))
    assert outcome.batches_pushed == outcome.batches_planned
    assert outcome.batches_skipped == 0
    assert port.block_total() == expected
    assert len(outcome.block_ids) == len(records)


# ---------------------------------------------------------------------------
# SC44: a single over-cap push is a named overflow, not a silent truncation
# ---------------------------------------------------------------------------


def test_single_push_above_the_cap_is_a_named_overflow() -> None:
    port = FakePushPort(max_batch_records=4)
    records = _records()
    assert len(records) > 4

    with pytest.raises(PushBatchOverflowError) as excinfo:
        port.push([record.to_wire() for record in records])

    assert excinfo.value.code == "push_batch_overflow"
    assert isinstance(excinfo.value, PushError)
    # refused as a whole: no truncated prefix landed
    assert port.block_total() == 0
    assert port.rejected_batches() == 1


def test_default_cap_is_the_module_constant() -> None:
    assert MAX_PUSH_BATCH_RECORDS == 200
    assert FakePushPort().max_batch_records == MAX_PUSH_BATCH_RECORDS


def test_batch_size_above_the_cap_is_refused_before_any_write() -> None:
    port = FakePushPort()
    records = _records()

    with pytest.raises(PushBatchOverflowError) as excinfo:
        plan_batches(records, batch_size=MAX_PUSH_BATCH_RECORDS + 1)

    assert excinfo.value.code == "push_batch_overflow"
    assert port.block_total() == 0


@pytest.mark.parametrize("batch_size", [0, -1])
def test_non_positive_batch_size_is_a_named_failure(batch_size: int) -> None:
    with pytest.raises(PushBatchSizeInvalidError) as excinfo:
        plan_batches(_records(), batch_size=batch_size)

    assert excinfo.value.code == "push_batch_size_invalid"


def test_empty_record_sequence_plans_no_batch() -> None:
    assert plan_batches((), batch_size=3) == ()


# ---------------------------------------------------------------------------
# SC23: interruption then resume
# ---------------------------------------------------------------------------


def test_interrupted_run_reports_a_named_failure_and_keeps_earlier_batches() -> None:
    inner = FakePushPort()
    port = FlakyPushPort(inner, fail_on_call=2)
    journal = PushJournal()
    records = _records()

    with pytest.raises(PushError) as excinfo:
        push_in_batches(port, records, journal=journal, batch_size=3)

    assert excinfo.value.code == "push_error"
    # the first batch landed; the rest did not
    assert inner.block_total() == 3
    failures = journal.failures()
    assert len(failures) == 1
    assert failures[0].code == "push_error"
    assert failures[0].batch_index == 1
    assert failures[0].block_count == 3
    assert failures[0].source_refs


def test_resume_does_not_rewrite_completed_batches_and_fills_the_rest() -> None:
    inner = FakePushPort()
    journal = PushJournal()
    records = _records()
    expected = expected_block_total(records)

    first = FlakyPushPort(inner, fail_on_call=2)
    with pytest.raises(PushError):
        push_in_batches(first, records, journal=journal, batch_size=3)
    completed_keys = set(first.forwarded_keys)
    total_after_interrupt = inner.block_total()
    writes_after_interrupt = inner.written_total()

    resumed = FlakyPushPort(inner, fail_on_call=0)
    outcome = push_in_batches(resumed, records, journal=journal, batch_size=3)

    # the already-successful batch was not pushed again at all
    assert completed_keys.isdisjoint(set(resumed.forwarded_keys))
    assert outcome.batches_skipped == 1
    assert outcome.batches_pushed == outcome.batches_planned - 1
    # unfinished batches were filled up to the expected total
    assert inner.block_total() == expected
    assert inner.written_total() - writes_after_interrupt == (
        expected - total_after_interrupt
    )
    assert journal.failures() == ()
    assert len(journal.resolved_failures()) == 1


def test_resuming_a_completed_run_writes_nothing_more() -> None:
    port = FakePushPort()
    journal = PushJournal()
    records = _records()

    push_in_batches(port, records, journal=journal, batch_size=3)
    before = port.block_total()

    for _ in range(2):
        outcome = push_in_batches(port, records, journal=journal, batch_size=3)
        assert outcome.batches_pushed == 0
        assert outcome.batches_skipped == outcome.batches_planned
        assert outcome.written == 0

    assert port.block_total() - before == 0


def test_replaying_a_failed_batch_against_the_store_is_idempotent() -> None:
    """Even when the interruption happened *after* the store accepted the
    batch (the acknowledgement was lost), the replay is a duplicate, not
    a second write — the T14 idempotency key carries that."""
    port = FakePushPort()
    journal = PushJournal()
    records = _records()
    batches = plan_batches(records, batch_size=3)

    # store the first batch behind the journal's back, then run
    port.push([record.to_wire() for record in batches[0].records])
    total_before = port.block_total()

    outcome = push_in_batches(port, records, journal=journal, batch_size=3)

    assert outcome.duplicates == len(batches[0].records)
    assert port.block_total() == expected_block_total(records)
    assert port.block_total() > total_before


# ---------------------------------------------------------------------------
# §B4 后台路径失败契约: enumerable, replayable, never silently dropped
# ---------------------------------------------------------------------------


def test_a_non_push_error_is_wrapped_in_a_named_failure_and_journaled() -> None:
    """The catch-all wrapper covers exceptions with no named shape of
    their own; transport-level `OSError`s have one since T16
    (``push_service_unavailable``, asserted in test_push_failures.py)."""
    inner = FakePushPort()
    port = FlakyPushPort(
        inner, fail_on_call=1, error=ValueError("garbled payload")
    )
    journal = PushJournal()
    records = _records()

    with pytest.raises(PushBatchFailedError) as excinfo:
        push_in_batches(port, records, journal=journal, batch_size=3)

    assert excinfo.value.code == "push_batch_failed"
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert [failure.code for failure in journal.failures()] == ["push_batch_failed"]
    assert "garbled payload" in journal.failures()[0].message


def test_unwritable_journal_is_named_and_does_not_update_memory(
    tmp_path,
) -> None:
    """A journal persistence failure is itself a named background
    failure, and the in-memory work list changes only after persistence."""
    journal_parent = tmp_path / "journal-parent"
    journal_parent.write_text("not a directory", encoding="utf-8")
    journal_path = journal_parent / "journal.jsonl"
    journal = PushJournal(path=journal_path)
    plan = plan_batches(_records(1), batch_size=3)[0]

    with pytest.raises(PushJournalUnwritableError) as excinfo:
        journal.record_failure(
            plan,
            code="push_batch_failed",
            message="background payload failed",
        )

    assert excinfo.value.code == "push_journal_unwritable"
    assert journal.failures() == ()


def test_journal_directory_is_a_named_replay_read_failure(tmp_path) -> None:
    journal_path = tmp_path / "journal-directory"
    journal_path.mkdir()

    with pytest.raises(PushError) as excinfo:
        PushJournal(path=journal_path)

    assert excinfo.value.code == "push_journal_unreadable"


def test_unreadable_journal_file_is_a_named_replay_read_failure(
    tmp_path, monkeypatch
) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("", encoding="utf-8")
    original_read_text = type(journal_path).read_text

    def deny_read(path, *args, **kwargs):
        if path == journal_path:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(journal_path), "read_text", deny_read)

    with pytest.raises(PushError) as excinfo:
        PushJournal(path=journal_path)

    assert excinfo.value.code == "push_journal_unreadable"


def test_bad_json_journal_line_is_a_named_corruption(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("{not json\n", encoding="utf-8")

    with pytest.raises(PushError) as excinfo:
        PushJournal(path=journal_path)

    assert excinfo.value.code == "push_journal_corrupt"


def test_journal_event_missing_fields_is_a_named_corruption(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text(
        json.dumps({"event": "completed"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PushError) as excinfo:
        PushJournal(path=journal_path)

    assert excinfo.value.code == "push_journal_corrupt"


def test_journal_records_are_enumerable_and_replayable_from_disk(tmp_path) -> None:
    path = tmp_path / "push-journal.jsonl"
    inner = FakePushPort()
    records = _records()

    interrupted = FlakyPushPort(inner, fail_on_call=2)
    with pytest.raises(PushError):
        push_in_batches(
            interrupted,
            records,
            journal=PushJournal(path=path),
            batch_size=3,
        )

    # the journal is a durable, human-enumerable log
    lines = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [line["event"] for line in lines] == ["completed", "failed"]
    assert lines[1]["code"] == "push_error"

    # a fresh journal reloaded from the same path resumes where it stopped
    reloaded = PushJournal(path=path)
    assert len(reloaded.completed_keys()) == 1
    assert len(reloaded.failures()) == 1

    outcome = push_in_batches(inner, records, journal=reloaded, batch_size=3)

    assert outcome.batches_skipped == 1
    assert inner.block_total() == expected_block_total(records)
    assert reloaded.failures() == ()


def test_failed_batch_is_not_counted_as_a_write(tmp_path) -> None:
    inner = FakePushPort()
    port = FlakyPushPort(inner, fail_on_call=1)
    journal = PushJournal(path=tmp_path / "journal.jsonl")
    records = _records()

    with pytest.raises(PushError):
        push_in_batches(port, records, journal=journal, batch_size=3)

    assert inner.block_total() == 0
    assert inner.written_total() == 0
    assert journal.completed_keys() == ()
    assert len(journal.failures()) == 1


# ---------------------------------------------------------------------------
# T1 wire-shape parity: a coerced envelope's chunks must carry the record's
# per-child id / content_id, so the batching wire matches the direct-push
# wire (envelope_of_unit). The catalog-side invalidation path keys on
# ``chunks[0]["id"]``; without this propagation the T3 live test's blocking
# recorder raises ``KeyError: 'id'``.
# ---------------------------------------------------------------------------


def test_coerced_envelope_chunks_carry_record_id_and_content_id() -> None:
    """A :class:`PushRecord` coerced into a :class:`PushEnvelope` must
    forward the per-child ``block_id`` and ``content_id`` onto the
    synthesized chunk.

    Without this propagation the batching wire drops the
    per-child identity that the direct-push wire (``envelope_of_unit``
    via ``live.push.push``) carries; the catalog-side invalidation
    path resolves its per-child keys off ``chunks[0]["id"]`` and a
    missing field raises ``KeyError: 'id'`` (live T3 regression).
    """
    records = _records(units=1, sentences=12)
    assert records, "fixture must yield at least one record"

    batches = plan_batches(records)

    # One envelope per record; each carries one chunk.
    assert len(batches) == 1
    envelopes = batches[0].envelopes
    assert len(envelopes) == len(records)

    for envelope, record in zip(envelopes, records):
        wire = envelope.to_wire()
        assert len(wire["chunks"]) == 1
        chunk = wire["chunks"][0]
        assert chunk["id"] == record.block_id
        assert chunk["content_id"] == record.content_id
        # The envelope-level idempotency key is
        # ``(source_ref, body_content_id)`` — on the coerced path
        # ``body_content_id`` is the record's ``content_id`` (the
        # record is the per-child leaf, so the unit-level and
        # child-level content identities collapse).
        assert envelope.body_content_id == record.content_id
        assert envelope.source_ref == record.source_ref


# ---------------------------------------------------------------------------
# Back-compat coercion boundary: degenerate PushRecord inputs
# ---------------------------------------------------------------------------


def _degenerate_record(**overrides: object) -> PushRecord:
    base: dict[str, Any] = {
        "block_id": "b0",
        "source_ref": "docs/x.md",
        "content_id": "c0",
        "text": "hello",
        "parent_block_id": "p0",
        "parent_text": "parent",
        "metadata": MappingProxyType({}),
    }
    base.update(overrides)
    return PushRecord(**base)


def test_coerced_record_without_block_id_omits_the_chunk_id_field() -> None:
    """A hand-rolled record whose ``block_id`` is ``None`` (or missing)
    coerces into an envelope whose chunk simply omits the ``id`` field —
    the projection is well-defined rather than raising or writing a
    ``null`` id the receiving schema would refuse."""
    envelopes = plan_batches([_degenerate_record(block_id=None)])

    wire = envelopes[0].envelopes[0].to_wire()
    assert wire["chunks"] == [{"index": 0, "content": "hello", "content_id": "c0"}]
    # the body identity is the record's own pinned content_id
    assert envelopes[0].envelopes[0].body_content_id == "c0"


def test_coerced_record_with_empty_text_is_forwarded_verbatim() -> None:
    """An empty-text record coerces to an empty-body envelope; the
    empty-document judgement is the push entry point's named refusal
    (``PushEmptyDocumentError``), never a silent zero-chunk write."""
    from fin.retrieve.fake_push_port import FakePushPort
    from fin.retrieve.push_batching import push_in_batches, PushJournal
    from fin.retrieve.push_port import PushEmptyDocumentError

    port = FakePushPort()
    journal = PushJournal()
    record = _degenerate_record(
        text="",
        metadata=MappingProxyType(
            {
                "fmt": "txt",
                "doc_type": "narrative",
                "section": "body",
                "source_sha256": "a" * 64,
                "parse_rule_version": "p1",
                "params_table_version": "t1",
            }
        ),
    )

    with pytest.raises(PushEmptyDocumentError):
        push_in_batches(port, [record], journal=journal)

    assert port.block_total() == 0


def test_coerce_rejects_items_that_are_neither_envelope_nor_record() -> None:
    """``plan_batches`` names the item type it cannot coerce instead of
    letting a ``AttributeError`` escape from the wrong layer."""
    from fin.corpus.push import PushEnvelope

    with pytest.raises(TypeError) as excinfo:
        plan_batches(["not an envelope"])

    assert "str" in str(excinfo.value)
    # the two supported shapes still pass through untouched
    record = _degenerate_record()
    assert plan_batches([record])[0].envelopes[0].body_content_id == "c0"
    assert plan_batches(
        [PushEnvelope(
            source_ref="s", body="b",
            chunks=(), metadata=MappingProxyType({}),
            valid_from="0001-01-01T00:00:00.000Z",
            valid_until="9999-12-31T23:59:59.999Z",
            body_content_id="c0",
        )]
    )[0].envelopes[0].body_content_id == "c0"


# ---------------------------------------------------------------------------
# T4 handoff: the batched run merges the identities each batch reported
# ---------------------------------------------------------------------------


class _IdentityAnsweringPort:
    """A `PushPort` that answers every batch with one identity per
    envelope, in send order — the legacy ``items[]`` response shape.

    ``identities`` maps a batch position to the hex value the "service"
    claims for it, which is how a test scripts two batches contradicting
    each other about one `source_ref`.
    """

    def __init__(self, identities: Mapping[str, str]) -> None:
        self.identities = dict(identities)
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        refs = [str(record["source_ref"]) for record in records]
        return {
            "block_ids": [f"block-{self.calls}-{index}" for index in refs],
            "written": len(refs),
            "duplicates": 0,
            "content_identities": [self.identities[ref] for ref in refs],
        }


class _ContradictingIdentityPort:
    """Answers the same `source_ref` with a different identity per call."""

    def __init__(self) -> None:
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        refs = [str(record["source_ref"]) for record in records]
        return {
            "block_ids": [f"block-{index}" for index in refs],
            "written": len(refs),
            "duplicates": 0,
            "content_identities": [
                format(self.calls, "064x") for _ in refs
            ],
        }


def test_batched_run_merges_the_identity_of_every_batch() -> None:
    """One `source_ref` → one identity, across every batch the run pushed;
    ``submit_index_job`` is fed the pair the service actually stored."""
    records = _records(units=4, sentences=2)
    port = _IdentityAnsweringPort(
        {record.source_ref: format(index, "064x") for index, record in enumerate(records, start=1)}
    )

    outcome = push_in_batches(
        port, records, journal=PushJournal(), batch_size=1
    )

    assert outcome.batches_pushed == len(records)
    assert dict(outcome.content_identities) == port.identities


def test_batched_run_reports_no_identity_when_no_batch_does() -> None:
    """A service that answers without the identity field batches cleanly
    and merges to an empty map — never a guessed one."""

    class _LegacyPort:
        def push(self, records):
            refs = [str(record["source_ref"]) for record in records]
            return {
                "block_ids": list(refs),
                "written": len(refs),
                "duplicates": 0,
            }

    records = _records(units=2, sentences=2)

    outcome = push_in_batches(
        _LegacyPort(), records, journal=PushJournal(), batch_size=1
    )

    assert outcome.batches_pushed == len(records)
    assert outcome.content_identities == {}


def test_a_batched_run_without_identities_defaults_to_empty() -> None:
    assert BatchedPushOutcome(
        batches_planned=0,
        batches_pushed=0,
        batches_skipped=0,
        written=0,
        duplicates=0,
        block_ids=(),
    ).content_identities == {}


def test_batches_disagreeing_on_one_source_ref_are_a_named_failure() -> None:
    """Two batches claiming different identities for the same `source_ref`
    is an off-contract answer: merging would publish whichever happened
    to land last, and the durable index job would be keyed on a value the
    service does not hold."""
    port = _ContradictingIdentityPort()
    journal = PushJournal()
    records = [
        _degenerate_record(block_id="b1", source_ref="docs/x.md", content_id="c1", text="one"),
        _degenerate_record(block_id="b2", source_ref="docs/x.md", content_id="c2", text="two"),
    ]
    plans = plan_batches(records, batch_size=1)

    with pytest.raises(PushProtocolError) as excinfo:
        push_in_batches(port, records, journal=journal, batch_size=1)

    assert excinfo.value.code == "push_protocol_error"
    assert port.calls == 2
    # The contradiction is a batch failure like any other, so the journal
    # stays the replay work list: without this record an operator reading
    # the journal would see a run that simply stopped after batch 0.
    failures = journal.failures()
    assert [(f.batch_index, f.code) for f in failures] == [(1, "push_protocol_error")]
    assert failures[0].source_refs == ("docs/x.md",)
    # The completing run is not progress the next one may skip past.
    assert not journal.is_completed(plans[1].key)
    assert journal.completed_keys() == (plans[0].key,)


def test_a_journal_skipped_batch_contributes_no_identity_to_the_replay(
) -> None:
    """A skipped batch is not pushed, so the service reports nothing for
    it and the replay's map is empty.

    An empty map here is the honest answer rather than a partial one a
    caller could misread as "the service holds no identity": the caller
    that needs the pairs reuses the map the run that completed the
    batches returned (spec ``corpus-serve-ingest`` Decision 2).
    """
    port = FakePushPort()
    journal = PushJournal()
    records = _records()

    first = push_in_batches(port, records, journal=journal, batch_size=3)
    replay = push_in_batches(port, records, journal=journal, batch_size=3)

    assert first.content_identities
    assert replay.batches_pushed == 0
    assert replay.batches_skipped == replay.batches_planned
    assert replay.content_identities == {}


def test_a_run_result_identity_map_refuses_to_be_written_through() -> None:
    """The merged map is the run's record of what the service stored.

    Written through, it would edit that record after the fact and — the
    map being built once and handed to several results — reach a map
    another caller still reads.
    """
    port = FakePushPort()
    records = _records()

    outcome = push_in_batches(port, records, journal=PushJournal(), batch_size=3)

    with pytest.raises(TypeError):
        outcome.content_identities["invented"] = "0" * 64  # type: ignore[index]
    assert "invented" not in outcome.content_identities


def test_push_in_batches_hands_the_merged_map_on_by_alias() -> None:
    """The run's map reaches the next result as the same object.

    Freezing wraps instead of copying, so the hand-off chain (batched
    run → ingest result → ``submit_index_job``) cannot fork two records
    of what the service reported; a consumer holding the earlier result
    reads exactly what the later one carries.
    """
    port = FakePushPort()
    records = _records()

    outcome = push_in_batches(port, records, journal=PushJournal(), batch_size=3)
    handed_on = BatchedPushOutcome(
        batches_planned=outcome.batches_planned,
        batches_pushed=outcome.batches_pushed,
        batches_skipped=outcome.batches_skipped,
        written=outcome.written,
        duplicates=outcome.duplicates,
        block_ids=outcome.block_ids,
        content_identities=outcome.content_identities,
    )

    assert isinstance(outcome.content_identities, MappingProxyType)
    assert handed_on.content_identities is outcome.content_identities
