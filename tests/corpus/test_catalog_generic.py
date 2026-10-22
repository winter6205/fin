"""Generic (metadata-free) catalog rows, read model and grouping (T6).

The frozen contract (``specs/qa-chain-corpus-retrieval.md`` sections
"Catalog fields and compatibility projection" and "Metadata-free ingest
and document identity") splits the catalog into three surfaces that
must not drift:

* the **physical row** — ``document_id`` / ``version`` / ``title`` /
  ``doc_type`` required non-empty, ``page_count`` a non-negative int,
  and ``company`` / ``period`` OPTIONAL (omitted, never invented as
  ``""``);
* the **generic read model** — ``document_id``, ``title``,
  ``doc_type``, ``version``, ``page_count`` plus present ``company`` /
  ``period``;
* the **financial compatibility projection** — ``list_filings`` keeps its
  exact six-key set for the existing financial callers, and legacy
  ``cik`` / ``form`` / ``ticker`` / ``filing_date`` survive on the
  physical row so those reads do not lose data.

Grouping is presentation-only: ``doc_type`` / ``company`` / ``period``
are the only candidate dimensions (``title`` never groups), only
dimensions with >= 2 distinct non-empty values are shown, missing values
land under 未分类, and a corpus with no qualifying dimension falls back
to a flat title list.

Every case here uses ``tmp_path``; nothing reads the development
catalog (``test-storage.md``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.catalog import (
    UNCLASSIFIED_LABEL,
    catalog_document_titles,
    catalog_grouping,
    list_catalog_entries,
    list_filings,
    record_filings,
)
from fin.corpus.errors import CatalogRowError


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "doc_" + "a" * 64,
        "version": "a" * 64,
        "title": "Vendor onboarding checklist",
        "doc_type": "narrative",
        "page_count": 3,
    }
    row.update(overrides)
    return row


# ---------- physical row: company / period are optional ----------


def test_a_row_without_company_or_period_is_accepted(tmp_path: Path) -> None:
    """A metadata-free upload's physical row needs no financial identity.

    The two fields are optional, so the row that makes an ordinary
    enterprise document catalog-visible at all must validate. Under the
    old required-string contract this was a ``CatalogRowError`` and the
    document was pushed but invisible.
    """
    catalog_path = tmp_path / "catalog.json"

    assert record_filings([_row()], catalog_path=catalog_path) == 1

    stored = json_rows(catalog_path)
    # ``extract_method`` is the one field the writer adds on its own
    # (ADR 0079 default); it is never a financial identity.
    assert stored == [{**_row(), "extract_method": "standard"}]
    assert "company" not in stored[0]
    assert "period" not in stored[0]


def test_a_present_company_or_period_is_kept_verbatim(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"

    record_filings(
        [_row(company="  Acme  ", period="2024Q1")], catalog_path=catalog_path
    )

    stored = json_rows(catalog_path)[0]
    assert stored["company"] == "  Acme  "
    assert stored["period"] == "2024Q1"


def test_source_sha256_equals_version_when_the_writer_supplies_one(
    tmp_path: Path,
) -> None:
    """The spec's "if a new row writes source_sha256, it equals version".

    A row that names the same digest for both fields writes and keeps
    the equality — the positive half of the contract.
    """
    catalog_path = tmp_path / "catalog.json"

    record_filings(
        [_row(source_sha256="a" * 64)], catalog_path=catalog_path
    )

    stored = json_rows(catalog_path)[0]
    assert stored["source_sha256"] == stored["version"]


def test_a_source_sha256_that_disagrees_with_version_is_refused(
    tmp_path: Path,
) -> None:
    """The spec's teeth: the equality is a WRITE-time constraint, not a
    convention the writer merely hopes holds.

    A mismatched pair would leave two different digests on one row: a
    reader cannot tell which one names the bytes, and the row's own
    identity (``version``) disagrees with the digest the ingest side
    recorded. The writer refuses it as the typed ``CatalogRowError``
    before any row lands, rather than trusting the caller.
    """
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError):
        record_filings(
            [_row(version="a" * 64, source_sha256="b" * 64)],
            catalog_path=catalog_path,
        )

    assert catalog_path.exists() is False, (
        "a refused row must not leave a table behind"
    )


def test_a_legacy_row_stored_with_a_mismatched_digest_still_reads(
    tmp_path: Path,
) -> None:
    """The read path is NOT tightened by the write rule.

    A row an earlier writer stored is the compatibility surface; the
    ``source_sha256 == version`` rule is about what a NEW row writes,
    so readably recovering an existing row whose digests disagree is
    what keeps that compatibility promise instead of turning a stored
    row into a corrupt table.
    """
    catalog_path = tmp_path / "catalog.json"
    legacy = _row(version="a" * 64, source_sha256="b" * 64)
    catalog_path.write_text(json.dumps({"filings": [legacy]}), encoding="utf-8")

    assert json_rows(catalog_path) == [legacy]
    assert list_catalog_entries(catalog_path=catalog_path)[0]["document_id"] == (
        "doc_" + "a" * 64
    )


def test_legacy_financial_fields_survive_a_generic_write(tmp_path: Path) -> None:
    """``cik`` / ``form`` / ``ticker`` / ``filing_date`` are preserved.

    The generic path adds fields; it never renames or deletes the ones
    the financial compatibility reads still need.
    """
    catalog_path = tmp_path / "catalog.json"

    record_filings(
        [
            _row(
                company="AAPL",
                period="2023Q3",
                cik="0000320193",
                form="10-Q",
                ticker="AAPL",
                filing_date="2023-11-03",
            )
        ],
        catalog_path=catalog_path,
    )

    stored = json_rows(catalog_path)[0]
    assert stored["form"] == "10-Q"
    assert stored["cik"] == "0000320193"
    assert stored["ticker"] == "AAPL"
    assert stored["filing_date"] == "2023-11-03"


# ---------- physical row: the new required fields ----------


@pytest.mark.parametrize(
    "missing",
    ["document_id", "version", "title", "doc_type"],
)
def test_the_generic_required_fields_are_enforced(
    tmp_path: Path, missing: str
) -> None:
    """``title`` and ``doc_type`` are required non-empty like the other two.

    Named failure, table untouched — never a row that publishes with a
    blank title the operator cannot find the document by.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_row()], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    bad = dict(_row())
    bad.pop(missing)
    with pytest.raises(CatalogRowError) as exc_info:
        record_filings([bad], catalog_path=catalog_path)

    assert missing in str(exc_info.value)
    assert catalog_path.read_bytes() == before


