"""The turn-facing visual read: reuse, prove-the-hash, charge, persist, refuse.

ADR 0089 gives a visual claim "a separate typed evidence path" that is
never a source quote, and the spec's T5 clause adds the run-side halves:
the current run reads the matching persisted observation, the token names
attachment / page / crop provenance, a sufficient stored observation is
reused with no re-upload and no vision call, and a missing detail
inspects the stored original and mints a new observation version charged
against the run's ceiling.

:func:`fin.host.attachment.attachment_vision_inspect.inspect_for_details` already
decides reuse-or-inspect; these cases pin the caller that wraps it — the
host-bound scope, the rendered-page hash proof, the write-back, the token's
provenance, and the typed refusals. The store is the real
:class:`~fin.host.attachment.attachment_store.InMemoryAttachmentStore`, the trust
faces are the real :class:`~fin.host.attachment.attachment_source_read.AttachmentSourceReader`,
and the provider is a fake: no network, no credentials, no live model.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.parsers._pages import (
    PAGE_RENDER_DPI,
    PageRaster,
    PdfplumberPageSource,
    Pypdfium2PageRenderer,
)
from fin.host.attachment.attachment_budgets import AttachmentBudgets, VisionCallLedger
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    CoveredDetail,
    DetailKey,
    ObservationEnvelope,
    plotted_value_key,
)
from fin.host.attachment.attachment_source_read import AttachmentSourceReader
from fin.host.attachment.attachment_store import (
    AttachmentStoreError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
    StoredPageRegion,
)
from fin.host.attachment.attachment_vision import (
    VisionCallFailedError,
    VisionCallResult,
    VisionCrop,
    VisionErrorClass,
    VisionUsage,
)
from fin.host.attachment.attachment_visual_read import (
    VisualDetail,
    VisualReadFaces,
    VisualReadRequest,
    observation_text,
    read_visual_detail,
)
from fin.host.thread_store import InMemoryThreadStore
from fin.host.evidence.visual_evidence import (
    VISUAL_BUDGET_EXCEEDED,
    VISUAL_DETAIL_INVALID,
    VISUAL_LANE_UNWIRED,
    VISUAL_PAGE_UNAVAILABLE,
    VISUAL_PROVIDER_FAILED,
    VISUAL_SCOPE_INVALID,
    VISUAL_STALE,
    VISUAL_UNAVAILABLE,
)

STAMP = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
MODEL_ID = "g4-fake-vision-1"
PAGE_BYTES = b"%PDF-1.4 fake"
RASTER_A = b"\x89PNG raster A"
RASTER_B = b"\x89PNG raster B"
HASH_A = "a" * 64
ANSWER = "The bar reaches 12.3 billion USD on the right axis."

DETAIL = VisualDetail(
    chart="Revenue by segment", series="Services", category="Q3 2024"
)
DETAIL_KEY = plotted_value_key(
    chart=DETAIL.chart, series=DETAIL.series, category=DETAIL.category
)
USAGE = VisionUsage(input_tokens=90, output_tokens=30, total_tokens=120)


class FakeVisionPort:
    """``VisionPort`` witness: scripted answers, every call kept."""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = list(outcomes or [])

    def inspect(
        self, *, image: bytes, target: Any, media_type: str, question: str | None = None
    ) -> VisionCallResult:
        self.calls.append(
            {"image": image, "target": target, "question": question}
        )
        outcome = self._outcomes.pop(0) if self._outcomes else ANSWER
        if isinstance(outcome, Exception):
            raise outcome
        return VisionCallResult(
            text=outcome,
            target=target,
            model_id=MODEL_ID,
            media_type=media_type,
            usage=USAGE,
            cost=None,
        )


class FakeRasterizer:
    """Draws one page to a known raster, and can be told to draw differently.

    ``drift`` makes every later render a different raster: the only way a
    stored observation can lose its hash proof without a new content
    version, which is precisely the reuse the face must refuse.
    """

    def __init__(self, *, sha: str = HASH_A) -> None:
        self.sha = sha
        self.drawn: list[int] = []

    def drift(self) -> None:
        self.sha = "b" * 64

    def __call__(self, payload: bytes, page_no: int) -> PageRaster:
        self.drawn.append(page_no)
        return PageRaster(
            png=RASTER_A if self.sha == HASH_A else RASTER_B,
            width_px=8,
            height_px=8,
            sha256=self.sha,
        )


class RefusingStore(InMemoryAttachmentStore):
    """A store that will not take the new observation version."""

    def record_observation(self, **kwargs: Any) -> ObservationEnvelope:
        raise RuntimeError("index row gone")


class UnreadableRegionsStore(InMemoryAttachmentStore):
    """A store whose region table answers with a named failure."""

    def __init__(self, threads: InMemoryThreadStore, error: Exception) -> None:
        super().__init__(threads)
        self.error = error
        self.asked: list[tuple[str, int, int | None]] = []

    def regions(
        self, attachment_id: str, version: int, *, page: int | None = None
    ) -> tuple[StoredPageRegion, ...]:
        self.asked.append((attachment_id, version, page))
        raise self.error


class StealthyFailureStore(InMemoryAttachmentStore):
    """A store whose region table fails with a type nothing here can name."""

    def __init__(self, threads: InMemoryThreadStore) -> None:
        super().__init__(threads)

    def regions(
        self, attachment_id: str, version: int, *, page: int | None = None
    ) -> tuple[StoredPageRegion, ...]:
        raise ZeroDivisionError("a defect no store contract names")


# The committed mixed-chart gold case, whose page 3 carries the 528 x 328 pt
# chart the manifest records at x=42, top y=100 on a 612 x 792 pt media box.
# The crop cases rasterize THIS document at the lane's own 200 dpi, so the
# pixels a provider is shown are the fixture's, not a drawn stand-in.
GOLD_MIXED_PDF = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "corpus"
    / "g4_attachment_gold"
    / "g4-mixed-chart-001.pdf"
)
GOLD_CHART_PT = (42.0, 100.0, 570.0, 428.0)


def _gold_media_box_pt(page_no: int = 3) -> tuple[float, float]:
    """The gold fixture's OWN page box, read from the document itself.

    WHY read and not spelled out: every crop assertion in this file is a
    statement about the same page, so a re-spelled ``612``/``792`` would be a
    constant that can drift from the fixture without any test noticing. The
    production source the layout engine itself measured is the authority, so
    an expected bound is derived from the bytes the provider is shown.
    """
    return _media_box_pt(GOLD_MIXED_PDF.read_bytes(), page_no)


def _gold_crop_pt(page_no: int = 3) -> tuple[float, float, float, float]:
    """The gold chart's stored box, normalized by the gold page's own box."""
    width, height = _gold_media_box_pt(page_no)
    left, top, right, bottom = GOLD_CHART_PT
    return (left / width, top / height, right / width, bottom / height)


class GoldPageRasterizer:
    """Draws the committed gold PDF at the lane's own render scale.

    WHY real bytes: the crop lane cuts a box out of the raster the host
    renders, so a fake ``b"PNG"`` stub would leave the cut itself unproven.
    Renders through the same
    :class:`~fin.corpus.parsers._pages.Pypdfium2PageRenderer` production
    entry the graph hands this face, so the page's media box and its raster
    agree by construction rather than by a test constant.
    """

    def __init__(self, source_bytes: bytes | None = None) -> None:
        self.drawn: list[int] = []
        self._bytes = (
            GOLD_MIXED_PDF.read_bytes() if source_bytes is None else source_bytes
        )
        self._boxes_agree = _cropbox_is_mediabox(self._bytes)

    def __call__(self, payload: bytes, page_no: int) -> PageRaster:
        self.drawn.append(page_no)
        with Pypdfium2PageRenderer(self._bytes) as renderer:
            raster = renderer.render_page(page_no=page_no)
        size_pt = _media_box_pt(self._bytes, page_no)
        # WHY the assertion is conditional: on a page whose CropBox is
        # smaller (the case the media-box refusal pins) the renderer
        # legitimately draws the CropBox, so this sanity check is only a
        # statement when the two boxes agree. It still catches the real
        # failure it exists for — a renderer off the lane's render scale.
        if self._boxes_agree:
            assert (
                abs(raster.width_px - size_pt[0] * PAGE_RENDER_DPI / 72.0) < 1.0
            ), "the raster's width must be the media box at the lane's own scale"
        return raster


def _cropbox_is_mediabox(source_bytes: bytes) -> bool:
    """Whether every page of a document draws at its own MediaBox.

    Read through pypdfium2's own boxes, so a fixture's sanity assertion is
    a statement about the committed document rather than a constant.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(source_bytes)
    try:
        return all(
            document[index].get_cropbox() == document[index].get_mediabox()
            for index in range(len(document))
        )
    finally:
        document.close()


