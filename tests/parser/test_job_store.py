"""Durable store tests for the parser job authority (contract sections 3-5).

The parser store is PostgreSQL state, so every case here runs against a
real ``_test`` database. Ownership races use separate OS processes
synchronised on an explicit barrier, never on a sleep, and a restart
read is always performed by a newly started process.

The store must never leak a raw driver error and must never accept work
it could not make durable.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.errors import (
    ParserEnvelopeError,
    ParserExecutionTimeoutError,
    ParserInvalidRequestError,
    ParserJobCapacityError,
    ParserJobCancelledError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobTombstonedError,
    ParserPersistenceError,
    ParserQueueFullError,
    ParserQueueWaitTimeoutError,
    ParserStaleFenceError,
)
from fin.parser.job_store import (
    DEFAULT_EXECUTION_SECONDS,
    DEFAULT_MAX_OPEN_JOBS,
    DEFAULT_QUEUE_WAIT_SECONDS,
    ParserStoreConfig,
    PostgresParserJobStore,
    _committed_document,
    _payload_int,
    _payload_optional_text,
    _payload_text,
    _payload_value,
    _terminal_outcome,
    resolve_parser_limits,
)
from fin.parser.ports import (
    JOB_STATUSES,
    Attempt,
    JobRecord,
    JobSubmission,
    Notification,
    OutboxEvent,
    ParserJobStore,
    ResultRecord,
    StagingArtifact,
    write_outbox_row,
)
from fin.parser.schema import JOB_STATUS_CONSTRAINT_NAME, _TABLE_DDL

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

# A fixed moment, so a deadline is crossed by moving the clock instead of
# by waiting for it.
MOMENT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# A queue-wait deadline so wide that no queued row of the shared test
# database expires while a case counts the pending queue.
NO_EXPIRY_SECONDS = 10_000_000.0

# The open list is read from a shared test database, so the limit is wide
# enough to hold the open jobs of every run.
OPEN_LIST_LIMIT = 100_000

# Every child process waits on this barrier before it touches the store,
# so a race is a race and not a scheduling accident.
_CHILD_PREAMBLE = """
import json, os, time
from pathlib import Path


def wait_for_peers(directory, name, parties, timeout=60.0):
    Path(directory).joinpath("ready-" + name).write_text("ready")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(list(Path(directory).glob("ready-*"))) >= parties:
            return
        time.sleep(0.01)
    raise SystemExit("the barrier peers did not arrive")


wait_for_peers(os.environ["FIN_BARRIER"], os.environ["FIN_PARTY"], int(os.environ["FIN_PARTIES"]))
"""

_SUBMIT_BODY = """
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_PARSER_DSN"])
submission = store.submit_job(
    idempotency_key=os.environ["FIN_KEY"],
    input_sha256=os.environ["FIN_SHA"],
    source_ref=None,
    declared_format=None,
)
print(json.dumps({
    "job_id": submission.job_id,
    "replayed": submission.replayed,
    "pid": os.getpid(),
}))
"""

_CLAIM_BODY = """
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_PARSER_DSN"])
attempt = store.claim_attempt(
    job_id=os.environ["FIN_JOB_ID"],
    worker_id=os.environ["FIN_PARTY"],
    lease_seconds=60,
)
print(json.dumps({
    "token": attempt.fencing_token if attempt else None,
    "worker": os.environ["FIN_PARTY"],
    "pid": os.getpid(),
}))
"""

_READ_BODY = """
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_PARSER_DSN"])
record = store.get_job(os.environ["FIN_JOB_ID"])
print(json.dumps({
    "job_id": record.job_id if record else None,
    "status": record.status if record else None,
    "pid": os.getpid(),
}))
"""


def _run_children(
    tmp_path: Path, bodies: list[str], env: dict[str, str]
) -> list[dict[str, object]]:
    """Run the given child programs together and return their JSON lines."""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    parties = len(bodies)
    processes = []
    for index, body in enumerate(bodies):
        child_env = {
            **os.environ,
            **env,
            "FIN_BARRIER": str(barrier),
            "FIN_PARTY": f"p{index}",
            "FIN_PARTIES": str(parties),
        }
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_PREAMBLE + body],
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    payloads: list[dict[str, object]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, f"the child process failed: {stderr}"
        payloads.append(json.loads(stdout.strip().splitlines()[-1]))
    return payloads


def _submit(store: PostgresParserJobStore, key: str, digest: str = DIGEST_A):
    return store.submit_job(
        idempotency_key=key,
        input_sha256=digest,
        source_ref=None,
        declared_format=None,
    )


def _rows_for_key(dsn: str, key: str) -> int:
    with psycopg.connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_parser.job WHERE idempotency_key = %s", (key,)
            ).fetchone()[0]
        )


def _pending_count(dsn: str) -> int:
    """The queued jobs the shared test database holds."""
    with psycopg.connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_parser.job WHERE status = 'queued'"
            ).fetchone()[0]
        )


def _outbox_events(dsn: str, job_id: str) -> list[dict[str, Any]]:
    """The lifecycle events the outbox holds for one job."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT topic, payload FROM fin_parser.outbox "
            "WHERE payload->>'job_id' = %s ORDER BY created_at, notification_id",
            (job_id,),
        ).fetchall()
    return [{"event": str(topic), **dict(payload)} for topic, payload in rows]


