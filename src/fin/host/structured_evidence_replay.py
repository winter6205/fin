"""EP7 replay: read back a stored snapshot's own reference, and nothing else.

The chain this module implements is six steps in this order, and each one is a
boundary rather than a step:

1. **History entry.** The caller hands one persisted ``fin_run.structured_evidence``
   record. :func:`find_replayable_record` finds it by the reference the caller
   is asking about, so the record that gets replayed is named by the caller and
   never re-resolved against anything current.
2. **The digest comparison.** :func:`~fin.host.structured_evidence.project_stored_structured_evidence`
   RECOMPUTES the record's digest from its own stored snapshot and compares it to
   the digest stored beside it. This is the digest comparison the ticket asks
   for, and it is the only one this codebase can source: see "What is compared"
   below. A record that no longer hashes to its own digest is refused before any
   transport call.
3. **The recorded reference.** ``evidence_ref`` is read OUT OF the stored
   snapshot. It is not a parameter of the replay, not derived from a subject or
   a query, and never re-resolved, so a replay cannot redirect to a newer source.
4. **One private read.** :meth:`EvidenceReadPort.read` on the recorded
   reference, and nothing else.
5. **The refusals.** iknow's own decision is authoritative for authorization,
   expiry and purge, and its code and reason are carried through unchanged. A
   second, local expiry check runs after the read so a mis-seeded snapshot is
   caught by fin as well as by the store.
6. **The identity check.** The envelope's fixed deadline and its three context
   tokens are compared against the frozen snapshot's. These are the facts that
   were pinned at grant time, in a table with no UPDATE path, so a grant that no
   longer denotes what was recorded is refused.

**What is compared, and against what.** This is the question the ticket turns
on, so it is answered here rather than in a comment elsewhere. Two comparisons
run, and the one that is NOT here is named so nobody adds it back:

* **The snapshot digest, recomputed.** The stored record keeps ``digest``
  beside ``snapshot`` (:func:`~fin.host.structured_evidence.build_stored_record`).
  Replay recomputes it and compares. This proves fin's own frozen copy is the
  one that was recorded, and it fails when the stored snapshot was mutated.

* **The grant-time identity, across the store boundary.** The grant's ``expires_at``
  and its ``context`` tokens (``query_digest``, ``scope_binding``,
  ``read_revision``) are compared against the same members of the frozen snapshot.
  Both sides were pinned at grant time and stored independently, so this fails
  when either side drifted.

* **NOT the payload.** The Evidence lane returns no digest: iknow's
  ``EvidenceReadResult`` carries no such member and ``evidence_grants`` has no
  payload-digest column (its ``query_digest`` digests the query). The grant's
  ``value`` column is also not the snapshot's ``value`` — the grant retains the
  row plan, the snapshot retains the envelope row — so no payload comparison
  exists to make. A comparison invented here would be computed from the same
  bytes on both sides and would pass forever. This is a finding, not a gap to
  paper over: see the report and ``plans/structured-corpus-p3-prerequisites.md``.

**Never queries, never redirects.** :class:`EvidenceReadPort` has exactly one
method. This module imports no retrieve adapter, no session and no port, so there
is no retrieval capability in scope to call — the property is structural, not a
convention. The reference is read out of the stored snapshot rather than taken
as an argument, so it cannot be redirected.

**The caller still supplies the identity.** Where ``owner_scope_binding`` /
``reader_binding`` / ``authorization_revision`` come from is unchanged and still
open (``plans/structured-corpus-p3-prerequisites.md`` §2.1). This module does not
mint a binding, read one from the untrusted producer sibling, or default one; the
read port is handed in already bound, and the refusals below are what a wrong
identity earns.

Refusals are typed and draw only on codes already in
:data:`fin.host.evidence_contract.CONTRACT_REFUSAL_CODES`. iknow's evidence codes
all exist there, so they pass through; anything outside the set (a transport
failure, an unknown token) becomes ``retrieve_unavailable`` rather than widening
either closed vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from fin.host.evidence_contract import (
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_EXPIRED,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.structured_evidence import project_stored_structured_evidence
from fin.host.structured_evidence_validation import StructuredRefusal, _refuse

#: The three grant-time identity tokens a stored snapshot carries as top-level
#: members and a grant carries inside its ``context`` column. Named here once so
#: the set cannot drift between the comparison and its comment.
GRANT_IDENTITY_CONTEXT_TOKENS: Final[tuple[str, ...]] = (
    "query_digest",
    "scope_binding",
    "read_revision",
)


@runtime_checkable
class EvidenceReadPort(Protocol):
    """The one capability replay is allowed to reach.

    Deliberately ONE method, and deliberately not
    :class:`~fin.retrieve.evidence_route.EvidenceRouteClient`: this module holds
    no retrieve adapter, no session and no query port, so there is no retrieval
    path in scope to call. ``EvidenceRouteClient`` satisfies this structurally,
    which is what keeps ``fin.host`` free of a ``fin.retrieve`` import here.
    """

    def read(self, evidence_ref: str) -> Mapping[str, Any]:
        """Return the stored envelope for ``evidence_ref``, or raise."""
        ...


@dataclass(frozen=True)
class ReplayedEvidence:
    """One replay that verified.

    ``snapshot`` is fin's own frozen copy, returned unchanged: replay serves
    what was recorded and never substitutes the envelope it just read. The
    envelope is kept beside it in ``envelope`` so a caller can see what the
    grant currently holds without this module deciding the two are
    interchangeable — they are compared, not merged.
    """

    evidence_ref: str
    snapshot: dict[str, Any]
    envelope: dict[str, Any]


@dataclass(frozen=True)
class ReplayOutcome:
    """The outcome of one replay. ``replayed`` is the field to branch on.

    A refused replay carries no snapshot, so an unavailable citation can never
    be mistaken for a successful empty one (spec §"Replay and failure").
    """

    replayed: bool
    evidence_ref: str | None = None
    evidence: ReplayedEvidence | None = None
    refusal: StructuredRefusal | None = None


def _refused(evidence_ref: str | None, code: str, reason: str) -> ReplayOutcome:
    return ReplayOutcome(
        replayed=False, evidence_ref=evidence_ref, refusal=_refuse(code, reason)
    )


def _read_refusal(exc: Exception, evidence_ref: str) -> ReplayOutcome:
    """Carry iknow's own refusal through, without widening either closed set.

    Every code iknow's Evidence lane can emit is already in
    :data:`CONTRACT_REFUSAL_CODES`, so a refusal about authorization, expiry or
    purge arrives with its code and its reason intact — a caller can still tell
    ``purged`` from ``expired`` even though both may share a transport code. A
    code outside the set is not passed through: it becomes
    ``retrieve_unavailable`` rather than adding a token to a published vocabulary.
    """

    from fin.retrieve.evidence_route import EvidenceGrantRefusal

    if isinstance(exc, EvidenceGrantRefusal):
        code = exc.code if exc.code in CONTRACT_REFUSAL_CODES else RETRIEVE_UNAVAILABLE
        return _refused(evidence_ref, code, exc.reason)
    # A port that raises anything else has not refused on contract; the Evidence
    # route simply did not answer, and a read that cannot be completed is not a
    # successful replay.
    return _refused(
        evidence_ref,
        RETRIEVE_UNAVAILABLE,
        f"the private Evidence route did not answer: {type(exc).__name__}",
    )


def _stored_ref(record: Any) -> str | None:
    """The reference one stored record names, read BY SHAPE.

    Read by shape rather than through the projector, for the same reason
    :func:`~fin.host.thread_store._stored_evidence_ref` does: a record that
    fails projection must still be FINDABLE, so the caller can be told which
    reference it asked about before it is refused for failing the check.
    """

    if not isinstance(record, Mapping):
        return None
    snapshot = record.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    ref = snapshot.get("evidence_ref")
    return ref if isinstance(ref, str) and ref else None


def find_replayable_record(
    records: Sequence[Any], evidence_ref: str
) -> Mapping[str, Any] | None:
    """The stored record naming ``evidence_ref``, or ``None``.

    The selector is the reference alone, so a caller asking for one recorded
    grant gets that grant's record and nothing else. Two records naming the same
    reference are not resolved by first match: ``None`` is returned, because
    choosing one of two records for one reference is exactly the substitution
    replay exists to prevent.
    """

    matches = [record for record in records if _stored_ref(record) == evidence_ref]
    return matches[0] if len(matches) == 1 else None


def _as_instant(value: Any) -> datetime | None:
    """One ISO instant, or ``None`` when the text is not one.

    The two sides spell the same deadline differently and MUST be compared as
    instants, not as strings: fin's snapshot carries the spec's whole-second
    ``Z`` form (``2027-10-03T00:00:00Z``) while the grant's ``timestamptz``
    column reaches the read through ``Date.toISOString()`` and arrives as
    ``2027-10-03T00:00:00.000Z``. A string comparison would refuse every honest
    replay on formatting alone, and a comparison that refuses everything is
    indistinguishable from one that proves nothing.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _verified(
    snapshot: Mapping[str, Any],
    envelope: Mapping[str, Any],
    evidence_ref: str,
) -> ReplayOutcome:
    """The comparison the read earns, or the refusal it does not."""

    recorded_deadline = _as_instant(snapshot.get("expires_at"))
    retained_deadline = _as_instant(envelope.get("expires_at"))
    if recorded_deadline is None or retained_deadline is None:
        return _refused(
            evidence_ref,
            MALFORMED_EVIDENCE_CONTRACT,
            "the deadline is not an ISO instant on one side of the comparison",
        )
    if retained_deadline != recorded_deadline:
        return _refused(
            evidence_ref,
            EVIDENCE_UNAVAILABLE,
            "the retained deadline no longer matches the frozen snapshot, so the "
            "reference no longer denotes what was recorded",
        )
    context = envelope.get("context")
    if not isinstance(context, Mapping):
        return _refused(
            evidence_ref,
            MALFORMED_EVIDENCE_CONTRACT,
            "the retained envelope carries no context to compare",
        )
    drifted = [
        token
        for token in GRANT_IDENTITY_CONTEXT_TOKENS
        if context.get(token) != snapshot.get(token)
    ]
    if drifted:
        return _refused(
            evidence_ref,
            EVIDENCE_UNAVAILABLE,
            "the retained grant identity does not match the frozen snapshot on "
            f"{', '.join(drifted)}, so the reference no longer denotes what was "
            "recorded",
        )
    return ReplayOutcome(
        replayed=True,
        evidence_ref=evidence_ref,
        evidence=ReplayedEvidence(
            evidence_ref=evidence_ref,
            snapshot=dict(snapshot),
            envelope=dict(envelope),
        ),
    )


