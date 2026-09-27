"""Preparation lifecycle for thread-owned attachments (ADR 0086).

The HTTP door accepts bytes and gets back an ``uploaded`` record; this
service owns what happens after that, once preparation has been asked
for — freezing the parsed text into versioned units, building the
thread-private derived index over the same text, and publishing
``ready`` only when both are usable. A restart resumes or rebuilds
whatever is missing instead of trusting a stored status, and deleting
the thread removes every layer this service created.

``uploaded`` is a deliberate resting state: durable bytes nobody has
asked to prepare. Recovery repairs rows whose preparation is in flight,
never an accepted upload, so a restart cannot start work that was never
requested.

Layered recovery is what makes ``ready`` mean something: raw bytes in the
object store, parsed units in the attachment store, derived postings on
top of those units. Recovery repairs from the layer below, so a crash
between any two of them is rebuilt rather than reported as a stale
``ready`` or as an empty successful search.

For a document that needs pages, this module is the adapter and no more:
the shared page/region core decides what a page yielded, and only here is
that outcome turned into an attachment code. A deployment that wired no
page lane is answered the same way — this door's own code, on the same
ladder, with the bytes kept. The core keeps no budget, index, or
thread-lifecycle authority, and never names an attachment code, so the same
extraction can serve a consumer with a different vocabulary.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, replace
from typing import ClassVar, Final, Literal, Sequence

from fin.corpus.errors import (
    EmptySourceParseError,
    FormatMismatchParseError,
    NoUnitsParseError,
    PageRenderError,
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.object_store import ObjectStore
from fin.corpus.ocr import OCRError, OcrRequiredUnavailableError
from fin.corpus.parsers import (
    PageParseResult,
    PageRegionParser,
    PdfplumberPageSource,
    has_pdf_signature,
)
from fin.host.attachment_budgets import (
    AttachmentBudgetError,
    AttachmentBudgets,
    AttachmentPageLimitExceeded,
    AttachmentVisionBudgetExceeded,
    preflight_run,
)
from fin.host.attachment_index import build_postings, is_usable
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStore,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    ParsedAttachmentUnit,
)

logger = logging.getLogger(__name__)

# Every failure code this door can publish is a named constant, as in
# :mod:`fin.host.attachment_budgets`: a reason written to a row is a wire
# value, so it is declared once, read by name, and asserted by name.
ATTACHMENT_EMPTY_TEXT: Final[str] = "attachment_empty_text"
ATTACHMENT_NOT_TEXT: Final[str] = "attachment_not_text"
ATTACHMENT_TYPE_UNSUPPORTED: Final[str] = "attachment_type_unsupported"
ATTACHMENT_PAGE_LANE_UNWIRED: Final[str] = "attachment_page_lane_unwired"
ATTACHMENT_INDEX_UNAVAILABLE: Final[str] = "attachment_index_unavailable"
SOURCE_UNAVAILABLE: Final[str] = "source_unavailable"
# The attachment exit for "a page never became pixels" is the same string
# the render stage already uses, so one shape keeps one name end to end.
RENDER_FAILED: Final[str] = PageRenderError.code

# The suffix of the upload's display filename decides which lane reads the
# bytes; the bytes themselves decide whether that claim was true. For the
# PDF lane the claim is checked at this door, against the core's own
# predicate, before a page is opened — which is this module's typed exit.
_LANE_PDF: Final[str] = "pdf"
_LANE_IMAGE: Final[str] = "image"
_PDF_SUFFIXES: Final[frozenset[str]] = frozenset({".pdf"})
_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg"})

# What a refused byte delete leaves behind, read the only two ways it can be:
# a record still owns the key, so the same door can be retried until the bytes
# are gone, or nothing owns it any more and only an operator can finish it. One
# log line names the key together with whichever of these is true.
_BYTES_STILL_OWED: Final[str] = "still owed, the same door can retry it"
_BYTES_ORPHANED: Final[str] = "orphaned, no record is left to retry it"

# The two above are the whole set: a verdict is never free text, because the
# one log line an operator reads has to say which of the two leftovers it is.
# Same ``Literal[*tuple]`` shape the retrieve tiers use.
_BYTE_DELETE_VERDICTS: Final[tuple[str, ...]] = (
    _BYTES_STILL_OWED,
    _BYTES_ORPHANED,
)
_ByteDeleteVerdict = Literal[*_BYTE_DELETE_VERDICTS]


# ----- typed door errors -----


class AttachmentError(Exception):
    """Base class for attachment-door typed errors."""


class AttachmentPreparationError(AttachmentError):
    """Preparation reached a named exit for these bytes.

    Each subclass carries exactly one stable :attr:`code`, the same
    ``code``-declaring shape :mod:`fin.host.attachment_budgets` uses, so the
    door records or publishes ``exc.code`` instead of matching types or
    reading prose. ``_freeze`` is the one place that turns one of these into
    a ``failed`` row.
    """

    code: ClassVar[str]

    def __init__(self, attachment_id: str, detail: str) -> None:
        super().__init__(f"attachment_id={attachment_id!r}: {detail}")
        self.attachment_id = attachment_id


class AttachmentTypeUnsupportedError(AttachmentPreparationError):
    """These bytes are not the document type they were named as, or not a
    type any lane of this adapter can read."""

    code = ATTACHMENT_TYPE_UNSUPPORTED


class AttachmentEmptyTextError(AttachmentPreparationError):
    """Nothing was extracted: an empty source or a document of no text."""

    code = ATTACHMENT_EMPTY_TEXT


class AttachmentNotTextError(AttachmentPreparationError):
    """Text that does not decode as UTF-8, with no page lane to fall back on."""

    code = ATTACHMENT_NOT_TEXT


class AttachmentRenderFailedError(AttachmentPreparationError):
    """A page could not be rasterized, so it can never reach the vision stage."""

    code = RENDER_FAILED


class AttachmentSourceRejectedError(AttachmentPreparationError):
    """A reader refused these bytes under its own stable name.

    Used for the failure shapes the attachment exit list gives no code of
    its own — a corrupt container, a broken layout claim, an OCR pass that
    could not run — and the reader's own ``code`` is carried unchanged so a
    shape never owns two strings. There is no default: a constructed
    instance without a code is a bug at the raise site, not an empty
    reason published to the row.
    """

    def __init__(
        self, attachment_id: str, code: str, detail: str = ""
    ) -> None:
        super().__init__(attachment_id, detail or f"source refused ({code})")
        self.code = code


class AttachmentPageLaneUnwiredError(AttachmentPreparationError):
    """A page document arrived at a deployment that has no page lane.

    ``budgets`` and ``pages`` are unset by construction on such a service,
    which is a legal text-only door rather than a violated bound, so this
    is the adapter's own named exit and not a
    :class:`~fin.host.attachment_budgets.AttachmentBudgetConfigError`: it
    judges nothing about the document, spends no page, and leaves the bytes
    for the retry that a wired deployment can answer. The refusal is judged
    per family: only a multi-page source reaches it, because its page count
    and vision pricing are exactly what the missing lane owns, while a
    standalone image is one page and waits on no lane economics — an
    unwired door answers those bytes by content, as it always did. The
    distinction an operator needs is in the code —
    ``attachment_page_lane_unwired`` says this deployment cannot read pages
    at all, where ``attachment_not_text`` and ``attachment_empty_text`` say
    something about the document.
    """

    code = ATTACHMENT_PAGE_LANE_UNWIRED


class AttachmentNotFoundError(AttachmentError):
    """Unknown attachment id, another thread's id, or one already voided.

    All three share this error so a caller on one thread cannot probe
    whether another thread holds a given attachment, nor tell a deletion
    that finished from one that was never asked for.
    """


class AttachmentNotReadyError(AttachmentError):
    """The named attachment has no usable parsed text and index yet."""


class AttachmentCleanupError(AttachmentError):
    """Raw bytes survived a delete, so the deletion is NOT complete.

    The tombstone is still in place when this raises: access stays blocked,
    the outstanding keys are named, and object deletion is idempotent, so the
    caller can retry the same door until the cleanup is confirmed. The void is
    the only durable change a refused cleanup leaves behind, and no thread row
    is removed by it, so a thread delete that hits this error is still visible,
    still owed its bytes, and still retryable through the same door.
    """

    def __init__(self, thread_id: str, keys: Sequence[str]) -> None:
        super().__init__(
            f"attachment bytes survived delete for thread_id={thread_id!r}: "
            f"{len(keys)} key(s) could not be removed"
        )
        self.thread_id = thread_id
        self.keys = tuple(keys)


@dataclass(frozen=True)
class ResolvedAttachment:
    """One ready attachment as a turn sees it."""

    attachment_id: str
    filename: str
    text: str


def _safe_display_filename(raw: str) -> str:
    """Strip path and control characters; the model gets a name, never a path."""
    name = os.path.basename(raw or "").strip()
    name = "".join(char for char in name if char.isprintable())
    return name[:200] or "attachment.txt"


def text_rejection_reason(payload: bytes) -> str | None:
    """Name why these bytes cannot be a text attachment, or ``None``.

    Preparation owns this call: the door stores raw bytes without
    classifying them, so the rejection is what a stored-but-unusable
    upload is reported as when its preparation runs.
    """
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return ATTACHMENT_NOT_TEXT
    return None


def _page_lane(filename: str) -> str | None:
    """The lane that reads these bytes, from the name the upload claimed."""
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in _PDF_SUFFIXES:
        return _LANE_PDF
    if suffix in _IMAGE_SUFFIXES:
        # EXIT: a standalone image is its own one-page document, so it takes
        # the pixel lane without ever being counted as a multi-page source.
        return _LANE_IMAGE
    return None


def _page_count(lane: str, payload: bytes) -> int:
    """How many physical pages this source holds, without extracting one.

    The count comes from the same page-count authority the core itself scans
    with, so the cap and the parse can never disagree about the size of the
    document they are both looking at.
    """
    if lane != _LANE_PDF:
        return 1
    source = PdfplumberPageSource(payload)
    try:
        return source.page_count
    finally:
        source.close()


# ----- the adapter's failure ladder -----


def corpus_failure_to_attachment_error(
    exc: ParseError | OCRError, attachment_id: str
) -> AttachmentPreparationError:
    """One reader's refusal as this door's named exit.

    Pure and total over the types the page lane can meet: it builds the
    error, and never raises, logs, reads bytes, or spends a budget, so the
    ladder the door answers with and the tests that pin it are the same one
    table. The arms run most specific first, and the last keeps the
    reader's own ``code`` so a shape with no attachment code of its own
    never ends up with two strings.
    """
    if isinstance(exc, PageRenderError):
        return AttachmentRenderFailedError(
            attachment_id, f"no pixels for a page: {exc}"
        )
    if isinstance(exc, (UnsupportedFormatParseError, FormatMismatchParseError)):
        return AttachmentTypeUnsupportedError(
            attachment_id, f"no reader for these bytes: {exc}"
        )
    if isinstance(exc, (EmptySourceParseError, NoUnitsParseError)):
        return AttachmentEmptyTextError(
            attachment_id, f"nothing was extracted: {exc}"
        )
    if isinstance(exc, OCRError):
        # Every OCR-required shape the core can report — no engine, a failed
        # engine, an empty read — is this one named exit, so a page that
        # needed OCR is never published as a blank unit.
        return AttachmentSourceRejectedError(
            attachment_id,
            OcrRequiredUnavailableError.code,
            f"the page needed OCR and could not get it: {exc}",
        )
    return AttachmentSourceRejectedError(attachment_id, exc.code, str(exc))


# ----- the service -----


class AttachmentPreparationService:
    """Owns accept → prepare → ready/failed, restart recovery, and deletion."""

    def __init__(
        self,
        *,
        store: AttachmentStore,
        objects: ObjectStore,
        budgets: AttachmentBudgets | None = None,
        pages: PageRegionParser | None = None,
    ) -> None:
        """``budgets`` and ``pages`` are the page-lane seams.

        Both stay unset for a service that only ever handles text, which is
        why neither has a value to fall back to: a page document that
        reaches such a service is answered with
        :attr:`AttachmentPageLaneUnwiredError.code` and never parsed
        against a guessed bound. ``pages`` is injectable so the extraction
        ports (layout, renderer, OCR) belong to whoever wires the run, not
        to this module.
        """
        self.store = store
        self.objects = objects
        self.budgets = budgets
        self.pages = pages

    # -- door: accept --

    def accept(
        self, *, thread_id: str, filename: str, payload: bytes
    ) -> AttachmentRecord:
        """Store the raw bytes and record the attachment as ``uploaded``.

        Nothing else happens here: no parse, no OCR, no layout, no vision,
        no index. The bytes land first so an orphan is removable; a store
        rejection (thread gone mid-upload, backing failure) takes those
        bytes with it so the door never leaves bytes no record owns.
        """
        attachment_id = str(uuid.uuid4())
        key = f"thread-attachments/{thread_id}/{attachment_id}"
        try:
            # No content type: this door has not judged the bytes, and a
            # declared media type would be a claim about content nothing
            # here has looked at.
            pointer = self.objects.put(key, payload)
        except Exception as exc:
            # EXIT: the byte store rejected the upload; there is nothing to
            # record and the door turns this into its named store failure.
            raise AttachmentStoreError(
                f"object store rejected attachment upload: {exc}"
            ) from exc
        try:
            return self.store.create(
                thread_id=thread_id,
                attachment_id=attachment_id,
                filename=_safe_display_filename(filename),
                size_bytes=pointer.size_bytes,
                sha256=pointer.sha256,
                object_key=pointer.key,
            )
        except AttachmentStoreError:
            self._discard_bytes(thread_id, (pointer.key,))
            raise

    # -- lifecycle: prepare --

    def _require(self, attachment_id: str) -> AttachmentRecord:
        record = self.store.get(attachment_id)
        if record is None:
            raise AttachmentNotFoundError(attachment_id)
        return record

    def prepare(self, attachment_id: str) -> AttachmentRecord:
        """Freeze parsed units and the derived index, then publish ready.

        This is also the retry path: a record that never reached ``ready`` is
        driven again from its retained bytes, while a published version is
        left alone because earlier answers cite its locators.
        """
        record = self._require(attachment_id)
        if record.searchable:
            # EXIT: already published and usable — re-preparing would mint a
            # version for identical bytes and race a reader that is done.
            return record
        return self._prepare(record, version=record.version + 1)

    def _prepare(self, record: AttachmentRecord, *, version: int) -> AttachmentRecord:
        attachment_id = record.attachment_id
        try:
            payload = self.objects.get(record.object_key)
        except Exception as exc:
            # EXIT: the byte store is unreachable, so no text exists to
            # freeze. Named failure, never an empty ready attachment.
            logger.warning(
                "attachment bytes unreadable: attachment_id=%s (%s)",
                attachment_id,
                exc,
            )
            return self.store.mark_failed(
                attachment_id=attachment_id,
                version=record.version,
                reason=SOURCE_UNAVAILABLE,
            )
        return self._freeze(record, version=version, payload=payload)

    def _freeze(
        self, record: AttachmentRecord, *, version: int, payload: bytes
    ) -> AttachmentRecord:
        attachment_id = record.attachment_id
        try:
            units = self._parse(record, version=version, payload=payload)
        except (
            AttachmentPreparationError,
            AttachmentPageLimitExceeded,
            AttachmentVisionBudgetExceeded,
        ) as exc:
            # EXIT: the adapter or the deployed bound named why these bytes
            # cannot become units, and every type here carries an attachment
            # code. The row records it and the bytes stay for a retry. A
            # budget CONFIG error is deliberately not in this list: a bound
            # that was deployed and cannot be read is an operator's broken
            # startup, not a product exit a client may retry.
            logger.warning("attachment preparation failed: %s", exc)
            return self.store.mark_failed(
                attachment_id=attachment_id,
                version=record.version,
                reason=exc.code,
            )
        try:
            unit_count = self.store.record_units(
                attachment_id=attachment_id, version=version, units=units
            )
            self.store.put_index(
                attachment_id=attachment_id,
                version=version,
                postings=build_postings(units),
            )
            return self.store.mark_ready(
                attachment_id=attachment_id, version=version, unit_count=unit_count
            )
        except AttachmentThreadGoneError:
            # The thread was deleted mid-flight and took the derived data
            # with it; the caller's ``AttachmentThreadGoneError`` is the
            # honest outcome, never a resurrected row.
            raise
        except AttachmentStoreError as exc:
            # EXIT: the parsed text or its index could not be written, so the
            # attachment is not searchable. Named failure, not an empty
            # success and not a silent retry against another source.
            logger.warning(
                "attachment derived data could not be stored: attachment_id=%s (%s)",
                attachment_id,
                exc,
            )
            return self.store.mark_failed(
                attachment_id=attachment_id,
                version=record.version,
                reason=ATTACHMENT_INDEX_UNAVAILABLE,
            )

    def _parse(
        self,
        record: AttachmentRecord,
        *,
        version: int,
        payload: bytes,
    ) -> tuple[ParsedAttachmentUnit, ...]:
        """Freeze the payload into versioned units, or name why it cannot be.

        This is the adapter: the shared page/region core returns its own
        outcomes, and only here do they become attachment codes. A payload
        that claims no document suffix is still read as text, which is what
        the thread has always accepted.
        """
        lane = _page_lane(record.filename)
        if lane is None:
            return self._text_units(record.attachment_id, payload)
        if lane == _LANE_IMAGE and not self._page_lane_deployed():
            # EXIT: the unwired refusal names what a deployment lacks for a
            # MULTI-PAGE source — the page cap and the wired ports a document
            # is priced with — and a standalone image is one page by
            # construction, so nothing about it waits on the lane. The
            # text-only door still owns the content answer for these bytes,
            # which is what it gave before any page lane existed.
            return self._text_units(record.attachment_id, payload)
        return self._page_units(record, version=version, lane=lane, payload=payload)

    def _page_lane_deployed(self) -> bool:
        """Whether this service can open pages: a deployed cap and wired ports."""
        return self.budgets is not None and self.pages is not None

    @staticmethod
    def _text_units(
        attachment_id: str, payload: bytes
    ) -> tuple[ParsedAttachmentUnit, ...]:
        rejected = text_rejection_reason(payload)
        if rejected is not None:
            # EXIT: bytes that neither decode as text nor carry a document
            # suffix have no reader here; naming that beats guessing a format.
            raise AttachmentNotTextError(
                attachment_id, "the bytes do not decode as UTF-8 text"
            )
        text = payload.decode("utf-8")
        if not text.strip():
            raise AttachmentEmptyTextError(
                attachment_id, "the decoded text was blank"
            )  # EXIT: an empty attachment is never published as ready.
        return (ParsedAttachmentUnit(unit=1, text=text),)

    def _page_units(
        self,
        record: AttachmentRecord,
        *,
        version: int,
        lane: str,
        payload: bytes,
    ) -> tuple[ParsedAttachmentUnit, ...]:
        attachment_id = record.attachment_id
        budgets, parser = self._preflight_page_source(
            record, lane=lane, payload=payload
        )
        try:
            # WHY a one-entry run: preparation owns exactly this document, so
            # both the page cap and the run total are asked about one file
            # here — the run that spans a turn's attachments re-preflights
            # with every count, and no page work of that run is priced at this
            # door. The cap is decided from the page COUNT, so an over-budget
            # document is refused whole before a single page is rasterized,
            # boxed, or scanned. Crops are not spent yet: the first
            # preparation scans pages, and its owner re-preflights for crops.
            preflight_run(budgets, (_page_count(lane, payload),), 0)
            result = self._extract(
                parser, record, version=version, lane=lane, payload=payload
            )
        except AttachmentBudgetError:
            # EXIT: a page or vision limit is already an attachment code.
            raise
        except (ParseError, OCRError) as exc:
            raise corpus_failure_to_attachment_error(exc, attachment_id) from exc
        units = tuple(
            ParsedAttachmentUnit(unit=unit.locator.unit, text=unit.text)
            for unit in result.frozen_units()
        )
        if not units:
            raise AttachmentEmptyTextError(
                attachment_id, "no page froze a unit"
            )  # EXIT: a document of no text is empty, not ready.
        return units

    def _preflight_page_source(
        self,
        record: AttachmentRecord,
        *,
        lane: str,
        payload: bytes,
    ) -> tuple[AttachmentBudgets, PageRegionParser]:
        """Every refusal that is due before a page is opened.

        Returning normally is the proof the page lane is deployed, so the
        caller gets the bound and the ports already narrowed. The order is
        the honesty of each answer: no bytes is empty by definition, an
        undeployed lane judges nothing about the document, and only then is
        the name's claim checked against the bytes.
        """
        attachment_id = record.attachment_id
        if not payload:
            # EXIT: no bytes is empty by definition, so it is named here
            # rather than spent on a reader or a budget check, and a page
            # count nobody took cannot mask the real reason.
            raise AttachmentEmptyTextError(
                attachment_id, "the source carries no bytes"
            )
        if not self._page_lane_deployed():
            # EXIT: page work with no deployed cap or wired ports is this
            # deployment having no page lane at all, so it is refused before
            # a page is opened, nothing about the document is judged, and
            # the bytes stay for a retry under a wired deployment. Only the
            # multi-page lane reaches here: the image lane's unwired answer
            # is decided by content in ``_parse``, because a one-page
            # source waits on no lane economics for its verdict.
            raise AttachmentPageLaneUnwiredError(
                attachment_id,
                "this deployment has no page cap and no wired extraction ports",
            )
        if lane == _LANE_PDF and not has_pdf_signature(payload):
            # EXIT: the name claimed a PDF the bytes never were, so the claim
            # is refused on the signature alone — before a page is counted
            # and before a reader gets to call them corrupt for a document
            # nobody claimed to open.
            raise AttachmentTypeUnsupportedError(
                attachment_id,
                "the filename claims a PDF and the bytes carry no PDF"
                " signature in their leading window",
            )
        return self.budgets, self.pages

    @staticmethod
    def _extract(
        parser: PageRegionParser,
        record: AttachmentRecord,
        *,
        version: int,
        lane: str,
        payload: bytes,
    ) -> PageParseResult:
        """Call the core's entry for the claimed lane with named arguments.

        WHY spelled out rather than splatted from a dict: this is the
        boundary between this adapter and the shared core, so a renamed or
        dropped core parameter has to break at this call site instead of
        surfacing as a ``TypeError`` for one of the keys at runtime. The
        units are published under this preparation version, so the core's
        locators must address the same one the store records.
        """
        if lane == _LANE_PDF:
            return parser.extract_pdf(
                source_bytes=payload,
                source_sha256=record.sha256,
                document_id=record.attachment_id,
                version=str(version),
            )
        return parser.extract_image(
            source_bytes=payload,
            source_sha256=record.sha256,
            document_id=record.attachment_id,
            version=str(version),
        )

    # -- reads --

    def status(self, attachment_id: str) -> AttachmentRecord | None:
        """The stored record, with a published-but-unusable row downgraded.

        ``ready`` is only ever reported when the derived data behind it is
        present for that version, so a torn rebuild reads as ``parsing``
        instead of a search that silently returns nothing.
        """
        record = self.store.get(attachment_id)
        if record is None or record.status != "ready":
            return record
        if self._derived_data_usable(record):
            return record
        logger.warning(
            "attachment %s reads ready without usable derived data; "
            "reporting it as parsing until it is rebuilt",
            attachment_id,
        )
        return replace(record, status="parsing", indexed=False)

    def list_for_thread(self, thread_id: str) -> tuple[AttachmentRecord, ...]:
        """Every attachment of a thread, with the same verification as a read.

        The stored status is not trusted here either: a row that claims
        ``ready`` without its derived data reads as ``parsing``, so the list
        and the single-status door can never disagree about one row.
        """
        verified: list[AttachmentRecord] = []
        for record in self.store.list_for_thread(thread_id):
            current = self.status(record.attachment_id)
            if current is not None:
                verified.append(current)
        return tuple(verified)

    def units(self, attachment_id: str) -> tuple[ParsedAttachmentUnit, ...]:
        record = self.store.get(attachment_id)
        if record is None or not record.version:
            return ()
        return self.store.units(attachment_id, record.version)

    def resolve_for_turn(
        self, *, thread_id: str, attachment_id: str
    ) -> ResolvedAttachment:
        """Resolve one attachment the active thread may put into a run."""
        record = self.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            # EXIT: unknown and other-thread ids share one error so the door
            # does not confirm what another thread holds.
            raise AttachmentNotFoundError(attachment_id)
        if not record.searchable:
            raise AttachmentNotReadyError(attachment_id)
        text = "\n\n".join(unit.text for unit in self.units(attachment_id))
        if not text.strip():
            # EXIT: a ready record whose frozen text is gone is not an empty
            # document; the turn gets the typed exit instead of a content
            # block that names nothing.
            raise AttachmentNotReadyError(attachment_id)
        return ResolvedAttachment(
            attachment_id=record.attachment_id,
            filename=record.filename,
            text=text,
        )

    # -- restart --

    def recover(self) -> int:
        """Resume or rebuild every attachment the stored status overstates.

        Returns how many rows needed work. Recovery never advances the
        parsed version of a row that already had one, so the locator the
        operator attached still addresses the same text afterwards.
        """
        repaired = 0
        for record in self.store.incomplete():
            repaired += 1
            try:
                self._prepare(record, version=record.version or 1)
            except AttachmentThreadGoneError:
                # EXIT: its thread is gone; nothing this row could write
                # would be readable, and the cascade already took the rest.
                continue
            except AttachmentStoreError:
                # EXIT: this row could not be rebuilt, so it keeps reading
                # as ``parsing`` and the next restart tries it again.
                logger.exception(
                    "attachment recovery could not rebuild attachment_id=%s",
                    record.attachment_id,
                )
        return repaired

    def _derived_data_usable(self, record: AttachmentRecord) -> bool:
        if not record.indexed or record.version not in self.store.indexed_versions(
            record.attachment_id
        ):
            return False
        postings = self.store.index_postings(
            record.attachment_id, record.version
        )
        if not is_usable(postings):
            return False
        return bool(self.store.units(record.attachment_id, record.version))

    # -- deletion --

    def delete_attachment(self, *, thread_id: str, attachment_id: str) -> None:
        """Void one attachment first, then remove its bytes.

        The order is the whole guarantee: once the tombstone is written the
        record is unreadable to every read, send, and preparation path, so a
        byte store that is down leaves blocked access and an outstanding
        obligation instead of a 200 over readable data.
        """
        object_key = self.store.tombstone(
            attachment_id=attachment_id, thread_id=thread_id
        )
        if object_key is None:
            # EXIT: unknown, other-thread, and already-finished identities
            # share one typed miss, so a delete cannot probe what a different
            # thread holds nor tell a finished deletion from a refused one.
            raise AttachmentNotFoundError(attachment_id)
        if self._remove_bytes(thread_id, (object_key,), verdict=_BYTES_STILL_OWED):
            # EXIT: the bytes survived, so the tombstone stays and the same
            # door stays retryable against the same obligation.
            raise AttachmentCleanupError(thread_id, (object_key,))
        self.store.confirm_cleanup(attachment_id=attachment_id)

    def delete_thread(self, thread_id: str) -> None:
        """Void every attachment of a thread, then remove the bytes.

        The same tombstone-first order as a single deletion, not a weaker
        copy of it: the rows go first and leave their obligations behind, so
        an interrupted cleanup keeps access blocked and stays retryable
        through this door.
        """
        self.store.delete_for_thread(thread_id)
        failures = self._remove_bytes(
            thread_id, self.store.pending_cleanup(thread_id), verdict=_BYTES_STILL_OWED
        )
        if failures:
            raise AttachmentCleanupError(thread_id, failures)
        self.store.confirm_thread_cleanup(thread_id)
        # WHY a second pass: an accept that read the live thread before the
        # void can still commit after it. Those rows are voided here, and
        # their bytes swept best-effort, because the thread row that follows
        # cannot be removed over attachments nobody can name any more.
        self._discard_bytes(thread_id, self.store.delete_for_thread(thread_id))

    def _remove_bytes(
        self, thread_id: str, keys: Sequence[str], *, verdict: _ByteDeleteVerdict
    ) -> list[str]:
        """Delete raw bytes, naming the keys the store refused to remove.

        Both adapters treat an already-absent key as success, so a non-empty
        result means the byte store failed the delete, not that the bytes
        were never there.

        WHY the log lives here and nowhere else: every refused key has to
        reach the operator exactly once, with the object store's own failure
        behind it, and only this method holds the key, the thread, and the
        exception together. ``verdict`` says whether a record still owns the
        key, which is the difference between a cleanup the same door can
        finish and a leftover no door can name any more.
        """
        failures: list[str] = []
        for key in keys:
            try:
                self.objects.delete(key)
            except Exception as exc:
                logger.error(
                    "attachment byte delete refused (%s): thread_id=%s key=%s",
                    verdict,
                    thread_id,
                    key,
                    exc_info=exc,
                )
                failures.append(key)
        return failures

    def _discard_bytes(self, thread_id: str, keys: Sequence[str]) -> None:
        """Best-effort byte sweep; a failure here is logged, never hidden.

        The durable rows are already gone when this runs, so the door cannot
        retry the leftover and the named key in the log is the operator's
        handle for it.
        """
        # EXIT: no record left owns this key to fail on, so the delete still
        # reports success and the key is named once, as an orphan.
        self._remove_bytes(thread_id, keys, verdict=_BYTES_ORPHANED)


__all__ = [
    "ATTACHMENT_EMPTY_TEXT",
    "ATTACHMENT_INDEX_UNAVAILABLE",
    "ATTACHMENT_NOT_TEXT",
    "ATTACHMENT_PAGE_LANE_UNWIRED",
    "ATTACHMENT_TYPE_UNSUPPORTED",
    "RENDER_FAILED",
    "SOURCE_UNAVAILABLE",
    "AttachmentCleanupError",
    "AttachmentEmptyTextError",
    "AttachmentError",
    "AttachmentNotFoundError",
    "AttachmentNotReadyError",
    "AttachmentNotTextError",
    "AttachmentPageLaneUnwiredError",
    "AttachmentPreparationError",
    "AttachmentPreparationService",
    "AttachmentRenderFailedError",
    "AttachmentSourceRejectedError",
    "AttachmentTypeUnsupportedError",
    "ResolvedAttachment",
    "text_rejection_reason",
]
