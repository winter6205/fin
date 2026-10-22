"""The endpoint's single public durable upload path.

This is the accept-and-run contract behind the HTTP door, tested at the
endpoint seam: the trusted scope id is INJECTED, the identity is the
scope-bound digest, and a file-only upload lands a catalog-visible row
with a sanitized title and an honest ``doc_type``. The explicit-ID seam
stays available to trusted in-process callers; the public HTTP door is
where a caller-chosen id is refused before any write (covered by the
workspace-http suite).

The durable-path cases here (accept + run through the in-memory ports)
cover the in-process machine; the Postgres-backed store cases live in
``test_ingest_state_store_durable.py`` and run only with
``FIN_TEST_DSN`` (reported separately).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import fin.corpus.catalog as catalog_mod
from fin.corpus.catalog import list_catalog_entries, record_filings
from fin.corpus.catalog_publication import read_accepted_metadata
from fin.corpus.ingest_endpoint import (
    IngestEndpoint,
    IngestInvalidUploadMetadataError,
)
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_push_port import FakePushPort

_SCOPE = "fin-default"
_TEXT = (
    "# Team handbook\n\n"
    "Onboarding starts with a laptop and an account.\n"
).encode("utf-8")


def _endpoint(**overrides: Any) -> IngestEndpoint:
    kwargs: dict[str, Any] = {
        "push_port": FakePushPort(),
        "object_store": InMemoryObjectStore(),
        "metadata_store": InMemoryMetadataStore(),
        "state_store": InMemoryIngestStateStore(),
        "corpus_scope_id": _SCOPE,
    }
    kwargs.update(overrides)
    return IngestEndpoint(**kwargs)


def _expected_document_id(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00"
        + _SCOPE.encode("utf-8")
        + b"\x00"
        + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


# ---------- accept: identity ----------


def test_a_file_only_upload_accepts_on_the_scope_bound_identity() -> None:
    endpoint = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, filename="team handbook.md"
    )

    assert submission.document_id == _expected_document_id(_TEXT)
    assert submission.version == hashlib.sha256(_TEXT).hexdigest()
    assert submission.state is IngestState.PENDING
    # The filename reached no identity: this upload's locator is the
    # digest-derived one even though the file is named like a document.
    assert "team" not in submission.document_id


def test_the_durable_seam_treats_an_explicit_id_as_trusted() -> None:
    """A caller-chosen id is refused at the public HTTP door, not here.

    This seam exists for trusted in-process callers that must construct a
    durable job for an identity the public path cannot mint (for example,
    a version replacement of an existing document). The public refusal is
    pinned in ``tests/host/test_workspace_http_ingest.py``; keeping the two
    seams distinct is what makes the refusal meaningful.
    """
    endpoint = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT,
        filename="a.md",
        document_id="CHOSEN-2024Q1",
    )

    assert submission.document_id == "CHOSEN-2024Q1"
    assert submission.version == hashlib.sha256(_TEXT).hexdigest()


def test_the_trusted_sync_seam_keeps_explicit_ids_and_metadata_derivation() -> None:
    """Internal trusted callers may still name an id on ``ingest``.

    The public accept has no such option; this seam exists for imports
    and replay, and it never consults the trusted corpus scope.
    """
    endpoint = _endpoint(corpus_scope_id="")  # not even configured
    seen: list[Any] = []

    by_metadata = endpoint.ingest(
        _TEXT,
        company="AAPL",
        period="2024Q3",
        declared_format="md",
        emit=lambda doc: seen.append(doc),
    )
    assert by_metadata.document_id == "AAPL-2024Q3"

    by_explicit = endpoint.ingest(
        b"other bytes",
        document_id="0000320193-24-000006",
        declared_format="md",
        emit=lambda doc: seen.append(doc),
    )
    assert by_explicit.document_id == "0000320193-24-000006"


def test_the_public_accept_refuses_an_unconfigured_scope() -> None:
    """No scope id wired: the public upload refuses rather than minting.

    A silent default scope would let every unconfigured deployment mint
    ONE identity per byte string — the refusal names the missing
    configuration instead.
    """
    endpoint = _endpoint(corpus_scope_id="")

    with pytest.raises(ValueError):
        endpoint.submit_durable(_TEXT, filename="a.md")


def test_the_same_bytes_rejoin_one_job_and_a_second_row_is_not_created() -> None:
    endpoint = _endpoint()
    objects = endpoint._object_store
    assert isinstance(objects, InMemoryObjectStore)

    first = endpoint.submit_durable(_TEXT, filename="a.md")
    second = endpoint.submit_durable(
        _TEXT, filename="renamed.md"
    )

    assert second.document_id == first.document_id
    assert second.version == first.version
    assert _stored_keys(objects) == [
        f"filings/{first.document_id}/{first.version}"
    ]


def test_resubmitting_with_other_metadata_keeps_first_accepted_values() -> None:
    """A retry joins the accepted job; it cannot rewrite what was accepted."""
    endpoint = _endpoint()
    first = endpoint.submit_durable(
        _TEXT,
        filename="a.md",
        title="First title",
        company="Acme",
        period="2024Q1",
    )
    second = endpoint.submit_durable(
        _TEXT,
        filename="renamed.md",
        title="Second title",
        company="Other",
        period="2024Q2",
    )

    assert second.document_id == first.document_id
    assert second.version == first.version
    assert endpoint.state_store is not None
    accepted = read_accepted_metadata(
        endpoint.state_store.list_audit(first.document_id)
    )
    assert accepted is not None
    assert accepted.title == "First title"
    assert accepted.company == "Acme"
    assert accepted.period == "2024Q1"


def test_another_trusted_scope_gives_another_identity() -> None:
    first = _endpoint(corpus_scope_id="scope-a").submit_durable(
        _TEXT, filename="a.md"
    )
    second = _endpoint(corpus_scope_id="scope-b").submit_durable(
        _TEXT, filename="a.md"
    )

    assert first.document_id != second.document_id
    assert first.version == second.version


def test_changed_bytes_with_the_same_filename_get_another_identity() -> None:
    endpoint = _endpoint()
    first = endpoint.submit_durable(b"first bytes", filename="same.md")
    second = endpoint.submit_durable(b"second bytes", filename="same.md")

    assert first.document_id != second.document_id
    assert first.version != second.version


# ---------- accept: metadata bounds ----------


def test_explicit_metadata_wildcards_are_refused_before_any_write() -> None:
    endpoint = _endpoint()
    objects = endpoint._object_store
    assert isinstance(objects, InMemoryObjectStore)

    with pytest.raises(IngestInvalidUploadMetadataError):
        endpoint.submit_durable(
            _TEXT,
            filename="a.md",
            company="Acme*",
        )

    assert _stored_keys(objects) == []
    assert endpoint.state_store is not None
    with pytest.raises(Exception):
        endpoint.state_store.get_job(_expected_document_id(_TEXT))


def test_explicit_metadata_controls_and_overlength_are_refused() -> None:
    endpoint = _endpoint()

    with pytest.raises(IngestInvalidUploadMetadataError):
        endpoint.submit_durable(
            _TEXT, filename="a.md", period="2024\x00Q1"
        )
    with pytest.raises(IngestInvalidUploadMetadataError):
        endpoint.submit_durable(
            _TEXT, filename="a.md", title="a" * 300
        )


def test_blank_explicit_metadata_is_absent_not_an_error() -> None:
    """A whitespace-only field means the uploader said nothing."""
    endpoint = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, filename="a.md", company="   ", period=""
    )

    assert submission.state is IngestState.PENDING


# ---------- run: title / doc_type / catalog row ----------


def test_the_run_lands_a_metadata_free_catalog_row(tmp_path: Path) -> None:
    """The end-to-end claim: upload one file, get a catalog-visible row.

    The row carries the sanitized title (filename fallback), the
    deterministic ``doc_type``, and NO company/period — the whole point
    of the metadata-free path.
    """
    catalog_path = tmp_path / "catalog.json"
    endpoint = _endpoint(
        on_ingested=_recording_hook(catalog_path),
    )

    accepted = endpoint.submit_durable(
        _TEXT, filename="Team Handbook.md"
    )
    endpoint.run_durable(accepted.document_id)

    entries = list_catalog_entries(catalog_path=catalog_path)
    assert len(entries) == 1
    row = entries[0]
    assert row["document_id"] == _expected_document_id(_TEXT)
    assert row["title"] == "Team Handbook.md"
    assert row["doc_type"] == "narrative"
    assert row["version"] == hashlib.sha256(_TEXT).hexdigest()
    assert "company" not in row
    assert "period" not in row


def test_a_document_with_no_parseable_text_is_refused_not_published(
    tmp_path: Path,
) -> None:
    """An empty upload never reaches a catalog row at all.

    The spec's ``unknown`` boundary is for "no reliable type DETECTED";
    a source the parser refuses is a stronger statement than that — the
    document has no text to speak of, so the honest answer is the typed
    ``empty_source`` refusal, not a published row labelled ``unknown``.
    Pinning this so a future leniency toward empty uploads cannot
    quietly start publishing phantom rows.
    """
    catalog_path = tmp_path / "catalog.json"
    endpoint = _endpoint(on_ingested=_recording_hook(catalog_path))

    accepted = endpoint.submit_durable(b"", filename="empty.txt")
    outcome = endpoint.run_durable(accepted.document_id)

    assert outcome.state is IngestState.FAILED
    assert list_catalog_entries(catalog_path=catalog_path) == []


def test_the_documented_unknown_doc_type_gap_in_the_accepted_row(
    tmp_path: Path,
) -> None:
    """Pins a real limitation of the frozen ``upload_metadata_accepted``
    payload, so it stays visible instead of hiding behind a passing test.

    ``doc_type`` is REQUIRED and non-empty, and the run cannot classify
    at accept time (that needs the parse). So an accept with no override
    must record SOMETHING, and the spec's only candidate for "no
    reliable type detected yet" is the literal ``unknown``. The payload
    has no field that distinguishes that from an uploader explicitly
    typing ``unknown``.

    Consequence, pinned here: on the durable path an explicit
    ``doc_type="unknown"`` override is read back as "no override" and the
    run classifies instead, so this document publishes as ``narrative``
    rather than ``unknown``. The synchronous path (where no audit round
    trip happens) honours the override; see
    ``test_the_synchronous_path_honours_an_explicit_unknown_override``.

    Resolving this properly needs either an extra optional payload field
    or a ``schema_version`` bump, and both change the frozen contract, so
    this is reported rather than worked around.
    """
    catalog_path = tmp_path / "catalog.json"
    endpoint = _endpoint(on_ingested=_recording_hook(catalog_path))

    accepted = endpoint.submit_durable(
        _TEXT, filename="a.md", doc_type="unknown"
    )
    endpoint.run_durable(accepted.document_id)

    assert list_catalog_entries(catalog_path=catalog_path)[0]["doc_type"] == (
        "narrative"
    )


def test_the_synchronous_path_honours_an_explicit_unknown_override(
    tmp_path: Path,
) -> None:
    """Without the audit round trip there is no gap: the override wins.

    This is the equivalent-or-stronger coverage the durable case's
    limitation must not cost: the override contract IS honoured where
    the accepted metadata is not flattened by persistence.
    """
    catalog_path = tmp_path / "catalog.json"
    seen: list[Any] = []
    endpoint = _endpoint(on_ingested=lambda doc: seen.append(doc))

    result = endpoint.ingest(
        _TEXT,
        declared_format="md",
        doc_type="unknown",
        content_identity=True,
    )

    assert result.doc_type == "unknown"
    assert seen[0].doc_type == "unknown"
    assert catalog_path.exists() is False


def test_the_run_uses_the_explicit_title_and_doc_type_when_accepted(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    endpoint = _endpoint(on_ingested=_recording_hook(catalog_path))

    accepted = endpoint.submit_durable(
        _TEXT,
        filename="raw upload name.md",
        title="  Vendor onboarding  ",
        doc_type="table_dense",
        company="Acme",
        period="2024Q1",
    )
    endpoint.run_durable(accepted.document_id)

    row = list_catalog_entries(catalog_path=catalog_path)[0]
    assert row["title"] == "Vendor onboarding"
    assert row["doc_type"] == "table_dense"
    assert row["company"] == "Acme"
    assert row["period"] == "2024Q1"


def test_a_three_concurrent_uploads_produce_three_rows(
    tmp_path: Path,
) -> None:
    """CR-5's concurrency claim, at the endpoint seam: three simultaneous
    accepts of three documents leave three distinct catalog rows."""
    import concurrent.futures

    catalog_path = tmp_path / "catalog.json"
    endpoint = _endpoint(on_ingested=_recording_hook(catalog_path))

    payloads = [b"first document bytes", b"second document bytes", b"third"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        accepted = list(
            pool.map(
                lambda data: endpoint.submit_durable(
                    data, filename="f.md"
                ),
                payloads,
            )
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda sub: endpoint.run_durable(sub.document_id), accepted))

    entries = list_catalog_entries(catalog_path=catalog_path)
    assert len(entries) == 3
    assert len({entry["document_id"] for entry in entries}) == 3


# ---------- helpers ----------


def _stored_keys(objects: InMemoryObjectStore) -> list[str]:
    """The keys the fake store holds.

    The fake has no inspection API, so a test that needs to prove "this
    accept wrote nothing" reads the backing dict instead of the store
    gaining a method that exists only for tests.
    """
    return list(objects._objects)


def _recording_hook(catalog_path: Path):
    """The production-shaped ``on_ingested`` hook writing the public row.

    Mirrors what the host wires: the row carries title/doc_type and only
    the company/period the document actually has. (The host
    implementation itself is covered by the workspace-http suite.)
    """

    def hook(doc: Any) -> None:
        row: dict[str, Any] = {
            "document_id": doc.document_id,
            "version": doc.version,
            "title": doc.title,
            "doc_type": doc.doc_type,
            "page_count": doc.page_count,
        }
        if doc.company:
            row["company"] = doc.company
        if doc.period:
            row["period"] = doc.period
        if doc.source_sha256:
            row["source_sha256"] = doc.source_sha256
        record_filings([row], catalog_path=catalog_path)

    return hook


@pytest.fixture(autouse=True)
def _isolate_catalog_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No test here may touch the development catalog default."""
    monkeypatch.setattr(
        catalog_mod, "DEFAULT_CATALOG_PATH", tmp_path / "unused.json"
    )
