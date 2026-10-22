"""The host composition layer for the private Evidence capability.

This module is the missing resolver that T40 named: it turns a host's trusted
identity into the environment an iknow child is launched with, and it is the
only caller of the private ``corpus_evidence`` JSON-RPC method.

Three decisions live here and nowhere else.

**The binding is explicit, and it is complete or it is nothing.** The child is
launched with an allowlisted environment, so every identity variable has to be
named by this module.  A partial binding refuses at construction time with a
named error; it is never completed from the parent process, and there is no
default scope to fall back to.  An unbound host therefore gets a child that
cannot answer an Evidence call at all, which is the correct failure: a refusal
at the transport is safer than an inherited identity.

**Identity never comes off the message channel.**  The MCP ``clientInfo``
payload is whatever the peer asserted, so it is read here for nothing and used
for nothing.  There is no parameter on this module's public surface that accepts
an actor, a scope or an authorization revision from a caller that could itself
be a model: the only source is :class:`EvidenceIdentityBinding`, constructed by
the host.

**The same session carries both planes.**  ``retrieve`` and the Evidence read
run on one persistent :class:`~fin.retrieve.mcp_session.MCPStdioSession`, so the
Evidence read is the same process, the same launch environment and the same
authorized-scope identity as the search that produced the reference.  Reading
evidence through a second connection with a second identity would make the
"same caller's own reference" property untestable.

Nothing in this module widens a closed code set.  A refusal is surfaced with the
code iknow sent, in a typed :class:`EvidenceGrantRefusal`, so a caller can
branch on ``access_denied`` without this module deciding what that means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioSession,
    MCPStdioTransportError,
)


#: The single private JSON-RPC method that carries the Evidence capability.
#:
#: It is a JSON-RPC *method*, not a tool, which is why it never appears in
#: ``tools/list`` and why a model cannot name it.  The capability sits on the
#: method axis, outside the tool registry.
EVIDENCE_PRIVATE_METHOD = "corpus_evidence"

#: The request and response tokens the Evidence spec fixes, and the envelope
#: version.  This module is the authority for the token because it is the only
#: module BOTH readers can import: ``fin.host`` reaches this one, but this one
#: must not import ``fin.host`` — ``fin.host.__init__`` loads
#: ``evidence_session_registry``, which imports back here, so a ``fin.host``
#: import above the class definitions is a circular import, not a style
#: preference.  The invalidation drain therefore names this token rather than
#: restating it.
EVIDENCE_REQUEST_SCHEMA = "iknow.evidence_request"
EVIDENCE_RESULT_SCHEMA = "iknow.evidence_result"
EVIDENCE_ENVELOPE_VERSION = 1

#: The binding contract version this client speaks.
TRUSTED_BINDING_VERSION = "1"

# The members a `read` success envelope must carry, mapped to `None`: required,
# type unchecked, and exactly as `af60514` left them. The request asked for
# a page, and a type table for this arm could not be truthful in the first
# place, because the grant's own `value` column is `unknown` on the wire and
# is carried back uninspected. Giving `read` a new refusal is a read
# ticket's own decision.
_READ_RESULT_FIELDS: Mapping[str, type | tuple[type, ...] | None] = {
    "evidence_ref": None,
    "kind": None,
    "context": None,
    "value": None,
    "expires_at": None,
}

# The members a `supports` success envelope must carry, with the JSON type each
# one is required to have. This is the wire shape iknow's support-continuation
# decision produces: the grant's own two members, the page itself, and the two
# that say whether more of the sequence exists. `next_cursor` is required even
# when it is `null`: a caller cannot tell the last page from a dropped cursor
# unless the member is present, so an absent one is a contract violation rather
# than a defaulted end.
_SUPPORT_RESULT_FIELDS: Mapping[str, type | tuple[type, ...] | None] = {
    "evidence_ref": str,
    "expires_at": str,
    "total_supports": int,
    "supports": list,
    "next_cursor": (str, type(None)),
    "selected": bool,
}

#: The closed allowlist of environment variables that carry an identity.
#:
#: This is the same closed set iknow reads.  A variable outside it is not part
#: of the contract: the child will neither look at it nor be authorized by it.
#:
#: ``consumer_id`` is the eighth member and it is not one of the seven identity
#: strings above, which is why it is separate rather than folded into one of
#: them.  Invalidations are durable **per-consumer** outbox events and the
#: backend records the trusted fin consumer against each grant
#: (``specs/structured-corpus-evidence.md:252`` and ``:254``), and a checkpoint
#: was minted for one consumer is refused by another (``:257-259``).  That last
#: refusal is only reachable with N >= 2, so the consumer has to travel as an
#: identity of its own: ``actor_id`` is a principal and ``owner_scope_binding``
#: is a scope, and a consumer riding either would put both streams in one slot.
#:
#: WHY this is stated as a property of the TRANSPORT and not of a running
#: consumer pair: nothing in this repo yet connects the consumer a binding
#: forwards to the one the drain reads
#: (``fin.host.evidence.structured_evidence_drain_worker.INVALIDATION_DRAIN_CONSUMER_ID``),
#: and the drain's own transport has no production binder — the backend half of
#: the invalidations channel lands with P3.  So the two-consumer separation is
#: available on the wire and is not yet in force end to end; claiming it is
#: already enforced would describe a property this code does not have.
TRUSTED_BINDING_ENV_VARS: Mapping[str, str] = {
    "version": "IKNOW_EVIDENCE_BINDING",
    "actor_id": "IKNOW_EVIDENCE_ACTOR_ID",
    "owner_scope_binding": "IKNOW_EVIDENCE_OWNER_SCOPE",
    "reader_binding": "IKNOW_EVIDENCE_READER",
    "source_refs": "IKNOW_EVIDENCE_SOURCE_REFS",
    "authorization_revision": "IKNOW_EVIDENCE_AUTH_REVISION",
    "deadline_at": "IKNOW_EVIDENCE_DEADLINE_AT",
    "consumer_id": "IKNOW_EVIDENCE_CONSUMER_ID",
}

#: The bound on the ``consumer_id`` token.  Same bound and same four rules as
#: the ones the invalidation drain applies to the value it stores: an opaque
#: identity label that is short, not a free-text name.  This module owns the
#: rule rather than each reader spelling it out, because a length accepted on
#: the transport and refused by the drain is a binding that launches a child
#: and then cannot drain — and the drain imports this copy, so the two cannot
#: drift.  It lives here rather than in ``fin.host.evidence.evidence_contract`` because
#: ``fin.host`` cannot be imported from here at all (see the token note above);
#: the drain reaches it the other way round.
MAX_BINDING_TOKEN_CHARS: int = 512


def _is_bounded_token(value: Any) -> bool:
    """Whether ``value`` is an opaque identity label rather than free text.

    Four rules: a string, non-empty, no longer than
    :data:`MAX_BINDING_TOKEN_CHARS`, and carrying no surrounding whitespace.

    WHY the ends only and not the interior: an interior newline is accepted
    deliberately, because this rule is the one the invalidation drain applies
    to the same label and refining it on one side alone would make the two
    disagree about a string both of them will see.
    """

    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_BINDING_TOKEN_CHARS
        and value.strip() == value
    )


class EvidenceIdentityError(ValueError):
    """The trusted identity binding is absent, partial, or malformed.

    Raised at construction time, before any process is launched, so an unbound
    host fails by name instead of starting a child that would silently answer
    for some other scope.
    """


class EvidenceGrantRefusal(RuntimeError):
    """iknow refused an Evidence read.

    ``code`` and ``reason`` are the two halves it returned.  ``code`` names the
    closed vocabulary the fact belongs to -- the evidence failure set for a
    store or contract fact, the structured-scope failure set for a transport or
    identity fact -- and ``reason`` says which fact it was, so an expiry and a
    purge stay distinguishable to a caller that only sees the shared transport
    code.
    """

    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(f"{code}/{reason}: {message}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class EvidenceIdentityBinding:
    """One trusted identity, as the host asserts it.

    This is plain data on the Python side too: it carries no authority, and the
    only thing that can be done with it is to name it in a child environment
    where iknow's authorized-scope seam decides whether it is honest.
    """

    actor_id: str
    owner_scope_binding: str
    reader_binding: str
    source_refs: tuple[str, ...]
    authorization_revision: str
    deadline_at: str
    #: Which invalidation stream this host drains.  Deliberately absent from the
    #: completeness list above: every field in that list is one of the seven
    #: identity strings the private read has always required, and making this one
    #: required would change the constructor contract of every existing caller and
    #: every live lane, which is a wider decision than registering a transport
    #: variable.  So absence is permitted here and is visible on the wire as an
    #: absent variable rather than a blank one; the refusal for a host that never
    #: configured a consumer belongs to the drain that must record it against a
    #: grant, not to the composition layer that only forwards what it was given.
    consumer_id: str = ""

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("actor_id", self.actor_id),
                ("owner_scope_binding", self.owner_scope_binding),
                ("reader_binding", self.reader_binding),
                ("authorization_revision", self.authorization_revision),
                ("deadline_at", self.deadline_at),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise EvidenceIdentityError(
                "the trusted identity binding is incomplete: "
                f"{', '.join(sorted(missing))} absent or blank. Every field is "
                "injected explicitly; an absent field is not defaulted and no "
                "unbound read is attempted."
            )
        if not self.source_refs:
            raise EvidenceIdentityError(
                "the trusted identity binding names no source reference. An "
                "identity with an empty authorized set is refused, not widened "
                "to every source."
            )
        if self.consumer_id != "" and not _is_bounded_token(self.consumer_id):
            # Only a *supplied* consumer id is judged. An absent one is the
            # deliberate case above; a supplied one that is not a token is a
            # malformed binding and is refused here, before any process starts.
            raise EvidenceIdentityError(
                "consumer_id must be a non-empty bounded token of at most "
                f"{MAX_BINDING_TOKEN_CHARS} characters with no surrounding "
                "whitespace"
            )
        if not isinstance(self.deadline_at, str) or not self.deadline_at.strip():
            raise EvidenceIdentityError("deadline_at must be an ISO instant")
        try:
            datetime.fromisoformat(self.deadline_at)
        except ValueError as exc:
            raise EvidenceIdentityError(
                f"deadline_at must be an ISO instant, got {self.deadline_at!r}"
            ) from exc

    def child_env(self) -> dict[str, str]:
        """Render the binding into the child environment's identity variables.

        The allowlisted mapping carries exactly the eight contract variables and
        nothing else, so the child sees an identity or it sees none.  The
        consumer is the one member that travels only when it was supplied: an
        absent consumer is an absent variable, never a present-and-blank one,
        because a child cannot tell a host that named no consumer apart from one
        that named an empty one.
        """

        env = {
            TRUSTED_BINDING_ENV_VARS["version"]: TRUSTED_BINDING_VERSION,
            TRUSTED_BINDING_ENV_VARS["actor_id"]: self.actor_id,
            TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]: (
                self.owner_scope_binding
            ),
            TRUSTED_BINDING_ENV_VARS["reader_binding"]: self.reader_binding,
            TRUSTED_BINDING_ENV_VARS["source_refs"]: ",".join(
                self.source_refs
            ),
            TRUSTED_BINDING_ENV_VARS["authorization_revision"]: (
                self.authorization_revision
            ),
            TRUSTED_BINDING_ENV_VARS["deadline_at"]: self.deadline_at,
        }
        if self.consumer_id:
            env[TRUSTED_BINDING_ENV_VARS["consumer_id"]] = self.consumer_id
        return env


def identity_env(
    binding: EvidenceIdentityBinding,
    *,
    transport_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete child environment for an identity-bound iknow session.

    ``transport_env`` carries the *storage* configuration the child needs to
    boot, which is a host concern and not an identity one.  It is merged first so
    an identity variable in it cannot displace the binding: the binding is
    written last and wins, which makes "the host cannot accidentally override
    the identity it just minted" a property rather than a convention.
    """

    env: dict[str, str] = dict(transport_env or {})
    env.update(binding.child_env())
    return env


