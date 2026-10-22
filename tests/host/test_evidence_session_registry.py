"""P2-I2a: the Evidence read rides one identity-bound session per run identity.

The registry is the SESSION-LIFECYCLE prerequisite, not the P2-I2 ticket body:
nothing here reads a stored evidence ref, compares a digest, or replays a
snapshot. What is pinned is the lifecycle underneath that future replay chain.

Nine properties, each able to fail:

* REUSE — a second call on the same identity must not launch a second child.
* ISOLATION — two identities in ONE host process must not share a session, and
  the retrieve, ``read_source`` and issuer legs of ONE identity must all ride
  the session that identity resolved.
* REFUSAL — a configured registry whose resolver selects nothing REFUSES with a
  closed §6 code. It never falls back to the shared adapter: that adapter has
  no identity, and answering under it is the silent identity default ADR B1
  clause 4 forbids.
* REPLACEMENT — a session whose launch failed, or whose child died, is replaced
  on the next bind rather than served. ``MCPStdioSession`` fails TERMINALLY, so
  a cached dead entry would poison that identity for the rest of the process.
* TEARDOWN — the registry registers a process-exit drain when it is built, so
  a child is closed without any caller remembering. ``close_all`` is
  idempotent, and closing a never-launched registry is legal.
* BOUND — the registry is bounded, and eviction CLOSES what it evicts, so a
  resolver that re-mints a deadline per call cannot grow children forever.
* FAIL-CLOSED — a port that cannot be rebound refuses instead of being handed
  back unchanged behind a launched identity.
* ONE RESOLUTION — one retrieve call resolves its binding ONCE, so the
  empty-hit rewrite's second attempt cannot land on a different identity.
* COMPOSITION — ``compile_for_runtime`` → ``compile_graph`` → the tool actually
  carries the bundle. Every case above builds a tool directly, which cannot see
  a threading line deleted between the composition root and the tool.

The session factory is injected so the cases count real launches without
replacing the transport the production path hands out. The two replacement
cases use the REAL ``MCPStdioSession`` with an unspawnable command, so the
terminal state being detected is the one the real transport produces.
"""

from __future__ import annotations

import atexit
import json
import sys
import threading
import types
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host.compile import compile_for_runtime
from fin.host.evidence.evidence_contract import CONTRACT_REFUSAL_CODES
from fin.host.evidence.evidence_session_registry import (
    EvidenceBindingRefusal,
    EvidenceSessionBinding,
    EvidenceSessionRegistry,
)
from fin.host.graph import _make_retrieve_tool, compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.host.tools.issuer_fanout import ISSUER_FANOUT_TOOL
from fin.retrieve.evidence_route import EvidenceIdentityBinding, deadline_within
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import MCPStdioSession, MCPStdioTransportError

#: Storage configuration a child needs to boot — NOT an identity. The registry
#: refuses to launch without it, so every hermetic case supplies it.
STORAGE_ENV = {"GRAPHRAG_MEMORY_STORAGE": "pgvector", "GRAPHRAG_MEMORY_DB_URL": "postgresql://live/db"}


def _binding(
    actor: str = "fin-reader-a", *, refs: tuple[str, ...] = ("src-a",)
) -> EvidenceIdentityBinding:
    return EvidenceIdentityBinding(
        actor_id=actor,
        owner_scope_binding=f"scope-{actor}",
        reader_binding=actor,
        source_refs=refs,
        authorization_revision="rev-1",
        deadline_at=deadline_within(600.0),
    )


class CountingSession(MCPStdioSession):
    """A session that counts its own launches and records what it was given.

    The identity travels in the child environment, so asserting on ``env`` is
    asserting on the real binding seam rather than on a restatement of it.
    """

    launches: list[dict[str, Any]] = []
    closed: list[bool] = []
    _lock: threading.Lock = threading.Lock()

    def __init__(self, command: Any, **kwargs: Any) -> None:
        super().__init__(command, **kwargs)
        with CountingSession._lock:
            CountingSession.launches.append(
                {"command": list(command), "env": dict(kwargs.get("env") or {})}
            )

    def close(self) -> None:
        with CountingSession._lock:
            CountingSession.closed.append(True)
        super().close()


@pytest.fixture(autouse=True)
def _reset_counters() -> Any:
    CountingSession.launches = []
    CountingSession.closed = []
    _MINTED.clear()
    yield
    CountingSession.launches = []
    CountingSession.closed = []
    _MINTED.clear()


