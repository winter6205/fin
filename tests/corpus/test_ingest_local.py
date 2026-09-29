"""Tests for the UA-gated live fetch path (T1) and the local ingest
side writing the fact table (T1).

The S1 spec splits the ingest command into two surface entry points:

* ``python -m fin.corpus.ingest`` — local PDF→extracted-tree + fact
  table write; no network call ever.
* ``python -m fin.corpus.live_fetch`` — gated live fetch; only fires
  when ``FIN_EDGAR_USER_AGENT`` is set, and the SEC HTTP call is the
  ONLY thing that touches ``data.sec.gov`` / ``www.sec.gov``.

Tests cover the typed EXITs required by the spec (T1 acceptance):

* ``SecUaUnsetError`` raised and NO network call when
  ``FIN_EDGAR_USER_AGENT`` is unset.
* ``SecHttpFailError`` raised on UA-set + HTTP fail, and the
  catalog/extracted tree for that accession are NOT marked complete.
* Local ``run_ingest`` over the committed ``AAPL-2023Q3`` tree writes
  the fact table row the later ``get_xbrl_fact`` lookup will read.

The live-fetch path is mocked via ``fin.corpus.live_fetch.urlopen``
(re-bound at module level) so these tests do not require a live SEC
connection (default pytest contract per spec § Success Criteria).
"""

from __future__ import annotations

import json
import shutil
import socket
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from fin.corpus.facts import read_facts
from fin.corpus.facts_seed import seed_facts_for_document
from fin.corpus.ingest import run_ingest, update_catalog_fields
from fin.corpus.live_fetch import (
    SecHttpFailError,
    SecUaUnsetError,
    fetch_and_ingest,
)


