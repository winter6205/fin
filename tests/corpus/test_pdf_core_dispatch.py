"""The corpus PDF dispatch consumes the shared page/region core.

Pinned contracts:

* text-layer PDFs: ``parse_document(fmt="pdf")`` yields the same frozen
  units (identity + text, byte-for-byte) as the retired ``parse_pdf``
  path — the old path's behavior is the contract, asserted directly
  against it on the committed text-layer fixtures;
* the PDF branch really goes through :class:`PageRegionParser`: injected
  spy layout / fake renderer ports are consulted per page, which the
  old pdfplumber-only path never had;
* the existing named-failure surface is unchanged on the dispatch:
  malformed -> malformed, blank document -> no units,
  ``max_units < 1`` -> no units (the old typed shape, not the core's
  ``ValueError``), scanned page with no engine wired ->
  ``ocr_required_unavailable`` (the outcome class the suite promised
  before the switch; verified against ``test_ocr_branch``'s pinned
  ``parse_pdf`` behavior);
* OCR threading: an injected raster engine turns scanned pages into
  ``OcrParsedUnit`` units carrying ``ocr_applied`` and the frozen
  recognized text, and the ports are PDF-only (other formats never
  consult them).

Default-suite discipline: fake layout/render/OCR ports plus the real
pypdfium2 renderer only on bounded page counts; no layout container, no
OCR binary, no network.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.corpus.parsers import PageRaster, parse_document
from fin.corpus.parsers._common import freeze_text
from fin.corpus.parsers._pdf import OcrParsedUnit, parse_pdf

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PDF_SAMPLE = _PROJECT_ROOT / "data" / "corpus" / "pdf" / "2023_Q3_AAPL.pdf"
_SCANNED_GOLD = (
    _PROJECT_ROOT
    / "data"
    / "corpus"
    / "g4_attachment_gold"
    / "g4-scanned-001.pdf"
)
_PDF_MAX_UNITS = 6


class SpyLayout:
    """Layout port recording every page it was asked about; yields no boxes."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def page_regions(self, *, page_no: int, size_pt):
        self.calls.append(page_no)
        return ()


class FakeRenderer:
    """Render port handing out a known 4 x 4 raster, recording page calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
        return PageRaster.of(
            buffer.getvalue(), width_px=4, height_px=4
        )


class FakeRasterOcr:
    """Raster OCR port answering from a queue, recording what it was fed."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.references: list[str] = []
        self.raster_sizes: list[tuple[int, int]] = []

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        from PIL import Image

        self.references.append(reference)
        with Image.open(io.BytesIO(png)) as image:
            self.raster_sizes.append(image.size)
        if not self.answers:
            raise AssertionError("more OCR calls than the test authorized")
        return self.answers.pop(0)


