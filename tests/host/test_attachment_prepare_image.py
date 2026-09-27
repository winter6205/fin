"""The preparation ADAPTER's standalone-image door (ADR 0089, G4 T2d).

Spec line 79 of ``specs/thread-attachment-source-read.md`` puts a standalone
image on the same page/region contract as a PDF: one unit per physical page,
``attachment_type_unsupported`` for bytes no reader can take, and a typed
failure rather than a blank ``ready``. This file drives that door from
``accept`` to a published record, with the real page/region core in place and
fake ports wherever a boundary is not the adapter's (rasterization, layout
boxing, the OCR engine), so it stays hermetic: no OCR binary, no vision
provider, no network, no database.

Expectations come from ``data/corpus/g4_attachment_gold/manifest.json``
(read with tesseract and Pillow outside fin) and from the spec's code list,
never from the implementation under test.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from fin.corpus.errors import MalformedSourceParseError
from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.corpus.parsers import (
    PageParseResult,
    PageRaster,
    PageRegionParser,
    Region,
)
from fin.host.attachment_budgets import AttachmentBudgets, required_run_calls
from fin.host.attachment_prepare import (
    ATTACHMENT_EMPTY_TEXT,
    ATTACHMENT_TYPE_UNSUPPORTED,
    AttachmentPreparationService,
)
from fin.host.attachment_store import (
    AttachmentRecord,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.thread_store import InMemoryThreadStore

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"

_IMAGE_CASE = next(
    case
    for case in json.loads((GOLD_DIR / "manifest.json").read_text())["cases"]
    if case.get("media_type") == "image/png"
)
GOLD_IMAGE_NAME = _IMAGE_CASE["filename"]
GOLD_IMAGE_BYTES = (REPO_ROOT / _IMAGE_CASE["path"]).read_bytes()
GOLD_IMAGE_PAGES = _IMAGE_CASE["page_count"]
GOLD_IMAGE_QUOTE = _IMAGE_CASE["per_page"][0]["expected_quote"]

# The gold set carries one image fixture and it is a PNG, so the JPEG
# signature is encoded here: the ``.jpg``/``.jpeg`` claim is then tested
# against a real JPEG header rather than a hand-written prefix.
_JPEG_BUFFER = io.BytesIO()
Image.new("RGB", (320, 200), (250, 250, 250)).save(_JPEG_BUFFER, format="JPEG")
JPEG_BYTES = _JPEG_BUFFER.getvalue()

# Neither PNG nor JPEG magic, built here because the gold set holds no
# unsupported image: the shape the spec names ``attachment_type_unsupported``.
NOT_AN_IMAGE = b"BM\x00\x00non-image bytes with no png or jpeg magic"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ----- fake ports: this door's own witnesses -----


class RecordingOcr:
    """``RasterOcrPort`` recording every read it was asked for."""

    def __init__(self, *, answer: str = GOLD_IMAGE_QUOTE) -> None:
        self.calls: list[dict[str, Any]] = []
        self._answer = answer

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        self.calls.append({"png": png, "reference": reference})
        return self._answer


class RecordingRenderer:
    """``PageRenderPort``: a standalone image has no page to rasterize."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        return PageRaster.of(JPEG_BYTES, width_px=1, height_px=1)


class RecordingLayout:
    """``PageLayoutPort``: a standalone image has no page to box."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        self.calls.append(page_no)
        return ()


class RoutingParser(PageRegionParser):
    """The real core, naming which of its two entries this door opened."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.image_calls: list[dict[str, Any]] = []
        self.pdf_calls: list[dict[str, Any]] = []

    def extract_image(self, **kwargs: Any) -> PageParseResult:
        self.image_calls.append(kwargs)
        return super().extract_image(**kwargs)

    def extract_pdf(self, **kwargs: Any) -> PageParseResult:
        self.pdf_calls.append(kwargs)
        return super().extract_pdf(**kwargs)


def _parser(
    *,
    ocr: Any = "default",
    layout: RecordingLayout | None = None,
    renderer: RecordingRenderer | None = None,
) -> RoutingParser:
    return RoutingParser(
        layout=layout if layout is not None else RecordingLayout(),
        renderer=renderer if renderer is not None else RecordingRenderer(),
        ocr=RecordingOcr() if ocr == "default" else ocr,
    )


def _service(
    parser: PageRegionParser, *, max_pages: int = 8
) -> tuple[AttachmentPreparationService, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="image lane thread")
    crop_calls = 8
    # Derive the run budget through the module that prices a run, so this
    # fixture cannot drift from the page + crop + retry-reserve relationship.
    run_calls = required_run_calls(
        AttachmentBudgets(
            max_upload_bytes=1_048_576,
            max_pdf_pages=max_pages,
            # Only the page and crop counts price a run, so this total is what
            # the call returns, never an input to it.
            max_vision_calls_per_run=max_pages + crop_calls,
            max_vision_crop_calls_per_run=crop_calls,
        )
    )
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads),
        objects=InMemoryObjectStore(),
        budgets=AttachmentBudgets(
            max_upload_bytes=1_048_576,
            max_pdf_pages=max_pages,
            max_vision_calls_per_run=run_calls,
            max_vision_crop_calls_per_run=crop_calls,
        ),
        pages=parser,
    )
    return service, thread.id


