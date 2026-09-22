"""检索档 (search-mode) decision cascade after Soft-Track ``grounded``.

Mirror of :func:`fin.host.track_decision.decide_track`'s tiered cascade
(ADR 0027 / 0062), run ONLY after the track decision already returned
``grounded`` (spec search-mode-host confirmed assumption 2: a
non-grounded track never reaches this cascade and assigns no 档).

Tier order (no silent default — the residual never names global):

1. **rules** — a documented pin/override, or an utterance that EXPLICITLY
   names a 档: the letter adjacent to 档 (``A档`` / ``档 B``) or a CONTEXT
   label adjacent to 档 (查找 / 知识问答 / 综述 / 真数据库). Requiring the
   档 marker keeps a single letter from firing on ordinary English ("A
  apl"-shaped text cannot reach 档 adjacency) and mirrors the closed
   selector shape ``fin.retrieve.tiers`` allows — question prose is never
   scanned for bare 档 words (``fin.host.named_failures`` T6 note). A name
   that cannot resolve uniquely (two different 档 named) stays ambiguous
   and falls through.
2. **jev** — one closed-set TypeSafe Choice over the lasting 档 tokens,
   only when rules miss **and** inherit would not already decide (the
   same peek shape as ``decide_track``, so a judge answer cannot steal an
   inherited 档). Fail-closed: a missing client, a typed
   ``JevChoiceError``, an unexpected throw, an unmappable label or
   sub-floor confidence all record ``signals["mode_jev_exit"]`` and fall
   through — never a crash, never a silent-open global. A key-less boot is
   named ``no_jev_client`` rather than left as a silent branch, exactly as
   ``decide_track`` names its own gray zone; like every other EXIT it says
   nothing was decided, and no label or confidence is written beside it.
   The judge's keys carry the ``mode_`` prefix for human-readable
   attribution in merged logs — the two ``signals`` dicts land in separate
   audit rows (``track_decision`` / ``search_mode_decision``) and are never
   merged.
3. **inherit** — a short follow-up on the same thread that stays in the
   same retrieve job (latest status snapshot still has research context;
   no company-change wipe since the last assigned 档) reuses that 档.
4. **clarify** — the residual: emits the structured option list for the
   operator (the choice-window ``interrupt`` wiring is Ticket 2; this
   ticket only persists the payload data). An unnamed ask keeps
   ``search_mode=None`` — the residual policy must not default to global.

A 档 named but not open (per the ``TIER_UNAVAILABLE_CODE`` SSOT — never
a duplicated enumeration) is a **typed fail** carrying the CONTEXT 具名
失败 code (``tier_not_open`` / ``real_db_not_wired``); it does not invent
a 档 and does not fall through to a different one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import BaseMessage

from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceError,
)
# The 档 SSOT (closed set + per-档 availability codes) is reused, not
# re-enumerated; ``named_failures`` is the host-side import surface for
# both (see its module docstring).
from fin.host.named_failures import (
    LASTING_RETRIEVE_TIERS,
    TIER_LABELS,
    TIER_UNAVAILABLE_CODE,
    RetrieveTier,
    normalize_tier_token,
)
# Package-internal reuse of the Soft-Track cascade primitives (utterance
# extraction, latest-snapshot context, company-change detection), of its
# gray-zone seam type and of its confidence floor: SSOT over duplication
# — these are the exact predicates ``decide_track``'s inherit tier uses,
# the exact client shape its judge is injected with, and the one floor
# below which a Choice is a typed EXIT rather than a decision (both
# judges are the same gray-zone mechanism, so the bar cannot drift).
from fin.host.track_decision import (
    _MIN_JEV_CONFIDENCE,
    JevChoiceProvider,
    _company_changed,
    _last_human_text,
    _latest_snapshot,
    _normalize_company,
    _snapshot_has_research_context,
)


# A 档 is NOT a fifth track: ``SearchMode`` is the retrieve-tier token
# re-aliased from the SSOT (``fin.retrieve.tiers`` via
# ``named_failures``) — the Soft-Track ``Track`` enum stays untouched.
SearchMode = RetrieveTier

# Same tier names as ``track_decision.DecisionTier`` for the shared four;
# the search cascade's residual is ``clarify`` (never a silent-assign
# ``conditional_policy``), and a named-but-unopened 档 exits via
# ``typed_fail`` instead. ``operator_pick`` is NOT a cascade tier (the
# cascade itself never produces it) but the auditable decision-tier a
# choice-window RESUME writes (Ticket 2 / ADR 0062 HITL) — an operator
# pick outranks the cascade, so it must be distinguishable from
# ``rules`` on the audit.
SearchModeDecisionTier = Literal[
    "rules", "jev", "inherit", "clarify", "typed_fail", "operator_pick"
]

# Operator-visible labels (CONTEXT terms). Aliased, not re-spelled: the
# retrieve 档 SSOT owns the four names so the choice window, an utterance
# and the copy cannot drift from each other.
MODE_LABELS: dict[str, str] = TIER_LABELS

_LABEL_TO_MODE: dict[str, str] = {label: mode for mode, label in MODE_LABELS.items()}

# Inherit follows up on SHORT text only (mirrors ``decide_track``'s
# 48-char inherit heuristic — same shape, same reason: a long ask is its
# own question, not a continuation).
_INHERIT_MAX_LEN = 48

# The judge is taught the operator labels AND what separates the 档. A bare
# ``token + label`` pair is not a discriminator, and shipping one cost the
# live judge its only opened 档 — ``D 真数据库`` reads like the natural home
# for any question about a financial figure, while the spec keeps a figure
# that lives only in a filing on 知识问答 (``graph``), so the label alone
# pulled the grounded asks that this tier exists to decide onto an unopened
# 档. Each gloss is therefore the boundary case it settles, not a
# restatement of the label — and it is sent per option, in the wire's
# ``criteria`` slot, not pasted into the instruction prose.
#
# The table is keyed by the lasting tokens and handed over whole, so a 档
# added to ``LASTING_RETRIEVE_TIERS`` without its discriminator is a
# mismatch the client names before it sends: a ``ValueError`` the cascade
# records as ``mode_jev_exit=unexpected_error`` with ``mode_jev_error_type``,
# never a silently undiscriminated option. The hermetic half of that pin —
# the row missing before anything is called — is
# ``tests/host/test_search_mode_gray_zone_jev.py``.
_JEV_MODE_TIER_GLOSSES: dict[str, str] = {
    "A": "an exact-string or single-known-field lookup in the filed documents",
    "graph": (
        "a knowledge question answered from the filed documents, including "
        "any figure that only lives in a filing (revenue, net sales, gross "
        "margin, growth)"
    ),
    "global": (
        "a synthesis that has to span many filings at once — across issuers, "
        "or across many reporting periods — because no single document "
        "answers it (a summary of one document is a knowledge question)"
    ),
    "D": (
        "a number that must come from a live business database or SQL table "
        "rather than from any document"
    ),
}

# Names only: which tokens exist and what the operator calls them. The
# bodies that separate them ride ``_JEV_MODE_TIER_GLOSSES`` into the
# criteria slot, so the two never have to be kept in prose sync here.
_JEV_MODE_INSTRUCTIONS: str = (
    "Classify this grounded knowledge question into exactly one 检索档 "
    "(retrieve tier): "
    + ", ".join(
        f"{mode} {MODE_LABELS[mode]}"
        for mode in LASTING_RETRIEVE_TIERS
        if mode in MODE_LABELS
    )
    + ". Answer with the token alone."
)

# A 档 is NAMED (high-confidence) only next to the 档 marker — a lasting
# token, a retired letter, or a CONTEXT label adjacent to 档. Bare words
# in question prose are NOT scanned (``fin.host.named_failures`` T6 note).
_TIER_NAMING_RE = re.compile(
    r"(graph|global|[ABCD])\s*档|档\s*(graph|global|[ABCD])"
    r"|(查找|知识问答|综述|真数据库)\s*档|档\s*(查找|知识问答|综述|真数据库)",
    re.IGNORECASE,
)


def normalize_search_mode(raw: object) -> SearchMode | None:
    """Map a wire/judge/pin/pick token onto the lasting 档 tokens.

    The alias map is :mod:`fin.retrieve.tiers`' — this is the host-side
    call of the same total function, kept as the name the cascade and the
    pin seams already use.

    Deliberately NOT :func:`fin.retrieve.port.resolve_search_mode`:
    that raises at the port seam; the cascade must OBSERVE an
    out-of-set token (typed EXIT) and keep falling through, so it needs
    a total function here. Openness (``TIER_UNAVAILABLE_CODE``) is
    checked by the caller so ``global``/``D`` can route to 具名失败.
    """
    return normalize_tier_token(raw)  # type: ignore[return-value]


@dataclass(frozen=True)
class SearchModeDecision:
    """Auditable search-mode decision for one run (mirrors TrackDecision).

    ``search_mode`` is ``None`` on clarify and on typed fail — the
    cascade records what it could NOT name rather than inventing a 档.
    ``options`` is non-empty only on clarify (Ticket 2 renders it as the
    choice window); ``failure_code`` only on typed fail.
    """

    search_mode: SearchMode | None
    confidence: float
    decision_tier: SearchModeDecisionTier
    signals: dict[str, Any] = field(default_factory=dict)
    options: list[dict[str, Any]] = field(default_factory=list)
    failure_code: str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        """The audit dict persisted on ``AgentState.search_mode_decision``
        (mirrors the ``track_decision`` state dict written by
        ``classify_node``)."""
        return {
            "search_mode": self.search_mode,
            "confidence": self.confidence,
            "decision_tier": self.decision_tier,
            "signals": dict(self.signals),
            "options": list(self.options),
            "failure_code": self.failure_code,
        }


def clarify_options() -> list[dict[str, Any]]:
    """The structured option list for the choice window (Ticket 2).

    Every 档 the operator can READ is listed (spec: an unopened one may
    appear, marked as such); ``opened`` reads the
    ``TIER_UNAVAILABLE_CODE`` SSOT so a newly opened 档 flips here and
    nowhere else. The option ``id`` is the LASTING token, so the pick an
    operator resumes with is already the value the audit and the port
    carry — nothing has to translate it on the way.
    """
    # The label map is what makes a token a choice an operator can read,
    # so the window follows it and nothing else; a lasting 档 added to
    # the closed set without a label stays out of the window until it is
    # named for the operator too.
    return [
        {
            "id": mode,
            "label": MODE_LABELS[mode],
            "opened": mode not in TIER_UNAVAILABLE_CODE,
        }
        for mode in LASTING_RETRIEVE_TIERS
        if mode in MODE_LABELS
    ]


def _named_modes(text: str) -> list[str]:
    """Unique 档 the utterance explicitly NAMES, in first-seen order."""
    out: list[str] = []
    for match in _TIER_NAMING_RE.finditer(text or ""):
        raw = next((group for group in match.groups() if group), "")
        mode = normalize_search_mode(raw) or _LABEL_TO_MODE.get(raw.strip(), "")
        if mode and mode not in out:
            out.append(mode)
    return out


def _assign_or_typed_fail(
    mode: str,
    signals: dict[str, Any],
    *,
    confidence: float,
    tier: SearchModeDecisionTier = "rules",
) -> SearchModeDecision:
    """Name resolved: open 档 assigns, unopened 档 is 具名失败 — the
    cascade never invents a substitute 档 for the request. The ``tier``
    records WHO named it (rules vs the judge) so the audit stays honest
    about the decision's origin."""
    search_mode = normalize_search_mode(mode)
    assert search_mode is not None  # callers resolve before calling
    code = TIER_UNAVAILABLE_CODE.get(search_mode)
    if code is not None:
        signals["requested_mode"] = search_mode
        signals["named_failure"] = code
        return SearchModeDecision(
            search_mode=None,
            confidence=0.0,
            decision_tier="typed_fail",
            signals=signals,
            failure_code=code,
        )
    return SearchModeDecision(
        search_mode=search_mode,
        confidence=confidence,
        decision_tier=tier,
        signals=signals,
    )


