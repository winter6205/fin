"""Boundary tests for the catalog metadata table (T2 / ADR 0047).

The catalog (``fin.corpus.catalog``) is a host metadata table at
``data/corpus/catalog.json``: ingested/pushed filings are registered
as rows of the cross-ticket contract ``{document_id, company, period,
version, page_count}`` (optional ``source_sha256``). ``list_filings``
reads the table; it never opens ``manifest.json`` or the extracted
tree — the serve-time HTML tree is not the catalog SSOT (ADR 0046 /
0047). Writes go through ``record_filings`` (upsert keyed by
``document_id`` + ``version``), the seam the ingest endpoint (T3)
will call.

These tests cover the five boundary classes from the
defensive-contract-validator axis: empty, negative, overflow,
concurrent, exception. Filter semantics under test (per ADR 0031 /
spec):

* Empty filter values (e.g. ``cik=""``) MUST behave as empty hits —
  NOT "match anything" and NOT an exception.
* Unknown ``cik`` / ``ticker`` / ``form`` / ``filing_date`` MUST
  return ``{"filings": []}``.
* Conflicting filters MUST return ``{"filings": []}`` — filters are
  AND-combined.
* The output hit shape is ``{document_id, company, period, version,
  page_count, form}``. ``cik`` / ``accession`` / ``filing_date`` /
  ``ticker`` / ``source_sha256`` remain stored-only fields and MUST
  NOT leak onto the hit.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.catalog import (
    DEFAULT_CATALOG_PATH,
    list_filings,
    record_filings,
    replace_filings,
    switch_extract_method,
)
from fin.corpus.errors import CatalogCorruptError, CatalogRowError
from fin.host.tools.list_files import list_files


# ---------- legacy rows on the read path ----------


def _legacy_row() -> dict[str, object]:
    """A row the pre-T6 financial writer stored: no ``title``/``doc_type``."""
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


def test_a_legacy_row_without_title_or_doc_type_is_readable(tmp_path: Path) -> None:
    """The read path tolerates a row with no ``title``/``doc_type``.

    T6 required those fields on NEW rows; the rows the earlier financial
    writer already stored never had them, and the compatibility read
    must not turn the existing library into ``CatalogCorruptError``.
    """
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"filings": [_legacy_row()]}))

    filings = list_filings(catalog_path=catalog_path)["filings"]

    assert len(filings) == 1
    assert set(filings[0]) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }
    assert filings[0]["document_id"] == "AAPL-2023Q3"
    assert filings[0]["form"] == "10-Q"


def test_a_legacy_row_keeps_a_present_but_wrong_typed_field_corrupt(
    tmp_path: Path,
) -> None:
    """Absence is tolerated; a present bad value is still corruption."""
    catalog_path = tmp_path / "catalog.json"
    row = _legacy_row()
    row["title"] = 7
    catalog_path.write_text(json.dumps({"filings": [row]}))

    with pytest.raises(CatalogCorruptError):
        list_filings(catalog_path=catalog_path)


def test_a_legacy_row_with_a_blank_title_is_still_corrupt(
    tmp_path: Path,
) -> None:
    """The read contract only forgives ABSENCE, not emptiness."""
    catalog_path = tmp_path / "catalog.json"
    row = _legacy_row()
    row["title"] = ""
    catalog_path.write_text(json.dumps({"filings": [row]}))

    with pytest.raises(CatalogCorruptError):
        list_filings(catalog_path=catalog_path)


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"document_id": 7}, id="document_id_not_a_string"),
        pytest.param({"version": None}, id="version_none"),
        pytest.param({"page_count": "3"}, id="page_count_not_int"),
        pytest.param({"page_count": True}, id="page_count_bool"),
        pytest.param({"page_count": -1}, id="page_count_negative"),
        pytest.param({"extract_method": "psychic"}, id="extract_method_out_of_set"),
        pytest.param({"source_sha256": 9}, id="source_sha256_not_a_string"),
    ],
)
def test_a_legacy_row_that_is_genuinely_corrupt_stays_corrupt(
    tmp_path: Path, broken: dict[str, object]
) -> None:
    """Every read-side constraint except ``title``/``doc_type`` absence
    stays at write strength: a broken row is ``CatalogCorruptError``."""
    catalog_path = tmp_path / "catalog.json"
    row = _legacy_row()
    row.update(broken)
    catalog_path.write_text(json.dumps({"filings": [row]}))

    with pytest.raises(CatalogCorruptError):
        list_filings(catalog_path=catalog_path)


def test_a_write_over_a_legacy_table_keeps_the_legacy_row(tmp_path: Path) -> None:
    """The legacy row survives an upsert onto its table.

    The upsert reads through the tolerant validation; the row it does
    not touch is rewritten verbatim, and the legacy row itself must not
    silently grow invented ``title`` / ``doc_type`` values.
    """
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"filings": [_legacy_row()]}))

    record_filings(
        [
            {
                "document_id": "doc_" + "b" * 64,
                "version": "b" * 64,
                "title": "Vendor onboarding checklist",
                "doc_type": "narrative",
                "page_count": 1,
            }
        ],
        catalog_path=catalog_path,
    )

    stored = json.loads(catalog_path.read_text())["filings"]
    legacy = next(row for row in stored if row["document_id"] == "AAPL-2023Q3")
    assert legacy == _legacy_row()
    assert [row["document_id"] for row in stored] == [
        "AAPL-2023Q3",
        "doc_" + "b" * 64,
    ]


def test_a_row_missing_title_is_still_rejected_on_the_write_path(
    tmp_path: Path,
) -> None:
    """The write contract is unchanged: a NEW row without ``title`` is
    ``CatalogRowError`` — the read tolerance never leaks onto writes."""
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError) as exc_info:
        record_filings([_legacy_row()], catalog_path=catalog_path)

    assert "title" in str(exc_info.value)
    assert not catalog_path.exists()


# ---------- paths ----------


def _project_data_dir() -> Path:
    """Project's committed ``data/corpus`` directory."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _catalog_path() -> Path:
    return _project_data_dir() / "catalog.json"


