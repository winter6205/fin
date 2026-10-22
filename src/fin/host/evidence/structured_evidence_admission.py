"""EP6 admission: one untrusted ``structured_result`` -> stored records or a refusal.

Every step of this lane was built and tested before anything called it.
:func:`fin.host.evidence.structured_evidence.structured_snapshots_of` validated the
producer sibling and :func:`~fin.host.evidence.structured_evidence.build_stored_record`
froze one persisted record per row or count receipt. None of it was reachable
from the retrieve tool, so ``fin_run.structured_evidence`` stayed ``[]`` in
production.

This module is the reduce that connects them, and it deliberately owns no new
rule: validation, the 32 KiB bound and the type-preserving digest all stay in
their existing homes. What is new here is the two decisions that no existing
function could make for us.

**The ``[]`` ambiguity.** :func:`structured_snapshots_of` answers ``[]`` both
for a valid sibling carrying zero rows and for a sibling it refused, and it
discards the refusal. Admission therefore re-runs
:func:`validate_structured_result` first and keeps THAT answer: a refused
sibling must surface as a typed :class:`StructuredRefusal`, never as a
success-shaped empty record list (T29's acceptance, spec §"Replay and
failure": an unavailable reference is never a successful empty result).

**Never raise.** The sibling is an untrusted transport value — it may be
``None``, a non-object, or an object whose own reads blow up. Admission
converts every such failure into a typed refusal from
:data:`~fin.host.evidence.evidence_contract.CONTRACT_REFUSAL_CODES`, because the
alternative is a raise inside the tool node that takes down the whole run row
over one structured lane.

**Bound before build.** The store's write guard
(:func:`fin.host.thread_store._outgoing_structured_error`) rejects an
oversized snapshot by RAISING ``EvidenceContractWriteError`` at the run-row
write, which fails the entire run rather than the structured lane. Each
snapshot is therefore re-checked against
:func:`~fin.host.evidence.structured_evidence_validation.snapshot_refusal` before
:func:`~fin.host.evidence.structured_evidence.build_stored_record` is asked to build
anything, so an oversized result is a refusal here and never reaches the
store's raise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from fin.host.evidence.evidence_contract import MALFORMED_EVIDENCE_CONTRACT
from fin.host.evidence.structured_evidence import (
    StructuredRefusal,
    build_stored_record,
    structured_snapshots_of,
    validate_structured_result,
)
from fin.host.evidence.structured_evidence_validation import snapshot_refusal

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructuredAdmission:
    """The outcome of admitting one ``structured_result`` sibling.

    ``admitted`` is the field to branch on, never ``records``: an admitted
    sibling may legitimately carry zero records (an empty row list, or no
    structured result at all), so ``records == ()`` alone cannot tell an
    empty success from a refusal.
    """

    admitted: bool
    records: tuple[dict[str, Any], ...] = ()
    refusal: StructuredRefusal | None = None

    def as_dicts(self) -> list[dict[str, Any]]:
        """The records in the list shape ``RunRecord.structured_evidence`` holds."""
        return [dict(record) for record in self.records]


@dataclass(frozen=True)
class _AcceptedSnapshots:
    """The admitted arm: the validated snapshots, possibly none."""

    snapshots: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _RefusedSnapshots:
    """The refused arm, carrying the refusal for those snapshots.

    XOR with :class:`_AcceptedSnapshots` — a batch is one or the other, never
    both and never neither. Modelling it as one record with a ``refused`` flag
    beside a nullable ``refusal`` made the two indistinguishable to the type
    checker, and every read of the nullable half needed a suppression.
    """

    refusal: StructuredRefusal


def _refuse(refusal: StructuredRefusal) -> StructuredAdmission:
    return StructuredAdmission(admitted=False, refusal=refusal)


def _typed_unreadable(detail: str) -> StructuredRefusal:
    return StructuredRefusal(code=MALFORMED_EVIDENCE_CONTRACT, reason=detail)


def _snapshots(sibling: Any) -> _AcceptedSnapshots | _RefusedSnapshots:
    """The frozen snapshots this sibling contributes, or its typed refusal.

    ``structured_snapshots_of`` answers ``[]`` for a refused sibling and
    discards why, so the refusal is taken from
    :func:`validate_structured_result` FIRST and the ``[]`` it would have
    returned for a refusal is never mistaken for an empty row list.
    """
    if sibling is None:
        # No structured sibling is not a malformed contract: the structured
        # lane is one arm of the shared retrieve tool, and a document read
        # carries no structured result by design.
        return _AcceptedSnapshots()
    refusal = validate_structured_result(sibling)
    if refusal is not None:
        return _RefusedSnapshots(refusal)
    return _AcceptedSnapshots(snapshots=tuple(structured_snapshots_of(sibling)))


def _bounded_snapshots(
    batch: _AcceptedSnapshots | _RefusedSnapshots,
) -> _AcceptedSnapshots | _RefusedSnapshots:
    """Drop to a refusal if any snapshot is over the bound or off-contract.

    The store's write guard RAISES on such a snapshot at the run-row write,
    which would fail the run instead of the structured lane, so the bound is
    enforced here — before a record is built.
    """
    if isinstance(batch, _RefusedSnapshots):
        return batch
    for snapshot in batch.snapshots:
        size_refusal = snapshot_refusal(snapshot)
        if size_refusal is not None:
            return _RefusedSnapshots(size_refusal)
    return batch


def _admit(sibling: Any) -> StructuredAdmission:
    """The reduce the public entry point runs; it never raises on untrusted input."""
    batch = _bounded_snapshots(_snapshots(sibling))
    if isinstance(batch, _RefusedSnapshots):
        return _refuse(batch.refusal)
    return StructuredAdmission(
        admitted=True,
        records=tuple(build_stored_record(snapshot) for snapshot in batch.snapshots),
    )


def admit_structured_evidence(sibling: Any) -> StructuredAdmission:
    """Admit one ``structured_result`` sibling into stored records.

    ``sibling`` is the raw transport value from
    :func:`fin.retrieve.port.structured_result_of` — it may be anything.
    The returned records are exactly the rows ``RunRecord.structured_evidence``
    holds, and ``refusal`` is a typed §6 code or ``None``.
    """
    try:
        return _admit(sibling)
    except Exception as exc:  # WHY broad: an untrusted Mapping may raise anything on read
        _LOG.warning("structured admission: unreadable sibling (%s)", type(exc).__name__)
        return _refuse(_typed_unreadable("structured_result could not be read"))


__all__ = [
    "StructuredAdmission",
    "admit_structured_evidence",
]
