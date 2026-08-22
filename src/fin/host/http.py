"""The v1 host HTTP surface (T9).

A thin FastAPI app on top of the SAME compiled graph object the CLI
uses (ADR 0013 / spec success criteria item 8). The route is the
only public entry point:

* ``POST /ask`` — body ``{"question": "...", "thread_id": "<opt>"}``,
  response ``{"status": "done|refuse|step_cap", "answer": "...",
  "citations": [...], "thread_id": "<uuid>", "reason": "<opt>"}``.

Sync ``invoke`` only — never ``ainvoke`` (ADR 0015 / spec). The
prod runtime composer (``compile_for_runtime``) is T8's
responsibility; this module lazy-imports it inside ``compile_for_http``
so it is not a module-load dependency. Tests pass the graph
directly via FastAPI's ``dependency_overrides`` so the lazy prod
path is never reached.

No auth, no streaming, no cookies (ADR 0013 / ADR 0018 / spec). v1
is a localhost-only, JSON-in / JSON-out POST route over the
shared graph.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from fin.corpus.errors import IngestMissingError
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER, strip_hidden_bounce_marker
from fin.host.graph import ConcurrentRunError, compile_graph


# Hidden bounce marker the gate stamps on internal SystemMessages
# (T6 / ADR 0021). Defensive: HTTP serialization must NEVER leak
# this string into the response body. The literal lives in
# ``fin.host.citation_gate`` (SSOT) — imported here so a rename
# touches one file.
_HIDDEN_BOUNCE_MARKER = HIDDEN_BOUNCE_MARKER


# ----- request / response schemas -----


class AskRequest(BaseModel):
    """Request body for ``POST /ask``.

    ``question`` is required; ``thread_id`` is optional. If omitted
    the server generates a UUID4 and returns it (ADR 0016:
    "auto-create UUID if omitted, return it to the caller").
    """

    question: str
    thread_id: str | None = None


class CitationSchema(BaseModel):
    """One code-filled citation in the response.

    ``document_id`` / ``version`` / ``page`` always come from the
    matched evidence item's locator — never from the model's claim
    (ADR 0021). ``quote`` is the ≥32-char substring the gate
    matched.
    """

    document_id: str
    version: str
    page: int
    quote: str


class AskResponse(BaseModel):
    """Response body for ``POST /ask``.

    ``citations`` is always present (empty list when ``status`` is
    not ``done``). ``reason`` is populated only on ``refuse`` or
    ``step_cap``. ``answer`` is empty on ``refuse`` / ``step_cap``.
    """

    status: Literal["done", "refuse", "step_cap"]
    answer: str = ""
    citations: list[CitationSchema] = Field(default_factory=list)
    thread_id: str
    reason: str | None = None


# ----- factory -----


# Module-level singleton cache for the prod runtime composer (T8).
# Test calls into ``compile_for_http`` with explicit ``checkpointer``
# + ``model`` bypass this cache entirely and rely on
# ``compile_graph``'s built-in idempotence (T6 LRU keyed on
# ``id(checkpointer) + id(model) + ...``).
_http_graph_singleton: Any | None = None
_http_singleton_lock = threading.Lock()


def compile_for_http(
    *,
    checkpointer: Any | None = None,
    model: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Build / return the v1 HTTP compiled graph.

    This is the HTTP-side equivalent of T7's ``compile_for_cli``.
    Both delegate to ``compile_graph`` (T6), which is itself
    idempotent — two calls with identical args return the SAME
    proxy object, so a CLI run and an HTTP run on the same
    ``checkpointer`` + ``model`` share one graph.

    Production path (``checkpointer=None``, ``model=None``)
    lazy-imports T8's ``compile_for_runtime`` and caches the
    resulting graph at module level. If T8 has not been merged
    the import fails and ``compile_for_http()`` raises — tests
    pass explicit ``checkpointer`` + ``model`` args and bypass
    the lazy path entirely.

    Parameters
    ----------
    checkpointer
        A LangGraph ``Checkpointer``. Tests pass
        ``InMemorySaver()``; T8 passes a configured
        ``PostgresSaver``. ``None`` triggers the prod lazy path.
    model
        A LangChain ``BaseChatModel``. Tests pass
        ``ScriptedChatModel``; T8 passes the ``ChatOpenAI``
        instance built from ``FIN_MODEL`` + ``FIN_BASE_URL``.
        ``None`` triggers the prod lazy path.
    **kwargs
        Forwarded to ``compile_graph`` (adapter, manifest_path,
        extracted_root, known_document_ids, step_cap,
        recursion_limit).

    Returns
    -------
    The compiled graph proxy (``_LockedGraph``).
    """
    if checkpointer is None and model is None:
        # Prod path. Lazy-import so module-load does not depend
        # on T8 / Postgres.
        global _http_graph_singleton
        with _http_singleton_lock:
            if _http_graph_singleton is None:
                from fin.host.compile import compile_for_runtime

                _http_graph_singleton = compile_for_runtime()
            return _http_graph_singleton
    return compile_graph(
        checkpointer=checkpointer,
        model=model,
        **kwargs,
    )


