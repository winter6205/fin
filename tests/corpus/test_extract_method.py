"""One extract method per corpus version (spec SC3/SC4, ADR 0079/0068).

The catalog row contract gains a stored-only ``extract_method`` field:
every recorded version carries exactly one method, default
``standard``. A second, different method on the same
``(document_id, version)`` through the ingest seam is the typed
``ExtractMethodConflictError`` — never a merged dual graph, never a
silent overwrite. The sanctioned change path is the operator switch
:func:`switch_extract_method`, which records the new method plus a
rebuild intent (the re-extract itself is the iknow sibling's job; fin
never runs extract or Leiden here).

The standard/fast vocabulary has a single authoritative definition in
``fin.corpus.extract_method`` (corpus is the lower layer; the host's
propose window imports it), and none of it ever appears on the
``list_filings`` hit.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from fin.corpus import extract_method as vocab
from fin.corpus.catalog import list_filings, record_filings, switch_extract_method
from fin.corpus.errors import (
    CatalogRowError,
    ExtractMethodConflictError,
    ExtractMethodTargetMissingError,
)
from fin.host.question import extract_method_proposal as host_proposal


def _valid_row(**overrides: object) -> dict[str, object]:
    # ``title`` and ``doc_type`` are required non-empty fields of the T6
    # row contract; every case here is about ``extract_method``, so the
    # fixture carries the minimum that satisfies the row contract.
    row: dict[str, object] = {
        "document_id": "TEST-2023Q1",
        "company": "TEST",
        "period": "2023 Q1",
        "version": "a" * 64,
        "page_count": 3,
        "title": "Test filing",
        "doc_type": "narrative",
    }
    row.update(overrides)
    return row


def _stored_rows(catalog_path: Path) -> list[dict[str, object]]:
    return json.loads(catalog_path.read_text())["filings"]


# ===== default: a new version records standard =====


def test_new_version_records_standard_by_default(tmp_path: Path) -> None:
    """No method on the row and no switch pick => the version is standard."""
    catalog_path = tmp_path / "catalog.json"
    assert record_filings([_valid_row()], catalog_path=catalog_path) == 1

    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"


def test_operator_switch_can_record_fast_on_a_new_version(tmp_path: Path) -> None:
    """A row recorded with the operator's fast pick carries that method."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(extract_method="fast")], catalog_path=catalog_path)

    assert _stored_rows(catalog_path)[0]["extract_method"] == "fast"


@pytest.mark.parametrize("method", ["turbo", "Standard", "", "fast ", 7, True])
def test_out_of_set_method_token_is_a_named_row_failure(
    tmp_path: Path, method: object
) -> None:
    """The closed set is exactly {standard, fast}; anything else is
    rejected before the table is touched."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    with pytest.raises(CatalogRowError):
        record_filings(
            [_valid_row(extract_method=method)], catalog_path=catalog_path
        )

    assert catalog_path.read_bytes() == before


# ===== one method per version: dual method is a typed fail =====


def test_second_different_method_on_one_version_is_typed_fail(
    tmp_path: Path,
) -> None:
    """standard recorded, fast recorded on the same key => named failure
    and the table is left untouched (no dual graph, no silent flip)."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    with pytest.raises(ExtractMethodConflictError):
        record_filings(
            [_valid_row(page_count=9, extract_method="fast")],
            catalog_path=catalog_path,
        )

    assert catalog_path.read_bytes() == before
    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"


def test_conflict_in_a_batch_writes_no_rows(tmp_path: Path) -> None:
    """One conflicting row fails the whole batch — no partial write."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    with pytest.raises(ExtractMethodConflictError):
        record_filings(
            [
                _valid_row(document_id="OTHER-2023Q1"),
                _valid_row(extract_method="fast"),
            ],
            catalog_path=catalog_path,
        )

    assert catalog_path.read_bytes() == before


def test_legacy_row_without_method_counts_as_standard(
    tmp_path: Path,
) -> None:
    """A row stored before the field existed is implicitly standard, so
    recording fast through the ingest seam is still the dual-method
    fail."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    legacy = [dict(_valid_row()) for _ in range(1)]
    catalog_path.write_text(json.dumps({"filings": legacy}))

    with pytest.raises(ExtractMethodConflictError):
        record_filings(
            [_valid_row(extract_method="fast")], catalog_path=catalog_path
        )


