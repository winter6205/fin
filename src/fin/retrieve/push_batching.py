"""Batched, resumable push (plan T15, SC23 / SC44, §B4 后台路径失败契约;
spec `operator-console-host-contract` T1).

This module is the driver on top of the T14 seam
(:mod:`fin.retrieve.push_port`): it splits a document's push
**envelopes** into batches the receiving side accepts, records per-batch
progress so an interrupted run can be resumed, and turns every batch
failure into an enumerable, replayable named record.

After T1 the unit of work is the **envelope** — one per frozen unit
(spec `operator-console-host-contract`): ``source_ref`` + 资料 body +
``chunks[]``. Per-child :class:`fin.corpus.push.PushRecord` rows still
exist on the corpus side (the invalidation path keys on them), but
they no longer ride the wire.

Splitting (SC44)
----------------

:func:`plan_batches` cuts the envelope sequence into consecutive
batches of at most ``batch_size`` envelopes (default
:data:`fin.retrieve.push_port.MAX_PUSH_BATCH_RECORDS`). The
concatenation of the batches is the input sequence — a document whose
envelope count exceeds the cap is split, never truncated. Asking for
a size above the cap or a non-positive size is a named failure
(``push_batch_overflow`` / ``push_batch_size_invalid``) rather than a
silently clamped run.

Resume (SC23)
-------------

Each batch has a **batch key**: the sha256 of the canonical JSON of
its envelopes' ``(source_ref, body_content_id)`` pairs, in order. It
is derived from the existing :func:`fin.corpus.push.idempotency_keys_digest`
helper — no second hashing scheme and no second `source_ref` spelling.
Because the key covers the batch's composition, a resumed run only
skips a batch whose contents are byte-for-byte the batch that already
succeeded.

:class:`PushJournal` holds those keys. A batch whose key is already
completed is not pushed at all, so a resumed run writes nothing for it
(block total increment = 0); the remaining batches are pushed and fill
the document up to :func:`expected_block_total`. Should an
acknowledgement be lost *after* the store accepted a batch, the replay
is still safe: the wire-level idempotency key
``(source_ref, body_content_id)`` makes it a duplicate, not a second
write.

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

from fin.corpus.push import (
    PushEnvelope,
    PushRecord,
    idempotency_keys_digest,
)
from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects
from fin.retrieve.input_validation import InputValidationLedger
from fin.retrieve.push_port import (
    MAX_PUSH_BATCH_RECORDS,
    PushBatchFailedError,
    PushBatchOverflowError,
    PushBatchSizeInvalidError,
    PushError,
    PushPort,
    push_envelopes,
    validate_batch_not_empty,
)

_EVENT_COMPLETED: Final[str] = "completed"
_EVENT_FAILED: Final[str] = "failed"


@dataclass(frozen=True)
class BatchPlan:
    """One planned push batch: its position, its key and its envelopes."""

    index: int
    key: str
    envelopes: tuple[PushEnvelope, ...]

    @property
    def records(self) -> tuple[PushEnvelope, ...]:
        """Backward-compat alias for :attr:`envelopes`.

        Older callers used ``plan.records`` (the pre-T1 per-child
        record tuple); after T1 the wire unit is the envelope. The
        alias lets a test keep reading ``plan.records`` without
        rewriting — the envelope tuple is what the driver actually
        pushes.
        """
        return self.envelopes

    def source_refs(self) -> tuple[str, ...]:
        """The distinct `source_ref` values in the batch, in first-seen
        order — the frozen units this batch touches."""
        seen: dict[str, None] = {}
        for envelope in self.envelopes:
            seen.setdefault(envelope.source_ref, None)
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

    ``block_ids`` covers the batches this run pushed, in envelope
    order; skipped batches contribute none (their ids were returned by
    the run that completed them).
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


def expected_block_total(envelopes: Sequence[PushEnvelope]) -> int:
    """The block total a complete push of ``envelopes`` must reach.

    Distinct envelope idempotency keys, not envelope count: two
    envelopes with the same ``(source_ref, body_content_id)`` pair are
    one write by contract.
    """
    return len({envelope.idempotency_key for envelope in envelopes})


def batch_key(envelopes: Sequence[PushEnvelope]) -> str:
    """The deterministic key of one batch composition (see module doc)."""
    return idempotency_keys_digest(
        [envelope.idempotency_key for envelope in envelopes]
    )


def plan_batches(
    items: Sequence[object],
    *,
    batch_size: int = MAX_PUSH_BATCH_RECORDS,
) -> tuple[BatchPlan, ...]:
    """Split ``items`` (envelopes or :class:`PushRecord` rows) into
    consecutive batches of at most ``batch_size`` items (SC44: split,
    never truncate).

    ``PushRecord`` rows are coerced into single-chunk envelopes so the
    wire-shape invariants (one envelope per 资料 + monotonic chunks)
    hold on the batches the driver pushes.
    """
    if batch_size <= 0:
        raise PushBatchSizeInvalidError(
            f"batch size must be positive, got {batch_size}"
        )
    if batch_size > MAX_PUSH_BATCH_RECORDS:
        raise PushBatchOverflowError(
            f"batch size {batch_size} exceeds the per-call cap of "
            f"{MAX_PUSH_BATCH_RECORDS}"
        )
    envelopes = tuple(_coerce_envelope(item) for item in items)
    plans: list[BatchPlan] = []
    for index, start in enumerate(range(0, len(envelopes), batch_size)):
        chunk = envelopes[start : start + batch_size]
        plans.append(
            BatchPlan(index=index, key=batch_key(chunk), envelopes=chunk)
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
                "block_count": len(plan.envelopes),
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
            block_count=len(plan.envelopes),
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
    items: Sequence[object],
    *,
    journal: PushJournal,
    batch_size: int = MAX_PUSH_BATCH_RECORDS,
    input_validation_ledger: InputValidationLedger | None = None,
) -> BatchedPushOutcome:
    """Push ``items`` (envelopes or :class:`PushRecord` rows) batch by
    batch, resuming from ``journal``.

    Batches already marked completed are skipped without a push call.
    The first failing batch is journaled with its stable error code and
    the typed error is re-raised; running again with the same journal
    replays exactly the batches that did not complete.

    A push of no items is invalid input (§B2 第 5 条) and is refused
    by name here rather than returning a zero-batch outcome, which
    would be exactly the empty success the contract forbids.
    :func:`plan_batches` stays a pure splitter and answers ``()`` for
    an empty sequence; the judgement lives on the push entry point.
    ``input_validation_ledger`` is where that refusal is counted (T21).

    Items can be :class:`PushEnvelope` instances (the wire shape) or
    :class:`PushRecord` rows (the per-child record the invalidation
    path still keys on); the latter are coerced into envelopes here so
    legacy callers don't break.
    """
    validate_batch_not_empty(items, ledger=input_validation_ledger)
    plans = plan_batches(items, batch_size=batch_size)
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
        return push_envelopes(port, plan.envelopes)
    except PushError as exc:
        journal.record_failure(plan, code=exc.code, message=str(exc))
        raise
    except Exception as exc:  # transport-level: wrap, never swallow
        wrapped = PushBatchFailedError(
            f"batch {plan.index} failed: {type(exc).__name__}: {exc}"
        )
        journal.record_failure(plan, code=wrapped.code, message=str(exc))
        raise wrapped from exc


def _coerce_envelope(item: object) -> PushEnvelope:
    """Convert a :class:`PushRecord` to a :class:`PushEnvelope` for the
    wire path, or return a wire envelope as-is.

    Used by the back-compat path so older tests / catalog callers that
    hand :class:`PushRecord` rows to ``push_in_batches`` keep working
    after T1. Production callers should pass :class:`PushEnvelope`
    instances built by :func:`fin.corpus.push.envelopes_of_document`.
    """
    if isinstance(item, PushEnvelope):
        return item
    if isinstance(item, PushRecord):
        from fin.corpus.push import envelope_of_unit  # avoid cycle
        # A PushRecord carries a single child block; reconstruct a
        # unit-shaped chunking for envelope_of_unit's ``payloads``
        # iterator. Easier: build the envelope directly.
        from fin.corpus.push import (
            PushChunk,
            content_identity,
            serialize_source_ref,
        )
        # Use the record's text as both body and single chunk, since
        # the record is the per-child leaf.
        metadata = dict(item.metadata)
        return PushEnvelope(
            source_ref=item.source_ref,
            body=item.text,
            chunks=(PushChunk(index=0, content=item.text),),
            metadata=metadata,  # type: ignore[arg-type]
            valid_from="0001-01-01T00:00:00.000Z",
            valid_until="9999-12-31T23:59:59.999Z",
            body_content_id=item.content_id,
        )
    raise TypeError(
        f"cannot coerce {type(item).__name__} into a push envelope"
    )


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
