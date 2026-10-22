"""The v1 host HTTP surface (T9 + workspace T1).

A thin FastAPI app on top of the SAME compiled graph object the CLI
uses (ADR 0013 / spec success criteria item 8). Two route families
sit on this app:

* ``POST /ask`` — sync invoke, JSON in / JSON out. v1 eval surface.
* ``/api/threads/...`` — workspace surface (T1): thread CRUD,
  message send, corpus list. See ``fin.host.workspace_http``.

Both route families share the SAME compiled graph proxy (the v1
durable graph object) and the SAME thread-store implementation.

Sync ``invoke`` only — never ``ainvoke`` (ADR 0015 / spec). The
prod runtime composer (``compile_for_runtime``) is T8's
responsibility; the application boundary lazy-imports it inside
``compile_for_http`` so it is not a module-load dependency.

No auth, no streaming on ``/ask`` (workspace routes stream per ADR
0026). No cookies. v1 is a localhost-only, JSON-in / JSON-out
server over the shared graph.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, model_serializer

from fin.corpus.errors import IngestMissingError
# The application boundary owns the composition. The transport reaches it
# through the module (so the owner is what runs) and re-exports the
# public names other callers already import from here.
from fin.host.application import composition
from fin.host.application.composition import (
    attachment_budgets,
    compile_for_http,
    http_runtime_handle,
    startup_steps,
)
from fin.host.application.lifetime import (
    ApplicationResources,
    StartupSteps,
)
from fin.host.attachment.attachment_budgets import AttachmentBudgets
from fin.host.grounded.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.graph import ConcurrentRunError, KnowledgeMCPFaultError
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_FAULT_ERROR,
    KNOWLEDGE_MCP_FAULT_MESSAGE,
)
from fin.host.run_control import RunAdmissionError, RunControl
from fin.host.run_payload import (
    _run_to_response_payload,
    citation_sections_from_evidence,
)
from fin.host.state import EVIDENCE_RESET, reset_input_state
from fin.host.thread_store import default_thread_store
from fin.host.filing_monitor import default_filing_monitor
from fin.host.observability import ObservabilityCollector, default_collector
from fin.host.watchlist import default_watchlist_store


logger = logging.getLogger(__name__)


# Hidden bounce marker the gate stamps on internal SystemMessages
# (T6 / ADR 0021). Defensive: HTTP serialization must NEVER leak
# this string into the response body. The literal lives in
# ``fin.host.grounded.citation_gate`` (SSOT) — imported here so a rename
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

    ``kind`` / ``document_id`` / ``attachment_id`` / ``version`` /
    ``unit`` / ``display`` / ``filename`` always come from the matched
    evidence item — never from the model's claim (ADR 0021). Exactly one
    source id is set: ``document_id`` for a company citation,
    ``attachment_id`` for an attachment one, whose ``filename`` is the
    safe display name. ``display`` says how ``unit`` is presented
    (``"page"`` for a physical PDF unit). ``quote`` is the ≥32-char
    substring the gate matched.

    A ``database-row`` citation sets NEITHER document id and carries its
    retained snapshot on ``database_row``, so a caller of this
    non-streaming door can read the same snapshot the SSE door and the
    turn table show — and so opening it never means re-querying the
    ledger (ADR 0091, spec 8.1).
    """

    # ``kind`` / ``display`` carry no default: an incomplete row must fail
    # here rather than surface as a company citation presenting a non-PDF
    # unit as a physical page.
    kind: str
    document_id: str
    attachment_id: str = ""
    version: str
    unit: int
    display: str
    filename: str = ""
    quote: str
    # A display-only label derived by the host from the matched
    # EvidenceItem's trusted full unit text. It is never accepted from a
    # model citation claim and is omitted when unavailable.
    section: str | None = None
    # The retained snapshot a ledger citation opens; ``None`` for the two
    # document kinds.
    database_row: dict[str, Any] | None = None

    # ONE mechanism for the omit-null rule. FastAPI (and any caller that
    # asks for JSON) encodes this model through the model layer, which is
    # exactly where a ``@model_serializer`` fires — including for a nested
    # list inside ``AskResponse``, so the enclosing response needs no rule
    # of its own. The snapshot key belongs to a ledger row; emitting
    # ``"database_row": null`` on a document citation would change the
    # body every existing caller of ``/ask`` sees, so the key is ABSENT
    # rather than null — byte-identical to the gate's own wire dict.
    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> Any:
        out = handler(self)
        if isinstance(out, dict):
            if out.get("database_row") is None:
                out.pop("database_row", None)
            if out.get("section") is None:
                out.pop("section", None)
        return out