def deadline_within(seconds: float, *, now: datetime | None = None) -> str:
    """A binding deadline ``seconds`` from now, as iknow parses it."""

    if seconds <= 0:
        raise EvidenceIdentityError(
            f"binding deadline window must be positive, got {seconds!r}"
        )
    instant = (now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)
    return instant.isoformat().replace("+00:00", "Z")


class EvidenceRouteClient:
    """The private ``corpus_evidence`` dispatch on one persistent session.

    The client is not a session owner.  It is handed a session that was launched
    with :func:`identity_env`, and it borrows it for the Evidence leg, so the
    read rides the same process and the same identity as the ``retrieve`` call
    that produced the reference.
    """

    def __init__(
        self,
        session: MCPStdioSession,
        *,
        binding: EvidenceIdentityBinding,
        timeout_seconds: float | None = None,
    ) -> None:
        self._session = session
        self._binding = binding
        self._timeout = timeout_seconds

    @property
    def binding(self) -> EvidenceIdentityBinding:
        return self._binding

    def read(self, evidence_ref: str) -> dict[str, Any]:
        """Return the exact stored row or count for ``evidence_ref``.

        The result is the retained grant's own ``value`` column, handed back
        unchanged.  No query is executed to produce it, so nothing that changed
        in the current corpus can appear in it.
        """

        return _read_envelope(
            self._dispatch("read", evidence_ref), evidence_ref, _READ_RESULT_FIELDS
        )

    def supports(
        self,
        evidence_ref: str,
        *,
        cursor: str | None = None,
        support_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of a row grant's frozen supports.

        ``cursor`` continues a sequence from a previous page and ``support_id``
        names one member of this grant.  iknow refuses a request that names both
        and refuses a selector that is not one of them, so each is carried
        verbatim and neither is second-guessed here: this module does not decode
        a cursor or resolve a membership it cannot see.

        A selector the caller did not name is absent from the request rather than
        sent as ``null``, because the private route accepts an exact key set per
        action.  A count grant retains no membership, so it refuses on every
        selector including none.
        """

        return _read_envelope(
            self._dispatch(
                "supports", evidence_ref, cursor=cursor, support_id=support_id
            ),
            evidence_ref,
            _SUPPORT_RESULT_FIELDS,
        )

    def _dispatch(
        self,
        action: str,
        evidence_ref: str,
        *,
        cursor: str | None = None,
        support_id: str | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "schema": EVIDENCE_REQUEST_SCHEMA,
            "schema_version": EVIDENCE_ENVELOPE_VERSION,
            "action": action,
            "evidence_ref": evidence_ref,
        }
        # Only a selector the caller actually named travels. The private route's
        # key check is per action and strict, so an unnamed selector is a missing
        # key and never a null one.
        if cursor is not None:
            params["cursor"] = cursor
        if support_id is not None:
            params["support_id"] = support_id
        payload = {
            "jsonrpc": "2.0",
            "id": _next_request_id(self._session),
            "method": EVIDENCE_PRIVATE_METHOD,
            "params": params,
        }
        try:
            return self._session.call(payload, self._timeout)
        except (MCPStdioTransportError, MCPStdioProtocolError) as exc:
            raise EvidenceGrantRefusal(
                "backend_unavailable",
                "private_route_unreachable",
                f"the private Evidence route did not answer: {exc}",
            ) from exc


def _next_request_id(session: MCPStdioSession) -> int:
    """A request id no earlier call on this session used."""

    return 1000 + len(session.requests)


def _read_envelope(
    response: Mapping[str, Any],
    evidence_ref: str,
    required: Mapping[str, type | tuple[type, ...] | None],
) -> dict[str, Any]:
    """Turn one JSON-RPC response into a result or a typed refusal.

    A JSON-RPC ``error`` member is itself a refusal: iknow answers an
    unreachable capability with ``-32601``, and that must not be read as an
    empty success.  A success envelope carrying an ``error`` member is refused
    too, and a refusal is raised before any field of the body is inspected, so
    no partially populated success can reach a caller.

    ``required`` is the member set of the action's success arm, so a ``read``
    envelope is never parsed as a page and a page is never parsed as a read; a
    member mapped to ``None`` is required but not type-checked.  Every refusal
    this path can emit reaches a caller as an :class:`EvidenceGrantRefusal`
    carrying the code and reason iknow chose; this module adds no member to
    either vocabulary.
    """

    rpc_error = response.get("error")
    if isinstance(rpc_error, Mapping):
        raise EvidenceGrantRefusal(
            "backend_unavailable",
            "private_route_unreachable",
            f"JSON-RPC error {dict(rpc_error)!r}",
        )
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise EvidenceGrantRefusal(
            "malformed_evidence_contract",
            "result_not_an_object",
            f"result is {type(result).__name__}, expected an object",
        )
    if result.get("schema") != EVIDENCE_RESULT_SCHEMA or (
        result.get("schema_version") != EVIDENCE_ENVELOPE_VERSION
    ):
        raise EvidenceGrantRefusal(
            "malformed_evidence_contract",
            "unknown_envelope",
            f"result is not a v{EVIDENCE_ENVELOPE_VERSION} "
            f"{EVIDENCE_RESULT_SCHEMA} envelope",
        )
    error = result.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        reason = error.get("reason")
        raise EvidenceGrantRefusal(
            code if isinstance(code, str) else "backend_unavailable",
            reason if isinstance(reason, str) else "backend_unavailable",
            f"for {evidence_ref}: {json.dumps(dict(error), sort_keys=True)}",
        )
    for field, expected in required.items():
        if field not in result:
            raise EvidenceGrantRefusal(
                "malformed_evidence_contract",
                "incomplete_result",
                f"a successful {EVIDENCE_RESULT_SCHEMA} envelope has no "
                f"{field!r}",
            )
        if expected is None:
            continue
        value = result[field]
        # `bool` is an `int` here, so a total that arrived as `true` is a
        # malformed envelope rather than the integer it pretends to be.
        if not isinstance(value, expected) or (
            expected is int and isinstance(value, bool)
        ):
            raise EvidenceGrantRefusal(
                "malformed_evidence_contract",
                "malformed_result_field",
                f"the {field!r} of a successful {EVIDENCE_RESULT_SCHEMA} "
                f"envelope is {type(value).__name__}",
            )
    # The envelope is the only thing linking the answer to the request, and both
    # actions need the same claim, so it is made once here rather than in each
    # arm: a page or a row that arrived for another reference is never returned.
    if result.get("evidence_ref") != evidence_ref:
        raise EvidenceGrantRefusal(
            "malformed_evidence_contract",
            "evidence_ref_mismatch",
            f"iknow answered for {result.get('evidence_ref')!r}, not "
            f"{evidence_ref!r}",
        )
    return dict(result)