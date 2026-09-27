"""The page rule's committed counterexample and crop-capable recognition.

The page-level rule (:func:`fin.corpus.ocr.detect_ocr_required`) asks one
question about one page: did the text layer collapse while the page still
carries images? On a page with a healthy text layer it can never answer
"yes", so text drawn inside a figure on such a page is reached by the core's
own figure rule — ``fin.corpus.parsers._pages._crop_is_legible`` and the crop
it gates — which :mod:`tests.corpus.test_page_region_core` covers. What is
read from the fixture here is the page the rule must not claim, together with
its committed expectations rather than invented input.

Recognition is the other half: :class:`fin.corpus.ocr.OCREngine` promises a
page call only, and the page/region core consumes pixels
(``recognize_raster(*, png, reference)``). These tests pin both shapes —
that the page call is untouched and that the pixel call exists, uses the
measured configuration, and names the region it failed on.
"""

from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.ocr import (
    DEFAULT_TESSERACT_PSM,
    OCREngine,
    OcrRequiredUnavailableError,
    RasterOCREngine,
    TesseractEngine,
    detect_ocr_required,
)
from fin.corpus.parsers._pages import (
    RasterOcrPort,
    Region,
)

_GOLD = Path(__file__).resolve().parents[2] / "data" / "corpus" / "g4_attachment_gold"
_MANIFEST = json.loads((_GOLD / "manifest.json").read_text())
_MIXED = _GOLD / "g4-mixed-chart-001.pdf"
_SCANNED = _GOLD / "g4-scanned-001.pdf"


def _case(case_id: str) -> dict:
    return next(c for c in _MANIFEST["cases"] if c["case_id"] == case_id)


def _page(case_id: str, page_no: int) -> dict:
    return next(
        p for p in _case(case_id)["per_page"] if p["page"] == page_no
    )


def _native_text(pdf: Path, page_no: int) -> str:
    """The page's text layer as the core's own source port reads it."""
    import pdfplumber

    with pdfplumber.open(pdf) as pdf_file:
        return pdf_file.pages[page_no - 1].extract_text() or ""


# ---------------------------------------------------------------------------
# the committed counterexample to a page-level-only rule
# ---------------------------------------------------------------------------


def test_gold_mixed_page_3_has_native_text_and_needs_ocr() -> None:
    """The manifest says page 3 carries a text layer AND needs OCR.

    Both halves are re-read from the fixture, not trusted from the prose:
    the committed quote must be in the page's own text layer, and the
    committed page rule must answer ``False`` for that same text at *any*
    threshold — which is why the core reaches that page's figure by its own
    region-shaped rule instead.
    """
    expectation = _page("g4-mixed-chart-001", 3)
    native_text = _native_text(_MIXED, 3)

    assert expectation["ocr_expected"] is True
    assert expectation["native_text_present"] is True
    assert len(native_text.strip()) >= 100
    assert " ".join(expectation["expected_quote"].split()) in " ".join(
        native_text.split()
    )
    # No page-level threshold can fire here: any threshold the rule accepts
    # is already below the character count of this page's own text layer.
    assert detect_ocr_required(
        text=native_text, image_count=1, min_text_chars=100
    ) is False
    assert detect_ocr_required(
        text=native_text, image_count=1, min_text_chars=1
    ) is False


# ---------------------------------------------------------------------------
# protocol compatibility: the page call is the contract two agents share
# ---------------------------------------------------------------------------


def test_page_engine_protocol_is_unchanged() -> None:
    """``OCREngine`` still names exactly the one page-level call.

    A sibling wires this Protocol without touching this module, so any
    member added here would break every page-only engine. The pixel call
    lives on a separate Protocol that extends it.
    """
    members = {n for n in dir(OCREngine) if not n.startswith("_")}
    assert members == {"recognize"}
    assert list(inspect.signature(OCREngine.recognize).parameters) == [
        "self",
        "source",
        "page",
    ]


