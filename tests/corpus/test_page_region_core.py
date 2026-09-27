"""The shared page/region extraction core (``fin.corpus.parsers._pages``).

Real committed gold sources plus fake ports: the default suite stays
hermetic, so no layout container and no OCR binary is reached here. The
pinned-pipeline round trip across the real Docling/Heron and CPU Tesseract
boundary is a separate opt-in suite, not this file.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import pytest

from fin.corpus.errors import (
    EmptySourceParseError,
    FormatMismatchParseError,
    MalformedSourceParseError,
    PageLayoutError,
    PageRenderError,
    UnsupportedFormatParseError,
)
from fin.corpus.ocr import OcrRequiredUnavailableError, detect_ocr_required
from fin.corpus.parsers import (
    MIN_OCR_REGION_EDGE_PX,
    PAGE_RENDER_DPI,
    PARSE_RULE_VERSION,
    PDF_MAGIC,
    NullLayout,
    PageRaster,
    PageRegionParser,
    ParsedPage,
    PdfplumberPageSource,
    Pypdfium2PageRenderer,
    Region,
    content_family,
    has_pdf_signature,
    parse_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
MIXED_PDF = GOLD_DIR / "g4-mixed-chart-001.pdf"
SCANNED_PDF = GOLD_DIR / "g4-scanned-001.pdf"
IMAGE_PNG = GOLD_DIR / "g4-image-chart-001.png"

MIXED_BYTES = MIXED_PDF.read_bytes()
SCANNED_BYTES = SCANNED_PDF.read_bytes()
IMAGE_BYTES = IMAGE_PNG.read_bytes()
SHA = "0" * 64

# The native-text gold case: the manifest (``manifest.json``, case
# ``g4-native-text-001``) is the single source for its quote and expectations,
# authored from the fixture with poppler only, never through this parser.
_GOLD_MANIFEST = json.loads((GOLD_DIR / "manifest.json").read_text())
NATIVE_CASE = next(
    case for case in _GOLD_MANIFEST["cases"] if case["case_id"] == "g4-native-text-001"
)
NATIVE_PDF = REPO_ROOT / NATIVE_CASE["path"]
NATIVE_BYTES = NATIVE_PDF.read_bytes()
NATIVE_QUOTE = NATIVE_CASE["per_page"][0]["expected_quote"]


def _normalise(text: str) -> str:
    """The manifest's own matching form: runs of whitespace collapse to one space."""
    return re.sub(r"\s+", " ", text).strip()

# Page 3 of the mixed fixture: the chart's own box, from
# ``g4_attachment_gold/manifest.json`` (``displayed_pt`` 528 x 328 at
# x=42, top y=100).
CHART_BOX = Region(
    kind="figure", left_pt=42.0, top_pt=100.0, right_pt=570.0, bottom_pt=428.0
)
LOGOTYPE_BOX = Region(
    kind="figure", left_pt=289.5, top_pt=376.0, right_pt=322.5, bottom_pt=416.0
)
CHART_TEXT = "Total net sales by fiscal quarter"


class FakeLayout:
    """A region engine that reports a fixed box set per page."""

    def __init__(self, by_page: dict[int, tuple[Region, ...]] | None = None) -> None:
        self.by_page = by_page or {}
        self.calls: list[int] = []

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        self.calls.append(page_no)
        return self.by_page.get(page_no, ())


class ExplodingLayout:
    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        raise RuntimeError("the container died")


class RecordingOcr:
    """An OCR engine that answers from a queue and records what it was fed."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = [CHART_TEXT] if answers is None else answers
        self.references: list[str] = []
        self.rasters: list[tuple[int, int]] = []

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        from PIL import Image

        self.references.append(reference)
        with Image.open(io.BytesIO(png)) as image:
            self.rasters.append(image.size)
        if not self.answers:
            raise AssertionError("more OCR calls than the test authorized")
        return self.answers.pop(0)


class RaisingOcr:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        raise self.error


MIXED_RENDER: "CachedRenderer"
SCANNED_RENDER: "CachedRenderer"


class CachedRenderer:
    """``PageRenderPort`` over the real renderer, memoized per source page.

    Rasterizing the gold pages is the slow part of this file and carries no
    assertion of its own, so each page is drawn once and shared.
    """

    def __init__(self, source_bytes: bytes) -> None:
        self._delegate = Pypdfium2PageRenderer(source_bytes)
        self._cache: dict[int, PageRaster] = {}
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        if page_no not in self._cache:
            self._cache[page_no] = self._delegate.render_page(page_no=page_no)
        return self._cache[page_no]


class FakeRenderer:
    """A renderer that hands out a known 4 x 4 raster, or fails on one page."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        if self.fail_on == page_no:
            raise PageRenderError(f"page {page_no} could not be rendered")
        return PageRaster.of(_png(4, 4), width_px=4, height_px=4)


