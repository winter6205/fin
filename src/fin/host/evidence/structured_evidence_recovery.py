"""EP10 recovery sweep: withhold on a gap, revalidate, scrub, then expose.

T33's second half, in the three parts
``plans/structured-corpus-evidence-lifecycle-p2.md`` records. The gap itself is
already handled safely by
:mod:`fin.host.evidence.structured_evidence_invalidation`: the drain refuses and leaves
the watermark where it was. This module is the part that was missing — what fin
does with the references it is still holding once the stream cannot be
continued.

1. **Withhold every retained reference.** :func:`withhold_retained_references`
   closes a DURABLE gate (``fin_evidence_invalidation_checkpoint.recovery_withheld``).
   The drain closes it too, at the moment it refuses on a gap, so the gate does
   not depend on a caller remembering.
2. **Revalidate them against the current authorized binding.**
   :func:`sweep_recovered_evidence` replays every retained record through
   :func:`~fin.host.evidence.structured_evidence_replay.replay_stored_evidence`.
3. **Scrub the denied ones before exposing them.** The denied set is deleted
   through storage, storage is asked to confirm the deletion, and only then does
   the gate open.

**"Against the current authorized binding" — what that means here, and who
answers it.** The answer is the read port, and it is worth being exact because
the weaker reading is available and wrong. The port is handed to this module
already bound to one :class:`~fin.retrieve.evidence_route.EvidenceIdentityBinding`.
The revalidation re-asks the STORE about each reference, and the store compares
the grant against that binding *at read time*, in two functions that run in a
fixed order and answer two different questions.
:func:`decideGrantRead` (``evidence-store.ts:299``) reads the grant's OWN scope's
live ``authorization_revision`` and ``purge_revision`` and denies on a
disagreement, and only :func:`callerRefusal` (``evidence-read.ts:428``, the
``grant.scope_binding !== context.ownerScopeBinding`` test on line 433) asks who
is asking. So "current" means current-as-of-this-read, and the authority is
iknow, not fin's frozen copy.

**Why that order is what makes this module's deny-set safe.** A revocation or
purge is decided from the grant and its own scope, before identity is
consulted, so every binding reaches the same verdict on it: it is a fact about
the payload and any sweep may scrub it. ``scope_not_authorized`` is decided
after, so it is a fact about the reader, and it is unreachable for a grant that
was already denied. :data:`INVALIDATION_DENIAL_REASONS` is therefore exactly the
two verdicts that carry the payload, and nothing that carries the caller.

This is a DIFFERENT property from the grant-time token comparison inside
:func:`~fin.host.evidence.structured_evidence_replay.replay_stored_evidence`. That one
asks "does the stored record still denote what it recorded" — both sides were
pinned at grant time, so it cannot observe a revocation at all. This module's
revalidation asks "does the store still authorize this reference for this
binding", which is the ticket's question. The sweep does not substitute the
first for the second; it depends on the second and treats the first as what it
is.

**When the binding is absent there is no second answer, and none is invented.**
The host still has no trusted source for ``owner_scope_binding`` /
``reader_binding`` / ``authorization_revision``
(``plans/structured-corpus-p3-prerequisites.md`` §2.1), so production cannot
hand this module a bound port. This module therefore REQUIRES one: it takes no
default, reads none from the untrusted producer sibling, and does not accept
``None``. A caller with no binding cannot sweep, and the gate stays closed. That
is the safe direction — the alternative, defaulting a binding and revalidating
against it, is the process-level default scope B1 forbids and the defect P1's
M1 finding was about.

**How a denied snapshot is kept from being exposed.** Three mechanisms, and the
third is what makes the other two defence in depth rather than load bearing:

* **The gate is checked before any transport call.**
  :func:`replay_retained_reference` reads the durable state and returns a
  refusal with NO snapshot attached, so a withheld consumer spends zero reads
  and a serving caller has nothing to render. ``ReplayOutcome`` carries the
  snapshot only on ``replayed=True``, so there is no success-shaped empty to
  mistake for an available citation.
* **The denied payload is deleted before the gate opens.** The gate is released
  only after storage reported ``remaining_entries == 0`` for the denied set, so
  the record a denial named is gone from ``fin_run.structured_evidence`` and
  :func:`~fin.host.evidence.structured_evidence_replay.find_replayable_record` can no
  longer name it.
* **fin can only ever serve what the store grants at read time.** This is the
  one that holds even if the gate is bypassed. Every snapshot leaves this
  codebase through a live read of the recorded reference, and a reference the
  store no longer authorizes is refused there. The gate's unique job is the
  window fin has not yet asked about — after it, the store is the authority.

**What the sweep does not do.** It does not scrub, confirm or acknowledge in
the drain's order — that machine is ADR 0099's and it is not re-derived here.
It writes no watermark and calls no ``advance_invalidation_acknowledgement``, so
"stale checkpoints and unavailable backends never advance acknowledgement" is
structural: there is no statement in this module that can move one. The reason
a scrub still happens is that recovering missed invalidations means deleting
the payloads those events would have named; the events themselves stay owed, so
the drain keeps its position and redelivers, scrubbing idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Mapping

from fin.host.evidence.evidence_contract import (
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_UNAVAILABLE,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.evidence.structured_evidence_replay import (
    EvidenceReadPort,
    ReplayOutcome,
    replay_stored_evidence,
)
from fin.host.evidence.structured_evidence_validation import StructuredRefusal, _refuse
from fin.host.thread_store import EvidenceRecoveryState, ThreadStore

#: The store's own lifecycle verdicts that mean this reference is gone for every
#: binding, not merely for the one asking.
#:
#: These are the two ``INVALIDATION_REASONS`` of
#: ``docs/decisions/p2-d1-ik-to-fin-invalidation-event-contract.md`` as IK's read
#: lane reports them: ``access_revoked`` becomes ``authorization_revoked`` and
#: ``purged`` keeps its name. Both are decided by a disagreement INSIDE the
#: store (the grant's revision against the scope's live revision), which is what
#: makes them safe to act on.
#:
#: WHY ``scope_not_authorized`` is NOT here, though it arrives as
#: ``access_denied``: it fires when the caller's binding does not hold the
#: grant's scope (``evidence-read.ts:433``), which is a fact about WHO IS
#: ASKING. Scrubbing on it would let a sweep handed a mis-bound port delete
#: every payload it touched, and would be data loss dressed as recovery. It is
#: left unreadable instead: the replay path refuses it, so it cannot be served.
#: ``unknown_reference`` is excluded on the same principle of scope — the grant
#: row is simply absent, which is not an invalidation this sweep was asked to
#: reconcile, and T32's event-driven scrub remains its owner.
INVALIDATION_DENIAL_REASONS: Final[frozenset[str]] = frozenset(
    {"authorization_revoked", "purged"}
)

#: The one code that means the lane produced NO verdict about this reference.
#: Every other code in the closed set is a structured answer iknow returned
#: about this reference, and the replay path refuses on all of them, so none of
#: them can expose a snapshot. This one cannot: "the private Evidence route did
#: not answer" leaves fin unable to say whether the reference is still
#: authorized, so recovery has not completed and the gate stays shut.
UNANSWERED_CODE: Final[str] = RETRIEVE_UNAVAILABLE

#: The four terminal states one retained reference can reach during a sweep.
#: Named as constants because each one carries a different consequence for the
#: gate, and collapsing any two of them would change what recovery claims.
VERIFIED: Final[str] = "verified"
DENIED: Final[str] = "denied"
UNREADABLE: Final[str] = "unreadable"
UNRESOLVED: Final[str] = "unresolved"


@dataclass(frozen=True)
class RevalidationVerdict:
    """What the store said about one reference during the sweep."""

    evidence_ref: str
    state: str
    code: str | None = None
    reason: str | None = None

    @property
    def denied(self) -> bool:
        return self.state == DENIED

    @property
    def unresolved(self) -> bool:
        return self.state == UNRESOLVED


@dataclass(frozen=True)
class RevalidationReport:
    """Every retained reference the sweep examined, and its verdict."""

    verdicts: tuple[RevalidationVerdict, ...]
    #: Entries whose snapshot names no reference at all. They are counted rather
    #: than swept: ``find_replayable_record`` matches on that name, so a record
    #: without one can never be found, replayed or served.
    unaddressable: int

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(v.evidence_ref for v in self.verdicts if v.denied)

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(v.evidence_ref for v in self.verdicts if v.unresolved)

    @property
    def verified(self) -> tuple[str, ...]:
        return tuple(v.evidence_ref for v in self.verdicts if v.state == VERIFIED)


@dataclass(frozen=True)
class RecoverySweepOutcome:
    """What one sweep did, and whether recovery completed."""

    consumer_id: str
    examined: int
    unaddressable: int
    denied: tuple[str, ...]
    scrubbed_entries: int
    scrubbed_runs: int
    #: True only when the gate was reopened, which is the only state in which a
    #: retained snapshot becomes servable again.
    released: bool
    refusal: StructuredRefusal | None = None


def _refused(
    consumer_id: str, code: str, reason: str
) -> RecoverySweepOutcome:
    return RecoverySweepOutcome(
        consumer_id=consumer_id,
        examined=0,
        unaddressable=0,
        denied=(),
        scrubbed_entries=0,
        scrubbed_runs=0,
        released=False,
        refusal=_refuse(code, reason),
    )


def withhold_retained_references(store: ThreadStore, consumer_id: str, reason: str) -> None:
    """Close the gate for ``consumer_id``: nothing retained is servable.

    Idempotent and watermark-free. The drain calls this for the consumer on its
    own (see :func:`~fin.host.evidence.structured_evidence_invalidation.drain_invalidation_batch`);
    it is public so a caller that learns of a gap by another route can record the
    same fact without reaching into the drain.
    """

    store.set_recovery_withheld(consumer_id, reason)


def recovery_is_withheld(store: ThreadStore, consumer_id: str) -> bool:
    """Whether this consumer's retained evidence is currently withheld."""

    return store.read_recovery_state(consumer_id).withheld


