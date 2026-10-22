"""The invalidation drain's caller: the cadence that owns the drain (P3's half of P2-I3).

``specs/structured-corpus-evidence.md:255-256`` — "Fin drains at least once per
minute while online, transactionally withholds and scrubs affected stored
snapshot payloads, and acknowledges only afterward" — had a consumer and no
caller. These cases pin what the caller owns, which is *when*, never the
scrub/ack ORDER (that is ADR 0099's, proven in
``test_structured_evidence_invalidation.py`` against the drain itself):

* the loop drains on the CONFIGURED cadence, on a fake clock rather than a
  wall-clock minute;
* the cadence is a CEILING on the period: with a sweep that costs time, drain
  to drain is still within one interval of real (here: fake) elapsed time —
  the sweep's cost comes out of its own interval instead of being added to
  the gap, so a one-minute default still drains once per minute;
* it never drains more often than that cadence (a busy loop would clear every
  ``assert waits == [interval]`` here), and a sweep that overran its interval
  is not a backlog to replay;
* an unreachable backend records the phase PENDING and the next interval
  retries it — no completion is recorded while nothing was acknowledged;
* the acknowledgement never precedes the committed scrub, read from a
  recording transport that captures the store state at ack time;
* a stop mid-drain is clean and leaves the resume cursor where the drain left
  it, so the next process reconciles instead of skipping;
* a restart over the same store does not double-scrub and does not skip a
  redelivered event;
* boot wiring: ``serve()`` starts the drain before binding and stops it
  first, and with no transport bound no thread is acquired at all.

The clock is injected as one seam (:class:`DrainCadence`), so none of these
cases sleep for a minute to assert a minute — and the singleton cases drive
the same seam rather than the real 60-second ``Event.wait``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from fin.host.application.lifetime import RuntimeHandle
from fin.host.attachment.attachment_prep_worker import DrainCadence
from fin.host.evidence.evidence_contract import RETRIEVE_UNAVAILABLE
from fin.host.evidence.structured_evidence import build_stored_record, structured_snapshots_of
from fin.host.evidence.structured_evidence_drain_worker import (
    INVALIDATION_DRAIN_CONSUMER_ID,
    INVALIDATION_DRAIN_INTERVAL_SECONDS,
    INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS,
    INVALIDATION_DRAIN_THREAD_NAME,
    InvalidationDrainStoppingError,
    StructuredInvalidationDrainWorker,
    _ATTEMPT_HISTORY,
)
from fin.host.thread_store import InMemoryThreadStore, RunRecord
from invalidation_batch import build_batch, build_event

_CONSUMER_ID = "fin-drain-scheduler"

_BASE = {
    "schema": "iknow.structured_result",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "query_digest": "sha256:0f1e2d3c",
    "scope_binding": "scope-fin-tenant-7",
    "read_revision": "rev-118",
    "queried_at": "2026-10-03T00:00:00Z",
    "maintenance": {
        "maintenance_revision": "maintenance-rev-12",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "conflict_checks": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    },
}

_ROW = {
    "id": "00000000-0000-4000-8000-000000000001",
    "canonical_name": "Northwind Logistics",
    "type": "organization",
    "display_support_id": "00000000-0000-4000-8000-000000000101",
    "display_disagreement": False,
    "evidence_ref": "evref-0001",
    "evidence_expires_at": "2027-10-03T00:00:00Z",
    "provenance": [
        {
            "support_id": "00000000-0000-4000-8000-000000000201",
            "source_ref": "company-source-1",
            "source_version": "source-version-1",
            "location": {
                "document_id": "example-document",
                "version": "frozen-unit-version",
                "unit": 1,
            },
            "span": {"start": 0, "end": 42},
            "extraction_ref": "extraction-revision-1",
        }
    ],
    "provenance_complete": True,
    "provenance_next_ref": None,
}


def _stored(evidence_ref: str) -> dict[str, Any]:
    row = {**_ROW, "evidence_ref": evidence_ref}
    snapshot = structured_snapshots_of({**_BASE, "rows": [row], "next_cursor": None})[0]
    return build_stored_record(snapshot)


def _record(run_id: str, thread_id: str, *refs: str) -> RunRecord:
    return RunRecord(
        id=run_id,
        thread_id=thread_id,
        question="who is the counterparty",
        status="done",
        structured_evidence=[_stored(ref) for ref in refs],
    )


def _event(evidence_ref: str, seq: int) -> dict[str, Any]:
    return build_event(evidence_ref, seq=seq)


def _batch(
    *events: dict[str, Any], next_checkpoint: str = "ck-2", gap: bool = False
) -> dict[str, Any]:
    return build_batch(*events, next_checkpoint=next_checkpoint, gap=gap)


class _FakeClock:
    """The loop's clock: ``Event.wait`` and ``time.monotonic``, both faked.

    ``waits`` records the interval each tick was asked to honour and ``now`` is
    the monotonic reading the loop schedules against, so a case can measure
    *elapsed time* between two drains rather than counting ticks. ``wait``
    advances ``now`` by exactly the interval it was handed, which is what an
    ideal clock does.

    ``sweep_cost`` is the fake time a drain spends inside one sweep: a case
    models a slow drain by having its transport bill that much per fetch.
    Without it a sweep costs nothing and the sweep-then-wait defect is
    invisible — the gap would measure one interval either way.

    ``stop_after`` bounds the loop: the tick that reaches it returns True, the
    ``Event.wait`` answer for "a stop was requested".
    """

    def __init__(self, *, stop_after: int, sweep_cost: float = 0.0) -> None:
        self.waits: list[float] = []
        self.now = 0.0
        self.stop_after = stop_after
        self.sweep_cost = sweep_cost

    def monotonic(self) -> float:
        return self.now

    def bill_sweep(self) -> None:
        """Charge this drain's cost to the clock, from inside the sweep."""
        self.now += self.sweep_cost

    def __call__(self, _stop_event: threading.Event, interval_s: float) -> bool:
        self.waits.append(interval_s)
        self.now += max(interval_s, 0.0)
        return len(self.waits) >= self.stop_after


