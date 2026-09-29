"""Citation gate for the v1 host agent (ADR 0008, ADR 0021).

This module is the **only** module in ``src/fin/host/`` that decides
whether the model's answer is grounded in evidence from this run's
tool calls. It runs on the path to END in the host graph (T6); it does
NOT call a model, the network, or a database.

The public API is the pure function ``verify_citations``. It returns one
of three typed outcomes:

- ``DoneResult`` — the answer contains at least one ≥``min_span_len``
  extractive span that is a contiguous substring of this run's evidence
  (a single returned slice, or two or more ADJACENT returned slices of
  the same frozen unit — see ``_match_spans``). ``citations`` is the
  deduplicated list of ``(identity, quote)`` pairs the user-visible
  citation list contains.

- ``BounceResult`` — the answer did not pass; the bounce path
  increments ``repair_count`` and returns a hidden template that T6
  injects into the model's next-turn system message. The template is
  data: CLI/HTTP rendering (T7, T9) does NOT display it.

- ``RefuseResult`` — terminal stop. Two reasons only: ``unknown_locator``
  (the answer claims a ``document_id`` not in the frozen corpus —
  refuses immediately, no repair) and ``citation_repair_cap``
  (``repair_count`` has already hit the cap).

The repair counter and the step cap are independent fuses.
``repair_count`` lives in fin state, not LangGraph
``recursion_limit``.

Citations come from the gate's matched evidence items, never from the
model's claim. This prevents model-invented ``version`` strings
(ADR 0021).

Evidence kinds are closed (spec ``thread-attachment-source-read``): only text the ``read_source`` / ``retrieve``
legs returned is quotable.
A generated visual observation is never evidence for a quote, and an
item carrying any other kind is a typed ``MalformedEvidenceError``, not
a silent drop. Attachment evidence additionally re-passes a trust
re-check supplied by the caller (ownership + version currency) before
it can produce a citation; the gate itself holds no store.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Iterable

from fin.corpus.extracted import Locator
from fin.retrieve.source_read import ATTACHMENT_KIND

# ----- shared hidden-bounce marker (SSOT, ADR 0021) -----
#
# The marker the host graph (T6) injects into the model's next-turn
# system message when the gate bounces. CLI / HTTP rendering (T7, T9)
# must strip this marker before printing. The marker is the single
# source of truth — every consumer (``graph.py``, ``cli.py``, ``http.py``)
# imports it from here. Do NOT redefine the literal anywhere else.
HIDDEN_BOUNCE_MARKER: Final[str] = "[FIN_HIDDEN_BOUNCE]"

# Strip the marker (and the whitespace immediately around it) from any
# string before user-facing output. Used by ``cli.py`` and ``http.py``.
_HIDDEN_BOUNCE_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*" + re.escape(HIDDEN_BOUNCE_MARKER) + r"\s*"
)


def strip_hidden_bounce_marker(text: str) -> str:
    """Remove every occurrence of ``HIDDEN_BOUNCE_MARKER`` from ``text``.

    Whitespace immediately around the marker is folded in (the gate's
    template typically leaves a leading newline). The marker is the
    single source of truth — see ``HIDDEN_BOUNCE_MARKER`` above.
    """
    return _HIDDEN_BOUNCE_RE.sub("", text)


# The closed set of evidence kinds the quote channel accepts: the
# company corpus leg and the thread-attachment leg. A generated visual
# observation — or anything else — is not quotable and raises at
# validation; it must never ride this channel into a citation.
COMPANY_KIND: Final[str] = "company"
QUOTABLE_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {COMPANY_KIND, ATTACHMENT_KIND}
)

# v1 document-id locator pattern (ADR 0006 / CONTEXT.md vocabulary).
# Matches the manifest ``document_id`` shape (e.g. ``AAPL-2022Q3``):
# 2-5 uppercase letters, a dash, four digits, then ``Q1``/``Q2``/
# ``Q3``/``Q4``. The frozen corpus exposes 12 such ids; this is the
# only shape the gate treats as a locator claim.
_LOCATOR_REGEX: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z]{2,5}-\d{4}Q[1-4])\b"
)


# Default template the host graph injects into the model's next-turn
# system message on a bounce. Hidden from CLI/HTTP rendering (T7, T9).
# Neutral instruction language — no "error" / "failed" framing.
_HIDDEN_BOUNCE_TEMPLATE: Final[str] = (
    "your answer did not cite evidence; re-answer with at least one "
    "≥32-character span copied verbatim from a passage or unit you "
    "retrieved or read this run."
)


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence item the gate may match against.

    ``unit_text`` is whatever the upstream tool returned for one frozen
    unit (or, for retrieve chunks, the passage slice — both are slices
    of the committed extracted tree or stored attachment text). The
    gate matches ``≥min_span_len`` substrings of ``answer`` against
    ``unit_text`` only; it does NOT reconstruct whole units on the fly
    (the gate's evidence list already has the right granularity).

    ``kind`` is the closed quotable kind (``"company"`` or
    ``ATTACHMENT_KIND``). Attachment items carry their identity in
    ``attachment_id`` (``locator.document_id`` stays empty); company
    items carry it in ``locator.document_id``. ``version_int`` is the
    trusted record's integer version an attachment item was admitted
    under — what the caller's re-check compares currency against;
    ``None`` for company items. ``filename`` is display-only metadata
    and never part of the citation identity; ``display`` says how the
    unit is presented (``"page"`` for a physical PDF page, ``"unit"``
    otherwise).

    ``position`` / ``next_position`` are the slice's code-point offsets
    within the full frozen unit, taken from the read contract: two
    items join into one matchable text only when one run's
    ``next_position`` equals the next run's ``position``. Retrieve
    chunks keep the defaults (``0`` / ``None``) because a passage slice
    is not a unit-aligned offset and must never bridge.
    """

    locator: Locator
    unit_text: str
    kind: str = COMPANY_KIND
    attachment_id: str = ""
    version_int: int | None = None
    filename: str = ""
    display: str = "page"
    position: int = 0
    next_position: int | None = None