class ClosableFakeRenderer(FakeRenderer):
    """``FakeRenderer`` that records whether its owner closed it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class ExplodingRenderer:
    """``PageRenderPort`` that raises on one page instead of drawing it.

    A third-party renderer reports its own failure in whatever type its
    library happens to raise, which is exactly the shape the core has to
    name on the injected renderer's behalf.
    """

    def __init__(self, error: Exception, *, fail_on: int | None = None) -> None:
        self.error = error
        self.fail_on = fail_on
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        if self.fail_on is None or self.fail_on == page_no:
            raise self.error
        return PageRaster.of(_png(4, 4), width_px=4, height_px=4)


class BlankOcr:
    """An engine that is present, runs, and reads nothing at all."""

    def __init__(self) -> None:
        self.references: list[str] = []

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        self.references.append(reference)
        return "   \n "


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


MIXED_RENDER = CachedRenderer(MIXED_BYTES)
SCANNED_RENDER = CachedRenderer(SCANNED_BYTES)


def _mixed(
    layout: dict[int, tuple[Region, ...]], ocr, **kwargs
) -> PageRegionParser:
    """A parser over the mixed gold PDF's pages, with the real renderer."""
    kwargs.setdefault("renderer", MIXED_RENDER)
    return PageRegionParser(layout=FakeLayout(layout), ocr=ocr, **kwargs)


def _scanned(
    layout: dict[int, tuple[Region, ...]], ocr, **kwargs
) -> PageRegionParser:
    kwargs.setdefault("renderer", SCANNED_RENDER)
    return PageRegionParser(layout=FakeLayout(layout), ocr=ocr, **kwargs)


def _figure(width_px: float) -> Region:
    """A figure box exactly ``width_px`` wide at the core's render scale."""
    edge = width_px * 72.0 / PAGE_RENDER_DPI
    return Region(
        kind="figure", left_pt=0.0, top_pt=0.0, right_pt=edge, bottom_pt=400.0
    )


# ---------------------------------------------------------------------------
# a PDF page yields one frozen unit plus its page and region metadata
# ---------------------------------------------------------------------------


def test_every_page_of_the_mixed_gold_pdf_yields_one_unit():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="g4-mixed-chart-001",
        version="v1",
    )
    assert [page.page_no for page in result.pages] == [1, 2, 3]
    assert [page.page_count for page in result.pages] == [3, 3, 3]
    units = result.frozen_units()
    assert [unit.locator.unit for unit in units] == [1, 2, 3]
    assert len(units) == len(result.pages)


def test_the_mixed_gold_units_carry_the_documents_identity():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="g4-mixed-chart-001",
        version="v1",
    )
    units = result.frozen_units()
    assert [unit.locator.document_id for unit in units] == ["g4-mixed-chart-001"] * 3
    assert [unit.locator.version for unit in units] == ["v1"] * 3
    assert {unit.source_sha256 for unit in units} == {SHA}
    assert {unit.fmt for unit in units} == {"pdf"}
    assert all(unit.char_count == len(unit.text) for unit in units)


def test_the_mixed_gold_result_names_its_versions_and_source():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="g4-mixed-chart-001",
        version="v1",
    )
    assert result.parse_rule_version and result.params_table_version
    assert (result.document_id, result.version, result.source_sha256) == (
        "g4-mixed-chart-001",
        "v1",
        SHA,
    )


def test_page_metadata_carries_the_media_box_and_the_native_layer():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert [page.size_pt for page in result.pages] == [(612.0, 792.0)] * 3
    assert [page.native_text_char_count for page in result.pages] == [444, 1086, 333]
    assert all(page.scanned_text == "" for page in result.pages)
    assert all(page.ocr_applied is False for page in result.pages)
    assert all(page.regions == () for page in result.pages)


def test_rendered_page_images_are_retained_for_the_vision_stage():
    result = _mixed({}, RecordingOcr()).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    for page in result.pages:
        assert (page.raster.width_px, page.raster.height_px) == (1700, 2200)
        assert page.raster.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert page.raster.sha256 == hashlib.sha256(page.raster.png).hexdigest()


def test_extraction_is_deterministic_over_the_same_bytes():
    def run() -> tuple[tuple[str, str], ...]:
        result = _mixed({}, RecordingOcr()).extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
        return tuple(
            (page.raster.sha256, page.unit.text[:40]) for page in result.pages
        )

    assert run() == run()


def _extract_native_gold() -> tuple[RecordingOcr, ParsedPage]:
    """Parse the native-text gold case once; (ocr, page) for both halves."""
    ocr = RecordingOcr()
    parser = PageRegionParser(layout=FakeLayout({}), ocr=ocr, renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=SHA,
        document_id="g4-native-text-001",
        version="v1",
    )
    assert len(result.pages) == 1
    return ocr, result.pages[0]


def test_the_native_text_gold_page_is_frozen_from_its_own_layer():
    """The fourth §8 shape, first half: the unit text is the text layer.

    Every expectation is the gold manifest's own (poppler-authored from the
    fixture bytes): the quote must come out of the parser's native layer
    under the manifest's whitespace rule.
    """
    _, page = _extract_native_gold()
    # Text extracted from the native layer, matching the manifest quote.
    assert NATIVE_QUOTE in _normalise(page.native_text)
    assert page.unit is not None
    assert page.unit.text == page.native_text
    assert _normalise(page.unit.text) and NATIVE_QUOTE in _normalise(page.unit.text)
    assert page.unit.locator.unit == page.page_no == 1


def test_the_native_text_gold_page_needs_no_ocr_and_reaches_none():
    """The fourth §8 shape, second half: a real text layer means no OCR.

    The page must be answered without a single OCR call — the engine being
    present but untouched is what ``ocr_required False`` means on this path.
    """
    ocr, page = _extract_native_gold()
    # ocr_required False: the page rule on this page's own layer (which
    # carries no image object, the fixture's recorded shape) says no scan,
    # and nothing reached the engine.
    assert (
        detect_ocr_required(
            text=page.native_text, image_count=0, min_text_chars=100
        )
        is False
    )
    assert page.ocr_applied is False
    assert page.ocr_empty is False
    assert page.scanned_text == ""
    assert ocr.references == []


