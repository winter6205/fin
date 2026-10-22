"""The corpus ingest endpoint service (corpus-serve-ingest, ADR 0047).

This module owns three ingest entry points on one endpoint:

* **durable accept** — :meth:`IngestEndpoint.submit_durable` is the HTTP
  door: it makes the raw object, its metadata pointer, a queued job
  record and the ``(document_id, version)`` identity durable, then
  reports the upload accepted-and-queued WITHOUT parsing, chunking or
  pushing, since those run off the request. :meth:`IngestEndpoint.job_status`
  reads back what the machine has persisted.
* **the synchronous chain** — :meth:`IngestEndpoint.ingest` is the
  programmatic path that parses, chunks and pushes in one call. The
  public upload reaches it through :meth:`IngestEndpoint.run_durable`;
  trusted explicit-ID and replay callers may also call it directly. The
  chain is fully deterministic — no model participates in any split
  decision:

1. **format recognition** — the format is named by the caller (an
   explicit declared format, or the upload's filename extension). An
   unrecognized name is the named ``unsupported_format`` failure
   (SC7); a missing name is ``format_unspecified``. There is no
   content sniffing and no model guess.
2. **parse** — :func:`fin.corpus.parsers.parse_document` produces the
   frozen units (every parse failure is the existing named family:
   ``empty_source`` / ``malformed_source`` / ``encrypted_source`` /
   ``format_mismatch`` / ``no_units`` / ``metadata_incomplete``).
3. **切分** — :func:`fin.corpus.chunking.split_document` /
   :func:`split_unit` with parameters from the single
   (document type x section x density) lookup
   (:func:`fin.corpus.params.lookup_chunk_params`, 场景适配颗粒度).
   The document-type axis is either passed in (validated against
   :data:`fin.corpus.params.DOC_TYPES`) or classified per unit by
   :func:`fin.corpus.doc_type.classify_page` — the dataset's
   deterministic integer classifier, shared (not copied) so the eval
   builder and this endpoint can never drift into two rules.
4. **入库推送** — :func:`fin.corpus.push.envelopes_of_document`
   turns the frozen units into wire envelopes (one per 资料:
   ``source_ref`` + body + ``chunks[]``); per-child
   :class:`PushRecord` rows are also built so the invalidation path
   can still key on them (see :mod:`fin.corpus.invalidation`).
   :func:`fin.retrieve.push_batching.push_in_batches` pushes the
   envelopes batch by batch through the ``PushPort`` (batch cap
   honored, resumable journal). Push failures are the named
   ``PushError`` family and leave nothing recorded as a write (SC8).
5. **版本失效** — when the caller supplies the previously pushed
   records for the same document (``previous_records``), the stale
   blocks are planned with :func:`fin.corpus.invalidation.
   plan_invalidations` and closed through the invalidation port
   (:func:`fin.retrieve.invalidation_port.invalidate_targets`). An
   invalidation failure AFTER a successful push still raises typed —
   never swallowed, never reported as a clean success (SC8); the push
   itself is idempotent, so retrying the ingest is the recovery.
6. **on_ingested hook** — runs last, only after the whole chain
   succeeded, carrying the row contract catalog registration
   (``record_filings``) consumes: ``{document_id, company, period,
   version, page_count}``. The hook's own failures propagate typed.

* **the durable execution** — :meth:`IngestEndpoint.run_durable` drives
  a job :meth:`submit_durable` persisted: it claims the job with a
  compare-and-set, reads the raw bytes back through the durable pointer,
  runs the synchronous chain above under a per-document push journal,
  and walks the state machine to ``ready``; every stage failure lands as
  a typed ``failed`` job the retry edge can pick up. A job another live
  worker owns is never failed: every busy the run's transitions can
  raise — claim, adoption, or landing — reports the typed busy join.
  A run that finds its ``push_succeeded`` already durable is a
  RECOVERY, not a re-run: it skips the push and redoes only the catalog
  upsert, from the row the pending event recorded. And a terminal
  publication event that could not be appended keeps the job
  ``cataloging`` rather than failing it, because its pending row is
  durable and recovery is exactly the redo that row exists for.

The HTTP surface (``fin.host.workspace_http``) and a future CLI both
construct :class:`IngestEndpoint` with their own ports; this module
never reads the environment and never imports FastAPI.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Final,
    Literal,
    Mapping,
    Sequence,
)

from fin.corpus.catalog_publication import (
    CATALOG_PUBLICATION_FAILED,
    CATALOG_PUBLICATION_PENDING,
    CATALOG_PUBLICATION_SUCCEEDED,
    PUSH_SUCCEEDED,
    UPLOAD_METADATA_ACCEPTED,
    PublicationWarning,
    accepted_metadata_payload,
    classify_catalog_failure,
    failed_payload,
    pending_payload,
    publication_warning,
    push_succeeded_payload,
    read_accepted_metadata,
    read_pending_publication,
    read_push_identities,
    read_push_succeeded,
    succeeded_payload,
)
from fin.corpus.chunking import (
    DocumentChunking,
    split_document,
    split_unit,
)
from fin.corpus.document_identity import (
    CORPUS_DOCUMENT_ID_PREFIX,
    DocumentIdentityError,
    DocumentIdentityStore,
    default_document_identity_store,
)
from fin.corpus.doc_type import classify_page
from fin.corpus.errors import UnsupportedFormatParseError
from fin.corpus.ingest_state import IngestJob, IngestState
from fin.corpus.ingest_state_errors import (
    DuplicateJobError,
    IngestBusyError,
    IngestStepError,
    UnknownJobError,
)
from fin.corpus.invalidation import (
    VersionChange,
    plan_invalidations,
    plan_replacement_invalidations,
)
from fin.corpus.params import DOC_TYPES
from fin.corpus.parsers import SUPPORTED_FORMATS, ParseResult, parse_document
from fin.corpus.push import (
    PushEnvelope,
    PushRecord,
    document_push_records,
    envelopes_of_document,
)
from fin.corpus.upload_metadata import (
    UNKNOWN_DOC_TYPE,
    InvalidUploadMetadataError,
    derive_document_identity,
    resolve_explicit_metadata,
    sanitize_upload_title,
)
from fin.retrieve.invalidation_port.errors import (
    InvalidationOutcome,
    InvalidationPort,
    InvalidationServiceUnavailableError,
)
from fin.retrieve.invalidation_port.operation import invalidate_targets
from fin.retrieve.invalidation_port.ledger import InvalidationLedger
from fin.retrieve.push_batching import PushJournal, push_in_batches
from fin.retrieve.push_port import PushPort, frozen_content_identities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fin.corpus.ingest_state import IngestStateStore
    from fin.corpus.metadata_store import MetadataStore
    from fin.corpus.object_store import ObjectPointer, ObjectStore

logger = logging.getLogger(__name__)

# Overflow class (§B2 第 5 条 / test allocation): the largest upload the
# endpoint accepts. Oversize is a named 413-class failure, never a
# truncated parse of a prefix. The value is the contract: the HTTP route's
# pre-buffer gate and this endpoint's check read the same number, so an
# upload of exactly this size is accepted and one byte more is refused.
MAX_INGEST_BYTES: Final[int] = 52_428_800

# Filename extension -> parser format. The root 文档类型 is the file
# format (spec Focus 2), recognized from the name the uploader gave
# it — never from EDGAR form names and never from content sniffing.
_EXTENSION_FORMATS: Final[dict[str, str]] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}

# Parser format -> media type recorded on the raw object. The durable
# accept writes the object before anything is parsed, so the type can only
# come from the recognized format name, never from sniffed content.
_FORMAT_CONTENT_TYPES: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "docx": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument"
        ".spreadsheetml.sheet"
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ),
    "html": "text/html",
    "md": "text/markdown",
    "txt": "text/plain",
}

# Media type -> parser format. The durable worker resolves the format
# back from the accepted pointer, so the media types must stay one-to-one
# with the formats above; a collision would silently pick a parser.
_CONTENT_TYPE_FORMATS: Final[dict[str, str]] = {
    media: fmt for fmt, media in _FORMAT_CONTENT_TYPES.items()
}
if len(_CONTENT_TYPE_FORMATS) != len(_FORMAT_CONTENT_TYPES):  # pragma: no cover
    raise RuntimeError(
        "the format table's media types are not one-to-one with its formats"
    )


class IngestEndpointError(RuntimeError):
    """Base class for the ingest endpoint's own named failures.

    Parse-, push- and invalidation-side failures keep their existing
    families; only the shapes no upstream family covers are defined
    here. Every subclass carries a distinct stable :attr:`code` and a
    failure is never signalled by returning ``None`` or an empty
    result.
    """

    code: str = "ingest_endpoint_error"


class IngestFormatUnspecifiedError(IngestEndpointError):
    """No format was declared and the filename names no known one.

    Invalid input (§B2 第 5 条 empty class): the endpoint refuses to
    guess a format, because a guessed format would put the split
    decision outside the deterministic table.
    """

    code = "format_unspecified"


class IngestUploadTooLargeError(IngestEndpointError):
    """The upload exceeds :data:`MAX_INGEST_BYTES` (overflow class)."""

    code = "ingest_upload_too_large"


class IngestDocumentIdMissingError(IngestEndpointError):
    """No ``document_id`` was supplied or derivable from metadata."""

    code = "document_id_missing"


#: One wording for the public refusal, shared by the HTTP door (which
#: rejects the form field) and the identity leg (which refuses the same
#: value should it reach the endpoint). The two must not drift apart.
PUBLIC_DOCUMENT_ID_REFUSAL = (
    "a public upload's identity is derived from the trusted corpus scope "
    "and the content digest; a caller-chosen document_id is not accepted"
)


class IngestDocumentIdNotAllowedError(IngestEndpointError):
    """A caller-chosen ``document_id`` reached the public upload path.

    The public identity contract derives the id from the trusted corpus
    scope plus the content digest; honoring a caller-chosen id there
    would let an upload choose its own identity and split one document
    into many, so the boundary refuses it instead of ignoring it.
    """

    code = "document_id_not_allowed"


class IngestInvalidDocTypeError(IngestEndpointError):
    """An explicit ``doc_type`` outside the params-table vocabulary.

    The table's document-type axis is a closed vocabulary
    (:data:`fin.corpus.params.DOC_TYPES`); an unknown value cannot
    silently land on the global default row because the caller clearly
    meant a specific scene.
    """

    code = "invalid_doc_type"


# The endpoint's named family for out-of-bounds explicit upload
# metadata. The bounds and the raise live in
# :mod:`fin.corpus.upload_metadata`; the alias here is so the HTTP error
# table dispatches it by ``isinstance`` like every other accept-time
# family, and so callers can catch it without importing two names.
IngestInvalidUploadMetadataError = InvalidUploadMetadataError


class IngestConcurrentRunError(IngestEndpointError):
    """A different version arrived while the document's job is in flight.

    Two flags racing on the same document would otherwise leave two raw
    objects and one pointer; the in-flight job is left undisturbed.
    """

    code = "concurrent_run"


# The accept-time step that failed to persist. Typed so the free-form
# string a caller sees is a closed set, not an open vocabulary.
IngestDurableStage = Literal[
    "unconfigured",
    "object_write",
    "pointer_write",
    "job_record",
    "pointer_read",
    "accepted_metadata",
    "identity_record",
    "publication_audit",
]


class IngestDurableCommitError(IngestEndpointError):
    """An accept-time durable item could not be persisted.

    No job is reported as accepted unless the raw object, the metadata
    pointer and the ``pending`` job record all landed, so every storage
    failure surfaces typed with the :attr:`stage` that failed.
    """

    code = "durable_commit_failed"

    def __init__(self, message: str, *, stage: IngestDurableStage) -> None:
        super().__init__(message)
        self.stage = stage


class IngestObjectCleanupError(IngestEndpointError):
    """A compensating cleanup of a rejected upload failed.

    The cleanup is the raw object's delete, or the pointer's restore or
    delete. Whatever it left behind is the residue of a failed accept, so
    losing it silently would leave an unaudited write; the original failure
    is kept as ``__cause__`` and this one names the key.
    """

    code = "object_cleanup_failed"

    def __init__(self, message: str, *, key: str) -> None:
        super().__init__(message)
        self.key = key


@dataclass(frozen=True)
class IngestedDocument:
    """What one successful ingest stored — the catalog row contract.

    ``page_count`` is the frozen-unit count (the physical granularity
    of the parse: pages for PDF/PPT, row blocks for Excel, structure
    blocks for HTML/Markdown/Word, paragraphs for text).

    ``title`` is the display title (explicit or sanitized filename
    fallback) and ``source_sha256`` the content digest a public upload
    row records — both default to ``None`` so the legacy replay caller
    that builds this row without them keeps its existing shape.
    """

    document_id: str
    company: str | None
    period: str | None
    version: str
    page_count: int
    fmt: str
    doc_type: str
    title: str | None = None
    source_sha256: str | None = None


@dataclass(frozen=True)
class AcceptedUpload:
    """What one accept validated, handed to the durable accept record.

    On the public upload path this is the ONLY source of a run's
    display metadata: it is persisted once, at accept time, as the
    ``upload_metadata_accepted`` audit event, and the run reads it back
    from there instead of from the caller's request, so a resumed run
    publishes the metadata that was accepted rather than whatever a
    retry happened to resend. The synchronous trusted explicit-ID
    caller (:meth:`ingest`) produces the same shape in memory but
    persists nothing (its audit sequence predates this contract): such
    a run keeps its own resolution — the pair its derived id encodes —
    through the ``None`` leg of ``_accepted_for_run``.

    ``doc_type`` is the accepted OVERRIDE, or ``""`` when the upload
    named none and the run will classify the document itself. It is
    recorded as ``unknown`` in that case: the accepted row is a
    redo input for the fields an accept can know, and the run's own
    classified type reaches the catalog on the pending-publication row,
    which is the row recovery actually upserts.
    """

    title: str
    doc_type: str
    company: str | None = None
    period: str | None = None


@dataclass(frozen=True)
class IngestEndpointResult:
    """The observable outcome of one ingest call.

    ``block_ids`` is the chunk block ids the receiving service
    handed back for this batch (one per slice, in chunk order across
    envelopes — the unit the citation gate keys on after T1).
    ``written`` / ``duplicates`` come from the push outcome (SC18
    idempotency accounting). ``content_identities`` comes straight off
    the same push outcome: each pushed ``source_ref`` mapped to the
    ``content_identity`` the service reported for it — the half of the
    pair the index-job handoff submits, which fin never recomputes. It is
    the batched run's own map, so it is read here and never written.
    ``invalidation`` is ``None`` when
    nothing was stale; ``records`` is the full per-child record set
    so callers (tests, the future catalog seam, the invalidation
    path) can hand it back as ``previous_records``.
    """

    document_id: str
    version: str
    fmt: str
    doc_type: str
    unit_count: int
    block_count: int
    block_ids: tuple[str, ...]
    written: int
    duplicates: int
    records: tuple[PushRecord, ...]
    invalidation: InvalidationOutcome | None
    content_identities: Mapping[str, str] = field(default_factory=dict)
    #: The wire envelopes this run pushed, in order. A resuming run needs
    #: them to re-read the identity of batches its journal skipped (an
    #: idempotent re-push returns the stored pair); the completing map is
    #: persisted on the durable ``cataloging -> ready`` edge.
    envelopes: tuple[PushEnvelope, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_identities",
            frozen_content_identities(self.content_identities),
        )


@dataclass(frozen=True)
class IngestSubmission:
    """What a durable accept reports: the job exists, nothing else yet.

    ``state`` is the persisted job state the accept left behind (or, for a
    duplicate submission, the state the existing job has already reached).
    """

    document_id: str
    version: str
    state: IngestState

    @property
    def job_id(self) -> str:
        """The job handle; ``document_id`` is the state store's key."""
        return self.document_id


