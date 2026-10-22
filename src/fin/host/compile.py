"""The durable v1 compile (T8).

The CLI (``python -m fin``) and the HTTP ask route (T9) both
call :func:`compile_for_runtime` to build the host graph for
production. Tests do NOT use this module — T6's ``compile_graph``
takes the ``checkpointer`` and ``model`` directly, and tests pass
``InMemorySaver()`` plus ``ScriptedChatModel``. This separation
keeps the test surface hermetic (no live Postgres, no live LLM)
and the production surface explicit (ADR 0015 + ADR 0022).

Why a helper at all
-------------------
ADR 0015 fixes ``PostgresSaver`` + ``FIN_CHECKPOINT_URL`` for
the durable v1 path. ADR 0022 fixes the runtime model selection
by ``FIN_MODEL`` (+ ``FIN_BASE_URL`` for the OpenAI-compatible
proxy on port 20128). Wiring both at every call site would
scatter the same lines (env read → model build → saver build →
``compile_graph``) across T7 and T9 and risk drift. Centralising
them here gives us one place to:

* raise ``MissingCheckpointURLError`` when ``FIN_CHECKPOINT_URL``
  is unset (a silent default would point checkpoints at the wrong
  host and lose state);
* wrap transient Postgres connection failures as
  ``CheckpointSetupError`` so the operator sees a typed stop
  kind, not a raw ``psycopg.OperationalError``;
* keep the ``PostgresSaver.from_conn_string`` connection open for
  the process lifetime — exiting the context manager closes the
  connection, which would break every subsequent ``invoke``.

Lifetime
--------
``PostgresSaver.from_conn_string`` is a ``contextmanager`` that
opens a single ``psycopg.Connection`` and closes it on exit. The
compiled graph needs that connection alive for every checkpoint
write, so :func:`compile_runtime_handle` keeps the ``ExitStack`` in
the returned :class:`~fin.host.application.lifetime.RuntimeHandle` and the
OWNER that asked for the handle closes it. No module-level registry
holds a live stack: a caller that wants the resources back calls
``handle.close()``, and one that does not keeps them for the life of
its own process.

:func:`compile_for_runtime` is the graph-only shape of the same
compile, kept for callers that own the process (the CLI). It cannot
release the connection — prefer the handle form in anything that
has a shutdown.

Lazy imports
------------
``ChatOpenAI`` and ``PostgresSaver`` are imported inside the
function — not at module top — so the module is importable even
when those heavy runtime deps are missing or have been replaced
in ``sys.modules`` by a test fixture. Production boot pulls the
real symbols; tests inject fakes.

The real saver is recognized by class identity, not by importing its
module: :func:`_is_installed_postgres_saver` matches the defining module
and name in the saver's MRO. Importing ``langgraph.checkpoint.postgres``
evaluates a module-level psycopg pool annotation, so it must stay inside
the production boot path. A test double is defined in its own module, so
it never matches and keeps the un-fenced pre-P1 shape.

URL contract (OpenAI SDK)
--------------------------
``ChatOpenAI`` is built on the official ``openai`` SDK; the
SDK appends ``/chat/completions`` to whatever ``base_url`` you
give it. So ``FIN_BASE_URL`` SHOULD include ``/v1`` — passing
``http://localhost:20128`` (without ``/v1``) would result in
requests to ``http://localhost:20128/chat/completions`` (404 at
the proxy). The default below is ``https://api.minimaxi.com/v1``;
the official CN Chat Completions API receives
``POST /v1/chat/completions``. Operators who still run a local
9router override ``FIN_BASE_URL`` (for example WSL
``http://172.31.128.1:20128/v1``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import ExitStack
from functools import wraps
from typing import TYPE_CHECKING, Any, Final, Mapping

import psycopg
from psycopg import conninfo

# T6's graph factory. Eagerly imported so tests can monkeypatch
# it via ``monkeypatch.setattr("fin.host.compile.compile_graph",
# ...)``. Lazy imports below cover the runtime deps.
from fin.host.application.lifetime import RuntimeHandle
from fin.host.graph import compile_graph
from fin.host.question.jev_choice import build_jev_client
from fin.host.memory_port import (
    DEFAULT_MEMORY_DREAM_BACKEND,
    MEMORY_DREAM_BACKEND_ENV_VAR,
    MEMORY_DREAM_BACKEND_KINDS,
    MemoryPort,
    build_memory_port,
)
from fin.host.runtime.admission import RuntimeCoordinator
from fin.host.runtime.schema import RuntimeStoreError
from fin.host.runtime.store import PostgresRuntimeStore
from fin.host.tools.ledger import PostgresToolOutcomeLedger
from fin.host.run_call_supervisor import (
    RunCallCancelled,
    RunCallContextRegistry,
    RunCallSupervisor,
    current_run_call_context,
)
from fin.host.stream_map import reasoning_preserving_chat_openai

if TYPE_CHECKING:
    # Type-only import: attachment_vision imports this module at runtime.
    from fin.host.attachment.attachment_prepare import AttachmentPreparationService


# The real saver's identity is recognized by its defining module and name,
# NOT by importing it here: importing ``langgraph.checkpoint.postgres``
# evaluates its module-level psycopg pool annotation, so it must stay inside
# the production boot where the dependency already exists.
_INSTALLED_POSTGRES_SAVER_MODULE: Final[str] = "langgraph.checkpoint.postgres"
_INSTALLED_POSTGRES_SAVER_NAME: Final[str] = "PostgresSaver"


# ----- public constants -----

# Project-owned env var name for the LLM API key (ADR 0022
# update 2026-08-27). Provider-agnostic by design — the previous
# name, ``ANTHROPIC_AUTH_TOKEN``, was a leftover from before the
# MiniMax cutover and confused operators who configured a MiniMax
# key while a legacy local-gateway key sat in their shell. Hard
# switch: there is NO fallback to the legacy name. A missing
# ``FIN_API_KEY`` falls through to the placeholder
# ``sk-no-key-required`` and the provider call returns 401 — a
# loud error signal rather than a silent use of an unexpected
# credential.
FIN_API_KEY_ENV = "FIN_API_KEY"

# Official MiniMax CN Chat Completions URL (the official ``openai``
# SDK appends ``/chat/completions`` to ``base_url``). The
# default INCLUDES ``/v1``. Overseas operators override with
# ``https://api.minimax.io/v1``. A local 9router override is
# still ``http://localhost:20128/v1``.
DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"

# The exact ``checkpoint.url`` shipped in ``settings.example.yaml``
# and asserted by the URL-match acceptance gate
# (spec success-criteria item 7).
EXPECTED_CHECKPOINT_URL = (
    "postgresql://fin:fin@localhost:5433/fin?sslmode=disable"
)

# ADR 0030: product retrieve is the iknow-memory MCP client. Both
# boot paths below select it explicitly — the fake port is the
# default pytest surface (``compile_graph``'s own default), never a
# shipping default. An operator without a live iknow-memory server
# opts out by name with ``FIN_RETRIEVE_ADAPTER=fake``.
DEFAULT_RETRIEVE_ADAPTER = "mcp"

# The launch command ``compile_graph`` uses when it auto-builds the
# product ``MCPRetrieveAdapter`` (``fin.host.graph``'s own default).
# Named here so the boot-time reachability probe in
# ``fin.host.operator_env`` checks the exact argv the graph would
# spawn instead of re-spelling the binary name.
# 兜底非契约 — operator may override; not a cross-repo API.
DEFAULT_MCP_COMMAND: tuple[str, ...] = ("iknow-memory",)

# Bounded durable writes on the checkpointer's connection
# (specs/qa-chain-server-runtime.md §"Bounded execution budgets").
# Two mechanisms, one per acquisition kind:
# - ``connect_timeout`` rides in the conninfo: libpq bounds session
#   establishment itself.
# - ``statement_timeout`` / ``lock_timeout`` are SET on the open session
#   AFTER ``setup()`` returns, so every run-time checkpoint statement and
#   lock wait sits under one phase budget — the receipt store's
#   per-transaction idiom hoisted to session scope. They are NOT applied
#   during ``setup()``: its migrations include ``CREATE INDEX
#   CONCURRENTLY``, which a timeout cancels mid-build and leaves an
#   INVALID index that a later ``IF NOT EXISTS`` never repairs.
#
# Lock timeout is deliberately UNDER the statement timeout: a write
# blocked on someone else's lock fails in 2s (the spec's
# database-acquisition bound); a statement that runs without waiting on
# a lock gets the full 5s persistence phase.
CHECKPOINT_CONNECT_TIMEOUT_SECONDS: Final[int] = 2
CHECKPOINT_LOCK_TIMEOUT_MS: Final[int] = 2000
CHECKPOINT_STATEMENT_TIMEOUT_MS: Final[int] = 5000
_CHECKPOINT_LOCK_POLL_SECONDS: Final[float] = 0.025

# Durable Runtime admission limits (ADR 0103). Both are configuration, so
# an impossible value stops the boot instead of surfacing as a request
# outcome. The defaults match ``RuntimeCoordinator``.
RUNTIME_QUEUE_CAPACITY_ENV: Final[str] = "FIN_RUNTIME_QUEUE_CAPACITY"
RUNTIME_LEASE_SECONDS_ENV: Final[str] = "FIN_RUNTIME_LEASE_SECONDS"
DEFAULT_RUNTIME_QUEUE_CAPACITY: Final[int] = 8
DEFAULT_RUNTIME_LEASE_SECONDS: Final[float] = 30.0


class _RunAwareCheckpointLock:
    """Keep queued saver calls from outliving their stopped run."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock

    def __enter__(self) -> _RunAwareCheckpointLock:
        context = current_run_call_context()
        if context is None:
            # EXIT: Non-run callers retain the checkpointer's normal lock behavior.
            self._lock.acquire()
            return self

        token = context.stop_token
        while not token.is_set():
            if self._lock.acquire(timeout=_CHECKPOINT_LOCK_POLL_SECONDS):
                if token.is_set():
                    self._lock.release()
                    raise RunCallCancelled()
                return self
        raise RunCallCancelled()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        context = current_run_call_context()
        self._lock.release()
        if (
            exc is not None
            and context is not None
            and context.stop_token.is_set()
            and isinstance(
                exc,
                (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled),
            )
        ):
            raise RunCallCancelled() from exc
        return False