def _rotated_gold_bytes(degrees: int = 90) -> bytes:
    """The gold fixture with page 3's ``/Rotate`` set, and nothing else.

    WHY a derived fixture rather than a new committed file: the point being
    pinned is what a rotation does to the two page-box authorities, and the
    gold case already carries the real chart box on the real page 3. Setting
    one rotation key keeps every other byte of the committed document — the
    region, the text layer, the pixels — and so keeps the only variable the
    test is about the only variable.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(GOLD_MIXED_PDF.read_bytes())
    try:
        document[2].set_rotation(degrees)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


def _cropboxed_gold_bytes() -> bytes:
    """The gold fixture with page 3's ``/CropBox`` shrunk, and nothing else.

    The one page shape where the two authorities genuinely DISAGREE: the
    layout measured the page against its MediaBox while the renderer drew
    the CropBox, so the raster extent is a different page. The gold case
    already carries a real chart box on the real page 3, so the only thing
    this adds is the disagreement under test.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(GOLD_MIXED_PDF.read_bytes())
    try:
        document[2].set_cropbox(0, 0, 400, 600)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


def _crop_bounds(crop: VisionCrop | None) -> tuple[float, float, float, float] | None:
    """The four bounds of a crop, so a test compares numbers not a dataclass."""
    if crop is None:
        return None
    return (crop.left, crop.top, crop.right, crop.bottom)


def _media_box_pt(source_bytes: bytes, page_no: int) -> tuple[float, float]:
    """One page's unrotated media box, read the way the layout reads it."""
    source = PdfplumberPageSource(source_bytes)
    try:
        return source.page_size_pt(page_no=page_no)
    finally:
        source.close()


def _pt_for_px(pixels: int) -> float:
    """The PDF points whose 200 dpi shorter edge rounds to ``pixels``.

    WHY derived and not hard-coded: the legibility floor is stated in raster
    pixels, so a case that pins the threshold from one side has to convert,
    not guess — and the conversion is the rule under test's own arithmetic.
    """
    return pixels * 72.0 / PAGE_RENDER_DPI


class StaleReader:
    """Wraps the real reader and reports the row as moved on after the read."""

    def __init__(self, reader: AttachmentSourceReader) -> None:
        self._reader = reader

    def owned_record(self, attachment_id: str) -> Any:
        return self._reader.owned_record(attachment_id)

    def identity_is_current(self, attachment_id: str, version_int: int) -> bool:
        return False


def _budgets(crop_cap: int = 8, page_cap: int = 2) -> AttachmentBudgets:
    return AttachmentBudgets(
        max_upload_bytes=4 * 1024 * 1024,
        max_pdf_pages=page_cap,
        max_vision_calls_per_run=page_cap + crop_cap + 1,
        max_vision_crop_calls_per_run=crop_cap,
    )


def _threads_and_store() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Grounded turn").id
    return threads, store, thread_id


def _ready_pdf(
    store: InMemoryAttachmentStore, thread_id: str, *, pages: int = 2, version: int = 1
) -> str:
    attachment_id = "att-visual-1"
    store.create(
        thread_id=thread_id,
        attachment_id=attachment_id,
        filename="Term Sheet.PDF",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{attachment_id}",
    )
    units = tuple(
        ParsedAttachmentUnit(unit=page, text=f"page {page} body")
        for page in range(1, pages + 1)
    )
    store.record_units(
        attachment_id=attachment_id, version=version, units=units
    )
    store.put_index(
        attachment_id=attachment_id,
        version=version,
        postings=build_postings(units),
    )
    store.mark_ready(
        attachment_id=attachment_id, version=version, unit_count=pages
    )
    return attachment_id


def _stored(
    *,
    observation_version: int = 1,
    covered: tuple[CoveredDetail, ...] = (),
    scope: VisionCrop | None = None,
    page_image_sha256: str = HASH_A,
    attachment_id: str = "att-visual-1",
    version: int = 1,
    page: int = 1,
    answer_text: str = ANSWER,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        attachment_id=attachment_id,
        coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        observation_version=observation_version,
        attachment_version=version,
        source_unit=page,
        page=page,
        page_image_sha256=page_image_sha256,
        provider=MODEL_ID,
        model_version=MODEL_ID,
        scope=scope,
        covered_details=covered,
        created_at=STAMP,
        answer_text=answer_text,
    )