class AskResponse(BaseModel):
    """Response body for ``POST /ask``.

    ``citations`` is always present (empty list when ``status`` is
    not ``done``). ``reason`` is populated only on ``refuse`` or
    ``step_cap``. ``answer`` is empty on ``refuse`` / ``step_cap``.

    ``visual_claims`` is the run's visual-claim rows (ADR 0089): a separate
    typed channel from ``citations`` — a generated visual observation is
    never a source quote — so the non-streaming door exposes the same
    provenance rows the streaming door and GET history carry. Always
    present (``[]`` when the run produced none) so a caller never branches
    on absence.
    """

    status: Literal["done", "refuse", "step_cap"]
    answer: str = ""
    citations: list[CitationSchema] = Field(default_factory=list)
    thread_id: str
    reason: str | None = None
    visual_claims: list[dict[str, Any]] = Field(default_factory=list)


# ----- factory -----


# The memory port owns a Postgres pool when memory is ON, so it is built
# once per process too (see ``get_memory_port``).
_http_memory_singleton: Any | None = None
_http_memory_lock = threading.Lock()


# ----- app + dependency -----


def get_graph(request: Request = cast(Request, None)) -> Any:
    """FastAPI dependency that returns the compiled graph.

    Tests override this via ``app.dependency_overrides[get_graph]``
    to inject a graph built with ``InMemorySaver`` +
    ``ScriptedChatModel`` (no live LLM, no Postgres). Production
    returns the graph owned by the serving application's lifespan. A
    direct legacy call without a request still uses ``compile_for_http``.
    """
    if request is not None:
        resources = getattr(request.app.state, "fin_resources", None)
        graph = None if resources is None else resources.graph
        if graph is not None:
            return graph
    # EXIT: direct legacy calls and consumers before a live app lifespan
    # retain the historical process compiler fallback. The application
    # boundary owns that compile, so the call goes through it.
    return composition.compile_for_http()


def get_thread_store() -> Any:
    """FastAPI dependency that returns the thread store.

    Picks the right implementation based on ``FIN_CHECKPOINT_URL``
    (matches the existing ``compile_for_runtime`` pattern). Tests
    override this to inject an in-memory store.
    """
    return default_thread_store()


def get_attachment_service() -> Any:
    """FastAPI dependency that returns the thread attachment service.

    Tests override this to inject a service over in-memory stores; the
    product boot gets the env-selected durable stores, built once per
    process with interrupted preparation already resumed.
    """
    return default_attachment_service()


def get_attachment_budgets() -> AttachmentBudgets:
    """FastAPI dependency that returns the deployed attachment bounds.

    One instance per process, read through
    :func:`fin.host.attachment.attachment_budgets.from_environ` — the module that
    owns the four bounds and their validation. Missing, non-positive, or
    unbounded values raise ``AttachmentBudgetConfigError`` here, so a
    server that cannot state its own upload cap fails at boot rather
    than serving an unbounded door.

    The instance belongs to the application boundary
    (:func:`fin.host.application.composition.attachment_budgets`), so
    the boot seam and this dependency share one answer.

    Tests override this via
    ``app.dependency_overrides[get_attachment_budgets]`` to re-deploy a
    smaller cap without shipping megabytes through the test process.
    """
    return composition.attachment_budgets()


