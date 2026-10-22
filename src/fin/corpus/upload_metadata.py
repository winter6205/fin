"""Public corpus upload: accepted metadata and document identity.

The frozen contract (``specs/qa-chain-corpus-retrieval.md`` section
"Public corpus upload identity") splits an upload into independent
facts, and this module owns the ones the accept boundary decides:

* the **display** half — an explicit title, else the sanitized upload
  filename basename (:func:`sanitize_upload_title`);
* the **identity** half — the trusted corpus scope id plus the SHA-256
  of the uploaded bytes, and nothing else
  (:func:`derive_document_identity`).

The **type** half is resolved by the run, not here: an accepted
``doc_type`` override is used verbatim, and otherwise the endpoint's
deterministic per-unit classification rolls up across units (falling
back to the literal ``unknown`` when nothing is detectable), so the
catalog row's ``doc_type`` and the splitter's own type can never
disagree.

The split is the point. A filename is an operator's free-form string
that changes when someone renames a file, so it may decide what a
document is CALLED and never what it IS. The trusted scope id is host
configuration injected at the endpoint boundary; it never comes from a
metadata field, a filter, a filename, or a model argument. Display
metadata therefore cannot move an identity, which is what makes a retry
join its accepted job instead of creating a second catalog row.

Why these are pure functions
---------------------------
Every one is called before the first durable write, so a rejected
upload leaves nothing behind. That is only checkable if the decision is
separable from the store, and it is also why the metadata bounds here
are the SAME bounds the generic metadata filter uses (normalized
non-empty string, 1-256 UTF-8 bytes, no Unicode ``Cc`` control, no
``*`` / ``?``): one rule, so an uploaded title is exactly as
filterable as a catalog title.

This module sits in ``fin.corpus`` and imports nothing from ``fin.host``
— the scope id arrives as an argument, which is the whole point of the
dependency-direction rule.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

#: What an upload is called when nothing survives sanitization. A
#: constant rather than an empty title: the catalog row's ``title`` is
#: required non-empty, and a document the operator uploaded IS named by
#: something.
UNTITLED_DOCUMENT: Final[str] = "Untitled document"

#: The document-type value used when no type can be detected. A member
#: of the closed vocabulary (``fin.corpus.params.DOC_TYPES``) rather
#: than a sentinel outside it, so it resolves on the params table like
#: any other type and can be filtered as an exact value.
UNKNOWN_DOC_TYPE: Final[str] = "unknown"

#: The value bound for an accepted title / ``doc_type`` / ``company`` /
#: ``period``. 256 BYTES, the same cap the generic metadata filter
#: applies, so an accepted value is filterable by construction.
MAX_METADATA_BYTES: Final[int] = 256

#: The two filter wildcard characters. Never valid in accepted upload
#: metadata: filters are exact comparisons with no wildcard matching, so
#: a value carrying one would name something no filter can reach.
_WILDCARDS: Final[str] = "*?"

#: The domain-separation tag and separator of the identity preimage. The
#: tag means an identity digest can never collide with any other SHA-256
#: fin computes (a catalog row's ``version``, say), and the NUL
#: separators make the concatenation unambiguous.
_IDENTITY_DOMAIN: Final[bytes] = b"fin-corpus-document-v1\x00"
_IDENTITY_SEPARATOR: Final[bytes] = b"\x00"


class InvalidUploadMetadataError(RuntimeError):
    """Explicit upload metadata outside its accepted bounds.

    Named by the wire code ``invalid_upload_metadata`` and raised BEFORE
    any durable write: the accept door has not created an object, a
    pointer or a job, so a refused upload leaves nothing behind.

    Defined here rather than on the endpoint so the pure helpers that
    enforce the bounds need no endpoint import; ``fin.corpus.
    ingest_endpoint`` re-exports it under its own family name and the
    ingest error table dispatches it like every other named accept-time
    failure. It inherits ``RuntimeError`` (like every other
    ``IngestEndpointError``) rather than ``ValueError`` so a caller that
    only knows the endpoint family catches it.
    """

    code = "invalid_upload_metadata"

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field

    @property
    def stage(self) -> str:
        """The accept-time stage this fails in (mirrors the sibling
        durable-stage vocabulary so one table can dispatch it)."""
        return "metadata_validate"


@dataclass(frozen=True)
class DocumentIdentity:
    """The one identity an upload may have.

    ``version`` is the content's own digest and ``document_id`` is the
    scope-bound identity. Both are lowercase hex SHA-256; the pair is
    the catalog's upsert key, so the same bytes in the same trusted
    scope always land on the same row.
    """

    document_id: str
    version: str


def sanitize_upload_title(
    explicit: str | None, *, filename: str | None
) -> str:
    """The title an upload publishes under.

    An accepted explicit title wins; it is VALIDATED by
    :func:`resolve_explicit_metadata` (a control character, a wildcard
    or an over-256-byte value is refused, not edited), and a title that
    normalizes to nothing falls back to the filename rather than to the
    untitled constant — the operator did send a name.

    Otherwise the upload filename's BASENAME, SANITIZED: both ``/`` and
    ``\\`` are separators (a Windows upload arrives on a POSIX host
    unchanged), only the last component survives, then Unicode NFC
    normalization, Unicode ``Cc`` control removal, ``*`` / ``?``
    replacement with ``_``, trimming, and a truncation to
    :data:`MAX_METADATA_BYTES` **on a UTF-8 boundary** so the stored
    title always decodes. Anything that sanitizes to nothing becomes
    :data:`UNTITLED_DOCUMENT`.
    """
    if explicit is not None:
        accepted = resolve_explicit_metadata(explicit, field="title")
        if accepted:
            return accepted
    if filename:
        basename = _sanitize_value(_basename(filename))
        if basename:
            return basename
    return UNTITLED_DOCUMENT


def _basename(filename: str) -> str:
    """The final path component under EITHER separator style.

    ``re.split`` on a character class rather than two chained ``rsplit``
    calls: ``max(rsplit("/"), key=len)`` picks the LAST ``/``, not the
    one nearest the end of the string, so ``a/b\\c/d.md`` kept the wrong
    component when a Windows segment followed a POSIX one. Splitting on
    both characters at once has no such ordering to get wrong.
    """
    return re.split(r"[/\\]", filename)[-1]


def resolve_explicit_metadata(
    value: str | None, *, field: str
) -> str | None:
    """One explicit metadata field, validated and normalized — or absent.

    NFC-normalized and trimmed; a whitespace-only or ``None`` value is
    ABSENT (not ``""``, so a consumer can tell "the uploader said
    nothing" from "the uploader said the empty string").

    A present value is then checked against the SAME bounds the generic
    metadata filter applies — 1-256 UTF-8 bytes and no Unicode ``Cc``
    control — plus the filter wildcards ``*`` / ``?``. Every breach is
    the typed :class:`InvalidUploadMetadataError`, decided before any
    durable write, naming the FIELD and never the value so unvalidated
    caller text cannot reach a log or a response.

    Explicit metadata is VALIDATED, not sanitized, which is the one
    place it deliberately differs from the filename fallback: a
    filename is an operator's untrusted free-form string whose only job
    is to name a document, so stripping a control character out of it is
    a courtesy; an explicit field is a deliberate assertion the caller
    can retype, and silently editing what someone asserted is worse than
    refusing it.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidUploadMetadataError(
            f"upload metadata {field!r} must be a string", field=field
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        return None  # EXIT: blank means unsaid, so no lower layer is invented
    if any(char in _WILDCARDS for char in normalized):
        raise InvalidUploadMetadataError(
            f"upload metadata {field!r} must not contain a wildcard "
            "character",
            field=field,
        )
    if len(normalized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidUploadMetadataError(
            f"upload metadata {field!r} exceeds the {MAX_METADATA_BYTES}-"
            "byte bound",
            field=field,
        )
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise InvalidUploadMetadataError(
            f"upload metadata {field!r} must not contain a control "
            "character",
            field=field,
        )
    return normalized


def derive_document_identity(
    data: bytes,
    *,
    corpus_scope_id: str,
    filename: str | None = None,
) -> DocumentIdentity:
    """The one identity a public upload may have.

    ``version`` is ``lowercase_hex(SHA256(raw_bytes))``. ``document_id``
    is ``"doc_" + lowercase_hex(SHA256("fin-corpus-document-v1\\0" ||
    UTF8(scope_id) || 0x00 || raw_digest))`` — the spec's formula,
    literal.

    ``corpus_scope_id`` is trusted host configuration injected at the
    endpoint boundary. It is NEVER a filename, a metadata field, a
    filter value or a model argument.

    ``filename`` is accepted and IGNORED, and that is the contract
    rather than a leftover: it is here so the boundary states out loud
    that the filename was considered and rejected as an identity input,
    and so a caller passing one cannot believe it took part in the
    digest. Same bytes in the same scope reuse the identity on retry;
    the same bytes in two scopes are two documents; the same filename
    with different bytes is two documents.

    The domain tag is what keeps an identity digest distinct from every
    other SHA-256 fin computes, and the NUL separators make the
    concatenation unambiguous — a scope id of ``"ab"`` plus ``"\\0c"``
    cannot forge another scope's identity.
    """
    if not isinstance(corpus_scope_id, str) or not corpus_scope_id.strip():
        # EXIT: an unconfigured trusted scope would make every
        # unconfigured deployment mint ONE shared identity per byte
        # string, which is worse than refusing the upload.
        raise ValueError(
            "the trusted corpus scope id must be a non-empty string; a "
            "public upload's identity cannot be derived without it"
        )
    digest = hashlib.sha256(data).digest()
    preimage = (
        _IDENTITY_DOMAIN
        + corpus_scope_id.strip().encode("utf-8")
        + _IDENTITY_SEPARATOR
        + digest
    )
    return DocumentIdentity(
        document_id="doc_" + hashlib.sha256(preimage).hexdigest(),
        version=digest.hex(),
    )


def _sanitize_value(value: str | None) -> str | None:
    """NFC, control removal, wildcard replacement, trim, byte-capped.

    Returns ``None`` when nothing survives. The cap is applied on a
    UTF-8 boundary: cutting mid-character would store a title that
    cannot be decoded, searched or rendered, which is a worse outcome
    than a shorter title.
    """
    if not isinstance(value, str):
        return None
    cleaned = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        char for char in cleaned if unicodedata.category(char) != "Cc"
    )
    cleaned = "".join("_" if char in _WILDCARDS else char for char in cleaned)
    cleaned = cleaned.strip()
    return _truncate_utf8(cleaned, MAX_METADATA_BYTES)


def _truncate_utf8(text: str, max_bytes: int) -> str | None:
    """Cut to ``max_bytes`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut.strip() or None


__all__ = [
    "MAX_METADATA_BYTES",
    "UNKNOWN_DOC_TYPE",
    "UNTITLED_DOCUMENT",
    "DocumentIdentity",
    "InvalidUploadMetadataError",
    "derive_document_identity",
    "resolve_explicit_metadata",
    "sanitize_upload_title",
]
