"""Grounded-decision admission for the QA chain (plan T9, spec
``qa-chain-grounded-experience.md``).

This module decides, from ONE model decision output plus ONE admitted
evidence set, whether the run merges a verified answer, offers at most
four document candidates, or refuses on insufficient evidence. It is
PURE: no I/O, no network, no database, no model call. Citation
verification is injected (``verify``), so this module never owns a
second citation parser — the spec forbids one, and the only authority
on grounding stays :mod:`fin.host.grounded.citation_gate`.

Three decisions live here and nowhere else:

* **One terminator, one payload shape.** The model marks a candidate
  request with the exact marker :data:`CANDIDATE_TERMINATOR` followed
  by one JSON object ``{"candidates": [{"document_id": ...} |
  {"attachment_id": ...}]}``. The spelling follows the
  ``HIDDEN_BOUNCE_MARKER`` house style (a bracketed FIN-prefixed token
  that cannot collide with inline ``[n]`` citation markers or ordinary
  prose) and is the single source of truth for this lane; the prompt
  that teaches the model the shape imports this constant rather than
  restating it.
* **Multiple documents alone never imply ambiguity.** Candidate
  selection happens ONLY because the model explicitly asked via the
  terminator. A supported multi-document answer is valid (spec:
  "Do not infer ambiguity merely from multiple documents"), so the
  shape of the evidence set — however many documents it spans — never
  produces a choice by itself. Zero surviving candidates never emit an
  empty choice set; the run falls through to the refusal path for that
  answer text instead.
* **Candidate identity is evidence, never model prose.** A requested
  ref is admitted only when the admitted hits back it (an
  eligible-but-unretrieved source is never invented), and every label,
  section and summary is minted from the trusted hit text by the
  deterministic rules documented on :func:`admit_decision` — the
  model's wording of a candidate is never displayed.

The user-visible copy exported here (:func:`rephrase_action`,
:func:`evidence_refusal_copy`) is ordinary-person Chinese with no
internal ids, tool names or slot names, per the readable-projection
spec.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from fin.host.grounded.citation_gate import (
    ATTACHMENT_KIND,
    BounceResult,
    COMPANY_KIND,
    Citation,
    DATABASE_ROW_KIND,
    DoneResult,
    EvidenceItem,
    GateResult,
    RefuseResult,
)
from fin.host.grounded.readable_projection import source_label
from fin.host.span_seams import current_segment, traced_seam

# ----- frozen vocabulary -----

# At most four hit-backed document candidates per decision (spec:
# "one to four distinct admitted source choices").
MAX_DOCUMENT_CANDIDATES: Final[int] = 4

# The deterministic marker that splits answer prose from the candidate
# request payload. ONE spelling, owned here; see the module docstring
# for why this shape. Case-sensitive exact match.
CANDIDATE_TERMINATOR: Final[str] = "[[FIN_DOCUMENT_CHOICES]]"

# The payload field the terminator's JSON object lists requested refs
# under, and the two id keys a ref may carry (frozen payload shape).
CANDIDATE_PAYLOAD_FIELD: Final[str] = "candidates"
DOCUMENT_ID_FIELD: Final[str] = "document_id"
ATTACHMENT_ID_FIELD: Final[str] = "attachment_id"

# The trailing Other choice of a document choice. Mirrors
# ``task_runtime_types.OTHER_CHOICE_ID`` (the shared wire contract owns
# the spelling; this module re-states the literal because its import
# surface is deliberately limited to the citation gate — the test suite
# pins the two constants equal so they cannot drift).
REPHASE_CHOICE_ID: Final[str] = "other"

# The user-visible label of that trailing Other action (frozen by the
# shared wire contract: "The UI label for Other on document choices is
# '换个问法'"). Exported as a constant so no caller restates the string.
REPHRASE_ACTION: Final[str] = "换个问法"

# The user-visible evidence-refusal sentence and its two supported ways
# to rephrase. Distinct from the index ("资料仍在整理中") and busy
# ("检索繁忙，请稍后重试") copy; states insufficient evidence without
# inventing facts, and names no tool, id or slot.
EVIDENCE_REFUSAL_SENTENCE: Final[str] = "资料中还没有找到能回答这个问题的内容"
EVIDENCE_REFUSAL_REPHRASING_1: Final[str] = "可以换个说法，再问一次试试"
EVIDENCE_REFUSAL_REPHRASING_2: Final[str] = "也可以补充时间、公司等细节后再问"

# Shared wire-contract size bounds (specs/question-task-runtime-contract.md,
# "Document clarification and accepted-input scheduling"): ids and source
# versions are nonempty strings of at most 256 UTF-8 bytes; labels and
# section labels at most 512 bytes; summaries nonempty, at most 1024
# bytes and one sentence. Oversized proposals are DROPPED, never
# truncated (the contract forbids truncating away material meaning).
MAX_ID_BYTES: Final[int] = 256
MAX_LABEL_BYTES: Final[int] = 512
MAX_SUMMARY_BYTES: Final[int] = 1024

# The label joins the source display name and the section with a spaced
# middle dot (spec: labels are "document title · section").
_LABEL_SEPARATOR: Final[str] = " · "

# A trusted section heading is a "Section N" / "Section N.M" line or
# an SEC "Item N. <heading>" line in the admitted hit text. The first
# pattern matches the citation projection in ``run_payload``; the SEC
# form supports filing pages whose section heading is not numbered as a
# generic "Section N". These patterns stay local to the citation gate.
_SECTION_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*Section\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE | re.MULTILINE
)
_SEC_ITEM_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(Item\s+\d+(?:\.\d+)*\.\s+[^\r\n]{1,160})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SEC_PART_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*PART\s+[IVX]+\b", re.IGNORECASE
)
_SUMMARY_COVER_PREFIXES: Final[tuple[str, ...]] = (
    "UNITED STATES",
    "SECURITIES AND EXCHANGE COMMISSION",
    "WASHINGTON, D.",
    "FORM ",
    "TABLE OF CONTENTS",
    "CONDENSED CONSOLIDATED",
)
_SUMMARY_ISSUER_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[\w&'’.,() -]{1,100}\s+(?:Inc\.?|Incorporated|Corporation|Corp\.?|"
    r"Company|Co\.?|Limited|Ltd\.?|LLC|L\.P\.|plc|N\.V\.|S\.A\.)$",
    re.IGNORECASE,
)
_SUMMARY_PERIOD_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:Three|Six|Nine|Twelve) Months? Ended|Year Ended)\b",
    re.IGNORECASE,
)
_SUMMARY_MONTH_NAME = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
)
_SUMMARY_DATE_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:{_SUMMARY_MONTH_NAME}\s+\d{{1,2}},\s*){{2,}}"
    rf"{_SUMMARY_MONTH_NAME}\s+\d{{1,2}},?$",
    re.IGNORECASE,
)
_SUMMARY_YEAR_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}(?:\s+\d{4})+$"
)
_SUMMARY_GROUP_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^[^.!?]{1,80}:$"
)
_SUMMARY_UNIT_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\(In\b.*\)$", re.IGNORECASE
)
_SUMMARY_MATCH_LINE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _SEC_ITEM_HEADING_RE,
    _SEC_PART_HEADING_RE,
    _SUMMARY_PERIOD_HEADING_RE,
    _SUMMARY_UNIT_HEADING_RE,
)
_SUMMARY_FULL_LINE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _SUMMARY_ISSUER_LINE_RE,
    _SUMMARY_DATE_HEADER_RE,
    _SUMMARY_YEAR_HEADING_RE,
    _SUMMARY_GROUP_HEADING_RE,
)
_TABLE_ROW_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\w)\d[\d,]*(?:\.\d+)?"
)

# One sentence ends at a strong CJK terminator or an ASCII period that is
# not a decimal point. The digit guard keeps "3.5" one sentence;
# abbreviations like "U.S." still split — accepted, because the rule must
# stay deterministic and the summary is presentation metadata, never
# evidence (the verified bytes stay in the answer). A line break is NOT a
# sentence terminator: the wire contract says prose may carry ordinary
# line breaks, so counting one would refuse legal prose as two sentences.
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(
    r"[。！？!?]|(?<!\d)\.(?!\d)"
)


# ----- value objects -----


@dataclass(frozen=True)
class SourceRef:
    """The versioned source identity a candidate binds a pick to.

    Exactly the ``choice_details.source_ref`` shape the shared wire
    contract freezes: a corpus document or a thread attachment, its
    trusted version, and the section label (``None`` when the trusted
    evidence has no section heading).
    """

    source_kind: Literal["corpus", "attachment"]
    source_id: str
    version: str
    section: str | None


@dataclass(frozen=True)
class DocumentCandidate:
    """One admitted, hit-backed document choice.

    ``choice_id`` is the stable candidate key (see
    :func:`candidate_key`) — distinct per document/section, never the
    reserved Other token. ``label``, ``summary``, and ``source_company``
    are presentation metadata minted from the trusted hit, not citation
    evidence and not stored permission (shared wire contract).
    """

    choice_id: str
    label: str
    summary: str
    source_ref: SourceRef
    source_company: str = ""


@dataclass(frozen=True)
class RequestedRef:
    """One candidate the model asked for: an id slot plus its id.

    The payload grammar gives the kind away — ``document_id`` names a
    corpus document, ``attachment_id`` a thread attachment — so a ref
    carries no version and no section: those come from the trusted
    evidence, never from the model (ADR 0021).
    """

    source_kind: Literal["corpus", "attachment"]
    source_id: str


@dataclass(frozen=True)
class ParsedDecision:
    """The split of one model decision output.

    ``original_text`` is the exact input, byte for byte.
    ``answer_text`` is the prose before the terminator (the whole text
    when no terminator is present). ``requested`` is empty when no
    terminator is present, when the payload's ref list is empty (an
    explicit "no candidates" — a merge), and when the payload is
    malformed (``parse_failed=True`` — a fallback, never a choice).
    Parsing never raises: a malformed payload is a return value.
    """

    original_text: str
    answer_text: str
    requested: tuple[RequestedRef, ...]
    terminator_present: bool
    parse_failed: bool


@dataclass(frozen=True)
class GroundedDecision:
    """The admitted outcome of one decision output against one evidence set.

    ``answer`` is always the exact bytes that passed the injected
    verifier (the pre-terminator prose on the merge path, the original
    text on the fallback path) so a citation quote is always a
    substring of the published answer. ``citations`` is nonempty only
    on ``merge`` and comes from the verifier, never from model output.
    ``candidates`` is nonempty only on ``choice``. ``fallback`` is
    ``fallback_merge`` / ``fallback_refuse`` exactly when
    ``parse_failed`` is true.
    """

    outcome: Literal["merge", "choice", "evidence_refuse"]
    answer: str | None = None
    candidates: tuple[DocumentCandidate, ...] = ()
    citations: tuple[Citation, ...] = ()
    fallback: Literal["none", "fallback_merge", "fallback_refuse"] = "none"
    parse_failed: bool = False
    retrieval_attempted: bool = False


# ----- parsing -----


def parse_decision_output(text: str) -> ParsedDecision:
    """Split one model decision output into prose and requested refs.

    Everything before the FIRST :data:`CANDIDATE_TERMINATOR` is the
    answer prose; everything after it must be one JSON object
    ``{"candidates": [...]}`` (leading/trailing whitespace and junk
    after the object are tolerated). Each list element must be an
    object carrying exactly one usable (nonempty string)
    ``document_id`` or ``attachment_id`` — an element with neither, or
    with both, makes the whole payload malformed, because a ref that
    does not name one identity unambiguously must not be half-read.

    Malformed input never raises: it returns ``parse_failed=True`` with
    empty ``requested``, which routes the original text through the
    fallback admission instead.
    """
    idx = text.find(CANDIDATE_TERMINATOR)
    if idx < 0:
        return ParsedDecision(
            original_text=text,
            answer_text=text,
            requested=(),
            terminator_present=False,
            parse_failed=False,
        )
    head = text[:idx]
    payload = text[idx + len(CANDIDATE_TERMINATOR) :]
    refs, ok = _parse_payload(payload)
    return ParsedDecision(
        original_text=text,
        answer_text=head,
        requested=tuple(refs),
        terminator_present=True,
        parse_failed=not ok,
    )


def _parse_payload(raw: str) -> tuple[list[RequestedRef], bool]:
    """The requested refs one terminator payload asks for, or failure.

    ``raw_decode`` (not ``loads``) so trailing junk after the JSON
    object — a stray sentence the model appended — is tolerated rather
    than failing an otherwise well-formed request. Any shape violation
    (not JSON, not an object, the field not a list, an element without
    one usable id) fails the WHOLE payload: a half-garbled request must
    take the fallback path, where the original text is fully verified,
    not be partially interpreted.
    """
    try:
        value, _end = json.JSONDecoder().raw_decode(raw.strip())
    except ValueError:
        return [], False
    if not isinstance(value, dict):
        return [], False
    refs_raw = value.get(CANDIDATE_PAYLOAD_FIELD)
    if not isinstance(refs_raw, list):
        return [], False
    refs: list[RequestedRef] = []
    for element in refs_raw:
        ref = _requested_ref(element)
        if ref is None:
            return [], False
        refs.append(ref)
    return refs, True


def _id_slot(element: Mapping[str, object], name: str) -> str | None:
    """One payload id slot's trimmed value, or ``None`` when unusable.

    ``None`` covers all three unusable cases the grammar rejects: the key
    absent, present but not a string, and present but blank. Returning
    one value rather than a flag keeps the ambiguity check in the caller
    a single comparison.
    """
    value = element.get(name)
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _requested_ref(element: object) -> RequestedRef | None:
    """The ref one payload element names, or ``None`` when unusable.

    A ref must name exactly one identity: an element naming BOTH slots is
    ambiguous (every evidence item has exactly one id slot), and one
    naming neither is no identity at all.
    """
    if not isinstance(element, dict):
        return None
    document_id = _id_slot(element, DOCUMENT_ID_FIELD)
    attachment_id = _id_slot(element, ATTACHMENT_ID_FIELD)
    if document_id is not None and attachment_id is not None:
        return None
    if document_id is not None:
        return RequestedRef(source_kind="corpus", source_id=document_id)
    if attachment_id is not None:
        return RequestedRef(
            source_kind="attachment", source_id=attachment_id
        )
    return None


# ----- the admitted-hit index -----


def candidate_key(
    source_kind: str, source_id: str, version: str, section: str | None
) -> str:
    """The one stable key a candidate (and its ``choice_id``) rides on.

    Four parts joined with ``:`` — kind, source id, version, section —
    with ``\\``, ``:``, ``@`` and ``#`` backslash-escaped in the
    variable parts (the same escape-first-backslash rule the citation
    gate applies to record ids), so two different identities can never
    spell one key. A ``None`` section and an empty one are the same
    "no section" candidate.
    """
    return ":".join(
        (
            source_kind,
            _escape_key_part(source_id),
            _escape_key_part(version),
            _escape_key_part(section or ""),
        )
    )


def _escape_key_part(part: str) -> str:
    """Make one key part unambiguous inside the ``:``-joined key."""
    return (
        part.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("@", "\\@")
        .replace("#", "\\#")
    )


def admitted_hits(evidence: Sequence[EvidenceItem]) -> Mapping[str, EvidenceItem]:
    """The admitted hits indexed by candidate key, one entry per
    document/section identity.

    Several hits backing the SAME document/section dedup to one entry —
    the first in caller order, which is what the label and summary are
    minted from. Every kind is keyed by its OWN identity: an attachment
    by its ``attachment_id`` (its ``locator.document_id`` is one of the
    never-read-empty document slots and must not become a key part), a
    ledger row by its record id, a corpus hit by its document id. A
    database row is indexed under the ``database`` kind but is never a
    selectable document candidate (see :func:`select_candidates`): the
    shared wire contract closes ``source_ref.source_kind`` to
    ``corpus | attachment``, and a ledger record is not a document a
    pick can scope reading to.

    An item whose identity slots are empty is skipped here rather than
    raising: this function only reads identity, and the same item makes
    the injected verifier raise its own ``MalformedEvidenceError`` on
    every admission path, so nothing malformed passes silently. The
    returned mapping is immutable and the input is never mutated.
    """
    hits: dict[str, EvidenceItem] = {}
    for item in evidence:
        key = _hit_key(item)
        if key is not None:
            hits.setdefault(key, item)
    return MappingProxyType(hits)


def _hit_key(item: EvidenceItem) -> str | None:
    """The candidate key one evidence item is admitted under, or ``None``
    when it carries no identity this lane can name."""
    if item.kind == COMPANY_KIND:
        if not item.locator.document_id:
            return None
        return candidate_key(
            "corpus",
            item.locator.document_id,
            item.locator.version,
            _section_heading(item.unit_text),
        )
    if item.kind == ATTACHMENT_KIND:
        if not item.attachment_id:
            return None
        return candidate_key(
            "attachment",
            item.attachment_id,
            item.locator.version,
            _section_heading(item.unit_text),
        )
    if item.kind == DATABASE_ROW_KIND:
        # One key per row: an item may carry several rows, and each row
        # is its own record with its own time.
        for row in item.database_row:
            if row.source and row.record_key:
                return candidate_key(
                    "database", row.source_id, row.queried_at, None
                )
        return None
    return None


# ----- candidate selection -----


def select_candidates(
    requested: Sequence[RequestedRef], evidence: Sequence[EvidenceItem]
) -> tuple[tuple[DocumentCandidate, ...], tuple[str, ...]]:
    """The admitted candidates one decision's refs earn, plus why the
    rest were dropped.

    Refs are resolved in request order against :func:`admitted_hits`; a
    ref that no admitted hit backs is DROPPED (never invented, never
    admitted from an eligible-but-unretrieved source), and selection
    stops at :data:`MAX_DOCUMENT_CANDIDATES` distinct candidates. A
    generated candidate that violates the shared size bounds is dropped
    too, with a named reason per violation; nothing is truncated.

    The returned reasons are for caller logging — this module's
    ``GroundedDecision`` cannot carry them without widening the frozen
    outcome shape.
    """
    hits = admitted_hits(evidence)
    picked: dict[str, EvidenceItem] = {}
    for ref in requested:
        if len(picked) >= MAX_DOCUMENT_CANDIDATES:
            break
        for key, item in hits.items():
            if len(picked) >= MAX_DOCUMENT_CANDIDATES:
                break
            if key in picked or not _matches_ref(item, ref):
                continue
            # A ledger row is evidence for merging but never a
            # selectable document: the wire contract has no database
            # source kind to bind a pick to.
            if item.kind == DATABASE_ROW_KIND:
                continue
            picked[key] = item
    candidates: list[DocumentCandidate] = []
    reasons: list[str] = []
    for key, item in picked.items():
        candidate = _candidate_from(key, item)
        violations = candidate_bounds_reasons(candidate)
        if violations:
            reasons.extend(violations)
            continue
        candidates.append(candidate)
    return tuple(candidates), tuple(reasons)


def _matches_ref(item: EvidenceItem, ref: RequestedRef) -> bool:
    """Whether one admitted hit backs one requested ref: same kind, same
    id — the version and section come from the hit, never the model."""
    if ref.source_kind == "corpus":
        return (
            item.kind == COMPANY_KIND
            and item.locator.document_id == ref.source_id
        )
    return item.kind == ATTACHMENT_KIND and item.attachment_id == ref.source_id


def _source_company_from_hit(unit_text: str) -> str:
    """Extract a legal issuer name from the same admitted hit."""
    for line in unit_text.splitlines()[:8]:
        candidate = line.strip()
        if (
            candidate
            and len(candidate) <= 120
            and _SUMMARY_ISSUER_LINE_RE.fullmatch(candidate) is not None
        ):
            return candidate
    return ""


def _candidate_from(key: str, item: EvidenceItem) -> DocumentCandidate:
    """Mint presentation fields from one admitted hit.

    The legal issuer line and the first readable summary are extracted
    independently from the same hit. This keeps document identity stable
    when a table-only hit needs a summary different from its cover text.
    """
    section = _section_heading(item.unit_text)
    if item.kind == ATTACHMENT_KIND:
        source_id = item.attachment_id
        name = source_label(
            {"kind": ATTACHMENT_KIND, "filename": item.filename}
        )
        kind: Literal["corpus", "attachment"] = "attachment"
    else:
        source_id = item.locator.document_id
        name = source_label(
            {"kind": COMPANY_KIND, "document_id": source_id}
        )
        kind = "corpus"
    label = (
        f"{name}{_LABEL_SEPARATOR}{section}" if section else name
    )
    return DocumentCandidate(
        choice_id=key,
        label=label,
        summary=_first_sentence(item.unit_text),
        source_ref=SourceRef(
            source_kind=kind,
            source_id=source_id,
            version=item.locator.version,
            section=section,
        ),
        source_company=(
            _source_company_from_hit(item.unit_text)
            if kind == "corpus"
            else ""
        ),
    )


def _section_heading(unit_text: str) -> str | None:
    """The first trusted section heading in one hit, or ``None``."""
    match = _SECTION_HEADING_RE.search(unit_text)
    if match is not None:
        return f"Section {match.group(1)}"
    sec_item = _SEC_ITEM_HEADING_RE.search(unit_text)
    return sec_item.group(1).strip() if sec_item is not None else None


def _is_summary_structure_line(line: str) -> bool:
    """Identify filing structure while preserving each pattern's match mode."""
    if line.upper().startswith(_SUMMARY_COVER_PREFIXES):
        return True
    if any(pattern.match(line) is not None for pattern in _SUMMARY_MATCH_LINE_PATTERNS):
        return True
    return any(
        pattern.fullmatch(line) is not None
        for pattern in _SUMMARY_FULL_LINE_PATTERNS
    )