# Real EDGAR-shaped values from the committed catalog. Anchored here
# so a catalog edit that breaks the assumption surfaces as a
# well-named fixture failure, not a misleading test result.
AAPL_CIK = "0000320193"
MSFT_CIK = "0000789019"


# ===== boundary 1: empty =====


@pytest.mark.parametrize(
    "field",
    ["cik", "form", "ticker", "filing_date"],
)
def test_empty_filter_value_returns_empty_hits(field: str) -> None:
    """Empty string filter (e.g. ``cik=""``) MUST return ``{"filings": []}``.

    Per ADR 0031 / spec: "Empty hits not exceptions". An empty filter
    value is NOT "match anything" and NOT an exception — it is a
    well-formed call that simply has no row matching it.
    """
    result = list_files(**{field: ""}, catalog_path=_catalog_path())
    assert result == {"filings": []}, (
        f"empty {field}='' must yield empty hits (per ADR 0031); "
        f"got {result!r}"
    )


def test_missing_catalog_table_is_an_empty_catalog(tmp_path: Path) -> None:
    """A missing table file is the empty catalog, NOT an error.

    ADR 0047: the catalog is the metadata table over ingested/pushed
    filings. A serve process whose corpus was never ingested has no
    table file at all — that is ``{"filings": []}``, not a 503 and
    not a tree scan.
    """
    assert list_filings(catalog_path=tmp_path / "absent" / "catalog.json") == {
        "filings": []
    }


def test_record_filings_empty_input_records_nothing(tmp_path: Path) -> None:
    """``record_filings([])`` is a no-op: 0 recorded, no file created."""
    catalog_path = tmp_path / "catalog.json"

    assert record_filings([], catalog_path=catalog_path) == 0
    assert not catalog_path.exists()
    assert list_filings(catalog_path=catalog_path) == {"filings": []}