def _bind_checkpoint_run_cancellation(
    saver: Any,
    context_registry: RunCallContextRegistry | None = None,
) -> None:
    """Make saver waits observe run cancellation, including runner callbacks."""
    lock = getattr(saver, "lock", None)
    if lock is None:
        # EXIT: Small saver stand-ins without a lock have no queued work to cancel.
        return
    if not isinstance(lock, _RunAwareCheckpointLock):
        saver.lock = _RunAwareCheckpointLock(lock)
    if context_registry is None:
        return

    put_writes = getattr(saver, "put_writes", None)
    if not callable(put_writes) or getattr(
        put_writes, "_fin_run_call_context_registry", None
    ) is context_registry:
        return

    @wraps(put_writes)
    def run_aware_put_writes(config: Any, *args: Any, **kwargs: Any) -> Any:
        configurable = (
            config.get("configurable") if isinstance(config, Mapping) else None
        )
        thread_id = (
            configurable.get("thread_id")
            if isinstance(configurable, Mapping)
            else None
        )
        with context_registry.activate_thread(
            thread_id if isinstance(thread_id, str) else None
        ):
            return put_writes(config, *args, **kwargs)

    run_aware_put_writes._fin_run_call_context_registry = context_registry  # type: ignore[attr-defined]
    saver.put_writes = run_aware_put_writes


