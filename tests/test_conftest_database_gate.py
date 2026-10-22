"""The conftest DSN gate: only ``FIN_TEST_DSN``, and only a ``_test`` library.

AGENTS.md hard boundary 4 forbids a test run from reading or writing the
development database. These tests pin URI and libpq keyword conninfo parsing,
the effective database suffix, and safe failures without exposing connection
fields.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Final

import pytest

from tests.conftest import (
    DatabaseGateError,
    _configure_test_database,
    _postgres_database_name,
)

TEST_DSN: Final = "postgresql://fin:secret@127.0.0.1:5433/fin_test?sslmode=disable"
DAILY_DSN: Final = "postgresql://fin:secret@127.0.0.1:5433/fin?sslmode=disable"
KEYWORD_TEST_DSN: Final = (
    "dbname=fin_test user=canary host=example.invalid port=5433 "
    "password=notreal sslmode=disable"
)
KEYWORD_DAILY_DSN: Final = (
    "dbname=fin user=canary host=example.invalid port=5433 "
    "password=notreal sslmode=disable"
)
MALFORMED_KEYWORD_DSN: Final = (
    "dbname=fin_test user=canary host=example.invalid port=5433 "
    "password=notreal broken='"
)


@pytest.fixture(autouse=True)
def _restore_database_env() -> Iterator[None]:
    """Undo ``_configure_test_database``'s direct writes to ``os.environ``.

    The gate mutates ``FIN_CHECKPOINT_URL`` outside monkeypatch's tracking, so a
    run of it here would otherwise leak the fake DSN into every later test in the
    session and push durable suites onto a bogus connection. Snapshot and restore
    the two keys the gate touches.
    """
    before = {
        key: os.environ.get(key) for key in ("FIN_TEST_DSN", "FIN_CHECKPOINT_URL")
    }
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (TEST_DSN, "fin_test"),
        (DAILY_DSN, "fin"),
        ("postgres://user@host:5432/warehouse_test", "warehouse_test"),
        ("postgresql://host/db", "db"),
        ("postgresql://host/", ""),
        ("postgresql://host/db;fallback=other", "db;fallback=other"),
        ("postgresql://host/fin%5Ftest", "fin_test"),
        ("postgresql://host/path?dbname=fin_test&dbname=fin", "fin"),
        ("postgresql://host/fin_test;fallback=other", "fin_test;fallback=other"),
        ("dbname=fin_test user=canary host=example.invalid", "fin_test"),
    ],
)
def test_database_name_reads_only_the_library(dsn: str, expected: str) -> None:
    """The name parse never exposes the host, credential, or option tail."""
    assert _postgres_database_name(dsn) == expected


def test_unset_test_dsn_drops_the_ambient_daily_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``FIN_TEST_DSN`` -> the development DSN must not reach the run."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", DAILY_DSN)

    _configure_test_database()

    assert "FIN_CHECKPOINT_URL" not in os.environ


def test_valid_test_dsn_is_bridged_onto_checkpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``_test`` DSN is published where the stores and durable suites read."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    _configure_test_database()

    assert os.environ["FIN_CHECKPOINT_URL"] == TEST_DSN


def test_non_test_dsn_aborts_instead_of_targeting_the_daily_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN without the ``_test`` suffix is a failure, not a silent skip."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", DAILY_DSN)

    with pytest.raises(DatabaseGateError, match="does not end in"):
        _configure_test_database()


def test_valid_keyword_conninfo_is_bridged_onto_checkpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Libpq keyword conninfo targets the same _test database as a URI."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", KEYWORD_TEST_DSN)

    _configure_test_database()

    assert os.environ["FIN_CHECKPOINT_URL"] == KEYWORD_TEST_DSN


def test_non_test_keyword_conninfo_is_rejected_without_leaking_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected keyword DSN exposes neither its value nor connection fields."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", KEYWORD_DAILY_DSN)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", DAILY_DSN)

    with pytest.raises(DatabaseGateError, match="does not end in") as raised:
        _configure_test_database()

    message = str(raised.value)
    for private_value in (KEYWORD_DAILY_DSN, "canary", "example.invalid", "notreal"):
        assert private_value not in message
    assert os.environ["FIN_CHECKPOINT_URL"] == DAILY_DSN


def test_malformed_keyword_conninfo_is_rejected_without_leaking_or_chaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed parser input becomes a generic gate error without field leaks."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", MALFORMED_KEYWORD_DSN)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", DAILY_DSN)

    with pytest.raises(
        DatabaseGateError, match="not a valid PostgreSQL connection setting"
    ) as raised:
        _configure_test_database()

    details = "\\n".join(
        str(error)
        for error in (
            raised.value,
            raised.value.__cause__,
            raised.value.__context__,
        )
        if error is not None
    )
    for private_value in (
        MALFORMED_KEYWORD_DSN,
        "canary",
        "example.invalid",
        "notreal",
    ):
        assert private_value not in details
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert os.environ["FIN_CHECKPOINT_URL"] == DAILY_DSN


def test_percent_encoded_test_database_name_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URI decoding follows libpq's effective database target."""
    encoded_test_dsn = "postgresql://canary:notreal@example.invalid/fin%5Ftest"
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", encoded_test_dsn)

    _configure_test_database()

    assert os.environ["FIN_CHECKPOINT_URL"] == encoded_test_dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://example.invalid/fin_test;fallback=other",
        "postgresql://example.invalid/path?dbname=fin_test&dbname=fin",
    ],
)
def test_effective_non_test_database_suffix_is_rejected(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suffix checks apply to libpq's effective name, not partial URI text."""
    monkeypatch.delenv("FIN_TEST_DSN", raising=False)
    monkeypatch.setenv("FIN_TEST_DSN", dsn)

    with pytest.raises(DatabaseGateError):
        _configure_test_database()