def _project_data_dir() -> Path:
    """Absolute path to the project's committed ``data/corpus`` dir."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


# ---------- catalog: manifest schema extension ----------


def test_manifest_entries_carry_optional_edgar_fields() -> None:
    """Manifest entries must include ``cik`` / ``accession`` /
    ``filing_date`` / ``ticker`` / ``form`` keys (per ADR 0031).

    For the demo (PDF) corpus these are populated with EDGAR-known
    values where the data is public; the catalog reader must accept
    them without raising.
    """
    manifest_path = _project_data_dir() / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    documents = payload["documents"]

    required_optional_keys = {"cik", "accession", "filing_date", "ticker", "form"}
    for doc in documents:
        assert required_optional_keys.issubset(doc.keys()), (
            f"{doc['document_id']} missing one of {required_optional_keys}"
        )


# The four frozen AAPL 10-Qs, keyed to the EDGAR submissions JSON for
# CIK 0000320193 (`data.sec.gov/submissions/CIK0000320193.json`). Each
# pair is the 10-Q whose EDGAR ``reportDate`` equals the quarterly
# period end printed on that document's frozen cover page — see
# ``test_manifest_filing_dates_match_the_frozen_signature_pages`` for
# the in-repo half of the evidence.
_AAPL_EDGAR_ROWS = {
    "AAPL-2022Q3": ("0000320193-22-000070", "2022-07-29"),
    "AAPL-2023Q1": ("0000320193-23-000006", "2023-02-03"),
    "AAPL-2023Q2": ("0000320193-23-000064", "2023-05-05"),
    "AAPL-2023Q3": ("0000320193-23-000077", "2023-08-04"),
}


def test_aapl_manifest_rows_carry_the_10q_accession_not_the_10k() -> None:
    """Regression: the AAPL rows had been shifted onto neighbouring
    filings — ``AAPL-2023Q3`` carried ``0000320193-23-000106``, which
    is Apple's FY2023 **10-K** (filed 2023-11-03), and ``AAPL-2022Q3``
    carried the FY2022 10-K. A catalog row must point at the filing
    whose bytes are frozen under that ``document_id``.
    """
    documents = json.loads(
        (_project_data_dir() / "manifest.json").read_text()
    )["documents"]
    rows = {doc["document_id"]: doc for doc in documents}

    for document_id, (accession, filing_date) in _AAPL_EDGAR_ROWS.items():
        assert rows[document_id]["accession"] == accession
        assert rows[document_id]["filing_date"] == filing_date
        assert rows[document_id]["form"] == "10-Q"


def test_manifest_filing_dates_match_the_frozen_signature_pages() -> None:
    """Every manifest ``filing_date`` must sit within five days of the
    signature date printed on that document's own frozen pages.

    This is the in-repo half of the catalog evidence: the frozen text
    is committed, so a row shifted onto a different filing (the AAPL
    10-K rows were 92 days out) fails without any SEC call. Documents
    whose frozen text carries no parseable signature date — the MSFT
    PDFs render theirs differently — have no local evidence to check
    and are skipped.

    Plan C added 8 EDGAR iXBRL primary docs (html_live kind); the
    live_fetch path doesn't carry a ``filing_date`` because that field
    requires an EDGAR submissions lookup, so EDGAR rows are out of
    scope for this local-evidence check.
    """
    import datetime
    import re

    data_dir = _project_data_dir()
    documents = json.loads((data_dir / "manifest.json").read_text())["documents"]
    checked = 0
    for doc in documents:
        if not doc.get("filing_date"):
            # EDGAR iXBRL primary docs do not carry a filing_date in
            # the catalog (the live-fetch path leaves it for the
            # submissions lookup); skip them.
            continue
        document_id = doc["document_id"]
        pages = sorted((data_dir / "extracted" / document_id).glob("page-*.json"))
        signature_date: datetime.date | None = None
        for page_path in pages[-4:]:
            text = json.loads(page_path.read_text())["text"]
            match = re.search(
                r"(?i)\bdated?\s*:?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text
            )
            if match:
                signature_date = datetime.datetime.strptime(
                    match.group(1), "%B %d, %Y"
                ).date()
        if signature_date is None:
            continue
        checked += 1
        filed = datetime.date.fromisoformat(doc["filing_date"])
        assert 0 <= (filed - signature_date).days <= 5, (
            f"{document_id} filing_date={doc['filing_date']} is not "
            f"consistent with its frozen signature date {signature_date}"
        )
    assert checked >= 8, "signature-date evidence went missing from the corpus"


# ---------- facts: local ingest writes the fact table ----------


def test_seed_facts_for_aapl_2023q3_is_emitted() -> None:
    """``seed_facts_for_document`` returns a non-empty fact list for
    ``AAPL-2023Q3`` at the manifest ``sha256``.

    The seed row's ``source_locator`` points at page 4 of the
    ``AAPL-2023Q3`` PDF, which is where the v1 gold span
    ``"[private source excerpt removed]"`` lives
    (see ``tests/eval/test_gold.py:98``). The full CLI run against the
    committed PDFs (verified manually + by the catalog tests) wires
    this seed through ``run_ingest`` → ``write_facts`` → ``read_facts``.
    """
    seed = seed_facts_for_document(
        document_id="AAPL-2023Q3",
        version="7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea",
    )
    assert seed is not None and len(seed) >= 1
    fact = seed[0]
    assert fact.source_locator.document_id == "AAPL-2023Q3"
    assert fact.source_locator.unit == 4
    assert fact.concept == "us-gaap:Revenues"


def test_seed_facts_returns_none_for_unknown_document() -> None:
    """``seed_facts_for_document`` returns ``None`` for documents that
    have no seed yet, so the ingest CLI can skip fact-file writes
    instead of writing empty ``facts: []`` files."""
    seed = seed_facts_for_document(
        document_id="UNKNOWN-2099Q9",
        version="0" * 64,
    )
    assert seed is None


def test_write_facts_round_trip_through_read(tmp_path: Path) -> None:
    """Smoke test: ``write_facts`` over the seeded list → ``read_facts``
    round-trips at least one fact for AAPL-2023Q3. This is the same
    code path the ingest CLI uses to seed
    ``data/corpus/facts/AAPL-2023Q3.json``."""
    from fin.corpus.facts import write_facts

    facts_root = tmp_path / "facts"
    seed = seed_facts_for_document(
        document_id="AAPL-2023Q3",
        version="7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea",
    )
    assert seed is not None

    write_facts(
        document_id="AAPL-2023Q3", facts=seed, facts_root=facts_root
    )

    loaded = read_facts(document_id="AAPL-2023Q3", facts_root=facts_root)
    assert len(loaded) == len(seed)
    assert loaded[0].concept == seed[0].concept


# ---------- live fetch: UA gate ----------


def test_ua_unset_never_opens_sec_network(tmp_path: Path, monkeypatch) -> None:
    """With ``FIN_EDGAR_USER_AGENT`` unset, ``fetch_and_ingest`` must
    raise ``SecUaUnsetError`` WITHOUT calling ``urllib.request.urlopen``.

    The spec requires that "unset ``FIN_EDGAR_USER_AGENT`` never opens
    SEC hosts" (T1 acceptance). Mocking ``urllib.request.urlopen`` and
    asserting it is NOT entered proves the contract.
    """
    monkeypatch.delenv("FIN_EDGAR_USER_AGENT", raising=False)

    with mock.patch("fin.corpus.live_fetch.urlopen") as urlopen_spy:
        with pytest.raises(SecUaUnsetError):
            fetch_and_ingest(
                cik="0000320193",
                accession="0000320193-23-000106",
                manifest_path=tmp_path / "manifest.json",
                extracted_root=tmp_path / "extracted",
                facts_root=tmp_path / "facts",
            )

    assert urlopen_spy.call_count == 0, (
        "live_fetch called urlopen even though FIN_EDGAR_USER_AGENT was unset"
    )


def test_ua_set_http_fail_exits_nonzero_and_does_not_mark_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """UA is set but ``urllib.request.urlopen`` raises ``HTTPError``:
    ``fetch_and_ingest`` must raise ``SecHttpFailError`` AND the
    catalog/extracted tree for that accession must NOT be created.

    This is the ``sec_http_fail`` typed EXIT (spec § Boundaries /
    success criterion "Live ingest with UA set and simulated SEC HTTP
    fail exits non-zero and does not mark that accession complete").
    """
    monkeypatch.setenv("FIN_EDGAR_USER_AGENT", "fin test@example.com")
    manifest_path = tmp_path / "manifest.json"
    extracted_root = tmp_path / "extracted"
    facts_root = tmp_path / "facts"

    def _raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://www.sec.gov/cgi-bin/browse-edgar",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=None,
        )

    with mock.patch(
        "fin.corpus.live_fetch.urlopen", side_effect=_raise_http_error
    ):
        with pytest.raises(SecHttpFailError):
            fetch_and_ingest(
                cik="0000320193",
                accession="0000320193-23-000106",
                manifest_path=manifest_path,
                extracted_root=extracted_root,
                facts_root=facts_root,
            )

    # The manifest must NOT be created and the extracted tree must NOT
    # contain the (failed) accession — ``sec_http_fail`` does not mark
    # the accession complete.
    assert not manifest_path.exists(), (
        "live_fetch wrote a manifest even though SEC HTTP failed"
    )
    failed_dir = extracted_root / "0000320193-23-000106"
    assert not failed_dir.exists(), (
        "live_fetch wrote an extracted tree even though SEC HTTP failed"
    )
    assert not (facts_root / "0000320193-23-000106.json").exists()


def test_ua_set_http_fail_typed_error_carries_status(
    tmp_path: Path, monkeypatch
) -> None:
    """The ``SecHttpFailError`` typed exception carries the failing
    HTTP status code so the CLI can log it for diagnostics."""
    monkeypatch.setenv("FIN_EDGAR_USER_AGENT", "fin test@example.com")

    def _raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://www.sec.gov/cgi-bin/browse-edgar",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

    with mock.patch(
        "fin.corpus.live_fetch.urlopen", side_effect=_raise_http_error
    ):
        with pytest.raises(SecHttpFailError) as excinfo:
            fetch_and_ingest(
                cik="0000320193",
                accession="0000320193-23-000106",
                manifest_path=tmp_path / "manifest.json",
                extracted_root=tmp_path / "extracted",
                facts_root=tmp_path / "facts",
            )

    assert excinfo.value.status_code == 404
    assert "0000320193-23-000106" in str(excinfo.value)


def test_ua_unset_empty_string_also_raises_typed_error(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty ``FIN_EDGAR_USER_AGENT`` is treated the same as unset."""
    monkeypatch.setenv("FIN_EDGAR_USER_AGENT", "")

    with mock.patch("fin.corpus.live_fetch.urlopen") as urlopen_spy:
        with pytest.raises(SecUaUnsetError):
            fetch_and_ingest(
                cik="0000320193",
                accession="0000320193-23-000106",
                manifest_path=tmp_path / "manifest.json",
                extracted_root=tmp_path / "extracted",
                facts_root=tmp_path / "facts",
            )

    assert urlopen_spy.call_count == 0


# ---------- catalog: local ingest emits/updates the EDGAR fields ----------


def _stage_aapl_2023q3(
    tmp_path: Path, *, strip_edgar_fields: bool = True
) -> tuple[Path, Path]:
    """Stage a one-document ``AAPL-2023Q3`` corpus under ``tmp_path``.

    Returns ``(manifest_path, pdf_dir)``. The manifest row keeps the
    local metadata the ingest CLI is allowed to read (``source_id`` /
    ``company`` / ``doc_type`` / ``period`` / ``sha256``) and, by
    default, drops every EDGAR-shaped catalog field so the test can
    prove the ingest CLI is the thing that puts them back.
    """
    _, real_pdf_dir, documents = _project_manifest()
    row = next(d for d in documents if d["document_id"] == "AAPL-2023Q3")
    if strip_edgar_fields:
        row = {
            key: value
            for key, value in row.items()
            if key not in {"cik", "accession", "filing_date", "form", "ticker"}
        }
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real_pdf_dir / row["filename"], pdf_dir / row["filename"])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "upstream": {"repo": "test"},
                "tickers": ["AAPL"],
                "periods": ["2023 Q3"],
                "note": "catalog-write fixture",
                "documents": [row],
            },
            indent=2,
        )
        + "\n"
    )
    return manifest_path, pdf_dir