@dataclass(frozen=True)
class IngestJobStatus:
    """The persisted state of one ingest job, as the status route reports it.

    ``version`` is read back from the durable pointer, never recomputed
    from a re-supplied upload, so what a poller sees is what was accepted.

    ``catalog_warning`` is the audit-derived publication state (a typed
    code plus safe copies, never raw exception text) or ``None`` once a
    success is recorded. ``catalog_warning_count`` is the number of
    DOCUMENTS with an unresolved publication, not the number of attempts.
    """

    document_id: str
    version: str
    state: IngestState
    error_type: str | None
    error_message: str | None
    catalog_warning: PublicationWarning | None = None
    catalog_warning_count: int = 0

    @property
    def job_id(self) -> str:
        """The job handle; ``document_id`` is the state store's key."""
        return self.document_id

    @property
    def text_ready(self) -> bool:
        """Whether the chunk/text side of the pipeline has landed."""
        return self.state is IngestState.READY


@dataclass(frozen=True)
class DurableRunOutcome:
    """What one :meth:`IngestEndpoint.run_durable` call reports.

    ``state`` is the job's persisted state after the call — never a
    local guess. ``ran`` says this call executed the chain (through to
    ``ready`` or to a typed ``failed``); a losing concurrent claim
    reports ``busy`` with the state it found, and a job already
    ``ready`` reports a plain join. ``error`` is the typed
    :class:`~fin.corpus.ingest_state_errors.IngestStepError` recorded
    on the job when the run failed.
    """

    document_id: str
    version: str
    state: IngestState
    ran: bool
    busy: bool
    result: IngestEndpointResult | None = None
    error: IngestStepError | None = None


def resolve_format(
    *, filename: str | None, declared_format: str | None
) -> str:
    """Name the parser format from the declared format or filename.

    An explicit declaration wins. An unrecognized name — declared or
    derived from the extension — is the named ``unsupported_format``
    failure carrying the received name (SC7); no name at all is
    ``format_unspecified``.
    """
    if declared_format:
        fmt = declared_format.strip().lower()
        if fmt not in SUPPORTED_FORMATS:
            raise UnsupportedFormatParseError(
                f"unsupported format {fmt!r}; supported: "
                f"{', '.join(SUPPORTED_FORMATS)}"
            )
        return fmt
    if filename:
        suffix = Path(filename).suffix.lower()
        fmt = _EXTENSION_FORMATS.get(suffix)
        if fmt is not None:
            return fmt
        raise UnsupportedFormatParseError(
            f"unsupported format from filename {filename!r} "
            f"(extension {suffix!r}); supported: "
            f"{', '.join(SUPPORTED_FORMATS)}"
        )
    raise IngestFormatUnspecifiedError(
        "no format was declared and the upload carries no recognized "
        f"filename extension; supported: {', '.join(SUPPORTED_FORMATS)}"
    )


def resolve_document_id(
    *,
    document_id: str | None,
    company: str | None,
    period: str | None,
) -> str:
    """Resolve the locator's ``document_id`` deterministically.

    An explicit non-empty ``document_id`` is used verbatim (trimmed).
    Otherwise the id is ``"{TICKER}-{FYQ}"`` from the metadata, upper-cased
    so the same filing re-uploaded with different capitalization joins one
    identity. The filename never participates: an uploader's file name is
    free-form and would let one document land under several ids. Neither an
    explicit id nor a complete metadata pair is the named
    ``document_id_missing`` failure — the locator triple cannot be built
    without it.
    """
    if document_id and document_id.strip():
        return document_id.strip()
    ticker = company.strip() if company else ""
    fiscal_period = period.strip() if period else ""
    if ticker and fiscal_period:
        return f"{ticker.upper()}-{fiscal_period.upper()}"
    raise IngestDocumentIdMissingError(
        "no document_id was supplied and this trusted explicit-ID seam "
        "has no complete company/period pair to derive one from"
    )


# The fiscal label half of a derived ``{TICKER}-{PERIOD}`` id: four
# digits then either a quarter digit or the fiscal-year marker. Anchored so
# a longer string that merely starts with a period is not accepted.
_DERIVED_IDENTITY_RE = re.compile(r"[0-9]{4}(Q[1-4]|FY)")