def _summary_content_line(line: str) -> str | None:
    """Remove a leading structural marker, retaining inline source prose."""
    section = _SECTION_HEADING_RE.match(line)
    if section is not None:
        content = line[section.end() :].lstrip(".: —–-").strip()
        return content or None
    if _is_summary_structure_line(line):
        return None
    return line


def _first_sentence(unit_text: str) -> str:
    """Pick the first readable hit sentence or table row after filing structure."""
    lines = [line.strip() for line in unit_text.splitlines() if line.strip()]
    while lines:
        content = _summary_content_line(lines[0])
        if content is None:
            lines.pop(0)
            continue
        lines[0] = content
        break
    if not lines:
        return ""

    first_line = lines[0]
    if (
        len(_TABLE_ROW_NUMBER_RE.findall(first_line)) >= 2
        and _SENTENCE_END_RE.search(first_line) is None
    ):
        return f"资料中列出了“{first_line}”。"

    text = "\n".join(lines)
    match = _SENTENCE_END_RE.search(text)
    if match is None:
        return text
    return text[: match.end()].strip()


# ----- size bounds -----


def candidate_bounds_ok(candidate: DocumentCandidate) -> bool:
    """Whether one candidate fits the shared wire-contract bounds."""
    return not candidate_bounds_reasons(candidate)


