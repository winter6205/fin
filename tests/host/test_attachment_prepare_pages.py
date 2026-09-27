"""The preparation ADAPTER's failure vocabulary (ADR 0089).

The shared page/region core returns its own outcomes; only this adapter
turns them into the attachment codes the door publishes. Every case here
drives the real core over the committed gold bytes, with fake ports for
the pieces whose boundaries are not the adapter's (rasterization, the OCR
engine), so the suite stays hermetic: no layout container, no OCR binary,
no local VLM, no provider.

Expectations come from ``data/corpus/g4_attachment_gold/manifest.json``
(quotes read with poppler and tesseract outside fin) and from
``specs/thread-attachment-source-read.md`` (the code list), never from the
implementation under test.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import textwrap
import zipfile
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from fin.corpus.errors import (
    EmptySourceParseError,
    EncryptedSourceParseError,
    FormatMismatchParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
    PageLayoutError,
    PageRenderError,
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.ocr import OCRError, OcrRequiredUnavailableError
from fin.corpus.parsers import (
    PARSE_RULE_VERSION,
    PARAMS_TABLE_VERSION,
    PDF_MAGIC,
    NullLayout,
    PageParseResult,
    PageRaster,
    PageRegionParser,
    Region,
)
from fin.host.attachment_budgets import (
    ATTACHMENT_PAGE_LIMIT_EXCEEDED,
    AttachmentBudgetConfigError,
    AttachmentBudgets,
    required_run_calls,
)
from fin.host.attachment_prepare import (
    ATTACHMENT_EMPTY_TEXT,
    ATTACHMENT_INDEX_UNAVAILABLE,
    ATTACHMENT_NOT_TEXT,
    ATTACHMENT_PAGE_LANE_UNWIRED,
    ATTACHMENT_TYPE_UNSUPPORTED,
    AttachmentEmptyTextError,
    AttachmentPageLaneUnwiredError,
    AttachmentPreparationError,
    AttachmentPreparationService,
    AttachmentRenderFailedError,
    AttachmentSourceRejectedError,
    AttachmentTypeUnsupportedError,
    RENDER_FAILED,
    SOURCE_UNAVAILABLE,
    corpus_failure_to_attachment_error,
)
from fin.host.attachment_store import AttachmentRecord, InMemoryAttachmentStore
from fin.host.thread_store import InMemoryThreadStore

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
MIXED_PDF = GOLD_DIR / "g4-mixed-chart-001.pdf"
SCANNED_PDF = GOLD_DIR / "g4-scanned-001.pdf"
CHART_PNG = GOLD_DIR / "g4-image-chart-001.png"

MIXED_BYTES = MIXED_PDF.read_bytes()
SCANNED_BYTES = SCANNED_PDF.read_bytes()
CHART_BYTES = CHART_PNG.read_bytes()

# Independent quotes, taken from the gold manifest: poppler read mixed page
# 2, tesseract read the standalone chart raster. Neither came from fin.
MIXED_PAGE2_QUOTE = "Total net sales 81,797 82,959 293,787 304,182"
CHART_PAGE_QUOTE = "Total net sales by fiscal quarter"

# The mixed fixture is 3 physical pages, so a cap of 2 must refuse it whole.
MIXED_PAGE_COUNT = 3

_PNG_1PX = io.BytesIO()
Image.new("RGB", (1, 1)).save(_PNG_1PX, format="PNG")
RASTER_PNG = _PNG_1PX.getvalue()


def _ooxml_package_bytes() -> bytes:
    r"""A minimal real OOXML zip package — the shape of DOCX bytes.

    Used to name an upload ``.pdf``: the container magic (``PK\x03\x04``) is
    built by ``zipfile``, not written by hand, so the claim is refused
    against a genuine foreign header rather than a stand-in.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types/>',
        )
        package.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


DOCX_BYTES = _ooxml_package_bytes()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ----- fake ports: this adapter's own witnesses -----


