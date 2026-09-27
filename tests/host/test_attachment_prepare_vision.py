"""T2e preparation-side vision wiring: one page scan per physical page.

The first preparation of a page document asks the configured vision provider
exactly one full-page question per physical page — including pages that hold
no Heron figure box and pages that froze no text unit — charges every
outbound attempt (failed ones included) against the run's :class:
`~fin.host.attachment_budgets.VisionCallLedger`, and persists one versioned
observation per page through the attachment store.

The provider is a :class:`~fin.host.attachment_vision.VisionPort` Fake
throughout: no network, no credentials, no live model. Expectations come from
``plans/thread-attachment-source-read.md`` item 6 (one call per page, the
80 + 8 + 1 = 89 relationship with the run-wide single retry reserve, the
``vision_call_failed`` / ``vision_empty_text`` /
``attachment_vision_budget_exceeded`` exits, and per-attempt usage/cost
recording with cost marked unavailable when the provider omits it), never
from the implementation under test.
"""

from __future__ import annotations

import hashlib
import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.parsers import (
    PARSE_RULE_VERSION,
    PARAMS_TABLE_VERSION,
    NullLayout,
    PageParseResult,
    PageRaster,
    PageRegionParser,
    ParsedPage,
    ParsedUnit,
    Region,
    UnitLocator,
)
from fin.host.attachment_budgets import (
    ATTACHMENT_PAGE_LIMIT_EXCEEDED,
    ATTACHMENT_VISION_BUDGET_EXCEEDED,
    AttachmentBudgets,
    VisionCallLedger,
    required_run_calls,
)
from fin.host.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    DetailKey,
    ObservationAttempt,
)
from fin.host.attachment_prepare import (
    CHART_PRESENCE_DETAIL,
    AttachmentPreparationError,
    AttachmentPreparationService,
    AttachmentVisionCallFailedError,
    AttachmentVisionEmptyTextError,
)
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
)
from fin.host.attachment_vision import (
    DEFAULT_MEDIA_TYPE,
    PAGE_SCAN_PROMPT,
    VISION_CALL_FAILED,
    VISION_EMPTY_TEXT,
    VisionCallFailedError,
    VisionCallResult,
    VisionCost,
    VisionEmptyTextError,
    VisionErrorClass,
    VisionUsage,
)
from fin.host.thread_store import InMemoryThreadStore

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
MIXED_BYTES = (GOLD_DIR / "g4-mixed-chart-001.pdf").read_bytes()
CHART_BYTES = (GOLD_DIR / "g4-image-chart-001.png").read_bytes()

MIXED_PAGE_COUNT = 3

SCAN_ANSWER = (
    "One clustered bar chart is present: Total net sales by fiscal quarter."
)
NO_CHART_ANSWER = "No chart, plot, or table is present on this page."


def _distinct_png(tint: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (tint, tint, tint)).save(buffer, format="PNG")
    return buffer.getvalue()


# A different raster per page: a page that borrows another page's image hash
# is then a test failure, not an unnoticed coincidence.
PNG_BY_PAGE = {page: _distinct_png(page) for page in (1, 2, 3)}


# ----- fake ports -----


