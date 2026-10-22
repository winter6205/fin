"""Read one persisted visual observation into the current run (ADR 0089).

The seam this sits on top of
(:func:`fin.host.attachment.attachment_vision_inspect.inspect_for_details`)
already decides reuse-or-inspect: a stored observation that jointly covers
what the run is asking comes back with nothing charged, and anything else
gets one bounded inspection of the original page. What was missing is the
turn-facing half — the caller that knows which attachment the run is
allowed to touch, renders the page so the stored hash can be proved rather
than trusted, writes the new version back, and hands the run an evidence
token. This module is that caller.

Two rules shape every path here:

* **Scope is host-bound.** The attachment is resolved through
  :meth:`~fin.host.attachment.attachment_source_read.AttachmentSourceReader.owned_record`,
  so another thread's id and a tombstoned row answer exactly as a missing id
  does (ADR 0088). The model never names a thread.
* **The raster is proved, not assumed.** Nothing persists page pixels —
  preparation kept only ``page_image_sha256`` — so reuse renders the page
  from the stored original bytes and compares that hash. A stored
  observation whose hash this build cannot reproduce is not reused: it would
  be a claim about pixels nobody has seen since.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

from fin.corpus.errors import PageRenderError
from fin.corpus.parsers._pages import (
    PAGE_RENDER_DPI,
    MalformedSourceParseError,
    PageRaster,
    PdfplumberPageSource,
    Region,
    crop_is_legible,
    crop_png,
)
from fin.host.attachment.attachment_budgets import (
    AttachmentVisionBudgetExceeded,
    VisionCallLedger,
)
from fin.host.attachment.attachment_observations import (
    COVERAGE_SCHEMA_VERSION,
    DetailKey,
    ObservationContractError,
    ObservationEnvelope,
    plotted_value_key,
)
from fin.host.attachment.attachment_prepare import filename_is_pdf, safe_display_filename
from fin.host.attachment.attachment_store import (
    AttachmentRecord,
    AttachmentStore,
    AttachmentStoreError,
    StoredPageRegion,
)
from fin.host.attachment.attachment_vision import (
    DEFAULT_MEDIA_TYPE,
    VisionCrop,
    VisionPort,
    VisionPortError,
)
from fin.host.attachment.attachment_vision_inspect import (
    InspectionOutcome,
    PageIdentity,
    inspect_for_details,
    plotted_value_covered,
)
from fin.host.evidence.visual_evidence import (
    VISUAL_BUDGET_EXCEEDED,
    VISUAL_DETAIL_INVALID,
    VISUAL_LANE_UNWIRED,
    VISUAL_PAGE_UNAVAILABLE,
    VISUAL_PROVIDER_FAILED,
    VISUAL_SCOPE_INVALID,
    VISUAL_STALE,
    VISUAL_UNAVAILABLE,
    VisualEvidence,
    VisualObservation,
    VisualReadOutcome,
    visual_crop_bounds,
)
from fin.retrieve.source_read import SOURCE_READ_MAX_TEXT_CODE_POINTS

logger = logging.getLogger(__name__)

# A plotted-value target part is a name the model typed, not a document: it
# is bounded so one ask cannot smuggle a page of prose into the provider
# prompt, the stored coverage record, and the claim row. Public: the tool
# contract enforces the same bound, so it reads it here instead of
# re-spelling the number.
MAX_DETAIL_PART_CHARS: Final[int] = 120

_QUESTION_SHAPE: Final[str] = (
    "What value is plotted for {chart} / series {series} / category "
    "{category} on this page? Report the number with its unit and scale, "
    "and say where on the page it is drawn."
)


@dataclass(frozen=True, slots=True)
class _Cut:
    """One decided crop: the bounds a claim will name, and their pixels.

    Paired because the two must never disagree — the bounds travel into the
    observation and the token while the bytes travel to the provider, and a
    claim about bounds nobody was shown is the exact failure this lane
    exists to prevent.
    """

    crop: VisionCrop
    png: bytes


@dataclass(frozen=True, slots=True)
class VisualDetail:
    """The one plotted value a turn is asking about."""

    chart: str
    series: str
    category: str

    def target_key(self) -> DetailKey:
        return plotted_value_key(
            chart=self.chart, series=self.series, category=self.category
        )

    def question(self) -> str:
        return _QUESTION_SHAPE.format(
            chart=self.chart, series=self.series, category=self.category
        )


@dataclass(frozen=True, slots=True)
class VisualReadFaces:
    """The world a visual read sees, every one of them injected.

    ``render`` draws one page of a payload to a raster and ``payload`` reads
    an object key's bytes; both are ``None`` in a build with no page lane,
    which is a typed refusal rather than a guessed render. ``now`` is passed
    in for the same reason the envelope factory takes it: nothing here reads
    a clock, so a test can pin ``created_at`` without freezing one.
    """

    reader: Any
    store: AttachmentStore
    vision: VisionPort | None
    ledger: VisionCallLedger | None
    payload: Callable[[str], bytes] | None
    render: Callable[[bytes, int], PageRaster] | None
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class VisualReadRequest:
    """What the run asked to see: one page, one plotted value, one region.

    ``scope`` is the normalized region the question is about, when the
    question has one; ``None`` means the whole page. It is the reuse
    question's subject AND the upper bound of what this lane may crop to:
    the inspection can only ever look at a region inside what the run named,
    so a question about a chart never sends a provider a page the question
    did not ask about.
    """

    attachment_id: str
    page: int
    detail: VisualDetail
    scope: tuple[float, float, float, float] | None = None


def read_visual_detail(
    faces: VisualReadFaces, request: VisualReadRequest
) -> VisualReadOutcome:
    """Earn the tokens for one visual ask, or one typed refusal.

    A stored observation that already covers the ask comes back with
    ``reused=True`` and no call charged; anything else inspects the reliable
    crop of the rendered page — or the whole page when no reliable crop
    exists — once, and persists the new observation version before it is
    cited. Whichever scope was actually inspected is what the envelope and
    the token carry, so a whole-page answer is never readable as a crop.
    """
    record = _owned(faces, request.attachment_id)
    if record is None:
        # EXIT: an unknown id, another thread's id, a tombstone, and a row
        # still being prepared all answer alike — the trust face grants a
        # record only for a readable attachment this thread owns.
        return VisualReadOutcome(refusal=VISUAL_UNAVAILABLE)
    if faces.vision is None or faces.ledger is None:
        return VisualReadOutcome(refusal=VISUAL_LANE_UNWIRED)
    if not _page_lane(record) or faces.render is None or faces.payload is None:
        return VisualReadOutcome(refusal=VISUAL_PAGE_UNAVAILABLE)
    if not _parts_bounded(request.detail):
        return VisualReadOutcome(refusal=VISUAL_DETAIL_INVALID)
    try:
        key = request.detail.target_key()
    except ObservationContractError:
        # EXIT: a blank or separator-bearing part is not a target, and an
        # ambiguous target grants no coverage even against an identical row.
        return VisualReadOutcome(refusal=VISUAL_DETAIL_INVALID)
    requested = _requested_crop(request.scope)
    if isinstance(requested, str):
        return VisualReadOutcome(refusal=requested)
    raster = _render(faces, record, request.page)
    if isinstance(raster, str):
        return VisualReadOutcome(refusal=raster)
    cut = _inspected_crop(
        faces,
        record,
        request.page,
        requested=requested,
        raster=raster,
        payload=faces.payload,
    )
    if isinstance(cut, str):
        return VisualReadOutcome(refusal=cut)
    stored = faces.store.observations(
        record.attachment_id, record.version, request.page, request.page
    )
    outcome = _inspect(
        faces, request, key, raster, record, stored, requested, cut
    )
    if isinstance(outcome, str):
        return VisualReadOutcome(refusal=outcome)
    return _tokens(faces, record, request, outcome)


def _owned(faces: VisualReadFaces, attachment_id: str) -> AttachmentRecord | None:
    try:
        return faces.reader.owned_record(attachment_id)
    except Exception:  # noqa: BLE001 - a store fault reads as no owned row
        # EXIT: the trust face is fail-closed by contract; a transport error
        # must not become a rendered page or a charged call.
        return None


def _page_lane(record: AttachmentRecord) -> bool:
    return filename_is_pdf(record.filename)


def _parts_bounded(detail: VisualDetail) -> bool:
    """Whether each plotted-target part stays a name rather than a document."""
    parts = (detail.chart, detail.series, detail.category)
    return all(0 < len(part) <= MAX_DETAIL_PART_CHARS for part in parts)


def _requested_crop(
    scope: tuple[float, float, float, float] | None,
) -> VisionCrop | str | None:
    """The region the question names, the typed refusal for a bad one,
    or ``None`` for the whole-page ask that has no region at all.

    ``VisionCrop`` is the one bound-checking shape, so nothing is validated
    twice here; a malformed region refuses before a page is drawn or a call
    charged rather than quietly widening to the whole page.
    """
    if scope is None:
        return None  # EXIT: a whole-page ask has no region to validate.
    try:
        if len(scope) != 4:
            # EXIT: a short or long sequence is not four bounds, and reading
            # positions 0..3 of a longer one would silently drop what the
            # question named.
            return VISUAL_SCOPE_INVALID
        return VisionCrop(
            left=float(scope[0]),
            top=float(scope[1]),
            right=float(scope[2]),
            bottom=float(scope[3]),
        )
    except (TypeError, ValueError, IndexError, KeyError):
        return VISUAL_SCOPE_INVALID


# The one place the crop lane turns persisted geometry into pixels and
# decides what was actually inspected.
#
# Four questions are answered here, in this order, and each has exactly one
# authority:
#
# * WHICH PAGE — the media box the layout itself laid the boxes out against
#   (:meth:`~fin.corpus.parsers._pages.PdfplumberPageSource.page_size_pt`),
#   re-read from the stored original bytes and CHECKED against the raster
#   this call drew. A page whose two extents disagree — a CropBox narrower
#   than its MediaBox, a renderer off the lane's scale — is a page this lane
#   cannot scale, and refusing it is the only answer that cannot mint a claim
#   whose bounds do not name the pixels sent.
# * WHICH box — the page's own persisted region rows, read for the version
#   the run owns. A box of a superseded version describes bytes this row no
#   longer is, so it is never a candidate.
# * IS IT RELIABLE — :func:`~fin.corpus.parsers._pages.crop_is_legible`, the
#   same 160 px shorter-edge figure rule preparation measured. A text or
#   table box is prose the page's own layer already carried; a figure below
#   the floor is a mark or a rule. Neither can hold the plotted value the run
#   asked for, so neither is a crop.
# * WHAT WAS SENT — the box cut out of the raster this call drew
#   (:func:`~fin.corpus.parsers._pages.crop_png`), so the pixels the provider
#   sees are the pixels the claim's bounds name.
#
# The request's own scope is the ceiling: a question about one region may be
# answered by that region's box, never by a box elsewhere on the page. With
# no box that both qualifies and sits inside the ask, the lane falls back to
# the whole page — the scope the spec permits — and reports ``crop=None`` so
# the envelope and token say whole page rather than naming a region no
# provider was shown.


# A raster whose own extent does not match the page's media box at the lane's
# render scale is not a page this lane can scale a box against. The bound is
# stated in pixels because that is the unit the disagreement arrives in: one
# pixel is 1/200 in, so a whole pixel of slack absorbs the renderer's integer
# rounding without also absorbing a genuinely different page box.
_MEDIA_BOX_PX_TOLERANCE: Final[int] = 1


def _inspected_crop(
    faces: VisualReadFaces,
    record: AttachmentRecord,
    page: int,
    *,
    requested: VisionCrop | None,
    raster: PageRaster,
    payload: Callable[[str], bytes],
) -> _Cut | None | str:
    """The crop this read inspects, the whole page, or a typed refusal.

    ``None`` means the whole page and is the permitted fallback, not a
    failure: a page with no reliable box, or an ask about a region none of
    the page's boxes covers, is inspected whole and reported whole. ``str`` is
    the one refusal this lane can raise on its own — a page whose media box
    and raster disagree, which is a question the whole page cannot answer
    either.
    """
    media_pt = _media_box_pt(faces, record, page, raster=raster, payload=payload)
    if isinstance(media_pt, str):
        return media_pt
    if media_pt is None:
        # EXIT: a raster with no usable extent cannot scale any box, and a
        # box scaled against a guessed page would be a crop of nothing.
        return None
    boxes = _stored_regions(faces, record, page)
    if isinstance(boxes, str):
        return boxes
    reliable = _reliable_boxes(boxes, media_pt=media_pt)
    if not reliable:
        return None  # EXIT: no reliable box, so the whole page is the scope.
    best: VisionCrop | None = None
    for box in reliable:
        if requested is not None and not _contains(box, requested):
            # EXIT: this box is not the region the question is about, so
            # cropping it would answer a different question.
            continue
        if best is None or _area(box) < _area(best):
            # WHY the tightest box: the run asked about one region, and the
            # smallest qualifying box inside it is the closest rendering of
            # that region at the highest effective resolution — which is the
            # whole reason a crop call is spent instead of a page call.
            best = box
    if best is None:
        return None  # EXIT: boxes exist but none is the region asked about.
    try:
        png = _cut_png(best, raster=raster, media_pt=media_pt)
    except (PageRenderError, OSError, ValueError):
        # EXIT: the box was legible against the page but this raster could
        # not show it; the whole page is the honest scope that remains.
        return None
    return _Cut(best, png)


def _media_box_pt(
    faces: VisualReadFaces,
    record: AttachmentRecord,
    page: int,
    *,
    raster: PageRaster,
    payload: Callable[[str], bytes],
) -> tuple[float, float] | None | str:
    """The page's own media box, or a typed refusal when it cannot be had.

    WHY the layout's own source and not the raster: the stored boxes are the
    boxes the layout engine reported, so the page they are divided by has to
    be the page the layout was handed —
    :meth:`~fin.corpus.parsers._pages.PdfplumberPageSource.page_size_pt`,
    re-read from the stored original bytes. The raster's own extent is a
    DIFFERENT authority and it disagrees with that page whenever the two
    were drawn from different boxes of the same page: a CropBox narrower
    than the MediaBox, or a renderer not at the lane's render scale.
    Dividing the layout's box by the raster's extent would make the
    normalized bounds name pixels the provider was never shown, which is
    the exact failure :class:`_Cut` exists to prevent.

    A rotation is NOT such a disagreement, and saying so is the point of
    measuring instead of assuming: pypdfium2 reports the ROTATED extent for
    a ``/Rotate``d page and pdfplumber reports the rotated page box for the
    same page, so the box and its page box are already one coordinate
    space and the cut is sound. The two authorities only part company over
    a CropBox or a render scale — which is what the check below sees, and
    what the CropBox case pins.

    So the raster is checked against the layout's box rather than used as
    it: a page the layout never measured at this scale is refused here
    instead of cropped, because the whole page is drawn from the same
    disagreeing box and so is not an answer to it either.
    """
    if raster.width_px < 1 or raster.height_px < 1:
        return None
    layout_pt = _layout_page_size_pt(record, page, payload=payload)
    if isinstance(layout_pt, str):
        return layout_pt
    if layout_pt is None:
        return None
    scale = PAGE_RENDER_DPI / 72.0
    drawn_pt = (raster.width_px / scale, raster.height_px / scale)
    for axis, (drawn, laid_out) in enumerate(zip(drawn_pt, layout_pt), start=1):
        # WHY the comparison is in pixels: the tolerance is stated in pixels
        # and the two extents are compared through a float divide and
        # multiply, which would otherwise make a raster exactly one pixel
        # away land a hair past its own bound.
        if abs(drawn - laid_out) * scale > _MEDIA_BOX_PX_TOLERANCE + 1e-6:
            # EXIT: the raster and the media box the boxes were laid out
            # against disagree on axis ``axis``; no normalized bounds derived
            # from them can name what the provider is shown.
            logger.warning(
                "visual crop refused: attachment %s version %s page %s "
                "renders at %s px where the layout's media box is %s pt "
                "(axis %s)",
                record.attachment_id,
                record.version,
                page,
                (raster.width_px, raster.height_px),
                layout_pt,
                axis,
            )
            return VISUAL_PAGE_UNAVAILABLE
    return layout_pt


def _layout_page_size_pt(
    record: AttachmentRecord, page: int, *, payload: Callable[[str], bytes]
) -> tuple[float, float] | None | str:
    """The media box the layout engine measured this page against, or none.

    Opened from the stored original bytes, because that is the same document
    the boxes were derived from — the raster this call drew is proof of the
    page, not a source for its unrotated geometry. A source that cannot be
    opened answers ``None`` (no boxes can be scaled, so the whole page is the
    scope), and a store or object-store fault answers the typed refusal.
    """
    try:
        source = PdfplumberPageSource(payload(record.object_key))
    except MalformedSourceParseError:
        # EXIT: bytes that no source will open hold no trustworthy geometry,
        # so no box on this page may be normalized against a guess.
        return None
    except Exception as exc:  # noqa: BLE001 - an unreadable object is a refusal
        logger.warning(
            "visual crop refused: attachment %s version %s page %s — "
            "stored bytes could not be read: %s",
            record.attachment_id,
            record.version,
            page,
            type(exc).__name__,
        )
        return VISUAL_PAGE_UNAVAILABLE
    try:
        size_pt = source.page_size_pt(page_no=page)
    except MalformedSourceParseError:
        # EXIT: the page is outside the source, so the media box is unknown
        # and a box cannot be scaled against one.
        return None
    except Exception as exc:  # noqa: BLE001 - a page that cannot be measured
        logger.warning(
            "visual crop refused: attachment %s version %s page %s — "
            "page size could not be measured: %s",
            record.attachment_id,
            record.version,
            page,
            type(exc).__name__,
        )
        return VISUAL_PAGE_UNAVAILABLE
    finally:
        source.close()
    if not all(value > 0.0 for value in size_pt):
        return None
    return float(size_pt[0]), float(size_pt[1])


def _stored_regions(
    faces: VisualReadFaces, record: AttachmentRecord, page: int
) -> tuple[StoredPageRegion, ...] | str:
    """The page's persisted boxes, or none when the store cannot say.

    WHY the named failures only: a blanket catch here reads a Postgres
    outage, a pre-migration schema and a page with no boxes as the same
    silent whole-page fallback, and the fallback branch has no exit
    condition, so an operator sees nothing. A store fault is therefore logged
    with the attachment and version it was reading for before it falls back,
    and an unattributable fault is a refusal rather than a quiet downgrade.
    """
    try:
        return tuple(
            faces.store.regions(
                record.attachment_id, record.version, page=page
            )
        )
    except (psycopg.Error, AttachmentStoreError):
        # EXIT: the store is unreachable, un-migrated, or the row moved; a
        # page nobody can name a region for is inspected whole, which is a
        # scope the token reports honestly rather than a claim about invented
        # geometry. The message names the row, never the exception text, so
        # a driver's DSN or credential can never reach a log line.
        logger.warning(
            "visual crop falls back to the whole page: attachment %s "
            "version %s page %s regions could not be read",
            record.attachment_id,
            record.version,
            page,
        )
        return ()
    except Exception as exc:  # noqa: BLE001 - an unattributable store fault
        # EXIT: nothing here can name what failed, so the caller cannot be
        # told a page it owns is unreadable. It refuses instead of downgrading
        # a question to an answer about a page nobody cropped.
        logger.warning(
            "visual crop refused: attachment %s version %s page %s — "
            "region lookup raised an unnamed %s",
            record.attachment_id,
            record.version,
            page,
            type(exc).__name__,
        )
        return VISUAL_PAGE_UNAVAILABLE


def _reliable_boxes(
    boxes: tuple[StoredPageRegion, ...], *, media_pt: tuple[float, float]
) -> tuple[VisionCrop, ...]:
    """The page's boxes that are reliable and convertible, in layout order.

    A box that is off the page, not the figure kind, or under the
    legibility floor is dropped here. The last two are the layout engine's
    own rule (:func:`~fin.corpus.parsers._pages.crop_is_legible`), called
    rather than restated; the first is this lane's, and it is deliberately
    not the same answer — see :func:`_on_page`.
    """
    width, height = media_pt
    kept: list[VisionCrop] = []
    for box in boxes:
        region = Region(
            kind=box.kind,
            left_pt=box.left_pt,
            top_pt=box.top_pt,
            right_pt=box.right_pt,
            bottom_pt=box.bottom_pt,
        )
        if not _on_page(region, media_pt=(width, height)):
            # EXIT: a box reaching outside the media box is a rounding
            # artifact or a bad row, and cutting it would send a provider
            # whatever the clip left over.
            continue
        if not crop_is_legible(region):
            # EXIT: a text or table box, or a figure under the 160 px
            # shorter-edge floor, holds no plotted value the run asked for.
            continue
        normalized = _normalized(region, media_pt=(width, height))
        if normalized is not None:
            kept.append(normalized)
    return tuple(kept)


def _on_page(region: Region, *, media_pt: tuple[float, float]) -> bool:
    """Whether a box fits inside the page box, with NO tolerance at all.

    AUTHORITY: the media box the layout engine measured the page against
    (:meth:`~fin.corpus.parsers._pages.PdfplumberPageSource.page_size_pt`),
    the same box :func:`_normalized` divides by — so "fits" and "normalizes
    inside 0..1" are one rule here and cannot disagree.

    WHY zero where the other copies allow slack, stated rather than assumed:
    this is the LAST gate before pixels are cut and sent, and its own
    tolerance is what decides whether an overhanging edge is clipped. The
    other two copies answer a different question at a different moment and
    are not interchangeable with it:

    * :func:`~fin.corpus.parsers._pages._checked_region` (the layout core)
      validates a box a live layout engine just reported, where Docling's
      rounding to the page edge is expected, so it allows 1.0 pt. That box
      has never been persisted and is never cut.
    * :class:`~fin.host.attachment.attachment_store.StoredPageRegion` validates a row
      on construction, where it checks only that the bounds increase —
      overhang is not its question, because a re-scaled raster can shift a
      rounding artifact and a row is never cut.

    None of the three is the authority for the other two, which is why this
    one's number is stated in its own docstring and pinned by a test rather
    than inherited.
    """
    width, height = media_pt
    return (
        region.left_pt >= 0.0
        and region.top_pt >= 0.0
        and region.right_pt <= width
        and region.bottom_pt <= height
    )


def _normalized(
    region: Region, *, media_pt: tuple[float, float]
) -> VisionCrop | None:
    """A box in PDF points as the 0..1 bounds an observation records.

    The stored points are divided by the page's own media box rather than
    by a nominal page size, so the normalized bounds a claim carries are the
    bounds of the page the provider was actually shown. A box that cannot
    become four increasing normalized bounds is not a crop.
    """
    width, height = media_pt
    if width <= 0.0 or height <= 0.0:
        return None
    try:
        return VisionCrop(
            left=region.left_pt / width,
            top=region.top_pt / height,
            right=region.right_pt / width,
            bottom=region.bottom_pt / height,
        )
    except ValueError:
        # EXIT: a box that collapses at the page's scale, or overhangs it,
        # is not a region the provider can be shown.
        return None


def _cut_png(
    crop: VisionCrop, *, raster: PageRaster, media_pt: tuple[float, float]
) -> bytes:
    """The normalized crop's pixels, cut from the raster this call drew."""
    width, height = media_pt
    return crop_png(
        Region(
            kind="figure",
            left_pt=crop.left * width,
            top_pt=crop.top * height,
            right_pt=crop.right * width,
            bottom_pt=crop.bottom * height,
        ),
        raster=raster,
    )