class RecordingRenderer:
    """``PageRenderPort`` recording which pages were asked for pixels."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[int] = []
        self._fail_with = fail_with

    def render_page(self, *, page_no: int) -> PageRaster:
        self.calls.append(page_no)
        if self._fail_with is not None:
            raise self._fail_with
        return PageRaster.of(RASTER_PNG, width_px=1, height_px=1)


class RecordingLayout:
    """``PageLayoutPort`` recording calls, able to fail on demand."""

    def __init__(
        self,
        *,
        by_page: dict[int, tuple[Region, ...]] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.calls: list[int] = []
        self._by_page = by_page or {}
        self._fail_with = fail_with

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        self.calls.append(page_no)
        if self._fail_with is not None:
            raise self._fail_with
        return self._by_page.get(page_no, ())


class RecordingOcr:
    """``RasterOcrPort`` answering with the gold manifest's own quote."""

    def __init__(self, *, answer: str = CHART_PAGE_QUOTE) -> None:
        self.references: list[str] = []
        self._answer = answer

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        self.references.append(reference)
        return self._answer


class BlankResultParser(PageRegionParser):
    """The core's entry, answering a document that froze no unit at all.

    Stands in for a PDF of blank pages, which the committed gold set does
    not carry; the mapping under test is the adapter's, not the core's.
    """

    def extract_pdf(self, **_kwargs: Any) -> PageParseResult:
        return PageParseResult(
            fmt="pdf",
            source_sha256="0" * 64,
            document_id="a",
            version="1",
            parse_rule_version=PARSE_RULE_VERSION,
            params_table_version=PARAMS_TABLE_VERSION,
            pages=(),
            source_page_count=0,
            truncated=False,
        )


def _parser(
    *,
    layout: Any = None,
    renderer: Any = None,
    ocr: Any = None,
) -> PageRegionParser:
    return PageRegionParser(
        layout=layout if layout is not None else NullLayout(),
        renderer=renderer if renderer is not None else RecordingRenderer(),
        ocr=ocr,
    )


# ----- fixtures -----


_CROP_CALLS = 8


def _budgets(*, max_pages: int = 8) -> AttachmentBudgets:
    # The run budget is derived through the module that prices a run, so this
    # fixture cannot drift from the page + crop + retry-reserve relationship.
    run_calls = required_run_calls(
        AttachmentBudgets(
            max_upload_bytes=1_048_576,
            max_pdf_pages=max_pages,
            # Only the page and crop counts price a run, so this total is what
            # the call returns, never an input to it.
            max_vision_calls_per_run=max_pages + _CROP_CALLS,
            max_vision_crop_calls_per_run=_CROP_CALLS,
        )
    )
    return AttachmentBudgets(
        max_upload_bytes=1_048_576,
        max_pdf_pages=max_pages,
        max_vision_calls_per_run=run_calls,
        max_vision_crop_calls_per_run=_CROP_CALLS,
    )


def _service(
    *,
    budgets: AttachmentBudgets | None,
    parser: PageRegionParser,
    store: InMemoryAttachmentStore | None = None,
    thread_id: str | None = None,
    objects: InMemoryObjectStore | None = None,
) -> tuple[AttachmentPreparationService, str]:
    if store is None or thread_id is None:
        store, thread_id = _store_and_thread()
    service = AttachmentPreparationService(
        store=store,
        objects=objects if objects is not None else InMemoryObjectStore(),
        budgets=budgets,
        pages=parser,
    )
    return service, thread_id


def _store_and_thread() -> tuple[InMemoryAttachmentStore, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="adapter thread")
    return InMemoryAttachmentStore(threads), thread.id


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


# ----- the published vocabulary -----


def test_the_adapter_publishes_the_codes_the_spec_names() -> None:
    """These strings are the contract; renaming one is a product change."""
    assert ATTACHMENT_TYPE_UNSUPPORTED == "attachment_type_unsupported"
    assert ATTACHMENT_EMPTY_TEXT == "attachment_empty_text"
    assert ATTACHMENT_NOT_TEXT == "attachment_not_text"
    assert RENDER_FAILED == "render_failed"
    assert ATTACHMENT_PAGE_LIMIT_EXCEEDED == "attachment_page_limit_exceeded"
    assert ATTACHMENT_INDEX_UNAVAILABLE == "attachment_index_unavailable"
    assert SOURCE_UNAVAILABLE == "source_unavailable"
    assert ATTACHMENT_PAGE_LANE_UNWIRED == "attachment_page_lane_unwired"


def test_no_page_lane_is_named_apart_from_a_document_of_no_text() -> None:
    """One code per fact, so an operator can tell the two deployment states
    and the two document states apart from the stored reason alone."""
    assert len({ATTACHMENT_PAGE_LANE_UNWIRED, ATTACHMENT_NOT_TEXT,
                ATTACHMENT_EMPTY_TEXT, ATTACHMENT_TYPE_UNSUPPORTED}) == 4