@pytest.mark.parametrize("blank", ["", None])
@pytest.mark.parametrize("field", ["title", "doc_type"])
def test_blank_generic_fields_are_refused(
    tmp_path: Path, field: str, blank: object
) -> None:
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError):
        record_filings([_row(**{field: blank})], catalog_path=catalog_path)

    assert not catalog_path.exists()


def test_a_non_string_title_or_doc_type_is_refused(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError):
        record_filings([_row(title=7)], catalog_path=catalog_path)
    with pytest.raises(CatalogRowError):
        record_filings([_row(doc_type=["narrative"])], catalog_path=catalog_path)


def test_company_and_period_must_still_be_strings_when_present(
    tmp_path: Path,
) -> None:
    """Optional means omittable, not untyped: a present value is a string."""
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError):
        record_filings([_row(company=17)], catalog_path=catalog_path)
    with pytest.raises(CatalogRowError):
        record_filings([_row(period=17)], catalog_path=catalog_path)


def test_page_count_stays_a_non_negative_int_not_a_bool(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"

    for bad in ("3", True, -1):
        with pytest.raises(CatalogRowError):
            record_filings([_row(page_count=bad)], catalog_path=catalog_path)


# ---------- generic read model ----------


def test_the_generic_read_model_omits_absent_company_and_period(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings([_row()], catalog_path=catalog_path)

    entries = list_catalog_entries(catalog_path=catalog_path)

    assert entries == [
        {
            "document_id": _row()["document_id"],
            "title": "Vendor onboarding checklist",
            "doc_type": "narrative",
            "version": "a" * 64,
            "page_count": 3,
        }
    ]
    # The absent keys are ABSENT, not ``None`` and not ``""``: a
    # consumer must be able to tell "no company" from "empty company".
    assert "company" not in entries[0]
    assert "period" not in entries[0]


def test_the_generic_read_model_carries_present_company_and_period(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            _row(company="Acme", period="2024Q1"),
            _row(document_id="doc_" + "b" * 64, version="b" * 64),
        ],
        catalog_path=catalog_path,
    )

    entries = {
        entry["document_id"]: entry
        for entry in list_catalog_entries(catalog_path=catalog_path)
    }

    with_pair = entries["doc_" + "a" * 64]
    assert with_pair["company"] == "Acme"
    assert with_pair["period"] == "2024Q1"
    assert "company" not in entries["doc_" + "b" * 64]


def test_the_generic_read_model_never_leaks_legacy_filter_fields(
    tmp_path: Path,
) -> None:
    """``cik`` / ``form`` / ``ticker`` / ``filing_date`` stay stored-only."""
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_row(cik="0000320193", form="10-Q", ticker="AAPL", filing_date="2023")],
        catalog_path=catalog_path,
    )

    entry = list_catalog_entries(catalog_path=catalog_path)[0]
    for forbidden in ("cik", "form", "ticker", "filing_date", "source_sha256"):
        assert forbidden not in entry


