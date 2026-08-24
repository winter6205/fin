"""MCP adapter for the product retrieve path (T3, ADR 0030).

The adapter is the v1 retrieve implementation behind
``RetrievePort``: a stdio JSON-RPC client to the
**iknow-memory** MCP server (separate TypeScript repo). It
supersedes the in-process BM25 adapter for the **product**
compile path; BM25 is retained as an offline / test adapter
only (ADR 0007 archived by 0030).

Wire-level protocol
-------------------

MCP uses JSON-RPC 2.0 over stdio. The adapter sends one
newline-delimited JSON object per ``tools/call`` request:

.. code-block:: json

    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "retrieve",
                "arguments": {"query": "...", "top_k": 8,
                              "document_id": "...",
                              "filter": {"cik": "...", ...}}}}

The server replies with one newline-delimited JSON object on
stdout. The adapter reads the first line and parses it as the
JSON-RPC response.

The adapter DOES NOT inline OCR / XBRL / parse / locator
mapping into the ranking call (complexity-anti-drift). The
locator map (``iknow-id`` → ``fin.Locator``) is configured at
construction and applied as a separate post-rank step. Hits
whose ``source_ref`` is not in the map are **dropped** — the
adapter never passes an iknow-memory id to the citation gate
(ADR 0030 + spec success-criteria item 7).

Error contract
--------------

* Empty / whitespace ``query``, unknown ``document_id``, and
  empty filter values (``cik=""``, etc.) are miss shapes and
  return ``{"chunks": []}`` without touching MCP.
* MCP subprocess spawn failure, protocol timeout, malformed
  response, or JSON-RPC error response → typed exception
  ``RetrieveUnavailableError`` internally; ``retrieve()``
  translates that to ``{"error": "retrieve_unavailable"}`` —
  the product EXIT shape (spec success-criteria item 6).
* The adapter NEVER falls back to BM25 on MCP failure (ADR
  0030 / error-handling-enforcer).

Concurrency
-----------

The public ``retrieve`` is sync and bridges to ``aretrieve``
via ``asyncio.run``. Each call allocates its own JSON-RPC
request id and per-call transport (subprocess lifetime is
per-call here for simplicity — a long-lived connection is a
later optimization). Concurrent calls share the read-only
locator map; there is no shared mutable state.

Test seam
---------

The default transport (``_call_mcp``) launches a subprocess.
Tests subclass and override ``_call_mcp`` to inject a
scripted response or scripted exception. The default is
NEVER invoked during the default pytest run — the test
surface is hermetic (no live MCP server).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fin.corpus.extracted import Locator
from fin.retrieve.port import Chunk


_DEFAULT_LIMIT = 8
_LIMIT_CAP = 20


# ----- typed exception -----


class RetrieveUnavailableError(Exception):
    """The MCP retrieve transport failed (subprocess down,
    protocol timeout, malformed response, or JSON-RPC error).

    This is a TYPED EXIT (ADR 0030 / error-handling-enforcer).
    It is raised internally by ``aretrieve`` and translated to
    ``{"error": "retrieve_unavailable"}`` by the sync
    ``retrieve`` wrapper. The host tool reads the structured
    error dict and surfaces it to the citation gate as a typed
    stop kind.

    The adapter does NOT swallow MCP failures as empty chunks
    and does NOT fall back to BM25.
    """


# ----- limit resolution -----


def _resolve_limit(limit: int) -> int:
    """Apply the port's limit rules: default 8, cap 20 (ADR 0009)."""
    if limit <= 0:
        return _DEFAULT_LIMIT
    if limit > _LIMIT_CAP:
        return _LIMIT_CAP
    return limit


def _is_blank(value: str | None) -> bool:
    """Return ``True`` when ``value`` is an empty string or
    whitespace-only.

    ``None`` is **not** blank — ``None`` means the caller did
    not pass this filter, while ``""`` / ``"   "`` means the
    caller explicitly passed an empty filter value (a miss
    shape per spec). The two cases must be treated differently.
    """
    return value is not None and not value.strip()


# ----- adapter -----


