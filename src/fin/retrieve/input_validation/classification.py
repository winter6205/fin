"""Closed code registry and failure classification rules."""

from __future__ import annotations

from typing import Final

from fin.corpus.errors import EmptySourceParseError, NoUnitsParseError
from fin.retrieve.degradation import DegradationSignal, read_degradation_signal

from fin.retrieve.input_validation.errors import (
    INPUT_CLASS_CONFLICTING_FILTERS,
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_EMPTY_DOCUMENT,
    INPUT_CLASS_EMPTY_QUERY,
    ConflictingFiltersError,
    EmptyQueryError,
)

# Every code that counts as an input-validation failure, mapped to its
# class. The two parse codes are read off the corpus error classes
# rather than re-typed, so a rename there cannot silently drop the
# document class out of the count. The push codes are strings because
# ``fin.retrieve.push_port`` imports *this* module (a test pins them
# against ``PushEmptyBatchError.code`` / ``PushEmptyDocumentError.code``
# so the pair cannot drift apart unnoticed).
_INPUT_CLASS_BY_CODE: Final[dict[str, str]] = {
    EmptyQueryError.code: INPUT_CLASS_EMPTY_QUERY,
    ConflictingFiltersError.code: INPUT_CLASS_CONFLICTING_FILTERS,
    EmptySourceParseError.code: INPUT_CLASS_EMPTY_DOCUMENT,
    NoUnitsParseError.code: INPUT_CLASS_EMPTY_DOCUMENT,
    "push_empty_document": INPUT_CLASS_EMPTY_DOCUMENT,
    "push_empty_batch": INPUT_CLASS_EMPTY_BATCH,
}


def input_validation_codes() -> tuple[str, ...]:
    """Every code the count accepts, sorted."""
    return tuple(sorted(_INPUT_CLASS_BY_CODE))


def codes_for_input_class(input_class: str) -> tuple[str, ...]:
    """The codes belonging to one of the four classes, sorted."""
    return tuple(
        sorted(
            code
            for code, klass in _INPUT_CLASS_BY_CODE.items()
            if klass == input_class
        )
    )


def classify_input_failure_code(code: str) -> str | None:
    """The input class ``code`` belongs to, or ``None``."""
    return _INPUT_CLASS_BY_CODE.get(code)


def classify_input_failure(exc: BaseException) -> str | None:
    """The input class ``exc`` belongs to, or ``None`` for anything else.

    Classification goes through the code registry, so an exception type
    that grew a new subclass is classified by the code it publishes
    rather than by where it sits in a class tree.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return None
    return classify_input_failure_code(code)


def degradation_signal_of_failure(exc: BaseException) -> DegradationSignal:
    """The §B3 signal ``exc`` declares, read through T20's authority.

    The two field names are spelled once, in
    :mod:`fin.retrieve.degradation`; this function builds that module's
    envelope shape from the failure's own attributes and hands it to
    :func:`read_degradation_signal`, so "is this failure degraded" has
    exactly one answer in the codebase — and a failure claiming a set
    flag without reasons fails there rather than passing here.
    """
    flag = bool(getattr(exc, "degraded", False))
    reasons = tuple(getattr(exc, "degraded_reasons", ()) or ())
    signal = DegradationSignal(degraded=flag, degraded_reasons=reasons)
    return read_degradation_signal(signal.as_envelope_fields())
