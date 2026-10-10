"""One persistent, identity-bound MCP session per host run identity.

``EvidenceRouteClient`` is not a session owner: it borrows a session launched
with :func:`~fin.retrieve.evidence_route.identity_env`, and the retrieve leg has
to ride that same session. :mod:`fin.host.graph` builds its adapter once per
host process, so freezing a session at build time would hand every caller in
that process one shared identity — the defect the IK Query lane still carries
(M1 in ``docs/validation/2026-10-07-p1-review-closure-verdict.md``). This
module is therefore keyed by identity, not by process: the host resolves the
run's binding at request time and every identity gets its own session.

**One session per identity, reused for its lifetime.** The key is the whole
frozen binding, so two callers whose bindings differ in any member cannot
collide, and two runs of the same identity share a session instead of paying a
child launch per call. Reuse is what makes the Evidence read ride the process
that produced the reference; relaunching per call would re-authenticate as a
fresh actor and could never answer an authorization question about a stored
snapshot's owner.

**Reuse requires a stable binding.** The key is the whole frozen binding, and a
binding carries its own ``deadline_at``, so a host that re-mints a deadline on
every call gets a fresh session per call rather than one shared child. That is
deliberate: identity binds per CONNECTION (ADR B1), so a run mints once per
call and both port attempts of one retrieve ride that one binding. A re-dated
grant is a new grant, and reusing the child launched under the older one would
silently extend a deadline the host had already let lapse. Because that makes a
re-minting resolver produce a new key per call, the registry is BOUNDED and
evicts the least recently used identity, closing what it evicts.

**A dead session is replaced, never served.** ``MCPStdioSession`` fails
terminally — ``_failed`` makes every later call raise — so caching a session
whose child died would poison that identity for the rest of the process. Lookup
therefore asks :attr:`~fin.retrieve.mcp_session.MCPStdioSession.is_spent` and
re-launches instead of handing back a dead entry. The registry replaces the
session; it never asks the session to forget it failed.

**An absent binding refuses.** A resolver that returns nothing has not
selected a scope, and there is no default one to fall back to (ADR B1: no
process-level fallback, no default scope). The refusal carries
:data:`~fin.host.evidence_contract.UNSUPPORTED_SOURCE_BINDING`, which is
already inside the closed §6 set, so no code is added here.

**Storage configuration is required at construction.** ``transport_env`` carries
the child's storage configuration, and :func:`identity_env` merges the binding on
top of it. With nothing to merge, the child would boot under a closed
allowlist holding only the identity variables and no storage variables at all —
a boot failure discovered once per identity, as a child that dies at startup. An
empty ``transport_env`` is refused here instead, where the operator who wired it
is still reading the traceback.

**Teardown is reachable.** The registry registers :meth:`close_all` with
:mod:`atexit` when it is constructed, so a child this registry launched is closed
when the host process exits — no caller has to remember. The registration is
per-instance and ``close_all`` is idempotent, so a host that also drains the
registry explicitly at shutdown has nothing to unwind twice.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping, Sequence, TypeVar, cast

from fin.host.evidence_contract import UNSUPPORTED_SOURCE_BINDING
from fin.retrieve.evidence_route import (
    EvidenceIdentityBinding,
    EvidenceRouteClient,
    identity_env,
)
from fin.retrieve.mcp_session import MCPStdioSession
from fin.retrieve.port import RetrievePort

#: The port the host already built, rebound per identity without being rebuilt.
_P = TypeVar("_P", bound=RetrievePort)

#: The session constructor, injectable so a test can count launches without
#: replacing the transport the registry hands out.
SessionFactory = Callable[..., MCPStdioSession]

#: How many identities may hold a session at once. A run mints one binding and
#: reuses it, so the steady state is the number of concurrent identities; the
#: headroom is for the deadline re-mint the module docstring describes, which is
#: otherwise unbounded subprocess growth on a long-lived host.
DEFAULT_MAX_IDENTITIES: Final[int] = 32

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


class EvidenceBindingRefusal(RuntimeError):
    """No identity binding was resolved for this run.

    ``code`` is the closed §6 vocabulary member this refusal belongs to. It is
    raised before any process is launched, so an unbound caller cannot end up
    reading under an identity it never selected.
    """

    code: Final[str] = UNSUPPORTED_SOURCE_BINDING

    def __init__(self, reason: str) -> None:
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class BoundEvidenceSession:
    """One identity's session and the two clients that ride it.

    ``adapter`` and ``client`` share the session on purpose: the Evidence read
    must be the same process and the same identity as the retrieve that produced
    the reference, which only holds while both borrow one session.
    """

    binding: EvidenceIdentityBinding
    session: MCPStdioSession
    client: EvidenceRouteClient

    def adapter_for(self: BoundEvidenceSession, adapter: _P) -> _P:
        """The adapter rebound to this session, sharing its frozen state.

        The host builds its adapter once, so the identity arrives per request
        rather than per process. Rebinding copies the adapter instead of
        rebuilding it, which keeps the locator map and both ledgers the graph
        already injected shared across identities.

        A port with no ``with_session`` REFUSES rather than being handed back
        unchanged. Returning it would be a silent fail-open on the enforcement
        path: the identity was minted, a child was launched and retained for it,
        and the call would then read under whatever transport the adapter
        happened to carry. That is the exact outcome the binding exists to
        prevent, reached through a port that cannot be rebound.
        """

        with_session = getattr(adapter, "with_session", None)
        if with_session is None:
            raise EvidenceBindingRefusal(
                f"{type(adapter).__name__} cannot be rebound to this identity: "
                "it has no with_session() seam, so the call would read under an "
                "unbound transport"
            )
        return cast("_P", with_session(self.session))


class EvidenceSessionRegistry:
    """Sessions keyed by identity binding, created on first use and reused.

    Bounded at ``max_identities`` and drained at process exit, so neither a
    deadline re-minting resolver nor a long-lived host can grow children
    without limit or outlive the process that launched them.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        transport_env: Mapping[str, str] | None = None,
        timeout_seconds: float = 90.0,
        session_factory: SessionFactory | None = None,
        max_identities: int = DEFAULT_MAX_IDENTITIES,
    ) -> None:
        if not command:
            raise ValueError("the evidence session command must not be empty")
        if not transport_env:
            # Refused HERE rather than at the first bind: an unconfigured
            # registry launches a child whose environment holds the five
            # identity variables and no storage configuration at all, so the
            # failure surfaces as a child that dies during boot — once per
            # identity, with nothing in the host naming the real cause.
            raise ValueError(
                "the evidence session registry needs transport_env: without it "
                "the child boots under a closed allowlist carrying identity "
                "variables only, and no storage configuration to reach"
            )
        if max_identities < 1:
            raise ValueError(
                f"max_identities must be positive, got {max_identities!r}"
            )
        self._command: tuple[str, ...] = tuple(str(part) for part in command)
        self._transport_env: dict[str, str] = dict(transport_env)
        self._timeout = float(timeout_seconds)
        self._max_identities = int(max_identities)
        self._session_factory: SessionFactory = (
            session_factory if session_factory is not None else MCPStdioSession
        )
        self._sessions: dict[EvidenceIdentityBinding, MCPStdioSession] = {}
        # Insertion-ordered mirror of ``_sessions`` keys: the front is the
        # least recently used identity, so eviction is a pop from the left
        # and reuse is a move to the right.
        self._usage: "OrderedDict[EvidenceIdentityBinding, None]" = OrderedDict()
        self._lock = threading.Lock()
        # Reachable teardown: the host has no shutdown hook that a registry can
        # be handed to, and a child that outlives the process is a leaked
        # process, so process exit is the hook. ``close_all`` is idempotent, so
        # a host that also drains the registry itself unwinds nothing twice.
        atexit.register(self.close_all)

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def max_identities(self) -> int:
        return self._max_identities

    @property
    def bound_identities(self) -> int:
        """How many distinct identities currently hold a session."""

        with self._lock:
            return len(self._sessions)

    def bind(
        self, binding: EvidenceIdentityBinding | None
    ) -> BoundEvidenceSession:
        """The session for this run's identity, launching one on first use.

        Refuses rather than defaulting when ``binding`` is ``None``: a run that
        resolved no identity has selected no scope, and inheriting some other
        caller's would answer for a subject this run never named.
        """

        if binding is None:
            raise EvidenceBindingRefusal(
                "this run resolved no evidence identity binding, and no default "
                "scope exists to fall back to"
            )
        session = self._session_for(binding)
        return BoundEvidenceSession(
            binding=binding,
            session=session,
            client=EvidenceRouteClient(session, binding=binding),
        )

    def close_all(self) -> None:
        """Close every session this registry launched.

        A child process outliving the host that launched it is a leaked
        process, so teardown drains the whole registry rather than the most
        recently used entry. Safe to call more than once, and safe to call when
        nothing was ever launched.
        """

        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._usage.clear()
        self._close_quietly(sessions)

    def _session_for(self, binding: EvidenceIdentityBinding) -> MCPStdioSession:
        """This identity's live session, launching or replacing as needed.

        Two entries can be replaced rather than returned: one whose child has
        died (serving it would answer every later call with a dead transport,
        because a failed session refuses every subsequent call by name), and
        the least recently used ones once the bound is reached.
        """

        with self._lock:
            cached = self._sessions.get(binding)
        # Probed OUTSIDE the registry lock, deliberately. ``is_spent`` takes the
        # session's own IO lock, which an in-flight call holds for up to the
        # call timeout. Probing under the registry lock would let one slow
        # identity block every OTHER identity's bind for that whole window —
        # the per-identity isolation this module exists to provide.
        if cached is not None and not cached.is_spent:
            with self._lock:
                current = self._sessions.get(binding)
                if current is not None:
                    self._usage.move_to_end(binding)
                    return current
            # Another thread replaced the entry between the two locks. Fall
            # through to the slow path rather than trusting a stale object.

        spent: MCPStdioSession | None = None
        with self._lock:
            existing = self._sessions.get(binding)
            if existing is not None:
                # Re-checked under the lock: the answer can have changed while
                # the probe above was waiting, and a live entry must be reused.
                if not existing.is_spent:
                    self._usage.move_to_end(binding)
                    return existing
                # Spent: drop the dead entry so the identity is not poisoned,
                # then re-launch below. The dead child is closed there, because
                # an exited one is already gone and a session that failed
                # before spawning has no process to reap.
                del self._sessions[binding]
                self._usage.pop(binding, None)
                spent = existing
            session = self._launch(binding)
            self._sessions[binding] = session
            self._usage[binding] = None
            evicted = self._evict_overflow()
        self._close_quietly(
            [item for item in (spent, *evicted) if item is not None]
        )
        return session

    def _evict_overflow(self) -> list[MCPStdioSession]:
        """Drop the least recently used identities past the bound.

        The caller holds ``self._lock``; closing is done by the caller so a
        child's shutdown never runs under the registry lock.
        """

        evicted: list[MCPStdioSession] = []
        while len(self._sessions) > self._max_identities:
            oldest, _ = self._usage.popitem(last=False)
            session = self._sessions.pop(oldest, None)
            if session is not None:
                evicted.append(session)
                _LOG.warning(
                    "evidence session evicted at the %d-identity bound for "
                    "actor %r",
                    self._max_identities,
                    oldest.actor_id,
                )
        return evicted

    @staticmethod
    def _close_quietly(sessions: Sequence[MCPStdioSession]) -> None:
        for session in sessions:
            try:
                session.close()
            except Exception as exc:  # WHY broad: teardown must not mask a failure
                _LOG.warning(
                    "evidence session close failed: %s", type(exc).__name__
                )

    def _launch(self, binding: EvidenceIdentityBinding) -> MCPStdioSession:
        return self._session_factory(
            list(self._command),
            timeout_seconds=self._timeout,
            # The binding is written last by ``identity_env``, so storage
            # configuration cannot displace the identity the host just minted.
            env=identity_env(binding, transport_env=self._transport_env),
        )


@dataclass(frozen=True)
class EvidenceSessionBinding:
    """The registry and the resolver that selects an identity, as ONE parameter.

    The two are useless apart: a registry with no resolver can never bind
    anything, and a resolver with no registry has nowhere to put the identity it
    resolved. Threading them separately let a composition root pass the first
    and forget the second, which is a silently unbound retrieve rather than a
    wiring error. One parameter makes the half-wired state unrepresentable.
    """

    sessions: EvidenceSessionRegistry
    resolve: Callable[[Mapping[str, Any] | None], EvidenceIdentityBinding | None]


__all__ = [
    "BoundEvidenceSession",
    "EvidenceBindingRefusal",
    "EvidenceSessionBinding",
    "EvidenceSessionRegistry",
]
