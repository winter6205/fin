"""The corpus ingest endpoint service (corpus-serve-ingest, ADR 0047).

This module owns three ingest entry points on one endpoint:

* **durable accept** — :meth:`IngestEndpoint.submit_durable` is the HTTP
  door: it makes the raw object, its metadata pointer, a queued job
  record and the ``(document_id, version)`` identity durable, then
  reports the upload accepted-and-queued WITHOUT parsing, chunking or
  pushing, since those run off the request. :meth:`IngestEndpoint.job_status`
  reads back what the machine has persisted.
* **the synchronous chain** — :meth:`IngestEndpoint.ingest` is the
  programmatic path that parses, chunks and pushes in one call. This is
  the entry where company documents reach the retrieval backend, and the
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

The HTTP surface (``fin.host.workspace_http``) and a future CLI both
construct :class:`IngestEndpoint` with their own ports; this module
never reads the environment and never imports FastAPI.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Final,
    Literal,
    Mapping,
    Sequence,
)

from fin.corpus.chunking import (
    DocumentChunking,
    split_document,
    split_unit,
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


class IngestInvalidDocTypeError(IngestEndpointError):
    """An explicit ``doc_type`` outside the params-table vocabulary.

    The table's document-type axis is a closed vocabulary
    (:data:`fin.corpus.params.DOC_TYPES`); an unknown value cannot
    silently land on the global default row because the caller clearly
    meant a specific scene.
    """

    code = "invalid_doc_type"


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
    """The compensating delete of a rejected upload's raw object failed.

    The orphan object is the residue of a persistence failure, so losing
    it silently would leave an unaudited write; the original failure is
    kept as ``__cause__`` and this one names the key.
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
    """

    document_id: str
    company: str | None
    period: str | None
    version: str
    page_count: int
    fmt: str
    doc_type: str


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
    """

    document_id: str
    version: str
    state: IngestState
    error_type: str | None
    error_message: str | None

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
        "no document_id was supplied and none is derivable: both company "
        "and period are required (e.g. company='aapl', period='2022q3' -> "
        "'AAPL-2022Q3')"
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
    the ticker test rather than being read as company ``0000320193``.

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
    ) -> tuple[str, str, str | None, str | None]:
        """Run every pre-parse named validation; resolve the locator.

        The failure classes that must never reach a parse or a push
        are decided HERE, in order: upload overflow
        (``ingest_upload_too_large``, checked on the raw bytes before
        anything else), format recognition (``unsupported_format`` /
        ``format_unspecified``), document-id derivation
        (``document_id_missing``) and the doc_type vocabulary
        (``invalid_doc_type``).

        Returns ``(fmt, doc_id, company, period)`` with the metadata
        fields normalized (``None`` when blank) so the whole chain —
        including the ``on_ingested`` row — shares one value.
        """
        if self._max_bytes > 0 and len(data) > self._max_bytes:
            raise IngestUploadTooLargeError(
                f"upload of {len(data)} bytes exceeds the ingest cap of "
                f"{self._max_bytes} bytes"
            )

        fmt = resolve_format(
            filename=filename, declared_format=declared_format
        )
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
    ) -> IngestEndpointResult:
        """Run the full chain on one upload (see module docstring).

        Every failure is typed: the endpoint families above, the
        parser's ``ParseError`` family, the push ``PushError`` family
        and the invalidation ``InvalidationError`` family. A failure
        before the push writes nothing; a failure after it (invalidation,
        hook) leaves the idempotent push in place and is the caller's
        signal to retry. The pre-parse validations live in
        :meth:`_validate_upload`.

        ``journal`` overrides the endpoint's push journal for this run
        only — the durable worker hands in a per-document journal whose
        completed batches survive a restart, so a replay skips exactly
        the batches that already landed.
        """
        fmt, doc_id, company, period = self._validate_upload(
            data,
            filename=filename,
            declared_format=declared_format,
            document_id=document_id,
            company=company,
            period=period,
            doc_type=doc_type,
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

        if self._on_ingested is not None:
            self._on_ingested(
                IngestedDocument(
                    document_id=doc_id,
                    company=company,
                    period=period,
                    version=version,
                    page_count=len(parsed.units),
                    fmt=fmt,
                    doc_type=resolved_doc_type,
                )
            )

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
    ) -> IngestSubmission:
        """Persist one upload durably and report the job that owns it.

        This is the HTTP ingest contract: nothing is parsed, chunked or
        pushed here. The upload is accepted only once all four
        accept-time items are durable — the raw object, the metadata
        pointer, the job record and the ``(document_id, version)``
        identity — and every validation that needs no bytes (size, format,
        identity, doc_type) is decided BEFORE the first write, so a
        rejected upload leaves nothing behind.

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
        fmt, doc_id, _company, _period = self._validate_upload(
            data,
            filename=filename,
            declared_format=declared_format,
            document_id=document_id,
            company=company,
            period=period,
            doc_type=doc_type,
        )
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
        superseded_version: str | None = None,
    ) -> IngestJob:
        """Write the object, pointer and job in order, compensating on failure.

        Nothing is reported accepted until all three are durable. A failure at
        any step deletes the object written this attempt (and restores the
        overwritten pointer), so a rejected upload leaves the document exactly
        as it was; a compensation that itself fails is the typed cleanup error.

        A replacement adds one step before the pointer moves: the blocks of
        the version being superseded are closed through the invalidation port.
        It runs FIRST because it is the one accept-time side effect fin cannot
        undo here — a failure must still leave the superseded version's
        pointer and ``ready`` job untouched, and a retry converges because the
        invalidation request is idempotent.
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
                cause=exc,
            )
            raise IngestDurableCommitError(
                f"the metadata pointer for {doc_id!r} could not be written "
                f"({type(exc).__name__})",
                stage="pointer_write",
            ) from exc

        try:
            return self._record_job(
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

    def job_status(self, job_id: str) -> IngestJobStatus:
        """Report the persisted state of one ingest job.

        ``job_id`` is the state store's key, i.e. the ``document_id``. The
        version is read back from the durable pointer, never recomputed from
        a re-supplied upload, so a poller sees what was accepted. An unknown
        id stays the store's typed :class:`UnknownJobError` — never an empty
        success.
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
        )

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
        """Run the chain and land the finished run on the job.

        The authoritative push identities are completed before the
        ``cataloging -> ready`` edge carries the job to ``ready``.
        """
        # The durable accept persisted the bytes and the identity but not
        # company/period (no columns exist for them), so they are recovered
        # from the id `resolve_document_id` derived from them. Without this
        # the catalog hook receives company=None and every DERIVED-identity
        # upload is pushed-but-invisible: searchable in the store, absent
        # from fin's own catalog. The explicit-id branch of Decision 1.1 is
        # not served here — see the splitter's KNOWN LIMIT.
        recovered_company, recovered_period = (
            split_company_period_from_document_id(document_id)
        )
        result = self.ingest(
            data,
            declared_format=declared_format,
            document_id=document_id,
            company=recovered_company,
            period=recovered_period,
            journal=journal,
        )
        identities = self._complete_content_identities(result)
        try:
            self._drive_to_ready(
                states, document_id, identities, actor=actor
            )
        except IngestBusyError:
            # A concurrent holder landed the job while this run was
            # in flight: its re-ran chain was made harmless by the
            # push's idempotency, and the landing edge is the store's
            # CAS that says which run owns the ``ready`` generation.
            # The join is the contract's typed outcome, not a failure.
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
        """
        if states.get_job(document_id).state is IngestState.READY:
            raise cause
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
        current = states.get_job(document_id).state
        if current in (IngestState.EXTRACTING, IngestState.OCR):
            states.transition(
                document_id, IngestState.CATALOGING, actor=actor
            )
        states.transition(
            document_id,
            IngestState.READY,
            actor=actor,
            metadata={"content_identities": dict(identities)},
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
        cause: BaseException | None = None,
    ) -> None:
        """Undo the writes a rejected accept already made; never silently.

        The pointer store has no delete, so a replacement that fails after
        its pointer overwrite puts the previous pointer back — otherwise the
        document would keep pointing at bytes that were never accepted.
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
        if restore is not None:
            try:
                pointers.put_pointer(document_id, restore)
            except Exception as exc:
                raise IngestObjectCleanupError(
                    f"the pointer of {document_id!r} still names the deleted "
                    "object of a rejected upload: the prior pointer could not "
                    "be restored",
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


__all__ = [
    "MAX_INGEST_BYTES",
    "DurableRunOutcome",
    "IngestConcurrentRunError",
    "IngestDocumentIdMissingError",
    "IngestDurableCommitError",
    "IngestDurableStage",
    "IngestEndpoint",
    "IngestEndpointError",
    "IngestEndpointResult",
    "IngestFormatUnspecifiedError",
    "IngestInvalidDocTypeError",
    "IngestJobStatus",
    "IngestObjectCleanupError",
    "IngestSubmission",
    "IngestUploadTooLargeError",
    "IngestedDocument",
    "resolve_document_id",
    "resolve_format",
]