def split_company_period_from_document_id(
    document_id: str,
) -> tuple[str | None, str | None]:
    """Recover ``(company, period)`` from a derived ``{TICKER}-{PERIOD}`` id.

    WHY this exists: the catalog row contract is
    ``{document_id, company, period, version, page_count}`` and
    ``company`` is required non-empty, but the DURABLE path accepted an
    upload without persisting company/period anywhere — the job record and
    the object pointer both lack the columns. The worker therefore reached
    the catalog hook with ``company=None`` and every DERIVED-identity
    upload failed registration with ``CatalogRowError``, leaving the
    pushed data invisible to search even though the push itself succeeded.
    That is the whole product path: the corpus door and the SPA behind it
    send ``company`` + ``period`` and never an explicit id
    (``web/src/api.ts`` ``ingestCorpus``).

    Splitting the identity back apart is the smallest fix that needs no
    schema migration and no second source of truth: the id is DERIVED from
    company+period by :func:`resolve_document_id`, so the pair is already
    encoded in it, and re-deriving reads that encoding rather than
    inventing state.

    Returns ``(None, None)`` for any id that is not recognizably a
    ``{TICKER}-{PERIOD}`` pair, because guessing a company out of an opaque
    string would put a wrong ticker in the catalog — a wrong company is
    worse than an absent one, since the filing then becomes searchable
    under a name that does not own it. The shape test is therefore on BOTH
    halves: the ticker must be all-uppercase-alphanumeric (a filing's
    ticker is), and the period must be a fiscal label (``2022Q3`` /
    ``2023FY``). An SEC accession id like ``0000320193-24-000006`` fails
    the ticker test rather than being read as company ``0000320193``. A
    corpus-owned id (``doc_...``) is never split at all: it carries no
    company and no period to recover.

    KNOWN LIMIT — the explicit-``document_id`` branch of
    ``specs/corpus-serve-ingest.md`` Decision 1.1 (an EDGAR accession
    with company/period as metadata only) is NOT served here: that id
    carries no ``{TICKER}-{PERIOD}`` encoding, and the accept path
    discards the supplied pair, so the worker has nothing to fall back
    on. Serving it needs the pair persisted at accept (a
    ``fin_ingest_job`` / ``fin_corpus_object_pointer`` column) — a
    migration this fix deliberately does not carry. Until then such a
    filing is pushed and, like before this fix, not catalog-visible.
    """
    if document_id.startswith(CORPUS_DOCUMENT_ID_PREFIX):
        # A corpus-owned id carries no company and no period: it is derived
        # from the scope and the bytes, so there is nothing to split back out
        # and a split would invent a pair the id never encoded.
        return None, None
    ticker, separator, period = document_id.partition("-")
    if not separator:
        return None, None
    ticker = ticker.strip()
    period = period.strip()
    if not ticker or not period:
        return None, None
    if not ticker.isalnum() or ticker != ticker.upper():
        return None, None
    if _DERIVED_IDENTITY_RE.fullmatch(period) is None:
        return None, None
    return ticker, period


def _split(
    result: ParseResult, *, doc_type: str | None
) -> tuple[DocumentChunking, str]:
    """Split the parsed document via the params table (no model).

    With an explicit ``doc_type`` every unit splits under it. Without
    one, each unit's document type is the deterministic
    :func:`classify_page` verdict on its own frozen text — the same
    integer classifier the eval dataset uses — and the document-level
    label is the majority verdict (ties broken by the fixed
    :data:`DOC_TYPES` order).
    """
    if doc_type is not None:
        return split_document(result, doc_type=doc_type), doc_type

    per_unit = tuple(
        (unit, classify_page(unit.text)) for unit in result.units
    )
    counts = Counter(doc for _, doc in per_unit)
    resolved = max(DOC_TYPES, key=lambda t: counts.get(t, 0))
    chunking = DocumentChunking(
        document_id=result.document_id,
        version=result.version,
        doc_type=resolved,
        units=tuple(
            split_unit(unit, doc_type=unit_doc_type)
            for unit, unit_doc_type in per_unit
        ),
    )
    return chunking, resolved


#: The display title a row gets when no explicit or filename fallback
#: title is available. Only the trusted explicit-ID replay leg lands
#: here: it predates the title-bearing public upload path and carries
#: its title in the ``company``/``period`` pair it derives its id from,
#: so it records that pair rather than inventing one. A public upload
#: always resolves its own title at accept time and never lands here.
_LEGACY_REPLAY_ROW_TITLE = "Filing"


def _physical_catalog_row(document: IngestedDocument) -> dict[str, Any]:
    """The COMPLETE physical catalog row for one landed run.

    Optional descriptive fields are OMITTED when the run carries none —
    never written as ``""``, which would invent a company the document
    does not have. This dict is the redo input a restart upserts
    verbatim, so it is built once and travels unchanged.
    """
    row: dict[str, Any] = {
        "document_id": document.document_id,
        "version": document.version,
        "title": document.title,
        "doc_type": document.doc_type,
        "page_count": document.page_count,
    }
    if document.company:
        row["company"] = document.company
    if document.period:
        row["period"] = document.period
    if document.source_sha256:
        row["source_sha256"] = document.source_sha256
    return row


def _row_optional(row: Mapping[str, Any], key: str) -> str | None:
    """Read an optional physical row field without inventing a value."""
    value = row.get(key)
    return value if isinstance(value, str) and value else None


def _ingested_document(
    *,
    doc_id: str,
    company: str | None,
    period: str | None,
    version: str,
    page_count: int,
    fmt: str,
    doc_type: str,
    title: str | None,
    content_identity: bool,
) -> IngestedDocument:
    """The hook's view of one finished run.

    A run on the content-identity leg carries the title it resolved at
    accept and its own digest as ``source_sha256``; the trusted
    explicit-ID replay leg keeps the pair it derived its id from and
    gets the fallback title the catalog's required ``title`` field
    needs.
    """
    return IngestedDocument(
        document_id=doc_id,
        company=company,
        period=period,
        version=version,
        page_count=page_count,
        fmt=fmt,
        doc_type=doc_type,
        title=title if content_identity else (title or _LEGACY_REPLAY_ROW_TITLE),
        source_sha256=version if content_identity else None,
    )


@dataclass(frozen=True)
class IngestIdentityRecord:
    """One accepted upload's identity record, and the writes it can undo.

    The values travel together: the accept path hands the record over as
    one argument, so what a refused identity record has to undo is stated
    in one place instead of being spread over a call that grows with
    every new value.
    """

    objects: ObjectStore
    pointers: MetadataStore
    key: str
    document_id: str
    version: str
    prior_pointer: ObjectPointer | None


