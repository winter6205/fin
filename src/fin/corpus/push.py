"""Push records: `source_ref`, content identity and the idempotency key
(plan T14, SC18 / SC20, §B4).

This module is the corpus-side half of T14: it turns the T11 chunking
products (`ChunkPayload`) into the records the retrieval service is
pushed. The three §B4 contract items it fixes:

`source_ref`
    Spelled verbatim ``source_ref``. Its content is the **stable
    serialization** of the fin locator triple ``{document_id, version,
    unit}``, and its granularity is the **frozen unit** — every block
    of one unit carries the same value. It is *not* block identity:
    block ids come from :func:`fin.corpus.chunking` (a deterministic
    sha256 over kind / locator / ordinal / text / the three rule
    versions), and a `source_ref` is never accepted as a block id.

content identity
    The deterministic sha256 of (block text + normalization rule
    version + parse rule version + params table version). It is
    content-scoped, not unit-scoped: the same text in two frozen units
    has the same content identity.

idempotency key
    The ``(source_ref, content identity)`` pair. Two records with the
    same key are the same write; the retrieval side must not produce a
    second copy and must return the same block ids (see
    :mod:`fin.retrieve.push_port`).

Serialization stability reuses the canonical JSON of
:mod:`fin.corpus.determinism` (``sort_keys``, no spaces, UTF-8), so
the `source_ref` bytes cannot drift with dict insertion order.

The SC20 metadata surface (format, doc type, section, source hash,
parse rule version, params table version) rides along in
:attr:`PushRecord.metadata`; :data:`REQUIRED_PUSH_METADATA_FIELDS` is
its single authority, consumed by the push adapter and by the contract
fake so the required-field list cannot fork into two copies.

One envelope per 资料 (plan `operator-console-host-contract` T1)
-----------------------------------------------------------------

After **切分**, the ingest endpoint pushes **one envelope per unit**
(spec `operator-console-host-contract`): `source_ref` + the unit's
extracted body + `chunks[]` of ``{content, index}``. The
:class:`PushEnvelope` dataclass is the wire shape and the carrier of
the unit-level content identity used as the idempotency key on the
wire. Per-child :class:`PushRecord` rows remain — they are what the
invalidation path keys on (see :mod:`fin.corpus.invalidation`) — but
they no longer ride the wire one-per-block.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence

from fin.corpus.chunking import ChunkPayload, DocumentChunking, UnitChunking
from fin.corpus.determinism import canonical_json_bytes
from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
from fin.corpus.parsers._common import UnitLocator

# SC20: the structured metadata the retrieval side consumes as filter
# conditions. Single authority — the push adapter and the contract
# fake both import this tuple instead of restating the list.
REQUIRED_PUSH_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "fmt",  # 格式
    "doc_type",  # 文档类型
    "section",  # 板块
    "source_sha256",  # 来源哈希
    "parse_rule_version",  # 解析规则版本
    "params_table_version",  # 参数表版本
)

_ID_SEPARATOR: Final[str] = "\x1f"


def serialize_source_ref(locator: UnitLocator) -> str:
    """The stable serialization of one locator triple (§B4).

    Canonical JSON of ``{document_id, version, unit}``: key order is
    sorted, so the value is a pure function of the triple. Frozen-unit
    granularity is structural — the triple has no block component, so
    every block of the unit necessarily gets the same string.
    """
    return canonical_json_bytes(locator.to_dict()).decode("utf-8")


def deserialize_source_ref(source_ref: str) -> UnitLocator:
    """Parse one canonical ``source_ref`` back into its locator triple.

    This is the inverse of :func:`serialize_source_ref` and the single
    authority for the wire representation.  Rejecting non-canonical JSON
    prevents the same frozen unit from acquiring multiple reference strings.
    """
    try:
        payload = json.loads(source_ref)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_ref is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "document_id",
        "version",
        "unit",
    }:
        raise ValueError(
            "source_ref must contain exactly document_id, version, and unit"
        )
    document_id = payload["document_id"]
    version = payload["version"]
    unit = payload["unit"]
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(version, str)
        or not version
        or isinstance(unit, bool)
        or not isinstance(unit, int)
        or unit < 1
    ):
        raise ValueError("source_ref contains an invalid locator value")
    locator = UnitLocator(
        document_id=document_id,
        version=version,
        unit=unit,
    )
    if source_ref != serialize_source_ref(locator):
        raise ValueError("source_ref is not canonical JSON")
    return locator


def content_identity(
    text: str,
    *,
    normalization_rule_version: str,
    parse_rule_version: str,
    params_table_version: str,
) -> str:
    """The content identity of one block (§B4): sha256 over the block
    text and the three rule versions, joined by ``\\x1f`` so no field
    value can impersonate a field boundary.

    This is also the SSOT for the wire-level ``body_content_id`` hash
    :func:`body_content_id` computes over an envelope's body — the
    underlying hashing construction is one function with one
    separator and one encoding, so the two can never drift.
    """
    parts = (
        text,
        normalization_rule_version,
        parse_rule_version,
        params_table_version,
    )
    return hashlib.sha256(_ID_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


def body_content_id(envelope: Mapping[str, Any]) -> str:
    """The wire-level body content identity of one envelope (§B4 / T1).

    Single authority for the ``body_content_id`` hash used as the
    envelope-level idempotency key component on the wire. The
    envelope may carry an explicit ``body_content_id`` (the corpus
    side pre-computes it via :func:`content_identity`); when it does,
    we trust the pre-computed value so a wire envelope that already
    pinned the id keys identically on both sides. Otherwise we derive
    the same hash from the envelope body + the three SC20 rule
    versions so a hand-rolled wire envelope that omits
    ``body_content_id`` still hashes identically.

    The push adapter (:mod:`fin.retrieve.mcp_push_port`), the
    contract fake (:mod:`fin.retrieve.fake_push_port`) and the
    batching path (:mod:`fin.retrieve.push_batching`) all funnel
    through this function — there is exactly one body-hashing
    construction in fin, and adding a fourth copy would let the
    envelope / adapter / fake diverge.
    """
    explicit = envelope.get("body_content_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    body = envelope.get("content")
    if body is None:
        body = envelope.get("body")
    metadata = envelope.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return content_identity(
        "" if body is None else str(body),
        normalization_rule_version=str(
            metadata.get("normalization_rule_version", "")
        ),
        parse_rule_version=str(metadata.get("parse_rule_version", "")),
        params_table_version=str(metadata.get("params_table_version", "")),
    )


def idempotency_keys_digest(keys: Sequence[tuple[str, str]]) -> str:
    """The deterministic digest of an ordered set of idempotency keys.

    One hashing construction for every "which writes does this request
    cover" question: the canonical JSON of the ``(source_ref, content
    identity)`` pairs, hashed with sha256. The batch key of
    :mod:`fin.retrieve.push_batching` and the invalidation request key of
    :mod:`fin.corpus.invalidation` both go through here rather than
    growing a second scheme.
    """
    payload = [list(key) for key in keys]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class PushRecord:
    """One child block as pushed to the retrieval service.

    ``block_id`` is the fin-side deterministic hash (the block
    identity); ``source_ref`` is the frozen-unit reference shared by
    every block of the unit; ``content_id`` is the content identity.
    The idempotency key is the pair of the latter two — deliberately
    *not* the block id, so a block whose text and rule versions are
    unchanged is recognized as the same write.
    """

    block_id: str
    source_ref: str
    content_id: str
    text: str
    parent_block_id: str
    parent_text: str
    metadata: Mapping[str, Any]

    @property
    def idempotency_key(self) -> tuple[str, str]:
        """§B4: the ``(source_ref, content identity)`` pair."""
        return (self.source_ref, self.content_id)

    def to_wire(self) -> dict[str, Any]:
        """The wire shape. Field names are verbatim §B4 / SC20; the
        metadata mapping is copied so the caller's mutation of the wire
        dict cannot reach back into the record."""
        return {
            "block_id": self.block_id,
            "source_ref": self.source_ref,
            "content_id": self.content_id,
            "idempotency_key": list(self.idempotency_key),
            "text": self.text,
            "parent_block_id": self.parent_block_id,
            "parent_text": self.parent_text,
            "metadata": dict(self.metadata),
        }

    def without_metadata_field(self, field: str) -> "PushRecord":
        """A copy with one metadata field dropped — the scripted shape
        of an incomplete push (SC20's rejection path is asserted with
        it; production records always carry the full set)."""
        remaining = {
            key: value for key, value in self.metadata.items() if key != field
        }
        return replace(self, metadata=MappingProxyType(remaining))


def record_of_payload(payload: ChunkPayload) -> PushRecord:
    """Build one push record from one child block's push payload."""
    return PushRecord(
        block_id=payload.block_id,
        source_ref=serialize_source_ref(payload.locator),
        content_id=content_identity(
            payload.text,
            normalization_rule_version=payload.normalization_rule_version,
            parse_rule_version=payload.parse_rule_version,
            params_table_version=payload.params_table_version,
        ),
        text=payload.text,
        parent_block_id=payload.parent_block_id,
        parent_text=payload.parent_text,
        metadata=MappingProxyType(
            {
                "fmt": payload.fmt,
                "doc_type": payload.doc_type,
                "section": payload.section,
                "source_sha256": payload.source_sha256,
                "parse_rule_version": payload.parse_rule_version,
                "params_table_version": payload.params_table_version,
                # beyond the SC20 minimum, kept for the retrieval
                # side's filter surface and the locator round-trip
                "density": payload.density,
                "normalization_rule_version": (
                    payload.normalization_rule_version
                ),
                "locator": dict(payload.locator.to_dict()),
            }
        ),
    )


