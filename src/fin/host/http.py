"""The v1 host HTTP surface (T9 + workspace T1, #28).

A thin FastAPI app on top of the SAME compiled graph object the CLI
uses (ADR 0013 / spec success criteria item 8). Two route families
sit on this app:

* ``POST /ask`` — sync invoke, JSON in / JSON out. v1 eval surface.
* ``/api/threads/...`` — workspace surface (T1, #28): thread CRUD,
  message send, corpus list. See ``fin.host.workspace_http``.

Both route families share the SAME compiled graph proxy (the v1
durable graph object) and the SAME thread-store implementation.

Sync ``invoke`` only — never ``ainvoke`` (ADR 0015 / spec). The
prod runtime composer (``compile_for_runtime``) is T8's
responsibility; this module lazy-imports it inside
``compile_for_http`` so it is not a module-load dependency.

No auth, no streaming on ``/ask`` (workspace routes stream per ADR
0026). No cookies. v1 is a localhost-only, JSON-in / JSON-out
server over the shared graph.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from fin.corpus.errors import IngestMissingError
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.graph import ConcurrentRunError, compile_graph
from fin.host.operator_env import apply_dotenv, warn_if_mcp_retrieve_unreachable
from fin.host.run_payload import _run_to_response_payload
from fin.host.state import EVIDENCE_RESET, reset_input_state
from fin.host.thread_store import default_thread_store


logger = logging.getLogger(__name__)


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

    See ``compile_graph`` for the underlying factory. The HTTP-side
    helper exists so the prod runtime composer (T8) can be cached
    at module level when the CLI / HTTP process boots once.

    Production path (``checkpointer=None``, ``model=None``)
    lazy-imports T8's ``compile_for_runtime`` and caches the
    resulting graph. If ``FIN_CHECKPOINT_URL`` is unset (typical
    WSL / laptop dev boot without Postgres), falls back to
    ``compile_for_runtime_dev`` which uses ``InMemorySaver`` and
    emits a WARNING that state is non-persistent. The fallback is
    one-way per process: a single dev-mode boot stays dev-mode
    for its lifetime. Tests pass explicit ``checkpointer`` +
    ``model`` args and bypass both paths entirely.

    ``FIN_SCRIPTED_MODEL`` (T7) selects the scripted-provider boot
    for the browser e2e lane instead. It is an explicit operator
    opt-in checked BEFORE the provider paths — never a fallback
    reached by a missing credential or an absent checkpoint URL.
    """
    if checkpointer is None and model is None:
        global _http_graph_singleton
        with _http_singleton_lock:
            if _http_graph_singleton is None:
                from fin.host.compile import (
                    MissingCheckpointURLError,
                    compile_for_runtime,
                    compile_for_runtime_dev,
                )
                from fin.host import scripted_boot

                if scripted_boot.scripted_boot_requested():
                    _http_graph_singleton = (
                        scripted_boot.compile_for_scripted_e2e()
                    )
                    return _http_graph_singleton
                try:
                    _http_graph_singleton = compile_for_runtime()
                except MissingCheckpointURLError:
                    _http_graph_singleton = compile_for_runtime_dev()
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
    description=(
        "Thin POST /ask + workspace routes over the v1 host graph "
        "(ADR 0013, ADR 0026)."
    ),
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


def get_thread_store() -> Any:
    """FastAPI dependency that returns the thread store.

    Picks the right implementation based on ``FIN_CHECKPOINT_URL``
    (matches the existing ``compile_for_runtime`` pattern). Tests
    override this to inject an in-memory store.
    """
    return default_thread_store()


# Mount the workspace routes (T1, #28) on the same FastAPI app.
# Done at module load so ``uvicorn fin.host.http:app`` exposes the
# workspace surface out of the box (spec / ADR 0013 / ADR 0024).
from fin.host.workspace_http import mount as _mount_workspace_routes  # noqa: E402

_mount_workspace_routes(app, get_graph=get_graph, get_thread_store=get_thread_store)

# Mount the built SPA (``web/dist``) if it exists - see the
# ``mount_spa(app)`` call below (after the function is defined).
# Same-origin per spec; a missing dist leaves the API routes
# untouched so tests and API-only boots are unaffected.


# ----- exception handlers -----


