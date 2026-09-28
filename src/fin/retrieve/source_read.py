"""Typed bounded source read: the shared contract of both ``read_source`` legs.

ADR 0087 / ADR 0088 (spec ``thread-attachment-source-read``): one
model-facing ``read_source({source_ref, position})`` resolves exactly one
frozen unit per call and returns a contiguous slice of at most
:data:`SOURCE_READ_MAX_TEXT_CODE_POINTS` Unicode code points whose
complete serialized result is at most
:data:`SOURCE_READ_MAX_RESULT_BYTES` UTF-8 bytes, plus a zero-based
code-point ``next_position`` (``None`` at end). The company leg maps the
iknow-memory wire into this shape and the attachment leg slices stored
unit text through this same boundary, so neither leg can hand the model
an unbounded body or an arbitrary path.

Every failure is a typed ``{"error": code, ...}`` dict — never prose,
never an empty success:

* :data:`INVALID_POSITION` — negative, non-integral, or beyond-end.
* :data:`INVALID_LOCATOR` — malformed or oversized locator identity.
* :data:`UNKNOWN_LOCATOR` — absent document/version, stale or
  cross-thread attachment.
* :data:`SOURCE_UNAVAILABLE` — backend transport or storage outage.
* :data:`SOURCE_BACKEND_CONTRACT_ERROR` — malformed or oversized
  backend result on the wire.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, runtime_checkable

# The one source of the read bounds (ADR 0088 initial policy). The byte
# cap measures the complete serialized tool result, matching the
# iknow-memory wire constants so a backend slice never arrives oversized.
SOURCE_READ_MAX_TEXT_CODE_POINTS: int = 8000
SOURCE_READ_MAX_RESULT_BYTES: int = 32768
SOURCE_READ_MAX_LOCATOR_CODE_POINTS: int = 1024

INVALID_POSITION: str = "invalid_position"
INVALID_LOCATOR: str = "invalid_locator"
UNKNOWN_LOCATOR: str = "unknown_locator"
SOURCE_UNAVAILABLE: str = "source_unavailable"
SOURCE_BACKEND_CONTRACT_ERROR: str = "source_backend_contract_error"

# The one routing key that selects the attachment leg (spec
# ``thread-attachment-source-read``: company triple vs attachment ref).
ATTACHMENT_KIND: str = "attachment"


def source_error(code: str, **fields: Any) -> dict[str, Any]:
    """Build one typed read failure dict."""
    return {"error": code, **fields}


def position_error(position: Any) -> dict[str, Any] | None:
    """The typed failure for a cursor no leg can honor, else ``None``.

    The single source of the two expressible position classes
    (non-integer, negative): every ``read_source`` boundary — the wrapper
    guard, the shared slicer, the company wire guard — rejects the same
    input with the same shape before resolving anything. Beyond-end is
    not here because only a leg that knows the unit length can name it.
    """
    if isinstance(position, bool) or not isinstance(position, int):
        return source_error(INVALID_POSITION, reason="position_not_an_integer")
    if position < 0:
        return source_error(INVALID_POSITION, position=position)
    return None


def is_slice_result(result: Mapping[str, Any]) -> bool:
    """True when the dict carries the success slice shape."""
    return (
        isinstance(result, Mapping)
        and "error" not in result
        and set(result) == {"source_ref", "text", "next_position"}
    )


def serialize_result(result: Any) -> str:
    """The one serialization of a read result a caller may emit.

    ``ensure_ascii=False`` keeps a multibyte slice the size the byte cap
    measures it at; ASCII-escaping CJK or astral text inflates the bytes
    handed to the model past a cap the slice legitimately passed. Emitter
    and measurement must agree, so both go through this function.
    """
    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def serialized_result_bytes(payload: Any) -> int:
    """UTF-8 size of the complete serialized result, canonical form.

    Measured through :func:`serialize_result` — the same bytes an emitter
    puts on the wire — so the cap is never checked against a form smaller
    than the one actually sent.
    """
    return len(serialize_result(payload).encode("utf-8"))


def bounded_slice(
    source_ref: Mapping[str, Any],
    text: str,
    position: int,
) -> dict[str, Any]:
    """Return one bounded contiguous slice of ``text`` from ``position``.

    ``position`` is a zero-based Unicode-code-point offset (Python
    ``str`` indices are code points, so supplementary-plane characters
    are single units and slice boundaries never split a pair). Success
    shapes are ``{source_ref, text, next_position}`` with
    ``next_position`` strictly advancing until ``None`` at the end;
    ``position == len(text)`` is the legal terminal empty slice.

    Shrinks the slice until the complete serialized result fits
    :data:`SOURCE_READ_MAX_RESULT_BYTES`. When even an empty slice does
    not fit, the locator metadata itself is oversized and the result is
    the typed :data:`INVALID_LOCATOR` failure, never a non-advancing
    cursor. Never raises.
    """
    bad_position = position_error(position)
    if bad_position is not None:
        # EXIT: the shared non-integer / negative classes are named by
        # one function so every boundary agrees on the shape.
        return bad_position

    total = len(text)
    if position > total:
        return source_error(
            INVALID_POSITION, position=position, code_points=total
        )

    def build(taken: int) -> dict[str, Any]:
        end = position + taken
        return {
            "source_ref": dict(source_ref),
            "text": text[position:end],
            "next_position": end if end < total else None,
        }

    taken = min(SOURCE_READ_MAX_TEXT_CODE_POINTS, total - position)
    # UTF-8 costs at most 4 bytes per code point, so one division
    # removes at least as many bytes as the excess counted.
    while taken > 0 and (
        excess := serialized_result_bytes(build(taken))
        - SOURCE_READ_MAX_RESULT_BYTES
    ) > 0:
        taken -= max(1, excess // 4)

    if taken == 0 and total - position > 0:
        # EXIT: the envelope alone (locator metadata) leaves no byte for
        # text; this is oversized locator metadata, not an empty success.
        return source_error(INVALID_LOCATOR, reason="locator_metadata_oversized")
    if serialized_result_bytes(build(taken)) > SOURCE_READ_MAX_RESULT_BYTES:
        # EXIT: reachable only with ``taken == 0`` at the terminal
        # position, which the guard above cannot catch because no text
        # remains — the concrete hazard is an envelope that sits just
        # under the cap while one more code point crosses it. Shipping
        # it would emit an empty slice over the cap and a cursor that
        # never advances.
        return source_error(INVALID_LOCATOR, reason="locator_metadata_oversized")
    return build(taken)


@runtime_checkable
class SourceReader(Protocol):
    """Structural protocol for one ``read_source`` leg.

    Implementations resolve exactly one locator-addressed frozen unit
    and answer either the success slice shape (see
    :func:`is_slice_result`) or a typed error dict built by
    :func:`source_error`. The model never sees an exception from this
    boundary.
    """

    def read_slice(
        self,
        source_ref: Mapping[str, Any],
        position: int = 0,
    ) -> dict[str, Any]:
        ...


__all__ = [
    "ATTACHMENT_KIND",
    "INVALID_LOCATOR",
    "INVALID_POSITION",
    "SOURCE_BACKEND_CONTRACT_ERROR",
    "SOURCE_READ_MAX_LOCATOR_CODE_POINTS",
    "SOURCE_READ_MAX_RESULT_BYTES",
    "SOURCE_READ_MAX_TEXT_CODE_POINTS",
    "SOURCE_UNAVAILABLE",
    "UNKNOWN_LOCATOR",
    "SourceReader",
    "bounded_slice",
    "is_slice_result",
    "position_error",
    "serialize_result",
    "serialized_result_bytes",
    "source_error",
]