def _faces(
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    vision: FakeVisionPort | None = None,
    rasterizer: Any = None,
    ledger: VisionCallLedger | None = None,
    reader: Any = None,
    gold: bool = False,
    source_bytes: bytes | None = None,
) -> VisualReadFaces:
    """The default faces, or the ones that read the real gold PDF.

    ``gold=True`` draws the committed mixed-chart fixture instead of a stub
    raster, so the crop cases cut the fixture's own pixels; everything else
    about the wiring is identical. ``source_bytes`` is the escape hatch for
    a page shape the committed fixture does not carry (a rotation), and is
    the same bytes the renderer draws, so the object store and the page
    lane can never disagree about which document is being read.
    """
    if source_bytes is not None:
        gold = True
    payload_bytes = (
        source_bytes
        if source_bytes is not None
        else (GOLD_MIXED_PDF.read_bytes() if gold else PAGE_BYTES)
    )
    return VisualReadFaces(
        reader=reader or AttachmentSourceReader(store, thread_id),
        store=store,
        vision=FakeVisionPort() if vision is None else vision,
        ledger=VisionCallLedger(_budgets()) if ledger is None else ledger,
        payload=(lambda _key: payload_bytes),
        render=(
            rasterizer
            or (GoldPageRasterizer(source_bytes) if gold else FakeRasterizer())
        ),
        now=lambda: STAMP,
    )


def _advance_to_version_2(
    store: InMemoryAttachmentStore, attachment_id: str, *, pages: int
) -> None:
    """Publish version 2 of a ready row, through the store's own doors.

    WHY a real advance and not a patched ``record.version``: the whole
    subject of the superseded-version case is WHICH version the lane reads
    boxes for, so the row a test hands the lane has to be the row a
    re-preparation would have produced. A stubbed version leaves the live
    box and the superseded box indistinguishable, which is how a case can
    pass without testing its own name.
    """
    units = tuple(
        ParsedAttachmentUnit(unit=page, text=f"page {page} of version 2")
        for page in range(1, pages + 1)
    )
    store.record_units(attachment_id=attachment_id, version=2, units=units)
    store.put_index(
        attachment_id=attachment_id, version=2, postings=build_postings(units)
    )
    store.mark_ready(attachment_id=attachment_id, version=2, unit_count=pages)


def _record_chart_region(
    store: InMemoryAttachmentStore,
    *,
    version: int,
    kind: str = "figure",
    page: int = 3,
    left_pt: float = 42.0,
    top_pt: float = 100.0,
    right_pt: float = 570.0,
    bottom_pt: float = 428.0,
) -> StoredPageRegion:
    """Persist one page's box, page 3's chart box by default.

    The gold manifest's own box unless a case names another, so a case that
    is about the RULE and not about the fixture's geometry still starts from
    a real box on a real page.
    """
    region = StoredPageRegion(
        page=page, kind=kind, left_pt=left_pt, top_pt=top_pt,
        right_pt=right_pt, bottom_pt=bottom_pt,
    )
    store.record_regions(
        attachment_id="att-visual-1", version=version, regions=(region,)
    )
    return region


def _request(page: int = 1, detail: VisualDetail = DETAIL) -> VisualReadRequest:
    return VisualReadRequest(attachment_id="att-visual-1", page=page, detail=detail)


def _whole_page(key: DetailKey) -> CoveredDetail:
    return CoveredDetail(key=key, scope=None)


# ----- the reuse door: a sufficient observation costs nothing -----------


def test_a_covering_stored_observation_is_reused_with_no_call_and_no_charge() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(covered=(_whole_page(DETAIL_KEY),)),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal is None
    assert len(outcome.evidence) == 1
    token = outcome.evidence[0]
    assert token.reused is True
    assert token.outbound_calls == 0
    assert port.calls == []
    # The stored observation is still the only version on record: reuse does
    # not mint a new one, so nothing is charged to the run's ceiling either.
    versions = [
        row.observation_version
        for row in store.observations("att-visual-1", 1, 1, 1)
    ]
    assert versions == [1]


def test_two_stored_observations_of_the_page_are_both_read_into_the_run() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    other = plotted_value_key(
        chart="Revenue by segment", series="Products", category="Q3 2024"
    )
    store.record_observation(
        attachment_id="att-visual-1", observation=_stored(covered=(_whole_page(DETAIL_KEY),))
    )
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(observation_version=2, covered=(_whole_page(other),)),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    # One ask, answered by the page's stored set: the run holds both
    # observations it read, and each carries its own provenance.
    assert outcome.refusal is None
    assert {token.observation_version for token in outcome.evidence} == {1, 2}
    assert all(token.reused for token in outcome.evidence)
    assert port.calls == []


# ----- the missing-detail door: inspect the original, mint a version -----


def test_a_detail_nothing_covers_inspects_the_page_and_persists_a_new_version() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(
            covered=(_whole_page(plotted_value_key(chart="X", series="Y", category="Z")),)
        ),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    token = outcome.evidence[0]
    assert token.reused is False
    assert token.outbound_calls == 1
    assert token.observation_version == 2
    assert len(port.calls) == 1
    assert [
        row.observation_version for row in store.observations("att-visual-1", 1, 1, 1)
    ] == [1, 2]
    # The provider was sent exactly the raster this build rendered, and the
    # coverage it records is exactly the value the run asked for.
    assert port.calls[0]["image"] == RASTER_A
    persisted = store.observations("att-visual-1", 1, 1, 1)[1]
    assert persisted.covered_details[0].key == DETAIL_KEY
    assert persisted.page_image_sha256 == HASH_A


def test_a_crop_observation_does_not_answer_a_whole_page_ask() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    region = VisionCrop(left=0.1, top=0.1, right=0.4, bottom=0.4)
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(
            covered=(CoveredDetail(key=DETAIL_KEY, scope=region),),
            scope=region,
        ),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    assert port.calls, "a region-scoped answer must not stand in for the page"
    token = outcome.evidence[0]
    assert token.reused is False
    # The new observation looked at the whole rendered page, so its own
    # scope is the page, not the region of the row it superseded in value.
    assert token.scope is None
    assert token.observation_version == 2
    assert port.calls[0]["image"] == RASTER_A


def test_a_stored_observation_the_render_no_longer_proves_is_not_reused() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(covered=(_whole_page(DETAIL_KEY),)),
    )
    rasterizer = FakeRasterizer()
    rasterizer.drift()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, rasterizer=rasterizer)

    outcome = read_visual_detail(faces, _request())

    assert port.calls, "pixels nobody has drawn since cannot back a claim"
    assert outcome.evidence[0].reused is False
    assert outcome.evidence[0].page_image_sha256 == "b" * 64