class FakeVisionPort:
    """``VisionPort`` witness: scripted per-page outcomes, every call kept.

    ``scripts`` maps a physical page to a queue of outcomes — an exception
    instance to raise, a string to answer with, or ``None`` for the default
    success — consumed one per call to that page. ``on_call`` runs a hook
    when a page's call arrives, which is how a test deletes the attachment
    mid-preparation.
    """

    def __init__(
        self,
        *,
        scripts: dict[int, list[Any]] | None = None,
        model_id: str = "g4-fake-vision-1",
        usage: VisionUsage | None = None,
        cost: VisionCost | None = None,
        on_call: dict[int, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queues = {page: list(queue) for page, queue in (scripts or {}).items()}
        self._model_id = model_id
        self._usage = usage if usage is not None else VisionUsage(11, 7, 18)
        self._cost = cost
        self._on_call = on_call or {}

    @property
    def called_pages(self) -> list[int]:
        return [call["target"].page for call in self.calls]

    def inspect(
        self,
        *,
        image: bytes,
        target: Any,
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
        hook = self._on_call.get(target.page)
        if hook is not None:
            hook()
        queue = self._queues.setdefault(target.page, [])
        outcome = queue.pop(0) if queue else None
        if isinstance(outcome, Exception):
            raise outcome
        return VisionCallResult(
            text=outcome if isinstance(outcome, str) else SCAN_ANSWER,
            target=target,
            model_id=self._model_id,
            media_type=media_type,
            usage=self._usage,
            cost=self._cost,
        )


class PerPageRenderer:
    """``PageRenderPort`` drawing one distinct 1x1 raster per page."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        return PageRaster.of(PNG_BY_PAGE[page_no], width_px=1, height_px=1)


class FigureOnlyThirdLayout:
    """``PageLayoutPort``: the only figure box in the document is on page 3,
    under the legibility floor — so pages 1 and 2 are the no-box pages the
    acceptance names, and page 3 proves a box changes nothing about scanning."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        self.calls.append(page_no)
        if page_no != 3:
            return ()
        return (Region(kind="figure", left_pt=10.0, top_pt=10.0, right_pt=30.0, bottom_pt=30.0),)


class StubOcr:
    """``RasterOcrPort`` with a fixed answer, for a page whose only text is
    its pixels — a bare image fires the page rule before any scan runs."""

    def __init__(self, answer: str = SCAN_ANSWER) -> None:
        self._answer = answer

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        return self._answer


def _parser(
    *, layout: Any = None, renderer: Any = None, ocr: Any = None
) -> PageRegionParser:
    return PageRegionParser(
        layout=layout if layout is not None else NullLayout(),
        renderer=renderer if renderer is not None else PerPageRenderer(),
        ocr=ocr,
    )


class UnitlessSecondPageParser(PageRegionParser):
    """A two-page document whose second page froze no unit at all.

    Stands in for a page that is pure artwork: the real core refuses to build
    an empty unit, yet the page still has pixels and still gets its scan.
    """

    def __init__(self) -> None:
        super().__init__(layout=NullLayout(), renderer=PerPageRenderer(), ocr=None)

    def extract_pdf(self, **kwargs: Any) -> PageParseResult:
        document_id = kwargs["document_id"]
        version = kwargs["version"]
        text = "page one carries a unit"
        unit = ParsedUnit(
            fmt="pdf",
            source_sha256=kwargs["source_sha256"],
            locator=UnitLocator(document_id=document_id, version=version, unit=1),
            char_count=len(text),
            language="en",
            parse_rule_version=PARSE_RULE_VERSION,
            params_table_version=PARAMS_TABLE_VERSION,
            text=text,
        )
        pages = tuple(
            ParsedPage(
                page_no=page_no,
                page_count=2,
                size_pt=(612.0, 792.0),
                native_text=text if page_no == 1 else "",
                scanned_text="",
                unit=unit if page_no == 1 else None,
                raster=PageRaster.of(PNG_BY_PAGE[page_no], width_px=1, height_px=1),
                regions=(),
                ocr_applied=False,
                ocr_empty=False,
            )
            for page_no in (1, 2)
        )
        return PageParseResult(
            fmt="pdf",
            source_sha256=kwargs["source_sha256"],
            document_id=document_id,
            version=version,
            parse_rule_version=PARSE_RULE_VERSION,
            params_table_version=PARAMS_TABLE_VERSION,
            pages=pages,
            source_page_count=2,
            truncated=False,
        )


# ----- fixtures -----


def _budgets(*, max_pages: int = 8, crops: int = 8) -> AttachmentBudgets:
    """The smallest legal run for these caps, derived — never written by
    hand — through the module that prices a run: pages + crops + 1 reserve
    (the 80 + 8 + 1 = 89 relationship at whatever scale the test needs)."""
    draft = AttachmentBudgets(
        max_upload_bytes=1_048_576,
        max_pdf_pages=max_pages,
        max_vision_calls_per_run=max_pages + crops,
        max_vision_crop_calls_per_run=crops,
    )
    return AttachmentBudgets(
        max_upload_bytes=1_048_576,
        max_pdf_pages=max_pages,
        max_vision_calls_per_run=required_run_calls(draft),
        max_vision_crop_calls_per_run=crops,
    )


def _service(
    *,
    budgets: AttachmentBudgets | None,
    parser: PageRegionParser,
    vision: FakeVisionPort | None = None,
    call_ledger: VisionCallLedger | None = None,
) -> tuple[AttachmentPreparationService, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="vision thread")
    store = InMemoryAttachmentStore(threads)
    service = AttachmentPreparationService(
        store=store,
        objects=InMemoryObjectStore(),
        budgets=budgets,
        pages=parser,
        vision=vision,
        call_ledger=call_ledger,
    )
    return service, thread.id


def _prepare(
    service: AttachmentPreparationService,
    thread_id: str,
    *,
    filename: str,
    payload: bytes,
) -> AttachmentRecord:
    record = service.accept(
        thread_id=thread_id, filename=filename, payload=payload
    )
    assert record.status == "uploaded", record
    return service.prepare(record.attachment_id)


def _transport_failure() -> VisionCallFailedError:
    return VisionCallFailedError("dropped", error_class=VisionErrorClass.TRANSPORT)


def _permanent_failure() -> VisionCallFailedError:
    return VisionCallFailedError("refused", error_class=VisionErrorClass.PERMANENT)


def _observations(
    service: AttachmentPreparationService,
    attachment_id: str,
    version: int,
    unit: int,
    page: int,
) -> tuple[Any, ...]:
    return service.store.observations(attachment_id, version, unit, page)


# ----- the vision exits join the door's caught ladder -----


def test_the_vision_exits_are_caught_preparation_codes() -> None:
    """Each provider exit is a named ``AttachmentPreparationError`` code, so
    ``_freeze`` turns it into a ``failed`` row rather than an escaping 500."""
    assert issubclass(AttachmentVisionCallFailedError, AttachmentPreparationError)
    assert issubclass(AttachmentVisionEmptyTextError, AttachmentPreparationError)
    assert AttachmentVisionCallFailedError("a", "d", retryable=False).code == (
        VISION_CALL_FAILED
    )
    assert AttachmentVisionEmptyTextError("a", "d").code == VISION_EMPTY_TEXT


# ----- one full-page scan per physical page -----


def test_first_preparation_scans_every_physical_page_once() -> None:
    vision = FakeVisionPort()
    layout = FigureOnlyThirdLayout()
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(layout=layout), vision=vision
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    # Pages 1 and 2 carry no figure box at all and page 3's box is under the
    # legibility floor: the scan decision is the page's existence, never the
    # layout engine's recall.
    assert layout.calls == [1, 2, 3]
    assert vision.called_pages == [1, 2, 3]
    for call, page in zip(vision.calls, (1, 2, 3)):
        target = call["target"]
        assert target.attachment_id == ready.attachment_id
        assert target.version == str(ready.version)
        assert target.page == page
        assert target.crop is None, "first preparation is whole-page only"
        assert call["image"] == PNG_BY_PAGE[page]
        assert call["media_type"] == "image/png"
        assert call["question"] == PAGE_SCAN_PROMPT
    for page in (1, 2, 3):
        (observation,) = _observations(service, ready.attachment_id, ready.version, page, page)
        assert observation.observation_version == 1, "first preparation is version 1"


def test_a_unitless_page_is_scanned_and_addressed_by_its_page_number() -> None:
    """A page with pixels but no frozen unit still gets its one call: the
    scan belongs to the physical page, not to the text lane's success."""
    vision = FakeVisionPort()
    service, thread_id = _service(
        budgets=_budgets(), parser=UnitlessSecondPageParser(), vision=vision
    )

    ready = _prepare(service, thread_id, filename="artwork.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    assert ready.unit_count == 1, ready
    assert vision.called_pages == [1, 2]
    (observation,) = _observations(service, ready.attachment_id, ready.version, 2, 2)
    assert observation.source_unit == 2
    assert observation.page == 2
    assert observation.page_image_sha256 == hashlib.sha256(
        PNG_BY_PAGE[2]
    ).hexdigest()


def test_a_standalone_image_gets_one_full_page_scan() -> None:
    vision = FakeVisionPort()
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(ocr=StubOcr()), vision=vision
    )

    ready = _prepare(service, thread_id, filename="chart.png", payload=CHART_BYTES)

    assert ready.status == "ready", ready
    assert vision.called_pages == [1]
    (observation,) = _observations(service, ready.attachment_id, ready.version, 1, 1)
    assert observation.page_image_sha256 == hashlib.sha256(CHART_BYTES).hexdigest()


def test_a_no_chart_answer_is_a_successful_persisted_observation() -> None:
    """A page answered "no chart" is a complete answer, not a failure or a
    missing row: the observation is persisted exactly like a page with a
    chart."""
    vision = FakeVisionPort(scripts={2: [NO_CHART_ANSWER]})
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    (observation,) = _observations(service, ready.attachment_id, ready.version, 2, 2)
    assert observation.coverage_schema_version == COVERAGE_SCHEMA_VERSION
    assert observation.scope is None
    assert DetailKey(kind=CHART_PRESENCE_DETAIL) in [
        covered.key for covered in observation.covered_details
    ]


# ----- the run-wide ledger: every outbound attempt is charged -----


def test_failed_attempts_are_charged_and_a_spent_reserve_exits_the_provider_failure() -> None:
    """Charge-before-send, proven observably.

    At the smallest legal run for a 3-page cap (3 pages + the minimum 1 crop
    call + 1 reserve = 5, derived never written) a first-page transport
    failure that succeeds on its reserved retry spends two calls, page 2
    spends a third, and page 3's transport failure then finds the run's one
    reserved retry gone: the retry is refused and NO fifth request leaves.
    The exit is the provider's dropped request (``vision_call_failed``), not
    a budget code — the operator cap refused nothing here, the provider did
    (specs/thread-attachment-source-read.md: ``attachment_vision_budget_exceeded``
    belongs to run-total/crop overflow alone). If a failed attempt had been
    free, the run arithmetic would not show four spends by page 3's refusal.
    """
    vision = FakeVisionPort(scripts={1: [_transport_failure()], 3: [_transport_failure()]})
    service, thread_id = _service(
        budgets=_budgets(max_pages=3, crops=1), parser=_parser(), vision=vision
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == VISION_CALL_FAILED, failed
    assert len(vision.calls) == 4, vision.calls
    assert vision.called_pages == [1, 1, 2, 3]


def test_the_reserve_belongs_to_the_run_and_its_exhaustion_exits_the_provider_failure() -> None:
    """One reserved retry for the whole run: after page 1 spent it, page 2's
    retryable failure cannot buy another attempt even though the run total
    still has calls left. The exit is the provider's dropped request
    (``vision_call_failed``) — a spent reserve with run-total headroom is
    the provider's fault, never a budget-code statement about the operator's
    cap, and the refusal sends nothing further."""
    vision = FakeVisionPort(
        scripts={1: [_transport_failure()], 2: [_transport_failure()]}
    )
    service, thread_id = _service(
        budgets=_budgets(max_pages=3, crops=1), parser=_parser(), vision=vision
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == VISION_CALL_FAILED, failed
    # 1 + its retry, then 2 — and page 2's second attempt and page 3 never
    # go out.
    assert vision.called_pages == [1, 1, 2]
    # The failed row keeps version 0 (a version is minted by a successful
    # preparation), while the scan that did land wrote the preparation's
    # attempted version 1.
    assert len(_observations(service, failed.attachment_id, 1, 1, 1)) == 1
    assert _observations(service, failed.attachment_id, 1, 2, 2) == ()


def test_a_run_total_exhausted_shared_ledger_keeps_the_budget_exit() -> None:
    """``attachment_vision_budget_exceeded`` stays exactly what the spec
    names it: the run total refusing to start another scan. With a
    run-priced ledger already at its deployed total, page 1's plain charge
    fails before any request leaves — a different cause and code from the
    spent-reserve exit above."""
    budgets = _budgets(max_pages=3, crops=1)  # 3 + 1 + 1 = 5
    ledger = VisionCallLedger(budgets)
    for _ in range(5):
        ledger.charge_full_page_attempt()
    assert ledger.remaining_calls == 0
    vision = FakeVisionPort()
    service, thread_id = _service(
        budgets=budgets, parser=_parser(), vision=vision, call_ledger=ledger
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_VISION_BUDGET_EXCEEDED, failed
    assert vision.calls == []


def test_retryable_transport_failure_spends_the_reserve_and_succeeds() -> None:
    vision = FakeVisionPort(scripts={1: [_transport_failure()]})
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    assert vision.called_pages == [1, 1, 2, 3]
    for page in (1, 2, 3):
        (observation,) = _observations(service, ready.attachment_id, ready.version, page, page)
        assert observation.observation_version == 1
    # The retried-away failure attempt is recorded with no provider numbers
    # (usage/cost unavailable, never invented zeros), ahead of the attempt
    # that answered — the same envelope convention the run-side
    # re-inspection uses, so an attempt count means outbound calls on both
    # seams.
    (observation,) = _observations(service, ready.attachment_id, ready.version, 1, 1)
    assert observation.attempts[0] == ObservationAttempt()
    assert [(attempt.usage, attempt.cost) for attempt in observation.attempts] == [
        (None, None),
        (VisionUsage(11, 7, 18), None),
    ]
    for page in (2, 3):
        (observation,) = _observations(service, ready.attachment_id, ready.version, page, page)
        assert len(observation.attempts) == 1


def test_second_retryable_failure_exits_vision_call_failed() -> None:
    vision = FakeVisionPort(
        scripts={1: [_transport_failure(), _transport_failure()]}
    )
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == VISION_CALL_FAILED, failed
    # Exactly one retry — a third attempt at the same page is never made,
    # and no later page is attempted after this one's exit.
    assert vision.called_pages == [1, 1]


def test_non_retryable_failure_is_never_retried() -> None:
    vision = FakeVisionPort(scripts={1: [_permanent_failure()]})
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == VISION_CALL_FAILED, failed
    assert vision.called_pages == [1]


def test_empty_output_exits_vision_empty_text_without_retry() -> None:
    """Malformed or empty provider output is not a transport fault: it must
    not be retried into a pass, and it owns its own code."""
    vision = FakeVisionPort(scripts={2: [VisionEmptyTextError("nothing usable")]})
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == VISION_EMPTY_TEXT, failed
    assert vision.called_pages == [1, 2]


# ----- what the persisted observation carries -----


def test_observation_records_hash_model_usage_and_reported_cost() -> None:
    usage = VisionUsage(input_tokens=120, output_tokens=45, total_tokens=165)
    cost = VisionCost(amount=Decimal("0.0042"), currency="USD")
    vision = FakeVisionPort(usage=usage, cost=cost, model_id="configured-model-v9")
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    for page in (1, 2, 3):
        (observation,) = _observations(service, ready.attachment_id, ready.version, page, page)
        assert observation.attachment_id == ready.attachment_id
        assert observation.attachment_version == ready.version
        assert observation.page == page
        assert (
            observation.page_image_sha256
            == hashlib.sha256(PNG_BY_PAGE[page]).hexdigest()
        )
        assert observation.model_version == "configured-model-v9"
        assert observation.provider == "configured-model-v9"
        assert observation.coverage_schema_version == COVERAGE_SCHEMA_VERSION
        assert observation.scope is None
        assert [(attempt.usage, attempt.cost) for attempt in observation.attempts] == [
            (usage, cost)
        ]


def test_cost_the_provider_omits_is_recorded_as_unavailable() -> None:
    """``cost is None`` is the availability marker — an omitted price, never
    a silently invented zero."""
    vision = FakeVisionPort(cost=None)
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    (observation,) = _observations(service, ready.attachment_id, ready.version, 1, 1)
    (attempt,) = observation.attempts
    assert attempt.usage is not None
    assert attempt.cost is None


# ----- the lanes that must never reach the provider -----


def test_a_text_only_upload_never_reaches_vision() -> None:
    vision = FakeVisionPort()
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )

    ready = _prepare(
        service, thread_id, filename="notes.txt", payload="plain text\n".encode()
    )

    assert ready.status == "ready", ready
    assert vision.calls == []


def test_a_refused_page_count_spends_no_vision_call() -> None:
    """The preflight still decides wholeness before any work: a document the
    page cap refuses never gets a single scan charged or sent."""
    vision = FakeVisionPort()
    service, thread_id = _service(
        budgets=_budgets(max_pages=MIXED_PAGE_COUNT - 1),
        parser=_parser(),
        vision=vision,
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_PAGE_LIMIT_EXCEEDED, failed
    assert vision.calls == []


def test_a_page_lane_without_a_vision_port_freezes_units_and_spends_nothing() -> None:
    """Pinned decision: the ``vision`` seam defaults to unset so the page
    lane this door shipped before the scan stage stays exactly that lane —
    units freeze, ``ready`` publishes, zero calls are charged, and the
    deployment gap is logged rather than silent."""
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=None
    )

    ready = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert ready.status == "ready", ready
    assert len(service.units(ready.attachment_id)) == MIXED_PAGE_COUNT
    for page in (1, 2, 3):
        assert _observations(service, ready.attachment_id, ready.version, page, page) == ()


# ----- deletion mid-preparation keeps its tombstone protection -----


def test_an_observation_written_after_a_delete_loses_the_race() -> None:
    """The store, not this adapter, owns late-write protection: deleting the
    attachment between page 2's send and its observation write surfaces the
    typed loss instead of publishing ``ready`` over a tombstone."""
    holder: dict[str, Any] = {}
    vision = FakeVisionPort(
        on_call={
            2: lambda: service.delete_attachment(
                thread_id=thread_id, attachment_id=holder["attachment_id"]
            )
        }
    )
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(), vision=vision
    )
    record = service.accept(
        thread_id=thread_id, filename="10q.pdf", payload=MIXED_BYTES
    )
    holder["attachment_id"] = record.attachment_id

    with pytest.raises(AttachmentThreadGoneError):
        service.prepare(record.attachment_id)

    assert service.status(record.attachment_id) is None
