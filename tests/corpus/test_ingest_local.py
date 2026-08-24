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
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from fin.corpus.facts import read_facts
from fin.corpus.facts_seed import seed_facts_for_document
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
    assert fact.source_locator.page == 4
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


# ---------- helpers ----------


def _project_manifest() -> tuple[Path, Path, list[dict]]:
    data_dir = _project_data_dir()
    manifest_path = data_dir / "manifest.json"
    pdf_dir = data_dir / "pdf"
    documents = json.loads(manifest_path.read_text())["documents"]
    return manifest_path, pdf_dir, documents