"""A reusable MCP-over-stdio session for opt-in live integration.

The ordinary adapters keep their simple one-call subprocess behavior.  A
caller that needs several tools to share one in-memory iknow process can
inject this session into the push, retrieve, and invalidation adapters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
INITIALIZE_REQUEST_ID = 1
TOOL_REQUEST_ID = 2

# The default per-call budget for an MCP exchange, overridable per operator
# with ``FIN_MCP_TIMEOUT_SECONDS``.
#
# WHY it lives here and is this size: every MCP tool that does real work
# before it answers — ``push`` (embeds every chunk, writes the rows) and
# ``submit_index_job`` (hands the graph re-index to a worker) — was built
# with a 5s ceiling. Measured against the live server, one push took ~11s
# end to end (0.8s handshake + embed + storage write), so 5s could not
# distinguish "slow" from "impossible" and reported every one of them as a
# transport timeout. A budget below the real cost of the work makes the
# typed failure a lie. This module is the shared transport, so it is where
# the one authority for the number belongs; the ports that need a different
# value still take it as a constructor argument.
DEFAULT_MCP_TIMEOUT_SECONDS: float = 60.0


def default_mcp_timeout_seconds() -> float:
    """Resolve ``FIN_MCP_TIMEOUT_SECONDS``, falling back to the default.

    An unparseable or non-positive override degrades to the default rather
    than raising: a typo in an operator setting must not take the ingest
    door down at import time. A budget that is merely too small still
    surfaces as the typed ``*ServiceUnavailableError`` the callers already
    handle, which is the honest failure for that case.
    """
    raw = os.environ.get("FIN_MCP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MCP_TIMEOUT_SECONDS
    try:
        value = float(raw)
        # `nan <= 0` is False and `inf > 0`, so a bare positivity test lets
        # both through: NaN then reaches `asyncio.wait_for(timeout=nan)`
        # and inf silently disables the ceiling this budget exists to
        # provide. Both are unusable values wearing a valid float's
        # clothes, so they are rejected here rather than downstream.
        usable = math.isfinite(value) and value > 0
    except ValueError:
        # Bind `value` even on the reject path: `usable` is the only
        # condition guarding the `return value` below, so leaving it
        # unbound here makes the narrowing unsound (pyright reports it
        # possibly-unbound) even though the guard is in fact total.
        value = DEFAULT_MCP_TIMEOUT_SECONDS
        usable = False
    if not usable:
        logger.warning(
            "FIN_MCP_TIMEOUT_SECONDS=%s is not a positive finite number; "
            "using %.0fs",
            raw,
            DEFAULT_MCP_TIMEOUT_SECONDS,
        )
        return DEFAULT_MCP_TIMEOUT_SECONDS
    return value


_PROCESS_EOF = object()


def _remaining(deadline: float) -> float:
    """The seconds left before one monotonic deadline, floored at 0."""
    return max(0.0, deadline - time.monotonic())


#: How long a blocked wait sleeps before it looks at the deadline and the
#: cancellation flag again. Short enough that a cancelled call is observed
#: promptly, long enough that a waiting caller does not spin.
_POLL_SECONDS = 0.05


#: Variables an allowlisted child still inherits from its parent.
#:
#: This list is the whole of what survives when a caller passes ``env=``. It
#: holds process start-up necessities and nothing an authority could travel in:
#: a path, a home for runtime caches, a locale and a temporary directory. Any
#: variable outside this list and outside the caller's mapping stops being
#: inherited, which is the property the private Evidence route depends on — an
#: identity that the host did not inject cannot arrive by inheritance.
#:
#: ``IKNOW_MEMORY_HOME`` is a *where*, not a *who*, and that distinction is why
#: it belongs here (ADR-0100 §2). iknow reads it as the directory that directly
#: holds ``setting.json`` and the home ``.env.local``, so it is what names the
#: stack — the ports included — an isolated dev run talks to. A child that
#: inherited the host's selection reads the same stores the host reads, which
#: is what the private Evidence route needs to answer a read against the same
#: corpus ordinary retrieval sees. It carries no principal and no scope, so
#: adding it widens *where* the child looks and never *who* it may act as: the
#: eight ``IKNOW_EVIDENCE_*`` variables stay out of this list and out of
#: inheritance, so the invariant above is untouched.
INHERITED_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "IKNOW_MEMORY_HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
    "SYSTEMROOT",
    "COMSPEC",
)


#: The stages a retrieval budget can expire in (spec
#: qa-chain-corpus-retrieval §"Result, error, readiness, and degradation
#: wire": a ``cause=busy`` ``not_ready`` names exactly one of these).
#:
#: ``queue`` is the wait for the session's I/O lock, ``startup`` is the
#: subprocess launch and its handshake, and ``response`` is the wait for
#: the business response.
BUDGET_STAGES: tuple[str, ...] = ("queue", "startup", "response")


class MCPStdioTransportError(OSError):
    """The shared MCP process could not be reached or stopped answering."""


class MCPStdioCancelledError(MCPStdioTransportError):
    """An owning run cancelled its MCP exchange."""


class MCPStdioDeadlineExceededError(MCPStdioTransportError):
    """One call's absolute deadline elapsed; ``stage`` names where.

    The deadline is ONE monotonic budget for the whole call, spent
    across the lock wait (``queue``), the subprocess startup/handshake
    (``startup``), and the response wait (``response``). A WAITER that
    expires on the lock raises this with ``stage="queue"`` and leaves
    the session intact — its expiry says nothing about the lock owner's
    progress. An OWNER that expires raises with the stage it was in and
    quarantines the session (terminal ``_failed`` + forced stop), so a
    late response can never satisfy a later request.

    This is the ONE typed error for an exhausted budget: the run-control
    deadline and the per-call budget are the same mechanism, so they
    cannot report the same condition as two unrelated errors.
    """

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = self.validate_stage(stage)

    @staticmethod
    def validate_stage(stage: str) -> str:
        """The ONE validation of a budget stage.

        The stage vocabulary is a wire contract, so the rule lives with
        the error that carries it and every other budget error delegates
        here instead of repeating the check.
        """
        if stage not in BUDGET_STAGES:
            raise ValueError(
                f"budget stage must be one of {BUDGET_STAGES}, got {stage!r}"
            )
        return stage


class MCPStdioProtocolError(RuntimeError):
    """The shared MCP process answered with an off-contract frame."""


# ----- shared wire primitives for the one-shot stdio adapters -----
#
# ``mcp_push_port`` and ``mcp_index_job_port`` drive a spawn-per-call
# JSON-RPC exchange; the handshake frames, the request-id constants, the
# response-line parse and the initialize validation are the wire
# contract itself and live exactly once, here. The adapter keeps its own
# exception type by translating ``MCPStdioProtocolError`` at the seam.


def build_initialize_frame(client_name: str) -> dict[str, Any]:
    """The ``initialize`` request frame; ``client_name`` is the only
    per-adapter difference on the wire."""
    return {
        "jsonrpc": "2.0",
        "id": INITIALIZE_REQUEST_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        },
    }


def build_initialized_notification() -> dict[str, Any]:
    """The ``notifications/initialized`` frame (no ``id``, no reply)."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def build_tools_call_frame(
    name: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``tools/call`` request frame for one tool invocation."""
    return {
        "jsonrpc": "2.0",
        "id": TOOL_REQUEST_ID,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


def build_handshake_wire(
    payload: dict[str, Any], *, client_name: str
) -> bytes:
    """The ordered initialize / initialized / tools-call frames as
    newline-delimited JSON, ready for the child's stdin."""
    return (
        "\n".join(
            json.dumps(message)
            for message in (
                build_initialize_frame(client_name),
                build_initialized_notification(),
                payload,
            )
        )
        + "\n"
    ).encode("utf-8")


