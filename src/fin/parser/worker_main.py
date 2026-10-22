"""Worker process CLI (T6/T7).

One worker process claims parser attempts, runs an injected producer under
its lease, and commits the result only while its fencing token is the
effective one.  Beside that loop it runs the NOTIFICATION lane: the durable
outbox is drained to the Redis wakeup channel, and a notification that was
lost, trimmed, or never published is found again by the reconciliation.

Every setting is read from the environment; nothing about a deployment is
hardcoded here:

* ``FIN_PARSER_DSN`` -- the parser-owned PostgreSQL authority (required).
* ``FIN_REDIS_URL`` -- the wakeup transport. Redis is a wakeup channel only,
  and the notification lane owns it, so this process keeps the setting and
  polls the durable authority instead of treating a delivery as truth.
* ``FIN_PARSER_WORKER_ID`` -- the identity recorded on every attempt.
* ``FIN_PARSER_LEASE_SECONDS`` / ``FIN_PARSER_POLL_SECONDS`` /
  ``FIN_PARSER_BATCH_SIZE`` -- lease length and poll cadence.
* ``FIN_PARSER_RECONCILE_INTERVAL_SECONDS`` -- how often the notification
  lane repairs the notifications that never reached the channel.
* ``FIN_PARSER_PRODUCER`` -- ``module:callable`` naming the producer. The
  worker knows nothing about parsing, so the producer is a deployment choice.

This module is the composition root: it picks the durable store, the
worker-side attempt bookkeeping, the notification lane, and the producer, and
hands them to the worker.

``SIGTERM`` and ``SIGINT`` stop the poll loop cleanly. The store uses
short-lived connections, so a stopped worker leaves no database session
behind.
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from fin.parser.errors import ParserError, ParserInvalidRequestError
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.notifications import (
    DEFAULT_RECONCILE_INTERVAL_SECONDS,
    NotificationPublisher,
    NotificationService,
    PostgresNotificationReader,
    PublisherConfig,
)
from fin.parser.ports import Attempt, JobRecord
from fin.parser.redis_client import RedisNotificationTransport, RedisTransportConfig
from fin.parser.worker import (
    AttemptResult,
    ParserWorker,
    ParserWorkerConfig,
    PostgresWorkerAttemptStates,
)

__all__ = ["WorkerSettings", "main"]

# The statuses this process may act on: a queued job waits for an attempt, a
# running job has one, and a recorded cancellation request has to be
# finalized.
ACTIONABLE_STATUSES: Final[tuple[str, ...]] = ("queued", "running", "cancel_requested")


@dataclass(frozen=True)
class WorkerSettings:
    """One worker process's resolved configuration."""

    dsn: str
    redis: RedisTransportConfig
    worker_id: str
    lease_seconds: float
    poll_seconds: float
    batch_size: int
    producer_spec: str | None
    reconcile_interval_seconds: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WorkerSettings":
        """Resolve the settings from the environment.

        A missing DSN is a refusal, not a default: a worker without its
        durable authority has nowhere to claim an attempt.
        """
        env = os.environ if environ is None else environ
        dsn = (env.get("FIN_PARSER_DSN") or "").strip()
        if not dsn:
            raise ParserInvalidRequestError("FIN_PARSER_DSN is required")
        worker_id = (env.get("FIN_PARSER_WORKER_ID") or "").strip() or (
            f"{socket.gethostname()}-{os.getpid()}"
        )
        return cls(
            dsn=dsn,
            redis=RedisTransportConfig.from_environment(env),
            worker_id=worker_id,
            lease_seconds=_positive_float(env, "FIN_PARSER_LEASE_SECONDS", 60.0),
            poll_seconds=_positive_float(env, "FIN_PARSER_POLL_SECONDS", 1.0),
            batch_size=_positive_int(env, "FIN_PARSER_BATCH_SIZE", 10),
            producer_spec=(env.get("FIN_PARSER_PRODUCER") or "").strip() or None,
            reconcile_interval_seconds=_positive_float(
                env,
                "FIN_PARSER_RECONCILE_INTERVAL_SECONDS",
                DEFAULT_RECONCILE_INTERVAL_SECONDS,
            ),
        )


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        # EXIT: the environment names no value, so the documented default
        # applies.
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ParserInvalidRequestError(f"{key} must be a number") from exc
    if value <= 0:
        raise ParserInvalidRequestError(f"{key} must be positive")
    return value


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        # EXIT: the environment names no value, so the documented default
        # applies.
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ParserInvalidRequestError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ParserInvalidRequestError(f"{key} must be positive")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fin-parser-worker",
        description="Claim parser attempts, run the configured producer, commit under the fence.",
    )
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="process jobs until the process is stopped")
    run.add_argument(
        "--job-id",
        default=None,
        help="process only this job instead of every open job",
    )
    cancel = commands.add_parser("cancel", help="cancel one parser job durably")
    cancel.add_argument("--job-id", required=True, help="the parser job to cancel")
    return parser