def bounded_checkpoint_conninfo(url: str) -> str:
    """Return ``url`` with libpq's connection-acquisition bound.

    Rewrites rather than replaces, so the addressed library, host and
    every operator-supplied parameter survive. A URL libpq cannot parse
    is returned verbatim — boot still fails exactly where it always did.
    An operator ``connect_timeout`` at least as tight as the budget is
    kept (explicit settings may tighten a budget, not raise it).
    """
    try:
        settings: dict[str, Any] = dict(conninfo.conninfo_to_dict(url))
    except psycopg.ProgrammingError:
        return url
    declared = settings.get("connect_timeout")
    if declared is not None:
        try:
            current = int(declared)
        except (TypeError, ValueError):
            # An unparseable value is the operator's; libpq rejects it at
            # connect exactly as it always did — we do not mask it.
            return conninfo.make_conninfo(**settings)
        if current <= CHECKPOINT_CONNECT_TIMEOUT_SECONDS:
            return conninfo.make_conninfo(**settings)
    settings["connect_timeout"] = str(CHECKPOINT_CONNECT_TIMEOUT_SECONDS)
    return conninfo.make_conninfo(**settings)


def checkpoint_session_bounds(url: str) -> tuple[tuple[str, int], ...]:
    """The session GUC bounds the checkpointer connection needs.

    A GUC the operator already set TIGHTER through the URL's ``options``
    is left alone (spec: explicit settings may tighten a budget but may
    not raise it). A URL libpq cannot parse yields no bounds — the
    connection never opens anyway.
    """
    try:
        settings = conninfo.conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return ()
    declared = settings.get("options")
    parts = declared.split() if isinstance(declared, str) else []
    bounds = []
    for name, value in (
        ("statement_timeout", CHECKPOINT_STATEMENT_TIMEOUT_MS),
        ("lock_timeout", CHECKPOINT_LOCK_TIMEOUT_MS),
    ):
        if not _declares_tighter_bound(parts, name, value):
            bounds.append((name, value))
    return tuple(bounds)


def bind_checkpoint_session_bounds(saver: Any, url: str) -> None:
    """SET the spec's statement/lock bounds on the saver's open session.

    The connection is in autocommit mode, so each ``SET`` holds for the
    session and covers every later checkpoint statement — ``put``,
    ``put_writes`` and the graph's reads alike. A saver stand-in without
    a connection (test fakes) has nothing to bind and is skipped.

    The bound is what makes a blocked durable write observable: the
    write raises inside the phase budget instead of waiting on the lock
    holder forever, so the run fails rather than appearing to have
    succeeded (spec: "A blocked operation that cannot be stopped is
    fenced so its late result cannot mutate current state").
    """
    session = getattr(saver, "conn", None)
    if session is None:
        return
    for name, bound_ms in checkpoint_session_bounds(url):
        session.execute(f"SET {name} = {bound_ms}")


def _declares_tighter_bound(parts: list[str], name: str, bound: int) -> bool:
    """True when ``options`` already sets ``name`` to at most ``bound`` ms."""
    marker = f"{name}="
    for index, part in enumerate(parts):
        if part == "-c" and index + 1 < len(parts) and parts[index + 1].startswith(marker):
            try:
                return int(parts[index + 1][len(marker) :]) <= bound
            except ValueError:
                # An unparseable value is the operator's; ours is appended and
                # libpq reports the conflict rather than this helper guessing.
                return False
    return False


# ----- typed errors -----


class HostCompileError(Exception):
    """Base class for typed errors raised by this module."""


