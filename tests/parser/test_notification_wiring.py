"""The notification lane of the product path, end to end (P2 T5, SC2).

The durable outbox is the fact source and Redis is a bounded wakeup channel.
These cases prove that the PRODUCT PATH does both halves of that: the worker
process drains the durable outbox to the real Redis stream, and a consumer
records the delivery in the durable inbox.  Nothing is mocked: the wakeup
channel is the real Redis, "Redis is down" is a real client pointed at a
refused port, and the job authority is the real PostgreSQL store.

Every case runs in a PRIVATE database, so a concurrent parser test session
can never empty the rows these cases assert on, and every case owns a
private Redis stream, so one case can never observe another case's wakeups.

A Redis outage is the point of the last two cases: the job still reaches its
terminal state, the durable result is still readable, and the notification
that never reached the channel is found again by the reconciliation.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.job_store import PostgresParserJobStore
from fin.parser.notifications import (
    NotificationService,
    PostgresNotificationReader,
    ReceivedNotification,
)
from fin.parser.redis_client import RedisNotificationTransport, RedisTransportConfig

REDIS_URL_ENV = "FIN_REDIS_URL"

# The refused port stands for "the wakeup channel is unreachable".
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"

# The stable, versioned lifecycle events the durable outbox carries.
EVENT_ACCEPTED = "parser.job.accepted.v1"
EVENT_COMPLETED = "parser.job.completed.v1"

_PRODUCER_SOURCE = '''
from fin.parser.worker import AttemptResult


def produce(attempt):
    return AttemptResult(
        envelope={"source_sha256": "0" * 64},
        result_ref="ref://wiring-" + str(attempt.fencing_token),
    )
'''


# ----- fixtures -----


@pytest.fixture(scope="session")
def redis_url() -> str:
    value = os.environ.get(REDIS_URL_ENV, "").strip()
    if not value:
        pytest.skip(
            f"{REDIS_URL_ENV} is unset; the notification lane needs the real Redis"
        )
    return value


@pytest.fixture(scope="session")
def isolated_dsn(parser_dsn: str) -> Iterator[str]:
    """A private database, so a concurrent session cannot empty our rows."""
    info = conninfo_to_dict(parser_dsn)
    name = f"fin_parser_wiring_{uuid.uuid4().hex[:10]}_test"
    assert name.endswith("_test"), "the isolated database must be a _test database"
    admin = make_conninfo(**{**info, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(make_conninfo(**{**info, "dbname": name})) as conn:
            assert conn.execute("SELECT current_database()").fetchone()[0] == name
        yield make_conninfo(**{**info, "dbname": name})
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def clean_isolated_schema(isolated_dsn: str) -> Iterator[str]:
    """Empty parser tables around each test, so cases cannot leak state."""
    _truncate(isolated_dsn)
    yield isolated_dsn
    _truncate(isolated_dsn)


@pytest.fixture
def stream_key() -> str:
    """A private stream key, so no case observes another case's wakeups."""
    return f"fin_parser:wiring:{uuid.uuid4().hex}"


@dataclass(frozen=True)
class _Wiring:
    """Everything a case needs to talk to the private parser lane."""

    dsn: str
    store: PostgresParserJobStore
    reader: PostgresNotificationReader
    transport: RedisNotificationTransport
    stream_key: str
    redis_url: str
    tmp_path: Path

    def service(self) -> NotificationService:
        """The consumer path over the private stream."""
        return NotificationService(
            store=self.store,
            transport=self.transport,
            payload_reader=self.reader,
        )

    def worker_env(self, *, redis_endpoint: str) -> dict[str, str]:
        """The environment of a worker process on this stream."""
        return {
            **os.environ,
            "FIN_PARSER_DSN": self.dsn,
            "FIN_PARSER_WORKER_ID": "wiring-worker",
            "FIN_PARSER_PRODUCER": "controlled_producer:produce",
            "FIN_PARSER_POLL_SECONDS": "0.2",
            "FIN_PARSER_LEASE_SECONDS": "30",
            "FIN_REDIS_URL": redis_endpoint,
            "FIN_PARSER_REDIS_STREAM": self.stream_key,
            "FIN_PARSER_REDIS_GROUP": "fin_parser_wiring_group",
            "FIN_PARSER_REDIS_CONSUMER": "wiring-consumer",
            "PYTHONPATH": str(self.tmp_path)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }

    def submit(self) -> str:
        """Accept one job through the store's public path."""
        return _submit_job(self.store)


@pytest.fixture
def wiring(
    clean_isolated_schema: str,
    redis_url: str,
    stream_key: str,
    tmp_path: Path,
) -> Iterator[_Wiring]:
    """The private lane a case works on."""
    (tmp_path / "controlled_producer.py").write_text(_PRODUCER_SOURCE, encoding="utf-8")
    transport = RedisNotificationTransport(
        RedisTransportConfig(
            url=redis_url,
            stream_key=stream_key,
            group="fin_parser_wiring_group",
            consumer="wiring-consumer",
        )
    )
    try:
        yield _Wiring(
            dsn=clean_isolated_schema,
            store=PostgresParserJobStore(clean_isolated_schema),
            reader=PostgresNotificationReader(clean_isolated_schema),
            transport=transport,
            stream_key=stream_key,
            redis_url=redis_url,
            tmp_path=tmp_path,
        )
    finally:
        with contextlib.suppress(redis.RedisError):
            redis.Redis.from_url(redis_url, socket_timeout=2).delete(stream_key)


