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
* ``MCPRetrieveAdapter.read`` (``fin.retrieve.mcp_adapter``) returns
  the named ``READ_UNAVAILABLE`` failure: iknow-memory exposes no read
  tool today (handoff 2026-09-04), and reading the local tree instead
  would reintroduce exactly the second SSOT ADR 0047 removes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import ExtractedPage, load_extracted_tree


# Named failure the product MCP read returns while the retrieve backend
# exposes no read tool. Deliberately NOT ``unknown_locator`` (which
# would claim the locator does not exist) and NOT a silent empty
# success (error-handling-enforcer: named failures only).
READ_UNAVAILABLE: str = "read_unavailable"

# The model-facing miss dict every miss path returns.
_UNKNOWN_LOCATOR: dict[str, str] = {"error": "unknown_locator"}

# ADR 0009 cap on ``page_limit`` (preserved from the original
# ``read_file``).
_PAGE_LIMIT_CAP = 3


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
    if page_limit > _PAGE_LIMIT_CAP:
        page_limit = _PAGE_LIMIT_CAP

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
    "ExtractedTreeReadPort",
    "MissReadPort",
    "ReadPort",
    "read_from_pages",
]