def _stored_ref(record: Any) -> str | None:
    """The reference one stored entry names, read BY SHAPE.

    Same reason as
    :func:`~fin.host.evidence.structured_evidence_replay._stored_ref`: an entry that fails
    projection must still be COUNTABLE, so a sweep can say how many it could
    not address instead of silently skipping them.
    """

    if not isinstance(record, Mapping):
        return None
    snapshot = record.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    ref = snapshot.get("evidence_ref")
    return ref if isinstance(ref, str) and ref else None


def _retained_records(store: ThreadStore) -> list[Mapping[str, Any]]:
    """Every ``structured_evidence`` entry the store currently retains.

    Enumerated through ``list_threads`` and then ``list_runs`` rather than from
    one thread, because "every retained reference" is the whole requirement: a
    sweep that only looked at the thread that happened to be in hand would leave
    the rest of the corpus exposed through a closed gate's release.
    """

    records: list[Mapping[str, Any]] = []
    for thread in store.list_threads():
        for run in store.list_runs(thread.id):
            for entry in run.structured_evidence:
                if isinstance(entry, Mapping):
                    records.append(entry)
    return records


def _verdict(outcome: ReplayOutcome) -> RevalidationVerdict:
    """Classify one replay outcome into the four terminal states."""

    refusal = outcome.refusal
    evidence_ref = outcome.evidence_ref or ""
    if outcome.replayed:
        return RevalidationVerdict(evidence_ref=evidence_ref, state=VERIFIED)
    assert refusal is not None, "a refused replay always carries its refusal"
    if refusal.code == UNANSWERED_CODE:
        return RevalidationVerdict(
            evidence_ref=evidence_ref,
            state=UNRESOLVED,
            code=refusal.code,
            reason=refusal.reason,
        )
    if (
        refusal.code in CONTRACT_REFUSAL_CODES
        and refusal.reason in INVALIDATION_DENIAL_REASONS
    ):
        return RevalidationVerdict(
            evidence_ref=evidence_ref,
            state=DENIED,
            code=refusal.code,
            reason=refusal.reason,
        )
    # WHY unreadable and not unresolved: the store DID answer about this
    # reference and the replay path refuses it, so the gate's release cannot
    # expose it. The payload is retained — expiry and fin-side refusals are not
    # invalidations, and T34 owns when a retained payload goes — but it is not
    # readable, which is the property this sweep has to establish.
    return RevalidationVerdict(
        evidence_ref=evidence_ref,
        state=UNREADABLE,
        code=refusal.code,
        reason=refusal.reason,
    )