# ----- helpers -----


def _truncate(dsn: str) -> None:
    """Empty every table the parser schema owns."""
    with psycopg.connect(dsn) as conn:
        names = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'fin_parser'"
        ).fetchall()
        if names:
            conn.execute(
                sql.SQL("TRUNCATE {}").format(
                    sql.SQL(", ").join(
                        sql.Identifier("fin_parser", str(row[0])) for row in names
                    )
                )
            )
            conn.commit()


def _job_status(dsn: str, job_id: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT status FROM fin_parser.job WHERE job_id = %s", (job_id,)
        ).fetchone()
        conn.commit()
    return None if row is None else str(row[0])


def _outbox_events(dsn: str, job_id: str) -> list[tuple[str, str]]:
    """The durable lifecycle events of one job, with the status they carry."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT topic, payload ->> 'status' FROM fin_parser.outbox "
            "WHERE payload ->> 'job_id' = %s ORDER BY created_at, notification_id",
            (job_id,),
        ).fetchall()
        conn.commit()
    return [(str(row[0]), str(row[1])) for row in rows]


def _inbox_ids(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT notification_id FROM fin_parser.inbox ORDER BY received_at"
        ).fetchall()
        conn.commit()
    return [str(row[0]) for row in rows]


def _wait_until(
    predicate: Callable[[], bool], *, reason: str, timeout: float = 60.0
) -> None:
    """Wait for a durable state to appear.

    This is a TTL wait on an OUTCOME, not a race sequencer: nothing else is
    being ordered here, and the case fails loudly when the state never comes.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.05)
    raise AssertionError(reason)


class _Collector:
    """Consumes the stream and keeps what arrived.

    WHY: a stream read returns the entries this consumer has not seen, so a
    case that waits for two events has to keep what the first read brought.
    """

    def __init__(self, service: NotificationService) -> None:
        self._service = service
        self.received: list[ReceivedNotification] = []

    def drain(self) -> int:
        """Read what is new and add it to what arrived before."""
        outcome = self._service.consume(count=10)
        self.received.extend(outcome.received)
        return len(self.received)