def test_the_native_text_fixture_is_smaller_than_the_mixed_one_and_still_admitted():
    """Sanity on the case the manifest pins: bytes, sha, one page, no image.

    The gold bytes are the manifest's claim about the file, so a drifted
    fixture fails here before any budget decision quotes this dataset.
    """
    assert NATIVE_CASE["media_type"] == "application/pdf"
    assert NATIVE_CASE["page_count"] == 1
    assert len(NATIVE_BYTES) == NATIVE_CASE["bytes"]
    assert hashlib.sha256(NATIVE_BYTES).hexdigest() == NATIVE_CASE["sha256"]
    assert NATIVE_BYTES.startswith(b"%PDF-1.4")
    # The page lane's own scan decision reads image_count from the source;
    # the fixture was authored with zero image objects.
    source = PdfplumberPageSource(NATIVE_BYTES)
    assert source.image_count(page_no=1) == 0
    assert source.native_text(page_no=1).strip()


# ---------------------------------------------------------------------------
# the two OCR rules, and the page the repo rule alone cannot read
# ---------------------------------------------------------------------------


def test_a_figure_region_on_a_native_text_page_is_read_from_its_pixels():
    ocr = RecordingOcr()
    result = _mixed({3: (CHART_BOX,)}, ocr).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    page = result.pages[2]
    assert [region.text for region in page.regions] == [CHART_TEXT]
    assert [region.ocr_applied for region in page.regions] == [True]
    assert page.ocr_applied is True
    # The crop is the chart box at the render scale, not the whole page:
    # each edge is rounded onto the raster independently.
    assert ocr.rasters == [(1466, 911)]
    assert ocr.references == ["doc page 3 figure region"]


def test_reading_a_figures_pixels_does_not_replace_the_pages_native_text():
    result = _mixed({3: (CHART_BOX,)}, RecordingOcr()).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    page = result.pages[2]
    assert page.unit is not None
    assert "94,836" in page.unit.text
    assert CHART_TEXT not in page.unit.text
    assert page.scanned_text == ""
    assert page.native_text_char_count == 333


def test_the_page_level_scan_rule_alone_cannot_reach_that_page():
    """The committed counterexample to ``detect_ocr_required`` by itself.

    Page 3 of the mixed fixture carries 333 native characters and one
    image: the page rule says no OCR, and the text drawn inside its chart
    is reachable only through the region rule.
    """
    native = (
        _mixed({}, RecordingOcr(), renderer=FakeRenderer())
        .extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
        .pages[2]
        .native_text
    )
    assert detect_ocr_required(text=native, image_count=1, min_text_chars=100) is False
    read = _mixed({3: (CHART_BOX,)}, RecordingOcr()).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert read.pages[2].regions[0].ocr_applied is True


def test_the_page_lane_states_its_own_scan_threshold_at_the_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """This lane scans below 100 characters, and writes that number itself.

    The page rule and the seven-format parse ask ``detect_ocr_required`` two
    different questions with two different numbers, so the page lane must not
    ride whichever value the shared rule happens to default to.
    """
    core = sys.modules[PageRegionParser.__module__]
    asked: list[int] = []

    def record(*, text: str, image_count: int, min_text_chars: int) -> bool:
        asked.append(min_text_chars)
        return False

    monkeypatch.setattr(core, "detect_ocr_required", record)
    _mixed({}, RecordingOcr(), renderer=FakeRenderer()).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert asked == [100, 100, 100]


def test_a_small_figure_is_not_read_and_keeps_its_box():
    parser = _mixed({1: (LOGOTYPE_BOX,)}, RecordingOcr())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    region = result.pages[0].regions[0]
    assert region == LOGOTYPE_BOX
    assert region.text == "" and region.ocr_applied is False


@pytest.mark.parametrize(
    "region, expected_legible",
    [
        (_figure(MIN_OCR_REGION_EDGE_PX), True),
        (_figure(MIN_OCR_REGION_EDGE_PX - 1), False),
        (_figure(MIN_OCR_REGION_EDGE_PX + 40), True),
        (
            Region(
                kind="table", left_pt=0.0, top_pt=0.0, right_pt=400.0,
                bottom_pt=400.0,
            ),
            False,
        ),
        (
            Region(
                kind="text", left_pt=0.0, top_pt=0.0, right_pt=400.0,
                bottom_pt=400.0,
            ),
            False,
        ),
    ],
)
def test_only_a_figure_at_or_over_the_legibility_floor_is_read(
    region, expected_legible
):
    ocr = RecordingOcr() if expected_legible else RecordingOcr([])
    parser = _mixed({1: (region,)}, ocr)
    parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=1,
    )
    assert bool(ocr.references) is expected_legible


def test_a_scanned_page_is_read_whole_and_its_regions_are_not_read_twice():
    ocr = RecordingOcr(["CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)"])
    result = _scanned({1: (CHART_BOX,)}, ocr).extract_pdf(
        source_bytes=SCANNED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=1,
    )
    page = result.pages[0]
    assert page.native_text_char_count == 0
    assert page.scanned_text.startswith("CONDENSED")
    assert page.unit is not None and page.unit.text == page.scanned_text
    assert page.ocr_applied is True
    # The region boxes stay as metadata; their pixels were just read whole.
    assert page.regions == (CHART_BOX,)
    assert ocr.references == ["doc page 1"]
    assert ocr.rasters == [(1700, 2200)]


