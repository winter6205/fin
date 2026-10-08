"""Tests for the corpus ingest endpoint service (corpus-serve-ingest).

The endpoint carries two entry points — the durable accept door
(``submit_durable``) and the synchronous parse + 切分 + push chain
(``ingest``, ADR 0047). These tests pin the service layer only (no HTTP):

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
  and carries the catalog row contract;
* ``submit_durable`` is the accept door of the permanent upload path:
  metadata-only validations run before a single byte is written, the
  four accept-time items (raw bytes, pointer, ``pending`` job record,
  ``(document_id, version)`` identity) are all durable before an
  accept, a duplicate ``(document_id, version)`` joins the existing
  job, a different version in flight is a typed conflict, a different
  version of a ``ready`` source is the settled replacement (the stale
  blocks are closed, one job reopens as a fresh ``pending`` generation,
  and one logical version remains), and a failed
  commit leaves no accepted job (with a failed compensating delete
  observable rather than swallowed);
* ``job_status`` reports the persisted job state, never a constant.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.errors import (
    EmptySourceParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.extracted import Locator
from fin.corpus.ingest_endpoint import (
    MAX_INGEST_BYTES,
    IngestConcurrentRunError,
    IngestDurableCommitError,
    IngestDocumentIdMissingError,
    IngestEndpoint,
    IngestFormatUnspecifiedError,
    IngestInvalidDocTypeError,
    IngestObjectCleanupError,
    IngestUploadTooLargeError,
    IngestedDocument,
    resolve_document_id,
    resolve_format,
)
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.ingest_state_errors import UnknownJobError
from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataPointer,
    MetadataPointerNotFoundError,
    MetadataStoreError,
)
from fin.corpus.object_store import InMemoryObjectStore, ObjectPointer
from fin.corpus.object_store_errors import ObjectStoreUnavailableError
from fin.corpus.params import DOC_TYPES
from fin.corpus.parsers import SUPPORTED_FORMATS
from fin.corpus.push import REQUIRED_PUSH_METADATA_FIELDS
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.iknow_identity import compute_iknow_content_identity
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


class _RecordingObjectStore(InMemoryObjectStore):
    """In-memory object store that records every key it was asked to write."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer:
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)

    def delete(self, key: str) -> None:
        self.deletes.append(key)
        super().delete(key)


class _FailingDeleteObjectStore(InMemoryObjectStore):
    """Object store that cannot complete a delete (cleanup outage)."""

    def delete(self, key: str) -> None:
        raise ObjectStoreUnavailableError(f"delete failed for {key!r}")


class _FailingPointerStore(InMemoryMetadataStore):
    """Pointer store whose commit fails (metadata outage)."""

    def put_pointer(self, document_id: str, pointer: ObjectPointer) -> None:
        raise MetadataStoreError("pointer table is unavailable")


class _HidingPointerStore(InMemoryMetadataStore):
    """Pointer store that can forget a document's row.

    Simulates the stores disagreeing with themselves — a ``ready`` job
    whose pointer row is gone — so the endpoint's refusal path can be
    exercised without corrupting a real database.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hidden: set[str] = set()

    def get_pointer(self, document_id: str) -> MetadataPointer:
        if document_id in self.hidden:
            raise MetadataPointerNotFoundError(
                f"no pointer recorded for {document_id!r}"
            )
        return super().get_pointer(document_id)


class _FailingCreateStateStore(InMemoryIngestStateStore):
    """State store that cannot record a job (state-table outage).

    ``fail_after`` keeps the first N creates working so a test can reach
    the job stage of a *replacement* and break there.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        super().__init__()
        self._fail_after = fail_after
        self._creates = 0

    def create_job(
        self, document_id: str, *, actor: str | None = None
    ) -> Any:
        self._creates += 1
        if self._fail_after is not None and self._creates <= self._fail_after:
            return super().create_job(document_id, actor=actor)
        raise RuntimeError("job table is unavailable")


@dataclass(frozen=True)
class _DurableStack:
    """One endpoint over the three accept-time ports, all observable."""

    endpoint: IngestEndpoint
    objects: Any
    pointers: Any
    states: InMemoryIngestStateStore
    push: FakePushPort


def _durable_stack(**overrides: Any) -> _DurableStack:
    objects = overrides.pop("objects", None) or _RecordingObjectStore()
    pointers = overrides.pop("pointers", None) or InMemoryMetadataStore()
    states = overrides.pop("states", None) or InMemoryIngestStateStore()
    push = FakePushPort()
    # A test can hand in its own invalidation face (``None`` for a backend
    # with no invalidation service); the default is the same contract fake
    # that serves push, as in one retrieval backend with both doors.
    invalidation = overrides.pop("invalidation_port", push)
    endpoint = IngestEndpoint(
        push_port=push,
        invalidation_port=invalidation,
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
        **overrides,
    )
    return _DurableStack(
        endpoint=endpoint,
        objects=objects,
        pointers=pointers,
        states=states,
        push=push,
    )