_AAPL_2023Q3_SEED = {
    "AAPL-2023Q3": {
        "accession": "0000320193-23-000077",
        "filing_date": "2023-08-04",
    }
}


def test_update_catalog_fields_writes_edgar_shape_from_local_metadata(
    tmp_path: Path,
) -> None:
    """``update_catalog_fields`` fills ``cik`` / ``ticker`` / ``form``
    from the manifest's own local metadata and ``accession`` /
    ``filing_date`` from the operator seed — no SEC GET.

    Spec § Success Criteria: "Ingest CLI over existing corpus can
    emit/update catalog fields for ``AAPL-2023Q3`` without SEC GET".
    """
    manifest_path, _ = _stage_aapl_2023q3(tmp_path)

    updated = update_catalog_fields(
        manifest_path=manifest_path, catalog_seed=_AAPL_2023Q3_SEED
    )

    assert updated == 1
    row = json.loads(manifest_path.read_text())["documents"][0]
    assert row["cik"] == "0000320193"
    assert row["ticker"] == "AAPL"
    assert row["form"] == "10-Q"
    assert row["accession"] == "0000320193-23-000077"
    assert row["filing_date"] == "2023-08-04"


def test_update_catalog_fields_is_byte_idempotent(tmp_path: Path) -> None:
    """A second ``update_catalog_fields`` over an already-complete
    catalog reports zero updates and leaves the file bytes alone
    (ADR 0020 idempotency)."""
    manifest_path, _ = _stage_aapl_2023q3(tmp_path)
    update_catalog_fields(
        manifest_path=manifest_path, catalog_seed=_AAPL_2023Q3_SEED
    )
    first = manifest_path.read_bytes()

    updated = update_catalog_fields(
        manifest_path=manifest_path, catalog_seed=_AAPL_2023Q3_SEED
    )

    assert updated == 0
    assert manifest_path.read_bytes() == first