def parse_jsonrpc_response_lines(stdout: bytes) -> list[Any]:
    """Parse the non-empty JSON-RPC response lines of an MCP stdout."""
    responses: list[Any] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            responses.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MCPStdioProtocolError(
                f"malformed JSON-RPC response: {exc}"
            ) from exc
    return responses


def validate_initialize_response(response: Any) -> None:
    """Validate an initialize response before accepting tool output."""
    if (
        not isinstance(response, dict)
        or response.get("id") != INITIALIZE_REQUEST_ID
    ):
        raise MCPStdioProtocolError(
            f"invalid MCP initialize response: {response!r}"
        )
    if "error" in response:
        raise MCPStdioProtocolError(
            f"MCP initialize returned JSON-RPC error: "
            f"{response['error']!r}"
        )
    if not isinstance(response.get("result"), dict):
        raise MCPStdioProtocolError(
            "MCP initialize response has no result object"
        )


# ----- the one-shot spawn-per-call exchange -----
#
# ``mcp_adapter``, ``mcp_push_port`` and ``mcp_invalidation_port`` each
# open a fresh child per call, write the whole handshake, and read
# exactly the frames the protocol owes. That transport lives here so
# there is one implementation of it rather than three.

#: Largest single response frame accepted off a one-shot child's stdout.
#:
#: ``StreamReader.readline`` is bounded by the stream ``limit`` and raises
#: ``LimitOverrunError`` past it, so the bound has to be set at launch
#: rather than discovered when a large tool result arrives.
ONESHOT_FRAME_LIMIT_BYTES: int = 8 * 1024 * 1024