class _ParkUntilStoppedClock(_FakeClock):
    """A loop that stays parked until the worker is stopped.

    For the lifecycle cases, where what matters is that the thread is ALIVE
    between ``start`` and ``stop`` — the real cadence does that by sleeping a
    minute. This polls the worker's own stop event instead, so the case keeps
    that posture without a minute of real time and without the tick-bounded
    fake exiting the loop out from under a ``start`` that is still asserting
    the singleton.
    """

    def __init__(self, *, poll_s: float = 0.01) -> None:
        super().__init__(stop_after=0)
        self._poll_s = poll_s

    def __call__(self, stop_event: threading.Event, interval_s: float) -> bool:
        self.waits.append(interval_s)
        self.now += interval_s
        return stop_event.wait(self._poll_s)


class _RecordingTransport:
    """The drain's ``(fetch, acknowledge)`` pair, recorded in call order.

    The same shape as the drain's own transport seam, so a case can assert
    the ORDER of the two calls and, through ``observed_payloads``, what
    storage held at the moment the acknowledgement went out. ``times`` records
    the clock reading at each fetch, which is where a drain begins, so a case
    can measure drain-to-drain elapsed time.
    """

    def __init__(
        self,
        batches: list[object],
        *,
        clock: _FakeClock | None = None,
    ) -> None:
        self._batches = batches
        self._clock = clock
        self.cursors: list[str | None] = []
        self.acks: list[str] = []
        self.times: list[float] = []
        self.observed_payloads: list[list[str]] = []
        self._observe: Callable[[], list[str]] | None = None

    def observe_storage(self, probe: Callable[[], list[str]]) -> None:
        self._observe = probe

    def on_clock(self, clock: _FakeClock) -> None:
        """Bill this transport's fetches to ``clock``, so a drain costs time."""
        self._clock = clock

    def fetch(self, cursor: str | None) -> object:
        self.cursors.append(cursor)
        self.times.append(self._clock.monotonic() if self._clock else 0.0)
        if self._clock is not None:
            self._clock.bill_sweep()
        batch = self._batches.pop(0) if self._batches else _batch()
        if isinstance(batch, Exception):
            raise batch
        return batch

    def acknowledge(self, checkpoint: str) -> None:
        self.acks.append(checkpoint)
        if self._observe is not None:
            self.observed_payloads.append(self._observe())