def test_update_catalog_fields_seed_corrects_a_drifted_row(
    tmp_path: Path,
) -> None:
    """The seed is authoritative: a row already carrying a wrong
    ``accession`` / ``filing_date`` is corrected, not left alone."""
    manifest_path, _ = _stage_aapl_2023q3(
        tmp_path, strip_edgar_fields=False
    )
    payload = json.loads(manifest_path.read_text())
    payload["documents"][0]["accession"] = "0000320193-23-000106"
    payload["documents"][0]["filing_date"] = "2023-11-03"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n")

    updated = update_catalog_fields(
        manifest_path=manifest_path, catalog_seed=_AAPL_2023Q3_SEED
    )

    assert updated == 1
    row = json.loads(manifest_path.read_text())["documents"][0]
    assert row["accession"] == "0000320193-23-000077"
    assert row["filing_date"] == "2023-08-04"


def test_committed_manifest_needs_no_catalog_update(tmp_path: Path) -> None:
    """The committed corpus manifest is already catalog-complete, so
    an ingest run over it never rewrites the repo's manifest.

    This is the guard that keeps ``run_ingest`` — which several test
    modules point at the committed ``data/corpus/manifest.json`` —
    from dirtying the working tree as a side effect.
    """
    committed = _project_data_dir() / "manifest.json"
    copy = tmp_path / "manifest.json"
    shutil.copyfile(committed, copy)

    assert update_catalog_fields(manifest_path=copy) == 0
    assert copy.read_bytes() == committed.read_bytes()


