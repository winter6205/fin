"""A reusable MCP-over-stdio session for opt-in live integration.

The ordinary adapters keep their simple one-call subprocess behavior.  A
caller that needs several tools to share one in-memory iknow process can
inject this session into the push, retrieve, and invalidation adapters.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import subprocess
import threading
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
    than raising: a typo in an operator's ``.env`` must not take the ingest
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


#: Variables an allowlisted child still inherits from its parent.
#:
#: This list is the whole of what survives when a caller passes ``env=``. It
#: holds process start-up necessities and nothing an authority could travel in:
#: a path, a home for runtime caches, a locale and a temporary directory. Any
#: variable outside this list and outside the caller's mapping stops being
#: inherited, which is the property the private Evidence route depends on — an
#: identity that the host did not inject cannot arrive by inheritance.
INHERITED_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
    "SYSTEMROOT",
    "COMSPEC",
)


class MCPStdioTransportError(OSError):
    """The shared MCP process could not be reached or stopped answering."""


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
        exited.  It clears nothing, so observing it cannot revive a session and
        cannot change this class's terminal-on-failure contract.
        """

        with self._io_lock:
            if self._closed or self._failed:
                return True
            process = self._process
            return process is not None and process.poll() is not None

    def call(
        self,
        payload: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send one tools/call request and return its JSON-RPC response."""

        timeout = (
            self._timeout
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            raise MCPStdioTransportError(
                f"MCP session timeout must be positive, got {timeout}"
            )
        with self._io_lock:
            self._ensure_started(timeout)
            self._requests.append(payload)
            self._write(payload)
            response = self._read_response(timeout)
            if not isinstance(response, dict):
                raise MCPStdioProtocolError(
                    f"MCP tools/call response is {type(response).__name__}, "
                    "expected an object"
                )
            if response.get("id") != payload.get("id"):
                raise MCPStdioProtocolError(
                    "MCP tools/call response id does not match request"
                )
            return response

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

    def _ensure_started(self, timeout: float) -> None:
        if self._closed:
            raise MCPStdioTransportError("MCP session is closed")
        if self._failed:
            raise MCPStdioTransportError("MCP session has failed")
        if self._process is not None:
            if self._process.poll() is not None:
                self._failed = True
                raise MCPStdioTransportError(self._process_failure_message())
            return

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
            self._write(initialize)
            self._write(initialized)
            response = self._read_response(timeout)
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

    def _read_response(self, timeout: float) -> Any:
        try:
            value = self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            self._failed = True
            self._stop_process(force=True)
            raise MCPStdioTransportError(
                f"MCP session timed out after {timeout:g}s"
            ) from exc
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
