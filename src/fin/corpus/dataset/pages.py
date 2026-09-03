"""Page-level deterministic helpers for the eval dataset (plan T10).

Everything in this module is pure integer/string logic with no clock,
no randomness, no set/dict iteration order dependence (it iterates
caller-supplied ordered lists), so two runs on the same input produce
identical output — the contract SC12/SC14 pin for the whole dataset
pipeline.

Three responsibilities:

* :func:`classify_page` — assign one of the three required document
  types (``narrative`` / ``table_dense`` / ``clause_list``) to a page
  of frozen unit text. Integer arithmetic only (SC13 discipline: no
  float thresholds, no model judgment).
* :func:`ensure_all_types_present` — deterministic redistribution so
  every native document contributes at least one unit of each of the
  three types (the dataset must cover all three document types).
* :func:`detect_repeated_lines` — find the genuinely repeated
  header / footer lines of a filing (real page furniture such as
  ``Table of Contents``), used both as the PDF hard-sample marker
  evidence and by the header/footer-interference hard variants.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from fin.corpus.errors import IngestMissingError

NARRATIVE = "narrative"
TABLE_DENSE = "table_dense"
CLAUSE_LIST = "clause_list"
DOCUMENT_TYPES = (NARRATIVE, TABLE_DENSE, CLAUSE_LIST)

# Number token: comma-grouped integer or decimal, optionally wrapped
# in accounting parens and/or preceded by a currency sign. Word-boundary
# guards keep ``June 25, 2022``-style prose out of the count only
# partially — deliberately: statement pages mix prose labels with
# number rows, and the threshold below accounts for that.
_NUMBER_TOKEN = re.compile(r"(?<![\w,.])[($]?\d[\d,]*(?:\.\d+)?\)?(?![\w])")
# Clause / list marker at line start: ``1.``, ``(a)``, ``•``, ``·``.
_CLAUSE_START = re.compile(r"^\s*(\(?[a-z0-9]{1,3}[\).]|•|·)\s")

# table_dense when numeric rows >= 1/4 of non-empty lines (integer form:
# numeric_rows * 4 >= total).
_TABLE_NUMERATOR = 4
# clause_list when at least this many clause-marker lines.
_CLAUSE_MIN_LINES = 3


@dataclass(frozen=True)
class FrozenUnit:
    """One native frozen unit: the 1-based page number and its frozen text."""

    unit: int
    text: str


def classify_page(text: str) -> str:
    """Classify one page of frozen text into a document type.

    Deterministic integer logic; ``narrative`` is the fallback for any
    page that is neither table-dense nor clause-heavy.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    total = len(lines)
    if total == 0:
        return NARRATIVE
    numeric_rows = sum(1 for line in lines if len(_NUMBER_TOKEN.findall(line)) >= 2)
    if numeric_rows * _TABLE_NUMERATOR >= total:
        return TABLE_DENSE
    clause_lines = sum(1 for line in lines if _CLAUSE_START.match(line))
    if clause_lines >= _CLAUSE_MIN_LINES:
        return CLAUSE_LIST
    return NARRATIVE


def classify_units(units: list[FrozenUnit]) -> dict[str, list[FrozenUnit]]:
    """Bucket every unit into its document type, in ascending unit order.

    Raises :class:`IngestMissingError` when a document has fewer than
    three non-empty units — it cannot then contribute all three
    document types and the dataset builder must refuse rather than
    invent content.
    """
    with_text = [u for u in units if u.text.strip()]
    if len(with_text) < len(DOCUMENT_TYPES):
        raise IngestMissingError(
            f"document has {len(with_text)} non-empty frozen units; "
            f"at least {len(DOCUMENT_TYPES)} are required to cover all "
            f"document types"
        )
    buckets: dict[str, list[FrozenUnit]] = {t: [] for t in DOCUMENT_TYPES}
    for u in sorted(with_text, key=lambda u: u.unit):
        buckets[classify_page(u.text)].append(u)
    return buckets


