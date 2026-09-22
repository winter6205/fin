"""检索档 (search-mode) decision cascade after Soft-Track ``grounded``.

Mirror of :func:`fin.host.track_decision.decide_track`'s tiered cascade
(ADR 0027 / 0062), run ONLY after the track decision already returned
``grounded`` (spec search-mode-host confirmed assumption 2: a
non-grounded track never reaches this cascade and assigns no 档).

Tier order (no silent default — the residual never names C):

1. **rules** — a documented pin/override, or an utterance that EXPLICITLY
   names a 档: the letter adjacent to 档 (``A档`` / ``档 B``) or a CONTEXT
   label adjacent to 档 (查找 / 知识问答 / 综述 / 真数据库). Requiring the
   档 marker keeps a single letter from firing on ordinary English ("A
  apl"-shaped text cannot reach 档 adjacency) and mirrors the closed
   selector shape ``fin.retrieve.tiers`` allows — question prose is never
   scanned for bare 档 words (``fin.host.named_failures`` T6 note). A name
   that cannot resolve uniquely (two different 档 named) stays ambiguous
   and falls through.
2. **llm** — structured classify, only when rules miss **and** inherit
   would not already decide (the same peek shape as ``decide_track``, so
   a production classify model cannot steal an inherited 档). JSON names
   exactly one of ``A|B|C|D``. Typed EXITs
   (``invalid_search_mode`` / ``schema_parse`` / ``model_error`` /
   ``low_confidence``) record ``signals["classify_exit"]`` and fall
   through: never a crash, never a silent-open C.
3. **inherit** — a short follow-up on the same thread that stays in the
   same retrieve job (latest status snapshot still has research context;
   no company-change wipe since the last assigned 档) reuses that 档.
4. **clarify** — the residual: emits the structured option list for the
   operator (the choice-window ``interrupt`` wiring is Ticket 2; this
   ticket only persists the payload data). An unnamed ask keeps
   ``search_mode=None`` — the residual policy must not default to C.

A 档 named but not open (per the ``TIER_UNAVAILABLE_CODE`` SSOT — never
a duplicated enumeration) is a **typed fail** carrying the CONTEXT 具名
失败 code (``tier_not_open`` / ``real_db_not_wired``); it does not invent
a 档 and does not fall through to a different one.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

# The 档 SSOT (closed set + per-档 availability codes) is reused, not
# re-enumerated; ``named_failures`` is the host-side import surface for
# both (see its module docstring).
from fin.host.named_failures import (
    RETRIEVE_TIERS,
    TIER_UNAVAILABLE_CODE,
    RetrieveTier,
)
# Package-internal reuse of the Soft-Track cascade primitives (utterance
# extraction, latest-snapshot context, company-change detection): SSOT
# over duplication — these are the exact predicates ``decide_track``'s
# inherit tier uses.
from fin.host.track_decision import (
    _company_changed,
    _last_human_text,
    _latest_snapshot,
    _normalize_company,
    _snapshot_has_research_context,
)


def _message_text(message: Any) -> str:
    if isinstance(message, AIMessage):
        content = message.content
        if isinstance(content, str):
            return content
        return str(content)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    return str(message)


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
    "rules", "llm", "inherit", "clarify", "typed_fail", "operator_pick"
]

# Operator-visible labels (CONTEXT terms). SSOT-adjacent: these are the
# same four names the retrieve tiers document.
MODE_LABELS: dict[str, str] = {
    "A": "查找",
    "B": "知识问答",
    "C": "综述",
    "D": "真数据库",
}

_LABEL_TO_MODE: dict[str, str] = {label: mode for mode, label in MODE_LABELS.items()}

# Same floor as the track classifier: a below-floor classify is a typed
# EXIT, not a decision.
_MIN_CLASSIFY_CONFIDENCE = 0.6

# Inherit follows up on SHORT text only (mirrors ``decide_track``'s
# 48-char inherit heuristic — same shape, same reason: a long ask is its
# own question, not a continuation).
_INHERIT_MAX_LEN = 48

_CLASSIFY_SYSTEM = (
    "Classify this grounded knowledge question into exactly one 检索档. "
    'Reply with JSON only: {"search_mode": "A"|"B"|"C"|"D"} where '
    "A 查找, B 知识问答, C 综述, D 真数据库."
)

# A 档 is NAMED (high-confidence) only next to the 档 marker — a letter
# or CONTEXT label adjacent to 档. Bare words in question prose are NOT
# scanned (``fin.host.named_failures`` T6 note).
_TIER_NAMING_RE = re.compile(
    r"([ABCD])\s*档|档\s*([ABCD])"
    r"|(查找|知识问答|综述|真数据库)\s*档|档\s*(查找|知识问答|综述|真数据库)",
    re.IGNORECASE,
)


def normalize_search_mode(raw: object) -> SearchMode | None:
    """Map a wire/classify/pin token onto the closed 档 set.

    Deliberately NOT :func:`fin.retrieve.port.resolve_search_mode`:
    that raises at the port seam; the cascade must OBSERVE an
    out-of-set token (typed EXIT) and keep falling through, so it needs
    a total function here. Openness (``TIER_UNAVAILABLE_CODE``) is
    checked by the caller so ``C``/``D`` can route to 具名失败.
    """
    if not isinstance(raw, str):
        return None
    mapped = raw.strip().upper()
    if mapped in RETRIEVE_TIERS:
        return mapped  # type: ignore[return-value]
    return None


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
    nowhere else.
    """
    # The closed set can already carry a lasting token that the label map
    # has not adopted yet (the rename lands in the labels one increment
    # later). An option with no label is not a choice an operator can
    # make, so the window follows the label map and nothing else.
    return [
        {
            "id": mode,
            "label": MODE_LABELS[mode],
            "opened": mode not in TIER_UNAVAILABLE_CODE,
        }
        for mode in RETRIEVE_TIERS
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
) -> SearchModeDecision:
    """Name resolved: open 档 assigns, unopened 档 is 具名失败 — the
    cascade never invents a substitute 档 for the request."""
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
        decision_tier="rules",
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
        # by construction, so rules abstain (classify / clarify decide).
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


