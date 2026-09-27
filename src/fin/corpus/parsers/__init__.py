"""Seven-format document parsing: frozen-unit text + structured
metadata (T6 / SC6 / SC7 / SC10).

The public surface is :func:`parse_document`:

.. code-block:: python

    result = parse_document(
        path, fmt="xlsx", document_id="AAPL-2022Q3", version=sha256
    )
    for unit in result.units:
        unit.text                    # frozen text (normalization outlet)
        unit.locator                 # {document_id, version, unit}
        unit.source_sha256           # sha256 of the raw source bytes
        ...

Every unit carries the eight SC10 metadata fields (format, source
byte hash, locator triple, unit ordinal, char count, language, parse
rule version, params-table version); the construction point
``_common.build_unit`` raises the named
``ParseMetadataIncompleteError`` if any field would be missing — a
"field missing but still successful" unit is never emitted (SC10).

Granularity (plan T6 "已决"): PDF and PPT per physical page / slide;
Excel per consecutive row block within a worksheet; HTML / Markdown /
Word per structure block (heading split + deterministic cap merge);
plain text per blank-line paragraph. PDF extraction goes through the
shared page/region core (:class:`PageRegionParser`,
``fin.corpus.parsers._pages``) — one page/region authority for the
corpus and the thread-attachment lane; the per-format unit shape above
is unchanged.

Determinism (SC12, function level): the same source bytes and the
same rule version always produce equal ``ParseResult`` values — all
iteration is in document order, no clock / locale / randomness enters
the pipeline. The full-corpus sha256 assertion is T12's deliverable.

Versioning: any change to splitting, merging, or granularity constants
must bump :data:`PARSE_RULE_VERSION` (``parse-2``); the params-table
version is :data:`PARAMS_TABLE_VERSION` (``params-1``), owned by
``fin.corpus.params`` — the versioned (document type x section x
content density) lookup table landed by T9, consumed by the T11
splitter.

Named failures (SC9 / SC40 lineage; all in ``fin.corpus.errors``):
``EmptySourceParseError`` (0-byte source), ``UnsupportedFormatParseError``
(non-target format name), ``FormatMismatchParseError`` (bytes carry a
signature conflicting with the declared format — T8's non-target-MIME
shape), ``EncryptedSourceParseError`` (encrypted PDF / encrypted OOXML
package — T8), ``MalformedSourceParseError`` (corrupt container /
bad XML / undecodable text), ``NoUnitsParseError`` (zero non-empty
units), ``ParseMetadataIncompleteError`` (missing metadata field).
Each subclass carries a distinct stable ``code``. The T8 signature
gate classifies failures only — it never redirects parsing. No
failure shape escapes as a bare exception; no failure yields a
silent empty success.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import NoReturn

from fin.corpus.errors import (
    EmptySourceParseError,
    EncryptedSourceParseError,
    FormatMismatchParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.ocr import OCRError
from fin.corpus.parsers._common import (
    FAMILY_CFB,
    FAMILY_PDF,
    FAMILY_ZIP,
    PARAMS_TABLE_VERSION,
    PARSE_RULE_VERSION,
    PDF_MAGIC,
    SUPPORTED_FORMATS,
    ParseResult,
    ParsedUnit,
    UnitLocator,
    content_family,
    has_pdf_signature,
)
from fin.corpus.parsers._docx import parse_docx
from fin.corpus.parsers._html import parse_html
from fin.corpus.parsers._markdown import parse_markdown
from fin.corpus.parsers._pages import (
    MIN_OCR_REGION_EDGE_PX,
    PAGE_RENDER_DPI,
    REGION_KINDS,
    NullLayout,
    PageLayoutPort,
    PageParseResult,
    PageRaster,
    PageRegionParser,
    PageRenderPort,
    PageSourcePort,
    ParsedPage,
    PdfplumberPageSource,
    Pypdfium2PageRenderer,
    RasterOcrPort,
    Region,
)
from fin.corpus.parsers._pdf import (
    OcrParsedUnit,
    _is_encryption_failure,
)
from fin.corpus.parsers._pptx import parse_pptx
from fin.corpus.parsers._text import parse_plain_text
from fin.corpus.parsers._xlsx import parse_xlsx


def _family_accepts(fmt: str, family: str) -> bool:
    """Does the declared format accept this content-signature family?

    Only strong foreign signatures fail: ``pdf`` must not arrive as a
    zip / CFB / image, the OOXML trio must not arrive as a PDF or
    image, and the text formats must not arrive as any known binary.
    ``None`` (no signature — the shape of every plain-text source) is
    decided by the format parser, never by this gate.
    """
    if family == FAMILY_PDF:
        return fmt == "pdf"
    if family == FAMILY_ZIP:
        return fmt in ("docx", "xlsx", "pptx")
    # FAMILY_CFB / FAMILY_BINARY: no declared format accepts them here
    # (CFB + OOXML trio is re-classified as encrypted by the caller).
    return False


@functools.lru_cache(maxsize=1)
def _placeholder_raster() -> PageRaster:
    """A fixed 1 x 1 raster, built once, for the no-OCR corpus call.

    WHY it is safe: on the core, page pixels are consumed only through
    :meth:`PageRegionParser._recognize` (the page rule or a figure
    crop), and that method raises ``OcrRequiredUnavailableError`` the
    moment it is asked to work with ``ocr=None`` — before any png
    bytes are read. A corpus call with no OCR capability therefore
    never looks at a raster, and paying ~1 s/page of 200 dpi render +
    PNG encode for bytes that are discarded would be a pure regression
    against the retired ``parse_pdf`` path. With an OCR port wired,
    the adapter hands the core its real renderer instead.

    ``lru_cache`` replaces the old lazily-filled module global: the
    one build is memoized without any unlocked shared mutable state
    across the HTTP and worker threads (the cache itself is
    thread-safe; a racing miss may at worst rebuild a 1 x 1 PNG).
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return PageRaster.of(buffer.getvalue(), width_px=1, height_px=1)


