"""Versioned visual-observation envelopes, the deterministic reuse check, and
their durable side in the attachment store.

These cases pin three contracts:
* the envelope carries the full provenance an observation must never lose
  (coverage schema, observation version, content version, unit/page, original
  page-image hash, provider/model, scope, covered details, per-attempt
  provider-reported numbers);
* reuse is decided by exact key equality and scope containment only — no
  semantic or fuzzy matching, and every unknown or incomplete shape answers
  "insufficient";
* observation writes lose the race with a delete exactly like unit and index
  writes do, and observation versions stay monotonic per content version and
  source unit/page.

The Postgres half follows the repo convention of
``tests/host/test_attachment_store.py``: env-gated on ``FIN_CHECKPOINT_URL``
and skipped when no Postgres is reachable.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

import pytest

from fin.host.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    CoveredDetail,
    DetailKey,
    ObservationAttempt,
    ObservationContractError,
    ObservationEnvelope,
    observation_covers,
    observation_from_call_result,
    observations_jointly_cover,
    plotted_value_key,
)
from fin.host.attachment_store import (
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.attachment_vision import (
    VisionCallResult,
    VisionCost,
    VisionCrop,
    VisionTarget,
    VisionUsage,
)
from fin.host.thread_store import InMemoryThreadStore

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

STAMP = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PAGE_HASH = "aa" * 32
OTHER_PAGE_HASH = "bb" * 32

REVENUE = plotted_value_key(
    chart="Revenue by quarter", series="Total revenue", category="Q3 2024"
)
SAME_CHART_OTHER_SERIES = plotted_value_key(
    chart="Revenue by quarter", series="Operating margin", category="Q3 2024"
)
UNRELATED_CHART = plotted_value_key(
    chart="Headcount trend", series="Employees", category="Q3 2024"
)
GENERIC_PLOTTED = DetailKey(kind="plotted_value")
TABLE_FACT = DetailKey(kind="table", target_key="income_statement|Q3|revenue")

CROP_TOP_LEFT = VisionCrop(left=0.0, top=0.0, right=0.5, bottom=0.5)
CROP_INSIDE = VisionCrop(left=0.1, top=0.1, right=0.4, bottom=0.3)
CROP_OVERLAPPING = VisionCrop(left=0.25, top=0.25, right=0.75, bottom=0.75)

USAGE = VisionUsage(input_tokens=120, output_tokens=40, total_tokens=160)
COST = VisionCost(amount=Decimal("0.004"), currency="USD")


def _envelope(**changes: Any) -> ObservationEnvelope:
    """A whole-page observation over ``att-1`` v1, unit 1, page 2, covering
    ``REVENUE``; each case overrides only the field it is about."""
    fields: dict[str, Any] = {
        "attachment_id": "att-1",
        "coverage_schema_version": COVERAGE_SCHEMA_VERSION,
        "observation_version": 1,
        "attachment_version": 1,
        "source_unit": 1,
        "page": 2,
        "page_image_sha256": PAGE_HASH,
        "provider": "openai-compatible",
        "model_version": "provider-model-1",
        "scope": None,
        "covered_details": (CoveredDetail(key=REVENUE),),
        "attempts": (ObservationAttempt(usage=USAGE, cost=COST),),
        "created_at": STAMP,
    }
    fields.update(changes)
    return ObservationEnvelope(**fields)


def _covers(
    observation: ObservationEnvelope,
    required: tuple[DetailKey, ...],
    requested_scope: VisionCrop | None = None,
    **identity: Any,
) -> bool:
    checks: dict[str, Any] = {
        "attachment_version": 1,
        "page_image_sha256": PAGE_HASH,
        "coverage_schema_version": COVERAGE_SCHEMA_VERSION,
    }
    checks.update(identity)
    return observation_covers(observation, required, requested_scope, **checks)


# ----- envelope construction -----


def test_envelope_carries_every_persisted_provenance_field() -> None:
    observation = _envelope()
    assert observation.coverage_schema_version == COVERAGE_SCHEMA_VERSION
    assert observation.observation_version == 1
    assert observation.attachment_version == 1
    assert observation.source_unit == 1
    assert observation.page == 2
    assert observation.page_image_sha256 == PAGE_HASH
    assert observation.provider == "openai-compatible"
    assert observation.model_version == "provider-model-1"
    assert observation.scope is None
    assert observation.covered_details == (CoveredDetail(key=REVENUE),)
    assert observation.attempts == (ObservationAttempt(usage=USAGE, cost=COST),)
    assert observation.created_at == STAMP


def test_envelope_rejects_blank_identity_and_nonpositive_positions() -> None:
    with pytest.raises(ObservationContractError):
        _envelope(page_image_sha256="  ")
    with pytest.raises(ObservationContractError):
        _envelope(page=0)
    with pytest.raises(ObservationContractError):
        _envelope(source_unit=0)
    with pytest.raises(ObservationContractError):
        _envelope(observation_version=0)
    with pytest.raises(ObservationContractError):
        _envelope(attachment_version=0)


def test_envelope_requires_a_tz_aware_created_at_passed_by_the_caller() -> None:
    naive = datetime(2026, 10, 1, 12, 0, 0)
    with pytest.raises(ObservationContractError):
        _envelope(created_at=naive)


def test_envelope_refuses_a_covered_detail_scope_beyond_the_observed_scope() -> None:
    """A detail cannot claim coverage of a region the call never looked at."""
    with pytest.raises(ObservationContractError):
        _envelope(
            scope=CROP_TOP_LEFT,
            covered_details=(
                CoveredDetail(key=REVENUE, scope=CROP_OVERLAPPING),
            ),
        )
    with pytest.raises(ObservationContractError):
        _envelope(
            scope=CROP_TOP_LEFT,
            covered_details=(CoveredDetail(key=REVENUE, scope=None),),
        )


def test_detail_key_rejects_a_blank_kind_and_blank_target() -> None:
    with pytest.raises(ObservationContractError):
        DetailKey(kind="  ")
    with pytest.raises(ObservationContractError):
        DetailKey(kind="table", target_key="   ")


def test_plotted_value_key_requires_all_three_binding_parts() -> None:
    with pytest.raises(ObservationContractError):
        plotted_value_key(chart="Revenue", series="  ", category="Q3 2024")
    with pytest.raises(ObservationContractError):
        plotted_value_key(chart="Revenue|evil", series="Total", category="Q3 2024")
    key = plotted_value_key(
        chart="Revenue by quarter", series="Total revenue", category="Q3 2024"
    )
    assert key.kind == "plotted_value"
    assert key.target_key is not None
    assert key == REVENUE


# ----- deterministic reuse: exact key and scope containment -----


def test_exact_key_whole_page_observation_covers_whole_page_request() -> None:
    assert _covers(_envelope(), (REVENUE,)) is True


def test_whole_page_coverage_contains_a_crop_request() -> None:
    assert _covers(_envelope(), (REVENUE,), CROP_INSIDE) is True


def test_crop_coverage_never_answers_a_whole_page_request() -> None:
    observation = _envelope(
        scope=CROP_TOP_LEFT,
        covered_details=(CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),),
    )
    assert _covers(observation, (REVENUE,), None) is False


def test_crop_coverage_contains_only_crops_fully_inside_it() -> None:
    observation = _envelope(
        scope=CROP_TOP_LEFT,
        covered_details=(CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),),
    )
    assert _covers(observation, (REVENUE,), CROP_INSIDE) is True
    assert _covers(observation, (REVENUE,), CROP_OVERLAPPING) is False


def test_absent_detail_is_insufficient() -> None:
    assert _covers(_envelope(), (SAME_CHART_OTHER_SERIES,)) is False


def test_unrelated_plotted_value_does_not_answer_the_exact_chart_series_category_request() -> None:
    """Another chart's value, or the same chart's other series, is not a
    match for the requested (chart, series, category) triple."""
    covering_other = _envelope(
        covered_details=(
            CoveredDetail(key=UNRELATED_CHART),
            CoveredDetail(key=SAME_CHART_OTHER_SERIES),
        )
    )
    assert _covers(covering_other, (REVENUE,)) is False


def test_generic_plotted_value_requirement_is_always_insufficient() -> None:
    """A request that never names chart/series/category has an ambiguous
    target, so the host refuses reuse even when a generic entry is stored."""
    observation = _envelope(
        covered_details=(CoveredDetail(key=GENERIC_PLOTTED),)
    )
    assert _covers(observation, (GENERIC_PLOTTED,)) is False
    # A blank target is not a representable "complete path with empty part":
    # construction refuses it, so only the None generic shape exists.
    with pytest.raises(ObservationContractError):
        DetailKey(kind="plotted_value", target_key="   ")
    assert _covers(observation, (TABLE_FACT,)) is False


def test_generic_plotted_value_coverage_never_covers_a_specific_request() -> None:
    observation = _envelope(
        covered_details=(CoveredDetail(key=GENERIC_PLOTTED),)
    )
    assert _covers(observation, (REVENUE,)) is False


def test_missing_target_is_insufficient_for_a_plotted_value_requirement() -> None:
    """A complete target is required on the request side; a covered key that
    carries the same incomplete shape cannot settle the question either."""
    observation = _envelope(covered_details=(CoveredDetail(key=REVENUE),))
    assert _covers(observation, (DetailKey(kind="plotted_value"),)) is False


def test_page_image_hash_mismatch_is_insufficient() -> None:
    assert _covers(_envelope(), (REVENUE,), page_image_sha256=OTHER_PAGE_HASH) is False
    assert (
        _covers(
            _envelope(page_image_sha256=OTHER_PAGE_HASH), (REVENUE,), page_image_sha256=PAGE_HASH
        )
        is False
    )


def test_attachment_content_version_mismatch_is_insufficient() -> None:
    assert _covers(_envelope(), (REVENUE,), attachment_version=2) is False
    assert (
        _covers(_envelope(attachment_version=2), (REVENUE,), attachment_version=2) is True
    )


def test_unknown_coverage_schema_is_insufficient_on_both_sides() -> None:
    assert (
        _covers(
            _envelope(), (REVENUE,), coverage_schema_version="fin.visual-observation/99"
        )
        is False
    )
    assert (
        _covers(
            _envelope(coverage_schema_version="fin.visual-observation/99"),
            (REVENUE,),
            coverage_schema_version="fin.visual-observation/99",
        )
        is False
    )


def test_empty_required_details_is_insufficient() -> None:
    """A request naming no structured detail has no coverage to verify; the
    conservative unknown handling answers "inspect again"."""
    assert _covers(_envelope(), ()) is False


def test_unknown_requested_scope_shape_is_insufficient() -> None:
    assert _covers(_envelope(), (REVENUE,), "whole_page") is False  # type: ignore[arg-type]


# ----- joint coverage across several observations of one source -----


def test_two_crop_observations_jointly_cover_two_required_details() -> None:
    requested = VisionCrop(left=0.2, top=0.2, right=0.3, bottom=0.3)
    page_scan = _envelope(
        observation_version=1,
        covered_details=(
            CoveredDetail(
                key=REVENUE, scope=VisionCrop(left=0.0, top=0.0, right=0.4, bottom=0.4)
            ),
        ),
    )
    crop_scan = _envelope(
        observation_version=2,
        scope=VisionCrop(left=0.1, top=0.1, right=0.9, bottom=0.9),
        covered_details=(
            CoveredDetail(
                key=SAME_CHART_OTHER_SERIES,
                scope=VisionCrop(left=0.1, top=0.1, right=0.9, bottom=0.9),
            ),
        ),
    )
    assert (
        observations_jointly_cover(
            (page_scan, crop_scan),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            requested,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is True
    )
    # Neither row alone covers the whole request.
    assert (
        observations_jointly_cover(
            (page_scan,),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            requested,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )
    assert (
        observations_jointly_cover(
            (crop_scan,),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            requested,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )


def test_joint_coverage_refuses_observations_from_another_page_or_unit() -> None:
    page_two = _envelope(
        covered_details=(
            CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),
        ),
        scope=CROP_TOP_LEFT,
    )
    page_three = _envelope(
        observation_version=1,
        page=3,
        covered_details=(CoveredDetail(key=SAME_CHART_OTHER_SERIES),),
    )
    assert (
        observations_jointly_cover(
            (page_two, page_three),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            CROP_INSIDE,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )
    other_unit = _envelope(
        observation_version=1, source_unit=2, page=2,
        covered_details=(CoveredDetail(key=SAME_CHART_OTHER_SERIES),),
    )
    assert (
        observations_jointly_cover(
            (page_two, other_unit),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            CROP_INSIDE,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )


def test_joint_coverage_applies_the_identity_checks_to_every_row() -> None:
    good = _envelope(covered_details=(CoveredDetail(key=REVENUE),))
    stale_hash = _envelope(
        observation_version=2,
        page_image_sha256=OTHER_PAGE_HASH,
        covered_details=(CoveredDetail(key=SAME_CHART_OTHER_SERIES),),
    )
    assert (
        observations_jointly_cover(
            (good, stale_hash),
            (REVENUE, SAME_CHART_OTHER_SERIES),
            None,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )


def test_joint_coverage_of_an_empty_set_is_insufficient() -> None:
    assert (
        observations_jointly_cover(
            (),
            (REVENUE,),
            None,
            attachment_version=1,
            page_image_sha256=PAGE_HASH,
            coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        )
        is False
    )


# ----- factory from one vision call -----


def test_observation_from_call_result_binds_identity_and_numbers() -> None:
    result = VisionCallResult(
        text="Chart shows Q3 revenue of 89,493 million dollars.",
        target=VisionTarget(
            attachment_id="att-1", version="3", page=2, crop=CROP_TOP_LEFT
        ),
        model_id="provider-model-1",
        media_type="image/png",
        usage=USAGE,
        cost=COST,
    )
    observation = observation_from_call_result(
        result,
        observation_version=4,
        source_unit=2,
        page_image_sha256=PAGE_HASH,
        provider="openai-compatible",
        created_at=STAMP,
        covered_details=(CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),),
    )
    assert observation.attachment_id == "att-1"
    assert observation.attachment_version == 3
    assert observation.page == 2
    assert observation.source_unit == 2
    assert observation.observation_version == 4
    assert observation.scope == CROP_TOP_LEFT
    assert observation.model_version == "provider-model-1"
    assert observation.attempts == (ObservationAttempt(usage=USAGE, cost=COST),)
    assert observation.covered_details == (
        CoveredDetail(key=REVENUE, scope=CROP_TOP_LEFT),
    )


def test_observation_from_call_result_rejects_a_non_integer_content_version() -> None:
    result = VisionCallResult(
        text="no chart on this page",
        target=VisionTarget(attachment_id="att-1", version="v3", page=1),
        model_id="provider-model-1",
        media_type="image/png",
    )
    with pytest.raises(ObservationContractError):
        observation_from_call_result(
            result,
            observation_version=1,
            source_unit=1,
            page_image_sha256=PAGE_HASH,
            provider="openai-compatible",
            created_at=STAMP,
        )


# ----- store: persist / read back (in-memory) -----


def _threads_and_store() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="observation thread")
    return threads, InMemoryAttachmentStore(threads), thread.id


def _create_attachment(store: InMemoryAttachmentStore, thread_id: str, aid: str) -> None:
    store.create(
        thread_id=thread_id,
        attachment_id=aid,
        filename="filing.pdf",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{aid}",
    )
    store.record_units(
        attachment_id=aid,
        version=1,
        units=(ParsedAttachmentUnit(unit=1, text="page one"),
               ParsedAttachmentUnit(unit=2, text="page two")),
    )


@pytest.fixture
def memory_store() -> tuple[InMemoryAttachmentStore, str]:
    _, store, thread_id = _threads_and_store()
    _create_attachment(store, thread_id, "att-1")
    return store, thread_id


def test_record_observation_round_trips_every_field(memory_store: tuple[Any, str]) -> None:
    store, _ = memory_store
    observation = _envelope()

    stored = store.record_observation(attachment_id="att-1", observation=observation)
    read_back = store.observations("att-1", 1, 1, 2)

    assert stored == observation
    assert read_back == (observation,)
    row = read_back[0]
    assert row.coverage_schema_version == COVERAGE_SCHEMA_VERSION
    assert row.page_image_sha256 == PAGE_HASH
    assert row.attempts[0].cost == COST
    assert row.attempts[0].cost is not None
    assert row.attempts[0].cost.amount == Decimal("0.004")
    assert row.attempts[0].usage == USAGE


def test_unreported_cost_round_trips_as_absent_not_zero(
    memory_store: tuple[Any, str],
) -> None:
    """``cost is None`` is provider-silent; a reported zero is a different
    fact and must survive the same round trip."""
    store, _ = memory_store
    unreported = _envelope(
        observation_version=1,
        attempts=(ObservationAttempt(usage=USAGE, cost=None),),
    )
    reported_zero = _envelope(
        observation_version=2,
        attempts=(
            ObservationAttempt(
                usage=USAGE, cost=VisionCost(amount=Decimal("0"))
            ),
        ),
    )
    store.record_observation(attachment_id="att-1", observation=unreported)
    store.record_observation(attachment_id="att-1", observation=reported_zero)

    rows = store.observations("att-1", 1, 1, 2)
    assert rows[0].attempts[0].cost is None
    assert rows[1].attempts[0].cost is not None
    assert rows[1].attempts[0].cost.amount == Decimal("0")


def test_observations_address_one_source_at_a_time(memory_store: tuple[Any, str]) -> None:
    store, _ = memory_store
    store.record_observation(attachment_id="att-1", observation=_envelope())
    store.record_observation(
        attachment_id="att-1", observation=_envelope(attachment_version=2)
    )
    store.record_observation(
        attachment_id="att-1", observation=_envelope(source_unit=2, page=3)
    )

    assert store.observations("att-1", 1, 1, 2) == (_envelope(),)
    assert store.observations("att-1", 2, 1, 2) == (_envelope(attachment_version=2),)
    assert store.observations("att-1", 1, 2, 3) == (
        _envelope(source_unit=2, page=3),
    )
    assert store.observations("att-1", 9, 1, 2) == ()
    assert store.observations("never-created", 1, 1, 2) == ()


def test_observation_versions_are_monotonic_per_source(
    memory_store: tuple[Any, str],
) -> None:
    """A late result from a superseded attempt must not overwrite newer
    coverage; re-writing the same version replaces it."""
    store, _ = memory_store
    store.record_observation(
        attachment_id="att-1",
        observation=_envelope(
            observation_version=2,
            covered_details=(CoveredDetail(key=UNRELATED_CHART),),
        ),
    )
    store.record_observation(
        attachment_id="att-1",
        observation=_envelope(observation_version=2),
    )
    assert store.observations("att-1", 1, 1, 2) == (_envelope(observation_version=2),)

    with pytest.raises(AttachmentStoreError):
        store.record_observation(
            attachment_id="att-1",
            observation=_envelope(observation_version=1),
        )
    # A different source keeps its own sequence starting at 1.
    store.record_observation(
        attachment_id="att-1", observation=_envelope(page=3, observation_version=1)
    )
    assert [row.observation_version for row in store.observations("att-1", 1, 1, 3)] == [1]


def test_record_observation_refuses_an_envelope_addressing_another_attachment(
    memory_store: tuple[Any, str],
) -> None:
    store, _ = memory_store
    with pytest.raises(AttachmentStoreError):
        store.record_observation(
            attachment_id="att-1", observation=_envelope(attachment_id="att-2")
        )
    assert store.observations("att-1", 1, 1, 2) == ()


def test_record_observation_on_an_unknown_attachment_raises_thread_gone() -> None:
    _, store, _ = _threads_and_store()
    with pytest.raises(AttachmentThreadGoneError):
        store.record_observation(
            attachment_id="never-created",
            observation=_envelope(attachment_id="never-created"),
        )


def test_delete_outranks_a_late_observation_write() -> None:
    """The tombstone blocks observations exactly like units and postings."""
    _, store, thread_id = _threads_and_store()
    _create_attachment(store, thread_id, "att-1")
    assert store.tombstone(attachment_id="att-1", thread_id=thread_id) is not None

    with pytest.raises(AttachmentThreadGoneError):
        store.record_observation(attachment_id="att-1", observation=_envelope())
    assert store.observations("att-1", 1, 1, 2) == ()


def test_thread_removal_blocks_a_late_observation_write() -> None:
    threads, store, thread_id = _threads_and_store()
    _create_attachment(store, thread_id, "att-1")
    store.record_observation(attachment_id="att-1", observation=_envelope())
    assert threads.delete_thread(thread_id) is True

    assert store.observations("att-1", 1, 1, 2) == ()
    with pytest.raises(AttachmentThreadGoneError):
        store.record_observation(
            attachment_id="att-1", observation=_envelope(observation_version=2)
        )


def test_single_observation_reuse_after_a_store_round_trip(
    memory_store: tuple[Any, str],
) -> None:
    """The persisted row answers the reuse check the same way the in-memory
    envelope does — the store must not lose detail structure."""
    store, _ = memory_store
    observation = _envelope(
        scope=None,
        covered_details=(
            CoveredDetail(key=REVENUE),
            CoveredDetail(key=TABLE_FACT, scope=CROP_TOP_LEFT),
        ),
    )
    store.record_observation(attachment_id="att-1", observation=observation)
    stored = store.observations("att-1", 1, 1, 2)[0]

    assert _covers(stored, (TABLE_FACT,), CROP_INSIDE) is True
    assert _covers(stored, (TABLE_FACT,), CROP_OVERLAPPING) is False
    assert _covers(stored, (REVENUE,), CROP_OVERLAPPING) is True


# ----- Postgres (env-gated on FIN_CHECKPOINT_URL) -----


@pytest.fixture
def postgres_store_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; PostgresAttachmentStore is "
            "untested in this environment"
        )
    return url


@pytest.fixture
def pg(postgres_store_url: str) -> Iterator[tuple[Any, Any]]:
    import psycopg

    from fin.host.attachment_store import PostgresAttachmentStore
    from fin.host.thread_store import PostgresThreadStore

    threads = PostgresThreadStore(postgres_store_url)
    store = PostgresAttachmentStore(postgres_store_url)
    yield threads, store
    with psycopg.connect(postgres_store_url) as conn:
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


def _pg_prepare(store: Any, thread_id: str) -> str:
    aid = f"pg-obs-{uuid.uuid4().hex[:8]}"
    _create_attachment_on(store, thread_id, aid)
    return aid


def _create_attachment_on(store: Any, thread_id: str, aid: str) -> None:
    store.create(
        thread_id=thread_id,
        attachment_id=aid,
        filename="filing.pdf",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{aid}",
    )
    store.record_units(
        attachment_id=aid,
        version=1,
        units=(
            ParsedAttachmentUnit(unit=1, text="page one"),
            ParsedAttachmentUnit(unit=2, text="page two"),
        ),
    )


def test_postgres_record_observation_round_trips_every_field(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg observation")
    aid = _pg_prepare(store, thread.id)
    observation = _envelope(attachment_id=aid)

    store.record_observation(attachment_id=aid, observation=observation)
    read_back = store.observations(aid, 1, 1, 2)

    assert read_back == (observation,)


def test_postgres_observations_cascade_with_deletion(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg observation cascade")
    aid = _pg_prepare(store, thread.id)
    store.record_observation(
        attachment_id=aid, observation=_envelope(attachment_id=aid)
    )

    assert threads.delete_thread(thread.id) is True

    assert store.observations(aid, 1, 1, 2) == ()
    with pytest.raises(AttachmentThreadGoneError):
        store.record_observation(
            attachment_id=aid,
            observation=_envelope(attachment_id=aid, observation_version=2),
        )


def test_postgres_tombstone_blocks_a_late_observation(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg observation tombstone")
    aid = _pg_prepare(store, thread.id)
    store.tombstone(attachment_id=aid, thread_id=thread.id)

    with pytest.raises(AttachmentThreadGoneError):
        store.record_observation(attachment_id=aid, observation=_envelope(attachment_id=aid))
    assert store.observations(aid, 1, 1, 2) == ()


def test_postgres_stale_observation_version_is_refused(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg observation monotonic")
    aid = _pg_prepare(store, thread.id)
    store.record_observation(
        attachment_id=aid,
        observation=_envelope(attachment_id=aid, observation_version=3),
    )
    with pytest.raises(AttachmentStoreError):
        store.record_observation(
            attachment_id=aid,
            observation=_envelope(attachment_id=aid, observation_version=2),
        )


def test_observation_methods_are_on_both_protocol_implementations(
    postgres_store_url: str,
) -> None:
    from fin.host.attachment_store import PostgresAttachmentStore

    memory = InMemoryAttachmentStore(InMemoryThreadStore())
    durable = PostgresAttachmentStore(postgres_store_url)
    for store in (memory, durable):
        assert callable(getattr(store, "record_observation", None))
        assert callable(getattr(store, "observations", None))


# ----- the provider's answer payload ------------------------------------


def test_envelope_defaults_the_answer_payload_to_empty() -> None:
    """A provenance-only writer is representable: the field exists and
    starts empty rather than being absent from the envelope."""
    assert _envelope().answer_text == ""


def test_observation_from_call_result_copies_the_provider_answer_text() -> None:
    """A sufficient stored observation must be citable, so the factory
    carries the provider's answer, not only its provenance keys."""
    answer = "Chart shows Q3 revenue of 89,493 million dollars."
    result = VisionCallResult(
        text=answer,
        target=VisionTarget(attachment_id="att-1", version="1", page=2),
        model_id="provider-model-1",
        media_type="image/png",
        usage=USAGE,
        cost=COST,
    )
    observation = observation_from_call_result(
        result,
        observation_version=1,
        source_unit=1,
        page_image_sha256=PAGE_HASH,
        provider="openai-compatible",
        created_at=STAMP,
    )
    assert observation.answer_text == answer


def test_record_observation_round_trips_the_provider_answer_text(
    memory_store: tuple[Any, str],
) -> None:
    store, _ = memory_store
    answer = "No chart on this page; the figure shows headcount by quarter."
    observation = _envelope(answer_text=answer)

    store.record_observation(attachment_id="att-1", observation=observation)
    read_back = store.observations("att-1", 1, 1, 2)

    assert read_back == (observation,)
    assert read_back[0].answer_text == answer


def test_postgres_record_observation_round_trips_the_provider_answer_text(
    pg: tuple[Any, Any],
) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg observation answer")
    aid = _pg_prepare(store, thread.id)
    answer = "Chart shows Q3 revenue of 89,493 million dollars."
    observation = _envelope(attachment_id=aid, answer_text=answer)

    store.record_observation(attachment_id=aid, observation=observation)
    read_back = store.observations(aid, 1, 1, 2)

    assert read_back == (observation,)
    assert read_back[0].answer_text == answer