def _submit(
    stack: _DurableStack,
    data: bytes,
    *,
    document_id: str = "DOC-1",
    **overrides: Any,
) -> Any:
    """Submit one durable upload with the text format declared."""
    overrides.setdefault("declared_format", "txt")
    return stack.endpoint.submit_durable(
        data, document_id=document_id, **overrides
    )


def _pointer_of(pointers: Any, document_id: str) -> ObjectPointer | None:
    try:
        return pointers.get_pointer(document_id).pointer
    except MetadataPointerNotFoundError:
        return None


def _assert_nothing_persisted(
    stack: _DurableStack, document_id: str = "DOC-1"
) -> None:
    """The reject-before-write invariant: no durable item landed."""
    assert stack.objects.puts == []
    assert _pointer_of(stack.pointers, document_id) is None
    with pytest.raises(UnknownJobError):
        stack.states.get_job(document_id)
    assert stack.push.written_total() == 0


def _advance(states: InMemoryIngestStateStore, document_id: str) -> None:
    """Walk one job through the whole legal chain to ``ready``."""
    for to_state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        states.transition(document_id, to_state)


# ----- SC5: supported fixture ingest is observable as push ------------


def test_md_fixture_ingest_is_observable_as_push() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(),
        filename="AAPL-2022Q3__table_dense__md__md.md",
        company="AAPL",
        period="2022Q3",
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
        company="AAPL",
        period="2022Q3",
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
        document_id="SAMPLE",
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
        document_id="SAMPLE",
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
    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(), document_id="SAMPLE", declared_format="md"
    )

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
                        unit=locator["unit"],
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
    first = endpoint.ingest(
        _MD_SAMPLE.read_bytes(), document_id="SAMPLE", declared_format="md"
    )
    second = endpoint.ingest(
        _MD_SAMPLE.read_bytes(), document_id="SAMPLE", declared_format="md"
    )

    assert second.block_ids == first.block_ids
    assert second.written == 0
    assert second.duplicates == first.block_count
    assert fake.block_total() == first.block_count


def test_result_is_deterministic_across_endpoints() -> None:
    endpoint_a = _endpoint(FakePushPort())
    endpoint_b = _endpoint(FakePushPort())
    data = _MD_SAMPLE.read_bytes()
    first = endpoint_a.ingest(
        data, document_id="SAMPLE", declared_format="md"
    )
    second = endpoint_b.ingest(
        data, document_id="SAMPLE", declared_format="md"
    )

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
        document_id="MISLEADING",
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
        # The empty-source class is decided by the parse, not by the
        # identity rule: the locator is supplied explicitly here.
        endpoint.ingest(b"", document_id="EMPTY", declared_format="txt")
    assert excinfo.value.code == "empty_source"
    assert fake.block_total() == 0


def test_oversize_upload_is_a_named_overflow_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake, max_bytes=1024)
    with pytest.raises(IngestUploadTooLargeError) as excinfo:
        endpoint.ingest(b"x" * 1025, document_id="BIG", declared_format="txt")
    assert excinfo.value.code == "ingest_upload_too_large"
    assert fake.block_total() == 0


def test_missing_document_id_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(IngestDocumentIdMissingError) as excinfo:
        endpoint.ingest(b"body text", declared_format="txt")
    assert excinfo.value.code == "document_id_missing"
    assert fake.block_total() == 0


# ----- document id / metadata derivation -------------------------------


def test_document_id_is_metadata_derived_or_explicit_never_the_filename() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
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

    # The filename is metadata only: an upload that names itself
    # ``DOC-1.txt`` with no metadata at all has no identity, because a
    # re-upload with a different file name would otherwise land a second
    # logical document.
    with pytest.raises(IngestDocumentIdMissingError):
        endpoint.ingest(_V1_TEXT, filename="DOC-1.txt")


# ----- doc_type: explicit vocabulary or deterministic per-unit table --


def test_explicit_doc_type_outside_vocabulary_is_a_named_failure() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    with pytest.raises(IngestInvalidDocTypeError) as excinfo:
        endpoint.ingest(
            _V1_TEXT,
            document_id="DOC-1",
            declared_format="txt",
            doc_type="bogus",
        )
    assert excinfo.value.code == "invalid_doc_type"
    assert fake.block_total() == 0


