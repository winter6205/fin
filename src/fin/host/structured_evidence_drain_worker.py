"""The invalidation drain's caller: the cadence that owns ``drain_invalidation_batch``.

``specs/structured-corpus-evidence.md:255-256`` says "Fin drains at least once
per minute while online, transactionally withholds and scrubs affected stored
snapshot payloads, and acknowledges only afterward". P2 built the drain
(:mod:`fin.host.structured_evidence_invalidation`) but left its caller to P3,
so the whole lane sat idle: an owner nothing polls never learns a payload was
revoked. This module is that owner — one loop per process, over the store the
host already resolved, driving the same two-phase drain and nothing else.

The posture is the host's existing boot-sweep posture, which is already
extracted as :class:`~fin.host.attachment_prep_worker.PeriodicDrainWorker`:
one sweep at start, then one sweep per interval, log-and-continue on
failure, exit only through the stop event. This module adds the three things
the spec's sentence adds on top of that posture:

* **A cadence CEILING, not a constant.** "At least once per minute" makes the
  interval a maximum the operator must not widen, so a configured cadence
  above :data:`INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS` is refused at
  construction rather than honoured slowly. The ceiling holds because the
  loop schedules against an absolute deadline
  (:class:`~fin.host.attachment_prep_worker.DrainCadence`): the time a sweep
  costs is spent out of its own interval instead of being added to the gap
  between two sweeps, so drain-to-drain is the interval plus at most the one
  sweep that overran it.
* **A pending phase that only the drain can clear.**
  :data:`InvalidationDrainAttempt.drain_completed` is set after
  ``drain_invalidation_batch`` *returns*, which is after the scrub committed
  AND the acknowledgement landed. A refused, unreachable or half-finished
  attempt records ``drain_completed=False``, so an unreachable owner stays
  pending as ``specs/structured-corpus-maintenance.md:329-330`` requires, and
  the next interval retries from the stored cursor instead of declaring
  completion.
* **No catch-up storm.** Downtime is not debt: the loop is one sweep per
  interval and never replays the intervals it slept through, so an hour
  offline costs one sweep on return, not sixty.

**Not delivered here: reconcile before evidence is served.** The spec's
restart clause ("Offline/unreachable owners leave that phase pending and must
reconcile before serving evidence after restart",
``specs/structured-corpus-maintenance.md:329-330``, and ADR 0099 §2.3) needs
an ordering gate: nothing must answer a historical structured-evidence
request until the retained references reconcile. This module cannot supply
one — its startup sweep runs on a daemon thread while ``uvicorn.run`` serves
in the same call stack, so it withholds nothing and gates nothing. That
requirement stays deferred and is unblocked only by the same P3 producer plus
a reader obeying a withhold flag (P2's own record, "What did NOT ship, and the
blocker", ``docs/validation/2026-10-07-p2-evidence-lifecycle-execution.md``).

The ordering itself is NOT re-implemented here. ``scrub → confirm →
acknowledge`` belongs to the drain (ADR 0099), and this loop only decides
*when* to ask it; it never calls ``advance_invalidation_acknowledgement``
itself, which is the one thing that could acknowledge before the scrub. The
loop holds no store lock across the transport: the drain reads the cursor,
releases, calls the network, and only then opens the write transaction, so a
slow backend cannot block another writer.

The ``fetch`` / ``acknowledge`` pair is the transport seam
:func:`fin.host.workspace_http.bind_invalidation_drain_transport` binds (read
back through
:func:`fin.host.workspace_http.default_invalidation_drain_transport`); the IK
invalidations producer that answers it arrives with P3, and until one is bound
no loop is started at all rather than a loop polling a channel that answers
nothing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fin.host.attachment_prep_worker import DrainCadence, PeriodicDrainWorker
from fin.host.structured_evidence_invalidation import (
    InvalidationDrainOutcome,
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.thread_store import ThreadStore

logger = logging.getLogger(__name__)

#: The ``(fetch, acknowledge)`` pair the drain is defined against: fetch takes
#: the resume cursor and answers a batch; acknowledge takes the checkpoint the
#: drained batch covered. Grouped into one value because they are bound
#: together (one channel, one lifecycle) and always travel as a pair — the
#: drain calls both or the two-phase contract cannot hold.
InvalidationDrainTransport = tuple[
    Callable[[str | None], object], Callable[[str], None]
]

#: The spec's cadence, and the widest gap that still satisfies "drains at least
#: once per minute while online" (``specs/structured-corpus-evidence.md:255``).
#: A configured cadence above this is a configuration that breaks the contract,
#: so it raises instead of quietly draining slower.
INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS: Final[float] = 60.0

INVALIDATION_DRAIN_INTERVAL_SECONDS: Final[float] = 60.0

INVALIDATION_DRAIN_THREAD_NAME: Final[str] = "fin-structured-invalidation-drain"

INVALIDATION_DRAIN_LOG_LABEL: Final[str] = "structured invalidation drain"

#: The consumer the backend records against a grant (D1 §2.4). A CONSTANT is
#: the intended contract here, not a missing configuration: the watermark rows
#: are deployment-scoped in the shared Postgres
#: (``specs/structured-corpus-maintenance.md`` separates them per owner), so one
#: identity yields one watermark per deployment. A second identity would not
#: merely re-scrub the same payloads under a fresh cursor — it would strand the
#: first identity's cursor forever, because nothing drains on it any more.
#: ADR 0097 §2.4 describes the identity as "the trusted consumer identity fin
#: was configured with"; giving it a configuration surface is deferred, and
#: changing this value is a watermark reset, not a config edit.
INVALIDATION_DRAIN_CONSUMER_ID: Final[str] = "fin-default"

#: Attempts kept for the phase read. One entry per sweep, so an unbounded list
#: would grow for the life of the process; the current phase is the last entry
#: and the window only exists so a retry sequence can be read back.
_ATTEMPT_HISTORY: Final[int] = 64


class InvalidationDrainStoppingError(RuntimeError):
    """Start refused while a timed-out stop's drain thread is still alive.

    The singleton guard of
    :func:`fin.host.workspace_http.start_invalidation_drain_worker`: a stop
    whose join times out keeps the live thread registered, and a start over it
    would run two drains in one process.
    """

    code = "structured_invalidation_drain_stopping"


@dataclass(frozen=True)
class InvalidationDrainAttempt:
    """One scheduled drain, as the phase this process actually reached.

    ``drain_completed`` is this process's own observation, recorded after
    ``drain_invalidation_batch`` returned — which is after the scrub committed
    and the acknowledgement was accepted. It is NOT the purge record's
    ``completed`` state: ADR 0099 §2.1 reserves that name for the reconciler
    that writes it, once every configured owner of a revision is scrubbed, and
    nothing in this process may read one owner sweep as that.

    A phase that reads complete while payloads are still unacknowledged is the
    M16 failure the two-phase machine exists to prevent, so the field is only
    set from the drain's return value.
    """

    drain_completed: bool
    events: int = 0
    scrubbed_entries: int = 0
    scrubbed_runs: int = 0
    checkpoint: str = ""
    last_seq: int = 0
    refusal_code: str | None = None


def _check_cadence(interval_s: float) -> None:
    """Refuse a cadence that would break the once-per-minute contract."""
    if not 0 < interval_s <= INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"the invalidation drain cadence must be in "
            f"(0, {INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS}] seconds "
            f"(specs/structured-corpus-evidence.md:255), got {interval_s!r}"
        )


def default_cadence() -> DrainCadence:
    """The shipped cadence: the spec's minute, on the real clock."""
    return DrainCadence(INVALIDATION_DRAIN_INTERVAL_SECONDS)