def test_the_unwired_lane_is_a_caught_preparation_exit_not_a_config_error() -> None:
    """The unwired seam is this door's own named answer.

    It is not a violated bound — a text-only service is a legal
    construction — so it declares a code on the preparation ladder and
    cannot be swept up by a caller that handles misconfigured budgets.
    """
    assert issubclass(AttachmentPageLaneUnwiredError, AttachmentPreparationError)
    assert not issubclass(AttachmentPageLaneUnwiredError, AttachmentBudgetConfigError)
    assert AttachmentPageLaneUnwiredError("a", "no lane").code == (
        ATTACHMENT_PAGE_LANE_UNWIRED
    )


_CORE_ENTRIES = ("extract_pdf", "extract_image")


def _core_keyword_parameters(entry: str) -> set[str]:
    """The core's own required keyword names for one lane entry.

    Read off the signature rather than written twice: this pins the
    boundary, not the formatting.
    """
    return {
        name
        for name, param in inspect.signature(
            getattr(PageRegionParser, entry)
        ).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY and name != "max_pages"
    }


def _extract_lane_calls() -> list[ast.Call]:
    """Every call to a core lane entry inside the adapter's ``_extract``."""
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(AttachmentPreparationService._extract))
    )
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CORE_ENTRIES
    ]


def test_the_adapter_calls_only_the_cores_lane_entries() -> None:
    """The door reaches the core through its two published entries."""
    calls = _extract_lane_calls()
    assert len(calls) == len(_CORE_ENTRIES)
    assert {call.func.attr for call in calls} == set(_CORE_ENTRIES)


def _call_for_lane(entry: str) -> ast.Call:
    """The single core lane call for ``entry`` inside the adapter."""
    lane_calls = [call for call in _extract_lane_calls() if call.func.attr == entry]
    assert len(lane_calls) == 1, f"one call per lane, {entry}"
    (call,) = lane_calls
    return call


@pytest.mark.parametrize("entry", _CORE_ENTRIES)
def test_the_core_call_site_names_the_cores_own_parameters(entry: str) -> None:
    """The adapter hands the shared core its arguments by their real names.

    A dict splat keyed by those names runs the same today and breaks the door
    in production the day the core renames a parameter, so the expected names
    are read off the core's own signature rather than written twice.
    """
    call = _call_for_lane(entry)
    splatted = [keyword.arg for keyword in call.keywords if keyword.arg is None]
    assert splatted == [], "the core is called with named arguments only"
    passed = {keyword.arg for keyword in call.keywords}
    assert passed == _core_keyword_parameters(entry) == {
        "source_bytes",
        "source_sha256",
        "document_id",
        "version",
    }


def test_a_source_rejection_cannot_be_constructed_without_a_code() -> None:
    """``code`` has no default: forgetting it fails at the raise site.

    Both halves are pinned — the argument is required, and the class
    declares no fallback that a forgotten argument would silently publish
    as an empty reason on the row.
    """
    assert "code" not in vars(AttachmentSourceRejectedError)
    with pytest.raises(TypeError):
        AttachmentSourceRejectedError("a")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "core_code",
    [
        MalformedSourceParseError.code,
        OcrRequiredUnavailableError.code,
        PageLayoutError.code,
    ],
)
def test_a_shape_with_no_attachment_code_keeps_the_readers_own_name(
    core_code: str,
) -> None:
    """One failure shape must never own two strings."""
    assert AttachmentSourceRejectedError("a", core_code).code == core_code


# ----- the refusal ladder as one pure table -----


