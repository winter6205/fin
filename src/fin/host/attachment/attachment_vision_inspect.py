"""Run-side visual re-inspection: cover what the run's observations missed.

When a visual question's required details are not covered by the
observations already read into the current run, the stored original page is
looked at again — at high resolution, through a reliable crop when the
caller knows the region, or over the whole page when the target or region
is unknown — and the answer becomes a NEW persisted observation version.
This module owns that one decision path and its outbound calls: the reuse
verdict itself belongs to :mod:`fin.host.attachment.attachment_observations`, the
spending to :mod:`fin.host.attachment.attachment_budgets`, and the provider leg to
:mod:`fin.host.attachment.attachment_vision`.

Bytes boundary: the durable store keeps a page raster's SHA-256, not its
pixels, so the image to inspect is caller-supplied — the host seam that
holds or re-renders the durable page image passes those bytes in. A crop
therefore arrives twice: ``image`` carries the high-resolution pixels the
call will actually send, and ``crop`` records the normalized page-image
bounds those pixels cover, so the persisted envelope never overstates what
was looked at.

Spending is settled before an attempt goes out and is never refunded. A
crop draws on the run's crop headroom and is never retried, whatever the
failure was; a full-page re-inspection draws as a full-page attempt and may
spend the run's one reserved transport retry, which a crop never touches.

"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Final, Sequence

from fin.host.attachment.attachment_budgets import VisionCallLedger, send_full_page
from fin.host.attachment.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    CoveredDetail,
    DetailKey,
    ObservationAttempt,
    ObservationEnvelope,
    observation_from_call_result,
    observations_jointly_cover,
    plotted_value_key,
)
from fin.host.attachment.attachment_vision import (
    VisionCallResult,
    VisionCrop,
    VisionPort,
    VisionTarget,
)

__all__ = [
    "InspectionOutcome",
    "PageIdentity",
    "inspect_for_details",
    "plotted_value_covered",
]


_QUESTION_REQUIRED: Final[str] = (
    "question must be a non-empty visual ask; a blank one would silently "
    "send the page-scan prompt instead of the detail request"
)


@dataclass(frozen=True, slots=True)
class InspectionOutcome:
    """The run's answer to one visual detail request.

    Exactly one side is populated: ``reused`` carries the current-run
    observations that already covered the ask (no call, no charge), while
    ``observation`` carries the newly minted envelope after an inspection.
    ``outbound_calls`` counts attempts that left the process, failures
    included.
    """

    observation: ObservationEnvelope | None = None
    reused: tuple[ObservationEnvelope, ...] = ()
    outbound_calls: int = 0
    retry_used: bool = False


@dataclass(frozen=True, slots=True)
class PageIdentity:
    """One physical page's provenance, carried as a single value.

    These five fields are the clump shared by :class:`VisionTarget` (the
    outbound call) and :class:`ObservationEnvelope` (the persisted record);
    grouping them here lets a re-inspection take one parameter and bridge
    the two shapes without re-forwarding fields. Field validity is enforced
    where each shape is built — ``to_target`` and the envelope factory —
    not duplicated here.
    """

    attachment_id: str
    attachment_version: int
    source_unit: int
    page: int
    page_image_sha256: str

    def to_target(self, crop: VisionCrop | None = None) -> VisionTarget:
        """The provider-side view: identity plus the region looked at."""
        return VisionTarget(
            attachment_id=self.attachment_id,
            version=str(self.attachment_version),
            page=self.page,
            crop=crop,
        )


def plotted_value_covered(
    *,
    chart: str,
    series: str,
    category: str,
    scope: VisionCrop | None = None,
) -> CoveredDetail:
    """One chart/series/category plotted value as a covered-detail record.

    The key is built by :func:`~fin.host.attachment.attachment_observations.plotted_value_key`,
    so a crop answer can only grant the exact target the reuse decision
    will later demand; a partial binding fails there, never here.
    """
    return CoveredDetail(
        key=plotted_value_key(chart=chart, series=series, category=category),
        scope=scope,
    )


def inspect_for_details(
    port: VisionPort,
    ledger: VisionCallLedger,
    *,
    image: bytes,
    media_type: str,
    question: str,
    required_details: Sequence[DetailKey],
    requested_scope: VisionCrop | None,
    run_observations: Sequence[ObservationEnvelope],
    created_at: datetime,
    identity: PageIdentity,
    crop: VisionCrop | None = None,
    covered_details_for: (
        Callable[[VisionCallResult], Sequence[CoveredDetail]] | None
    ) = None,
) -> InspectionOutcome:
    """Reuse what the run read, else inspect the stored original once more.

    The page is addressed by one :class:`PageIdentity` in ``identity``; a
    missing or ``None`` identity is a :class:`TypeError` before anything is
    charged. ``crop`` is the reliable region: giving one sends exactly one
    crop inspection over ``image``; leaving it ``None`` re-inspects the full
    page. ``covered_details_for`` maps the provider's structured answer
    onto detail keys and is caller-supplied so this module stays
    host-deterministic — nothing here reads prose for coverage. The new
    envelope is not persisted here; the caller writes it through the
    store's monotonic-version rule.
    """
    if not isinstance(identity, PageIdentity):
        raise TypeError(
            "inspect_for_details requires identity=PageIdentity(...); the "
            "five page fields travel as one value object"
        )  # EXIT: no identity means no envelope provenance to bind
    if not isinstance(image, (bytes, bytearray)) or not image:
        raise ValueError(
            "image must be non-empty caller-supplied page or crop bytes"
        )  # EXIT: without the durable raster bytes there is nothing to send
    if not isinstance(question, str) or not question.strip():
        raise ValueError(_QUESTION_REQUIRED)  # EXIT: a blank ask is not a question
    return _inspect(
        port,
        ledger,
        image=image,
        media_type=media_type,
        question=question,
        required_details=required_details,
        requested_scope=requested_scope,
        run_observations=run_observations,
        created_at=created_at,
        identity=identity,
        crop=crop,
        covered_details_for=covered_details_for,
    )


def _same_source(
    run_observations: Sequence[ObservationEnvelope], identity: PageIdentity
) -> tuple[ObservationEnvelope, ...]:
    return tuple(
        observation
        for observation in run_observations
        if observation.source_unit == identity.source_unit
        and observation.page == identity.page
    )


def _inspect(
    port: VisionPort,
    ledger: VisionCallLedger,
    *,
    image: bytes,
    media_type: str,
    question: str,
    required_details: Sequence[DetailKey],
    requested_scope: VisionCrop | None,
    run_observations: Sequence[ObservationEnvelope],
    created_at: datetime,
    identity: PageIdentity,
    crop: VisionCrop | None,
    covered_details_for: (
        Callable[[VisionCallResult], Sequence[CoveredDetail]] | None
    ),
) -> InspectionOutcome:
    """The one decision path, with the page addressed by ``identity``."""
    same_source = _same_source(run_observations, identity)
    if observations_jointly_cover(
        same_source,
        required_details,
        requested_scope,
        attachment_version=identity.attachment_version,
        page_image_sha256=identity.page_image_sha256,
        coverage_schema_version=COVERAGE_SCHEMA_VERSION,
    ):
        # EXIT: covered — the current-run observations return untouched and
        # not one call is charged.
        return InspectionOutcome(reused=same_source)
    target = identity.to_target(crop)
    if crop is not None:
        result = _send_crop(
            port, ledger, target=target, image=image, media_type=media_type,
            question=question,
        )
        failed_attempts: tuple[ObservationAttempt, ...] = ()
        retry_used = False
    else:
        result, failed_attempts, retry_used = _send_full_page(
            port, ledger, target=target, image=image, media_type=media_type,
            question=question,
        )
    observation = _mint_observation(
        result,
        identity=identity,
        same_source=same_source,
        created_at=created_at,
        covered_details_for=covered_details_for,
        failed_attempts=failed_attempts,
    )
    return InspectionOutcome(
        observation=observation,
        outbound_calls=len(failed_attempts) + 1,
        retry_used=retry_used,
    )


def _mint_observation(
    result: VisionCallResult,
    *,
    identity: PageIdentity,
    same_source: Sequence[ObservationEnvelope],
    created_at: datetime,
    covered_details_for: (
        Callable[[VisionCallResult], Sequence[CoveredDetail]] | None
    ),
    failed_attempts: tuple[ObservationAttempt, ...],
) -> ObservationEnvelope:
    """Bind the answered call to the store's next version for this page."""
    # WHY the highest prior plus one: the store sequences observation
    # versions per content version, unit, and page, so the new version is
    # minted against exactly that sequence — priors of other pages cannot
    # push it.
    next_version = max(
        (observation.observation_version for observation in same_source),
        default=0,
    ) + 1
    details = (
        () if covered_details_for is None else tuple(covered_details_for(result))
    )
    observation = observation_from_call_result(
        result,
        observation_version=next_version,
        source_unit=identity.source_unit,
        page_image_sha256=identity.page_image_sha256,
        # The port reports only the model that answered and this deployment
        # configures one provider through it — the same convention the
        # first-preparation scan uses.
        provider=result.model_id,
        created_at=created_at,
        covered_details=details,
    )
    if failed_attempts:
        # A retried-away failure has no provider numbers, and recording it
        # as usage-none/cost-none keeps the attempt count honest without
        # inventing zeros.
        observation = dataclasses.replace(
            observation, attempts=tuple(failed_attempts) + observation.attempts
        )
    return observation


