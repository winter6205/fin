"""The attachment leg of the bounded ``read_source`` boundary (T4, ADR 0088).

Contract basis: ``specs/thread-attachment-source-read.md`` Boundaries items
(ready-hit locator, host-validated thread ownership, shared
``unknown_locator`` reveal-nothing answer, typed position/locator exits) and
Success Criteria 4/7/9. Hermetic against
:class:`fin.host.attachment_store.InMemoryAttachmentStore`, seeded through
the same doors the preparation tests use (``create`` → ``record_units`` →
``put_index`` → ``mark_ready``).

The same store seeds the module's two citation identity faces,
``owned_record`` and ``identity_is_current`` — the faces the host graph asks
before it grants a citation its filename, its presentation, or its currency.
Their fail-closed answers are pinned here against a real record rather than
a hand-rolled reader, because a citation identity granted for a foreign,
re-prepared, or tombstoned row is the failure those faces exist to prevent.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from fin.host.attachment_source_read import (
    ATTACHMENT_KIND,
    AttachmentLocatorError,
    AttachmentSourceLocator,
    AttachmentSourceReader,
    parse_attachment_source_locator,
)
from fin.host.attachment_index import build_postings
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.graph import _display_lookup_for
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_READ_MAX_LOCATOR_CODE_POINTS,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    SourceReader,
    is_slice_result,
    serialized_result_bytes,
)

# Representative shapes: CJK, ASCII, and a supplementary-plane character
# (the spec's "include supplementary-plane Unicode" requirement lives at
# the slice boundary, not only in the shared-contract file).
_SHORT_TEXT = "Q3 营业收入 89,493 百万美元 😀 end."
_LONG_TAIL = 12


def _long_unit_text() -> str:
    """16,012 code points with astral characters AT the slice ends.

    Indices 7999 and 15999 are ``😀``: exactly where the 8000-code-point
    default slices terminate, so a continuation walk that split a
    surrogate pair would show up as a failed reconstruction.
    """
    return (
        "a" * 7999
        + "😀"
        + "a" * 7999
        + "😀"
        + "b" * _LONG_TAIL
    )


def _threads_and_store() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str, str]:
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_a = threads.create_thread(title="Thread A").id
    thread_b = threads.create_thread(title="Thread B").id
    return threads, store, thread_a, thread_b


def _create(
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    text: str = _SHORT_TEXT,
    filename: str = "notes.txt",
) -> tuple[str, tuple[ParsedAttachmentUnit, ...]]:
    attachment_id = f"att-{uuid.uuid4().hex[:8]}"
    store.create(
        thread_id=thread_id,
        attachment_id=attachment_id,
        filename=filename,
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{attachment_id}",
    )
    units = (ParsedAttachmentUnit(unit=1, text=text),)
    return attachment_id, units


def _prepare(
    store: InMemoryAttachmentStore,
    attachment_id: str,
    units: tuple[ParsedAttachmentUnit, ...],
    *,
    version: int = 1,
) -> None:
    """Write both derived layers and publish ``version`` as ready."""
    store.record_units(
        attachment_id=attachment_id, version=version, units=units
    )
    store.put_index(
        attachment_id=attachment_id, version=version, postings=build_postings(units)
    )
    store.mark_ready(
        attachment_id=attachment_id, version=version, unit_count=len(units)
    )


def _ready(
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    text: str = _SHORT_TEXT,
    filename: str = "notes.txt",
) -> str:
    attachment_id, units = _create(store, thread_id, text=text, filename=filename)
    _prepare(store, attachment_id, units)
    return attachment_id


def _ref(attachment_id: str, version: int = 1, unit: int = 1) -> dict[str, Any]:
    """The wire mapping a ready hit carries (canonical string version)."""
    return {
        "kind": ATTACHMENT_KIND,
        "attachment_id": attachment_id,
        "version": str(version),
        "unit": unit,
    }


# ----- locator parse / serialize round trip -----


def test_the_locator_serializes_canonically_with_a_string_version() -> None:
    locator = AttachmentSourceLocator(attachment_id="att-1", version=3, unit=2)
    assert locator.serialize() == (
        '{"attachment_id":"att-1","kind":"attachment","unit":2,"version":"3"}'
    )
    # WHY the literal: sorted keys and compact separators are the one
    # spelling; a drift here forks locator identity across the legs.


def test_serialize_parse_round_trips() -> None:
    for version, unit in ((0, 1), (1, 2), (17, 399)):
        locator = AttachmentSourceLocator(
            attachment_id="att-roundtrip", version=version, unit=unit
        )
        parsed = parse_attachment_source_locator(
            json.loads(locator.serialize())
        )
        assert parsed == locator
        assert parsed.serialize() == locator.serialize()


def test_the_locator_accepts_the_host_int_version_spelling_too() -> None:
    locator = parse_attachment_source_locator(
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": 2, "unit": 5}
    )
    assert locator == AttachmentSourceLocator("att-1", 2, 5)


# ----- happy path -----


def test_a_ready_unit_reads_one_slice_to_the_end() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    ref = _ref(attachment_id)
    result = reader.read_slice(ref)
    assert result == {
        "source_ref": ref,
        "text": _SHORT_TEXT,
        "next_position": None,
    }
    assert is_slice_result(result)


def test_the_reader_satisfies_the_shared_source_reader_protocol() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    assert isinstance(AttachmentSourceReader(store, thread_a), SourceReader)


def test_the_terminal_position_is_a_legal_empty_slice() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    result = reader.read_slice(_ref(attachment_id), position=len(_SHORT_TEXT))
    assert result["text"] == ""
    assert result["next_position"] is None


def test_a_long_unit_continues_to_an_exact_reconstruction() -> None:
    text = _long_unit_text()
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a, text=text)
    reader = AttachmentSourceReader(store, thread_a)
    ref = _ref(attachment_id)

    collected: list[str] = []
    positions: list[int] = []
    position = 0
    while True:
        result = reader.read_slice(ref, position=position)
        assert is_slice_result(result), result
        chunk = result["text"]
        assert len(chunk) <= SOURCE_READ_MAX_TEXT_CODE_POINTS
        assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
        collected.append(chunk)
        positions.append(position)
        next_position = result["next_position"]
        if next_position is None:
            break
        # WHY strict advance: the citation gate keys contiguity on it.
        assert next_position > position
        position = next_position

    assert "".join(collected) == text
    assert len(collected) == 3  # 8000 + 8000 + the 12-code-point tail
    # The astral characters sit exactly at slice ends and survived whole.
    assert collected[0].endswith("😀")
    assert collected[1].endswith("😀")
    assert positions == [0, 8000, 16000]


# ----- unknown_locator: the reveal-nothing identity answers -----


def test_a_cross_thread_locator_reveals_no_text() -> None:
    _threads, store, thread_a, thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_b)
    reader = AttachmentSourceReader(store, thread_a)
    result = reader.read_slice(_ref(attachment_id))
    assert result == {"error": UNKNOWN_LOCATOR}
    assert "text" not in json.dumps(result)


def test_a_stale_parsed_version_reveals_no_text() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id, units = _create(store, thread_a)
    _prepare(store, attachment_id, units, version=1)
    reprepared = (
        ParsedAttachmentUnit(unit=1, text="revised text"),
        ParsedAttachmentUnit(unit=2, text="second page"),
    )
    _prepare(store, attachment_id, reprepared, version=2)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.read_slice(_ref(attachment_id, version=1)) == {
        "error": UNKNOWN_LOCATOR
    }
    # The current version still reads.
    assert reader.read_slice(_ref(attachment_id, version=2))["text"] == "revised text"


def test_an_attachment_deleted_after_preparation_reveals_no_text() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert is_slice_result(reader.read_slice(_ref(attachment_id)))
    store.tombstone(attachment_id=attachment_id, thread_id=thread_a)
    # EXIT path: the row is gone; the read answers the shared miss code.
    assert reader.read_slice(_ref(attachment_id)) == {"error": UNKNOWN_LOCATOR}


def test_an_attachment_whose_thread_was_deleted_reveals_no_text() -> None:
    threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    threads.delete_thread(thread_a)
    assert reader.read_slice(_ref(attachment_id)) == {"error": UNKNOWN_LOCATOR}


def test_an_unknown_attachment_id_reveals_no_text() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.read_slice(_ref("att-never-created")) == {"error": UNKNOWN_LOCATOR}


def test_a_unit_ordinal_out_of_range_reveals_no_text() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.read_slice(_ref(attachment_id, unit=2)) == {"error": UNKNOWN_LOCATOR}
    assert reader.read_slice(_ref(attachment_id, unit=99)) == {"error": UNKNOWN_LOCATOR}


# ----- ready-state gating: stored bytes are not citable text -----


def test_an_uploaded_but_unprepared_attachment_has_nothing_to_read() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id, _units = _create(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.read_slice(_ref(attachment_id)) == {"error": UNKNOWN_LOCATOR}


def test_a_ready_row_whose_index_was_cleared_is_not_searchable() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    store.clear_index(attachment_id)
    reader = AttachmentSourceReader(store, thread_a)
    # WHY the unit rows are still on disk but unreadable: ``ready`` plus a
    # usable index is the searchable promise the spec gates reads on.
    assert reader.read_slice(_ref(attachment_id)) == {"error": UNKNOWN_LOCATOR}


# ----- invalid_locator: malformed or oversized model input -----


def test_a_non_attachment_kind_is_refused_by_this_leg() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    company_ref = {"kind": "company", "document_id": "doc-a", "version": "3", "unit": 1}
    result = reader.read_slice(company_ref)
    assert result == {"error": INVALID_LOCATOR, "reason": "locator_kind"}
    assert "text" not in json.dumps(result)


@pytest.mark.parametrize(
    "source_ref",
    [
        None,
        "attachment:string",
        ["kind", "attachment"],
        {"attachment_id": "att-1", "version": "1", "unit": 1},  # missing kind
        {"kind": ATTACHMENT_KIND, "version": "1", "unit": 1},  # missing attachment_id
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "unit": 1},  # missing version
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1"},  # missing unit
        {  # EXIT-relevant: a model-supplied thread override is not a locator key
            "kind": ATTACHMENT_KIND,
            "attachment_id": "att-1",
            "version": "1",
            "unit": 1,
            "thread_id": "thread-x",
        },
        {"kind": ATTACHMENT_KIND, "attachment_id": "", "version": "1", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "   ", "version": "1", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": 123, "version": "1", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1", "unit": True},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1", "unit": 0},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1", "unit": -3},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1", "unit": "2"},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": True, "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": -1, "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "01", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "+3", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": "1.5", "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": 1.5, "unit": 1},
        {"kind": ATTACHMENT_KIND, "attachment_id": "att-1", "version": None, "unit": 1},
        {"kind": "Attachment", "attachment_id": "att-1", "version": "1", "unit": 1},
    ],
    ids=[
        "none", "string", "list", "missing_kind", "missing_attachment_id",
        "missing_version", "missing_unit", "thread_override", "empty_id",
        "blank_id", "int_id", "bool_unit", "zero_unit", "negative_unit",
        "string_unit", "bool_version", "negative_int_version", "empty_version",
        "leading_zero_version", "plus_version", "decimal_string_version",
        "float_version", "null_version", "capitalized_kind",
    ],
)
def test_malformed_locator_shapes_are_invalid_locator(source_ref: Any) -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    result = reader.read_slice(source_ref)
    assert result["error"] == INVALID_LOCATOR
    assert "text" not in json.dumps(result)


def test_an_oversized_locator_identity_is_invalid_locator() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    huge = "x" * (SOURCE_READ_MAX_LOCATOR_CODE_POINTS + 1)
    result = reader.read_slice(_ref(huge))
    assert result == {"error": INVALID_LOCATOR, "reason": "locator_identity_oversized"}


def test_a_locator_at_the_identity_cap_parses() -> None:
    base_len = len(
        AttachmentSourceLocator("x", 0, 1).serialize()
    ) - 1  # serialization of a one-character id
    padding = SOURCE_READ_MAX_LOCATOR_CODE_POINTS - base_len
    locator = parse_attachment_source_locator(
        _ref("x" * max(1, padding))
    )
    assert len(locator.serialize()) <= SOURCE_READ_MAX_LOCATOR_CODE_POINTS


# ----- invalid_position: typed by the shared slice boundary -----


@pytest.mark.parametrize(
    "position",
    [-1, 10**9, len(_SHORT_TEXT) + 1, True, 1.5, "0", None],
    ids=["negative", "far_beyond", "just_past_end", "bool", "float", "string", "none"],
)
def test_invalid_position_classes_are_typed(position: Any) -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    result = reader.read_slice(_ref(attachment_id), position=position)
    assert result["error"] == INVALID_POSITION
    assert "text" not in result


# ----- storage faults never reach the model as text or exceptions -----


class _FaultyStore:
    """Stand-in whose store doors raise the way a live backend would.

    Only ``get``/``units`` are reached by the reader; the full
    :class:`AttachmentStore` surface is passed to the constructor as
    ``Any`` because the fault is exactly what the two read doors do.
    """

    def __init__(self, fault: Exception) -> None:
        self._fault = fault

    def get(self, attachment_id: str) -> Any:
        raise self._fault

    def units(self, attachment_id: str, version: int) -> Any:
        raise self._fault


def test_a_genuine_store_fault_is_source_unavailable() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    faulty: Any = _FaultyStore(AttachmentStoreError("disk down"))
    reader = AttachmentSourceReader(faulty, thread_a)
    assert reader.read_slice(_ref(attachment_id)) == {"error": SOURCE_UNAVAILABLE}


def test_a_gone_race_surfaces_as_unknown_locator() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    gone: Any = _FaultyStore(AttachmentThreadGoneError("attachment is gone"))
    reader = AttachmentSourceReader(gone, thread_a)
    assert reader.read_slice(_ref(attachment_id)) == {"error": UNKNOWN_LOCATOR}


# ----- trust face: ``owned_record`` (the citation identity grant) -----


def test_owned_record_answers_the_thread_s_own_ready_row() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    record = reader.owned_record(attachment_id)
    # WHY the store's own record, not a projection: the graph reads the
    # filename and version straight off what the face returned, so the
    # trust is exactly "the row this thread owns and can read".
    assert isinstance(record, AttachmentRecord)
    assert record.attachment_id == attachment_id
    assert record.thread_id == thread_a
    assert record.filename == "notes.txt"
    assert record.version == 1
    assert record.status == "ready" and record.searchable


def test_owned_record_refuses_another_thread_s_ready_row() -> None:
    _threads, store, thread_a, thread_b = _threads_and_store()
    foreign = _ready(store, thread_b)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.owned_record(foreign) is None


def test_owned_record_refuses_an_attachment_that_was_never_created() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.owned_record("att-never-created") is None


def test_owned_record_refuses_a_row_that_is_ready_no_more() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    store.clear_index(attachment_id)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.owned_record(attachment_id) is None
    # A re-check of the same row that lost its index cannot grant currency
    # either: the trust faces agree with the read door.
    assert reader.identity_is_current(attachment_id, 1) is False


def test_owned_record_refuses_an_uploaded_row_that_was_never_prepared() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id, _units = _create(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.owned_record(attachment_id) is None


def test_owned_record_refuses_a_tombstoned_row_and_a_row_whose_thread_is_gone() -> None:
    threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    deleted = _ready(store, thread_a)
    store.tombstone(attachment_id=deleted, thread_id=thread_a)
    assert reader.owned_record(deleted) is None
    thread_c = threads.create_thread(title="Thread C").id
    doomed = _ready(store, thread_c)
    reader_c = AttachmentSourceReader(store, thread_c)
    threads.delete_thread(thread_c)
    assert reader_c.owned_record(doomed) is None


def test_a_reprepared_row_grants_identity_only_at_the_version_the_store_now_holds() -> None:
    """Both halves of the version guard, against the real record.

    ``owned_record`` is deliberately version-blind — it attests that the
    row belongs to this thread and is readable, and nothing more. The
    mismatch a citation must not survive is caught one layer up, by the
    lookup comparing the hit's version with the record's, so the pin has
    to show both: what the face answers, and what the graph does with it.
    """
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id, _units = _create(store, thread_a)
    _prepare(store, attachment_id, (ParsedAttachmentUnit(unit=1, text=_SHORT_TEXT),))
    reader = AttachmentSourceReader(store, thread_a)
    lookup = _display_lookup_for(reader)
    assert lookup is not None
    assert lookup(attachment_id, 1) == ("notes.txt", "unit")

    _prepare(
        store,
        attachment_id,
        (ParsedAttachmentUnit(unit=1, text="revised text"),),
        version=2,
    )
    stale = reader.owned_record(attachment_id)
    assert stale is not None and stale.version == 2
    assert lookup(attachment_id, 1) is None  # the guard rejects the old version
    assert lookup(attachment_id, 2) == ("notes.txt", "unit")
    assert reader.identity_is_current(attachment_id, 1) is False
    assert reader.identity_is_current(attachment_id, 2) is True


@pytest.mark.parametrize(
    "fault",
    [
        AttachmentStoreError("disk down"),
        AttachmentThreadGoneError("attachment is gone"),
        OSError("transport closed"),
    ],
    ids=["store_error", "thread_gone", "unexpected_os_error"],
)
def test_the_identity_faces_fail_closed_on_a_store_fault_and_never_raise(
    fault: Exception,
) -> None:
    """A store that cannot answer grants nothing.

    Neither face may propagate: the citation path has no error channel of
    its own, so a raising identity check would surface in the answer path
    instead of refusing the citation.
    """
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    faulty: Any = _FaultyStore(fault)
    reader = AttachmentSourceReader(faulty, thread_a)
    assert reader.owned_record(attachment_id) is None
    assert reader.identity_is_current(attachment_id, 1) is False
    assert _display_lookup_for(reader)(attachment_id, 1) is None


# ----- trust face: ``identity_is_current`` (the citation gate re-check) -----


def test_identity_is_current_holds_only_while_the_stored_version_matches() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.identity_is_current(attachment_id, 1) is True
    _prepare(
        store,
        attachment_id,
        (ParsedAttachmentUnit(unit=1, text="revised text"),),
        version=2,
    )
    assert reader.identity_is_current(attachment_id, 1) is False
    assert reader.identity_is_current(attachment_id, 2) is True


def test_identity_is_current_is_false_for_another_thread_s_row() -> None:
    _threads, store, thread_a, thread_b = _threads_and_store()
    foreign = _ready(store, thread_b)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.identity_is_current(foreign, 1) is False
    # The owning thread still attests it: the refusal is ownership, not
    # the row's shape.
    assert AttachmentSourceReader(store, thread_b).identity_is_current(foreign, 1) is True


def test_identity_is_current_is_false_once_the_row_is_tombstoned() -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.identity_is_current(attachment_id, 1) is True
    store.tombstone(attachment_id=attachment_id, thread_id=thread_a)
    assert reader.identity_is_current(attachment_id, 1) is False


def test_identity_is_current_is_false_without_a_version_to_compare() -> None:
    """No committed version means nothing to attest currency against.

    The graph hands this face whatever the evidence item carried, so an
    unparseable locator version arrives as ``None`` and must not be
    treated as "matches whatever is stored".
    """
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a)
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.identity_is_current(attachment_id, None) is False
    # The record IS owned and readable; the missing version is still a
    # refusal, not a pass.
    assert reader.owned_record(attachment_id) is not None


@pytest.mark.parametrize(
    "attachment_id, version_int",
    [("att-never-created", 1), ("att-never-created", None)],
    ids=["unknown_id", "unknown_id_no_version"],
)
def test_identity_is_current_is_false_for_an_attachment_the_store_does_not_have(
    attachment_id: str, version_int: Any
) -> None:
    _threads, store, thread_a, _thread_b = _threads_and_store()
    reader = AttachmentSourceReader(store, thread_a)
    assert reader.identity_is_current(attachment_id, version_int) is False


# ----- the two faces agree over every row shape the store can hold -----


def test_the_grant_passes_over_every_row_shape_the_store_can_hold() -> None:
    """One store, one scan of the states a row can actually be in.

    The refusals stay indistinguishable — a foreign id, a tombstoned id,
    and an id the store never had all answer the same ``None`` / ``False``
    pair — so neither face can be probed to learn which of the three a
    thread holds.
    """
    _threads, store, thread_a, thread_b = _threads_and_store()
    foreign = _ready(store, thread_b)
    tombstoned = _ready(store, thread_a)
    store.tombstone(attachment_id=tombstoned, thread_id=thread_a)
    unindexed = _ready(store, thread_a)
    store.clear_index(unindexed)
    never_prepared = _create(store, thread_a)[0]
    states = {
        "own": _ready(store, thread_a),
        "foreign": foreign,
        "tombstoned": tombstoned,
        "unindexed": unindexed,
        "never_prepared": never_prepared,
        "unknown": "att-never-created",
    }
    reader = AttachmentSourceReader(store, thread_a)
    answers = {
        name: (
            reader.owned_record(attachment_id) is not None,
            reader.identity_is_current(attachment_id, 1),
        )
        for name, attachment_id in states.items()
    }
    assert answers == {
        "own": (True, True),
        "foreign": (False, False),
        "tombstoned": (False, False),
        "unindexed": (False, False),
        "never_prepared": (False, False),
        "unknown": (False, False),
    }


# ----- the display pair comes from the STORED filename -----


@pytest.mark.parametrize(
    "filename,display",
    [
        ("quarter-2023.pdf", "page"),
        ("QUARTER-2023.PDF", "page"),
        ("notes.txt", "unit"),
        ("memo.md", "unit"),
        ("ledger", "unit"),
    ],
    ids=["pdf_lower", "pdf_upper", "txt", "md", "no_suffix"],
)
def test_the_granted_presentation_follows_the_stored_filename(filename: str, display: str) -> None:
    """A PDF unit is cited as a page and every other kind as a unit.

    The presentation is read off the record the store holds, so a model
    cannot choose how its citation is rendered by naming a suffix in the
    locator, in the tool arguments, or in its own answer.
    """
    _threads, store, thread_a, _thread_b = _threads_and_store()
    attachment_id = _ready(store, thread_a, filename=filename)
    reader = AttachmentSourceReader(store, thread_a)
    lookup = _display_lookup_for(reader)
    assert lookup is not None
    assert lookup(attachment_id, 1) == (filename, display)