def _load_producer(settings: WorkerSettings) -> Callable[[Attempt], AttemptResult]:
    """Resolve the configured ``module:callable`` producer."""
    spec = settings.producer_spec or ""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ParserInvalidRequestError(
            "FIN_PARSER_PRODUCER is required to run a worker and must name 'module:callable'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ParserInvalidRequestError(
            "the configured producer module could not be imported"
        ) from exc
    producer = getattr(module, attribute, None)
    if not callable(producer):
        raise ParserInvalidRequestError("the configured producer is not callable")
    return cast(Callable[[Attempt], AttemptResult], producer)


def _install_stop_signals(stop: threading.Event) -> None:
    """Stop the poll loop on SIGTERM/SIGINT.

    The handler only sets the event: the loop finishes the job it is on and
    then exits, so a stop request never abandons a half-written transaction.
    """

    def _handle(_signum: int, _frame: object) -> None:
        stop.set()

    # Only the main thread may own a signal disposition. A worker started on
    # another thread leaves the disposition to its caller instead of failing.
    if threading.current_thread() is not threading.main_thread():
        return

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, _handle)


def _cancel(settings: WorkerSettings, job_id: str) -> int:
    store = PostgresParserJobStore(settings.dsn)
    job = store.cancel_job(job_id)
    print(f"{job.status} {job.job_id}")
    return 0


def _build_publisher(
    settings: WorkerSettings, store: PostgresParserJobStore
) -> NotificationPublisher:
    """The notification lane of this worker process.

    The lane is built from the durable store: the outbox rows are the source
    of truth, and the wakeup channel only carries them.
    """
    service = NotificationService(
        store=store,
        transport=RedisNotificationTransport(settings.redis),
        payload_reader=PostgresNotificationReader(settings.dsn),
    )
    return NotificationPublisher(
        service,
        PublisherConfig(
            publish_interval_seconds=settings.poll_seconds,
            reconcile_interval_seconds=settings.reconcile_interval_seconds,
            batch_size=settings.batch_size,
        ),
    )


def _run(
    settings: WorkerSettings,
    *,
    producer: Callable[[Attempt], AttemptResult],
    job_id: str | None = None,
) -> int:
    store = PostgresParserJobStore(settings.dsn)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store,
            states=PostgresWorkerAttemptStates(settings.dsn),
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            producer=producer,
            # Renew well inside the lease: a producer that outruns its lease must
            # not lose the attempt it is still working on.
            renew_interval=settings.lease_seconds / 3,
        )
    )
    stop = threading.Event()
    _install_stop_signals(stop)
    lane = threading.Thread(
        target=_build_publisher(settings, store).run,
        args=(stop,),
        name="fin-parser-notifications",
        daemon=True,
    )
    lane.start()
    try:
        _poll_loop(worker, store, settings, job_id, stop)
    finally:
        stop.set()
        lane.join(timeout=30)
    return 0


def _poll_loop(
    worker: ParserWorker,
    store: PostgresParserJobStore,
    settings: WorkerSettings,
    job_id: str | None,
    stop: threading.Event,
) -> None:
    """Work on the open jobs until the process is asked to stop."""
    while not stop.is_set():
        _poll_once(worker, store, settings, job_id, stop)
        stop.wait(settings.poll_seconds)


def _poll_once(
    worker: ParserWorker,
    store: PostgresParserJobStore,
    settings: WorkerSettings,
    job_id: str | None,
    stop: threading.Event,
) -> None:
    """Take the open jobs once and work on them until a stop is asked."""
    for job in _jobs_to_process(store, settings.batch_size, job_id):
        if stop.is_set():
            break
        _process_job(worker, job)


def _process_job(worker: ParserWorker, job: JobRecord) -> None:
    """Work on one job; a failure on it never stops the worker."""
    try:
        worker.process(job.job_id)
    except ParserError as exc:
        # EXIT: a contended or cancelled job is normal traffic — the next poll
        # picks it up again, and nothing here may stop the worker.
        print(
            f"fin-parser-worker: job {job.job_id} not processed: {exc.code}",
            file=sys.stderr,
        )


def _jobs_to_process(
    store: PostgresParserJobStore, limit: int, job_id: str | None
) -> list[JobRecord]:
    """The jobs this process may work on right now.

    A scoped worker (``--job-id``) touches only its own job, so a process
    that was pointed at one job never claims work it was not asked for.
    """
    if job_id is None:
        return store.list_open_jobs(limit=limit)
    job = store.get_job(job_id)
    if job is None or job.status not in ACTIONABLE_STATUSES:
        return []
    return [job]


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    producer: Callable[[Attempt], AttemptResult] | None = None,
) -> int:
    """Run the worker CLI. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = args.command or "run"

    try:
        settings = WorkerSettings.from_env(environ)
    except ParserError as exc:
        print(f"fin-parser-worker: {exc.code}", file=sys.stderr)
        return 2

    if command == "cancel":
        try:
            return _cancel(settings, args.job_id)
        except ParserError as exc:
            print(f"fin-parser-worker: {exc.code}", file=sys.stderr)
            return 1

    try:
        resolved = producer if producer is not None else _load_producer(settings)
    except ParserError as exc:
        print(f"fin-parser-worker: {exc.code}", file=sys.stderr)
        return 2
    # ``job_id`` only exists when a subcommand declared it.
    return _run(settings, producer=resolved, job_id=getattr(args, "job_id", None))


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