def candidate_bounds_reasons(candidate: DocumentCandidate) -> tuple[str, ...]:
    """Every bound one candidate violates, by name (empty = fits).

    The contract bounds are checked exactly: ids and source versions
    nonempty and at most :data:`MAX_ID_BYTES` UTF-8 bytes; label and
    section at most :data:`MAX_LABEL_BYTES` bytes; summary nonempty, at
    most :data:`MAX_SUMMARY_BYTES` bytes and exactly one sentence; no
    control characters in identifiers or labels (prose may carry
    ordinary line breaks, so the summary is exempt from that check).
    """
    reasons: list[str] = []
    reasons.extend(_id_field_reasons("choice_id", candidate.choice_id))
    reasons.extend(
        _id_field_reasons("source_id", candidate.source_ref.source_id)
    )
    reasons.extend(_id_field_reasons("version", candidate.source_ref.version))
    reasons.extend(_prose_field_reasons("label", candidate.label))
    if candidate.source_ref.section is not None:
        reasons.extend(
            _prose_field_reasons("section", candidate.source_ref.section)
        )
    reasons.extend(_summary_reasons(candidate.summary))
    return tuple(reasons)


def _utf8_len(text: str) -> int:
    """A string's size the way the wire contract measures it: UTF-8 bytes."""
    return len(text.encode("utf-8"))