def ensure_all_types_present(
    buckets: dict[str, list[FrozenUnit]],
) -> dict[str, list[FrozenUnit]]:
    """Deterministically redistribute units so no type bucket is empty.

    When a document's own classification leaves one of the three
    required document types with zero units, the largest bucket
    donates from its highest-numbered units (stable, order-driven —
    no randomness) until every bucket holds at least one unit. The
    donated unit's *content* is unchanged real frozen text; only its
    document-type assignment in the derived sample is by allocation,
    which the manifest records via ``source_units``.
    """
    buckets = {t: list(buckets[t]) for t in DOCUMENT_TYPES}

    def _donor_order() -> list[str]:
        # Largest bucket first; ties broken by DOCUMENT_TYPES order.
        return sorted(
            DOCUMENT_TYPES, key=lambda t: (-len(buckets[t]), DOCUMENT_TYPES.index(t))
        )

    for target in DOCUMENT_TYPES:
        while not buckets[target]:
            donors = [t for t in _donor_order() if len(buckets[t]) > 1]
            if not donors:
                raise IngestMissingError(
                    "cannot cover all document types: every bucket is "
                    "single-unit while some bucket is still empty"
                )
            donor = donors[0]
            buckets[target].append(buckets[donor].pop())
    for target in DOCUMENT_TYPES:
        buckets[target].sort(key=lambda u: u.unit)
    return buckets


def detect_repeated_lines(units: list[FrozenUnit]) -> tuple[str, str]:
    """Return the ``(header_line, footer_line)`` genuinely repeated on pages.

    A line qualifies when it appears (stripped) at the same edge of at
    least three pages and is at least three characters long. Ties break
    by first-seen unit order, which is deterministic. When nothing
    repeats, the first page's first line and the last page's last line
    are returned (still real page furniture of that filing).
    """
    ordered = sorted((u for u in units if u.text.strip()), key=lambda u: u.unit)
    if not ordered:
        raise IngestMissingError("cannot detect repeated lines: no non-empty units")

    def _most_common(edge: int) -> str | None:
        counter: Counter[str] = Counter()
        first_seen: dict[str, int] = {}
        for u in ordered:
            lines = [l.strip() for l in u.text.splitlines() if l.strip()]
            if not lines:
                continue
            idx = edge if edge < len(lines) else len(lines) - 1
            line = lines[idx]
            if len(line) < 3:
                continue
            counter[line] += 1
            first_seen.setdefault(line, u.unit)
        if not counter:
            return None
        best = max(counter.values())
        if best < 3:
            return None
        candidates = [l for l, c in counter.items() if c == best]
        candidates.sort(key=lambda l: first_seen[l])
        return candidates[0]

    header = _most_common(0)
    footer = _most_common(-1)
    first_lines = [l.strip() for l in ordered[0].text.splitlines() if l.strip()]
    last_lines = [l.strip() for l in ordered[-1].text.splitlines() if l.strip()]
    return (header or first_lines[0], footer or last_lines[-1])


def is_repeated_page_furniture(units: list[FrozenUnit]) -> bool:
    """True when the filing has a genuinely repeated header or footer line.

    This is the evidence the manifest records for marking a frozen
    tier-1 PDF as a hard sample of kind ``header_footer``: 10-Q filings
    whose pages carry a repeated running head (e.g. ``Table of
    Contents``) and/or repeated footer text interfere with naive page
    parsing.
    """
    try:
        header, footer = detect_repeated_lines(units)
    except IngestMissingError:
        return False
    counter: Counter[str] = Counter()
    for u in units:
        lines = [l.strip() for l in u.text.splitlines() if l.strip()]
        if lines and lines[0] == header:
            counter["header"] += 1
        if lines and lines[-1] == footer:
            counter["footer"] += 1
    return counter["header"] >= 3 or counter["footer"] >= 3


def split_table_row(line: str) -> list[str]:
    """Split one frozen table row into cells, deterministically.

    Cells break before each standalone number token (including
    accounting parens and ``$``-prefixed amounts, whose ``$`` stays
    attached) so a row like ``Total net sales $ 19,442 $ 21,744``
    becomes ``["Total net sales", "$ 19,442", "$ 21,744"]``. The frozen
    text is not modified — this only drives the hard merged-cell
    variants.
    """
    tokens = list(_NUMBER_TOKEN.finditer(line))
    if len(tokens) < 2:
        return [line.strip()]
    cells: list[str] = []
    pos = 0
    for token in tokens:
        start = token.start()
        # Keep a "$ " prefix attached to its amount.
        if start - 2 >= pos and line[start - 2 : start] == "$ ":
            start -= 2
        elif start - 1 >= pos and line[start - 1] == "$":
            start -= 1
        label = line[pos:start].strip()
        if label:
            cells.append(label)
        cells.append(line[start : token.end()].strip())
        pos = token.end()
    tail = line[pos:].strip()
    if tail:
        cells.append(tail)
    return [c for c in cells if c]