def _registry(**kwargs: Any) -> EvidenceSessionRegistry:
    kwargs.setdefault("transport_env", dict(STORAGE_ENV))
    return EvidenceSessionRegistry(
        ["iknow-memory"],
        session_factory=CountingSession,
        **kwargs,
    )


def _evidence(
    registry: EvidenceSessionRegistry,
    resolver: Any,
) -> EvidenceSessionBinding:
    return EvidenceSessionBinding(sessions=registry, resolve=resolver)


def _static(binding: EvidenceIdentityBinding | None) -> Any:
    return lambda _config: binding


class _SessionRecordingAdapter(MCPRetrieveAdapter):
    """An adapter that reports the session each call actually rode.

    Overriding ``_call_mcp`` is the documented transport seam, so this observes
    the real hop: whatever session the tool bound is the one that would have
    been used, not the one a constructor was handed. The rebound adapter is a
    shallow copy, so ``ridden`` is the SAME list as the base's — that is what
    lets one assertion see every leg.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ridden: list[Any] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ridden.append(self._session)
        return {"chunks": []}


def _invoke(tool: Any, query: str, thread_id: str) -> Any:
    return tool.invoke(
        {"name": "retrieve", "args": {"query": query}, "id": "c1", "type": "tool_call"},
        config={"configurable": {"thread_id": thread_id}},
    )


def _envelope(result: Any) -> dict[str, Any]:
    """The JSON body of a tool result's ToolMessage.

    ``tool.invoke`` on a bare tool-call dict returns the ``ToolMessage`` itself,
    not the ``Command`` the node returns, so this unwraps one level.
    """
    message = getattr(result, "content", None)
    if message is None:
        message = result.update["messages"][-1].content
    return json.loads(message)


def _stable_binding(
    actor: str, *, refs: tuple[str, ...] = ("src-a",)
) -> EvidenceIdentityBinding:
    """One binding per actor, minted ONCE and reused.

    ``_binding`` re-mints ``deadline_at`` on every call — that is what the
    re-minting case is about. A case that means "the same identity twice" must
    therefore say so explicitly, or it silently exercises the new-key path.
    """
    return _MINTED.setdefault(actor, _binding(actor, refs=refs))


_MINTED: dict[str, EvidenceIdentityBinding] = {}


# ----- storage configuration (Low 1) -----


def test_an_unconfigured_registry_refuses_before_it_can_launch_a_child() -> None:
    """``transport_env`` carries the child's storage configuration, not an identity.

    ``identity_env`` merges the binding ON TOP of ``transport_env``, so an empty
    one leaves the child with the five identity variables and no storage
    variables at all: it dies during boot, once per identity, with nothing in
    the host naming the cause. Refusing at construction puts the failure where
    the operator who wired the registry is still reading the traceback.
    """

    with pytest.raises(ValueError, match="transport_env"):
        EvidenceSessionRegistry(["iknow-memory"], session_factory=CountingSession)

    assert CountingSession.launches == [], "nothing was launched"


def test_the_bound_session_keeps_the_storage_configuration_it_was_given() -> None:
    """The storage env must survive the merge — it is what lets the child boot."""

    registry = _registry(transport_env={"GRAPHRAG_MEMORY_DB_URL": "postgresql://x"})

    registry.bind(_binding())

    child_env = CountingSession.launches[0]["env"]
    assert child_env["GRAPHRAG_MEMORY_DB_URL"] == "postgresql://x"
    # And the identity is still written last, so storage config cannot displace it.
    assert child_env["IKNOW_EVIDENCE_ACTOR_ID"] == "fin-reader-a"


# ----- reuse -----


def test_one_identity_reuses_one_session_across_calls() -> None:
    """The second call must not launch a second child.

    This is the defect ``mcp_adapter.py``'s per-call
    ``_launch_mcp_process`` fallback caused: relaunching re-authenticates as a
    fresh actor, so the Evidence read could never answer an authorization
    question about a stored snapshot's owner.
    """

    registry = _registry()
    binding = _binding()

    first = registry.bind(binding)
    second = registry.bind(binding)

    assert len(CountingSession.launches) == 1, "one identity, one child"
    assert first.session is second.session
    assert first.client._session is second.client._session


def test_the_bound_session_carries_the_binding_into_the_child_env() -> None:
    """The child is launched with the binding, not with the parent's identity."""

    registry = _registry()
    binding = _binding("fin-reader-env")

    registry.bind(binding)

    child_env = CountingSession.launches[0]["env"]
    assert child_env["IKNOW_EVIDENCE_ACTOR_ID"] == "fin-reader-env"
    assert child_env["IKNOW_EVIDENCE_OWNER_SCOPE"] == "scope-fin-reader-env"
    assert child_env["IKNOW_EVIDENCE_SOURCE_REFS"] == "src-a"