def _rules_decision(
    text: str,
    signals: dict[str, Any],
    pinned_mode: str | None,
) -> SearchModeDecision | None:
    """High-confidence namings only: documented pin, then utterance name."""
    if pinned_mode is not None:
        pinned = normalize_search_mode(pinned_mode)
        if pinned is None:
            # A pin outside the closed set is a named EXIT; the rest of
            # the cascade may still resolve the turn (and may still fail
            # typed) — the invalid token itself never becomes a 档.
            signals["pin_exit"] = "invalid_search_mode"
            signals["pin"] = pinned_mode
        else:
            signals["pinned"] = True
            return _assign_or_typed_fail(pinned, signals, confidence=0.95)

    named = _named_modes(text)
    if len(named) == 1:
        signals["tier_naming"] = named[0]
        return _assign_or_typed_fail(named[0], signals, confidence=0.9)
    if len(named) > 1:
        # "A档还是B档?" cannot be resolved by confidence at all: ambiguous
        # by construction, so rules abstain (the judge / clarify decide).
        signals["ambiguous_naming"] = named
    return None


def _last_assigned_mode(snapshots: list[Any] | None) -> SearchMode | None:
    """The 档 of the CURRENT retrieve job: the most recent assigned mode,
    unless a later company-change wipe separated it from the latest
    snapshot (a new job never inherits the old 档)."""
    latest = _latest_snapshot(snapshots)
    latest_company = _normalize_company(
        str((latest or {}).get("company") or "") or None
    )
    for snapshot in reversed(list(snapshots or [])):
        if not isinstance(snapshot, dict):
            continue
        mode = normalize_search_mode(snapshot.get("search_mode"))
        if mode is None or mode in TIER_UNAVAILABLE_CODE:
            # An unopened 档 was never assignable, so a stray token in
            # history does not count as an assignment; keep scanning.
            continue
        snap_company = _normalize_company(
            str(snapshot.get("company") or "") or None
        )
        if latest_company and snap_company and latest_company != snap_company:
            return None
        return mode
    return None


