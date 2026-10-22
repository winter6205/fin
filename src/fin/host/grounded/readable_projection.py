"""The one admitted readable projection on the host.

Everything an ordinary person reads — the answer, its trailing source
list, a document-choice label, a failure sentence — comes out of here.
Before this module each of those surfaces built its own bytes from the
run's internal representations, and the representations won: the web
source locator printed a raw ``document_id`` or a raw ledger
view+key, the tool trace spelled the raw ``degraded_reasons``, and the
refusal table carried filing boilerplate into a corpus that has none.

The projection is **deterministic and representational only**. It never
calls a model, never re-synthesizes, never adds a fact and never edits
an evidence byte: a matched quote leaves here exactly as the gate read
it. Everything it does is remove or replace, and the ONE thing it can
invent is a redaction mark — which says 「已隐藏」, never what was
hidden.

Two halves, and the split is the point:

- :func:`project_text` filters a single string. It is cheap and it is
  NOT sufficient for a stream: a forbidden fragment can be cut in half
  by the transport, and then neither half matches.
- :func:`project_admitted_answer` is the whole-text filter the
  terminal projection calls. It runs AFTER admission (the citation
  gate has already decided what may be shown) and over the JOINED text,
  so a split fragment reassembles before anything is matched.

There is deliberately no streaming rewriter here. Chunk-by-chunk
rewriting cannot be correct — the boundary is the transport's choice,
not the projection's — and a rewriter would give a false guarantee.
See the module tests' split-leak pair: the per-chunk filter provably
lets the leak through, and the terminal filter provably does not.

Legitimate terminology is NOT blanket-redacted. A finance corpus whose
sources really are SEC filings keeps ``10-Q`` / ``XBRL`` in a readable
label, because redacting them would delete the very words the ask was
about; the same strings are dropped for a corpus that has no such
business, where they are leaked boilerplate. What is always dropped is
the internal *enumeration* — field names, tool names, slot names,
engine names, typed failure codes, and exception type names — never the
subject matter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from fin.host.grounded.citation_gate import (
    ATTACHMENT_KIND,
    DATABASE_ROW_KIND,
    DATABASE_ROW_SNAPSHOT_KEY,
    UNSUPPORTED_DATABASE_CLAIM,
)
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    CORPUS_NOT_READY,
    EMPTY_FAST_GRAPH_FAILURE,
    INSUFFICIENT_EVIDENCE,
    INVALID_BUSINESS_QUERY,
    MAPPING_DROP,
    SQL_NO_MATCHING_ROWS,
)


# ----- what an internal representation reads as -----

# The one replacement text. A redacted span keeps a mark so the reader
# can tell an omission from a sentence that never had the words, and the
# mark deliberately says nothing about what it stands in for — a
# replacement that named the leak would reprint it.
HIDDEN_MARK: Final[str] = "〔已隐藏〕"

# The wire key the readable label rides on. It is ADDITIVE: a source row
# keeps its machine identity so a click still resolves, and gains the
# text a human reads beside it.
SOURCE_LABEL_FIELD: Final[str] = "source_label"

# A label for a source whose identifier is entirely opaque — an EDGAR
# accession, a hash, an opaque id. Derived words are taken from the id
# only when they are ordinary words; an all-caps ticker or a
# digits-bearing token is an identifier, not a word, and is dropped.
OPAQUE_SOURCE_LABEL: Final[str] = "资料来源"

# An attachment with no safe filename, and a ledger row with no minted
# display line. Both say what the row IS rather than naming an id.
UNNAMED_ATTACHMENT_LABEL: Final[str] = "本对话中的附件"
LEDGER_RECORD_LABEL: Final[str] = "业务记录"


# ----- the forbidden vocabulary -----

# Internal enumerations that may never reach the default view. Grouped by
# what they are, because that is what a reviewer has to check: each group
# names one class of internal representation, and adding a new tool or a
# new typed code is one row in the right group rather than a new rule.
#
# Deliberately absent: ordinary English words a legitimate source title
# may contain. ``redline`` is a tool name here and a finance verb there;
# redacting it would corrupt a real title, so it is not in this set.
# The tool-name leak that actually reaches a user is spelled with its
# wire separators (``list_files``), which is what these rows match.
FORBIDDEN_IDENTIFIERS: Final[tuple[str, ...]] = (
    # internal field names
    "document_id",
    "attachment_id",
    "source_ref",
    "record_key",
    "evidence_schema",
    "locator",
    # host tool names
    "retrieve",
    "read_source",
    "list_files",
    "get_xbrl_fact",
    "read_visual_detail",
    "extract_tables",
    "compare_facts",
    "three_year_statement",
    "research_issuers",
    # classify slot names
    "missing_slots",
    # engine / service names
    "rank_bm25",
    "bm25",
    "pgvector",
    "elasticsearch",
    "neo4j",
    "jieba",
    "langchain",
    "langgraph",
    "langchain_core",
    # typed failure and refusal codes
    CORPUS_NOT_READY,
    MAPPING_DROP,
    INSUFFICIENT_EVIDENCE,
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    EMPTY_FAST_GRAPH_FAILURE,
    UNSUPPORTED_DATABASE_CLAIM,
    "citation_repair_cap",
    "unknown_locator",
    "numeric_gate_fail",
    "retrieve_unavailable",
    "retrieve_degraded",
    "retrieve_protocol_error",
    "not_ready",
    "attachment_not_ready",
    # framework internals
    "AgentState",
    "ToolMessage",
    "AIMessage",
    "FIN_HIDDEN_BOUNCE",
)

# EDGAR / XBRL vocabulary. Boilerplate in one corpus and the subject
# matter in another, so these are dropped only when the corpus is not a
# finance corpus.
FORBIDDEN_FINANCE_BOILERPLATE: Final[tuple[str, ...]] = (
    "10-Q",
    "10-K",
    "8-K",
    "XBRL",
    "EDGAR",
    "accession",
    "cik",
)

# The whole set the six-question text scan asserts on, i.e. the finance
# boilerplate INCLUDED. A caller that wants the conservative answer
# iterates this; the filter applies the boilerplate conditionally.
FORBIDDEN_LABELS: Final[frozenset[str]] = frozenset(
    FORBIDDEN_IDENTIFIERS + FORBIDDEN_FINANCE_BOILERPLATE
)


def _assignment(label: str) -> re.Pattern[str]:
    """``label`` plus an assigned value, longest match first.

    An identifier almost never reaches a reader bare: it arrives as
    ``document_id=AAPL-2022Q3`` or ``source_ref: {...}``. Dropping only
    the name would leave the value — which is the more identifying half —
    standing in the sentence.
    """
    return re.compile(
        r"(?<![\w-])"
        + re.escape(label)
        + r"\s*[=:：]\s*[^\s，。；、)\]}:：）】》」]{1,64}"
        + r"|(?<![\w-])"
        + re.escape(label)
        + r"(?![\w-])",
        re.IGNORECASE,
    )


_IDENTIFIER_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (label, _assignment(label)) for label in FORBIDDEN_IDENTIFIERS
)

_BOILERPLATE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (
        label,
        re.compile(r"(?<![\w-])" + re.escape(label) + r"(?![\w-])"),
    )
    for label in FORBIDDEN_FINANCE_BOILERPLATE
)

# A degradation reason is ``<engine>:<what-happened>``. The engines are
# internal even though the shape is not obvious, and a reason reaching a
# reader names a backend the operator cannot act on — which is exactly
# what the access-controlled ledger is for.
_DEGRADATION_REASON_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w-])(?:bm25|vector|graph|graph_ppr|lexical|tables|reports"
    r"|attachments|jev|sql):[A-Za-z0-9_]+(?![\w-])"
)

# An exception type name, plus the detail after it. A traceback is the
# developer diagnostic, and it rides an access-controlled surface; a
# default view shows that something was hidden, not what threw.
_EXCEPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"Traceback \(most recent call last\)[^\n]*"
    r"|(?<![\w.])(?:[A-Za-z_][\w.]*\.)?[A-Za-z_]\w*(?:Error|Exception|Warning)"
    r"(?::[^\n]{0,120})?"
)

_CODE_POINT_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# The reason pattern has no bare-word row, so its label is the shape
# itself; a survivor reported under it is still actionable.
_DEGRADATION_REASON_LABEL: Final[str] = "<engine>:<outcome>"
_EXCEPTION_LABEL: Final[str] = "<exception>"


# ----- text projection -----

# The longest label, which bounds how much trailing text a stream must hold
# back before a possible fragment can be ruled out.
_LONGEST_LABEL: Final[int] = max(
    len(label) for label in FORBIDDEN_IDENTIFIERS + FORBIDDEN_FINANCE_BOILERPLATE
)


def _label_holdback(text: str, labels: Sequence[str]) -> int:
    """Length of the trailing text that could still grow into ``labels``.

    A proper prefix of a label is the only shape that is invisible to every
    rule and can still become one: ``document_`` matches nothing and
    ``document_id`` matches everything. Withholding it costs at most
    ``_LONGEST_LABEL - 1`` characters of pacing, and never withholds a
    label the answer has already finished writing (that case is a complete
    match, handled by :func:`projection_holdback`).

    The suffix must start on a label boundary: every rule refuses to match
    a label that sits inside a word (``(?<![\\w-])``), so a prefix preceded
    by a word character or hyphen cannot grow into that label and is not
    ambiguous. Holding it back anyway would only shift a clean answer's
    frame boundaries, and a stream's pacing is not free to move.
    """
    lowered = text.lower()
    longest = 0
    for label in labels:
        for cut in range(min(len(label) - 1, len(lowered)), 0, -1):
            start = len(lowered) - cut
            if start > 0 and (lowered[start - 1].isalnum() or lowered[start - 1] in "_-"):
                continue
            if lowered.endswith(label[:cut].lower()):
                longest = max(longest, cut)
                break
    return longest


def projection_holdback(text: str, *, corpus_is_finance: bool = False) -> int:
    """Trailing characters of ``text`` a stream must not put on the wire yet.

    The projection is a whole-text filter by design, so a stream cannot ask
    it "is this chunk safe" — the transport chose the boundary and neither
    half of a split fragment matches any rule. What a stream CAN ask is the
    narrower question this answers: which trailing characters are still
    ambiguous, because more text could complete a match there? Two shapes
    are ambiguous:

    * a proper prefix of a forbidden label (a label not yet finished);
    * a match that already ends at the end of the buffer, whose *value*
      may still grow — ``document_id: A`` is a closed match now and an
      open one after ``B`` arrives.

    Everything before the hold-back is safe to project and release: it
    holds no match that the buffer has not already closed. So a stream that
    withholds this much and projects the rest loses no pacing and no text,
    and the concatenation of its frames equals the whole-text projection
    of the joined answer.

    The caller projects the RELEASED part, so the mark lands on the same
    frame the fragment would have landed on, and the withheld fragment is
    released — already projected — as soon as it closes.
    """
    if not text:
        return 0
    labels = FORBIDDEN_IDENTIFIERS if corpus_is_finance else tuple(FORBIDDEN_LABELS)
    held = _label_holdback(text, labels)
    patterns = (
        _ACTIVE_PATTERNS
        if corpus_is_finance
        else _ACTIVE_PATTERNS + _BOILERPLATE_PATTERNS
    )
    end = len(text)
    for _label, pattern in patterns:
        for match in pattern.finditer(text):
            if match.end() == end:
                held = max(held, end - match.start())
    return held



_ACTIVE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (_DEGRADATION_REASON_LABEL, _DEGRADATION_REASON_RE),
    (_EXCEPTION_LABEL, _EXCEPTION_RE),
) + _IDENTIFIER_PATTERNS


def leaked_fragments(
    text: str, *, corpus_is_finance: bool = False
) -> tuple[str, ...]:
    """Which forbidden labels survive ``text``, for an admission assertion.

    The projection replaces rather than deletes, so a leak leaves no
    trace by the time the text has been projected — a caller that wants
    to PROVE the text is clean (the six-question scan, the export
    re-check) needs the pre-projection view. Reporting the surviving
    fragment LABELS, not pattern text or a bare boolean, is what makes
    a failing assertion name the offending label.
    """
    survivors: list[str] = [
        label for label, pattern in _ACTIVE_PATTERNS if pattern.search(text)
    ]
    if not corpus_is_finance:
        survivors.extend(
            label
            for label, pattern in _BOILERPLATE_PATTERNS
            if pattern.search(text)
        )
    return tuple(survivors)


def project_text(text: str, *, corpus_is_finance: bool = False) -> str:
    """``text`` with every internal representation replaced by the mark.

    ``corpus_is_finance`` is the ONE conditional in the module: a corpus
    of real filings keeps the EDGAR vocabulary in its own words, and a
    corpus that is not a finance corpus does not. Default ``False`` is
    the conservative reading — a caller that has not established the
    corpus is financial gets the stricter filter.
    """
    if not text:
        return text
    projected = _CODE_POINT_CONTROL_RE.sub("", text)
    for _label, pattern in _ACTIVE_PATTERNS:
        projected = pattern.sub(HIDDEN_MARK, projected)
    if not corpus_is_finance:
        for _label, pattern in _BOILERPLATE_PATTERNS:
            projected = pattern.sub(HIDDEN_MARK, projected)
    return projected


def project_admitted_answer(
    chunks: Iterable[str],
    *,
    degradation_reasons: Sequence[str] = (),
    corpus_is_finance: bool = False,
) -> str:
    """The whole-text filter the terminal projection calls.

    WHY this exists and why a per-chunk filter is not enough: a stream
    can cut ``document_id`` between ``document_`` and ``id``, and
    neither half matches any rule. Filtering chunk by chunk therefore
    passes the leak through and looks like it worked. Joining FIRST
    makes the fragment whole again, and only then can a rule see it.

    Admission is upstream and this function does not perform it: it is
    handed the text the run already decided may be shown. It only
    filters representations out of it.

    ``degradation_reasons`` are removed verbatim before the rules run.
    They are usually already gone by the time the answer is admitted,
    but a reason echoed back by the model is a raw backend reason in a
    user-facing sentence, and the retrieval port's own copy of them
    (access-controlled diagnostics) is where they belong.
    """
    joined = "".join(
        chunk for chunk in chunks if isinstance(chunk, str) and chunk
    )
    for reason in sorted(
        (r for r in degradation_reasons if isinstance(r, str) and r),
        key=len,
        reverse=True,
    ):
        joined = joined.replace(reason, HIDDEN_MARK)
    return project_text(joined, corpus_is_finance=corpus_is_finance)


# ----- the readable source label -----


def _derived_name(document_id: str) -> str:
    """Ordinary words from ``document_id``, or the opaque label.

    Splitting on the id's own separators and title-casing the pieces is
    what lets ``zeus-expense-policy`` read as ``Zeus Expense Policy``
    without the corpus carrying a title. A piece that is an all-caps
    ticker or that carries digits is an IDENTIFIER, not a word: keeping
    it would print ``AAPL`` from ``AAPL-2022Q3``, which is the leak this
    whole function exists to stop. When no piece survives, the id says
    nothing readable and the opaque label says so honestly.
    """
    pieces = [
        piece
        for piece in re.split(r"[-_.\s]+", document_id.strip())
        if piece and not piece.isdigit() and not piece.isupper()
    ]
    if not pieces:
        return OPAQUE_SOURCE_LABEL
    return " ".join(piece[:1].upper() + piece[1:] for piece in pieces)


def _ledger_label(row: Mapping[str, Any], *, corpus_is_finance: bool) -> str:
    """The row's own minted display line, or a generic record label.

    NEVER the ``source`` + ``record_key`` pair: that pair is the row's
    addressable identity and printing it is what made a ledger citation
    read as a database dump. The ``summary`` is minted by the gate from
    the values THIS ROUND returned, so it states the record without
    naming the table or the employee number.
    """
    snapshot = row.get(DATABASE_ROW_SNAPSHOT_KEY)
    summary = ""
    if isinstance(snapshot, Mapping):
        raw = snapshot.get("summary")
        if isinstance(raw, str):
            summary = raw.strip()
    if summary:
        return project_text(summary, corpus_is_finance=corpus_is_finance)
    return LEDGER_RECORD_LABEL


def source_label(
    row: Mapping[str, Any],
    *,
    title: str = "",
    section: str = "",
    corpus_is_finance: bool = False,
) -> str:
    """The text a human reads for one admitted source row.

    Preference order is title, then section, then a name derived from
    the identifier, then the opaque label. A title is the document's own
    name and is the most faithful representation a source label can
    carry; the derived name exists for the rows that arrive without one,
    and the opaque label for the ids no derivation can honestly read.

    A finance corpus must keep its own terminology in that title — an
    ordinary person asking about a filing needs to read the filing's
    name — so the EDGAR vocabulary is filterable only away from a
    finance corpus, and the rest of the label is filtered either way,
    because a title is host data and may carry an internal field name.
    """
    kind = row.get("kind")
    if kind == ATTACHMENT_KIND:
        filename = row.get("filename")
        base = filename.strip() if isinstance(filename, str) else ""
        return project_text(
            base or UNNAMED_ATTACHMENT_LABEL,
            corpus_is_finance=corpus_is_finance,
        )
    if kind == DATABASE_ROW_KIND:
        return _ledger_label(row, corpus_is_finance=corpus_is_finance)
    for candidate in (title, section):
        if isinstance(candidate, str) and candidate.strip():
            return project_text(
                candidate.strip(), corpus_is_finance=corpus_is_finance
            )
    document_id = row.get("document_id")
    if isinstance(document_id, str) and document_id.strip():
        return project_text(
            _derived_name(document_id), corpus_is_finance=corpus_is_finance
        )
    return OPAQUE_SOURCE_LABEL


def project_citation_row(
    row: Mapping[str, Any],
    *,
    title: str = "",
    section: str = "",
    corpus_is_finance: bool = False,
) -> dict[str, Any]:
    """One wire citation row plus its readable label.

    The row's own keys are returned UNCHANGED — the quote is the
    matched evidence span and the identity is what a source click
    resolves against. Only :data:`SOURCE_LABEL_FIELD` is added (or
    re-projected when the caller already set one), so admitting this
    projection cannot change which evidence a citation points at.
    """
    projected = dict(row)
    existing = projected.get(SOURCE_LABEL_FIELD)
    if not isinstance(existing, str) or not existing.strip():
        projected[SOURCE_LABEL_FIELD] = source_label(
            row,
            title=title,
            section=section,
            corpus_is_finance=corpus_is_finance,
        )
    else:
        projected[SOURCE_LABEL_FIELD] = project_text(
            existing.strip(), corpus_is_finance=corpus_is_finance
        )
    return projected


# ----- the four failure classes -----

FailureClass = Literal[
    "index_work",
    "busy",
    "evidence_refusal",
    "retrieval_degraded",
]

# The frozen Chinese sentences. Each is a DIFFERENT product fact and
# each has a different next action, so a run that renders one as another
# sends the operator somewhere useless: 「资料仍在整理中」 is ongoing work,
# 「检索繁忙」 is a queue the user can retry, the refusal is a question
# that needs rephrasing, and 「部分资料暂不可用」 is a partial answer they
# may still read.
INDEX_WORK_COPY: Final[str] = "资料仍在整理中"
BUSY_COPY: Final[str] = "检索繁忙，请稍后重试"
DEGRADATION_SAFE_COPY: Final[str] = "部分资料暂不可用，当前结果可能不完整"
ATTACHMENT_SCOPE_COPY: Final[str] = "仅在本对话中可用"

# An evidence refusal names the insufficiency and gives two SUPPORTED
# ways to ask again. The two prompts restate the question against what a
# corpus can answer — a named subject, or a narrower slice of it — and
# assert no figure, no document and no period, because a hint that
# invented a fact would be exactly the unsupported answer this class
# exists to withhold.
EVIDENCE_REFUSAL_COPY: Final[str] = (
    "现有资料不足以回答这个问题。可以试着（一）写明要问的对象，"
    "例如具体的制度或产品；（二）把问题缩小到某一份资料里的某一条内容。"
)

# Provider policy disposition is a terminal status, not an evidence failure.
# Keep its short safe explanation separate from the four failure classes and
# their evidence-refusal vocabulary.
POLICY_REFUSAL_COPY: Final[str] = "抱歉，我无法协助处理这个请求。"

FAILURE_COPY: Final[dict[str, str]] = {
    "index_work": INDEX_WORK_COPY,
    "busy": BUSY_COPY,
    "evidence_refusal": EVIDENCE_REFUSAL_COPY,
    "retrieval_degraded": DEGRADATION_SAFE_COPY,
}

# The proven readiness pairs, and ONLY these. Index work is claimed when
# the cause and the stage are both ``index``; busy covers the three
# stages where the retrieval budget expired. A pair outside these — an
# absent envelope, a lookalike cause, a missing stage — yields no class
# at all rather than the nearest guess: claiming index work that nothing
# proved would send the user away to wait for an indexing job that is
# not running.
_INDEX_READINESS: Final[tuple[str, str]] = ("index", "index")
_BUSY_STAGES: Final[frozenset[str]] = frozenset({"queue", "startup", "response"})

# Reasons whose CLASS the reader is told. A reason that names the SAME
# product fact through more than one backend exit reads once: the operator
# can act on 「检索繁忙」 or not at all, and which internal code produced it
# is a diagnostic fact (the typed reason still rides the receipt).
FAILURE_CLASS_BY_REASON: Final[dict[str, FailureClass]] = {
    # 空闲集合：retrieve 跑过而集合是空的。这是「资料还在整理」这一
    # 类的唯一具名失败出口，也是它与 ``insufficient_evidence`` 分开的
    # 地方——GE-5 要求 index 与 insufficient-evidence 的文案不同，而
    # 旧实现让两者共用同一句话。
    CORPUS_NOT_READY: "index_work",
    MAPPING_DROP: "evidence_refusal",
    INSUFFICIENT_EVIDENCE: "evidence_refusal",
    COMMUNITY_SUMMARY_INDEX_FAILURE: "evidence_refusal",
    EMPTY_FAST_GRAPH_FAILURE: "evidence_refusal",
    "citation_repair_cap": "evidence_refusal",
    "unknown_locator": "evidence_refusal",
    "numeric_gate_fail": "evidence_refusal",
    "task_insufficient_evidence": "evidence_refusal",
    SQL_NO_MATCHING_ROWS: "evidence_refusal",
    INVALID_BUSINESS_QUERY: "evidence_refusal",
    UNSUPPORTED_DATABASE_CLAIM: "evidence_refusal",
}

# Every terminal reason that means「the evidence does not support an
# answer」. They stay DISTINCT as typed codes for the receipt and the
# diagnostics; here they collapse onto one sentence because the
# difference between them is a backend detail the user cannot act on,
# and the typed code must not reach the default view to name it.
EVIDENCE_REFUSAL_REASONS: Final[frozenset[str]] = frozenset(
    reason
    for reason, failure in FAILURE_CLASS_BY_REASON.items()
    if failure == "evidence_refusal"
)


class UnclassifiableTerminal(RuntimeError):
    """A terminal run reason this module has no safe copy for.

    Raised instead of returning ``""``: an unrecognized terminal is a
    table row this ticket has not added yet, and silently rendering
    nothing would let a refusal reach the user with no sentence at all —
    which reads as a dropped connection rather than as a refusal. The
    typed reason still rides the receipt and the diagnostics, so the
    caller that catches this has everything it needs to decide whether
    the run is genuinely class-free or the table is behind.
    """


def _readiness_class(readiness: Mapping[str, Any] | None) -> FailureClass | None:
    """The class a proven ``not_ready`` envelope carries, or ``None``."""
    if not isinstance(readiness, Mapping):
        return None
    if readiness.get("error") != "not_ready":
        return None
    cause = readiness.get("cause")
    stage = readiness.get("stage")
    if (cause, stage) == _INDEX_READINESS:
        return "index_work"
    if cause == "busy" and stage in _BUSY_STAGES:
        return "busy"
    return None


def classify_failure(
    *,
    status_reason: str,
    readiness: Mapping[str, Any] | None = None,
    degraded: bool = False,
) -> FailureClass:
    """Which of the four failure classes this run ended in.

    Degradation wins over a bare refusal: a degraded answer is
    可用但必须可见, and the sentence that says a partial result may be
    incomplete is the one the operator needs even when the run also
    refused. Readiness is read as a pair, never inferred from the
    reason alone, so a ``not_ready`` with no proven stage does not
    masquerade as index work.

    A reason outside the table raises :class:`UnclassifiableTerminal`
    rather than returning a guess.
    """
    if not isinstance(status_reason, str) or not status_reason.strip():
        raise UnclassifiableTerminal(
            "a terminal run needs a non-empty status_reason to classify"
        )
    proven = _readiness_class(readiness)
    if proven is not None:
        return proven
    if degraded:
        return "retrieval_degraded"
    if status_reason in EVIDENCE_REFUSAL_REASONS:
        return "evidence_refusal"
    if status_reason in FAILURE_CLASS_BY_REASON:
        return FAILURE_CLASS_BY_REASON[status_reason]
    raise UnclassifiableTerminal(
        f"no safe default copy is defined for terminal reason {status_reason!r}"
    )


def failure_copy(failure: FailureClass) -> str:
    """The ordinary-person sentence for one class."""
    return FAILURE_COPY[failure]


# WHY a 具名失败 keeps its own sentence inside a class: each names a
# DIFFERENT next action, and the advice is what the sentence is for. The
# class sentence is the FLOOR a reason falls back to; a reason here reads
# its own because acting on it means something the class sentence does not
# say. Collapsing two of them onto one sentence — which is what the old
# English table did to ``corpus_not_ready`` and ``insufficient_evidence``
# — sends the operator somewhere useless, and a sentence that sends the
# operator to re-run the wrong job is worse than a blank one.
UNKNOWN_LOCATOR_COPY: Final[str] = (
    "这条回答没能找到可以核对的资料出处。可以换一种更具体的问法，"
    "例如写明要查的对象或范围。"
)
CITATION_REPAIR_CAP_COPY: Final[str] = (
    "试了几次都没能把回答和引用到的资料对应上。可以换一种问法，"
    "或者把范围收窄一些。"
)
UNSUPPORTED_DATABASE_CLAIM_COPY: Final[str] = (
    "回答里的这个数字和业务记录里查到的不一致。可以换一种问法再问一次，"
    "或者直接查这条业务记录。"
)
NUMERIC_GATE_FAIL_COPY: Final[str] = (
    "回答里的数字和资料里记录的数值对不上。可以一次只问一个数字。"
)
MAPPING_DROP_COPY: Final[str] = (
    "检索到了一些内容，但都没能对应到可引用的资料来源。可能是索引里的资料"
    "对应关系过期了，重建一次索引后再问。"
)
COMMUNITY_SUMMARY_INDEX_FAILURE_COPY: Final[str] = (
    "综述索引的构建没有完成，这个范围暂时查不到内容。可以稍后再试，"
    "或者换一个问题再问一次。"
)
EMPTY_FAST_GRAPH_FAILURE_COPY: Final[str] = (
    "这份资料用当前的快速抽取方式没有建出关系图，换一个抽取方式并重建索引后"
    "再试一次。"
)
SQL_NO_MATCHING_ROWS_COPY: Final[str] = (
    "业务数据库在你要的时间范围里没有匹配的记录。可以放宽时间范围，"
    "或者换一个业务对象再问一次。"
)
STEP_CAP_COPY: Final[str] = (
    "这次问题问得太长，已经达到单次问答的步数上限。可以把它拆成"
    "几个更小的问题分别问。"
)

# The reasons whose DIFFERENCE the operator must act on, and every other
# reason's absence from it. A reason here overrides its class: the class
# says what the fact IS, while this sentence names the fix. Everything not
# listed falls back to its class sentence, so a new 具名失败 is served the
# moment it is given a class, and this table only ever narrows that.
REASON_COPY: Final[dict[str, str]] = {
    "unknown_locator": UNKNOWN_LOCATOR_COPY,
    "citation_repair_cap": CITATION_REPAIR_CAP_COPY,
    UNSUPPORTED_DATABASE_CLAIM: UNSUPPORTED_DATABASE_CLAIM_COPY,
    "numeric_gate_fail": NUMERIC_GATE_FAIL_COPY,
    MAPPING_DROP: MAPPING_DROP_COPY,
    COMMUNITY_SUMMARY_INDEX_FAILURE: COMMUNITY_SUMMARY_INDEX_FAILURE_COPY,
    EMPTY_FAST_GRAPH_FAILURE: EMPTY_FAST_GRAPH_FAILURE_COPY,
    SQL_NO_MATCHING_ROWS: SQL_NO_MATCHING_ROWS_COPY,
}


def ordinary_person_copy(
    status: str,
    reason: str,
    *,
    readiness: Mapping[str, Any] | None = None,
    degraded: bool = False,
) -> str:
    """The sentence the default view renders for a terminal run.

    ``done`` -> ``""`` (the run produced a real answer). ``step_cap`` ->
    the step-cap sentence regardless of the reason. ``refuse`` -> the
    reason's sentence, resolved through :func:`classify_failure` so the
    four classes stay the authority on WHAT is said and this function
    only decides WHICH one.

    A reason with no class returns ``""`` rather than a borrowed
    sentence. :func:`classify_failure` raises on one, which is right for
    a diagnostics caller but wrong here: the default view must not invent
    an explanation for a terminal the table has not learned yet, and a
    blank card on a run that is already terminal is the failure the
    strict accessor exists to make loud.

    The typed reason is NOT rendered here and never needs sanitizing on
    this path — it rides the receipt and the access-controlled
    diagnostics, and nothing else.
    """
    if status == "done":
        return ""
    if status == "step_cap":
        return STEP_CAP_COPY
    if status != "refuse":
        return ""
    if reason == "policy_refuse":
        return POLICY_REFUSAL_COPY
    try:
        failure = classify_failure(
            status_reason=reason, readiness=readiness, degraded=degraded
        )
    except UnclassifiableTerminal:
        return ""
    return REASON_COPY.get(reason) or FAILURE_COPY[failure]


__all__ = [
    "ATTACHMENT_SCOPE_COPY",
    "BUSY_COPY",
    "COMMUNITY_SUMMARY_INDEX_FAILURE_COPY",
    "DEGRADATION_SAFE_COPY",
    "EVIDENCE_REFUSAL_COPY",
    "EVIDENCE_REFUSAL_REASONS",
    "EMPTY_FAST_GRAPH_FAILURE_COPY",
    "FAILURE_CLASS_BY_REASON",
    "FAILURE_COPY",
    "FORBIDDEN_FINANCE_BOILERPLATE",
    "FORBIDDEN_IDENTIFIERS",
    "FORBIDDEN_LABELS",
    "FailureClass",
    "HIDDEN_MARK",
    "INDEX_WORK_COPY",
    "LEDGER_RECORD_LABEL",
    "OPAQUE_SOURCE_LABEL",
    "POLICY_REFUSAL_COPY",
    "REASON_COPY",
    "SOURCE_LABEL_FIELD",
    "STEP_CAP_COPY",
    "SQL_NO_MATCHING_ROWS_COPY",
    "UNNAMED_ATTACHMENT_LABEL",
    "UnclassifiableTerminal",
    "classify_failure",
    "failure_copy",
    "leaked_fragments",
    "ordinary_person_copy",
    "project_admitted_answer",
    "project_citation_row",
    "project_text",
    "projection_holdback",
    "source_label",
]