# ----- per-identity isolation -----


def test_two_identities_in_one_process_do_not_share_a_session() -> None:
    """Distinct identities must not share a child.

    One shared session would be M1 — the defect the IK Query lane still
    carries — in a different module: a process-frozen identity answering for
    every caller it serves.
    """

    registry = _registry()
    a = registry.bind(_binding("fin-reader-a"))
    b = registry.bind(_binding("fin-reader-b"))

    assert a.session is not b.session
    assert len(CountingSession.launches) == 2
    assert registry.bound_identities == 2


def test_a_binding_differing_only_in_scope_still_gets_its_own_session() -> None:
    """The key is the whole binding, so no member can be varied away."""

    registry = _registry()
    same_actor = _binding("fin-reader-x", refs=("src-a",))
    narrowed = _binding("fin-reader-x", refs=("src-b",))

    assert same_actor is not narrowed
    assert registry.bind(same_actor).session is not registry.bind(narrowed).session
    assert len(CountingSession.launches) == 2


def test_a_re_resolved_deadline_mints_a_new_session_rather_than_reusing_one() -> None:
    """Reuse requires a stable binding; a re-dated grant is a new session.

    This pins the invariant the registry keys on, so a host that re-mints a
    deadline per call gets a fresh session (fail-closed, the old grant is not
    silently extended) instead of a child launched under a stale one. The BOUND
    below is what keeps that from becoming unbounded growth.
    """

    registry = _registry()

    registry.bind(_binding("fin-reader-x"))
    registry.bind(_binding("fin-reader-x"))

    assert len(CountingSession.launches) == 2
    assert registry.bound_identities == 2


# ----- refusal (High 1) -----


def test_an_absent_binding_refuses_with_a_closed_typed_code() -> None:
    """No binding is a refusal, not a default scope (ADR B1)."""

    registry = _registry()

    with pytest.raises(EvidenceBindingRefusal) as excinfo:
        registry.bind(None)

    refusal = excinfo.value
    assert refusal.code in CONTRACT_REFUSAL_CODES, "the code is inside §6"
    assert refusal.code == "unsupported_source_binding"
    assert refusal.as_dict()["code"] == refusal.code
    assert CountingSession.launches == [], "a refusal launches no child"


def test_a_refusal_leaves_no_session_behind_for_a_later_identity() -> None:
    """Refusing must not poison the registry's own bookkeeping."""

    registry = _registry()

    with pytest.raises(EvidenceBindingRefusal):
        registry.bind(None)

    assert registry.bound_identities == 0
    assert registry.bind(_binding()).session is not None


def test_a_port_that_cannot_be_rebound_refuses_instead_of_failing_open() -> None:
    """A port with no ``with_session`` must not be handed back unchanged.

    Returning it would be a silent fail-open on the enforcement path: the
    identity was minted and a child was launched and retained for it, and the
    call would then read under whatever transport the adapter happened to
    carry — which is the whole outcome the binding exists to prevent.
    """

    registry = _registry()
    unbound_port = object()

    with pytest.raises(EvidenceBindingRefusal) as excinfo:
        registry.bind(_binding()).adapter_for(unbound_port)  # pyright: ignore[reportArgumentType]

    assert excinfo.value.code == "unsupported_source_binding"
    # The identity really was bound first, so this is a late refusal, not a
    # short-circuit that never reached the registry.
    assert registry.bound_identities == 1


# ----- the retrieve tool on the refusal path (High 1) -----