def _has_control_char(text: str, *, allow_line_breaks: bool) -> bool:
    """Whether any character is a forbidden control character (Unicode
    category Cc).

    Ordinary line breaks are the one control character prose may carry —
    the shared wire contract says so explicitly, and a label or summary
    spanning two lines is not malformed. Identifiers pass
    ``allow_line_breaks=False``: an id is a token, so a break inside one
    is never part of it.
    """
    allowed = {"\n", "\r"} if allow_line_breaks else set()
    return any(
        unicodedata.category(ch) == "Cc" and ch not in allowed
        for ch in text
    )


def _id_field_reasons(field: str, value: str) -> list[str]:
    """The bound violations of one nonempty-bounded id/version field."""
    reasons: list[str] = []
    if not value:
        reasons.append(f"empty_{field}")
        return reasons
    if _utf8_len(value) > MAX_ID_BYTES:
        reasons.append(f"{field}_oversized")
    if _has_control_char(value, allow_line_breaks=False):
        reasons.append(f"control_char_in_{field}")
    return reasons


def _prose_field_reasons(field: str, value: str) -> list[str]:
    """The bound violations of one label/section field (size + control)."""
    reasons: list[str] = []
    if _utf8_len(value) > MAX_LABEL_BYTES:
        reasons.append(f"{field}_oversized")
    if _has_control_char(value, allow_line_breaks=True):
        reasons.append(f"control_char_in_{field}")
    return reasons


