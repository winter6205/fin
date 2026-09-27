"""The shipped Tesseract configuration, exercised through the real binary.

Every assertion here runs a real ``tesseract`` over real gold pixels by way
of the shipped core (:class:`fin.corpus.parsers._pages.PageRegionParser`),
so what is proven is the whole seam — trigger rule, render, crop, language
and segmentation mode — and not one function in isolation. This is the case
a fake engine cannot stand in for, because what it decides is which
configuration fin ships.

It needs the optional ``[ocr]`` extra and a system ``tesseract`` carrying the
languages :data:`fin.corpus.ocr.DEFAULT_TESSERACT_LANG` names, so it is
skipped when that import fails; the default hermetic suite stays unaffected
while ``uv run --with pytesseract -q tests/corpus`` runs it for real.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

pytest.importorskip(
    "pytesseract", reason="the [ocr] extra and a system tesseract are optional"
)

from fin.corpus.ocr import (  # noqa: E402  (must follow the availability gate)
    DEFAULT_TESSERACT_LANG,
    DEFAULT_TESSERACT_PSM,
    TesseractEngine,
)
from fin.corpus.parsers._pages import (  # noqa: E402
    MIN_OCR_REGION_EDGE_PX,
    PAGE_RENDER_DPI,
    PageRegionParser,
    Region,
)

_GOLD = Path(__file__).resolve().parents[2] / "data" / "corpus" / "g4_attachment_gold"
_MANIFEST = json.loads((_GOLD / "manifest.json").read_text())
_MIXED = _GOLD / "g4-mixed-chart-001.pdf"
_SCANNED = _GOLD / "g4-scanned-001.pdf"
_IMAGE = _GOLD / "g4-image-chart-001.png"

# The committed figure box on the mixed case's third page: the chart is
# displayed at 528 x 328 pt with its top-left at x 42, top y 100.
_MIXED_PAGE3_FIGURE = Region(
    kind="figure", left_pt=42.0, top_pt=100.0, right_pt=570.0, bottom_pt=428.0
)


class _CommittedLayout:
    """A layout port that hands back exactly the boxes the manifest names.

    The pinned Docling/Heron pass is not part of this decision; the question
    here is whether the shipped configuration reads the pixels those
    committed boxes point at.
    """

    def __init__(self, boxes: dict[int, tuple[Region, ...]]) -> None:
        self._boxes = boxes

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        return self._boxes.get(page_no, ())


def _flat(text: str) -> str:
    return " ".join(text.split())


def _page_expectation(case_id: str, page_no: int) -> dict:
    case = next(c for c in _MANIFEST["cases"] if c["case_id"] == case_id)
    return next(p for p in case["per_page"] if p["page"] == page_no)


def _parse(pdf: Path, layout: _CommittedLayout | None = None):
    data = pdf.read_bytes()
    return PageRegionParser(
        layout=layout or _CommittedLayout({}),
        ocr=TesseractEngine(),
    ).extract_pdf(
        source_bytes=data,
        source_sha256=hashlib.sha256(data).hexdigest(),
        document_id=pdf.stem,
        version="v1",
    )


@pytest.fixture(scope="module")
def mixed_pages():
    """One real parse of the mixed case, with the committed chart box wired."""
    return _parse(_MIXED, _CommittedLayout({3: (_MIXED_PAGE3_FIGURE,)})).pages


@pytest.fixture(scope="module")
def scanned_pages():
    """One real parse of the three image-only pages, priced once."""
    return _parse(_SCANNED).pages


def test_shipped_config_is_named_not_assumed() -> None:
    """The binary is told the mode, so cost figures describe real calls.

    Tesseract's own default with no ``--psm`` on the command line is mode 3;
    a shipped value equal to it would be an unchosen configuration again.
    """
    engine = TesseractEngine()
    assert engine.psm == DEFAULT_TESSERACT_PSM
    assert engine.lang == DEFAULT_TESSERACT_LANG
    assert engine.tesseract_config == f"--psm {DEFAULT_TESSERACT_PSM}"
    assert DEFAULT_TESSERACT_PSM == "6"


def test_figure_text_on_a_native_text_page_is_reached_by_the_crop(
    mixed_pages,
) -> None:
    """The chart title exists only as pixels on a page that has a text layer.

    Both halves are asserted: the figure region carries the read, and the
    page's own unit keeps its native layer, so a region read never replaces
    the text a citation is quoted from.
    """
    expectation = _page_expectation("g4-mixed-chart-001", 3)
    assert expectation["ocr_expected"] is True

    page = mixed_pages[2]
    figure = next(r for r in page.regions if r.kind == "figure")

    assert figure.ocr_applied is True
    assert _flat(expectation["expected_ocr_quote"]) in _flat(figure.text)
    assert any(
        _flat(alt) in _flat(figure.text)
        for alt in expectation["expected_ocr_quote_alternatives"]
    ), "no committed alternative expectation was reproduced either"
    assert page.native_text_char_count >= 100
    assert _flat(expectation["expected_quote"]) in _flat(page.unit.text)


def test_a_logo_box_under_the_floor_costs_no_call_and_holds_no_text() -> None:
    """The committed 33 x 40 pt logotype is not worth an OCR call.

    Asserted as a real parse rather than as arithmetic: the box stays region
    metadata, unread, while the page keeps its native text.
    """
    logo = Region(
        kind="figure", left_pt=0.0, top_pt=0.0, right_pt=33.0, bottom_pt=40.0
    )
    logo_edge_px = round(min(33.0, 40.0) * PAGE_RENDER_DPI / 72.0)
    assert logo_edge_px < MIN_OCR_REGION_EDGE_PX <= round(
        min(528.0, 328.0) * PAGE_RENDER_DPI / 72.0
    )

    pages = _parse(_MIXED, _CommittedLayout({1: (logo,)})).pages

    assert [r.ocr_applied for r in pages[0].regions] == [False]
    assert pages[0].regions[0].text == ""
    assert pages[0].ocr_applied is False


@pytest.mark.parametrize("page_no", [1, 2, 3])
def test_every_scanned_page_yields_its_committed_quote(
    page_no: int, scanned_pages
) -> None:
    """An image-only page's whole payload comes from the page-level read.

    Digits are asserted separately: a financial row that lost a number is a
    wrong answer even when the heading came back.
    """
    expectation = _page_expectation("g4-scanned-001", page_no)
    assert expectation["ocr_expected"] is True
    assert expectation["native_text_chars"] == 0

    page = scanned_pages[page_no - 1]
    text = _flat(page.scanned_text)

    assert _flat(expectation["expected_quote"]) in text
    for digit_run in re.findall(r"\d[\d,]{3,}", expectation["expected_quote"]):
        assert digit_run in text.replace(" ", ""), (
            f"page {page_no}: OCR lost or mangled the number {digit_run}"
        )


def test_standalone_image_is_readable_without_a_page_container() -> None:
    """A bare PNG needs no PDF wrapper: its pixels are the page raster.

    The unit and the figure region both carry the read, which is how an
    image attachment stays citable and addressable at the same time.
    """
    expectation = _page_expectation("g4-image-chart-001", 1)
    data = _IMAGE.read_bytes()

    result = PageRegionParser(
        layout=_CommittedLayout({}), ocr=TesseractEngine()
    ).extract_image(
        source_bytes=data,
        source_sha256=hashlib.sha256(data).hexdigest(),
        document_id=_IMAGE.stem,
        version="v1",
    )
    page = result.pages[0]
    text = _flat(page.unit.text)

    assert _flat(expectation["expected_quote"]) in text
    hits = [
        alt
        for alt in expectation["expected_ocr_quote_alternatives"]
        if _flat(alt) in text
    ]
    assert len(hits) >= 2, f"only {len(hits)} committed labels reproduced"
    assert page.regions[0].ocr_applied is True
    assert (page.raster.width_px, page.raster.height_px) == (1200, 746)
