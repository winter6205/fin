"""Contract tests for the durable parser notification slice (P2 T5, SC2).

The parser-owned PostgreSQL outbox is the notification source of truth and
Redis is only a bounded wakeup channel, so these tests run against the real
PostgreSQL test database and the real Redis instance.  Nothing under test is
mocked: "Redis is down" is a real client pointed at a refused port, and "Redis
restarted" is a flush of a dedicated Redis database.

The store under test is the real ``PostgresParserJobStore`` and the schema
under test is the real ``fin_parser`` schema.  They live in a PRIVATE
DATABASE for this test session: the shared test database is never truncated,
and this session truncates only inside the private database it owns.  Every
test additionally starts from an empty parser schema, so one test can never
observe another test's rows.  Each test also owns a private Redis stream, so
one test can never observe another test's wakeups.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.errors import ParserNotificationError
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.notifications import (
    NotificationService,
    PostgresNotificationReader,
    ReceivedNotification,
)
from fin.parser.redis_client import (
    DEFAULT_STREAM_MAXLEN,
    RedisNotificationTransport,
    RedisTransportConfig,
)

REDIS_URL_ENV = "FIN_REDIS_URL"

# A dedicated Redis database so the restart simulation can never touch the
# database other tests use.
RESTART_REDIS_DB = 15


# ----- fixtures -----


@pytest.fixture(scope="session")
def redis_url() -> str:
    value = os.environ.get(REDIS_URL_ENV)
    if not value:
        pytest.skip(f"{REDIS_URL_ENV} is unset; the notification transport needs the real Redis")
    return value


@pytest.fixture(scope="session")
def isolated_dsn(parser_dsn: str) -> Iterator[str]:
    """A private database, so a whole-table truncate stays inside it.

    The shared test database is never truncated: every key a case writes
    carries a run prefix and the teardown removes exactly those rows.  A
    whole-table truncate is only acceptable inside a database the session
    owns, so this fixture creates one and drops it again.
    """
    info = conninfo_to_dict(parser_dsn)
    name = f"fin_parser_iso_{uuid.uuid4().hex[:10]}_test"
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
def store(clean_isolated_schema: str) -> PostgresParserJobStore:
    """The real parser store over the private database."""
    return PostgresParserJobStore(clean_isolated_schema)


@pytest.fixture
def reader(clean_isolated_schema: str) -> PostgresNotificationReader:
    """The reconciliation read over the parser-owned outbox."""
    return PostgresNotificationReader(clean_isolated_schema)


@pytest.fixture
def transport(redis_url: str) -> Iterator[RedisNotificationTransport]:
    """A transport on a private stream key, cleaned up after the test."""
    stream_key = f"fin_parser:test:{uuid.uuid4().hex}"
    client = RedisNotificationTransport(
        RedisTransportConfig(
            url=redis_url,
            stream_key=stream_key,
            group="fin_parser_test_group",
            consumer="test-consumer",
        )
    )
    yield client
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


def _dead_transport() -> RedisNotificationTransport:
    """A real client aimed at a refused port: the wakeup channel is unreachable."""
    return RedisNotificationTransport(
        RedisTransportConfig(url="redis://127.0.0.1:1/0", socket_timeout=0.5)
    )


def _database_url(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def _outbox_row(dsn: str, notification_id: str) -> Any:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT notification_id, topic, payload, created_at, published_at "
            "FROM fin_parser.outbox WHERE notification_id = %s",
            (notification_id,),
        ).fetchone()


def _inbox_ids(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT notification_id FROM fin_parser.inbox ORDER BY received_at"
        ).fetchall()
    return [str(row[0]) for row in rows]


# ----- contract tests -----


def test_published_notification_reaches_the_consumer_group_and_is_acked(
    clean_isolated_schema: str,
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
    transport: RedisNotificationTransport,
) -> None:
    """A published notification is consumed by the group and acknowledged."""
    service = NotificationService(
        store=store, transport=transport, payload_reader=reader
    )
    payload = {"job_id": str(uuid.uuid4()), "kind": "parser.job.accepted.v1"}

    gate = threading.Barrier(2)
    received: list[ReceivedNotification] = []

    def consume_after_publish() -> None:
        gate.wait(timeout=10)
        received.extend(service.consume(count=10).received)

    consumer = threading.Thread(
        target=consume_after_publish, name="parser-notification-consumer"
    )
    consumer.start()

    outcome = service.enqueue(topic="parser.job.accepted.v1", payload=payload)
    assert outcome.published is True
    assert outcome.error is None

    gate.wait(timeout=10)
    consumer.join(timeout=10)
    assert not consumer.is_alive()

    assert [item.notification_id for item in received] == [outcome.notification_id]
    assert received[0].first_receipt is True
    # the durable outbox row records the publication
    assert _outbox_row(clean_isolated_schema, outcome.notification_id)[4] is not None
    # the stream entry was acknowledged, so nothing stays pending for the group
    assert transport.pending_count() == 0


def test_duplicate_delivery_records_the_inbox_once(
    clean_isolated_schema: str,
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
    transport: RedisNotificationTransport,
) -> None:
    """The same notification delivered twice leaves exactly one inbox row."""
    service = NotificationService(
        store=store, transport=transport, payload_reader=reader
    )
    outcome = service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert outcome.published is True

    # the identical notification is delivered a second time
    transport.publish(
        notification_id=outcome.notification_id,
        topic="parser.job.accepted.v1",
        payload={"job_id": str(uuid.uuid4())},
    )

    consumed = service.consume(count=10)
    assert len(consumed.received) == 2
    assert [item.first_receipt for item in consumed.received] == [True, False]
    assert {item.notification_id for item in consumed.received} == {outcome.notification_id}
    # the inbox holds exactly one row for the notification
    assert _inbox_ids(clean_isolated_schema) == [outcome.notification_id]
    # a further receipt of the same id is refused
    assert store.record_notification_received(outcome.notification_id) is False


def test_missed_delivery_is_found_by_reconciliation_and_republished(
    clean_isolated_schema: str,
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
    transport: RedisNotificationTransport,
) -> None:
    """A notification whose delivery was lost is found and re-published."""
    lost_service = NotificationService(
        store=store, transport=_dead_transport(), payload_reader=reader
    )
    outcome = lost_service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert outcome.published is False
    assert outcome.error is not None
    assert outcome.error.code == "notification_failed"

    # the durable row survived the lost delivery and is still claimable
    claimed = store.claim_unpublished_notifications(limit=100)
    assert outcome.notification_id in [item.notification_id for item in claimed]
    # reconciliation finds the missed notification
    assert outcome.notification_id in store.unreconciled_notification_ids()

    healthy = NotificationService(
        store=store, transport=transport, payload_reader=reader
    )
    report = healthy.reconcile(limit=100)
    assert outcome.notification_id in report.republished

    consumed = healthy.consume(count=10)
    assert [item.notification_id for item in consumed.received] == [outcome.notification_id]
    assert consumed.received[0].first_receipt is True
    # the notification is reconciled now
    assert store.unreconciled_notification_ids() == []
    assert _inbox_ids(clean_isolated_schema) == [outcome.notification_id]


def test_redis_down_leaves_the_outbox_row_claimable_without_raising(
    clean_isolated_schema: str,
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
) -> None:
    """With Redis down the row stays durable, claimable, and unpublished."""
    service = NotificationService(
        store=store, transport=_dead_transport(), payload_reader=reader
    )
    outcome = service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert outcome.published is False
    assert outcome.error is not None

    row = _outbox_row(clean_isolated_schema, outcome.notification_id)
    assert row is not None
    assert row[4] is None
    assert outcome.notification_id in [
        item.notification_id for item in store.claim_unpublished_notifications(limit=100)
    ]

    # re-publishing while Redis is down fails without raising and without
    # pretending the delivery happened
    report = service.publish_pending(limit=100)
    assert report.published == ()
    assert outcome.notification_id in report.failed
    assert report.error is not None
    assert _outbox_row(clean_isolated_schema, outcome.notification_id)[4] is None


def test_redis_transport_failure_is_a_typed_error_without_the_endpoint() -> None:
    """The transport raises a typed error that never carries the endpoint."""
    dead = _dead_transport()
    with pytest.raises(ParserNotificationError) as excinfo:
        dead.publish(
            notification_id=str(uuid.uuid4()), topic="parser.job.accepted.v1", payload={}
        )
    error = excinfo.value
    assert error.code == "notification_failed"
    assert "127.0.0.1" not in str(error)
    assert "redis://" not in str(error)


class _SummaryClient:
    """A client whose pending summary is whatever the case states."""

    def __init__(self, summary: Any) -> None:
        self._summary = summary

    def xpending(self, stream_key: str, group: str) -> Any:
        return self._summary


def test_an_unknown_pending_summary_is_a_typed_error_not_an_empty_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary shape the driver does not document is an unknown answer.

    "Nothing is pending" and "the answer is not one I know" are different
    answers, so the second one is a typed failure instead of a zero.
    """
    transport = RedisNotificationTransport(
        RedisTransportConfig(url="redis://127.0.0.1:1/0")
    )

    for summary in (["not", "a", "summary"], {"consumers": 2}):
        monkeypatch.setattr(transport, "_client", _SummaryClient(summary))
        with pytest.raises(ParserNotificationError) as excinfo:
            transport.pending_count()
        assert excinfo.value.code == "notification_failed"

    monkeypatch.setattr(transport, "_client", _SummaryClient({"pending": 3}))
    assert transport.pending_count() == 3


