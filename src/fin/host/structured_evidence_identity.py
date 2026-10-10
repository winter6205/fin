"""Operator configuration for the structured-evidence identity binding.

:mod:`fin.retrieve.evidence_route` defines the identity strings a private
Evidence read needs, and :func:`fin.host.compile.compile_for_runtime`
already threads an
:class:`~fin.host.evidence_session_registry.EvidenceSessionBinding`
into the graph. What had no production source was the trusted *input*:
this module is where an operator names those strings, and
:func:`load_structured_evidence_identity` hands one boot a single frozen
:class:`~fin.retrieve.evidence_route.EvidenceIdentityBinding`.

**Unconfigured is the default and is silent.**  A host with none of these
variables set reads ``None``, and ``fin.host.http.serve()`` then compiles
exactly the graph it compiled before this module existed — the Evidence
lane simply stays out of the retrieve path.  Nothing raises and nothing
is logged, because a laptop that never asked for the Evidence lane must
not acquire a boot warning for it.

**Configured is all-or-nothing, and every refusal names its variable.**  A
variable that is absent OR blank counts as absent: the loader says which
ones are missing and serves unconfigured, which is the only safe reading
of a half-filled identity — answering under half an identity is the
process-level default scope ADR B1 forbids.  A variable that is present,
non-blank and unusable (a ``source_refs`` list with no member, a
``deadline_at`` that is not an ISO instant, one with no timezone, or one
already in the past) raises :class:`StructuredEvidenceIdentityConfigError`
instead: an operator who configured this and got it wrong is told at
boot, before any child process exists.

**``deadline_at`` is a fixed instant the operator sets, never one minted
per read.**  Two rules already in the tree decide it.  A read does not
renew a deadline — :mod:`fin.host.structured_evidence_replay` refuses a
passed one with ``EVIDENCE_EXPIRED`` — and the registry keys its sessions
by the whole frozen binding, so a re-minted deadline is a new key and
therefore a new child process per call.  So the instant is validated once
here, and :meth:`StructuredEvidenceIdentity.resolver` hands it out only
while it is still in the future: a host that outlives its own deadline
answers a read with ``None``, which the registry refuses by name
(:class:`~fin.host.evidence_session_registry.EvidenceBindingRefusal`),
rather than serving a reader binding whose deadline has passed.

**``consumer_id`` stays absent unless configured.**  It is deliberately
optional on the binding, and it is NOT unified with
``fin.host.structured_evidence_drain_worker.INVALIDATION_DRAIN_CONSUMER_ID``:
that is the id the drain records against a grant and this is the id a read
forwards, and nothing in this repo yet connects the two streams, so
defaulting one from the other would assert an equivalence that does not
hold.

**The child's storage configuration is named, not inherited.**  An
identity-bound session runs under a closed allowlist, so the storage
variables it boots against must be forwarded explicitly; the operator
lists their names in ``FIN_EVIDENCE_TRANSPORT_FORWARD`` and this module
refuses a name that is unset, an empty list, or an identity variable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Final, Mapping

from fin.retrieve.evidence_route import (
    TRUSTED_BINDING_ENV_VARS,
    EvidenceIdentityBinding,
)

logger = logging.getLogger(__name__)

FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_ACTOR_ID"
FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_OWNER_SCOPE"
FIN_EVIDENCE_IDENTITY_READER_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_READER"
FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_SOURCE_REFS"
FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV: Final[str] = (
    "FIN_EVIDENCE_IDENTITY_AUTH_REVISION"
)
FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_DEADLINE_AT"
FIN_EVIDENCE_IDENTITY_CONSUMER_ID_ENV: Final[str] = "FIN_EVIDENCE_IDENTITY_CONSUMER_ID"
FIN_EVIDENCE_TRANSPORT_FORWARD_ENV: Final[str] = "FIN_EVIDENCE_TRANSPORT_FORWARD"

#: The identity fields a private read has always required, each paired with the
#: operator variable that supplies it.  ``consumer_id`` is deliberately not a
#: member: it is the one field the binding permits to be absent, so requiring it
#: here would be a stricter contract than the one it validates.
_REQUIRED_FIELD_ENV: Final[tuple[tuple[str, str], ...]] = (
    ("actor_id", FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV),
    ("owner_scope_binding", FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV),
    ("reader_binding", FIN_EVIDENCE_IDENTITY_READER_ENV),
    ("source_refs", FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV),
    ("authorization_revision", FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV),
    ("deadline_at", FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV),
)


class StructuredEvidenceIdentityConfigError(ValueError):
    """The operator configured an Evidence identity this host cannot use.

    Raised at boot, before a registry is built and therefore before any child
    process exists, so a mistyped scope or a stale deadline fails by name
    instead of surfacing later as a read that answers for nobody.  The
    sibling class :class:`~fin.retrieve.evidence_route.EvidenceIdentityError`
    owns the binding's own validation; this one owns the translation from an
    operator's environment to that binding, which is why the message names an
    environment variable rather than a field.
    """


@dataclass(frozen=True, slots=True)
class StructuredEvidenceIdentity:
    """One operator-configured identity and the child variables it travels with.

    ``transport_env`` holds ``(variable, value)`` pairs rather than bare names
    so the resolved values travel with the configuration instead of being
    re-read at wiring time from a mapping that may no longer be the one this
    loader was handed.
    """

    actor_id: str
    owner_scope_binding: str
    reader_binding: str
    source_refs: tuple[str, ...]
    authorization_revision: str
    deadline_at: str
    #: Absent unless configured — see the module docstring.  Never defaulted to
    #: the invalidation drain's own consumer id.
    consumer_id: str = ""
    transport_env: tuple[tuple[str, str], ...] = ()

    def binding(self) -> EvidenceIdentityBinding:
        """The frozen binding this configuration asserts.

        Construction is the last word: ``EvidenceIdentityBinding`` re-checks
        completeness, the non-empty authorized set, the consumer token and the
        ISO instant, so a dataclass built directly (a test, a future caller)
        cannot present an identity this module would have refused.
        """

        return EvidenceIdentityBinding(
            actor_id=self.actor_id,
            owner_scope_binding=self.owner_scope_binding,
            reader_binding=self.reader_binding,
            source_refs=self.source_refs,
            authorization_revision=self.authorization_revision,
            deadline_at=self.deadline_at,
            consumer_id=self.consumer_id,
        )

    def child_storage_env(self) -> dict[str, str]:
        """The storage configuration an identity-bound child boots against."""

        return dict(self.transport_env)

    def resolver(
        self,
    ) -> Callable[[Mapping[str, Any] | None], EvidenceIdentityBinding | None]:
        """The per-read selector ``EvidenceSessionBinding.resolve`` expects.

        The run's own ``configurable`` is not consulted: this host serves ONE
        identity, so the only question a read asks is whether that identity is
        still inside its own deadline.  ``None`` is the answer once it is not,
        and the registry refuses ``None`` by name rather than answering under
        some other caller's identity.
        """

        def resolve(
            config: Mapping[str, Any] | None,
        ) -> EvidenceIdentityBinding | None:
            if _instant(self.deadline_at) <= datetime.now(timezone.utc):
                return None
            return self.binding()

        return resolve


def _trimmed(environ: Mapping[str, str], name: str) -> str | None:
    """The operator's value, or ``None`` when it is unset or blank.

    Absent and blank are the same answer on purpose: a variable the operator
    opened and left empty has configured nothing, so it is reported as missing
    by name instead of being read as an empty identity string.
    """

    raw = environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return value


def _members(raw: str) -> tuple[str, ...]:
    """The comma-separated members of an operator value, blanks dropped."""

    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _instant(value: str) -> datetime:
    """``value`` as an aware instant, or a typed refusal naming its variable."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV} must be an ISO instant "
            f"(iknow's whole-second 'Z' form is accepted), got {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV} must carry a timezone, so "
            f"a deadline is never read in the host's local time, got {value!r}"
        )
    return parsed