def _inherit_decision(
    text: str,
    snapshots: list[Any] | None,
    signals: dict[str, Any],
) -> SearchModeDecision | None:
    if not text or len(text) > _INHERIT_MAX_LEN:
        return None
    latest = _latest_snapshot(snapshots)
    if not _snapshot_has_research_context(latest):
        return None
    if _company_changed(text, latest):
        return None
    mode = _last_assigned_mode(snapshots)
    if mode is None:
        return None
    signals["inherit"] = True
    signals["inherited_mode"] = mode
    return SearchModeDecision(
        search_mode=mode,
        confidence=0.75,
        decision_tier="inherit",
        signals=signals,
    )


def _try_jev_decide(
    text: str,
    jev_client: JevChoiceProvider | None,
    signals: dict[str, Any],
) -> SearchModeDecision | None:
    """Attempt the gray-zone 档 Choice. On any typed EXIT, record
    ``mode_jev_exit`` and return ``None`` so the cascade falls through to
    inherit / clarify — a failed judge must neither crash the run nor
    open global.

    An absent client is one of those EXITs (``no_jev_client``), mirroring
    ``decide_track``'s gray-zone tier: a key-less boot still reaches this
    tier, and a 具名失败 on the audit row beats a silent branch. The EXIT
    is the only thing written — no label, no confidence, because nothing
    was asked.
    """
    if jev_client is None:
        signals["mode_jev_exit"] = JEV_CHOICE_EXIT_NO_JEV_CLIENT
        return None
    try:
        choice = jev_client.choose(
            utterance=text,
            options=LASTING_RETRIEVE_TIERS,
            criteria=_JEV_MODE_TIER_GLOSSES,
            question_id="search_mode",
            instructions=_JEV_MODE_INSTRUCTIONS,
        )
    except JevChoiceError as exc:
        signals["mode_jev_exit"] = exc.exit_code
        return None
    except Exception as exc:  # noqa: BLE001 — fail-closed at the cascade boundary
        signals["mode_jev_exit"] = JEV_CHOICE_EXIT_UNEXPECTED_ERROR
        signals["mode_jev_error_type"] = type(exc).__name__
        return None

    signals["mode_jev_label"] = choice.label
    signals["mode_jev_confidence"] = choice.confidence
    mode = normalize_search_mode(choice.label)
    if mode is None:
        # ``normalize_search_mode`` is the SSOT total function, so a
        # retired spelling (``B`` / ``C``) still resolves to its lasting
        # 档 and is judged on availability like any other name; only a
        # label it cannot map at all names nothing. The product client
        # raises ``invalid_label`` before returning, so this arm covers a
        # non-conforming injection too.
        signals["mode_jev_exit"] = JEV_CHOICE_EXIT_INVALID_SEARCH_MODE
        return None
    if choice.confidence < _MIN_JEV_CONFIDENCE:
        # An unsure name of an unopened 档 must not open it either: the ban
        # is on SILENT global, and low confidence is precisely a silent
        # answer.
        signals["mode_jev_exit"] = JEV_CHOICE_EXIT_LOW_CONFIDENCE
        return None
    if mode in TIER_UNAVAILABLE_CODE:
        return _assign_or_typed_fail(
            mode, signals, confidence=choice.confidence, tier="jev"
        )
    return SearchModeDecision(
        search_mode=mode,
        confidence=choice.confidence,
        decision_tier="jev",
        signals=signals,
    )