def test_an_unresolved_binding_refuses_the_call_instead_of_riding_the_shared_adapter() -> (
    None
):
    """A configured registry whose resolver selects nothing must REFUSE.

    THIS CASE REPLACED ``test_an_unresolved_binding_leaves_the_call_on_the_shared_adapter``,
    which asserted ``set(base.ridden) == {None}`` — that assertion PINNED the
    defect it appeared to describe. Returning the shared adapter there is a
    silent identity default: the shared adapter carries no session, so
    ``mcp_adapter`` relaunches a child per call and answers as nobody this run
    selected. ADR B1 clause 4 requires a typed refusal instead, so that is what
    the envelope now carries.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])

    tool = _make_retrieve_tool(
        base,
        evidence=_evidence(registry, _static(None)),
    )

    result = _invoke(tool, "what was revenue", "thread-a")

    assert _envelope(result) == {"error": "unsupported_source_binding"}
    assert base.ridden == [], "the port was never called under an unbound identity"
    assert CountingSession.launches == [], "a refusal launches no child"
    assert registry.bound_identities == 0


def test_an_unwired_boot_is_the_only_unbound_answer() -> None:
    """``evidence is None`` keeps the pre-P2-I2 transport, and says why.

    This is the one surviving unbound path, and it means NO REGISTRY IS
    CONFIGURED for the boot — not that a run failed to resolve one. A wired
    deployment that resolved nothing refuses (above), so the two can never be
    confused.
    """

    base = _SessionRecordingAdapter(command=["iknow-memory"])

    tool = _make_retrieve_tool(base)

    result = _invoke(tool, "what was revenue", "thread-a")

    assert "error" not in _envelope(result), "an unwired boot is not a refusal"
    assert set(base.ridden) == {None}, "no registry, so no identity to ride"
    assert CountingSession.launches == []


# ----- one resolution per call (Medium 2) -----


def test_one_call_resolves_its_binding_once_so_both_attempts_share_it() -> None:
    """The resolver runs ONCE per call, not once per port attempt.

    The binding carries its own ``deadline_at``, so a second resolution could
    hand the empty-hit rewrite's attempt a DIFFERENT identity from the first —
    the exact "stricter, but not what the comment claimed" drift this pins. A
    counter on the resolver is the observation; the shared session across both
    attempts is the consequence.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])
    resolved: list[str] = []

    def _resolver(_config: Any) -> EvidenceIdentityBinding:
        resolved.append("called")
        # Deliberately re-minting: if the tool resolved per attempt, the two
        # attempts would carry different deadlines and different sessions.
        return _binding("fin-reader-rewrite")

    tool = _make_retrieve_tool(
        base,
        evidence=_evidence(registry, _resolver),
    )

    _invoke(tool, "what was revenue", "thread-a")

    assert len(resolved) == 1, "one resolution per call"
    assert len(base.ridden) > 1, "the empty hit really did rewrite and retry"
    assert len(set(id(session) for session in base.ridden)) == 1, (
        "every attempt of one call rode one identity"
    )
    assert len(CountingSession.launches) == 1, "one child, not one per attempt"


# ----- replacement of a dead session (High 2) -----


def test_a_session_whose_launch_failed_is_replaced_rather_than_served() -> None:
    """A failed launch must not poison the identity for the process lifetime.

    ``MCPStdioSession`` fails TERMINALLY: the first launch failure sets
    ``_failed``, and every later ``call`` raises "MCP session has failed" with no
    re-spawn. A registry that cached that entry would answer every later call
    for this identity with a dead transport forever. The real session class is
    used here, driven by a command that cannot spawn, so the state being
    detected is the one the real transport produces.
    """

    served: list[Any] = []

    def _factory(command: Any, **kwargs: Any) -> MCPStdioSession:
        if not served:
            broken = MCPStdioSession(
                ["/nonexistent-iknow-binary"], timeout_seconds=1.0, env={}
            )
            served.append(broken)
            return broken
        healthy = CountingSession(command, **kwargs)
        served.append(healthy)
        return healthy

    registry = EvidenceSessionRegistry(
        ["iknow-memory"],
        transport_env=dict(STORAGE_ENV),
        session_factory=_factory,
    )
    binding = _binding("fin-reader-retry")

    first = registry.bind(binding)
    with pytest.raises(MCPStdioTransportError, match="failed to launch"):
        first.session.call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, 1.0)
    assert first.session.is_spent, "the first session really is terminal"

    second = registry.bind(binding)

    assert second.session is not first.session, "the dead entry was replaced"
    assert len(served) == 2, "a fresh child was launched"
    assert registry.bound_identities == 1, "and the identity still holds exactly one"
    # The dead entry is CLOSED, not merely dropped: a session that failed after
    # spawning would otherwise leave its process behind when it is replaced.
    assert served[0]._closed is True, "the replaced session was closed"