def _stored_refs(store: InMemoryThreadStore, run_id: str) -> list[str]:
    for thread in list(store.list_runs("thread-1")):
        if thread.id == run_id:
            return [
                row["snapshot"]["evidence_ref"] for row in thread.structured_evidence
            ]
    return []


def _worker(
    store: InMemoryThreadStore,
    transport: _RecordingTransport,
    *,
    interval_s: float | None = None,
    clock: _FakeClock | None = None,
    consumer_id: str = _CONSUMER_ID,
) -> StructuredInvalidationDrainWorker:
    """The worker under test, on the shipped cadence unless one is given.

    ``interval_s=None`` builds the default cadence — the spec's minute on the
    real clock — so a case that asserts the shipped default's behaviour is
    asserting the constant the boot path uses.
    """
    cadence = None if interval_s is None and clock is None else DrainCadence(
        INVALIDATION_DRAIN_INTERVAL_SECONDS if interval_s is None else interval_s,
        wait=clock if clock is not None else threading.Event.wait,
        monotonic=clock.monotonic if clock is not None else time.monotonic,
    )
    if clock is not None:
        transport.on_clock(clock)
    return StructuredInvalidationDrainWorker(
        store,
        (transport.fetch, transport.acknowledge),
        consumer_id=consumer_id,
        cadence=cadence,
    )


# ----- the cadence ---------------------------------------------------------


def test_the_drain_runs_on_the_configured_cadence_while_online() -> None:
    """Startup sweep, then one sweep per configured interval.

    Four ticks at 45s: four drains — the startup one plus three more — and
    never a minute of real time.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    transport = _RecordingTransport([_batch(_event("evref-0001", 1))])
    clock = _FakeClock(stop_after=4)
    worker = _worker(store, transport, interval_s=45.0, clock=clock)

    thread = worker.start()
    thread.join(timeout=10.0)  # the clock bounds the loop; no polling needed
    assert not thread.is_alive()

    assert clock.waits == [45.0, 45.0, 45.0, 45.0], "the configured interval, every tick"
    assert len(worker.attempts) == 4
    assert all(attempt.drain_completed for attempt in worker.attempts)
    assert worker.attempts[0].scrubbed_entries == 1
    assert transport.acks == ["ck-2"] * 4


def test_the_shipped_default_drains_at_least_once_per_minute_with_a_slow_sweep() -> None:
    """The spec's clause, on the shipped default: "drains at least once per
    minute while online" (``specs/structured-corpus-evidence.md:255-256``).

    Each drain here costs 20 seconds — it fetches from a backend, scrubs in a
    transaction and acknowledges — and the interval is the default minute.
    Drain to drain must still be at most one minute: the sweep's cost comes
    out of its own interval, not on top of the next one. Sweep-then-sleep
    makes this 80 seconds per drain and fails; so does any loop that lets the
    cost accumulate instead of amortizing it.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    clock = _FakeClock(stop_after=4, sweep_cost=20.0)
    transport = _RecordingTransport(
        [_batch(_event("evref-0001", 1))], clock=clock
    )
    # No interval_s: this is the cadence the boot path ships.
    worker = _worker(store, transport, clock=clock)

    thread = worker.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert len(transport.times) == 4, "four drains, four fetch calls"
    gaps = [later - earlier for earlier, later in zip(transport.times, transport.times[1:])]
    assert gaps == [60.0, 60.0, 60.0], (
        f"drain to drain must not exceed the interval; measured {gaps}"
    )
    assert max(gaps) <= INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS
    assert clock.now == 240.0, (
        "four drains cost four minutes of the clock; the 20s each spent "
        "fetching came out of its own interval, not on top of the next"
    )
    assert all(attempt.drain_completed for attempt in worker.attempts)


