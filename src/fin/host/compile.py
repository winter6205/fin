"""The durable v1 compile (T8, #17).

The CLI (``python -m fin``) and the HTTP ask route (T9) both
call :func:`compile_for_runtime` to build the host graph for
production. Tests do NOT use this module — T6's ``compile_graph``
takes the ``checkpointer`` and ``model`` directly, and tests pass
``InMemorySaver()`` plus ``ScriptedChatModel``. This separation
keeps the test surface hermetic (no live Postgres, no live LLM)
and the production surface explicit (ADR 0015 + ADR 0013).

Why a helper at all
-------------------
ADR 0015 fixes ``PostgresSaver`` + ``FIN_CHECKPOINT_URL`` for
the durable v1 path. ADR 0013 fixes the runtime model selection
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
write, so we hold an ``ExitStack`` in a module-level registry
(``_LIVE_RUNTIME_STACKS``). Each call to ``compile_for_runtime``
appends one stack; the list grows linearly with the number of
processes' worth of compiled graphs, which is one per v1 CLI /
HTTP process. Tests append fakes, so no real resource is held.

Lazy imports
------------
``ChatOpenAI`` and ``PostgresSaver`` are imported inside the
function — not at module top — so the module is importable even
when those heavy runtime deps are missing or have been replaced
in ``sys.modules`` by a test fixture. Production boot pulls the
real symbols; tests inject fakes.

URL contract (OpenAI SDK)
--------------------------
``ChatOpenAI`` is built on the official ``openai`` SDK; the
SDK appends ``/chat/completions`` to whatever ``base_url`` you
give it. So ``FIN_BASE_URL`` SHOULD include ``/v1`` — passing
``http://localhost:20128`` (without ``/v1``) would result in
requests to ``http://localhost:20128/chat/completions`` (404 at
the proxy). The default below is ``http://localhost:20128/v1``;
the proxy receives ``POST /v1/chat/completions`` as expected.

The 9router proxy also accepts the Anthropic Messages API at
``/v1/messages`` (ADR 0013 path 2). Operators who prefer that
path can swap to ``ChatAnthropic`` and override ``FIN_BASE_URL``
to the bare host — but the v1 default is OpenAI Chat Completions.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Mapping

import psycopg

# T6's graph factory. Eagerly imported so tests can monkeypatch
# it via ``monkeypatch.setattr("fin.host.compile.compile_graph",
# ...)``. Lazy imports below cover the runtime deps.
from fin.host.graph import compile_graph


# ----- public constants -----


# ADR 0013 default for ``FIN_MODEL``. The local proxy at
# ``DEFAULT_BASE_URL`` exposes a model whose id uses a slash
# separator (``provider/model``); ``ChatOpenAI`` passes it
# verbatim to the proxy.
DEFAULT_MODEL = "fin/MiniMax-M3"

# OpenAI Chat-Completions proxy URL (the official ``openai``
# SDK appends ``/chat/completions`` to ``base_url``). The
# default INCLUDES ``/v1`` — with this value the SDK will hit
# ``http://localhost:20128/v1/chat/completions`` (verified
# empirically). Operators running against OpenAI's hosted API
# override via ``FIN_BASE_URL=https://api.openai.com/v1``.
DEFAULT_BASE_URL = "http://localhost:20128/v1"

# The exact ``FIN_CHECKPOINT_URL`` example shipped in
# ``.env.example`` and asserted by the URL-match acceptance gate
# (spec success-criteria item 7).
EXPECTED_CHECKPOINT_URL = (
    "postgresql://fin:fin@localhost:5432/fin?sslmode=disable"
)


# ----- typed errors -----


class HostCompileError(Exception):
    """Base class for typed errors raised by this module."""


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


# ----- process-lifetime registry -----


# Each successful ``compile_for_runtime`` appends one ``ExitStack``
# so the Postgres connection stays open past the ``with`` block.
# v1 runs one compile per process, so this list stays short.
_LIVE_RUNTIME_STACKS: list[ExitStack] = []


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


# ----- the helper -----


def compile_for_runtime(
    *,
    checkpointer_url: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Build the durable v1 host graph for CLI / HTTP boot.

    Parameters
    ----------
    checkpointer_url
        Postgres connection string. Defaults to
        ``os.environ["FIN_CHECKPOINT_URL"]``. Required: there is
        no fallback — a missing value raises
        :class:`MissingCheckpointURLError`.
    model_name
        Model id passed verbatim to the proxy (e.g.
        ``"fin/MiniMax-M3"``). Defaults to
        ``os.environ.get("FIN_MODEL", DEFAULT_MODEL)``.
    base_url
        OpenAI-compatible chat-completions endpoint (the
        ``openai`` SDK appends ``/chat/completions``). Defaults
        to ``os.environ.get("FIN_BASE_URL", DEFAULT_BASE_URL)``.
        The local v1 dev proxy listens on
        ``http://localhost:20128/v1`` and receives
        ``POST /v1/chat/completions``; WSL2 users override with
        the Windows-host gateway IP, e.g.
        ``http://172.31.128.1:20128/v1``.
    env
        Mapping to read env-style keys from. Defaults to
        ``os.environ``. Tests inject a ``monkeypatch.setenv``
        shim by relying on ``os.environ`` directly.

    Returns
    -------
    Any
        The compiled host graph (T6's ``_LockedGraph`` proxy),
        ready for sync ``invoke``. The underlying Postgres
        connection is held in a process-lifetime ``ExitStack``
        so it survives past this function's return; the caller
        does NOT need to enter another ``with`` block.

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

    # Step 2: resolve the model spec + base_url. Both are
    # optional with sensible defaults; ADR 0013 fixes the
    # model, the local-proxy URL is a deployment knob. We
    # resolve BEFORE the saver so a bad model name fails fast
    # without touching Postgres.
    if model_name is None:
        model_name = _read_env(env, "FIN_MODEL") or DEFAULT_MODEL
    if base_url is None:
        base_url = _read_env(env, "FIN_BASE_URL") or DEFAULT_BASE_URL

    # Step 3: lazy import the runtime deps. Module import stays
    # cheap when the heavy packages are absent; tests inject
    # fakes into ``sys.modules``.
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.postgres import PostgresSaver

    # ``ANTHROPIC_API_KEY`` is the user's chosen credential
    # name in their shell; ``ChatOpenAI`` forwards it as the
    # ``Authorization: Bearer <key>`` header. The local 9router
    # proxy passes the header through; we never log the value.
    api_key = _read_env(env, "ANTHROPIC_API_KEY") or "sk-no-key-required"

    model = ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
    )

    # Step 4: open the Postgres connection via the context
    # manager and run ``setup()`` once (idempotent — creates
    # the ``checkpoint_migrations`` schema if absent). The CM
    # closes the connection on exit; we keep the stack alive in
    # a module-level registry so the compiled graph can use it
    # for every checkpoint write.
    stack = ExitStack()
    try:
        saver = stack.enter_context(PostgresSaver.from_conn_string(url))
    except psycopg.OperationalError as exc:
        stack.close()
        raise CheckpointSetupError(
            "could not open the Postgres checkpointer connection"
        ) from exc

    try:
        saver.setup()
    except psycopg.OperationalError as exc:
        stack.close()
        raise CheckpointSetupError(
            "could not initialize the Postgres checkpointer schema"
        ) from exc

    # Step 5: hand the saver + model to T6's factory. The
    # factory is LRU-cached on ``(id(checkpointer), id(model),
    # step_cap, recursion_limit)`` so two calls with the same
    # ``PostgresSaver`` instance return the SAME proxy object.
    # That identity is what T9 will assert between the CLI
    # helper's graph and the HTTP handler's graph.
    compiled = compile_graph(checkpointer=saver, model=model)

    # Step 6: keep the stack alive for process lifetime. We do
    # this AFTER a successful ``compile_graph`` so a factory
    # failure does not leave a dangling connection.
    _LIVE_RUNTIME_STACKS.append(stack)
    return compiled


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "EXPECTED_CHECKPOINT_URL",
    "CheckpointSetupError",
    "HostCompileError",
    "MissingCheckpointURLError",
    "compile_for_runtime",
]