def test_a_session_whose_child_died_is_replaced_on_the_next_bind() -> None:
    """A child that exited is as unusable as one that never launched."""

    registry = _registry()
    binding = _binding("fin-reader-died")

    first = registry.bind(binding)
    first.session.kill()

    second = registry.bind(binding)

    assert first.session.is_spent
    assert second.session is not first.session
    assert len(CountingSession.launches) == 2, "a re-spawn, not a re-read of the corpse"


# ----- teardown reachability and the bound (High 3) -----


def test_the_registry_registers_a_process_exit_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown is REACHABLE: nothing has to remember to call ``close_all``.

    The previous claim — "closing the registry leaves no live child" — was true
    of the method and false of the system: nothing in ``src/`` ever called it,
    so on a long-lived host every child launched survived until process exit by
    accident rather than by design. Construction is where the registry can
    reach process exit on its own, and it is the point at which the operator who
    wired it is present.
    """

    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", registered.append)

    registry = _registry()
    registry.bind(_binding("fin-reader-a"))
    registry.bind(_binding("fin-reader-b"))

    assert registry.close_all in registered, "the drain is registered at construction"

    # Invoke what atexit would invoke, rather than trusting the registration.
    for callback in registered:
        callback()

    assert len(CountingSession.closed) == 2, "both children were closed at exit"
    assert registry.bound_identities == 0


def test_close_all_closes_every_launched_session() -> None:
    """A session must not leak its process on shutdown."""

    registry = _registry()
    registry.bind(_binding("fin-reader-a"))
    registry.bind(_binding("fin-reader-b"))

    registry.close_all()

    assert len(CountingSession.closed) == 2, "one child per identity, both closed"
    assert registry.bound_identities == 0


def test_close_all_is_idempotent_and_safe_on_an_empty_registry() -> None:
    """Both callers of the drain may run, and neither may have launched anything.

    A double drain is the normal case: ``atexit`` fires whatever the host did at
    its own shutdown, and a registry that was never used still gets one.
    """

    registry = _registry()
    registry.close_all()
    registry.bind(_binding("fin-reader-a"))
    registry.close_all()
    registry.close_all()

    assert len(CountingSession.closed) == 1, "the second drain had nothing left"
    assert registry.bound_identities == 0


def test_the_registry_evicts_the_least_recently_used_identity_at_its_bound() -> None:
    """``_sessions`` is bounded, and eviction CLOSES what it evicts.

    A resolver that re-mints a deadline on every call produces a new key every
    call, so each one launches a child. Without a bound that is unbounded
    subprocess growth on a long-lived host; with a bound but no close it is
    bounded bookkeeping over unbounded leaked processes.
    """

    registry = _registry(max_identities=2)

    first = registry.bind(_stable_binding("fin-reader-1"))
    registry.bind(_stable_binding("fin-reader-2"))
    third = registry.bind(_stable_binding("fin-reader-3"))

    assert registry.bound_identities == 2, "the bound holds"
    assert len(CountingSession.closed) == 1, "the evicted child was closed, not leaked"
    assert len(CountingSession.launches) == 3
    assert first.session.is_spent, "the evicted entry really was dropped"

    # The identity that is still held is the one the bound kept, and it is
    # reused rather than relaunched.
    assert registry.bind(_stable_binding("fin-reader-3")).session is third.session
    assert len(CountingSession.launches) == 3, "the survivor is reused"


def test_a_recently_used_identity_survives_eviction() -> None:
    """Eviction is least-recently-USED, not first-in.

    Without this, "evicts something" would pass on a FIFO that discards the
    identity a live run is still calling with — the same identity loss the
    registry exists to prevent, just quieter.
    """

    registry = _registry(max_identities=2)
    oldest = registry.bind(_stable_binding("fin-reader-1"))
    middle = registry.bind(_stable_binding("fin-reader-2"))

    # Touch the OLDEST, making the middle the least recently used.
    assert registry.bind(_stable_binding("fin-reader-1")).session is oldest.session

    registry.bind(_stable_binding("fin-reader-3"))

    assert middle.session.is_spent, "the middle entry was the victim"
    assert registry.bind(_stable_binding("fin-reader-1")).session is oldest.session
    assert len(CountingSession.launches) == 3, "the touched identity was not relaunched"


# ----- the rebound adapter, on the registry rather than through the tool -----


def test_the_adapter_is_rebound_to_the_identity_session_not_mutated_in_place() -> None:
    """Rebinding copies the adapter, so the shared one keeps no identity.

    ``compile_graph`` builds one adapter per process and shares its locator map
    and both ledgers across every identity. Mutating that instance in place would
    hand the LAST caller's identity to the next caller — M1 again, one layer
    down. Only the copy may change.
    """

    registry = _registry()
    bound = _binding("fin-reader-tool")
    base = MCPRetrieveAdapter(command=["iknow-memory"])

    rebound = registry.bind(bound).adapter_for(base)

    assert rebound is not base, "the adapter is rebound, not mutated in place"
    assert rebound._session is registry.bind(bound).session
    assert base._session is None, "the shared adapter keeps no identity of its own"


def test_the_retrieve_tool_resolves_its_binding_per_call_not_at_build_time() -> None:
    """Two calls under different identities must not share one adapter.

    A build-time binding would send both calls down one session, which is M1 in
    a different module — a process-frozen identity answering for every caller
    the process serves.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])
    minted: dict[str, EvidenceIdentityBinding] = {}

    def _resolver(config: Any) -> EvidenceIdentityBinding:
        # Identity binds per CONNECTION (ADR B1), so the host mints once per
        # thread and every port attempt of that thread's call sees the same
        # binding rather than a freshly dated one.
        thread = str(((config or {}).get("configurable") or {}).get("thread_id") or "")
        return minted.setdefault(thread, _binding(f"fin-{thread}"))

    tool = _make_retrieve_tool(
        base,
        evidence=_evidence(registry, _resolver),
    )

    # Building the tool must not have captured an identity.
    assert len(CountingSession.launches) == 0, "construction launches no child"

    _invoke(tool, "what was revenue", "thread-a")
    _invoke(tool, "what was revenue", "thread-b")

    # The empty-hit rewrite retries the port, so the count is not the claim; the
    # set of sessions those calls rode is.
    assert base.ridden, "the port was called"
    assert all(session is not None for session in base.ridden), "every call was bound"
    ridden = list(dict.fromkeys(base.ridden))
    assert len(ridden) == 2, "two identities, two sessions"
    assert ridden[0] is registry.bind(minted["thread-a"]).session
    assert ridden[1] is registry.bind(minted["thread-b"]).session
    assert len(CountingSession.launches) == 2, "one child per identity, not per call"


