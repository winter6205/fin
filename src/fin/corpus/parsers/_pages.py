"""Physical-page and region extraction shared by every document consumer.

This is the core the thread-attachment preparation adapter and the
permanent-corpus upload path both consume. It turns source bytes into one
frozen text unit per physical page, plus that page's geometry, its region
metadata, and its rendered raster — and nothing else. It returns values
only: no store, no index, no push, no vision provider, no thread
lifecycle, no budget decision. A page or call cap belongs to the caller
that owns budgets (``fin.host.attachment_budgets`` preflights an
attachment run before any page here is scanned).

Three capabilities are injected as ports, so this module stays free of a
container, an external binary, and an optional dependency:

* :class:`PageSourcePort` — page count, media box, text layer, image
  count. The default :class:`PdfplumberPageSource` is the same text-layer
  stack ``parse_pdf`` uses, so fin keeps one native-text authority.
* :class:`PageLayoutPort` — region kinds and coordinates for one page.
  Production wires the pinned Docling/Heron pass; nothing here imports it.
* :class:`PageRenderPort` / :class:`RasterOcrPort` — pixels in, page
  raster and recognized text out.

Two rules answer two different questions about what OCR runs:

* the *page* rule (:func:`fin.corpus.ocr.detect_ocr_required`) asks
  whether the text layer collapsed while the page still carries images.
  It stays page-level because the measured layout recall is far too low to
  carry a scan decision (one figure box across 72 pages of a real filing),
  so a region-shaped rule would silently drop whole scanned pages.
* the *figure* rule (:func:`crop_is_legible`) asks whether one region is
  a figure whose box is big enough to hold text drawn as pixels, which is
  how text inside a chart on a page that also has a text layer is reached
  at all. It is the same rule the host's crop inspection lane reuses, so a
  box judged unreliable here is never cropped there.

The questions are independent; the runs are not: once the page rule has
had the whole raster read, the figure crops are skipped so the same
bitmap is never recognized twice for nothing added.

Region text is additive metadata. A page's own unit keeps its native text
whenever it has any, so reading a page's pixels never discards the layer a
citation was quoted from.

A page the reader could not describe is reported, not fatal. An empty OCR
read is data about that one page, while an OCR engine that is absent or that
failed says nothing about any page and so ends the parse with a typed
failure; collapsing the two would let one unreadable blank page cost the
caller every page in the document.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, replace
from typing import Final, Protocol

from fin.corpus.errors import (
    EmptySourceParseError,
    MalformedSourceParseError,
    PageLayoutError,
    PageRenderError,
    UnsupportedFormatParseError,
)
from fin.corpus.ocr import (
    PAGE_COLLAPSE_TEXT_CHARS,
    OCRError,
    OcrRequiredUnavailableError,
    detect_ocr_required,
)
from fin.corpus.parsers._common import (
    CONTENT_SIGNATURES,
    PARAMS_TABLE_VERSION,
    PARSE_RULE_VERSION,
    ParsedUnit,
    build_unit,
    freeze_text,
    has_pdf_signature,
)

# Render scale of the page raster every page is kept at. 200 dpi is the
# scale the pinned pipeline measured at and the scale the figure-crop floor
# below is expressed against; a 150 dpi scan is still readable here, and a
# dedicated-device re-reading of this number belongs to the run that prices
# shipped defaults, not to this module.
PAGE_RENDER_DPI: Final[int] = 200

# Shorter-edge floor, in raster pixels, below which a region's pixels are
# not worth a crop call. At 200 dpi 160 px is 57 pt — too small to hold a
# label line, which makes a box under it a mark or a rule rather than
# text-bearing artwork. Measured against the committed gold set: the
# 33 x 40 pt logotype on page 1 of ``g4-mixed-chart-001.pdf`` (91 px) is
# rejected, the 528 x 328 pt chart on its page 3 (911 px) is accepted.
MIN_OCR_REGION_EDGE_PX: Final[int] = 160

REGION_KINDS: Final[tuple[str, ...]] = ("text", "table", "figure")


# ---------------------------------------------------------------------------
# published value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Region:
    """One layout region of one physical page, in PDF points.

    Coordinates are top-left origin (``top_pt`` measures from the top of
    the media box), matching both ``pdfplumber`` and Docling's ``bbox`` so
    a box scales straight onto the page raster.

    ``text`` and ``ocr_applied`` record what the figure rule did with the
    box: a region the rule passed over keeps both unset, and one it read and
    got nothing from keeps its text empty but still records the read. A
    layout port returns boxes only.
    """

    kind: str
    left_pt: float
    top_pt: float
    right_pt: float
    bottom_pt: float
    text: str = ""
    ocr_applied: bool = False

    @property
    def size_pt(self) -> tuple[float, float]:
        return (self.right_pt - self.left_pt, self.bottom_pt - self.top_pt)


@dataclass(frozen=True, slots=True)
class PageRaster:
    """One physical page rendered to pixels, plus its own identity.

    The bytes are the PNG a vision call or a region crop is made from; the
    hash is over exactly those bytes, so a stored observation can name the
    raster it came from after the pixels themselves are gone.
    """

    png: bytes
    width_px: int
    height_px: int
    sha256: str

    @staticmethod
    def of(png: bytes, *, width_px: int, height_px: int) -> "PageRaster":
        return PageRaster(
            png=png,
            width_px=width_px,
            height_px=height_px,
            sha256=hashlib.sha256(png).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """Everything one physical page yielded: its unit and its evidence.

    ``unit`` is ``None`` only for a page that holds no text at all after
    the page rule ran and nothing was recognized — a genuinely blank page,
    or a page whose pixels an engine did read came back empty from. Neither
    can carry a frozen unit (the single construction point refuses empty
    text), and neither is a reason to lose the other pages. A page that
    needed a read and could not be *asked* — no engine, or an engine that
    failed — is the case that ends the whole parse with a named failure.

    ``native_text`` is the text layer as extracted and ``scanned_text`` the
    page-level OCR result; both are always present so a caller can tell "no
    text layer" from "no text read". ``unit.text`` is the native text when
    there was any, otherwise the scanned text.

    The OCR state of a page is named in three parts: ``ocr_empty`` records
    that an engine was asked about *this* page and read nothing,
    ``ocr_applied`` records that an engine ran on the page whether or not
    anything came back, and a page that could not be asked at all — no engine
    configured — ends the whole parse with
    :class:`~fin.corpus.ocr.OcrRequiredUnavailableError`. An unreadable page
    is therefore data about that page and never a reason to lose the pages
    around it, so a caller can reach its own empty-text exit for one page
    instead of a failed document.
    """

    page_no: int
    page_count: int
    size_pt: tuple[float, float]
    native_text: str
    scanned_text: str
    unit: ParsedUnit | None
    raster: PageRaster
    regions: tuple[Region, ...]
    ocr_applied: bool
    ocr_empty: bool

    @property
    def native_text_char_count(self) -> int:
        return len(self.native_text.strip())


@dataclass(frozen=True, slots=True)
class PageParseResult:
    """Document-level page/region product: parse identity + ordered pages.

    ``source_page_count`` is how many pages the source really has and
    ``pages`` is the deterministic prefix of them that was extracted, so
    ``truncated`` — whether the two differ — is always observable. A caller's
    page cap is its own decision; carrying the unreported-away count is what
    lets that caller say *why* it stopped instead of silently receiving a
    short document.
    """

    fmt: str
    source_sha256: str
    document_id: str
    version: str
    parse_rule_version: str
    params_table_version: str
    pages: tuple[ParsedPage, ...]
    source_page_count: int
    truncated: bool

    def frozen_units(self) -> tuple[ParsedUnit, ...]:
        """The page units in physical page order, blank pages absent."""
        return tuple(page.unit for page in self.pages if page.unit is not None)


@dataclass(frozen=True, slots=True)
class _Identity:
    """The three document fields every unit of one result must agree on."""

    document_id: str
    version: str
    source_sha256: str


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


class PageSourcePort(Protocol):
    """The text-layer and geometry side of one source document."""

    @property
    def page_count(self) -> int: ...

    def page_size_pt(self, *, page_no: int) -> tuple[float, float]: ...

    def native_text(self, *, page_no: int) -> str: ...

    def image_count(self, *, page_no: int) -> int: ...


class PageLayoutPort(Protocol):
    """Region kinds and boxes for one page; never region text."""

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]: ...


class PageRenderPort(Protocol):
    """Rasterize one page at the scale this parser was built with.

    The port is the drawing call only. A renderer that opens something gets
    to keep that something's lifetime as its own question: the core closes
    the renderer it builds itself and never closes one handed to it.
    """

    def render_page(self, *, page_no: int) -> PageRaster: ...


class RasterOcrPort(Protocol):
    """Recognize the text drawn in one raster's pixels.

    ``reference`` names what is being read (a page or a region) so a
    failure can say so; an engine must not interpret it.
    """

    def recognize_raster(self, *, png: bytes, reference: str) -> str: ...


class NullLayout:
    """The layout port for a caller with no region engine wired.

    Yields no regions, which leaves the page rule and the raster in place
    and drops only the additive region lane. It is spelled out rather than
    passed as ``layout=None`` so a caller cannot forget the choice.
    """

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        return ()


# ---------------------------------------------------------------------------
# default source and renderer
# ---------------------------------------------------------------------------


class PdfplumberPageSource:
    """``PageSourcePort`` over the repo's existing PDF text stack.

    Deliberately the same ``pdfplumber`` call ``parse_pdf`` makes:
    pypdfium2's ``get_text_range`` reports a page's text layer up to 6%
    larger on the mixed gold fixture (460 / 1,111 / 337 characters against
    460-16 = 444 / 1,086 / 333), and a rule keyed on a character count must
    not depend on which library a caller happened to open the file with.
    ``image_count`` is ``page.images`` — the same page-level signal the
    existing scan heuristic was defined over.
    """

    def __init__(self, source_bytes: bytes) -> None:
        import pdfplumber

        try:
            self._pdf = pdfplumber.open(io.BytesIO(source_bytes))
        except Exception as exc:
            raise MalformedSourceParseError(
                f"PDF source cannot be opened for page extraction: {exc}"
            ) from exc
        if not self._pdf.pages:
            # The document this module opened is this module's to release,
            # so a refusal on the way in closes what it read rather than
            # leaving a live file to the garbage collector.
            self.close()
            raise MalformedSourceParseError("PDF source has no pages to extract")

    def close(self) -> None:
        self._pdf.close()

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    def _page(self, page_no: int):
        if not 1 <= page_no <= self.page_count:
            raise MalformedSourceParseError(
                f"page {page_no} is outside the {self.page_count}-page source"
            )
        return self._pdf.pages[page_no - 1]

    def page_size_pt(self, *, page_no: int) -> tuple[float, float]:
        page = self._page(page_no)
        return (float(page.width), float(page.height))

    def native_text(self, *, page_no: int) -> str:
        return self._page(page_no).extract_text() or ""

    def image_count(self, *, page_no: int) -> int:
        return len(self._page(page_no).images or [])


class Pypdfium2PageRenderer:
    """``PageRenderPort`` that rasterizes a PDF page with pypdfium2.

    Reached only through the dependency ``pdfplumber`` already pins, and
    lazy-imported like the other PDF doors here.

    Naming a draw failure is not this renderer's job: :meth:`_render_page`
    of :class:`PageRegionParser` is the one site that turns a rendering
    problem into :class:`~fin.corpus.errors.PageRenderError`, and it names
    whatever any renderer raises — here or from a third-party port. This
    renderer therefore names only the two states that are its own: a
    released document and a page that drew to no pixels.

    The opened document is a resource, so the renderer owns it down:
    :meth:`close` releases it, and using the renderer as a context manager
    closes it on the way out. Drawing after a close is a named render failure
    rather than a use-after-free inside the native library.
    """

    def __init__(self, source_bytes: bytes, *, dpi: int = PAGE_RENDER_DPI) -> None:
        if dpi < 1:
            raise ValueError(f"dpi must be positive, got {dpi}")
        import pypdfium2 as pdfium

        self._scale = dpi / 72.0
        self._closed = False
        try:
            self._doc = pdfium.PdfDocument(source_bytes)
        except Exception as exc:
            raise PageRenderError(
                f"PDF source cannot be opened for rendering: {exc}"
            ) from exc

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release the document. Idempotent, so an owner and a context
        manager can both close without stepping on each other."""
        if self._closed:
            return
        self._closed = True
        self._doc.close()

    def __enter__(self) -> "Pypdfium2PageRenderer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def render_page(self, *, page_no: int) -> PageRaster:
        if self._closed:
            raise PageRenderError(
                "this renderer is closed and cannot draw page "
                f"{page_no}: its document has been released"
            )
        image = self._doc[page_no - 1].render(scale=self._scale).to_pil()
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        png = buffer.getvalue()
        if not png:
            raise PageRenderError(f"page {page_no} rendered to empty pixels")
        return PageRaster.of(png, width_px=image.width, height_px=image.height)


