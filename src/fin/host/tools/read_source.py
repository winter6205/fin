"""Host tool wrapper for ADR 0087 / 0088's bounded ``read_source`` read.

Pure function: one model-facing call resolves exactly ONE frozen unit
and returns at most one bounded slice of it. The tool owns no storage
path and no locator semantics — it routes to one of two injected
:class:`~fin.retrieve.source_read.SourceReader` legs and returns what
that leg answers:

* ``source_ref["kind"] == "attachment"`` → the attachment leg (the
  host-bound thread's stored unit text);
* anything else → the company leg (the bound retrieve adapter).

Everything past the routing key belongs to the legs: an absent leg
answers the typed miss dict (the same convention as
``MissReadPort``), and a present leg validates locator identity,
version, position bounds, and result size. The exceptions are the two
cheap shape guards below — a non-record ref carries no routing key to
read, and a garbage position can never address text — because this
boundary can name both without resolving anything, which is also why
needing a record to route at all is checked here rather than left to
the company leg.

Never raises to the model: every documented exit is a dict.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from fin.retrieve.source_read import (
    ATTACHMENT_KIND,
    INVALID_LOCATOR,
    UNKNOWN_LOCATOR,
    SourceReader,
    position_error,
    source_error,
)


def read_source(
    source_ref: Mapping[str, Any],
    position: int = 0,
    *,
    company_reader: SourceReader | None = None,
    attachment_reader: SourceReader | None = None,
) -> dict[str, Any]:
    """Read one bounded slice of the unit ``source_ref`` names.

    Returns the leg's dict unchanged — either the success slice shape
    (``{source_ref, text, next_position}``, see
    :func:`fin.retrieve.source_read.is_slice_result`) or a typed error
    dict (``invalid_position`` / ``invalid_locator`` /
    ``unknown_locator`` / ``source_unavailable`` /
    ``source_backend_contract_error``). No leg wired for the requested
    kind answers ``{"error": "unknown_locator"}``.
    """
    bad_position = position_error(position)
    if bad_position is not None:
        # EXIT: a garbage cursor is rejected here so no leg has to
        # resolve a locator for input that can never address text.
        return bad_position
    if not isinstance(source_ref, Mapping):
        # EXIT: routing needs a record to read a kind from, and a leg
        # would have to re-derive one from a non-record; the shape
        # verdict is the same the company leg names before any lookup.
        return source_error(INVALID_LOCATOR, reason="locator_not_a_record")

    kind = source_ref.get("kind")
    reader = attachment_reader if kind == ATTACHMENT_KIND else company_reader
    # getattr does not narrow a Protocol method through the callable
    # check; name the face this wrapper is allowed to call.
    read_slice: Callable[..., dict[str, Any]] | None = getattr(
        reader, "read_slice", None
    )
    if not callable(read_slice):
        # EXIT: this build has no leg for that source kind; the typed
        # miss is the same answer an unknown locator gets, so the model
        # learns nothing more than "there is no such readable source".
        return source_error(UNKNOWN_LOCATOR)
    return read_slice(dict(source_ref), position)


__all__ = ["read_source"]