def test_recording_the_same_method_twice_updates_in_place(
    tmp_path: Path,
) -> None:
    """Same method on the same version is the ordinary in-place upsert."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(extract_method="fast")], catalog_path=catalog_path)
    record_filings(
        [_valid_row(page_count=9, extract_method="fast")], catalog_path=catalog_path
    )

    rows = _stored_rows(catalog_path)
    assert len(rows) == 1
    assert rows[0]["page_count"] == 9
    assert rows[0]["extract_method"] == "fast"


def test_repush_without_a_method_inherits_the_recorded_fast(
    tmp_path: Path,
) -> None:
    """A plain ingest re-push must not silently flip an operator's fast
    version back to standard — it inherits what the version records."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(extract_method="fast")], catalog_path=catalog_path)
    record_filings([_valid_row(page_count=7)], catalog_path=catalog_path)

    assert _stored_rows(catalog_path)[0]["extract_method"] == "fast"


# ===== operator switch: change the method + record the rebuild intent =====


def test_switch_records_the_new_method_and_a_rebuild_intent(
    tmp_path: Path,
) -> None:
    """The switch is the sanctioned change path: it flips the version's
    method and marks it for rebuild (the re-extract is the sibling's
    job, never run here)."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)

    switch_extract_method(
        "TEST-2023Q1", "a" * 64, "fast", catalog_path=catalog_path
    )

    row = _stored_rows(catalog_path)[0]
    assert row["extract_method"] == "fast"
    assert row["rebuild_pending"] is True


def test_switch_to_the_recorded_method_is_a_noop(tmp_path: Path) -> None:
    """No method change => no rebuild intent and no table rewrite."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(extract_method="fast")], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    switch_extract_method(
        "TEST-2023Q1", "a" * 64, "fast", catalog_path=catalog_path
    )

    assert catalog_path.read_bytes() == before


def test_switch_unknown_version_is_a_named_failure(tmp_path: Path) -> None:
    """The switch targets a recorded version only; a missing row is a
    named failure, not a create."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)
    before = catalog_path.read_bytes()

    with pytest.raises(ExtractMethodTargetMissingError):
        switch_extract_method("TEST-2023Q1", "b" * 64, "fast", catalog_path=catalog_path)

    assert catalog_path.read_bytes() == before


@pytest.mark.parametrize("method", ["turbo", "STANDARD"])
def test_switch_rejects_out_of_set_method(tmp_path: Path, method: str) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row()], catalog_path=catalog_path)

    with pytest.raises(CatalogRowError):
        switch_extract_method(
            "TEST-2023Q1", "a" * 64, method, catalog_path=catalog_path
        )

    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"


# ===== storage-only: the vocabulary never reaches the wire =====


def test_extract_method_stays_off_the_list_filings_hit(
    tmp_path: Path,
) -> None:
    """The hit shape is the ADR 0009 contract; ``extract_method`` is
    stored metadata like ``source_sha256``."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_valid_row(extract_method="fast")], catalog_path=catalog_path)

    hit = list_filings(catalog_path=catalog_path)["filings"][0]
    assert "extract_method" not in hit
    assert "rebuild_pending" not in hit


# ===== vocabulary SSOT: one definition, host imports it =====


def test_corpus_owns_the_vocabulary_host_propose_window_imports() -> None:
    """The host propose window must reuse the corpus vocabulary, not
    redefine the standard/fast token set (import direction is
    host -> corpus; corpus never imports host)."""
    assert vocab.EXTRACT_METHODS == {"standard", "fast"}
    assert host_proposal.DEFAULT_EXTRACT_METHOD == vocab.DEFAULT_EXTRACT_METHOD
    assert host_proposal.EXTRACT_METHOD_STANDARD is vocab.EXTRACT_METHOD_STANDARD
    assert host_proposal.EXTRACT_METHOD_FAST is vocab.EXTRACT_METHOD_FAST

    source = inspect.getsource(host_proposal)
    # Docstrings may mention the tokens; only an actual (re)binding of
    # the vocabulary constants to string values would fork the SSOT.
    forked: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        names = {
            t.id
            for t in node.targets
            if isinstance(t, ast.Name)
            and (
                t.id.startswith("EXTRACT_METHOD")
                or t.id == "ExtractMethod"
            )
        }
        if names:
            forked.update(
                c.value
                for c in ast.walk(node.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            )
    assert "standard" not in forked and "fast" not in forked, (
        "the host propose window must not redefine the method vocabulary"
    )
