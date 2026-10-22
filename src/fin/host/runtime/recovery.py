"""Safe-recovery facts for one run.

The seam answers one question: may this run be resumed, must it be
reconciled, or is it over? Lease expiry alone never proves that an external
effect failed, so the projection only reports ``proven_safe`` when every
effect the run started is provably finished; an unpaired effect keeps the
run in ``reconciliation_required`` and out of any replay path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from fin.host.runtime.schema import (
    ATTEMPT_ACTIVE,
    EFFECT_COMPLETED_EVENT,
    EFFECT_STARTED_EVENT,
    RECOVERY_INTERRUPTED,
    RECOVERY_LEASE_ACTIVE,
    RECOVERY_LEASE_EXPIRED_WITHOUT_EFFECT,
    RECOVERY_NO_EFFECT_STARTED,
    RECOVERY_PROVEN_SAFE,
    RECOVERY_RECONCILIATION_REQUIRED,
    RECOVERY_RUN_ABANDONED,
    RECOVERY_RUN_CANCELLED,
    RECOVERY_RUN_INTERRUPTED,
    RECOVERY_RUN_STATE_UNKNOWN,
    RECOVERY_TERMINAL_COMMITTED,
    RECOVERY_THREAD_CANCELLED,
    RECOVERY_THREAD_TOMBSTONED,
    RECOVERY_UNCERTAIN_EFFECT,
    RUN_ABANDONED,
    RUN_CANCELLED,
    RUN_INTERRUPTED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_WAITING,
    TERMINAL_RUN_STATES,
    RecoveryFacts,
    RecoveryVerdict,
    RunRecoverySnapshot,
    UnknownOperation,
)
from fin.host.runtime.store import PostgresRuntimeStore


def open_effect_keys(events: tuple[Any, ...]) -> tuple[str, ...]:
    """The effects a run started and never proved finished.

    Effects are paired by their stable identity, not by order, so a late
    completion still closes its own effect and never closes another's.
    """
    pending: dict[str, bool] = {}
    unkeyed = 0
    for event in events:
        if event.kind not in (EFFECT_STARTED_EVENT, EFFECT_COMPLETED_EVENT):
            continue
        key = None
        if isinstance(event.payload, Mapping):
            raw = event.payload.get("effect_key")
            if isinstance(raw, str) and raw:
                key = raw
        if event.kind == EFFECT_STARTED_EVENT:
            if key is None:
                unkeyed += 1
            else:
                pending[key] = True
        elif key is not None:
            pending.pop(key, None)
    keys = tuple(sorted(key for key, active in pending.items() if active))
    if unkeyed:
        keys = (*keys, *(f"unkeyed-{index}" for index in range(unkeyed)))
    return keys


def project_recovery_facts(
    snapshot: RunRecoverySnapshot, *, now: datetime | None = None
) -> RecoveryFacts:
    """Classify one run's durable facts into a recovery verdict."""
    moment = now if now is not None else datetime.now(timezone.utc)
    open_keys = open_effect_keys(snapshot.events)
    if snapshot.tombstoned:
        return _facts(snapshot, RECOVERY_INTERRUPTED, RECOVERY_THREAD_TOMBSTONED, False, open_keys)
    if snapshot.cancellation_reason is not None:
        return _facts(snapshot, RECOVERY_INTERRUPTED, RECOVERY_THREAD_CANCELLED, False, open_keys)
    if snapshot.terminal_committed:
        return _facts(snapshot, RECOVERY_PROVEN_SAFE, RECOVERY_TERMINAL_COMMITTED, False, open_keys)
    if snapshot.run_state in TERMINAL_RUN_STATES:
        reason = {
            RUN_CANCELLED: RECOVERY_RUN_CANCELLED,
            RUN_ABANDONED: RECOVERY_RUN_ABANDONED,
            RUN_INTERRUPTED: RECOVERY_RUN_INTERRUPTED,
        }.get(snapshot.run_state, RECOVERY_RUN_STATE_UNKNOWN)
        return _facts(snapshot, RECOVERY_INTERRUPTED, reason, False, open_keys)
    if open_keys:
        return _facts(
            snapshot,
            RECOVERY_RECONCILIATION_REQUIRED,
            RECOVERY_UNCERTAIN_EFFECT,
            False,
            open_keys,
        )
    if snapshot.run_state in (RUN_QUEUED, RUN_WAITING):
        return _facts(
            snapshot, RECOVERY_PROVEN_SAFE, RECOVERY_NO_EFFECT_STARTED, True, open_keys
        )
    if snapshot.run_state == RUN_RUNNING:
        lease_live = any(
            attempt.state == ATTEMPT_ACTIVE and attempt.lease_expires_at > moment
            for attempt in snapshot.attempts
        )
        if lease_live:
            return _facts(
                snapshot, RECOVERY_PROVEN_SAFE, RECOVERY_LEASE_ACTIVE, False, open_keys
            )
        return _facts(
            snapshot,
            RECOVERY_PROVEN_SAFE,
            RECOVERY_LEASE_EXPIRED_WITHOUT_EFFECT,
            True,
            open_keys,
        )
    return _facts(
        snapshot,
        RECOVERY_RECONCILIATION_REQUIRED,
        RECOVERY_RUN_STATE_UNKNOWN,
        False,
        open_keys,
    )


def recover_run(store: PostgresRuntimeStore, run_id: str) -> RecoveryFacts:
    """Read one run's durable facts and classify them."""
    snapshot = store.read_recovery_snapshot(run_id)
    if snapshot is None:
        raise UnknownOperation(run_id, kind="run")
    return project_recovery_facts(snapshot)


def _facts(
    snapshot: RunRecoverySnapshot,
    verdict: RecoveryVerdict,
    reason: str,
    resumable: bool,
    open_keys: tuple[str, ...],
) -> RecoveryFacts:
    return RecoveryFacts(
        run_id=snapshot.run_id,
        thread_id=snapshot.thread_id,
        verdict=verdict,
        reason=reason,
        resumable=resumable,
        run_state=snapshot.run_state,
        attempt_generation=snapshot.attempt_generation,
        open_effect_keys=open_keys,
        projection_revision=snapshot.projection_revision,
        high_water_seq=snapshot.high_water_seq,
    )


__all__ = ["open_effect_keys", "project_recovery_facts", "recover_run"]
