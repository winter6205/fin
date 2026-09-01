"""Batched, resumable push (plan T15, SC23 / SC44, §B4 后台路径失败契约).

This module is the driver on top of the T14 seam
(:mod:`fin.retrieve.push_port`): it splits a document's push records
into batches the receiving side accepts, records per-batch progress so
an interrupted run can be resumed, and turns every batch failure into
an enumerable, replayable named record.

Splitting (SC44)
----------------

:func:`plan_batches` cuts the record sequence into consecutive batches
of at most ``batch_size`` records (default
:data:`fin.retrieve.push_port.MAX_PUSH_BATCH_RECORDS`). The
concatenation of the batches is the input sequence — a document whose
block count exceeds the cap is split, never truncated. Asking for a
size above the cap or a non-positive size is a named failure
(``push_batch_overflow`` / ``push_batch_size_invalid``) rather than a
silently clamped run.

Resume (SC23)
-------------

Each batch has a **batch key**: the sha256 of the canonical JSON of its
records' T14 idempotency keys, in order. It is derived from the existing
``(source_ref, content identity)`` pairs — no second hashing scheme and
no second `source_ref` spelling. Because the key covers the batch's
composition, a resumed run only skips a batch whose contents are
byte-for-byte the batch that already succeeded.

:class:`PushJournal` holds those keys. A batch whose key is already
completed is not pushed at all, so a resumed run writes nothing for it
(block total increment = 0); the remaining batches are pushed and fill
the document up to :func:`expected_block_total`. Should an
acknowledgement be lost *after* the store accepted a batch, the replay
is still safe: the T14 idempotency key makes it a duplicate, not a
second write.

Failure records (§B4 后台路径失败契约)
------------------------------------

A failing batch is journaled as a :class:`BatchFailure` (batch key,
index, stable error ``code``, message, the batch's `source_ref` values)
and the typed error is re-raised — never swallowed, never reported as an
empty success. Failures are enumerable via :meth:`PushJournal.failures`
and replayable by running :func:`push_in_batches` again with the same
journal; the record moves to :meth:`PushJournal.resolved_failures` once
its batch completes, so a retried failure is neither lost nor
double-counted. A batch that failed for a named reason keeps that
reason: the SC21 codes (``push_service_unavailable`` /
``push_protocol_error``) travel through the driver unflattened, and only
an untyped transport exception becomes ``push_batch_failed``.

The journal is an append-only JSONL log when constructed with a
``path``, which is what makes the failure list survive the process that
produced it; without a path it is in-memory only (the default suite's
shape).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Sequence

from fin.corpus.push import PushRecord, idempotency_keys_digest
from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects
from fin.retrieve.input_validation import InputValidationLedger
from fin.retrieve.push_port import (
    MAX_PUSH_BATCH_RECORDS,
    PushBatchFailedError,
    PushBatchOverflowError,
    PushBatchSizeInvalidError,
    PushError,
    PushPort,
    push_records,
    validate_batch_not_empty,
)

_EVENT_COMPLETED: Final[str] = "completed"
_EVENT_FAILED: Final[str] = "failed"


@dataclass(frozen=True)
class BatchPlan:
    """One planned push batch: its position, its key and its records."""

    index: int
    key: str
    records: tuple[PushRecord, ...]

    def source_refs(self) -> tuple[str, ...]:
        """The distinct `source_ref` values in the batch, in first-seen
        order — the frozen units this batch touches."""
        seen: dict[str, None] = {}
        for record in self.records:
            seen.setdefault(record.source_ref, None)
        return tuple(seen)


@dataclass(frozen=True)
class BatchFailure:
    """A named record of one failed batch (§B4).

    ``code`` is the stable :attr:`fin.retrieve.push_port.PushError.code`
    of the failure, so a caller branches on it instead of on message
    text. ``resolved`` flips once the batch later completes: the record
    stays enumerable as history rather than being deleted.
    """

    batch_key: str
    batch_index: int
    code: str
    message: str
    source_refs: tuple[str, ...]
    block_count: int
    resolved: bool = False


@dataclass(frozen=True)
class BatchedPushOutcome:
    """The result of one batched run.

    ``block_ids`` covers the batches this run pushed, in record order;
    skipped batches contribute none (their ids were returned by the run
    that completed them).
    """

    batches_planned: int
    batches_pushed: int
    batches_skipped: int
    written: int
    duplicates: int
    block_ids: tuple[str, ...]


class PushJournalUnwritableError(PushError):
    """The background push journal could not persist an event.

    The journal's in-memory state is updated only after this operation
    succeeds, so callers never observe a completed or failed batch that
    cannot be replayed from disk.
    """

    code = "push_journal_unwritable"


class PushJournalUnreadableError(PushError):
    """The background push journal could not be read for replay.

    A journal path that is a directory, inaccessible, or otherwise
    unreadable is a named failure rather than a fresh empty journal.
    """

    code = "push_journal_unreadable"


class PushJournalCorruptError(PushError):
    """The background push journal contains an unreplayable event.

    Invalid JSON and JSON objects with a damaged event shape are kept
    distinct from read failures so operators can repair the journal
    content instead of changing its permissions or location.
    """

    code = "push_journal_corrupt"


def expected_block_total(records: Sequence[PushRecord]) -> int:
    """The block total a complete push of ``records`` must reach.

    Distinct T14 idempotency keys, not record count: two records with
    the same key are one write by contract.
    """
    return len({record.idempotency_key for record in records})


def batch_key(records: Sequence[PushRecord]) -> str:
    """The deterministic key of one batch composition (see module doc)."""
    return idempotency_keys_digest(
        [record.idempotency_key for record in records]
    )


def plan_batches(
    records: Sequence[PushRecord],
    *,
    batch_size: int = MAX_PUSH_BATCH_RECORDS,
) -> tuple[BatchPlan, ...]:
    """Split ``records`` into consecutive batches of at most
    ``batch_size`` records (SC44: split, never truncate)."""
    if batch_size <= 0:
        raise PushBatchSizeInvalidError(
            f"batch size must be positive, got {batch_size}"
        )
    if batch_size > MAX_PUSH_BATCH_RECORDS:
        raise PushBatchOverflowError(
            f"batch size {batch_size} exceeds the per-call cap of "
            f"{MAX_PUSH_BATCH_RECORDS}"
        )
    plans: list[BatchPlan] = []
    for index, start in enumerate(range(0, len(records), batch_size)):
        chunk = tuple(records[start : start + batch_size])
        plans.append(
            BatchPlan(index=index, key=batch_key(chunk), records=chunk)
        )
    return tuple(plans)


class PushJournal:
    """Per-batch push progress plus the named failure records.

    With a ``path`` the journal is an append-only JSONL log, reloaded by
    replaying its events, so progress and failures outlive the process
    that produced them. Without one it lives in memory.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._completed: dict[str, None] = {}
        self._failures: dict[str, BatchFailure] = {}
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- state -----

    def is_completed(self, key: str) -> bool:
        return key in self._completed

    def completed_keys(self) -> tuple[str, ...]:
        """Completed batch keys, in completion order."""
        return tuple(self._completed)

    def failures(self) -> tuple[BatchFailure, ...]:
        """Unresolved failure records — the replay work list."""
        return tuple(
            failure
            for failure in self._failures.values()
            if not failure.resolved
        )

    def resolved_failures(self) -> tuple[BatchFailure, ...]:
        """Failure records whose batch later completed."""
        return tuple(
            failure for failure in self._failures.values() if failure.resolved
        )

    # ----- recording -----

    def mark_completed(self, plan: BatchPlan) -> None:
        """Record that ``plan`` landed; resolve its failure if it had one."""
        self._append(
            {
                "event": _EVENT_COMPLETED,
                "batch_key": plan.key,
                "batch_index": plan.index,
                "block_count": len(plan.records),
            }
        )
        self._completed.setdefault(plan.key, None)
        failure = self._failures.get(plan.key)
        if failure is not None and not failure.resolved:
            self._failures[plan.key] = replace(failure, resolved=True)

    def record_failure(
        self, plan: BatchPlan, *, code: str, message: str
    ) -> BatchFailure:
        """Record a named failure for ``plan`` and return it."""
        failure = BatchFailure(
            batch_key=plan.key,
            batch_index=plan.index,
            code=code,
            message=message,
            source_refs=plan.source_refs(),
            block_count=len(plan.records),
        )
        self._append(
            {
                "event": _EVENT_FAILED,
                "batch_key": failure.batch_key,
                "batch_index": failure.batch_index,
                "code": failure.code,
                "message": failure.message,
                "source_refs": list(failure.source_refs),
                "block_count": failure.block_count,
            }
        )
        self._failures[plan.key] = failure
        return failure

    # ----- persistence -----

    def _append(self, event: dict[str, object]) -> None:
        append_jsonl_object(
            self._path,
            event,
            unwritable=PushJournalUnwritableError,
            message=f"cannot append push journal at {self._path}",
        )

    def _replay(self, path: Path) -> None:
        replay_jsonl_objects(
            path,
            lambda event, number: self._apply_or_corrupt(event, path, number),
            on_read_oserror=lambda exc: PushJournalUnreadableError(
                f"cannot read push journal at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_unicode=lambda exc: PushJournalCorruptError(
                f"push journal at {path} is not valid UTF-8: {exc}"
            ),
            on_json=lambda exc, number: PushJournalCorruptError(
                f"push journal {path} line {number} is not valid JSON: "
                f"{exc}"
            ),
        )

    def _apply_or_corrupt(
        self, event: object, path: Path, number: int
    ) -> None:
        try:
            self._apply_replayed_event(event)
        except (KeyError, TypeError, ValueError) as exc:
            raise PushJournalCorruptError(
                f"push journal {path} line {number} is not a "
                f"replayable event: {type(exc).__name__}: {exc}"
            ) from exc

    def _apply_replayed_event(self, event: object) -> None:
        if not isinstance(event, dict):
            raise TypeError(
                f"event is {type(event).__name__}, expected an object"
            )
        event_name = event.get("event")
        if event_name == _EVENT_COMPLETED:
            self._validate_replayed_event(event, failed=False)
            self._apply_completed(event["batch_key"])  # type: ignore[arg-type]
        elif event_name == _EVENT_FAILED:
            self._validate_replayed_event(event, failed=True)
            self._apply_failed(event)
        else:
            raise ValueError(f"unsupported event {event_name!r}")

    @staticmethod
    def _validate_replayed_event(
        event: dict[str, object], *, failed: bool
    ) -> None:
        required = ["event", "batch_key", "batch_index", "block_count"]
        if failed:
            required.extend(("code", "message", "source_refs"))
        missing = [field for field in required if field not in event]
        if missing:
            raise KeyError(f"missing fields: {', '.join(missing)}")
        if not isinstance(event["batch_key"], str) or not event["batch_key"]:
            raise TypeError("batch_key must be a non-empty string")
        for field in ("batch_index", "block_count"):
            value = event[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TypeError(f"{field} must be a non-negative integer")
        if not failed:
            return
        if not isinstance(event["code"], str) or not event["code"]:
            raise TypeError("code must be a non-empty string")
        if not isinstance(event["message"], str):
            raise TypeError("message must be a string")
        source_refs = event["source_refs"]
        if not isinstance(source_refs, list) or not all(
            isinstance(ref, str) for ref in source_refs
        ):
            raise TypeError("source_refs must be a list of strings")

    def _apply_completed(self, key: str) -> None:
        self._completed.setdefault(key, None)
        failure = self._failures.get(key)
        if failure is not None and not failure.resolved:
            self._failures[key] = replace(failure, resolved=True)

    def _apply_failed(self, event: dict[str, object]) -> None:
        key = str(event["batch_key"])
        self._failures[key] = BatchFailure(
            batch_key=key,
            batch_index=int(event["batch_index"]),  # type: ignore[arg-type]
            code=str(event["code"]),
            message=str(event["message"]),
            source_refs=tuple(str(ref) for ref in event["source_refs"]),  # type: ignore[union-attr]
            block_count=int(event["block_count"]),  # type: ignore[arg-type]
            resolved=key in self._completed,
        )


def push_in_batches(
    port: PushPort,
    records: Sequence[PushRecord],
    *,
    journal: PushJournal,
    batch_size: int = MAX_PUSH_BATCH_RECORDS,
    input_validation_ledger: InputValidationLedger | None = None,
) -> BatchedPushOutcome:
    """Push ``records`` batch by batch, resuming from ``journal``.

    Batches already marked completed are skipped without a push call.
    The first failing batch is journaled with its stable error code and
    the typed error is re-raised; running again with the same journal
    replays exactly the batches that did not complete.

    A push of no records is invalid input (§B2 第 5 条) and is refused
    by name here rather than returning a zero-batch outcome, which would
    be exactly the empty success the contract forbids.
    :func:`plan_batches` stays a pure splitter and answers ``()`` for an
    empty sequence; the judgement lives on the push entry point.
    ``input_validation_ledger`` is where that refusal is counted (T21).
    """
    validate_batch_not_empty(records, ledger=input_validation_ledger)
    plans = plan_batches(records, batch_size=batch_size)
    pushed = skipped = written = duplicates = 0
    block_ids: list[str] = []
    for plan in plans:
        if journal.is_completed(plan.key):
            skipped += 1
            continue
        outcome = _push_one_batch(port, plan, journal=journal)
        journal.mark_completed(plan)
        pushed += 1
        written += outcome.written
        duplicates += outcome.duplicates
        block_ids.extend(outcome.block_ids)
    return BatchedPushOutcome(
        batches_planned=len(plans),
        batches_pushed=pushed,
        batches_skipped=skipped,
        written=written,
        duplicates=duplicates,
        block_ids=tuple(block_ids),
    )


def _push_one_batch(port: PushPort, plan: BatchPlan, *, journal: PushJournal):
    """Push one batch, journaling a named failure before re-raising."""
    try:
        return push_records(port, plan.records)
    except PushError as exc:
        journal.record_failure(plan, code=exc.code, message=str(exc))
        raise
    except Exception as exc:  # transport-level: wrap, never swallow
        wrapped = PushBatchFailedError(
            f"batch {plan.index} failed: {type(exc).__name__}: {exc}"
        )
        journal.record_failure(plan, code=wrapped.code, message=str(exc))
        raise wrapped from exc


__all__ = [
    "BatchFailure",
    "BatchPlan",
    "BatchedPushOutcome",
    "PushJournal",
    "PushJournalCorruptError",
    "PushJournalUnreadableError",
    "PushJournalUnwritableError",
    "batch_key",
    "expected_block_total",
    "plan_batches",
    "push_in_batches",
]