@pytest.mark.parametrize(
    ("exc", "refusal", "code"),
    [
        (PageRenderError("no pixels"), AttachmentRenderFailedError, RENDER_FAILED),
        (
            UnsupportedFormatParseError("no reader here"),
            AttachmentTypeUnsupportedError,
            ATTACHMENT_TYPE_UNSUPPORTED,
        ),
        (
            FormatMismatchParseError("not the claimed type"),
            AttachmentTypeUnsupportedError,
            ATTACHMENT_TYPE_UNSUPPORTED,
        ),
        (
            EmptySourceParseError("nothing in the source"),
            AttachmentEmptyTextError,
            ATTACHMENT_EMPTY_TEXT,
        ),
        (
            NoUnitsParseError("no text on any page"),
            AttachmentEmptyTextError,
            ATTACHMENT_EMPTY_TEXT,
        ),
        (
            OcrRequiredUnavailableError("no engine"),
            AttachmentSourceRejectedError,
            OcrRequiredUnavailableError.code,
        ),
        (
            EncryptedSourceParseError("locked"),
            AttachmentSourceRejectedError,
            "encrypted_source",
        ),
        (
            PageLayoutError("boxes are a lie"),
            AttachmentSourceRejectedError,
            PageLayoutError.code,
        ),
        (
            MalformedSourceParseError("container died"),
            AttachmentSourceRejectedError,
            "malformed_source",
        ),
    ],
)
def test_the_ladder_maps_a_refusal_without_running_anything(
    exc: ParseError | OCRError,
    refusal: type[AttachmentPreparationError],
    code: str,
) -> None:
    """The ladder is total over the reader's refusals and pure: it returns an
    attachment error carrying the expected type and code, and never raises or
    judges a budget on the way.

    The door type is pinned next to the string because one value is shared by
    both sides — ``render_failed`` is the core's own code and this door's — so
    a ladder that let it fall through to the generic refusal would publish the
    right reason and still lose the distinction this adapter exists to make.
    """
    mapped = corpus_failure_to_attachment_error(exc, "a")
    assert isinstance(mapped, AttachmentPreparationError)
    assert type(mapped) is refusal
    assert mapped.code == code
    assert mapped.attachment_id == "a"



# ----- the page lane reaching ready -----


def test_a_native_text_pdf_becomes_one_unit_per_physical_page() -> None:
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(layout=layout, renderer=renderer),
    )

    ready = _prepare(
        service, thread_id, filename="10q.pdf", payload=MIXED_BYTES
    )

    assert ready.status == "ready", ready
    assert ready.unit_count == MIXED_PAGE_COUNT, ready
    units = service.units(ready.attachment_id)
    # Ordinals address physical pages, so a page never renumbers a cite.
    assert [unit.unit for unit in units] == [1, 2, 3]
    assert MIXED_PAGE2_QUOTE in _flat(units[1].text)
    assert renderer.calls == [1, 2, 3]
    assert layout.calls == [1, 2, 3]


def test_a_standalone_image_becomes_one_unit_from_its_own_pixels() -> None:
    ocr = RecordingOcr()
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(ocr=ocr),
    )

    ready = _prepare(
        service, thread_id, filename="chart.png", payload=CHART_BYTES
    )

    assert ready.status == "ready", ready
    units = service.units(ready.attachment_id)
    assert [unit.unit for unit in units] == [1]
    assert _flat(units[0].text) == CHART_PAGE_QUOTE
    assert ocr.references, "an image's pixels must be read, not skipped"


# ----- page overflow: named before a single page is parsed -----