def test_the_drain_never_runs_more_often_than_the_configured_cadence() -> None:
    """The floor of the same contract: exactly one sweep per interval.

    A loop that re-swept on a busy spin, or that slept for less than its own
    cadence, records more attempts than ticks — the count here is pinned
    against the tick count, and the recorded intervals against the
    configured one.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    transport = _RecordingTransport([])
    clock = _FakeClock(stop_after=3)
    worker = _worker(store, transport, interval_s=60.0, clock=clock)

    thread = worker.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert clock.waits == [60.0, 60.0, 60.0]
    assert len(worker.attempts) == 3, "one sweep per tick, and none between ticks"
    assert len(transport.cursors) == 3


def test_an_overrunning_sweep_is_not_a_backlog_to_replay() -> None:
    """Downtime is not debt, measured in elapsed time rather than tick counts.

    Each drain here costs an hour of the clock — the shape of a worker
    returning after a long outage — against a one-minute cadence. The loop
    must not replay the sixty intervals it slept through: it sweeps once, and
    because the sweep overran the whole interval there is nothing left to wait
    out. A loop that swept once per missed interval would burn 60 drains here
    instead of 3, and one that slept its interval on top of the sweep would
    measure an hour per drain rather than running straight on.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    clock = _FakeClock(stop_after=3, sweep_cost=3600.0)
    transport = _RecordingTransport([], clock=clock)
    worker = _worker(store, transport, interval_s=60.0, clock=clock)

    thread = worker.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert len(worker.attempts) == 3, "one sweep per tick, never one per missed interval"
    assert clock.now == 10800.0, "three hours of the clock, three sweeps, no replay"
    assert clock.waits == [0.0, 0.0, 0.0], (
        "an overrun leaves nothing to wait out; it is not a queue of intervals"
    )


def test_a_cadence_above_the_specs_minute_is_refused_at_construction() -> None:
    """The ceiling is a contract, not a hint: 61s would drain slower than
    "at least once per minute" promises."""
    store = InMemoryThreadStore()
    transport = _RecordingTransport([])

    with pytest.raises(ValueError):
        _worker(
            store,
            transport,
            interval_s=INVALIDATION_DRAIN_MAX_INTERVAL_SECONDS + 1.0,
        )
    with pytest.raises(ValueError):
        _worker(store, transport, interval_s=0.0)


# ----- unreachable backend leaves the phase pending ------------------------


def test_an_unreachable_backend_leaves_the_phase_pending_and_the_next_tick_retries() -> None:
    """Maintenance spec :328-330: an unreachable owner leaves its phase
    pending, and a retry re-enters it — never recorded as complete."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    run_id = "run-1"
    store.append_run(_record(run_id, "thread-1", "evref-0001"))
    transport = _RecordingTransport(
        [
            ConnectionError("backend unreachable"),
            _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        ]
    )
    worker = _worker(
        store, transport, interval_s=60.0, clock=_FakeClock(stop_after=2)
    )

    worker.start()
    try:
        _wait_until(
            lambda: len(worker.attempts) >= 2
            and worker.last_attempt is not None
            and worker.last_attempt.drain_completed
        )
    finally:
        worker.stop(timeout=10.0)

    first, second = worker.attempts
    assert first.drain_completed is False, "an unreachable backend is not a completion"
    assert first.refusal_code is None, (
        "a transport error is not a typed contract refusal"
    )
    assert second.drain_completed is True
    assert second.scrubbed_entries == 1
    # The retry resumed from the empty stored cursor and did the work the
    # unreachable attempt never could.
    assert transport.cursors == [None, None]
    assert transport.acks == ["ck-2"]
    assert _stored_refs(store, run_id) == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"


def test_a_refused_batch_records_the_typed_code_and_stays_pending() -> None:
    """A ``gap`` refusal keeps its typed code in the phase read."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    transport = _RecordingTransport(
        [_batch(_event("evref-0001", 1), next_checkpoint="ck-2", gap=True)]
    )
    worker = _worker(store, transport, interval_s=60.0, clock=_FakeClock(stop_after=1))

    worker.start()
    try:
        _wait_until(lambda: worker.last_attempt is not None)
    finally:
        worker.stop(timeout=10.0)

    attempt = worker.last_attempt
    assert attempt is not None
    assert attempt.drain_completed is False
    assert attempt.refusal_code == RETRIEVE_UNAVAILABLE
    assert transport.acks == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""


