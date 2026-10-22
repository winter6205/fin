"""Soft-Track track decision cascade (rules → inherit → Jev → policy).

``decide_track`` chooses ``dialogue`` / ``clarify`` / ``off_topic`` /
``grounded`` before the host graph executes a leaf.

Evaluation order (no single global default track):

1. **rules** — a greeting returns dialogue. Otherwise a positive
   count of already-retrievable authorized materials returns
   grounded. Word lists (off-domain, fully slotted research,
   enterprise phrasing, an attachment named with an upload frame, a
   Chinese catalog-discovery question, an operator-delegated scope)
   break ties only when that count is zero.
2. **inherit** — a latest status snapshot can resolve a short or
   underspecified follow-up when it does not cross a company change.
3. **jev** — called for non-empty text not decided by rules or inherit;
   one TypeSafe Jev closed-set Choice over the four tracks (ADR 0080).
   A valid ``clarify`` choice remains a canonical task clarification;
   missing financial slots alone never short-circuit to it. Missing,
   malformed, low-confidence or failed Jev choices leave the utterance
   on the retrieval lane as ``grounded`` with tier ``jev_fallback`` and
   a recorded ``jev_exit`` (GE-1: judge down is not a missing user slot).
4. **conditional_policy** — empty text cannot be sent to the judge, so
   the residual resolves to ``dialogue``.

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

from fin.host.question.jev_choice import (
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
    "jev_fallback",
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

# Bare "show me what exists" ask ("看看", "看一下", "有哪些资料"). It is a
# catalog discovery request with no subject named, so it belongs on the
# overview route; the track cascade has no rule for it and would otherwise
# fall to the ``conditional_policy`` dialogue residual.
#
# Anchored to the WHOLE utterance on purpose: an unanchored scan would fire
# on "看看第三季度的收入" and turn an ordinary question into a catalog dump.
# Leading/trailing whitespace and trailing CJK/ASCII punctuation are typing
# noise, not content.
BARE_CATALOG_VIEW_RE = re.compile(
    r"\A\s*"
    r"(?:看看|看一下|看一下资料|瞧瞧|瞅瞅|浏览一下|先看看|帮我看看|有什么资料|有哪些资料)"
    r"[\s。！？，、；：,.!?;:]*\Z"
)


def is_bare_catalog_view(text: str) -> bool:
    """True when this utterance is nothing but a request to look.

    The whole-utterance anchor is the contract: a false positive here
    would silently turn a knowledge question into a catalog listing, so
    an ask that also names a subject must not match. The predicate lives
    in this module (not ``route_intent``) because it is a routing RULE
    and the rule cascade owns the signal that consumes it.
    """
    if not isinstance(text, str):
        return False
    return BARE_CATALOG_VIEW_RE.match(text) is not None


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
    "dialogue": "dialogue needing no authorized material",
    "clarify": "an ask missing a slot",
    "off_topic": "off-topic for this dialogue",
    "grounded": "an answer from authorized materials",
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

# Attachment-as-source cue. The answer is to be read out of material
# uploaded in this thread. A bare word is not a source: 附件 alone is
# the output of a 起草/写 ask ("作为邮件附件发送"), so the noun counts
# only under an upload frame (上传 / 附加 / 附上), a source frame over
# a demonstrative ("根据这份文件"), or the demonstrative itself
# ("这份费用政策"). The CJK window cannot cross punctuation and must
# end on the document noun, so 这条借阅台账 and 这个季度 do not match.
# This routes the turn only; it does not prepare the attachment.
_ATTACHMENT_SOURCE_RE = re.compile(
    r"(?:附加文件|附(?:加|上)(?:的)?附件|上传(?:的)?(?:文件|文档|材料|报告|表格|数据|附件))"
    r"|"
    r"\b(?:attached|uploaded)\s+"
    r"(?:file|document|doc|report|spreadsheet|csv|xlsx|pdf|pptx)\b"
    r"|"
    r"(?:根据|按照|依据|基于|依照|引用|摘自|出自|参见|据)\s*"
    r"(?:这|那)\s*(?:份|个|篇|张|条)\s*[^\s，。、；：？?！!]{0,8}?"
    r"(?:文件|文档|材料|报告|表格|数据|政策|规定|说明|手册|清单|附件|记录)"
    r"|"
    r"(?:这|那)\s*(?:份|个|篇|张|条)\s*[^\s，。、；：？?！!]{0,8}?"
    r"(?:文件|文档|材料|报告|表格|数据|政策|规定|说明|手册|清单|附件)",
    re.IGNORECASE,
)


# Chinese catalog-discovery cue. The operator asking what the 知识库
# HOLDS ("仓库有数据吗", "库里有什么", "有哪些公司") is not a gray-zone
# ask missing a slot — it is the one question ``list_files`` answers.
# Routing it to ``clarify`` dead-ends: the clarify leaf has no tools,
# so the model can only restate the slots it is missing, which is
# precisely the turn the operator asked to skip. Grounded routes the
# turn into the existing ToolNode / ``list_files`` seam, so the
# catalog is enumerated instead of described.
#
# The window is a corpus noun (仓库 / 资料库 / 知识库 / 文档库) or an
# explicit inventory predicate (有哪些 / 都有什么 / 有数据吗 / 有资料吗),
# paired so a bare 有没有 elsewhere is not swept in. No hardcoded
# ticker, period or document id — the catalog is read at model call
# time, never frozen into the route.
_CORPUS_INVENTORY_RE = re.compile(
    r"(?:仓库|资料库|知识库|文档库|数据库)[^，。、；：？?！!\s]{0,12}?"
    r"(?:有|都有|里面|中有)"
    r"|"
    r"(?:有没有|是否有|有没)"
    r"(?:数据|资料|文档|文件|内容|东西|报告)"
    r"|"
    r"(?:有什么|有啥|都有什么|有些什么)"
    # 「有哪些」 must name a catalog noun, so 「有哪些风险」 is ordinary
    # question prose rather than a request to enumerate the corpus.
    r"|"
    r"(?:有哪些|列一下|列出|都有哪(?:些|个))"
    r"[^，。、；：？?！!\s]{0,6}?"
    r"(?:公司|企业|发行人|报告|文件|文档|资料|材料|数据|表单|公告)"
)


# Operator-delegated scope. "随便" / "你随便查" / "帮我查判官公司的"
# hand the DECISION back to fin instead of naming a slot: the operator
# has explicitly said the scope is fin's to pick. Treating that as a
# missing slot produces the generic company/period loop the operator
# reported — the question never moves, because the delegation is the
# very thing the loop keeps asking for again.
#
# Two independent cues, OR-ed so neither shape alone has to carry the
# arm: a bare delegation adverb (the reply to a clarify turn) and a
# delegated retrieve verb. The delegate frame must ride an actual
# retrieval verb, so ordinary prose that merely contains 随便 is not
# swept into research; 查 is required because that is the verb the
# operator uses for the retrieve path in both Chinese and the demo
# corpus.
_DELEGATED_SCOPE_RE = re.compile(
    # (a) A bare delegation adverb, alone or with one retrieval verb —
    # the reply an operator sends when fin asked for a slot and they
    # decline to choose ("随便", "你随便查", "都行，你看着办").
    r"^\s*[\s]*"
    r"(?:你|您)?\s*"
    r"(?:随便|隨便|任意|都行|无所谓|都可以|你看着(?:办|来)|你决定|由你决定)"
    # The verb slot tolerates a verb-aspect suffix ("随便查一下", "随便看看")
    # because the delegation adverb, not the suffix, is what makes the ask a
    # delegation; requiring the bare verb would miss the acceptance phrase.
    r"(?:[，,。.！!？?\s]*(?:查|搜|检索|找|看|看看|研究|分析|处理|来)(?:一?下|一?遍|一?次)?)?"
    r"\s*$"
    # (b) A delegated retrieve verb — but ONLY when the utterance also
    # carries the unfinished-scope marker that names what is being
    # delegated. Without this second requirement 「帮我查一下revenue」
    # would read as a delegation, and a filing ask that genuinely has
    # both slots missing would stop clarifying. The marker is either
    # the delegation adverb, or a trailing 的 that leaves the scope
    # deliberately open ("帮我查判官公司的").
    r"|"
    r"(?:帮我|替我|给我|帮忙|劳驾|你帮我|您帮我)"
    r"\s*"
    r"(?:查|搜|检索|找|看|看看|研究|分析|处理)"
    r"[^，。、；：？?！!\n]{0,40}?"
    r"(?:的\s*$|(?:随便|隨便|任意|都行|你决定|你看着(?:办|来)))"
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


def company_of_utterance(text: str) -> str | None:
    """The canonical company this utterance names, if any.

    Public seam for the task runtime, so company identity stays decided
    by this module's single extractor rather than a second pattern
    elsewhere.
    """
    return _extract_company(text)


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
        # Incomplete filing asks fall through to inherit/Jev; a missing
        # company or period is not a pre-retrieval clarify rule.
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
    # signal at all. Placed after the research arms and before the
    # attachment arm, so every utterance an existing rule already
    # decided keeps its prior route; only asks that used to fail-close
    # to the gray-zone clarify enter the grounded knowledge-QA path here.
    if text and _ENTERPRISE_MATERIAL_RE.search(text):
        signals["enterprise_material"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # After the off-domain and enterprise arms, so a weather ask that
    # names an attachment stays off_topic, and an enterprise-policy ask
    # keeps its own signal.
    if text and _ATTACHMENT_SOURCE_RE.search(text):
        signals["attachment_source"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Catalog discovery: "does the 知识库 hold anything / which companies
    # are in it" is answered by ``list_files``, so it enters the
    # grounded ToolNode lane rather than the tool-less clarify leaf.
    # Placed AFTER every arm above so an utterance an existing rule
    # already decided (off-domain, greeting, attachment) keeps its prior
    # route; only the turns that used to fall into the gray zone — and
    # fail-close to clarify without a judge — reach grounded here.
    if text and _CORPUS_INVENTORY_RE.search(text):
        signals["corpus_inventory"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.9,
            decision_tier="rules",
            signals=signals,
        )

    # Delegated scope: the operator handed the choice of company /
    # period back to fin ("随便", "你随便查", "帮我查判官公司的"). The
    # clarify tier would ask for exactly the slot the operator just
    # delegated, so this arm routes into discovery instead. A genuine
    # filing ask that merely mentions a company still resolves through
    # the research arms above; only an explicit delegation frame
    # reaches here.
    if text and _DELEGATED_SCOPE_RE.search(text):
        signals["delegated_scope"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.85,
            decision_tier="rules",
            signals=signals,
        )

    # Bare "let me see what exists" ask. Last, so every arm above keeps its
    # prior route; a bare view of the corpus is discovery, not a knowledge
    # question the gray-zone judge should own.
    if text and is_bare_catalog_view(text):
        signals["bare_catalog_view"] = True
        return TrackDecision(
            track="grounded",
            confidence=0.85,
            decision_tier="rules",
            signals=signals,
        )

    return None


def _gray_zone_retrieve(
    signals: dict[str, Any],
    *,
    confidence: float = 0.0,
) -> TrackDecision:
    """The judge-down residual: the utterance retrieves (GE-1).

    A Jev exit means the judge decided nothing — it is not a missing
    user condition, so the turn stays on the retrieval lane and
    ``signals["jev_exit"]`` keeps recording WHY no decision was made
    (specs/qa-chain-grounded-experience.md GE-1: every receipt records
    rule/jev/fallback).
    """
    return TrackDecision(
        track="grounded",
        confidence=confidence,
        decision_tier="jev_fallback",
        signals=signals,
    )


def _try_jev_classify(
    text: str,
    jev_client: JevChoiceProvider | None,
    signals: dict[str, Any],
) -> TrackDecision:
    """Gray-zone tier: one Jev closed-set Choice (ADR 0080).

    A missing client, any typed ``JevChoiceError`` exit, or
    sub-threshold confidence means the judge decided nothing, so the
    utterance falls back to the retrieval lane (GE-1) — never to a
    MiniMax JSON classifier (removed) and never to a silent
    ``dialogue``/``off_topic`` pick. A usable explicit ``clarify`` label
    remains available to the canonical task runtime; financial-slot
    details are recorded only after Jev chooses that track.
    """
    if jev_client is None:
        signals["jev_exit"] = JEV_CHOICE_EXIT_NO_JEV_CLIENT
        return _gray_zone_retrieve(signals)
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
        return _gray_zone_retrieve(signals)
    except Exception as exc:  # noqa: BLE001 — contained at the cascade boundary
        signals["jev_exit"] = JEV_CHOICE_EXIT_UNEXPECTED_ERROR
        signals["jev_error_type"] = type(exc).__name__
        return _gray_zone_retrieve(signals)

    # ``choice.label`` is in the closed set by the client contract
    # (``invalid_label`` raises before returning), so no remap here.
    signals["jev_label"] = choice.label
    signals["jev_confidence"] = choice.confidence
    if choice.confidence < _MIN_JEV_CONFIDENCE:
        signals["jev_exit"] = JEV_CHOICE_EXIT_LOW_CONFIDENCE
        return _gray_zone_retrieve(signals, confidence=choice.confidence)
    if choice.label == "clarify" and _FILING_ASK_RE.search(text):
        missing = _missing_slots(text)
        if missing:
            # The Jev choice owns the route. Preserve the host-recognized
            # slot detail for Task Runtime's canonical pending question.
            signals["filing_ask"] = True
            signals["missing_slots"] = missing
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


def _retrievable_material_count(facts: Mapping[str, Any] | None) -> int:
    """The integer count, or zero when the caller passed no signal.

    A missing argument, a non-mapping, or a non-integer count must not
    raise and must not count as materials. The mapping is not written.
    """
    if not isinstance(facts, Mapping):
        return 0
    raw = facts.get("retrievable_count", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _bound_material_grounding(
    text: str,
    retrievable_count: int,
    signals: dict[str, Any],
) -> TrackDecision | None:
    """Ground when authorized material is already retrievable.

    Word lists are the tie-break only when this count is zero. A
    greeting stays dialogue: it is not a question for those materials.
    """
    if retrievable_count <= 0:
        return None
    if text and _GREETING_RE.match(text):
        return None
    signals["retrievable_materials"] = retrievable_count
    return TrackDecision(
        track="grounded",
        confidence=0.9,
        decision_tier="rules",
        signals=signals,
    )


def decide_track(
    messages: list[BaseMessage],
    *,
    snapshots: list[Any] | None = None,
    jev_client: JevChoiceProvider | None = None,
    bound_materials: Mapping[str, Any] | None = None,
) -> TrackDecision:
    """Return the Soft-Track decision for this turn (tiered cascade).

    ``bound_materials`` is read-only. Omitted facts, or a count that
    is not a positive integer, are no bound-material signal.
    """
    text = _last_human_text(messages)
    signals: dict[str, Any] = {"utterance_len": len(text)}
    latest = _latest_snapshot(snapshots)
    bound_hit = _bound_material_grounding(
        text,
        _retrievable_material_count(bound_materials),
        signals,
    )
    if bound_hit is not None:
        return bound_hit

    rules_hit = _rules_decision(text, signals)
    if rules_hit is not None:
        return rules_hit

    # A prior status snapshot can resolve an actionable follow-up, but
    # missing financial slots alone never bypass Jev or retrieval.
    inherit_hit = _inherit_decision(text, latest, dict(signals))
    if text and inherit_hit is None:
        # Gray zone (ADR 0080): the judge owns this utterance — including
        # a key-less boot, which falls back to retrieval rather than
        # resolving to a default track (GE-1).
        return _try_jev_classify(text, jev_client, signals)

    if inherit_hit is not None:
        return inherit_hit

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
    "BARE_CATALOG_VIEW_RE",
    "decide_track",
    "is_bare_catalog_view",
    "normalize_track",
    "snapshot_delta_for_utterance",
]