def get_memory_port() -> Any:
    """FastAPI dependency that returns the configured memory port.

    Built once per process. The ON port owns a Postgres pool, so building
    one per request would open a pool per request and exhaust the
    server's connections. Memory stays OFF unless ``FIN_MEMORY_ENABLED``
    is set, in which case nothing here touches Postgres at all.

    Tests override this via ``app.dependency_overrides[get_memory_port]``.
    """
    global _http_memory_singleton
    with _http_memory_lock:
        if _http_memory_singleton is None:
            # Imported lazily: compile owns the env contract, and it
            # pulls in psycopg for the checkpoint path.
            from fin.host.compile import build_memory_port_for_runtime

            _http_memory_singleton = build_memory_port_for_runtime()
        return _http_memory_singleton


def get_watchlist_store() -> Any:
    """FastAPI dependency that returns the watchlist / notice store.

    T4 workflow. Location comes from ``FIN_WATCHLIST_PATH``
    (default ``data/workspace/watchlist.json``); tests override this
    to point at a tmp path. Separate from the thread store because a
    watchlist is operator preference, not turn presentation state.
    """
    return default_watchlist_store()


def get_filing_monitor() -> Any:
    """FastAPI dependency for the optional real filing monitor."""
    return default_filing_monitor()


def get_feedback_store() -> Any:
    """FastAPI dependency that returns the thumbs-feedback store.

    T8 (#T8): backs ``POST /api/threads/{id}/runs/{run_id}/feedback``
    and the ``feedback`` field on GET history. Selection mirrors
    ``get_thread_store`` exactly: ``FIN_CHECKPOINT_URL`` set ->
    Postgres-backed store (ADR 0035/0036 SSOT); unset -> in-memory.
    Process-lifetime singleton behavior (the Postgres path owns a
    connection pool) is owned by ``fin.host.feedback``'s own cache —
    one caching layer, one owner — so this dependency delegates
    exactly like ``get_thread_store`` does.

    Tests override this via ``app.dependency_overrides[get_feedback_store]``.
    """
    from fin.host.feedback import default_feedback_store

    return default_feedback_store()


def get_observability_collector() -> ObservabilityCollector:
    """FastAPI dependency that returns the per-process observability
    collector.

    T9: backs ``GET /api/observability`` and per-turn terminal-status
    recording on the SSE send route. The singleton itself is built
    once per process by ``fin.host.observability.default_collector``
    (lock-guarded, same pattern as the other singletons in this
    module) — it lives below ``graph`` because the retrieve tool
    records empty-hit rewrite retries on the SAME collector, and
    ``graph`` cannot import this module (``http`` imports ``graph``).

    Tests override this via
    ``app.dependency_overrides[get_observability_collector]`` with a
    fresh ``ObservabilityCollector`` for hermetic counter assertions.
    """
    return default_collector()


# The workspace routes (T1), the health door and the demo
# controls are mounted by ``create_app`` below, which every application
# — the deployed one and any second instance in one process — goes
# through, so no mount can exist without the ownership that goes with
# it.
# Wave A (spec SC8 / ADR 0049): ``get_workspace_root`` is re-exported
# under its own name so tests can override the seam in one place
# (``app.dependency_overrides[get_workspace_root]``) for both the
# ``/ask`` route and the workspace send / resume routes.
from fin.host.workspace_http import (  # noqa: E402
    _visual_claims_wire_payload,
    default_attachment_service,
    get_workspace_root,
    mount as _mount_workspace_routes,
    start_attachment_prep_worker,
    start_attachment_resume_worker,
    start_ingest_worker,
    start_invalidation_drain_worker,
)

# ``GET /health`` (SC9): checkpoint reachability, separate from
# the boot-scoped ``GET /api/status`` (ADR 0033). Mounted before
# ``mount_spa`` below so the SPA catch-all cannot shadow it.
from fin.host.health import mount as _mount_health_route  # noqa: E402

# ``GET /api/threads/{id}/turns/{id}/receipt``: the developer view of one
# turn's operational receipt. Its own route, mounted before ``mount_spa``
# below so the SPA catch-all cannot shadow it, and built over the same
# ``get_thread_store`` dependency so a test's override redirects it.
from fin.host.receipts_http import mount as _mount_receipt_routes  # noqa: E402