def test_an_empty_or_missing_table_is_an_empty_generic_read(
    tmp_path: Path,
) -> None:
    assert list_catalog_entries(catalog_path=tmp_path / "absent.json") == []


# ---------- legacy rows on the generic surfaces ----------


def _legacy_row() -> dict[str, object]:
    """A row the pre-generic financial writer stored: no ``title``
    and no ``doc_type``."""
    return {
        "document_id": "AAPL-2023Q3",
        "company": "AAPL",
        "period": "2023 Q3",
        "version": "a" * 64,
        "page_count": 3,
        "cik": "0000320193",
        "form": "10-Q",
        "ticker": "AAPL",
    }


def test_a_legacy_row_reads_through_the_generic_model_with_synthesized_values(
    tmp_path: Path,
) -> None:
    """A legacy row yields a complete generic entry.

    The read tolerates the absent ``title`` / ``doc_type`` (the earlier
    financial writer never recorded them), the projection synthesizes a
    title from the row's financial identity and reports ``doc_type``
    ``unknown`` — the closed-vocabulary member for "no reliable type
    recorded" — and the physical row on disk is never rewritten with
    the synthesized values.
    """
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"filings": [_legacy_row()]}))

    entry = list_catalog_entries(catalog_path=catalog_path)[0]

    assert entry == {
        "document_id": "AAPL-2023Q3",
        "title": "AAPL 2023 Q3 10-Q",
        "doc_type": "unknown",
        "version": "a" * 64,
        "page_count": 3,
        "company": "AAPL",
        "period": "2023 Q3",
    }
    assert json_rows(catalog_path)[0] == _legacy_row()


def test_a_legacy_row_without_financial_identity_falls_back_to_the_document_id(
    tmp_path: Path,
) -> None:
    """No company/period/form: the derived title is the document id."""
    catalog_path = tmp_path / "catalog.json"
    row = {
        "document_id": "OLD-1",
        "version": "a" * 64,
        "page_count": 0,
    }
    catalog_path.write_text(json.dumps({"filings": [row]}))

    entry = list_catalog_entries(catalog_path=catalog_path)[0]

    assert entry["title"] == "OLD-1"
    assert entry["doc_type"] == "unknown"
    assert "company" not in entry
    assert "period" not in entry


