"""Input-validation failure count and persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn

from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects

from fin.retrieve.input_validation.classification import (
    classify_input_failure,
    degradation_signal_of_failure,
)
from fin.retrieve.input_validation.errors import (
    INPUT_VALIDATION_CLASSES,
    INPUT_VALIDATION_LOG_ENV,
    InputValidationError,
    InputValidationLedgerCorruptError,
    InputValidationLedgerUnwritableError,
    InvalidInputMarkedDegradedError,
    UnclassifiedInputFailureError,
)


@dataclass(frozen=True)
class InputValidationRecord:
    """One counted input-validation failure."""

    code: str
    input_class: str
    detail: str


class InputValidationLedger:
    """Counts input-validation failures; persists them given a ``path``.

    With a ``path`` the ledger is an append-only JSONL log and building
    one over an existing path replays it, so ``count() > 0`` stays
    assertable in a later process. Without one it counts in memory —
    which is still a count: a seam built without a ledger path is quiet,
    never blind.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._records: list[InputValidationRecord] = []
        if path is not None and path.exists():
            self._records.extend(self._replay(path))

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- reading -----

    def count(self) -> int:
        """Total counted input-validation failures."""
        return len(self._records)

    def records(self) -> tuple[InputValidationRecord, ...]:
        return tuple(self._records)

    def count_by_class(self) -> dict[str, int]:
        """Per-class totals over the four classes."""
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.input_class] = counts.get(record.input_class, 0) + 1
        return counts

    def count_by_code(self) -> dict[str, int]:
        """Per-code totals — the closed vocabulary makes this assertable."""
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.code] = counts.get(record.code, 0) + 1
        return counts

    # ----- recording -----

    def record(
        self, *, code: str, input_class: str, detail: str = ""
    ) -> InputValidationRecord:
        """Append one record. Persistence happens before the in-memory
        count moves, so a ledger that raises never leaves a count the
        caller believes was written."""
        record = InputValidationRecord(
            code=code, input_class=input_class, detail=detail
        )
        self._append(record)
        self._records.append(record)
        return record

    # ----- persistence -----

    def _append(self, record: InputValidationRecord) -> None:
        append_jsonl_object(
            self._path,
            {
                "code": record.code,
                "input_class": record.input_class,
                "detail": record.detail,
            },
            unwritable=InputValidationLedgerUnwritableError,
            message=(
                "cannot append the input-validation record to "
                f"{self._path}"
            ),
            ensure_ascii=False,
        )

    def _replay(self, path: Path) -> list[InputValidationRecord]:
        return replay_jsonl_objects(
            path,
            lambda event, number: self._record_of_event(event, path, number),
            on_read_oserror=lambda exc: InputValidationLedgerCorruptError(
                f"cannot read the input-validation ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_unicode=lambda exc: InputValidationLedgerCorruptError(
                f"cannot read the input-validation ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_json=lambda exc, number: InputValidationLedgerCorruptError(
                f"input-validation ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ),
        )

    @staticmethod
    def _record_of_event(
        event: object, path: Path, number: int
    ) -> InputValidationRecord:
        try:
            payload = event if isinstance(event, dict) else {}
            if not isinstance(event, dict):
                raise TypeError(
                    f"event is {type(event).__name__}, expected an object"
                )
            return InputValidationRecord(
                code=str(payload["code"]),
                input_class=str(payload["input_class"]),
                detail=str(payload.get("detail", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InputValidationLedgerCorruptError(
                f"input-validation ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ) from exc


def input_validation_ledger_from_env(
    env: Mapping[str, str] | None = None,
) -> InputValidationLedger:
    """Build the ledger the operator configured.

    ``FIN_INPUT_VALIDATION_LOG`` set to a path makes the count persist;
    unset or blank keeps it in memory.
    """
    source = os.environ if env is None else env
    configured = (source.get(INPUT_VALIDATION_LOG_ENV) or "").strip()
    if not configured:
        return InputValidationLedger()
    return InputValidationLedger(path=Path(configured))


def record_input_failure(
    exc: BaseException, *, ledger: InputValidationLedger, detail: str = ""
) -> InputValidationRecord:
    """Count ``exc`` as an input-validation failure.

    Refuses two shapes rather than counting them: a failure outside the
    four classes (:class:`UnclassifiedInputFailureError`) and one that
    declares ``degraded`` (:class:`InvalidInputMarkedDegradedError`).
    """
    input_class = classify_input_failure(exc)
    if input_class is None:
        raise UnclassifiedInputFailureError(
            f"{type(exc).__name__} (code "
            f"{getattr(exc, 'code', None)!r}) is not one of the "
            f"input-validation classes {list(INPUT_VALIDATION_CLASSES)}"
        )
    if degradation_signal_of_failure(exc).degraded:
        raise InvalidInputMarkedDegradedError(
            f"{type(exc).__name__} is invalid input yet declares "
            "degraded; §B2 第 5 条: 入参非法不是能力失败"
        )
    return ledger.record(
        code=str(getattr(exc, "code")),
        input_class=input_class,
        detail=detail or str(exc),
    )


def raise_input_failure(
    exc: InputValidationError, *, ledger: InputValidationLedger
) -> NoReturn:
    """Count ``exc`` and raise it — the count and the typed error are one
    act, so no validation site can name a failure without leaving the
    number SC42 asserts on."""
    record_input_failure(exc, ledger=ledger)
    raise exc