#: Seconds allowed for the child to honour ``terminate`` before it is
#: killed. Outside the caller's budget on purpose: cleanup must not be
#: cut short by the timeout that triggered it.
_ONESHOT_STOP_SECONDS = 2.0

#: How much of the child's stderr is kept for a failure message.
_ONESHOT_STDERR_TAIL_BYTES = 4096


async def launch_oneshot_mcp_process(
    command: Sequence[str],
) -> "asyncio.subprocess.Process":
    """Launch a spawn-per-call MCP child with the shared stdio wiring."""

    try:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=ONESHOT_FRAME_LIMIT_BYTES,
        )
    except (OSError, FileNotFoundError) as exc:
        raise OSError(f"failed to launch MCP command: {exc}") from exc


async def _drain_stderr(stream: asyncio.StreamReader | None) -> bytes:
    """Read the child's stderr to EOF, keeping only a tail.

    Draining has to happen concurrently with the response reads: a
    server that logs more than a pipe buffer would otherwise block on
    its own stderr write and never answer.
    """

    if stream is None:
        return b""
    try:
        data = await stream.read()
    except (asyncio.LimitOverrunError, ValueError, OSError):
        return b""
    if len(data) > _ONESHOT_STDERR_TAIL_BYTES:
        return data[-_ONESHOT_STDERR_TAIL_BYTES:]
    return data


async def _stop_oneshot_process(proc: "asyncio.subprocess.Process") -> None:
    """Terminate the child and reap it, killing it if it ignores that."""

    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=_ONESHOT_STOP_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError):
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(
                asyncio.TimeoutError, TimeoutError, OSError
            ):
                await asyncio.wait_for(proc.wait(), timeout=_ONESHOT_STOP_SECONDS)
    else:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), timeout=_ONESHOT_STOP_SECONDS)


async def _read_oneshot_frames(
    proc: "asyncio.subprocess.Process",
    expected_frames: int,
) -> bytes:
    """Read exactly ``expected_frames`` newline-delimited frames.

    Reading a fixed frame count rather than to EOF is the whole point:
    a conforming MCP stdio server answers and then keeps serving, so
    waiting for the stream to close would wait for a child that never
    intends to close. EOF before the last owed frame means the child
    died mid-call, which is a transport failure.
    """

    stdout = proc.stdout
    if stdout is None:
        raise MCPStdioTransportError("MCP process stdout is unavailable")
    chunks: list[bytes] = []
    for _ in range(expected_frames):
        try:
            line = await stdout.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            # A transport-level ceiling, so it is reported as one: the
            # callers contract on ``OSError`` for "no trustworthy
            # answer", and a protocol error here would escape them.
            raise MCPStdioTransportError(
                f"MCP response frame exceeds the "
                f"{ONESHOT_FRAME_LIMIT_BYTES}-byte frame limit: {exc}"
            ) from exc
        if not line:
            raise MCPStdioTransportError(
                f"MCP server closed stdout after {len(chunks)} of "
                f"{expected_frames} expected response frames"
            )
        chunks.append(line)
    return b"".join(chunks)


