"""Boot-time knowledge-MCP reachability: probe, snapshot, and gate (T1/T2).

The product runtime has one retrieve path: the 工作台 talks to the
**iknow-memory** MCP server through the retrieve port. Whether that
server can answer is a three-state fact — 缺 / 坏 / 可达 — and it is
derived from a REAL MCP 2.0 handshake (``initialize`` then
``notifications/initialized`` then ``tools/list``), not from a
``PATH`` lookup alone. A binary that exists but cannot complete the
handshake is 坏, never 可达 (spec SC9; this supersedes ADR 0033's
PATH-only product derivation).

The probe result is published into one in-process snapshot, guarded by
a lock. ``GET /api/status`` and the conversation-entry gate read the
SAME snapshot, so the left corner and the 409 cannot drift.

Why the adapter kind is the BOUND one
-------------------------------------
The snapshot alone does not say whether 缺 closes the gate: that depends
on which retrieve port the process bound. A scripted boot
(``FIN_SCRIPTED_MODEL``) compiles the graph with the fake retrieve port
whatever ``FIN_RETRIEVE_ADAPTER`` names
(``scripted_boot.compile_for_scripted_e2e`` passes
``retrieve_adapter="fake"``), so reading the configured string published
``mcp`` for a scripted process whose gate was open — and the
operator-facing classifier in ``scripts/lib/readiness.sh``, which
mirrors this module's gate, read that payload as GATED and told the
operator that create / open / send would 409 on a process that answers
all three. :func:`resolve_bound_retrieve_adapter_kind` is the ONE
resolution of the bound port; the gate and the status route both read
it, so the published field and the gate cannot disagree (ADR 0033
amendment 2026-09-20).

Why ``None`` means "gate open"
------------------------------
``knowledge_mcp_state()`` returns ``None`` when this process never
probed — the case for a plain ``TestClient`` boot that never goes
through ``fin.host.http.serve()``. The gate treats ``None`` as open so
the hermetic default pytest suite (which never boots ``fin-serve``)
keeps its current conversation contracts. Only a boot that actually
published 缺 / 坏 closes the gate for the bound ``mcp`` adapter.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from typing import Mapping, Sequence

from fin.host.graph import RETRIEVE_ADAPTER_FAKE, RETRIEVE_ADAPTER_MCP
from fin.host.operator_env import resolve_retrieve_adapter_kind
from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioSession,
    MCPStdioTransportError,
)


logger = logging.getLogger(__name__)


# ----- the three states (spec: 缺 / 坏 / 可达) -----

KNOWLEDGE_MCP_MISSING = "missing"
KNOWLEDGE_MCP_FAULT = "fault"
KNOWLEDGE_MCP_REACHABLE = "reachable"

KNOWLEDGE_MCP_STATES = (
    KNOWLEDGE_MCP_MISSING,
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_REACHABLE,
)

# The stable 409 token + operator copy shared by the HTTP gate and (T3)
# the SPA left corner.
KNOWLEDGE_MCP_FAULT_ERROR = "knowledge_mcp_fault"
KNOWLEDGE_MCP_FAULT_MESSAGE = "知识 MCP 故障"


# Probe budget defaults: a short bounded retry, read by tests to shrink.
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_PROBE_ATTEMPTS = 2

# JSON-RPC id for the ``tools/list`` request that completes the probe.
_TOOLS_LIST_ID = 2


# ----- in-process snapshot -----

_state_lock = threading.Lock()
_state: str | None = None


def publish_knowledge_mcp_state(state: str) -> None:
    """Store a known state in the snapshot.

    Validated against :data:`KNOWLEDGE_MCP_STATES` so a typo can never
    become a fourth wire value. Used by boot and by tests.
    """
    if state not in KNOWLEDGE_MCP_STATES:
        raise ValueError(
            f"unknown knowledge MCP state {state!r}; "
            f"expected one of {list(KNOWLEDGE_MCP_STATES)}"
        )
    global _state
    with _state_lock:
        _state = state


def knowledge_mcp_state() -> str | None:
    """Return the snapshot, or ``None`` when this process never probed."""
    with _state_lock:
        return _state


def reset_knowledge_mcp_state() -> None:
    """Clear the snapshot (test / teardown reset)."""
    global _state
    with _state_lock:
        _state = None


def resolve_bound_retrieve_adapter_kind(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Return the kind of retrieve port this process actually BOUND.

    The resolution ``fin.host.http.compile_for_http`` performs when it
    compiles the serving graph: the scripted boot
    (:func:`fin.host.scripted_boot.compile_for_scripted_e2e`) hands
    ``compile_graph`` the fake port unconditionally, so a scripted
    process published the configured string alone is a lie about the
    seam it serves. Every other boot takes
    :func:`fin.host.operator_env.resolve_retrieve_adapter_kind` verbatim.

    ``scripted_boot`` stays lazily imported (the ``_scripted_requested``
    seam below): this module is imported at boot by the HTTP surface, and
    the scripted path pulls a model + a corpus reader that a product boot
    must not pay for.

    ONE resolution, read by the conversation gate AND by the status
    route's ``retrieve_adapter_kind`` field, so the published port and
    the gate cannot drift (ADR 0033 amendment 2026-09-20; the shell
    classifier in ``scripts/lib/readiness.sh`` mirrors the same
    reading).
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    if _scripted_requested(source):
        return RETRIEVE_ADAPTER_FAKE
    return resolve_retrieve_adapter_kind(environ=source)


def knowledge_mcp_gate_closed(*, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether conversation entry must be refused.

    Closed iff the BOUND adapter is the product ``mcp`` adapter AND the
    published snapshot is 缺 or 坏. A snapshot of ``None`` (never probed
    — e.g. plain ``TestClient`` tests) is NOT closed; see the module
    docstring. Fake / scripted boots are never gated: both bound the
    fake port, and :func:`resolve_bound_retrieve_adapter_kind` is what
    says so — the configured ``FIN_RETRIEVE_ADAPTER`` string alone
    misreads the scripted lane (which leaves it unset) as the product
    adapter.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    if resolve_bound_retrieve_adapter_kind(environ=source) != RETRIEVE_ADAPTER_MCP:
        return False
    return knowledge_mcp_state() in (KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT)


# ----- the probe -----


def _probe_once(command: Sequence[str], *, timeout: float) -> bool:
    """Run one handshake attempt. ``True`` iff ``tools/list`` is valid."""
    session = MCPStdioSession(command, timeout_seconds=timeout)
    try:
        # ``call()`` performs initialize + notifications/initialized in
        # ``_ensure_started`` before it writes this request.
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": _TOOLS_LIST_ID,
                "method": "tools/list",
                "params": {},
            },
            timeout_seconds=timeout,
        )
    finally:
        session.close()
    result = response.get("result")
    return isinstance(result, dict) and isinstance(result.get("tools"), list)


def probe_knowledge_mcp(
    *,
    command: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_PROBE_ATTEMPTS,
) -> str:
    """Probe the MCP server and return one of the three states.

    ``missing`` when the launch command is empty, or when its executable
    is not on ``PATH``, or when the caller supplies an ``environ`` mapping
    with no ``PATH`` key — a hand-built mapping must not silently consult
    the ambient process ``PATH``. Otherwise spawn the command and complete
    ``initialize`` + ``notifications/initialized`` + ``tools/list``; a
    valid result is ``reachable``. Any transport / protocol error,
    non-zero exit, timeout, or malformed response is retried a bounded
    number of times and then reported as ``fault``.

    Reuses :class:`fin.retrieve.mcp_session.MCPStdioSession` so the wire
    handshake is the same one the product adapter speaks. Every attempt
    closes its session in a ``finally`` — no child survives the probe.
    """
    from fin.host.compile import DEFAULT_MCP_COMMAND

    launch = tuple(command) if command is not None else DEFAULT_MCP_COMMAND
    if not launch:
        return KNOWLEDGE_MCP_MISSING
    source: Mapping[str, str] = os.environ if environ is None else environ
    search_path = source.get("PATH")
    if search_path is None:
        return KNOWLEDGE_MCP_MISSING
    if shutil.which(launch[0], path=search_path) is None:
        return KNOWLEDGE_MCP_MISSING

    for _attempt in range(max(int(attempts), 1)):
        try:
            if _probe_once(launch, timeout=timeout_seconds):
                return KNOWLEDGE_MCP_REACHABLE
        except (
            MCPStdioTransportError,
            MCPStdioProtocolError,
            OSError,
            ValueError,
        ):
            logger.debug(
                "knowledge MCP handshake attempt failed for %s",
                launch,
                exc_info=True,
            )
    return KNOWLEDGE_MCP_FAULT


def _scripted_requested(environ: Mapping[str, str]) -> bool:
    """Whether this boot is the scripted e2e lane.

    The scripted boot forces the fake retrieve adapter
    (``compile_for_scripted_e2e``), so it must publish 缺 like any
    other fake boot and resolve to the fake bound kind
    (:func:`resolve_bound_retrieve_adapter_kind`). A malformed
    ``FIN_SCRIPTED_MODEL`` is a typed boot failure that
    ``compile_for_http`` will raise anyway; here it only means "not
    confirmed scripted".
    """
    from fin.host.scripted_boot import (
        ScriptedBootConfigError,
        scripted_boot_requested,
    )

    try:
        return scripted_boot_requested(environ)
    except ScriptedBootConfigError:
        return False


def publish_knowledge_mcp(*, environ: Mapping[str, str] | None = None) -> str:
    """Probe, store in the snapshot, and WARN when not reachable.

    The product ``mcp`` adapter is the only configuration whose MCP
    state gates conversation entry: fake / scripted boots publish
    ``missing`` (never ``reachable``) for a test-only adapter that
    ``knowledge_mcp_gate_closed`` leaves open (assumption 8), so the
    warning must not claim those lanes refuse anything. The process is
    NEVER exited here — a missing or unhealthy MCP is a refused
    conversation, not a failed boot.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    product_mcp = (
        resolve_bound_retrieve_adapter_kind(environ=source) == RETRIEVE_ADAPTER_MCP
    )
    state = (
        probe_knowledge_mcp(environ=source)
        if product_mcp
        else KNOWLEDGE_MCP_MISSING
    )
    publish_knowledge_mcp_state(state)

    if state == KNOWLEDGE_MCP_REACHABLE:
        return state
    if product_mcp:
        # Gate is closed: ``knowledge_mcp_gate_closed`` returns True on
        # this exact branch (adapter ``mcp``, not scripted, 缺 / 坏).
        logger.warning(
            "knowledge MCP is %s: %s. Conversation entry (create / open / "
            "send) is refused with %s until the iknow-memory server answers "
            "initialize + tools/list; the process keeps serving.",
            state,
            KNOWLEDGE_MCP_FAULT_MESSAGE,
            KNOWLEDGE_MCP_FAULT_ERROR,
        )
    else:
        # Fake / scripted lane: ``knowledge_mcp_gate_closed`` is False by
        # construction, so do NOT claim a refusal that never happens.
        logger.warning(
            "knowledge MCP is published as %s for a test-only adapter "
            "(fake / scripted); conversation entry (create / open / send) "
            "is NOT gated on this lane.",
            state,
        )
    return state


__all__ = [
    "DEFAULT_PROBE_ATTEMPTS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "KNOWLEDGE_MCP_FAULT",
    "KNOWLEDGE_MCP_FAULT_ERROR",
    "KNOWLEDGE_MCP_FAULT_MESSAGE",
    "KNOWLEDGE_MCP_MISSING",
    "KNOWLEDGE_MCP_REACHABLE",
    "KNOWLEDGE_MCP_STATES",
    "knowledge_mcp_gate_closed",
    "knowledge_mcp_state",
    "probe_knowledge_mcp",
    "publish_knowledge_mcp",
    "publish_knowledge_mcp_state",
    "reset_knowledge_mcp_state",
    "resolve_bound_retrieve_adapter_kind",
]