def test_a_legacy_row_reads_through_the_financial_projection(
    tmp_path: Path,
) -> None:
    """``list_filings`` keeps its exact six-key set on a legacy row."""
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"filings": [_legacy_row()]}))

    hit = list_filings(catalog_path=catalog_path)["filings"][0]

    assert set(hit) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }
    assert hit == {
        "document_id": "AAPL-2023Q3",
        "company": "AAPL",
        "period": "2023 Q3",
        "version": "a" * 64,
        "page_count": 3,
        "form": "10-Q",
    }
    assert "title" not in hit
    assert "doc_type" not in hit


def test_a_legacy_row_groups_by_doc_type_under_unknown(tmp_path: Path) -> None:
    """The synthesized doc_type participates in grouping like any value.

    With one legacy row and one typed row, ``doc_type`` has two distinct
    values, so the dimension qualifies and the legacy row lands in the
    ``unknown`` bucket rather than disappearing.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_row(document_id="d1", title="Handbook", doc_type="narrative")],
        catalog_path=catalog_path,
    )
    stored = json.loads(catalog_path.read_text())["filings"]
    stored.append(_legacy_row())
    catalog_path.write_text(json.dumps({"filings": stored}))

    grouping = catalog_grouping(catalog_path=catalog_path)

    doc_type = next(
        d for d in grouping["dimensions"] if d["key"] == "doc_type"
    )
    values = {group["value"] for group in doc_type["groups"]}
    assert values == {"narrative", "unknown"}


# ---------- financial compatibility projection ----------


def test_list_filings_keeps_its_exact_six_key_projection(
    tmp_path: Path,
) -> None:
    """``list_filings`` is unchanged: the same key set, on generic rows too.

    A metadata-free row reads through the compatibility projection with
    ``company`` / ``period`` absent, so the projection must omit them
    rather than inventing an empty string — and must still refuse to
    grow ``title`` / ``doc_type``.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_row()], catalog_path=catalog_path)

    hit = list_filings(catalog_path=catalog_path)["filings"][0]

    assert set(hit) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }
    assert hit["document_id"] == _row()["document_id"]
    assert "title" not in hit
    assert "doc_type" not in hit


def test_list_filings_still_filters_on_company(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_row(company="Acme"), _row(document_id="doc_" + "b" * 64)],
        catalog_path=catalog_path,
    )

    hits = list_filings(company="Acme", catalog_path=catalog_path)["filings"]

    assert [hit["document_id"] for hit in hits] == [_row()["document_id"]]


# ---------- grouping ----------


def test_only_qualifying_dimensions_are_offered() -> None:
    """A dimension with fewer than two distinct non-empty values is not shown.

    Offering a one-value or empty dimension would present a single-item
    grouping as if it discriminated anything.
    """
    rows = [
        _row(document_id="d1", doc_type="narrative", company="Acme", period="2024Q1"),
        _row(document_id="d2", doc_type="table_dense", company="Acme", period="2024Q1"),
    ]

    grouping = catalog_grouping(rows)

    assert [dimension["key"] for dimension in grouping["dimensions"]] == [
        "doc_type"
    ]
    # ``flat_titles`` is the no-dimension fallback: it carries the titles
    # the operator can pivot to, and the view shows one or the other.
    assert grouping["flat_titles"] == ["Vendor onboarding checklist"]


def test_a_dimension_with_missing_values_places_them_under_unclassified() -> (
    None
):
    """The realistic case: ``company`` present on some rows and absent
    on others. The rows without one share the 未分类 bucket rather than
    disappearing from the grouping."""
    rows = [
        _row(document_id="d1", company="Acme"),
        _row(document_id="d2", company="Acme"),
        _row(document_id="d3"),
    ]

    grouping = catalog_grouping(rows)

    dimension = grouping["dimensions"][0]
    assert dimension["key"] == "company"
    values = {group["value"] for group in dimension["groups"]}
    assert values == {"Acme", UNCLASSIFIED_LABEL}
    unclassified = next(
        group for group in dimension["groups"] if group["value"] == UNCLASSIFIED_LABEL
    )
    assert sorted(unclassified["document_ids"]) == ["d3"]
    # 未分类 sorts last so the real buckets read first.
    assert dimension["groups"][-1]["value"] == UNCLASSIFIED_LABEL


