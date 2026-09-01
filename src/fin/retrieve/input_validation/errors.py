"""Named failures and input-class constants for the §B2 dichotomy."""

from __future__ import annotations

from typing import Final

from fin.retrieve.degradation import DegradationSignal

INPUT_CLASS_EMPTY_DOCUMENT: Final[str] = "empty_document"
INPUT_CLASS_EMPTY_QUERY: Final[str] = "empty_query"
INPUT_CLASS_EMPTY_BATCH: Final[str] = "empty_batch"
INPUT_CLASS_CONFLICTING_FILTERS: Final[str] = "conflicting_filters"

INPUT_VALIDATION_CLASSES: Final[tuple[str, ...]] = (
    INPUT_CLASS_EMPTY_DOCUMENT,
    INPUT_CLASS_EMPTY_QUERY,
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_CONFLICTING_FILTERS,
)

# Operator-set path for the persisted count. Unset (or blank) keeps the
# ledger in memory, which is the default test suite's shape.
INPUT_VALIDATION_LOG_ENV: Final[str] = "FIN_INPUT_VALIDATION_LOG"


class InputValidationError(RuntimeError):
    """Base class for「入参非法」failures.

    Every subclass carries a distinct stable :attr:`code` (the T14
    ``PushError`` shape) and names its :attr:`input_class` — one of
    :data:`INPUT_VALIDATION_CLASSES`.

    :attr:`degraded` / :attr:`degraded_reasons` are declared (field names
    verbatim per §B3) and are **not** overridable to a set flag by any
    subclass of this family: invalid input is not a capability failure,
    and :func:`record_input_failure` refuses to count a failure that
    claims otherwise.
    """

    code: str = "input_validation_error"
    input_class: str = ""
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


class EmptyQueryError(InputValidationError):
    """The query is empty or whitespace-only (§B2 第 5 条).

    Raised instead of answering ``{"chunks": []}``: an empty result would
    tell the caller "the corpus has nothing to say about this", when the
    truth is that nothing was asked.
    """

    code = "empty_query"
    input_class = INPUT_CLASS_EMPTY_QUERY


class ConflictingFiltersError(InputValidationError):
    """Two filter conditions that no single document can satisfy (SC43).

    Distinct from an **unknown** filter value, which is syntactically
    fine and simply matches nothing: that stays a legal empty result. The
    conflict is only named where fin can prove it from bindings it holds
    (see :func:`detect_filter_conflict`) — fin does not invent a
    contradiction it cannot demonstrate.
    """

    code = "conflicting_filters"
    input_class = INPUT_CLASS_CONFLICTING_FILTERS


class UnclassifiedInputFailureError(RuntimeError):
    """A failure was offered to the input-validation count without
    belonging to any of the four classes.

    Raised rather than counted under a catch-all bucket: a count that
    accepts anything stops being evidence for SC42.
    """

    code = "unclassified_input_failure"


class InvalidInputMarkedDegradedError(RuntimeError):
    """An invalid-input failure arrived with ``degraded`` set.

    The one rule 不可变通 in this ticket, enforced where the count is
    taken rather than only in a test: a caller error must never be
    reported as a capability degradation.
    """

    code = "invalid_input_marked_degraded"


class _InputValidationEnvelopeError(InputValidationError):
    """Represent a validated error envelope at the exception seam."""

    def __init__(
        self,
        *,
        code: str,
        input_class: str,
        signal: DegradationSignal,
    ) -> None:
        super().__init__(f"error envelope: {code}")
        self.code = code
        self.input_class = input_class
        self.degraded = signal.degraded
        self.degraded_reasons = signal.degraded_reasons


class InputValidationLedgerUnwritableError(RuntimeError):
    """The ledger's path could not be appended to.

    Raised instead of counting in memory and reporting success: a
    validation failure the operator cannot read later is not counted.
    """

    code = "input_validation_ledger_unwritable"


class InputValidationLedgerCorruptError(RuntimeError):
    """A persisted ledger line could not be replayed.

    Skipping it would silently lower the count SC42 asserts on.
    """

    code = "input_validation_ledger_corrupt"
