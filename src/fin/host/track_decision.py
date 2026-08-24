"""Soft-Track track decision (rules primary; clarify slots in T2).

``decide_track`` chooses ``dialogue`` / ``clarify`` / ``off_topic`` /
``research`` before the host graph executes a leaf.

T1 + T2 rules primary:

* high-confidence greeting → ``dialogue`` (``decision_tier=rules``)
* filing-shaped ask missing company and/or period → ``clarify``
* clear research cues (company+period or strong corpus signals) →
  ``research``
* high-confidence off-domain → ``off_topic``
* else → ``research`` (``conditional_policy`` for short/ambiguous
  utterances like ``q`` — must stay research for default graph suites)

T3 will wire snapshot inherit. T4 will add the LLM / EXIT cascade.
``classify_model`` and ``snapshots`` are accepted as stub hooks so
later tickets can wire them without signature churn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage

Track = Literal["dialogue", "clarify", "off_topic", "research"]

DecisionTier = Literal[
    "rules",
    "llm",
    "inherit",
    "clarify",
    "conditional_policy",
]


@dataclass(frozen=True)
class TrackDecision:
    """Auditable Soft-Track decision for one run.

    ``signals`` is a machine-readable dict (not a free-form dump).
    """

    track: Track
    confidence: float
    decision_tier: DecisionTier
    signals: dict[str, Any] = field(default_factory=dict)


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

# Metric / filing-shaped asks that need grounded company+period slots.
# Deliberately excludes bare "what was" so off-domain questions are
# not mis-routed to clarify.
_FILING_ASK_RE = re.compile(
    r"\b("
    r"revenue|net\s+sales|operating\s+income|EPS|earnings|"
    r"gross\s+margin|cost\s+of\s+sales|"
    r"10-?Q|filing|filings"
    r")\b",
    re.IGNORECASE,
)

# Tickers / corpus / period / tool cues that keep a turn on research
# when slots are complete (or the ask is catalog/tool shaped).
_RESEARCH_SIGNAL_RE = re.compile(
    r"\b("
    r"AAPL|MSFT|AMZN|Apple|Microsoft|Amazon|"
    r"10-?Q|filing|filings|SEC|"
    r"revenue|net\s+sales|operating\s+income|EPS|"
    r"document_id|retrieve|read_file|list_files|"
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


def _missing_slots(text: str) -> list[str]:
    missing: list[str] = []
    if not _COMPANY_RE.search(text):
        missing.append("company")
    if not _PERIOD_RE.search(text):
        missing.append("period")
    return missing


def decide_track(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    classify_model: Any | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn (rules primary).

    Stub hooks (unused until T3/T4; documented for later wiring):

    * ``snapshots`` — latest status-snapshot view (T3 inherit).
    * ``classify_model`` — ambiguous LLM classify (T4 cascade).
    """
    _ = snapshots
    _ = classify_model

    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}

    if text and _GREETING_RE.match(text):
        signals["greeting"] = True
        return TrackDecision(
            track="dialogue",
            confidence=0.95,
            decision_tier="rules",
            signals=signals,
        )

    # Off-domain before clarify so weather/etc. never become clarify.
    if text and _OFF_DOMAIN_RE.search(text):
        signals["off_domain"] = True
        return TrackDecision(
            track="off_topic",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Filing-shaped ask missing company and/or period → clarify
    # BEFORE generic research-signal promotion, so underspecified
    # metric asks do not bind tools.
    if text and _FILING_ASK_RE.search(text):
        missing = _missing_slots(text)
        if missing:
            signals["filing_ask"] = True
            signals["missing_slots"] = missing
            return TrackDecision(
                track="clarify",
                confidence=0.85,
                decision_tier="rules",
                signals=signals,
            )

    research_hits = _research_signal_hits(text)
    if research_hits:
        signals["research_signals"] = research_hits
        return TrackDecision(
            track="research",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Ambiguous / short (e.g. HumanMessage("q")) → research so
    # existing citation-gate bounce suites keep working.
    signals["fallback"] = "research"
    return TrackDecision(
        track="research",
        confidence=0.4,
        decision_tier="conditional_policy",
        signals=signals,
    )


__all__ = [
    "DecisionTier",
    "Track",
    "TrackDecision",
    "decide_track",
]