# ----- scrub before ack, from the caller's side ---------------------------


def test_the_acknowledgement_never_precedes_the_committed_scrub() -> None:
    """A recording transport reads storage at ack time: the payload must
    already be gone. This is the ADR 0099 order as the caller drives it."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    run_id = "run-1"
    store.append_run(_record(run_id, "thread-1", "evref-0001", "evref-0002"))
    transport = _RecordingTransport([_batch(_event("evref-0001", 1))])
    transport.observe_storage(lambda: _stored_refs(store, run_id))
    worker = _worker(store, transport, interval_s=60.0, clock=_FakeClock(stop_after=1))

    worker.start()
    try:
        _wait_until(lambda: worker.last_attempt is not None)
    finally:
        worker.stop(timeout=10.0)

    assert transport.observed_payloads == [["evref-0002"]], (
        "the acknowledged batch's payload must be out of storage before the ack"
    )
    assert transport.acks == ["ck-2"]
    assert _stored_refs(store, run_id) == ["evref-0002"]


def test_a_failed_attempt_acknowledges_nothing_and_records_pending() -> None:
    """The caller's own guard against a future edit that acks from the loop
    body rather than through the drain: the phase a failed attempt reaches is
    pending, and it sent no acknowledgement to reach it with."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    transport = _RecordingTransport([ConnectionError("backend unreachable")])
    worker = _worker(store, transport, interval_s=60.0)

    with pytest.raises(ConnectionError):
        worker.drain_once()

    attempt = worker.last_attempt
    assert attempt is not None
    assert attempt.drain_completed is False
    assert transport.acks == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).applied_checkpoint == ""
    assert _stored_refs(store, "run-1") == ["evref-0001"]


# ----- shutdown ------------------------------------------------------------


def test_a_stop_mid_drain_is_clean_and_leaves_recoverable_state() -> None:
    """Shutdown must not hang the caller, and must not corrupt what the
    in-flight batch left behind.

    What this actually proves, stated so the assertions below match it: the
    stop does NOT block the caller and does NOT cut the batch short. The join
    times out while the fetch is gated, and once the fetch is released the
    drained batch commits normally — scrub then acknowledge — so the recorded
    attempt IS complete and the stored cursor HAS moved. Recoverability is
    covered by the next case, where the acknowledgement itself fails.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    run_id = "run-1"
    store.append_run(_record(run_id, "thread-1", "evref-0001"))
    entered = threading.Event()
    release = threading.Event()

    def fetch(cursor: str | None) -> object:
        entered.set()
        release.wait(10.0)
        return _batch(_event("evref-0001", 1), next_checkpoint="ck-2")

    acks: list[str] = []
    worker = StructuredInvalidationDrainWorker(
        store,
        (fetch, acks.append),
        consumer_id=_CONSUMER_ID,
    )
    thread = worker.start()
    assert entered.wait(10.0)

    # A stop while the sweep is in flight returns on its timeout rather than
    # hanging the caller, and leaves the sweep to finish on its own.
    worker.stop(timeout=0.1)
    assert thread.is_alive()

    release.set()
    _wait_until(lambda: not thread.is_alive())
    worker.stop(timeout=10.0)

    assert worker.last_attempt is not None
    assert worker.last_attempt.drain_completed is True
    assert acks == ["ck-2"], "the released in-flight batch landed, not vanished"
    # Recoverable: a fresh store instance reads the cursor the drain wrote.
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"


def test_a_drain_cut_before_its_acknowledgement_leaves_the_cursor_before_the_batch() -> None:
    """A transport that dies at ack time leaves the resume cursor where it
    was, so the next process redelivers rather than skips the event."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    run_id = "run-1"
    store.append_run(_record(run_id, "thread-1", "evref-0001"))

    def dead(checkpoint: str) -> None:
        raise RuntimeError("transport died after the scrub committed")

    worker = StructuredInvalidationDrainWorker(
        store,
        (
            lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
            dead,
        ),
        consumer_id=_CONSUMER_ID,
    )

    with pytest.raises(RuntimeError):
        worker.drain_once()

    stored = store.read_invalidation_checkpoint(_CONSUMER_ID)
    assert stored.applied_checkpoint == "ck-2", "the scrub did commit"
    assert stored.acked_checkpoint == "", "so the resume cursor did not move"
    assert worker.last_attempt is not None and worker.last_attempt.drain_completed is False
    assert _stored_refs(store, run_id) == []