def unit_push_records(unit: UnitChunking) -> tuple[PushRecord, ...]:
    """The push records of one frozen unit — children only (SC16: the
    hit set is the child blocks; parents ride along as
    ``parent_text``)."""
    return tuple(record_of_payload(payload) for payload in unit.payloads())


def document_push_records(document: DocumentChunking) -> tuple[PushRecord, ...]:
    """The push records of a whole document, in unit order."""
    return tuple(record_of_payload(payload) for payload in document.payloads())


# ---------------------------------------------------------------------------
# One envelope per 资料 — operator-console-host-contract T1
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushChunk:
    """One slice inside a :class:`PushEnvelope`.

    ``index`` is the 0-based monotonic position of the slice inside the
    envelope (``0, 1, ..., N-1``); ``content`` is the slice text, a
    contiguous substring of the envelope body (SC11 / SC16「连续子串」).
    ``id`` and ``content_id`` are propagated from the per-child
    :class:`ChunkPayload` when the envelope is built by
    :func:`envelope_of_unit`, so the catalog side keeps pinning the
    same block id / content identity it had pre-T1 and the
    invalidation path's per-child keys resolve on the receiving side.
    """

    index: int
    content: str
    id: str | None = None
    content_id: str | None = None


@dataclass(frozen=True)
class PushEnvelope:
    """The wire shape of one 资料 push (spec `operator-console-host-contract`).

    One envelope per frozen unit: ``source_ref`` (the unit locator
    triple), the unit's full extracted text as the ``body``, and the
    child slices as ``chunks`` with monotonic ``index`` starting at 0.
    The metadata carries the SC20 filter surface; ``valid_from`` /
    ``valid_until`` ride along for the retrieval side's interval
    tracking.

    The idempotency key on the wire is
    ``(source_ref, body_content_id)`` — ``body_content_id`` is the
    deterministic sha256 of (body text + normalization rule version +
    parse rule version + params table version), the same hash
    construction :func:`content_identity` uses, just over the body
    instead of a single child block.
    """

    source_ref: str
    body: str
    chunks: tuple[PushChunk, ...]
    metadata: Mapping[str, Any]
    valid_from: str
    valid_until: str
    body_content_id: str

    @property
    def idempotency_key(self) -> tuple[str, str]:
        """The wire-level idempotency key: ``(source_ref, body_content_id)``."""
        return (self.source_ref, self.body_content_id)

    def to_wire(self) -> dict[str, Any]:
        """The wire shape (verbatim field names the receiving schema
        accepts). Metadata is copied so the caller's mutation cannot
        reach back into the envelope.

        ``chunks[i]`` carries the per-child ``id`` and ``content_id``
        when the envelope was built from a :class:`UnitChunking` —
        the receiving service uses these as the canonical
        per-slice identifiers (the citation gate keys on them,
        the invalidation path keys on ``content_id``).
        """
        return {
            "source_ref": self.source_ref,
            "body": self.body,
            "chunks": [
                {
                    "index": chunk.index,
                    "content": chunk.content,
                    **(
                        {"id": chunk.id}
                        if chunk.id is not None
                        else {}
                    ),
                    **(
                        {"content_id": chunk.content_id}
                        if chunk.content_id is not None
                        else {}
                    ),
                }
                for chunk in self.chunks
            ],
            "metadata": dict(self.metadata),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }


def envelope_of_unit(
    unit: UnitChunking,
    *,
    valid_from: str,
    valid_until: str,
) -> PushEnvelope:
    """Build the wire envelope of one frozen unit.

    The body is the unit's full extracted text (the same text
    ``UnitChunking.unit_text`` holds — the SC11/SC16 invariant is that
    every child block text is a contiguous substring of its parent's
    text, every parent text is a contiguous substring of the unit
    text, so the body contains every slice). The chunks are the
    children in order, indexed from 0.

    Each chunk's ``id`` / ``content_id`` is propagated from the
    per-child :class:`ChunkPayload` so the catalog side keeps pinning
    the same block id and per-child content identity it already had
    pre-T1: the invalidation path keys on these via
    :func:`fin.corpus.invalidation.plan_invalidations`, and the
    contract fake's per-chunk index needs the matching keys to resolve.

    The body content identity is :func:`content_identity` over the
    body text and the three rule versions — the same hashing
    construction per-child records use, so the envelope and a
    would-be per-child record share the content-identity contract.
    """
    chunks = tuple(
        PushChunk(
            index=index,
            content=payload.text,
            id=payload.block_id,
            content_id=content_identity(
                payload.text,
                normalization_rule_version=payload.normalization_rule_version,
                parse_rule_version=payload.parse_rule_version,
                params_table_version=payload.params_table_version,
            ),
        )
        for index, payload in enumerate(unit.payloads())
    )
    return PushEnvelope(
        source_ref=serialize_source_ref(unit.locator),
        body=unit.unit_text,
        chunks=chunks,
        metadata=MappingProxyType(
            {
                # SC20 filter surface — single authority is
                # ``REQUIRED_PUSH_METADATA_FIELDS``, asserted by both
                # the push adapter and the contract fake.
                "fmt": unit.fmt,
                "doc_type": unit.doc_type,
                "section": unit.section,
                "source_sha256": unit.source_sha256,
                "parse_rule_version": unit.parse_rule_version,
                "params_table_version": unit.params_table_version,
                # beyond SC20 — locator round-trip + density filter.
                "density": unit.density,
                "normalization_rule_version": NORMALIZATION_RULE_VERSION,
                "locator": dict(unit.locator.to_dict()),
            }
        ),
        valid_from=valid_from,
        valid_until=valid_until,
        body_content_id=content_identity(
            unit.unit_text,
            normalization_rule_version=NORMALIZATION_RULE_VERSION,
            parse_rule_version=unit.parse_rule_version,
            params_table_version=unit.params_table_version,
        ),
    )