def _blank_pdf() -> bytes:
    """One valid page with no text operators and no image XObject."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return out


# ----- the old path's behavior is the pinned contract --------------------


def test_text_layer_units_match_the_retired_parse_pdf_path() -> None:
    source_bytes = _PDF_SAMPLE.read_bytes()
    old_units = parse_pdf(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        document_id="T3B-PIN",
        version="v1",
        max_units=_PDF_MAX_UNITS,
    )
    result = parse_document(
        source_bytes,
        fmt="pdf",
        document_id="T3B-PIN",
        version="v1",
        max_units=_PDF_MAX_UNITS,
    )
    assert len(old_units) == _PDF_MAX_UNITS
    assert result.units == old_units


def test_text_layer_gold_fixture_matches_the_retired_path_whole_document() -> None:
    # The mixed text+figure gold fixture, uncapped: region/figure
    # behavior differs by design, the page-unit text may not.
    source_bytes = (
        _PROJECT_ROOT
        / "data"
        / "corpus"
        / "g4_attachment_gold"
        / "g4-mixed-chart-001.pdf"
    ).read_bytes()
    old_units = parse_pdf(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        document_id="T3B-GOLD",
        version="v1",
    )
    result = parse_document(
        source_bytes,
        fmt="pdf",
        document_id="T3B-GOLD",
        version="v1",
    )
    assert result.units == old_units


# ----- the dispatch consults the core's ports ----------------------------


def test_pdf_dispatch_goes_through_the_page_region_core() -> None:
    layout = SpyLayout()
    renderer = FakeRenderer()
    result = parse_document(
        _PDF_SAMPLE.read_bytes(),
        fmt="pdf",
        document_id="T3B-CORE",
        version="v1",
        max_units=3,
        layout=layout,
        renderer=renderer,
    )
    # The old pdfplumber-only path never queried a layout or a renderer;
    # seeing per-page calls on both ports is what proves the core serves
    # this branch.
    assert layout.calls == [1, 2, 3]
    assert renderer.calls == [1, 2, 3]
    assert len(result.units) == 3


def test_pdf_ports_are_not_consulted_for_other_formats() -> None:
    layout = SpyLayout()
    renderer = FakeRenderer()
    ocr = FakeRasterOcr(["never called"])
    result = parse_document(
        b"## Heading\n\nBody sentence with enough characters to freeze.\n",
        fmt="md",
        document_id="T3B-MD",
        version="v1",
        layout=layout,
        renderer=renderer,
        ocr=ocr,
    )
    assert result.units
    assert layout.calls == []
    assert renderer.calls == []
    assert ocr.references == []


# ----- OCR threading on the corpus path ----------------------------------


def test_scanned_pdf_without_engine_keeps_the_named_ocr_required_exit() -> None:
    # The outcome the pre-switch suite promised: a scanned page with no
    # engine wired ends the parse in ``OcrRequiredUnavailableError``
    # (pinned to ``parse_pdf``'s behavior in ``test_ocr_branch``) — the
    # core path keeps the class, not a silent skip or a bare exception.
    scanned = _SCANNED_GOLD.read_bytes()
    with pytest.raises(OcrRequiredUnavailableError):
        parse_document(
            scanned,
            fmt="pdf",
            document_id="T3B-SCAN",
            version="v1",
            renderer=FakeRenderer(),
        )


def test_scanned_pdf_with_raster_engine_freezes_ocr_units() -> None:
    answers = [
        "Consolidated statement line one reports net sales of 89,503.",
        "Consolidated statement line two reports other income of 1,000.",
        "Notes to the consolidated financial statements page three text.",
    ]
    ocr = FakeRasterOcr(answers)
    result = parse_document(
        _SCANNED_GOLD.read_bytes(),
        fmt="pdf",
        document_id="T3B-OCR",
        version="v1",
        ocr=ocr,
        renderer=FakeRenderer(),
    )
    assert len(result.units) == 3
    assert [u.locator.unit for u in result.units] == [1, 2, 3]
    for unit, answer in zip(result.units, answers):
        assert isinstance(unit, OcrParsedUnit)
        assert unit.ocr_applied is True
        # The engine's answer is frozen through the single outlet — the
        # same construction guarantee ``build_unit`` gives the text layer.
        assert unit.text == freeze_text(answer)
        assert unit.text == normalize_frozen_text(unit.text)
    assert ocr.references == ["T3B-OCR page 1", "T3B-OCR page 2", "T3B-OCR page 3"]


def test_text_layer_page_never_reaches_the_ocr_port() -> None:
    ocr = FakeRasterOcr([])
    result = parse_document(
        _PDF_SAMPLE.read_bytes(),
        fmt="pdf",
        document_id="T3B-NOOCR",
        version="v1",
        max_units=2,
        ocr=ocr,
        renderer=FakeRenderer(),
    )
    assert len(result.units) == 2
    assert all(not isinstance(u, OcrParsedUnit) for u in result.units)
    assert ocr.references == []


def test_ocr_call_without_injected_renderer_gets_real_pixels() -> None:
    # The placeholder raster is only ever handed to a call that cannot
    # consume pixels (no OCR port wired); with an engine and no
    # injected renderer the core's own pypdfium2 pass must reach the
    # engine with the page's real pixels.
    ocr = FakeRasterOcr(
        [
            "Consolidated statement line one reports net sales of 89,503.",
            "Consolidated statement line two reports other income of 1,000.",
            "Notes to the consolidated financial statements page three text.",
        ]
    )
    parse_document(
        _SCANNED_GOLD.read_bytes(),
        fmt="pdf",
        document_id="T3B-REAL",
        version="v1",
        ocr=ocr,
    )
    assert ocr.raster_sizes
    for width, height in ocr.raster_sizes:
        assert (width, height) != (1, 1)
        assert width > 100 and height > 100


# ----- named failures keep their pre-switch shapes -----------------------


def test_malformed_pdf_still_names_malformed_source() -> None:
    with pytest.raises(MalformedSourceParseError) as excinfo:
        parse_document(
            b"%PDF-1.4\nno xref table follows here",
            fmt="pdf",
            document_id="T3B-CORRUPT",
            version="v1",
        )
    assert excinfo.value.code == "malformed_source"


def test_blank_pdf_still_names_no_units() -> None:
    with pytest.raises(NoUnitsParseError) as excinfo:
        parse_document(
            _blank_pdf(),
            fmt="pdf",
            document_id="T3B-BLANK",
            version="v1",
            renderer=FakeRenderer(),
        )
    assert excinfo.value.code == "no_units"
    assert "T3B-BLANK" in str(excinfo.value)


def test_max_units_below_one_keeps_the_old_typed_shape() -> None:
    # The core guards its own bound with ``ValueError``; the corpus
    # dispatch contract is the named ParseError family —
    # the pre-switch shape must survive the adapter.
    with pytest.raises(NoUnitsParseError) as excinfo:
        parse_document(
            _PDF_SAMPLE.read_bytes(),
            fmt="pdf",
            document_id="T3B-CAP",
            version="v1",
            max_units=0,
        )
    assert excinfo.value.code == "no_units"