class IngestEndpoint:
    """Parse + 切分 + push/invalidate driver over injected ports.

    Parameters
    ----------
    push_port:
        The ``PushPort`` the children are pushed through (the MCP
        ``push`` adapter in production, ``FakePushPort`` in the
        default suite).
    invalidation_port:
        The ``InvalidationPort`` stale blocks are closed through. Only
        needed when callers pass ``previous_records``; a planned
        invalidation with no port is the typed
        ``invalidation_service_unavailable`` failure, never a silent
        skip.
    max_bytes:
        Upload cap (overflow class). Defaults to
        :data:`MAX_INGEST_BYTES`.
    journal:
        The push journal (resumable batches). When omitted, every
        ``ingest`` call starts a fresh in-memory journal: resume
        (SC23) is a within-run concern, while idempotency ACROSS
        ingest calls is the store's contract (a replay reports its
        duplicates). When supplied, the caller owns its scope.
    invalidation_ledger:
        The queryable invalidation record. Defaults to an in-memory
        ledger owned by this endpoint.
    version_signal:
        Optional T13 signal (parse/params version changes) for
        attributing invalidation reasons.
    previous_records:
        Optional provider of the previously pushed records for the
        document being re-ingested (per-call, or wired once here for
        service-level callers). This is the seam the T2 catalog will
        satisfy after the merge; without it there is nothing fin can
        know to be stale, which is a documented no-op — never a
        skipped failure.
    on_ingested:
        Optional callback invoked once per fully successful ingest
        with the :class:`IngestedDocument` row (the T2 catalog
        registration seam; leader wires it after the merge).
    object_store:
        Raw-bytes port used by :meth:`submit_durable`. Required only for
        the durable accept path, so the synchronous ``ingest`` chain and
        callers that never submit keep working without it.
    metadata_store:
        Pointer port used by :meth:`submit_durable` to associate the
        document with its raw object (and to read back the accepted
        version).
    state_store:
        :class:`~fin.corpus.ingest_state.IngestStateStore` carrying the
        job's persisted state; ``document_id`` is the job id, so
        ``job_id == document_id``.
    corpus_scope_id:
        The TRUSTED corpus scope id the public upload identity is
        derived from (host configuration, injected here — never read
        from a request, a filename or a model argument). Required for
        the public accept; the trusted explicit-ID replay leg keeps its
        own ``{TICKER}-{FYQ}`` locator and never consults it.
    """

    def __init__(
        self,
        *,
        push_port: PushPort,
        invalidation_port: InvalidationPort | None = None,
        max_bytes: int = MAX_INGEST_BYTES,
        journal: PushJournal | None = None,
        invalidation_ledger: InvalidationLedger | None = None,
        version_signal: VersionChange | None = None,
        previous_records: Callable[[], Sequence[PushRecord]] | None = None,
        on_ingested: Callable[[IngestedDocument], None] | None = None,
        object_store: ObjectStore | None = None,
        metadata_store: MetadataStore | None = None,
        state_store: IngestStateStore | None = None,
        identity_store: DocumentIdentityStore | None = None,
        corpus_scope_id: str = "",
    ) -> None:
        self._push_port = push_port
        self._invalidation_port = invalidation_port
        self._max_bytes = int(max_bytes)
        self._journal = journal
        self._invalidation_ledger = (
            invalidation_ledger
            if invalidation_ledger is not None
            else InvalidationLedger()
        )
        self._version_signal = version_signal
        self._previous_records = previous_records
        self._on_ingested = on_ingested
        self._object_store = object_store
        self._metadata_store = metadata_store
        self._state_store = state_store
        self._identity_store = identity_store
        self._corpus_scope_id = corpus_scope_id
        # A per-document submit lock serializes the check-write-read of
        # submit_durable so two identical uploads cannot both pass the
        # "no prior job" read and race on the durable write. The guard
        # protects the registry itself, not any single document.
        self._submit_locks: dict[str, threading.Lock] = {}
        self._submit_locks_guard = threading.Lock()

    @property
    def push_port(self) -> PushPort:
        """The push port behind this endpoint (diagnostics / tests)."""
        return self._push_port

    @property
    def state_store(self) -> IngestStateStore | None:
        """The persisted job store, when one is wired (diagnostics)."""
        return self._state_store

    @property
    def max_bytes(self) -> int:
        """The configured upload cap (overflow class; ``0`` = no cap).

        The HTTP route's pre-buffer gate reads this so the route and
        the endpoint refuse the SAME oversize boundary from one value.
        """
        return self._max_bytes

    def _validate_upload_bytes(
        self,
        data: bytes,
        *,
        filename: str | None,
        declared_format: str | None,
    ) -> None:
        """The two pre-parse refusals that need the bytes themselves.

        Split out of :meth:`_validate_upload` because a RESUMING run
        carries already-accepted identity and metadata: those are not
        re-validated, but an oversize upload and an unrecognized format
        are properties of the bytes and must still be refused here
        rather than handed to a parser.
        """
        if self._max_bytes > 0 and len(data) > self._max_bytes:
            raise IngestUploadTooLargeError(
                f"upload of {len(data)} bytes exceeds the ingest cap of "
                f"{self._max_bytes} bytes"
            )
        resolve_format(filename=filename, declared_format=declared_format)

    def _validate_upload(
        self,
        data: bytes,
        *,
        filename: str | None,
        declared_format: str | None,
        document_id: str | None,
        company: str | None,
        period: str | None,
        doc_type: str | None,
        content_identity: bool = False,
    ) -> tuple[str, str, str | None, str | None]:
        """Run every pre-parse named validation; resolve the locator.

        The failure classes that must never reach a parse or a push
        are decided HERE, in order: upload overflow
        (``ingest_upload_too_large``, checked on the raw bytes before
        anything else), format recognition (``unsupported_format`` /
        ``format_unspecified``), document-id derivation
        (``document_id_missing``) and the doc_type vocabulary
        (``invalid_doc_type``).

        With ``content_identity=True`` — the public upload contract and
        the accepted durable run — the locator is NOT derived from
        company/period: the identity is the scope-bound digest
        (:func:`derive_document_identity`) over the injected trusted
        corpus scope, an explicit caller ``document_id`` is refused, and
        the explicit metadata fields are validated against the shared
        bounds (``invalid_upload_metadata``). ``company`` / ``period``
        stay optional display metadata.

        The default leg is the trusted internal sync seam for
        explicit-ID and replay callers: an explicit id is honored and a
        company/period pair may derive one. It is not a public upload
        mode.

        Returns ``(fmt, doc_id, company, period)``. On the content leg
        the metadata is normalized (``None`` when blank) so the whole
        chain — including the ``on_ingested`` row — shares one value;
        the trusted leg returns the pair as given (blank becomes
        ``None``), and a caller that persists it normalizes explicitly.
        """
        self._validate_upload_bytes(
            data, filename=filename, declared_format=declared_format
        )

        fmt = resolve_format(
            filename=filename, declared_format=declared_format
        )
        if content_identity:
            identity = derive_document_identity(
                data, corpus_scope_id=self._corpus_scope_id, filename=filename
            )
            # The scope-bound id IS the locator. An explicit caller id
            # cannot be honored here without breaking the identity
            # contract, so it is refused rather than ignored.
            if document_id is not None and document_id.strip():
                raise IngestDocumentIdNotAllowedError(
                    PUBLIC_DOCUMENT_ID_REFUSAL
                )
            company_value = resolve_explicit_metadata(
                company, field="company"
            )
            period_value = resolve_explicit_metadata(period, field="period")
            if doc_type is not None and doc_type not in DOC_TYPES:
                raise IngestInvalidDocTypeError(
                    f"unknown doc_type {doc_type!r}; the params table's "
                    f"document types are: {', '.join(DOC_TYPES)}"
                )
            return fmt, identity.document_id, company_value, period_value
        doc_id = resolve_document_id(
            document_id=document_id,
            company=company,
            period=period,
        )
        if doc_type is not None and doc_type not in DOC_TYPES:
            raise IngestInvalidDocTypeError(
                f"unknown doc_type {doc_type!r}; the params table's "
                f"document types are: {', '.join(DOC_TYPES)}"
            )
        return fmt, doc_id, company or None, period or None

    def ingest(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        declared_format: str | None = None,
        document_id: str | None = None,
        company: str | None = None,
        period: str | None = None,
        doc_type: str | None = None,
        previous_records: Callable[[], Sequence[PushRecord]] | None = None,
        journal: PushJournal | None = None,
        content_identity: bool = False,
        title: str | None = None,
        accepted: AcceptedUpload | None = None,
        emit: Callable[[IngestedDocument], None] | None = None,
    ) -> IngestEndpointResult:
        """Run the full chain on one upload (see module docstring).

        Every failure is typed: the endpoint families above, the
        parser's ``ParseError`` family, the push ``PushError`` family
        and the invalidation ``InvalidationError`` family. A failure
        before the push writes nothing; a failure after it (invalidation,
        hook) leaves the idempotent push in place and is the caller's
        signal to retry. The pre-parse validations live in
        :meth:`_validate_upload`.

        ``content_identity`` selects the content-derived locator of the
        public upload contract. It is an INTERNAL leg, not a public
        mode: trusted explicit-ID/replay callers leave it off and pass
        the id they already own.

        ``journal`` overrides the endpoint's push journal for this run
        only — the durable worker hands in a per-document journal whose
        completed batches survive a restart, so a replay skips exactly
        the batches that already landed.

        ``accepted`` is the durable accept's already-validated,
        already-persisted metadata, handed back by
        :meth:`run_durable`. When it is given, the identity and the
        display metadata are taken from it verbatim instead of being
        re-derived: re-deriving would be the reconstruction the audit
        trail exists to prevent, and the run would publish whatever a
        retry happened to resend instead of what was accepted.

        ``emit`` REPLACES the ``on_ingested`` hook for this run. The
        durable run needs the row as data — it has to persist the row
        as redo input before the write, not merely observe it — so it
        captures the document and publishes it in the ordered sequence
        the audit contract fixes.
        """
        if accepted is not None:
            # The accept already validated the identity and the metadata
            # bounds and persisted them; re-deriving here would both
            # duplicate that work and let a retry change what lands.
            # Only the checks that need the BYTES still apply: the size
            # cap and format recognition.
            self._validate_upload_bytes(
                data,
                filename=filename,
                declared_format=declared_format,
            )
            fmt = resolve_format(
                filename=filename, declared_format=declared_format
            )
            doc_id = document_id or ""
            company, period = accepted.company, accepted.period
            resolved_title: str | None = accepted.title
            # The accepted override is the one the run splits under;
            # leaving the raw argument would silently re-classify a
            # document the uploader already typed.
            doc_type = accepted.doc_type or None
            content_identity = True
        else:
            fmt, doc_id, company, period = self._validate_upload(
                data,
                filename=filename,
                declared_format=declared_format,
                document_id=document_id,
                company=company,
                period=period,
                doc_type=doc_type,
                content_identity=content_identity,
            )
            # The sanitized title is computed before any write so a
            # non-string ``title`` is part of the same pre-parse refusal.
            resolved_title = (
                None
                if not content_identity
                else sanitize_upload_title(title, filename=filename)
            )

        version = hashlib.sha256(data).hexdigest()
        parsed = parse_document(
            data,
            fmt=fmt,
            document_id=doc_id,
            version=version,
        )
        chunking, resolved_doc_type = _split(parsed, doc_type=doc_type)
        # Per-child records are kept for the invalidation path (it
        # keys on (source_ref, per-child content_id)); envelopes are
        # the wire shape (one per 资料, with body + chunks).
        records = document_push_records(chunking)
        envelopes = envelopes_of_document(
            chunking,
            valid_from=version,
            valid_until="9999-12-31T23:59:59.999Z",
        )

        push_outcome = push_in_batches(
            self._push_port,
            envelopes,
            journal=self._journal_for_run(journal),
        )
        invalidation = self._invalidate_stale(
            doc_id=doc_id,
            current=records,
            previous_records=(
                previous_records
                if previous_records is not None
                else self._previous_records
            ),
        )

        landed = _ingested_document(
            doc_id=doc_id,
            company=company,
            period=period,
            version=version,
            page_count=len(parsed.units),
            fmt=fmt,
            doc_type=resolved_doc_type,
            title=resolved_title,
            content_identity=content_identity,
        )
        emit = emit if emit is not None else self._on_ingested
        if emit is not None:
            emit(landed)

        return IngestEndpointResult(
            document_id=doc_id,
            version=version,
            fmt=fmt,
            doc_type=resolved_doc_type,
            unit_count=len(parsed.units),
            block_count=push_outcome.written + push_outcome.duplicates,
            block_ids=tuple(push_outcome.block_ids),
            written=push_outcome.written,
            duplicates=push_outcome.duplicates,
            records=records,
            invalidation=invalidation,
            content_identities=push_outcome.content_identities,
            envelopes=envelopes,
        )

    def _invalidate_stale(
        self,
        *,
        doc_id: str,
        current: Sequence[PushRecord],
        previous_records: Callable[[], Sequence[PushRecord]] | None,
    ) -> InvalidationOutcome | None:
        """Close the stale blocks of a re-ingest; ``None`` when none.

        Without ``previous_records`` there is nothing fin can know to
        be stale (the retrieval backend owns the old rows) — that is a
        documented no-op, not a skipped failure. With stale targets
        planned and no port configured, the failure is typed (SC8:
        push landing must not let the invalidation failure vanish).
        """
        if previous_records is None:
            return None
        targets = plan_invalidations(
            previous_records(), current, version_signal=self._version_signal
        )
        if not targets:
            return None
        if self._invalidation_port is None:
            raise InvalidationServiceUnavailableError(
                f"ingest pushed {len(current)} blocks for {doc_id!r} but "
                "no invalidation port is configured; the stale blocks "
                "cannot be closed"
            )
        return invalidate_targets(
            self._invalidation_port,
            targets,
            ledger=self._invalidation_ledger,
        )

    def _journal_for_run(
        self, journal: PushJournal | None
    ) -> PushJournal:
        """This run's push journal: per-run override, endpoint-owned, fresh."""
        if journal is not None:
            return journal
        if self._journal is not None:
            return self._journal
        return PushJournal()

    def submit_durable(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        declared_format: str | None = None,
        document_id: str | None = None,
        company: str | None = None,
        period: str | None = None,
        doc_type: str | None = None,
        title: str | None = None,
    ) -> IngestSubmission:
        """Persist one upload durably and report the job that owns it.

        The public contract has one flow: the file is the only required
        input, the identity is derived from the trusted corpus scope and
        the content digest, and ``company`` / ``period`` / ``doc_type`` /
        ``title`` are optional display metadata. The public HTTP door
        rejects a multipart ``document_id`` before calling this method.

        ``document_id`` is the TRUSTED explicit-ID seam for internal
        imports and replay: a non-blank value is honored as the identity
        (never derived from company/period), and it is not a public
        upload option. Nothing is parsed, chunked or pushed here.

        The upload is accepted only once every accept-time item is
        durable — the raw object, the metadata pointer, the job record,
        the ``(document_id, version)`` identity and the accepted
        metadata audit row — and every validation that needs no bytes
        (size, format, identity, metadata, doc_type) is decided BEFORE
        the first write, so a rejected upload leaves nothing behind.

        A submission of the bytes the document already points at idempotently
        joins that job. A different version while the job is in flight is the
        typed :class:`IngestConcurrentRunError`. A different version after a
        ``failed`` job re-enters the machine at ``fetching``, the retry edge
        the state machine already defines. A different version after a
        ``ready`` job settles as a replacement: the stale blocks of the
        version being replaced are invalidated, the pointer moves to the new
        object and the job reopens as a fresh ``pending`` generation of the
        same document job, so one document never carries two logical
        versions at once.

        Any persistence failure yields no accepted job: the object written
        for this attempt is deleted again, and a delete that itself fails is
        the typed :class:`IngestObjectCleanupError`, never a swallow.
        """
        trusted_id = (
            document_id.strip()
            if document_id is not None and document_id.strip()
            else None
        )
        fmt, doc_id, resolved_company, resolved_period = self._validate_upload(
            data,
            filename=filename,
            declared_format=declared_format,
            document_id=trusted_id,
            company=company,
            period=period,
            doc_type=doc_type,
            content_identity=trusted_id is None,
        )
        if trusted_id is not None:
            resolved_company = resolve_explicit_metadata(
                company, field="company"
            )
            resolved_period = resolve_explicit_metadata(period, field="period")
        # The title is resolved (and validated) BEFORE the first durable
        # write: a control-bearing or over-long explicit title refuses
        # here, and the value that lands in the accepted-metadata audit
        # row is the very one the run will publish.
        resolved_title = sanitize_upload_title(title, filename=filename)
        objects, pointers, states = self._durable_stores()
        version = hashlib.sha256(data).hexdigest()
        # The key is identity-derived, so re-submitting the same bytes can
        # never land a second object and a different filename cannot name a
        # second copy of one version.
        key = f"filings/{doc_id}/{version}"
        # Resolved before the lock: a missing media type is a code bug in the
        # format table, not a storage outage, so it must not be misclassified
        # as a 503 by the write's except handler.
        content_type = _FORMAT_CONTENT_TYPES[fmt]

        with self._lock_for(doc_id):
            prior_pointer = self._known_pointer(doc_id)
            prior_job = self._known_job(doc_id)

            joined = self._resolve_join(
                prior_pointer, prior_job, doc_id, version
            )
            if joined is not None:
                return joined
            replacing = (
                prior_job is not None
                and prior_job.state is IngestState.READY
            )
            if replacing and prior_pointer is None:
                # ``ready`` is reached only by an accept that made its pointer
                # durable, so a ready job with no pointer is the store
                # disagreeing with itself. This accept must not paper over it
                # by guessing what was replaced.
                raise IngestDurableCommitError(
                    f"{doc_id!r} reports a ready job with no durable object "
                    "pointer",
                    stage="pointer_read",
                )  # EXIT: no accepted job, nothing was written
            if not replacing:
                self._raise_if_conflict(prior_job, doc_id)

            # Every public accept persists its redo input: the run reads
            # the accepted metadata back from the audit row instead of
            # re-deriving it on retry.
            accepted_metadata = AcceptedUpload(
                title=resolved_title,
                doc_type=doc_type or "",
                company=resolved_company,
                period=resolved_period,
            )
            job = self._persist_accept(
                objects,
                pointers,
                states,
                key=key,
                data=data,
                content_type=content_type,
                doc_id=doc_id,
                version=version,
                prior_pointer=prior_pointer,
                accepted_metadata=accepted_metadata,
                superseded_version=(
                    prior_pointer.sha256
                    if replacing and prior_pointer is not None
                    else None
                ),
            )
            return IngestSubmission(
                document_id=doc_id, version=version, state=job.state
            )

    def _durable_stores(
        self,
    ) -> tuple[ObjectStore, MetadataStore, IngestStateStore]:
        """The three durable ports, or a typed unconfigured failure.

        The accept writes to all three; a missing one cannot be compensated,
        so the boundary check happens before any write.
        """
        objects = self._object_store
        pointers = self._metadata_store
        states = self._state_store
        if objects is None or pointers is None or states is None:
            raise IngestDurableCommitError(
                "the durable accept needs an object store, a metadata "
                "pointer store and an ingest state store; at least one of "
                "them is not wired",
                stage="unconfigured",
            )
        return objects, pointers, states

    def _lock_for(self, document_id: str) -> threading.Lock:
        """The one submit lock for a document, created on first use."""
        with self._submit_locks_guard:
            lock = self._submit_locks.get(document_id)
            if lock is None:
                lock = threading.Lock()
                self._submit_locks[document_id] = lock
            return lock

    def _resolve_join(
        self,
        prior_pointer: ObjectPointer | None,
        prior_job: IngestJob | None,
        document_id: str,
        version: str,
    ) -> IngestSubmission | None:
        """The submission that owns these bytes already, if there is one.

        A re-upload of the version the document already points at is an
        idempotent join onto the existing job — no second object, pointer or
        job row is written.
        """
        if (
            prior_pointer is not None
            and prior_pointer.sha256 == version
            and prior_job is not None
        ):
            return IngestSubmission(
                document_id=document_id,
                version=version,
                state=prior_job.state,
            )
        return None

    def _raise_if_conflict(
        self, prior_job: IngestJob | None, document_id: str
    ) -> None:
        """Reject a version swap the machine has no settled path for.

        A ``failed`` job is not a conflict: the retry edge re-enters it at
        ``fetching``. A ``ready`` job is the settled replacement case and is
        owned by the caller. Any other state is a job still in flight, so a
        different version is the typed concurrent-run error and nothing is
        written.
        """
        if prior_job is None or prior_job.state is IngestState.FAILED:
            return
        raise IngestConcurrentRunError(
            f"a job for {document_id!r} is already in state "
            f"{prior_job.state.value!r}; a different version is not "
            "accepted while it is in flight"
        )

    def _persist_accept(
        self,
        objects: ObjectStore,
        pointers: MetadataStore,
        states: IngestStateStore,
        *,
        key: str,
        data: bytes,
        content_type: str,
        doc_id: str,
        version: str,
        prior_pointer: ObjectPointer | None,
        accepted_metadata: AcceptedUpload,
        superseded_version: str | None = None,
    ) -> IngestJob:
        """Write the object, pointer, job, accepted metadata and identity in order.

        Nothing is reported accepted until all five are durable. A failure at
        any step deletes the object written this attempt (and restores the
        overwritten pointer), so a rejected upload leaves the document exactly
        as it was; a compensation that itself fails is the typed cleanup error.

        A replacement adds one step before the pointer moves: the blocks of
        the version being superseded are closed through the invalidation port.
        It runs FIRST because it is the one accept-time side effect fin cannot
        undo here — a failure must still leave the superseded version's
        pointer and ``ready`` job untouched, and a retry converges because the
        invalidation request is idempotent.

        ``accepted_metadata`` is persisted once the job record exists to
        own it: it is the public upload's worker redo input, so a run
        that started without it durable could publish a row it can no
        longer rebuild, and a refusal here rejects the whole accept rather
        than admitting a worker that cannot finish. The identity record
        follows as the last accepting step.
        """
        try:
            pointer = objects.put(key, data, content_type=content_type)
        except Exception as exc:
            raise IngestDurableCommitError(
                f"the raw object for {doc_id!r} could not be written "
                f"({type(exc).__name__})",
                stage="object_write",
            ) from exc
        if pointer.sha256 != version or pointer.size_bytes != len(data):
            # The version IS the object's identity; a store that reports a
            # different digest cannot be trusted with the accept.
            self._discard_rejected_upload(
                objects=objects, pointers=pointers, key=key
            )
            raise IngestDurableCommitError(
                f"the object store returned a pointer that does not match "
                f"the upload for {doc_id!r}",
                stage="object_write",
            )

        if superseded_version is not None:
            try:
                self._invalidate_replaced(doc_id, version=version)
            except Exception as exc:
                self._discard_rejected_upload(
                    objects=objects,
                    pointers=pointers,
                    key=key,
                    cause=exc,
                )
                raise

        try:
            pointers.put_pointer(doc_id, pointer)
        except Exception as exc:
            self._discard_rejected_upload(
                objects=objects,
                pointers=pointers,
                key=key,
                document_id=doc_id,
                restore=prior_pointer,
                pointer_written=False,
                cause=exc,
            )
            raise IngestDurableCommitError(
                f"the metadata pointer for {doc_id!r} could not be written "
                f"({type(exc).__name__})",
                stage="pointer_write",
            ) from exc

        try:
            job = self._record_job(
                states,
                doc_id,
                version=version,
                superseded_version=superseded_version,
            )
        except IngestDurableCommitError:
            raise
        except Exception as exc:
            self._discard_rejected_upload(
                objects=objects,
                pointers=pointers,
                key=key,
                document_id=doc_id,
                restore=prior_pointer,
                cause=exc,
            )
            raise IngestDurableCommitError(
                f"the ingest job for {doc_id!r} could not be recorded "
                f"({type(exc).__name__})",
                stage="job_record",
            ) from exc

        try:
            self._record_accepted_metadata(
                states, doc_id, version=version, accepted=accepted_metadata
            )
        except Exception as exc:
            # The object and pointer are already written; a rejected
            # accept must not leave them, or the document would point at
            # bytes no worker will ever publish.
            self._discard_rejected_upload(
                objects=objects,
                pointers=pointers,
                key=key,
                document_id=doc_id,
                restore=prior_pointer,
                cause=exc,
            )
            raise IngestDurableCommitError(
                f"the accepted metadata for {doc_id!r} could not be "
                f"recorded durably ({type(exc).__name__})",
                stage="accepted_metadata",
            ) from exc
        self._record_identity_or_fail(
            IngestIdentityRecord(
                objects=objects,
                pointers=pointers,
                key=key,
                document_id=doc_id,
                version=version,
                prior_pointer=prior_pointer,
            )
        )
        return job

    def _record_identity(self, doc_id: str, *, version: str) -> None:
        """Record the accepted document's place in the corpus identity mapping.

        The mapping only ADDS resolution: it says which id a second
        identifier resolves to, and never renames an identifier or a
        document. A deployment that named no identity store records nothing
        and the corpus behaves exactly as it did before the mapping existed.
        """
        store = (
            self._identity_store
            if self._identity_store is not None
            else default_document_identity_store()
        )
        if store is None:
            # EXIT: no identity store configured — nothing is recorded.
            return
        store.record_document(document_id=doc_id, version=version)

    def _record_identity_or_fail(self, record: IngestIdentityRecord) -> None:
        """Record the document's place in the identity mapping, or refuse the accept.

        The document is already durable, so the mapping is the last thing
        that can refuse the accept: a document accepted without its place in
        the mapping would resolve under one id only, which is the hole this
        mapping exists to close. Only a typed identity failure is a data
        failure; a programming error must not be dressed up as one.
        """
        try:
            self._record_identity(record.document_id, version=record.version)
        except DocumentIdentityError as exc:
            self._discard_rejected_upload(
                objects=record.objects,
                pointers=record.pointers,
                key=record.key,
                document_id=record.document_id,
                restore=record.prior_pointer,
                cause=exc,
            )
            raise IngestDurableCommitError(
                f"the document identity for {record.document_id!r} could not be "
                f"recorded ({type(exc).__name__})",
                stage="identity_record",
            ) from exc

    def _record_accepted_metadata(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        accepted: "AcceptedUpload",
    ) -> None:
        """Persist the accepted metadata so a restart can redo the publish.

        The ``doc_type`` written here is the run's FINAL resolved type
        when one is already known and an override otherwise; a run that
        classifies the document itself records the classified type on its
        own pending-publication row, which is the row recovery upserts.
        """
        states.append_audit(
            document_id,
            UPLOAD_METADATA_ACCEPTED,
            accepted_metadata_payload(
                version=version,
                title=accepted.title,
                doc_type=accepted.doc_type or UNKNOWN_DOC_TYPE,
                company=accepted.company,
                period=accepted.period,
            ),
        )

    def job_status(self, job_id: str) -> IngestJobStatus:
        """Report the persisted state of one ingest job.

        ``job_id`` is the state store's key, i.e. the ``document_id``. The
        version is read back from the durable pointer, never recomputed from
        a re-supplied upload, so a poller sees what was accepted. An unknown
        id stays the store's typed :class:`UnknownJobError` — never an empty
        success.

        ``catalog_warning`` is DERIVED FROM THE AUDIT TRAIL on every poll,
        never carried through thread-local state. That is what makes it
        survive a process restart: a poller that reconnects sees the same
        unresolved publication the crashed process left behind, and a
        recorded success clears it.
        """
        states = self._state_store
        if states is None or self._metadata_store is None:
            raise IngestDurableCommitError(
                "job status needs a metadata pointer store and an ingest "
                "state store; at least one of them is not wired",
                stage="unconfigured",
            )
        job = states.get_job(job_id)
        pointer = self._known_pointer(job.document_id)
        if pointer is None:
            raise IngestDurableCommitError(
                f"job {job.document_id!r} has no durable object pointer",
                stage="pointer_read",
            )
        return IngestJobStatus(
            document_id=job.document_id,
            version=pointer.sha256,
            state=job.state,
            error_type=job.error_type,
            error_message=job.error_message,
            catalog_warning=publication_warning(
                states.list_audit(job.document_id)
            ),
            catalog_warning_count=self.catalog_warning_count(),
        )

    def catalog_warning_count(self) -> int:
        """How many DOCUMENTS have an unresolved publication.

        A COUNT of documents, not of attempts: one document retried five
        times is one warning, because the operator has one row to fix.
        """
        states = self._state_store
        if states is None:
            return 0
        count = 0
        for job in states.list_jobs():
            if publication_warning(states.list_audit(job.document_id)) is None:
                continue
            count += 1
        return count

    def run_durable(
        self,
        document_id: str,
        *,
        journal: PushJournal | None = None,
        actor: str | None = None,
    ) -> DurableRunOutcome:
        """Drive one accepted job through the state machine to ``ready``.

        This is the worker-side composition of :meth:`submit_durable`'s
        persistence and the synchronous chain: the raw bytes are read
        back through the durable pointer, the format is the one the
        accept recorded, and the existing transitions carry the run
        ``fetching -> extracting -> cataloging -> ready``. Nothing is
        re-uploaded and no second pipeline exists — :meth:`ingest` is
        the single splitter.

        The claim is a compare-and-set (``expected_state``): a
        ``pending`` or ``failed`` job is taken into ``fetching`` only by
        the worker whose CAS lands, so a concurrent worker for the same
        version gets the typed busy outcome instead of double-running.
        A job found mid-run (``fetching``/``extracting``/``ocr``/
        ``cataloging``) is interrupted work a restart must resume; a
        ``ready`` job is a no-op join.

        Any stage failure lands on the job as typed ``failed`` with
        ``error_type`` / ``error_message`` (``job_status`` reads it
        back); the ``failed -> fetching`` retry edge plus the per-
        document push journal keep the replay retryable and idempotent.
        ``ready`` is reached only after the whole chain settled, so a
        partial push is never reported complete. The completing run
        persists the authoritative ``content_identities`` map on the
        ``cataloging -> ready`` transition metadata; a restart whose
        journal skipped batches re-reads those pairs from the service
        through an idempotent re-push instead of handing out an empty
        map.

        Every busy the run's own transitions can raise — the claim CAS,
        an adoption move on a job found mid-run, or the landing edge —
        reports the typed busy outcome: a concurrent worker joins or
        gets busy, and a job another live worker owns is never failed.
        """
        objects, _pointers, states = self._durable_stores()
        claim = self._claim_for_run(states, document_id, actor=actor)
        if isinstance(claim, DurableRunOutcome):
            return claim
        version = claim.sha256
        try:
            fetched = self._fetch_for_run(
                objects, states, claim, document_id, actor=actor
            )
        except Exception as exc:
            return self._fail_or_join(
                states,
                document_id,
                step="fetch",
                cause=exc,
                actor=actor,
                version=version,
            )
        if isinstance(fetched, DurableRunOutcome):
            return fetched
        data, fmt = fetched
        try:
            return self._complete_and_land(
                states,
                document_id,
                version=version,
                data=data,
                declared_format=fmt,
                journal=journal,
                actor=actor,
            )
        except Exception as exc:
            return self._fail_or_join(
                states,
                document_id,
                step="ingest",
                cause=exc,
                actor=actor,
                version=version,
            )

    def _claim_for_run(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        actor: str | None,
    ) -> ObjectPointer | DurableRunOutcome:
        """Take the job for this run, or return the join outcome.

        A ``pending``/``failed`` job is claimed through the CAS into
        ``fetching``; the CAS loser reports the typed busy join and a
        ``ready`` job the plain no-op join. Any other state is
        interrupted work this run adopts. The accepted pointer is read
        up front because a join outcome still reports its version.
        """
        job = states.get_job(document_id)
        pointer = self._known_pointer(document_id)
        if pointer is None:
            raise IngestDurableCommitError(
                f"job {document_id!r} has no durable object pointer",
                stage="pointer_read",
            )
        version = pointer.sha256
        if job.state is IngestState.READY:
            return DurableRunOutcome(
                document_id=document_id,
                version=version,
                state=IngestState.READY,
                ran=False,
                busy=False,
            )
        if job.state in (IngestState.PENDING, IngestState.FAILED):
            try:
                states.transition(
                    document_id,
                    IngestState.FETCHING,
                    actor=actor,
                    expected_state=job.state,
                    metadata={"claim_version": version},
                )
            except IngestBusyError:
                return self._busy_outcome(states, document_id, version)
        return pointer

    def _busy_outcome(
        self,
        states: IngestStateStore,
        document_id: str,
        version: str,
    ) -> DurableRunOutcome:
        """The typed join while another live worker owns the job."""
        return DurableRunOutcome(
            document_id=document_id,
            version=version,
            state=states.get_job(document_id).state,
            ran=False,
            busy=True,
        )

    def _fetch_for_run(
        self,
        objects: ObjectStore,
        states: IngestStateStore,
        pointer: ObjectPointer,
        document_id: str,
        *,
        actor: str | None,
    ) -> tuple[bytes, str] | DurableRunOutcome:
        """Read the accepted bytes back and drive the job into extraction.

        The bytes must match the accepted digest and the pointer must
        name a supported format — either way out is a typed failure,
        never a parse of corrupt or mislabeled input. An adopted job
        can lose its adoption transition to a live holder that moves
        first; that busy is the join outcome, not a failure of the
        other worker's live job.
        """
        data = objects.get(pointer.key)
        if hashlib.sha256(data).hexdigest() != pointer.sha256:
            raise IngestDurableCommitError(
                f"the raw object for {document_id!r} no longer matches "
                "the digest of its accepted pointer",
                stage="pointer_read",
            )
        fmt = _CONTENT_TYPE_FORMATS.get(pointer.content_type or "")
        if fmt is None:
            raise IngestFormatUnspecifiedError(
                f"the accepted pointer for {document_id!r} records no "
                f"supported format (media type "
                f"{pointer.content_type!r})"
            )
        if states.get_job(document_id).state is IngestState.FETCHING:
            try:
                states.transition(
                    document_id,
                    IngestState.EXTRACTING,
                    actor=actor,
                )
            except IngestBusyError:
                return self._busy_outcome(
                    states, document_id, pointer.sha256
                )
        return data, fmt

    def _complete_and_land(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        data: bytes,
        declared_format: str,
        journal: PushJournal | None,
        actor: str | None,
    ) -> DurableRunOutcome:
        """Run the chain, publish the row, and land the job on ``ready``.

        The publication is a RECOVERABLE multi-step effect, so every step
        is durable before the next depends on it: push, then
        ``push_succeeded``, then ``catalog_publication_pending`` carrying
        the complete physical row, then the idempotent upsert, then
        ``catalog_publication_succeeded``, and only then the
        ``cataloging -> ready`` edge.

        The accepted metadata is read from the audit trail, never
        re-derived. When the accept persisted no row — the trusted
        explicit-ID replay leg, or a job accepted before this contract
        existed — the run keeps its own resolution and recovers the pair
        from the derived ``{TICKER}-{PERIOD}`` id it came from
        (:func:`split_company_period_from_document_id`), the pre-T6
        contract; the public upload always leaves a row, so nothing is
        ever guessed for it.

        A catalog failure is NOT a job failure. The typed failure is
        appended, the job stays ``cataloging`` — pushed and searchable,
        unpublished — and the run returns without claiming ``ready``.
        Recovery redoes the idempotent upsert from the durable pending
        row and records success, which is what clears the warning.

        A restart whose ``push_succeeded`` is already durable is the
        other recovery leg: the push completed once, so re-running the
        chain would deliver it a SECOND time. That run skips the push
        entirely and goes straight to the catalog upsert, from the
        ``catalog_row`` the pending event recorded and with the identity
        map that same event's push persisted
        (:meth:`_recover_publication`). When that recorded row is not
        there to be read, the durable proof still stands: the run
        reports a typed failure and the job stays ``cataloging`` rather
        than falling through to a chain that would push again.
        """
        recovered = self._recover_publication(
            states,
            document_id,
            version=version,
            actor=actor,
        )
        if recovered is not None:
            return recovered
        accepted = self._accepted_for_run(states, document_id)
        captured: list[IngestedDocument] = []

        def capture(document: IngestedDocument) -> None:
            """Take the row the run produced; publication happens below.

            The hook cannot run here: the row must be durable as redo
            input BEFORE the catalog is touched, and that ordering is
            exactly what this method owns.
            """
            captured.append(document)

        # The legacy leg keeps its own resolution: the id is DERIVED from
        # company+period, so splitting it back apart reads that encoding
        # rather than guessing (baseline contract). The content-identity
        # leg carries its accepted row instead and never splits. Split
        # once: the two halves are one decode of one string.
        legacy_company, legacy_period = (
            split_company_period_from_document_id(document_id)
            if accepted is None
            else (None, None)
        )
        result = self.ingest(
            data,
            declared_format=declared_format,
            document_id=document_id,
            journal=journal,
            company=legacy_company,
            period=legacy_period,
            content_identity=accepted is not None,
            title=accepted.title if accepted is not None else None,
            accepted=accepted,
            emit=capture,
        )
        identities = self._complete_content_identities(result)
        self._append_publication_event(
            states,
            document_id,
            PUSH_SUCCEEDED,
            push_succeeded_payload(version=version, content_identities=identities),
            actor=actor,
        )
        # Enter ``cataloging`` BEFORE the publication: that is the state
        # the job must hold while the row is being written, so a crash
        # or a failed upsert leaves it there — the state recovery acts
        # on. Transitioning afterwards would leave a failed publication
        # looking like an interrupted extraction.
        self._enter_cataloging(states, document_id, actor=actor)

        row = captured[0] if captured else None
        if row is None:
            # No ``emit`` was configured, so this run has no catalog row
            # to publish. The push identities are still durable; there is
            # no publication to record and no warning to raise.
            return self._land_ready(
                states, document_id, version, identities, result, actor=actor
            )
        return self._publish_and_land(
            states,
            document_id,
            version=version,
            row=row,
            identities=identities,
            result=result,
            actor=actor,
        )

    def _recover_publication(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        actor: str | None,
    ) -> DurableRunOutcome | None:
        """Retry ONLY the catalog upsert when the push is already durable.

        The spec's redo boundary: ``push_succeeded`` is the durable proof
        that every required envelope landed, so a run that finds one for
        this version has nothing left to push. Re-running the chain would
        deliver the same envelopes a second time — which the spec
        explicitly refuses to call a recovery — so this run goes straight
        to the upsert.

        Everything it needs is read back from the audit trail, never
        recomputed:

        * the row to upsert is the ``catalog_row`` of the LATEST
          ``catalog_publication_pending`` row. It is the row the
          accepted run produced; rebuilding one from the id would be the
          reconstruction the contract forbids, and would invent a
          ``company`` / ``period`` the document does not have;
        * the identity map on the ``cataloging -> ready`` edge is the one
          the completing push persisted, read off the same
          ``push_succeeded`` event. A pair is never invented here.

        ``None`` means this is NOT that recovery: the push is not
        durable, so there is no proof the envelopes landed and the
        caller runs the full chain — the pre-T6 behaviour, and the only
        correct answer when nothing is known.

        A durable push with NO usable redo row is a third answer, and
        deliberately not ``None``: the proof exists, so the chain must
        not run again, and the row is gone, so nothing can be upserted.
        Spec line 131 covers exactly this shape — "if reconciliation
        cannot prove the push result, keep the job unready and return a
        typed failure" — and the job must not be FAILED, because its
        push already landed and ``failed`` would route the retry edge
        back through a second delivery. The typed failure travels on the
        outcome; the job stays ``cataloging``.
        """
        rows = states.list_audit(document_id)
        if not read_push_succeeded(rows, version=version):
            return None
        pending = read_pending_publication(rows)
        if pending is None or pending.version != version:
            # The push IS durable but the redo input is not, so there is
            # no catalog row to write. Refusing to invent one is right,
            # but falling through to the full chain is not: the durable
            # ``push_succeeded`` above already proved every envelope
            # landed, and running the chain again would deliver them a
            # SECOND time — spec line 131 says a recovery retries only
            # the catalog upsert, never a second push. The row is not
            # recoverable by re-pushing, so the run reports the typed
            # failure line 131 asks for and leaves the job where the
            # next reconciliation can find it.
            return self._hold_cataloging(
                states,
                document_id,
                version=version,
                cause=IngestDurableCommitError(
                    f"the catalog publication for {document_id!r} is "
                    f"unrecoverable: push_succeeded is durable but no "
                    f"catalog_publication_pending row exists for version "
                    f"{version!r}, so there is no recorded catalog_row to "
                    f"upsert",
                    stage="publication_audit",
                ),
                step="recover_publication",
            )
        identities = read_push_identities(rows, version=version)
        # The job may still be before the publication edge (a crash
        # between the push record and the pending row's caller); the
        # publication owns entering cataloging, so make sure the state
        # recovery acts on is the one that holds the pending row.
        self._enter_cataloging(states, document_id, actor=actor)
        return self._publish_and_land(
            states,
            document_id,
            version=version,
            row=_ingested_document(
                doc_id=document_id,
                company=_row_optional(pending.catalog_row, "company"),
                period=_row_optional(pending.catalog_row, "period"),
                version=str(pending.catalog_row["version"]),
                page_count=int(pending.catalog_row["page_count"]),
                fmt="",
                doc_type=str(pending.catalog_row["doc_type"]),
                title=_row_optional(pending.catalog_row, "title"),
                content_identity=True,
            ),
            identities=identities,
            result=None,
            actor=actor,
        )

    def _accepted_for_run(
        self, states: IngestStateStore, document_id: str
    ) -> AcceptedUpload | None:
        """The accepted metadata for this run, from the audit trail only.

        The recorded ``doc_type`` is an OVERRIDE only when the uploader
        named one. An accept cannot classify — that needs the parse —
        so it records ``unknown`` for "no override", and feeding that
        back as an override would pin every document to ``unknown``
        instead of classifying it. The run's own classified type reaches
        the catalog on the pending-publication row, which is the row
        recovery upserts.

        ``None`` means the accept predates this contract and persisted
        nothing — the run then keeps its own resolution rather than
        refusing, because the pushed data still deserves a catalog row.
        """
        accepted = read_accepted_metadata(states.list_audit(document_id))
        if accepted is None:
            return None
        return AcceptedUpload(
            title=accepted.title,
            doc_type=(
                "" if accepted.doc_type == UNKNOWN_DOC_TYPE else accepted.doc_type
            ),
            company=accepted.company,
            period=accepted.period,
        )

    def _publish_and_land(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        row: IngestedDocument,
        identities: Mapping[str, str],
        result: IngestEndpointResult | None,
        actor: str | None,
    ) -> DurableRunOutcome:
        """Persist the pending row, upsert, record the terminal event.

        ``result`` is ``None`` on a recovery run, which has no live chain
        to report — its push is already durable — so the outcome carries
        no in-memory result rather than a fabricated one.

        The attempt number is the newest one already recorded plus one,
        so a retry names the attempt it is rather than reusing ``1`` and
        a poller reading ``attempts`` sees the real generation.
        """
        previous = read_pending_publication(states.list_audit(document_id))
        attempt = previous.attempt + 1 if previous is not None else 1
        catalog_row = _physical_catalog_row(row)
        self._append_publication_event(
            states,
            document_id,
            CATALOG_PUBLICATION_PENDING,
            pending_payload(
                version=version,
                document_id=document_id,
                attempt=attempt,
                catalog_row=catalog_row,
            ),
            actor=actor,
        )
        try:
            self._upsert_catalog_row(catalog_row)
        except Exception as exc:
            return self._record_publication_failure(
                states,
                document_id,
                version=version,
                attempt=attempt,
                cause=exc,
                actor=actor,
            )
        self._append_publication_event(
            states,
            document_id,
            CATALOG_PUBLICATION_SUCCEEDED,
            succeeded_payload(
                version=version, document_id=document_id, attempt=attempt
            ),
            actor=actor,
        )
        return self._land_ready(
            states, document_id, version, identities, result, actor=actor
        )

    def _upsert_catalog_row(self, catalog_row: Mapping[str, Any]) -> None:
        """Write one physical row through the endpoint's catalog hook.

        The hook IS the write: the host wires ``on_ingested`` to the
        catalog table, so a run without one has no catalog to write to
        and reports no publication rather than inventing one.
        """
        if self._on_ingested is None:
            return
        self._on_ingested(
            IngestedDocument(
                document_id=str(catalog_row["document_id"]),
                company=_row_optional(catalog_row, "company"),
                period=_row_optional(catalog_row, "period"),
                version=str(catalog_row["version"]),
                page_count=int(catalog_row["page_count"]),
                fmt="",
                doc_type=str(catalog_row["doc_type"]),
                title=str(catalog_row["title"]),
                source_sha256=_row_optional(catalog_row, "source_sha256"),
            )
        )

    def _record_publication_failure(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        attempt: int,
        cause: BaseException,
        actor: str | None,
    ) -> DurableRunOutcome:
        """Record a typed publication failure; keep the job in cataloging.

        The classification is what makes the warning actionable without
        leaking anything: a row that fails the row contract, a table
        that will not read, and a filesystem that will not cooperate are
        three different operator problems. An exception that is none of
        those is a bug and propagates through the existing typed job
        failure path instead of being filed as a catalog warning.
        """
        failure_code, failure_stage = classify_catalog_failure(cause)
        self._append_publication_event(
            states,
            document_id,
            CATALOG_PUBLICATION_FAILED,
            failed_payload(
                version=version,
                document_id=document_id,
                attempt=attempt,
                failure_code=failure_code,
                failure_stage=failure_stage,
            ),
            actor=actor,
        )
        logger.warning(
            "ingest: catalog publication failed for %s (%s/%s); the job "
            "stays cataloging and recovery retries the upsert",
            document_id,
            failure_code,
            failure_stage,
        )
        # The job keeps ``cataloging``: the push stands, the row does not.
        # Reporting a job failure here would be a lie about the pipeline
        # and would let a retry re-push what already landed.
        return DurableRunOutcome(
            document_id=document_id,
            version=version,
            state=IngestState.CATALOGING,
            ran=True,
            busy=False,
        )

    def _append_publication_event(
        self,
        states: IngestStateStore,
        document_id: str,
        action: str,
        metadata: Mapping[str, Any],
        *,
        actor: str | None,
    ) -> None:
        """Append one publication event, or refuse the step that needs it.

        A pending row that did not land means the redo input is not
        durable, so the upsert must not run: raising here is what stops
        a write no restart could repeat.
        """
        try:
            states.append_audit(document_id, action, metadata, actor=actor)
        except Exception as exc:
            raise IngestDurableCommitError(
                f"the publication event {action!r} for {document_id!r} could "
                f"not be recorded durably ({type(exc).__name__})",
                stage="publication_audit",
            ) from exc

    def _land_ready(
        self,
        states: IngestStateStore,
        document_id: str,
        version: str,
        identities: Mapping[str, str],
        result: IngestEndpointResult | None,
        *,
        actor: str | None,
    ) -> DurableRunOutcome:
        """Walk to ``ready`` once the publication is durable.

        A concurrent holder that landed first wins the CAS; this run's
        re-pushed chain was made harmless by the push's idempotency, and
        the busy join is the contract's typed outcome, not a failure.
        """
        try:
            self._drive_to_ready(states, document_id, identities, actor=actor)
        except IngestBusyError:
            return self._busy_outcome(states, document_id, version)
        return DurableRunOutcome(
            document_id=document_id,
            version=version,
            state=IngestState.READY,
            ran=True,
            busy=False,
            result=result,
        )

    def _fail_or_join(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        step: str,
        cause: BaseException,
        actor: str | None,
        version: str,
    ) -> DurableRunOutcome:
        """Land a run exception on the job as typed ``failed``.

        A job that reached ``ready`` while this run was in flight
        belongs to a holder that finished it, so the exception is
        re-raised rather than failing a completed generation.
        ``fail_step`` always raises its own typed step error (the
        store's return type is ``NoReturn``); the failed outcome is
        built from that error.

        ONE exception family is not a job failure, and only in the state
        that makes it one. A TERMINAL publication event that could not
        be appended is the spec's case: the job is already
        ``cataloging`` and its pending row is already durable, so the
        run is recoverable and recovery is exactly the redo those rows
        were written for. Moving the job to ``failed`` there would send
        the retry edge back through the fetch and the push of work that
        already landed. So the job stays where it is and the typed audit
        failure travels in the outcome instead of on the job.

        The scoping is the pair, not the exception alone: an append
        failure BEFORE the job owns a durable pending row (the
        ``push_succeeded`` event, and the pending row itself) has no
        redo input, so it is a genuine failure and lands ``failed`` as
        before — as does every parse, push and hook failure.
        """
        job = states.get_job(document_id)
        if job.state is IngestState.READY:
            raise cause
        if (
            job.state is IngestState.CATALOGING
            and _is_publication_audit_failure(cause)
        ):
            return self._hold_cataloging(
                states,
                document_id,
                version=version,
                cause=cause,
                step=step,
            )
        try:
            states.fail_step(
                document_id, step=step, cause=cause, actor=actor
            )
        except IngestStepError as step_error:
            return DurableRunOutcome(
                document_id=document_id,
                version=version,
                state=IngestState.FAILED,
                ran=True,
                busy=False,
                error=step_error,
            )

    def _hold_cataloging(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        version: str,
        cause: BaseException,
        step: str,
    ) -> DurableRunOutcome:
        """Report a recoverable publication failure as ``cataloging``.

        Nothing is written to the job: the pending row already carries
        the redo input, so the machine's own state — the state the
        warning is derived from — is the whole record of the failure.
        Writing a ``failed`` here would be the one thing that cannot be
        recovered, because it discards the state recovery acts on.

        The typed audit failure still travels, on the outcome's
        ``error``, in the same :class:`IngestStepError` shape the job's
        ``error_type`` / ``error_message`` would have carried had the
        failure been allowed to land. So a caller watching the outcome
        sees the refusal it needs to see; only the job's own claim that
        the whole run failed is withheld.
        """
        logger.warning(
            "ingest: the publication audit for %s could not be recorded "
            "(%s); the job stays cataloging and recovery redoes the "
            "upsert from the durable pending row",
            document_id,
            type(cause).__name__,
        )
        return DurableRunOutcome(
            document_id=document_id,
            version=version,
            state=IngestState.CATALOGING,
            ran=True,
            busy=False,
            error=IngestStepError(document_id, step, cause),
        )

    def _complete_content_identities(
        self, result: IngestEndpointResult
    ) -> dict[str, str]:
        """The full ``source_ref`` -> identity map for one finished run.

        A journal-skipped batch produces neither a push nor a pair (the
        pair came back with the run that completed it), so a resuming
        run re-pushes exactly those envelopes: the store recognizes the
        unchanged ``(source_ref, body_content_id)`` pair, writes
        nothing, and returns the stored identity. The identity stays the
        service's answer; it is never recomputed here.
        """
        identities = dict(result.content_identities)
        missing = tuple(
            envelope
            for envelope in result.envelopes
            if envelope.source_ref not in identities
        )
        if not missing:
            return identities
        repushed = push_in_batches(
            self._push_port, missing, journal=PushJournal()
        )
        identities.update(repushed.content_identities)
        return identities

    def _drive_to_ready(
        self,
        states: IngestStateStore,
        document_id: str,
        identities: Mapping[str, str],
        *,
        actor: str | None,
    ) -> None:
        """Walk the job from wherever the run left it to ``ready``.

        ``ocr`` is traversed only when a prior holder of the job entered
        it: this chain's parse has no separately observable OCR phase,
        so the machine's ``extracting -> cataloging`` skip edge is the
        honest path and no state is invented.
        """
        self._enter_cataloging(states, document_id, actor=actor)
        states.transition(
            document_id,
            IngestState.READY,
            actor=actor,
            metadata={"content_identities": dict(identities)},
        )

    def _enter_cataloging(
        self,
        states: IngestStateStore,
        document_id: str,
        *,
        actor: str | None,
    ) -> None:
        """Move an extracting (or OCR) job into ``cataloging``, once.

        A job already past the edge is left alone: the publication path
        enters ``cataloging`` before writing the row, so landing must
        not try to re-enter a state the job already holds.
        """
        current = states.get_job(document_id).state
        if current in (IngestState.EXTRACTING, IngestState.OCR):
            states.transition(
                document_id, IngestState.CATALOGING, actor=actor
            )

    def _invalidate_replaced(
        self, document_id: str, *, version: str
    ) -> InvalidationOutcome | None:
        """Close the blocks of the version this accept supersedes.

        The incoming version has produced no blocks yet — that is exactly what
        the accept door knows — so the stale set is every block the document
        pushed under a version it no longer points at. Without a record
        provider fin cannot name what is stale, which is the same documented
        no-op the synchronous chain has; with stale blocks and no invalidation
        port the failure is typed rather than accepted, because an accept that
        left the predecessor hittable would be the stale read this closes.
        """
        if self._previous_records is None:
            return None
        targets = plan_replacement_invalidations(
            self._previous_records(), document_id, version=version
        )
        if not targets:
            return None
        if self._invalidation_port is None:
            raise InvalidationServiceUnavailableError(
                f"replacing {document_id!r} would leave "
                f"{len(targets)} pushed blocks hittable and no invalidation "
                "port is configured"
            )
        return invalidate_targets(
            self._invalidation_port,
            targets,
            ledger=self._invalidation_ledger,
        )

    def _record_job(
        self,
        states: IngestStateStore,
        doc_id: str,
        *,
        version: str,
        superseded_version: str | None = None,
    ) -> IngestJob:
        """Reach a queued job record for this upload's version.

        A replacement does not transition the generation that completed:
        ``ready`` is terminal, so the store opens the document's next
        ``pending`` generation over the same job key and the completed
        generation stays in the audit trail.

        A job already in ``failed`` re-enters at ``fetching`` — the retry
        edge the state machine defines — instead of claiming a second
        ``pending`` job for the same document.

        A :class:`DuplicateJobError` here means another submission committed
        the job between the caller's read and this create. The submit lock
        makes that impossible within one process, so it can only be a
        cross-process competitor that already accepted these same bytes; the
        safe recovery is to join the committed job, never to raise — raising
        would make the caller delete the shared object the winner depends on.
        """
        if superseded_version is not None:
            return states.supersede_job(
                doc_id,
                metadata={"superseded_version": superseded_version},
            )
        try:
            return states.create_job(doc_id)
        except DuplicateJobError:
            job = states.get_job(doc_id)
            if job.state is IngestState.FAILED:
                states.transition(
                    doc_id,
                    IngestState.FETCHING,
                    metadata={"retry_version": version},
                )
                return states.get_job(doc_id)
            return job

    def _discard_rejected_upload(
        self,
        *,
        objects: ObjectStore,
        pointers: MetadataStore,
        key: str,
        document_id: str = "",
        restore: ObjectPointer | None = None,
        pointer_written: bool = True,
        cause: BaseException | None = None,
    ) -> None:
        """Undo the writes a rejected accept already made; never silently.

        A replacement that fails after its pointer overwrite puts the
        previous pointer back — otherwise the document would keep pointing
        at bytes that were never accepted. A first accept has no previous
        pointer, so the row a successful pointer write left is deleted
        instead; the delete is idempotent and scoped to the object key this
        attempt wrote, so a winner's row that names a different object is
        left alone (a same-bytes winner shares the key and its object). A
        failed pointer write leaves no confirmed row, so it skips this step.
        """
        try:
            objects.delete(key)
        except Exception as del_exc:
            # Both failures must stay recoverable: the persistence failure
            # that triggered the rollback (``cause``) is the reason this
            # object is being discarded at all, so it owns the exception
            # chain; the delete's own failure (``del_exc``) is named in the
            # message instead. When there is no triggering cause the delete
            # failure is the whole story and chains itself.
            chained = cause if cause is not None else del_exc
            raise IngestObjectCleanupError(
                f"the raw object {key!r} of a rejected upload is orphaned: "
                f"the compensating delete failed "
                f"({type(del_exc).__name__})"
                + (
                    f" after a persistence failure ({type(cause).__name__})"
                    if cause is not None
                    else ""
                ),
                key=key,
            ) from chained
        if pointer_written:
            try:
                if restore is not None:
                    pointers.put_pointer(document_id, restore)
                elif document_id:
                    pointers.delete_pointer(document_id, object_key=key)
            except Exception as exc:
                raise IngestObjectCleanupError(
                    f"the pointer of {document_id!r} still names the deleted object of a "
                    "rejected upload: the pointer could not be restored or deleted",
                    key=key,
                ) from exc

    def _known_pointer(self, document_id: str) -> ObjectPointer | None:
        """The document's durable pointer, or ``None`` when it has none."""
        if self._metadata_store is None:
            return None
        # Imported here because the pointer store module pulls in the
        # database driver, which must stay off this module's import surface.
        from fin.corpus.metadata_store import MetadataPointerNotFoundError

        try:
            return self._metadata_store.get_pointer(document_id).pointer
        except MetadataPointerNotFoundError:
            return None

    def _known_job(self, document_id: str) -> IngestJob | None:
        """The document's job, or ``None`` when nothing was ever accepted."""
        if self._state_store is None:
            return None
        try:
            return self._state_store.get_job(document_id)
        except UnknownJobError:
            return None


def _is_publication_audit_failure(cause: BaseException) -> bool:
    """Whether this run died appending a publication audit event.

    The durable-stage vocabulary is the discriminator, so the check
    names no action and cannot drift from the set
    :meth:`IngestEndpoint._append_publication_event` actually raises it
    for.
    """
    return (
        isinstance(cause, IngestDurableCommitError)
        and cause.stage == "publication_audit"
    )


__all__ = [
    "MAX_INGEST_BYTES",
    "PUBLIC_DOCUMENT_ID_REFUSAL",
    "DurableRunOutcome",
    "IngestConcurrentRunError",
    "IngestDocumentIdMissingError",
    "IngestDocumentIdNotAllowedError",
    "IngestDurableCommitError",
    "IngestDurableStage",
    "IngestEndpoint",
    "IngestEndpointError",
    "IngestEndpointResult",
    "IngestFormatUnspecifiedError",
    "IngestInvalidDocTypeError",
    "IngestInvalidUploadMetadataError",
    "IngestJobStatus",
    "IngestObjectCleanupError",
    "IngestSubmission",
    "IngestUploadTooLargeError",
    "IngestedDocument",
    "resolve_document_id",
    "resolve_format",
]