class MCPRetrieveAdapter:
    """stdio JSON-RPC client to the iknow-memory MCP server.

    Parameters
    ----------
    command
        The MCP server launch command (e.g. ``["iknow-memory",
        "serve"]``). When ``None``, ``_call_mcp`` raises
        ``OSError("no MCP command configured")`` so the caller
        sees a typed ``retrieve_unavailable`` rather than a
        silent fall-through.
    locator_map
        Map from iknow-memory ``source_ref`` id (typically a
        UUID) to a fin ``Locator``. Hits whose id is not in
        the map are dropped (never passed through to the
        citation gate).
    timeout_seconds
        Per-call timeout on the JSON-RPC exchange. Default 5s
        (operator override via constructor).
    """

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        locator_map: dict[str, Locator] | None = None,
        known_document_ids: set[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._command: list[str] | None = (
            list(command) if command is not None else None
        )
        # Defensive copy: a caller mutating the source dict
        # after construction must not affect this adapter.
        self._locator_map: dict[str, Locator] = dict(locator_map or {})
        # ``None`` means "no client-side membership test —
        # defer to the MCP server". ``set()`` means "no
        # document is known — every document_id filter short-
        # circuits to a miss". ``{"DOC-..."}`` enables the
        # membership test.
        self._known_doc_ids: frozenset[str] | None = (
            frozenset(known_document_ids)
            if known_document_ids is not None
            else None
        )
        self._timeout: float = float(timeout_seconds)

    # ----- public surface -----

    def retrieve(
        self,
        query: str,
        document_id: str | None = None,
        *,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Sync retrieve: returns ``{"chunks": [...]}`` or
        ``{"error": "retrieve_unavailable"}``.

        Empty / whitespace queries and unknown filters
        short-circuit BEFORE the MCP round-trip. Any MCP
        transport / protocol failure surfaces as
        ``{"error": "retrieve_unavailable"}`` (no BM25
        fallback).
        """
        try:
            return asyncio.run(
                self.aretrieve(
                    query,
                    document_id=document_id,
                    cik=cik,
                    form=form,
                    ticker=ticker,
                    filing_date=filing_date,
                    limit=limit,
                )
            )
        except RetrieveUnavailableError:
            # Typed EXIT — the product shape is the error dict.
            return {"error": "retrieve_unavailable"}

    async def aretrieve(
        self,
        query: str,
        document_id: str | None = None,
        *,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Async retrieve: raises ``RetrieveUnavailableError``
        on transport failure; ``retrieve`` translates that to
        the structured error dict.

        Tests subclass and override ``_call_mcp`` for scripted
        transport. The default transport launches a subprocess.
        """
        # Empty / whitespace query: short-circuit (miss shape).
        if not query or not query.strip():
            return {"chunks": []}
        # Empty / whitespace document_id filter: miss shape.
        if document_id is not None and not document_id.strip():
            return {"chunks": []}
        # Unknown document_id filter (when ``known_document_ids``
        # is configured): miss shape. We do this BEFORE the
        # MCP round-trip so the operator never pays for an
        # expensive retrieve call on a guaranteed miss. When
        # ``known_document_ids`` is ``None`` the adapter defers
        # to the MCP server's own membership logic.
        if (
            document_id is not None
            and self._known_doc_ids is not None
            and document_id not in self._known_doc_ids
        ):
            return {"chunks": []}
        # Empty filter values (cik / form / ticker /
        # filing_date) are miss shapes — short-circuit before
        # MCP per spec.
        if any(_is_blank(v) for v in (cik, form, ticker, filing_date)):
            return {"chunks": []}

        effective_limit = _resolve_limit(limit)

        # Build the JSON-RPC payload. The MCP ``tools/call``
        # method takes ``arguments`` matching the tool's input
        # schema; we forward the lexical query, the optional
        # ``top_k`` (limit), and the optional ``filter`` block.
        arguments: dict[str, Any] = {
            "query": query,
            "top_k": effective_limit,
        }
        if document_id is not None:
            arguments["document_id"] = document_id
        if not _is_blank(cik):
            arguments.setdefault("filter", {})["cik"] = cik
        if not _is_blank(form):
            arguments.setdefault("filter", {})["form"] = form
        if not _is_blank(ticker):
            arguments.setdefault("filter", {})["ticker"] = ticker
        if not _is_blank(filing_date):
            arguments.setdefault("filter", {})["filing_date"] = filing_date

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "retrieve",
                "arguments": arguments,
            },
        }

        # Transport. The default ``_call_mcp`` launches a
        # subprocess; tests override it.
        try:
            response = await self._call_mcp(payload)
        except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
            # EXIT: typed MCP-transport failure. Translate to
            # the product error dict at the sync seam.
            raise RetrieveUnavailableError(
                f"MCP transport failed: {exc!r}"
            ) from exc

        # JSON-RPC error response: surface as unavailable.
        if isinstance(response, dict) and "error" in response:
            raise RetrieveUnavailableError(
                f"MCP returned JSON-RPC error: {response['error']!r}"
            )

        # Result shape: accept three envelope variants for
        # forward-compatibility with different MCP server
        # implementations:
        #
        # 1. ``{"result": {"hits": [...]}}`` — the standard
        #    JSON-RPC success envelope with the raw tool output
        #    as ``result``.
        # 2. ``{"hits": [...]}`` — server returns the raw tool
        #    output as the JSON-RPC result (some MCP servers
        #    unwrap automatically).
        # 3. ``{"result": {"content": [{"type": "text",
        #    "text": "<json with hits>"}]}}`` — the canonical
        #    MCP ``tools/call`` envelope with content blocks.
        if not isinstance(response, dict):
            raise RetrieveUnavailableError(
                f"MCP returned non-dict response: {response!r}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            # Variant 2: ``{"hits": [...]}`` at top level.
            if isinstance(response.get("hits"), list):
                result = response
            else:
                raise RetrieveUnavailableError(
                    f"MCP returned non-dict result: {result!r}"
                )
        # Variant 3: content-block envelope.
        if "hits" not in result and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and isinstance(
                    first.get("text"), str
                ):
                    try:
                        inner = json.loads(first["text"])
                    except ValueError as exc:
                        raise RetrieveUnavailableError(
                            f"MCP content text is not JSON: {exc}"
                        ) from exc
                    if isinstance(inner, dict) and "hits" in inner:
                        result = inner
        hits = result.get("hits")
        if not isinstance(hits, list):
            raise RetrieveUnavailableError(
                f"MCP returned non-list hits: {hits!r}"
            )

        # Map iknow ids → fin locators; drop unmapped. The cap
        # is enforced AFTER mapping so the returned list is
        # the locatable subset, not a slice of unmapped ids.
        chunks: list[Chunk] = []
        for hit in hits:
            if len(chunks) >= effective_limit:
                break
            if not isinstance(hit, dict):
                continue
            text = hit.get("text", "")
            if not isinstance(text, str):
                continue
            source_ref = hit.get("source_ref")
            if not isinstance(source_ref, str):
                continue
            locator = self._locator_map.get(source_ref)
            if locator is None:
                # Mapper-drop: omit this hit; do NOT pass
                # through an iknow id to the citation gate.
                continue
            try:
                score = float(hit.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            chunks.append(
                Chunk(text=text, source_ref=locator, score=score)
            )
        return {"chunks": chunks}

    # ----- transport (test seam) -----

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send ``payload`` (a JSON-RPC request) to the MCP
        server and return the parsed response dict.

        The default implementation:

        1. Launches ``self._command`` via asyncio subprocess.
        2. Writes the JSON-serialized payload + ``\\n`` on stdin.
        3. Reads stdout until the first newline (one JSON-RPC
           response per call).
        4. Times out after ``self._timeout`` seconds.
        5. Returns the parsed response dict.

        Tests subclass and override this method with a
        scripted response / exception. The override is the
        documented seam — modifying the default transport
        requires the override to honor the same contract
        (return dict on success; raise ``OSError`` /
        ``TimeoutError`` on failure).
        """
        if self._command is None:
            raise OSError("no MCP command configured")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise OSError(f"failed to launch MCP command: {exc}") from exc
        try:
            line = (json.dumps(payload) + "\n").encode("utf-8")
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(line),
                timeout=self._timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise
        if proc.returncode != 0:
            raise OSError(
                f"MCP server exited {proc.returncode}: "
                f"{stderr.decode(errors='replace')!r}"
            )
        # The response is one JSON-RPC object per line. Read
        # the FIRST non-empty line; ignore any subsequent ones.
        first_line = b""
        for line in stdout.split(b"\n"):
            if line.strip():
                first_line = line
                break
        try:
            return json.loads(first_line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RetrieveUnavailableError(
                f"malformed JSON-RPC response: {exc}"
            ) from exc


__all__ = ["MCPRetrieveAdapter", "RetrieveUnavailableError"]