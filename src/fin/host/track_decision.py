"""Soft-Track track decision (rules primary for T1).

``decide_track`` chooses ``dialogue`` / ``off_topic`` / ``research``
before the host graph executes a leaf. T1 implements RULES PRIMARY
only:

* high-confidence greeting → ``dialogue`` (``decision_tier=rules``)
* high-confidence off-domain (no 10-Q / corpus / company / period
  cues) → ``off_topic`` (``decision_tier=rules``)
* else → ``research`` (``rules`` when research cues hit;
  ``conditional_policy`` for short/ambiguous utterances like ``q``)

T2 will add ``clarify`` routing. T4 will add the LLM / inherit /
EXIT cascade. ``classify_model`` and ``snapshots`` are accepted as
stub hooks so later tickets can wire them without signature churn.
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


# ----- rules patterns (T1) -----

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

# Tickers / corpus / period cues that keep a turn on research.
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


def decide_track(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    classify_model: Any | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn (rules primary).

    Stub hooks (unused in T1; documented for T3/T4):

    * ``snapshots`` — latest status-snapshot view (T3 inherit).
    * ``classify_model`` — ambiguous LLM classify (T4 cascade).
    """
    # T3/T4 hooks: accepted so call sites can pass them early.
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

    research_hits = _research_signal_hits(text)
    if research_hits:
        signals["research_signals"] = research_hits
        return TrackDecision(
            track="research",
            confidence=0.9,
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

    # Ambiguous / short (e.g. HumanMessage("q")) → research so
    # existing citation-gate bounce suites keep working. T2 may
    # route underspecified filing asks to clarify; T4 may EXIT
    # through LLM → inherit → clarify → conditional_policy.
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
