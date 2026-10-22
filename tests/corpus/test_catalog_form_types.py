"""Spec SC3: form types on the catalog.

Two halves, both binary:

* negative — ``form=10-K`` and ``form=8-K`` against the frozen AAPL
  rows return ``{"filings": []}``. Those rows are 10-Q, so a catalog
  that answered either alternate form would be inventing rows;
* positive — the same filters over a test catalog that DOES hold those
  forms return the matching rows.

Plus the output half of SC3: a hit carries ``form``, so the workspace
card and the model both see what kind of filing a row is. The other
four EDGAR-shaped fields (``cik`` / ``accession`` / ``filing_date`` /
``ticker``) stay filter-only — SC3 asks for the form on the card, not
for the whole submission header on every hit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.catalog import record_filings
from fin.host.tools.list_files import list_files

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "demo-corpus"


def _fixture_rows_by_form() -> dict[str, set[str]]:
    """``form`` -> the ``source_ref``s the fixture files declare for it.

    Read off disk rather than hard-coded: the positive case below asserts
    against the corpus that ``multi_form_corpus`` actually seeds, so a fixture
    that gains or loses a document moves the expectation instead of leaving a
    stale literal here. The demo fixture's ``form`` is an EDGAR form, and one
    form legitimately covers several documents.
    """
    rows: dict[str, set[str]] = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        # expected.json travels with the corpus as its question set, not as a
        # document -- the same distinction the seed makes.
        if path.name == "expected.json":
            continue
        envelope = json.loads(path.read_text(encoding="utf-8"))
        form = str(envelope["metadata"]["form"])
        rows.setdefault(form, set()).add(str(envelope["source_ref"]))
    return rows


#: Evaluated at collection so the parametrisation names real rows. Empty means
#: the fixture corpus is gone, and an empty parametrisation would report green
#: while testing nothing -- so it fails here instead.
_ROWS_BY_FORM = _fixture_rows_by_form()
if not _ROWS_BY_FORM:
    raise RuntimeError(
        f"no demo-corpus documents under {FIXTURE_DIR}; the SC3 positive case "
        "has no corpus to filter"
    )


def _frozen_twelve() -> dict[str, Path]:
    data_dir = Path(__file__).resolve().parents[2] / "data" / "corpus"
    return {"catalog_path": data_dir / "catalog.json"}


# ===== negative: the frozen AAPL rows hold neither alternate form =====


@pytest.mark.parametrize("form", ["10-K", "8-K"])
def test_form_filter_on_frozen_twelve_returns_empty_hits(form: str) -> None:
    """The frozen AAPL 10-Q rows have neither alternate form."""
    assert (
        list_files(form=form, ticker="AAPL", **_frozen_twelve())
        == {"filings": []}
    )


# ===== positive: a test catalog that holds those forms =====


@pytest.mark.parametrize(
    ("form", "expected_document_ids"),
    sorted(_ROWS_BY_FORM.items()),
)
def test_form_filter_returns_rows_when_the_corpus_holds_that_form(
    multi_form_corpus: dict[str, Path], form: str, expected_document_ids: set[str]
) -> None:
    """The same filter returns rows once the corpus has that form.

    Exact set equality, so the filter stays a genuine discriminator: a catalog
    that ignored ``form`` and returned the whole table would fail here rather
    than pass on a superset. The expectation is the set of ``source_ref``s the
    fixture files declare for this EDGAR form, not a hand-written id -- several
    fixture documents legitimately share one form.
    """
    result = list_files(form=form, **multi_form_corpus)

    assert {f["document_id"] for f in result["filings"]} == expected_document_ids
    assert all(f["form"] == form for f in result["filings"])


def test_unfiltered_listing_spans_every_form_in_the_corpus(
    multi_form_corpus: dict[str, Path],
) -> None:
    """An unfiltered listing is not silently narrowed to one form."""
    result = list_files(**multi_form_corpus)

    assert {f["form"] for f in result["filings"]} == {"10-K", "8-K", "10-Q"}


# ===== output: the hit carries the form =====


def test_frozen_twelve_hits_carry_their_form() -> None:
    """Every committed 10-Q hit reports the form it actually is.

    Plan C expanded the tier-1 corpus via 8 EDGAR iXBRL primary docs
    (all marked ``form="10-Q"``), and SC6 adds a separate GOOG 10-K
    row."""
    result = list_files(form="10-Q", **_frozen_twelve())

    assert len(result["filings"]) >= 20
    for filing in result["filings"]:
        assert filing["form"] == "10-Q"


def test_hit_form_is_none_when_the_row_has_no_form(tmp_path: Path) -> None:
    """A row with no form reports ``None`` rather than dropping the key.

    A missing key would make the card blank for a reason the operator
    cannot distinguish from a rendering bug; an explicit ``None`` says
    "this corpus row was never given a form".
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "NOFORM-2023Q1",
                "company": "NOFORM",
                "period": "2023 Q1",
                "version": "f" * 64,
                "page_count": 1,
                "title": "Noform filing",
                "doc_type": "narrative",
            }
        ],
        catalog_path=catalog_path,
    )

    result = list_files(catalog_path=catalog_path)

    assert result["filings"] == [
        {
            "document_id": "NOFORM-2023Q1",
            "company": "NOFORM",
            "period": "2023 Q1",
            "version": "f" * 64,
            "page_count": 1,
            "form": None,
        }
    ]
