"""Soft-Track track decision (rules + snapshot inherit).

``decide_track`` chooses ``dialogue`` / ``clarify`` / ``off_topic`` /
``research`` before the host graph executes a leaf.

Cascade so far (T1–T3; T4 adds LLM EXITs):

* high-confidence greeting → ``dialogue`` (``rules``)
* high-confidence off-domain → ``off_topic`` (``rules``)
* fully slotted filing ask / strong research cues → ``research``
  (``rules``)
* short follow-up with latest status snapshot research context →
  ``research`` (``inherit``) — does not inherit across company change
* filing-shaped ask missing company and/or period → ``clarify``
* else → ``research`` (``conditional_policy`` for bare ``q``)

``snapshot_delta_for_utterance`` builds the append-only
``status_snapshots`` delta for classify (company change clears
dependents).
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

# Prefer the most specific period token when normalizing.
_PERIOD_SPECIFIC_RE = re.compile(
    r"\b(2022\s*Q3|2023\s*Q[1-3]|Q[1-4]\s*202[23])\b",
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
        # Normalize "Q3 2023" → "2023 Q3"
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
    """Return 0–1 snapshot dicts to append for this utterance.

    Appends when company and/or period appear. Company change yields a
    new snapshot with dependent fields cleared (period only if stated
    in this utterance; ``document_ids`` empty).
    """
    company = _extract_company(text)
    period = _extract_period(text)
    if not company and not period:
        return []

    latest = _latest_snapshot(prior_snapshots) or {}
    prior_company = _normalize_company(
        str(latest.get("company") or "") or None
    )

    if company and prior_company and company != prior_company:
        snap: dict[str, Any] = {
            "company": company,
            "period": period or "",
            "document_ids": [],
        }
        return [snap]

    snap = {
        "company": company or prior_company or "",
        "period": period or str(latest.get("period") or ""),
        "document_ids": list(latest.get("document_ids") or []),
    }
    # Skip no-op append when identical to latest.
    if (
        latest
        and snap.get("company") == latest.get("company")
        and snap.get("period") == latest.get("period")
        and list(snap.get("document_ids") or [])
        == list(latest.get("document_ids") or [])
    ):
        return []
    return [snap]


def decide_track(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    classify_model: Any | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn.

    ``classify_model`` is accepted for T4 wiring; unused until then.
    """
    _ = classify_model

    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}
    latest = _latest_snapshot(snapshots)

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

    # Fully slotted filing ask → research (rules) before inherit/clarify.
    if text and _FILING_ASK_RE.search(text) and not _missing_slots(text):
        signals["filing_ask"] = True
        signals["research_signals"] = _research_signal_hits(text)
        return TrackDecision(
            track="research",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Inherit: short / underspec follow-up with prior research context.
    # Do not inherit across an explicit company change.
    if (
        text
        and _snapshot_has_research_context(latest)
        and not _company_changed(text, latest)
    ):
        missing = _missing_slots(text) if _FILING_ASK_RE.search(text) else []
        short = len(text) <= 48
        follow_up = bool(missing) or (
            short
            and not _FILING_ASK_RE.search(text)
            and not _research_signal_hits(text)
        )
        if follow_up:
            signals["inherit"] = True
            signals["snapshot_company"] = latest.get("company") if latest else None
            signals["snapshot_period"] = latest.get("period") if latest else None
            return TrackDecision(
                track="research",
                confidence=0.75,
                decision_tier="inherit",
                signals=signals,
            )

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
    "snapshot_delta_for_utterance",
]
