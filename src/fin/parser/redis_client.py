"""Redis delivery transport for parser notifications.

(contract §8 -- docs/decisions/p2-parser-service-contract.md §8)

Redis is a BOUNDED WAKEUP CHANNEL for the parser service, never the source of
truth.  Every notification this module carries is also durable in the
parser-owned PostgreSQL outbox, so a lost, duplicated, or missed wakeup can
never lose or corrupt job state.

Redis Streams with a consumer group are used because they provide exactly the
three properties the reconciliation path needs: a durable publish (``XADD``),
consumer-group consumption (``XREADGROUP``), and an explicit acknowledgement
(``XACK``).  The group is created at id ``0-0`` so a wakeup that was published
before the consumer existed is still delivered instead of being skipped.

The stream is trimmed to a bounded length on every publish, so the channel
cannot grow without limit.  A trimmed wakeup is an ordinary recoverable event:
the outbox row is still there and reconciliation republishes it.

The transport is OPTIONAL.  With no ``FIN_REDIS_URL`` the client is disabled
and the caller keeps accumulating unpublished outbox rows; when Redis is
unreachable every command raises :class:`ParserNotificationError` instead of
fabricating a successful delivery.  The configured endpoint is never logged,
echoed, or carried in an error message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

import redis

from fin.parser.errors import ParserNotificationError

ENV_REDIS_URL: Final[str] = "FIN_REDIS_URL"
ENV_STREAM_KEY: Final[str] = "FIN_PARSER_REDIS_STREAM"
ENV_GROUP: Final[str] = "FIN_PARSER_REDIS_GROUP"
ENV_CONSUMER: Final[str] = "FIN_PARSER_REDIS_CONSUMER"
ENV_STREAM_MAXLEN: Final[str] = "FIN_PARSER_REDIS_STREAM_MAXLEN"

DEFAULT_STREAM_KEY: Final[str] = "fin_parser:notifications"
DEFAULT_GROUP: Final[str] = "fin_parser_workers"
DEFAULT_CONSUMER: Final[str] = "consumer"
DEFAULT_SOCKET_TIMEOUT: Final[float] = 2.0
DEFAULT_COUNT: Final[int] = 10
DEFAULT_STREAM_MAXLEN: Final[int] = 1000

_FIELD_NOTIFICATION_ID: Final[str] = "notification_id"
_FIELD_TOPIC: Final[str] = "topic"
_FIELD_PAYLOAD: Final[str] = "payload"

# The group starts at the beginning of the stream: a wakeup published before
# the consumer group existed must still be delivered, otherwise the
# missed-delivery reconciliation would have to replay it by hand.
_GROUP_START_ID: Final[str] = "0-0"


def _read_maxlen(raw: str | None) -> int:
    """Read the stream bound from a raw environment value."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        # EXIT: an unparsable bound would leave the stream unbounded, so the
        # default applies.
        return DEFAULT_STREAM_MAXLEN
    if value <= 0:
        # EXIT: a non-positive bound would leave the stream unbounded, so the
        # default applies.
        return DEFAULT_STREAM_MAXLEN
    return value


@dataclass(frozen=True)
class RedisTransportConfig:
    """Where the wakeup channel lives and who reads it.

    The endpoint is optional: with no URL the transport is disabled and the
    caller keeps accumulating unpublished outbox rows until a channel comes
    back.  The settings travel as one object so the transport itself stays a
    one-argument constructor.
    """

    url: str | None = None
    stream_key: str = DEFAULT_STREAM_KEY
    group: str = DEFAULT_GROUP
    consumer: str = DEFAULT_CONSUMER
    socket_timeout: float = DEFAULT_SOCKET_TIMEOUT
    # the wakeup stream is trimmed to this many entries on every publish
    stream_maxlen: int = DEFAULT_STREAM_MAXLEN

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "RedisTransportConfig":
        """Read the channel's settings from the environment."""
        source = os.environ if environ is None else environ
        return cls(
            url=(source.get(ENV_REDIS_URL) or "").strip() or None,
            stream_key=source.get(ENV_STREAM_KEY) or DEFAULT_STREAM_KEY,
            group=source.get(ENV_GROUP) or DEFAULT_GROUP,
            consumer=source.get(ENV_CONSUMER) or DEFAULT_CONSUMER,
            stream_maxlen=_read_maxlen(source.get(ENV_STREAM_MAXLEN)),
        )