def _contains(outer: VisionCrop, inner: VisionCrop) -> bool:
    return (
        inner.left >= outer.left
        and inner.top >= outer.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )


def _area(crop: VisionCrop) -> float:
    return (crop.right - crop.left) * (crop.bottom - crop.top)


def _render(
    faces: VisualReadFaces, record: AttachmentRecord, page: int
) -> PageRaster | str:
    if page < 1 or (record.unit_count and page > record.unit_count):
        return VISUAL_PAGE_UNAVAILABLE  # EXIT: no such page on this version.
    assert faces.payload is not None and faces.render is not None
    try:
        payload = faces.payload(record.object_key)
        return faces.render(payload, page)
    except Exception:  # noqa: BLE001 - an undrawable page yields no claim
        # EXIT: the original bytes or the renderer failed, and a claim about
        # a page nobody rendered cannot be minted from the stored hash alone.
        return VISUAL_PAGE_UNAVAILABLE


def _inspect(
    faces: VisualReadFaces,
    request: VisualReadRequest,
    key: DetailKey,
    raster: PageRaster,
    record: AttachmentRecord,
    stored: Sequence[ObservationEnvelope],
    requested_scope: VisionCrop | None,
    cut: _Cut | None,
) -> InspectionOutcome | str:
    detail = request.detail
    identity = PageIdentity(
        attachment_id=record.attachment_id,
        attachment_version=record.version,
        # The core's unit ordinals are physical page numbers by construction
        # (attachment_prepare._page_observation), so the page the run names
        # is the unit the observation was sequenced under.
        source_unit=request.page,
        page=request.page,
        page_image_sha256=raster.sha256,
    )
    # The coverage this call grants is exactly the region it sent: a crop
    # answer grants its own bounds and nothing wider, so a later turn asking
    # for a larger region cannot read a narrower observation as answering it.
    # The caller's unwired guard already refused either face; the assert
    # restates that invariant here so the checker sees the same promise
    # (same convention as :func:`_render` above).
    assert faces.vision is not None and faces.ledger is not None
    scope = cut.crop if cut is not None else None
    try:
        return inspect_for_details(
            faces.vision,
            faces.ledger,
            image=cut.png if cut is not None else raster.png,
            media_type=DEFAULT_MEDIA_TYPE,
            question=detail.question(),
            required_details=(key,),
            requested_scope=requested_scope,
            run_observations=tuple(stored),
            created_at=faces.now(),
            identity=identity,
            # The reliable region decided above, and the pixels cut for it.
            # ``None`` is the spec's permitted whole-page fallback, taken when
            # no reliable box covers what the question asked about; the
            # envelope then records whole-page coverage for the exact key, so
            # the provenance never names a region no provider was shown.
            crop=scope,
            # The coverage this call grants is exactly the target it asked
            # for, within exactly the scope it sent: nothing here reads prose
            # to infer more, so a later turn can only reuse an observation
            # that was asked this same value of this same region.
            covered_details_for=lambda _result: (
                plotted_value_covered(
                    chart=detail.chart,
                    series=detail.series,
                    category=detail.category,
                    scope=scope,
                ),
            ),
        )
    except AttachmentVisionBudgetExceeded:
        return VISUAL_BUDGET_EXCEEDED
    except VisionPortError:
        return VISUAL_PROVIDER_FAILED
    except (ObservationContractError, TypeError, ValueError):
        return VISUAL_DETAIL_INVALID