def _transport_env(
    environ: Mapping[str, str], raw: str | None
) -> tuple[tuple[str, str], ...]:
    """The operator-named child variables, resolved to their values."""

    if raw is None:
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV} must list the variables an "
            "identity-bound child needs to boot, comma-separated: the session "
            "registry refuses an empty transport_env, and the child runs under "
            "a closed allowlist that would otherwise hold identity variables only"
        )
    names = _members(raw)
    if not names:
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV} must name at least one child "
            f"variable, got {raw!r}"
        )
    carried: list[tuple[str, str]] = []
    for name in names:
        if name in TRUSTED_BINDING_ENV_VARS.values():
            raise StructuredEvidenceIdentityConfigError(
                f"{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV} names {name!r}, which is "
                "an identity variable: the binding is written into the child "
                "after the transport, so it would be overridden — and a consumer "
                "the host never configured would travel this way"
            )
        value = _trimmed(environ, name)
        if value is None:
            raise StructuredEvidenceIdentityConfigError(
                f"{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV} names {name!r}, which is "
                "not set in this process environment, so the child would boot "
                "without it"
            )
        carried.append((name, value))
    return tuple(carried)


def load_structured_evidence_identity(
    *, environ: Mapping[str, str] | None = None
) -> StructuredEvidenceIdentity | None:
    """Return the operator's Evidence identity, or ``None`` when unconfigured.

    ``None`` means "this host serves without an Evidence identity" and is the
    answer for two different situations, kept apart on purpose.  NOTHING set is
    silence: no identity was asked for, so there is nothing to report.  Something
    set but incomplete is a WARNING naming the missing variables, because the
    operator did ask and half an identity must not be used in place of the whole.
    A value that is present and unusable raises instead.
    """

    import os

    source: Mapping[str, str] = os.environ if environ is None else environ
    values = {field: _trimmed(source, name) for field, name in _REQUIRED_FIELD_ENV}
    consumer_id = _trimmed(source, FIN_EVIDENCE_IDENTITY_CONSUMER_ID_ENV)
    forward = _trimmed(source, FIN_EVIDENCE_TRANSPORT_FORWARD_ENV)

    if not any(values.values()) and consumer_id is None and forward is None:
        return None

    missing = sorted(name for field, name in _REQUIRED_FIELD_ENV if not values[field])
    if missing:
        logger.warning(
            "the structured-evidence identity is NOT configured and this boot "
            "serves without one: %s absent or blank. All of %s are required to "
            "put the Evidence lane in the retrieve path; a partial identity is "
            "never used, because answering under half an identity is the "
            "process-level default scope ADR B1 forbids.",
            ", ".join(missing),
            ", ".join(name for _, name in _REQUIRED_FIELD_ENV),
        )
        return None

    source_refs = _members(values["source_refs"] or "")
    if not source_refs:
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV} names no source reference "
            f"({values['source_refs']!r}): an identity with an empty authorized "
            "set is refused, not widened to every source"
        )
    deadline_at = values["deadline_at"] or ""
    if _instant(deadline_at) <= datetime.now(timezone.utc):
        raise StructuredEvidenceIdentityConfigError(
            f"{FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV}={deadline_at!r} has already "
            "passed: an expired reader binding is refused at boot rather than "
            "served, because a read does not renew a deadline. Configure a future "
            "instant and restart the host"
        )
    return StructuredEvidenceIdentity(
        actor_id=values["actor_id"] or "",
        owner_scope_binding=values["owner_scope_binding"] or "",
        reader_binding=values["reader_binding"] or "",
        source_refs=source_refs,
        authorization_revision=values["authorization_revision"] or "",
        deadline_at=deadline_at,
        consumer_id=consumer_id or "",
        transport_env=_transport_env(source, forward),
    )


__all__ = [
    "FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV",
    "FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV",
    "FIN_EVIDENCE_IDENTITY_CONSUMER_ID_ENV",
    "FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV",
    "FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV",
    "FIN_EVIDENCE_IDENTITY_READER_ENV",
    "FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV",
    "FIN_EVIDENCE_TRANSPORT_FORWARD_ENV",
    "StructuredEvidenceIdentity",
    "StructuredEvidenceIdentityConfigError",
    "load_structured_evidence_identity",
]