def _try_classify(
    text: str,
    classify_model: Any,
    signals: dict[str, Any],
) -> SearchModeDecision | None:
    """Attempt the structured search-mode classify. On any typed EXIT,
    record ``classify_exit`` and return ``None`` so the cascade falls
    through — a failed classify must neither crash nor open C."""
    try:
        raw = classify_model.invoke(
            [
                SystemMessage(content=_CLASSIFY_SYSTEM),
                HumanMessage(content=text),
            ]
        )
    except Exception as exc:  # noqa: BLE001 — typed EXIT, must not crash
        signals["classify_exit"] = "model_error"
        signals["classify_error_type"] = type(exc).__name__
        return None

    payload_text = _message_text(raw).strip()
    # Allow optional fenced JSON (same tolerance as the track classify).
    if payload_text.startswith("```"):
        payload_text = re.sub(r"^```(?:json)?\s*", "", payload_text)
        payload_text = re.sub(r"\s*```$", "", payload_text)
    try:
        data = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        signals["classify_exit"] = "schema_parse"
        return None

    if not isinstance(data, dict):
        signals["classify_exit"] = "schema_parse"
        return None

    mode = normalize_search_mode(data.get("search_mode"))
    if mode is None:
        signals["classify_exit"] = "invalid_search_mode"
        signals["invalid_search_mode"] = data.get("search_mode")
        return None

    confidence = data.get("confidence", 0.7)
    try:
        confidence_f = float(confidence)
    except (TypeError, ValueError):
        confidence_f = 0.7
    if not math.isfinite(confidence_f):
        confidence_f = 0.0
    confidence_f = min(max(confidence_f, 0.0), 1.0)

    signals["llm_mode"] = mode
    if confidence_f < _MIN_CLASSIFY_CONFIDENCE:
        # An unsure name of C must not assign C either: the ban is on
        # SILENT C, and low-confidence is precisely a silent answer.
        signals["classify_exit"] = "low_confidence"
        signals["low_confidence"] = True
        return None
    if mode in TIER_UNAVAILABLE_CODE:
        return _assign_or_typed_fail(mode, signals, confidence=confidence_f)
    return SearchModeDecision(
        search_mode=mode,
        confidence=confidence_f,
        decision_tier="llm",
        signals=signals,
    )


def decide_search_mode(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    classify_model: Any | None = None,
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

    # Peek inherit once before the classify call, exactly like
    # ``decide_track``: if inherit would decide, a production
    # classify_model must not steal the inherited 档.
    inherit_hit = _inherit_decision(text, snapshots, dict(signals))
    if (
        classify_model is not None
        and text
        and inherit_hit is None
    ):
        llm_hit = _try_classify(text, classify_model, signals)
        if llm_hit is not None:
            return llm_hit

    if inherit_hit is not None:
        return inherit_hit

    # Residual: no single default 档 (spec: the residual must not open
    # C). Clarify emits the structured option list; Ticket 2 turns it
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
    classify_model: Any | None = None,
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
        classify_model=classify_model,
        pinned_mode=pinned_mode,
    )
    return {
        "decision": decision,
        "audit": decision.to_state_dict(),
        "snapshot_patch": mode_snapshot_patch(
            _latest_snapshot(snapshots), decision.search_mode
        ),
    }


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
    an UNOPENED 档 is the 具名失败 ``typed_fail`` (mirroring
    :func:`_assign_or_typed_fail`): the audit names the failure and
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
    mode = normalize_search_mode(pick)
    assert mode is not None  # caller validated against the options list
    code = TIER_UNAVAILABLE_CODE.get(mode)
    if code is not None:
        signals["requested_mode"] = mode
        signals["named_failure"] = code
        # NOT ``_assign_or_typed_fail``: that writes ``decision_tier=
        # "rules"``, but a resume-audit typed fail must name its own
        # origin tier (see SearchModeDecisionTier note).
        audit = SearchModeDecision(
            search_mode=None,
            confidence=0.0,
            decision_tier="typed_fail",
            signals=signals,
            failure_code=code,
        ).to_state_dict()
        return {"audit": audit, "snapshot_patch": []}
    audit = SearchModeDecision(
        search_mode=mode,
        confidence=1.0,  # an operator pick is certain by definition
        decision_tier="operator_pick",
        signals=signals,
        options=[],  # the window data served its purpose
        failure_code=None,
    ).to_state_dict()
    return {
        "audit": audit,
        "snapshot_patch": mode_snapshot_patch(
            _latest_snapshot(snapshots), mode
        ),
    }


__all__ = [
    "MODE_LABELS",
    "SearchMode",
    "SearchModeDecision",
    "SearchModeDecisionTier",
    "apply_operator_pick",
    "clarify_options",
    "decide_search_mode",
    "mode_snapshot_patch",
    "normalize_search_mode",
    "search_mode_update_for_turn",
]
