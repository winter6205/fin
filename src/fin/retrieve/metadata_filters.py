"""The four-key generic metadata filter: validation and wire mapping.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Retrieve request and metadata
filters". One module owns the contract so every port consumer (the MCP
adapter, the fake port, the host tool) reads the same rules:

* the allowlist is catalog-owned — :data:`fin.corpus.catalog.
  METADATA_FILTER_KEYS` — never a second literal here;
* validation runs BEFORE any I/O: a refused request answers
  ``{"error": "invalid_filter"}`` and never touches the transport;
* values normalize by Unicode NFC and Unicode-whitespace trim; a
  whitespace-only value is omitted, and a map whose every value is
  omitted is an EMPTY filter (the whole-scope request, not a refusal);
* each remaining value must be a string of 1–256 UTF-8 bytes with no
  Unicode ``Cc`` control character and no ``*`` / ``?`` wildcard;
* the legacy ``cik`` / ``form`` / ``ticker`` / ``filing_date`` names are
  NOT members — inside the generic map they are the named refusal. The
  legacy arguments remain port parameters during the migrate/expand
  window and apply conjunctively; the map itself rejects them
  throughout.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Final, Mapping

from fin.corpus.catalog import METADATA_FILTER_KEYS

#: The canonical named input failure (spec §"Result, error, readiness, and
#: degradation wire"): no ``chunks`` key — an empty hit list would read as
#: "the corpus has nothing", which is a different fact.
INVALID_FILTER_CODE: Final[str] = "invalid_filter"

#: The wire prefix the upstream exact-match vocabulary gives per-document
#: metadata predicates (upstream ``METADATA_FILTER_PREFIX``).
METADATA_WIRE_PREFIX: Final[str] = "metadata."

#: Upper bound on one normalized value, in UTF-8 bytes.
FILTER_VALUE_MAX_BYTES: Final[int] = 256

#: The maximum members an explicitly supplied filter object may carry —
#: exactly the allowlist's size.
FILTER_KEY_MAX_COUNT: Final[int] = len(METADATA_FILTER_KEYS)


def invalid_filter() -> dict[str, Any]:
    """The canonical refusal envelope, built fresh per call."""
    return {"error": INVALID_FILTER_CODE}


def normalized_metadata_filters(
    filters: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Validate and normalize one generic metadata filter request.

    Returns the wire map — each surviving key spelled
    ``metadata.<key>``, each value NFC-normalized and trimmed — or
    ``None`` when the request carries no filter at all (absent, empty,
    or every value omitted), which searches the caller's whole
    authorized scope. Raises :class:`InvalidFilterError` for every
    out-of-contract state; the port seam translates the raise into the
    canonical refusal before any I/O starts.
    """
    if filters is None:
        return None
    if not isinstance(filters, Mapping):
        raise InvalidFilterError("filters must be a JSON object")
    if len(filters) > FILTER_KEY_MAX_COUNT:
        raise InvalidFilterError(
            f"filters carries {len(filters)} keys, above the "
            f"{FILTER_KEY_MAX_COUNT} bound"
        )
    wire: dict[str, str] = {}
    for key, value in filters.items():
        if key not in METADATA_FILTER_KEYS:
            raise InvalidFilterError(
                f"filters key {key!r} is not a member of the metadata "
                f"allowlist {list(METADATA_FILTER_KEYS)}"
            )
        if not isinstance(value, str):
            raise InvalidFilterError(
                f"filters[{key!r}] is {type(value).__name__}, expected a "
                "string"
            )
        normalized = unicodedata.normalize("NFC", value).strip()
        if not normalized:
            # A whitespace-only value is omitted, not refused.
            continue
        _validate_value(key, normalized)
        wire[METADATA_WIRE_PREFIX + key] = normalized
    return wire or None


def _validate_value(key: str, value: str) -> None:
    """The per-value contract bounds, on the already-normalized string."""
    encoded = value.encode("utf-8")
    if len(encoded) > FILTER_VALUE_MAX_BYTES:
        raise InvalidFilterError(
            f"filters[{key!r}] is {len(encoded)} UTF-8 bytes, above the "
            f"{FILTER_VALUE_MAX_BYTES} bound"
        )
    for character in value:
        if unicodedata.category(character) == "Cc":
            raise InvalidFilterError(
                f"filters[{key!r}] carries a control character"
            )
        if character in ("*", "?"):
            raise InvalidFilterError(
                f"filters[{key!r}] carries the wildcard {character!r}"
            )


class InvalidFilterError(Exception):
    """The generic metadata filter is outside its contract.

    The port seam answers this with the canonical
    ``{"error": "invalid_filter"}`` before any I/O; the exception
    exists so the raise carries the reason for diagnostics without
    putting reason text on the wire.
    """


__all__ = [
    "FILTER_KEY_MAX_COUNT",
    "FILTER_VALUE_MAX_BYTES",
    "INVALID_FILTER_CODE",
    "InvalidFilterError",
    "METADATA_WIRE_PREFIX",
    "invalid_filter",
    "normalized_metadata_filters",
]