def _start_worker(
    job_id: str, env: dict[str, str]
) -> subprocess.Popen[str]:
    """Start the real worker process on one job."""
    return subprocess.Popen(
        [sys.executable, "-m", "fin.parser.worker_main", "run", "--job-id", job_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop_worker(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop the worker process and return what it wrote."""
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=120)
    if proc.poll() is None:  # pragma: no cover - the signal always lands
        proc.kill()
        proc.wait(timeout=30)
    return stdout, stderr


def _submit_job(store: PostgresParserJobStore) -> str:
    submission = store.submit_job(
        idempotency_key=f"wiring-{uuid.uuid4()}",
        input_sha256="0" * 64,
        source_ref=None,
        declared_format=None,
    )
    return submission.job_id


def _run_worker_until(
    wiring: _Wiring,
    job_id: str,
    env: dict[str, str],
    reason: str,
) -> tuple[str, str]:
    """Run the worker until the job is terminal, then stop it."""
    proc = _start_worker(job_id, env)
    try:
        _wait_until(
            lambda: _job_status(wiring.dsn, job_id) == "succeeded",
            reason=reason,
        )
        stdout, stderr = _stop_worker(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - the stop always lands
            proc.kill()
            proc.wait(timeout=30)
    return stdout, stderr


# ----- the contract cases -----


def test_a_terminal_job_produces_an_outbox_row_and_the_publisher_delivers_it(
    wiring: _Wiring,
) -> None:
    """The worker process drains the durable outbox to the Redis stream."""
    job_id = wiring.submit()
    collector = _Collector(wiring.service())

    proc = _start_worker(job_id, wiring.worker_env(redis_endpoint=wiring.redis_url))
    try:
        _wait_until(
            lambda: _job_status(wiring.dsn, job_id) == "succeeded",
            reason="the worker never committed the controlled result",
        )
        _wait_until(
            lambda: collector.drain() >= 2,
            reason="the publisher never delivered the durable events",
        )
        stdout, stderr = _stop_worker(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - the stop always lands
            proc.kill()
            proc.wait(timeout=30)

    assert proc.returncode == 0, stderr
    assert wiring.dsn not in stdout
    assert wiring.dsn not in stderr

    # The durable outbox carries the stable, versioned lifecycle events.
    assert _outbox_events(wiring.dsn, job_id) == [
        (EVENT_ACCEPTED, "queued"),
        (EVENT_COMPLETED, "succeeded"),
    ]

    # The consumer received both, with the job they belong to.
    delivered = {str(item.topic): item.payload for item in collector.received}
    assert set(delivered) == {EVENT_ACCEPTED, EVENT_COMPLETED}
    assert delivered[EVENT_ACCEPTED]["job_id"] == job_id
    assert delivered[EVENT_ACCEPTED]["status"] == "queued"
    assert delivered[EVENT_COMPLETED]["job_id"] == job_id
    assert delivered[EVENT_COMPLETED]["status"] == "succeeded"


def test_a_consumer_records_the_event_and_a_duplicate_delivery_changes_nothing(
    wiring: _Wiring,
) -> None:
    """The inbox makes a repeated delivery a no-op."""
    job_id = wiring.submit()
    collector = _Collector(wiring.service())

    proc = _start_worker(job_id, wiring.worker_env(redis_endpoint=wiring.redis_url))
    try:
        _wait_until(
            lambda: _job_status(wiring.dsn, job_id) == "succeeded",
            reason="the worker never committed the controlled result",
        )
        _wait_until(
            lambda: collector.drain() >= 2,
            reason="the publisher never delivered the durable events",
        )
        stdout, stderr = _stop_worker(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - the stop always lands
            proc.kill()
            proc.wait(timeout=30)

    assert proc.returncode == 0, stderr
    assert len(collector.received) == 2
    assert all(item.first_receipt for item in collector.received)

    # The inbox holds the delivered event ids.
    recorded = _inbox_ids(wiring.dsn)
    assert sorted(recorded) == sorted(item.notification_id for item in collector.received)

    # The very same notification is delivered a second time.
    source = wiring.reader.notification_by_id(recorded[0])
    assert source is not None
    wiring.transport.publish(
        notification_id=source.notification_id,
        topic=source.topic,
        payload=source.payload,
    )
    repeated = wiring.service().consume(count=10)
    assert [item.notification_id for item in repeated.received] == [recorded[0]]
    assert [item.first_receipt for item in repeated.received] == [False]
    # The inbox still holds exactly the same rows.
    assert sorted(_inbox_ids(wiring.dsn)) == sorted(recorded)


def test_a_notification_that_was_never_delivered_is_found_by_reconciliation(
    wiring: _Wiring,
) -> None:
    """A wakeup that never left the outbox is repaired by the reconciliation."""
    job_id = wiring.submit()

    # The first worker cannot reach the wakeup channel: the job still runs.
    stdout, stderr = _run_worker_until(
        wiring,
        job_id,
        wiring.worker_env(redis_endpoint=DEAD_REDIS_URL),
        "the worker never committed the controlled result",
    )
    # The outage is reported as the typed notification failure, and the job
    # is unaffected by it.
    assert "notification_failed" in stderr
    assert wiring.dsn not in stderr

    # The events are durable and outstanding: nothing reached the channel.
    assert _outbox_events(wiring.dsn, job_id) == [
        (EVENT_ACCEPTED, "queued"),
        (EVENT_COMPLETED, "succeeded"),
    ]
    assert len(wiring.store.unreconciled_notification_ids()) == 2
    assert wiring.service().consume(count=10).received == ()

    # The second worker starts with a healthy channel: its startup
    # reconciliation finds the missed wakeups and re-drives them.
    collector = _Collector(wiring.service())
    proc = _start_worker(job_id, wiring.worker_env(redis_endpoint=wiring.redis_url))
    try:
        _wait_until(
            lambda: collector.drain() >= 2,
            reason="the reconciliation never delivered the missed wakeups",
        )
        stdout, stderr = _stop_worker(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - the stop always lands
            proc.kill()
            proc.wait(timeout=30)

    assert proc.returncode == 0, stderr
    assert {str(item.topic) for item in collector.received} == {
        EVENT_ACCEPTED,
        EVENT_COMPLETED,
    }
    # The durable job state is the one the first worker left behind.
    assert _job_status(wiring.dsn, job_id) == "succeeded"
    assert wiring.store.unreconciled_notification_ids() == []
    assert len(_inbox_ids(wiring.dsn)) == 2


def test_with_redis_down_the_job_reaches_its_terminal_state_and_the_result_is_readable(
    wiring: _Wiring,
) -> None:
    """The wakeup channel is not the job authority."""
    job_id = wiring.submit()

    stdout, stderr = _run_worker_until(
        wiring,
        job_id,
        wiring.worker_env(redis_endpoint=DEAD_REDIS_URL),
        "the worker never committed the controlled result",
    )

    # The outage is reported as the typed notification failure, and the job
    # is unaffected by it.
    assert "notification_failed" in stderr
    assert wiring.dsn not in stderr
    # The job finished and its durable result is readable without Redis.
    assert _job_status(wiring.dsn, job_id) == "succeeded"
    result = wiring.store.read_result(job_id)
    assert result is not None
    assert result.result_ref.startswith("ref://wiring-")
    # The wakeups are still outstanding, and the reconciliation can find them.
    outstanding = wiring.store.unreconciled_notification_ids()
    assert len(outstanding) == 2
    assert all(
        wiring.reader.notification_by_id(notification_id) is not None
        for notification_id in outstanding
    )