def _summary_reasons(summary: str) -> list[str]:
    """The bound violations of the summary field.

    "One sentence" is judged by the same splitter that MINTS summaries
    (:func:`_first_sentence`), so a summary this module built always
    passes it and a hand-built one that packs two sentences is refused.
    """
    reasons: list[str] = []
    if not summary.strip():
        reasons.append("empty_summary")
        return reasons
    if _utf8_len(summary) > MAX_SUMMARY_BYTES:
        reasons.append("summary_oversized")
    sentences = [
        part.strip()
        for part in _SENTENCE_END_RE.split(summary)
        if part.strip()
    ]
    if len(sentences) > 1:
        reasons.append("summary_not_one_sentence")
    return reasons


# ----- admission -----


@traced_seam("admit_decision", lambda _config: current_segment())
def admit_decision(
    *,
    answer_text: str,
    parsed: ParsedDecision,
    evidence: Sequence[EvidenceItem],
    retrieval_attempted: bool,
    verify: Callable[[str], GateResult],
) -> GroundedDecision:
    """The single admission entry point for one grounded decision.

    ``answer_text`` must be the same model output ``parsed`` was parsed
    from (a mismatch is a caller bug and raises ``ValueError``).
    ``verify`` is the caller's R3 citation verifier with everything but
    the answer text bound; it is called EXACTLY ONCE per invocation on
    every branch — on the pre-terminator prose for the ordinary paths
    and on the original text for the fallback path — so the
    one-verifier-call budget is uniform and auditable, and no second
    citation parser ever exists on this lane.

    Branches:

    * **merge** — no terminator, or a terminator whose ref list is
      empty. The prose is published only on a ``DoneResult``; a
      ``RefuseResult``/``BounceResult`` is an evidence refusal (this
      module performs no second synthesis and cannot loop; the bounce
      verdict is the caller's terminal admission here).
    * **choice** — the terminator requested candidates, and
      :func:`select_candidates` admitted one to four. Candidates are
      admitted from evidence alone; the verifier's verdict on the prose
      does not gate them because no prose is published on this path
      (the call is made to keep the budget uniform). Zero survivors
      falls through to the refusal path for the same prose — never an
      empty choice set.
    * **fallback** — the payload was malformed, so the ORIGINAL text
      (verbatim, terminator and all) takes the verifier's verdict: done
      records ``fallback_merge`` with the verifier's citations; any
      rejection records ``fallback_refuse`` with no citations and no
      answer. Citation strings are never salvaged out of the malformed
      payload.

    Every ``evidence_refuse`` outcome requires
    ``retrieval_attempted=True`` and raises ``ValueError`` otherwise: a
    refusal without a real retrieval attempt is a contract violation
    (GE-2/A06) — the corpus must never be reported as having no answer
    for a turn that never searched. ``retrieval_attempted`` means the
    model INVOKED retrieve at least once this run — an empty result, a
    typed EXIT and a binding refusal all count; it does not mean chunks
    were delivered.
    """
    return _admit_decision(
        answer_text=answer_text,
        parsed=parsed,
        evidence=evidence,
        retrieval_attempted=retrieval_attempted,
        verify=verify,
    )