@dataclass(frozen=True)
class Citation:
    """A passed citation.

    ``kind`` / ``document_id`` / ``attachment_id`` / ``version`` /
    ``unit`` / ``display`` / ``filename`` always come from the matched
    evidence item — never from the model's claim. This prevents
    model-invented ``version`` strings (ADR 0021).

    Exactly one source id is set: ``document_id`` for a company
    citation (``attachment_id`` empty), ``attachment_id`` for an
    attachment citation (``document_id`` empty, ``filename`` the safe
    display name). ``display`` is ``"page"`` when the cited unit is
    presented as a physical page and ``"unit"`` otherwise.

    ``quote`` is the ≥``min_span_len`` substring of the answer that
    matched; the gate keeps the verbatim span so downstream rendering
    can echo it as a quoted excerpt.
    """

    kind: str
    document_id: str
    attachment_id: str
    version: str
    unit: int
    display: str
    filename: str
    quote: str


# ----- result types -----


@dataclass(frozen=True)
class DoneResult:
    """Gate accepted: ≥1 ≥``min_span_len`` span is grounded in evidence.

    ``citations`` is the deduplicated list of ``(identity, quote)``
    pairs (a unique evidence identity plus quote is at most one
    ``Citation``). The host graph forwards ``answer`` and ``citations``
    to the CLI/HTTP surface.
    """

    outcome: str  # always "done"
    citations: tuple[Citation, ...]
    answer: str


@dataclass(frozen=True)
class BounceResult:
    """Gate rejected: recoverable. The caller loops back to the model.

    ``repair_count`` is the **post-bounce** count — what the caller
    should pass on the next ``verify_citations`` call. ``hidden_template``
    is the fixed instruction the host graph injects into the model's
    next-turn system message; it is data, not user-facing output.
    """

    outcome: str  # always "bounce"
    repair_count: int
    hidden_template: str


@dataclass(frozen=True)
class RefuseResult:
    """Gate rejected: terminal. Run status is ``refuse``.

    ``reason`` is one of ``"unknown_locator"`` or
    ``"citation_repair_cap"``. No other values are produced (ADR 0008).
    """

    outcome: str  # always "refuse"
    reason: str


GateResult = DoneResult | BounceResult | RefuseResult


# ----- typed errors -----