def test_redis_restart_converges_on_the_same_durable_state(
    clean_isolated_schema: str,
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
    redis_url: str,
) -> None:
    """After a Redis restart the durable state is unchanged and converges."""
    restart_url = _database_url(redis_url, RESTART_REDIS_DB)
    stream_key = f"fin_parser:test:restart:{uuid.uuid4().hex}"
    transport = RedisNotificationTransport(
        RedisTransportConfig(
            url=restart_url,
            stream_key=stream_key,
            group="fin_parser_restart_group",
            consumer="restart-consumer",
        )
    )
    service = NotificationService(
        store=store, transport=transport, payload_reader=reader
    )

    # a notification that was published but never confirmed
    published = service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert published.published is True
    # a notification whose delivery never left the durable outbox
    lost = NotificationService(
        store=store, transport=_dead_transport(), payload_reader=reader
    )
    missed = lost.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert missed.published is False

    # the Redis instance loses everything it held
    redis.Redis.from_url(restart_url, socket_timeout=2).flushdb()

    # the durable state is untouched by the restart
    assert _outbox_row(clean_isolated_schema, published.notification_id)[4] is not None
    assert _outbox_row(clean_isolated_schema, missed.notification_id)[4] is None
    assert _inbox_ids(clean_isolated_schema) == []
    # both notifications are outstanding after the restart
    assert set(store.unreconciled_notification_ids()) == {
        published.notification_id,
        missed.notification_id,
    }

    # reconciliation re-drives both from the durable authority
    report = service.reconcile(limit=100)
    assert set(report.republished) == {published.notification_id, missed.notification_id}

    consumed = service.consume(count=10)
    assert {item.notification_id for item in consumed.received} == {
        published.notification_id,
        missed.notification_id,
    }
    # each notification is recorded exactly once, so the redelivery is a no-op
    assert sorted(_inbox_ids(clean_isolated_schema)) == sorted(
        [published.notification_id, missed.notification_id]
    )
    assert store.unreconciled_notification_ids() == []