class MissingModelError(HostCompileError):
    """``FIN_MODEL`` is absent or contains only whitespace."""

    def __init__(self, env_var: str = "FIN_MODEL") -> None:
        super().__init__(
            f"{env_var} must be a non-empty string to compile the host; configure it "
            "in operator settings or the environment, or pass "
            "`model_name=` explicitly."
        )
        self.env_var = env_var


class MissingCheckpointURLError(HostCompileError):
    """``FIN_CHECKPOINT_URL`` is unset (or empty).

    The helper MUST NOT default the URL — pointing durable
    checkpoints at the wrong host silently loses state (ADR
    0015). The ``env_var`` attribute carries the offending key
    name for diagnostics.
    """

    def __init__(self, env_var: str = "FIN_CHECKPOINT_URL") -> None:
        super().__init__(
            f"{env_var} is required for the durable v1 compile "
            "(ADR 0015); set it in the environment or pass "
            "`checkpointer_url=` explicitly."
        )
        self.env_var = env_var


class CheckpointSetupError(HostCompileError):
    """``PostgresSaver.setup()`` raised a transient connection error.

    The helper catches ``psycopg.OperationalError`` so the
    operator sees a typed stop kind (matching ADR 0015's "typed
    stop / error kinds" rule) instead of a raw psycopg exception.
    The original is chained via ``__cause__`` for debugging. The
    message intentionally does NOT echo the connection URL —
    URLs can carry embedded credentials; the typed-error contract
    keeps the URL out of the user-facing string.
    """


class RuntimeConfigError(HostCompileError):
    """A durable Runtime admission setting is missing, malformed or unusable."""

    def __init__(self, env_var: str, reason: str = "must be a positive number") -> None:
        self.env_var = env_var
        super().__init__(f"{env_var} {reason}")


# ----- env helpers -----


def _read_env(
    env: Mapping[str, str] | None, name: str
) -> str | None:
    """Read ``name`` from ``env`` (or ``os.environ`` when None).

    Returns ``None`` if absent or whitespace-only so callers can
    raise a typed error instead of dereferencing ``KeyError``.
    """
    src = env if env is not None else __import__("os").environ
    value = src.get(name)
    if value is None or value.strip() == "":
        return None
    return value


def runtime_queue_capacity(env: Mapping[str, str] | None = None) -> int:
    """The per-thread accepted-operation bound for this boot."""
    raw = _read_env(env, RUNTIME_QUEUE_CAPACITY_ENV)
    if raw is None:
        return DEFAULT_RUNTIME_QUEUE_CAPACITY
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeConfigError(
            RUNTIME_QUEUE_CAPACITY_ENV, "must be an integer"
        ) from exc
    if value < 1:
        raise RuntimeConfigError(
            RUNTIME_QUEUE_CAPACITY_ENV, "must be at least 1"
        )
    return value