def test_raster_engine_protocol_extends_the_page_protocol() -> None:
    """The pixel call is additive and matches the core's own port shape."""
    members = {n for n in dir(RasterOCREngine) if not n.startswith("_")}
    assert {"recognize", "recognize_raster"} <= members
    assert list(inspect.signature(RasterOCREngine.recognize_raster).parameters) == [
        "self",
        "png",
        "reference",
    ]
    assert list(inspect.signature(RasterOcrPort.recognize_raster).parameters) == [
        "self",
        "png",
        "reference",
    ]


def test_tesseract_engine_satisfies_both_protocols_by_shape() -> None:
    """``TesseractEngine`` answers the page call and the pixel call."""
    engine = TesseractEngine()
    assert callable(engine.recognize)
    assert callable(engine.recognize_raster)
    assert list(inspect.signature(TesseractEngine.recognize).parameters) == [
        "self",
        "source",
        "page",
    ]
    assert list(inspect.signature(TesseractEngine.recognize_raster).parameters) == [
        "self",
        "png",
        "reference",
    ]


def test_page_only_engine_is_still_accepted_by_the_ingest_door() -> None:
    """A double with only ``recognize`` keeps working end to end.

    The regression this guards is the one that would be silent: making the
    pixel call a member of ``OCREngine`` would leave every existing
    page-only engine typing-fine and running-broken, so the shape is
    exercised through the door that consumes it. The scanned gold case is
    three image-only pages, so its page rule fires on every one.
    """
    from fin.corpus.parsers._pdf import parse_pdf

    class _PageOnly:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, int]] = []

        def recognize(self, *, source: Path, page: int) -> str:
            self.calls.append((source, page))
            return f"scanned page {page} text"

    engine = _PageOnly()
    units = parse_pdf(
        source_bytes=_SCANNED.read_bytes(),
        source_sha256="0" * 64,
        document_id="doc",
        version="v1",
        ocr_engine=engine,
    )
    assert [page for _, page in engine.calls] == [1, 2, 3]
    assert [u.locator.unit for u in units] == [1, 2, 3]
    assert all(u.text.strip() for u in units)


# ---------------------------------------------------------------------------
# recognize_raster: pixels in, measured configuration out
# ---------------------------------------------------------------------------