class _PlaceholderPageRenderer:
    """``PageRenderPort`` answering every call with the fixed placeholder.

    The spelling of "no pixels will be read on this call" for the PDF
    dispatch; :func:`_placeholder_raster` states why the answer is
    never consumed.
    """

    def render_page(self, *, page_no: int) -> PageRaster:
        return _placeholder_raster()


def _encrypted_error(
    exc: BaseException, document_id: str
) -> EncryptedSourceParseError:
    """The one corpus spelling of "encrypted PDF, no credentials"."""
    return EncryptedSourceParseError(
        f"PDF source for {document_id!r} is encrypted and no "
        f"credentials were supplied: {exc}"
    )


def _classify_core_failure(
    exc: Exception, document_id: str
) -> NoReturn:
    """Map one core failure onto its corpus name and raise it.

    The core reports every open failure as malformed, while the corpus
    dispatch distinguishes encrypted from corrupt via the shared
    cause-chain classifier; named OCR outcomes and the core's own
    already-corpus-named ``ParseError``s are re-raised as themselves
    (same object, no re-wrapping, no bare failure escaping untyped).
    """
    if isinstance(exc, OCRError):
        # Named OCR outcomes reach the caller as themselves.
        raise exc
    if _is_encryption_failure(exc):
        # An encrypted PDF is structurally intact — it is named
        # encrypted, not corrupt, because the remediation is
        # credentials, not repair. The cause chain survives the core's
        # wrapping, so the shared classifier still sees pdfminer.
        raise _encrypted_error(exc, document_id) from exc
    if isinstance(exc, ParseError):
        raise exc
    raise MalformedSourceParseError(
        f"PDF source for {document_id!r} cannot be parsed: {exc}"
    ) from exc


def _freeze_page_unit(page: ParsedPage) -> ParsedUnit | None:
    """One core page -> its corpus unit (``None`` = contribute nothing).

    Native text (or an unscanned page) keeps the unit exactly as the
    core built it. A unit whose text came from the OCR lane is
    re-stamped as :class:`OcrParsedUnit` so ``ocr_applied`` reaches
    chunking and push exactly as it did on the retired ``parse_pdf``
    path.
    """
    if page.unit is None:
        return None
    if page.native_text.strip() or not page.scanned_text.strip():
        return page.unit
    # Unit text came from the OCR lane — carry the ocr_applied flag
    # the ingest write-back convention reads.
    return OcrParsedUnit.from_unit(page.unit)