def test_stop_is_an_idempotent_noop_before_any_start() -> None:
    from fin.host import workspace_http

    before = {thread.name for thread in threading.enumerate()}
    workspace_http.stop_invalidation_drain_worker()
    workspace_http.stop_invalidation_drain_worker(timeout=1.0)
    assert {thread.name for thread in threading.enumerate()} == before


# ----- restart -------------------------------------------------------------


def test_a_restart_does_not_double_scrub_or_skip_a_redelivered_event() -> None:
    """Two processes over the same store: the crash window (scrub committed,
    ack lost) redelivers, the second drain scrubs nothing again and still
    lands the cursor — neither a second scrub of the same payload nor a skip
    of the event."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    run_id = "run-1"
    store.append_run(_record(run_id, "thread-1", "evref-0001"))

    def dead_transport(checkpoint: str) -> None:
        raise RuntimeError("transport died after the scrub committed")

    crashed = StructuredInvalidationDrainWorker(
        store,
        (
            lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
            dead_transport,
        ),
        consumer_id=_CONSUMER_ID,
        cadence=DrainCadence(
            60.0,
            wait=_FakeClock(stop_after=1),
            monotonic=time.monotonic,
        ),
    )
    crashed.start()
    with pytest.raises(RuntimeError):
        crashed.drain_once()

    stored = store.read_invalidation_checkpoint(_CONSUMER_ID)
    assert stored.applied_checkpoint == "ck-2"
    assert stored.acked_checkpoint == ""
    assert _stored_refs(store, run_id) == []

    # The restart: a NEW worker object over the same store, redelivery
    # included, exactly as a fresh process would see it.
    redelivered = _RecordingTransport(
        [_batch(_event("evref-0001", 1), next_checkpoint="ck-2")]
    )
    restarted = _worker(store, redelivered, interval_s=60.0)
    attempt = restarted.drain_once()

    assert attempt.drain_completed is True
    assert attempt.scrubbed_entries == 0, "the payload was already gone"
    assert redelivered.cursors == [None], "the resume cursor is the acknowledged one"
    assert redelivered.acks == ["ck-2"]
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"

    # And the event after it is neither skipped nor double-drained.
    following = _RecordingTransport(
        [_batch(_event("evref-0002", 2), next_checkpoint="ck-3")]
    )
    third = _worker(store, following, interval_s=60.0)
    third.drain_once()
    assert following.cursors == ["ck-2"]
    assert following.acks == ["ck-3"]


# ----- lifecycle and boot wiring ------------------------------------------


def test_start_is_singleton_safe_daemon_and_restart_after_clean_stop() -> None:
    from fin.host import workspace_http

    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    transport = _RecordingTransport([])
    # The clock seam: the loop parks until ``stop()`` sets its event, as the
    # real 60-second cadence does, without spending a real minute doing it.
    cadence = DrainCadence(
        60.0, wait=_ParkUntilStoppedClock(), monotonic=time.monotonic
    )

    first = workspace_http.start_invalidation_drain_worker(
        store=store,
        transport=(transport.fetch, transport.acknowledge),
        cadence=cadence,
    )
    assert first is not None
    second = workspace_http.start_invalidation_drain_worker(
        store=store,
        transport=(transport.fetch, transport.acknowledge),
        cadence=cadence,
    )
    assert second is first
    assert first.name == INVALIDATION_DRAIN_THREAD_NAME
    assert first.daemon

    workspace_http.stop_invalidation_drain_worker(timeout=10.0)
    assert not first.is_alive()

    third = workspace_http.start_invalidation_drain_worker(
        store=store,
        transport=(transport.fetch, transport.acknowledge),
        cadence=cadence,
    )
    assert third is not None and third is not first
    assert third.is_alive()
    workspace_http.stop_invalidation_drain_worker(timeout=10.0)
    workspace_http.stop_invalidation_drain_worker(timeout=1.0)


def test_start_over_a_timed_out_stop_refuses_until_the_old_drain_dies() -> None:
    """The singleton guard's own typed refusal: a stop whose join timed out
    keeps the live thread registered, and a start over it would run two drains
    in one process."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    entered = threading.Event()
    release = threading.Event()

    def fetch(_cursor: str | None) -> object:
        entered.set()
        release.wait(10.0)
        return _batch()

    worker = StructuredInvalidationDrainWorker(
        store,
        (fetch, lambda _checkpoint: None),
    )
    try:
        first = worker.start()
        assert entered.wait(10.0)

        worker.stop(timeout=0.1)  # gated mid-drain: the join times out
        assert first.is_alive()
        with pytest.raises(InvalidationDrainStoppingError):
            worker.start()

        release.set()
        _wait_until(lambda: not first.is_alive())
        second = worker.start()
        assert second is not first, "a start once the old thread exited is allowed"
        worker.stop(timeout=10.0)
    finally:
        release.set()
        worker.stop(timeout=10.0)