def test_company_and_period_group_when_they_have_distinct_values() -> None:
    rows = [
        _row(document_id="d1", company="Acme", period="2024Q1"),
        _row(document_id="d2", company="Globex", period="2024Q2"),
    ]

    keys = [d["key"] for d in catalog_grouping(rows)["dimensions"]]

    assert keys == ["company", "period"]


def test_title_is_never_a_grouping_dimension() -> None:
    rows = [
        _row(document_id="d1", title="Alpha"),
        _row(document_id="d2", title="Beta"),
    ]

    grouping = catalog_grouping(rows)

    assert grouping["dimensions"] == []
    assert grouping["flat_titles"] == ["Alpha", "Beta"]


def test_a_corpus_with_no_qualifying_dimension_falls_back_to_a_title_list() -> (
    None
):
    rows = [
        _row(document_id="d1", title="Alpha"),
        _row(document_id="d2", title="Beta"),
    ]

    grouping = catalog_grouping(rows)

    assert grouping["dimensions"] == []
    assert grouping["flat_titles"] == ["Alpha", "Beta"]


def test_a_single_document_qualifies_no_dimension() -> None:
    grouping = catalog_grouping([_row(title="Only")])

    assert grouping["dimensions"] == []
    assert grouping["flat_titles"] == ["Only"]


def test_empty_rows_group_to_nothing() -> None:
    grouping = catalog_grouping([])

    assert grouping["dimensions"] == []
    assert grouping["flat_titles"] == []


def test_grouping_is_deterministic_and_carries_member_ids() -> None:
    rows = [
        _row(document_id="d2", doc_type="table_dense"),
        _row(document_id="d1", doc_type="narrative"),
        _row(document_id="d3", doc_type="narrative"),
    ]

    grouping = catalog_grouping(rows)
    narrative = grouping["dimensions"][0]["groups"][0]

    assert grouping["dimensions"][0]["key"] == "doc_type"
    assert narrative["value"] == "narrative"
    assert sorted(narrative["document_ids"]) == ["d1", "d3"]
    # The values sort, so two reads of the same corpus render the same
    # groups in the same order.
    assert [g["value"] for g in grouping["dimensions"][0]["groups"]] == [
        "narrative",
        "table_dense",
    ]


def test_grouping_reads_a_catalog_table_by_path(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            _row(document_id="d1", doc_type="narrative"),
            _row(document_id="d2", doc_type="table_dense"),
        ],
        catalog_path=catalog_path,
    )

    grouping = catalog_grouping(catalog_path=catalog_path)

    assert grouping["dimensions"][0]["key"] == "doc_type"


# ---------- stored titles ----------


def test_document_titles_are_the_stored_values(tmp_path: Path) -> None:
    """The map carries each document's own title, never a synthesis."""
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            _row(company="Acme", period="2024Q1"),
            _row(
                document_id="doc_" + "b" * 64,
                version="b" * 64,
                title="Beta handbook",
            ),
        ],
        catalog_path=catalog_path,
    )

    assert catalog_document_titles(catalog_path=catalog_path) == {
        "doc_" + "a" * 64: "Vendor onboarding checklist",
        "doc_" + "b" * 64: "Beta handbook",
    }


def test_a_titleless_legacy_row_maps_to_none(tmp_path: Path) -> None:
    """An untitled document is a known id with no title, not a derived one.

    The generic read model would synthesize ``AAPL 2023 Q3 10-Q`` from
    company/period; a caller that may not depend on those fields must
    see ``None`` and fall back to the document id itself.
    """
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"filings": [_legacy_row()]}))

    assert catalog_document_titles(catalog_path=catalog_path) == {
        "AAPL-2023Q3": None
    }


def test_a_missing_table_has_no_document_titles(tmp_path: Path) -> None:
    assert catalog_document_titles(catalog_path=tmp_path / "absent.json") == {}


# ---------- helpers ----------


def json_rows(catalog_path: Path) -> list[dict[str, object]]:
    import json

    return json.loads(catalog_path.read_text())["filings"]