def _admit_decision(
    *,
    answer_text: str,
    parsed: ParsedDecision,
    evidence: Sequence[EvidenceItem],
    retrieval_attempted: bool,
    verify: Callable[[str], GateResult],
) -> GroundedDecision:
    if answer_text != parsed.original_text:
        raise ValueError(
            "answer_text does not match parsed.original_text; pass the "
            "same model output to both"
        )
    head = parsed.answer_text.strip()
    if parsed.parse_failed:
        verdict = verify(parsed.original_text)
        return _admit_fallback(verdict, parsed, retrieval_attempted)
    verdict = verify(head)
    if parsed.requested:
        candidates, _reasons = select_candidates(parsed.requested, evidence)
        if candidates:
            return GroundedDecision(
                outcome="choice",
                candidates=candidates,
                retrieval_attempted=retrieval_attempted,
            )
    return _admit_verdict(verdict, retrieval_attempted)


def _admit_verdict(
    verdict: GateResult, retrieval_attempted: bool
) -> GroundedDecision:
    """The merge-or-refuse outcome of one verified prose."""
    if isinstance(verdict, DoneResult):
        return GroundedDecision(
            outcome="merge",
            answer=verdict.answer,
            citations=verdict.citations,
            retrieval_attempted=retrieval_attempted,
        )
    return _refusal(
        fallback="none",
        parse_failed=False,
        retrieval_attempted=retrieval_attempted,
    )


