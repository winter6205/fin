"""Application capability: the composition both entry points share.

This module is the ONE observable owner of

* the application composition — the boot seams (:func:`startup_steps`) and
  the process-scoped resources behind them; and
* the host-graph composition — the compiled product graph, the runtime
  handle that owns it, and the caches that make one process answer with
  one graph.

Both entry points reach it from here: the HTTP surface through
:func:`compile_for_http` and :func:`http_runtime_handle`, the CLI
through :func:`product_graph_factory`. Nothing here imports the
transport module — the boundary sits below the surfaces, not above them.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

from fin.host.application.lifetime import (
    SEAM_ACQUIRED,
    SEAM_PROCESS,
    SEAM_PROBE,
    RuntimeHandle,
    StartupSteps,
    require_single_process,
)
from fin.host.attachment.attachment_budgets import AttachmentBudgets, from_environ
from fin.host.evidence.evidence_session_registry import (
    EvidenceSessionBinding,
    EvidenceSessionRegistry,
)
from fin.host.evidence.structured_evidence_identity import (
    StructuredEvidenceIdentity,
    StructuredEvidenceIdentityConfigError,
    load_structured_evidence_identity,
)
from fin.host.graph import compile_graph
from fin.host.knowledge_mcp import publish_knowledge_mcp
from fin.host.operator_settings import apply_operator_settings
from fin.host.receipt_export import load_receipt_export_config
from fin.host.receipt_export_worker import start_application_receipt_export_worker
from fin.host.run_call_supervisor import RunCallSupervisor
from fin.host.thread_store import default_thread_store
from fin.host.workspace_http import (
    default_attachment_service,
    get_workspace_root,
    start_application_attachment_prep_worker,
    start_application_attachment_resume_worker,
    start_application_ingest_worker,
    start_application_invalidation_drain_worker,
    stop_attachment_prep_worker,
    stop_attachment_resume_worker,
    stop_invalidation_drain_worker,
    stop_ingest_worker,
)


logger = logging.getLogger(__name__)


# ----- the host-graph composition -----

# Module-level singleton cache for the prod runtime composer (T8).
# Test calls into ``compile_for_http`` with explicit ``checkpointer``
# + ``model`` bypass this cache entirely and rely on
# ``compile_graph``'s built-in idempotence (T6 LRU keyed on
# ``id(checkpointer) + id(model) + ...``).
_http_graph_singleton: Any | None = None
# REENTRANT on purpose. ``compile_for_http`` and ``http_runtime_handle``
# hold this lock across the compile, and that compile reaches
# ``_product_run_call_supervisor``, which takes the SAME lock to build the
# process-wide supervisor. A plain ``Lock`` deadlocks the calling thread on
# itself the first time the product boot runs without a supplied supervisor
# (ADR 0103's work found this: ``test_compile_for_http_uses_scripted_graph_only_on_optin``
# hung). The guarded regions are short and the re-entry is the same
# thread, so a reentrant lock keeps the mutual exclusion and drops the
# self-deadlock.
_http_singleton_lock = threading.RLock()

# The owner of whatever ``_http_graph_singleton`` holds. The cache is
# shared (every dependency asks for the same graph); the checkpointer
# connection behind it is not, so exactly one handle may release it and
# only the composition that compiled it ever gets that handle.
_http_runtime_handle: RuntimeHandle | None = None

# The process-wide run-call supervisor behind every product graph.
# Process-scoped for the same reason the graph singleton is: the compiled
# proxy is cached by IDENTITY, so two supervisors would mean two graphs
# for one boot and one of them could never be closed. The application
# that compiled the graph owns its close (see ``ApplicationResources``);
# a compile reached outside one — the legacy ``compile_for_http`` fallback
# — gets this instance, and the process is what outlives it either way.
_http_run_call_supervisor: RunCallSupervisor | None = None

# The exporter's own release, captured at start. The owner runs the
# callable the start seam returned (which IS this one); the module global
# is what lets the declared stop seam name a release rather than a
# no-op, and it is cleared on the first stop so a repeated shutdown
# cannot end the same worker twice.
_receipt_export_release: Callable[[], None] | None = None

# The deployed attachment bounds are read once per process: they are
# deployment configuration, which cannot change under a running server,
# and reading them at boot is what makes an unusable bound abort the
# startup instead of the first upload (see ``attachment_budgets``).
_http_attachment_budgets_singleton: AttachmentBudgets | None = None
_http_attachment_budgets_lock = threading.Lock()


def attachment_budgets() -> AttachmentBudgets:
    """The deployed attachment bounds, one instance per process.

    Read through :func:`fin.host.attachment.attachment_budgets.from_environ`
    — the module that owns the four bounds and their validation. Missing,
    non-positive, or unbounded values raise
    ``AttachmentBudgetConfigError`` here, so a server that cannot state
    its own upload cap fails at boot rather than serving an unbounded
    door.

    The HTTP dependency and the boot seam share THIS instance: the
    bounds are deployment configuration, so one read per process is the
    one answer both of them get.
    """
    global _http_attachment_budgets_singleton
    with _http_attachment_budgets_lock:
        if _http_attachment_budgets_singleton is None:
            _http_attachment_budgets_singleton = from_environ(environ=os.environ)
        return _http_attachment_budgets_singleton


def _product_run_call_supervisor(
    supplied: RunCallSupervisor | None,
) -> RunCallSupervisor:
    """The one supervisor this process's product graphs run under."""
    global _http_run_call_supervisor
    if supplied is not None:
        return supplied
    with _http_singleton_lock:
        if _http_run_call_supervisor is None:
            _http_run_call_supervisor = RunCallSupervisor()
        return _http_run_call_supervisor


