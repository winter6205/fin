"""Citation-evidence admission for retrieved chunks (plan T17, SC22).

The citation gate (:mod:`fin.host.grounded.citation_gate`) reads
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
from fin.host.grounded.citation_gate import (
    DATABASE_ROW_DISPLAY,
    DATABASE_ROW_KIND,
    DatabaseRow,
    EvidenceItem,
)
from fin.host.evidence.evidence_contract import (
    seed_context_from_rows,
    validate_evidence,
)
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_STAGE_EVIDENCE_ADMISSION,
    MappingFailureLedger,
)
from fin.retrieve.port import (
    DATABASE_ROWS_KEY,
    DATABASE_ROWS_RESULT_COUNT_FIELD,
    DATABASE_ROWS_ROWS_FIELD,
    DATABASE_ROWS_SCHEMA,
    DATABASE_ROWS_SCHEMA_FIELD,
    DATABASE_ROWS_SCHEMA_VERSION,
    DATABASE_ROWS_SCHEMA_VERSION_FIELD,
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


def database_row_shape_refusal(raw: Any) -> str | None:
    """The exact §6 code the structural validator refuses one wire row
    with, or ``None`` when its shape holds.

    The single shape decision for this lane lives in
    :mod:`fin.host.evidence.evidence_contract`; this seam wraps one row in a
    minimal conforming envelope so the frozen envelope checks (schema
    token, key set, types, bounds, timestamp) judge the row, and the
    admission boundary never re-implements them inline. The context
    seeding is the validator home's :func:`seed_context_from_rows` —
    one posture for every boundary, including its never-hash rule.
    """
    envelope = {
        DATABASE_ROWS_KEY: {
            DATABASE_ROWS_SCHEMA_FIELD: DATABASE_ROWS_SCHEMA,
            DATABASE_ROWS_SCHEMA_VERSION_FIELD: DATABASE_ROWS_SCHEMA_VERSION,
            DATABASE_ROWS_ROWS_FIELD: [raw],
            DATABASE_ROWS_RESULT_COUNT_FIELD: 1,
        }
    }
    context = seed_context_from_rows(database_row_values=[raw])
    validation = validate_evidence(envelope, context)
    if validation.refusal is None:
        return None
    return validation.refusal.code


def evidence_from_database_rows(rows: Sequence[Any] | None) -> list[EvidenceItem]:
    """One ``database-row`` ``EvidenceItem`` per readable wire row.

    The sql 档's consumer half (spec
    search-mode-business-sql-contract §8.0): a ledger row arrives as the
    ``database_rows`` sibling of ``chunks`` and is NOT a chunk — it has
    no document, no version, no ordinal, and bridging one into the
    ``Locator`` shape would put a fake file identity in front of the
    citation gate. Each row rides the d2c3aa0 ``DatabaseRow`` carrier
    with the empty document slots, and the gate's own row lane verifies
    the claim against ``record_key`` / ``fields`` (ADR 0091).

    A row the E1 structural validator refuses is DROPPED, never
    half-built: :func:`database_row_shape_refusal` holds that decision
    (a non-mapping, an off-shape key set, a bad schema token, an
    unknown field, a non-strict timestamp — each named by its §6 code
    at the boundary). The empty-value guard below stays a separate
    carrier check the shape validator does not own: an all-keys-present
    row with an empty ``source`` / ``record_key`` / ``queried_at`` /
    ``summary`` is shape-legal, but ``_validate_evidence`` raises
    ``MalformedEvidenceError`` on an empty ``unit_text``, so such a row
    would refuse the whole run instead of simply not being citable.
    ``None`` input is the absent sibling (no rows applicable) and builds
    nothing.
    """
    items: list[EvidenceItem] = []
    for raw in rows or ():
        if database_row_shape_refusal(raw) is not None:
            continue
        row = DatabaseRow.from_wire(raw)
        if (
            not row.source
            or not row.record_key
            or not row.queried_at
            or not row.summary
        ):
            continue
        items.append(
            EvidenceItem(
                locator=Locator(document_id="", version="", unit=1),
                unit_text=row.summary,
                kind=DATABASE_ROW_KIND,
                display=DATABASE_ROW_DISPLAY,
                database_row=(row,),
            )
        )
    return items


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
    "database_row_shape_refusal",
    "evidence_from_chunks",
    "evidence_from_database_rows",
    "unit_ordinal",
]