def _admit_fallback(
    verdict: GateResult,
    parsed: ParsedDecision,
    retrieval_attempted: bool,
) -> GroundedDecision:
    """The fallback outcome of one verified original text.

    With no answer prose at all there is nothing admissible to publish,
    so the refusal stands regardless of the verdict — with the real R3
    verifier an empty text can never be done anyway (no span can
    match).
    """
    if isinstance(verdict, DoneResult) and parsed.answer_text.strip():
        return GroundedDecision(
            outcome="merge",
            # The verifier's own text, which is the ORIGINAL output it was
            # given — citations quote from the published answer, so the
            # admitted bytes are never re-spliced around the terminator.
            answer=verdict.answer,
            citations=verdict.citations,
            fallback="fallback_merge",
            parse_failed=True,
            retrieval_attempted=retrieval_attempted,
        )
    return _refusal(
        fallback="fallback_refuse",
        parse_failed=True,
        retrieval_attempted=retrieval_attempted,
    )


def _refusal(
    *,
    fallback: Literal["none", "fallback_merge", "fallback_refuse"],
    parse_failed: bool,
    retrieval_attempted: bool,
) -> GroundedDecision:
    """One evidence refusal — or the contract violation of claiming one
    for a turn that never searched."""
    if not retrieval_attempted:
        raise ValueError(
            "evidence_refuse requires retrieval_attempted=True; a turn "
            "that never searched cannot report that the corpus has no "
            "answer"
        )
    return GroundedDecision(
        outcome="evidence_refuse",
        fallback=fallback,
        parse_failed=parse_failed,
        retrieval_attempted=retrieval_attempted,
    )