@dataclass(frozen=True)
class StreamMessage:
    """One delivered wakeup as it came out of the stream."""

    message_id: str
    notification_id: str
    topic: str
    payload: dict[str, Any]


def _guard(action: str, operation: Callable[[redis.Redis], Any], client: redis.Redis) -> Any:
    """Run one Redis command and convert any failure into a typed error.

    The caller must be able to tell "the wakeup channel is unreachable" from
    "the notification was delivered", and the error must never carry the
    configured endpoint.
    """
    try:
        return operation(client)
    except (redis.RedisError, OSError) as exc:
        raise ParserNotificationError(
            f"{action}: the Redis wakeup channel could not be reached"
        ) from exc


class RedisNotificationTransport:
    """Stream-backed delivery for parser notifications."""

    def __init__(self, config: RedisTransportConfig) -> None:
        self._stream_key = config.stream_key
        self._group = config.group
        self._consumer = config.consumer
        self._socket_timeout = config.socket_timeout
        self._stream_maxlen = config.stream_maxlen
        self._client: redis.Redis | None = None
        if config.url:
            self._client = self._build_client(config.url, config.socket_timeout)

    @staticmethod
    def _build_client(url: str, socket_timeout: float) -> redis.Redis:
        try:
            return redis.Redis.from_url(
                url,
                socket_timeout=socket_timeout,
                socket_connect_timeout=socket_timeout,
                decode_responses=True,
            )
        except (TypeError, ValueError) as exc:
            # EXIT: an unusable endpoint is a configuration fault — the typed
            # error is raised and the message never carries the endpoint.
            raise ParserNotificationError(
                "the configured Redis endpoint is not usable"
            ) from exc

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "RedisNotificationTransport":
        """Build a transport from the environment alone."""
        return cls(RedisTransportConfig.from_environment(environ))

    @property
    def enabled(self) -> bool:
        """Whether a Redis endpoint is configured at all."""
        return self._client is not None

    @property
    def stream_key(self) -> str:
        return self._stream_key

    @property
    def group(self) -> str:
        return self._group

    @property
    def consumer(self) -> str:
        return self._consumer

    def _require_client(self) -> redis.Redis:
        if self._client is None:
            raise ParserNotificationError("the Redis wakeup channel is not configured")
        return self._client

    def ping(self) -> bool:
        """Whether the configured endpoint answers."""
        client = self._require_client()
        return bool(_guard("ping", lambda target: target.ping(), client))

    def ensure_group(self) -> None:
        """Create the consumer group if it is not there yet."""
        client = self._require_client()

        def _create(target: redis.Redis) -> None:
            try:
                target.xgroup_create(
                    name=self._stream_key,
                    groupname=self._group,
                    id=_GROUP_START_ID,
                    mkstream=True,
                )
            except redis.ResponseError as exc:
                # EXIT: BUSYGROUP means the group already exists, which is the
                # state this call wanted; any other reply is a real failure.
                if "BUSYGROUP" not in str(exc):
                    raise

        _guard("ensure_group", _create, client)

    def publish(
        self, *, notification_id: str, topic: str, payload: Mapping[str, object]
    ) -> str:
        """Durably add one wakeup to the stream and return its entry id."""
        try:
            body = json.dumps(dict(payload), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ParserNotificationError(
                "the notification payload is not JSON-serialisable"
            ) from exc
        client = self._require_client()
        entry_id = _guard(
            "publish",
            lambda target: target.xadd(
                self._stream_key,
                {
                    _FIELD_NOTIFICATION_ID: notification_id,
                    _FIELD_TOPIC: topic,
                    _FIELD_PAYLOAD: body,
                },
                maxlen=self._stream_maxlen,
                approximate=True,
            ),
            client,
        )
        return str(entry_id)

    def consume(
        self, *, count: int = DEFAULT_COUNT, block_ms: int = 0
    ) -> list[StreamMessage]:
        """Read wakeups this consumer has not seen yet."""
        self.ensure_group()
        client = self._require_client()
        response = _guard(
            "consume",
            lambda target: target.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream_key: ">"},
                count=count,
                block=block_ms if block_ms > 0 else None,
            ),
            client,
        )
        return self._decode(response)

    @staticmethod
    def _decode(response: Any) -> list[StreamMessage]:
        messages: list[StreamMessage] = []
        for _stream, entries in response or []:
            for message_id, fields in entries:
                body = fields.get(_FIELD_PAYLOAD) or "{}"
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError) as exc:
                    raise ParserNotificationError(
                        "a delivered notification payload is not valid JSON"
                    ) from exc
                messages.append(
                    StreamMessage(
                        message_id=str(message_id),
                        notification_id=str(fields.get(_FIELD_NOTIFICATION_ID) or ""),
                        topic=str(fields.get(_FIELD_TOPIC) or ""),
                        # EXIT: a payload that is not an object is wrapped as
                        # one, so the consumer always reads a mapping.
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                    )
                )
        return messages

    def acknowledge(self, message_id: str) -> bool:
        """Acknowledge one delivered wakeup; False when it was already acked."""
        client = self._require_client()
        return bool(
            _guard(
                "acknowledge",
                lambda target: target.xack(self._stream_key, self._group, message_id),
                client,
            )
        )

    def pending_count(self) -> int:
        """How many delivered wakeups are still unacknowledged in the group.

        A summary the driver does not document is an unknown answer, so it
        raises instead of answering zero.
        """
        client = self._require_client()
        try:
            summary = client.xpending(self._stream_key, self._group)
        except redis.ResponseError as exc:
            # EXIT: NOGROUP means the group is gone, so nothing can be
            # pending and the count answers zero.
            if "NOGROUP" in str(exc):
                return 0
            raise ParserNotificationError(
                "pending_count: the Redis wakeup channel could not be reached"
            ) from exc
        except (redis.RedisError, OSError) as exc:
            raise ParserNotificationError(
                "pending_count: the Redis wakeup channel could not be reached"
            ) from exc
        if isinstance(summary, dict) and "pending" in summary:
            try:
                return int(summary["pending"])
            except (TypeError, ValueError) as exc:
                raise ParserNotificationError(
                    "pending_count: the Redis wakeup channel answered an "
                    "unreadable pending count"
                ) from exc
        # EXIT: a summary shape the driver does not document is an unknown
        # answer, never "nothing pending", so it is a typed failure.
        raise ParserNotificationError(
            "pending_count: the Redis wakeup channel answered an unknown summary shape"
        )

    def close(self) -> None:
        """Release the connection; a failure here is still a typed failure."""
        client = self._client
        if client is None:
            # EXIT: no channel is configured, so there is nothing to release.
            return
        try:
            client.close()
        except (redis.RedisError, OSError) as exc:
            raise ParserNotificationError(
                "close: the Redis wakeup channel could not be released"
            ) from exc


__all__ = [
    "DEFAULT_CONSUMER",
    "DEFAULT_COUNT",
    "DEFAULT_GROUP",
    "DEFAULT_SOCKET_TIMEOUT",
    "DEFAULT_STREAM_KEY",
    "DEFAULT_STREAM_MAXLEN",
    "ENV_CONSUMER",
    "ENV_GROUP",
    "ENV_REDIS_URL",
    "ENV_STREAM_KEY",
    "ENV_STREAM_MAXLEN",
    "RedisNotificationTransport",
    "RedisTransportConfig",
    "StreamMessage",
]