class StructuredInvalidationDrainWorker(PeriodicDrainWorker):
    """One process's executor for the structured-evidence invalidation drain.

    :class:`PeriodicDrainWorker` owns the singleton guard, the daemon thread
    and the stop-event exit; this subclass binds the spec's cadence and the
    per-attempt phase. ``transport`` is the ``(fetch, acknowledge)`` pair the
    drain is defined against and the seam P3's producer binds — nothing in this
    process reaches the invalidations channel without both halves of it.
    """

    def __init__(
        self,
        store: ThreadStore,
        transport: InvalidationDrainTransport,
        *,
        consumer_id: str = INVALIDATION_DRAIN_CONSUMER_ID,
        cadence: DrainCadence | None = None,
    ) -> None:
        clock = cadence if cadence is not None else default_cadence()
        _check_cadence(clock.interval_s)
        self._store = store
        self._fetch, self._acknowledge = transport
        self._consumer_id = consumer_id
        self._attempts: deque[InvalidationDrainAttempt] = deque(
            maxlen=_ATTEMPT_HISTORY
        )
        self._attempts_lock = threading.Lock()
        super().__init__(
            self.drain_once,
            clock,
            thread_name=INVALIDATION_DRAIN_THREAD_NAME,
            stopping_error=InvalidationDrainStoppingError,
            log_label=INVALIDATION_DRAIN_LOG_LABEL,
            worker_logger=logger,
        )

    def drain_once(self) -> InvalidationDrainAttempt:
        """Drain one batch and record the phase the attempt reached.

        The refusal — and any transport error, which is the ordinary
        unreachable-backend case — is recorded as pending and then re-raised:
        the shared loop logs it with exception context and keeps the interval,
        which is what makes "the next attempt retries" true rather than
        aspirational. Recording without re-raising would swallow the typed
        refusal code the log is the only carrier of.
        """
        try:
            outcome = drain_invalidation_batch(
                self._store,
                consumer_id=self._consumer_id,
                fetch=self._fetch,
                acknowledge=self._acknowledge,
            )
        except Exception as exc:
            self._record(
                InvalidationDrainAttempt(
                    drain_completed=False,
                    refusal_code=(
                        exc.code
                        if isinstance(exc, StructuredInvalidationRefusal)
                        else None
                    ),
                )
            )
            raise
        return self._record(_drained(outcome))

    @property
    def attempts(self) -> tuple[InvalidationDrainAttempt, ...]:
        """The recorded attempts, oldest first; the last is the current phase."""
        with self._attempts_lock:
            return tuple(self._attempts)

    @property
    def last_attempt(self) -> InvalidationDrainAttempt | None:
        """The most recent attempt, or ``None`` before the first sweep."""
        with self._attempts_lock:
            return self._attempts[-1] if self._attempts else None

    def _record(self, attempt: InvalidationDrainAttempt) -> InvalidationDrainAttempt:
        with self._attempts_lock:
            self._attempts.append(attempt)
        return attempt


def _drained(outcome: InvalidationDrainOutcome) -> InvalidationDrainAttempt:
    return InvalidationDrainAttempt(
        drain_completed=True,
        events=outcome.events,
        scrubbed_entries=outcome.scrubbed_entries,
        scrubbed_runs=outcome.scrubbed_runs,
        checkpoint=outcome.checkpoint,
        last_seq=outcome.last_seq,
    )


__all__ = [
    "INVALIDATION_DRAIN_CONSUMER_ID",
    "INVALIDATION_DRAIN_INTERVAL_SECONDS",
    "INVALIDATION_DRAIN_LOG_LABEL",
    "INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS",
    "INVALIDATION_DRAIN_THREAD_NAME",
    "InvalidationDrainAttempt",
    "InvalidationDrainStoppingError",
    "InvalidationDrainTransport",
    "StructuredInvalidationDrainWorker",
    "default_cadence",
]