# ----- the region the question names is part of the sufficiency question --


def test_a_region_ask_is_reused_from_a_whole_page_observation() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(covered=(_whole_page(DETAIL_KEY),)),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)
    request = VisualReadRequest(
        attachment_id="att-visual-1",
        page=1,
        detail=DETAIL,
        scope=(0.2, 0.2, 0.6, 0.6),
    )

    outcome = read_visual_detail(faces, request)

    # A whole-page observation contains every region, so the ask is answered
    # with nothing charged, and the token records that what was looked at was
    # the whole page rather than the region alone.
    assert outcome.refusal is None
    assert port.calls == []
    assert outcome.evidence[0].reused is True
    assert outcome.evidence[0].scope is None


def test_a_region_ask_is_not_answered_by_an_observation_of_a_smaller_region() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    narrower = CoveredDetail(
        key=DETAIL_KEY, scope=VisionCrop(left=0.3, top=0.3, right=0.5, bottom=0.5)
    )
    store.record_observation(
        attachment_id="att-visual-1", observation=_stored(covered=(narrower,))
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)
    request = VisualReadRequest(
        attachment_id="att-visual-1",
        page=1,
        detail=DETAIL,
        scope=(0.2, 0.2, 0.6, 0.6),
    )

    outcome = read_visual_detail(faces, request)

    assert outcome.refusal is None
    assert len(port.calls) == 1
    assert outcome.evidence[0].observation_version == 2


# ----- the persisted region is the crop the provider is shown -------------


def test_a_reliable_persisted_region_is_cropped_and_reported_as_that_scope() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    assert outcome.refusal is None
    # The provider was sent THAT crop's pixels, not the whole page, and the
    # normalized box it was given is the persisted PDF-point box divided by
    # the gold fixture's own 612 x 792 pt media box.
    assert len(port.calls) == 1
    sent = port.calls[0]["target"]
    assert sent.crop == VisionCrop(*_gold_crop_pt())
    assert port.calls[0]["image"] != RASTER_A
    # The provenance names the region actually inspected, so the claim is
    # about the chart and never about a page nobody looked at.
    token = outcome.evidence[0]
    assert token.scope == _gold_crop_pt()
    assert token.as_claim_row()["scope"] == list(token.scope)
    # A crop is a crop call: it draws on the crop sub-allocation, not the
    # full-page allowance, so the run ledger records the kind it spent.
    assert faces.ledger.crop_calls_spent == 1
    assert faces.ledger.full_page_calls_spent == 0
    # And the persisted observation binds the same region, so a later turn
    # asking this exact value reuses it instead of re-cropping.
    persisted = store.observations("att-visual-1", 1, 3, 3)
    assert [row.observation_version for row in persisted] == [1]
    assert persisted[0].scope == sent.crop
    assert persisted[0].covered_details[0].scope == sent.crop


def test_the_cropped_answer_is_reused_by_a_later_ask_for_the_same_value() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    first = FakeVisionPort()
    first_faces = _faces(store, thread_id, vision=first, gold=True)
    # The later turn names the same region the first one was answered about,
    # which is what makes the stored crop a sufficient answer to it.
    asked_scope = _gold_crop_pt()
    first_request = VisualReadRequest(
        attachment_id="att-visual-1", page=3, detail=DETAIL, scope=asked_scope
    )

    assert read_visual_detail(first_faces, first_request).refusal is None
    second = FakeVisionPort()
    second_faces = _faces(store, thread_id, vision=second, gold=True)

    outcome = read_visual_detail(second_faces, first_request)

    # Same target, same version, same inspected scope: the stored crop answers
    # it with nothing sent and nothing charged.
    assert second.calls == []
    assert outcome.evidence[0].reused is True
    assert outcome.evidence[0].outbound_calls == 0
    assert outcome.evidence[0].scope == pytest.approx(asked_scope)
    assert second_faces.ledger.calls_spent == 0


def test_a_crop_does_not_answer_a_later_whole_page_ask() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    _faces(store, thread_id, vision=FakeVisionPort(), gold=True)
    scope = _gold_crop_pt()
    assert read_visual_detail(
        _faces(store, thread_id, vision=FakeVisionPort(), gold=True),
        VisualReadRequest(
            attachment_id="att-visual-1", page=3, detail=DETAIL, scope=scope
        ),
    ).refusal is None
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # A chart crop does not contain the page, so the stored one is not reused
    # for a whole-page ask. The page still has a reliable box, so the re-
    # inspection crops that box — a tighter, higher-resolution answer to the
    # whole-page question than the first page raster would be — and says so.
    assert outcome.refusal is None
    assert len(port.calls) == 1
    assert _crop_bounds(port.calls[0]["target"].crop) == pytest.approx(
        _gold_crop_pt()
    )
    assert outcome.evidence[0].reused is False
    assert outcome.evidence[0].scope == pytest.approx(
        _gold_crop_pt()
    )


def test_a_cropped_answer_does_not_answer_an_ask_naming_a_wider_scope() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    _faces(store, thread_id).ledger  # wiring sanity: the lane is priced
    store.record_observation(
        attachment_id="att-visual-1",
        observation=_stored(
            attachment_id="att-visual-1",
            page=3,
            covered=(
                CoveredDetail(
                    key=DETAIL_KEY,
                    scope=VisionCrop(*_gold_crop_pt()),
                ),
            ),
            scope=VisionCrop(*_gold_crop_pt()),
        ),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)
    request = VisualReadRequest(
        attachment_id="att-visual-1", page=3, detail=DETAIL, scope=(0.0, 0.0, 1.0, 1.0)
    )

    outcome = read_visual_detail(faces, request)

    # The stored crop does not contain the whole page, and this lane has no
    # page-level observation to widen it, so the ask is re-inspected rather
    # than answered by a region that shows less than was requested.
    assert outcome.refusal is None
    assert len(port.calls) == 1
    assert outcome.evidence[0].reused is False
    assert outcome.evidence[0].observation_version == 2


