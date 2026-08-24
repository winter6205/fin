"""Soft-Track track decision cascade (rules → LLM → inherit → clarify → policy).

``decide_track`` chooses ``dialogue`` / ``clarify`` / ``off_topic`` /
``research`` before the host graph executes a leaf.

Tier order (no single global default track):

1. **rules** — high-confidence greeting / off-domain / fully slotted
   research / strong research cues (never replaced by LLM).
2. **llm** — only when ambiguous (inherit/clarify would not already
   decide) and ``classify_model`` is provided; structured
   ``{"track": ...}``; typed EXITs (``invalid_track`` /
   ``schema_parse`` / ``model_error``) fall through.
3. **inherit** — short / underspec follow-up with latest status snapshot
   research context (not across company change).
4. **clarify** — filing-shaped ask missing company and/or period.
5. **conditional_policy** — residual (no signal → ``dialogue``).

``snapshot_delta_for_utterance`` builds the append-only
``status_snapshots`` delta for classify (company change clears
dependents).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

Track = Literal["dialogue", "clarify", "off_topic", "research"]

DecisionTier = Literal[
    "rules",
    "llm",
    "inherit",
    "clarify",
    "conditional_policy",
]

_VALID_TRACKS: frozenset[str] = frozenset(
    {"dialogue", "clarify", "off_topic", "research"}
)

# Canonical ticker spellings for snapshot storage.
_COMPANY_CANON: dict[str, str] = {
    "aapl": "AAPL",
    "apple": "AAPL",
    "msft": "MSFT",
    "microsoft": "MSFT",
    "amzn": "AMZN",
    "amazon": "AMZN",
}

_CLASSIFY_SYSTEM = (
    "Classify the operator utterance into exactly one Soft-Track track. "
    "Reply with JSON only: {\"track\": \"dialogue\"|\"clarify\"|"
    "\"off_topic\"|\"research\"}."
)


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
            track="research",
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
        # Underspec filing asks are ambiguous (LLM → inherit → clarify).
        # Bare ticker/period/tool cues remain high-confidence research.
        if _extract_company(text) and _extract_period(text):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="research",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )
        if re.search(
            r"\b(document_id|retrieve|read_file|list_files)\b",
            text or "",
            re.IGNORECASE,
        ):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="research",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )
        # Company or period alone without metric → research rules
        # (catalog-shaped), not clarify.
        if not _FILING_ASK_RE.search(text or ""):
            signals["research_signals"] = research_hits
            return TrackDecision(
                track="research",
                confidence=0.9,
                decision_tier="rules",
                signals=signals,
            )

    return None


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


def _try_llm_classify(
    text: str,
    classify_model: Any,
    signals: dict[str, Any],
) -> TrackDecision | None:
    """Attempt LLM classify. On EXIT, record ``classify_exit`` and return None."""
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
    # Allow optional fenced JSON.
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

    track = data.get("track")
    if track not in _VALID_TRACKS:
        signals["classify_exit"] = "invalid_track"
        signals["invalid_track"] = track
        return None

    confidence = data.get("confidence", 0.7)
    try:
        confidence_f = float(confidence)
    except (TypeError, ValueError):
        confidence_f = 0.7

    signals["llm_track"] = track
    return TrackDecision(
        track=track,  # type: ignore[arg-type]
        confidence=confidence_f,
        decision_tier="llm",
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
        track="research",
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
    classify_model: Any | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn (tiered cascade)."""
    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}
    latest = _latest_snapshot(snapshots)

    rules_hit = _rules_decision(text, signals)
    if rules_hit is not None:
        return rules_hit

    # Peek inherit/clarify once: if either would decide, skip LLM so a
    # production classify_model cannot steal MUST (2)/(5).
    inherit_hit = _inherit_decision(text, latest, dict(signals))
    clarify_hit = _clarify_decision(text, dict(signals))
    if (
        classify_model is not None
        and text
        and inherit_hit is None
        and clarify_hit is None
    ):
        llm_hit = _try_llm_classify(text, classify_model, signals)
        if llm_hit is not None:
            return llm_hit

    if inherit_hit is not None:
        return inherit_hit

    if clarify_hit is not None:
        return clarify_hit

    # No single global default track (specs/soft-track.md): when the
    # cascade exhausts rules + (optional) LLM + inherit + clarify,
    # resolve to "dialogue" so the residual branch never silently
    # pins every ambiguous utterance onto a single track.
    signals["fallback"] = "dialogue"
    return TrackDecision(
        track="dialogue",
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