def decide_search_mode(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    jev_client: JevChoiceProvider | None = None,
    pinned_mode: str | None = None,
) -> SearchModeDecision:
    """Return the 档 decision for one grounded turn (tiered cascade).

    The caller (``classify_node``) invokes this ONLY when
    ``decide_track`` already returned ``grounded``; this helper does not
    re-check the track.
    """
    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}

    rules_hit = _rules_decision(text, signals, pinned_mode)
    if rules_hit is not None:
        return rules_hit

    # Peek inherit once before the judge call, exactly like
    # ``decide_track``: if inherit would decide, a judge answer must not
    # steal the inherited 档. The gray zone itself is entered with or
    # without a client, again exactly like ``decide_track`` — a missing
    # judge is named on the audit instead of falling through quietly.
    inherit_hit = _inherit_decision(text, snapshots, dict(signals))
    if text and inherit_hit is None:
        jev_hit = _try_jev_decide(text, jev_client, signals)
        if jev_hit is not None:
            return jev_hit

    if inherit_hit is not None:
        return inherit_hit

    # Residual: no single default 档 (spec: the residual must not open
    # global). Clarify emits the structured option list; Ticket 2 turns it
    # into the choice-window interrupt.
    signals["clarify"] = True
    return SearchModeDecision(
        search_mode=None,
        confidence=0.0,
        decision_tier="clarify",
        signals=signals,
        options=clarify_options(),
    )


