"""The seed fact must be evidenced by the frozen unit it cites.

Spec ``specs/s1-corpus-retrieve.md`` (§ Objective / § Boundaries,
``get_xbrl_fact``): "A numeric citation gate matches the claimed
value to that row; the model does not invent the arithmetic." The
gate can only be a grounding check if the row it matches against is
itself grounded — a seed whose value does not appear on the page it
cites would certify a fabricated number as evidenced.

These tests bind the seed in ``fin.corpus.facts_seed`` (and the
committed sidecar under ``data/corpus/facts/``) to the frozen locator
unit named by each row's ``source_locator``:

* the seeded numeral, rendered the way the filing prints it, is a
  literal substring of that unit's frozen text;
* the seeded ``scale`` matches the unit-of-measure the page declares,
  so the numeral needs no invented arithmetic to be read as the value;
* the locator's ``version`` is the frozen unit's own version;
* the committed sidecar under ``data/corpus/facts/`` carries exactly
  the rows the seed emits, so the shipped fact table cannot drift away
  from the reviewed seed.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

import json
import re
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, fact_matches_value
from fin.corpus.facts_seed import seed_facts_for_document


_AAPL_2023Q3_ID = "AAPL-2023Q3"
_AAPL_2023Q3_VERSION = (
    load_private_fixture("5cae37674ca6bf538f54bca53ca28b15aaf96dd1b7ae386553bd32387f254b73")
)

# The statement-wide unit-of-measure parenthetical a filing prints
# directly under the statement title, e.g. "(In millions, except number
# of shares which are reflected in thousands and per share amounts)".
# Only this header denominates the statement's rows; the trailing
# "reflected in thousands" clause covers the share-count rows alone.
_UNIT_HEADER_RE = re.compile(
    r"^\(In (thousands|millions|billions)\b", re.MULTILINE | re.IGNORECASE
)


def _corpus_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _frozen_unit_text(locator: Locator) -> str:
    """Return the frozen text of the locator unit, asserting the unit's
    recorded ``version`` matches the locator's."""
    page_path = (
        _corpus_dir()
        / "extracted"
        / locator.document_id
        / f"page-{locator.page:03d}.json"
    )
    assert page_path.is_file(), f"cited unit missing on disk: {page_path}"
    payload = json.loads(page_path.read_text())
    assert payload["version"] == locator.version, (
        f"{locator.document_id} page {locator.page} froze version "
        f"{payload['version']!r}, seed cites {locator.version!r}"
    )
    return payload["text"]


def _seed() -> list[Fact]:
    facts = seed_facts_for_document(
        document_id=_AAPL_2023Q3_ID, version=_AAPL_2023Q3_VERSION
    )
    assert facts, "AAPL-2023Q3 seed must not be empty"
    return facts


def _printed_forms(value: float | int | str) -> list[str]:
    """Every way the filing could print ``value`` verbatim: bare digits
    and the thousands-separated form. No rescaling — a seed that needs
    arithmetic to be found on its page is exactly what we reject.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, int):
        return [str(value), f"{value:,}"]
    if value.is_integer():
        whole = int(value)
        return [str(whole), f"{whole:,}", str(value), f"{whole:,}.0"]
    return [str(value), f"{value:,}"]


@pytest.mark.parametrize("fact", _seed(), ids=lambda f: f.concept)
def test_seeded_numeral_is_substring_of_cited_unit_text(fact: Fact) -> None:
    """The seeded value must appear verbatim in the frozen text of the
    unit it cites, so the numeric gate matches a number the source
    actually prints."""
    text = _frozen_unit_text(fact.source_locator)
    forms = _printed_forms(fact.value)
    assert any(form in text for form in forms), (
        f"{fact.concept} value {fact.value!r} is unevidenced: none of "
        f"{forms} appears in {fact.source_locator.document_id} page "
        f"{fact.source_locator.page}"
    )


@pytest.mark.parametrize("fact", _seed(), ids=lambda f: f.concept)
def test_seeded_scale_matches_cited_unit_header(fact: Fact) -> None:
    """``scale`` must be the unit-of-measure the cited page declares.

    A numeral printed under "(In millions ...)" seeded as ``thousands``
    is off by 1000x for any consumer that applies the scale.
    """
    text = _frozen_unit_text(fact.source_locator)
    header = _UNIT_HEADER_RE.search(text)
    assert header is not None, (
        f"page {fact.source_locator.page} declares no unit-of-measure "
        f"header; a scaled seed cannot cite it"
    )
    declared = header.group(1).lower()
    assert fact.scale == declared, (
        f"page {fact.source_locator.page} is denominated in {declared!r} "
        f"({header.group(0)!r}), seed claims scale={fact.scale!r}"
    )


def test_seed_revenues_row_is_apples_nine_month_total_net_sales() -> None:
    """[private source excerpt removed]"""
    (fact,) = [f for f in _seed() if f.concept == "us-gaap:Revenues"]
    text = _frozen_unit_text(fact.source_locator)

    row = re.search(r"Total net sales[\d,\s]+", text)
    assert row is not None, "page 4 must carry the 'Total net sales' row"
    assert load_private_fixture("c18591bcf0070a92596e8fa66285e8cb7eafe482710c7f278506d2292ef3c207") in row.group(0)
    assert "383,285" not in text
    assert "383285" not in text

    assert fact.value == load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94")
    assert fact.unit == "USD"
    assert fact.scale == "millions"
    assert fact.period == "2023-Q3-YTD"


def test_numeric_gate_comparison_tracks_the_evidenced_numeral() -> None:
    """``fact_matches_value`` compares against the raw ``value``; the
    reseeded row therefore matches the numeral the page prints and
    rejects the old full-year figure."""
    (fact,) = [f for f in _seed() if f.concept == "us-gaap:Revenues"]

    assert fact_matches_value(fact, load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94")) is True
    assert fact_matches_value(fact, load_private_fixture("df9e4df0d00599a1820d5afef6c4cd83e960f0e8bc8f1a76340084c8c5597f23")) is True
    # Old (wrong) seed: FY2023 total net sales, in whole USD.
    assert fact_matches_value(fact, 383285000000) is False


def test_committed_fact_file_matches_the_seed() -> None:
    """The committed sidecar is the ingest output of the seed; a drift
    between them means the shipped fact table is not the reviewed one.
    """
    on_disk = json.loads(
        (_corpus_dir() / "facts" / f"{_AAPL_2023Q3_ID}.json").read_text()
    )
    entries = on_disk["facts"]
    seed = _seed()
    assert len(entries) == len(seed)
    for entry, fact in zip(entries, seed):
        assert entry["concept"] == fact.concept
        assert entry["value"] == fact.value
        assert entry["period"] == fact.period
        assert entry["unit"] == fact.unit
        assert entry["scale"] == fact.scale
        assert entry["source_locator"] == {
            "document_id": fact.source_locator.document_id,
            "version": fact.source_locator.version,
            "page": fact.source_locator.page,
        }