def _event_for(dsn: str, job_id: str, event: str) -> dict[str, Any]:
    """One lifecycle event of one job."""
    for row in _outbox_events(dsn, job_id):
        if row["event"] == event:
            return row
    raise AssertionError(f"the outbox holds no {event} event for {job_id}")


class _Clock:
    """A clock the case moves by hand, so no deadline is ever waited for."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def __call__(self) -> datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        self._moment = self._moment + timedelta(seconds=seconds)


def test_the_store_satisfies_the_parser_job_store_port(
    store: PostgresParserJobStore,
) -> None:
    assert isinstance(store, ParserJobStore)


def test_the_port_value_objects_keep_the_contract_field_names() -> None:
    assert [f.name for f in dataclasses.fields(JobSubmission)] == [
        "job_id",
        "status",
        "replayed",
    ]
    assert [f.name for f in dataclasses.fields(JobRecord)] == [
        "job_id",
        "idempotency_key",
        "input_sha256",
        "status",
        "created_at",
        "updated_at",
    ]
    assert [f.name for f in dataclasses.fields(Attempt)] == [
        "job_id",
        "attempt_no",
        "fencing_token",
        "worker_id",
        "lease_expires_at",
    ]
    assert [f.name for f in dataclasses.fields(ResultRecord)] == [
        "job_id",
        "fencing_token",
        "envelope",
        "result_ref",
        "created_at",
    ]
    assert [f.name for f in dataclasses.fields(Notification)] == [
        "notification_id",
        "topic",
        "payload",
        "created_at",
    ]
    assert [f.name for f in dataclasses.fields(StagingArtifact)] == [
        "artifact_id",
        "job_id",
        "fencing_token",
        "path",
        "referenced",
        "created_at",
    ]


# ----- the store configuration -----


def test_the_store_takes_its_settings_in_one_object(parser_database: str) -> None:
    config = ParserStoreConfig(
        dsn=parser_database,
        max_pending_jobs=2,
        queue_wait_seconds=NO_EXPIRY_SECONDS,
    )

    store = PostgresParserJobStore(config)

    assert store.connection_dsn == parser_database
    assert store.max_pending_jobs == 2
    assert store.queue_wait_seconds == NO_EXPIRY_SECONDS


def test_one_overridden_setting_does_not_restate_the_others(
    monkeypatch: pytest.MonkeyPatch, parser_database: str
) -> None:
    monkeypatch.delenv("FIN_PARSER_MAX_OPEN_JOBS", raising=False)
    monkeypatch.delenv("FIN_PARSER_MAX_PENDING_JOBS", raising=False)
    monkeypatch.delenv("FIN_PARSER_QUEUE_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("FIN_PARSER_EXECUTION_SECONDS", raising=False)

    store = PostgresParserJobStore(
        ParserStoreConfig.from_environment(dsn=parser_database, max_pending_jobs=2)
    )

    assert store.max_pending_jobs == 2
    assert store.max_open_jobs == DEFAULT_MAX_OPEN_JOBS
    assert store.queue_wait_seconds == DEFAULT_QUEUE_WAIT_SECONDS
    assert store.execution_seconds == DEFAULT_EXECUTION_SECONDS


def test_the_config_reads_its_dsn_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_PARSER_DSN", make_conninfo(dbname="parser_test"))

    config = ParserStoreConfig.from_environment()

    assert config.dsn == make_conninfo(dbname="parser_test")


def test_the_dsn_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, parser_database: str
) -> None:
    monkeypatch.setenv("FIN_PARSER_DSN", parser_database)

    store = PostgresParserJobStore(ParserStoreConfig())

    assert store.connection_dsn == parser_database


def test_a_store_without_a_dsn_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIN_PARSER_DSN", raising=False)

    with pytest.raises(ParserPersistenceError) as refusal:
        PostgresParserJobStore(ParserStoreConfig())

    assert refusal.value.code == "persistence_unavailable"


def test_a_blank_dsn_is_refused() -> None:
    with pytest.raises(ParserPersistenceError) as refusal:
        PostgresParserJobStore(ParserStoreConfig(dsn="   "))

    assert refusal.value.code == "persistence_unavailable"


def test_an_out_of_range_setting_is_refused(parser_database: str) -> None:
    with pytest.raises(ParserInvalidRequestError) as refusal:
        PostgresParserJobStore(
            ParserStoreConfig(dsn=parser_database, max_open_jobs=0)
        )

    assert refusal.value.code == "invalid_request"


def test_an_unknown_setting_is_refused(parser_database: str) -> None:
    with pytest.raises(ParserInvalidRequestError) as refusal:
        PostgresParserJobStore(parser_database, max_open_job=1)

    assert refusal.value.code == "invalid_request"


def test_a_settings_object_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ParserInvalidRequestError) as refusal:
        PostgresParserJobStore(42)

    assert refusal.value.code == "invalid_request"


def test_the_settings_object_is_frozen(parser_database: str) -> None:
    config = ParserStoreConfig(dsn=parser_database)

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.dsn = "elsewhere"


def test_the_legacy_call_shape_still_builds_the_store(parser_database: str) -> None:
    # WHY: the settings used to be constructor arguments, and callers in
    # other modules still hand them over that way.
    store = PostgresParserJobStore(parser_database, max_open_jobs=10_000)

    assert store.connection_dsn == parser_database
    assert store.max_open_jobs == 10_000


def test_the_store_constructor_takes_at_most_four_parameters() -> None:
    parameters = inspect.signature(PostgresParserJobStore.__init__).parameters

    assert len(parameters) <= 4


# ----- acceptance, replay and conflict -----


def test_a_submitted_job_is_durable_and_readable(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)

    record = store.get_job(submission.job_id)

    assert record is not None
    assert record.job_id == submission.job_id
    assert record.status == "queued"
    assert record.input_sha256 == DIGEST_A
    assert record.idempotency_key == job_key


def test_the_same_key_and_the_same_input_replay_the_same_job(
    store: PostgresParserJobStore, job_key: str
) -> None:
    first = _submit(store, job_key)
    second = _submit(store, job_key)

    assert first.replayed is False
    assert second.replayed is True
    assert second.job_id == first.job_id


def test_the_same_key_with_a_different_input_conflicts(
    store: PostgresParserJobStore, job_key: str
) -> None:
    _submit(store, job_key, DIGEST_A)

    with pytest.raises(ParserJobConflictError):
        _submit(store, job_key, DIGEST_B)


def test_an_empty_identity_is_refused_before_anything_is_written(
    store: PostgresParserJobStore,
) -> None:
    with pytest.raises(ParserInvalidRequestError):
        _submit(store, "   ")

    assert _rows_for_key(store.connection_dsn, "   ") == 0


def test_concurrent_identical_submissions_in_separate_processes_converge_on_one_job(
    tmp_path: Path, parser_database: str, job_key: str
) -> None:
    env = {
        "FIN_PARSER_DSN": parser_database,
        "FIN_KEY": job_key,
        "FIN_SHA": DIGEST_A,
    }

    payloads = _run_children(tmp_path, [_SUBMIT_BODY, _SUBMIT_BODY], env)

    assert payloads[0]["job_id"] == payloads[1]["job_id"]
    assert payloads[0]["pid"] != payloads[1]["pid"]
    assert payloads[0]["pid"] != os.getpid()
    assert _rows_for_key(parser_database, job_key) == 1


# ----- the restricted role -----


def test_the_store_works_through_a_role_that_may_touch_only_the_parser_schema(
    restricted_dsn: str, restricted_role: str, parser_dsn: str, job_key: str
) -> None:
    store = PostgresParserJobStore(restricted_dsn)

    submission = _submit(store, job_key)
    record = store.get_job(submission.job_id)

    assert record is not None
    assert record.job_id == submission.job_id
    with psycopg.connect(parser_dsn) as conn:
        rights = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, "
            "has_database_privilege(%s, current_database(), 'CREATE') "
            "FROM pg_roles WHERE rolname = %s",
            (restricted_role, restricted_role),
        ).fetchone()
    assert rights == (False, False, False, False)


def test_the_store_constructs_against_a_role_that_may_not_run_ddl(
    parser_dsn: str, restricted_dsn: str, restricted_role: str, job_key: str
) -> None:
    # The tables already exist; the role holds rights on them but may not
    # create objects, so the store must not insist on running its DDL.
    with psycopg.connect(parser_dsn) as conn:
        conn.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA fin_parser FROM {role}").format(
                role=sql.Identifier(restricted_role)
            )
        )
        conn.commit()
    try:
        store = PostgresParserJobStore(restricted_dsn)
        submission = _submit(store, job_key)
        assert store.get_job(submission.job_id) is not None
    finally:
        with psycopg.connect(parser_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT CREATE ON SCHEMA fin_parser TO {role}").format(
                    role=sql.Identifier(restricted_role)
                )
            )
            conn.commit()


def test_a_postgres_failure_surfaces_as_persistence_error_and_accepts_nothing(
    parser_dsn: str, restricted_dsn: str, restricted_role: str, job_key: str
) -> None:
    store = PostgresParserJobStore(restricted_dsn)
    # The role keeps its read rights but loses the right to write, so the
    # failure lands on the acceptance write itself.
    with psycopg.connect(parser_dsn) as conn:
        conn.execute(
            sql.SQL("REVOKE INSERT ON fin_parser.job FROM {role}").format(
                role=sql.Identifier(restricted_role)
            )
        )
        conn.commit()
    try:
        with pytest.raises(ParserPersistenceError):
            _submit(store, job_key)
    finally:
        with psycopg.connect(parser_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT INSERT ON fin_parser.job TO {role}").format(
                    role=sql.Identifier(restricted_role)
                )
            )
            conn.commit()

    assert _rows_for_key(parser_dsn, job_key) == 0


def test_a_store_that_cannot_reach_postgres_refuses_to_start(parser_dsn: str) -> None:
    # The port carries no PostgreSQL server, so the connection fails.
    info = conninfo_to_dict(parser_dsn)
    info["port"] = "1"

    with pytest.raises(ParserPersistenceError):
        PostgresParserJobStore(make_conninfo(**info))


# ----- fencing and ownership -----


def test_competing_claims_yield_different_fencing_tokens_and_one_effective_attempt(
    tmp_path: Path, parser_database: str, job_key: str
) -> None:
    store = PostgresParserJobStore(parser_database)
    submission = _submit(store, job_key)
    env = {"FIN_PARSER_DSN": parser_database, "FIN_JOB_ID": submission.job_id}

    payloads = _run_children(tmp_path, [_CLAIM_BODY, _CLAIM_BODY], env)

    tokens = sorted(int(payload["token"]) for payload in payloads if payload["token"])
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]
    with psycopg.connect(parser_database) as conn:
        effective = conn.execute(
            "SELECT MAX(fencing_token) FROM fin_parser.attempt WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()[0]
    assert effective == tokens[1]


def test_commit_result_with_a_stale_token_raises_and_writes_nothing(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    first = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    second = store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)
    assert first is not None and second is not None
    assert first.fencing_token < second.fencing_token

    with pytest.raises(ParserStaleFenceError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=first.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )

    with psycopg.connect(store.connection_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_parser.result WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()[0] == 0


def test_commit_result_without_any_attempt_raises_and_writes_nothing(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)

    with pytest.raises(ParserStaleFenceError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=1,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )

    with psycopg.connect(store.connection_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_parser.result WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()[0] == 0


def test_commit_result_with_the_effective_token_stores_the_result(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    record = store.commit_result(
        job_id=submission.job_id,
        fencing_token=attempt.fencing_token,
        envelope={"schema_version": "1", "units": []},
        result_ref="derived://job/out.json",
    )

    assert record.job_id == submission.job_id
    assert record.fencing_token == attempt.fencing_token
    assert record.envelope["schema_version"] == "1"
    assert store.read_result(submission.job_id) is not None
    assert store.get_job(submission.job_id).status == "succeeded"


def test_renew_attempt_succeeds_only_for_the_current_effective_token(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    first = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    second = store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)
    assert first is not None and second is not None

    assert store.renew_attempt(
        job_id=submission.job_id, fencing_token=second.fencing_token, lease_seconds=60
    )
    assert not store.renew_attempt(
        job_id=submission.job_id, fencing_token=first.fencing_token, lease_seconds=60
    )


def test_the_store_names_the_attempts_that_may_still_act(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """The store owns the rule that decides which attempt may still act.

    The effective attempt of a job is the one with its highest token, and
    it is live while its lease has not run out.
    """
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(
        job_id=submission.job_id, worker_id="w1", lease_seconds=60
    )
    assert attempt is not None
    token = (submission.job_id, attempt.fencing_token)

    assert token in store.live_attempt_tokens(now=datetime.now(timezone.utc))
    # a lease that ran out frees the attempt's output: the next claim
    # carries a higher token
    assert token not in store.live_attempt_tokens(
        now=datetime.now(timezone.utc) + timedelta(seconds=120)
    )


def test_the_store_names_the_output_a_committed_result_claims(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """The committed result is the durable claim the collection must honour."""
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(
        job_id=submission.job_id, worker_id="w1", lease_seconds=60
    )
    assert attempt is not None
    store.commit_result(
        job_id=submission.job_id,
        fencing_token=attempt.fencing_token,
        envelope={"schema_version": "1", "units": []},
        result_ref="derived://job/out.json",
    )

    assert "derived://job/out.json" in store.committed_result_refs()
    assert (submission.job_id, attempt.fencing_token) in store.committed_result_tokens()


def test_a_cancelled_job_refuses_a_commit_and_writes_nothing(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    cancelled = store.cancel_job(submission.job_id)
    assert cancelled.status == "cancel_requested"

    with pytest.raises(ParserJobCancelledError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=attempt.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )
    with psycopg.connect(store.connection_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_parser.result WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()[0] == 0


def test_cancelling_an_unknown_job_is_typed(store: PostgresParserJobStore) -> None:
    with pytest.raises(ParserJobNotFoundError):
        store.cancel_job("11111111-1111-1111-1111-111111111111")


# ----- deletion and replay protection -----


def test_a_deleted_job_leaves_a_tombstone_that_blocks_a_replay(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)

    store.delete_job(submission.job_id)

    assert store.get_job(submission.job_id) is None
    with pytest.raises(ParserJobTombstonedError):
        _submit(store, job_key)
    with psycopg.connect(store.connection_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_parser.tombstone WHERE idempotency_key = %s",
            (job_key,),
        ).fetchone()[0] == 1


def test_a_late_worker_action_after_a_delete_is_refused(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    store.delete_job(submission.job_id)

    with pytest.raises(ParserJobTombstonedError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=attempt.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )
    with pytest.raises(ParserJobTombstonedError):
        store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)


def test_a_repeated_delete_is_idempotent_and_leaves_one_tombstone(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """A second delete has nothing to do and writes no second tombstone."""
    submission = _submit(store, job_key)

    store.delete_job(submission.job_id)
    store.delete_job(submission.job_id)

    assert store.get_job(submission.job_id) is None
    with psycopg.connect(store.connection_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_parser.tombstone WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()[0] == 1
    with pytest.raises(ParserJobTombstonedError):
        _submit(store, job_key)


def test_deleting_an_unknown_job_is_typed(store: PostgresParserJobStore) -> None:
    with pytest.raises(ParserJobNotFoundError):
        store.delete_job("11111111-1111-1111-1111-111111111111")


# ----- capacity -----


def test_the_open_job_limit_returns_a_typed_capacity_outcome(
    parser_database: str, job_key: str
) -> None:
    store = PostgresParserJobStore(
        ParserStoreConfig(dsn=parser_database, max_open_jobs=1)
    )

    # The limit counts every open job in the shared schema, so a case
    # accepts until the store refuses. The refusal is the evidence.
    refused_key = ""
    for index in range(64):
        candidate = f"{job_key}-{index}"
        try:
            _submit(store, candidate)
        except ParserJobCapacityError:
            refused_key = candidate
            break
    assert refused_key, "the open-job limit never refused the work"

    assert _rows_for_key(parser_database, refused_key) == 0


def test_a_replay_is_not_new_work_and_survives_the_capacity_limit(
    parser_database: str, job_key: str
) -> None:
    store = PostgresParserJobStore(
        ParserStoreConfig(dsn=parser_database, max_open_jobs=1000)
    )
    first = _submit(store, job_key)

    replay = _submit(store, job_key)

    assert replay.job_id == first.job_id
    assert replay.replayed is True


# ----- durability across a restart -----


def test_a_fresh_process_reads_the_durable_job_status(
    tmp_path: Path, parser_database: str, job_key: str
) -> None:
    store = PostgresParserJobStore(parser_database)
    submission = _submit(store, job_key)
    env = {"FIN_PARSER_DSN": parser_database, "FIN_JOB_ID": submission.job_id}

    payloads = _run_children(tmp_path, [_READ_BODY], env)

    assert payloads[0]["status"] == "queued"
    assert payloads[0]["job_id"] == submission.job_id
    assert payloads[0]["pid"] != os.getpid()


def test_a_cancelled_job_stays_cancelled_in_a_fresh_process(
    tmp_path: Path, parser_database: str, job_key: str
) -> None:
    store = PostgresParserJobStore(parser_database)
    submission = _submit(store, job_key)
    store.cancel_job(submission.job_id)
    env = {"FIN_PARSER_DSN": parser_database, "FIN_JOB_ID": submission.job_id}

    payloads = _run_children(tmp_path, [_READ_BODY], env)

    assert payloads[0]["status"] == "cancelled"


# ----- the status vocabulary -----


def test_the_job_status_constraint_carries_the_closed_vocabulary(
    parser_database: str,
) -> None:
    with psycopg.connect(parser_database) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            (JOB_STATUS_CONSTRAINT_NAME,),
        ).fetchone()
    assert row is not None, "the job table must carry the status constraint"
    assert set(re.findall(r"'([^']*)'", str(row[0]))) == set(JOB_STATUSES)


def test_the_frozen_ddl_and_the_runtime_vocabulary_agree() -> None:
    # A fresh database takes the frozen DDL, an existing one is aligned
    # with the runtime vocabulary: both must allow the same statuses.
    ddl = dict(_TABLE_DDL)["job"]

    assert set(re.findall(r"'([^']*)'", ddl)) == set(JOB_STATUSES)


def test_a_claimed_job_is_running_and_a_finished_one_is_succeeded(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    running = store.get_job(submission.job_id)
    assert running is not None
    assert running.status == "running"

    store.commit_result(
        job_id=submission.job_id,
        fencing_token=attempt.fencing_token,
        envelope={"schema_version": "1"},
        result_ref="derived://job/out.json",
    )

    finished = store.get_job(submission.job_id)
    assert finished is not None
    assert finished.status == "succeeded"


def test_a_partial_result_is_a_terminal_state(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    record = store.commit_result(
        job_id=submission.job_id,
        fencing_token=attempt.fencing_token,
        envelope={
            "schema_version": "1",
            "coverage": {"status": "partial", "reason": "a scanned page was not read"},
        },
        result_ref="derived://job/out.json",
    )

    assert record.result_ref == "derived://job/out.json"
    job = store.get_job(submission.job_id)
    assert job is not None
    assert job.status == "partial"
    assert store.read_result(submission.job_id) is not None


# ----- the bounded queue -----


def test_the_pending_queue_cap_refuses_new_work_with_a_typed_outcome(
    parser_database: str, job_key: str
) -> None:
    pending = _pending_count(parser_database)
    store = PostgresParserJobStore(
        ParserStoreConfig(
            dsn=parser_database,
            max_pending_jobs=pending,
            queue_wait_seconds=NO_EXPIRY_SECONDS,
        )
    )

    with pytest.raises(ParserQueueFullError) as refusal:
        _submit(store, job_key)

    assert refusal.value.code == "parser_queue_full"
    assert refusal.value.details["max_pending_jobs"] == pending
    assert refusal.value.details["pending_count"] >= pending
    assert _rows_for_key(parser_database, job_key) == 0


def test_a_submission_below_the_queue_cap_is_accepted(
    parser_database: str, job_key: str
) -> None:
    pending = _pending_count(parser_database)
    store = PostgresParserJobStore(
        ParserStoreConfig(
            dsn=parser_database,
            max_pending_jobs=pending + 5,
            queue_wait_seconds=NO_EXPIRY_SECONDS,
        )
    )

    submission = _submit(store, job_key)

    assert submission.status == "queued"
    assert store.get_job(submission.job_id) is not None


def test_a_replay_is_not_new_work_and_survives_the_queue_cap(
    parser_database: str, job_key: str
) -> None:
    accepting = PostgresParserJobStore(
        ParserStoreConfig(
            dsn=parser_database,
            max_pending_jobs=1000,
            queue_wait_seconds=NO_EXPIRY_SECONDS,
        )
    )
    first = _submit(accepting, job_key)

    refusing = PostgresParserJobStore(
        ParserStoreConfig(
            dsn=parser_database,
            max_pending_jobs=0,
            queue_wait_seconds=NO_EXPIRY_SECONDS,
        )
    )
    replay = _submit(refusing, job_key)

    assert replay.job_id == first.job_id
    assert replay.replayed is True


def test_the_queue_cap_and_the_deadlines_have_their_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIN_PARSER_MAX_PENDING_JOBS", raising=False)
    monkeypatch.delenv("FIN_PARSER_QUEUE_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("FIN_PARSER_EXECUTION_SECONDS", raising=False)

    resolved = resolve_parser_limits()

    assert resolved.max_pending_jobs == 20
    assert resolved.queue_wait_seconds == 600.0
    assert resolved.execution_seconds == 900.0


def test_the_queue_cap_and_the_deadlines_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_PARSER_MAX_PENDING_JOBS", "7")
    monkeypatch.setenv("FIN_PARSER_QUEUE_WAIT_SECONDS", "12.5")
    monkeypatch.setenv("FIN_PARSER_EXECUTION_SECONDS", "90")

    resolved = resolve_parser_limits()

    assert resolved.max_pending_jobs == 7
    assert resolved.queue_wait_seconds == 12.5
    assert resolved.execution_seconds == 90.0


def test_the_pending_cap_is_in_force_for_the_parser_cases(
    store: PostgresParserJobStore,
) -> None:
    """The parser cases run with the raised cap, not the shipped default.

    The cap is set for the case and restored afterwards, so the rest of
    the session is unaffected.
    """
    assert store.max_pending_jobs == 1000


# ----- the deadlines -----


def test_a_job_that_waited_longer_than_the_deadline_is_not_claimable(
    parser_database: str, job_key: str
) -> None:
    clock = _Clock(MOMENT)
    store = PostgresParserJobStore(
        ParserStoreConfig(dsn=parser_database, queue_wait_seconds=600, clock=clock)
    )
    submission = _submit(store, job_key)

    clock.advance(601)

    with pytest.raises(ParserQueueWaitTimeoutError) as refusal:
        store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)

    assert refusal.value.code == "parser_queue_wait_timeout"
    job = store.get_job(submission.job_id)
    assert job is not None
    assert job.status == "timed_out"
    timed_out = _event_for(
        parser_database, submission.job_id, "parser.job.timed_out.v1"
    )
    assert timed_out["error_code"] == "parser_queue_wait_timeout"


def test_a_result_committed_after_the_execution_deadline_is_refused(
    parser_database: str, job_key: str
) -> None:
    clock = _Clock(MOMENT)
    store = PostgresParserJobStore(
        ParserStoreConfig(dsn=parser_database, execution_seconds=900, clock=clock)
    )
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    clock.advance(901)

    with pytest.raises(ParserExecutionTimeoutError) as refusal:
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=attempt.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )

    assert refusal.value.code == "parser_execution_timeout"
    job = store.get_job(submission.job_id)
    assert job is not None
    assert job.status == "timed_out"
    assert store.read_result(submission.job_id) is None
    timed_out = _event_for(
        parser_database, submission.job_id, "parser.job.timed_out.v1"
    )
    assert timed_out["error_code"] == "parser_execution_timeout"
    # a late result may not revive the job that gave up
    with pytest.raises(ParserExecutionTimeoutError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=attempt.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )
    assert store.read_result(submission.job_id) is None


# ----- the cancellation request -----


def test_cancelling_a_running_job_records_the_request_and_fences_the_attempt(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    record = store.cancel_job(submission.job_id)

    assert record.status == "cancel_requested"
    with pytest.raises(ParserJobCancelledError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=attempt.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )
    assert store.read_result(submission.job_id) is None
    with pytest.raises(ParserJobCancelledError):
        store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)
    assert (
        store.renew_attempt(
            job_id=submission.job_id, fencing_token=attempt.fencing_token, lease_seconds=60
        )
        is False
    )


def test_a_cancel_request_becomes_cancelled_once_the_attempt_is_gone(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    assert store.cancel_job(submission.job_id).status == "cancel_requested"

    with psycopg.connect(store.connection_dsn) as conn:
        row = conn.execute(
            "SELECT state, (lease_expires_at <= now()) FROM fin_parser.attempt "
            "WHERE job_id = %s",
            (submission.job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "expired"
    assert row[1] is True

    assert store.cancel_job(submission.job_id).status == "cancelled"


def test_a_cancel_requested_job_is_visible_to_an_unscoped_worker_and_is_not_claimable(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    assert store.cancel_job(submission.job_id).status == "cancel_requested"

    open_jobs = store.list_open_jobs(limit=OPEN_LIST_LIMIT)
    visible = [job for job in open_jobs if job.job_id == submission.job_id]

    # A worker that polls the open list must see the recorded
    # cancellation, or the job never reaches its terminal state.
    assert len(visible) == 1
    assert visible[0].status == "cancel_requested"
    # The cancellation is durable, so no new attempt may take the job.
    with pytest.raises(ParserJobCancelledError) as refusal:
        store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)
    assert refusal.value.code == "job_cancelled"
    job = store.get_job(submission.job_id)
    assert job is not None
    assert job.status == "cancel_requested"


# ----- the completion event -----


def test_an_accepted_job_carries_its_acceptance_event(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)

    accepted = _event_for(
        store.connection_dsn, submission.job_id, "parser.job.accepted.v1"
    )

    assert accepted["job_id"] == submission.job_id
    assert accepted["status"] == "queued"


def test_the_completion_event_commits_with_the_result(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    attempt = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None

    store.commit_result(
        job_id=submission.job_id,
        fencing_token=attempt.fencing_token,
        envelope={"schema_version": "1"},
        result_ref="derived://job/out.json",
    )

    completed = _event_for(
        store.connection_dsn, submission.job_id, "parser.job.completed.v1"
    )
    assert completed["status"] == "succeeded"
    assert completed["result_ref"] == "derived://job/out.json"
    assert completed["job_version"] == attempt.fencing_token


def test_a_refused_commit_writes_no_completion_event(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)
    first = store.claim_attempt(job_id=submission.job_id, worker_id="w1", lease_seconds=60)
    second = store.claim_attempt(job_id=submission.job_id, worker_id="w2", lease_seconds=60)
    assert first is not None and second is not None

    with pytest.raises(ParserStaleFenceError):
        store.commit_result(
            job_id=submission.job_id,
            fencing_token=first.fencing_token,
            envelope={"schema_version": "1"},
            result_ref="derived://job/out.json",
        )

    events = [
        row["event"] for row in _outbox_events(store.connection_dsn, submission.job_id)
    ]
    assert "parser.job.accepted.v1" in events
    assert "parser.job.completed.v1" not in events


# ----- the payload guards -----
#
# The store serialises every body it writes, so the malformed branches are
# the defensive ones. They still have to raise: a payload that cannot be
# read must never reach a durable row as an empty field.


def test_a_malformed_notification_body_is_refused() -> None:
    with pytest.raises(ParserInvalidRequestError):
        _payload_text("{not json", "job_id")
    with pytest.raises(ParserInvalidRequestError):
        _payload_int("{not json", "job_version")
    with pytest.raises(ParserInvalidRequestError):
        _payload_value("{not json", "job_id")


def test_a_notification_body_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ParserInvalidRequestError):
        _payload_text("[]", "job_id")


def test_a_notification_field_of_the_wrong_type_is_refused() -> None:
    body = json.dumps({"status": 5, "job_version": "1"})

    with pytest.raises(ParserInvalidRequestError):
        _payload_text(body, "status")
    with pytest.raises(ParserInvalidRequestError):
        _payload_int(body, "job_version")


def test_a_readable_notification_body_keeps_its_values() -> None:
    body = json.dumps({"job_id": "job-1", "job_version": 3, "status": "queued"})

    assert _payload_text(body, "job_id") == "job-1"
    assert _payload_int(body, "job_version") == 3
    assert _payload_text(body, "status") == "queued"
    assert _payload_optional_text(body, "result_ref") is None


def test_a_notification_with_a_wrongly_typed_field_is_refused_and_writes_no_row(
    store: PostgresParserJobStore,
) -> None:
    with pytest.raises(ParserInvalidRequestError):
        store.enqueue_notification(
            topic="parser.job.completed.v1",
            payload={"job_id": "job-typed", "status": 5},
        )

    assert _outbox_events(store.connection_dsn, "job-typed") == []


# ----- the committed result guard -----


def test_an_unreadable_committed_payload_is_refused() -> None:
    with pytest.raises(ParserEnvelopeError):
        _committed_document("{not json")
    with pytest.raises(ParserEnvelopeError):
        _committed_document("[]")


def test_the_terminal_status_of_a_readable_payload_is_unchanged() -> None:
    assert _terminal_outcome({"coverage": {"status": "partial"}}) == "partial"
    assert _terminal_outcome({"coverage": {"status": "complete"}}) == "succeeded"
    assert _terminal_outcome({"schema_version": "1"}) == "succeeded"


# ----- the outbox event -----


def test_the_outbox_row_is_written_from_one_event_object(
    store: PostgresParserJobStore, job_key: str
) -> None:
    submission = _submit(store, job_key)

    with psycopg.connect(store.connection_dsn) as conn:
        notification_id = write_outbox_row(
            conn,
            OutboxEvent(
                event="parser.job.accepted.v1",
                job_id=submission.job_id,
                job_version=0,
                status="queued",
            ),
        )
        conn.commit()

    with psycopg.connect(store.connection_dsn) as conn:
        payload = conn.execute(
            "SELECT payload FROM fin_parser.outbox WHERE notification_id = %s",
            (notification_id,),
        ).fetchone()[0]
    assert payload["job_id"] == submission.job_id
    assert payload["job_version"] == 0
    assert payload["status"] == "queued"
    assert payload["result_ref"] is None
    assert payload["error_code"] is None


def test_the_changed_functions_take_at_most_four_parameters() -> None:
    for function in (
        write_outbox_row,
        PostgresParserJobStore._insert_job,
        PostgresParserJobStore._store_result,
    ):
        names = [
            name for name in inspect.signature(function).parameters if name != "self"
        ]
        assert len(names) <= 4, function