# ``GET /api/threads/{id}/turns/{id}/spans``: the developer trace view of
# one turn. It reports the exporter selection this boot RESOLVED (the same
# value the export worker starts from) and answers a normal 200 with no
# spans when tracing was never on. Mounted before ``mount_spa`` for the
# same reason as the receipt view.
from fin.host.trace_spans_http import mount as _mount_trace_span_routes  # noqa: E402

# Demo controls are registered after the product API routes and before the
# SPA catch-all, so the static fallback cannot hide their JSON responses.
from fin.host.demo_http import mount as _mount_demo_routes  # noqa: E402

_get_workspace_root = get_workspace_root


# ----- exception handlers -----


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


async def knowledge_mcp_fault_handler(
    request: Request, exc: KnowledgeMCPFaultError
) -> JSONResponse:
    """Map ``KnowledgeMCPFaultError`` → 409 with the stable
    ``knowledge_mcp_fault`` token and the operator copy 「知识 MCP 故障」.

    The three conversation mouths raise this when the boot snapshot
    (``GET /api/status``'s ``knowledge_mcp``) is 缺 or 坏. The body copy
    is the SAME string the SPA left corner renders, so the surface and
    the server cannot drift.
    """
    return JSONResponse(
        status_code=409,
        content={
            "error": KNOWLEDGE_MCP_FAULT_ERROR,
            "message": KNOWLEDGE_MCP_FAULT_MESSAGE,
        },
    )


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


def ask(
    body: AskRequest,
    graph: Any = Depends(get_graph),
    workspace_root: Path = Depends(_get_workspace_root),
) -> "AskResponse | JSONResponse":
    """Run one v1 host-graph ``invoke`` on a ``POST /ask`` request.

    The handler is a sync ``def`` and calls ``graph.invoke`` — never
    ``graph.ainvoke`` (ADR 0015: durable CLI/HTTP require sync
    ``invoke`` with the sync saver).

    ``thread_id`` is auto-created as a UUID4 if the caller omits
    it (ADR 0016). Status mapping:

    * ``done`` → ``answer`` + ``citations`` populated; ``reason``
      omitted.
    * ``refuse`` → empty ``answer`` + empty ``citations``;
      ``reason`` = ``status_reason``. The vocabulary is not
      re-listed here: ``fin.host.named_failures`` owns the 具名失败
      codes and ``fin.host.grounded.citation_gate`` owns its own verdicts.
    * ``step_cap`` → empty ``answer`` + empty ``citations``;
      ``reason`` = ``"step_cap"``.

    The hidden bounce marker is stripped from ``answer`` and
    ``reason`` defensively before serialization (ADR 0021).

    Wave A (spec SC8 / ADR 0049): the resolved default 工作区 rides
    the run config as ``configurable.workspace_root`` — the graph
    reads it per call and never bakes its own root.
    """
    thread_id = body.thread_id or str(uuid.uuid4())

    # T2: explicit reset of run-scoped fields (ADR 0016 / spec
    # SC62). Without ``EVIDENCE_RESET`` the prior run's evidence
    # would carry into the new run, letting the citation gate
    # ``done`` on stale pages. The helper clears the six
    # run-scoped fields per ADR 0016 / spec line 34.
    raw = graph.invoke(
        reset_input_state(body.question),
        config={
            "configurable": {
                "thread_id": thread_id,
                "workspace_root": str(workspace_root),
            }
        },
    )

    # search-mode host T2: when the grounded ask PAUSES on the 检索档
    # choice window, the sync ``invoke`` returns with the pending
    # interrupt (the ADR 0015 sync contract — it never raises on
    # /ask). The unattended surface answers typed 409 ``choice_pending``
    # and closes nothing: the run stays resumable on the workspace
    # ``POST /threads/{id}/resume`` with the operator pick. /ask must
    # never pick a 档 on the operator's behalf (no silent C).
    if raw.get("__interrupt__"):
        return JSONResponse(
            status_code=409, content={"error": "choice_pending"}
        )

    # Shared mapping (also used by the workspace send route) so the
    # two endpoints cannot drift on status extraction / bounce-marker
    # stripping / citation shaping. See ``fin.host.run_payload``.
    payload = _run_to_response_payload(raw)

    # ADR 0089: the visual-claim channel is NOT part of the citation
    # payload (run_payload is quote-channel only), so the non-streaming
    # door reads it straight off the final state and normalises it with the
    # SAME projector the streaming door uses — the two surfaces cannot
    # drift, and the rows never ride ``citations``. ``[]`` when the run
    # produced none, matching the persisted column and the SSE default.
    return AskResponse(
        status=payload["status"],
        answer=payload["answer"],
        citations=[
            CitationSchema(
                **{
                    **citation,
                    **(
                        {"section": section}
                        if section is not None
                        else {}
                    ),
                }
            )
            for citation, section in zip(
                payload["citations"],
                citation_sections_from_evidence(
                    payload["citations"], raw.get("evidence")
                ),
                strict=True,
            )
        ],
        thread_id=thread_id,
        reason=payload["reason"] or None,
        visual_claims=_visual_claims_wire_payload(raw.get("visual_claims"))[0],
    )