def test_no_reliable_region_falls_back_to_the_whole_page_and_says_so() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=1)
    # The 33 x 40 pt logotype of the gold fixture's cover page: a real box
    # from the fixture, and one the legibility floor rejects at 200 dpi.
    store.record_regions(
        attachment_id="att-visual-1",
        version=1,
        regions=(
            StoredPageRegion(
                page=1, kind="figure", left_pt=289.5, top_pt=322.0,
                right_pt=322.5, bottom_pt=362.0,
            ),
        ),
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request(page=1))

    assert outcome.refusal is None
    # The spec's permitted fallback: the whole page, inspected as a whole
    # page, and the provenance says exactly that instead of inventing a crop.
    assert port.calls[0]["target"].crop is None
    assert port.calls[0]["image"] == RASTER_A
    assert outcome.evidence[0].scope is None
    assert faces.ledger.full_page_calls_spent == 1
    assert faces.ledger.crop_calls_spent == 0


def test_a_page_with_no_recorded_region_at_all_is_inspected_whole() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=2)

    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request(page=2))

    assert outcome.refusal is None
    assert port.calls[0]["target"].crop is None
    assert outcome.evidence[0].scope is None


def test_a_region_reaching_outside_the_page_is_never_cropped() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # A box a whole 1 pt taller than the gold page's own media box: the store
    # admits it (its rule is increasing bounds, and a re-scaled raster can
    # legitimately shift a rounding artifact), so the crop lane is the one
    # that must refuse it. ONE point is the smallest overhang the layout
    # core's own checker forgives, so this case is also what pins that the
    # crop lane's tolerance is not that one.
    _height = _gold_media_box_pt()[1]
    _record_chart_region(
        store, version=1, left_pt=42.0, top_pt=100.0,
        right_pt=570.0, bottom_pt=_height + 1.0,
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # The box is not the page's to give: cutting it would send the provider
    # whatever the clip left over, so the whole page is the honest scope.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop is None
    assert outcome.evidence[0].scope is None
    # The provider still got the whole page's pixels, not a clipped fragment.
    assert port.calls[0]["image"] != RASTER_A


def test_a_box_touching_the_page_edge_is_still_the_page() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # The chart box walked out to every page edge and no further: a
    # Docling-shaped box that rounds TO the edge, which the layout core
    # allows and this lane must allow too, at zero tolerance.
    _width, _height = _gold_media_box_pt()
    _record_chart_region(
        store, version=1, left_pt=0.0, top_pt=0.0,
        right_pt=_width, bottom_pt=_height,
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # Exactly ON the edge is inside; the case above is exactly 1 pt past it.
    # Together they are the tolerance, pinned from both sides.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop == VisionCrop(0.0, 0.0, 1.0, 1.0)
    assert outcome.evidence[0].scope == (0.0, 0.0, 1.0, 1.0)


def test_a_box_one_point_past_the_page_edge_is_refused_at_zero_tolerance() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _width, _height = _gold_media_box_pt()
    _record_chart_region(
        store, version=1, left_pt=0.0, top_pt=0.0,
        right_pt=_width + 1.0, bottom_pt=_height,
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # WHY this number is asserted and not merely the outcome: the layout
    # core's checker forgives exactly 1.0 pt of overhang, and a reader who
    # later consolidates the two must learn from a failure here that the
    # crop lane never adopted that number.
    assert port.calls[0]["target"].crop is None
    assert outcome.evidence[0].scope is None


# ----- the page box is the layout's, and a disagreeing raster is a refusal -


def test_a_page_whose_cropbox_differs_from_its_media_box_is_refused() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # A box that fits INSIDE the 400 x 600 pt CropBox, so it is on-page for
    # the raster this call drew AND for the 612 x 792 pt MediaBox the layout
    # measured. Nothing but the extent disagreement can catch this one: a
    # raster-derived page box would happily cut it, and the cut's normalized
    # bounds (0.075 against 400) would name a region 1.5x wider and 1.3x
    # taller than the pixels actually sent.
    _record_chart_region(
        store, version=1, left_pt=30.0, top_pt=30.0,
        right_pt=330.0, bottom_pt=510.0,
    )
    cropped = _cropboxed_gold_bytes()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, source_bytes=cropped)

    outcome = read_visual_detail(faces, _request(page=3))

    # WHY this is the case the media box has to be checked for: the layout
    # measured page 3 against its 612 x 792 pt MEDIABOX, so the stored box
    # is in that space, while the renderer drew the 400 x 600 pt CROPBOX.
    # Recovering the box from the layout's own source and CHECKING it against
    # the raster is what turns a wrong-but-plausible crop into a refusal —
    # and a refusal, not a whole page, because the whole page is drawn at
    # that same CropBox and so does not answer a box measured in another.
    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert port.calls == []
    assert outcome.evidence == ()


def test_a_rotated_page_keeps_its_own_box_and_is_still_cropped() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    rotated = _rotated_gold_bytes(90)
    # The rotated page's own measured box, taken from the same document the
    # renderer drew. On a rotated page the layout's source and the renderer
    # AGREE (both report the rotated 792 x 612 pt extent), so the box and
    # its page box are one coordinate space and the crop is sound.
    width, height = _media_box_pt(rotated, 3)
    _record_chart_region(
        store, version=1, left_pt=42.0, top_pt=100.0,
        right_pt=42.0 + 528.0, bottom_pt=100.0 + 328.0,
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, source_bytes=rotated)

    outcome = read_visual_detail(faces, _request(page=3))

    # The rotation is a NON-event for this lane and the case says so: both
    # authorities move together, so the box normalizes inside its own page
    # and the cut names the pixels sent.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop == VisionCrop(
        left=42.0 / width, top=100.0 / height,
        right=570.0 / width, bottom=428.0 / height,
    )
    assert outcome.evidence[0].scope == pytest.approx(
        (42.0 / width, 100.0 / height, 570.0 / width, 428.0 / height)
    )


def test_a_page_whose_raster_disagrees_with_its_media_box_is_refused() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    gold = GoldPageRasterizer()

    def narrow(payload: bytes, page_no: int) -> PageRaster:
        """The real gold page, drawn into a raster of a DIFFERENT extent.

        A renderer not at :data:`PAGE_RENDER_DPI`, or a build whose raster
        is resized downstream, arrives here. The box would still divide
        cleanly — the only thing that catches it is the disagreement itself.
        """
        raster = gold(payload, page_no)
        return PageRaster(
            png=raster.png,
            width_px=raster.width_px // 2,
            height_px=raster.height_px // 2,
            sha256=raster.sha256,
        )

    port = FakeVisionPort()
    faces = _faces(
        store, thread_id, vision=port, rasterizer=narrow, gold=True
    )

    outcome = read_visual_detail(faces, _request(page=3))

    # A raster that is not the page at the lane's own scale has no pixels
    # the stored box can name, so the whole page is not an answer either and
    # the ask is refused rather than answered about a page nobody cropped.
    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert port.calls == []
    assert outcome.evidence == ()


def test_a_raster_within_one_pixel_of_the_media_box_is_still_the_page() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    gold = GoldPageRasterizer()
    width, height = _gold_media_box_pt()

    def one_pixel_short(payload: bytes, page_no: int) -> PageRaster:
        """The real gold page minus ONE raster pixel of width.

        A renderer's own integer rounding, which is why the tolerance is
        one pixel and not zero: refusing it would refuse a real page.
        """
        raster = gold(payload, page_no)
        return PageRaster(
            png=raster.png,
            width_px=round(width * PAGE_RENDER_DPI / 72.0) - 1,
            height_px=raster.height_px,
            sha256=raster.sha256,
        )

    port = FakeVisionPort()
    faces = _faces(
        store, thread_id, vision=port, rasterizer=one_pixel_short, gold=True
    )

    outcome = read_visual_detail(faces, _request(page=3))

    assert outcome.refusal is None
    assert port.calls[0]["target"].crop == VisionCrop(*_gold_crop_pt())


def test_a_media_box_disagreement_is_logged_before_it_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    gold = GoldPageRasterizer()

    def halved(payload: bytes, page_no: int) -> PageRaster:
        raster = gold(payload, page_no)
        return PageRaster(
            png=raster.png,
            width_px=raster.width_px // 2,
            height_px=raster.height_px // 2,
            sha256=raster.sha256,
        )

    faces = _faces(store, thread_id, rasterizer=halved, gold=True)

    with caplog.at_level("WARNING", logger="fin.host.attachment.attachment_visual_read"):
        outcome = read_visual_detail(faces, _request(page=3))

    # WHY this must be loud: the refusal is indistinguishable from "this
    # page has no boxes" to a caller, so without the line an operator sees a
    # page that stopped being croppable with nothing saying why.
    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert "att-visual-1" in caplog.text
    assert "page 3" in caplog.text
    assert "media box" in caplog.text


# ----- an unreadable box set is a logged fallback, not a silent one ------


def test_a_named_store_fault_falls_back_to_the_whole_page_and_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    threads, plain, thread_id = _threads_and_store()
    failing = UnreadableRegionsStore(threads, AttachmentStoreError("no such table"))
    _ready_pdf(failing, thread_id, pages=3)
    _record_chart_region(failing, version=1)
    port = FakeVisionPort()
    faces = _faces(failing, thread_id, vision=port, gold=True)

    with caplog.at_level("WARNING", logger="fin.host.attachment.attachment_visual_read"):
        outcome = read_visual_detail(faces, _request(page=3))

    # A named store failure is a fallback, not a refusal — the run still has
    # a whole page to look at — but the line is emitted FIRST so an outage
    # is not indistinguishable from a page that genuinely has no boxes.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop is None
    assert "att-visual-1" in caplog.text
    assert "version 1" in caplog.text
    # The driver's own text may carry a DSN or a credential, so it must not
    # be in the message.
    assert "no such table" not in caplog.text
    assert failing.asked == [("att-visual-1", 1, 3)]


def test_a_store_fault_no_contract_names_is_a_refusal_not_a_quiet_downgrade() -> None:
    threads, _plain, thread_id = _threads_and_store()
    stealthy = StealthyFailureStore(threads)
    _ready_pdf(stealthy, thread_id, pages=3)
    _record_chart_region(stealthy, version=1)
    port = FakeVisionPort()
    faces = _faces(stealthy, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # A failure this module cannot attribute is a failure in the run's own
    # store, and the run cannot be told its page is unreadable by answering
    # it. It refuses rather than spending a call on a whole page that says
    # nothing about why the region was unavailable.
    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert port.calls == []
    assert outcome.evidence == ()


def test_a_text_region_is_not_a_crop_the_provider_is_shown() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # The gold chart's own box, on the gold page, with its kind changed to
    # "text" — the ONLY difference from the case that does crop. Read on a
    # real page box, so the box cannot be dropped earlier for being off the
    # page and the kind is left as the only thing left to reject it.
    _record_chart_region(store, version=1, kind="text")
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # A prose block is a region the layout found, not chart artwork; a
    # plotted value is not in it, so the lane stays on the whole page.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop is None
    assert port.calls[0]["image"] != RASTER_A
    assert outcome.evidence[0].scope is None


@pytest.mark.parametrize(
    "edge_px, cropped, side",
    [
        (159, False, "one pixel under the floor"),
        (161, True, "one pixel over the floor"),
    ],
)
def test_the_legibility_floor_is_pinned_from_both_sides_on_a_real_page(
    edge_px: int, cropped: bool, side: str
) -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # A SQUARE box whose shorter edge is exactly the stated number of raster
    # pixels at the lane's own 200 dpi, so the only thing separating the two
    # rows of this table is the 160 px figure rule itself.
    edge_pt = _pt_for_px(edge_px)
    _record_chart_region(
        store, version=1, left_pt=100.0, top_pt=100.0,
        right_pt=100.0 + edge_pt, bottom_pt=100.0 + edge_pt,
    )
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # A box well inside the real page, so this is not the on-page gate
    # answering: on the real media box this box is unambiguously the page's.
    assert outcome.refusal is None, side
    sent = port.calls[0]["target"]
    if cropped:
        assert sent.crop == VisionCrop(
            left=100 / _gold_media_box_pt()[0],
            top=100 / _gold_media_box_pt()[1],
            right=(100 + edge_pt) / _gold_media_box_pt()[0],
            bottom=(100 + edge_pt) / _gold_media_box_pt()[1],
        ), side
        assert outcome.evidence[0].scope == _crop_bounds(sent.crop), side
    else:
        assert sent.crop is None, side
        assert outcome.evidence[0].scope is None, side


def test_a_region_of_a_superseded_version_is_never_cropped() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    # The box belongs to version 1, and the row is genuinely on version 2:
    # the advance is a real preparation, so the box under test is genuinely
    # superseded rather than merely described as such.
    _record_chart_region(store, version=1)
    _advance_to_version_2(store, "att-visual-1", pages=3)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)
    request = VisualReadRequest(
        attachment_id="att-visual-1", page=3, detail=DETAIL
    )

    # A stale version's geometry describes bytes this row no longer is, and
    # this box WOULD have been cropped on a real page — version 2 carries no
    # box at all, so the whole page is the scope for a different reason.
    assert read_visual_detail(faces, request).evidence[0].scope is None
    assert port.calls[0]["target"].crop is None
    assert port.calls[0]["image"] != RASTER_A


def test_the_superseded_versions_box_is_still_a_crop_on_the_version_that_owns_it() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    _advance_to_version_2(store, "att-visual-1", pages=3)
    _record_chart_region(store, version=2)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # The control the case above needs to mean anything: the same geometry
    # under version 2 — a real page, a legible figure box — IS cropped. So
    # the version-1 case's whole page is the VERSION talking, not the page
    # or the rule.
    assert outcome.refusal is None
    assert port.calls[0]["target"].crop == VisionCrop(*_gold_crop_pt())
    assert outcome.evidence[0].scope == pytest.approx(_gold_crop_pt())


def test_a_crop_costs_the_crop_sub_allocation_and_nothing_else() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    budgets = _budgets(page_cap=1, crop_cap=1)
    ledger = VisionCallLedger(budgets)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, ledger=ledger, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    assert outcome.refusal is None
    assert ledger.calls_spent == 1
    assert ledger.crop_calls_spent == 1
    assert ledger.full_page_calls_spent == 0


def test_a_run_out_of_crop_headroom_refuses_and_persists_nothing() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    budgets = _budgets(page_cap=8, crop_cap=1)
    ledger = VisionCallLedger(budgets)
    ledger.charge_crop_attempt()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, ledger=ledger, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # The run still has full-page headroom, so this is the crop sub-cap that
    # refuses: the chart is known, but this run may not spend a crop on it.
    assert outcome.refusal == VISUAL_BUDGET_EXCEEDED
    assert outcome.evidence == ()
    assert port.calls == []
    assert store.observations("att-visual-1", 1, 3, 3) == ()


def test_a_crop_inspection_is_never_retried_when_the_provider_fails() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    port = FakeVisionPort(
        outcomes=[
            VisionCallFailedError("gone", error_class=VisionErrorClass.PERMANENT)
        ]
    )
    faces = _faces(store, thread_id, vision=port, gold=True)

    outcome = read_visual_detail(faces, _request(page=3))

    # A crop draws on no transport reserve: one attempt, its charge kept, and
    # no observation named for pixels no provider ever read.
    assert outcome.refusal == VISUAL_PROVIDER_FAILED
    assert len(port.calls) == 1
    assert faces.ledger.calls_spent == 1
    assert faces.ledger.retries_spent == 0
    assert store.observations("att-visual-1", 1, 3, 3) == ()


def test_a_cropped_claim_carries_its_region_and_no_source_quote() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=3)
    _record_chart_region(store, version=1)
    faces = _faces(store, thread_id, gold=True)

    row = read_visual_detail(faces, _request(page=3)).evidence[0].as_claim_row()

    # The region travels on the typed visual path; the generated prose never
    # becomes a quoted-text span on it.
    assert row["kind"] == "visual_observation"
    assert row["page"] == 3
    assert row["scope"] == list(_gold_crop_pt())
    assert "answer_text" not in row
    assert "text" not in row