class CitationGateError(Exception):
    """Base class for citation gate typed errors.

    The gate never silently swallows malformed input; it raises a
    subclass of this for each invalid call shape (defensive-contract
    axis: malformed evidence → typed error).
    """


class MalformedEvidenceError(CitationGateError):
    """An evidence item has an empty ``unit_text``, an invalid locator
    (``unit < 1`` / missing source id / empty ``version``), or a kind
    outside the closed quotable set.

    The ``index`` attribute carries the position of the offending item
    in the caller's evidence iterable (useful for run logs; matches the
    host graph's evidence-list shape).
    """

    def __init__(self, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


# ----- core gate -----


def verify_citations(
    answer: str,
    evidence: Iterable[EvidenceItem],
    repair_count: int,
    *,
    known_document_ids: set[str],
    repair_cap: int = 3,
    min_span_len: int = 32,
    attachment_identity_current: Callable[[EvidenceItem], bool] | None = None,
) -> GateResult:
    """Verify the model's answer is grounded in this run's evidence.

    Parameters
    ----------
    answer
        The model's final answer prose. Treated as an opaque UTF-8
        string; the gate never decodes / re-encodes it.
    evidence
        The union of this run's ``read_source`` returned slices and
        ``retrieve`` chunk texts, each carrying its locator and offsets.
        Empty evidence is allowed; the gate then falls through to the
        bounce path (or refuse, if ``repair_count`` already hit
        ``repair_cap``).
    repair_count
        The number of recoverable citation failures already
        accumulated on this run. Negative values are treated as 0
        (defensive). ``unknown_locator`` does **not** increment it;
        the bounce path does.
    known_document_ids
        The corpus manifest's document_id set. Any regex-shaped
        ``document_id`` claim in ``answer`` that is not in this set
        refuses immediately on the first failure (ADR 0008).
    repair_cap
        Hard cap on consecutive bounce-able failures. Default 3
        (ADR 0008). When ``repair_count >= repair_cap``, the gate
        returns ``RefuseResult("citation_repair_cap")`` instead of
        a bounce.
    min_span_len
        Minimum length of an extractive span, in characters. Default
        32 (ADR 0021).
    attachment_identity_current
        Trust re-check for attachment evidence, supplied by the caller
        which alone can see the trusted store: it must return True only
        when the item's ``attachment_id`` is still owned by the current
        thread and ``version_int`` still matches the record. An
        attachment item the check rejects — or any attachment item
        when no check is supplied — never yields a citation, even when
        its text matches. Company items are unaffected.

    Returns
    -------
    GateResult
        One of ``DoneResult``, ``BounceResult``, ``RefuseResult``.
        Discriminate on ``outcome`` (always ``"done"`` / ``"bounce"``
        / ``"refuse"``).

    Raises
    ------
    MalformedEvidenceError
        If any item in ``evidence`` has ``unit_text == ""``,
        ``locator.unit < 1``, an empty source id / ``version``, or a
        ``kind`` outside ``QUOTABLE_EVIDENCE_KINDS``. The gate does NOT
        silently skip malformed items — ADR 0021 forbids hidden drops.
    """
    # Repair counter is clamped to 0 (defensive: a negative value from
    # a caller bug should not accidentally cap the bounce path).
    if repair_count < 0:
        repair_count = 0

    # Validate evidence eagerly so the span-match path below never
    # sees a half-broken input. Materialize first so validation can
    # report each offending item's index in caller order.
    evidence_tuple: tuple[EvidenceItem, ...] = tuple(evidence)
    for idx, item in enumerate(evidence_tuple):
        _validate_evidence(item, index=idx)

    # Attachment trust re-check: refuse (silently, per spec: this is
    # not a malformed-input case, it is an untrusted-identity case)
    # any attachment item whose identity is stale, foreign, or
    # unverifiable before it can carry a quote.
    eligible = tuple(
        item
        for item in evidence_tuple
        if item.kind != ATTACHMENT_KIND
        or (
            attachment_identity_current is not None
            and attachment_identity_current(item)
        )
    )

    # Step 1: unknown-locator heuristic. Refuses immediately on the
    # first failure (no repair) — ADR 0008 + ADR 0021. The regex is
    # intentionally narrow (matches the manifest document_id shape
    # only); loose phrase matches would falsely flag English text
    # like "AAPL revenue" as locator claims.
    for match in _LOCATOR_REGEX.finditer(answer):
        candidate = match.group(1)
        if candidate not in known_document_ids:
            return RefuseResult(outcome="refuse", reason="unknown_locator")

    # Step 2: span matching against evidence.
    citations = _match_spans(
        answer, eligible, min_span_len=min_span_len
    )

    if citations:
        return DoneResult(
            outcome="done",
            citations=citations,
            answer=answer,
        )

    # Step 3: no spans passed → bounce or refuse.
    if repair_count >= repair_cap:
        return RefuseResult(outcome="refuse", reason="citation_repair_cap")

    return BounceResult(
        outcome="bounce",
        repair_count=repair_count + 1,
        hidden_template=_HIDDEN_BOUNCE_TEMPLATE,
    )


# ----- internals -----


def _validate_evidence(item: EvidenceItem, *, index: int) -> None:
    """Validate a single ``EvidenceItem``.

    Raises ``MalformedEvidenceError`` rather than silently skipping.
    The error carries the offending item's index so the host graph's
    run logs can point at the specific evidence entry that broke the
    contract.
    """
    if item.kind not in QUOTABLE_EVIDENCE_KINDS:
        raise MalformedEvidenceError(
            f"evidence[{index}].kind {item.kind!r} is not quotable; only "
            f"{sorted(QUOTABLE_EVIDENCE_KINDS)} text may enter the quote "
            f"channel",
            index=index,
        )
    if not isinstance(item.unit_text, str):
        raise MalformedEvidenceError(
            f"evidence[{index}].unit_text must be str, "
            f"got {type(item.unit_text).__name__}",
            index=index,
        )
    if item.unit_text == "":
        raise MalformedEvidenceError(
            f"evidence[{index}].unit_text is empty",
            index=index,
        )
    locator = item.locator
    if not isinstance(locator, Locator):
        raise MalformedEvidenceError(
            f"evidence[{index}].locator must be Locator, "
            f"got {type(locator).__name__}",
            index=index,
        )
    if (
        not isinstance(locator.unit, int)
        or isinstance(locator.unit, bool)
        or locator.unit < 1
    ):
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.unit must be int ≥ 1, "
            f"got {locator.unit!r}",
            index=index,
        )
    if not locator.version:
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.version is empty",
            index=index,
        )
    if item.kind == ATTACHMENT_KIND:
        if not item.attachment_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].attachment_id is empty for an "
                f"attachment item",
                index=index,
            )
        if locator.document_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].locator.document_id must be empty "
                f"for an attachment item, got {locator.document_id!r}",
                index=index,
            )
    else:
        if not locator.document_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].locator.document_id is empty",
                index=index,
            )


