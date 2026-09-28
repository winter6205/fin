"""Read port: the seam ``read_file`` fetches object bodies through.

ADR 0046 / ADR 0047 (spec ``corpus-serve-ingest``): the serve path must
not keep a second on-disk extracted tree as the company-corpus SSOT.
``read_file`` stays "an ordinary read of a known locator (MCP when that
is where the bytes live)", so the body fetch is an injected **read
port** — the same port idea as :mod:`fin.retrieve.port`:

* :class:`ReadPort` — structural protocol. ``read(document_id, version,
  page, page_limit)`` returns the ``read_file`` contract dict
  (``{document_id, version, page, text, page_count}``) or the typed
  miss dict ``{"error": "unknown_locator"}``. It never raises at the
  model.
* :func:`read_from_pages` — the shared boundary logic (page bounds,
  ADR 0009 ``page_limit`` clamp, version match, ``\\n\\n`` join) every
  pages-backed implementation reuses.
* :data:`PAGE_LIMIT_CAP` / :func:`clamp_page_limit` — the one source of
  the ADR 0009 bound (value and rule), shared by
  :func:`read_from_pages` and ``MCPRetrieveAdapter._read_locators``.
* :class:`ExtractedTreeReadPort` — the legacy tree-backed adapter over
  :func:`fin.corpus.extracted.load_extracted_tree`. Retained for
  offline / committed-corpus tests; **no product compile path builds
  it** — the graph injects the retrieve adapter's own ``read`` or a
  typed-miss port instead.
* :class:`MissReadPort` — the explicit "this backend cannot read
  objects" adapter: every read is the typed miss.

Adapters that also implement retrieve carry ``read`` on themselves:

* ``FakeRetrievePort.read`` (``fin.retrieve.fake_port``) answers from
  its optional ``pages_by_doc`` manifest — the scripted-serve answer to
  "where does a fake read body come from without a local tree".
* ``MCPRetrieveAdapter.read`` (``fin.retrieve.mcp_adapter``) dispatches
  an ingested locator to the MCP ``read_source`` tool (one call per
  frozen unit, canonical ``source_ref``) and answers the typed miss
  dict for anything else — it NEVER falls back to the local extracted
  tree, which would reintroduce exactly the second SSOT ADR 0047
  removes.
* ``MCPRetrieveAdapter.read_slice`` / ``FakeRetrievePort.read_slice`` —
  the bounded ``read_source`` legs (ADR 0088, spec
  ``thread-attachment-source-read``): one frozen unit, one slice. Both
  resolve their locator with :func:`validate_company_source_ref`, the
  one place the company triple's shape and size are judged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import ExtractedPage, load_extracted_tree
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    SOURCE_READ_MAX_LOCATOR_CODE_POINTS,
    source_error,
)


# Named failure the product MCP read returns while the retrieve backend
# exposes no read tool. Deliberately NOT ``unknown_locator`` (which
# would claim the locator does not exist) and NOT a silent empty
# success (error-handling-enforcer: named failures only).
READ_UNAVAILABLE: str = "read_unavailable"

# The model-facing miss dict every miss path returns.
_UNKNOWN_LOCATOR: dict[str, str] = {"error": "unknown_locator"}

# ADR 0009 cap on ``page_limit`` (preserved from the original
# ``read_file``).
PAGE_LIMIT_CAP = 3


def clamp_page_limit(page_limit: int) -> int:
    """Bound ``page_limit`` at :data:`PAGE_LIMIT_CAP` (ADR 0009).

    Single source of the clamp rule for every read path. Callers reject
    ``page_limit < 1`` as a miss BEFORE clamping; this helper only
    enforces the upper bound.
    """
    return min(page_limit, PAGE_LIMIT_CAP)


# The closed key set of a company locator triple (ADR 0088): an extra
# ``kind`` / ``page`` / path key would let the same frozen unit acquire
# a second identity, so the shape is checked against the set, not
# against a subset.
_COMPANY_LOCATOR_KEYS = frozenset({"document_id", "version", "unit"})


def validate_company_source_ref(
    source_ref: Any,
) -> tuple[str, str, int] | dict[str, Any]:
    """Resolve one company triple, or name why it is not one.

    The company locator is exactly ``{document_id, version, unit}`` with
    non-empty string ``document_id`` / ``version`` and a one-based
    integer ``unit`` — the same field rules
    :func:`fin.corpus.push.deserialize_source_ref` applies to the wire
    string, so fin never accepts a ref it could not send.

    Returns ``(document_id, version, unit)`` on success, otherwise the
    typed ``invalid_locator`` failure (a malformed shape or a canonical
    serialization over :data:`SOURCE_READ_MAX_LOCATOR_CODE_POINTS`,
    whose ``reason`` reuses the backend's own
    ``request_locator_oversized`` token so one oversized identity has
    one name on both legs). Whether the triple addresses a unit that
    exists is the caller's lookup, and answers ``unknown_locator``.

    Never raises.
    """
    if not isinstance(source_ref, Mapping):
        # EXIT: anything that is not a record (a bare serialized string,
        # a list, None) carries no typed identity to resolve.
        return source_error(INVALID_LOCATOR, reason="locator_not_a_record")
    if frozenset(source_ref) != _COMPANY_LOCATOR_KEYS:
        return source_error(INVALID_LOCATOR, reason="locator_keys_malformed")

    document_id = source_ref["document_id"]
    version = source_ref["version"]
    unit = source_ref["unit"]
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(version, str)
        or not version
        # ``bool`` is an ``int`` subclass: ``True`` is a malformed
        # ordinal, never a spelling of unit 1.
        or isinstance(unit, bool)
        or not isinstance(unit, int)
        or unit < 1
    ):
        return source_error(INVALID_LOCATOR, reason="locator_fields_malformed")

    if (
        len(serialize_source_ref(UnitLocator(document_id, version, unit)))
        > SOURCE_READ_MAX_LOCATOR_CODE_POINTS
    ):
        # EXIT: an identity this long leaves the bounded result no room
        # for any text, so it is refused before a round trip rather than
        # answered with a shrunken or oversized slice.
        return source_error(INVALID_LOCATOR, reason="request_locator_oversized")
    return document_id, version, unit



@runtime_checkable
class ReadPort(Protocol):
    """Structural protocol for read_file body sources.

    Implementations answer the ``read_file`` contract dict directly and
    convert every failure they can represent into the typed miss dict —
    the tool wrapper never sees an exception for a "no such object"
    answer.
    """

    def read(
        self,
        document_id: str,
        version: str,
        page: int = 1,
        page_limit: int = 1,
    ) -> dict[str, Any]:
        """Return the read_file contract dict or the miss dict.

        Miss shapes (all ``{"error": "unknown_locator"}``): unknown
        ``document_id``, version mismatch, ``page < 1`` / ``page >
        page_count``, ``page_limit < 1``. ``page_limit > 3`` is
        silently clamped to 3 (ADR 0009).
        """
        ...


def read_from_pages(
    pages_by_doc: Mapping[str, Sequence[ExtractedPage]],
    document_id: str,
    version: str,
    page: int = 1,
    page_limit: int = 1,
) -> dict[str, Any]:
    """Read a page range from an in-memory ``{document_id: pages}`` table.

    This is the shared boundary logic of every pages-backed ``ReadPort``
    (extracted-tree rows and fake-manifest rows have the same shape).

    Returns one of:

    * happy-path dict ``{"document_id", "version", "page", "text",
      "page_count"}`` where ``text`` joins pages
      ``[page, page + page_limit - 1]`` (clamped to ``page_count``)
      with the ``\\n\\n`` separator;
    * miss dict ``{"error": "unknown_locator"}`` for an unknown id, a
      version mismatch, a page out of range, or ``page_limit < 1``.

    Never raises.
    """
    # Lower-bound rejections happen before any table lookup; the upper
    # cap on ``page_limit`` clamps silently per ADR 0009.
    if page < 1 or page_limit < 1:
        return dict(_UNKNOWN_LOCATOR)
    page_limit = clamp_page_limit(page_limit)

    pages = pages_by_doc.get(document_id)
    if not pages:
        return dict(_UNKNOWN_LOCATOR)

    page_count = len(pages)
    if page > page_count:
        return dict(_UNKNOWN_LOCATOR)

    # The rows' locator ``version`` is the ingested sha256; comparing
    # the caller's ``version`` against the first page rejects any
    # drift without a separate manifest read.
    expected_version = pages[0].locator.version
    if version != expected_version:
        return dict(_UNKNOWN_LOCATOR)

    end_page = min(page + page_limit - 1, page_count)
    text = "\n\n".join(pages[i - 1].text for i in range(page, end_page + 1))

    return {
        "document_id": document_id,
        "version": version,
        "page": page,
        "text": text,
        "page_count": page_count,
    }


class ExtractedTreeReadPort:
    """Legacy read adapter over the committed extracted tree.

    Loads ``load_extracted_tree(manifest_path, extracted_root)`` per
    read (no cache — the original ``read_file`` behaviour) and answers
    through :func:`read_from_pages`. ``IngestMissingError`` from a
    missing / malformed tree converts to the miss dict so the model
    never sees a typed exception (ADR 0020 contract preserved).

    This adapter exists for offline / committed-corpus tests. Product
    compile paths must NOT build it: the serve path has no local-tree
    SSOT (ADR 0046 / ADR 0047).
    """

    def __init__(self, manifest_path: Path, extracted_root: Path) -> None:
        self._manifest_path = Path(manifest_path)
        self._extracted_root = Path(extracted_root)

    def read(
        self,
        document_id: str,
        version: str,
        page: int = 1,
        page_limit: int = 1,
    ) -> dict[str, Any]:
        try:
            pages_by_doc = load_extracted_tree(
                self._manifest_path, self._extracted_root
            )
        except (IngestMissingError, OSError):
            # Typed error from a broken / absent tree (a missing
            # manifest surfaces as FileNotFoundError, an incomplete one
            # as IngestMissingError): convert either to the
            # model-facing miss dict so the model can decide. The
            # graph boot path (T6 / ADR 0020) reports tree problems
            # separately where a diagnostic belongs.
            return dict(_UNKNOWN_LOCATOR)
        return read_from_pages(
            pages_by_doc, document_id, version, page, page_limit
        )


class MissReadPort:
    """Read adapter whose corpus can never be read: every read misses.

    Used by ``compile_graph`` when the bound retrieve adapter offers no
    ``read`` (legacy / offline adapters such as ``BM25Adapter``): the
    tool answers the typed miss instead of silently falling back to a
    local tree (SC3 — no silent success that implies a local tree).
    """

    def read(
        self,
        document_id: str,
        version: str,
        page: int = 1,
        page_limit: int = 1,
    ) -> dict[str, Any]:
        return dict(_UNKNOWN_LOCATOR)


__all__ = [
    "READ_UNAVAILABLE",
    "PAGE_LIMIT_CAP",
    "ExtractedTreeReadPort",
    "MissReadPort",
    "ReadPort",
    "clamp_page_limit",
    "read_from_pages",
    "validate_company_source_ref",
]