def _parse_pdf_via_page_core(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
    max_units: int | None = None,
    ocr: RasterOcrPort | None = None,
    layout: PageLayoutPort | None = None,
    renderer: PageRenderPort | None = None,
) -> tuple[ParsedUnit, ...]:
    """PDF frozen units from the shared page/region core.

    One unit per physical page, built from the page's own frozen units
    (:meth:`PageParseResult.frozen_units`' selection, in page order);
    the per-page unit shape, including the OCR-lane re-stamp, lives in
    :func:`_freeze_page_unit`.

    Failure naming stays the corpus contract, not the core's — see
    :func:`_classify_core_failure`; ``max_units < 1`` keeps its typed
    ``NoUnitsParseError`` shape rather than the core's ``ValueError``;
    and a document with no frozen unit at all is the named
    ``NoUnitsParseError``, never an empty success.
    """
    if max_units is not None and max_units < 1:
        raise NoUnitsParseError(
            f"max_units must be >= 1 or None, got {max_units}"
        )
    if renderer is None and ocr is None:
        # No OCR capability on this call: no page's pixels can be read
        # (see _placeholder_raster), so the corpus path skips the
        # rasterization the old parse_pdf never paid for.
        renderer = _PlaceholderPageRenderer()
    parser = PageRegionParser(
        layout=layout if layout is not None else NullLayout(),
        renderer=renderer,
        ocr=ocr,
    )
    try:
        result = parser.extract_pdf(
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            max_pages=max_units,
        )
    except Exception as exc:
        _classify_core_failure(exc, document_id)

    units: list[ParsedUnit] = []
    for page in result.pages:
        unit = _freeze_page_unit(page)
        if unit is not None:
            units.append(unit)
    if not units:
        raise NoUnitsParseError(
            f"PDF source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


def parse_document(
    source: Path | bytes,
    *,
    fmt: str,
    document_id: str,
    version: str,
    max_units: int | None = None,
    ocr: RasterOcrPort | None = None,
    layout: PageLayoutPort | None = None,
    renderer: PageRenderPort | None = None,
) -> ParseResult:
    """Parse one document into frozen units with structured metadata.

    Parameters
    ----------
    source:
        A filesystem path or the raw source bytes.
    fmt:
        One of :data:`SUPPORTED_FORMATS` (``pdf`` / ``docx`` /
        ``xlsx`` / ``pptx`` / ``html`` / ``md`` / ``txt``). The format
        is named by the caller — there is no content sniffing.
    document_id:
        Locator field 1. Must be non-empty (named failure otherwise).
    version:
        Locator field 2 — the frozen source version (the manifest
        sha256 lineage). Must be non-empty (named failure otherwise).
    max_units:
        Optional deterministic cap: parse only the first ``max_units``
        units of work (for PDF: the first ``max_units`` physical
        pages). Used by bounded contexts (the default pytest suite) to
        cap work on the frozen 10-Qs; ``None`` (default) parses
        everything. Only PDF honors it today; the other formats are
        already whole-document passes over in-memory bytes.
    ocr:
        PDF-only. Optional :class:`RasterOcrPort` for the scanned-page
        lane of the shared page/region core: a page whose text layer
        collapsed is frozen from its pixels. ``None`` (default) is no
        OCR capability — such a page then ends the parse with the named
        ``OcrRequiredUnavailableError``, the same outcome shape the
        retired ``parse_pdf`` produced without an engine. Ignored by
        the other formats.
    layout:
        PDF-only. Optional region port for the core; ``None`` (default)
        is :class:`NullLayout` — the corpus path records no regions and
        runs no figure crops, keeping the frozen units page-level.
    renderer:
        PDF-only. Optional render port handed to the core; ``None``
        (default) lets the core build and release its own pypdfium2
        rasterizer per call. Tests inject a fake to stay fast/hermetic.
        These three ports are the corpus OCR seam, wired by a later
        ticket; no production caller passes them yet.

    Returns
    -------
    ParseResult
        Document identity plus the ordered tuple of frozen units.

    Raises
    ------
    EmptySourceParseError
        The source bytes are empty.
    UnsupportedFormatParseError
        ``fmt`` is not one of the seven supported formats.
    FormatMismatchParseError
        The bytes carry a content signature that conflicts with the
        declared ``fmt`` (T8: non-target MIME given the explicit-name
        API).
    EncryptedSourceParseError
        The source is encrypted (PDF standard security handler, or an
        ECMA-376 encrypted OOXML package in a CFB container) and no
        credentials were supplied.
    MalformedSourceParseError
        The source cannot be parsed as its claimed format.
    NoUnitsParseError
        The parse produced zero non-empty frozen units.
    ParseMetadataIncompleteError
        Any of the eight metadata fields would be missing.
    """
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatParseError(
            f"unsupported format {fmt!r}; supported: "
            f"{', '.join(SUPPORTED_FORMATS)}"
        )
    source_bytes = (
        source.read_bytes() if isinstance(source, Path) else source
    )
    if not source_bytes:
        raise EmptySourceParseError(
            f"source for {document_id!r} is empty (0 bytes)"
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    # T8 signature gate — failure classification only. A known magic
    # that cannot belong to the declared format is a named mismatch
    # (SC9's non-target-MIME shape under the explicit-name API); an
    # encrypted OOXML package (CFB container) is named encrypted. No
    # signature never fails, and dispatch below is untouched.
    family = content_family(source_bytes)
    if family is not None and not _family_accepts(fmt, family):
        if family == FAMILY_CFB and fmt in ("docx", "xlsx", "pptx"):
            raise EncryptedSourceParseError(
                f"source for {document_id!r} is an encrypted OOXML "
                f"package (Compound File Binary container, declared "
                f"format {fmt!r}) and no credentials were supplied"
            )
        raise FormatMismatchParseError(
            f"source for {document_id!r} carries a {family!r} content "
            f"signature but was declared as {fmt!r}"
        )

    parser_kwargs = {
        "source_bytes": source_bytes,
        "source_sha256": source_sha256,
        "document_id": document_id,
        "version": version,
    }
    if fmt == "pdf":
        units = _parse_pdf_via_page_core(
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            max_units=max_units,
            ocr=ocr,
            layout=layout,
            renderer=renderer,
        )
    elif fmt == "docx":
        units = parse_docx(**parser_kwargs)
    elif fmt == "xlsx":
        units = parse_xlsx(**parser_kwargs)
    elif fmt == "pptx":
        units = parse_pptx(**parser_kwargs)
    elif fmt == "html":
        units = parse_html(**parser_kwargs)
    elif fmt == "md":
        units = parse_markdown(**parser_kwargs)
    else:
        units = parse_plain_text(**parser_kwargs)

    return ParseResult(
        fmt=fmt,
        source_sha256=source_sha256,
        document_id=document_id,
        version=version,
        parse_rule_version=PARSE_RULE_VERSION,
        params_table_version=PARAMS_TABLE_VERSION,
        units=units,
    )


__all__ = [
    "MIN_OCR_REGION_EDGE_PX",
    "PAGE_RENDER_DPI",
    "PARAMS_TABLE_VERSION",
    "PARSE_RULE_VERSION",
    "PDF_MAGIC",
    "REGION_KINDS",
    "SUPPORTED_FORMATS",
    "NullLayout",
    "PageLayoutPort",
    "PageParseResult",
    "PageRaster",
    "PageRegionParser",
    "PageRenderPort",
    "PageSourcePort",
    "ParseResult",
    "ParsedPage",
    "ParsedUnit",
    "PdfplumberPageSource",
    "Pypdfium2PageRenderer",
    "RasterOcrPort",
    "Region",
    "UnitLocator",
    "has_pdf_signature",
    "parse_document",
]