# ----- SPA mount + uvicorn runner -----


# The idempotence guard lives on the APP, not on this module: two
# applications in one process are two servers, and a process-global
# guard silently left the second one with neither ``/assets`` nor the
# catch-all — and no warning, because the early return came before the
# missing-dist check.
_SPA_MOUNTED = "fin_spa_mounted"


def mount_spa(app: FastAPI, dist_path: str | None = None) -> None:
    """Serve the built SPA from ``web/dist`` same-origin.

    Mounts ``dist_path`` as a static directory and registers a
    catch-all that returns ``index.html`` for any non-API path so
    client-side routing (``/t/{id}``) does not 404 on reload.

    This is intentionally minimal — the SPA is small (a few
    hundred KB) and FastAPI's StaticFiles is sufficient. No auth
    or caching headers — the operator runs this server locally.

    Mounting is idempotent PER APP: a second ``mount_spa`` call on the
    same app is a no-op (the app's own flag short-circuits before
    Starlette sees a duplicate path, which it rejects), while a second
    app in the same process mounts its own.

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

    if app.state.__dict__.get(_SPA_MOUNTED):
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

    setattr(app.state, _SPA_MOUNTED, True)
    resolved_dist = dist.resolve()

    app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def spa_catchall(path: str, request: Request):  # type: ignore[no-redef]
        from fastapi.responses import FileResponse

        # Unmapped ``/api/*`` is missing API, not a client-side route.
        # A GET-only catch-all would serve ``index.html`` (200) on GET
        # and 405 on POST — which re-animates unshipped surfaces such
        # as ``/api/workspaces`` whenever ``web/dist`` exists.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(status_code=405, detail="method not allowed")

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



# ----- application construction -----


def create_app(
    *,
    steps: Callable[[], StartupSteps] | None = None,
    process_count: int | None = None,
    run_control: RunControl | None = None,
) -> FastAPI:
    """Build one fin server: routes, mounts, and the resources it owns.

    Construction acquires nothing. It wires the same dependency functions
    every caller already overrides — ``get_graph``, ``get_thread_store``
    and the rest keep their identity, so ``app.dependency_overrides``
    keeps working — and hands the app a lifespan that owns composition,
    worker supervision and shutdown (``ApplicationResources``). That
    ordering is why importing this module starts no worker and opens no
    connection.

    The ``RunControl`` created here is the ONE registry the mounted
    workspace routes answer Stop against and the one the attachment
    auto-resumer registers its runs in; a background run the operator
    cannot stop is not a background run they can be told about.

    ``steps`` replaces the deployed boot seams — a complete set already
    permits a real lifespan with no model, database or operator
    settings — and ``process_count`` states the server-process count a
    caller already knows. Nothing else is injectable: production passes
    neither, and a seam that only a test can set is not a seam. The
    owned resources are published as ``app.state.fin_resources``.

    Construction never refuses a deployment: a count other than one is
    refused at BOOT, as the first thing the lifespan does, so importing
    this module from a request path cannot raise.
    """
    resources = ApplicationResources(
        build_steps=(
            steps
            if steps is not None
            else partial(
                composition.startup_steps, declared_process_count=process_count
            )
        ),
        run_control=run_control,
    )
    app = FastAPI(
        title="fin v1 host agent",
        version="0.1.0",
        description=(
            "Thin POST /ask + workspace routes over the v1 host graph "
            "(ADR 0013, ADR 0026)."
        ),
        lifespan=resources.lifespan,
    )
    app.state.fin_resources = resources
    app.exception_handler(ConcurrentRunError)(concurrent_run_handler)
    # An admission refusal and a lost lock race are the same operator
    # fact — this thread already has a run — so they answer the same 409
    # rather than teaching the SPA a second code for one condition.
    app.exception_handler(RunAdmissionError)(concurrent_run_handler)
    app.exception_handler(KnowledgeMCPFaultError)(knowledge_mcp_fault_handler)
    app.exception_handler(IngestMissingError)(ingest_missing_handler)
    app.post("/ask", response_model=AskResponse)(ask)
    _mount_workspace_routes(
        app,
        get_graph=get_graph,
        get_thread_store=get_thread_store,
        get_attachment_service=get_attachment_service,
        get_attachment_budgets=get_attachment_budgets,
        get_watchlist_store=get_watchlist_store,
        get_memory_port=get_memory_port,
        get_filing_monitor=get_filing_monitor,
        get_feedback_store=get_feedback_store,
        get_observability_collector=get_observability_collector,
        get_workspace_root=_get_workspace_root,
        run_control=resources.run_control,
    )
    _mount_health_route(app)
    # The developer receipt and trace views read stores the workspace
    # routes write to, and are mounted before the SPA catch-all for the
    # same reason health is.
    _mount_receipt_routes(app, get_thread_store=get_thread_store)
    _mount_trace_span_routes(app)
    _mount_demo_routes(app)
    # LAST: the SPA catch-all matches every remaining path, so it has to
    # be registered after ``/ask``, the workspace API, health and the
    # demo controls or it would shadow them.
    mount_spa(app)
    return app


# The deployed server. ``uvicorn fin.host.http:app`` and ``fin-serve``
# both resolve here; the module-level object starts nothing until its
# lifespan runs.
app = create_app()


# ----- uvicorn runner -----


def serve() -> None:
    """Entry point for ``fin-serve``: bind ``0.0.0.0:8000`` and serve.

    Spec: "Bind ``0.0.0.0:8000``, no auth, no cookies. Document as a
    local operator server (WSL)." Lazy-imports uvicorn so the
    module stays importable in environments without the runtime
    (tests, --help, etc.).

    Thin on purpose: the boot order, the rollback and the shutdown order
    live in the app's lifespan, and this only starts it, binds, and stops
    it again. Starting it HERE rather than letting the bind do it keeps
    the failure posture: operator settings, the attachment bounds, the
    ingest-storage preflight, the knowledge-MCP probe, the compile and
    the four workers all run before the socket exists, so a deployment
    with no ``FIN_CHECKPOINT_URL`` — or with a bound it cannot state —
    refuses the boot instead of the first request.

    The app object is passed rather than the ``"fin.host.http:app"``
    import string: this function has already started that very
    lifespan, and an import string would resolve a second, separately
    constructed module. No reload or worker mode is used, so the two
    spellings differ only in when the module is imported.
    """
    resources: ApplicationResources = app.state.fin_resources
    resources.startup()
    import uvicorn

    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    finally:
        resources.shutdown()


__all__ = [
    "AskRequest",
    "AskResponse",
    "CitationSchema",
    "app",
    "compile_for_http",
    "create_app",
    "get_attachment_budgets",
    "get_attachment_service",
    "get_feedback_store",
    "get_filing_monitor",
    "get_graph",
    "get_observability_collector",
    "get_thread_store",
    "get_watchlist_store",
    "http_runtime_handle",
    "mount_spa",
    "serve",
    "startup_steps",
]