def test_page_overflow_is_named_before_any_page_is_parsed() -> None:
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    ocr = RecordingOcr()
    service, thread_id = _service(
        budgets=_budgets(max_pages=MIXED_PAGE_COUNT - 1),
        parser=_parser(layout=layout, renderer=renderer, ocr=ocr),
    )

    failed = _prepare(
        service, thread_id, filename="10q.pdf", payload=MIXED_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_PAGE_LIMIT_EXCEEDED, failed
    # The ordering claim: the cap is decided from the page COUNT, so nothing
    # was rasterized, boxed, or scanned before the refusal.
    assert (renderer.calls, layout.calls, ocr.references) == ([], [], [])
    assert service.units(failed.attachment_id) == ()


def test_the_declared_page_count_of_one_page_is_not_an_overflow() -> None:
    service, thread_id = _service(
        budgets=_budgets(max_pages=1),
        parser=_parser(ocr=RecordingOcr()),
    )

    ready = _prepare(
        service, thread_id, filename="chart.png", payload=CHART_BYTES
    )

    assert ready.status == "ready", ready


def test_a_page_document_is_named_when_no_page_cap_is_deployed() -> None:
    """A missing bound is never a guessed default, and never a 500 either.

    The service answers with its own code, the row records it, and the page
    is not opened on the way to that answer.
    """
    renderer = RecordingRenderer()
    service, thread_id = _service(
        budgets=None,
        parser=_parser(layout=RecordingLayout(), renderer=renderer),
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_PAGE_LANE_UNWIRED, failed
    assert renderer.calls == []
    assert service.units(failed.attachment_id) == ()


def test_a_page_document_is_named_when_no_extraction_port_is_wired() -> None:
    """The other half of the seam: a deployed cap with no ports is still no
    page lane, and is answered the same way rather than by a missing port."""
    store, thread_id = _store_and_thread()
    service = AttachmentPreparationService(
        store=store,
        objects=InMemoryObjectStore(),
        budgets=_budgets(),
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=MIXED_BYTES)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_PAGE_LANE_UNWIRED, failed


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        # No bytes is empty whatever the deployment can read, and naming it
        # that way is more useful than a deployment fact nobody was asked.
        (b"", ATTACHMENT_EMPTY_TEXT),
        # A lying name is not this door's to judge: no document claim is read
        # at all until a lane exists to read it with.
        (DOCX_BYTES, ATTACHMENT_PAGE_LANE_UNWIRED),
        (b"not a pdf at all", ATTACHMENT_PAGE_LANE_UNWIRED),
    ],
)
def test_an_undeployed_lane_is_named_before_anything_about_the_document(
    payload: bytes, reason: str
) -> None:
    service, thread_id = _service(budgets=None, parser=_parser())

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=payload)

    assert failed.failure_reason == reason, failed


# ----- unsupported input -----


def test_input_the_core_refuses_is_attachment_type_unsupported() -> None:
    """A declared image carrying no supported signature is not a format guess.

    The image lane reads its format from the bytes, so a GIF named ``.png``
    is the core's own format refusal and this adapter's unsupported type.
    """
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(ocr=RecordingOcr())
    )

    failed = _prepare(
        service, thread_id, filename="chart.png", payload=b"GIF89a..truncated.."
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_TYPE_UNSUPPORTED, failed


@pytest.mark.parametrize(
    "payload",
    [
        # A DOCX package wearing a ``.pdf`` name: the bytes claim a
        # different container family entirely.
        DOCX_BYTES,
        # Bytes no container rule can read.
        b"not a pdf at all",
        # The marker exists but past the leading window both the door and
        # the core search, so it is not a PDF claim.
        b"x" * 1024 + PDF_MAGIC + b"1.4 broken body",
    ],
)
def test_a_pdf_name_without_the_signature_is_a_type_refusal_not_a_parse_failure(
    payload: bytes,
) -> None:
    """The name is checked against the bytes before a page is opened.

    A headerless ``.pdf`` must not reach the reader: opening it would report
    the unreadable container as ``malformed_source`` and blame the document
    for a claim its name never earned. The refusal is a typed pre-parse
    rejection, so no port is spent and no page is counted on it.
    """
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    ocr = RecordingOcr()
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(layout=layout, renderer=renderer, ocr=ocr),
    )

    failed = _prepare(
        service, thread_id, filename="mislabeled.pdf", payload=payload
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_TYPE_UNSUPPORTED, failed
    assert (renderer.calls, layout.calls, ocr.references) == ([], [], [])
    assert service.units(failed.attachment_id) == ()


# ----- empty extracted text -----


def test_a_source_with_no_bytes_yields_the_empty_text_exit() -> None:
    renderer = RecordingRenderer()
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(layout=RecordingLayout(), renderer=renderer),
    )

    failed = _prepare(service, thread_id, filename="10q.pdf", payload=b"")

    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed
    assert renderer.calls == []


def test_a_pdf_that_freezes_no_unit_at_all_is_empty_text_not_ready() -> None:
    service, thread_id = _service(
        budgets=_budgets(),
        parser=BlankResultParser(layout=NullLayout()),
    )

    failed = _prepare(
        service, thread_id, filename="blank.pdf", payload=MIXED_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed


def test_bytes_no_lane_can_read_keep_their_existing_name() -> None:
    """A binary that is neither text nor a document stays ``not_text``.

    Preparation has rejected non-UTF-8 bytes this way since the durable
    upload landed; the page lanes add codes without taking this one away.
    """
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(ocr=RecordingOcr())
    )

    failed = _prepare(
        service, thread_id, filename="mystery.bin", payload=b"\xff\xfe\x00\x01"
    )

    assert failed.failure_reason == ATTACHMENT_NOT_TEXT, failed


