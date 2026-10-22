"""Resolve user source intent against trusted authority into one effective scope.

A source restriction is USER INTENT, not an authorization grant. This module
keeps those two apart. :class:`ScopeRestriction` records only how the operator
wanted to narrow the attachment branch, and it survives runs and follow-ups;
the current authority it must be re-checked against is host-supplied through
:class:`AuthorityFacts`. :func:`resolve_scope` combines the two into the
:class:`EffectiveScope` that the host enforces at every source and evidence
boundary with :class:`ScopeGate`. Nothing here reads a store, persists a
change, or classifies language: the model interprets intent, Runtime accepts
and persists changes, and this module deterministically resolves and enforces
what the current trusted facts actually support.

Three facts stay separate (spec question-source-scope decision 1). An
attachment is ELIGIBLE when current authority stands behind it inside the
restriction, READY when its preparation lifecycle has produced usable
products, and PERMITTED when current authority allows the operation at all.
The effective scope is the ELIGIBILITY set and never filters on readiness;
readiness is read live at each boundary instead. Readiness is not a permission
and never grants access: a not-ready-but-authorized hit is IN scope and earns
``unavailable`` (it says the search cannot answer yet), never ``permitted``,
which is why the gate answers :data:`SCOPE_ATTACHMENT_NOT_READY` rather than a
denial.

Inheriting a restriction reuses the INTENT, never the permission that happened
to apply last time. An inherited restriction resolves by exactly the same rules
as an explicit one, and :data:`ScopeOrigin` is recorded on the result basis but
never consulted to widen. A saved restriction that current authority no longer
supports resolves to fewer attachments, never to a broader selection.
:func:`propose_scope_change` returns a :class:`ScopeProposal` for exactly the
requested change — a REQUEST that only Runtime may accept and persist, and that
grants nothing on its own.

Two company-branch facts stay apart. A restriction that NAMES attachments
narrows the ATTACHMENT branch only: ADR 0088:10 keeps company sources eligible
so a requested comparison can mix branches. A restriction the operator declared
EXCLUSIVE (``company_excluded``) is a durable statement about the whole approved
selection, and spec question-source-scope decision 3 / SC-2 (a restriction is
never silently broadened and no operation falls back to a broader selection)
requires the company branch to fall outside it.
:attr:`EffectiveScope.company_allowed` is therefore the resolved fact, and every
boundary reads it instead of assuming the branch is open.

Operation capability is a derived fact, never a default: a broad or company
check consults :data:`SCOPE_EXPRESSIBLE_OPERATIONS`, the closed set of
operations a wired adapter is known to be able to express. An operation outside
that set is ``unsupported`` rather than silently allowed, so adding a source
operation without proving its adapter expresses the narrowing fails closed.

The module is pure and terminal: it is built from frozen value objects and
injected callables, :func:`resolve_scope` raises no exception, and it owns no
store.

:meth:`ScopeGate.report` is the READ-ONLY exposure Workspace reads: the same
resolved :class:`EffectiveScope`, the operations the gate would permit right
now, and every KNOWN LIMIT the operator could otherwise be misled about. It
reuses :meth:`ScopeGate.check` for the capabilities so the report can never
claim more access than enforcement grants, and an empty ``known_limits`` means
no limit applies.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, Final, Literal, Protocol, runtime_checkable

from fin.host.task.task_runtime_types import (
    TASK_APPLICABILITY_UNRESOLVED,
    TASK_DEPENDENCY_FAILURE,
    TASK_INPUT_INVALID,
    TASK_PERMISSION_DENIED,
    TaskRuntimeError,
)
from fin.retrieve.source_read import ATTACHMENT_KIND

# The two reasons an operator's restriction is in force. ``explicit`` records a
# narrowing the operator asked for now; ``inherited`` records the same intent
# carried forward. The distinction is a record of where the INTENT came from —
# it is never a permission and never changes how the restriction resolves.
ScopeOrigin = Literal["explicit", "inherited"]

# The closed vocabulary of a scope decision. ``permitted`` and ``empty`` are not
# refusals: the first is an allowed operation, the second is the truthful
# "nothing is selected to search". Every refusal is one of the four distinct
# failures the spec keeps apart (denied, unsupported, unavailable,
# dependency_failed).
ScopeOutcome = Literal[
    "permitted",
    "denied",
    "unsupported",
    "unavailable",
    "dependency_failed",
    "empty",
]

# The model-facing typed exits. One code per distinct cause, mirroring how
# :mod:`fin.host.evidence.visual_evidence` closes ``VISUAL_REFUSAL_CODES``: the model
# sees the code, and no code is ever reused for two causes. ``readiness`` is a
# distinct code from every denial because it says "cannot answer yet" rather
# than "not allowed".
SCOPE_THREAD_UNBOUND: Final[str] = "scope_thread_unbound"
SCOPE_SOURCE_REF_UNREADABLE: Final[str] = "scope_source_ref_unreadable"
SCOPE_ATTACHMENT_NOT_PERMITTED: Final[str] = "scope_attachment_not_permitted"
SCOPE_ATTACHMENT_UNKNOWN: Final[str] = "scope_attachment_unknown"
SCOPE_ATTACHMENT_VERSION_STALE: Final[str] = "scope_attachment_version_stale"
SCOPE_ATTACHMENT_NOT_READY: Final[str] = "scope_attachment_not_ready"
SCOPE_COMPANY_NOT_PERMITTED: Final[str] = "scope_company_not_permitted"
SCOPE_OPERATION_UNSUPPORTED: Final[str] = "scope_operation_unsupported"
SCOPE_DEPENDENCY_FAILED: Final[str] = "scope_dependency_failed"
SCOPE_SELECTION_EMPTY: Final[str] = "scope_selection_empty"
SCOPE_PERMITTED: Final[str] = "scope_permitted"
# A stored snapshot that could not be READ as a snapshot at all — a
# malformed value, an unknown schema version, an unrecognised mode. It is a
# distinct cause from every refusal above: those all describe a source that
# was successfully checked and then refused, while this one says there was no
# valid selection to check. A reader that met one MUST refuse rather than
# treat the absence of a readable selection as the absence of a restriction,
# which is the widening this code exists to name.
SCOPE_SNAPSHOT_UNREADABLE: Final[str] = "scope_snapshot_unreadable"

# Operations that read the thread-attachment branch alone. A broad request for
# one of these (no ``source_ref``) against an effective scope with no
# attachments is the truthful empty success — nothing was refused and nothing
# failed — not an error. Company-capable operations are never in this set:
# whether they may run at all is the branch decision, not an emptiness fact.
# Membership is limited to the operations a wired boundary actually passes:
# ``search_attachments`` covers the ``retrieve`` attachment branch. No
# attachment-enumeration boundary is wired (the only catalog tool,
# ``list_files``, enumerates the shared company corpus, not this thread's
# attachments and so is not attachment-scoped), so naming one here would be
# vocabulary no caller could ever exercise.
ATTACHMENT_SCOPED_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"search_attachments"}
)

# The single capability token that stands for the company branch in a report.
# It is named with this one token rather than an operation catalog: the report
# says whether the branch is open and Workspace lists the concrete company tools
# it presents.
COMPANY_CAPABILITY: Final[str] = "company_sources"

# The operations a wired adapter is proven to express the approved restriction
# through. Membership is a discovery result, each member named for the seam that
# proves it: ``search_attachments`` takes an id narrowing
# (:meth:`fin.host.attachment.attachment_retrieval.AttachmentSearchPort.search`),
# ``search_corpus``, ``list_files``, ``redline``, ``extract_tables`` and
# ``three_year_statement`` read the corpus, the catalog or the company bodies
# and fact table the branch decision governs, ``read_source`` and
# ``read_visual_detail`` take one named locator, ``admit_evidence`` is
# host-side, ``get_xbrl_fact`` and ``compare_facts`` name one document, and
# :data:`COMPANY_CAPABILITY` stands for the branch in a report. An operation
# outside this set is answered :data:`SCOPE_OPERATION_UNSUPPORTED`: capability is
# never assumed, so a new source operation fails closed until its adapter's
# expressibility is proven here.
SCOPE_EXPRESSIBLE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "search_attachments",
        "search_corpus",
        "list_files",
        "read_source",
        "read_visual_detail",
        "admit_evidence",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
        COMPANY_CAPABILITY,
    }
)

# The operations a report enumerates when it derives feasible capabilities.
# Attachment-scoped operations come from the wired boundaries; the company
# branch contributes its single token. No operation registry is invented.
_REPORTABLE_OPERATIONS: Final[frozenset[str]] = (
    ATTACHMENT_SCOPED_OPERATIONS | {COMPANY_CAPABILITY}
)

# The closed vocabulary of codes a known limit may carry. Each is an EXISTING
# refusal code whose cause is the same as the limit it names, so the report
# reuses this module's one vocabulary instead of minting a parallel string set:
# ``scope_thread_unbound`` (no trusted thread), ``scope_attachment_unknown``
# (no current authority stands behind the id the restriction named, so the
# saved intent cannot be honored), ``scope_attachment_not_ready`` (authorized
# but the preparation lifecycle has not produced usable products yet),
# ``scope_company_not_permitted`` (the selection in force excludes the corpus),
# and ``scope_dependency_failed`` (authority could not be consulted at all). None
# of these is a permission; each is reported so access is never overstated.
KNOWN_LIMIT_CODES: Final[frozenset[str]] = frozenset(
    {
        SCOPE_THREAD_UNBOUND,
        SCOPE_ATTACHMENT_UNKNOWN,
        SCOPE_ATTACHMENT_NOT_READY,
        SCOPE_COMPANY_NOT_PERMITTED,
        SCOPE_DEPENDENCY_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class ScopeRestriction:
    """A durable user restriction on the approved selection — INTENT only.

    ``attachment_ids`` is the set of attachment ids the operator narrowed to, or
    ``None`` when they imposed no attachment narrowing. ``company_excluded``
    records that the operator declared that set to be the WHOLE approved
    selection ("use only …"), which puts the company corpus outside it; a
    restriction that merely names attachments leaves the branch eligible
    (ADR 0088:10). This holds NO permission, NO authority, and NO version: a
    saved restriction is not an authorization grant. It survives across runs and
    follow-ups and is never silently broadened. Inheriting one reuses this
    INTENT, never the permission that applied the last time it was used — the
    host re-checks current authority before every operation regardless of what
    this record still says.
    """

    attachment_ids: tuple[str, ...] | None
    origin: ScopeOrigin
    company_excluded: bool = False

    @property
    def in_force(self) -> bool:
        """Whether this restriction narrows the selection at all.

        ``None`` ids with no exclusion narrows nothing, so a boundary has no
        approved selection to enforce beyond the default one. An EMPTY tuple is
        a narrowing ("no attachment"), and any exclusion is one.
        """
        return self.attachment_ids is not None or self.company_excluded


@dataclass(frozen=True, slots=True)
class AttachmentAuthority:
    """What the trusted store currently stands behind for one attachment.

    ``version`` is the parsed-content version (the store's own integer), the
    same identity the attachment read leg validates. It is the CURRENT version,
    not the one a saved locator happens to carry.
    """

    attachment_id: str
    version: int


@dataclass(frozen=True, slots=True)
class AuthorityFacts:
    """The trusted current facts a scope is resolved and enforced against.

    Every field is host-supplied and never model-supplied. Each callable is
    REQUIRED, with no default, so a missing authority cannot be constructed by
    omission: an incomplete fact set is a construction error, not a silent
    allow-all.

    * ``thread_id`` is the host-bound active thread (the ``_bound_thread_id``
      shape) — a blank value means no thread scope and never widens access.
    * ``resolve_attachment`` answers current identity + version for an id, or
      ``None`` when no authority stands behind it (unknown, foreign-thread,
      tombstoned, re-prepared).
    * ``attachment_ready`` answers readiness for an id at a version — a SEPARATE
      fact from eligibility, consulted so a not-ready hit is ``unavailable``
      rather than a denial.
    * ``operation_capable`` answers whether the requested operation is
      expressible at all for the current authority.
    """

    thread_id: str
    resolve_attachment: Callable[[str], AttachmentAuthority | None]
    attachment_ready: Callable[[str, int], bool]
    operation_capable: Callable[[str], bool]
    list_attachments: Callable[[], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class EffectiveScope:
    """The resolved authorized selection the host enforces at each boundary.

    ``attachment_ids`` is the concrete set of attachment ids current authority
    stands behind inside the restriction — an ELIGIBILITY set, never filtered on
    readiness and never a wildcard, so a membership test is always exact.
    ``company_allowed`` is the resolved branch fact: ``False`` only when the
    restriction in force declared an exclusive selection, and ``True`` for a
    default scope and for a restriction that merely narrows attachments
    (ADR 0088:10). Every company boundary reads it rather than assuming.
    ``basis`` records why: the origin of an in-force restriction (``explicit`` /
    ``inherited``), or ``default`` for no restriction, or ``thread_unbound`` when
    there is no trusted thread.
    ``resolution_fault`` is the failure that stopped an attachment — the
    enumeration or a named id — from being consulted at all (its type name), or
    ``None`` when authority answered, including when it answered "nothing". It is
    what keeps a dependency failure distinct from a genuine empty selection: a
    boundary reads it and answers :data:`SCOPE_DEPENDENCY_FAILED` instead of a
    successful empty, so "authority could not be consulted" is never consumed
    as "authority answered, nothing matched".
    ``snapshot_refusal_code`` is the refusal EVERY boundary must answer when an
    accepted versioned selection could not be confirmed against current
    authority, or could not be read as a selection at all. It is separate from
    ``resolution_fault`` because that one describes THIS graph's inability to
    enumerate the attachment branch, while this one describes a SELECTION that
    was accepted and is no longer confirmable: the branch answers the code
    directly, with no ids and no permitted corpus path, so a dead selection is
    never released into the unrestricted posture it was meant to replace. It
    is ``None`` for every resolution that did not come from a stored snapshot,
    which is what keeps a run with no accepted selection byte-identical to the
    pre-snapshot behaviour.
    ``excluded_ids`` is the ids an in-force restriction named that current
    authority answered it does NOT stand behind (blank and duplicate names are
    not ids and never appear). They are the saved intent that could NOT be
    honored (decision 4: a saved restriction is not a grant): the report surfaces
    each as a known limit so a restriction that current permission prevents is
    never read as a current selection. It is empty for a default, no-narrowing,
    or fully-honored restriction, and empty whenever authority could not be
    consulted at all — an unanswered id was never refuted.
    """

    attachment_ids: tuple[str, ...]
    company_allowed: bool
    basis: str
    resolution_fault: str | None = None
    excluded_ids: tuple[str, ...] = ()
    snapshot_refusal_code: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeProposal:
    """A REQUESTED scope change, returned for Runtime to accept and persist.

    This module never persists it and owns no store: Runtime alone accepts and
    persists a scope change against its current input/task basis. A proposal
    alone is not authority — until Runtime accepts it, and until the current
    facts are re-checked, it grants nothing. It carries the WHOLE intent, the
    exclusion included, so an accepted change cannot quietly lose half of it.
    """

    attachment_ids: tuple[str, ...] | None
    origin: ScopeOrigin
    requested_by: str
    company_excluded: bool = False


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """One boundary's answer: the outcome and the exact cause that produced it.

    ``reason`` is a code from this module's closed vocabulary (a member of
    :attr:`ScopeGate.DENIAL_CODES` for every refusal), so the same cause is
    always named the same way and two causes are never collapsed onto one code.
    """

    outcome: ScopeOutcome
    reason: str


@dataclass(frozen=True, slots=True)
class KnownLimit:
    """One access limit the operator could otherwise be misled about.

    ``code`` is from the closed :data:`KNOWN_LIMIT_CODES` set — one cause, one
    code, never a permission. ``detail`` names the specific limit (the
    attachment id where one applies) so the report is direct rather than a bare
    code. ``remedy`` is the operator action that would remove the limit, or
    ``None`` when no operator scope action changes it (a fault clears itself, a
    not-ready attachment finishes preparing). A limit is a fact about what
    access is NOT available; it never grants any.
    """

    code: str
    detail: str
    remedy: str | None


@dataclass(frozen=True, slots=True)
class ScopeReport:
    """What Workspace reads: the effective scope, capabilities, and limits.

    ``effective`` is the SAME resolved scope the gate enforces, so presentation
    and enforcement share one truth. ``feasible_capabilities`` is exactly the
    :data:`_REPORTABLE_OPERATIONS` the gate would permit right now, derived by
    driving :meth:`ScopeGate.check` rather than re-deriving it, so the report
    can never list an operation the gate would refuse. ``known_limits`` is every
    limit that applies; an EMPTY tuple means no limit applies.
    """

    effective: EffectiveScope
    feasible_capabilities: frozenset[str]
    known_limits: tuple[KnownLimit, ...]



def _is_bound_thread(thread_id: Any) -> bool:
    """True only for a non-blank trusted thread id (the graph shape)."""
    return isinstance(thread_id, str) and thread_id.strip() != ""


def _clean_id(raw: Any) -> str | None:
    """The attachment id ``raw`` names, or ``None`` when it names none.

    An id is opaque, so a non-blank string is returned unchanged; only a
    non-string or an all-whitespace value names no attachment (never a valid id,
    and never the empty string a careless caller might pass).
    """
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    return raw


def _ref_version(raw: Any) -> int | None:
    """The parsed-content version a locator field carries, or ``None``.

    Accepts the integer identity the host builds from an ``AttachmentRecord``
    and the canonical string the company wire spells, so a locator from either
    leg compares against the store's version. A bool, a negative, a
    non-canonical string (``"+3"`` / ``"01"``), or any other type names no
    version.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 and str(value) == raw else None
    return None