async def exchange_oneshot_mcp(
    proc: "asyncio.subprocess.Process",
    wire_messages: bytes,
    *,
    timeout: float,
    expected_frames: int = 2,
) -> bytes:
    """Run one complete handshake + ``tools/call`` and return the frames.

    ``wire_messages`` carries the ordered ``initialize`` /
    ``notifications/initialized`` / ``tools/call`` frames. Because the
    notification draws no reply, ``expected_frames`` response lines are
    owed: the initialize response first, then the ``tools/call``
    response.

    The returned bytes are exactly those frames, so the caller's
    existing response-line parse and handshake validation apply
    unchanged.

    A child that is still running when the last owed frame has been read
    is the NORMAL case and is not a failure — the exchange stops it
    afterwards. A child that already exited non-zero before its frames
    were complete is a real failure and raises ``OSError``.

    The child is stopped and reaped on every path: success, timeout,
    and transport error alike.
    """

    drain = asyncio.ensure_future(_drain_stderr(proc.stderr))
    try:
        try:
            frames = await asyncio.wait_for(
                _run_oneshot_exchange(proc, wire_messages, expected_frames),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise
        except MCPStdioTransportError as exc:
            # The exchange itself failed. Reap the child, then report
            # the exit it died with when there is a non-zero one —
            # that is the diagnostic that names a crash. A zero exit
            # (clean EOF short of the owed frames) and an oversized
            # frame carry their own message already.
            await _stop_oneshot_process(proc)
            if proc.returncode is not None and proc.returncode != 0:
                stderr = await _await_drain(drain)
                raise OSError(
                    f"MCP server exited {proc.returncode}: "
                    f"{stderr.decode(errors='replace')!r}"
                ) from exc
            raise
        # Read the exit state BEFORE stopping the child: a child that
        # is still running has answered correctly and is waiting for
        # the next request, which is the normal long-lived-server case
        # and not a failure. Only a genuine non-zero exit is one.
        if proc.returncode is not None and proc.returncode != 0:
            stderr = await _await_drain(drain)
            raise OSError(
                f"MCP server exited {proc.returncode}: "
                f"{stderr.decode(errors='replace')!r}"
            )
        return frames
    finally:
        drain.cancel()
        await _stop_oneshot_process(proc)


async def _await_drain(drain: "asyncio.Future[bytes]") -> bytes:
    """Collect the stderr drain, bounded — a child may not have closed it."""

    with contextlib.suppress(
        asyncio.TimeoutError, TimeoutError, OSError, asyncio.CancelledError
    ):
        return await asyncio.wait_for(
            asyncio.shield(drain), timeout=_ONESHOT_STOP_SECONDS
        )
    return b""


async def _run_oneshot_exchange(
    proc: "asyncio.subprocess.Process",
    wire_messages: bytes,
    expected_frames: int,
) -> bytes:
    """Write the wire and read the owed frames, with stdin then closed."""

    stdin = proc.stdin
    if stdin is None:
        raise MCPStdioTransportError("MCP process stdin is unavailable")
    try:
        # A write that fails means the child died before reading its
        # request. It is suppressed deliberately: the read below is the
        # honest report, because it either finds the frames or names
        # the exit that cut the child off.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            stdin.write(wire_messages)
            await stdin.drain()
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            stdin.close()
    return await _read_oneshot_frames(proc, expected_frames)


# ----- the synchronous one-shot exchange -----
#
# ``mcp_index_job_port`` is a sync port: its ``submit_index_job`` /
# ``poll_index_job`` are called from the sync workspace surface, so it
# cannot reuse the ``asyncio`` helpers above without bridging every
# call through a loop. The transport is the same contract, so it is
# written once here rather than a fourth time in the port.


def _stop_sync_oneshot_process(
    proc: "subprocess.Popen[bytes]",
) -> bytes:
    """Terminate and reap the child, returning its captured stderr."""

    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_ONESHOT_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(
                OSError, subprocess.TimeoutExpired
            ):
                proc.wait(timeout=_ONESHOT_STOP_SECONDS)
    try:
        stderr = proc.stderr.read() if proc.stderr is not None else b""
    except (OSError, ValueError):
        stderr = b""
    return stderr[-_ONESHOT_STDERR_TAIL_BYTES:]


def _write_sync_oneshot_request(
    process: "subprocess.Popen[bytes]", data: bytes
) -> None:
    """Write the wire and close stdin, so the child sees the request."""

    if process.stdin is None:
        raise MCPStdioTransportError("MCP process stdin is unavailable")
    try:
        # A failed write means the child died before reading; the read
        # that follows names that exit rather than hiding it.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            process.stdin.write(data)
            process.stdin.flush()
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            process.stdin.close()


def _read_sync_oneshot_exchange(
    process: "subprocess.Popen[bytes]",
    *,
    expected_frames: int,
) -> bytes:
    """Read exactly the owed frames off the blocking stdout pipe.

    Run on the worker thread so the caller can enforce its budget: the
    reads block on the pipe, which is exactly where a wedged server
    hangs. A child that closes stdout before owing every frame died
    mid-call, which is a transport failure.
    """

    stdout = process.stdout
    if stdout is None:
        raise MCPStdioTransportError("MCP process stdout is unavailable")
    frames: list[bytes] = []
    for _ in range(expected_frames):
        line = stdout.readline()
        if not line:
            raise MCPStdioTransportError(
                f"MCP server closed stdout after {len(frames)} of "
                f"{expected_frames} expected response frames"
            )
        frames.append(line)
    return b"".join(frames)


def exchange_sync_oneshot_mcp(
    command: Sequence[str],
    wire_messages: bytes,
    *,
    timeout: float,
    expected_frames: int = 2,
) -> bytes:
    """The synchronous sibling of :func:`exchange_oneshot_mcp`.

    Same contract, same owed-frame discipline: a conforming MCP stdio
    server answers and keeps serving, so the exchange reads the frames
    the protocol owes instead of waiting for the child to exit. The
    child is stopped and reaped on every path.

    ``timeout`` bounds the write-and-read phase on a monotonic clock.
    The pipe reads block, so they run on a worker thread while the
    caller waits with a deadline; a server that never answers raises
    ``subprocess.TimeoutExpired`` exactly as the port's previous
    ``communicate(timeout=...)`` did. ``stderr`` is drained on its own
    thread because a server that logs more than a pipe buffer would
    otherwise block on its own stderr write and never answer.
    """

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as exc:
        raise OSError(f"failed to launch MCP command: {exc}") from exc

    stderr_thread = threading.Thread(
        target=_drain_sync_stderr,
        args=(process.stderr,),
        name="fin-mcp-sync-stderr",
        daemon=True,
    )
    stderr_thread.start()

    result: dict[str, Any] = {}

    def _work() -> None:
        try:
            _write_sync_oneshot_request(process, wire_messages)
            result["frames"] = _read_sync_oneshot_exchange(
                process, expected_frames=expected_frames
            )
        except BaseException as exc:
            # Relayed to the caller thread below; the exception is never
            # swallowed, only moved across the thread boundary.
            result["error"] = exc

    worker = threading.Thread(
        target=_work,
        name="fin-mcp-sync-exchange",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        # The worker is stuck on the pipe. Tear the child down so those
        # reads unblock, then report the expiry the port contracts on.
        _stop_sync_oneshot_process(process)
        raise subprocess.TimeoutExpired(list(command), timeout)

    try:
        error = result.get("error")
        if isinstance(error, MCPStdioTransportError):
            if process.poll() is not None and process.returncode != 0:
                raise OSError(
                    f"MCP server exited {process.returncode}: "
                    f"{_stop_sync_oneshot_process(process).decode(errors='replace')!r}"
                ) from error
            raise error
        if error is not None:
            raise error
        # Read the exit BEFORE stopping the child: a child still running
        # has answered correctly and is waiting for more input, which
        # is the normal long-lived-server case, not a failure.
        if process.poll() is not None and process.returncode != 0:
            raise OSError(
                f"MCP server exited {process.returncode}: "
                f"{_stop_sync_oneshot_process(process).decode(errors='replace')!r}"
            )
        return result["frames"]
    finally:
        _stop_sync_oneshot_process(process)
        stderr_thread.join(timeout=_ONESHOT_STOP_SECONDS)


def _drain_sync_stderr(stream: Any) -> None:
    """Read a blocking stderr pipe to EOF so the child never blocks."""

    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        while stream.read(4096):
            continue


class MCPStdioSession:
    """Thread-safe, lazy, long-lived MCP JSON-RPC stdio session.

    The session performs the MCP initialize handshake once, then serializes
    tool calls on the connection.  A reader thread makes response timeouts
    possible without blocking the adapter's event loop.  The process is
    deliberately lazy so constructing an opt-in adapter never starts a
    service during the default test run.

    ``env`` decides what the child inherits.  ``None`` keeps the historical
    whole-parent inheritance, which is what the ordinary tool adapters need.
    Passing a mapping switches the child to a **closed allowlist**: a small
    fixed base set carried from the parent, plus exactly the variables named in
    the mapping, and nothing else.  That is the seam the private Evidence route
    uses, because whole-parent inheritance made every session in a host process
    share one identity, which made the authorized subset a process-wide
    constant.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("MCP session command must not be empty")
        if timeout_seconds is None:
            timeout_seconds = default_mcp_timeout_seconds()
        if timeout_seconds <= 0:
            raise ValueError(
                f"MCP session timeout must be positive, got {timeout_seconds}"
            )
        if env is not None:
            for name, value in env.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"MCP session env names must be non-empty strings, got "
                        f"{name!r}"
                    )
                if not isinstance(value, str):
                    raise ValueError(
                        f"MCP session env value for {name!r} must be a string, "
                        f"got {type(value).__name__}"
                    )
        self._command = tuple(str(part) for part in command)
        self._cwd = str(cwd) if cwd is not None else None
        self._timeout = float(timeout_seconds)
        self._explicit_env = dict(env) if env is not None else None
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[object] = queue.Queue()
        self._io_lock = threading.RLock()
        self._closed = False
        self._failed = False
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []

    @property
    def child_env(self) -> dict[str, str] | None:
        """The exact child environment, or ``None`` when inheriting.

        ``None`` is the whole-parent case.  A mapping is the closed allowlist,
        returned verbatim so a caller can assert what stopped being inherited
        without launching a process.
        """

        return dict(self._explicit_env) if self._explicit_env is not None else None

    @property
    def requests(self) -> tuple[dict[str, Any], ...]:
        """Requests sent on this session, for opt-in wire assertions."""

        with self._io_lock:
            return tuple(self._requests)

    @property
    def is_spent(self) -> bool:
        """Whether this session can no longer serve a call — reported, not reset.

        ``_closed`` and ``_failed`` are terminal BY DESIGN: a failed session is
        never revived in place, because the failure that set the flag is the
        evidence that this child (or this launch) is unusable.  A caller that
        OWNS the session lifecycle therefore needs to be able to ASK whether an
        entry it is holding is still alive, so it can drop it and launch a fresh
        child for the next call instead of answering every later call with the
        same dead transport.

        This property only reads the three signals that already decide the
        session's fate: explicitly closed, marked failed, or a child that has
        exited. If an active call owns the IO lock, the probe returns false
        without waiting; that owner will publish its terminal state when it
        unwinds, and a later probe can then replace the session.
        """

        if not self._io_lock.acquire(blocking=False):
            return False
        try:
            if self._closed or self._failed:
                return True
            process = self._process
            return process is not None and process.poll() is not None
        finally:
            self._io_lock.release()

    def call(
        self,
        payload: dict[str, Any],
        timeout_seconds: float | None = None,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Send one tools/call request and return its JSON-RPC response.

        ONE monotonic deadline bounds the whole call: the wait for the
        session lock, the subprocess startup/handshake, and the response
        read all consume the same remaining budget (spec
        qa-chain-corpus-retrieval §"Result, error, readiness, and
        degradation wire"). ``deadline`` is an absolute
        ``time.monotonic()`` timestamp the caller may impose; without
        one, the budget of ``timeout_seconds`` (or the session default)
        becomes that deadline HERE, before the lock is waited on.

        ``cancel_event`` is checked while queued and while waiting for a
        response; cancellation after the exchange starts spends this
        session and terminates its child.

        A waiter that expires on the lock raises
        :class:`MCPStdioDeadlineExceededError` with ``stage="queue"`` and
        does NOT terminate the lock owner; an owner that expires raises
        with the stage it was in and quarantines this session.
        """

        budget = self._timeout if timeout_seconds is None else float(timeout_seconds)
        if budget <= 0:
            raise MCPStdioTransportError(
                f"MCP session timeout must be positive, got {budget}"
            )
        self._validate_run_control(deadline, cancel_event)
        # ONE deadline for the whole call, computed BEFORE the lock wait:
        # the caller's own absolute deadline and the call's budget are
        # the same bound, and the earlier of the two is the one that
        # counts.
        call_deadline = time.monotonic() + budget
        if deadline is not None and deadline < call_deadline:
            call_deadline = deadline
        self._acquire_io_lock(call_deadline, cancel_event)
        try:
            self._check_run_control(call_deadline, cancel_event, "startup")
            self._ensure_started(call_deadline, cancel_event)
            self._check_quarantining_run_control(
                call_deadline, cancel_event, "response"
            )
            self._requests.append(payload)
            self._write(payload)
            response = self._read_response(
                call_deadline, cancel_event, stage="response"
            )
            if not isinstance(response, dict):
                raise MCPStdioProtocolError(
                    f"MCP tools/call response is "
                    f"{type(response).__name__}, expected an object"
                )
            if response.get("id") != payload.get("id"):
                raise MCPStdioProtocolError(
                    "MCP tools/call response id does not match request"
                )
            return response
        finally:
            self._io_lock.release()

    @staticmethod
    def _validate_run_control(
        deadline: float | None,
        cancel_event: threading.Event | None,
    ) -> None:
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise TypeError("MCP session deadline must be a monotonic timestamp")
            if not math.isfinite(deadline):
                raise ValueError("MCP session deadline must be finite")
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            raise TypeError("MCP session cancel_event must be a threading.Event")

    def _acquire_io_lock(
        self,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> None:
        """Take the I/O lock, or report the budget that ran out waiting.

        The wait is bounded by the call's ONE deadline, and a waiter that
        runs out of budget leaves the lock OWNER untouched: it raises
        with ``stage="queue"`` without quarantining the session, because
        its expiry says nothing about the owner's progress.

        The lock can be granted AFTER the budget ran out — the wait is
        the stage that spent it, so a late grant reports ``queue`` and
        hands the lock straight back instead of letting the call report
        the stage it never reached.
        """
        while True:
            self._check_run_control(deadline, cancel_event, "queue")
            if self._io_lock.acquire(timeout=_POLL_SECONDS):
                try:
                    self._check_run_control(deadline, cancel_event, "queue")
                except (MCPStdioCancelledError, MCPStdioDeadlineExceededError):
                    self._io_lock.release()
                    raise
                return

    @staticmethod
    def _check_run_control(
        deadline: float,
        cancel_event: threading.Event | None,
        stage: str,
    ) -> None:
        """The budget and cancellation check, and nothing else.

        It never quarantines the session: an expiry says only that this
        call ran out of budget, and the caller decides what that means
        for the session it is in.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise MCPStdioCancelledError("MCP session call was cancelled")
        if time.monotonic() >= deadline:
            raise MCPStdioDeadlineExceededError(
                "MCP session call deadline was exceeded", stage=stage
            )

    def _check_quarantining_run_control(
        self,
        deadline: float,
        cancel_event: threading.Event | None,
        stage: str,
    ) -> None:
        """The same check for a call that must spend the session on expiry.

        An expiry here spends the session: the child is stopped and the
        session is marked failed, so a late response can never satisfy a
        later request. The forced stop is cleanup and runs outside the
        caller's remaining budget — it cannot extend the user-facing
        deadline.
        """
        try:
            self._check_run_control(deadline, cancel_event, stage)
        except (MCPStdioCancelledError, MCPStdioDeadlineExceededError):
            self._failed = True
            self._stop_process(force=True)
            raise

    def close(self) -> None:
        """Terminate the child process, if it was started."""

        with self._io_lock:
            self._closed = True
            self._stop_process(force=False)

    def kill(self) -> None:
        """Kill the child process and make future calls fail by name."""

        with self._io_lock:
            self._closed = True
            self._failed = True
            self._stop_process(force=True)

    def __enter__(self) -> "MCPStdioSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _resolve_env(self) -> dict[str, str] | None:
        """Build the child environment for the allowlisted case.

        The base list is intentionally short and contains nothing an identity
        could be smuggled through: process start-up needs a path, a home for
        runtime caches, a locale and a temp directory.  Every other variable the
        child sees has to be named in the mapping the caller passed, so a host
        that forgets to inject an identity gets a child with **no** identity
        variable rather than one that inherited it from the parent.
        """

        explicit = self._explicit_env
        if explicit is None:
            return None
        env: dict[str, str] = {}
        for name in INHERITED_ENV_ALLOWLIST:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.update(explicit)
        return env

    def _ensure_started(
        self,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> None:
        if self._closed:
            raise MCPStdioTransportError("MCP session is closed")
        if self._failed:
            raise MCPStdioTransportError("MCP session has failed")
        if self._process is not None:
            if self._process.poll() is not None:
                self._failed = True
                raise MCPStdioTransportError(self._process_failure_message())
            self._check_run_control(deadline, cancel_event, "startup")
            return

        self._check_run_control(deadline, cancel_event, "startup")

        try:
            process = subprocess.Popen(
                self._command,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=self._resolve_env(),
            )
        except (OSError, ValueError) as exc:
            self._failed = True
            raise MCPStdioTransportError(
                f"failed to launch MCP session: {exc}"
            ) from exc
        self._process = process
        threading.Thread(
            target=self._read_stdout,
            name="fin-mcp-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            name="fin-mcp-stderr",
            daemon=True,
        ).start()

        initialize = build_initialize_frame("fin-live-session")
        initialized = build_initialized_notification()
        try:
            self._check_quarantining_run_control(deadline, cancel_event, "startup")
            self._write(initialize)
            self._check_quarantining_run_control(deadline, cancel_event, "startup")
            self._write(initialized)
            response = self._read_response(
                deadline, cancel_event, stage="startup"
            )
            validate_initialize_response(response)
        except (MCPStdioTransportError, MCPStdioProtocolError):
            self._failed = True
            self._stop_process(force=True)
            raise

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPStdioTransportError("MCP session stdin is unavailable")
        try:
            process.stdin.write(
                (json.dumps(message) + "\n").encode("utf-8")
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._failed = True
            raise MCPStdioTransportError(
                f"MCP session write failed: {exc}"
            ) from exc

    def _read_response(
        self,
        deadline: float,
        cancel_event: threading.Event | None,
        *,
        stage: str,
    ) -> Any:
        """Read one response frame within the call's ONE deadline.

        The wait is polled so a cancellation is observed while it blocks.
        A budget expiry here is the OWNER's expiry: the session is
        quarantined, so a late response can never satisfy another
        request, and the forced stop is cleanup that runs outside the
        caller's remaining budget.
        """
        while True:
            self._check_quarantining_run_control(deadline, cancel_event, stage)
            remaining = _remaining(deadline)
            if remaining <= 0:
                self._failed = True
                self._stop_process(force=True)
                raise MCPStdioDeadlineExceededError(
                    "MCP session call deadline was exceeded", stage=stage
                )
            if cancel_event is not None:
                remaining = min(remaining, _POLL_SECONDS)
            try:
                value = self._responses.get(timeout=remaining)
            except queue.Empty:
                continue
            self._check_quarantining_run_control(deadline, cancel_event, stage)
            break
        if value is _PROCESS_EOF:
            self._failed = True
            raise MCPStdioTransportError(self._process_failure_message())
        if not isinstance(value, bytes):
            raise MCPStdioTransportError(
                f"MCP session reader failed: {value!r}"
            )
        try:
            return json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MCPStdioProtocolError(
                f"malformed MCP JSON-RPC response: {exc}"
            ) from exc

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._responses.put(_PROCESS_EOF)
            return
        while True:
            line = process.stdout.readline()
            if not line:
                self._responses.put(_PROCESS_EOF)
                return
            if line.strip():
                self._responses.put(line)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = process.stderr.readline()
            if not line:
                return
            with self._stderr_lock:
                self._stderr.extend(line[-4096:])
                if len(self._stderr) > 16384:
                    del self._stderr[:-16384]

    def _process_failure_message(self) -> str:
        process = self._process
        status = process.poll() if process is not None else None
        with self._stderr_lock:
            detail = bytes(self._stderr).decode("utf-8", errors="replace")
        suffix = f": {detail.strip()}" if detail.strip() else ""
        return f"MCP session exited {status!r}{suffix}"

    def _stop_process(self, *, force: bool) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                if force:
                    process.kill()
                else:
                    process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=2.0)
                    except (OSError, subprocess.TimeoutExpired):
                        return
