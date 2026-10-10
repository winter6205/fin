"""Type-preserving canonical bytes and digest for the structured-result snapshot.

``specs/structured-corpus-evidence.md`` §"Type-preserving digest" defines a
canonical UTF-8 encoding scoped to ``fin.evidence.structured_snapshot``
version 1. It is a NEW contract next to ADR 0092's legacy textified digest,
not a replacement for it: ``render_value_text`` collapses ``30``, ``"30"``
and ``30.0`` onto overlapping text, which is exactly what the structured
profile must not do. Nothing here routes through the legacy path, and
``compute_snapshot_digest`` in :mod:`fin.host.evidence_contract` is
untouched by this module.

Four rules, and the reason each is spelled out rather than left to a JSON
library default:

1. **Types stay distinct.** ``null``, ``true``, ``"1"`` and ``1`` must not
   collide, or a structured claim could be verified against a value the
   producer never sent. Numbers that are non-finite, outside the safe
   integer range, or fractional are rejected instead of coerced -- a
   snapshot is a contract value, and a silent ``repr(nan)`` would enter the
   digest under a spelling no producer wrote. Accepted integers normalize to
   base-10 text with zero as ``0``, which is what makes a decoded ``1.0``
   and a wire ``1`` agree (ADR 0093: one version, two spellings).
2. **Keys sort by UTF-8 bytes** and array order is preserved, so a support
   array's required stable ordering survives the digest. For valid scalars
   UTF-8 byte order equals code-point order, but this sorts on the encoded
   bytes anyway: the spec names bytes, and the property should hold by
   construction rather than by an encoding coincidence.
3. **Strings escape explicitly.** ``"`` and ``\\``, then U+0000-U+001F as
   lowercase six-character ``\\u00xx``. Every other scalar is emitted
   directly in UTF-8 with no Unicode normalization. ``json.dumps`` would
   emit ``\\n``/``\\t`` short escapes, which is a library default, not a
   contract; the spec wants the byte sequence pinned independently of it.
4. **Literals are literal.** ``null``/``true``/``false``, recursing into
   every nested value including maintenance and count metadata.

An unpaired surrogate is rejected here rather than at the boundary. Python
``str`` can hold lone surrogates, and they reach the digest only if some
earlier parser produced them; catching them at the encoder keeps the
refusal typed and keeps a mangled value from entering the digest.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Final, Mapping

from fin.host.evidence_contract import MALFORMED_EVIDENCE_CONTRACT

STRUCTURED_SNAPSHOT_SCHEMA: Final[str] = "fin.evidence.structured_snapshot"
STRUCTURED_SNAPSHOT_SCHEMA_FIELD: Final[str] = "schema"
STRUCTURED_SNAPSHOT_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"
STRUCTURED_SNAPSHOT_SCHEMA_VERSION: Final[int] = 1
STRUCTURED_SNAPSHOT_KIND_FIELD: Final[str] = "kind"
STRUCTURED_RESULT_KIND: Final[str] = "structured-result"

# Rule 1: the safe-integer ceiling is 2**53 - 1, the largest integer a
# double represents exactly. A number beyond it may have already lost
# precision in the producer's JSON text, so accepting it would digest a
# value the producer never measured.
SAFE_INTEGER_MAX: Final[int] = 2**53 - 1

STRUCTURED_SNAPSHOT_MAX_BYTES: Final[int] = 32 * 1024

_BARE_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

# The frozen key set of §"Frozen snapshot". ``value`` holds the exact row
# or count receipt iknow returned, verbatim and untruncated; ``maintenance``
# holds that turn's maintenance summary. Both are producer-owned payloads,
# and both are covered by the 32 KiB bound like any other content.
STRUCTURED_SNAPSHOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "schema_version",
        "kind",
        "subject",
        "operation",
        "query_digest",
        "scope_binding",
        "read_revision",
        "queried_at",
        "expires_at",
        "evidence_ref",
        "value",
        "coverage",
        "maintenance",
    }
)


class StructuredSnapshotError(ValueError):
    """A value the v1 canonical profile cannot encode.

    ``reason`` is the machine-readable cause; ``code`` is the contract exit
    the host surfaces. This is raised, never returned as an empty success:
    an unencodable value is a malformed contract, not a missing one.
    """

    __slots__ = ("code", "reason")

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = MALFORMED_EVIDENCE_CONTRACT


def _refuse(reason: str) -> StructuredSnapshotError:
    return StructuredSnapshotError(reason)


def _canonical_string(value: str) -> str:
    out: list[str] = ['"']
    for char in value:
        code_point = ord(char)
        if 0xD800 <= code_point <= 0xDFFF:
            raise _refuse("string carries an unpaired surrogate")
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code_point <= 0x1F:
            out.append(f"\\u{code_point:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _canonical_number(value: int | float) -> str:
    # bool is an int subclass, but rule 4 routes booleans to a literal
    # before a number ever reaches here; the guard keeps that true if this
    # function is ever called directly.
    if isinstance(value, bool):
        raise _refuse("boolean is not a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _refuse("number is not finite")
        if not value.is_integer():
            raise _refuse("number has a fractional decoded value")
        as_integer = int(value)
    elif isinstance(value, int):
        as_integer = value
    else:
        raise _refuse(f"unsupported number type: {type(value).__name__}")
    if not -SAFE_INTEGER_MAX <= as_integer <= SAFE_INTEGER_MAX:
        raise _refuse("number is outside the safe integer range")
    return str(as_integer)


def _canonical_key(value: Any) -> bytes:
    if not isinstance(value, str):
        raise _refuse(f"object key is not a string: {type(value).__name__}")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _refuse("object key carries an unpaired surrogate") from exc


def canonical_value(value: Any) -> str:
    """Encode one validated JSON value under the v1 canonical rules."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, Mapping):
        parts = [
            f"{_canonical_string(key)}:{canonical_value(value[key])}"
            for key in sorted(value, key=_canonical_key)
        ]
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_value(item) for item in value) + "]"
    raise _refuse(f"unsupported value type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """The canonical UTF-8 encoding of one validated JSON value."""
    return canonical_value(value).encode("utf-8")


def structured_snapshot_canonical_json(snapshot: Mapping[str, Any]) -> str:
    """Canonical text for a whole snapshot, digest field excluded.

    The frozen snapshot carries no digest field of its own (§"Frozen
    snapshot": the turn/citation record stores the digest beside the
    snapshot), so the whole validated mapping is the digest input.
    """
    if not isinstance(snapshot, Mapping):
        raise _refuse("snapshot is not an object")
    return canonical_value(snapshot)


def structured_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Lowercase hex SHA-256 of the canonical UTF-8 encoding.

    Bare hex, unlike the legacy ``sha256:``-prefixed carrier: the spec names
    "lowercase hexadecimal SHA-256" for this profile and prefixes it for no
    reason. Keeping the two spellings distinct is deliberate -- it makes an
    accidental cross-profile comparison fail loudly instead of appearing to
    agree.
    """
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()


def is_structured_snapshot_digest(value: Any) -> bool:
    """True only for a bare lowercase 64-hex digest of this profile."""
    return isinstance(value, str) and _BARE_SHA256_HEX_RE.match(value) is not None


def structured_snapshot_bytes(snapshot: Mapping[str, Any]) -> int:
    """Serialized UTF-8 size, the number §"Frozen snapshot" bounds at 32 KiB."""
    return len(canonical_bytes(snapshot))


__all__ = [
    "SAFE_INTEGER_MAX",
    "STRUCTURED_RESULT_KIND",
    "STRUCTURED_SNAPSHOT_FIELDS",
    "STRUCTURED_SNAPSHOT_KIND_FIELD",
    "STRUCTURED_SNAPSHOT_MAX_BYTES",
    "STRUCTURED_SNAPSHOT_SCHEMA",
    "STRUCTURED_SNAPSHOT_SCHEMA_FIELD",
    "STRUCTURED_SNAPSHOT_SCHEMA_VERSION",
    "STRUCTURED_SNAPSHOT_SCHEMA_VERSION_FIELD",
    "StructuredSnapshotError",
    "canonical_bytes",
    "canonical_value",
    "is_structured_snapshot_digest",
    "structured_snapshot_bytes",
    "structured_snapshot_canonical_json",
    "structured_snapshot_digest",
]
