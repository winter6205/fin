"""Identity-bound cancellation and admission for in-flight workspace runs.

The run owns the synchronous graph and its per-thread lock. Stop signals
cooperative graph exit and cancel external async calls; graph cleanup still
runs on the owner thread before admission is released.

Why the registry never acquires the graph lock: a stop is only
meaningful while a run holds that lock, so a stop that waited for it
could never fire.

Admission lives here for the mirror-image reason: it is the OTHER side
of that lock's rule, and the door must hold it before it reads thread
state, not after the graph takes its own lock.
"""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class StopToken:
    """The stop flag for one ``(thread_id, run_id)`` pair.

    Handed to the executor and scoped to each graph iterator operation.
    """

    thread_id: str
    run_id: str
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _reason_guard: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    _reason: str | None = field(default=None, init=False, repr=False, compare=False)

    def stop(self, *, reason: str = "user_stop") -> None:
        with self._reason_guard:
            if self._reason is None:
                object.__setattr__(self, "_reason", reason)
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._event

    @property
    def reason(self) -> str | None:
        with self._reason_guard:
            return self._reason


class RunAdmissionError(RuntimeError):
    """Another admitted run already owns this thread.

    The typed refusal of the door, distinct from the graph's own
    ``ConcurrentRunError`` because it fires BEFORE any graph work: the
    caller learns its turn was never admitted rather than learning it
    was admitted and then lost a race. The transport maps both to the
    one 409 ``concurrent_run`` answer.
    """


@dataclass(frozen=True, slots=True)
class RunCounts:
    """How many runs this process is in each state right now.

    Counts and nothing else: no thread, no run id, no elapsed figure, no
    question or answer. A boot-scoped surface can publish how busy the
    process is without publishing anything about one turn.
    """

    active: int
    paused: int
    suspected_stuck: int


@dataclass(frozen=True, slots=True)
class StopRequestOutcome:
    """What the stop door reached at its linearized observation point."""

    run_id: str | None
    pending: bool


@dataclass(slots=True)
class _LiveRun:
    """One thread's live run, as the status counts read it.

    ``last_progress`` is written on every observed event, so it is the
    same instant the executor's own stage ledger starts its no-progress
    clock from. A run paused for the operator keeps its entry and its
    clock — a wait nobody bounds is not a run failing to progress.
    """

    run_id: str
    stuck_after_s: float
    last_progress: float
    paused: bool = False


class RunAdmission:
    """One admitted run's claim on its thread.

    Released exactly once, by whoever holds it — the door on an early
    failure, the response when the stream ends. It owns no graph
    state, so releasing it cannot strand a lock the run still needs.
    """

    __slots__ = (
        "_lock",
        "_owner",
        "thread_id",
        "_released",
        "_guard",
        "_stop_reason",
        "_run_started",
        "_stop_pending",
    )

    def __init__(
        self,
        lock: threading.Lock,
        thread_id: str,
        *,
        owner: RunControl,
        stop_pending: bool,
    ) -> None:
        self._lock = lock
        self._owner = owner
        self.thread_id = thread_id
        self._released = False
        self._stop_reason: str | None = None
        self._run_started = False
        self._stop_pending = stop_pending
        # WHY a second lock for a flag: two finalizers on one claim are
        # genuinely possible — the run generator and the response both
        # release, and a transport failure can overlap them. Check-then-
        # act without this would let the second unlock a lock the first
        # already unlocked.
        self._guard = threading.Lock()

    def release(self) -> None:
        # The unlock rides INSIDE the guard, so the claim is never
        # observed released before the lock it owns is actually free.
        with self._guard:
            if self._released:
                return
            self._owner._release_admission(self)
            self._released = True