def _prepare(
    service: AttachmentPreparationService,
    thread_id: str,
    *,
    filename: str,
    payload: bytes,
) -> tuple[AttachmentRecord, tuple[ParsedAttachmentUnit, ...]]:
    record = service.accept(
        thread_id=thread_id, filename=filename, payload=payload
    )
    assert record.status == "uploaded", record
    outcome = service.prepare(record.attachment_id)
    return outcome, service.units(record.attachment_id)


# ----- the routing itself -----


def test_an_image_claim_opens_the_image_reader_with_the_uploaded_bytes() -> None:
    parser = _parser()
    service, thread_id = _service(parser)

    record, _units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert [len(parser.pdf_calls), len(parser.image_calls)] == [0, 1], record
    claim = parser.image_calls[0]
    assert claim["source_bytes"] == GOLD_IMAGE_BYTES
    assert claim["document_id"] == record.attachment_id
    assert claim["source_sha256"] == record.sha256
    assert claim["version"] == str(record.version)


# ----- one page, one unit: the published shape -----


@pytest.mark.parametrize("max_pages", [1, 8])
def test_a_gold_image_freezes_exactly_the_units_its_page_count_claims(
    max_pages: int,
) -> None:
    service, thread_id = _service(_parser(), max_pages=max_pages)

    ready, units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert ready.status == "ready", ready
    assert ready.unit_count == GOLD_IMAGE_PAGES == 1, ready
    assert [unit.unit for unit in units] == [1], units
    assert ready.indexed, ready


@pytest.mark.parametrize("filename", ["chart.jpg", "chart.jpeg"])
def test_a_jpeg_upload_freezes_the_same_one_unit_shape(filename: str) -> None:
    service, thread_id = _service(_parser())

    ready, units = _prepare(
        service, thread_id, filename=filename, payload=JPEG_BYTES
    )

    assert ready.status == "ready", ready
    assert [unit.unit for unit in units] == [1], units


# ----- what the unit carries, and what it cost -----


def test_the_image_unit_carries_the_text_the_reader_returned() -> None:
    service, thread_id = _service(
        _parser(
            ocr=RecordingOcr(
                answer="Total net sales by fiscal quarter\n"
                "(US$ millions, three months ended)\n82,959  117,154"
            )
        )
    )

    ready, units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert ready.status == "ready", ready
    assert _flat(units[0].text) == (
        "Total net sales by fiscal quarter "
        "(US$ millions, three months ended) 82,959 117,154"
    ), units
    assert GOLD_IMAGE_QUOTE in _flat(units[0].text), units


def test_a_recognized_image_pays_for_one_whole_page_read_and_nothing_else() -> None:
    ocr = RecordingOcr()
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(_parser(ocr=ocr, layout=layout, renderer=renderer))

    record, _units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert len(ocr.calls) == 1, ocr.calls
    assert "page 1" in ocr.calls[0]["reference"], ocr.calls
    assert ocr.calls[0]["png"] == GOLD_IMAGE_BYTES, record
    assert (renderer.calls, layout.calls) == ([], []), record


# ----- refusals name themselves and freeze nothing -----


def test_a_truncated_gold_png_is_a_named_source_refusal_never_ready() -> None:
    ocr = RecordingOcr()
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(_parser(ocr=ocr, layout=layout, renderer=renderer))

    failed, units = _prepare(
        service,
        thread_id,
        filename=GOLD_IMAGE_NAME,
        payload=GOLD_IMAGE_BYTES[: len(GOLD_IMAGE_BYTES) // 2],
    )

    assert MalformedSourceParseError.code == "malformed_source"
    assert failed.status == "failed", failed
    assert failed.failure_reason == MalformedSourceParseError.code, failed
    assert failed.failure_reason != ATTACHMENT_TYPE_UNSUPPORTED, failed
    assert units == (), units
    assert (ocr.calls, renderer.calls, layout.calls) == ([], [], []), failed


def test_bytes_without_a_png_or_jpeg_signature_are_a_type_refusal() -> None:
    ocr = RecordingOcr()
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(_parser(ocr=ocr, layout=layout, renderer=renderer))

    failed, units = _prepare(
        service, thread_id, filename="chart.png", payload=NOT_AN_IMAGE
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_TYPE_UNSUPPORTED, failed
    assert units == (), units
    assert (ocr.calls, renderer.calls, layout.calls) == ([], [], []), failed


def test_an_empty_image_answers_the_empty_door_before_any_reader() -> None:
    ocr = RecordingOcr()
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(_parser(ocr=ocr, layout=layout, renderer=renderer))

    failed, units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=b""
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed
    assert units == (), units
    assert (ocr.calls, renderer.calls, layout.calls) == ([], [], []), failed


def test_an_image_with_no_ocr_engine_fails_typed_instead_of_publishing_blank() -> None:
    """A bare image has no text layer, so its only reader is the engine."""
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(_parser(ocr=None, layout=layout, renderer=renderer))

    failed, units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == OcrRequiredUnavailableError.code, failed
    assert units == (), units
    assert (renderer.calls, layout.calls) == ([], []), failed


def test_an_image_whose_pixels_hold_no_readable_text_is_empty_not_ready() -> None:
    ocr = RecordingOcr(answer="   ")
    service, thread_id = _service(_parser(ocr=ocr))

    failed, units = _prepare(
        service, thread_id, filename=GOLD_IMAGE_NAME, payload=GOLD_IMAGE_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed
    assert units == (), units
    assert len(ocr.calls) == 1, ocr.calls