# ---------------------------------------------------------------------------
# the core
# ---------------------------------------------------------------------------


class PageRegionParser:
    """Page/region extraction over an injected layout, render scale, and OCR.

    Side-effect free and reusable: the ports are the only world it sees, no
    call mutates them, and nothing is cached across documents.
    """

    def __init__(
        self,
        *,
        layout: PageLayoutPort,
        renderer: PageRenderPort | None = None,
        ocr: RasterOcrPort | None = None,
        dpi: int = PAGE_RENDER_DPI,
        min_region_edge_px: int = MIN_OCR_REGION_EDGE_PX,
    ) -> None:
        """Inject the ports, each with its own spelling for "not here".

        Three absences mean three different things, so each is stated at the
        one place that decides it:

        * ``layout`` has no default and never takes ``None`` — a caller with
          no region engine writes :class:`NullLayout`, which is the choice
          made visible rather than a value to forget.
        * ``renderer=None`` is a working choice, not a missing port: pages
          are drawn here with :class:`Pypdfium2PageRenderer` at ``dpi``, and
          that document is released when the call ends.
        * ``ocr=None`` is a real absence: the page and raster lanes stay
          usable, and a page that needs a read ends the parse with
          :class:`~fin.corpus.ocr.OcrRequiredUnavailableError` the first time
          one is asked for.
        """
        if dpi < 1:
            raise ValueError(f"dpi must be positive, got {dpi}")
        if min_region_edge_px < 0:
            raise ValueError(
                f"min_region_edge_px must be non-negative, got {min_region_edge_px}"
            )
        self._layout = layout
        self._renderer = renderer
        self._ocr = ocr
        self._dpi = dpi
        self._min_region_edge_px = min_region_edge_px

    @property
    def dpi(self) -> int:
        return self._dpi

    # -- entries --

    def extract_pdf(
        self,
        *,
        source_bytes: bytes,
        source_sha256: str,
        document_id: str,
        version: str,
        max_pages: int | None = None,
    ) -> PageParseResult:
        """Extract every physical page of a PDF.

        ``max_pages`` is a caller-side work bound for bounded contexts —
        the same shape ``parse_pdf`` carries, a deterministic prefix of
        pages 1..N. It is not a resource decision: an attachment page cap
        is checked by the capability that owns budgets before this runs.
        The pages the bound left behind are not lost to the caller —
        :attr:`PageParseResult.source_page_count` and
        :attr:`PageParseResult.truncated` name them, so a cap can be
        reported as the overflow it is rather than as a short document.
        """
        if not source_bytes:
            raise EmptySourceParseError("PDF source is empty (0 bytes)")
        if not has_pdf_signature(source_bytes):
            raise UnsupportedFormatParseError(
                f"page extraction of {document_id!r} was asked for a PDF and "
                "the bytes carry no %PDF- signature"
            )
        if max_pages is not None and max_pages < 1:
            raise ValueError(f"max_pages must be >= 1 or None, got {max_pages}")
        identity = _Identity(document_id, version, source_sha256)
        source = PdfplumberPageSource(source_bytes)
        # EXIT: a renderer is per-document because it is built over these
        # bytes, so it is built *and released* here; ``None`` is the PDF
        # default, not a missing port. A renderer handed in stays open — its
        # owner holds it.
        owned: Pypdfium2PageRenderer | None = None
        try:
            source_page_count = source.page_count
            limit = source_page_count if max_pages is None else min(
                max_pages, source_page_count
            )
            rasterizer = self._renderer
            if rasterizer is None:
                owned = Pypdfium2PageRenderer(source_bytes, dpi=self._dpi)
                rasterizer = owned
            pages = tuple(
                self._one_pdf_page(
                    source=source,
                    renderer=rasterizer,
                    identity=identity,
                    page_no=page_no,
                    page_count=limit,
                )
                for page_no in range(1, limit + 1)
            )
        finally:
            source.close()
            if owned is not None:
                owned.close()
        return _result(
            "pdf",
            identity,
            pages,
            source_page_count=source_page_count,
            truncated=limit < source_page_count,
        )

    def extract_image(
        self,
        *,
        source_bytes: bytes,
        source_sha256: str,
        document_id: str,
        version: str,
    ) -> PageParseResult:
        """Extract a standalone image as a one-page document.

        The image is its own page raster — no container is synthesized, so
        the pixels the vision stage sees are byte-identical to the ones
        that were uploaded. A bare image has no text layer by definition,
        so the page rule fires: the whole-page read is what the unit is
        built from, and the region lane records those same pixels as an
        addressable figure rather than only as text.
        """
        if not source_bytes:
            raise EmptySourceParseError("image source is empty (0 bytes)")
        media = _image_media_type(source_bytes)
        if media is None:
            accepted = " or ".join(
                name.upper() for name in _accepted_image_media_types()
            )
            raise UnsupportedFormatParseError(
                f"page extraction of {document_id!r} found no supported image "
                f"signature ({accepted}) in the source bytes"
            )
        identity = _Identity(document_id, version, source_sha256)
        width_px, height_px = _image_size(source_bytes)
        size_pt = (width_px * 72.0 / self._dpi, height_px * 72.0 / self._dpi)
        raster = PageRaster.of(
            source_bytes, width_px=width_px, height_px=height_px
        )
        read = self._recognize(
            png=source_bytes, reference=f"{document_id} image page 1"
        )
        scanned_text = read or ""
        figure = Region(
            kind="figure",
            left_pt=0.0,
            top_pt=0.0,
            right_pt=size_pt[0],
            bottom_pt=size_pt[1],
            text=scanned_text,
            ocr_applied=True,
        )
        page = self._page(
            fmt=media,
            identity=identity,
            page_no=1,
            page_count=1,
            size_pt=size_pt,
            native_text="",
            scanned_text=scanned_text,
            raster=raster,
            regions=(figure,),
            ocr_empty=read is None,
        )
        return _result(
            media,
            identity,
            (page,),
            source_page_count=1,
            truncated=False,
        )

    # -- one page --

    def _one_pdf_page(
        self,
        *,
        source: PageSourcePort,
        renderer: PageRenderPort,
        identity: _Identity,
        page_no: int,
        page_count: int,
    ) -> ParsedPage:
        native_text = source.native_text(page_no=page_no)
        size_pt = source.page_size_pt(page_no=page_no)
        raster = self._render_page(renderer, page_no=page_no)
        boxes = self._layout_regions(page_no=page_no, size_pt=size_pt)
        scan_required = detect_ocr_required(
            text=native_text,
            image_count=source.image_count(page_no=page_no),
            # This lane asks "did the text layer collapse on a page that
            # still carries images", and answers with the one named
            # threshold that question owns, ``PAGE_COLLAPSE_TEXT_CHARS``.
            # The seven-format parse asks a different question — whether a
            # page worth a scan at all is left — and passes 1 there; the
            # two numbers are not one threshold forked, and this call
            # states its own rather than borrowing a default.
            min_text_chars=PAGE_COLLAPSE_TEXT_CHARS,
        )
        scanned_text = ""
        ocr_empty = False
        regions = boxes
        if scan_required:
            read = self._recognize(
                png=raster.png,
                reference=f"{identity.document_id} page {page_no}",
            )
            scanned_text = read or ""
            ocr_empty = read is None
            # The scan rule already read every pixel on this page; cropping
            # its regions would recognize the same bitmap twice for nothing
            # added. The boxes stay as region metadata.
        else:
            regions = self._read_figure_crops(
                boxes,
                raster=raster,
                reference=f"{identity.document_id} page {page_no}",
            )
        return self._page(
            fmt="pdf",
            identity=identity,
            page_no=page_no,
            page_count=page_count,
            size_pt=size_pt,
            native_text=native_text,
            scanned_text=scanned_text,
            raster=raster,
            regions=regions,
            ocr_empty=ocr_empty,
        )

    def _page(
        self,
        *,
        fmt: str,
        identity: _Identity,
        page_no: int,
        page_count: int,
        size_pt: tuple[float, float],
        native_text: str,
        scanned_text: str,
        raster: PageRaster,
        regions: tuple[Region, ...],
        ocr_empty: bool,
    ) -> ParsedPage:
        # Native text wins: a citation is quoted from the layer that exists
        # in the document, and what OCR read stays additive beside it.
        unit_text = native_text if native_text.strip() else scanned_text
        unit: ParsedUnit | None = None
        if unit_text.strip():
            unit = build_unit(
                fmt=fmt,
                source_sha256=identity.source_sha256,
                document_id=identity.document_id,
                version=identity.version,
                unit=page_no,
                text=unit_text,
            )
        return ParsedPage(
            page_no=page_no,
            page_count=page_count,
            size_pt=size_pt,
            native_text=native_text,
            scanned_text=scanned_text,
            unit=unit,
            raster=raster,
            regions=regions,
            # An empty page-level read is still a read that happened, so it
            # belongs in ``ocr_applied``; ``ocr_empty`` is what separates it
            # from a read that produced text.
            ocr_applied=bool(scanned_text)
            or ocr_empty
            or any(region.ocr_applied for region in regions),
            ocr_empty=ocr_empty,
        )

    # -- ports, with every failure shape named --

    def _render_page(self, renderer: PageRenderPort, *, page_no: int) -> PageRaster:
        """Draw one page, whoever the renderer is.

        This is the one site that names a render failure. A failed draw
        arrives as whatever the renderer's library raises — the shipped
        pypdfium2 document and a third-party port alike — and it has to reach
        the caller as the named error, or a library crash would escape the
        parse family and read as a bug in the code that caught it. A renderer
        that already named its own failure is passed through untouched.
        """
        try:
            return renderer.render_page(page_no=page_no)
        except PageRenderError:
            raise
        except Exception as exc:
            raise PageRenderError(
                f"page {page_no} could not be rendered: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _layout_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        try:
            boxes = self._layout.page_regions(page_no=page_no, size_pt=size_pt)
        except PageLayoutError:
            raise
        except Exception as exc:
            raise PageLayoutError(
                f"layout engine failed on page {page_no}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return tuple(
            _checked_region(box, page_no=page_no, size_pt=size_pt)
            for box in boxes
        )

    def _recognize(self, *, png: bytes, reference: str) -> str | None:
        """Read one raster, or report ``None`` when nothing was read.

        Two conditions that used to share one exit are named apart here. An
        engine that is absent, or that failed, says nothing about the page at
        all, so the parse ends with a typed failure. An engine that ran and
        recognized no text is answering the question asked: the page holds no
        readable text, which is data about that page and cannot be allowed to
        cost the caller the rest of the document.
        """
        if self._ocr is None:
            raise OcrRequiredUnavailableError(
                f"{reference}: OCR is required and no engine is configured"
            )
        try:
            text = self._ocr.recognize_raster(png=png, reference=reference)
        except OCRError:
            raise
        except Exception as exc:
            raise OcrRequiredUnavailableError(
                f"{reference}: OCR engine raised {type(exc).__name__}: {exc}"
            ) from exc
        if not text or not text.strip():
            # EXIT: an empty read is data about this one page, not a failed
            # read — the difference is this method's stated contract.
            return None
        return freeze_text(text)

    def _read_figure_crops(
        self, boxes: tuple[Region, ...], *, raster: PageRaster, reference: str
    ) -> tuple[Region, ...]:
        """Read the pixels of every figure region big enough to hold text."""
        regions: list[Region] = []
        for box in boxes:
            if not _crop_is_legible(
                box, dpi=self._dpi, min_edge_px=self._min_region_edge_px
            ):
                # EXIT: a non-figure region, or one under the legibility
                # floor, is a design fact about the page and keeps no text
                # of its own.
                regions.append(box)
                continue
            read = self._recognize(
                png=_crop_png(box, raster=raster, dpi=self._dpi),
                reference=f"{reference} {box.kind} region",
            )
            # A box the engine read and got nothing from keeps the record
            # that it was read: the page's artwork is text-free, which is a
            # different fact from the region never having been offered.
            regions.append(replace(box, text=read or "", ocr_applied=True))
        return tuple(regions)


# ---------------------------------------------------------------------------
# geometry and identity helpers
# ---------------------------------------------------------------------------


def _checked_region(
    region: Region, *, page_no: int, size_pt: tuple[float, float]
) -> Region:
    """Reject a box the page cannot hold, before it reaches a crop."""
    width, height = size_pt
    if region.kind not in REGION_KINDS:
        raise PageLayoutError(
            f"page {page_no}: layout reported region kind {region.kind!r}, "
            f"which is not one of {', '.join(REGION_KINDS)}"
        )
    if region.right_pt <= region.left_pt or region.bottom_pt <= region.top_pt:
        raise PageLayoutError(
            f"page {page_no}: layout reported an inverted {region.kind} box "
            f"[{region.left_pt}, {region.top_pt}, {region.right_pt}, "
            f"{region.bottom_pt}]"
        )
    # One point of tolerance: Docling rounds a box to the page edge, and a
    # point is 1/72 of an inch (2.8 px at the core's 200 dpi), so a touching
    # edge is a rounding artifact rather than an overhang. The tolerance is
    # stated in points, so the corrected conversion changes no number here.
    if (
        region.left_pt < 0.0
        or region.top_pt < 0.0
        or region.right_pt > width + 1.0
        or region.bottom_pt > height + 1.0
    ):
        raise PageLayoutError(
            f"page {page_no}: {region.kind} box reaches outside the "
            f"{width} x {height} pt media box"
        )
    if region.text or region.ocr_applied:
        raise PageLayoutError(
            f"page {page_no}: layout returned text in a {region.kind} region; "
            "recognized text is this module's own output"
        )
    return region


def _crop_is_legible(region: Region, *, dpi: int, min_edge_px: int) -> bool:
    """Whether a region's pixels are big enough to be worth a read."""
    if region.kind != "figure":
        return False
    # EXIT: only a figure can hold text drawn as pixels; a text or table
    # region's own layer, when the page has one, was read page-level.
    scale = dpi / 72.0
    width_px = max(1, int(round((region.right_pt - region.left_pt) * scale)))
    height_px = max(1, int(round((region.bottom_pt - region.top_pt) * scale)))
    return min(width_px, height_px) >= min_edge_px


# ----- the crop rules, shared with the host's crop inspection lane -------
#
# The figure rule and the cut are the ONE authority for what a region crop
# is: preparation reads figure regions with them, and a later question's
# crop inspection crops with them. A second copy in the host would let the
# two lanes disagree about which box is legible and about where its pixels
# start, and a claim about a crop would then name bounds the provider was
# never shown.


def crop_is_legible(
    region: Region,
    *,
    dpi: int = PAGE_RENDER_DPI,
    min_edge_px: int = MIN_OCR_REGION_EDGE_PX,
) -> bool:
    """Whether a region's box is reliable enough to crop and inspect.

    The public spelling of the figure rule, for a caller that holds a box
    from a store rather than from a live layout port. The defaults are the
    core's own, so "reliable" means the same number preparation used to
    measure it — the 160 px shorter-edge floor at 200 dpi.
    """
    return _crop_is_legible(region, dpi=dpi, min_edge_px=min_edge_px)


def crop_png(
    region: Region, *, raster: PageRaster, dpi: int = PAGE_RENDER_DPI
) -> bytes:
    """Cut one region's box out of a page raster, as PNG bytes.

    The public spelling of the cut, so a crop inspection sends the very
    pixels the legibility rule was measured against. Raises
    :class:`~fin.corpus.errors.PageRenderError` for a box the raster cannot
    show, which is the same named failure the OCR figure read reports.
    """
    return _crop_png(region, raster=raster, dpi=dpi)


def _crop_png(region: Region, *, raster: PageRaster, dpi: int) -> bytes:
    """Cut one region's box out of the page raster at the raster's scale."""
    from PIL import Image

    box = (
        _px(region.left_pt, dpi=dpi, limit=raster.width_px),
        _px(region.top_pt, dpi=dpi, limit=raster.height_px),
        _px(region.right_pt, dpi=dpi, limit=raster.width_px),
        _px(region.bottom_pt, dpi=dpi, limit=raster.height_px),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        # EXIT: a box clipped to nothing is a region the raster cannot show;
        # it is named, not silently skipped, or the page would read as a
        # figure-free page.
        raise PageRenderError(
            f"region box [{region.left_pt}, {region.top_pt}, "
            f"{region.right_pt}, {region.bottom_pt}] pt falls outside the "
            f"{raster.width_px} x {raster.height_px} px raster"
        )
    try:
        with Image.open(io.BytesIO(raster.png)) as image:
            buffer = io.BytesIO()
            image.crop(box).convert("RGB").save(buffer, format="PNG")
            png = buffer.getvalue()
    except Exception as exc:
        raise PageRenderError(
            f"region crop could not be cut from the page raster: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not png:
        raise PageRenderError("region crop encoded to empty pixels")
    return png


def _px(points: float, *, dpi: int, limit: int) -> int:
    """One point coordinate on a raster of ``limit`` pixels at ``dpi``."""
    return max(0, min(int(round(points * dpi / 72.0)), limit))


def _image_media_type(source_bytes: bytes) -> str | None:
    """Name an accepted standalone image from the one signature table.

    A row with no ``image_media_type`` — GIF today — is a container this
    lane refuses to decode even though the failure gate knows it; the
    asymmetry belongs to the table, not to a second list kept here.
    """
    for signature in CONTENT_SIGNATURES:
        media = signature.image_media_type
        if media is not None and signature.matches(source_bytes):
            return media
    # EXIT: no accepted image magic, so the caller names the refusal instead
    # of decoding bytes that are not an image this lane speaks.
    return None


def _accepted_image_media_types() -> tuple[str, ...]:
    """The image types this lane speaks, read from the table it shares."""
    accepted: list[str] = []
    for signature in CONTENT_SIGNATURES:
        media = signature.image_media_type
        if media is not None:
            accepted.append(media)
    return tuple(accepted)


def _image_size(source_bytes: bytes) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(source_bytes)) as image:
            # The pixels are decoded, not only the header read: an image
            # truncated mid-file has a valid size and would otherwise be
            # handed to OCR as if it were whole, where it fails as an
            # empty read instead of naming the source.
            image.load()
            return (int(image.width), int(image.height))
    except Exception as exc:
        raise MalformedSourceParseError(
            f"image source cannot be decoded: {type(exc).__name__}: {exc}"
        ) from exc


def _result(
    fmt: str,
    identity: _Identity,
    pages: tuple[ParsedPage, ...],
    *,
    source_page_count: int,
    truncated: bool,
) -> PageParseResult:
    return PageParseResult(
        fmt=fmt,
        source_sha256=identity.source_sha256,
        document_id=identity.document_id,
        version=identity.version,
        parse_rule_version=PARSE_RULE_VERSION,
        params_table_version=PARAMS_TABLE_VERSION,
        pages=pages,
        source_page_count=source_page_count,
        truncated=truncated,
    )


__all__ = [
    "MIN_OCR_REGION_EDGE_PX",
    "PAGE_RENDER_DPI",
    "REGION_KINDS",
    "NullLayout",
    "PageLayoutPort",
    "PageParseResult",
    "PageRaster",
    "PageRegionParser",
    "PageRenderPort",
    "PageSourcePort",
    "ParsedPage",
    "PdfplumberPageSource",
    "Pypdfium2PageRenderer",
    "RasterOcrPort",
    "Region",
    "crop_is_legible",
    "crop_png",
    "has_pdf_signature",
]
