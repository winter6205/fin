"""Queryable invalidation record persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Sequence

from fin.corpus.invalidation import InvalidationTarget
from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects

from fin.retrieve.invalidation_port.errors import (
    CODE_INVALIDATION_SETTLED,
    InvalidationLedgerCorruptError,
    InvalidationLedgerUnwritableError,
    InvalidationOutcome,
    InvalidationRecord,
)

_EVENT_SETTLED: Final[str] = "settled"
_EVENT_FAILED: Final[str] = "failed"


class InvalidationLedger:
    """The queryable record of the invalidation requests fin issued.

    Append-only. With a ``path`` every entry is also a JSONL line and the
    ledger reloads by replaying them, so the record survives the process;
    without a path it is in-memory only (the default suite's shape).
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._records: list[InvalidationRecord] = []
        if path is not None and path.exists():
            self._records.extend(self._replay(path))

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- queries -----

    def records(self) -> tuple[InvalidationRecord, ...]:
        """Every entry, in the order it was recorded."""
        return tuple(self._records)

    def failures(self) -> tuple[InvalidationRecord, ...]:
        """Entries for requests that failed — the replay work list."""
        return tuple(record for record in self._records if not record.settled)

    def settled_requests(self) -> tuple[str, ...]:
        """Distinct request keys the service settled, in first-seen order.

        A repeated (idempotent) request does not add a key here, though
        it does add an entry to :meth:`records`.
        """
        seen: dict[str, None] = {}
        for record in self._records:
            if record.settled:
                seen.setdefault(record.request_key, None)
        return tuple(seen)

    def records_for_source_ref(
        self, source_ref: str
    ) -> tuple[InvalidationRecord, ...]:
        """Entries touching one frozen unit."""
        return tuple(
            record
            for record in self._records
            if source_ref in record.source_refs
        )

    def count(self) -> int:
        return len(self._records)

    # ----- recording -----

    def record_settled(
        self,
        key: str,
        targets: Sequence[InvalidationTarget],
        outcome: InvalidationOutcome,
    ) -> InvalidationRecord:
        """Record that ``key`` was settled by the service."""
        return self._record(
            InvalidationRecord(
                request_key=key,
                code=CODE_INVALIDATION_SETTLED,
                settled=True,
                source_refs=_distinct(t.source_ref for t in targets),
                block_ids=tuple(t.block_id for t in targets),
                reasons=_distinct(t.reason for t in targets),
                closed=outcome.closed,
                already_closed=outcome.already_closed,
            ),
            _EVENT_SETTLED,
        )

    def record_failure(
        self,
        key: str,
        targets: Sequence[InvalidationTarget],
        *,
        code: str,
        message: str,
    ) -> InvalidationRecord:
        """Record that ``key`` failed, by name."""
        return self._record(
            InvalidationRecord(
                request_key=key,
                code=code,
                settled=False,
                source_refs=_distinct(t.source_ref for t in targets),
                block_ids=tuple(t.block_id for t in targets),
                reasons=_distinct(t.reason for t in targets),
                closed=0,
                already_closed=0,
                message=message,
            ),
            _EVENT_FAILED,
        )

    # ----- persistence -----

    def _record(
        self, record: InvalidationRecord, event: str
    ) -> InvalidationRecord:
        # Persist first, count second: an entry fin failed to write must
        # not be reported as recorded.
        self._append(record, event)
        self._records.append(record)
        return record

    def _append(self, record: InvalidationRecord, event: str) -> None:
        append_jsonl_object(
            self._path,
            {
                "event": event,
                "request_key": record.request_key,
                "code": record.code,
                "source_refs": list(record.source_refs),
                "block_ids": list(record.block_ids),
                "reasons": list(record.reasons),
                "closed": record.closed,
                "already_closed": record.already_closed,
                "message": record.message,
            },
            unwritable=InvalidationLedgerUnwritableError,
            message=(
                "cannot append the invalidation record for request "
                f"{record.request_key!r} to {self._path}"
            ),
        )

    def _replay(self, path: Path) -> list[InvalidationRecord]:
        return replay_jsonl_objects(
            path,
            lambda event, number: self._record_of_event(event, path, number),
            on_json=lambda exc, number: InvalidationLedgerCorruptError(
                f"{path}:{number} is not a readable invalidation record: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    def _record_of_event(
        self, event: object, path: Path, number: int
    ) -> InvalidationRecord:
        try:
            if not isinstance(event, dict):
                raise TypeError(
                    f"event is {type(event).__name__}, expected an object"
                )
            settled = str(event["event"]) == _EVENT_SETTLED
            return InvalidationRecord(
                request_key=str(event["request_key"]),
                code=str(event["code"]),
                settled=settled,
                source_refs=tuple(str(ref) for ref in event["source_refs"]),
                block_ids=tuple(str(bid) for bid in event["block_ids"]),
                reasons=tuple(str(reason) for reason in event["reasons"]),
                closed=int(event["closed"]),
                already_closed=int(event["already_closed"]),
                message=str(event.get("message", "")),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidationLedgerCorruptError(
                f"{path}:{number} is not a readable invalidation record: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def _distinct(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(str(value), None)
    return tuple(seen)