def test_local_ingest_emits_catalog_fields_without_opening_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full local ingest CLI path over the existing ``AAPL-2023Q3``
    corpus writes the EDGAR-shaped catalog fields, and opens no socket
    while doing it.

    ``socket.socket.connect`` is patched to raise, so any attempt to
    reach ``www.sec.gov`` / ``data.sec.gov`` (or anything else) during
    ingest fails the test rather than silently succeeding.
    """
    manifest_path, pdf_dir = _stage_aapl_2023q3(tmp_path)
    seed_path = tmp_path / "catalog-seed.json"
    seed_path.write_text(json.dumps({"documents": _AAPL_2023Q3_SEED}))

    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("local ingest opened a network socket")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    result = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=tmp_path / "extracted",
        facts_root=tmp_path / "facts",
        catalog_seed_path=seed_path,
    )

    assert result.catalog_updated == 1
    row = json.loads(manifest_path.read_text())["documents"][0]
    assert row["cik"] == "0000320193"
    assert row["accession"] == "0000320193-23-000077"
    assert row["filing_date"] == "2023-08-04"
    assert row["form"] == "10-Q"
    assert row["ticker"] == "AAPL"


def test_catalog_fields_land_in_list_filings_filters(tmp_path: Path) -> None:
    """The fields ingest writes are the ones ``list_filings`` filters
    on — the ingest side registers them via the ``record_filings``
    seam (T3), carrying the EDGAR-shaped fields as catalog metadata."""
    from fin.corpus.catalog import list_filings, record_filings

    manifest_path, pdf_dir = _stage_aapl_2023q3(tmp_path)
    extracted_root = tmp_path / "extracted"
    run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
        facts_root=tmp_path / "facts",
        catalog_seed=_AAPL_2023Q3_SEED,
    )

    doc = json.loads(manifest_path.read_text())["documents"][0]
    index = json.loads((extracted_root / "_index.json").read_text())
    record_filings(
        [
            {
                "document_id": doc["document_id"],
                "company": doc["company"],
                "period": doc["period"],
                "version": doc["sha256"],
                "page_count": index["documents"][doc["document_id"]][
                    "page_count"
                ],
                "cik": doc.get("cik"),
                "form": doc.get("form"),
                "ticker": doc.get("ticker"),
                "filing_date": doc.get("filing_date"),
            }
        ],
        catalog_path=tmp_path / "catalog.json",
    )

    hits = list_filings(
        cik="0000320193",
        form="10-Q",
        filing_date="2023-08-04",
        catalog_path=tmp_path / "catalog.json",
    )

    assert [hit["document_id"] for hit in hits["filings"]] == ["AAPL-2023Q3"]


# ---------- helpers ----------


def _project_manifest() -> tuple[Path, Path, list[dict]]:
    data_dir = _project_data_dir()
    manifest_path = data_dir / "manifest.json"
    pdf_dir = data_dir / "pdf"
    documents = json.loads(manifest_path.read_text())["documents"]
    return manifest_path, pdf_dir, documents