def test_the_scan_decision_is_not_gated_on_regions_the_layout_missed():
    """Additive metadata: a page the layout engine describes as empty is
    still scanned.

    Measured recall is 0 tables on page 2 of the mixed fixture and 1 figure
    across 72 pages of a real filing, so a region-shaped scan rule would
    drop every scanned page this fixture stands for.
    """
    for layout in (NullLayout(), FakeLayout()):
        parser = PageRegionParser(
            layout=layout,
            ocr=RecordingOcr(["x", "y", "z"]),
            renderer=SCANNED_RENDER,
        )
        result = parser.extract_pdf(
            source_bytes=SCANNED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
        assert [page.ocr_applied for page in result.pages] == [True, True, True]
        assert len(result.frozen_units()) == 3


# ---------------------------------------------------------------------------
# the standalone-image page
# ---------------------------------------------------------------------------


def _extract_bare_image(ocr: RecordingOcr) -> tuple[object, ParsedPage]:
    parser = PageRegionParser(layout=NullLayout(), ocr=ocr)
    result = parser.extract_image(
        source_bytes=IMAGE_BYTES,
        source_sha256=SHA,
        document_id="img",
        version="v1",
    )
    (page,) = result.pages
    return result, page


def test_a_bare_image_is_one_page_whose_raster_is_the_uploaded_bytes():
    result, page = _extract_bare_image(RecordingOcr([CHART_TEXT]))
    assert (page.page_no, page.page_count) == (1, 1)
    assert page.raster.png == IMAGE_BYTES
    assert (page.raster.width_px, page.raster.height_px) == (1200, 746)
    assert page.size_pt == pytest.approx((432.0, 268.56), abs=0.01)


def test_a_bare_images_unit_is_the_recognized_text_not_a_native_layer():
    _, page = _extract_bare_image(RecordingOcr([CHART_TEXT]))
    assert page.native_text == "" and page.native_text_char_count == 0
    assert page.unit is not None and page.unit.text == CHART_TEXT
    assert page.unit.fmt == "png"


def test_a_bare_image_is_one_figure_region_whole_read():
    result, page = _extract_bare_image(RecordingOcr([CHART_TEXT]))
    assert [region.kind for region in page.regions] == ["figure"]
    assert page.regions[0].text == CHART_TEXT
    assert page.regions[0].ocr_applied is True
    assert result.fmt == "png"


def test_a_bare_image_is_recognized_once():
    ocr = RecordingOcr([CHART_TEXT])
    PageRegionParser(layout=NullLayout(), ocr=ocr).extract_image(
        source_bytes=IMAGE_BYTES, source_sha256=SHA, document_id="img", version="v1"
    )
    assert ocr.references == ["img image page 1"]
    assert ocr.rasters == [(1200, 746)]


# ---------------------------------------------------------------------------
# the public PDF signature predicate
# ---------------------------------------------------------------------------


def _marker_at(offset: int) -> bytes:
    """Bytes carrying the marker starting at ``offset``, plus a short body."""
    return b"x" * offset + PDF_MAGIC + b"1.4 body"


def test_the_exported_signature_constant_is_the_marker_bytes():
    assert PDF_MAGIC == b"%PDF-"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_marker_at(0), True),  # the common case: header at offset 0
        (_marker_at(42), True),  # a small prefix is legal per the PDF spec
        (_marker_at(1019), True),  # marker ends exactly at the window edge
        (_marker_at(1020), False),  # one byte later it spills past the window
        (_marker_at(1024), False),
        (_marker_at(4096), False),  # a real header, too deep to count
        (b"", False),  # empty input
        (b"%PD", False),  # shorter than the marker itself
        (b"\x00\x01\xff\xfe no header here", False),  # arbitrary bytes
    ],
)
def test_has_pdf_signature_names_the_marker_inside_the_leading_window(
    raw, expected
):
    assert has_pdf_signature(raw) is expected


def test_the_signature_gate_asks_the_pdf_question_the_page_lane_asks():
    """One predicate for one fact, asserted on the routing it decides.

    A header behind leading bytes is a PDF to both the failure-classifying
    gate and the page lane, so a file is never mismatched under one window
    and accepted under another.
    """
    envelope = b"envelope\n" + MIXED_BYTES
    assert has_pdf_signature(envelope) is True
    assert content_family(envelope) == "pdf"

    with pytest.raises(FormatMismatchParseError) as excinfo:
        parse_document(
            envelope, fmt="docx", document_id="doc", version="v1"
        )
    assert excinfo.value.code == "format_mismatch"


def test_a_headerless_document_shaped_file_is_not_a_pdf():
    """A real committed DOCX: reads as a document, carries no PDF header."""
    docx = (
        REPO_ROOT
        / "data/corpus/dataset/documents/docx"
        / "AAPL-2022Q3__table_dense__docx__docx.docx"
    ).read_bytes()
    assert docx.startswith(b"PK")
    assert has_pdf_signature(docx) is False


@pytest.mark.parametrize(
    "raw",
    [
        bytes(range(256)),
        b"\xff" * 4096,
        PDF_MAGIC[:-1] * 100,
        bytes(reversed(PDF_MAGIC)) * 300,
    ],
)
def test_has_pdf_signature_never_raises_on_arbitrary_bytes(raw):
    assert isinstance(has_pdf_signature(raw), bool)