# ===== boundary 2: negative (bad rows are named failures) =====


def _valid_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "TEST-2023Q1",
        "company": "TEST",
        "period": "2023 Q1",
        "version": "a" * 64,
        "page_count": 3,
        # T6 added ``title`` / ``doc_type`` to the physical row contract;
        # the missing-required-field cases live in
        # ``test_catalog_generic.py`` (which also pins that ``company``
        # and ``period`` became OPTIONAL, so this file's negative class
        # is about the fields that are still required).
        "title": "Test filing",
        "doc_type": "narrative",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "bad_row",
    [
        pytest.param({"company": "TEST", "period": "2023 Q1", "version": "a" * 64, "page_count": 1, "title": "t", "doc_type": "narrative"}, id="missing_document_id"),
        pytest.param({"document_id": "TEST-2023Q1", "period": "2023 Q1", "version": "a" * 64, "page_count": 1, "title": "t", "doc_type": "narrative"}, id="missing_company_is_not_required"),
        pytest.param({"document_id": "TEST-2023Q1", "company": "TEST", "period": "2023 Q1", "page_count": 1, "title": "t", "doc_type": "narrative"}, id="missing_version"),
        pytest.param(_valid_row(page_count="3"), id="page_count_not_int"),
        pytest.param(_valid_row(page_count=True), id="page_count_bool"),
        pytest.param(_valid_row(page_count=-1), id="page_count_negative"),
        pytest.param(_valid_row(document_id=""), id="empty_document_id"),
        pytest.param(_valid_row(version=None), id="version_none"),
        pytest.param("not-a-mapping", id="not_a_mapping"),
        pytest.param(_valid_row(source_sha256=7), id="source_sha256_not_str"),
    ],
)
def test_record_filings_rejects_bad_rows_with_named_failure(
    tmp_path: Path, bad_row: object
) -> None:
    """A row violating the contract raises ``CatalogRowError``.

    Never a silent skip-as-success: the write does not happen and the
    existing table is untouched.
    """
    if bad_row == "not-a-mapping":
        # A non-mapping is still refused; keep it out of the
        # write-then-compare path below, which needs a table on disk.
        catalog_path = tmp_path / "catalog.json"
        with pytest.raises(CatalogRowError):
            record_filings([bad_row], catalog_path=catalog_path)
        return
    if _is_accepted_row(bad_row):
        # ``company`` became optional in T6, so this case is a positive
        # one: it must NOT raise. Asserted here so the parametrize id
        # keeps pointing at the contract change rather than silently
        # dropping the case.
        catalog_path = tmp_path / "catalog.json"
        assert record_filings([bad_row], catalog_path=catalog_path) == 1
        stored = json.loads(catalog_path.read_text())["filings"][0]
        assert "company" not in stored
        return
    catalog_path = tmp_path / "catalog.json"
    assert record_filings([_valid_row()], catalog_path=catalog_path) == 1
    before = catalog_path.read_bytes()

    with pytest.raises(CatalogRowError):
        record_filings([bad_row], catalog_path=catalog_path)

    assert catalog_path.read_bytes() == before, (
        "a rejected row must not partially mutate the table"
    )
    assert len(list_filings(catalog_path=catalog_path)["filings"]) == 1


def _is_accepted_row(bad_row: object) -> bool:
    """The one negative case that became positive: no ``company``."""
    return (
        isinstance(bad_row, dict)
        and "document_id" in bad_row
        and "company" not in bad_row
    )


def test_record_filings_names_the_offending_row(tmp_path: Path) -> None:
    """The error message names the row index and the offending field."""
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError) as exc_info:
        record_filings(
            [
                _valid_row(),
                _valid_row(document_id="OTHER"),
                {"company": "X", "title": "t", "doc_type": "narrative"},
            ],
            catalog_path=catalog_path,
        )

    message = str(exc_info.value)
    assert "#2" in message
    assert "document_id" in message


# ===== negative: unknown filters (committed catalog) =====