def _unit_identity(
    item: EvidenceItem,
) -> tuple[str, str, str, int]:
    """The identity two evidence items share exactly when they are
    slices of the same frozen unit: kind, source id, version, ordinal.
    """
    source_id = (
        item.attachment_id if item.kind == ATTACHMENT_KIND else item.locator.document_id
    )
    return (item.kind, source_id, item.locator.version, item.locator.unit)


def _citation_from(item: EvidenceItem, quote: str) -> Citation:
    """Render one matched evidence item as a ``Citation``."""
    return Citation(
        kind=item.kind,
        document_id="" if item.kind == ATTACHMENT_KIND else item.locator.document_id,
        attachment_id=item.attachment_id,
        version=item.locator.version,
        unit=item.locator.unit,
        display=item.display,
        filename=item.filename,
        quote=quote,
    )


def _contiguous_runs(
    evidence: tuple[EvidenceItem, ...],
) -> list[list[EvidenceItem]]:
    """Maximal ADJACENT slice runs (length ≥ 2) of the same unit.

    Items are grouped by unit identity, ordered by ``position`` inside
    each group, and split wherever continuity breaks — an item with no
    ``next_position`` (a retrieve chunk, or an unread tail slice) or a
    nonzero gap between ``prev.next_position`` and ``next.position``.
    A run never bridges a gap: quote text spanning an unseen hole stays
    unmatched (spec thread-attachment-source-read SC10). Group order
    follows first-seen caller order; deterministic, no set iteration.
    """
    groups: dict[tuple[str, str, str, int], list[EvidenceItem]] = {}
    for item in evidence:
        groups.setdefault(_unit_identity(item), []).append(item)

    runs: list[list[EvidenceItem]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda it: it.position)
        current = [ordered[0]]
        for nxt in ordered[1:]:
            prev = current[-1]
            if prev.next_position is not None and prev.next_position == nxt.position:
                current.append(nxt)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [nxt]
        if len(current) >= 2:
            runs.append(current)
    return runs