def revalidate_retained_references(
    store: ThreadStore,
    *,
    reader: EvidenceReadPort,
    now: datetime | None = None,
) -> RevalidationReport:
    """Ask the store, per retained reference, whether it is still authorized.

    This is the sweep's revalidation and it is the ticket's "current": each
    verdict comes from a live read of the recorded reference, decided by iknow
    against the binding the port carries. One reference is asked once even when
    several runs retain it, because the store's answer is per reference and a
    second identical read could only disagree with itself.

    ``reader`` is required and is never defaulted: with no trusted source for a
    binding there is nothing to revalidate AGAINST, and a sweep that invented
    one would be deciding that a snapshot is authorized by its own author.
    """

    verdicts: list[RevalidationVerdict] = []
    unaddressable = 0
    asked: set[str] = set()
    for record in _retained_records(store):
        evidence_ref = _stored_ref(record)
        if evidence_ref is None:
            unaddressable += 1
            continue
        if evidence_ref in asked:
            continue
        asked.add(evidence_ref)
        verdicts.append(
            _verdict(replay_stored_evidence(record, reader=reader, now=now))
        )
    return RevalidationReport(verdicts=tuple(verdicts), unaddressable=unaddressable)


def replay_retained_reference(
    store: ThreadStore,
    record: Any,
    *,
    consumer_id: str,
    reader: EvidenceReadPort,
    now: datetime | None = None,
) -> ReplayOutcome:
    """Replay one retained record, honouring the recovery gate.

    This is the serving surface T33 adds, and it is where "keep snapshots
    unavailable until recovery completes" is enforced. The gate is read BEFORE
    any transport call, so a withheld consumer costs zero reads, and the refusal
    it returns carries no snapshot: ``ReplayOutcome`` holds one only when
    ``replayed`` is true, so a withheld citation cannot be rendered as an empty
    successful one.

    WHY this exists beside :func:`replay_stored_evidence` rather than inside it:
    that function is a pure decision over one record and holds no store, so
    giving it the gate would have made a live database read a precondition for
    replaying anything. The gate is a property of the CONSUMER's recovery
    state, not of a record, and it is composed here where the store is in hand.
    """

    state = store.read_recovery_state(consumer_id)
    if state.withheld:
        return ReplayOutcome(
            replayed=False,
            evidence_ref=_stored_ref(record),
            refusal=_refuse(
                EVIDENCE_UNAVAILABLE,
                "recovery for this consumer is not complete, so retained "
                "evidence is unavailable until the recovered events apply",
            ),
        )
    return replay_stored_evidence(record, reader=reader, now=now)