def test_unknown_cik_returns_empty_hits() -> None:
    """Unknown ``cik`` returns ``{"filings": []}`` — NOT an error."""
    assert list_files(cik="9999999999", catalog_path=_catalog_path()) == {
        "filings": []
    }


def test_unknown_ticker_returns_empty_hits() -> None:
    """Unknown ``ticker`` returns ``{"filings": []}``."""
    assert list_files(ticker="ZZZZ", catalog_path=_catalog_path()) == {
        "filings": []
    }


def test_unknown_form_returns_empty_hits() -> None:
    """Unknown ``form`` returns ``{"filings": []}``."""
    assert list_files(form="8-K", catalog_path=_catalog_path()) == {
        "filings": []
    }


def test_unknown_filing_date_returns_empty_hits() -> None:
    """Unknown exact ``filing_date`` returns ``{"filings": []}``."""
    assert list_files(
        filing_date="2099-12-31", catalog_path=_catalog_path()
    ) == {"filings": []}


def test_conflicting_ticker_and_cik_returns_empty_hits() -> None:
    """Conflicting ``ticker``+``cik`` returns ``{"filings": []}``.

    AAPL's ticker paired with MSFT's CIK — no row satisfies both.
    Filters are AND-combined, so a row that passes one but fails the
    other is dropped; the empty hits path is the contract, NOT an
    exception.
    """
    assert list_files(
        ticker="AAPL", cik=MSFT_CIK, catalog_path=_catalog_path()
    ) == {"filings": []}


def test_matching_ticker_and_cik_returns_hits() -> None:
    """Matching ``ticker``+``cik`` returns the AND-combined hits.

    The 4 frozen private fixture source AAPL PDFs carry ``ticker="AAPL"``; the
    EDGAR iXBRL primary docs carry ``ticker="EDGAR-<cik>"``, so the
    ticker+cik AND stays at 4 rows.
    """
    result = list_files(
        ticker="AAPL", cik=AAPL_CIK, catalog_path=_catalog_path()
    )
    assert len(result["filings"]) == 4
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_cik_filter_alone_returns_one_issuer_hits() -> None:
    """``cik`` alone narrows to the issuer's filings (7 AAPL rows)."""
    result = list_files(cik=AAPL_CIK, catalog_path=_catalog_path())
    assert len(result["filings"]) == 7
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_form_filter_alone_returns_all_10Q_hits() -> None:
    """``form="10-Q"`` returns every 10-Q row in the corpus (floor 20)."""
    result = list_files(form="10-Q", catalog_path=_catalog_path())
    assert len(result["filings"]) >= 20


def test_filing_date_filter_alone_returns_single_hit() -> None:
    """Exact ``filing_date`` returns exactly the one matching row."""
    result = list_files(filing_date="2022-07-29", catalog_path=_catalog_path())
    assert len(result["filings"]) == 1
    assert result["filings"][0]["document_id"] == "AAPL-2022Q3"


def test_company_and_cik_filters_are_and_combined() -> None:
    """``company`` + ``cik`` agree on AAPL → 7 hits; cross-issuer → empty."""
    matching = list_files(
        company="AAPL", cik=AAPL_CIK, catalog_path=_catalog_path()
    )
    assert len(matching["filings"]) == 7

    cross = list_files(
        company="AAPL", cik=MSFT_CIK, catalog_path=_catalog_path()
    )
    assert cross == {"filings": []}


# ===== boundary 3: overflow (large table round-trip) =====


def test_large_table_round_trips_completely(tmp_path: Path) -> None:
    """300 recorded rows all come back, in deterministic key order.

    The catalog has no internal cap: a large table must round-trip
    without dropping rows or losing the sort.
    """
    catalog_path = tmp_path / "catalog.json"
    rows = [
        {
            "document_id": f"DOC-{index:04d}",
            "company": f"CO{index % 7}",
            "period": "2023 Q1",
            "version": f"{index:064d}",
            "page_count": index % 13,
            "title": f"Document {index:04d}",
            "doc_type": "narrative",
        }
        for index in range(300)
    ]
    assert record_filings(rows, catalog_path=catalog_path) == 300

    result = list_filings(catalog_path=catalog_path)
    assert len(result["filings"]) == 300
    keys = [(f["document_id"], f["version"]) for f in result["filings"]]
    assert keys == sorted(keys)


