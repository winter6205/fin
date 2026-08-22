"""Citation gate for the v1 host agent (ADR 0008, ADR 0021).

This module is the **only** module in ``src/fin/host/`` that decides
whether the model's answer is grounded in evidence from this run's
tool calls. It runs on the path to END in the host graph (T6); it does
NOT call a model, the network, or a database.

The public API is the pure function ``verify_citations``. It returns one
of three typed outcomes:

- ``DoneResult`` — the answer contains at least one ≥``min_span_len``
  extractive span that is a contiguous substring of an evidence
  item's ``page_text``. ``citations`` is the deduplicated list of
  ``(locator, quote)`` pairs the user-visible citation list contains.

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable

from fin.corpus.extracted import Locator


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
    "≥32-character span copied verbatim from a page you retrieved "
    "or read this run."
)


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence item the gate may match against.

    ``page_text`` is whatever the upstream tool produced for that page
    (or, for retrieve chunks, the passage slice — both are slices of
    the committed extracted tree). The gate matches
    ``≥min_span_len`` substrings of ``answer`` against
    ``page_text`` only; it does NOT reconstruct whole pages on the
    fly (the gate's evidence list already has the right granularity).
    """

    locator: Locator
    page_text: str


@dataclass(frozen=True)
class Citation:
    """A passed citation.

    ``document_id`` / ``version`` / ``page`` always come from the
    matched evidence item's locator — never from the model's claim.
    This prevents model-invented ``version`` strings (ADR 0021).

    ``quote`` is the ≥``min_span_len`` substring of the answer that
    matched; the gate keeps the verbatim span so downstream rendering
    can echo it as a quoted excerpt.
    """

    document_id: str
    version: str
    page: int
    quote: str


# ----- result types -----


@dataclass(frozen=True)
class DoneResult:
    """Gate accepted: ≥1 ≥``min_span_len`` span is grounded in evidence.

    ``citations`` is the deduplicated list of
    ``(locator, quote)`` pairs (a unique ``(document_id, version,
    page, quote)`` tuple is at most one ``Citation``). The host graph
    forwards ``answer`` and ``citations`` to the CLI/HTTP surface.
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
    """An evidence item has an empty ``page_text`` or an invalid
    locator (``page < 1`` / empty ``document_id`` / empty
    ``version``).

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
) -> GateResult:
    """Verify the model's answer is grounded in this run's evidence.

    Parameters
    ----------
    answer
        The model's final answer prose. Treated as an opaque UTF-8
        string; the gate never decodes / re-encodes it.
    evidence
        The union of this run's ``read_file`` page texts and
        ``retrieve`` chunk texts, each carrying its
        ``{document_id, version, page}`` locator. Empty evidence is
        allowed; the gate then falls through to the bounce path
        (or refuse, if ``repair_count`` already hit ``repair_cap``).
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

    Returns
    -------
    GateResult
        One of ``DoneResult``, ``BounceResult``, ``RefuseResult``.
        Discriminate on ``outcome`` (always ``"done"`` / ``"bounce"``
        / ``"refuse"``).

    Raises
    ------
    MalformedEvidenceError
        If any item in ``evidence`` has ``page_text == ""``,
        ``locator.page < 1``, or empty ``document_id`` / ``version``.
        The gate does NOT silently skip malformed items — ADR 0021
        forbids hidden drops.
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
        answer, evidence_tuple, min_span_len=min_span_len
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
    if not isinstance(item.page_text, str):
        raise MalformedEvidenceError(
            f"evidence[{index}].page_text must be str, "
            f"got {type(item.page_text).__name__}",
            index=index,
        )
    if item.page_text == "":
        raise MalformedEvidenceError(
            f"evidence[{index}].page_text is empty",
            index=index,
        )
    locator = item.locator
    if not isinstance(locator, Locator):
        raise MalformedEvidenceError(
            f"evidence[{index}].locator must be Locator, "
            f"got {type(locator).__name__}",
            index=index,
        )
    if not isinstance(locator.page, int) or locator.page < 1:
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.page must be int ≥ 1, "
            f"got {locator.page!r}",
            index=index,
        )
    if not locator.document_id:
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.document_id is empty",
            index=index,
        )
    if not locator.version:
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.version is empty",
            index=index,
        )


def _match_spans(
    answer: str,
    evidence: tuple[EvidenceItem, ...],
    *,
    min_span_len: int,
) -> tuple[Citation, ...]:
    """Find all maximal ≥``min_span_len`` substrings of ``answer``
    that appear in any evidence item's ``page_text``.

    Returns ``Citation`` objects deduplicated by
    ``(locator, quote)``, in deterministic order: by evidence order
    (caller's order), then by left-to-right position in the answer.
    The order is the order ``(locator, quote)`` was first discovered;
    repeats are dropped silently.

    Algorithm: for each evidence item, walk ``answer`` left-to-right.
    At each starting position ``i``, greedily extend the match
    (``answer[i:j] in evidence_text``) until the next character would
    fail. The longest match starting at ``i`` is recorded (if it
    meets ``min_span_len`` and contains an alphanumeric token) and
    we skip past it; otherwise we advance one position. The greedy
    rule guarantees the longest match per starting position; the
    "skip past the match" rule prevents nested-match explosions
    without losing non-overlapping spans.
    """
    n = len(answer)
    if n < min_span_len:
        return ()

    seen: set[tuple[Locator, str]] = set()
    citations: list[Citation] = []

    for ev in evidence:
        ev_text = ev.page_text
        ev_len = len(ev_text)
        if ev_len == 0:
            # Already validated to be non-empty, but guard anyway so
            # an empty ``page_text`` (which would mean every
            # substring search returns False immediately) cannot
            # loop pointlessly. This is a defensive branch that
            # should be unreachable after ``_validate_evidence``.
            continue

        i = 0
        while i <= n - min_span_len:
            # Greedy longest match starting at i.
            j = i + min_span_len
            # Cap the extension by both the answer length and the
            # evidence length: ``answer[i:j]`` cannot be a substring
            # of ``ev_text`` if its length exceeds ``ev_len``.
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
                    key = (ev.locator, quote)
                    if key not in seen:
                        seen.add(key)
                        citations.append(
                            Citation(
                                document_id=ev.locator.document_id,
                                version=ev.locator.version,
                                page=ev.locator.page,
                                quote=quote,
                            )
                        )
                # Skip past this match (whether or not we recorded
                # it — any shorter substring at the same start is
                # either no-alnum or non-matching, by definition of
                # the greedy longest-match).
                i += match_len
            else:
                i += 1

    return tuple(citations)


__all__ = [
    "BounceResult",
    "Citation",
    "CitationGateError",
    "DoneResult",
    "EvidenceItem",
    "GateResult",
    "MalformedEvidenceError",
    "RefuseResult",
    "verify_citations",
]