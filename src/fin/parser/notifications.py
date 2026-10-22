"""Durable parser notifications: reconciliation and the Redis wakeup channel.

(contract §8 -- docs/decisions/p2-parser-service-contract.md §8)

The parser-owned PostgreSQL outbox is the notification SOURCE OF TRUTH and
Redis is a bounded wakeup channel.  Redis may be down, may restart, and may
deliver the same notification twice; none of that may lose or corrupt job
state.  The service below therefore keeps the durable row as the authority
and treats every Redis outcome as best effort.

The service is written against the frozen ports: ``fin.parser.ports`` owns the
``Notification`` value object and the ``ParserJobStore`` port whose
notification methods carry the outbox and the inbox.  Reconciling a
notification that was published but never confirmed needs that row's payload,
and the frozen port list exposes no read for it, so the optional
``NotificationPayloadReader`` capability carries the one read it needs; the
write side stays with the parser store.

The publisher below is the PRODUCT PATH: it is what turns the durable rows
into wakeups, and what repairs the rows that were lost, trimmed, or never
published.  Without it the outbox would have no producer at all.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Iterator, Mapping, Protocol

from fin.parser.db import parser_connection
from fin.parser.errors import ParserError, ParserNotificationError
from fin.parser.ports import Notification, ParserJobStore
from fin.parser.redis_client import StreamMessage

logger = logging.getLogger(__name__)

DEFAULT_CLAIM_LIMIT: Final[int] = 100
DEFAULT_PUBLISH_INTERVAL_SECONDS: Final[float] = 1.0
DEFAULT_RECONCILE_INTERVAL_SECONDS: Final[float] = 30.0


# ----- the ports this slice adds -----


class NotificationPayloadReader(Protocol):
    """Read one notification row back by id.

    It is separate because reconciling a notification that was published but
    never confirmed needs the row's payload, and the frozen port list exposes
    no read for it.
    """

    def notification_by_id(self, notification_id: str) -> Notification | None:
        """Return the stored notification, or None when it is unknown."""
        ...


class NotificationTransport(Protocol):
    """The wakeup channel the service talks to."""

    @property
    def enabled(self) -> bool:
        """Whether a wakeup channel is configured at all."""
        ...

    def publish(
        self, *, notification_id: str, topic: str, payload: Mapping[str, object]
    ) -> str:
        """Durably hand one notification to the wakeup channel."""
        ...

    def consume(self, *, count: int, block_ms: int) -> list[StreamMessage]:
        """Read the wakeups this consumer has not seen yet."""
        ...

    def acknowledge(self, message_id: str) -> bool:
        """Acknowledge one delivered wakeup."""
        ...


# ----- outcomes -----


@dataclass(frozen=True)
class EnqueueOutcome:
    """The result of recording one notification durably."""

    notification_id: str
    published: bool
    error: ParserNotificationError | None = None


@dataclass(frozen=True)
class PublishOutcome:
    """The result of publishing the notifications the outbox still holds."""

    published: tuple[str, ...]
    failed: tuple[str, ...]
    error: ParserNotificationError | None = None


@dataclass(frozen=True)
class ConsumeOutcome:
    """The result of reading, recording, and acknowledging wakeups."""

    received: tuple["ReceivedNotification", ...]
    error: ParserNotificationError | None = None


@dataclass(frozen=True)
class ReceivedNotification:
    """One delivered notification and whether the inbox saw it first."""

    notification_id: str
    topic: str
    payload: Mapping[str, object]
    first_receipt: bool


@dataclass(frozen=True)
class ReconciliationReport:
    """The result of re-driving every outstanding notification."""

    republished: tuple[str, ...]
    still_unreconciled: tuple[str, ...]
    error: ParserNotificationError | None = None


# ----- the reconciliation read -----


class PostgresNotificationReader:
    """Read one outbox row back, for the reconciliation path.

    It exists because the reconciliation must re-publish a notification that
    was published but never confirmed, and it needs the row's payload.  The
    frozen port list has no read for it.  This class only reads: the write
    side of the outbox and the inbox stays with the parser store.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @property
    def connection_dsn(self) -> str:
        """The durable URL this reader reads from."""
        return self._dsn

    @contextmanager
    def _guarded(self, action: str) -> Iterator[Any]:
        with parser_connection(
            self._dsn,
            message=f"{action}: the parser notification store could not be reached",
        ) as conn:
            yield conn

    def notification_by_id(self, notification_id: str) -> Notification | None:
        """Return the stored notification, or None when it is unknown."""
        with self._guarded("read a notification") as conn:
            row = conn.execute(
                "SELECT notification_id, topic, payload, created_at "
                "FROM fin_parser.outbox WHERE notification_id = %s",
                (notification_id,),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return Notification(
            notification_id=str(row[0]),
            topic=str(row[1]),
            payload=dict(row[2]) if isinstance(row[2], Mapping) else {},
            created_at=row[3],
        )


# ----- the service -----


class NotificationService:
    """Durable notification recording with a best-effort wakeup channel.

    The service never lets a Redis condition decide whether work was accepted:
    the outbox row is written first, and every Redis failure degrades into an
    unpublished row that the reconciliation path re-drives.
    """

    def __init__(
        self,
        *,
        store: ParserJobStore,
        transport: NotificationTransport | None = None,
        payload_reader: NotificationPayloadReader | None = None,
    ) -> None:
        self._store = store
        self._transport = transport
        self._payload_reader = payload_reader

    def enqueue(
        self, *, topic: str, payload: Mapping[str, object]
    ) -> EnqueueOutcome:
        """Record the notification durably, then try to wake a consumer up.

        A Redis failure is reported in the outcome instead of raised: the row
        is durable, so raising would only lose the caller's chance to know
        that the wakeup is outstanding.
        """
        notification_id = self._store.enqueue_notification(topic=topic, payload=payload)
        published, error = self._publish(
            notification_id=notification_id, topic=topic, payload=payload
        )
        return EnqueueOutcome(
            notification_id=notification_id, published=published, error=error
        )

    def publish_pending(self, *, limit: int = DEFAULT_CLAIM_LIMIT) -> PublishOutcome:
        """Publish every notification the durable outbox still holds unpublished."""
        published: list[str] = []
        failed: list[str] = []
        first_error: ParserNotificationError | None = None
        for notification in self._store.claim_unpublished_notifications(limit=limit):
            ok, error = self._publish(
                notification_id=notification.notification_id,
                topic=notification.topic,
                payload=notification.payload,
            )
            if ok:
                published.append(notification.notification_id)
            else:
                failed.append(notification.notification_id)
                first_error = first_error or error
        return PublishOutcome(
            published=tuple(published), failed=tuple(failed), error=first_error
        )

    def receive(self, notification_id: str) -> bool:
        """Record a receipt; False when the inbox already holds the id."""
        return self._store.record_notification_received(notification_id)

    def consume(self, *, count: int = 10, block_ms: int = 0) -> ConsumeOutcome:
        """Read wakeups, record them in the inbox, then acknowledge them.

        The receipt is durable before the acknowledgement, so a failure in
        between leaves a redelivery that the inbox turns into a no-op.
        """
        if self._transport is None or not self._transport.enabled:
            # EXIT: no wakeup channel is configured, so there is nothing to
            # consume and the outcome carries no message.
            return ConsumeOutcome(received=())
        messages = self._transport.consume(count=count, block_ms=block_ms)
        received: list[ReceivedNotification] = []
        ack_error: ParserNotificationError | None = None
        for message in messages:
            first_receipt = self._store.record_notification_received(
                message.notification_id
            )
            try:
                self._transport.acknowledge(message.message_id)
            except ParserNotificationError as exc:
                # EXIT: the acknowledgement failure does not abort the loop —
                # the receipt is already durable and a redelivery is
                # idempotent — so the first error is carried in the outcome.
                ack_error = ack_error or exc
            received.append(
                ReceivedNotification(
                    notification_id=message.notification_id,
                    topic=message.topic,
                    payload=message.payload,
                    first_receipt=first_receipt,
                )
            )
        return ConsumeOutcome(received=tuple(received), error=ack_error)

    def unreconciled(self) -> list[str]:
        """The durable ids the outbox still considers outstanding."""
        return self._store.unreconciled_notification_ids()

    def reconcile(self, *, limit: int = DEFAULT_CLAIM_LIMIT) -> ReconciliationReport:
        """Re-drive every outstanding notification from the durable authority.

        Two cases are covered: a row that was never published, and a row that
        was published but never confirmed.  The first comes back from the
        claim; the second needs its payload, which only the payload reader
        can supply.

        The repair is bounded: a pass that re-drove every outstanding row
        could take minutes when the channel was down for a while.  The bound
        keeps a pass short, and the next pass picks up what is left.
        """
        pending = self.publish_pending(limit=limit)
        republished = list(pending.published)
        error = pending.error
        just_published = set(pending.published)
        outstanding = [
            notification_id
            for notification_id in self._store.unreconciled_notification_ids()
            if notification_id not in just_published
        ][:limit]
        for notification_id in outstanding:
            source = self._load_outstanding(notification_id)
            if source is None:
                # EXIT: the row's payload cannot be read back, so the
                # notification stays outstanding and a later pass re-drives it.
                continue
            ok, publish_error = self._publish(
                notification_id=source.notification_id,
                topic=source.topic,
                payload=source.payload,
            )
            if ok:
                republished.append(source.notification_id)
            else:
                error = error or publish_error
        return ReconciliationReport(
            republished=tuple(republished),
            still_unreconciled=tuple(self._store.unreconciled_notification_ids()),
            error=error,
        )

    def _load_outstanding(self, notification_id: str) -> Notification | None:
        if self._payload_reader is None:
            # EXIT: without a payload reader the row cannot be read back, so
            # the notification stays outstanding.
            return None
        return self._payload_reader.notification_by_id(notification_id)

    def _publish(
        self, *, notification_id: str, topic: str, payload: Mapping[str, object]
    ) -> tuple[bool, ParserNotificationError | None]:
        if self._transport is None or not self._transport.enabled:
            # EXIT: the wakeup channel is optional — the row stays unpublished
            # and the reconciliation path re-drives it when a channel is back.
            return False, None
        try:
            self._transport.publish(
                notification_id=notification_id, topic=topic, payload=payload
            )
        except ParserNotificationError as exc:
            # EXIT: the publish failed — the row stays unpublished and the
            # typed error is carried in the outcome for the reconciliation.
            return False, exc
        # EXIT: the stamp comes after the publish — a stamped row that never
        # reached the channel would be invisible to the reconciliation path.
        self._store.mark_notification_published(notification_id)
        return True, None


# ----- the publisher -----


@dataclass(frozen=True)
class PublisherConfig:
    """How often the publisher drains the outbox and repairs lost wakeups."""

    publish_interval_seconds: float = DEFAULT_PUBLISH_INTERVAL_SECONDS
    reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS
    batch_size: int = DEFAULT_CLAIM_LIMIT


class NotificationPublisher:
    """Turns the durable outbox into wakeups on the wakeup channel.

    It exists because the durable outbox is the source of truth, and
    without a producer in the product path nothing would ever reach the
    channel.  The publisher is that producer, and it is also the repair path:
    a notification that was lost, trimmed, or never published is found again
    from the durable rows, so the channel converges on the durable state.
    """

    def __init__(self, service: NotificationService, config: PublisherConfig) -> None:
        self._service = service
        self._config = config

    def publish_once(self) -> PublishOutcome:
        """Publish every notification the durable outbox still holds unpublished."""
        return self._service.publish_pending(limit=self._config.batch_size)

    def reconcile_once(self) -> ReconciliationReport:
        """Re-drive every outstanding notification from the durable authority."""
        return self._service.reconcile(limit=self._config.batch_size)

    def run(self, stop: threading.Event) -> None:
        """Publish and repair the outbox until the process is asked to stop.

        Every failure is caught here because the wakeup channel is not the job
        authority, so a Redis outage must never stop the work that runs
        beside it.
        """
        self._reconcile_safely()
        last_reconciliation = time.monotonic()
        while not stop.is_set():
            self._publish_safely()
            now = time.monotonic()
            if now - last_reconciliation >= self._config.reconcile_interval_seconds:
                self._reconcile_safely()
                last_reconciliation = now
            stop.wait(self._config.publish_interval_seconds)

    def _publish_safely(self) -> None:
        """Publish once, and report a failure without stopping the lane."""
        try:
            outcome = self.publish_once()
        except ParserError as exc:
            # EXIT: the publish failed — the failure is reported and the lane
            # keeps running, because the durable outbox stays the authority.
            logger.warning(
                "the parser notifications could not be published: %s", exc.code
            )
            return
        if not outcome.failed:
            return
        if outcome.error is not None:
            logger.warning(
                "%s parser notifications did not reach the wakeup channel: %s",
                len(outcome.failed),
                outcome.error.code,
            )
            return
        logger.warning(
            "%s parser notifications did not reach the wakeup channel",
            len(outcome.failed),
        )

    def _reconcile_safely(self) -> None:
        """Reconcile once, and report a failure without stopping the lane."""
        try:
            report = self.reconcile_once()
        except ParserError as exc:
            # EXIT: the reconciliation failed — the failure is reported and the
            # lane keeps running, because the next pass repairs the rows.
            logger.warning(
                "the parser notifications could not be reconciled: %s", exc.code
            )
            return
        if report.republished:
            logger.info(
                "%s parser notifications were re-driven to the wakeup channel",
                len(report.republished),
            )


__all__ = [
    "ConsumeOutcome",
    "DEFAULT_CLAIM_LIMIT",
    "DEFAULT_PUBLISH_INTERVAL_SECONDS",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "EnqueueOutcome",
    "Notification",
    "NotificationPayloadReader",
    "NotificationPublisher",
    "NotificationService",
    "NotificationTransport",
    "PostgresNotificationReader",
    "PublishOutcome",
    "PublisherConfig",
    "ReceivedNotification",
    "ReconciliationReport",
]