class _FakeTesseract:
    """Records the call and returns scripted text, PNG bytes intact."""

    def __init__(self, *, text: str = "recognized text", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def image_to_string(self, image, *, lang=None, config=None, **kwargs):  # noqa: D102
        self.calls.append({"image": image, "lang": lang, "config": config})
        if self.error is not None:
            raise self.error
        return self.text


def _engine_with(monkeypatch: pytest.MonkeyPatch, fake: _FakeTesseract, **kwargs):
    import sys

    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    return TesseractEngine(**kwargs)


def _png_bytes(*, size: tuple[int, int] = (60, 40)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_recognize_raster_reads_the_supplied_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crop's own pixels are what gets recognized, as a decoded image."""
    fake = _FakeTesseract(text="Total net sales by fiscal quarter")
    engine = _engine_with(monkeypatch, fake)
    png = _png_bytes(size=(64, 40))

    text = engine.recognize_raster(
        png=png, reference="g4-mixed-chart-001 page 3 figure region"
    )

    assert text == "Total net sales by fiscal quarter"
    assert len(fake.calls) == 1
    image = fake.calls[0]["image"]
    assert not isinstance(image, (str, Path)), "pixels were handed over as a path"
    assert (image.width, image.height) == (64, 40)


def test_recognize_raster_sends_the_measured_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--psm`` and ``-l`` are the shipped ones, not Tesseract's defaults.

    An unset ``--psm`` silently selects mode 3, so this asserts the
    argument is actually on the command line for both calls.
    """
    fake = _FakeTesseract()
    engine = _engine_with(monkeypatch, fake)
    png = _png_bytes()

    engine.recognize_raster(png=png, reference="page 3 figure")
    engine.recognize(source=Path("page.png"), page=3)

    assert [call["config"] for call in fake.calls] == [
        f"--psm {DEFAULT_TESSERACT_PSM}",
        f"--psm {DEFAULT_TESSERACT_PSM}",
    ]
    assert [call["lang"] for call in fake.calls] == [engine.lang, engine.lang]


def test_recognize_raster_honours_a_psm_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that measured differently can pass its own mode."""
    fake = _FakeTesseract()
    engine = _engine_with(monkeypatch, fake, psm="11")
    engine.recognize_raster(png=_png_bytes(), reference="page 3 figure")
    assert fake.calls[0]["config"] == "--psm 11"
    assert engine.psm == "11"


@pytest.mark.parametrize(
    ("text", "error"),
    [("", None), ("   \n ", None), ("", RuntimeError("tesseract died"))],
)
def test_recognize_raster_failure_shapes_are_typed(
    monkeypatch: pytest.MonkeyPatch, text: str, error: Exception | None
) -> None:
    """Empty, whitespace-only and engine-exception shapes all name the region.

    A failure that cannot say which region failed would leave the caller
    unable to report the unit it could not freeze.
    """
    fake = _FakeTesseract(text=text, error=error)
    engine = _engine_with(monkeypatch, fake)
    reference = "doc-a page 3 figure region"

    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        engine.recognize_raster(png=_png_bytes(), reference=reference)

    assert reference in str(excinfo.value)


def test_recognize_raster_rejects_pixels_that_cannot_be_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty and non-image bytes are a named OCR failure, not a crash."""
    fake = _FakeTesseract()
    engine = _engine_with(monkeypatch, fake)
    reference = "doc-a page 3 figure region"

    for png in (b"", b"not an image"):
        with pytest.raises(OcrRequiredUnavailableError) as excinfo:
            engine.recognize_raster(png=png, reference=reference)
        assert reference in str(excinfo.value)
    assert fake.calls == [], "undecodable pixels reached the OCR binary"


def test_recognize_raster_without_the_extra_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing optional dependency stays the spec's named EXIT."""
    import sys

    monkeypatch.setitem(sys.modules, "pytesseract", None)
    engine = TesseractEngine()
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        engine.recognize_raster(png=_png_bytes(), reference="page 3 figure region")
    assert "pytesseract" in str(excinfo.value)


def test_region_of_a_page_is_addressable_by_geometry() -> None:
    """A region can be cut by box without a page index at all.

    This is the shape the core's crop passes to recognition: pixels plus a
    human-readable reference, no page number in the signature.
    """
    region = Region(
        kind="figure", left_pt=42.0, top_pt=100.0, right_pt=570.0, bottom_pt=428.0
    )
    assert region.size_pt == (528.0, 328.0)
    assert "page" not in inspect.signature(TesseractEngine.recognize_raster).parameters


# ---------------------------------------------------------------------------
# input contract of the shipped configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("psm", ["", "0", "14", "auto", " 6", "6 ", None, 6])
def test_engine_refuses_an_unusable_page_segmentation_mode(psm: Any) -> None:
    """``--psm`` reaches a subprocess argv, so only a real mode is accepted.

    A value that is not one of Tesseract's modes would either be ignored by
    the binary — silently reverting to the unmeasured default — or turn into
    an argv the caller never asked for.
    """
    with pytest.raises(ValueError):
        TesseractEngine(psm=psm)


@pytest.mark.parametrize("lang", ["", "   ", "eng eng", "not a lang"])
def test_engine_refuses_an_unusable_language_string(lang: str) -> None:
    """Language names reach ``-l``; a blank or spaced value is refused."""
    with pytest.raises(ValueError):
        TesseractEngine(lang=lang)


def test_recognize_raster_refuses_pixels_that_are_not_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path or a text string is a caller bug, named as such."""
    engine = _engine_with(monkeypatch, _FakeTesseract())
    for bad in ("not bytes", 42, None):
        with pytest.raises(TypeError):
            engine.recognize_raster(png=bad, reference="page 3 figure region")

