"""Application-owned resource lifetime (server lifecycle, ADR 0101).

The server owns what it acquires: the compiled runtime's checkpointer
connection, the four background workers, and the one run-control
registry that both the HTTP stop door and attachment auto-resume
signal. This module holds the three pieces that make that ownership
observable without knowing what any of them are:

* :class:`RuntimeHandle` — the compiled graph plus an idempotent
  ``close``. A caller releases the checkpointer without knowing that a
  ``PostgresSaver`` context manager exists behind it.
* :class:`StartupSteps` — the ordered startup seams, injected. The
  deployed seams live in ``fin.host.http``; a test supplies its own, so
  startup order, rollback and shutdown are provable without a live
  model, database or worker thread.
* :class:`ApplicationResources` — the owner. ``startup`` runs the steps
  in dependency order and records what each one acquired; ``shutdown``
  releases that record in reverse, exactly once, including after a
  startup that raised part way through.

Why the owner is an object and not a module-level registry: a registry
of live stacks keeps a connection alive until interpreter exit and
makes "who releases this?" unanswerable, which is the state that makes
a second application in one process, a refused boot and a repeated
lifespan impossible to reason about.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from fin.host.run_call_supervisor import RunCallSupervisor
from fin.host.run_control import RunControl
from fin.host.turn_receipts import default_receipts

if TYPE_CHECKING:
    from fin.host.receipt_export import ReceiptExportConfig


logger = logging.getLogger(__name__)


# uvicorn reads this key for its worker count, so it is where a declared
# multi-process deployment is actually visible to a process serving
# ``fin.host.http:app``.
WORKER_COUNT_ENV_VAR = "WEB_CONCURRENCY"

# uvicorn's own spelling of the same declaration. The environment key is
# only half the story: ``uvicorn --workers 4 fin.host.http:app`` leaves it
# unset in the worker children, which import this app with nothing to
# read. The flag is on their inherited ``sys.argv`` (multiprocessing's
# spawn carries it), so it is the only place a spawned child can still
# see the declaration it was forked from.
WORKERS_FLAG = "--workers"


class UnsupportedProcessCountError(RuntimeError):
    """The deployment declared a server process count other than one.

    The execution owner here is in-process: the run-control registry,
    the compiled graph lock and the worker singletons exist once per
    process, so two processes would each believe they own every run.
    Multi-process ownership needs a separately reviewed database-backed
    design, not a second process against this one.

    ``declared`` is the value AS WRITTEN, because the operator has to see
    it: a refusal that rewrites ``two`` as ``-1`` reports a number the
    deployment never typed.
    """

    def __init__(self, declared: str, *, source: str = WORKER_COUNT_ENV_VAR) -> None:
        self.declared = declared
        self.source = source
        super().__init__(
            f"{source}={declared!r} declares {declared!r} server "
            "process(es); this server owns run control, the compiled graph "
            "lock and its workers in ONE process. Serve a single process, or "
            "select a separately reviewed database-backed ownership design."
        )


def declared_process_count(
    process_count: int | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> tuple[str, str]:
    """The server-process count this deployment declared, as written.

    Returns the raw declaration and where it was read from. An explicit
    ``process_count`` wins, then uvicorn's environment key, then its
    command-line flag — the order uvicorn itself resolves them, so what
    this refuses is what uvicorn would run. An undeclared deployment
    answers ``("1", ...)``: nothing declared, so one process.
    """
    if process_count is not None:
        return str(int(process_count)), "the declared process count"
    raw = (environ if environ is not None else os.environ).get(
        WORKER_COUNT_ENV_VAR, ""
    ).strip()
    if raw:
        return raw, WORKER_COUNT_ENV_VAR
    flagged = _workers_flag(argv if argv is not None else sys.argv)
    if flagged is not None:
        return flagged, f"{WORKERS_FLAG} {flagged!r}"
    return "1", WORKER_COUNT_ENV_VAR


def _workers_flag(argv: Sequence[str]) -> str | None:
    """The value after ``--workers`` (or ``--workers=``), as written."""
    for index, token in enumerate(argv):
        if token == WORKERS_FLAG:
            following = argv[index + 1] if index + 1 < len(argv) else ""
            return following
        if token.startswith(f"{WORKERS_FLAG}="):
            return token[len(WORKERS_FLAG) + 1 :]
    return None


def require_single_process(
    process_count: int | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> None:
    """Refuse a declared server process count other than one.

    Run at boot, where nothing has been acquired yet, so importing this
    module in an environment that declares four workers still yields a
    usable app object — ``fin-serve`` and the lifespan are what refuse.
    A declaration this cannot read as a count is a refusal too, never a
    guess at one.
    """
    declared, source = declared_process_count(
        process_count, environ=environ, argv=argv
    )
    if declared.strip().lstrip("+") == "1":
        return
    raise UnsupportedProcessCountError(declared, source=source)


class RuntimeHandle:
    """The closeable compiled runtime one application owns.

    ``graph`` is the compiled proxy callers invoke; ``close`` releases
    whatever the compile acquired and is safe to call more than once, so
    a partial startup, a shutdown and a repeated lifespan can all reach
    for it. A handle built around an injected graph carries no release:
    the caller that supplied the graph still owns it, and ``close`` is
    a no-op rather than a claim on someone else's resource.
    """

    def __init__(
        self,
        graph: Any,
        *,
        release: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._graph = graph
        self._release = release
        self._on_close = on_close
        self._closed = False
        self._lock = threading.Lock()

    @property
    def graph(self) -> Any:
        return self._graph

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release the runtime once. Repeat calls do nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            release, self._release = self._release, None
            on_close = self._on_close
        try:
            if release is not None:
                release()
        finally:
            # WHY a finally: the release is whatever the compile entered and
            # can raise; ``on_close`` is what keeps a dependency cache from
            # answering with a graph whose checkpointer this handle already
            # dropped, so skipping it would leave that dead graph reachable.
            if on_close is not None:
                on_close()


# What one seam call acquires, declared per composition so that "every
# acquisition has a release" is a property a test can read rather than a
# claim the owner makes about itself:
#
# * ``SEAM_ACQUIRED`` — this application holds it and releases it.
# * ``SEAM_PROCESS`` — the built value is a process singleton another
#   caller in this process shares, so the PROCESS owns its connections
#   and an application-owned close would break that other caller.
# * ``SEAM_PROBE`` — the call acquires nothing: a check that refuses, or
#   a one-shot child that exits on its own.
SeamOwnership = Literal["acquired", "process", "probe"]
SEAM_ACQUIRED: SeamOwnership = "acquired"
SEAM_PROCESS: SeamOwnership = "process"
SEAM_PROBE: SeamOwnership = "probe"


@dataclass(frozen=True)
class StartupSteps:
    """The ordered startup seams one application composition injects.

    Every field is resolved at startup rather than captured at
    construction, so a deployment boots through the functions this
    process actually holds. The order of the fields is the boot order;
    the ``stop_*`` fields are their reversals.

    ``ownership`` names what each seam call acquires, and it has no
    default: a seam nobody classified cannot be reasoned about, and a
    composition that grows an acquisition without saying who releases it
    is the state this class exists to prevent.
    """

    require_single_process: Callable[[], Any]
    apply_operator_settings: Callable[[], Any]
    attachment_budgets: Callable[[], Any]
    receipt_export_config: Callable[[], Any]
    require_checkpoint_url: Callable[[], Any]
    preflight_ingest_storage: Callable[[], Any]
    publish_knowledge_mcp: Callable[[], Any]
    compile_runtime: Callable[..., RuntimeHandle]
    start_ingest_worker: Callable[[], Any]
    thread_store: Callable[[], Any]
    attachment_service: Callable[[], Any]
    start_attachment_prep_worker: Callable[[Any], Any]
    start_attachment_resume_worker: Callable[..., Any]
    start_invalidation_drain_worker: Callable[..., Any]
    start_receipt_export_worker: Callable[..., Any]
    stop_ingest_worker: Callable[..., None]
    stop_attachment_prep_worker: Callable[..., None]
    stop_attachment_resume_worker: Callable[..., None]
    stop_invalidation_drain_worker: Callable[..., None]
    stop_receipt_export_worker: Callable[..., None]
    workspace_root: Callable[[], Any]
    ownership: Mapping[str, SeamOwnership] = field(default_factory=dict)


class ApplicationResources:
    """The resources one application owns, and their order.

    ``startup`` composes the boot in dependency order: settings, then the
    deployment's own upload bounds, then the configuration that must refuse
    before anything is acquired, then the durable storage probe, then the
    compiled runtime, then the four workers. Every acquired resource is
    pushed onto one stack, so a failure at any stage releases exactly what
    ran before it — in reverse — and leaves nothing running.

    ``startup`` and ``shutdown`` are idempotent and mutually exclusive,
    which is what lets ``serve()`` start the boot before binding and the
    server's own lifespan start it again, and lets a second lifespan
    enter/exit without leaking a worker, a session or a checkpointer.
    """

    def __init__(
        self,
        *,
        build_steps: Callable[[], StartupSteps],
        run_control: RunControl | None = None,
        run_call_supervisor: RunCallSupervisor | None = None,
    ) -> None:
        self._build_steps = build_steps
        self.run_control = run_control if run_control is not None else RunControl()
        # An INJECTED supervisor is reused by every boot of this owner; a
        # default one is minted per boot, because shutdown closes it and a
        # second lifespan must not compile a graph against a dead loop.
        self._supervisor = run_call_supervisor
        self.run_call_supervisor = (
            run_call_supervisor if run_call_supervisor is not None else RunCallSupervisor()
        )
        self._handle: RuntimeHandle | None = None
        self._live: ExitStack | None = None
        self._lock = threading.Lock()
        #: The exporter selection this boot resolved, published so the
        #: export worker and the developer span view answer from ONE
        #: value instead of each reading the environment.
        self._receipt_export_config: "ReceiptExportConfig | None" = None

    @property
    def graph(self) -> Any:
        """The compiled graph this application serves, or ``None``."""
        handle = self._handle
        return None if handle is None else handle.graph

    @property
    def receipt_export_config(self) -> "ReceiptExportConfig | None":
        """The trace exporter selection this boot resolved, or ``None``.

        ``None`` is both "no exporter selected" and "this application is
        not booted"; the span view reads it only while serving, so the two
        cannot be confused by a caller that has started.
        """
        return self._receipt_export_config

    @property
    def running(self) -> bool:
        with self._lock:
            return self._live is not None

    @staticmethod
    def _releasing(
        seam: str, release: Callable[[], None]
    ) -> Callable[[], None]:
        """One release that cannot skip the releases around it.

        ``ExitStack`` stops at the first callback that raises, so a
        checkpointer that will not close would leave every worker this
        boot started running. Each release is attempted exactly once and
        a failure is logged against the seam that owns it — the operator
        reads which resource did not release from one place, instead of
        from a traceback raised out of the middle of the unwind.
        """

        def _release_once() -> None:
            try:
                release()
            except BaseException:
                logger.exception("releasing the %s failed", seam)

        return _release_once

    @classmethod
    def _own_worker(
        cls,
        opened: ExitStack,
        seam: str,
        start: Callable[[], Any],
        stop: Callable[[], None],
    ) -> None:
        """Start one worker seam and register the stop that ends it.

        Start and stop are named together at the one place that knows
        what is running, so a worker cannot be added to the boot here
        without the stop that ends it.
        """
        acquired = start()
        if callable(acquired):
            def release() -> None:
                acquired()
        else:
            # EXIT: older injected StartupSteps return only a thread or None
            # and still pair their start with the declared legacy stop seam.
            release = stop
        opened.callback(cls._releasing(seam, release))

    def startup(self) -> None:
        """Acquire the boot's resources in dependency order."""
        with self._lock:
            if self._live is not None:
                return
            steps = self._build_steps()
            opened = ExitStack()
            # WHY here and not per compile: the compiled graph keys its
            # idempotence cache on this object, so a supervisor minted
            # inside the compile would hand a cached graph a supervisor the
            # application cannot close, and one minted per request would
            # never be the object the running graph holds. Minting before
            # the compile makes the boot — not the compile — its owner.
            if self._supervisor is None:
                self.run_call_supervisor = RunCallSupervisor()
            try:
                # WHY first: a deployment that would run several server
                # processes over one in-process owner is refused here,
                # where nothing has been acquired yet — and here rather
                # than at import, because ``fin.host.http`` is imported
                # from request paths as well as from the boot.
                steps.require_single_process()
                steps.apply_operator_settings()
                receipt_export_config = steps.receipt_export_config()
                # Published as soon as it is resolved, so the span view
                # reports the same selection the worker below starts from.
                self._receipt_export_config = receipt_export_config
                steps.attachment_budgets()
                # WHY both refusals precede preflight: they are pure
                # configuration reads, so an invalid optional exporter
                # configuration cannot leave an acquired store, worker or
                # runtime behind.
                steps.require_checkpoint_url()
                steps.preflight_ingest_storage()
                steps.publish_knowledge_mcp()
                handle = steps.compile_runtime(
                    run_call_supervisor=self.run_call_supervisor
                )
                opened.callback(self._releasing("compile_runtime", handle.close))
                self._handle = handle
                self._own_worker(
                    opened,
                    "ingest_worker",
                    steps.start_ingest_worker,
                    steps.stop_ingest_worker,
                )
                service = steps.attachment_service()
                self._own_worker(
                    opened,
                    "attachment_prep_worker",
                    lambda: steps.start_attachment_prep_worker(service),
                    steps.stop_attachment_prep_worker,
                )
                store = steps.thread_store()
                root = steps.workspace_root()
                projector = default_receipts(store)
                self._own_worker(
                    opened,
                    "attachment_resume_worker",
                    lambda: steps.start_attachment_resume_worker(
                        graph=handle.graph,
                        store=store,
                        service=service,
                        workspace_root=root,
                        run_control=self.run_control,
                        receipts=projector,
                    ),
                    steps.stop_attachment_resume_worker,
                )
                self._own_worker(
                    opened,
                    "invalidation_drain_worker",
                    lambda: steps.start_invalidation_drain_worker(store=store),
                    steps.stop_invalidation_drain_worker,
                )
                self._own_worker(
                    opened,
                    "receipt_export_worker",
                    lambda: steps.start_receipt_export_worker(
                        store=projector.store,
                        config=receipt_export_config,
                        projector=projector,
                    ),
                    steps.stop_receipt_export_worker,
                )
            except BaseException:
                try:
                    opened.close()
                finally:
                    # WHY a finally: a release that raised used to leave the
                    # handle reachable while ``running`` was already False,
                    # so ``.graph`` handed out a runtime this owner had
                    # already tried to release. The selection goes with it:
                    # a boot that failed owns no exporter.
                    self._handle = None
                    self._receipt_export_config = None
                raise
            self._live = opened.pop_all()

    def shutdown(self) -> None:
        """Stop the runs, then the workers, then close the runtime.

        The run sweep comes FIRST and is not a release: a run still in
        flight holds the graph's per-thread lock and a model call the
        runtime is about to drop the checkpointer under. Stopping every
        live run is what lets those runs unwind cooperatively while the
        runtime they hold is still standing.

        The supervisor closes with the resources rather than with the
        compile, because a second boot of the same application reuses the
        same supervisor and a handle that closed it would leave the next
        boot with a dead event loop. The one thing that outlives an
        application here is the process, so ``stop_all`` still runs on
        every shutdown: a lifespan that re-entered leaves nothing running
        behind it either way.
        """
        with self._lock:
            live, self._live = self._live, None
            self._handle = None
            self._receipt_export_config = None
        if live is None:
            return
        self.run_control.stop_all(reason="application_shutdown")
        try:
            live.close()
        finally:
            self._close_supervisor()

    def _close_supervisor(self) -> None:
        """Close the run-call loop once, logging a cleanup that failed.

        A supervisor that cannot settle its loop is a leak, not a boot
        failure: the runtime is already released and the server is already
        going down, so the operator reads one line naming the resource
        instead of a traceback raised out of the shutdown.
        """
        if self.run_call_supervisor.closed:
            return
        try:
            self.run_call_supervisor.close()
        except Exception:
            logger.exception("closing the run-call supervisor failed")

    @asynccontextmanager
    async def lifespan(self, app: Any) -> AsyncIterator[None]:
        """The FastAPI lifespan that owns this composition.

        Startup and shutdown are blocking (a compile opens a database
        connection; a worker stop joins a thread), so they run off the
        event loop: blocking it for the length of a drain would also
        freeze every in-flight response during shutdown.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.startup)
        try:
            yield
        finally:
            await loop.run_in_executor(None, self.shutdown)


__all__ = [
    "SEAM_ACQUIRED",
    "SEAM_PROCESS",
    "SEAM_PROBE",
    "WORKER_COUNT_ENV_VAR",
    "WORKERS_FLAG",
    "ApplicationResources",
    "RuntimeHandle",
    "SeamOwnership",
    "StartupSteps",
    "UnsupportedProcessCountError",
    "declared_process_count",
    "require_single_process",
]
