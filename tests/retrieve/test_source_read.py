"""Contract tests for the shared bounded source-read slice (T4, ADR 0088)."""

from __future__ import annotations

import json

from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_READ_MAX_LOCATOR_CODE_POINTS,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    bounded_slice,
    is_slice_result,
    serialized_result_bytes,
)

_REF = {"document_id": "doc-a", "version": "3", "unit": 1}


def test_the_bounded_read_limits_are_the_pinned_policy_values() -> None:
    """The bound's VALUE is the product promise the model can plan
    against, so it is pinned through the public symbols rather than only
    exercised: 8,000 code points of text, a 32 KiB serialized result, a
    locator allowed 1,024 code points of its own."""
    assert SOURCE_READ_MAX_TEXT_CODE_POINTS == 8000
    assert SOURCE_READ_MAX_RESULT_BYTES == 32768
    assert SOURCE_READ_MAX_LOCATOR_CODE_POINTS == 1024


def test_a_short_unit_reads_one_slice_to_the_end() -> None:
    result = bounded_slice(_REF, "hello world", 0)
    assert result == {
        "source_ref": _REF,
        "text": "hello world",
        "next_position": None,
    }


def test_an_overlong_unit_slices_at_the_code_point_cap() -> None:
    text = "a" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 7)
    result = bounded_slice(_REF, text, 0)
    assert len(result["text"]) == SOURCE_READ_MAX_TEXT_CODE_POINTS
    assert result["next_position"] == SOURCE_READ_MAX_TEXT_CODE_POINTS
    follow_up = bounded_slice(_REF, text, result["next_position"])
    assert follow_up["text"] == text[SOURCE_READ_MAX_TEXT_CODE_POINTS:]
    assert follow_up["next_position"] is None
    assert result["text"] + follow_up["text"] == text


def test_the_terminal_position_is_a_legal_empty_slice() -> None:
    result = bounded_slice(_REF, "abc", 3)
    assert result["text"] == ""
    assert result["next_position"] is None


def test_beyond_end_and_malformed_positions_are_typed() -> None:
    assert bounded_slice(_REF, "abc", 4) == {
        "error": INVALID_POSITION,
        "position": 4,
        "code_points": 3,
    }
    assert bounded_slice(_REF, "abc", -1)["error"] == INVALID_POSITION
    assert bounded_slice(_REF, "abc", True) == {
        "error": INVALID_POSITION,
        "reason": "position_not_an_integer",
    }
    assert bounded_slice(_REF, "abc", "0")["error"] == INVALID_POSITION


def test_supplementary_plane_characters_never_split_at_boundaries() -> None:
    # The cap boundary lands inside an astral run: each emoji is one
    # code point, so a UTF-16-style cut would produce a lone surrogate.
    text = "\U0001f600" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 2)
    first = bounded_slice(_REF, text, 0)
    assert len(first["text"]) == SOURCE_READ_MAX_TEXT_CODE_POINTS
    assert not any(
        0xD800 <= ord(ch) <= 0xDFFF for ch in first["text"]
    )
    second = bounded_slice(_REF, text, first["next_position"])
    assert first["text"] + second["text"] == text


def test_escaping_heavy_text_shrinks_to_the_byte_cap() -> None:
    # 8000 quotation marks serialize to 16000 escaped bytes plus the
    # envelope; 8000 4-byte emoji serialize to 32000 bytes plus the
    # envelope — both must exceed 32 KiB only through encoding, and the
    # returned slice must fit while concatenation still reproduces text.
    for text in ('"' * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 100),
                 "\U0001f4a9" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 100)):
        collected = ""
        position = 0
        guard = 0
        while True:
            result = bounded_slice(_REF, text, position)
            assert is_slice_result(result)
            assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
            collected += result["text"]
            if result["next_position"] is None:
                break
            assert result["next_position"] > position
            position = result["next_position"]
            guard += 1
            assert guard < 100
        assert collected == text


def test_an_oversized_locator_envelope_fails_typed_not_empty_success() -> None:
    fat_ref = {"document_id": "x" * 40000, "version": "1", "unit": 1}
    result = bounded_slice(fat_ref, "some body text", 0)
    assert result == {
        "error": INVALID_LOCATOR,
        "reason": "locator_metadata_oversized",
    }


def test_slice_result_shape_and_serialization_are_canonical() -> None:
    result = bounded_slice(_REF, "abc", 0)
    assert is_slice_result(result)
    assert not is_slice_result({"error": INVALID_POSITION})
    assert json.dumps(result, sort_keys=True) == json.dumps(
        result, sort_keys=True
    )