def test_redis_delivery_never_becomes_job_truth(
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
    transport: RedisNotificationTransport,
) -> None:
    """No Redis condition may change the durable job row."""
    submission = store.submit_job(
        idempotency_key=f"key-{uuid.uuid4()}",
        input_sha256="a" * 64,
        source_ref=None,
        declared_format=None,
    )
    before = store.get_job(submission.job_id)
    assert before is not None
    assert before.status == "queued"

    service = NotificationService(
        store=store, transport=transport, payload_reader=reader
    )

    # Redis up: publish, consume, acknowledge
    outcome = service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": submission.job_id}
    )
    assert outcome.published is True
    assert len(service.consume(count=10).received) == 1

    # duplicate delivery
    transport.publish(
        notification_id=outcome.notification_id,
        topic="parser.job.accepted.v1",
        payload={"job_id": submission.job_id},
    )
    service.consume(count=10)

    # Redis down: the wakeup is lost
    dead = NotificationService(
        store=store, transport=_dead_transport(), payload_reader=reader
    )
    dead.enqueue(topic="parser.job.accepted.v1", payload={"job_id": submission.job_id})

    after = store.get_job(submission.job_id)
    assert after == before
    assert after.status == "queued"


def test_redis_is_optional_when_the_url_is_unset(
    store: PostgresParserJobStore,
    reader: PostgresNotificationReader,
) -> None:
    """Without Redis the notification simply stays unpublished."""
    service = NotificationService(store=store, transport=None, payload_reader=reader)
    outcome = service.enqueue(
        topic="parser.job.accepted.v1", payload={"job_id": str(uuid.uuid4())}
    )
    assert outcome.published is False
    assert outcome.error is None
    assert outcome.notification_id in [
        item.notification_id for item in store.claim_unpublished_notifications(limit=100)
    ]