# ----- app + dependency -----


app = FastAPI(
    title="fin v1 host agent",
    version="0.1.0",
    description="Thin POST /ask surface over the v1 host graph (ADR 0013).",
)


def get_graph() -> Any:
    """FastAPI dependency that returns the compiled graph.

    Tests override this via ``app.dependency_overrides[get_graph]``
    to inject a graph built with ``InMemorySaver`` +
    ``ScriptedChatModel`` (no live LLM, no Postgres). Production
    boots the runtime composer once and reuses the singleton
    via ``compile_for_http``.
    """
    return compile_for_http()


# ----- exception handlers -----


@app.exception_handler(ConcurrentRunError)
async def concurrent_run_handler(request: Request, exc: ConcurrentRunError) -> JSONResponse:
    """Map ``ConcurrentRunError`` → 409 with
    ``{"error": "concurrent_run"}``.

    ADR 0016 / spec: "at most one active run per thread." A
    second concurrent ``invoke`` raises the typed
    ``ConcurrentRunError``; the surface translates it to 409
    without leaking internal details.
    """
    return JSONResponse(status_code=409, content={"error": "concurrent_run"})


@app.exception_handler(IngestMissingError)
async def ingest_missing_handler(request: Request, exc: IngestMissingError) -> JSONResponse:
    """Map ``IngestMissingError`` → 503 with
    ``{"error": "ingest_missing"}``.

    ADR 0020: the ask path must not fall back to opening a PDF.
    If the committed extracted tree is missing or incomplete,
    the operator must restore the frozen PDF (or refresh the
    manifest) and re-run ingest. The HTTP body intentionally
    carries no detail beyond ``ingest_missing`` so internal
    paths / sha256 do not leak (security-boundaries: error
    messages do not leak sensitive details).
    """
    return JSONResponse(status_code=503, content={"error": "ingest_missing"})


# ----- the route -----


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, graph: Any = Depends(get_graph)) -> AskResponse:
    """Run one v1 host-graph ``invoke`` on a ``POST /ask`` request.

    The handler is a sync ``def`` and calls ``graph.invoke`` — never
    ``graph.ainvoke`` (ADR 0015: durable CLI/HTTP require sync
    ``invoke`` with the sync saver).

    ``thread_id`` is auto-created as a UUID4 if the caller omits
    it (ADR 0016). Status mapping:

    * ``done`` → ``answer`` + ``citations`` populated; ``reason``
      omitted.
    * ``refuse`` → empty ``answer`` + empty ``citations``;
      ``reason`` = ``status_reason`` (``unknown_locator`` or
      ``citation_repair_cap``).
    * ``step_cap`` → empty ``answer`` + empty ``citations``;
      ``reason`` = ``"step_cap"``.

    The hidden bounce marker is stripped from ``answer`` and
    ``reason`` defensively before serialization (ADR 0021).
    """
    thread_id = body.thread_id or str(uuid.uuid4())

    raw = graph.invoke(
        {"messages": [HumanMessage(content=body.question)]},
        config={"configurable": {"thread_id": thread_id}},
    )

    status_value = raw.get("status")
    answer_value = raw.get("answer", "") or ""
    citations_value = raw.get("citations", ()) or ()
    reason_value = raw.get("status_reason", "") or ""

    # Defensive strip: ADR 0021 forbids leaking the bounce marker.
    answer_value = strip_hidden_bounce_marker(answer_value)
    reason_value = strip_hidden_bounce_marker(reason_value)

    citations_payload: list[CitationSchema] = []
    if status_value == "done":
        for cit in citations_value:
            citations_payload.append(
                CitationSchema(
                    document_id=cit.document_id,
                    version=cit.version,
                    page=cit.page,
                    quote=cit.quote,
                )
            )
        return AskResponse(
            status="done",
            answer=answer_value,
            citations=citations_payload,
            thread_id=thread_id,
        )

    # ``refuse`` / ``step_cap`` (or any unexpected status — defend
    # by collapsing to refuse with no detail).
    if status_value not in ("done", "refuse", "step_cap"):
        status_value = "refuse"
        reason_value = ""

    reason_payload: str | None = reason_value or None
    return AskResponse(
        status=status_value,  # type: ignore[arg-type]
        answer="",
        citations=[],
        thread_id=thread_id,
        reason=reason_payload,
    )


__all__ = [
    "AskRequest",
    "AskResponse",
    "CitationSchema",
    "app",
    "compile_for_http",
    "get_graph",
]
