"""The typed evidence path a visual claim travels on (ADR 0089, spec T5).

ADR 0089 separates the two kinds of support a published turn may rest on:
text is citable only after a current-run ``retrieve`` / ``read_source``
returned it, while "generated visual observations use a separate typed
evidence path and are never source quotes". The quote channel therefore
cannot carry a visual claim — :mod:`fin.host.citation_gate` closes
``QUOTABLE_EVIDENCE_KINDS`` to the company and attachment legs precisely
so an observation's prose can never be harvested as a verbatim span.

This module is the other channel. One :class:`VisualEvidence` is minted
only by a read that happened inside the current run, and every field of
its provenance comes from the stored observation envelope, never from
what the model asserted about it — the same rule that keeps a
:func:`~fin.host.citation_gate.verify_citations` citation honest. The
token names the attachment, its content version, the source unit, the
physical page, the region actually looked at (``scope`` — ``None`` is the
whole rendered page), the observation version that answered, the provider
and model that produced it, and the SHA-256 of the raster it was produced
from, which is what links the claim back to the original location.

Nothing here quotes: ``answer_text`` travels to the model as derived
material, and the row this module emits carries the provenance instead of
a span.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

VISUAL_EVIDENCE_KIND: Final[str] = "visual_observation"

# The typed exits of a visual read. Each names one reason the run holds no
# observation for the ask; the model sees the code, the operator sees it in
# the turn's tool row, and no code is ever reused for two causes.
VISUAL_UNAVAILABLE: Final[str] = "attachment_unavailable"
VISUAL_PAGE_UNAVAILABLE: Final[str] = "page_unavailable"
VISUAL_DETAIL_INVALID: Final[str] = "detail_target_invalid"
VISUAL_SCOPE_INVALID: Final[str] = "crop_scope_invalid"
VISUAL_LANE_UNWIRED: Final[str] = "visual_lane_unwired"
VISUAL_BUDGET_EXCEEDED: Final[str] = "vision_budget_exceeded"
VISUAL_PROVIDER_FAILED: Final[str] = "vision_call_failed"
VISUAL_STALE: Final[str] = "observation_stale"

VISUAL_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        VISUAL_UNAVAILABLE,
        VISUAL_PAGE_UNAVAILABLE,
        VISUAL_DETAIL_INVALID,
        VISUAL_SCOPE_INVALID,
        VISUAL_LANE_UNWIRED,
        VISUAL_BUDGET_EXCEEDED,
        VISUAL_PROVIDER_FAILED,
        VISUAL_STALE,
    }
)

# The visual-claim wire row IS this key set; a consumer that reads a field
# not listed here is guessing, and a row missing one is not a claim.
VISUAL_CLAIM_FIELDS: Final[tuple[str, ...]] = (
    "kind",
    "attachment_id",
    "version",
    "unit",
    "page",
    "observation_version",
    "scope",
    "provider",
    "model_version",
    "page_image_sha256",
    "detail",
    "filename",
    "reused",
    "outbound_calls",
)


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    """One observation this run read, with the provenance that backs a claim.

    ``scope`` is the four normalized crop bounds the provider looked at, or
    ``None`` for the whole rendered page; it is the field that makes a
    crop-scoped observation unable to answer a whole-page question.
    ``reused`` records that the stored observation already covered the ask,
    so no upload and no vision call happened, and ``outbound_calls`` is the
    attempt count that left the process for this read.
    """

    attachment_id: str
    version: str
    version_int: int
    unit: int
    page: int
    observation_version: int
    scope: tuple[float, float, float, float] | None
    provider: str
    model_version: str
    page_image_sha256: str
    detail: str
    filename: str
    reused: bool
    outbound_calls: int

    def as_claim_row(self) -> dict[str, Any]:
        """The persisted / wire shape of this token.

        ``answer_text`` is deliberately absent: the row is provenance for a
        claim, and shipping the observation's prose alongside it is how a
        derived note would end up rendered as though it were a quote.
        """
        return {
            "kind": VISUAL_EVIDENCE_KIND,
            "attachment_id": self.attachment_id,
            "version": self.version,
            "unit": self.unit,
            "page": self.page,
            "observation_version": self.observation_version,
            "scope": list(self.scope) if self.scope is not None else None,
            "provider": self.provider,
            "model_version": self.model_version,
            "page_image_sha256": self.page_image_sha256,
            "detail": self.detail,
            "filename": self.filename,
            "reused": self.reused,
            "outbound_calls": self.outbound_calls,
        }


@dataclass(frozen=True, slots=True)
class VisualObservation:
    """One stored observation this read earned: its provenance and its prose.

    The pairing is the point. ``token`` is what the run may persist and cite;
    ``text`` is what the model is told this turn. Only the token has a row —
    the derived prose has no verbatim span behind it, so it travels the tool
    result and stops there.
    """

    token: VisualEvidence
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class VisualReadOutcome:
    """Exactly one side of a visual read is populated.

    ``reads`` holds what this read earned — one entry per stored observation
    covering the target, since joint coverage may be granted by more than one
    — and ``refusal`` is one code from :data:`VISUAL_REFUSAL_CODES` that
    travels with no reads at all.
    """

    reads: tuple[VisualObservation, ...] = ()
    refusal: str | None = None

    @property
    def evidence(self) -> tuple[VisualEvidence, ...]:
        return tuple(read.token for read in self.reads)

    @property
    def granted(self) -> bool:
        return bool(self.reads)


def visual_crop_bounds(scope: Any) -> tuple[float, float, float, float] | None:
    """The crop bounds a stored scope carries, or ``None`` for whole page.

    A scope this face does not understand is reported as whole-page rather
    than guessed at: the token may then claim no more than the page it read,
    which is the fail-closed direction for a claim about a region.
    """
    if scope is None:
        return None
    left = getattr(scope, "left", None)
    top = getattr(scope, "top", None)
    right = getattr(scope, "right", None)
    bottom = getattr(scope, "bottom", None)
    if not (
        isinstance(left, (int, float))
        and isinstance(top, (int, float))
        and isinstance(right, (int, float))
        and isinstance(bottom, (int, float))
    ):
        return None  # EXIT: an unreadable scope claims no region.
    return (float(left), float(top), float(right), float(bottom))


__all__ = [
    "VISUAL_BUDGET_EXCEEDED",
    "VISUAL_CLAIM_FIELDS",
    "VISUAL_DETAIL_INVALID",
    "VISUAL_EVIDENCE_KIND",
    "VISUAL_LANE_UNWIRED",
    "VISUAL_PAGE_UNAVAILABLE",
    "VISUAL_PROVIDER_FAILED",
    "VISUAL_REFUSAL_CODES",
    "VISUAL_SCOPE_INVALID",
    "VISUAL_STALE",
    "VISUAL_UNAVAILABLE",
    "VisualEvidence",
    "VisualObservation",
    "VisualReadOutcome",
    "visual_crop_bounds",
]