# ----- render, OCR, layout, and malformed-parser failures -----


def test_a_page_that_yields_no_pixels_fails_as_render_failed() -> None:
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(
            layout=RecordingLayout(),
            renderer=RecordingRenderer(
                fail_with=PageRenderError("page 1 could not be rendered")
            ),
        ),
    )

    failed = _prepare(
        service, thread_id, filename="10q.pdf", payload=MIXED_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == RENDER_FAILED, failed
    assert service.units(failed.attachment_id) == ()


def test_a_scanned_page_without_an_ocr_engine_is_a_typed_exit() -> None:
    """A page that needed OCR is never published blank over an engine gap."""
    renderer = RecordingRenderer()
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(layout=RecordingLayout(), renderer=renderer),
    )

    failed = _prepare(
        service, thread_id, filename="scan.pdf", payload=SCANNED_BYTES
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == OcrRequiredUnavailableError.code, failed
    assert renderer.calls == [1]


def test_a_broken_layout_claim_terminates_the_parse_typed() -> None:
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(
            layout=RecordingLayout(fail_with=RuntimeError("container died"))
        ),
    )

    failed = _prepare(
        service, thread_id, filename="10q.pdf", payload=MIXED_BYTES
    )

    assert failed.failure_reason == PageLayoutError.code, failed


def test_a_container_that_cannot_be_opened_names_the_source() -> None:
    """A header at offset 0 whose body is a lie is the reader's to name."""
    service, thread_id = _service(
        budgets=_budgets(), parser=_parser(ocr=RecordingOcr())
    )

    failed = _prepare(
        service,
        thread_id,
        filename="broken.pdf",
        payload=b"%PDF-1.4\nthis xref table is a rumour\n",
    )

    assert failed.status == "failed", failed
    assert failed.failure_reason == MalformedSourceParseError.code, failed


def test_a_marker_inside_the_window_with_an_unreadable_body_blames_the_body() -> None:
    """The window tolerance is a signature rule, not a license to invent a
    format refusal for a document that really did claim to be a PDF.

    A leading envelope line before ``%PDF-`` is spec-legal, so the door's
    signature check passes and the reader gets the bytes; a body it cannot
    open is named by the reader, never as ``attachment_type_unsupported``.
    """
    renderer = RecordingRenderer()
    layout = RecordingLayout()
    ocr = RecordingOcr()
    payload = b"envelope\n" + PDF_MAGIC + b"1.4\ntrailer? never heard of it\n"
    service, thread_id = _service(
        budgets=_budgets(),
        parser=_parser(layout=layout, renderer=renderer, ocr=ocr),
    )

    failed = _prepare(
        service, thread_id, filename="late-marker.pdf", payload=payload
    )

    assert failed.status == "failed", failed
    assert MalformedSourceParseError.code == "malformed_source"
    assert failed.failure_reason == MalformedSourceParseError.code, failed
    assert failed.failure_reason != ATTACHMENT_TYPE_UNSUPPORTED, failed
    assert (renderer.calls, layout.calls, ocr.references) == ([], [], [])


# ----- a refusal stays a retryable row, not a lost attachment -----


def test_a_refused_page_lane_keeps_its_bytes_and_retries_to_ready() -> None:
    store, thread_id = _store_and_thread()
    objects = InMemoryObjectStore()
    accepted = AttachmentPreparationService(
        store=store, objects=objects
    ).accept(thread_id=thread_id, filename="10q.pdf", payload=MIXED_BYTES)
    assert accepted.status == "uploaded", accepted

    narrow = _service(
        budgets=_budgets(max_pages=1),
        parser=_parser(),
        store=store,
        thread_id=thread_id,
        objects=objects,
    )[0]
    refused = narrow.prepare(accepted.attachment_id)
    assert refused.failure_reason == ATTACHMENT_PAGE_LIMIT_EXCEEDED, refused

    wide = _service(
        budgets=_budgets(max_pages=8),
        parser=_parser(ocr=RecordingOcr()),
        store=store,
        thread_id=thread_id,
        objects=objects,
    )[0]
    retried = wide.prepare(accepted.attachment_id)
    assert retried.status == "ready", retried
    assert retried.failure_reason is None, retried
    assert len(wide.units(accepted.attachment_id)) == MIXED_PAGE_COUNT