def _harvest_spans(
    answer: str,
    ev_text: str,
    anchor: EvidenceItem,
    *,
    min_span_len: int,
    seen: set[tuple[tuple[str, str, str, int], str]],
    citations: list[Citation],
) -> None:
    """Record every maximal grounded span of ``answer`` inside
    ``ev_text`` onto ``citations`` (dedup via ``seen``).

    Algorithm: walk ``answer`` left-to-right. At each starting
    position ``i``, greedily extend the match (``answer[i:j] in
    ev_text``) until the next character would fail. The longest match
    starting at ``i`` is recorded (if it meets ``min_span_len`` and
    contains an alphanumeric token) and we skip past it; otherwise we
    advance one position. The greedy rule guarantees the longest match
    per starting position; the "skip past the match" rule prevents
    nested-match explosions without losing non-overlapping spans.
    """
    n = len(answer)
    ev_len = len(ev_text)
    i = 0
    while i <= n - min_span_len:
        # Greedy longest match starting at i. Cap the extension by
        # both the answer length and the evidence length:
        # ``answer[i:j]`` cannot be a substring of ``ev_text`` if its
        # length exceeds ``ev_len``.
        j = i + min_span_len
        max_j = min(n + 1, i + ev_len + 1)
        while j < max_j and answer[i:j] in ev_text:
            j += 1
        match_len = j - 1 - i

        if match_len >= min_span_len:
            quote = answer[i : i + match_len]
            # A span must contain at least one alphanumeric
            # token — whitespace-only / punctuation-only spans
            # are ignored (ADR 0021).
            if any(ch.isalnum() for ch in quote):
                key = (_unit_identity(anchor), quote)
                if key not in seen:
                    seen.add(key)
                    citations.append(_citation_from(anchor, quote))
            # Skip past this match (whether or not we recorded
            # it — any shorter substring at the same start is
            # either no-alnum or non-matching, by definition of
            # the greedy longest-match).
            i += match_len
        else:
            i += 1


def _match_spans(
    answer: str,
    evidence: tuple[EvidenceItem, ...],
    *,
    min_span_len: int,
) -> tuple[Citation, ...]:
    """Find all maximal ≥``min_span_len`` substrings of ``answer``
    that are grounded in this run's evidence.

    Grounded means: a contiguous substring of ONE returned slice, or
    of two or more ADJACENT returned slices of the same frozen unit —
    joined only where the offsets are contiguous. A quote that spans a
    gap no slice covered stays unmatched.

    Returns ``Citation`` objects deduplicated by
    ``(unit identity, quote)``, in deterministic order: every single
    item first in caller order (left-to-right within the answer), then
    every maximal contiguous run in first-item order. Repeats are
    dropped silently.
    """
    n = len(answer)
    if n < min_span_len:
        return ()

    seen: set[tuple[tuple[str, str, str, int], str]] = set()
    citations: list[Citation] = []

    for ev in evidence:
        _harvest_spans(
            answer, ev.unit_text, ev, min_span_len=min_span_len, seen=seen, citations=citations
        )

    for run in _contiguous_runs(evidence):
        joined = "".join(ev.unit_text for ev in run)
        _harvest_spans(
            answer,
            joined,
            run[0],
            min_span_len=min_span_len,
            seen=seen,
            citations=citations,
        )

    return tuple(citations)


__all__ = [
    "COMPANY_KIND",
    "BounceResult",
    "Citation",
    "CitationGateError",
    "DoneResult",
    "EvidenceItem",
    "GateResult",
    "MalformedEvidenceError",
    "QUOTABLE_EVIDENCE_KINDS",
    "RefuseResult",
    "verify_citations",
]