def test_start_with_no_transport_bound_acquires_no_thread(monkeypatch: Any) -> None:
    """P3's producer owns the channel; without it there is nothing to call,
    so the boot start is a no-op rather than a loop that refuses forever.

    The lazy store default is load-bearing here: no transport means the
    function returns before it ever asks for a store, so a build whose drain
    never runs never opens the Postgres pool."""
    from fin.host import workspace_http
    from fin.host import thread_store

    monkeypatch.setattr(
        workspace_http, "default_invalidation_drain_transport", lambda: None
    )

    def _never(store: Any = None) -> Any:
        raise AssertionError("the no-transport path must not resolve a store")

    monkeypatch.setattr(thread_store, "default_thread_store", _never)
    before = {thread.name for thread in threading.enumerate()}

    assert workspace_http.start_invalidation_drain_worker() is None

    assert {thread.name for thread in threading.enumerate()} == before


def test_app_owned_invalidation_start_without_transport_returns_idempotent_stop(
    monkeypatch: Any,
) -> None:
    """An app with no producer acquires neither a drain nor a global worker."""
    from fin.host import workspace_http

    monkeypatch.setattr(
        workspace_http, "default_invalidation_drain_transport", lambda: None
    )
    monkeypatch.setattr(
        workspace_http,
        "StructuredInvalidationDrainWorker",
        lambda *_args, **_kwargs: pytest.fail("no transport must not build a worker"),
    )
    before = {thread.name for thread in threading.enumerate()}

    stop = workspace_http.start_application_invalidation_drain_worker(
        store=object()
    )
    stop()
    stop()

    assert {thread.name for thread in threading.enumerate()} == before


def test_a_bound_transport_is_used_by_a_start_without_injected_seams() -> None:
    """The transport seam, end to end: bind once, start with no transport
    argument, and the drain calls THAT pair.

    Asserted through the transport's own record rather than through the fact
    that a thread appeared: the bound ``fetch`` is the one that receives the
    resume cursor and the bound ``acknowledge`` is the one the scrubbed
    checkpoint goes to. A start wired to a stub pair — or to no pair — leaves
    this recording empty while still returning a live thread.
    """
    from fin.host import workspace_http

    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    transport = _RecordingTransport([_batch(_event("evref-0001", 1))])
    original = workspace_http.default_invalidation_drain_transport()
    workspace_http.bind_invalidation_drain_transport(
        transport.fetch, transport.acknowledge
    )
    try:
        assert workspace_http.default_invalidation_drain_transport() is not None
        thread = workspace_http.start_invalidation_drain_worker(
            store=store,
            cadence=DrainCadence(
                60.0, wait=_FakeClock(stop_after=1), monotonic=time.monotonic
            ),
        )
        assert thread is not None
        workspace_http.stop_invalidation_drain_worker(timeout=10.0)
    finally:
        from fin.host import workspace_http as module

        module._invalidation_drain_transport = original

    assert transport.cursors == [None], (
        "the drain called the bound fetch, with the resume cursor it read"
    )
    assert transport.acks == ["ck-2"], "and acknowledged through the bound pair"
    assert store.read_invalidation_checkpoint(
        INVALIDATION_DRAIN_CONSUMER_ID
    ).acked_checkpoint == "ck-2"


