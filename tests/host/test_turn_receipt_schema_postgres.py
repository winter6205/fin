"""The receipt table's own refusals, against a real ``fin_turn_receipt``.

A store that validates in Python can only prove its own code path. The
adopted table also carries the enum CHECKs, the twelve-key
``stage_metrics`` shape and the not-ready pair, and those are what a
second writer, a migration or a hand-run query has to get wrong. Each
case below writes a row that is valid except for the one value under
test, so a rejection is attributable to that constraint.

Skipped when ``FIN_CHECKPOINT_URL`` is unset (the repo's convention).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from fin.host.turn_receipt_store import (
    STAGE_METRIC_FIELDS,
    STAGE_METRIC_KEYS,
    PostgresReceiptStore,
    zero_stage_metrics,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

THREAD_ID = "receipt-schema-thread"


class _Now:
    """Marks a timestamp the database fills in, so no case pins a clock."""


_NOW = _Now()


@pytest.fixture
def url() -> Iterator[str]:
    dsn = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the receipt table's own "
            "constraints are untested in this environment"
        )
    store = PostgresReceiptStore(dsn)
    with _empty(dsn):
        yield dsn
    del store


class _empty:
    """Start from a thread with no receipt row of its own."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def __enter__(self) -> None:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread WHERE id = %s", (THREAD_ID,))
            conn.execute(
                "INSERT INTO fin_thread (id, title) VALUES (%s, %s)",
                (THREAD_ID, "schema"),
            )

    def __exit__(self, *exc: object) -> None:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread WHERE id = %s", (THREAD_ID,))


def _raw_row(row_id: str, **over: Any) -> tuple[str, str, list[Any]]:
    """A row that is valid except for whatever ``over`` replaces."""
    cells: dict[str, Any] = {
        "thread_id": THREAD_ID,
        "turn_id": row_id,
        "schema_version": 1,
        "revision": 1,
        "execution_state": "running",
        "accepted_input_count": 1,
        "accepted_input_version": 0,
        "work_elapsed_ms": 0,
        "user_wait_ms": 0,
        "stage_metrics": zero_stage_metrics(),
        "catalog_warning_count": 0,
    }
    cells.update(over)
    names = ", ".join(cells)
    values: list[Any] = []
    placeholders: list[str] = []
    for name, value in cells.items():
        if value is _NOW:
            placeholders.append("now()")
        elif isinstance(value, (dict, list)):
            placeholders.append("%s")
            values.append(json.dumps(value))
        else:
            placeholders.append("%s")
            values.append(value)
    return names, f"({', '.join(placeholders)})", values


def _insert(url: str, row_id: str, **over: Any) -> None:
    """Write one raw row; a constraint violation reaches the caller."""
    import psycopg

    columns, placeholders, values = _raw_row(row_id, started_at=_NOW,
                                             updated_at=_NOW, **over)
    with psycopg.connect(url) as conn:
        conn.execute(
            f"INSERT INTO fin_turn_receipt ({columns}) VALUES {placeholders}", values
        )
        conn.commit()


def _accepted(url: str, row_id: str, **over: Any) -> bool:
    """Whether one raw row landed. A rejection is the assertion."""
    import psycopg

    try:
        _insert(url, row_id, **over)
    except psycopg.errors.CheckViolation:
        return False
    return True


# ----- the columns themselves -----