def test_the_extract_gate_and_the_public_predicate_agree_on_the_window():
    """One rule for one question: what the consumer pre-checks is what the
    core then accepts, so a file is never rejected under one window and
    opened under another."""
    parser = PageRegionParser(layout=NullLayout(), renderer=FakeRenderer())

    def gate_error(payload: bytes) -> Exception | None:
        try:
            parser.extract_pdf(
                source_bytes=payload,
                source_sha256=SHA,
                document_id="doc",
                version="v1",
            )
        except Exception as exc:  # noqa: BLE001 — the test names the one exit
            return exc
        return None

    outside = b"x" * 1024 + MIXED_BYTES
    assert has_pdf_signature(outside) is False
    error = gate_error(outside)
    assert isinstance(error, UnsupportedFormatParseError)
    assert error.code == "unsupported_format"

    inside = b"x" * 64 + MIXED_BYTES
    assert has_pdf_signature(inside) is True
    assert not isinstance(gate_error(inside), UnsupportedFormatParseError)


# ---------------------------------------------------------------------------
# named exits
# ---------------------------------------------------------------------------


def test_empty_source_bytes_is_named_before_anything_is_opened():
    parser = PageRegionParser(layout=NullLayout())
    with pytest.raises(EmptySourceParseError) as excinfo:
        parser.extract_pdf(
            source_bytes=b"", source_sha256="", document_id="doc", version="v1"
        )
    assert excinfo.value.code == "empty_source"
    with pytest.raises(EmptySourceParseError):
        parser.extract_image(
            source_bytes=b"", source_sha256="", document_id="d", version="v1"
        )