def replay_stored_evidence(
    record: Any,
    *,
    reader: EvidenceReadPort,
    now: datetime | None = None,
) -> ReplayOutcome:
    """Replay one stored record against the reference it froze.

    ``record`` is the untrusted persisted value — whatever came back out of
    ``fin_run.structured_evidence``. ``reader`` is an already-bound private read
    port; this module never constructs, resolves or defaults one.
    """

    evidence_ref = _stored_ref(record)
    if evidence_ref is None:
        return _refused(
            None, MALFORMED_EVIDENCE_CONTRACT, "the stored record names no reference"
        )

    # Step 2: the digest comparison, before any transport call. A record that no
    # longer hashes to its own digest must not be served, and must not spend a
    # read to discover that.
    projected = project_stored_structured_evidence(record)
    if projected is None:
        return _refused(
            evidence_ref,
            MALFORMED_EVIDENCE_CONTRACT,
            "the stored record is not the frozen shape: its snapshot does not "
            "recompute to its own digest, or its selector does not match it",
        )
    snapshot = projected["snapshot"]

    # Step 4: one private read of the RECORDED reference. iknow decides
    # authorization, expiry and purge; its refusal is authoritative and is
    # carried through with its code and reason.
    try:
        envelope = reader.read(evidence_ref)
    except Exception as exc:  # WHY broad: a transport may raise its own type
        return _read_refusal(exc, evidence_ref)
    if not isinstance(envelope, Mapping):
        return _refused(
            evidence_ref,
            MALFORMED_EVIDENCE_CONTRACT,
            f"the retained envelope is {type(envelope).__name__}, expected an object",
        )
    if envelope.get("evidence_ref") != evidence_ref:
        return _refused(
            evidence_ref,
            MALFORMED_EVIDENCE_CONTRACT,
            "the retained envelope answers for a different reference",
        )

    # Step 5, fin's own half. The store already refused an expired grant; this
    # catches a snapshot whose recorded deadline has passed even if the grant
    # row still claims otherwise. A read never renews a deadline, so the local
    # instant is the only clock that may decide this.
    instant = now or datetime.now(timezone.utc)
    recorded_deadline = _as_instant(snapshot.get("expires_at"))
    if recorded_deadline is not None and instant >= recorded_deadline:
        return _refused(
            evidence_ref,
            EVIDENCE_EXPIRED,
            "the frozen snapshot's own deadline has passed; a read does not "
            "renew it",
        )

    # Step 6: the grant-time identity, compared across the store boundary.
    return _verified(snapshot, envelope, evidence_ref)


__all__ = [
    "GRANT_IDENTITY_CONTEXT_TOKENS",
    "EvidenceReadPort",
    "ReplayedEvidence",
    "ReplayOutcome",
    "find_replayable_record",
    "replay_stored_evidence",
]