def test_the_worker_records_under_the_backend_s_consumer_id() -> None:
    """One consumer per DEPLOYMENT: the watermark rows are deployment-scoped,
    so a second identity would strand the first identity's cursor."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    transport = _RecordingTransport([_batch(_event("evref-0001", 1))])
    worker = StructuredInvalidationDrainWorker(
        store,
        (transport.fetch, transport.acknowledge),
    )

    worker.drain_once()

    stored = store.read_invalidation_checkpoint(INVALIDATION_DRAIN_CONSUMER_ID)
    assert stored.acked_checkpoint == "ck-2"


def test_the_attempt_history_is_bounded_and_keeps_the_newest() -> None:
    """The phase read is a window, not a leak: one entry per sweep, capped,
    and the current phase is always the last entry."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    transport = _RecordingTransport([])
    worker = StructuredInvalidationDrainWorker(
        store,
        (transport.fetch, transport.acknowledge),
    )

    for _ in range(_ATTEMPT_HISTORY + 7):
        worker.drain_once()

    assert _ATTEMPT_HISTORY == 64, "the bound this window is sized against"
    assert len(worker.attempts) == _ATTEMPT_HISTORY, (
        "a long-lived process does not grow the attempt log without limit"
    )
    assert worker.last_attempt is worker.attempts[-1]
    assert worker.last_attempt.drain_completed is True


def test_serve_starts_the_drain_before_binding_and_stops_it_first(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Boot owns the loop: no thread at import, ``serve()`` starts it before
    uvicorn binds and stops it first, and it receives the boot-resolved store
    (the same instance the other workers scrub through)."""
    import fin.corpus.ingest_storage as ingest_storage
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module
    import uvicorn

    events: list[str] = []
    store_stub = object()
    service_stub = object()

    # The boot refuses an undeclared durable checkpointer before it opens
    # anything, so this deployment has to state one. Nothing connects to
    # it: the compile seam is the only thing that would.
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setattr(composition_module, "apply_operator_settings", lambda: None)
    monkeypatch.setattr(composition_module, "attachment_budgets", lambda: None)
    monkeypatch.setattr(ingest_storage, "preflight_ingest_storage", lambda: None)
    monkeypatch.setattr(composition_module, "publish_knowledge_mcp", lambda: None)
    # The boot seam is the closeable runtime handle, not the graph-only
    # compile it delegates to, so that is what has to be stubbed here.
    monkeypatch.setattr(
        composition_module,
        "_compile_http_runtime",
        lambda **_kwargs: RuntimeHandle(None),
    )
    monkeypatch.setattr(composition_module, "default_thread_store", lambda: store_stub)
    monkeypatch.setattr(composition_module, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        composition_module, "default_attachment_service", lambda: service_stub
    )
    monkeypatch.setattr(composition_module, "start_application_ingest_worker", lambda: None)
    monkeypatch.setattr(
        composition_module, "start_application_attachment_prep_worker", lambda _service: None
    )
    monkeypatch.setattr(
        composition_module, "start_application_attachment_resume_worker", lambda **_kw: None
    )

    drain_kwargs: dict[str, Any] = {}

    def _drain_start(**kwargs: Any) -> None:
        events.append("drain_start")
        drain_kwargs.update(kwargs)

    monkeypatch.setattr(composition_module, "start_application_invalidation_drain_worker", _drain_start)
    monkeypatch.setattr(
        composition_module,
        "stop_invalidation_drain_worker",
        lambda: events.append("drain_stop"),
    )
    monkeypatch.setattr(composition_module, "stop_attachment_resume_worker", lambda: None)
    monkeypatch.setattr(composition_module, "stop_attachment_prep_worker", lambda: None)
    monkeypatch.setattr(composition_module, "stop_ingest_worker", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: events.append("serve"))

    http_module.serve()

    assert events == ["drain_start", "serve", "drain_stop"]
    assert drain_kwargs == {"store": store_stub}


# ----- helpers -------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll until ``predicate`` holds; the fake clock does not yield."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"condition did not hold within {timeout}s")