def test_bytes_that_are_not_the_claimed_kind_are_named_unsupported():
    parser = PageRegionParser(layout=NullLayout(), ocr=RecordingOcr(["x"]))
    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        parser.extract_pdf(
            source_bytes=b"not a pdf at all",
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
    assert excinfo.value.code == "unsupported_format"
    # A PDF is not an image, and a GIF is not one either.
    for payload in (MIXED_BYTES, b"GIF89a...."):
        with pytest.raises(UnsupportedFormatParseError):
            parser.extract_image(
                source_bytes=payload,
                source_sha256=SHA,
                document_id="d",
                version="v1",
            )


def test_the_image_lane_and_the_signature_gate_read_one_table():
    """PNG and JPEG are images everywhere; GIF is a container only.

    The asymmetry is the table's decision rather than two lists that happen
    to differ: a GIF is a known binary container for the failure gate, and
    the standalone-image lane still refuses to decode one. The refusal names
    exactly the types the table accepts.
    """
    gif = b"GIF89a" + b"\x00" * 32
    for payload, magic in (
        (_png(8, 8), "png"),
        (b"\xff\xd8\xff" + b"\x00" * 32, "jpeg"),
        (gif, None),
    ):
        assert content_family(payload) == "binary", magic

    parser = PageRegionParser(layout=NullLayout(), ocr=RecordingOcr(["x"]))
    one_page = parser.extract_image(
        source_bytes=_png(8, 8), source_sha256=SHA, document_id="d", version="v1"
    )
    assert one_page.fmt == "png"
    assert one_page.pages[0].page_no == 1
    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        parser.extract_image(
            source_bytes=gif, source_sha256=SHA, document_id="d", version="v1"
        )
    assert "PNG or JPEG" in str(excinfo.value)


def test_a_truncated_image_is_named_malformed_not_empty():
    parser = PageRegionParser(layout=NullLayout(), ocr=RecordingOcr(["x"]))
    with pytest.raises(MalformedSourceParseError) as excinfo:
        parser.extract_image(
            source_bytes=IMAGE_BYTES[:200],
            source_sha256=SHA,
            document_id="d",
            version="v1",
        )
    assert excinfo.value.code == "malformed_source"


def test_a_corrupt_pdf_is_named_malformed():
    parser = PageRegionParser(layout=NullLayout(), renderer=FakeRenderer())
    with pytest.raises(MalformedSourceParseError):
        parser.extract_pdf(
            source_bytes=b"%PDF-1.4\nbroken body\n%%EOF",
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )


def test_ocr_required_without_an_engine_is_the_named_engine_exit():
    scanned = PageRegionParser(
        layout=NullLayout(), renderer=SCANNED_RENDER
    )
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        scanned.extract_pdf(
            source_bytes=SCANNED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert excinfo.value.code == "ocr_required_unavailable"

    figure_only = PageRegionParser(layout=FakeLayout({3: (CHART_BOX,)}), ocr=None)
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        figure_only.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
    assert excinfo.value.code == "ocr_required_unavailable"
    assert "page 3 figure region" in str(excinfo.value)


def test_a_typed_engine_failure_passes_through_unmodified():
    parser = PageRegionParser(
        layout=NullLayout(),
        ocr=RaisingOcr(OcrRequiredUnavailableError("tesseract is missing")),
        renderer=SCANNED_RENDER,
    )
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        parser.extract_pdf(
            source_bytes=SCANNED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert str(excinfo.value) == "tesseract is missing"


def test_an_untyped_engine_crash_is_converted_and_names_the_page():
    parser = PageRegionParser(
        layout=NullLayout(),
        ocr=RaisingOcr(RuntimeError("segfault in the child")),
        renderer=SCANNED_RENDER,
    )
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        parser.extract_pdf(
            source_bytes=SCANNED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert excinfo.value.code == "ocr_required_unavailable"
    assert "doc page 1" in str(excinfo.value)


def test_empty_ocr_text_is_data_about_one_page_not_a_document_failure():
    """An engine that ran and read nothing must not cost the other pages.

    The blank first page of a scan-shaped document is a fact about that page;
    the caller's own empty-text exit needs the parse to finish so the page can
    reach it. A page that never got an engine at all stays a document failure
    (see ``test_ocr_required_without_an_engine_is_the_named_engine_exit``).
    """
    ocr = RecordingOcr(["   \n ", "SECOND PAGE TEXT", ""])
    result = _scanned({}, ocr).extract_pdf(
        source_bytes=SCANNED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
    )
    assert ocr.references == ["doc page 1", "doc page 2", "doc page 3"]
    assert [page.scanned_text for page in result.pages] == [
        "",
        "SECOND PAGE TEXT",
        "",
    ]
    assert [page.ocr_empty for page in result.pages] == [True, False, True]
    # The read was still attempted on each page: "no text" and "no engine"
    # stay distinguishable, and "OCR ran here" is not the same claim as
    # "OCR returned text here".
    assert [page.ocr_applied for page in result.pages] == [True, True, True]
    assert [page.unit is None for page in result.pages] == [True, False, True]
    assert [unit.text for unit in result.frozen_units()] == ["SECOND PAGE TEXT"]
    assert [page.ocr_empty for page in result.pages if not page.ocr_empty] == [
        False
    ]


def test_a_blank_read_page_still_carries_its_geometry_raster_and_identity():
    result = _scanned({}, BlankOcr()).extract_pdf(
        source_bytes=SCANNED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=1,
    )
    (page,) = result.pages
    assert page.ocr_empty is True and page.unit is None
    assert page.size_pt == (612.0, 792.0)
    assert (page.raster.width_px, page.raster.height_px) == (1700, 2200)
    assert page.native_text == "" and page.scanned_text == ""


def test_a_figure_the_engine_reads_nothing_keeps_its_box_and_records_the_read():
    ocr = RecordingOcr([""])
    result = _mixed({3: (CHART_BOX,)}, ocr).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    page = result.pages[2]
    assert [region.text for region in page.regions] == [""]
    assert [region.ocr_applied for region in page.regions] == [True]
    assert page.ocr_applied is True
    # The page-level lane did not run at all on this native-text page, so an
    # empty crop read is never reported as an empty page read.
    assert page.ocr_empty is False
    assert page.scanned_text == ""
    assert page.unit is not None and page.unit.text
    assert ocr.references == ["doc page 3 figure region"]
    # Purity: the source bytes are untouched and the shared input box the
    # crop was made from was rebuilt, never mutated.
    assert MIXED_BYTES == MIXED_PDF.read_bytes()
    assert CHART_BOX.text == "" and CHART_BOX.ocr_applied is False


def test_a_render_failure_is_named_and_not_reported_as_a_textless_page():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer(fail_on=2))
    with pytest.raises(PageRenderError) as excinfo:
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
    assert excinfo.value.code == "render_failed"
    assert "page 2" in str(excinfo.value)


def test_an_injected_renderer_crash_surfaces_as_the_named_render_failure():
    """A renderer's own untyped crash is still a render failure.

    The pixel pass is a required product, so the caller needs one type to
    catch whoever drew the page — otherwise a bare library error would
    escape the parse family and read as a bug in the caller.
    """
    crash = RuntimeError("the native library segfaulted")
    renderer = ExplodingRenderer(crash, fail_on=2)
    ocr = RecordingOcr([])
    parser = _mixed({}, ocr, renderer=renderer)
    with pytest.raises(PageRenderError) as excinfo:
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
    assert excinfo.value.code == "render_failed"
    assert "page 2" in str(excinfo.value)
    assert "RuntimeError: the native library segfaulted" in str(excinfo.value)
    assert excinfo.value.__cause__ is crash
    # Page 1 was fully drawn and still there was no result to hand back: a
    # failed page ends the parse, it does not yield a short document.
    assert renderer.calls == [1, 2]
    assert ocr.references == []


def test_an_injected_renderer_that_already_fails_typed_is_passed_through():
    typed = PageRenderError("page 1 could not be rendered")
    parser = _mixed({}, RecordingOcr([]), renderer=ExplodingRenderer(typed))
    with pytest.raises(PageRenderError) as excinfo:
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert excinfo.value is typed
    assert excinfo.value.__cause__ is None


def test_a_region_box_off_the_raster_is_named_as_a_render_failure():
    # Inside the page's media box, but wholly outside a raster too small to
    # show it: reading it cannot be silently skipped.
    off_raster = Region(
        kind="figure", left_pt=300.0, top_pt=300.0, right_pt=400.0, bottom_pt=400.0
    )
    parser = _mixed({1: (off_raster,)}, RecordingOcr(), renderer=FakeRenderer())
    with pytest.raises(PageRenderError, match="falls outside"):
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )


@pytest.mark.parametrize(
    "region, message",
    [
        (
            Region(
                kind="sparkline", left_pt=0.0, top_pt=0.0, right_pt=10.0,
                bottom_pt=10.0,
            ),
            "region kind",
        ),
        (
            Region(
                kind="figure", left_pt=10.0, top_pt=10.0, right_pt=5.0,
                bottom_pt=20.0,
            ),
            "inverted",
        ),
        (
            Region(
                kind="figure", left_pt=0.0, top_pt=0.0, right_pt=900.0,
                bottom_pt=100.0,
            ),
            "outside",
        ),
        (
            Region(
                kind="figure", left_pt=0.0, top_pt=0.0, right_pt=400.0,
                bottom_pt=400.0, text="pre-filled",
            ),
            "returned text",
        ),
    ],
)
def test_an_unusable_region_claim_terminates_the_parse(region, message):
    parser = _mixed({1: (region,)}, RecordingOcr(), renderer=FakeRenderer())
    with pytest.raises(PageLayoutError, match=message) as excinfo:
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert excinfo.value.code == "layout_unavailable"


def test_a_layout_engine_crash_is_named_not_swallowed():
    parser = PageRegionParser(
        layout=ExplodingLayout(), ocr=RecordingOcr(["x"]), renderer=FakeRenderer()
    )
    with pytest.raises(PageLayoutError, match="container died") as excinfo:
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=1,
        )
    assert excinfo.value.code == "layout_unavailable"


# ---------------------------------------------------------------------------
# caller-side work bound and injection
# ---------------------------------------------------------------------------


def test_max_pages_is_a_deterministic_prefix_not_a_budget_decision():
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=2,
    )
    assert [page.page_no for page in result.pages] == [1, 2]
    assert [page.page_count for page in result.pages] == [2, 2]

    with pytest.raises(ValueError, match="max_pages"):
        parser.extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
            max_pages=0,
        )