def _tokens(
    faces: VisualReadFaces,
    record: AttachmentRecord,
    request: VisualReadRequest,
    outcome: InspectionOutcome,
) -> VisualReadOutcome:
    fresh = outcome.observation
    if fresh is not None:
        written = _persist(faces, record, fresh)
        if isinstance(written, str):
            return VisualReadOutcome(refusal=written)
        candidates: Sequence[ObservationEnvelope] = (fresh,)
        reused = False
    else:
        candidates = tuple(outcome.reused)
        reused = True
    if not candidates:
        return VisualReadOutcome(refusal=VISUAL_STALE)
    if not _identity_current(faces, record):
        # The row moved (or went) while the read was in flight: the answer
        # describes a version this thread no longer owns.
        return VisualReadOutcome(refusal=VISUAL_STALE)
    return VisualReadOutcome(
        reads=tuple(
            _read(record, envelope, reused, outcome.outbound_calls)
            for envelope in candidates
        )
    )


def _read(
    record: AttachmentRecord,
    envelope: ObservationEnvelope,
    reused: bool,
    outbound_calls: int,
) -> VisualObservation:
    """One earned observation: its provenance and the prose it describes.

    Paired here because the read is the only place that has both — a token
    alone cannot say which page said what, and prose alone cannot be
    persisted as provenance.
    """
    text, truncated = observation_text(envelope)
    return VisualObservation(
        token=_token(record, envelope, reused, outbound_calls),
        text=text,
        truncated=truncated,
    )