def test_wakeup_stream_stays_bounded_when_many_notifications_are_published(
    redis_url: str,
) -> None:
    """The wakeup stream is trimmed, so it does not grow without limit."""
    stream_key = f"fin_parser:test:bound:{uuid.uuid4().hex}"
    bound = 50
    published = 500
    transport = RedisNotificationTransport(
        RedisTransportConfig(
            url=redis_url,
            stream_key=stream_key,
            group="fin_parser_test_bound_group",
            consumer="bound-consumer",
            stream_maxlen=bound,
        )
    )
    client = redis.Redis.from_url(redis_url, socket_timeout=2)
    try:
        for index in range(published):
            entry_id = transport.publish(
                notification_id=str(uuid.uuid4()),
                topic="parser.job.accepted.v1",
                payload={"index": index},
            )
            assert entry_id
        length = int(client.xlen(stream_key))
        # Redis trims approximately and in whole nodes, so the bound is checked
        # with a generous margin instead of against an exact length.
        assert length < published
        assert length <= bound * 4
    finally:
        with contextlib.suppress(redis.RedisError):
            client.delete(stream_key)


def test_stream_bound_comes_from_the_environment_and_falls_back_on_bad_values(
    redis_url: str,
) -> None:
    """The bound is operator configuration; a bad value keeps the default."""
    environ = {
        "FIN_REDIS_URL": redis_url,
        "FIN_PARSER_REDIS_STREAM_MAXLEN": "42",
    }
    config = RedisTransportConfig.from_environment(environ=environ)
    assert config.stream_maxlen == 42

    for bad in ("", "not-a-number", "0", "-5", "12.5"):
        environ["FIN_PARSER_REDIS_STREAM_MAXLEN"] = bad
        fallback = RedisTransportConfig.from_environment(environ=environ)
        assert fallback.stream_maxlen == DEFAULT_STREAM_MAXLEN

    del environ["FIN_PARSER_REDIS_STREAM_MAXLEN"]
    unset = RedisTransportConfig.from_environment(environ=environ)
    assert unset.stream_maxlen == DEFAULT_STREAM_MAXLEN


def test_stream_key_and_group_come_from_the_environment(redis_url: str) -> None:
    """The stream key, group, and consumer are operator configuration."""
    stream_key = f"fin_parser:test:env:{uuid.uuid4().hex}"
    environ = {
        "FIN_REDIS_URL": redis_url,
        "FIN_PARSER_REDIS_STREAM": stream_key,
        "FIN_PARSER_REDIS_GROUP": "fin_parser_test_env_group",
        "FIN_PARSER_REDIS_CONSUMER": "env-consumer",
    }
    client = RedisNotificationTransport.from_environment(environ=environ)
    assert client.stream_key == stream_key
    assert client.group == "fin_parser_test_env_group"
    assert client.consumer == "env-consumer"

    notification_id = str(uuid.uuid4())
    client.publish(
        notification_id=notification_id,
        topic="parser.job.accepted.v1",
        payload={"job_id": str(uuid.uuid4())},
    )
    messages = client.consume(count=10)
    assert [message.notification_id for message in messages] == [notification_id]
    client.acknowledge(messages[0].message_id)
    with contextlib.suppress(redis.RedisError):
        redis.Redis.from_url(redis_url, socket_timeout=2).delete(stream_key)