def test_a_page_cap_reports_the_page_count_it_was_cut_against():
    """Truncation has to be observable, or a cap silently loses pages.

    The caller that owns budgets decides the overflow exit; corpus only
    reports the two counts, so the decision needs no budget type here and
    the corpus -> host edge stays at zero.
    """
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    capped = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=2,
    )
    assert capped.source_page_count == 3
    assert capped.truncated is True
    assert len(capped.pages) == 2
    # Per-page ``page_count`` keeps its meaning: pages in this result.
    assert [page.page_count for page in capped.pages] == [2, 2]

    whole = parser.extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert whole.source_page_count == 3
    assert whole.truncated is False
    assert [page.page_count for page in whole.pages] == [3, 3, 3]

    at_the_cap = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=3,
    )
    assert (at_the_cap.source_page_count, at_the_cap.truncated) == (3, False)

    over_the_cap = parser.extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
        max_pages=9,
    )
    assert (over_the_cap.source_page_count, over_the_cap.truncated) == (3, False)


def test_a_standalone_image_reports_one_untruncated_source_page():
    result = PageRegionParser(layout=NullLayout(), ocr=RecordingOcr([CHART_TEXT]))
    image = result.extract_image(
        source_bytes=IMAGE_BYTES, source_sha256=SHA, document_id="img", version="v1"
    )
    assert (image.source_page_count, image.truncated) == (1, False)
    assert image.pages[0].ocr_empty is False


def test_the_renderer_and_layout_are_asked_once_per_page():
    renderer = FakeRenderer()
    layout = FakeLayout()
    parser = PageRegionParser(
        layout=layout, ocr=RecordingOcr([]), dpi=150, renderer=renderer
    )
    parser.extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert renderer.calls == [1, 2, 3]
    assert layout.calls == [1, 2, 3]
    assert parser.dpi == 150


def test_a_parser_refuses_an_unusable_scale_or_floor():
    with pytest.raises(ValueError, match="dpi"):
        PageRegionParser(layout=NullLayout(), dpi=0)
    with pytest.raises(ValueError, match="min_region_edge_px"):
        PageRegionParser(layout=NullLayout(), min_region_edge_px=-1)


def test_a_blank_page_yields_no_unit_and_is_not_reported_as_an_ocr_failure():
    import pdfplumber

    blank = _single_blank_page_pdf()
    with pdfplumber.open(io.BytesIO(blank)) as doc:
        assert doc.pages[0].extract_text() in (None, "")
    parser = _mixed({}, RecordingOcr([]), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=blank, source_sha256=SHA, document_id="doc", version="v1"
    )
    (page,) = result.pages
    assert isinstance(page, ParsedPage)
    assert page.unit is None
    assert page.ocr_applied is False
    # A blank page is not an empty OCR read: no engine was ever asked.
    assert page.ocr_empty is False
    assert page.raster.width_px == 4
    assert result.frozen_units() == ()


def _single_blank_page_pdf() -> bytes:
    import pypdfium2 as pdfium

    builder = pdfium.PdfDocument.new()
    page = builder.new_page(612, 792)
    buffer = io.BytesIO()
    builder.save(buffer)
    page.close()
    builder.close()
    return buffer.getvalue()


def test_the_parse_rule_version_is_pinned_to_the_value_that_keys_identity():
    """Exact equality, not truthiness: the stamp is an identity ingredient.

    Every unit's ``parse_rule_version`` and the block id derived from it are
    how the permanent corpus decides a stored unit is stale, so a silent move
    of this constant is a whole-corpus rebuild and a silent non-move is two
    different rules claiming to be one.
    """
    assert PARSE_RULE_VERSION == "parse-2"
    parser = _mixed({}, RecordingOcr(), renderer=FakeRenderer())
    result = parser.extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert result.parse_rule_version == "parse-2"
    assert {unit.parse_rule_version for unit in result.frozen_units()} == {"parse-2"}
    image = PageRegionParser(layout=NullLayout(), ocr=RecordingOcr([CHART_TEXT]))
    image_result = image.extract_image(
        source_bytes=IMAGE_BYTES, source_sha256=SHA, document_id="img", version="v1"
    )
    assert image_result.parse_rule_version == "parse-2"
    assert image_result.pages[0].unit.parse_rule_version == "parse-2"