# ----- every leg of one identity rides the same session (Medium 1) -----


def _read_source_invoke(tool: Any, thread_id: str) -> Any:
    return tool.invoke(
        {
            "name": "read_source",
            "args": {
                "source_ref": {
                    "document_id": "AAPL-2022Q3",
                    "version": "a" * 64,
                    "unit": 1,
                }
            },
            "id": "c-read",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def test_the_read_source_company_leg_rides_the_same_binding_as_the_retrieve() -> None:
    """A slice and the retrieve that produced its locator share one identity.

    ``read_source``'s company leg used to be the BUILD-TIME adapter, so a wired
    deployment could hold an identity-bound ``retrieve`` and an unbound
    ``read_source`` in the same run. The citation would then rest on a body the
    run's own identity never authorized.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])
    binding = _binding("fin-reader-legs")

    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=base,
        evidence=_evidence(registry, _static(binding)),
    )
    read_tool = next(
        tool for tool in model.bound_tools if getattr(tool, "name", "") == "read_source"
    )

    _read_source_invoke(read_tool, "thread-a")

    assert base.ridden, "the company leg was used"
    assert all(session is not None for session in base.ridden), (
        "read_source rode a bound identity, not the shared adapter"
    )
    assert base.ridden[0] is registry.bind(binding).session


def test_the_read_source_company_leg_refuses_when_no_binding_resolves() -> None:
    """The company leg speaks the same typed EXIT, not ``unknown_locator``.

    Answering ``unknown_locator`` would blame the caller's locator for a refusal
    that was about identity.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])

    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=base,
        evidence=_evidence(registry, _static(None)),
    )
    read_tool = next(
        tool for tool in model.bound_tools if getattr(tool, "name", "") == "read_source"
    )

    result = _read_source_invoke(read_tool, "thread-a")

    assert _envelope(result) == {"error": "unsupported_source_binding"}
    assert base.ridden == [], "no body source was read under an unbound identity"


