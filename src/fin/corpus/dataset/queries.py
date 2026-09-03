"""Query set for the eval dataset (plan T10 / SC29, B1 查询分档).

Two kinds of "should-hit" annotations (问题 → 期待 locator 单元):

* **Manual gold annotations** (:data:`MANUAL_ANNOTATIONS`) — questions
  written by hand for this ticket against *real* frozen units of the
  private financial fixtures. Each record pins the exact frozen text that
  answers the question (``evidence_span``), and the builder verifies
  every span is a contiguous substring of the pinned unit before the
  dataset is accepted. These are annotations over real content, not
  synthetic samples — nothing here counts toward the *source-document*
  floors, which only real filings feed.
* **Deterministic phrase probes** (:func:`generate_phrase_queries`) —
  queries derived mechanically from distinctive verbatim lines of the
  frozen units. Selection is order-driven and repeatable, so a rebuild
  reproduces the same set byte-for-byte.

Query classes (four, each must hold >= 10% of the set — B1 查询分档):

* ``factoid`` — single-period numeric fact from a statement page.
* ``narrative`` — comprehension question over MD&A / certification /
  risk prose.
* ``phrase`` — verbatim phrase probe (exact-substring retrieval).
* ``cross_period`` — question whose answer needs the comparative
  period column of a statement page.

The locator ``version`` is NOT hardcoded here: the builder fills it
from the corpus manifest registry, and manifest validation re-checks
it against the frozen sha256 of the native document.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

from fin.corpus.dataset.pages import DOCUMENT_TYPES, FrozenUnit, classify_page

# --------------------------------------------------------------------------
# Manual gold annotations (question -> expected native locator unit)
# --------------------------------------------------------------------------
# Fields: query_id, query_class, doc_type, document_id, unit, question,
# evidence_span. All evidence spans below were read out of the frozen
# page text of the extracted tree when this ticket was implemented; the
# builder refuses to build if any span is not an exact substring.

M = lambda **kw: kw  # noqa: E731  (annotation literal shorthand)

MANUAL_ANNOTATIONS: tuple[dict, ...] = tuple(load_private_fixture("d533c50039c5bf27c78a31a1f2576ffe58574b2835b6c8f3f60d12bf1dac9f3c"))
del M

assert len(MANUAL_ANNOTATIONS) == 40
assert {a["query_id"] for a in MANUAL_ANNOTATIONS}.__len__() == 40
assert all(a["doc_type"] in DOCUMENT_TYPES for a in MANUAL_ANNOTATIONS)


def verify_manual_annotations(
    annotations: tuple[dict, ...],
    pages_by_doc: dict[str, dict[int, str]],
) -> list[str]:
    """Check every manual annotation's evidence span against the real tree.

    Returns a list of problems (empty = all spans are contiguous
    substrings of their pinned frozen unit). This is what makes the
    "人工标注" auditable: an annotation whose span drifted (or was
    written against a page that does not exist) fails the build with a
    typed error instead of silently entering the dataset.
    """
    problems: list[str] = []
    for ann in annotations:
        doc_id = ann["document_id"]
        unit = ann["unit"]
        pages = pages_by_doc.get(doc_id)
        if pages is None or unit not in pages:
            problems.append(f"{ann['query_id']}: {doc_id} unit {unit} not in extracted tree")
            continue
        text = pages[unit]
        if ann["evidence_span"] not in text:
            problems.append(
                f"{ann['query_id']}: evidence_span is not a contiguous substring of "
                f"{doc_id} unit {unit}"
            )
    return problems


# --------------------------------------------------------------------------
# Deterministic phrase probes
# --------------------------------------------------------------------------

MIN_LINE_CHARS = 48
MAX_LINE_CHARS = 240
MIN_WORDS = 7
MAX_DIGIT_CHARS = 8


def _is_distinctive_prose_line(line: str) -> bool:
    """Order-driven filter for phrase-probe candidates.

    A candidate is a verbatim single line of frozen prose: long enough
    to be distinctive, short enough to quote, mostly alphabetic, and
    not a table row. No rewriting: the query text is the exact frozen
    substring, so a future citation-gate span check against the unit
    text will match character-for-character.
    """
    if not (MIN_LINE_CHARS <= len(line) <= MAX_LINE_CHARS):
        return False
    words = line.split()
    if len(words) < MIN_WORDS:
        return False
    alpha = sum(1 for ch in line if ch.isalpha())
    digits = sum(1 for ch in line if ch.isdigit())
    if digits > MAX_DIGIT_CHARS:
        return False
    if alpha * 5 < len(line) * 3:  # < 60% alphabetic: not prose
        return False
    from fin.corpus.dataset.pages import _NUMBER_TOKEN

    if len(_NUMBER_TOKEN.findall(line)) >= 2:
        return False
    if not any(ch.islower() for ch in line):
        return False
    return True


def generate_phrase_queries(
    units_by_doc: dict[str, list[FrozenUnit]],
    *,
    target: int,
    per_doc_cap: int = 6,
) -> list[dict]:
    """Deterministically select phrase-probe queries from real units.

    Iterates documents in sorted id order, units in ascending unit
    order, and takes the first qualifying distinctive line per unit,
    at most ``per_doc_cap`` per document, until ``target`` queries are
    collected. The same input always yields the same output (no
    randomness, no set iteration).
    """
    queries: list[dict] = []
    counter = 0
    for doc_id in sorted(units_by_doc):
        doc_text = "\n".join(u.text for u in units_by_doc[doc_id])
        taken = 0
        for unit in sorted(units_by_doc[doc_id], key=lambda u: u.unit):
            if taken >= per_doc_cap or len(queries) >= target:
                break
            for line in unit.text.splitlines():
                if len(queries) >= target or taken >= per_doc_cap:
                    break
                if not _is_distinctive_prose_line(line):
                    continue
                if doc_text.count(line) != 1:
                    continue
                counter += 1
                taken += 1
                queries.append(
                    {
                        "query_id": f"q-phrase-{counter:03d}",
                        "query_class": "phrase",
                        "doc_type": classify_page(unit.text),
                        "question": line,
                        "evidence_span": line,
                        "document_id": doc_id,
                        "unit": unit.unit,
                        "annotation": "derived_span",
                    }
                )
    if len(queries) < target:
        raise ValueError(
            f"phrase-probe generator produced {len(queries)} queries, target {target}"
        )
    return queries