def test_upsert_same_document_same_version_updates_in_place(
    tmp_path: Path,
) -> None:
    """Same ``(document_id, version)`` upserts — no duplicate rows."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(page_count=3)], catalog_path=catalog_path)
    record_filings([_valid_row(page_count=9)], catalog_path=catalog_path)

    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert len(filings) == 1
    assert filings[0]["page_count"] == 9


def test_upsert_same_document_new_version_keeps_both_rows(
    tmp_path: Path,
) -> None:
    """A new version of the same document is a second row."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(version="a" * 64)], catalog_path=catalog_path)
    record_filings([_valid_row(version="b" * 64)], catalog_path=catalog_path)

    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert [f["version"] for f in filings] == ["a" * 64, "b" * 64]


def test_replace_filings_replaces_the_rows_of_the_documents_it_is_given(
    tmp_path: Path,
) -> None:
    """A writer that owns a document replaces its rows instead of adding to them.

    The upsert keys on ``(document_id, version)``, so a new version is a
    second row. A writer that owns the whole document -- the demo seed --
    must not leave the previous version's row behind.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(version="a" * 64)], catalog_path=catalog_path)

    written = replace_filings(
        [_valid_row(version="b" * 64)],
        document_ids=["TEST-2023Q1"],
        catalog_path=catalog_path,
    )

    assert written == 1
    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert [(f["document_id"], f["version"]) for f in filings] == [
        ("TEST-2023Q1", "b" * 64)
    ]


def test_replace_filings_keeps_the_rows_of_documents_it_does_not_own(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_valid_row(document_id="OTHER-2023Q1", version="a" * 64)],
        catalog_path=catalog_path,
    )
    record_filings([_valid_row(version="a" * 64)], catalog_path=catalog_path)

    replace_filings(
        [_valid_row(version="b" * 64)],
        document_ids=["TEST-2023Q1"],
        catalog_path=catalog_path,
    )

    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert [(f["document_id"], f["version"]) for f in filings] == [
        ("OTHER-2023Q1", "a" * 64),
        ("TEST-2023Q1", "b" * 64),
    ]


def test_replace_filings_keeps_the_recorded_extract_method(tmp_path: Path) -> None:
    """Replacing a row is not a method switch (ADR 0068)."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    switch_extract_method(
        "TEST-2023Q1", "a" * 64, "fast", catalog_path=catalog_path
    )

    replace_filings(
        [_valid_row()],
        document_ids=["TEST-2023Q1"],
        catalog_path=catalog_path,
    )

    stored = json.loads(catalog_path.read_text())["filings"][0]
    assert stored["extract_method"] == "fast"


def test_replace_filings_refuses_a_row_outside_the_documents_it_replaces(
    tmp_path: Path,
) -> None:
    """A row the caller does not own is a named failure, not a silent write."""
    catalog_path = tmp_path / "catalog.json"

    with pytest.raises(CatalogRowError, match="OTHER-2023Q1"):
        replace_filings(
            [_valid_row(document_id="OTHER-2023Q1")],
            document_ids=["TEST-2023Q1"],
            catalog_path=catalog_path,
        )