@pytest.mark.parametrize(
    "scope, label",
    [
        ((0.6, 0.1, 0.2, 0.9), "right edge left of the left edge"),
        ((0.1, 0.7, 0.9, 0.3), "bottom edge above the top edge"),
        ((-0.1, 0.0, 0.5, 0.5), "a bound below zero"),
        ((0.1, 0.0, 1.5, 0.5), "a bound past the page"),
        ((0.1, 0.2, 0.3), "three numbers"),
        ((0.1, 0.2, 0.3, 0.4, 0.5), "five numbers"),
        ((0.1, 0.2, "x", 0.4), "a bound that is not a number"),
    ],
)
def test_a_malformed_region_is_refused_before_anything_is_drawn(
    scope: tuple, label: str
) -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    rasterizer = FakeRasterizer()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, rasterizer=rasterizer)
    request = VisualReadRequest(
        attachment_id="att-visual-1", page=1, detail=DETAIL, scope=scope
    )

    outcome = read_visual_detail(faces, request)

    assert outcome.refusal == VISUAL_SCOPE_INVALID, label
    assert rasterizer.drawn == []
    assert port.calls == []
    assert store.observations("att-visual-1", 1, 1, 1) == ()


# ----- provenance travels on the token, never as a quote -----------------


def test_the_token_carries_stored_provenance_and_no_answer_body() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    port = FakeVisionPort(outcomes=[ANSWER])
    faces = _faces(store, thread_id, vision=port)

    row = read_visual_detail(faces, _request()).evidence[0].as_claim_row()

    assert row["kind"] == "visual_observation"
    assert row["attachment_id"] == "att-visual-1"
    assert row["version"] == "1"
    assert row["unit"] == 1
    assert row["page"] == 1
    assert row["observation_version"] == 1
    assert row["provider"] == MODEL_ID
    assert row["model_version"] == MODEL_ID
    assert row["page_image_sha256"] == HASH_A
    assert row["detail"] == DETAIL_KEY.target_key
    # The safe display name, not the raw upload name.
    assert row["filename"] == "Term Sheet.PDF"
    assert row["scope"] is None
    assert "answer_text" not in row


