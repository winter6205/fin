"""Contract tests for the parser error taxonomy (contract section 6).

Every parser failure must be a typed exception with a stable,
machine-readable ``code``. The code is what travels to the HTTP body
(``{"error": "<code>"}``), so it must not drift per call site, and no
failure may be signalled by returning ``None``, ``-1`` or ``""``.
"""

from __future__ import annotations

import re

import pytest

from fin.parser.errors import (
    ParserEnvelopeError,
    ParserError,
    ParserInvalidRequestError,
    ParserJobCancelledError,
    ParserJobCapacityError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobTombstonedError,
    ParserNotificationError,
    ParserPersistenceError,
    ParserStaleFenceError,
)

ERROR_CLASSES = (
    ParserInvalidRequestError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobCapacityError,
    ParserJobCancelledError,
    ParserJobTombstonedError,
    ParserStaleFenceError,
    ParserPersistenceError,
    ParserEnvelopeError,
    ParserNotificationError,
)

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def test_parser_error_is_the_common_base_of_every_taxonomy_member() -> None:
    assert issubclass(ParserError, RuntimeError)
    for error_class in ERROR_CLASSES:
        assert issubclass(error_class, ParserError), error_class.__name__


def test_every_error_class_exposes_a_stable_machine_readable_code() -> None:
    for error_class in (*ERROR_CLASSES, ParserError):
        code = error_class.code
        assert isinstance(code, str), error_class.__name__
        assert code, error_class.__name__
        assert CODE_PATTERN.match(code), code


def test_error_codes_are_unique_across_the_taxonomy() -> None:
    codes = [error_class.code for error_class in (*ERROR_CLASSES, ParserError)]
    assert len(set(codes)) == len(codes)


def test_an_instance_reports_the_code_of_its_own_class() -> None:
    for error_class in ERROR_CLASSES:
        error = error_class("boom")
        assert error.code == error_class.code
        assert isinstance(error, ParserError)


def test_an_error_carries_a_human_message() -> None:
    error = ParserJobNotFoundError("job 42 does not exist")
    assert str(error) == "job 42 does not exist"
    assert error.message == "job 42 does not exist"


def test_an_error_carries_optional_structured_details() -> None:
    error = ParserJobConflictError(
        "idempotency key reused with a different digest",
        idempotency_key="k-1",
    )
    assert error.details["idempotency_key"] == "k-1"


@pytest.mark.parametrize("error_class", ERROR_CLASSES)
def test_a_failure_is_raised_not_returned(error_class: type[ParserError]) -> None:
    with pytest.raises(error_class):
        raise error_class("typed failure")