class RunControl:
    """At most one stoppable run per thread, mirroring the graph lock.

    One instance per mounted router (not a module global) so two test
    apps — or two mounts in one process — cannot signal each other's
    runs.

    It also owns ADMISSION for the same thread, because that is the
    same authority seen from the other side: a run is stoppable and
    admitted per ``thread_id``, and a process-global registry would let
    two mounted apps refuse each other's threads. Admission is
    deliberately NOT the graph's own lock: that lock is taken lazily
    inside ``graph.stream``, so probing it admits two callers who then
    both read thread state, both accept an input, and only then does
    one lose — after its acceptance was already computed from a
    snapshot nobody committed.

    And it is what this process is RUNNING: the same per-thread
    authority records each run's lifecycle as local state, which is the
    only thing the status counts may read. That state never leaves this
    process and never waits on a graph, MCP or checkpoint lock — a
    status answer that had to take one of those would be the first
    thing to stop answering during the incident it exists to report.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, StopToken] = {}
        self._pending_stop_claims: dict[str, RunAdmission] = {}
        self._clock = clock
        self._runs: dict[str, _LiveRun] = {}
        # WHY weak values and not a pop: a lock must stay the SAME lock
        # for as long as anyone still holds it. Dropping the entry while
        # a live claim references that lock would let the next caller
        # mint a second one and both would believe they own the thread.
        # Held weakly, the entry dies only when no claim references it —
        # which is exactly when a fresh lock is safe.
        self._admissions: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )

    def begin(
        self,
        thread_id: str,
        run_id: str,
        *,
        admission: RunAdmission | None = None,
    ) -> StopToken:
        """Register a run and transfer any stop latched on its admission."""
        token = StopToken(thread_id=thread_id, run_id=run_id)
        with self._lock:
            if admission is not None and admission._stop_pending:
                if self._pending_stop_claims.get(thread_id) is not admission:
                    raise RunAdmissionError(
                        f"run admission is no longer current for thread_id={thread_id!r}"
                    )
                admission._run_started = True
                if admission._stop_reason is not None:
                    token.stop(reason=admission._stop_reason)
            self._tokens[thread_id] = token
        return token

    def admit(
        self, thread_id: str, *, stop_pending: bool = False
    ) -> RunAdmission:
        """Claim ``thread_id`` for one run, or refuse without waiting.

        Non-blocking on purpose: the door answers this refusal before
        the response headers are flushed, so a caller sees a plain 409
        instead of a stream that fails one frame in. ``stop_pending``
        exposes an admitted send to Stop before its token exists; the
        claim owns that latch until release or run start.
        """
        with self._lock:
            lock = self._admissions.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._admissions[thread_id] = lock
            if not lock.acquire(blocking=False):
                raise RunAdmissionError(
                    f"another admitted run owns thread_id={thread_id!r}"
                )
            admission = RunAdmission(
                lock, thread_id, owner=self, stop_pending=stop_pending
            )
            if stop_pending:
                self._pending_stop_claims[thread_id] = admission
            return admission

    def _release_admission(self, admission: RunAdmission) -> None:
        """Remove this claim's pending-stop target before freeing its lock."""
        with self._lock:
            current = self._pending_stop_claims.get(admission.thread_id)
            if current is admission:
                del self._pending_stop_claims[admission.thread_id]
            admission._lock.release()

    def stop_current(
        self, thread_id: str, *, reason: str = "user_stop"
    ) -> StopRequestOutcome | None:
        """Stop the current token or latch a stop on this admitted send.

        Target selection and cancellation happen under the registry lock,
        so the HTTP door cannot observe one run and then accidentally stop
        a newer admission after the first run ends.
        """
        with self._lock:
            token = self._tokens.get(thread_id)
            if token is not None:
                token.stop(reason=reason)
                return StopRequestOutcome(run_id=token.run_id, pending=False)
            admission = self._pending_stop_claims.get(thread_id)
            if admission is None or admission._run_started:
                return None
            if admission._stop_reason is None:
                admission._stop_reason = reason
            return StopRequestOutcome(run_id=None, pending=True)

    def request_stop(
        self,
        thread_id: str,
        *,
        expected_run_id: str | None = None,
        reason: str = "user_stop",
    ) -> str | None:
        """Flip the flag for ``thread_id``; return the stopped run id.

        ``None`` means no run is registered — the route answers 204
        (nothing to stop) rather than inventing a failure.
        """
        with self._lock:
            token = self._tokens.get(thread_id)
            if token is None or (
                expected_run_id is not None and token.run_id != expected_run_id
            ):
                return None
        token.stop(reason=reason)
        return token.run_id

    def stop_all(self, *, reason: str = "application_shutdown") -> tuple[str, ...]:
        """Stop every live run; return the run ids that took the flag.

        A shutdown must not wait on a run, so this flips every token
        under the registry lock and returns: each run then unwinds on its
        own thread through the same cooperative exit an operator Stop
        uses. Tokens are copied out under the lock and stopped outside it,
        because ``StopToken.stop`` takes the run's own lock and a stop
        must never be able to block the registry.

        The reason is recorded on tokens that had none, so a turn
        abandoned by shutdown reports ``application_shutdown`` rather
        than the default a bare ``stop()`` would stamp.
        """
        with self._lock:
            tokens = tuple(self._tokens.values())
        for token in tokens:
            token.stop(reason=reason)
        return tuple(token.run_id for token in tokens)

    def end(self, token: StopToken) -> None:
        """Deregister ``token``'s run once its stream is finished.

        Identity-checked: a late ``end`` from a previous run must not
        deregister the run that replaced it, or the operator would
        press Stop on a live run and nothing would happen.
        """
        with self._lock:
            current = self._tokens.get(token.thread_id)
            if current is token:
                del self._tokens[token.thread_id]

    def active_run_id(self, thread_id: str) -> str | None:
        with self._lock:
            token = self._tokens.get(thread_id)
        return None if token is None else token.run_id

    # ----- what this process is running -----

    def executing(self, thread_id: str, run_id: str, *, stuck_after_s: float) -> None:
        """Open ``run_id``'s execution window on ``thread_id``.

        Any window this thread already had is replaced: a run that pauses
        and is then continued is one turn resuming, and the count must
        describe the run that is executing now rather than accumulate the
        run that is waiting.
        """
        with self._lock:
            self._runs[thread_id] = _LiveRun(
                run_id=run_id,
                stuck_after_s=stuck_after_s,
                last_progress=self._clock(),
            )

    def progress(self, thread_id: str) -> None:
        """One observed event: the run's no-progress clock starts over."""
        with self._lock:
            run = self._runs.get(thread_id)
            if run is not None and not run.paused:
                run.last_progress = self._clock()

    def paused(self, thread_id: str, run_id: str) -> None:
        """This run is now waiting on the operator, not on its own work."""
        with self._lock:
            run = self._runs.get(thread_id)
            if run is not None and run.run_id == run_id:
                run.paused = True

    def finished(self, thread_id: str, run_id: str) -> None:
        """Close ``run_id``'s window, unless a later run replaced it."""
        with self._lock:
            run = self._runs.get(thread_id)
            if run is not None and run.run_id == run_id:
                del self._runs[thread_id]

    def forget(self, thread_id: str) -> None:
        """Drop whatever this thread was doing, because it no longer exists.

        Unconditional on purpose: the thread row is gone, so there is no
        run left to wait for and an entry nobody closes would report a
        paused turn forever.
        """
        with self._lock:
            self._runs.pop(thread_id, None)

    def counts(self) -> RunCounts:
        """Active, paused and suspected-stuck, from local state alone."""
        with self._lock:
            runs = list(self._runs.values())
            now = self._clock()
        active = 0
        paused = 0
        stuck = 0
        for run in runs:
            if run.paused:
                # A deliberate wait is unbounded by design — the operator
                # owns its clock — so it is never suspected-stuck no
                # matter how long the turn has sat there.
                paused += 1
                continue
            active += 1
            if now - run.last_progress > run.stuck_after_s:
                stuck += 1
        return RunCounts(active=active, paused=paused, suspected_stuck=stuck)


__all__ = [
    "RunAdmission",
    "RunAdmissionError",
    "RunControl",
    "RunCounts",
    "StopRequestOutcome",
    "StopToken",
]