# ----- scope is host-bound -----------------------------------------------


def test_an_attachment_of_another_thread_answers_as_unavailable_and_costs_nothing() -> None:
    _threads, store, thread_id = _threads_and_store()
    other_thread = _threads.create_thread(title="Other").id
    _ready_pdf(store, other_thread)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_UNAVAILABLE
    assert outcome.evidence == ()
    assert port.calls == []


def test_an_uploaded_row_that_is_not_ready_answers_exactly_as_a_foreign_one() -> None:
    _threads, store, thread_id = _threads_and_store()
    store.create(
        thread_id=thread_id,
        attachment_id="att-visual-1",
        filename="Term Sheet.PDF",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/att-visual-1",
    )
    rasterizer = FakeRasterizer()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, rasterizer=rasterizer)

    outcome = read_visual_detail(faces, _request())

    # The trust face grants a record only for a readable row, so a row still
    # being prepared cannot be told apart from one this thread does not own.
    assert outcome.refusal == VISUAL_UNAVAILABLE
    assert rasterizer.drawn == []
    assert port.calls == []


def test_the_row_moving_while_the_read_is_in_flight_voids_the_token() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    reader = StaleReader(AttachmentSourceReader(store, thread_id))
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, reader=reader)

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_STALE
    assert outcome.evidence == ()
    # A charged call whose observation the thread no longer owns stays
    # charged: there is no refund path, and the run must not cite it.
    assert len(port.calls) == 1


