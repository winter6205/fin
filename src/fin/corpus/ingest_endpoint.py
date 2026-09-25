"""The corpus ingest endpoint service (corpus-serve-ingest T3, ADR 0047).

This is the ONE product entry where company documents are parsed and
chunked for storage on the retrieval backend. The chain is fully
deterministic — no model participates in any split decision:

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
   succeeded, carrying the row contract T2's catalog registration
   (``record_filings``) consumes: ``{document_id, company, period,
   version, page_count}``. The hook's own failures propagate typed.

The HTTP surface (``fin.host.workspace_http``) and a future CLI both
construct :class:`IngestEndpoint` with their own ports; this module
never reads the environment and never imports FastAPI.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Final, Sequence

from fin.corpus.chunking import (
    DocumentChunking,
    split_document,
    split_unit,
)
from fin.corpus.doc_type import classify_page
from fin.corpus.errors import UnsupportedFormatParseError
from fin.corpus.ingest_state import IngestJob, IngestState
from fin.corpus.ingest_state_errors import DuplicateJobError, UnknownJobError
from fin.corpus.invalidation import VersionChange, plan_invalidations
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
from fin.retrieve.push_port import PushPort

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fin.corpus.ingest_state import IngestStateStore
    from fin.corpus.metadata_store import MetadataStore
    from fin.corpus.object_store import ObjectStore

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


class IngestReplacementUnsupportedError(IngestEndpointError):
    """A ready document was re-submitted with different bytes.

    That is the replacement case, and closing it needs the invalidation
    side effect the state machine cannot express: ``ready`` is terminal, so
    the accepted upload's chunks could never be produced. Refusing it is a
    correctness gate, not a missing feature flag — until the state store
    gains a re-queue edge for replacements.
    """

    code = "replacement_unsupported"


class IngestDurableCommitError(IngestEndpointError):
    """An accept-time durable item could not be persisted.

    No job is reported as accepted unless the raw object, the metadata
    pointer and the ``pending`` job record all landed, so every storage
    failure surfaces typed with the :attr:`stage` that failed.
    """

    code = "durable_commit_failed"

    def __init__(self, message: str, *, stage: str) -> None:
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
    """What one successful ingest stored — the T2 catalog row contract.

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
    idempotency accounting). ``invalidation`` is ``None`` when
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
    ) -> IngestEndpointResult:
        """Run the full chain on one upload (see module docstring).

        Every failure is typed: the endpoint families above, the
        parser's ``ParseError`` family, the push ``PushError`` family
        and the invalidation ``InvalidationError`` family. A failure
        before the push writes nothing; a failure after it (invalidation,
        hook) leaves the idempotent push in place and is the caller's
        signal to retry. The pre-parse validations live in
        :meth:`_validate_upload`.
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
            journal=(
                self._journal if self._journal is not None else PushJournal()
            ),
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
        the state machine already defines; a ``ready`` job has no outgoing
        edge at all, so replacing it is the typed
        :class:`IngestReplacementUnsupportedError` with nothing written,
        never a pretend accept.

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

        version = hashlib.sha256(data).hexdigest()
        # The key is identity-derived, so re-submitting the same bytes can
        # never land a second object and a different filename cannot name a
        # second copy of one version.
        key = f"filings/{doc_id}/{version}"
        prior_pointer = self._known_pointer(doc_id)
        prior_job = self._known_job(doc_id)

        if prior_pointer is not None and prior_pointer.sha256 == version:
            # Duplicate: join the job that owns this version instead of
            # writing a second object, job row or logical version.
            if prior_job is not None:
                return IngestSubmission(
                    document_id=doc_id,
                    version=version,
                    state=prior_job.state,
                )

        if prior_job is not None and prior_job.state is not IngestState.FAILED:
            if prior_job.state is IngestState.READY:
                raise IngestReplacementUnsupportedError(
                    f"{doc_id!r} is ready on another version and the upload "
                    "is not those bytes: the state machine has no edge out of "
                    "'ready', so the replacement cannot be accepted yet"
                )
            raise IngestConcurrentRunError(
                f"a job for {doc_id!r} is already in state "
                f"{prior_job.state.value!r}; a different version is not "
                "accepted while it is in flight"
            )

        try:
            pointer = objects.put(
                key, data, content_type=_FORMAT_CONTENT_TYPES[fmt]
            )
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
            job = self._record_job(states, doc_id, version=version)
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

        return IngestSubmission(
            document_id=doc_id, version=version, state=job.state
        )

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

    def _record_job(
        self, states: IngestStateStore, doc_id: str, *, version: str
    ) -> IngestJob:
        """Reach a queued job record for this upload's version.

        A job already in ``failed`` re-enters at ``fetching`` — the retry
        edge the state machine defines — instead of claiming a second
        ``pending`` job for the same document.
        """
        try:
            return states.create_job(doc_id)
        except DuplicateJobError:
            job = states.get_job(doc_id)
            if job.state is not IngestState.FAILED:
                raise
            states.transition(
                doc_id,
                IngestState.FETCHING,
                metadata={"replaced_version": version},
            )
            return states.get_job(doc_id)

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
        except Exception:
            # ``from cause`` keeps the persistence failure that triggered
            # the rollback as the cause; the delete's own failure stays as
            # the implicit context, so both are auditable.
            raise IngestObjectCleanupError(
                f"the raw object {key!r} of a rejected upload is orphaned: "
                "the compensating delete failed after a persistence failure",
                key=key,
            ) from cause
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
    "IngestConcurrentRunError",
    "IngestDocumentIdMissingError",
    "IngestDurableCommitError",
    "IngestEndpoint",
    "IngestEndpointError",
    "IngestEndpointResult",
    "IngestFormatUnspecifiedError",
    "IngestInvalidDocTypeError",
    "IngestJobStatus",
    "IngestObjectCleanupError",
    "IngestReplacementUnsupportedError",
    "IngestSubmission",
    "IngestUploadTooLargeError",
    "IngestedDocument",
    "resolve_document_id",
    "resolve_format",
]