def envelopes_of_document(
    document: DocumentChunking,
    *,
    valid_from: str,
    valid_until: str,
) -> tuple[PushEnvelope, ...]:
    """The push envelopes of a whole document, in unit order."""
    return tuple(
        envelope_of_unit(unit, valid_from=valid_from, valid_until=valid_until)
        for unit in document.units
    )


def envelopes_of_unit(
    unit: UnitChunking,
    *,
    valid_from: str,
    valid_until: str,
) -> tuple[PushEnvelope, ...]:
    """The push envelopes of one frozen unit (a one-element tuple).

    Provided so callers that have a single :class:`UnitChunking` can
    iterate like they would over :func:`envelopes_of_document`
    without branching on the document/unit shape.
    """
    return (envelope_of_unit(unit, valid_from=valid_from, valid_until=valid_until),)


# Sentinels the per-child-record back-compat path uses: a record is a
# leaf with no interval of its own, so it reads as "always valid" on
# the wire. Shared by every caller so the coercion shapes cannot drift.
_RECORD_VALID_FROM: Final[str] = "0001-01-01T00:00:00.000Z"
_RECORD_VALID_UNTIL: Final[str] = "9999-12-31T23:59:59.999Z"


def record_to_envelope(record: PushRecord) -> PushEnvelope:
    """Project one per-child :class:`PushRecord` into the
    one-envelope-per-资料 wire shape (single authority for the
    back-compat coercion).

    The record's ``text`` is the body and the single chunk (the record
    is the per-child leaf, so the unit-level and child-level content
    identities collapse); its ``block_id`` / ``content_id`` ride along
    as the chunk's ``id`` / ``content_id`` so the receiving service and
    the catalog-side invalidation path keep resolving the same
    per-child identities they pinned pre-T1. The envelope's
    ``body_content_id`` is the record's ``content_id`` for the same
    reason — a hand-rolled record's pinned identity is preserved
    verbatim rather than re-derived, so a wire envelope coerced here
    keys idempotently the same on both sides of the seam.

    :mod:`fin.retrieve.push_batching` (``_coerce_envelope``) and
    :mod:`fin.retrieve.push_port` (``_record_or_wire_to_envelope``)
    both go through this function — two projections of one record
    would let the batching wire and the direct-push wire drift.
    """
    return PushEnvelope(
        source_ref=record.source_ref,
        body=record.text,
        chunks=(
            PushChunk(
                index=0,
                content=record.text,
                id=record.block_id,
                content_id=record.content_id,
            ),
        ),
        metadata=dict(record.metadata),
        valid_from=_RECORD_VALID_FROM,
        valid_until=_RECORD_VALID_UNTIL,
        body_content_id=record.content_id,
    )


__all__ = [
    "REQUIRED_PUSH_METADATA_FIELDS",
    "PushChunk",
    "PushEnvelope",
    "PushRecord",
    "body_content_id",
    "content_identity",
    "deserialize_source_ref",
    "document_push_records",
    "envelope_of_unit",
    "envelopes_of_document",
    "envelopes_of_unit",
    "idempotency_keys_digest",
    "record_of_payload",
    "record_to_envelope",
    "serialize_source_ref",
    "unit_push_records",
]
