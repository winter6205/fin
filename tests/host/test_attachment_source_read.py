"""The attachment leg of the bounded ``read_source`` boundary (T4, ADR 0088).

Contract basis: ``specs/thread-attachment-source-read.md`` Boundaries items
(ready-hit locator, host-validated thread ownership, shared
``unknown_locator`` reveal-nothing answer, typed position/locator exits) and
Success Criteria 4/7/9. Hermetic against
:class:`fin.host.attachment_store.InMemoryAttachmentStore`, seeded through
the same doors the preparation tests use (``create`` → ``record_units`` →
``put_index`` → ``mark_ready``).
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
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
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
    store: InMemoryAttachmentStore, thread_id: str, *, text: str = _SHORT_TEXT
) -> tuple[str, tuple[ParsedAttachmentUnit, ...]]:
    attachment_id = f"att-{uuid.uuid4().hex[:8]}"
    store.create(
        thread_id=thread_id,
        attachment_id=attachment_id,
        filename="notes.txt",
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
    store: InMemoryAttachmentStore, thread_id: str, *, text: str = _SHORT_TEXT
) -> str:
    attachment_id, units = _create(store, thread_id, text=text)
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