def _persist(
    faces: VisualReadFaces, record: AttachmentRecord, envelope: ObservationEnvelope
) -> None | str:
    try:
        faces.store.record_observation(
            attachment_id=record.attachment_id, observation=envelope
        )
    except Exception:  # noqa: BLE001 - an unwritable observation is unclaimed
        # EXIT: thread or row gone, or the store refused the version; either
        # way the run must not cite an observation nobody persisted.
        return VISUAL_STALE
    return None


def _identity_current(faces: VisualReadFaces, record: AttachmentRecord) -> bool:
    try:
        return bool(
            faces.reader.identity_is_current(
                record.attachment_id, record.version
            )
        )
    except Exception:  # noqa: BLE001 - unverifiable identity is not current
        return False


def _token(
    record: AttachmentRecord,
    envelope: ObservationEnvelope,
    reused: bool,
    outbound_calls: int,
) -> VisualEvidence:
    return VisualEvidence(
        attachment_id=envelope.attachment_id,
        version=str(envelope.attachment_version),
        version_int=envelope.attachment_version,
        unit=envelope.source_unit,
        page=envelope.page,
        observation_version=envelope.observation_version,
        scope=visual_crop_bounds(envelope.scope),
        provider=envelope.provider,
        model_version=envelope.model_version,
        page_image_sha256=envelope.page_image_sha256,
        detail=_detail_label(envelope),
        filename=safe_display_filename(record.filename),
        reused=reused,
        outbound_calls=outbound_calls,
        # ``coverage_schema_version`` is checked before reuse by the seam it
        # came from; a schema this build does not understand never arrives
        # here at all.
    )


def _detail_label(envelope: ObservationEnvelope) -> str:
    for covered in envelope.covered_details:
        return covered.key.target_key or covered.key.kind
    return ""


def observation_text(envelope: ObservationEnvelope) -> tuple[str, bool]:
    """The model-facing answer body of one observation, and its cap flag.

    Bound by the same code-point ceiling a bounded text read uses, so one
    rule covers how much source-ish prose a single tool result may carry.
    """
    text = envelope.answer_text
    if not isinstance(text, str):
        return "", True
    truncated = len(text) > SOURCE_READ_MAX_TEXT_CODE_POINTS
    return text[:SOURCE_READ_MAX_TEXT_CODE_POINTS], truncated


__all__ = [
    "MAX_DETAIL_PART_CHARS",
    "VisualDetail",
    "VisualReadFaces",
    "VisualReadRequest",
    "observation_text",
    "read_visual_detail",
]