def test_the_table_carries_exactly_the_adopted_columns(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable FROM "
            "information_schema.columns WHERE table_name = 'fin_turn_receipt' "
            "ORDER BY ordinal_position"
        ).fetchall()

    assert [name for name, _type, _null in rows] == [
        "thread_id",
        "turn_id",
        "schema_version",
        "revision",
        "execution_state",
        "technical_result",
        "semantic_result",
        "accepted_input_count",
        "accepted_input_version",
        "route_source",
        "route_intent",
        "grounded_decision",
        "decision_parse",
        "citation_verified",
        "not_ready_cause",
        "not_ready_stage",
        "failure_kind",
        "cancellation_reason",
        "started_at",
        "updated_at",
        "terminal_at",
        "work_elapsed_ms",
        "user_wait_ms",
        "accounting_complete",
        "stage_metrics",
        "catalog_warning_count",
        "token_usage",
        "exported_revision",
    ]
    types = {name: kind for name, kind, _null in rows}
    assert types["revision"] == "bigint"
    assert types["accepted_input_version"] == "bigint"
    assert types["stage_metrics"] == "jsonb"
    assert types["token_usage"] == "jsonb"
    assert types["accounting_complete"] == "boolean"


def test_the_receipt_is_removed_with_its_thread(url: str) -> None:
    """The foreign key is what leaves zero receipts behind a delete."""
    import psycopg

    store = PostgresReceiptStore(url)
    store.begin_turn(
        thread_id=THREAD_ID,
        turn_id="turn-cascade",
        started_at=datetime.now(timezone.utc),
        accepted_input_version=0,
    )
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM fin_thread WHERE id = %s", (THREAD_ID,))
        assert conn.execute(
            "SELECT count(*) FROM fin_turn_receipt WHERE turn_id = %s",
            ("turn-cascade",),
        ).fetchone()[0] == 0


# ----- the enums -----


def test_a_valid_row_is_accepted(url: str) -> None:
    assert _accepted(url, "row-ok") is True


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("execution_state", "finished"),
        ("execution_state", ""),
        ("technical_result", "answered"),
        ("technical_result", "cancelled"),
        ("semantic_result", "answer"),
        ("semantic_result", "done"),
        ("route_source", "model"),
        ("route_intent", "search"),
        ("route_intent", "refuse"),
        ("grounded_decision", "answered"),
        ("grounded_decision", "answer!"),
        ("decision_parse", "json"),
        ("cancellation_reason", "because"),
        ("cancellation_reason", "fenced"),
        ("failure_kind", "weird"),
        ("failure_kind", "task_input"),
        ("not_ready_cause", "cold"),
        ("not_ready_stage", "model"),
    ],
)
def test_an_unlisted_enum_value_is_refused(url: str, column: str, value: str) -> None:
    assert _accepted(url, f"enum-{column}-{value}", **{column: value}) is False


@pytest.mark.parametrize(
    ("cause", "stage"),
    [("index", None), (None, "queue"), ("busy", "index"), ("index", "queue")],
)
def test_an_unobserved_not_ready_pair_is_refused(
    url: str, cause: str | None, stage: str | None
) -> None:
    assert (
        _accepted(
            url,
            f"pair-{cause}-{stage}",
            not_ready_cause=cause,
            not_ready_stage=stage,
        )
        is False
    )


@pytest.mark.parametrize(
    ("cause", "stage"),
    [("index", "index"), ("busy", "queue"), ("busy", "startup"), ("busy", "response")],
)
def test_an_observed_not_ready_pair_is_accepted(
    url: str, cause: str, stage: str
) -> None:
    assert (
        _accepted(
            url,
            f"pair-ok-{cause}-{stage}",
            not_ready_cause=cause,
            not_ready_stage=stage,
        )
        is True
    )


# ----- the twelve stage entries -----


def test_a_missing_stage_is_refused(url: str) -> None:
    incomplete = {
        key: {"calls": 0, "elapsed_ms": 0, "complete": True}
        for key in STAGE_METRIC_KEYS
        if key != "disconnect_recovery"
    }
    assert _accepted(url, "stage-missing", stage_metrics=incomplete) is False


def test_an_unlisted_stage_is_refused(url: str) -> None:
    extra = {
        **zero_stage_metrics(),
        "exporter": {"calls": 1, "elapsed_ms": 1, "complete": True},
    }
    assert _accepted(url, "stage-extra", stage_metrics=extra) is False