def runtime_lease_seconds(env: Mapping[str, str] | None = None) -> float:
    """The lease bound for one worker attempt, in seconds."""
    raw = _read_env(env, RUNTIME_LEASE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_RUNTIME_LEASE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeConfigError(
            RUNTIME_LEASE_SECONDS_ENV, "must be a number"
        ) from exc
    if value <= 0:
        raise RuntimeConfigError(
            RUNTIME_LEASE_SECONDS_ENV, "must be positive"
        )
    return value


def _is_installed_postgres_saver(saver: Any) -> bool:
    """True only for the real saver class (or a subclass), never a double.

    Identity is the defining module and name in the MRO, which recognizes a
    real production saver without importing langgraph's Postgres module into
    every ``fin.host.compile`` importer. A test double is defined in its own
    module, so it never matches.
    """
    for base in type(saver).__mro__:
        if (
            getattr(base, "__module__", None)
            == _INSTALLED_POSTGRES_SAVER_MODULE
            and getattr(base, "__name__", None)
            == _INSTALLED_POSTGRES_SAVER_NAME
        ):
            return True
    return False


def _resolve_retrieve_adapter(env: Mapping[str, str] | None) -> str:
    """Return the retrieve adapter kind for a production boot.

    ``FIN_RETRIEVE_ADAPTER`` when set, otherwise
    ``DEFAULT_RETRIEVE_ADAPTER``. ``compile_graph`` validates the
    value and raises ``RetrieveAdapterConfigError`` on a typo, so a
    misspelled kind stops the boot instead of falling back.
    """
    return _read_env(env, "FIN_RETRIEVE_ADAPTER") or DEFAULT_RETRIEVE_ADAPTER


def _resolve_memory_dream_backend(env: Mapping[str, str] | None) -> str:
    """Return the dream backend kind for a production boot.

    ``FIN_MEMORY_DREAM_BACKEND`` when set, otherwise
    ``DEFAULT_MEMORY_DREAM_BACKEND``. The key is deliberately not
    ``FIN_RETRIEVE_ADAPTER``: ADR 0035 forbids sharing one implied
    config between the retrieve MCP and the memory MCP. A misspelled
    kind raises ``MemoryConfigError`` from
    :func:`fin.host.memory_port.build_memory_dream_backend`, so a typo
    stops the boot instead of falling back.
    """
    return (
        _read_env(env, MEMORY_DREAM_BACKEND_ENV_VAR)
        or DEFAULT_MEMORY_DREAM_BACKEND
    )


def build_memory_port_for_runtime(
    *, env: Mapping[str, str] | None = None
) -> MemoryPort:
    """Build the memory port for a production boot from ``env``.

    Mirrors ``compile_for_runtime``: this module owns the env contract,
    ``memory_port`` owns the port. Memory stays OFF unless
    ``FIN_MEMORY_ENABLED`` is set, so the default boot never opens a
    Postgres connection (SC2).
    """
    return build_memory_port(
        env=env, dream_backend_kind=_resolve_memory_dream_backend(env)
    )


# ----- shared model factory -----


def _build_default_model(
    *,
    model_name: str | None,
    base_url: str | None,
    env: Mapping[str, str] | None,
) -> Any:
    """Construct the production ``ChatOpenAI`` instance.

    Used by both :func:`compile_for_runtime` (production Postgres
    path) and :func:`compile_for_runtime_dev` (InMemorySaver helper)
    so model construction stays single-sourced. Lazy-imports
    ``ChatOpenAI`` so the module stays importable without the heavy
    runtime dep.

    Reads the required ``FIN_MODEL`` and optional ``FIN_BASE_URL`` /
    ``FIN_API_KEY`` when the corresponding arg is ``None``; otherwise
    uses the caller-supplied value verbatim. The ``extra_body`` /
    ``reasoning_split`` block is required by ADR 0022 + ADR 0026;
    keeping it here avoids drift between prod and dev.

    The class is ``stream_map``'s reasoning-preserving ``ChatOpenAI``
    subclass, not the stock one: asking for ``reasoning_split`` is
    only half the contract, because ``langchain-openai`` drops the
    provider's split channel before the chunk reaches the host.
    Building the model is the one place that choice can be made, so
    the wiring lives here and the converter itself lives beside the
    rest of the stream mapping.
    """
    if model_name is None:
        model_name = _read_env(env, "FIN_MODEL")
    if not isinstance(model_name, str) or not model_name.strip():
        raise MissingModelError()
    if base_url is None:
        base_url = _read_env(env, "FIN_BASE_URL") or DEFAULT_BASE_URL
    # Hard switch: only ``FIN_API_KEY`` is read. The legacy
    # ``ANTHROPIC_AUTH_TOKEN`` env var is intentionally NOT
    # honoured — a stale shell value with the old name used to
    # masquerade as the active credential (ADR 0022 update
    # 2026-08-27). A missing key falls through to the
    # placeholder so the provider returns 401 instead of
    # silently answering with an unexpected key.
    api_key = _read_env(env, FIN_API_KEY_ENV) or "sk-no-key-required"
    # OpenAI-compatible only: ``FIN_MODEL`` / ``FIN_BASE_URL`` /
    # ``FIN_API_KEY``. Vendor extras (MiniMax ``thinking.type=adaptive``)
    # are not injected — a gateway that is not MiniMax rejects them as
    # InvalidParameter, and MiniMax still answers without them.
    chat_openai = reasoning_preserving_chat_openai()
    return chat_openai(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout=120.0,
        max_retries=0,
    )


def _fence_checkpoint_saver(
    saver: Any,
    *,
    url: str,
    env: Mapping[str, str] | None,
) -> RuntimeCoordinator:
    """Attach Runtime ownership to a real production saver.

    Called only when the saver is the installed ``PostgresSaver``. A test
    double has no connection or lock to fence, and pretending it did would
    hide a missing adapter instead of failing closed.

    The Runtime store is a separate short-transaction connection pool, so it
    is built from the operator URL rather than the saver's bounded session.
    An unusable store becomes the same typed boot stop as a broken
    checkpointer schema; the caller closes the acquisition stack.
    """
    from fin.host.runtime.fencing import fence_postgres_saver

    capacity = runtime_queue_capacity(env)
    lease = runtime_lease_seconds(env)
    try:
        store = PostgresRuntimeStore(url)
        store.setup()
    except RuntimeStoreError as exc:
        raise CheckpointSetupError(
            "could not initialize the durable Runtime store"
        ) from exc
    coordinator = RuntimeCoordinator(
        store, queue_capacity=capacity, lease_seconds=lease
    )
    fence_postgres_saver(saver, store)
    return coordinator


def _open_tool_outcome_ledger(url: str) -> PostgresToolOutcomeLedger:
    """The append-only tool outcome ledger beside the Runtime store.

    Same database and URL as the store: every fact is fenced against the
    very attempt rows the store owns. A broken setup is the same typed boot
    stop as a broken checkpointer schema.
    """
    try:
        ledger = PostgresToolOutcomeLedger(url)
        ledger.setup()
    except RuntimeStoreError as exc:
        raise CheckpointSetupError(
            "could not initialize the tool outcome ledger"
        ) from exc
    return ledger


# ----- the helper -----


def compile_for_runtime(
    *,
    checkpointer_url: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
    attachment_service: (
        AttachmentPreparationService
        | Callable[[], AttachmentPreparationService]
        | None
    ) = None,
    evidence: Any | None = None,
    run_call_supervisor: RunCallSupervisor | None = None,
) -> Any:
    """Build the durable v1 host graph and hand back only the graph.

    The graph-only shape of :func:`compile_runtime_handle`, kept for
    callers that own the process for its lifetime (the CLI). Parameters
    and raised errors are that function's contract, unchanged.

    Nothing here releases the checkpointer connection: the caller that
    has a shutdown should compile through
    :func:`compile_runtime_handle` and close its own handle instead.
    """
    return compile_runtime_handle(
        checkpointer_url=checkpointer_url,
        model_name=model_name,
        base_url=base_url,
        env=env,
        attachment_service=attachment_service,
        evidence=evidence,
        run_call_supervisor=run_call_supervisor,
    ).graph


def compile_runtime_handle(
    *,
    checkpointer_url: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
    attachment_service: (
        AttachmentPreparationService
        | Callable[[], AttachmentPreparationService]
        | None
    ) = None,
    evidence: Any | None = None,
    run_call_supervisor: RunCallSupervisor | None = None,
) -> RuntimeHandle:
    """Build the durable v1 host graph and return its closeable owner.

    Parameters
    ----------
    ----------
    checkpointer_url
        Postgres connection string. Defaults to
        ``os.environ["FIN_CHECKPOINT_URL"]``. Required: there is
        no fallback — a missing value raises
        :class:`MissingCheckpointURLError`.
    model_name
        Non-empty model id passed verbatim to Chat Completions. When
        omitted, ``FIN_MODEL`` must be configured in the environment.
    base_url
        OpenAI-compatible chat-completions endpoint (the
        ``openai`` SDK appends ``/chat/completions``). Defaults
        to ``os.environ.get("FIN_BASE_URL", DEFAULT_BASE_URL)``.
        The product default is official MiniMax CN
        ``https://api.minimaxi.com/v1``. A local 9router
        override is ``http://localhost:20128/v1``; WSL2 users
        targeting Windows-side 9router use the gateway IP.
    env
        Mapping to read env-style keys from. Defaults to
        ``os.environ``. Tests inject a ``monkeypatch.setenv``
        shim by relying on ``os.environ`` directly.
    attachment_service
        Optional attachment preparation service for the T2f durable
        graph gate: the HTTP composition root passes its service (or a
        zero-arg resolver for it, so a boot that dies BEFORE compiling
        never pays the construction) and the grounded route then gains
        the preparation interrupt. ``None`` (the default, every CLI /
        test caller today) compiles the graph without the gate. This
        factory never builds a service of its own — no boot here pays
        an attachment store or a database connection it did not ask
        for. From T3 the same object is ALSO threaded to ``compile_graph``
        as the ``retrieve`` tool's attachment search seam (it ships
        ``search()`` next to the preparation methods), so one injected
        service serves both the preparation gate and the thread-scoped read.
    evidence
        Optional
        :class:`~fin.host.evidence.evidence_session_registry.EvidenceSessionBinding`,
        which bundles the registry owning one persistent session per run
        identity with the resolver that selects that identity from a run's
        config (P2-I2). The two travel as ONE parameter because neither is
        usable alone: a registry with no resolver can never bind, and a
        resolver with no registry has nowhere to put the identity it resolved.
        ``None`` (the default) compiles the graph without the Evidence lane,
        which is the pre-P2-I2 envelope shape. When it IS wired, a run whose
        resolver selects nothing is REFUSED with a closed §6 code rather than
        answered under the shared adapter.
    run_call_supervisor
        Optional :class:`~fin.host.run_call_supervisor.RunCallSupervisor`
        this runtime's model calls run under, so a stop or a client
        disconnect cancels an in-flight native async call instead of
        waiting it out. The caller owns its lifetime: this factory
        threads it into the compiled graph (which keys its idempotence
        cache on that object) and never closes it, because a second
        compile against the same supervisor is a cache hit rather than
        a new acquisition. ``None`` — every CLI and test caller — keeps
        the plain synchronous ``runnable.stream`` path.

    Returns
    -------
    RuntimeHandle
        The compiled host graph (T6's ``_LockedGraph`` proxy), ready
        for sync ``invoke``, plus the cleanup that releases the
        Postgres connection the compile opened. The caller owns that
        handle: ``handle.close()`` is idempotent and is the only way
        the connection goes away.

    Raises
    ------
    MissingCheckpointURLError
        When neither ``checkpointer_url`` nor ``FIN_CHECKPOINT_URL``
        is supplied.
    CheckpointSetupError
        When ``PostgresSaver.from_conn_string`` enter or
        ``PostgresSaver.setup()`` raises a
        ``psycopg.OperationalError`` (covers connection refused,
        pool timeout, server down, etc.).
    """
    # Step 1: resolve the checkpointer URL. Missing → typed
    # error. We resolve BEFORE any model work so a missing URL
    # never wastes an LLM round-trip.
    if checkpointer_url is None:
        url = _read_env(env, "FIN_CHECKPOINT_URL")
        if url is None:
            raise MissingCheckpointURLError()
    else:
        url = checkpointer_url

    # Step 2: resolve the configured model spec + base_url via the shared
    # factory. The endpoint remains a deployment knob. Resolve BEFORE the saver so a bad model
    # name fails fast without touching Postgres.
    model = _build_default_model(
        model_name=model_name, base_url=base_url, env=env
    )

    # Step 3: lazy import the PostgresSaver. Module import stays
    # cheap when the heavy runtime dep is absent; tests inject
    # fakes into ``sys.modules``.
    from langgraph.checkpoint.postgres import PostgresSaver

    # Step 4: open the Postgres connection via the context
    # manager and run ``setup()`` once (idempotent — creates
    # the ``checkpoint_migrations`` schema if absent). The CM
    # closes the connection on exit; we keep the stack alive in
    # a module-level registry so the compiled graph can use it
    # for every checkpoint write.
    #
    # The acquisition bound rides in the URL; the statement/lock bounds
    # are SET on the session AFTER ``setup()`` so they cover every
    # run-time checkpoint statement but never its CREATE INDEX
    # CONCURRENTLY migrations — a timeout there leaves an INVALID index.
    stack = ExitStack()
    try:
        saver = stack.enter_context(
            PostgresSaver.from_conn_string(bounded_checkpoint_conninfo(url))
        )
    except psycopg.OperationalError as exc:
        stack.close()
        raise CheckpointSetupError(
            "could not open the Postgres checkpointer connection"
        ) from exc
    except BaseException:
        stack.close()
        raise

    runtime: RuntimeCoordinator | None = None
    tool_ledger: PostgresToolOutcomeLedger | None = None
    try:
        saver.setup()
        bind_checkpoint_session_bounds(saver, url)
        # P1 T3: a real saver gains the generation-aware fencing adapter and
        # the coordinator the compiled graph exposes. A test double keeps the
        # pre-P1 shape with ``runtime=None`` — it cannot offer atomic fencing,
        # so this is a test seam, never a production fallback.
        if _is_installed_postgres_saver(saver):
            runtime = _fence_checkpoint_saver(saver, url=url, env=env)
            # P3 T4: the tool outcome ledger lives in the same database and
            # is fenced by the same worker attempts, so a real boot records
            # its tool facts and a fake saver records none.
            tool_ledger = _open_tool_outcome_ledger(url)
        run_call_context_registry = RunCallContextRegistry()
        _bind_checkpoint_run_cancellation(saver, run_call_context_registry)
    except psycopg.OperationalError as exc:
        stack.close()
        raise CheckpointSetupError(
            "could not initialize the Postgres checkpointer schema"
        ) from exc
    except psycopg.ProgrammingError as exc:
        stack.close()
        raise CheckpointSetupError(
            "could not apply the checkpointer session bounds"
        ) from exc
    except BaseException:
        stack.close()
        raise

    # Step 5: hand the saver + model to T6's factory. The
    # factory is LRU-cached on ``(id(checkpointer), id(model),
    # step_cap, recursion_limit)`` so two calls with the same
    # ``PostgresSaver`` instance return the SAME proxy object.
    # That identity is what T9 will assert between the CLI
    # helper's graph and the HTTP handler's graph.
    # Soft-Track gray zone: the Jev Choice client is built here; a missing
    # FIN_JEV_API_KEY yields ``None`` and each gray zone then fail-closes
    # to its own residual (the track ``clarify``, or the 检索档 cascade's
    # recorded default — the 档 lane no longer opens a choice window of its
    # own, ADR 0104). One client serves both judges, so no second model
    # seam is threaded.
    # T2f: a callable ``attachment_service`` is the composition root's
    # resolver — invoked only now that the compile is actually reached,
    # so a boot that dies above (missing URL, refused setup) never
    # constructed the attachment store either.
    try:
        resolved_attachment_service = (
            attachment_service()
            if callable(attachment_service)
            else attachment_service
        )
    except BaseException:
        stack.close()
        raise
    try:
        compiled = compile_graph(
            checkpointer=saver,
            model=model,
            jev_client=build_jev_client(environ=env),
            retrieve_adapter=_resolve_retrieve_adapter(env),
            # search-mode host T2 / ADR 0062: the durable PRODUCTION boot
            # ships the 检索档 choice-window HITL lane (a window pauses on
            # ``interrupt``; the workspace resumes with the operator pick).
            # The cascade itself no longer opens one for a routing residual
            # (ADR 0104) — the lane still carries the Fast 抽图 propose
            # window and a window an earlier build left pending. The
            # checkpointer above is what makes the pause durable. The
            # dev / scripted-e2e / eval lanes keep the factory default OFF
            # so their pre-T2 flows are untouched.
            enable_choice_window=True,
            # T2f durable graph gate: the HTTP composition root injects the
            # process's attachment service so the grounded route gains the
            # preparation gate; ``None`` (every direct caller today) keeps
            # the gate out of the graph entirely.
            # ROOT CAUSE LIVES IN graph.py, not here: ``_PreparationOutcomeView``
            # / ``_ResolvedAttachmentView`` declare MUTABLE members (``state:
            # str``), so the checker demands invariant writable attributes and a
            # frozen dataclass's read-only fields can never satisfy them — even
            # though the gate only READS these fields. The durable fix is making
            # those view members read-only (@property) in graph.py; until then
            # this one call site carries the documented, rule-specific exception.
            attachment_service=resolved_attachment_service,  # pyright: ignore[reportArgumentType]
            # T3 (thread-attachment-source-read): the same service object is
            # the retrieve tool's attachment search seam — it ships
            # ``search()`` alongside the preparation methods, so the
            # composition root threads ONE object into both graph seams
            # rather than building a second capability. ``None`` (every
            # caller today that does not inject a service) keeps the
            # attachment branch out of the ``retrieve`` envelope entirely.
            attachment_retriever=resolved_attachment_service,
            # P2-I2: the identity-bound Evidence session is INJECTED, never built
            # here. The host holds no trusted source for the authority facts a
            # binding needs (owner scope, reader, authorization revision) — the only
            # scope it can see is the untrusted ``structured_result`` sibling, and
            # identity never comes off the message channel. Defaulting them here
            # would be the process-level fallback and default scope ADR B1 forbids,
            # so an unwired boot keeps the Evidence lane out of the retrieve path
            # rather than answering under an invented identity.
            #
            # The registry's own teardown needs nothing from this root: it
            # registers an ``atexit`` drain when constructed, so a child it
            # launched is closed at process exit without a caller remembering to
            # call ``close_all``.
            evidence=evidence,
            # Injected, never built: the supervisor owns an event loop
            # whose lifetime is the APPLICATION's, not this compile's.
            run_call_supervisor=run_call_supervisor,
            run_call_context_registry=run_call_context_registry,
            # P1 T3: ``None`` for the fake/test path; a real boot exposes the
            # durable admission coordinator on the compiled proxy.
            runtime=runtime,
            # P3 T4: ``None`` for the fake/test path; a real boot records
            # every tool step's canonical facts in the ledger.
            tool_ledger=tool_ledger,
        )
    except BaseException:
        stack.close()
        raise

    # Step 6: hand the acquired stack to the handle that owns it. The
    # compile is only complete once the caller can release what it
    # opened, so the stack travels with the graph rather than with this
    # module.
    return RuntimeHandle(compiled, release=stack.close)


# ----- InMemorySaver helper (tests / eval only) -----


def compile_for_runtime_dev(
    *,
    model_name: str | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Build a host graph with ``InMemorySaver`` for tests and eval.

    Product ``fin-serve`` / ``compile_for_http`` must not call this
    when ``FIN_CHECKPOINT_URL`` is unset (ADR 0015). Explicit
    ``FIN_SCRIPTED_MODEL`` e2e is a separate opt-in. Differs from
    :func:`compile_for_runtime` in two ways:

    * **No Postgres.** ``InMemorySaver`` keeps state in process
      memory. Threads and runs are lost on restart.
    * **Same explicit model configuration** as the prod path
      (``_build_default_model``).

    Retrieve is NOT downgraded here: the checkpointer decides where
    threads persist, not where evidence comes from.

    The contract on :func:`compile_for_runtime` — raise
    ``MissingCheckpointURLError`` when no URL — is INTACT.

    The model side is **NOT** auto-faked here. A configured
    ``FIN_MODEL`` (or explicit ``model_name``) is required. If
    ``FIN_BASE_URL`` is unset, ``DEFAULT_BASE_URL`` is used; the
    model call will surface a real auth/network error if the key or
    network is missing — better a loud failure than a hidden scripted
    substitute masquerading as a real conversation.
    """
    import logging
    import warnings

    message = (
        "FIN_CHECKPOINT_URL is not set; using InMemorySaver. Threads and "
        "runs will NOT persist across restarts. Set FIN_CHECKPOINT_URL "
        "to enable durable Postgres-backed state."
    )
    # Two channels: ``warnings.warn`` so callers of this helper see
    # it, and ``logging.warning`` so a log capture can assert it.
    # Tests suppress both.
    warnings.warn(message, stacklevel=2)
    logging.getLogger("fin.host.compile").warning(message)

    from langgraph.checkpoint.memory import InMemorySaver

    model = _build_default_model(
        model_name=model_name, base_url=base_url, env=env
    )
    saver = InMemorySaver()
    # Soft-Track gray zone: mirror the durable path — the dev compile
    # exercises the same Jev tier (or the same key-less fail-closed
    # residual).
    return compile_graph(
        checkpointer=saver,
        model=model,
        jev_client=build_jev_client(environ=env),
        retrieve_adapter=_resolve_retrieve_adapter(env),
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MCP_COMMAND",
    "DEFAULT_RETRIEVE_ADAPTER",
    "EXPECTED_CHECKPOINT_URL",
    "CheckpointSetupError",
    "FIN_API_KEY_ENV",
    "HostCompileError",
    "MissingCheckpointURLError",
    "MissingModelError",
    "RuntimeConfigError",
    "compile_for_runtime",
    "compile_for_runtime_dev",
    "compile_runtime_handle",
    "runtime_lease_seconds",
    "runtime_queue_capacity",
]
