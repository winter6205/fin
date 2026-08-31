"""T7 / SC8: scanned-page OCR branch in the PDF parser + named failure.

SC8: a page with no text layer AND images goes through the OCR branch
and is written with non-empty frozen text plus ``ocr_applied``; when
the OCR engine is missing or the OCR call fails, that unit terminates
as a **named failure** — the product never contains an "empty text but
recorded as successful" unit, and no unexpected (untyped) exception
escapes to the caller.

Default-suite discipline: no real OCR engine is needed. The success
path is driven by an injected fake engine; the missing-engine path is
driven by simply not injecting one (the spec's own "engine missing"
shape). The scanned fixture is a tiny hand-assembled PDF (no text
operators, one image XObject) — no large corpus, no network, no
service.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fin.corpus.errors import NoUnitsParseError
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.ocr import OCRError, OcrRequiredUnavailableError
from fin.corpus.parsers._common import ParsedUnit
from fin.corpus.parsers._pdf import OcrParsedUnit, parse_pdf

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PDF_SAMPLE = _PROJECT_ROOT / "data" / "corpus" / "pdf" / "2023_Q3_AAPL.pdf"


# ---------------------------------------------------------------------------
# Fixtures: a deterministic hand-assembled PDF + a fake engine.
# ---------------------------------------------------------------------------


def _build_pdf(pages: list[str]) -> bytes:
    """Assemble a minimal deterministic PDF from a page plan.

    Each plan entry is either ``"text"`` (one line of extractable text,
    no images) or ``"scan"`` (no text operators, one image XObject).
    Only pdfminer-level correctness matters: the xref table is exact and
    every ``stream``/``endstream`` pair declares its real length.
    """
    header = b"%PDF-1.4\n"
    # Object 1: catalog, 2: pages, 3: font (for text pages).
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [",
        b"<< /Base14 /Helvetica /Type /Font /Subtype /Type1 >>",
    ]
    page_obj_nums: list[int] = []
    next_num = 4
    for kind in pages:
        page_obj_nums.append(next_num)
        next_num += 1  # the page object
        if kind == "text":
            content = b"BT /F1 12 Tf 72 720 Td (Fixture narrative text.) Tj ET"
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {next_num} 0 R >>"
                ).encode()
            )
            next_num += 1  # the content object
            objects.append(
                f"<< /Length {len(content)} >>\nstream\n".encode()
                + content
                + b"\nendstream"
            )
        elif kind == "scan":
            image_num = next_num
            next_num += 1  # the image object
            content = b"q 400 0 0 600 100 100 cm /Im0 Do Q"
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /XObject << /Im0 {image_num} 0 R >> >> "
                    f"/Contents {next_num} 0 R >>"
                ).encode()
            )
            next_num += 1  # the content object
            image_data = b"\x00" * 16
            objects.append(
                (
                    "<< /Type /XObject /Subtype /Image /Width 4 /Height 4 "
                    "/ColorSpace /DeviceGray /BitsPerComponent 8 "
                    f"/Length {len(image_data)} >>\nstream\n"
                ).encode()
                + image_data
                + b"\nendstream"
            )
            objects.append(
                f"<< /Length {len(content)} >>\nstream\n".encode()
                + content
                + b"\nendstream"
            )
        else:  # pragma: no cover - plan typo guard
            raise ValueError(f"unknown page kind {kind!r}")
    objects[1] = (
        "<< /Type /Pages /Kids ["
        + " ".join(f"{n} 0 R" for n in page_obj_nums)
        + f"] /Count {len(page_obj_nums)} >>"
    ).encode()

    out = bytearray(header)
    offsets: list[int] = []
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
    return bytes(out)


class FakeOCREngine:
    """Scriptable fake engine — the injection seam for the success path.

    Never touches a real OCR engine: ``text_by_page`` maps 1-based page
    numbers to returned text, ``raises`` (an exception *class*) makes
    every unrecognized page fail like a broken engine, and pages not in
    the map fall back to ``default_text``.
    """

    def __init__(
        self,
        *,
        text_by_page: dict[int, str] | None = None,
        default_text: str = "Scanned revenue table OCR output.",
        raises: type[Exception] | None = None,
    ) -> None:
        self.text_by_page = text_by_page or {}
        self.default_text = default_text
        self.raises = raises
        self.calls: list[tuple[Path | None, int]] = []

    def recognize(self, *, source: Path | None, page: int) -> str:
        self.calls.append((source, page))
        if self.raises is not None:
            raise self.raises(f"fake engine crashed on page {page}")
        if page in self.text_by_page:
            return self.text_by_page[page]
        return self.default_text


def _parse(pages: list[str], **kwargs: object) -> tuple[ParsedUnit, ...]:
    source_bytes = _build_pdf(pages)
    defaults: dict[str, object] = {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "document_id": "DOC-OCR",
        "version": "ver-1",
    }
    defaults.update(kwargs)
    return parse_pdf(source_bytes=source_bytes, **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Success path: OCR branch writes frozen text + ocr_applied.
# ---------------------------------------------------------------------------


def test_scanned_page_routes_through_ocr_and_sets_ocr_applied() -> None:
    units = _parse(["scan"], ocr_engine=FakeOCREngine())
    assert len(units) == 1
    unit = units[0]
    assert isinstance(unit, OcrParsedUnit)
    assert unit.ocr_applied is True
    # Non-empty frozen text, recognized from the fake engine.
    assert unit.text == "Scanned revenue table OCR output."
    assert unit.text.strip() != ""
    # The eight SC10 metadata fields are all present and non-empty.
    assert unit.fmt == "pdf"
    assert unit.source_sha256
    assert unit.locator.document_id == "DOC-OCR"
    assert unit.locator.version == "ver-1"
    assert unit.locator.unit == 1
    assert unit.char_count == len(unit.text)
    assert unit.language
    assert unit.parse_rule_version
    assert unit.params_table_version


def test_ocr_text_goes_through_normalization_outlet() -> None:
    # Raw engine output carrying all four normalization families:
    # soft hyphen (invisible), non-breaking hyphen, CRLF, NBSP,
    # doubled spaces.
    raw = "OCRed\u00ad word\u2011join\r\nwith\u00a0nbsp  gap"
    units = _parse(
        ["scan"],
        ocr_engine=FakeOCREngine(default_text=raw),
    )
    frozen = units[0].text
    assert frozen == normalize_frozen_text(raw)
    # The transformation genuinely ran (not a pass-through of raw).
    assert frozen != raw
    assert "\u00ad" not in frozen
    assert "\u2011" not in frozen
    assert "\r" not in frozen
    assert "\u00a0" not in frozen
    assert "  " not in frozen


def test_mixed_document_only_scanned_page_is_ocr_applied() -> None:
    units = _parse(["text", "scan"], ocr_engine=FakeOCREngine())
    assert len(units) == 2
    first, second = units
    # The text-layer page keeps the plain (non-OCR) unit shape.
    assert not isinstance(first, OcrParsedUnit)
    assert getattr(first, "ocr_applied", False) is False
    assert first.locator.unit == 1
    # The scanned page goes through the OCR branch.
    assert isinstance(second, OcrParsedUnit)
    assert second.ocr_applied is True
    assert second.locator.unit == 2


def test_engine_receives_source_and_page() -> None:
    engine = FakeOCREngine()
    _parse(["scan"], ocr_engine=engine)
    assert len(engine.calls) == 1
    source, page = engine.calls[0]
    assert page == 1
    assert isinstance(source, Path)


# ---------------------------------------------------------------------------
# Named failure paths: engine missing / OCR failed / empty OCR text.
# ---------------------------------------------------------------------------


def test_missing_engine_is_named_failure_not_empty_success() -> None:
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan"])
    # The named code and the failing unit are both identifiable.
    assert "DOC-OCR" in str(excinfo.value)
    assert "page 1" in str(excinfo.value)
    # Not the generic no-units / malformed shapes.
    assert not isinstance(excinfo.value, NoUnitsParseError)


def test_engine_crash_is_named_failure_not_bare_exception() -> None:
    engine = FakeOCREngine(raises=RuntimeError)
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan"], ocr_engine=engine)
    # The original failure is named inside the typed error, and the
    # RuntimeError itself did not escape (pytest.raises would have let
    # it propagate and failed this test).
    assert "RuntimeError" in str(excinfo.value)


def test_engine_ocerror_is_named_failure() -> None:
    engine = FakeOCREngine(raises=OCRError)
    with pytest.raises(OcrRequiredUnavailableError):
        _parse(["scan"], ocr_engine=engine)


def test_engine_typed_ocr_error_is_reraised_verbatim() -> None:
    class EngineMissingExtra(FakeOCREngine):
        def recognize(self, *, source: Path | None, page: int) -> str:
            raise OcrRequiredUnavailableError("pytesseract is not installed")

    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan"], ocr_engine=EngineMissingExtra())
    assert "pytesseract is not installed" in str(excinfo.value)


def test_empty_ocr_text_is_named_failure_not_empty_success_unit() -> None:
    engine = FakeOCREngine(default_text="   ")
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan"], ocr_engine=engine)
    assert "empty" in str(excinfo.value).lower()


def test_every_failed_page_is_named_in_one_failure() -> None:
    # Page 1 OCRs fine, page 2's engine crashes: the named failure
    # names page 2 (and only page 2 as failed).
    class FlakyOnPageTwo:
        def recognize(self, *, source: Path | None, page: int) -> str:
            if page == 2:
                raise RuntimeError("fake engine crashed on page 2")
            return "Page one recovered text."

    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan", "scan"], ocr_engine=FlakyOnPageTwo())
    assert "page 2" in str(excinfo.value)
    assert "page 1 (" not in str(excinfo.value)


def test_failure_names_all_failed_pages() -> None:
    engine = FakeOCREngine(raises=RuntimeError)
    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _parse(["scan", "scan"], ocr_engine=engine)
    assert "page 1" in str(excinfo.value)
    assert "page 2" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Non-OCR path unchanged (T6 behavior preserved).
# ---------------------------------------------------------------------------


def test_text_layer_pages_need_no_engine_and_stay_plain() -> None:
    units = _parse(["text", "text"])
    assert len(units) == 2
    for unit in units:
        assert not isinstance(unit, OcrParsedUnit)
        assert getattr(unit, "ocr_applied", False) is False


def test_real_text_layer_pdf_does_not_trigger_ocr_branch() -> None:
    # The frozen real 10-Q sample has a text layer on every page: with
    # no engine injected the parse must succeed exactly as in T6 (no
    # OCR branch, no named failure).
    source_bytes = _PDF_SAMPLE.read_bytes()
    units = parse_pdf(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        document_id="AAPL-2023Q3",
        version="ver-1",
        max_units=2,
    )
    assert len(units) == 2
    assert all(getattr(u, "ocr_applied", False) is False for u in units)
