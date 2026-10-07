"""Soft-Track track decision cascade (rules → inherit/clarify → Jev → policy).

``decide_track`` chooses ``dialogue`` / ``clarify`` / ``off_topic`` /
``grounded`` before the host graph executes a leaf.

Evaluation order (no single global default track):

1. **rules** — high-confidence greeting / off-domain / fully slotted
   research / strong research cues; a match returns immediately.
2. **inherit / clarify candidates** — for the remaining utterances,
   check whether the latest status snapshot can resolve a short or
   underspecified follow-up (not across a company change), or whether a
   filing-shaped ask is missing company and/or period. If both candidates
   match, ``inherit`` wins.
3. **jev** — called only for non-empty text when neither candidate
   decides; one TypeSafe Jev closed-set Choice over the four tracks
   (ADR 0080). Every failure path — missing client, typed exit, low
   confidence — resolves to ``clarify`` (fail-closed, no fallback).
4. **conditional_policy** — empty text has no candidate and cannot be
   sent to the judge, so the residual resolves to ``dialogue``.

``snapshot_delta_for_utterance`` builds the append-only
``status_snapshots`` delta for classify (company change clears
dependents).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage, HumanMessage

from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoice,
    JevChoiceError,
)

Track = Literal["dialogue", "clarify", "off_topic", "grounded"]

DecisionTier = Literal[
    "rules",
    "jev",
    "inherit",
    "clarify",
    "conditional_policy",
]

# The Jev gray-zone closed set (ADR 0080): the four Soft-Track leaves.
_JEV_TRACK_OPTIONS: tuple[str, ...] = ("dialogue", "clarify", "off_topic", "grounded")
_VALID_TRACKS: frozenset[str] = frozenset(_JEV_TRACK_OPTIONS)
# Checkpoint / classify wire alias (ADR 0061).
_TRACK_ALIASES: dict[str, Track] = {"research": "grounded"}
_MIN_JEV_CONFIDENCE = 0.6

# Live smoke: the client's generic default instructions misjudge
# off-domain asks as grounded; leaf semantics must come with the question.
# The four leaf bodies are the closed set's per-option criteria (the wire
# slot that describes ONE option), and the instruction text is only the
# task — a prompt that pastes the leaves into prose is a second copy of
# them, and only the criteria reaches the judge's per-option scoring.
# The taught names are JOINED FROM THE CLOSED SET (mirroring
# ``search_mode_decision._JEV_MODE_INSTRUCTIONS``): a leaf added to
# ``_JEV_TRACK_OPTIONS`` without a prose re-spelling cannot silently stop
# being taught, because this text cannot drift from the tuple.
_JEV_TRACK_INSTRUCTIONS: str = (
    "Classify the operator utterance into exactly one Soft-Track track: "
    + ", ".join(_JEV_TRACK_OPTIONS)
    + ". Answer with the leaf name alone."
)

_JEV_TRACK_LEAF_CRITERIA: dict[str, str] = {
    "dialogue": "conversational reply needing no filing data",
    "clarify": "a filing-shaped ask missing company or period",
    "off_topic": "outside US-filing financial research",
    "grounded": "a research ask answerable from SEC filings",
}

# Canonical ticker spellings for snapshot storage.
_COMPANY_CANON: dict[str, str] = {
    "aapl": "AAPL",
    "apple": "AAPL",
    "msft": "MSFT",
    "microsoft": "MSFT",
    "amzn": "AMZN",
    "amazon": "AMZN",
}

@dataclass(frozen=True)
class TrackDecision:
    """Auditable Soft-Track decision for one run.

    ``signals`` is a machine-readable dict (not a free-form dump).
    """

    track: Track
    confidence: float
    decision_tier: DecisionTier
    signals: dict[str, Any] = field(default_factory=dict)


class JevChoiceProvider(Protocol):
    """The gray-zone seam: a closed-set Choice ``choose`` (ADR 0080).

    ``JevChoiceClient.choose`` is the product implementation; tests
    inject the Fake from ``tests/host/jev_fake.py``.
    """

    def choose(
        self,
        *,
        utterance: str,
        options: Sequence[str],
        criteria: Mapping[str, str],
        question_id: str = "track",
        instructions: str | None = None,
    ) -> JevChoice: ...


def normalize_track(raw: object) -> Track | None:
    """Map a wire/checkpoint track string onto the live enum (ADR 0061)."""
    if not isinstance(raw, str):
        return None
    mapped = _TRACK_ALIASES.get(raw, raw)
    if mapped in _VALID_TRACKS:
        return mapped  # type: ignore[return-value]
    return None


# ----- rules patterns -----

_GREETING_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|howdy|yo|greetings|"
    r"good\s+(morning|afternoon|evening)|"
    r"how\s+are\s+you(?:\s+doing)?|"
    r"what'?s\s+up|"
    r"thanks|thank\s+you"
    r")[\s!.?]*$",
    re.IGNORECASE,
)

_COMPANY_RE = re.compile(
    r"\b(AAPL|MSFT|AMZN|Apple|Microsoft|Amazon)\b",
    re.IGNORECASE,
)

_PERIOD_RE = re.compile(
    r"\b("
    r"2022\s*Q3|2023\s*Q[1-3]|"
    r"Q[1-4]\s*202[23]|"
    r"2022|2023|"
    r"Q[1-4]"
    r")\b",
    re.IGNORECASE,
)

_PERIOD_SPECIFIC_RE = re.compile(
    r"\b(2022\s*Q3|2023\s*Q[1-3]|Q[1-4]\s*202[23])\b",
    re.IGNORECASE,
)

_FILING_ASK_RE = re.compile(
    r"\b("
    r"revenue|net\s+sales|operating\s+income|EPS|earnings|"
    r"gross\s+margin|cost\s+of\s+sales|"
    r"10-?Q|filing|filings"
    r")\b",
    re.IGNORECASE,
)

_RESEARCH_SIGNAL_RE = re.compile(
    r"\b("
    r"AAPL|MSFT|AMZN|Apple|Microsoft|Amazon|"
    r"10-?Q|filing|filings|SEC|"
    r"revenue|net\s+sales|operating\s+income|EPS|"
    r"document_id|retrieve|read_source|list_files|"
    r"2022|2023|Q[1-4]"
    r")\b",
    re.IGNORECASE,
)

_OFF_DOMAIN_RE = re.compile(
    r"\b("
    r"weather|forecast|temperature|"
    r"recipe|cook(?:ing)?|ingredient|"
    r"sports?|score|football|basketball|soccer|"
    r"capital\s+of|translate|joke|poem|"
    r"movie|lyrics|horoscope"
    r")\b",
    re.IGNORECASE,
)

# Enterprise-material cue (spec ``enterprise-evidence-contract`` E2 /
# ADR 0090): the utterance names an authorized non-financial document
# ("corporate travel expense policy"). fin is a knowledge-QA entry
# point for ANY ingested material, so such an ask is a grounded
# knowledge-QA request, never a gray-zone clarify — filing vocabulary
# (company ticker + period, SEC metrics) is not the only route into
# the retrieve path.
_ENTERPRISE_MATERIAL_RE = re.compile(
    r"\b(?:(?:corporate|company|travel|expense|employee)\s+){1,3}"
    r"polic(?:y|ies)\b",
    re.IGNORECASE,
)


def _last_human_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content.strip()
            return str(content).strip()
    return ""


def _research_signal_hits(text: str) -> list[str]:
    return [m.group(0) for m in _RESEARCH_SIGNAL_RE.finditer(text)]


def _normalize_company(raw: str | None) -> str | None:
    if not raw:
        return None
    return _COMPANY_CANON.get(raw.strip().lower(), raw.strip().upper())


def _extract_company(text: str) -> str | None:
    match = _COMPANY_RE.search(text or "")
    if not match:
        return None
    return _normalize_company(match.group(1))


def _extract_period(text: str) -> str | None:
    specific = _PERIOD_SPECIFIC_RE.search(text or "")
    if specific:
        token = re.sub(r"\s+", " ", specific.group(1)).strip().upper()
        swapped = re.match(r"^(Q[1-4])\s+(202[23])$", token, re.IGNORECASE)
        if swapped:
            return f"{swapped.group(2)} {swapped.group(1).upper()}"
        return token
    match = _PERIOD_RE.search(text or "")
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip().upper()


def _missing_slots(text: str) -> list[str]:
    missing: list[str] = []
    if not _extract_company(text):
        missing.append("company")
    if not _extract_period(text):
        missing.append("period")
    return missing


def _latest_snapshot(snapshots: list[Any] | None) -> dict[str, Any] | None:
    if not snapshots:
        return None
    latest = snapshots[-1]
    if not isinstance(latest, dict):
        return None
    return latest


def _snapshot_has_research_context(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    if snapshot.get("company") or snapshot.get("period"):
        return True
    docs = snapshot.get("document_ids") or []
    return bool(docs)


def _company_changed(text: str, snapshot: dict[str, Any] | None) -> bool:
    new_company = _extract_company(text)
    if not new_company or not snapshot:
        return False
    prior = _normalize_company(str(snapshot.get("company") or "") or None)
    return bool(prior) and new_company != prior


def snapshot_delta_for_utterance(
    text: str,
    prior_snapshots: list[Any] | None,
) -> list[dict[str, Any]]:
    """Return 0–1 snapshot dicts to append for this utterance."""
    company = _extract_company(text)
    period = _extract_period(text)
    if not company and not period:
        return []

    latest = _latest_snapshot(prior_snapshots) or {}
    prior_company = _normalize_company(
        str(latest.get("company") or "") or None
    )

    if company and prior_company and company != prior_company:
        return [
            {
                "company": company,
                "period": period or "",
                "document_ids": [],
            }
        ]

    snap = {
        "company": company or prior_company or "",
        "period": period or str(latest.get("period") or ""),
        "document_ids": list(latest.get("document_ids") or []),
    }
    if (
        latest
        and snap.get("company") == latest.get("company")
        and snap.get("period") == latest.get("period")
        and list(snap.get("document_ids") or [])
        == list(latest.get("document_ids") or [])
    ):
        return []
    return [snap]


def _rules_decision(text: str, signals: dict[str, Any]) -> TrackDecision | None:
    """High-confidence rules only. ``None`` means ambiguous."""
    if text and _GREETING_RE.match(text):
        signals["greeting"] = True
        return TrackDecision(
            track="dialogue",
            confidence=0.95,
            decision_tier="rules",
            signals=signals,
        )

    if text and _OFF_DOMAIN_RE.search(text):
        signals["off_domain"] = True
        return TrackDecision(
            track="off_topic",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    if text and _FILING_ASK_RE.search(text) and not _missing_slots(text):
        signals["filing_ask"] = True
        signals["research_signals"] = _research_signal_hits(text)
        return TrackDecision(
            track="grounded",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Strong research cues without being an underspec filing ask:
    # company+period, or tool/document_id mentions.
    research_hits = _research_signal_hits(text)
    if research_hits and not (
        text and _FILING_ASK_RE.search(text) and _missing_slots(text)
    ):
        # Underspec filing asks are left for the inherit/clarify checks
        # before Jev. Bare ticker/period/tool cues remain high-confidence
        # research.
        if _extract_company(text) and _extract_period(text):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="grounded",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )
        if re.search(
            r"\b(document_id|retrieve|read_source|list_files)\b",
            text or "",
            re.IGNORECASE,
        ):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="grounded",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )
        # Company or period alone without metric → research rules
        # (catalog-shaped), not clarify.
        if not _FILING_ASK_RE.search(text or ""):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="grounded",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )

    # Enterprise-material ask (E2): a question anchored to a named
    # policy document is grounded even when it carries no filing
    # signal at all. Placed LAST in the rules tier so every utterance
    # an existing rule already decided keeps its prior route; only
    # asks that used to fail-close to the gray-zone clarify enter the
    # grounded knowledge-QA path here.
    if text and _ENTERPRISE_MATERIAL_RE.search(text):
        signals["enterprise_material"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    return None


def _gray_zone_clarify(
    signals: dict[str, Any],
    *,
    confidence: float = 0.0,
) -> TrackDecision:
    return TrackDecision(
        track="clarify",
        confidence=confidence,
        decision_tier="clarify",
        signals=signals,
    )


def _try_jev_classify(
    text: str,
    jev_client: JevChoiceProvider | None,
    signals: dict[str, Any],
) -> TrackDecision:
    """Gray-zone tier: one Jev closed-set Choice (ADR 0080).

    Fail-closed by design: a missing client, any typed ``JevChoiceError``
    exit, or sub-threshold confidence all resolve to ``clarify`` — the
    cascade never falls through to a MiniMax JSON classifier (removed)
    and never silently picks ``grounded`` or ``dialogue`` for an
    utterance the judge was supposed to decide.
    """
    if jev_client is None:
        signals["jev_exit"] = JEV_CHOICE_EXIT_NO_JEV_CLIENT
        return _gray_zone_clarify(signals)
    try:
        choice = jev_client.choose(
            utterance=text,
            options=_JEV_TRACK_OPTIONS,
            criteria=_JEV_TRACK_LEAF_CRITERIA,
            question_id="track",
            instructions=_JEV_TRACK_INSTRUCTIONS,
        )
    except JevChoiceError as exc:
        signals["jev_exit"] = exc.exit_code
        return _gray_zone_clarify(signals)
    except Exception as exc:  # noqa: BLE001 — fail-closed at the cascade boundary
        signals["jev_exit"] = JEV_CHOICE_EXIT_UNEXPECTED_ERROR
        signals["jev_error_type"] = type(exc).__name__
        return _gray_zone_clarify(signals)

    # ``choice.label`` is in the closed set by the client contract
    # (``invalid_label`` raises before returning), so no remap here.
    signals["jev_label"] = choice.label
    signals["jev_confidence"] = choice.confidence
    if choice.confidence < _MIN_JEV_CONFIDENCE:
        signals["jev_exit"] = JEV_CHOICE_EXIT_LOW_CONFIDENCE
        return _gray_zone_clarify(signals, confidence=choice.confidence)
    return TrackDecision(
        track=choice.label,  # type: ignore[arg-type]
        confidence=choice.confidence,
        decision_tier="jev",
        signals=signals,
    )


def _inherit_decision(
    text: str,
    latest: dict[str, Any] | None,
    signals: dict[str, Any],
) -> TrackDecision | None:
    if not text or not _snapshot_has_research_context(latest):
        return None
    if _company_changed(text, latest):
        return None
    missing = _missing_slots(text) if _FILING_ASK_RE.search(text) else []
    short = len(text) <= 48
    follow_up = bool(missing) or (
        short
        and not _FILING_ASK_RE.search(text)
        and not _research_signal_hits(text)
    )
    if not follow_up:
        return None
    signals["inherit"] = True
    signals["snapshot_company"] = latest.get("company") if latest else None
    signals["snapshot_period"] = latest.get("period") if latest else None
    return TrackDecision(
        track="grounded",
        confidence=0.75,
        decision_tier="inherit",
        signals=signals,
    )


def _clarify_decision(text: str, signals: dict[str, Any]) -> TrackDecision | None:
    if not text or not _FILING_ASK_RE.search(text):
        return None
    missing = _missing_slots(text)
    if not missing:
        return None
    signals["filing_ask"] = True
    signals["missing_slots"] = missing
    return TrackDecision(
        track="clarify",
        confidence=0.85,
        decision_tier="clarify",
        signals=signals,
    )


def decide_track(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    jev_client: JevChoiceProvider | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn (tiered cascade)."""
    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}
    latest = _latest_snapshot(snapshots)

    rules_hit = _rules_decision(text, signals)
    if rules_hit is not None:
        return rules_hit

    # Peek inherit/clarify once: if either would decide, skip the judge
    # so it cannot steal MUST (2)/(5).
    inherit_hit = _inherit_decision(text, latest, dict(signals))
    clarify_hit = _clarify_decision(text, dict(signals))
    if text and inherit_hit is None and clarify_hit is None:
        # Gray zone (ADR 0080): the judge owns this utterance — including
        # a key-less boot, which fail-closes to clarify rather than
        # resolving to a default track.
        return _try_jev_classify(text, jev_client, signals)

    if inherit_hit is not None:
        return inherit_hit

    if clarify_hit is not None:
        return clarify_hit

    # No single global default track (specs/soft-track.md): only an
    # utterance the judge cannot be asked about (empty text) reaches
    # this residual, which resolves to "dialogue".
    signals["fallback"] = "dialogue"
    return TrackDecision(
        track="dialogue",
        confidence=0.4,
        decision_tier="conditional_policy",
        signals=signals,
    )


__all__ = [
    "DecisionTier",
    "JevChoiceProvider",
    "Track",
    "TrackDecision",
    "decide_track",
    "normalize_track",
    "snapshot_delta_for_utterance",
]