def _named_order(names: Any) -> tuple[str, ...]:
    """The cleaned, de-duplicated ids a restriction names, in first-seen order.

    Blank names and duplicates name no distinct attachment, so they are dropped
    here (the shared pre-pass for both the eligibility set and the excluded
    set, so the two can never disagree about which ids were named).
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in names or ():
        attachment_id = _clean_id(raw)
        if attachment_id is None or attachment_id in seen:
            continue
        seen.add(attachment_id)
        ordered.append(attachment_id)
    return tuple(ordered)


def _supported_ids(
    facts: AuthorityFacts, names: Any
) -> tuple[tuple[str, ...], str | None]:
    """The named ids authority stands behind, in order, and any fault.

    This is the ELIGIBILITY set only: an id is dropped when it is blank, when it
    is a duplicate, or when ``resolve_attachment`` answers ``None`` — never
    silently re-added. Readiness is deliberately NOT consulted here; it is a
    separate per-operation fact the gate reads live, so a not-ready-but-
    authorized id stays in scope and is reported ``unavailable``, not denied.

    An id whose resolver RAISED is dropped from the selection but is NOT refuted,
    so the fault is returned for the caller to record: a selection narrowed by an
    unconsultable authority is a dependency failure, never the successful empty
    an answered "nothing" is.
    """
    fault: str | None = None
    supported: list[str] = []
    for attachment_id in _named_order(names):
        try:
            authority = facts.resolve_attachment(attachment_id)
        except Exception as exc:
            # EXIT: a raising read contributes no id — never a fallback allow —
            # and records the fault, so an unconsultable authority is never
            # consumed as the answered "nothing" that refutes the saved intent.
            if fault is None:
                fault = type(exc).__name__
            continue
        if authority is not None:
            supported.append(attachment_id)
    return tuple(supported), fault


def _default_ids(facts: AuthorityFacts) -> tuple[tuple[str, ...], str | None]:
    """Every attachment the current facts stand behind, and any fault.

    The enumeration comes from the host's own list of thread-owned ids; each is
    then re-resolved through the same authority-only rule, so a stale row drops
    out exactly as a named one would. Readiness is not consulted — the gate
    reads it live per operation. A fault in the enumeration is RETURNED, not
    swallowed: an empty tuple from a fault would be indistinguishable from a
    genuinely empty enumeration, which the spec keeps apart, so the caller
    records it as ``resolution_fault``.
    """
    try:
        enumerated = facts.list_attachments()
    except Exception as exc:
        # EXIT: authority could not be consulted. The selection is empty here,
        # but that is NOT the same fact as "authority answered, nothing
        # matched" — the fault is recorded so a boundary can answer a
        # dependency failure instead of a successful empty.
        return (), type(exc).__name__
    return _supported_ids(facts, enumerated)


def resolve_scope(
    restriction: ScopeRestriction | None, facts: AuthorityFacts
) -> EffectiveScope:
    """The effective authorized selection, resolved purely and without raising.

    No restriction resolves to every attachment the current facts stand behind.
    A restriction naming ids resolves to only those ids the current facts still
    stand behind. Both sets are ELIGIBILITY only: readiness is never consulted
    here, so a not-ready-but-authorized id stays in scope and the gate answers
    ``unavailable`` for it live. An inherited restriction resolves by the SAME
    rules as an explicit one; its ``origin`` records that the intent was carried
    forward and is never consulted to widen. A blank trusted thread resolves to
    no attachments at all — never to allow-all. A resolver that raises
    contributes nothing, never a fallback allow, and is recorded as the
    resolution fault rather than an empty selection. A fault in the default
    enumeration is recorded on ``resolution_fault`` so the empty it produces is
    never read as a genuine empty selection.

    The company branch keeps its eligibility under a restriction that only names
    attachments (ADR 0088:10) and loses it under one the operator declared
    exclusive — a declared exclusion applies whatever the thread looks like,
    because falling back to the corpus would broaden the approved selection.
    """
    company_allowed = restriction is None or not restriction.company_excluded
    if not _is_bound_thread(facts.thread_id):
        # EXIT: no trusted thread means no attachment branch. An attachment
        # id cannot be selected without a thread, and an exclusive restriction
        # still excludes the corpus.
        return EffectiveScope(
            attachment_ids=(),
            company_allowed=company_allowed,
            basis="thread_unbound",
        )

    if restriction is None:
        ids, fault = _default_ids(facts)
        return EffectiveScope(
            attachment_ids=ids,
            company_allowed=True,
            basis="default",
            resolution_fault=fault,
        )

    if restriction.attachment_ids is None:
        # An inherited (or explicit) "no narrowing" still re-checks current
        # facts: the intent was to scope nothing, not to grant anything.
        ids, fault = _default_ids(facts)
        return EffectiveScope(
            attachment_ids=ids,
            company_allowed=company_allowed,
            basis=restriction.origin,
            resolution_fault=fault,
        )

    supported, fault = _supported_ids(facts, restriction.attachment_ids)
    supported_set = set(supported)
    # The named ids authority ANSWERS that it does not stand behind are the saved
    # intent that could not be honored. Recorded (not dropped silently) so the
    # report can surface each as a known limit instead of reading as a current
    # selection. An id whose resolver raised was never answered, so under a fault
    # nothing is claimed refuted.
    excluded = (
        ()
        if fault is not None
        else tuple(
            named
            for named in _named_order(restriction.attachment_ids)
            if named not in supported_set
        )
    )
    return EffectiveScope(
        attachment_ids=supported,
        company_allowed=company_allowed,
        basis=restriction.origin,
        resolution_fault=fault,
        excluded_ids=excluded,
    )


def propose_scope_change(
    requested: ScopeRestriction,
    facts: AuthorityFacts,
    *,
    requested_by: str,
) -> ScopeProposal | None:
    """The scope change to hand Runtime, or ``None`` when nothing would change.

    ``requested`` is the operator's new intent — a narrowing, or none. It is
    resolved against ``facts`` by the SAME rules as :func:`resolve_scope` and
    compared with the current effective selection (what authority stands behind
    with no restriction). A request that resolves to that same selection
    changes nothing and yields ``None``; anything else yields a
    :class:`ScopeProposal` carrying the REQUESTED intent and the caller's
    ``requested_by``.

    Pure and terminal: it reads only the two values it is given, owns no store,
    persists nothing, and mutates nothing. A proposal is a REQUEST, not
    authority — until Runtime accepts it and the current facts are re-checked
    it grants nothing, so a proposal that names an id authority does not stand
    behind cannot widen access on its own.
    """
    current = resolve_scope(None, facts)
    requested_scope = resolve_scope(requested, facts)
    if (
        requested_scope.attachment_ids == current.attachment_ids
        and requested_scope.company_allowed == current.company_allowed
    ):
        return None
    return ScopeProposal(
        attachment_ids=requested.attachment_ids,
        origin=requested.origin,
        requested_by=requested_by,
        company_excluded=requested.company_excluded,
    )


class ScopeGate:
    """The enforcement object injected at every source and evidence boundary.

    Constructed with the host's current :class:`AuthorityFacts` and the
    :class:`EffectiveScope` resolved for this task. :meth:`check` decides one
    operation against one optional ``source_ref`` and NEVER returns
    ``permitted`` as a fallback for an unknown or failed check: a missing or
    unreadable authority resolves to a denial, and a fault in the authority
    itself is the distinct ``dependency_failed`` — never silently allowed.
    """

    DENIAL_CODES: Final[frozenset[str]] = frozenset(
        {
            SCOPE_THREAD_UNBOUND,
            SCOPE_SOURCE_REF_UNREADABLE,
            SCOPE_ATTACHMENT_NOT_PERMITTED,
            SCOPE_ATTACHMENT_UNKNOWN,
            SCOPE_ATTACHMENT_VERSION_STALE,
            SCOPE_ATTACHMENT_NOT_READY,
            SCOPE_COMPANY_NOT_PERMITTED,
            SCOPE_OPERATION_UNSUPPORTED,
            SCOPE_DEPENDENCY_FAILED,
            SCOPE_SNAPSHOT_UNREADABLE,
        }
    )

    def __init__(self, *, effective: EffectiveScope, facts: AuthorityFacts) -> None:
        self._effective = effective
        self._facts = facts

    def check(
        self, operation: str, *, source_ref: Mapping[str, Any] | None = None
    ) -> ScopeDecision:
        """Decide one operation against the current scope.

        An attachment-typed ref is checked against the trusted thread, the
        effective selection, current authority and version, readiness, and
        operation capability, in that order. A company-typed ref (a locator
        with no ``kind == "attachment"``) is refused when the effective scope
        excludes the company branch, and is otherwise permitted subject to
        capability: branch eligibility outranks capability so an excluded
        corpus reads as ``denied`` with a remedy rather than being re-labelled
        by a capability fault. ``None`` is a broad request: attachment-scoped
        operations answer ``empty`` when an answered authority selected nothing
        and :data:`SCOPE_DEPENDENCY_FAILED` when authority could not be
        consulted at all; company-scoped operations follow ``company_allowed``
        and capability.
        """
        if self._effective.snapshot_refusal_code is not None:
            # EXIT: an accepted selection exists but could not be confirmed
            # against current authority (or could not be read as a selection).
            # EVERY operation answers this one cause the same way, ahead of any
            # per-source ruling, because the alternative — ruling on whatever
            # each source happens to name — is exactly the widening a confirmed
            # selection exists to prevent: it would answer "permitted" for a
            # source outside a selection nobody can now honour.
            return ScopeDecision(
                "dependency_failed", self._effective.snapshot_refusal_code
            )
        if source_ref is None:
            return self._check_broad(operation)
        if not isinstance(source_ref, Mapping):
            # EXIT: an unreadable source_ref names no source, so it can never
            # be a permitted fallback.
            return ScopeDecision("denied", SCOPE_SOURCE_REF_UNREADABLE)
        if source_ref.get("kind") == ATTACHMENT_KIND:
            return self._check_attachment(operation, source_ref)
        return self._check_company(operation)

    def _capability(self, operation: str) -> bool | None:
        """``True`` / ``False``, or ``None`` when the capability check faulted."""
        try:
            return bool(self._facts.operation_capable(operation))
        except Exception:
            return None

    def _capability_decision(self, operation: str) -> ScopeDecision | None:
        """The refusal a capability check yields, or ``None`` when capable.

        :data:`SCOPE_EXPRESSIBLE_OPERATIONS` is the authoritative rule and is
        consulted HERE, so no host-supplied callable can open an operation no
        wired adapter is proven to express. The facts are then asked for the
        adapter's live answer: a check that raises is a dependency failure, never
        an assumed grant.
        """
        if operation not in SCOPE_EXPRESSIBLE_OPERATIONS:
            # EXIT: nothing wired is known to express the approved restriction
            # for this operation, so it is refused before any source data.
            return ScopeDecision("unsupported", SCOPE_OPERATION_UNSUPPORTED)
        capable = self._capability(operation)
        if capable is None:
            # EXIT: the capability probe faulted, so capability is unknown — a
            # dependency failure, never the assumed grant an exception would
            # otherwise fall through to.
            return ScopeDecision("dependency_failed", SCOPE_DEPENDENCY_FAILED)
        if not capable:
            # EXIT: the probe said "no" for a listed operation — unsupported, not
            # a permission and not the faulted-probe answer. Unreachable under
            # the wired probe, whose only answer derives from that same set.
            return ScopeDecision("unsupported", SCOPE_OPERATION_UNSUPPORTED)
        return None

    def _check_broad(self, operation: str) -> ScopeDecision:
        if operation not in ATTACHMENT_SCOPED_OPERATIONS:
            # EXIT: an operation reading no attachment branch is answered by the
            # company ruling — the same one a company locator gets, never an
            # attachment-branch empty or dependency outcome.
            return self._check_company(operation)
        refusal = self._capability_decision(operation)
        if refusal is not None:
            return refusal
        if self._effective.resolution_fault is not None:
            # EXIT: authority could not be consulted to enumerate the
            # attachment branch. That is a dependency failure, never the
            # successful empty an answered-but-empty enumeration is.
            return ScopeDecision("dependency_failed", SCOPE_DEPENDENCY_FAILED)
        if not self._effective.attachment_ids:
            # EXIT: an attachment-only request with nothing selected is a
            # successful empty, not a denial and not an outage.
            return ScopeDecision("empty", SCOPE_SELECTION_EMPTY)
        return ScopeDecision("permitted", SCOPE_PERMITTED)

    def _check_company(self, operation: str) -> ScopeDecision:
        if not self._effective.company_allowed:
            # EXIT: the selection in force puts the corpus outside the approved
            # scope. The branch ruling precedes capability, so an out-of-scope
            # source is a denial with a remedy, never a capability answer.
            return ScopeDecision("denied", SCOPE_COMPANY_NOT_PERMITTED)
        refusal = self._capability_decision(operation)
        if refusal is not None:
            return refusal
        return ScopeDecision("permitted", SCOPE_PERMITTED)

    def _check_attachment(
        self, operation: str, source_ref: Mapping[str, Any]
    ) -> ScopeDecision:
        if not _is_bound_thread(self._facts.thread_id):
            # EXIT: no host-bound thread; a model-supplied id cannot reach here
            # with authority, so the answer is a denial, never a fallback.
            return ScopeDecision("denied", SCOPE_THREAD_UNBOUND)

        attachment_id = _clean_id(source_ref.get("attachment_id"))
        if attachment_id is None:
            return ScopeDecision("denied", SCOPE_SOURCE_REF_UNREADABLE)

        if attachment_id not in self._effective.attachment_ids:
            if self._effective.resolution_fault is not None:
                # EXIT: the id is outside a selection that authority could not
                # finish assembling, so the emptiness is unproven and the honest
                # answer is the fault, not a permission denial.
                return ScopeDecision("dependency_failed", SCOPE_DEPENDENCY_FAILED)
            # EXIT: an id outside the effective selection is a denial — the
            # readable authority is never consulted to widen it.
            return ScopeDecision("denied", SCOPE_ATTACHMENT_NOT_PERMITTED)

        version = _ref_version(source_ref.get("version"))
        if version is None:
            return ScopeDecision("denied", SCOPE_SOURCE_REF_UNREADABLE)

        refusal = self._authority_refusal(attachment_id, version)
        if refusal is not None:
            return refusal

        try:
            ready = bool(self._facts.attachment_ready(attachment_id, version))
        except Exception:
            return ScopeDecision("dependency_failed", SCOPE_DEPENDENCY_FAILED)
        if not ready:
            # EXIT: readiness is not permission — a not-ready hit is
            # ``unavailable`` (cannot answer yet), never a denial and never a
            # grant.
            return ScopeDecision("unavailable", SCOPE_ATTACHMENT_NOT_READY)

        refusal = self._capability_decision(operation)
        if refusal is not None:
            return refusal
        return ScopeDecision("permitted", SCOPE_PERMITTED)

    def _authority_refusal(
        self, attachment_id: str, version: int
    ) -> ScopeDecision | None:
        """The authority/version refusal for an in-scope id, or ``None``.

        An id already known to be in the effective selection is re-resolved
        here: a resolver that faults is ``dependency_failed`` (distinct from a
        denial), an id no authority stands behind is ``unknown``, and an id
        current at a DIFFERENT version is ``version_stale`` (the requested
        version is not what the trusted store holds).
        """
        try:
            authority = self._facts.resolve_attachment(attachment_id)
        except Exception:
            # EXIT: the resolver itself faulted — a distinct outcome from a
            # denial, so a transient outage is not read as a permission answer.
            return ScopeDecision("dependency_failed", SCOPE_DEPENDENCY_FAILED)
        if authority is None:
            # EXIT: no current authority stands behind the id (unknown,
            # foreign-thread, tombstoned, re-prepared) — a denial.
            return ScopeDecision("denied", SCOPE_ATTACHMENT_UNKNOWN)
        if authority.version != version:
            # EXIT: the id is current but not at the requested version.
            return ScopeDecision("denied", SCOPE_ATTACHMENT_VERSION_STALE)
        return None

    def report(self) -> ScopeReport:
        """The read-only view Workspace reads: scope, capabilities, limits.

        PURE and non-mutating: it reads the already-resolved effective scope and
        the trusted facts, and derives capabilities by DRIVING :meth:`check`
        (never re-deriving permission by hand), so the report and the gate can
        never disagree about what is permitted. It performs no I/O and owns no
        store. An empty ``known_limits`` means no limit applies.
        """
        return ScopeReport(
            effective=self._effective,
            feasible_capabilities=self._feasible_capabilities(),
            known_limits=self._known_limits(),
        )

    def _feasible_capabilities(self) -> frozenset[str]:
        """The reportable operations the gate would permit right now.

        Each operation is decided by the SAME :meth:`check` the boundary uses
        (a broad request, no ``source_ref``), so a capability the gate refuses —
        unsupported, dependency_failed, or the truthful empty — is never listed.
        """
        return frozenset(
            operation
            for operation in _REPORTABLE_OPERATIONS
            if self.check(operation).outcome == "permitted"
        )

    def _known_limits(self) -> tuple[KnownLimit, ...]:
        """Every limit that applies to the current scope, in report order.

        A structural limit (an excluded company branch, no trusted thread, or an
        authority that could not be consulted) is reported first: those limits
        already explain why a source is unusable, so per-attachment consequences
        under a thread or enumeration fault are not repeated. Otherwise each
        category of attachment limit is derived from the SAME facts the gate
        reads.
        """
        limits: list[KnownLimit] = []
        if not self._effective.company_allowed:
            limits.append(
                KnownLimit(
                    SCOPE_COMPANY_NOT_PERMITTED,
                    "The restriction in force is an exclusive selection, so the "
                    "company corpus is outside the effective scope.",
                    "Ask to widen the scope (an explicit request Runtime must "
                    "accept and current authority must support).",
                )
            )
        if not _is_bound_thread(self._facts.thread_id):
            # EXIT: no trusted thread — no attachment can be selected, and the
            # report must say so rather than read as an empty (full-access) scope.
            limits.append(
                KnownLimit(
                    SCOPE_THREAD_UNBOUND,
                    "No research thread is bound, so no attachment can be selected.",
                    None,
                )
            )
            return tuple(limits)
        if self._effective.resolution_fault is not None:
            # EXIT: the enumeration itself faulted, so the selection is unknown
            # rather than genuinely empty; the fault is the one root limit.
            limits.append(
                KnownLimit(
                    SCOPE_DEPENDENCY_FAILED,
                    "The attachment authority could not be consulted, so the "
                    "effective selection is unknown.",
                    None,
                )
            )
            return tuple(limits)
        limits.extend(
            KnownLimit(
                SCOPE_ATTACHMENT_UNKNOWN,
                f"Attachment {attachment_id} is named by the current restriction "
                "but current authority does not stand behind it, so it is not in "
                "the effective selection.",
                "Reselect the attachment, re-upload it, or widen the scope.",
            )
            for attachment_id in self._effective.excluded_ids
        )
        limits.extend(
            limit
            for attachment_id in self._effective.attachment_ids
            if (limit := self._attachment_limit(attachment_id)) is not None
        )
        return tuple(limits)

    def _attachment_limit(self, attachment_id: str) -> KnownLimit | None:
        """The limit an in-scope id carries, or ``None`` when it is usable.

        Reads the SAME facts the enforcement path reads — the current identity
        and version, then readiness at that version — so a report never calls an
        attachment usable that the gate would refuse.
        """
        try:
            authority = self._facts.resolve_attachment(attachment_id)
        except Exception:
            return KnownLimit(
                SCOPE_DEPENDENCY_FAILED,
                f"Authority for attachment {attachment_id} could not be checked.",
                None,
            )
        if authority is None:
            # EXIT: in the selection at resolution, but no current authority
            # stands behind it now (removed, re-prepared) — a race limit.
            return KnownLimit(
                SCOPE_ATTACHMENT_UNKNOWN,
                f"Attachment {attachment_id} is in the effective selection but "
                "current authority no longer stands behind it.",
                None,
            )
        try:
            ready = bool(self._facts.attachment_ready(attachment_id, authority.version))
        except Exception:
            return KnownLimit(
                SCOPE_DEPENDENCY_FAILED,
                f"Readiness for attachment {attachment_id} could not be checked.",
                None,
            )
        if not ready:
            # EXIT: ready is not permission — an authorized-but-not-ready id is
            # a limit ("cannot answer yet"), never a grant.
            return KnownLimit(
                SCOPE_ATTACHMENT_NOT_READY,
                f"Attachment {attachment_id} is authorized but is still being "
                "prepared, so it cannot answer yet.",
                None,
            )
        return None


def scope_denied_error(decision: ScopeDecision) -> dict[str, Any]:
    """The model's typed error dict for a refusal, or raise for a non-refusal.

    Mirrors the retrieve leg's error envelope: exactly ``{"error": <code>}``
    with a code from the closed :attr:`ScopeGate.DENIAL_CODES` set. A
    ``permitted`` or ``empty`` decision is not a refusal and has no error code,
    so it is refused here rather than rendered as one.
    """
    if decision.reason not in ScopeGate.DENIAL_CODES:
        raise ValueError(
            f"scope decision is not a refusal: {decision.outcome} ({decision.reason})"
        )
    return {"error": decision.reason}


__all__ = [
    "ATTACHMENT_SCOPED_OPERATIONS",
    "COMPANY_CAPABILITY",
    "KNOWN_LIMIT_CODES",
    "SCOPE_EXPRESSIBLE_OPERATIONS",
    "AttachmentAuthority",
    "AuthorityFacts",
    "EffectiveScope",
    "KnownLimit",
    "SCOPE_ATTACHMENT_NOT_PERMITTED",
    "SCOPE_ATTACHMENT_NOT_READY",
    "SCOPE_ATTACHMENT_UNKNOWN",
    "SCOPE_ATTACHMENT_VERSION_STALE",
    "SCOPE_COMPANY_NOT_PERMITTED",
    "SCOPE_DEPENDENCY_FAILED",
    "SCOPE_OPERATION_UNSUPPORTED",
    "SCOPE_PERMITTED",
    "SCOPE_SELECTION_EMPTY",
    "SCOPE_SOURCE_REF_UNREADABLE",
    "SCOPE_SNAPSHOT_UNREADABLE",
    "SCOPE_THREAD_UNBOUND",
    "ScopeDecision",
    "ScopeGate",
    "ScopeOrigin",
    "ScopeOutcome",
    "ScopeProposal",
    "ScopeReport",
    "ScopeRestriction",
    "propose_scope_change",
    "resolve_scope",
    "scope_denied_error",
]


# ----- versioned thread source-scope snapshot (shared wire contract) -----

# The schema version stamped on every snapshot the Runtime writes. A reader
# that meets a different version refuses the value rather than reinterpreting
# it under today's rules: the wire shape is frozen, and an unknown shape must
# not silently acquire new meaning.
SOURCE_SCOPE_SCHEMA_VERSION: Final[int] = 1

# The maximum number of DISTINCT source identities a restricted selection may
# name (shared wire contract: "one to 200 distinct source identities"). The
# bound keeps one clarified choice — and one snapshot — representable without
# unbounded growth.
MAX_SCOPE_SOURCES: Final[int] = 200

# The maximum size, in UTF-8 bytes, of one ``source_id`` or ``version`` value.
# The shared contract freezes this bound for choice source identities; a
# snapshot identity is the same wire value, so the same bound applies.
MAX_SCOPE_FIELD_BYTES: Final[int] = 256

# The inclusive top of the nonnegative signed-64-bit counter range the wire
# freezes for ``accepted_input_version``; 2**63 is overflow.
_INT64_MAX: Final[int] = 2**63 - 1

# The closed set of source classes a snapshot identity can name. The contract
# freezes exactly these two: the shared company corpus and the active thread's
# attachments. Anything else is invalid rather than a new class.
ScopeSourceKind = Literal["corpus", "attachment"]

# ``SourceKind`` is the alias the snapshot slice exposes under the contract's
# spelling; :data:`ScopeSourceKind` is the same closed set.
SourceKind = ScopeSourceKind

_SCOPE_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"corpus", "attachment"})

# Snapshot-specific codes, in the same one-cause-one-code style as the
# SCOPE_* vocabulary above. Each is surfaced through the task failure kinds
# named by the shared contract (task_input_invalid for every malformed or
# out-of-contract envelope value); they are reason detail, never a second
# error convention.
SCOPE_SNAPSHOT_NOT_AN_OBJECT: Final[str] = "scope_snapshot_not_an_object"
SCOPE_SNAPSHOT_SCHEMA_UNSUPPORTED: Final[str] = "scope_snapshot_schema_unsupported"
SCOPE_SNAPSHOT_MODE_INVALID: Final[str] = "scope_snapshot_mode_invalid"
SCOPE_SNAPSHOT_SOURCES_INVALID: Final[str] = "scope_snapshot_sources_invalid"
SCOPE_SNAPSHOT_KIND_UNKNOWN: Final[str] = "scope_snapshot_kind_unknown"
SCOPE_SNAPSHOT_INPUT_INVALID: Final[str] = "scope_snapshot_input_invalid"
SCOPE_SNAPSHOT_VERSION_INVALID: Final[str] = "scope_snapshot_version_invalid"

# The task failure kinds the contract names for this slice, re-exported so a
# caller of this module reads one vocabulary instead of two.
SCOPE_SNAPSHOT_FAILURE_KINDS: Final[frozenset[str]] = frozenset(
    {TASK_INPUT_INVALID, TASK_PERMISSION_DENIED, TASK_APPLICABILITY_UNRESOLVED}
)


def _field_is_identifier(value: object) -> bool:
    """True only for a nonempty, ≤256-byte, control-character-free string.

    This is the shared contract's identity rule for ``source_id`` and
    ``version``: nonempty strings of at most 256 UTF-8 bytes with no control
    characters. A non-string never identifies a source, so it is invalid
    rather than coerced.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8")) > MAX_SCOPE_FIELD_BYTES:
        return False
    return not any(
        unicodedata.category(char) == "Cc"  # Unicode Other-control
        for char in value
    )


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """One versioned source a restricted selection names — a value, not a grant.

    ``version`` is the wire's opaque version string (the store's own spelling),
    the identity Source Scope rechecks against current authority. Holding an
    identity is a record of intent: it confers no permission and is never
    read as "this source is currently readable".
    """

    source_kind: ScopeSourceKind
    source_id: str
    version: str

    def to_state_dict(self) -> dict[str, str]:
        """The wire object exactly as the shared contract freezes it."""
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "version": self.version,
        }

    @classmethod
    def from_state_dict(cls, raw: object) -> SourceIdentity:
        """The identity ``raw`` carries, or a typed error — never a guess.

        A non-object, an unknown kind, or a field that is not a nonempty
        ≤256-byte control-free string raises ``task_input_invalid`` with the
        specific snapshot code as ``detail["code"]``. Nothing is defaulted:
        a malformed identity is a refusal, not a repaired one.
        """
        if not isinstance(raw, Mapping):
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "a source identity must be an object",
                code=SCOPE_SNAPSHOT_NOT_AN_OBJECT,
            )
        if raw.get("source_kind") not in _SCOPE_SOURCE_KINDS:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "unknown source kind",
                code=SCOPE_SNAPSHOT_KIND_UNKNOWN,
            )
        for field in ("source_id", "version"):
            if not _field_is_identifier(raw.get(field)):
                raise TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    f"{field} must be a nonempty string of at most "
                    f"{MAX_SCOPE_FIELD_BYTES} UTF-8 bytes with no control characters",
                    code=SCOPE_SNAPSHOT_INPUT_INVALID,
                )
        return cls(
            source_kind=raw["source_kind"],  # type: ignore[index]
            source_id=raw["source_id"],  # type: ignore[index]
            version=raw["version"],  # type: ignore[index]
        )


