"""Canonical serialization + byte comparison of chunking products
(plan T12, SC12 / SC14).

The T12 assertion entry serializes chunking products into a canonical
byte string and compares sha256 digests across two consecutive
parse + split passes. Serialization is **canonical**:

* dicts — ``json.dumps(..., sort_keys=True)``, so benign dict key
  insertion order (however the pipeline happened to build a dict, e.g.
  by iterating a set) cannot produce a false mismatch;
* arrays — order-preserving, because block order is one of the four
  asserted aspects (块文本 / 块标识 / 顺序 / 元数据): a product whose
  block array came out in a different order hashes differently.

The four asserted aspects are all in the product:

1. **block text** — ``unit_text``, every parent/child ``text``, and
   the payload ``text`` / ``parent_text``;
2. **block identity** — every ``block_id`` / ``parent_block_id``
   (the deterministic sha256 ids from :mod:`fin.corpus.chunking`);
3. **order** — parent/child/payload arrays in product order, each
   block carrying its per-kind ``ordinal`` and ``parent_ordinal``;
4. **metadata** — the SC10/SC20 fields (format, doc type, section,
   density, source sha256, locator triple, parse rule version, params
   table version, normalization rule version) at unit and payload
   level, plus the resolved parameter row.

This module is deliberately dependency-free beyond the chunking
shapes: no clock, no randomness, no locale — the product bytes are a
pure function of the chunking products.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator

from fin.corpus.chunking import (
    ChunkBlock,
    DocumentChunking,
    UnitChunking,
)

_CANONICAL_JSON_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "separators": (",", ":"),
}


def canonical_json_bytes(product: dict[str, Any]) -> bytes:
    """Serialize one chunking product dict into canonical bytes.

    ``sort_keys`` neutralizes key insertion order; JSON arrays keep
    their element order (block order is asserted, not canonicalized).
    """
    return json.dumps(product, **_CANONICAL_JSON_KWARGS).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def chunking_product_sha256(product: dict[str, Any]) -> str:
    """The digest the T12 assertion compares across passes."""
    return sha256_hex(canonical_json_bytes(product))


def _block_product(block: ChunkBlock) -> dict[str, Any]:
    """One block: identity + text + order (kind, ordinals, parent link)."""
    return {
        "kind": block.kind,
        "block_id": block.block_id,
        "text": block.text,
        "char_count": block.char_count,
        "ordinal": block.ordinal,
        "parent_ordinal": block.parent_ordinal,
        "parent_block_id": block.parent_block_id,
    }


def unit_chunking_product(unit: UnitChunking) -> dict[str, Any]:
    """The canonical comparison product of one ``UnitChunking``.

    Arrays are lists in product order (order coverage); every payload
    dict comes from ``ChunkPayload.to_dict`` (metadata coverage).
    """
    return {
        "locator": unit.locator.to_dict(),
        "fmt": unit.fmt,
        "doc_type": unit.doc_type,
        "section": unit.section,
        "density": unit.density,
        "source_sha256": unit.source_sha256,
        "parse_rule_version": unit.parse_rule_version,
        "params_table_version": unit.params_table_version,
        # the resolved parameter row: part of the metadata surface
        "params": {
            "target_chars": unit.params.target_chars,
            "min_chars": unit.params.min_chars,
            "max_chars": unit.params.max_chars,
            "boundary_priority": list(unit.params.boundary_priority),
        },
        "unit_text": unit.unit_text,
        "parents": [_block_product(p) for p in unit.parents],
        "children": [_block_product(c) for c in unit.children],
        "payloads": [payload.to_dict() for payload in unit.payloads()],
    }


def document_chunking_product(document: DocumentChunking) -> dict[str, Any]:
    """The canonical comparison product of a whole ``DocumentChunking``
    (units in document order)."""
    return {
        "document_id": document.document_id,
        "version": document.version,
        "doc_type": document.doc_type,
        "units": [unit_chunking_product(unit) for unit in document.units],
    }


def first_mismatches(
    first: Any, second: Any, *, limit: int | None = 10, path: str = "$"
) -> Iterator[str]:
    """Yield the paths of the first differences between two products
    (diagnostics for a failing T12 comparison; the assertion itself
    compares digests). Scalars compare by ``!=``; dict keys missing on
    either side are differences; lists compare element-wise plus
    length. Yields at most ``limit`` paths (``None`` = unlimited)."""
    found: list[str] = []
    _collect_mismatches(first, second, path, found, [limit])
    yield from found


def _collect_mismatches(
    first: Any, second: Any, path: str, found: list[str], budget: list[int | None]
) -> None:
    if budget[0] is not None and budget[0] <= 0:
        return
    if isinstance(first, dict) and isinstance(second, dict):
        for key in sorted(set(first) | set(second)):
            if key not in first or key not in second:
                side = "second" if key in first else "first"
                found.append(f"{path}.{key}: missing on {side} side")
                if budget[0] is not None:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
                continue
            _collect_mismatches(
                first[key], second[key], f"{path}.{key}", found, budget
            )
        return
    if isinstance(first, list) and isinstance(second, list):
        if len(first) != len(second):
            found.append(f"{path}: length {len(first)} vs {len(second)}")
            if budget[0] is not None:
                budget[0] -= 1
                if budget[0] <= 0:
                    return
        for index in range(min(len(first), len(second))):
            _collect_mismatches(
                first[index], second[index], f"{path}[{index}]", found, budget
            )
        return
    if first != second:
        found.append(f"{path}: {first!r} vs {second!r}")
        if budget[0] is not None:
            budget[0] -= 1


__all__ = [
    "canonical_json_bytes",
    "chunking_product_sha256",
    "document_chunking_product",
    "first_mismatches",
    "sha256_hex",
    "unit_chunking_product",
]
