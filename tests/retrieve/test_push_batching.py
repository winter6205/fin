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
from fin.corpus.push import PushRecord, unit_push_records
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_batching import (
    PushJournal,
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
            self.forwarded_keys.append(
                (str(record["source_ref"]), str(record["content_id"]))
            )
        return self.inner.push(records)


# ---------------------------------------------------------------------------
# SC44: over-limit block count is split, never truncated
# ---------------------------------------------------------------------------


def test_plan_batches_splits_without_losing_the_tail() -> None:
    records = _records()
    assert len(records) > 5

    batches = plan_batches(records, batch_size=3)

    assert sum(len(batch.records) for batch in batches) == len(records)
    flattened = [record for batch in batches for record in batch.records]
    assert flattened == list(records)
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
    inner = FakePushPort()
    port = FlakyPushPort(
        inner, fail_on_call=1, error=ConnectionResetError("socket closed")
    )
    journal = PushJournal()
    records = _records()

    with pytest.raises(PushBatchFailedError) as excinfo:
        push_in_batches(port, records, journal=journal, batch_size=3)

    assert excinfo.value.code == "push_batch_failed"
    assert isinstance(excinfo.value.__cause__, ConnectionResetError)
    assert [failure.code for failure in journal.failures()] == ["push_batch_failed"]
    assert "socket closed" in journal.failures()[0].message


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
