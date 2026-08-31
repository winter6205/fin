"""Citation-evidence admission for retrieved chunks (plan T17, SC22).

The citation gate (:mod:`fin.host.citation_gate`) reads
``EvidenceItem.locator`` and copies ``document_id`` / ``version`` /
``page`` straight into the user-visible citation. So a chunk whose
``source_ref`` never mapped back to a fin **locator** — the shape a
stale ``locator_map`` produces — must not become evidence at all: the
retrieval service's own identifier would otherwise be quoted to the
user as a source (ADR 0030 / SC22).

This module is that admission point, and it is one sentence long in
substance: a retrieved chunk is evidence-eligible only if its
``source_ref`` IS a :class:`~fin.corpus.extracted.Locator`. Everything
else is dropped and counted on the shared
:class:`~fin.retrieve.hit_mapping.MappingFailureLedger`, so the drop is
auditable rather than silent.

Scope, deliberately narrow: admission decides *whether a hit exists as
evidence*, never *how the gate rules on evidence*. The gate's own
contracts — the ``≥min_span_len`` extractive-span rule,
``unknown_locator``, the repair cap, and the malformed-evidence error
for an empty ``page_text`` — are untouched by this ticket. In
particular, a mapping failure is a drop, not a new refusal reason.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from fin.corpus.extracted import Locator
from fin.host.citation_gate import EvidenceItem
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_STAGE_EVIDENCE_ADMISSION,
    MappingFailureLedger,
)


def admit_retrieved_chunks(
    chunks: Iterable[Any],
    *,
    ledger: MappingFailureLedger,
) -> tuple[Any, ...]:
    """The subset of ``chunks`` that may become citation evidence.

    A chunk whose ``source_ref`` is not a ``Locator`` is dropped and
    recorded on ``ledger`` as ``non_locator_source_ref``. The returned
    tuple keeps the caller's order, so the ranking the adapter produced
    survives admission.
    """
    admitted: list[Any] = []
    for chunk in chunks:
        source_ref = getattr(chunk, "source_ref", None)
        if isinstance(source_ref, Locator):
            admitted.append(chunk)
            continue
        ledger.record(
            code=MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
            stage=MAPPING_STAGE_EVIDENCE_ADMISSION,
            retrieval_ref=(
                source_ref
                if isinstance(source_ref, str)
                else repr(source_ref)
            ),
            message=(
                "retrieved chunk source_ref is "
                f"{type(source_ref).__name__}, expected a fin Locator; the "
                "hit is dropped and never enters citation evidence"
            ),
        )
    return tuple(admitted)


def evidence_from_chunks(chunks: Sequence[Any]) -> list[EvidenceItem]:
    """One ``EvidenceItem`` per already-admitted chunk.

    Separate from :func:`admit_retrieved_chunks` so the admission
    decision and the evidence shape stay independently readable; callers
    admit first, then build.
    """
    return [
        EvidenceItem(locator=chunk.source_ref, page_text=chunk.text)
        for chunk in chunks
    ]


__all__ = ["admit_retrieved_chunks", "evidence_from_chunks"]