def test_optional_source_sha256_is_stored_but_not_emitted(
    tmp_path: Path,
) -> None:
    """``source_sha256`` round-trips in the table but stays off the hit.

    The hit shape is the ADR 0009 contract plus ``form`` (SC3); the
    optional contract field is storage metadata for the ingest side.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_valid_row(source_sha256="a" * 64)], catalog_path=catalog_path
    )
    # The stored row keeps it...
    stored = json.loads(catalog_path.read_text())["filings"][0]
    assert stored["source_sha256"] == "a" * 64
    # ...the emitted hit does not.
    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert filings[0] == {
        "document_id": "TEST-2023Q1",
        "company": "TEST",
        "period": "2023 Q1",
        "version": "a" * 64,
        "page_count": 3,
        "form": None,
    }


# ===== boundary 4: concurrent (parallel upserts do not corrupt) =====


def test_concurrent_record_filings_do_not_corrupt_or_lose_rows(
    tmp_path: Path,
) -> None:
    """8 threads upsert distinct rows on the same table concurrently.

    Every row must survive (read-modify-write is serialized by a
    file lock) and the file must stay valid JSON of the catalog
    shape. Atomic ``os.replace`` means even an unsynchronized reader
    never sees a torn file.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(document_id="SEED")], catalog_path=catalog_path)

    def _record(worker: int) -> int:
        rows = [
            {
                "document_id": f"W{worker}-DOC-{index:03d}",
                "company": f"W{worker}",
                "period": "2023 Q1",
                "version": f"{worker:02d}" * 32,
                "page_count": index,
                "title": f"W{worker} doc {index}",
                "doc_type": "narrative",
            }
            for index in range(10)
        ]
        return record_filings(rows, catalog_path=catalog_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_record, worker) for worker in range(8)]
        assert [f.result() for f in futures] == [10] * 8

    payload = json.loads(catalog_path.read_text())
    document_ids = [row["document_id"] for row in payload["filings"]]
    assert len(document_ids) == 81  # 1 seed + 8 workers x 10 rows
    assert len(set(document_ids)) == 81
    assert document_ids == sorted(document_ids)
    assert len(list_filings(catalog_path=catalog_path)["filings"]) == 81


def test_concurrent_list_reads_during_record_are_intact(
    tmp_path: Path,
) -> None:
    """Readers running against a table being rewritten never see a
    torn file — they see either the old or the new complete table."""
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [_valid_row(document_id=f"SEED-{index}") for index in range(50)],
        catalog_path=catalog_path,
    )

    def _flip(_: int) -> None:
        record_filings(
            [_valid_row(document_id=f"FLIP-{_:04d}", company="FLIP")],
            catalog_path=catalog_path,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        writer = pool.submit(lambda: [_flip(i) for i in range(25)])
        reader = pool.submit(
            lambda: [
                list_filings(catalog_path=catalog_path)["filings"]
                for _ in range(40)
            ]
        )
        writer.result()
        snapshots = reader.result()

    for snapshot in snapshots:
        assert snapshot, "a read must never observe the empty torn state"
        keys = [(f["document_id"], f["version"]) for f in snapshot]
        assert keys == sorted(keys)


# ===== boundary 5: exception (corrupt table is a typed error) =====


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{ not json", id="invalid_json"),
        pytest.param('["a", "b"]', id="top_level_list"),
        pytest.param('{"rows": []}', id="missing_filings_key"),
        pytest.param(
            '{"filings": [{"document_id": "X"}]}', id="row_missing_fields"
        ),
        pytest.param('{"filings": ["not-a-mapping"]}', id="row_not_mapping"),
    ],
)
def test_corrupt_catalog_table_raises_typed_error(
    tmp_path: Path, content: str
) -> None:
    """A table file that exists but violates the schema is
    ``CatalogCorruptError`` — NOT an empty success, NOT a bare
    ``ValueError``/``FileNotFoundError``.

    A *missing* file stays the empty catalog (boundary 1); only an
    existing-but-broken table is corruption.
    """
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(content)

    with pytest.raises(CatalogCorruptError):
        list_filings(catalog_path=catalog_path)


def test_record_filings_over_corrupt_table_raises_typed_error(
    tmp_path: Path,
) -> None:
    """An upsert refuses to build on a corrupt table."""
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text("{ not json")

    with pytest.raises(CatalogCorruptError):
        record_filings([_valid_row()], catalog_path=catalog_path)


