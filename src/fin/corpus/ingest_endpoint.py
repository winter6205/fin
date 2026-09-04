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
4. **入库推送** — :func:`fin.corpus.push.document_push_records` turns
   the children into push records; :func:`fin.retrieve.push_batching.
   push_in_batches` pushes them batch by batch through the
   ``PushPort`` (batch cap honored, resumable journal). Push failures
   are the named ``PushError`` family and leave nothing recorded as a
   write (SC8).
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
from typing import Callable, Final, Sequence

from fin.corpus.chunking import (
    DocumentChunking,
    split_document,
    split_unit,
)
from fin.corpus.doc_type import classify_page
from fin.corpus.errors import UnsupportedFormatParseError
from fin.corpus.invalidation import VersionChange, plan_invalidations
from fin.corpus.params import DOC_TYPES
from fin.corpus.parsers import SUPPORTED_FORMATS, ParseResult, parse_document
from fin.corpus.push import PushRecord, document_push_records
from fin.retrieve.invalidation_port.errors import (
    InvalidationOutcome,
    InvalidationPort,
    InvalidationServiceUnavailableError,
)
from fin.retrieve.invalidation_port.operation import invalidate_targets
from fin.retrieve.invalidation_port.ledger import InvalidationLedger
from fin.retrieve.push_batching import PushJournal, push_in_batches
from fin.retrieve.push_port import PushPort

# Overflow class (§B2 第 5 条 / test allocation): the largest upload the
# endpoint accepts. Oversize is a named 413-class failure, never a
# truncated parse of a prefix. The value is headroom, not contract.
MAX_INGEST_BYTES: Final[int] = 50 * 1024 * 1024

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

    ``block_ids`` is parallel to the children pushed; ``written`` /
    ``duplicates`` come from the push outcome (SC18 idempotency
    accounting). ``invalidation`` is ``None`` when nothing was stale;
    ``records`` is the full push-record set so callers (tests, the
    future catalog seam) can hand it back as ``previous_records``.
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
    filename: str | None,
) -> str:
    """Resolve the locator's ``document_id`` deterministically.

    Precedence: explicit ``document_id``, then ``company`` + ``period``
    joined with ``-`` (omitting empty parts), then the filename stem.
    Nothing left is the named ``document_id_missing`` failure — the
    locator triple cannot be built without it.
    """
    if document_id and document_id.strip():
        return document_id.strip()
    parts = [part.strip() for part in (company, period) if part and part.strip()]
    if parts:
        return "-".join(parts)
    if filename:
        stem = Path(filename).stem.strip()
        if stem:
            return stem
    raise IngestDocumentIdMissingError(
        "no document_id was supplied and none is derivable from "
        "company/period metadata or the filename"
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

    @property
    def push_port(self) -> PushPort:
        """The push port behind this endpoint (diagnostics / tests)."""
        return self._push_port

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
            filename=filename,
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
        records = document_push_records(chunking)

        push_outcome = push_in_batches(
            self._push_port,
            records,
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
            block_count=len(records),
            block_ids=tuple(record.block_id for record in records),
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


__all__ = [
    "MAX_INGEST_BYTES",
    "IngestDocumentIdMissingError",
    "IngestEndpoint",
    "IngestEndpointError",
    "IngestEndpointResult",
    "IngestFormatUnspecifiedError",
    "IngestInvalidDocTypeError",
    "IngestUploadTooLargeError",
    "IngestedDocument",
    "resolve_document_id",
    "resolve_format",
]
