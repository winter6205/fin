"""Observe which side of the §B2 dichotomy one call landed on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping, Sequence

from fin.retrieve.degradation import read_degradation_signal

from fin.retrieve.input_validation.classification import (
    classify_input_failure,
    classify_input_failure_code,
    degradation_signal_of_failure,
)
from fin.retrieve.input_validation.errors import _InputValidationEnvelopeError
from fin.retrieve.input_validation.ledger import InputValidationLedger, record_input_failure

OUTCOME_INVALID_INPUT: Final[str] = "invalid_input"
OUTCOME_LEGAL_EMPTY: Final[str] = "legal_empty"
OUTCOME_HITS: Final[str] = "hits"
OUTCOME_OTHER_FAILURE: Final[str] = "other_failure"


@dataclass(frozen=True)
class InputOutcome:
    """Which side of the §B2 第 5 条 split one call landed on."""

    kind: str
    code: str = ""
    input_class: str = ""
    degraded: bool = False
    counted: bool = False
    result: Any = None


def is_empty_result(result: Any, *, result_key: str = "chunks") -> bool:
    """True for a success envelope carrying an empty result list.

    An ``{"error": ...}`` envelope is not an empty result — that is the
    named-failure shape, and calling it "empty" is the conflation §B2
    forbids.
    """
    if not isinstance(result, Mapping) or "error" in result:
        return False
    value = result.get(result_key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    return len(value) == 0


def observe_input_outcome(
    call: Callable[[], Any],
    *,
    ledger: InputValidationLedger,
    result_key: str = "chunks",
) -> InputOutcome:
    """Run ``call`` once and classify its answer against the dichotomy.

    This is the seam that makes 「两类不被混同」 one comparison: the same
    function judges an invalid input and a legal empty result, so a test
    asserts the two land on different kinds rather than eyeballing two
    unrelated shapes. A recognised invalid input is counted on ``ledger``
    as it passes through; an exception outside the four classes is
    re-raised untouched — this seam names failures, it never absorbs them.
    """
    try:
        result = call()
    except Exception as exc:
        input_class = classify_input_failure(exc)
        if input_class is None:
            raise
        record_input_failure(exc, ledger=ledger)
        return InputOutcome(
            kind=OUTCOME_INVALID_INPUT,
            code=str(getattr(exc, "code")),
            input_class=input_class,
            degraded=degradation_signal_of_failure(exc).degraded,
            counted=True,
        )

    if isinstance(result, Mapping) and "error" in result:
        code = str(result["error"])
        input_class = classify_input_failure_code(code)
        signal = read_degradation_signal(result)
        if input_class is None:
            return InputOutcome(
                kind=OUTCOME_OTHER_FAILURE,
                code=code,
                degraded=signal.degraded,
                result=result,
            )
        record_input_failure(
            _InputValidationEnvelopeError(
                code=code,
                input_class=input_class,
                signal=signal,
            ),
            ledger=ledger,
            detail="error envelope",
        )
        return InputOutcome(
            kind=OUTCOME_INVALID_INPUT,
            code=code,
            input_class=input_class,
            degraded=signal.degraded,
            counted=True,
            result=result,
        )

    signal = read_degradation_signal(result)
    empty = is_empty_result(result, result_key=result_key)
    return InputOutcome(
        kind=OUTCOME_LEGAL_EMPTY if empty else OUTCOME_HITS,
        degraded=signal.degraded,
        result=result,
    )