def mode_snapshot_patch(
    latest: dict[str, Any] | None,
    search_mode: SearchMode | None,
) -> list[dict[str, Any]]:
    """The append-only snapshot entry that carries the assigned 档.

    The 档 belongs to the retrieve job recorded in ``status_snapshots``
    (the durable channel inherit reads). The patch carries the current
    working context forward so a later company-change wipe (which
    ``snapshot_delta_for_utterance`` emits WITHOUT a 档) is the newest
    entry and blocks inherit by itself. Returns ``[]`` when nothing was
    assigned or the latest snapshot already carries this 档 (no
    duplicate append each turn). Never mutates ``latest``.
    """
    if search_mode is None:
        return []
    if latest and latest.get("search_mode") == search_mode:
        return []
    return [
        {
            "company": str((latest or {}).get("company") or ""),
            "period": str((latest or {}).get("period") or ""),
            "document_ids": list((latest or {}).get("document_ids") or []),
            "search_mode": search_mode,
        }
    ]


def search_mode_update_for_turn(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    jev_client: JevChoiceProvider | None = None,
    pinned_mode: str | None = None,
) -> dict[str, Any]:
    """Single-call seam for the host ``classify_node`` (grounded turns).

    Returns ``{"decision": SearchModeDecision, "audit": <state dict>,
    "snapshot_patch": [...]}`` — the run-scoped audit value plus the
    append-only snapshot entry that carries an ASSIGNED 档 into the
    durable history (inherit source for the next turn). Never mutates
    state or ``snapshots``.
    """
    decision = decide_search_mode(
        messages,
        snapshots=snapshots,
        jev_client=jev_client,
        pinned_mode=pinned_mode,
    )
    return {
        "decision": decision,
        "audit": decision.to_state_dict(),
        "snapshot_patch": mode_snapshot_patch(
            _latest_snapshot(snapshots), decision.search_mode
        ),
    }