def _forget_http_graph(graph: Any) -> None:
    """Drop the cached graph once its runtime has been released."""
    global _http_graph_singleton, _http_runtime_handle
    with _http_singleton_lock:
        if _http_graph_singleton is not graph:
            return
        _http_graph_singleton = None
        _http_runtime_handle = None


def _compile_product_handle(**kwargs: Any) -> RuntimeHandle:
    """Compile this process's graph and return the owning handle.

    The two product boots (scripted e2e and the durable runtime) differ
    only in what they hand ``compile_graph``; the caching that makes the
    zero-argument :func:`compile_for_http` shape work lives in the
    caller, so a caller that needs the handle cannot be bypassed by a
    cached graph it did not compile.
    """
    from fin.host import scripted_boot

    if scripted_boot.scripted_boot_requested():
        return RuntimeHandle(scripted_boot.compile_for_scripted_e2e())
    # T2f durable graph gate: the production boot wires the
    # process's attachment service so the grounded route
    # gains the preparation interrupt. The RESOLVER is
    # passed, not the built service: construction waits
    # until the compile actually happens, so a boot that
    # raises on a missing URL or a patched factory never
    # pays an attachment-store or database construction.
    from fin.host.compile import compile_runtime_handle

    return compile_runtime_handle(
        attachment_service=default_attachment_service,
        # Forwarded, and only on this branch, because this is the
        # PRODUCT path: without it ``serve()``'s configured Evidence
        # identity would reach a factory that drops it and the
        # retrieve leg would silently stay unbound. ``None`` when no
        # identity was configured, which is this call unchanged.
        evidence=kwargs.get("evidence"),
        run_call_supervisor=_product_run_call_supervisor(
            kwargs.get("run_call_supervisor")
        ),
    )


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
    resulting graph. A missing ``FIN_CHECKPOINT_URL`` raises
    ``MissingCheckpointURLError`` — there is no InMemorySaver
    fallback (ADR 0015). Tests pass explicit ``checkpointer`` +
    ``model`` args and bypass this path entirely.

    ``FIN_SCRIPTED_MODEL`` (T7) selects the scripted-provider boot
    for the browser e2e lane instead. It is an explicit operator
    opt-in checked BEFORE the provider path — never a fallback
    reached by a missing credential or an absent checkpoint URL.
    """
    if checkpointer is None and model is None:
        global _http_graph_singleton, _http_runtime_handle
        with _http_singleton_lock:
            if _http_graph_singleton is None:
                handle = _compile_product_handle(**kwargs)
                _http_runtime_handle = handle
                _http_graph_singleton = handle.graph
            return _http_graph_singleton
    return compile_graph(
        checkpointer=checkpointer,
        model=model,
        **kwargs,
    )


def http_runtime_handle(**kwargs: Any) -> RuntimeHandle:
    """The closeable runtime this application's lifespan owns.

    The composition that compiles the graph receives the owning handle
    and releases the checkpointer on shutdown. An application that joins
    an already-compiled graph — a caller that ran a dependency before
    the lifespan — receives a view that releases nothing: the connection
    belongs to whoever compiled it, and two owners closing one
    connection is how a live server loses its checkpointer.
    """
    global _http_graph_singleton, _http_runtime_handle
    with _http_singleton_lock:
        if _http_runtime_handle is None or _http_graph_singleton is None:
            handle = _compile_product_handle(**kwargs)
            _http_runtime_handle = handle
            _http_graph_singleton = handle.graph
            return RuntimeHandle(
                handle.graph,
                release=handle.close,
                # WHY: the dependency cache must not answer with a graph
                # whose checkpointer this application already released.
                on_close=lambda: _forget_http_graph(handle.graph),
            )
        return RuntimeHandle(_http_graph_singleton)


class ProductCompilerUnavailableError(RuntimeError):
    """The product graph compiler could not be imported.

    The CLI turns this into its own typed refusal. The boundary cannot
    raise the CLI's error type, and the CLI no longer imports
    ``fin.host.compile`` itself, so the boundary states the fact and
    the surface names it in its own words.
    """


def product_graph_factory(
    evidence_factory: Callable[[], Any | None] | None = None,
) -> Any:
    """The product graph a CLI entry point runs on.

    The CLI's entry to the SAME host-graph composition the HTTP surface
    reaches through :func:`compile_for_http`: ``fin.host.compile`` owns
    the product boot, and both entry points meet it here.

    The Evidence identity is the operator's configuration, so the caller
    resolves it — as a factory, so the compiler's own availability is
    settled before anything else is built. The import is lazy for the
    same reason it is in the HTTP path: the prod runtime composer pulls
    in psycopg, and neither ``--help`` nor an import of this module may
    pay for it.
    """
    try:
        from fin.host.compile import compile_for_runtime
    except ModuleNotFoundError as exc:
        raise ProductCompilerUnavailableError(str(exc)) from exc
    evidence = evidence_factory() if evidence_factory is not None else None
    if evidence is None:
        return compile_for_runtime()
    return compile_for_runtime(evidence=evidence)


# ----- the application composition -----


def _require_checkpoint_url() -> None:
    """Refuse a deployment with no durable checkpointer before acquiring.

    ADR 0015 has no in-memory fallback for the product boot, so an unset
    ``FIN_CHECKPOINT_URL`` is fatal — and it has to be fatal at a point
    where nothing has been opened yet. The MCP probe spawns a child
    process and the ingest preflight opens the upload door's durable
    storage, so a check that ran after either of them would refuse the
    boot only after spending resources on it. ``FIN_SCRIPTED_MODEL`` is
    the one declared exception: that boot states its in-memory saver on
    purpose.
    """
    from fin.host import scripted_boot
    from fin.host.compile import MissingCheckpointURLError

    if scripted_boot.scripted_boot_requested():
        return
    if not (os.environ.get("FIN_CHECKPOINT_URL") or "").strip():
        raise MissingCheckpointURLError()


def _compile_http_runtime(
    *, run_call_supervisor: RunCallSupervisor | None = None
) -> RuntimeHandle:
    """Compile this application's graph with the operator's identity.

    P2-I2: the Evidence identity is operator configuration, read here — after
    ``apply_operator_settings``, so ``~/.fin/settings.yaml`` is part of the boot — and INJECTED into the
    compile rather than defaulted inside it (ADR B1: no process-level fallback,
    no default scope). An unconfigured host keeps the pre-P2-I2 graph: the bare
    call below is the only answer that host ever takes, so nothing about its
    behaviour, its logging, or its zero-argument ``compile_for_http`` shape
    changes. A configured one gets a session registry plus the resolver that
    hands out that identity, which is the pair ``compile_graph`` demands.
    #
    # A MISCONFIGURED identity is reported and the host still boots. Refusing to
    # start would mean a half-filled settings file takes fin down entirely to fix an
    # optional subsystem, and ``load_jev_config`` — the precedent this module
    # follows — never raises for a value it is willing to default. Serving without
    # the identity leaves structured evidence unavailable, which is a degradation
    # the operator is told about, not a stale-read hazard.
    """
    try:
        identity: StructuredEvidenceIdentity | None = load_structured_evidence_identity()
    except StructuredEvidenceIdentityConfigError as exc:
        logger.warning("structured evidence identity is misconfigured; serving without it: %s", exc)
        return _compile_product_handle(run_call_supervisor=run_call_supervisor)
    if identity is None:
        return _compile_product_handle(run_call_supervisor=run_call_supervisor)
    # Lazy for the same reason ``preflight_ingest_storage`` is: the launch
    # command is ``fin.host.compile``'s, and this module does not import it
    # at load time. It is the same iknow server every other MCP adapter in
    # this repo spawns, so the Evidence read rides the corpus the retrieve
    # leg is already talking to.
    from fin.host.compile import DEFAULT_MCP_COMMAND

    owned = ExitStack()
    try:
        sessions = EvidenceSessionRegistry(
            DEFAULT_MCP_COMMAND,
            transport_env=identity.child_storage_env(),
        )
        owned.callback(sessions.close_all)
        compiled = _compile_product_handle(
            evidence=EvidenceSessionBinding(
                sessions=sessions,
                resolve=identity.resolver(),
            ),
            run_call_supervisor=run_call_supervisor,
        )
        owned.callback(compiled.close)
    except BaseException:
        owned.close()
        raise
    runtime = owned.pop_all()
    return RuntimeHandle(compiled.graph, release=runtime.close)


def _start_receipt_export_worker(
    *, store: Any, config: Any, projector: Any = None
) -> Callable[[], None]:
    """Start the exporter and remember the release the owner will use.

    The start seam already returns the worker's own stop, so the owner
    runs THAT release. This wrapper exists only so the declared
    ``stop_receipt_export_worker`` seam has a live meaning: a seam that
    names a resource no call can end is a release nobody can prove, and
    the exporter's two daemon threads are exactly the resource a shutdown
    must be able to end.
    """
    stop = start_application_receipt_export_worker(
        store=store, config=config, projector=projector
    )
    global _receipt_export_release
    _receipt_export_release = stop
    return stop


def _stop_receipt_export_worker() -> None:
    """Release the exporter this process started, exactly once."""
    global _receipt_export_release
    stop, _receipt_export_release = _receipt_export_release, None
    if stop is not None:
        stop()


def startup_steps(
    *, declared_process_count: int | None = None
) -> StartupSteps:
    """The deployed boot seams, resolved at each startup.

    Read from this module's globals rather than captured once, so the
    boot is the module's own composition: a test (or an operator's
    ``compile_for_runtime`` replacement) that patches a seam here is
    patching the boot that actually runs.

    ``declared_process_count`` is the one deployment fact construction
    cannot read for itself; the environment and the command line are
    resolved at boot, where a refusal still precedes every acquisition.
    """
    # Lazy: the ingest storage module and its database driver stay off
    # this module's import surface for tests and ``--help``.
    from fin.corpus.ingest_storage import preflight_ingest_storage

    return StartupSteps(
        require_single_process=lambda: require_single_process(
            declared_process_count
        ),
        apply_operator_settings=apply_operator_settings,
        attachment_budgets=attachment_budgets,
        receipt_export_config=load_receipt_export_config,
        require_checkpoint_url=_require_checkpoint_url,
        preflight_ingest_storage=preflight_ingest_storage,
        publish_knowledge_mcp=publish_knowledge_mcp,
        compile_runtime=_compile_http_runtime,
        start_ingest_worker=start_application_ingest_worker,
        thread_store=default_thread_store,
        attachment_service=default_attachment_service,
        start_attachment_prep_worker=start_application_attachment_prep_worker,
        start_attachment_resume_worker=start_application_attachment_resume_worker,
        start_invalidation_drain_worker=start_application_invalidation_drain_worker,
        start_receipt_export_worker=_start_receipt_export_worker,
        stop_ingest_worker=stop_ingest_worker,
        stop_attachment_prep_worker=stop_attachment_prep_worker,
        stop_attachment_resume_worker=stop_attachment_resume_worker,
        stop_invalidation_drain_worker=stop_invalidation_drain_worker,
        stop_receipt_export_worker=_stop_receipt_export_worker,
        workspace_root=get_workspace_root,
        ownership={
            # Calls that read, refuse or publish a state: nothing survives
            # them, so nothing is owed.
            "require_single_process": SEAM_PROBE,
            "apply_operator_settings": SEAM_PROBE,
            "require_checkpoint_url": SEAM_PROBE,
            "publish_knowledge_mcp": SEAM_PROBE,
            "workspace_root": SEAM_PROBE,
            # WHY process: each of these answers a per-process singleton
            # that the HTTP dependency, the ingest door and the CLI share
            # in this process, so the PROCESS owns the connection behind
            # it and an application-owned close would break those callers.
            "attachment_budgets": SEAM_PROCESS,
            "receipt_export_config": SEAM_PROBE,
            "thread_store": SEAM_PROCESS,
            "attachment_service": SEAM_PROCESS,
            # Building the ingest ports opens the durable connections the
            # ingest service then uses, and its only reset is a test
            # seam — the process holds them for as long as it serves
            # uploads.
            "preflight_ingest_storage": SEAM_PROCESS,
            # Released here: the runtime releases itself through its
            # handle, every worker through its own stop seam.
            "compile_runtime": SEAM_ACQUIRED,
            "start_ingest_worker": SEAM_ACQUIRED,
            "start_attachment_prep_worker": SEAM_ACQUIRED,
            "start_attachment_resume_worker": SEAM_ACQUIRED,
            "start_invalidation_drain_worker": SEAM_ACQUIRED,
            "start_receipt_export_worker": SEAM_ACQUIRED,
        },
    )


__all__ = [
    "ProductCompilerUnavailableError",
    "attachment_budgets",
    "compile_for_http",
    "http_runtime_handle",
    "product_graph_factory",
    "startup_steps",
]
