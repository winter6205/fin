"""Spec SC3: form types on the catalog.

Two halves, both binary:

* negative — ``form=10-K`` and ``form=8-K`` against the frozen-12-only
  fixture return ``{"filings": []}``. The frozen 12 are 10-Q, so a
  catalog that answered anything else would be inventing rows;
* positive — the same filters over a test corpus that DOES hold those
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

from fin.host.tools.list_files import list_files


def _frozen_twelve() -> dict[str, Path]:
    data_dir = Path(__file__).resolve().parents[2] / "data" / "corpus"
    return {
        "manifest_path": data_dir / "manifest.json",
        "extracted_root": data_dir / "extracted",
    }


# ===== negative: the frozen-12-only fixture holds neither form =====


@pytest.mark.parametrize("form", ["10-K", "8-K"])
def test_form_filter_on_frozen_twelve_returns_empty_hits(form: str) -> None:
    """``form=10-K`` / ``form=8-K`` on the frozen 12 -> ``{"filings": []}``."""
    assert list_files(form=form, **_frozen_twelve()) == {"filings": []}


# ===== positive: a test corpus that holds those forms =====


@pytest.mark.parametrize(
    ("form", "expected_document_id"),
    [("10-K", "ZEUS-2023-10K"), ("8-K", "ZEUS-2023-8K")],
)
def test_form_filter_returns_rows_when_the_corpus_holds_that_form(
    multi_form_corpus: dict[str, Path], form: str, expected_document_id: str
) -> None:
    """The same filter returns rows once the corpus has that form."""
    result = list_files(form=form, **multi_form_corpus)

    assert [f["document_id"] for f in result["filings"]] == [
        expected_document_id
    ]
    assert result["filings"][0]["form"] == form


def test_unfiltered_listing_spans_every_form_in_the_corpus(
    multi_form_corpus: dict[str, Path],
) -> None:
    """An unfiltered listing is not silently narrowed to one form."""
    result = list_files(**multi_form_corpus)

    assert {f["form"] for f in result["filings"]} == {"10-K", "8-K", "10-Q"}


# ===== output: the hit carries the form =====


def test_frozen_twelve_hits_carry_their_form() -> None:
    """Every frozen-12 hit reports the 10-Q it actually is."""
    result = list_files(**_frozen_twelve())

    assert len(result["filings"]) == 12
    for filing in result["filings"]:
        assert filing["form"] == "10-Q"


def test_hit_form_is_none_when_the_manifest_row_has_no_form(
    tmp_path: Path,
) -> None:
    """A row with no form reports ``None`` rather than dropping the key.

    A missing key would make the card blank for a reason the operator
    cannot distinguish from a rendering bug; an explicit ``None`` says
    "this corpus row was never given a form".
    """
    version = "f" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": "NOFORM-2023Q1",
                        "company": "NOFORM",
                        "period": "2023 Q1",
                        "sha256": version,
                    }
                ]
            }
        )
    )
    doc_dir = tmp_path / "extracted" / "NOFORM-2023Q1"
    doc_dir.mkdir(parents=True)
    text = "NOFORM-2023Q1 page 1 body"
    (doc_dir / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": "NOFORM-2023Q1",
                "version": version,
                "page": 1,
                "text": text,
            }
        )
    )
    (doc_dir / "passages-001.json").write_text(
        json.dumps(
            {
                "passages": [
                    {"text": text, "char_start": 0, "char_end": len(text)}
                ]
            }
        )
    )

    result = list_files(
        manifest_path=manifest_path,
        extracted_root=tmp_path / "extracted",
    )

    assert result["filings"] == [
        {
            "document_id": "NOFORM-2023Q1",
            "company": "NOFORM",
            "period": "2023 Q1",
            "version": version,
            "page_count": 1,
            "form": None,
        }
    ]