@dataclass(frozen=True, slots=True)
class SourceScopeValidation:
    """The snapshot ``raw`` carries, or the typed reason it does not.

    Exactly one side is populated: ``snapshot`` is ``None`` whenever ``code``
    names a failure, so a caller can never read a half-valid snapshot. The
    code is one of this module's snapshot codes and ``kind`` is the task
    failure kind it surfaces through.
    """

    snapshot: SourceScopeSnapshot | None
    code: str | None
    kind: str | None


@dataclass(frozen=True, slots=True)
class SourceScopeSnapshot:
    """The append-only versioned selection the Runtime persists in the thread.

    One snapshot records the WHOLE accepted intent: the mode, the exact
    versioned identities when restricted, and the accepted input that
    established it. It is INTENT plus provenance, never a permission —
    every read rechecks current authority through :func:`recheck_scope`,
    because ``accepted_input_version`` records WHICH accepted input
    established the intent and never confers permission on its own.

    ``mode="unrestricted"`` requires ``sources == ()`` and means the currently
    authorized corpus plus eligible thread sources. ``mode="restricted"``
    requires one to :data:`MAX_SCOPE_SOURCES` DISTINCT identities. A clear is
    written as ``mode="unrestricted"`` with ``sources=()`` — an explicit key,
    never an omitted one. The snapshot is frozen: a new accepted intent
    writes a new snapshot, it never edits this one.
    """

    schema_version: int
    mode: Literal["unrestricted", "restricted"]
    sources: tuple[SourceIdentity, ...]
    accepted_input_version: int

    @property
    def in_force(self) -> bool:
        """Whether this snapshot narrows the selection at all.

        ``True`` only for a restricted selection. Unrestricted is the default
        whole-corpus-plus-thread posture, so it is not "in force" — and an
        empty ``document_ids`` projection must never be read as the opposite.
        """
        return self.mode == "restricted"

    def to_state_dict(self) -> dict[str, Any]:
        """The frozen wire shape exactly as the shared contract spells it."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "sources": [source.to_state_dict() for source in self.sources],
            "accepted_input_version": self.accepted_input_version,
        }

    @classmethod
    def from_state_dict(cls, raw: object) -> SourceScopeSnapshot:
        """The snapshot ``raw`` carries, or a typed error — never a guess.

        Every contract rule is enforced here: object envelope, frozen schema
        version, mode/source agreement, one to :data:`MAX_SCOPE_SOURCES`
        distinct identities, identifier-shaped fields, and a nonnegative
        signed-64-bit ``accepted_input_version``. Any violation raises
        ``task_input_invalid``; nothing falls back to unrestricted.
        """
        if not isinstance(raw, Mapping):
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "a source scope snapshot must be an object",
                code=SCOPE_SNAPSHOT_NOT_AN_OBJECT,
            )
        if raw.get("schema_version") != SOURCE_SCOPE_SCHEMA_VERSION:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "unsupported source_scope schema version",
                code=SCOPE_SNAPSHOT_SCHEMA_UNSUPPORTED,
            )
        mode = raw.get("mode")
        if mode not in ("unrestricted", "restricted"):
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "mode must be unrestricted or restricted",
                code=SCOPE_SNAPSHOT_MODE_INVALID,
            )
        version = raw.get("accepted_input_version")
        if (
            type(version) is not int
            or version < 0
            or version > _INT64_MAX
        ):
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "accepted_input_version must be a nonnegative signed 64-bit integer",
                code=SCOPE_SNAPSHOT_VERSION_INVALID,
            )
        raw_sources = raw.get("sources")
        if not isinstance(raw_sources, (list, tuple)):
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "sources must be a list of source identities",
                code=SCOPE_SNAPSHOT_SOURCES_INVALID,
            )
        sources = tuple(
            SourceIdentity.from_state_dict(entry) for entry in raw_sources
        )
        if mode == "unrestricted" and sources:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                "an unrestricted scope names no sources",
                code=SCOPE_SNAPSHOT_MODE_INVALID,
            )
        if mode == "restricted":
            if not sources:
                # EXIT: an empty restricted selection is a refusal — never a
                # silent fallback to unrestricted, which would widen exactly
                # where the user asked to narrow.
                raise TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    "a restricted scope names at least one source",
                    code=SCOPE_SNAPSHOT_SOURCES_INVALID,
                )
            if len(sources) > MAX_SCOPE_SOURCES:
                raise TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    f"a restricted scope names at most {MAX_SCOPE_SOURCES} sources",
                    code=SCOPE_SNAPSHOT_SOURCES_INVALID,
                )
            distinct = set(sources)
            if len(distinct) != len(sources):
                raise TaskRuntimeError(
                    TASK_INPUT_INVALID,
                    "a restricted scope names each source once",
                    code=SCOPE_SNAPSHOT_SOURCES_INVALID,
                )
        return cls(
            schema_version=SOURCE_SCOPE_SCHEMA_VERSION,
            mode=mode,  # type: ignore[arg-type]
            sources=sources,
            accepted_input_version=version,
        )


@dataclass(frozen=True, slots=True)
class ScopeInput:
    """An EXPLICITLY scoped input: the scope it names, accepted or not.

    This is the input type that makes "the user named a scope in this input"
    representable. It carries the whole requested intent so an accepted
    replacement cannot quietly lose half of it. It grants nothing on its own.
    """

    mode: Literal["unrestricted", "restricted"]
    sources: tuple[SourceIdentity, ...]


@dataclass(frozen=True, slots=True)
class UnsetScopeInput:
    """The explicit absence of scope intent in one input.

    A fresh unscoped question, a short follow-up, and a search-mode-only
    change all carry scope intent = UNSET. It is deliberately a distinct type
    from a clear: "no scope named here" and "clear the scope" are different
    intents, and collapsing them is what would let a fresh question silently
    broaden a restriction.
    """


# The singleton every unscoped input carries. Named in the wire-visible sense:
# the ABSENCE of a scope key in one input, not a cleared scope.
UNSET_SCOPED_INPUT: Final[UnsetScopeInput] = UnsetScopeInput()

# What an input can name as scope intent: an explicit selection, or unset.
ScopeScopedInput = ScopeInput | UnsetScopeInput


@dataclass(frozen=True, slots=True)
class SourceScopeRecheck:
    """The outcome of rechecking a saved snapshot against current authority.

    ``in_force`` is True only when every identity is confirmed NOW — that is
    the only path from a saved intent to usable authority. ``kind`` is the
    task failure kind when confirmation failed (``task_dependency_failure``
    is distinguishable from a definite denial); ``snapshot`` is non-None only
    when the recheck confirmed the restriction. A failed recheck NEVER
    returns an unrestricted snapshot: a restriction that current authority no
    longer supports resolves to nothing usable, never to the whole corpus.
    """

    snapshot: SourceScopeSnapshot | None
    kind: str | None

    @property
    def in_force(self) -> bool:
        return self.snapshot is not None


@runtime_checkable
class SourceScopeAuthority(Protocol):
    """The checks a caller must perform before a saved intent is authority.

    The host supplies the implementation; this module owns no store and no
    permission table. Each method answers about ONE identity:

    * ``identity_exists`` — does any current authority stand behind it?
    * ``version_current`` — is the saved version still the current one?
    * ``caller_permitted`` — may THIS caller use this source at all?

    Every method may raise; a raise means the authority could not answer,
    which is a dependency failure, never a permission answer.
    """

    def identity_exists(self, source: SourceIdentity) -> bool: ...

    def version_current(self, source: SourceIdentity) -> bool: ...

    def caller_permitted(self, source: SourceIdentity) -> bool: ...


@runtime_checkable
class SourceScopeReader(Protocol):
    """How a host hands the snapshot its OWN checkpoint already accepted.

    A single method, deliberately: the reader is not a second store and not a
    second policy. It answers one question — "what snapshot, if any, is
    committed on this thread?" — and the caller still validates the answer
    through :func:`validate_source_scope` and re-asks current authority
    through :func:`recheck_scope` before any read honours it.

    The contract for implementations:

    * ``None`` means NO snapshot has ever been accepted on that thread, which
      is the pre-scope posture, not a cleared restriction (a clear is a real
      ``mode="unrestricted"`` snapshot, never an absent one);
    * the value is returned VERBATIM. A reader must not repair, default, or
      reinterpret it — an unreadable value is a named cause the caller must
      surface, and repairing it here would reintroduce exactly the silent
      widening this protocol exists to prevent;
    * it raises rather than answering when the channel cannot be read. A
      fault is not an absence, and the caller treats the two differently.
    """

    def __call__(self, thread_id: str) -> dict[str, Any] | None: ...


def unrestricted_scope(*, accepted_input_version: int) -> SourceScopeSnapshot:
    """The explicit unrestricted snapshot — the shape a clear writes.

    Pure construction: it carries no permission, and ``accepted_input_version``
    only records which accepted input established the intent.
    """
    return SourceScopeSnapshot(
        schema_version=SOURCE_SCOPE_SCHEMA_VERSION,
        mode="unrestricted",
        sources=(),
        accepted_input_version=accepted_input_version,
    )


def restricted_scope(
    sources: Sequence[SourceIdentity], *, accepted_input_version: int
) -> SourceScopeSnapshot:
    """The restricted snapshot for ``sources``, or a typed error.

    Construction goes through the same validation as the wire round-trip, so
    a builder and a reader can never disagree about what a valid snapshot is.
    Duplicates collapse first (naming a source twice is one intent), and an
    empty selection is ``task_input_invalid`` — never a silent unrestricted.
    """
    ordered: list[SourceIdentity] = []
    for source in sources:
        if source not in ordered:
            ordered.append(source)
    return SourceScopeSnapshot.from_state_dict(
        {
            "schema_version": SOURCE_SCOPE_SCHEMA_VERSION,
            "mode": "restricted",
            "sources": [source.to_state_dict() for source in ordered],
            "accepted_input_version": accepted_input_version,
        }
    )


def validate_source_scope(raw: object) -> SourceScopeValidation:
    """The snapshot ``raw`` carries, with failures as CODES, not exceptions.

    The semantic validator for callers that branch on the reason (Runtime's
    proposal admission). It performs no source I/O and raises nothing: the
    same rules as :meth:`SourceScopeSnapshot.from_state_dict`, delivered as a
    value.
    """
    try:
        return SourceScopeValidation(
            snapshot=SourceScopeSnapshot.from_state_dict(raw), code=None, kind=None,
        )
    except TaskRuntimeError as exc:
        code = exc.detail.get("code")
        return SourceScopeValidation(
            snapshot=None,
            code=code if isinstance(code, str) else SCOPE_SNAPSHOT_NOT_AN_OBJECT,
            kind=exc.kind,
        )


def scope_from_candidates(
    candidates: Sequence[SourceIdentity], *, accepted_input_version: int
) -> SourceScopeSnapshot | None:
    """The proposal a confirmed document choice implies, or ``None``.

    A REQUEST for Runtime to accept — never the write itself, and never a
    mutation of the stored snapshot (pending candidates are not durable user
    preferences). ``None`` means no proposal: with no candidate there is
    nothing to accept, and an empty restricted selection is invalid rather
    than silently reinterpreted as unrestricted.
    """
    if not candidates:
        return None
    return restricted_scope(candidates, accepted_input_version=accepted_input_version)


def replace_scope(
    previous: SourceScopeSnapshot,
    /,
    *,
    requested: ScopeScopedInput,
    accepted_input_version: int,
) -> SourceScopeSnapshot:
    """The snapshot after one accepted input, under the transition table.

    The transition rules, frozen by the shared contract:

    * ``requested is UNSET_SCOPED_INPUT`` (a fresh unscoped question, a short
      follow-up, a search-mode-only change) KEEPS the previous restriction —
      never a silent broadening — while ``accepted_input_version`` becomes the
      authoritative new value the caller supplies (the accepted input is real;
      it just names no scope).
    * an explicit :class:`ScopeInput` replaces the selection only because the
      caller accepted it; this function does not accept, it projects.
    * a clear is not represented here: :func:`clear_scope` writes it, as an
      explicit ``mode="unrestricted"`` key, never an omitted one.

    Pending candidates change nothing here: a proposal exists only through
    :func:`scope_from_candidates`, and until Runtime accepts it the previous
    snapshot is the snapshot.
    """
    if isinstance(requested, UnsetScopeInput):
        return dataclass_replace(previous, accepted_input_version=accepted_input_version)
    return SourceScopeSnapshot.from_state_dict(
        {
            "schema_version": SOURCE_SCOPE_SCHEMA_VERSION,
            "mode": requested.mode,
            "sources": [source.to_state_dict() for source in requested.sources],
            "accepted_input_version": accepted_input_version,
        }
    )


def clear_scope(
    previous: SourceScopeSnapshot, /, *, accepted_input_version: int
) -> SourceScopeSnapshot:
    """The EXPLICIT unrestricted snapshot a cleared restriction writes.

    A clear is an explicit key, never an omitted one: the result is a real
    ``mode="unrestricted"`` snapshot with ``sources=()`` and its own accepted
    input version, distinguishable from "this input named no scope" (which
    keeps the previous restriction via :func:`replace_scope`).
    """
    del previous  # the clear is total; the previous value cannot survive in it
    return unrestricted_scope(accepted_input_version=accepted_input_version)


def legacy_document_ids(snapshot: SourceScopeSnapshot) -> tuple[str, ...]:
    """The DERIVED LEGACY corpus projection of ``snapshot``.

    For a restricted snapshot it lists the corpus identities' ``source_id``
    values, in order; an attachment-only restriction cannot be expressed and
    projects to ``()``; unrestricted projects to ``()``. This is a
    compatibility projection for the existing ``StatusSnapshot.document_ids``
    reader — NEVER a second authority, and unrestricted access is never
    inferred from ``()`` when a snapshot is present: the snapshot itself is
    the decision, and readers must consult ``source_scope`` before attachment
    choices are enabled.
    """
    return tuple(
        source.source_id
        for source in snapshot.sources
        if source.source_kind == "corpus"
    )


def recheck_scope(
    current: SourceScopeSnapshot,
    /,
    *,
    authority: SourceScopeAuthority,
) -> SourceScopeRecheck:
    """Turn a saved intent into usable authority, or name why it cannot.

    This is the ONLY path from a stored snapshot to a permission-bearing
    answer: ``accepted_input_version`` never confers permission, so every
    identity is re-asked NOW — does current authority still stand behind it,
    at the saved version, for this caller. An unresolved identity or version
    is ``task_applicability_unresolved``; a permission answer of "no" is
    ``task_permission_denied``; an authority that RAISES is the distinct
    ``task_dependency_failure`` (an outage is not a denial and not an
    answer). A failed recheck returns no snapshot — never an unrestricted
    fallback, which would broaden exactly where the intent narrowed. An
    unrestricted snapshot names no identities, so there is nothing to
    recheck and it confirms as itself.
    """
    if current.mode != "restricted":
        return SourceScopeRecheck(snapshot=current, kind=None)
    for source in current.sources:
        try:
            if not authority.identity_exists(source):
                return SourceScopeRecheck(
                    snapshot=None, kind=TASK_APPLICABILITY_UNRESOLVED
                )
            if not authority.version_current(source):
                return SourceScopeRecheck(
                    snapshot=None, kind=TASK_APPLICABILITY_UNRESOLVED
                )
            if not authority.caller_permitted(source):
                return SourceScopeRecheck(
                    snapshot=None, kind=TASK_PERMISSION_DENIED
                )
        except Exception:
            # EXIT: the authority could not answer this identity. Recorded as
            # a dependency failure — distinct from a denial — and never
            # consumed as "answered, allowed".
            return SourceScopeRecheck(snapshot=None, kind=TASK_DEPENDENCY_FAILURE)
    return SourceScopeRecheck(snapshot=current, kind=None)


__all__ += [
    "MAX_SCOPE_FIELD_BYTES",
    "MAX_SCOPE_SOURCES",
    "SCOPE_SNAPSHOT_FAILURE_KINDS",
    "SCOPE_SNAPSHOT_INPUT_INVALID",
    "SCOPE_SNAPSHOT_KIND_UNKNOWN",
    "SCOPE_SNAPSHOT_MODE_INVALID",
    "SCOPE_SNAPSHOT_NOT_AN_OBJECT",
    "SCOPE_SNAPSHOT_SOURCES_INVALID",
    "SCOPE_SNAPSHOT_SCHEMA_UNSUPPORTED",
    "SCOPE_SNAPSHOT_VERSION_INVALID",
    "SOURCE_SCOPE_SCHEMA_VERSION",
    "UNSET_SCOPED_INPUT",
    "SourceIdentity",
    "SourceKind",
    "SourceScopeAuthority",
    "SourceScopeReader",
    "SourceScopeRecheck",
    "SourceScopeSnapshot",
    "SourceScopeValidation",
    "ScopeInput",
    "ScopeScopedInput",
    "ScopeSourceKind",
    "clear_scope",
    "legacy_document_ids",
    "replace_scope",
    "recheck_scope",
    "restricted_scope",
    "scope_from_candidates",
    "unrestricted_scope",
    "validate_source_scope",
]
