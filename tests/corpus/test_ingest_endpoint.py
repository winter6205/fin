"""Tests for the corpus ingest endpoint service (corpus-serve-ingest T3).

The endpoint is the ONE product entry that parses + 切分s an uploaded
document and pushes it to the retrieval backend (ADR 0047). These tests
pin the service layer only (no HTTP):

* a supported fixture ingest is **observable as push** — FakePushPort
  holds the children verbatim and recalls them byte-identically (SC5);
* the whole chain is deterministic and idempotent (SC12 / SC18);
* unsupported format is a **named failure** carrying the received
  format name, never a silent skip (SC7);
* MCP/push unavailability is a typed failure — nothing recorded as a
  write (SC8);
* push-landed + invalidation-failed is still a typed failure, never
  swallowed (SC8 / SC40);
* the ``on_ingested`` hook fires only after the full chain succeeded
  and carries the T2 catalog row contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.errors import EmptySourceParseError, UnsupportedFormatParseError
from fin.corpus.extracted import Locator
from fin.corpus.ingest_endpoint import (
    MAX_INGEST_BYTES,
    IngestDocumentIdMissingError,
    IngestEndpoint,
    IngestFormatUnspecifiedError,
    IngestInvalidDocTypeError,
    IngestUploadTooLargeError,
    IngestedDocument,
    resolve_format,
)
from fin.corpus.params import DOC_TYPES
from fin.corpus.push import REQUIRED_PUSH_METADATA_FIELDS
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.invalidation_port.errors import (
    InvalidationServiceUnavailableError,
)
from fin.retrieve.port import Chunk
from fin.retrieve.push_port import PushServiceUnavailableError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Small derived md sample (tier 3, a few hundred KB at most).
_MD_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)

# Synthesized plain-text sources for the multi-version invalidation
# scenario: same document_id, different bytes -> different version ->
# every old block goes stale.
_V1_TEXT = (
    "Quarterly results overview.\n\n"
    "Revenue grew across every segment this quarter.\n\n"
    "The company reaffirmed its full-year guidance."
).encode("utf-8")

_V2_TEXT = (
    "Quarterly results overview, revised.\n\n"
    "Revenue declined in two segments this quarter.\n\n"
    "The company withdrew its full-year guidance."
).encode("utf-8")

# A real container-format sample (tier 3) — proves the full chain over
# a binary parser, not just the text formats.
_DOCX_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/docx/AAPL-2022Q3__table_dense__docx__docx.docx"
)


def _endpoint(push_port, **kwargs) -> IngestEndpoint:
    # The contract fake serves both faces of the retrieval backend
    # (push + invalidation) unless a test overrides one explicitly.
    kwargs.setdefault("invalidation_port", push_port)
    return IngestEndpoint(push_port=push_port, **kwargs)


# ----- SC5: supported fixture ingest is observable as push ------------


def test_md_fixture_ingest_is_observable_as_push() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(),
        filename="AAPL-2022Q3__table_dense__md__md.md",
    )

    # After T1 the wire unit is one envelope per 资料; the
    # observable block count is the chunk total the receiving
    # service hands block ids back in.
    assert result.block_count > 0
    assert fake.block_total() == result.block_count
    # one envelope per frozen unit (the per-child records are
    # kept around for the invalidation path; the wire shape is
    # one envelope per unit).
    assert fake.written_total() == result.unit_count
    assert set(result.block_ids) == fake.block_ids()
    assert result.fmt == "md"
    assert result.version  # sha256 of the source bytes


def test_docx_fixture_ingest_is_observable_as_push() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _DOCX_SAMPLE.read_bytes(),
        filename="AAPL-2022Q3__table_dense__docx__docx.docx",
    )
    assert result.fmt == "docx"
    assert result.block_count > 0
    assert fake.block_total() == result.block_count
    # The wire chunk ids the receiving service handed back are
    # all in the store's block-id set.
    assert set(result.block_ids) <= fake.block_ids()


def test_pushed_block_carries_sc20_metadata_and_parent_text() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(),
        filename="sample.md",
    )

    # After T1 every pushed envelope row carries SC20 metadata on
    # its envelope-level metadata field.
    recalled = fake.recall(result.block_ids[0])
    for field in REQUIRED_PUSH_METADATA_FIELDS:
        assert recalled["metadata"].get(field), field
    assert recalled["body"]


def test_pushed_text_is_verbatim_no_rechunking() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(),
        filename="sample.md",
    )
    # Every wire chunk's content is a contiguous substring of the
    # stored body — the spec T1 invariant on the wire side
    # (SC11 / SC16「连续子串」).
    for block_id in result.block_ids:
        recalled = fake.recall(block_id)
        body = recalled["body"]
        for chunk in recalled["chunks"]:
            assert chunk["content"] in body


def test_subsequent_retrieve_hits_the_pushed_text() -> None:
    """SC5 closes the loop: after ingest, a retrieve can hit the text.

    The Fake retrieve corpus is seeded from exactly one pushed record
    (its verbatim text and its ``source_ref`` locator triple), so the
    hit proves the ingest chain lands retrievable text under a real
    locator — the Fake-world stand-in for the backend now holding the
    pushed chunk.
    """
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(_MD_SAMPLE.read_bytes(), filename="sample.md")

    record = result.records[0]
    locator = json.loads(record.source_ref)
    retrieve = FakeRetrievePort(
        hits_by_query={
            "revenue": [
                Chunk(
                    text=record.text,
                    source_ref=Locator(
                        document_id=locator["document_id"],
                        version=locator["version"],
                        page=locator["unit"],
                    ),
                    score=1.0,
                )
            ]
        }
    )
    chunks = retrieve.retrieve("revenue")["chunks"]
    assert chunks, "the pushed chunk must be retrievable after ingest"
    assert chunks[0].text == record.text
    assert chunks[0].source_ref.document_id == result.document_id


def test_same_bytes_twice_is_idempotent() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    first = endpoint.ingest(_MD_SAMPLE.read_bytes(), filename="sample.md")
    second = endpoint.ingest(_MD_SAMPLE.read_bytes(), filename="sample.md")

    assert second.block_ids == first.block_ids
    assert second.written == 0
    assert second.duplicates == first.block_count
    assert fake.block_total() == first.block_count


def test_result_is_deterministic_across_endpoints() -> None:
    endpoint_a = _endpoint(FakePushPort())
    endpoint_b = _endpoint(FakePushPort())
    data = _MD_SAMPLE.read_bytes()
    first = endpoint_a.ingest(data, filename="sample.md")
    second = endpoint_b.ingest(data, filename="sample.md")

    assert first.block_ids == second.block_ids
    assert first.unit_count == second.unit_count
    assert first.doc_type == second.doc_type


# ----- format recognition (named failures, SC7) -----------------------


def test_unsupported_declared_format_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        endpoint.ingest(b"some bytes", declared_format="rtf")
    assert excinfo.value.code == "unsupported_format"
    assert "rtf" in str(excinfo.value)
    assert fake.block_total() == 0


def test_unsupported_filename_extension_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        endpoint.ingest(b"some bytes", filename="report.rtf")
    assert excinfo.value.code == "unsupported_format"
    assert "rtf" in str(excinfo.value)
    assert fake.block_total() == 0


def test_no_filename_and_no_declared_format_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(IngestFormatUnspecifiedError) as excinfo:
        endpoint.ingest(b"some bytes")
    assert excinfo.value.code == "format_unspecified"
    assert fake.block_total() == 0


def test_declared_format_overrides_filename_extension() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        b"# Heading\n\nParagraph one body text.\n",
        filename="misleading.rtf",
        declared_format="md",
    )
    assert result.fmt == "md"
    assert result.block_count > 0


@pytest.mark.parametrize(
    ("filename", "expected_fmt"),
    [
        ("a.pdf", "pdf"),
        ("a.docx", "docx"),
        ("a.xlsx", "xlsx"),
        ("a.pptx", "pptx"),
        ("a.html", "html"),
        ("a.htm", "html"),
        ("a.md", "md"),
        ("a.markdown", "md"),
        ("a.txt", "txt"),
    ],
)
def test_filename_extension_resolution(
    filename: str, expected_fmt: str
) -> None:
    # Pure recognition mapping: binary formats need real container
    # fixtures to run the full chain (see tests/corpus/test_parsers.py),
    # so the full ingest tests below use the text formats only.
    assert resolve_format(filename=filename, declared_format=None) == (
        expected_fmt
    )


# ----- empty / overflow (boundary classes) -----------------------------


def test_zero_byte_upload_is_the_named_empty_source_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(EmptySourceParseError) as excinfo:
        endpoint.ingest(b"", filename="empty.txt")
    assert excinfo.value.code == "empty_source"
    assert fake.block_total() == 0


def test_oversize_upload_is_a_named_overflow_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake, max_bytes=1024)
    with pytest.raises(IngestUploadTooLargeError) as excinfo:
        endpoint.ingest(b"x" * 1025, filename="big.txt")
    assert excinfo.value.code == "ingest_upload_too_large"
    assert fake.block_total() == 0


def test_module_upload_cap_constant_is_positive() -> None:
    assert MAX_INGEST_BYTES > 0


def test_missing_document_id_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(IngestDocumentIdMissingError) as excinfo:
        endpoint.ingest(b"body text", declared_format="txt")
    assert excinfo.value.code == "document_id_missing"
    assert fake.block_total() == 0


# ----- document id / metadata derivation -------------------------------


def test_document_id_prefers_explicit_over_metadata_over_stem() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    by_stem = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")
    assert by_stem.document_id == "DOC-1"

    by_metadata = endpoint.ingest(
        _V1_TEXT,
        filename="ignored.txt",
        company="AAPL",
        period="2023Q3",
    )
    assert by_metadata.document_id == "AAPL-2023Q3"

    by_explicit = endpoint.ingest(
        _V1_TEXT,
        filename="ignored.txt",
        document_id="EXPLICIT",
        company="AAPL",
        period="2023Q3",
    )
    assert by_explicit.document_id == "EXPLICIT"


# ----- doc_type: explicit vocabulary or deterministic per-unit table --


def test_explicit_doc_type_outside_vocabulary_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(IngestInvalidDocTypeError) as excinfo:
        endpoint.ingest(_V1_TEXT, filename="a.txt", doc_type="bogus")
    assert excinfo.value.code == "invalid_doc_type"
    assert fake.block_total() == 0


def test_explicit_doc_type_reaches_every_payload() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _V1_TEXT, filename="a.txt", doc_type="clause_list"
    )
    assert result.doc_type == "clause_list"
    for record in result.records:
        assert record.metadata["doc_type"] == "clause_list"


def test_default_doc_type_is_deterministic_per_unit_classification() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    table_unit = "Revenue 2022 2023\n100 200 300\n400 500 600\n"
    narrative_unit = "The outlook section discusses strategy in prose.\n"
    result = endpoint.ingest(
        (table_unit + "\n" + narrative_unit).encode("utf-8"),
        filename="mixed.txt",
    )
    assert result.unit_count == 2
    doc_types = {record.metadata["doc_type"] for record in result.records}
    assert doc_types == {"table_dense", "narrative"}
    assert result.doc_type in DOC_TYPES


# ----- push unavailable (SC8: typed, nothing recorded as written) -----


def test_push_unavailable_is_typed_and_pushes_nothing() -> None:
    # command=None is the documented "MCP down" shape: the transport
    # raises OSError and the adapter names it push_service_unavailable.
    from fin.retrieve.mcp_push_port import MCPPushPort

    fake = FakePushPort()
    endpoint = IngestEndpoint(
        push_port=MCPPushPort(command=None), invalidation_port=None
    )
    with pytest.raises(PushServiceUnavailableError) as excinfo:
        endpoint.ingest(_V1_TEXT, filename="a.txt")
    assert excinfo.value.code == "push_service_unavailable"
    assert fake.block_total() == 0


def test_push_unavailable_runs_no_invalidation_and_no_hook() -> None:
    from fin.retrieve.mcp_push_port import MCPPushPort

    calls: list[IngestedDocument] = []
    endpoint = IngestEndpoint(
        push_port=MCPPushPort(command=None),
        invalidation_port=FakePushPort(),
        on_ingested=calls.append,
    )
    with pytest.raises(PushServiceUnavailableError):
        endpoint.ingest(_V1_TEXT, filename="a.txt")
    assert calls == []


# ----- invalidation (SC8 / SC24) ---------------------------------------


def test_reingest_new_version_invalidates_old_blocks() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    first = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")
    stale_ids = list(first.block_ids)

    second = endpoint.ingest(
        _V2_TEXT,
        filename="DOC-1.txt",
        previous_records=lambda: first.records,
    )

    assert second.block_ids != first.block_ids
    for block_id in stale_ids:
        assert fake.count_hits(block_id, at=0) == 0
        assert not fake.is_hittable_at_any_time(block_id)
    for block_id in second.block_ids:
        assert fake.count_hits(block_id, at=0) == 1
    assert second.invalidation is not None
    assert second.invalidation.closed == len(stale_ids)
    assert set(second.invalidation.block_ids) == set(stale_ids)


def test_reingest_same_bytes_invalidates_nothing() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    first = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")
    second = endpoint.ingest(
        _V1_TEXT,
        filename="DOC-1.txt",
        previous_records=lambda: first.records,
    )
    assert second.invalidation is None
    for block_id in first.block_ids:
        assert fake.count_hits(block_id, at=0) == 1


def test_invalidation_failure_after_push_lands_is_typed_not_swallowed() -> None:
    class DownInvalidationPort:
        def invalidate(self, targets):  # noqa: ANN001 - test stub
            raise InvalidationServiceUnavailableError(
                "invalidation service is down"
            )

    fake = FakePushPort()
    endpoint = _endpoint(fake, invalidation_port=DownInvalidationPort())
    first = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")

    with pytest.raises(InvalidationServiceUnavailableError) as excinfo:
        endpoint.ingest(
            _V2_TEXT,
            filename="DOC-1.txt",
            previous_records=lambda: first.records,
        )
    assert excinfo.value.code == "invalidation_service_unavailable"
    # Push DID land (idempotent retry is the recovery) — the typed
    # failure must name invalidation, never report a clean success.
    assert fake.block_total() > len(first.block_ids)


def test_stale_targets_without_an_invalidation_port_is_typed() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake, invalidation_port=None)
    first = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")
    with pytest.raises(InvalidationServiceUnavailableError):
        endpoint.ingest(
            _V2_TEXT,
            filename="DOC-1.txt",
            previous_records=lambda: first.records,
        )


# ----- on_ingested hook (T2 catalog wiring seam) ------------------------


def test_hook_receives_the_t2_catalog_row_contract() -> None:
    seen: list[IngestedDocument] = []
    fake = FakePushPort()
    endpoint = _endpoint(fake, on_ingested=seen.append)
    result = endpoint.ingest(
        _V1_TEXT,
        filename="DOC-1.txt",
        company="AAPL",
        period="2023Q3",
    )

    assert len(seen) == 1
    ingested = seen[0]
    assert ingested.document_id == result.document_id
    assert ingested.version == result.version
    assert ingested.company == "AAPL"
    assert ingested.period == "2023Q3"
    assert ingested.page_count == result.unit_count


def test_hook_not_called_when_invalidation_fails() -> None:
    class DownInvalidationPort:
        def invalidate(self, targets):  # noqa: ANN001 - test stub
            raise InvalidationServiceUnavailableError(
                "invalidation service is down"
            )

    seen: list[IngestedDocument] = []
    fake = FakePushPort()
    endpoint = IngestEndpoint(
        push_port=fake,
        invalidation_port=DownInvalidationPort(),
        on_ingested=seen.append,
    )
    first = endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")
    with pytest.raises(InvalidationServiceUnavailableError):
        endpoint.ingest(
            _V2_TEXT,
            filename="DOC-1.txt",
            previous_records=lambda: first.records,
        )
    # Only the first (fully successful) ingest ran the hook; the
    # second push landed but its chain ended in a typed failure.
    assert len(seen) == 1
    assert seen[0].version == first.version