def test_the_default_renderer_is_closed_when_a_pdf_run_ends(
    monkeypatch: pytest.MonkeyPatch,
):
    """The core builds the pypdfium2 document, so the core releases it.

    A renderer handed in by a caller stays open: closing it would pull the
    pixels out from under the object someone else is still holding.
    """
    core = sys.modules[PageRegionParser.__module__]

    built: list[Pypdfium2PageRenderer] = []

    class Watched(core.Pypdfium2PageRenderer):
        def __init__(self, source_bytes: bytes, *, dpi: int = PAGE_RENDER_DPI) -> None:
            super().__init__(source_bytes, dpi=dpi)
            built.append(self)

    monkeypatch.setattr(core, "Pypdfium2PageRenderer", Watched)
    PageRegionParser(layout=NullLayout()).extract_pdf(
        source_bytes=MIXED_BYTES,
        source_sha256=SHA,
        document_id="doc",
        version="v1",
    )
    assert len(built) == 1
    assert built[0].closed is True
    # The document it opened is really gone, not only flagged.
    with pytest.raises(PageRenderError, match="closed"):
        built[0].render_page(page_no=1)


def test_a_renderer_failure_still_closes_the_document_it_opened(
    monkeypatch: pytest.MonkeyPatch,
):
    core = sys.modules[PageRegionParser.__module__]

    built: list[Pypdfium2PageRenderer] = []

    class Watched(core.Pypdfium2PageRenderer):
        def __init__(self, source_bytes: bytes, *, dpi: int = PAGE_RENDER_DPI) -> None:
            super().__init__(source_bytes, dpi=dpi)
            built.append(self)

        def render_page(self, *, page_no: int) -> PageRaster:
            raise PageRenderError(f"page {page_no} could not be rendered")

    monkeypatch.setattr(core, "Pypdfium2PageRenderer", Watched)
    with pytest.raises(PageRenderError):
        PageRegionParser(layout=NullLayout()).extract_pdf(
            source_bytes=MIXED_BYTES,
            source_sha256=SHA,
            document_id="doc",
            version="v1",
        )
    assert [renderer.closed for renderer in built] == [True]


def test_an_injected_renderer_is_left_open_for_its_owner():
    renderer = ClosableFakeRenderer()
    PageRegionParser(layout=NullLayout(), renderer=renderer).extract_pdf(
        source_bytes=MIXED_BYTES, source_sha256=SHA, document_id="doc", version="v1"
    )
    assert renderer.calls == [1, 2, 3]
    assert renderer.close_calls == 0


def test_the_renderer_is_closable_by_hand_and_refuses_to_draw_afterwards():
    renderer = Pypdfium2PageRenderer(MIXED_BYTES)
    assert renderer.closed is False
    assert renderer.render_page(page_no=1).width_px == 1700
    with renderer as entered:
        assert entered is renderer
        assert entered.render_page(page_no=2).height_px == 2200
    assert renderer.closed is True
    renderer.close()  # idempotent: a second close is not an error
    with pytest.raises(PageRenderError, match="closed"):
        renderer.render_page(page_no=1)


def test_the_shipped_renderer_leaves_the_naming_to_the_core():
    """One site writes the render-failure sentence, and it is not this one.

    :meth:`PageRegionParser._render_page` turns any renderer's failure into
    the named ``PageRenderError``, so the shipped renderer keeps no copy of
    that message — two copies of one sentence drift the moment either is
    edited.
    """
    raised: BaseException | None = None
    with Pypdfium2PageRenderer(MIXED_BYTES) as renderer:
        try:
            renderer.render_page(page_no=99)
        except BaseException as exc:  # noqa: BLE001 — the test names the type
            raised = exc
    assert raised is not None
    assert not isinstance(raised, PageRenderError)


def test_a_source_with_no_pages_closes_the_document_it_opened(
    monkeypatch: pytest.MonkeyPatch,
):
    """Refusing a pageless document must not leak the file it just read.

    The caller cannot close what the constructor never returned, so the
    refusal releases the document on its way out.
    """
    import pdfplumber

    opened: list["_PagelessDocument"] = []

    class _PagelessDocument:
        pages: list[object] = []

        def __init__(self) -> None:
            self.closed = False
            opened.append(self)

        def close(self) -> None:
            self.closed = True

    def fake_open(*_args: object, **_kwargs: object) -> _PagelessDocument:
        return _PagelessDocument()

    monkeypatch.setattr(pdfplumber, "open", fake_open)
    with pytest.raises(MalformedSourceParseError, match="no pages"):
        PdfplumberPageSource(b"%PDF-1.4 body")
    assert [document.closed for document in opened] == [True]


def _core_imported_modules() -> list[str]:
    """Every module name the shipped core's own file imports."""
    core = sys.modules[PageRegionParser.__module__]
    tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def test_the_core_import_walk_finds_something_to_check():
    assert _core_imported_modules(), "the import walk found nothing to check"


def test_the_core_holds_no_host_knowledge():
    """The seam's ownership contract, asserted on the shipped module.

    The corpus context owns no budget, no lifecycle, and no thread state
    (``docs/context-map.md``); the ``corpus -> host`` edge count must stay
    zero, so it is checked here as well as by the map's grep gate. Which
    modules the core reaches inside ``fin.corpus`` is its own to grow, and an
    exact-set whitelist would turn every legitimate shared helper into a test
    edit — so the forbidden direction is the whole assertion.
    """
    host_edges = [
        name
        for name in _core_imported_modules()
        if name == "fin.host" or name.startswith("fin.host.")
    ]
    assert len(host_edges) == 0, host_edges
