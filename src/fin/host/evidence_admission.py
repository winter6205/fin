"""Citation-evidence admission for retrieved chunks (plan T17, SC22).

The citation gate (:mod:`fin.host.citation_gate`) reads
``EvidenceItem.locator`` and copies ``document_id`` / ``version`` /
``unit`` straight into the user-visible citation. So a chunk whose
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
for an empty ``unit_text`` — are untouched by this ticket. In
particular, a mapping failure is a drop, not a new refusal reason.

Both legs that can produce quotable text pass through here: the company
``retrieve`` chunks through :func:`admit_retrieved_chunks`, and the
attachment legs (``retrieve`` hits and ``read_source`` slices alike)
through :func:`attachment_evidence_from_ref`, because one identity rule
spread over two callers is one rule that can fork.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from fin.corpus.extracted import Locator
from fin.host.citation_gate import EvidenceItem
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_STAGE_EVIDENCE_ADMISSION,
    MappingFailureLedger,
)
from fin.retrieve.source_read import ATTACHMENT_KIND


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
        EvidenceItem(locator=chunk.source_ref, unit_text=chunk.text)
        for chunk in chunks
    ]


def unit_ordinal(raw: Any) -> int | None:
    """The frozen-unit ordinal ``raw`` names, or ``None`` if it names none.

    A unit is addressed 1-based, so anything below one is not a unit; a
    bool is an ``int`` subclass but not an ordinal, and a coerced string
    is not the identity either leg carries.
    """
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < 1
    ):
        return None
    return raw


def attachment_evidence_from_ref(
    source_ref: Mapping[str, Any],
    unit_text: str,
    *,
    grant: Callable[[str, int | None], tuple[str, str] | None] | None,
    position: int = 0,
    next_position: int | None = None,
) -> EvidenceItem | None:
    """The evidence one attachment hit or slice earns, or ``None``.

    An attachment citation's identity does not come from the locator the
    model holds: the safe filename and how its unit is presented come
    from the thread's trusted record, and the version must still equal
    that record's. ``grant`` is that record's answer — ``None`` when no
    readable record stands behind the id (unknown, another thread's,
    tombstoned, or re-prepared to a newer version), and then this text
    has nothing to be attributed to: the model still received it, but no
    citation may rest on it.

    ``position`` / ``next_position`` are only what a ``read_source``
    slice carries; a retrieved passage pass-through leaves both at their
    defaults, which is what keeps a hit from ever bridging to an
    adjacent slice of the same unit.
    """
    unit = unit_ordinal(source_ref.get("unit"))
    attachment_id = str(source_ref.get("attachment_id") or "")
    version = str(source_ref.get("version") or "")
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        version_int = None
    # An item the gate must reject is worse than no item: the gate
    # validates its evidence eagerly and raises rather than skipping, so
    # an unattributable identity stops the run instead of the citation.
    if unit is None or not attachment_id or not version or version_int is None:
        return None
    granted = grant(attachment_id, version_int) if grant is not None else None
    if granted is None:
        return None
    filename, display = granted
    return EvidenceItem(
        # An attachment's identity rides its own field, never the
        # company document slot.
        locator=Locator(document_id="", version=version, unit=unit),
        unit_text=unit_text,
        kind=ATTACHMENT_KIND,
        attachment_id=attachment_id,
        version_int=version_int,
        filename=filename,
        display=display,
        position=position,
        next_position=next_position,
    )


__all__ = [
    "admit_retrieved_chunks",
    "attachment_evidence_from_ref",
    "evidence_from_chunks",
    "unit_ordinal",
]