def assigned_retrieve_mode(state: Any) -> Any:
    """The 档 the cascade assigned THIS run, read off its state (SC2).

    This module owns the ``search_mode_decision`` channel, so it owns the
    only read of it: ``AgentState.search_mode_decision`` is the single
    source of truth for「本次跑的是哪个档」. Every entry point (utterance,
    pin, choice-window pick, the cascade's own classify output) resolves
    onto a lasting token there, and the audit deliberately holds ``None``
    on clarify and on 具名失败 — so this reader has nothing to send for a
    refused turn and iknow answers with its own default rather than with a
    档 fin invented (SC3 / SC6: no silent ``global``, no silent ``graph``).

    It forwards the audit VERBATIM — no ``normalize_search_mode`` here.
    The audit is post-normalization by construction, so a retired letter
    or a garbage token reaching this seam means a boundary was skipped,
    which :func:`fin.retrieve.port.resolve_search_mode` names as
    ``invalid_search_mode``. Guessing around it here would hide the wiring
    bug and put a 档 behind a request that never chose one — and so would
    swallowing a value that is not the audit mapping into「没送档」, which
    hands the run to iknow's own default instead of naming the skipped
    boundary.

    The value is read PER CALL from the caller's state, never captured at
    compile time: the compiled graph is idempotence-cached and shared by
    every run of the process (see ``_IDEMPOTENCE_CACHE``), so a closed-
    over 档 would leak one run's choice into the next run's wire call.

    ``Any`` on purpose: an unclassified channel value is still forwarded to
    the port, which is the seam that types it.
    """
    audit = (
        state.get("search_mode_decision")
        if isinstance(state, Mapping)
        else None
    )
    if isinstance(audit, Mapping):
        return audit.get("search_mode")
    # An absent channel (the seeded ``{}``, or a state with no such key)
    # genuinely means nothing was assigned; anything else is the value
    # itself, so the port's typed refusal is the exit.
    return audit


def apply_operator_pick(
    decision: dict[str, Any],
    *,
    pick: str,
    snapshots: list[Any] | None = None,
    rejected: list[str] | None = None,
) -> dict[str, Any]:
    """Merge one validated choice-window operator pick into the clarify
    audit dict (Ticket 2 / ADR 0062 HITL).

    ``pick`` is a choice-window option id the CALLER (the graph node)
    already validated against the emitted options — an out-of-list pick
    never reaches here; the node reopens the window instead. A pick of
    an UNOPENED 档 is the 具名失败 ``typed_fail``, by way of
    :func:`_assign_or_typed_fail` itself: the audit names the failure and
    invents no substitute mode.

    Returns ``{"audit": <state dict>, "snapshot_patch": [...]}`` — the
    same seam shape as :func:`search_mode_update_for_turn`, so the
    picked 档 rides the append-only snapshot history (inherit source for
    the next turn). Never mutates ``decision``.
    """
    signals = dict(decision.get("signals") or {})
    signals["resume_pick"] = pick
    if rejected:
        signals["rejected_picks"] = list(rejected)
    # One helper for both arms: ``_assign_or_typed_fail`` takes the
    # ``tier`` it records, so the resume audit names its own origin
    # (``operator_pick``) exactly as the cascade names ``rules`` / ``jev``.
    resolved = _assign_or_typed_fail(
        pick,
        signals,
        confidence=1.0,  # an operator pick is certain by definition
        tier="operator_pick",
    )
    return {
        "audit": resolved.to_state_dict(),
        # ``None`` on a typed fail, which is also the "nothing to append"
        # answer of the patch seam — an unopened pick never rides history.
        "snapshot_patch": mode_snapshot_patch(
            _latest_snapshot(snapshots), resolved.search_mode
        ),
    }


__all__ = [
    "MODE_LABELS",
    "SearchMode",
    "SearchModeDecision",
    "SearchModeDecisionTier",
    "apply_operator_pick",
    "assigned_retrieve_mode",
    "clarify_options",
    "decide_search_mode",
    "mode_snapshot_patch",
    "normalize_search_mode",
    "search_mode_update_for_turn",
]