@app.exception_handler(ConcurrentRunError)
async def concurrent_run_handler(request: Request, exc: ConcurrentRunError) -> JSONResponse:
    """Map ``ConcurrentRunError`` → 409 with
    ``{"error": "concurrent_run"}``.

    ADR 0016 / spec: "at most one active run per thread." A
    second concurrent ``invoke`` raises the typed
    ``ConcurrentRunError``; the surface translates it to 409
    without leaking internal details. Critically: 409 happens
    BEFORE any SSE headers are sent — the exception fires from
    inside the route handler before ``StreamingResponse`` starts
    yielding frames.
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
    paths / sha256 do not leak.
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

    # T2: explicit reset of run-scoped fields (ADR 0016 / spec
    # SC62). Without ``EVIDENCE_RESET`` the prior run's evidence
    # would carry into the new run, letting the citation gate
    # ``done`` on stale pages. The helper clears the six
    # run-scoped fields per ADR 0016 / spec line 34.
    raw = graph.invoke(
        reset_input_state(body.question),
        config={"configurable": {"thread_id": thread_id}},
    )

    # Shared mapping (also used by the workspace send route) so the
    # two endpoints cannot drift on status extraction / bounce-marker
    # stripping / citation shaping. See ``fin.host.run_payload``.
    payload = _run_to_response_payload(raw)

    return AskResponse(
        status=payload["status"],
        answer=payload["answer"],
        citations=[
            CitationSchema(**cit) for cit in payload["citations"]
        ],
        thread_id=thread_id,
        reason=payload["reason"] or None,
    )


# ----- SPA mount + uvicorn runner -----


# Module-level guard so the SPA is mounted at most once even
# when both the import-time call and ``serve()`` would otherwise
# try to register the catch-all twice (Starlette raises
# ``AssertionError: Prefix must not end with ...`` on a duplicate
# path). Set inside ``mount_spa``; checked before mounting.
_spa_mounted: bool = False


def mount_spa(app: FastAPI, dist_path: str | None = None) -> None:
    """Serve the built SPA from ``web/dist`` same-origin.

    Mounts ``dist_path`` as a static directory and registers a
    catch-all that returns ``index.html`` for any non-API path so
    client-side routing (``/t/{id}``) does not 404 on reload.

    This is intentionally minimal — the SPA is small (a few
    hundred KB) and FastAPI's StaticFiles is sufficient. No auth
    or caching headers — the operator runs this server locally.

    Mounting is idempotent: a second ``mount_spa`` call on the
    same app is a no-op (the module-level ``_spa_mounted`` flag
    short-circuits before Starlette sees a duplicate path).

    Missing dist: the API surface still serves, but the operator
    MUST see a WARNING — silent API-only mode on a server that
    should be serving the SPA is a footgun. ``web/dist`` is a
    build artifact and is not committed; the build step is
    documented in ``README.md``
    (``cd web && npm install && npm run build``).

    Path-traversal hardening: the catch-all resolves the
    candidate path and checks containment under ``dist`` BEFORE
    serving. Without this guard, ``GET /..%2f..%2fpyproject.toml``
    would escape ``web/dist`` on a no-auth ``0.0.0.0:8000``
    server and serve repo files (a path-traversal CVE).
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    global _spa_mounted
    if _spa_mounted:
        return

    if dist_path is None:
        # ``src/fin/host/http.py`` -> ``src`` (parents[2]) -> project
        # root (parents[3]) -> ``web/dist`` (the Vite output).
        dist_path = str(Path(__file__).resolve().parents[3] / "web" / "dist")

    dist = Path(dist_path)
    if not dist.is_dir():
        # WHY a WARNING (not INFO / DEBUG): ``web/dist`` is a build
        # artifact, not committed source. A missing directory on
        # boot means the operator's SPA build step never ran (or
        # the wrong path is configured); silent API-only serving
        # would hide that from everyone but the first person to
        # open the UI. The module is imported once per process,
        # so the cost is one log line even across a full test
        # session.
        logger.warning(
            "SPA dist directory %s not found; only API routes will "
            "serve. Run `cd web && npm install && npm run build`.",
            dist,
        )
        return  # SPA not built yet — API routes still serve.

    _spa_mounted = True
    resolved_dist = dist.resolve()

    app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa_catchall(path: str):  # type: ignore[no-redef]
        from fastapi.responses import FileResponse

        # Path-traversal containment: resolve the candidate
        # against the dist root and reject anything that
        # escapes it. URL-encoded ``%2e%2e%2f`` (``../``)
        # segments decode to ``../`` in Starlette path
        # converters; ``Path.resolve()`` collapses the
        # resulting absolute path which ``is_relative_to``
        # then refuses. Without this guard,
        # ``GET /..%2f..%2fpyproject.toml`` would return 200
        # with ``web/dist`` siblings served — an unauth path
        # traversal on a 0.0.0.0:8000 server (Standards #1).
        candidate = (dist / path).resolve()
        if not candidate.is_relative_to(resolved_dist):
            raise HTTPException(status_code=404, detail="not found")
        if candidate.is_file():
            return FileResponse(candidate)
        # Otherwise return index.html so SPA routing can take over.
        return FileResponse(dist / "index.html")


# Register the SPA catch-all AFTER every API route so ``/api/*``
# and ``/ask`` win on path conflicts.
mount_spa(app)


def serve() -> None:
    """Entry point for ``fin-serve``: bind ``0.0.0.0:8000`` and serve.

    Spec: "Bind ``0.0.0.0:8000``, no auth, no cookies. Document as a
    local operator server (WSL)." Lazy-imports uvicorn so the
    module stays importable in environments without the runtime
    (tests, --help, etc.).

    NOTE: the SPA is mounted at module import time, NOT here.
    Re-calling ``mount_spa`` from ``serve`` was previously
    guarded only by the dist-existence check, which is a
    false idempotent (a second call would re-register the
    catch-all). The module-level ``_spa_mounted`` flag
    short-circuits the second call correctly.

    ``apply_dotenv`` runs first so ``FIN_*`` from a local ``.env``
    are in ``os.environ`` before the first request compiles the
    graph. Explicit process env still wins.

    The retrieve preflight runs on the assembled env, before
    uvicorn binds: the graph compiles lazily on the first request,
    so without this the "retrieve is wired to a server you do not
    have" signal would only reach the operator as an unexplained
    evidence-free answer.
    """
    apply_dotenv()
    warn_if_mcp_retrieve_unreachable()
    import uvicorn

    uvicorn.run("fin.host.http:app", host="0.0.0.0", port=8000, log_level="info")


__all__ = [
    "AskRequest",
    "AskResponse",
    "CitationSchema",
    "app",
    "compile_for_http",
    "get_graph",
    "get_thread_store",
    "mount_spa",
    "serve",
]
