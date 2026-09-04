"""Deterministic document-type classification (corpus-serve-ingest T3).

:func:`classify_page` assigns one of the three document types of the
chunk-parameter table's type axis (``narrative`` / ``table_dense`` /
``clause_list``) to a span of frozen unit text. It is the SINGLE
classifier for that axis: the eval dataset builder classifies pages
with it (``fin.corpus.dataset.pages`` re-exports it) and the corpus
ingest endpoint derives its default document type with it, so the two
callers can never drift into two integer rules.

Everything here is pure integer/string logic with no clock, no
randomness, no set/dict iteration order dependence — the determinism
discipline SC12/SC13 pin (integer arithmetic only, no float
thresholds, no model judgment).

This module deliberately lives OUTSIDE the ``fin.corpus.dataset``
package: the dataset package's ``__init__`` drags in the eval build
stack (exporters, live fetch), which must never sit on the serve path
just to classify one document type.
"""

from __future__ import annotations

import re

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


__all__ = [
    "CLAUSE_LIST",
    "DOCUMENT_TYPES",
    "NARRATIVE",
    "TABLE_DENSE",
    "classify_page",
]
