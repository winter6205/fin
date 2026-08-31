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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, Mapping

from fin.corpus.chunking import ChunkPayload, DocumentChunking, UnitChunking
from fin.corpus.determinism import canonical_json_bytes
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


def content_identity(
    text: str,
    *,
    normalization_rule_version: str,
    parse_rule_version: str,
    params_table_version: str,
) -> str:
    """The content identity of one block (§B4): sha256 over the block
    text and the three rule versions, joined by ``\\x1f`` so no field
    value can impersonate a field boundary."""
    parts = (
        text,
        normalization_rule_version,
        parse_rule_version,
        params_table_version,
    )
    return hashlib.sha256(_ID_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


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


__all__ = [
    "REQUIRED_PUSH_METADATA_FIELDS",
    "PushRecord",
    "content_identity",
    "document_push_records",
    "record_of_payload",
    "serialize_source_ref",
    "unit_push_records",
]