def test_an_observation_the_store_will_not_take_is_never_cited() -> None:
    threads, _store, thread_id = _threads_and_store()
    refusing = RefusingStore(threads)
    refusing.create(
        thread_id=thread_id,
        attachment_id="att-visual-1",
        filename="Term Sheet.PDF",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/att-visual-1",
    )
    units = (ParsedAttachmentUnit(unit=1, text="page 1 body"),)
    refusing.record_units(attachment_id="att-visual-1", version=1, units=units)
    refusing.put_index(
        attachment_id="att-visual-1", version=1, postings=build_postings(units)
    )
    refusing.mark_ready(attachment_id="att-visual-1", version=1, unit_count=1)
    faces = _faces(refusing, thread_id)

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_STALE
    assert outcome.evidence == ()


# ----- typed refusals at the input and deployment edges ------------------


def test_a_build_with_no_page_lane_refuses_without_opening_the_object() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    reads: list[str] = []

    def payload(key: str) -> bytes:
        reads.append(key)
        return PAGE_BYTES

    faces = VisualReadFaces(
        reader=AttachmentSourceReader(store, thread_id),
        store=store,
        vision=FakeVisionPort(),
        ledger=VisionCallLedger(_budgets()),
        payload=payload,
        render=None,
        now=lambda: STAMP,
    )

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert reads == []


def test_a_text_attachment_has_no_pages_to_look_at() -> None:
    _threads, store, thread_id = _threads_and_store()
    store.create(
        thread_id=thread_id,
        attachment_id="att-visual-1",
        filename="memo.txt",
        size_bytes=64,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/att-visual-1",
    )
    units = (ParsedAttachmentUnit(unit=1, text="memo body"),)
    store.record_units(attachment_id="att-visual-1", version=1, units=units)
    store.put_index(attachment_id="att-visual-1", version=1, postings=build_postings(units))
    store.mark_ready(attachment_id="att-visual-1", version=1, unit_count=1)
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port)

    assert read_visual_detail(faces, _request()).refusal == VISUAL_PAGE_UNAVAILABLE
    assert port.calls == []


def test_an_unwired_vision_port_answers_unwired_instead_of_answering_worse() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    faces = VisualReadFaces(
        reader=AttachmentSourceReader(store, thread_id),
        store=store,
        vision=None,
        ledger=None,
        payload=lambda _key: PAGE_BYTES,
        render=FakeRasterizer(),
        now=lambda: STAMP,
    )

    assert read_visual_detail(faces, _request()).refusal == VISUAL_LANE_UNWIRED


@pytest.mark.parametrize(
    "detail, label",
    [
        (VisualDetail(chart="", series="S", category="C"), "blank chart"),
        (VisualDetail(chart="C", series=" ", category="C"), "blank series"),
        (VisualDetail(chart="C", series="S", category=""), "blank category"),
        (
            VisualDetail(chart="a|b", series="S", category="C"),
            "separator in chart",
        ),
        (
            VisualDetail(chart="C", series="a=b", category="C"),
            "separator in series",
        ),
        (VisualDetail(chart="C" * 400, series="S", category="C"), "oversized chart"),
    ],
)
def test_an_ambiguous_or_oversized_target_is_refused_before_anything_is_drawn(
    detail: VisualDetail, label: str
) -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    rasterizer = FakeRasterizer()
    port = FakeVisionPort()
    faces = _faces(store, thread_id, vision=port, rasterizer=rasterizer)

    outcome = read_visual_detail(faces, _request(detail=detail))

    assert outcome.refusal == VISUAL_DETAIL_INVALID, label
    assert rasterizer.drawn == []
    assert port.calls == []


def test_a_page_beyond_the_frozen_unit_count_is_refused() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id, pages=2)
    rasterizer = FakeRasterizer()
    faces = _faces(store, thread_id, rasterizer=rasterizer)

    assert read_visual_detail(faces, _request(page=3)).refusal == (
        VISUAL_PAGE_UNAVAILABLE
    )
    assert read_visual_detail(faces, _request(page=0)).refusal == (
        VISUAL_PAGE_UNAVAILABLE
    )
    assert rasterizer.drawn == []


def test_an_object_that_cannot_be_drawn_is_refused_not_fabricated() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)

    def render(_payload: bytes, _page: int) -> PageRaster:
        raise RuntimeError("page drew no pixels")

    port = FakeVisionPort()
    faces = VisualReadFaces(
        reader=AttachmentSourceReader(store, thread_id),
        store=store,
        vision=port,
        ledger=VisionCallLedger(_budgets()),
        payload=lambda _key: PAGE_BYTES,
        render=render,
        now=lambda: STAMP,
    )

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_PAGE_UNAVAILABLE
    assert port.calls == []


def test_a_run_out_of_visual_calls_is_refused_and_persists_nothing() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    port = FakeVisionPort()
    # A deployment's numbers are never zero-sized, so exhaustion is made real
    # by spending the whole allowance before the ask.
    budgets = _budgets(page_cap=1, crop_cap=1)
    ledger = VisionCallLedger(budgets)
    for _ in range(budgets.max_vision_calls_per_run):
        ledger.charge_full_page_attempt()
    rasterizer = FakeRasterizer()
    faces = _faces(
        store, thread_id, vision=port, rasterizer=rasterizer, ledger=ledger
    )

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_BUDGET_EXCEEDED
    assert outcome.evidence == ()
    assert ledger.calls_spent == budgets.max_vision_calls_per_run
    assert store.observations("att-visual-1", 1, 1, 1) == ()


def test_a_provider_failure_keeps_its_charge_and_names_no_observation() -> None:
    _threads, store, thread_id = _threads_and_store()
    _ready_pdf(store, thread_id)
    port = FakeVisionPort(
        outcomes=[
            VisionCallFailedError("gone", error_class=VisionErrorClass.PERMANENT)
        ]
    )
    faces = _faces(store, thread_id, vision=port)

    outcome = read_visual_detail(faces, _request())

    assert outcome.refusal == VISUAL_PROVIDER_FAILED
    assert outcome.evidence == ()
    assert store.observations("att-visual-1", 1, 1, 1) == ()


# ----- the model-facing body stays bounded ------------------------------


def test_the_answer_body_is_bounded_to_one_read_worth_of_code_points() -> None:
    envelope = _stored(covered=(_whole_page(DETAIL_KEY),), answer_text="x" * 9_000)

    text, truncated = observation_text(envelope)

    assert len(text) == 8_000
    assert truncated is True


def test_a_short_answer_body_is_passed_through_uncut() -> None:
    envelope = _stored(covered=(_whole_page(DETAIL_KEY),), answer_text="9.4 percent")

    text, truncated = observation_text(envelope)

    assert (text, truncated) == ("9.4 percent", False)