def test_catalog_corrupt_error_is_in_the_ingest_missing_family() -> None:
    """``CatalogCorruptError`` inherits ``IngestMissingError`` so the
    host's 503 ``ingest_missing`` mapping and the status strip's
    zero-count fallback treat a broken table as the same known-bad
    corpus-state family (ADR 0020 / ADR 0033)."""
    from fin.corpus.errors import IngestMissingError

    assert issubclass(CatalogCorruptError, IngestMissingError)


# ===== committed-catalog defaults =====


def test_default_catalog_path_points_at_the_committed_table() -> None:
    """The module default is ``data/corpus/catalog.json``."""
    assert DEFAULT_CATALOG_PATH == _project_data_dir() / "catalog.json"


# ===== v1-shape preservation =====


_HIT_KEYS = {
    "document_id",
    "company",
    "period",
    "version",
    "page_count",
    "form",
}

# The EDGAR-shaped fields that stay filter inputs. Named so a later
# ticket that wants one of them on the wire has to say so out loud.
_FILTER_ONLY_FIELDS = ("cik", "accession", "filing_date", "ticker", "source_sha256")


def test_list_files_aapl_preserves_hit_shape() -> None:
    """``list_files(company="AAPL")`` returns the documented hit shape.

    ``form`` is on the hit per spec SC3; the remaining EDGAR-shaped
    fields (``cik``, ``accession``, ``filing_date``, ``ticker``) and
    the optional ``source_sha256`` MUST NOT appear in the output dict.
    """
    result = list_files(company="AAPL", catalog_path=_catalog_path())
    assert result["filings"], "expected at least one AAPL hit"
    for filing in result["filings"]:
        assert set(filing.keys()) == _HIT_KEYS, (
            f"output hit drifted from the documented shape; got "
            f"{set(filing.keys())}"
        )
        for forbidden in _FILTER_ONLY_FIELDS:
            assert forbidden not in filing, (
                f"{forbidden!r} leaked onto the hit; only ``form`` was "
                "promoted from filter input to output key"
            )


def test_list_files_default_returns_full_library_with_hit_shape() -> None:
    """``list_files()`` returns every committed filing with the hit shape.

    The SC6 GOOG 10-K fixture is part of that committed library."""
    result = list_files(catalog_path=_catalog_path())
    assert len(result["filings"]) >= 21
    for filing in result["filings"]:
        assert set(filing.keys()) == _HIT_KEYS
        for forbidden in _FILTER_ONLY_FIELDS:
            assert forbidden not in filing

    # Deterministic order: sorted by document_id.
    document_ids = [f["document_id"] for f in result["filings"]]
    assert document_ids == sorted(document_ids)

    # Every filing has a positive page_count from the table row.
    for filing in result["filings"]:
        assert filing["page_count"] > 0


# ===== synthetic catalog: form rows other than 10-Q =====


def test_list_files_form_filter_accepts_non_demo_form(tmp_path: Path) -> None:
    """A synthetic table with 10-K rows: ``form='10-K'`` returns only
    10-K rows, ``form='10-Q'`` returns only 10-Q rows.

    Per spec acceptance: ``form`` is NOT hard-coded to the demo
    10-Q. We assert the filter accepts other values when rows for
    those values exist.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "TEST-10K-2023Q1",
                "company": "TEST",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 1,
                "title": "10-K test",
                "doc_type": "narrative",
                "form": "10-K",
            },
            {
                "document_id": "TEST-10Q-2023Q1",
                "company": "TEST",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 1,
                "title": "10-Q test",
                "doc_type": "narrative",
                "form": "10-Q",
            },
        ],
        catalog_path=catalog_path,
    )

    ten_k = list_files(form="10-K", catalog_path=catalog_path)
    ten_q = list_files(form="10-Q", catalog_path=catalog_path)
    assert len(ten_k["filings"]) == 1
    assert ten_k["filings"][0]["document_id"] == "TEST-10K-2023Q1"
    assert len(ten_q["filings"]) == 1
    assert ten_q["filings"][0]["document_id"] == "TEST-10Q-2023Q1"