# ----- user-visible actions and copy -----


def rephrase_action() -> str:
    """The user-visible label of a document choice's trailing Other
    action."""
    return REPHRASE_ACTION


def evidence_refusal_copy() -> tuple[str, str, str]:
    """The evidence-refusal sentence and its two supported rephrasings.

    Ordered (refusal, rephrasing 1, rephrasing 2); all three are
    ordinary-person Chinese naming no internal id, tool or slot.
    """
    return (
        EVIDENCE_REFUSAL_SENTENCE,
        EVIDENCE_REFUSAL_REPHRASING_1,
        EVIDENCE_REFUSAL_REPHRASING_2,
    )


__all__ = [
    "ATTACHMENT_ID_FIELD",
    "CANDIDATE_PAYLOAD_FIELD",
    "CANDIDATE_TERMINATOR",
    "DocumentCandidate",
    "DOCUMENT_ID_FIELD",
    "EVIDENCE_REFUSAL_REPHRASING_1",
    "EVIDENCE_REFUSAL_REPHRASING_2",
    "EVIDENCE_REFUSAL_SENTENCE",
    "MAX_DOCUMENT_CANDIDATES",
    "MAX_ID_BYTES",
    "MAX_LABEL_BYTES",
    "MAX_SUMMARY_BYTES",
    "GroundedDecision",
    "ParsedDecision",
    "RequestedRef",
    "REPHASE_CHOICE_ID",
    "REPHRASE_ACTION",
    "SourceRef",
    "admit_decision",
    "admitted_hits",
    "candidate_bounds_ok",
    "candidate_bounds_reasons",
    "candidate_key",
    "evidence_refusal_copy",
    "parse_decision_output",
    "rephrase_action",
    "select_candidates",
]