def test_every_issuer_branch_rides_the_runs_own_identity() -> None:
    """The fan-out branch is a retrieve leg too, so it binds like one.

    It rode the build-time adapter, so a wired run could fan out and gather
    evidence under a different identity than the one that planned the fan-out.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])
    binding = _binding("fin-reader-fanout")
    issuers = ["AAPL", "MSFT"]

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": ISSUER_FANOUT_TOOL,
                            "args": {
                                "issuers": issuers,
                                "query": "services revenue",
                            },
                            "id": "c-fanout",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        adapter=base,
        evidence=_evidence(registry, _static(binding)),
    )

    graph.invoke(
        reset_input_state(
            "Compare services revenue across AAPL and MSFT for Q3 2023."
        ),
        config={"configurable": {"thread_id": "thread-fanout"}},
    )

    assert base.ridden, "the branches retrieved"
    assert all(session is not None for session in base.ridden), (
        "every branch rode a bound identity"
    )
    assert set(id(session) for session in base.ridden) == {
        id(registry.bind(binding).session)
    }, "one identity for the whole fan-out"


# ----- the composition seam (Medium 4) -----


class _FakeChatOpenAI:
    def __init__(self, **_kwargs: Any) -> None:
        self.bound_tools: list[Any] = []


class _FakePostgresSaver:
    def setup(self) -> None:
        return None


class _FakePostgresCM:
    def __enter__(self) -> _FakePostgresSaver:
        return _FakePostgresSaver()

    def __exit__(self, *exc: object) -> None:
        return None


class _FakePostgresSaverFactory:
    @classmethod
    def from_conn_string(cls, _url: str, **_kwargs: Any) -> _FakePostgresCM:
        return _FakePostgresCM()


@pytest.fixture
def _stub_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_module = types.ModuleType("langchain_openai")
    chat_module.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", chat_module)
    pg_module = types.ModuleType("langgraph.checkpoint.postgres")
    pg_module.PostgresSaver = _FakePostgresSaverFactory
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_module)
    for key in ("FIN_RETRIEVE_ADAPTER", "FIN_MODEL", "FIN_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def test_compile_for_runtime_hands_the_bundle_to_the_graph(
    monkeypatch: pytest.MonkeyPatch, _stub_boot: None
) -> None:
    """The composition root must not drop the bundle on the way down.

    Every other case in this file builds ``_make_retrieve_tool`` directly, so
    deleting this line leaves all of them green while production boots with the
    Evidence lane silently switched off.
    """

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("FIN_MODEL", "synthetic-evidence-test-model")
    monkeypatch.setattr(
        "fin.host.compile.compile_graph",
        lambda **kwargs: calls.append(kwargs) or "compiled",
    )

    registry = _registry()
    compile_for_runtime(
        checkpointer_url="postgresql://x",
        evidence=_evidence(registry, _static(_binding())),
    )
    assert calls[-1]["evidence"] is not None, "the bundle reached compile_graph"
    assert calls[-1]["evidence"].sessions is registry

    # And the unwired boot still compiles the pre-P2-I2 shape.
    compile_for_runtime(checkpointer_url="postgresql://x")
    assert calls[-1]["evidence"] is None


def test_the_compiled_graphs_retrieve_tool_actually_rides_the_registry_session() -> None:
    """``compile_graph`` must hand the bundle to the tool, not just accept it.

    The seam has two halves — root to graph, and graph to tool — and deleting
    either one is invisible to every other case here. This drives the real
    compiled graph's tool, so the assertion is on the hop a run makes.
    """

    registry = _registry()
    base = _SessionRecordingAdapter(command=["iknow-memory"])
    binding = _binding("fin-reader-composed")

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": "what was revenue"},
                            "id": "c-composed",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Revenue grew."),
            ]
        ),
        adapter=base,
        evidence=_evidence(registry, _static(binding)),
    )

    graph.invoke(
        # The 档 prefix is what carries this run past the cascade's clarify
        # residual to a tool hop; without it the model is never asked to call
        # ``retrieve`` and the port is never reached at all.
        reset_input_state("知识问答档 what was AAPL net sales in 2023 Q3?"),
        config={"configurable": {"thread_id": "thread-composed"}},
    )

    assert base.ridden, "the compiled tool called the port"
    assert all(session is not None for session in base.ridden), (
        "the bundle reached the tool: every call rode an identity"
    )
    assert base.ridden[0] is registry.bind(binding).session