def sweep_recovered_evidence(
    store: ThreadStore,
    *,
    consumer_id: str,
    reader: EvidenceReadPort,
    now: datetime | None = None,
) -> RecoverySweepOutcome:
    """Run the whole sweep: revalidate, scrub the denied, then release.

    The order is the contract. The gate is released LAST and only after storage
    confirmed the deletion, so there is no state in which a denied payload is
    both still retained and servable.

    Recovery does NOT complete while any reference is
    :data:`UNRESOLVED`: fin could not get a verdict on it, so it cannot claim
    the missed invalidations have been reconciled, and the gate stays shut. A
    backend that is down therefore leaves every retained snapshot unavailable,
    which is the ticket's instruction rather than an outage of fin's own making.

    No statement in this function advances an acknowledgement. The stream
    position stays owed to the drain.
    """

    if not consumer_id or not consumer_id.strip():
        return _refused(
            consumer_id,
            EVIDENCE_UNAVAILABLE,
            "sweep: consumer_id must be a non-empty token",
        )
    state = store.read_recovery_state(consumer_id)
    if not state.withheld:
        # WHY refuse instead of sweeping anyway: a sweep scrubs. Running one
        # with no gap to recover would delete payloads on the strength of a
        # read that nobody asked for, so the call has to name a recovery that
        # is actually in progress.
        return _refused(
            consumer_id,
            EVIDENCE_UNAVAILABLE,
            f"sweep: {consumer_id} is not in recovery, so there is nothing to "
            "recover and no payload may be scrubbed",
        )

    report = revalidate_retained_references(store, reader=reader, now=now)
    denied = report.denied

    scrubbed_entries = 0
    scrubbed_runs = 0
    if denied:
        outcome = store.scrub_retained_evidence_payloads(denied)
        scrubbed_entries = outcome.scrubbed_entries
        scrubbed_runs = outcome.scrubbed_runs
        if outcome.remaining_entries != 0:
            # WHY refuse with the gate still shut: storage could not back the
            # deletion, so opening the gate would expose exactly the payloads
            # the sweep exists to remove. The withheld state is left as it is —
            # a sweep that failed must not look like one that succeeded.
            return RecoverySweepOutcome(
                consumer_id=consumer_id,
                examined=len(report.verdicts),
                unaddressable=report.unaddressable,
                denied=denied,
                scrubbed_entries=scrubbed_entries,
                scrubbed_runs=scrubbed_runs,
                released=False,
                refusal=_refuse(
                    EVIDENCE_UNAVAILABLE,
                    f"sweep: storage still holds {outcome.remaining_entries} "
                    f"payload(s) for {len(denied)} denied reference(s)",
                ),
            )

    if report.unresolved:
        return RecoverySweepOutcome(
            consumer_id=consumer_id,
            examined=len(report.verdicts),
            unaddressable=report.unaddressable,
            denied=denied,
            scrubbed_entries=scrubbed_entries,
            scrubbed_runs=scrubbed_runs,
            released=False,
            refusal=_refuse(
                RETRIEVE_UNAVAILABLE,
                "sweep: the private Evidence route did not answer for "
                f"{len(report.unresolved)} retained reference(s), so recovery "
                "is incomplete and every retained reference stays withheld",
            ),
        )

    # Only now, with every reference decided and every denied payload confirmed
    # gone, does anything become servable again.
    store.set_recovery_released(consumer_id)
    return RecoverySweepOutcome(
        consumer_id=consumer_id,
        examined=len(report.verdicts),
        unaddressable=report.unaddressable,
        denied=denied,
        scrubbed_entries=scrubbed_entries,
        scrubbed_runs=scrubbed_runs,
        released=True,
    )


__all__ = [
    "DENIED",
    "INVALIDATION_DENIAL_REASONS",
    "UNANSWERED_CODE",
    "UNREADABLE",
    "UNRESOLVED",
    "VERIFIED",
    "EvidenceRecoveryState",
    "RecoverySweepOutcome",
    "RevalidationReport",
    "RevalidationVerdict",
    "replay_retained_reference",
    "revalidate_retained_references",
    "sweep_recovered_evidence",
    "withhold_retained_references",
]
