"""Host tool wrapper for ADR 0009's ``read_file`` tool.

Pure function: takes ``document_id`` + ``version`` (and optional
``page`` / ``page_limit``) and returns either a happy-path page-text
dict or ``{"error": "unknown_locator"}``. The body fetch goes through
an injected :class:`~fin.retrieve.read_port.ReadPort` (ADR 0046 /
0047) — the tool itself owns NO storage path:

* the product MCP path injects the ``MCPRetrieveAdapter``'s own
  ``read``, which dispatches an ingested locator to the MCP
  ``read_source`` tool and misses otherwise;
* the fake path injects ``FakeRetrievePort.read``, which serves
  bodies from the fake's ``pages_by_doc`` manifest;
* offline / committed-corpus tests may inject
  ``ExtractedTreeReadPort``; the serve path never builds one, so the
  local extracted tree is not a company-corpus SSOT.

The happy-path shape is::

    {"document_id", "version", "page", "text", "page_count"}

where ``text`` is the joined page text of
``[page, page + page_limit - 1]`` (clamped to ``page_count``). The
model paginates by calling again with a higher ``page``.

``page_limit`` is silently clamped to 3 (ADR 0009 cap); ``page < 1``
or ``page > page_count`` returns the miss dict. The reader converts
every lookup failure to the miss dict, so the model never sees a
typed exception from this tool (T6 catches ``IngestMissingError``
separately at boot, ADR 0020).
"""

from __future__ import annotations

from typing import Any

from fin.retrieve.read_port import ReadPort


def read_file(
    document_id: str,
    version: str,
    page: int = 1,
    page_limit: int = 1,
    *,
    reader: ReadPort,
) -> dict[str, Any]:
    """Read a page (or short page range) of a known locator.

    Returns one of:

    * happy-path dict ``{"document_id", "version", "page", "text",
      "page_count"}``;
    * miss dict ``{"error": "unknown_locator"}`` for an unknown id,
      a version mismatch, a page out of range, a broken body source,
      or ``page_limit < 1``.

    Never raises. The ``reader`` is the injected read port — there is
    deliberately no default body source (ADR 0047: MCP when that is
    where the bytes live; never the local extracted tree by accident).
    """
    return reader.read(document_id, version, page, page_limit)


__all__ = ["read_file"]
