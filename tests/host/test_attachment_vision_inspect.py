"""Run-side crop re-inspection: reuse, one crop, the reserved retry, versions.

These cases pin the inspection-side contract of the visual-evidence budget:
* observations already read into the run that jointly cover the ask end the
  question with zero calls and zero charges;
* an exact detail key whose observed scope does not contain the requested
  scope is insufficient and triggers one inspection;
* a reliable crop costs exactly one call and is never retried, even on a
  retryable transport failure; without a reliable region the full page is
  re-inspected and the run's single reserved retry may be spent — never by
  a crop;
* the ninth crop of a run is refused before anything is sent;
* a new envelope follows the store's monotonic sequence (highest prior
  version of the same source plus one), keeps the page-image hash, records
  the inspected scope, and carries provider-reported usage/cost per
  attempt, absence staying absence.

The provider is a :class:`FakeVisionPort` throughout: no network, no
credentials, no live model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from fin.host.attachment.attachment_budgets import (
    ATTACHMENT_VISION_BUDGET_EXCEEDED,
    AttachmentBudgets,
    AttachmentVisionBudgetExceeded,
    VisionCallLedger,
)
from fin.host.attachment.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    CoveredDetail,
    DetailKey,
    ObservationContractError,
    ObservationEnvelope,
    plotted_value_key,
)
from fin.host.attachment.attachment_vision import (
    DEFAULT_MEDIA_TYPE,
    VISION_CALL_FAILED,
    VisionCallFailedError,
    VisionCallResult,
    VisionCost,
    VisionCrop,
    VisionEmptyTextError,
    VisionErrorClass,
    VisionTarget,
    VisionUsage,
)
from fin.host.attachment.attachment_vision_inspect import (
    InspectionOutcome,
    PageIdentity,
    inspect_for_details,
    plotted_value_covered,
)

STAMP = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PAGE_HASH = "aa" * 32
ATTACHMENT_ID = "att-1"
VERSION = 7
UNIT = 3
PAGE = 3
PAGE_BYTES = b"\x89PNG fake page raster"
QUESTION = "What is Total revenue plotted for Q3 2024?"
MODEL_ID = "g4-fake-vision-1"

REVENUE = plotted_value_key(
    chart="Revenue by quarter", series="Total revenue", category="Q3 2024"
)
MARGIN = plotted_value_key(
    chart="Revenue by quarter", series="Operating margin", category="Q3 2024"
)
TABLE_FACT = DetailKey(kind="table", target_key="income_statement|Q3|revenue")
ABSENT = DetailKey(kind="footer_note", target_key="sources")

CHART_CROP = VisionCrop(left=0.1, top=0.2, right=0.6, bottom=0.7)
CROP_TOP_LEFT = VisionCrop(left=0.0, top=0.0, right=0.5, bottom=0.5)

USAGE = VisionUsage(input_tokens=120, output_tokens=40, total_tokens=160)


class FakeVisionPort:
    """``VisionPort`` witness: scripted per-call outcomes, every call kept."""

    def __init__(
        self,
        *,
        outcomes: list[Any] | None = None,
        usage: VisionUsage | None = USAGE,
        cost: VisionCost | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = list(outcomes or [])
        self._usage = usage
        self._cost = cost

    def inspect(
        self,
        *,
        image: bytes,
        target: VisionTarget,
        media_type: str = DEFAULT_MEDIA_TYPE,
        question: str | None = None,
    ) -> VisionCallResult:
        self.calls.append(
            {
                "image": image,
                "target": target,
                "media_type": media_type,
                "question": question,
            }
        )
        outcome = self._outcomes.pop(0) if self._outcomes else "noted"
        if isinstance(outcome, Exception):
            raise outcome
        return VisionCallResult(
            text=outcome,
            target=target,
            model_id=MODEL_ID,
            media_type=media_type,
            usage=self._usage,
            cost=self._cost,
        )


def transport_failure() -> VisionCallFailedError:
    return VisionCallFailedError(
        "vision provider call failed: APITimeoutError",
        error_class=VisionErrorClass.TRANSPORT,
    )


def permanent_failure() -> VisionCallFailedError:
    return VisionCallFailedError(
        "vision provider call failed: BadRequestError",
        error_class=VisionErrorClass.PERMANENT,
    )


def budgets(crop_cap: int = 8) -> AttachmentBudgets:
    return AttachmentBudgets(
        max_upload_bytes=4 * 1024 * 1024,
        max_pdf_pages=2,
        max_vision_calls_per_run=2 + crop_cap + 1,
        max_vision_crop_calls_per_run=crop_cap,
    )


def stored_observation(
    observation_version: int,
    *,
    covered: tuple[CoveredDetail, ...] = (),
    scope: VisionCrop | None = None,
    source_unit: int = UNIT,
    page: int = PAGE,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        attachment_id=ATTACHMENT_ID,
        coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        observation_version=observation_version,
        attachment_version=VERSION,
        source_unit=source_unit,
        page=page,
        page_image_sha256=PAGE_HASH,
        provider=MODEL_ID,
        model_version=MODEL_ID,
        scope=scope,
        covered_details=covered,
        created_at=STAMP,
    )


def whole_page_detail(key: DetailKey) -> CoveredDetail:
    return CoveredDetail(key=key, scope=None)


PAGE_IDENTITY = PageIdentity(
    attachment_id=ATTACHMENT_ID,
    attachment_version=VERSION,
    source_unit=UNIT,
    page=PAGE,
    page_image_sha256=PAGE_HASH,
)


def run_inspect(
    port: FakeVisionPort,
    ledger: VisionCallLedger,
    **overrides: Any,
) -> InspectionOutcome:
    kwargs: dict[str, Any] = {
        "image": PAGE_BYTES,
        "media_type": DEFAULT_MEDIA_TYPE,
        "question": QUESTION,
        "required_details": (REVENUE,),
        "requested_scope": None,
        "run_observations": (),
        "identity": PAGE_IDENTITY,
        "created_at": STAMP,
    }
    kwargs.update(overrides)
    return inspect_for_details(port, ledger, **kwargs)


# ----- pin 1: the reuse path costs nothing ---------------------------------


def test_joint_coverage_across_two_run_observations_makes_no_call():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    first = stored_observation(1, covered=(whole_page_detail(REVENUE),))
    second = stored_observation(2, covered=(whole_page_detail(TABLE_FACT),))

    outcome = run_inspect(
        port,
        ledger,
        required_details=(REVENUE, TABLE_FACT),
        run_observations=(first, second),
    )

    assert port.calls == []
    assert outcome.observation is None
    assert outcome.reused == (first, second)
    assert outcome.outbound_calls == 0
    assert ledger.calls_spent == 0


def test_single_covering_observation_returns_the_read_set_unchanged():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    covering = stored_observation(3, covered=(whole_page_detail(REVENUE),))
    other = stored_observation(4, covered=(whole_page_detail(ABSENT),))

    outcome = run_inspect(
        port, ledger, run_observations=(covering, other)
    )

    assert port.calls == []
    assert outcome.reused == (covering, other)
    assert ledger.calls_spent == 0


def test_exact_key_with_scope_not_contained_is_insufficient():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    # REVENUE is covered exactly, but only inside a corner crop; a
    # whole-page ask (requested_scope None) is not contained by that crop.
    partial = stored_observation(
        1,
        covered=(CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),),
        scope=CROP_TOP_LEFT,
    )

    outcome = run_inspect(
        port, ledger, run_observations=(partial,), crop=CHART_CROP
    )

    assert len(port.calls) == 1
    assert outcome.observation is not None


# ----- pin 2: a reliable crop is exactly one call, never retried -----------


def test_reliable_crop_sends_one_charged_call_with_the_question():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    outcome = run_inspect(port, ledger, crop=CHART_CROP)

    assert len(port.calls) == 1
    assert port.calls[0]["target"].crop == CHART_CROP
    assert port.calls[0]["question"] == QUESTION
    assert port.calls[0]["image"] == PAGE_BYTES
    assert ledger.crop_calls_spent == 1
    assert ledger.retries_spent == 0
    assert outcome.outbound_calls == 1
    assert outcome.retry_used is False


def test_retryable_transport_failure_on_a_crop_is_never_retried():
    port = FakeVisionPort(outcomes=[transport_failure()])
    ledger = VisionCallLedger(budgets())

    with pytest.raises(VisionCallFailedError) as raised:
        run_inspect(port, ledger, crop=CHART_CROP)

    assert raised.value.retryable is True
    assert raised.value.code == VISION_CALL_FAILED
    assert len(port.calls) == 1
    # the failed attempt keeps its charge and buys no retry
    assert ledger.crop_calls_spent == 1
    assert ledger.retries_spent == 0


def test_unusable_crop_answer_is_the_ports_typed_exit():
    port = FakeVisionPort(outcomes=[VisionEmptyTextError("no text")])
    ledger = VisionCallLedger(budgets())

    with pytest.raises(VisionEmptyTextError):
        run_inspect(port, ledger, crop=CHART_CROP)

    assert len(port.calls) == 1
    assert ledger.retries_spent == 0


# ----- pin 3: no reliable region re-inspects the full page ------------------


def test_full_page_retryable_failure_spends_the_reserved_retry_once():
    port = FakeVisionPort(outcomes=[transport_failure(), "14,888 million"])
    ledger = VisionCallLedger(budgets())

    outcome = run_inspect(port, ledger)

    assert len(port.calls) == 2
    assert all(call["target"].crop is None for call in port.calls)
    assert ledger.full_page_calls_spent == 2
    assert ledger.crop_calls_spent == 0  # the reserve is never a crop's
    assert ledger.retries_spent == 1
    assert outcome.outbound_calls == 2
    assert outcome.retry_used is True
    assert outcome.observation is not None
    attempts = outcome.observation.attempts
    assert len(attempts) == 2
    # the retried-away failure has no provider numbers: usage None, cost None
    assert attempts[0].usage is None
    assert attempts[0].cost is None
    assert attempts[1].usage == USAGE


def test_permanent_full_page_failure_is_not_retried():
    port = FakeVisionPort(outcomes=[permanent_failure()])
    ledger = VisionCallLedger(budgets())

    with pytest.raises(VisionCallFailedError) as raised:
        run_inspect(port, ledger)

    assert raised.value.retryable is False
    assert len(port.calls) == 1
    assert ledger.retries_spent == 0
    assert ledger.full_page_calls_spent == 1


def test_full_page_empty_answer_is_not_retried():
    port = FakeVisionPort(outcomes=[VisionEmptyTextError("no text")])
    ledger = VisionCallLedger(budgets())

    with pytest.raises(VisionEmptyTextError):
        run_inspect(port, ledger)

    assert len(port.calls) == 1
    assert ledger.retries_spent == 0


def test_second_full_page_failure_is_final():
    port = FakeVisionPort(outcomes=[transport_failure(), transport_failure()])
    ledger = VisionCallLedger(budgets())

    with pytest.raises(VisionCallFailedError):
        run_inspect(port, ledger)

    assert len(port.calls) == 2
    assert ledger.retries_spent == 1


def test_spent_reserve_leaves_a_retryable_failure_unretried():
    port = FakeVisionPort(outcomes=[transport_failure()])
    ledger = VisionCallLedger(budgets())
    ledger.charge_full_page_attempt(retry=True)  # spent elsewhere in the run

    with pytest.raises(VisionCallFailedError):
        run_inspect(port, ledger)

    assert len(port.calls) == 1
    assert ledger.retries_spent == 1


# ----- pin 4: the ninth crop never leaves the process -----------------------


def test_ninth_crop_of_a_run_is_refused_before_sending():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets(crop_cap=8))
    for _ in range(8):
        ledger.charge_crop_attempt()

    with pytest.raises(AttachmentVisionBudgetExceeded) as raised:
        run_inspect(port, ledger, crop=CHART_CROP)

    assert raised.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert port.calls == []
    assert ledger.crop_calls_spent == 8


# ----- pin 5: the new envelope's identity, version, and coverage ------------


def test_new_envelope_continues_the_source_sequence_and_keeps_identity():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    priors = (
        stored_observation(1, covered=(whole_page_detail(ABSENT),)),
        stored_observation(4, covered=(whole_page_detail(MARGIN),)),
        # another page's higher version must not push this page's sequence
        stored_observation(9, covered=(whole_page_detail(ABSENT),), page=PAGE + 1),
    )

    def details_from_answer(result: VisionCallResult) -> tuple[CoveredDetail, ...]:
        assert result.text == "noted"
        return (
            plotted_value_covered(
                chart="Revenue by quarter",
                series="Total revenue",
                category="Q3 2024",
                scope=CHART_CROP,
            ),
        )

    outcome = run_inspect(
        port,
        ledger,
        crop=CHART_CROP,
        run_observations=priors,
        covered_details_for=details_from_answer,
    )

    observation = outcome.observation
    assert observation is not None
    assert observation.observation_version == 5
    assert observation.page_image_sha256 == PAGE_HASH
    assert observation.attachment_id == ATTACHMENT_ID
    assert observation.attachment_version == VERSION
    assert observation.source_unit == UNIT
    assert observation.page == PAGE
    assert observation.scope == CHART_CROP
    assert observation.provider == MODEL_ID
    assert observation.model_version == MODEL_ID
    assert observation.created_at == STAMP
    assert observation.covered_details == (
        CoveredDetail(key=REVENUE, scope=CHART_CROP),
    )


def test_whole_page_reinspection_records_scope_as_whole_page():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    outcome = run_inspect(port, ledger)

    observation = outcome.observation
    assert observation is not None
    assert observation.scope is None
    assert observation.observation_version == 1


def test_crop_answer_claiming_more_than_the_crop_is_refused():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    def overreaching(result: VisionCallResult) -> tuple[CoveredDetail, ...]:
        # a whole-page detail cannot be granted by a crop observation
        return (whole_page_detail(REVENUE),)

    with pytest.raises(ObservationContractError):
        run_inspect(port, ledger, crop=CHART_CROP, covered_details_for=overreaching)


# ----- pin 6: per-attempt usage and cost, absence stays absence -------------


def test_reported_usage_and_absent_cost_are_recorded_per_attempt():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    outcome = run_inspect(port, ledger, crop=CHART_CROP)

    observation = outcome.observation
    assert observation is not None
    assert len(observation.attempts) == 1
    assert observation.attempts[0].usage == USAGE
    assert observation.attempts[0].cost is None


def test_reported_zero_cost_stays_distinct_from_silence():
    port = FakeVisionPort(cost=VisionCost(amount=Decimal("0"), currency="USD"))
    ledger = VisionCallLedger(budgets())

    outcome = run_inspect(port, ledger, crop=CHART_CROP)

    cost = outcome.observation.attempts[0].cost
    assert cost is not None
    assert cost.amount == Decimal("0")
    assert cost.currency == "USD"


# ----- pin 7: multiple crops in one run charge separately -------------------


def test_two_crops_in_one_run_each_take_their_own_charge_and_version():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    first = run_inspect(port, ledger, crop=CHART_CROP)
    second = run_inspect(
        port,
        ledger,
        crop=CROP_TOP_LEFT,
        required_details=(MARGIN,),
        run_observations=(first.observation,),
    )

    assert len(port.calls) == 2
    assert ledger.crop_calls_spent == 2
    assert ledger.calls_spent == 2
    assert first.observation.observation_version == 1
    assert second.observation.observation_version == 2
    assert second.observation.scope == CROP_TOP_LEFT


# ----- input guards ----------------------------------------------------------


def test_empty_image_is_refused_before_any_charge():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    with pytest.raises(ValueError, match="non-empty"):
        run_inspect(port, ledger, image=b"", crop=CHART_CROP)

    assert port.calls == []
    assert ledger.calls_spent == 0


def test_blank_question_is_refused_before_any_charge():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    with pytest.raises(ValueError, match="question"):
        run_inspect(port, ledger, question="   ")

    assert port.calls == []
    assert ledger.calls_spent == 0


# ----- page identity: one value object, refusals before any charge ----------


def test_a_missing_identity_is_refused_before_any_charge():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    kwargs: dict[str, Any] = {
        "image": PAGE_BYTES,
        "media_type": DEFAULT_MEDIA_TYPE,
        "question": QUESTION,
        "required_details": (REVENUE,),
        "requested_scope": None,
        "run_observations": (),
        "created_at": STAMP,
    }

    with pytest.raises(TypeError, match="identity"):
        inspect_for_details(port, ledger, **kwargs)

    assert port.calls == []
    assert ledger.calls_spent == 0


def test_a_none_identity_is_refused_before_any_charge():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    with pytest.raises(TypeError, match="identity"):
        run_inspect(port, ledger, identity=None)

    assert port.calls == []
    assert ledger.calls_spent == 0


def test_a_stray_page_keyword_is_refused():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())

    with pytest.raises(TypeError, match="unexpected keyword"):
        run_inspect(port, ledger, attachment_page=PAGE)

    assert port.calls == []
    assert ledger.calls_spent == 0


def test_identity_object_drives_the_reuse_verdict():
    port = FakeVisionPort()
    ledger = VisionCallLedger(budgets())
    covering = stored_observation(1, covered=(whole_page_detail(REVENUE),))

    outcome = run_inspect(port, ledger, run_observations=(covering,))

    assert port.calls == []
    assert outcome.reused == (covering,)
    assert ledger.calls_spent == 0
