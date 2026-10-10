"""The reusable per-process drain-thread guard for boot-owned sweeps.

One posture, one implementation: startup drain first (the
restart-resumes-work clause needs no re-ask), then a bounded periodic
re-check so a job queued between two checks still runs within one
interval; a failing sweep is logged with exception context and the loop
keeps going, because silent thread death would leave the process with no
executor and the durable store makes the next sweep the recovery. The
claim CAS in the job stores is what keeps two processes safe; the
singleton guard here is what keeps one process from running two loops
over the same seams.

The interval is a bound on the *period* — sweep to sweep — and the loop
schedules against an absolute deadline so it is honoured however long a
sweep takes; see :class:`DrainCadence`.

This module was extracted from what had become three hand-rolled copies
of that same block (the ingest worker's module globals in
``workspace_http``, this module's prep loop, and the attachment
auto-resumer's module globals). New periodic boot sweeps use
:class:`PeriodicDrainWorker` — do not hand-roll a fourth copy.
:func:`drain_periodically` is the loop body alone, kept callable
without any thread so a test can drive the exact posture directly.
Nothing here imports the HTTP layer and nothing runs at import time — a
loop exists only while a caller calls :meth:`PeriodicDrainWorker.start`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fin.host.attachment_prepare import AttachmentPreparationService

logger = logging.getLogger(__name__)

ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS: Final[float] = 30.0

ATTACHMENT_PREP_WORKER_THREAD_NAME: Final[str] = "fin-attachment-prep-worker"

#: The loop's clock: given the stop event and the interval, block until the
#: interval elapses or a stop is requested, and answer whether a stop was
#: requested. Signature-compatible with ``threading.Event.wait``, which is the
#: default — a test substitutes a fake clock instead of sleeping.
Waiter = Callable[[threading.Event, float], bool]

#: A monotonic reading of the clock ``Waiter`` blocks on, defaulting to
#: ``time.monotonic``. Paired with :class:`DrainCadence` so a test can read
#: elapsed time without sleeping through it.
Monotonic = Callable[[], float]


@dataclass(frozen=True)
class DrainCadence:
    """How often the loop sweeps, and the clock that schedules it.

    ``interval_s`` bounds the *period* — sweep to sweep — not the sleep
    between two sweeps. The loop schedules against an absolute deadline, so
    the time a sweep costs is spent out of its own interval rather than added
    to the gap between two of them; a sweep that costs more than one interval
    runs again as soon as it returns. ``wait`` and ``monotonic`` are the two
    halves of the loop's clock seam (both ``threading``/``time`` by default),
    injectable for the same reason ``decideGrantRead`` takes the caller's
    ``at``: a cadence contract is only testable if a test can move the clock
    instead of sleeping through it.
    """

    interval_s: float
    wait: Waiter = threading.Event.wait
    monotonic: Monotonic = time.monotonic


class AttachmentPrepWorkerStoppingError(RuntimeError):
    """Start refused while a timed-out stop's thread is still draining.

    The singleton guard of :meth:`PeriodicDrainWorker.start`, bound for
    the prep drain: a stop whose join times out keeps the live thread
    registered, and a start over it would run a second drain loop in one
    process. Callers retry once the old sweep finishes (the loop exits
    through its stop event on its own).
    """

    code = "attachment_prep_worker_stopping"


def drain_periodically(
    sweep: Callable[[], object],
    stop_event: threading.Event,
    cadence: DrainCadence,
    *,
    worker_logger: logging.Logger,
    log_label: str,
) -> None:
    """Run ``sweep`` once at startup, then re-check until stopped.

    The cadence is scheduled against an absolute deadline — the next sweep is
    due ``cadence.interval_s`` after the previous one was *due* — so drain to
    drain is the interval plus at most the sweep that overran it, never the
    interval plus every sweep's cost on top. Sweep-then-sleep would make the
    gap ``interval + sweep``, which for a sweep that costs anything is already
    past a ceiling stated as "at least once per minute", and the overshoot
    would accumulate rather than amortize.

    An overrun is not a backlog: the loop advances the deadline one interval
    per iteration and never replays the intervals it slept through, so
    downtime is not replayed as a storm of sweeps.

    ``worker_logger`` / ``log_label`` belong to the owning module, not
    to this plumbing: a failure must be attributed in the logs to the
    worker that owns the sweep (its log name and message prefix), so
    two workers sharing this loop stay distinguishable in one log file.
    """
    deadline = cadence.monotonic() + cadence.interval_s
    while True:
        try:
            sweep()
        except Exception:
            # EXIT: a failing sweep is the transient case, not an exit:
            # the durable store plus the journals make the NEXT sweep
            # the recovery, so log with exception context and keep the
            # loop — killing it would leave the process with no
            # executor at all.
            worker_logger.exception(
                "%s: drain failed; continuing to the next check", log_label
            )
        remaining = deadline - cadence.monotonic()
        # A sweep that overran its interval leaves nothing to wait out; the
        # zero-time wait is still asked, because it is also where a stop
        # requested during the sweep is observed.
        if cadence.wait(stop_event, remaining if remaining > 0.0 else 0.0):
            # EXIT: the only exit — a stop was requested; return once
            # the in-flight sweep settles, because a half-run durable
            # job is exactly what the next boot's startup sweep
            # re-adopts.
            return
        deadline += cadence.interval_s


class PeriodicDrainWorker:
    """One process's singleton guard over one :func:`drain_periodically`.

    Singleton-safe per instance: ``start`` returns the live thread on a
    second call, refuses to start over a stopping one (raising the
    owner's ``stopping_error``), and ``stop`` joins cleanly so a restart
    can ``start`` again. The thread is a daemon, so a host that forgets
    to stop never blocks interpreter exit.
    """

    def __init__(
        self,
        sweep: Callable[[], object],
        cadence: DrainCadence,
        *,
        thread_name: str,
        stopping_error: type[RuntimeError],
        log_label: str,
        worker_logger: logging.Logger | None = None,
    ) -> None:
        self._sweep = sweep
        self._cadence = cadence
        self._thread_name = thread_name
        self._stopping_error = stopping_error
        self._log_label = log_label
        self._worker_logger = (
            worker_logger if worker_logger is not None else logger
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    def _loop(self, stop_event: threading.Event) -> None:
        drain_periodically(
            self._sweep,
            stop_event,
            self._cadence,
            worker_logger=self._worker_logger,
            log_label=self._log_label,
        )

    def start(self) -> threading.Thread:
        """Start the drain loop, or return the live one (idempotent)."""
        with self._lock:
            current = self._thread
            if current is not None and current.is_alive():
                if self._stop_event is not None and self._stop_event.is_set():
                    raise self._stopping_error(
                        f"the {self._log_label} is stopping (a prior stop "
                        "join timed out); start again once its thread exits"
                    )
                return current
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._loop,
                args=(stop_event,),
                name=self._thread_name,
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            thread.start()
            return thread

    def stop(self, *, timeout: float = 60.0) -> None:
        """Signal the drain loop to exit and join it. Idempotent.

        The in-flight sweep finishes its current claim first (a half-run
        job is exactly what the boot sweep re-adopts), then the loop
        returns. Stopping a worker that was never started is a no-op. On
        a join timeout the live thread stays registered so the next
        ``start`` refuses it rather than sharing the process with two
        loops.
        """
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is not None and not thread.is_alive():
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                    self._stop_event = None

    def is_live(self) -> bool:
        """True while a registered thread has not confirmed its exit.

        The rebind seam an owner holding a module-level singleton needs
        to decide whether a new worker object (honoring freshly passed
        seams) may replace this one: only after the previous thread's
        exit is confirmed, never over a live or still-stopping one.
        """
        with self._lock:
            thread = self._thread
            return thread is not None and thread.is_alive()


def _drain_attachment_queue(
    service: AttachmentPreparationService,
    stop_event: threading.Event,
    check_interval_seconds: float,
) -> None:
    """Startup drain, then a bounded periodic re-check, until stopped.

    The loop body is kept separate from the thread plumbing so a test can
    drive the exact same posture — drain, log-and-continue on failure,
    exit only through ``stop_event`` — without spawning anything. This
    is the prep's own pin over the shared :func:`drain_periodically`.
    """
    drain_periodically(
        lambda: service.drain_attachment_jobs(),
        stop_event,
        DrainCadence(check_interval_seconds),
        worker_logger=logger,
        log_label="attachment prep worker",
    )


class AttachmentPrepWorker(PeriodicDrainWorker):
    """One process's executor for durable attachment preparation jobs.

    The prep-specific binding (drain sweep, thread name, typed stopping
    error, log label) of the shared :class:`PeriodicDrainWorker` guard;
    kept as a named subclass so the composition-root imports, the
    constructor signature, and the singleton tests stay addressed to
    this worker by name.
    """

    def __init__(
        self,
        service: AttachmentPreparationService,
        *,
        interval_s: float = ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            lambda: service.drain_attachment_jobs(),
            DrainCadence(interval_s),
            thread_name=ATTACHMENT_PREP_WORKER_THREAD_NAME,
            stopping_error=AttachmentPrepWorkerStoppingError,
            log_label="attachment prep worker",
        )


__all__ = [
    "ATTACHMENT_PREP_WORKER_CHECK_INTERVAL_SECONDS",
    "ATTACHMENT_PREP_WORKER_THREAD_NAME",
    "AttachmentPrepWorker",
    "AttachmentPrepWorkerStoppingError",
    "DrainCadence",
    "Monotonic",
    "PeriodicDrainWorker",
    "Waiter",
    "drain_periodically",
]