def test_a_stage_entry_without_its_three_fields_is_refused(url: str) -> None:
    short = {
        **zero_stage_metrics(),
        "model": {"calls": 1, "elapsed_ms": 1},
    }
    assert _accepted(url, "stage-short", stage_metrics=short) is False


def test_a_stage_entry_with_a_wrong_type_is_refused(url: str) -> None:
    mistyped = {
        **zero_stage_metrics(),
        "jev": {"calls": 1, "elapsed_ms": "quick", "complete": True},
    }
    assert _accepted(url, "stage-type", stage_metrics=mistyped) is False


def test_a_stage_entry_carrying_only_the_three_fields_is_accepted(url: str) -> None:
    assert set(STAGE_METRIC_FIELDS) == {"calls", "elapsed_ms", "complete"}
    counted = {
        **zero_stage_metrics(),
        "model": {"calls": 3, "elapsed_ms": 40, "complete": False},
    }
    assert _accepted(url, "stage-ok", stage_metrics=counted) is True


# ----- token usage -----


@pytest.mark.parametrize(
    "usage",
    [
        {"guesses": 3},
        {"input_tokens": "lots"},
        {"input_tokens": -1},
    ],
)
def test_token_usage_that_the_contract_does_not_name_is_refused(
    url: str, usage: dict[str, Any]
) -> None:
    assert _accepted(url, f"usage-{sorted(usage)}", token_usage=usage) is False


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10},
        {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        {},
    ],
)
def test_token_usage_may_be_partial_but_is_never_estimated(
    url: str, usage: dict[str, Any]
) -> None:
    assert _accepted(url, f"usage-ok-{len(usage)}", token_usage=usage) is True


def test_absent_token_usage_stays_absent(url: str) -> None:
    assert _accepted(url, "usage-absent", token_usage=None) is True


# ----- bounds -----


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("schema_version", 2),
        ("schema_version", 0),
        ("revision", 0),
        ("revision", -1),
        ("accepted_input_count", 0),
        ("accepted_input_version", -1),
        ("work_elapsed_ms", -1),
        ("user_wait_ms", -1),
        ("catalog_warning_count", -1),
        ("exported_revision", 5),
        ("thread_id", ""),
        ("turn_id", ""),
    ],
)
def test_an_out_of_bounds_value_is_refused(url: str, column: str, value: Any) -> None:
    assert _accepted(url, f"bound-{column}-{value}", **{column: value}) is False


def test_an_over_long_key_is_refused(url: str) -> None:
    assert _accepted(url, "bound-long-turn", turn_id="t" * 257) is False


def test_the_terminal_state_invariant_is_the_store_seam_not_a_check(
    url: str,
) -> None:
    """Where the table stops, stated rather than glossed over.

    ``terminal`` needs a technical result and a terminal time, but that
    pairing is a rule about the turn rather than one value's own range,
    so the table does not police it. The store seam refuses such a
    draft before it is written; this case documents that the row the
    store never produces is still storable by a raw INSERT.
    """
    assert _accepted(url, "terminal-bare", execution_state="terminal") is True


# ----- the failure cause's own pairing -----


def test_a_failure_cause_on_a_turn_that_did_not_fail_is_refused(url: str) -> None:
    """A ``stopped`` or ``done`` turn carries no failure cause.

    The Python validator refuses this pair, and so must the table: a row
    only one of the two twins can write is exactly the drift the durable
    store exists to catch.
    """
    assert _accepted(
        url, "cause-stopped", failure_kind="graph_failed", technical_result="stopped"
    ) is False
    assert _accepted(
        url, "cause-done", failure_kind="graph_failed", technical_result="done"
    ) is False
    assert _accepted(
        url, "cause-failed", failure_kind="graph_failed", technical_result="failed"
    ) is True
    assert _accepted(
        url,
        "cause-timeout",
        failure_kind="model_timeout",
        technical_result="timeout",
    ) is True