def test_explicit_doc_type_reaches_every_payload() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    result = endpoint.ingest(
        _V1_TEXT,
        document_id="DOC-1",
        declared_format="txt",
        doc_type="clause_list",
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
        document_id="MIXED-2024Q1",
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
        endpoint.ingest(
            _V1_TEXT, document_id="DOC-1", declared_format="txt"
        )
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
        endpoint.ingest(
            _V1_TEXT, document_id="DOC-1", declared_format="txt"
        )
    assert calls == []


# ----- invalidation (SC8 / SC24) ---------------------------------------


def test_reingest_new_version_invalidates_old_blocks() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    stale_ids = list(first.block_ids)

    second = endpoint.ingest(
        _V2_TEXT,
        document_id="DOC-1",
        declared_format="txt",
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
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    second = endpoint.ingest(
        _V1_TEXT,
        document_id="DOC-1",
        declared_format="txt",
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
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )

    with pytest.raises(InvalidationServiceUnavailableError) as excinfo:
        endpoint.ingest(
            _V2_TEXT,
            document_id="DOC-1",
            declared_format="txt",
            previous_records=lambda: first.records,
        )
    assert excinfo.value.code == "invalidation_service_unavailable"
    # Push DID land (idempotent retry is the recovery) — the typed
    # failure must name invalidation, never report a clean success.
    assert fake.block_total() > len(first.block_ids)


def test_stale_targets_without_an_invalidation_port_is_typed() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake, invalidation_port=None)
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    with pytest.raises(InvalidationServiceUnavailableError):
        endpoint.ingest(
            _V2_TEXT,
            document_id="DOC-1",
            declared_format="txt",
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
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    with pytest.raises(InvalidationServiceUnavailableError):
        endpoint.ingest(
            _V2_TEXT,
            document_id="DOC-1",
            declared_format="txt",
            previous_records=lambda: first.records,
        )
    # Only the first (fully successful) ingest ran the hook; the
    # second push landed but its chain ended in a typed failure.
    assert len(seen) == 1
    assert seen[0].version == first.version


# ----- durable accept: submit_durable (accept-time durability) ---------


def test_derivation_is_ticker_plus_fiscal_period() -> None:
    # The spec's worked example: the metadata pair is the identity.
    assert (
        resolve_document_id(document_id=None, company="aapl", period="2022q3")
        == "AAPL-2022Q3"
    )
    assert (
        resolve_document_id(
            document_id=None, company="  aapl ", period=" 2022Q3"
        )
        == "AAPL-2022Q3"
    )
    assert (
        resolve_document_id(document_id=" DOC-9 ", company="aapl", period="1")
        == "DOC-9"
    )


@pytest.mark.parametrize(
    "company, period",
    [("aapl", None), ("aapl", "  "), (None, "2022q3"), (None, None), ("", "")],
)
def test_an_incomplete_metadata_pair_is_document_id_missing(
    company: str | None, period: str | None
) -> None:
    with pytest.raises(IngestDocumentIdMissingError) as excinfo:
        resolve_document_id(document_id=None, company=company, period=period)
    assert excinfo.value.code == "document_id_missing"


def test_filename_never_participates_in_identity() -> None:
    stack = _durable_stack()
    with pytest.raises(IngestDocumentIdMissingError):
        stack.endpoint.submit_durable(_V1_TEXT, filename="AAPL-2022Q3.md")
    _assert_nothing_persisted(stack)


def test_acceptance_requires_the_four_durable_items_and_pushes_nothing() -> None:
    stack = _durable_stack()
    submission = _submit(stack, _V1_TEXT)
    version = hashlib.sha256(_V1_TEXT).hexdigest()

    assert submission.job_id == "DOC-1"
    assert submission.document_id == "DOC-1"
    assert submission.version == version
    assert submission.state is IngestState.PENDING
    # 1 raw bytes
    key = f"filings/DOC-1/{version}"
    assert stack.objects.puts == [key]
    assert stack.objects.get(key) == _V1_TEXT
    # 2 metadata pointer, carrying the same version
    pointer = _pointer_of(stack.pointers, "DOC-1")
    assert pointer is not None
    assert pointer.key == key
    assert pointer.sha256 == version
    assert pointer.size_bytes == len(_V1_TEXT)
    assert pointer.content_type == "text/plain"
    # 3 the pending job record
    assert stack.states.get_job("DOC-1").state is IngestState.PENDING
    # the accept door does no pipeline work: parse / 切分 / push are the
    # async worker's, so nothing reached the push port.
    assert stack.push.written_total() == 0


def test_exact_cap_is_accepted_and_one_byte_over_is_refused() -> None:
    # The pinned contract value; the route gate reads the same number off
    # the service, so the two can never disagree on the boundary.
    assert MAX_INGEST_BYTES == 52_428_800
    stack = _durable_stack()
    at_cap = b"x" * MAX_INGEST_BYTES
    accepted = _submit(stack, at_cap, document_id="CAP")
    assert accepted.state is IngestState.PENDING
    with pytest.raises(IngestUploadTooLargeError) as excinfo:
        _submit(stack, at_cap + b"x", document_id="CAP-OVER")
    assert excinfo.value.code == "ingest_upload_too_large"
    assert stack.objects.puts == [f"filings/CAP/{accepted.version}"]


def test_unsupported_format_is_refused_before_any_write() -> None:
    stack = _durable_stack()
    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        _submit(stack, _V1_TEXT, declared_format="rtf")
    assert excinfo.value.code == "unsupported_format"
    _assert_nothing_persisted(stack)


def test_invalid_doc_type_is_refused_before_any_write() -> None:
    stack = _durable_stack()
    with pytest.raises(IngestInvalidDocTypeError) as excinfo:
        _submit(stack, _V1_TEXT, doc_type="bogus")
    assert excinfo.value.code == "invalid_doc_type"
    _assert_nothing_persisted(stack)


def test_every_supported_format_has_a_media_type() -> None:
    # The durable accept keys the raw object's media type off the parser
    # format table, so a format added on one side but not the other would
    # raise a bare KeyError inside the write. Pin the two vocabularies to
    # one another so that drift fails here, not in production.
    from fin.corpus.ingest_endpoint import _FORMAT_CONTENT_TYPES

    assert set(_FORMAT_CONTENT_TYPES) == set(SUPPORTED_FORMATS)


def test_duplicate_submission_joins_the_job_without_a_second_write() -> None:
    stack = _durable_stack()
    first = _submit(stack, _V1_TEXT)
    _advance(stack.states, "DOC-1")

    joined = _submit(stack, _V1_TEXT, filename="renamed-by-the-uploader.txt")

    assert joined.job_id == first.job_id
    assert joined.version == first.version
    # the joined job's CURRENT state is reported, not a fresh accept
    assert joined.state is IngestState.READY
    assert len(stack.objects.puts) == 1
    assert stack.states.get_job("DOC-1").state is IngestState.READY


def test_concurrent_identical_submissions_join_without_deleting() -> None:
    # Two uploads of the same bytes racing on one document must both resolve
    # to the accepted job and never compensate the shared object away: a
    # loser that deleted the key the winner just committed would fail a
    # durable accept that already succeeded. The per-document submit lock
    # serializes the check-write-read, and a lost race joins rather than
    # raising, so exactly one object lands and none is deleted.
    stack = _durable_stack()
    version = hashlib.sha256(_V1_TEXT).hexdigest()
    key = f"filings/DOC-1/{version}"
    start = threading.Barrier(2)
    outcomes: list[object] = []
    failures: list[BaseException] = []

    def submit() -> None:
        start.wait()
        try:
            outcomes.append(_submit(stack, _V1_TEXT))
        except BaseException as exc:  # noqa: BLE001 - re-raised in main
            failures.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == [], failures
    assert len(outcomes) == 2
    assert {submission.version for submission in outcomes} == {version}
    assert {submission.job_id for submission in outcomes} == {"DOC-1"}
    # one object, one job, and no compensating delete of the shared key
    assert stack.objects.puts == [key]
    assert stack.objects.deletes == []
    assert stack.states.get_job("DOC-1").state is IngestState.PENDING


def test_different_version_while_in_flight_is_a_typed_conflict() -> None:
    stack = _durable_stack()
    first = _submit(stack, _V1_TEXT)
    stack.states.transition("DOC-1", IngestState.FETCHING)

    with pytest.raises(IngestConcurrentRunError) as excinfo:
        _submit(stack, _V2_TEXT)

    assert excinfo.value.code == "concurrent_run"
    # the in-flight job and its version are untouched
    assert stack.states.get_job("DOC-1").state is IngestState.FETCHING
    assert _pointer_of(stack.pointers, "DOC-1").sha256 == first.version
    assert len(stack.objects.puts) == 1


def test_a_failed_job_reenters_on_the_retry_edge() -> None:
    stack = _durable_stack()
    _submit(stack, _V1_TEXT)
    stack.states.transition("DOC-1", IngestState.FETCHING)
    with pytest.raises(Exception):  # noqa: BLE001 - the store's own raise
        stack.states.fail_step(
            "DOC-1", step="extract", cause=RuntimeError("extractor died")
        )

    replacement = _submit(stack, _V2_TEXT)

    # ``failed`` has no edge back to ``pending``; the machine's retry edge
    # is ``failed -> fetching``, and that is what is reported.
    assert replacement.state is IngestState.FETCHING
    assert _pointer_of(stack.pointers, "DOC-1").sha256 == replacement.version
    assert stack.states.get_job("DOC-1").state is IngestState.FETCHING


def test_replacing_a_ready_document_invalidates_the_stale_blocks(
) -> None:
    # The settled replacement: a different version of a ``ready`` source is
    # accepted, the blocks the replaced version pushed are closed through the
    # invalidation port, and the document's one job reopens. ``ready`` is
    # terminal, so the reopened job is a fresh ``pending`` generation of the
    # same document job rather than a transition out of ``ready`` — and the
    # document is left naming exactly one logical version.
    pushed: list[Any] = []
    stack = _durable_stack(previous_records=lambda: pushed)
    first = _submit(stack, _V1_TEXT)
    pushed.extend(
        stack.endpoint.ingest(
            _V1_TEXT, declared_format="txt", document_id="DOC-1"
        ).records
    )
    assert pushed
    _advance(stack.states, "DOC-1")

    second = _submit(stack, _V2_TEXT)

    assert second.job_id == first.job_id
    assert second.version == hashlib.sha256(_V2_TEXT).hexdigest()
    assert second.state is IngestState.PENDING
    assert stack.states.get_job("DOC-1").state is IngestState.PENDING
    # one logical version: the durable pointer names the new bytes only
    assert _pointer_of(stack.pointers, "DOC-1").sha256 == second.version
    assert len(stack.objects.puts) == 2
    # every block the replaced version pushed was closed, with the reason
    # that attributes it to a version move
    closed = stack.push.invalidation_records()
    assert sorted(row["block_id"] for row in closed) == sorted(
        record.block_id for record in pushed
    )
    assert {row["reason"] for row in closed} == {"unit_version_changed"}
    # the superseded generation stays in the audit trail
    superseded = [
        row
        for row in stack.states.list_audit("DOC-1")
        if row.action == "job_superseded"
    ]
    assert [(row.from_state, row.to_state) for row in superseded] == [
        (IngestState.READY, IngestState.PENDING)
    ]


def test_resubmitting_the_replacement_version_joins_without_a_second_reopen(
) -> None:
    # The replacement moves the document's pointer, so the same
    # ``(document_id, version)`` submitted again is an idempotent join on the
    # reopened job: no third object, no second generation, no second
    # invalidation request.
    pushed: list[Any] = []
    stack = _durable_stack(previous_records=lambda: pushed)
    first = _submit(stack, _V1_TEXT)
    pushed.extend(
        stack.endpoint.ingest(
            _V1_TEXT, declared_format="txt", document_id="DOC-1"
        ).records
    )
    _advance(stack.states, "DOC-1")
    replacement = _submit(stack, _V2_TEXT)
    closed_after_replacement = stack.push.invalidation_records()

    joined = _submit(stack, _V2_TEXT)

    assert joined.job_id == first.job_id == replacement.job_id
    assert joined.version == replacement.version
    assert joined.state is IngestState.PENDING
    assert len(stack.objects.puts) == 2
    assert stack.push.invalidation_records() == closed_after_replacement
    assert len(
        [
            row
            for row in stack.states.list_audit("DOC-1")
            if row.action == "job_superseded"
        ]
    ) == 1


def test_a_replacement_that_cannot_close_its_stale_blocks_is_refused() -> None:
    # Stale blocks with no invalidation service is a typed failure, never a
    # silent accept of a version whose predecessor is still hittable: the
    # object written for this attempt goes away and the document keeps the
    # version, pointer and ``ready`` job it had before.
    pushed: list[Any] = []
    stack = _durable_stack(
        previous_records=lambda: pushed, invalidation_port=None
    )
    first = _submit(stack, _V1_TEXT)
    pushed.extend(
        stack.endpoint.ingest(
            _V1_TEXT, declared_format="txt", document_id="DOC-1"
        ).records
    )
    _advance(stack.states, "DOC-1")

    with pytest.raises(InvalidationServiceUnavailableError):
        _submit(stack, _V2_TEXT)

    assert _pointer_of(stack.pointers, "DOC-1").sha256 == first.version
    assert stack.states.get_job("DOC-1").state is IngestState.READY
    assert stack.objects.deletes == [stack.objects.puts[1]]
    assert stack.push.invalidation_records() == ()


def test_a_replacement_with_nothing_known_still_swaps_the_version() -> None:
    # No record provider means fin cannot name what is stale — the same
    # documented no-op the synchronous chain has, never a skipped failure.
    # The replacement itself is still settled: new version, reopened job.
    stack = _durable_stack()
    _submit(stack, _V1_TEXT)
    _advance(stack.states, "DOC-1")

    second = _submit(stack, _V2_TEXT)

    assert second.state is IngestState.PENDING
    assert _pointer_of(stack.pointers, "DOC-1").sha256 == second.version
    assert stack.states.get_job("DOC-1").state is IngestState.PENDING


def test_a_ready_job_with_no_pointer_row_refuses_before_any_write() -> None:
    # ``ready`` is reached only by an accept that made its pointer durable,
    # so a ready job whose pointer row is missing is the stores disagreeing
    # with themselves. The replacement accept refuses typed at the pointer
    # read (stage ``pointer_read``) instead of guessing what was replaced,
    # and the refusal lands before any write of this attempt.
    pointers = _HidingPointerStore()
    stack = _durable_stack(pointers=pointers)
    _submit(stack, _V1_TEXT)
    _advance(stack.states, "DOC-1")
    pointers.hidden.add("DOC-1")
    puts_before = list(stack.objects.puts)

    with pytest.raises(IngestDurableCommitError) as excinfo:
        _submit(stack, _V2_TEXT)

    assert excinfo.value.code == "durable_commit_failed"
    assert excinfo.value.stage == "pointer_read"
    # nothing of this attempt was written and nothing was compensated away;
    # the disagreeing stores are left exactly as the refusal found them
    assert stack.objects.puts == puts_before
    assert stack.objects.deletes == []
    assert stack.states.get_job("DOC-1").state is IngestState.READY


def test_pointer_commit_failure_leaves_no_accepted_job() -> None:
    stack = _durable_stack(pointers=_FailingPointerStore())
    with pytest.raises(IngestDurableCommitError) as excinfo:
        _submit(stack, _V1_TEXT)
    assert excinfo.value.code == "durable_commit_failed"
    assert excinfo.value.stage == "pointer_write"
    # the object written first was compensated away, and no job exists
    version = hashlib.sha256(_V1_TEXT).hexdigest()
    assert stack.objects.deletes == [f"filings/DOC-1/{version}"]
    with pytest.raises(UnknownJobError):
        stack.states.get_job("DOC-1")


def test_a_rejected_replacement_restores_the_previous_pointer() -> None:
    # The job stage fails AFTER the pointer overwrite, so the compensating
    # cleanup has to put the prior pointer back: the pointer store has no
    # delete, and leaving the new one would advertise an unaccepted version.
    stack = _durable_stack(states=_FailingCreateStateStore(fail_after=1))
    first = _submit(stack, _V1_TEXT)
    stack.states.transition("DOC-1", IngestState.FAILED)

    with pytest.raises(IngestDurableCommitError) as excinfo:
        _submit(stack, _V2_TEXT)

    assert excinfo.value.stage == "job_record"
    assert _pointer_of(stack.pointers, "DOC-1") is not None
    assert _pointer_of(stack.pointers, "DOC-1").sha256 == first.version
    assert len(stack.objects.puts) == 2
    assert stack.objects.deletes == [stack.objects.puts[1]]
    assert stack.states.get_job("DOC-1").state is IngestState.FAILED


def test_a_failing_compensating_delete_is_observable_not_swallowed() -> None:
    stack = _durable_stack(
        objects=_FailingDeleteObjectStore(), pointers=_FailingPointerStore()
    )
    with pytest.raises(IngestObjectCleanupError) as excinfo:
        _submit(stack, _V1_TEXT)
    assert excinfo.value.code == "object_cleanup_failed"
    assert excinfo.value.key == (
        f"filings/DOC-1/{hashlib.sha256(_V1_TEXT).hexdigest()}"
    )
    # the cleanup failure names the persistence failure that triggered it
    # as its cause, with the delete's own failure as the context
    assert isinstance(excinfo.value.__cause__, MetadataStoreError)
    assert isinstance(excinfo.value.__context__, ObjectStoreUnavailableError)


def test_durable_accept_without_wired_stores_is_a_typed_failure() -> None:
    endpoint = IngestEndpoint(push_port=FakePushPort())
    with pytest.raises(IngestDurableCommitError) as excinfo:
        endpoint.submit_durable(_V1_TEXT, document_id="DOC-1", declared_format="txt")
    assert excinfo.value.code == "durable_commit_failed"
    assert excinfo.value.stage == "unconfigured"


def test_the_synchronous_ingest_chain_still_needs_no_stores() -> None:
    # Backward compatibility: the existing chain works with only the push
    # port wired, exactly as before the durable accept door existed.
    fake = FakePushPort()
    endpoint = IngestEndpoint(push_port=fake, invalidation_port=fake)
    result = endpoint.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")
    assert result.block_count > 0
    assert fake.block_total() == result.block_count


# ----- job_status: the polled state is the persisted one ----------------


def test_job_status_reports_the_persisted_state_not_a_constant() -> None:
    stack = _durable_stack()
    submission = _submit(stack, _V1_TEXT)

    status = stack.endpoint.job_status("DOC-1")
    assert status.job_id == "DOC-1"
    assert status.document_id == "DOC-1"
    assert status.version == submission.version
    assert status.state is IngestState.PENDING
    assert status.text_ready is False
    assert status.error_type is None

    stack.states.transition("DOC-1", IngestState.FETCHING)
    moved = stack.endpoint.job_status("DOC-1")
    assert moved.state is IngestState.FETCHING
    assert moved.text_ready is False

    for to_state in (IngestState.EXTRACTING, IngestState.CATALOGING,
                     IngestState.READY):
        stack.states.transition("DOC-1", to_state)
    ready = stack.endpoint.job_status("DOC-1")
    assert ready.state is IngestState.READY
    assert ready.text_ready is True


def test_job_status_carries_the_typed_failure_of_a_failed_job() -> None:
    stack = _durable_stack()
    _submit(stack, _V1_TEXT)
    stack.states.transition("DOC-1", IngestState.FETCHING)
    with pytest.raises(Exception):  # noqa: BLE001 - the store's own raise
        stack.states.fail_step(
            "DOC-1", step="extract", cause=RuntimeError("parser exploded")
        )

    status = stack.endpoint.job_status("DOC-1")

    assert status.state is IngestState.FAILED
    assert status.text_ready is False
    assert status.error_type == "RuntimeError"
    assert "parser exploded" in (status.error_message or "")


def test_job_status_of_an_unknown_job_stays_typed() -> None:
    stack = _durable_stack()
    with pytest.raises(UnknownJobError):
        stack.endpoint.job_status("NEVER-ACCEPTED")


def test_job_status_reads_the_version_from_the_durable_pointer() -> None:
    stack = _durable_stack()
    _submit(stack, _V1_TEXT)
    # The pointer, not the job row, is where the accepted version lives.
    assert stack.endpoint.job_status("DOC-1").version == (
        _pointer_of(stack.pointers, "DOC-1").sha256
    )


# ----- T4 handoff: the ingest result carries the service's per-source identity --
#
# ``compute_iknow_content_identity`` is the one mirror of iknow's formula
# the fake answers with, so re-deriving it here checks that the endpoint
# passed the reported value through — neither dropping it nor substituting
# fin's own hash. It is a re-derivation of the mirror, not an observation
# of the service; the cross-repo claim belongs to the live driver
# (``tests/retrieve/test_live_content_identity.py``, opt-in ``-m mcp``).


def test_result_carries_the_content_identity_of_every_pushed_source() -> None:
    fake = FakePushPort()
    endpoint = _endpoint(fake)

    result = endpoint.ingest(
        _MD_SAMPLE.read_bytes(),
        filename="AAPL-2022Q3__table_dense__md__md.md",
        company="AAPL",
        period="2022Q3",
    )

    assert set(result.content_identities) == {
        record.source_ref for record in result.records
    }
    for block_id in result.block_ids:
        stored = fake.recall(block_id)
        assert (
            result.content_identities[stored["source_ref"]]
            == compute_iknow_content_identity(stored["body"])
        )


def test_result_identity_map_refuses_to_be_written_through() -> None:
    """The result hands on the push run's map of what the service
    reported, and that map is also read by the run's own outcome —
    writing through the result would rewrite both."""
    endpoint = _endpoint(FakePushPort())

    result = endpoint.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")

    with pytest.raises(TypeError):
        result.content_identities["invented"] = "0" * 64  # type: ignore[index]
    assert "invented" not in result.content_identities


def test_reingesting_the_same_bytes_reports_the_same_identities() -> None:
    """``submit_index_job`` is keyed on the pair, so a replay that wrote
    nothing must still hand back the identity of the stored row."""
    fake = FakePushPort()
    endpoint = _endpoint(fake)
    first = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    second = endpoint.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )

    assert second.written == 0
    assert second.content_identities == first.content_identities


# ----- recovering company/period for the catalog row (durable path) -----
#
# The catalog row contract is {document_id, company, period, version,
# page_count} and `company` is required non-empty, but the DURABLE accept
# persisted neither company nor period: `fin_ingest_job` and
# `fin_corpus_object_pointer` have no columns for them, and
# `submit_durable` unpacked the validated pair into `_company` /
# `_period`. The worker therefore drove `ingest()` with no company, the
# catalog hook received company=None, and EVERY uploaded filing failed
# registration with CatalogRowError — the data was pushed and searchable in
# iknow-memory but invisible to fin's own catalog.


def test_a_derived_identity_recovers_its_company_and_period() -> None:
    from fin.corpus.ingest_endpoint import (
        split_company_period_from_document_id as split_id,
    )

    assert split_id("MSFT-2023Q1") == ("MSFT", "2023Q1")
    assert split_id("GOOG-2023FY") == ("GOOG", "2023FY")


@pytest.mark.parametrize(
    "document_id",
    [
        # An SEC accession is not a {TICKER}-{PERIOD} pair; reading it as one
        # would file AAPL's 10-Q under company "0000320193".
        "0000320193-24-000006",
        # A caller may pass ANY verbatim id, so an opaque one must not be
        # guessed at — a wrong company is searchable under a name that does
        # not own the filing, which is worse than an absent one.
        "explicit-id-no-dash",
        "",
        "AAPL-",           # no period half
        "-2022Q3",         # no ticker half
        "aapl-2022Q3",     # lower-case ticker: ids are upper-cased at derive
        "AAPL-2022Q3-EXTRA",  # trailing content is not a fiscal label
    ],
)
def test_an_unrecognised_identity_yields_no_company(document_id: str) -> None:
    from fin.corpus.ingest_endpoint import (
        split_company_period_from_document_id as split_id,
    )

    assert split_id(document_id) == (None, None)


def test_the_durable_run_hands_the_catalog_hook_a_company(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The end-to-end claim: the recovered pair reaches `record_filings`.

    Driven through the REAL durable landing — ``submit_durable`` then
    ``run_durable`` — so the assertion is on what the catalog hook is
    actually handed by the worker. A test that called ``ingest()`` with an
    already-split pair would stay green if the recovery in
    ``_complete_and_land`` were deleted, which is exactly the regression
    this fix exists to prevent.

    The catalog is redirected to ``tmp_path`` so the run leaves the
    committed ``data/corpus/catalog.json`` untouched.
    """
    from pathlib import Path

    import fin.corpus.catalog as catalog_mod
    from fin.corpus.ingest_endpoint import IngestEndpoint
    from fin.corpus.ingest_storage import _local_ports

    pdf = Path(__file__).resolve().parents[2] / "data/corpus/pdf/2023_Q1_MSFT.pdf"
    if not pdf.exists():  # pragma: no cover - corpus fixture is optional
        pytest.skip("corpus PDF not present")

    catalog_path = Path(tmp_path) / "catalog.json"
    catalog_path.write_text('{"filings": []}', encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "DEFAULT_CATALOG_PATH", catalog_path)

    seen: dict[str, object] = {}

    # Deliberately stricter than the production hook
    # (``workspace_http._record_ingested_filing``), which swallows the
    # failure and answers 200 with a ``catalog_warning``. Letting it raise
    # here means a lost company lands the job on ``failed`` — a red test
    # rather than a silently invisible filing.
    def hook(doc: object) -> None:
        seen.update(
            document_id=doc.document_id,
            company=doc.company,
            period=doc.period,
        )
        catalog_mod.record_filings(
            [
                {
                    "document_id": doc.document_id,
                    "company": doc.company,
                    "period": doc.period,
                    "version": doc.version,
                    "page_count": doc.page_count,
                }
            ]
        )

    class RecordingPort:
        def push(self, envelopes):
            # The push contract counts one block id per CHUNK.
            n = sum(len(e.get("chunks") or []) for e in envelopes)
            return {
                "written": n,
                "duplicates": 0,
                "block_ids": [f"c{i}" for i in range(n)],
                "content_identities": {},
                "records": [],
            }

    ports = _local_ports()
    endpoint = IngestEndpoint(
        push_port=RecordingPort(),
        invalidation_port=type(
            "I", (), {"invalidate": staticmethod(lambda *a, **k: None)}
        )(),
        object_store=ports.objects,
        metadata_store=ports.pointers,
        state_store=ports.states,
        on_ingested=hook,
    )

    # No company / period is supplied anywhere on this path: the worker has
    # only the document id, exactly as in production.
    accepted = endpoint.submit_durable(
        pdf.read_bytes(),
        document_id="MSFT-2023Q1",
        declared_format="pdf",
    )
    outcome = endpoint.run_durable(accepted.document_id)

    assert outcome.state.value == "ready"
    assert seen["document_id"] == "MSFT-2023Q1"
    assert seen["company"] == "MSFT"
    assert seen["period"] == "2023Q1"
    # And the row really landed in the catalog, so the fix is not merely a
    # value passed to a hook that silently drops it.
    assert catalog_mod.list_filings(company="MSFT")