def _send_crop(
    port: VisionPort,
    ledger: VisionCallLedger,
    *,
    target: VisionTarget,
    image: bytes,
    media_type: str,
    question: str,
) -> VisionCallResult:
    """Exactly one crop attempt, charged before it leaves the door.

    The crop sub-cap is checked by the charge itself, so a run out of crop
    headroom exits :class:`~fin.host.attachment.attachment_budgets.AttachmentVisionBudgetExceeded`
    with nothing sent. Port errors are surfaced uncaught: a crop is never
    retried, retryable or not, and the ledger keeps the charge either way.
    """
    ledger.charge_crop_attempt()
    return port.inspect(
        image=bytes(image), target=target, media_type=media_type, question=question
    )


def _send_full_page(
    port: VisionPort,
    ledger: VisionCallLedger,
    *,
    target: VisionTarget,
    image: bytes,
    media_type: str,
    question: str,
) -> tuple[VisionCallResult, tuple[ObservationAttempt, ...], bool]:
    """One full-page re-inspection through the run's single send ladder."""
    send = send_full_page(
        port,
        ledger,
        image=image,
        target=target,
        media_type=media_type,
        question=question,
    )
    failed = (ObservationAttempt(),) if send.retry_used else ()
    return send.result, failed, send.retry_used
