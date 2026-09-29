"""T4 live acceptance: fin's bounded read legs against real iknow-memory.

Drives the shipped ``MCPRetrieveAdapter.read_slice`` leg and the
model-facing ``read_source`` tool (not raw tools/call frames) against the
real stdio server whose ``read_source`` caps each slice at 8,000 code
points / 32 KiB — the exact condition the removed whole-body fetch had to
grow a cursor walk for. Concatenation, cursor advance, and typed-error
mapping are asserted on observable wire behavior (spec
``thread-attachment-source-read`` acceptance 7/8/9).
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit
from fin.host.tools.read_source import read_source
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    UNKNOWN_LOCATOR,
    is_slice_result,
    serialized_result_bytes,
)

from live_iknow_support import IKNOW_ROOT, live_iknow

pytestmark = pytest.mark.mcp

_DOCUMENT_ID = "T4-LIVE-BOUNDED-READ"


def _unit_text() -> str:
    """One >16,000-code-point unit with astral characters at the seams.

    The 8,000-code-point cap is what the live server slices on, so two
    full slices plus a tail exercise advance-and-terminate; the rocket
    emoji sits exactly at slice boundaries 8,000 and 16,000 to prove the
    wire never splits a surrogate pair.
    """
    seg = "Revenue line item \U0001f680 and CJK 收入 for unit text. "
    text = seg * ((2 * SOURCE_READ_MAX_TEXT_CODE_POINTS) // len(seg) + 2)
    marker = len(text) - (len(text) % SOURCE_READ_MAX_TEXT_CODE_POINTS)
    return text[:marker] + "\U0001f680" + text[marker + 1 :]


_VERSION = hashlib.sha256(_DOCUMENT_ID.encode("utf-8")).hexdigest()
_TRIPLE = {"document_id": _DOCUMENT_ID, "version": _VERSION, "unit": 1}


@pytest.fixture(scope="module")
def live() -> Iterator[Any]:
    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} "
            "to exercise the bounded read legs"
        )
    monkeypatch = pytest.MonkeyPatch()
    try:
        with live_iknow(monkeypatch) as session:
            yield session
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def pushed(live: Any) -> str:
    """Push one real long frozen unit; return its full text."""
    text = _unit_text()
    parsed = build_unit(
        fmt="txt",
        source_sha256=_VERSION,
        document_id=_DOCUMENT_ID,
        version=_VERSION,
        unit=1,
        text=text,
    )
    split = split_unit(parsed, doc_type="narrative")
    envelope = envelope_of_unit(
        split,
        valid_from="2026-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.59Z",
    )
    pushed = live.push.push([envelope.to_wire()])
    # ``written`` counts search blocks; the body is stored per source_ref
    # whichever way 切分 chunked it. The frozen body — not the raw input
    # — is ground truth: freeze normalization fixes text to a fixed point.
    assert pushed["written"] >= 1, pushed
    return envelope.body


def test_live_read_slice_cursor_walk_reproduces_the_unit_exactly(
    live: Any, pushed: str
) -> None:
    collected = ""
    position = 0
    slices = 0
    while True:
        result = live.retrieve.read_slice(_TRIPLE, position)
        assert is_slice_result(result), result
        assert len(result["text"]) <= SOURCE_READ_MAX_TEXT_CODE_POINTS
        assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
        collected += result["text"]
        if result["next_position"] is None:
            break
        assert result["next_position"] > position
        position = result["next_position"]
        slices += 1
        assert slices < 10
    assert slices >= 2  # the live server really did bound each response
    assert collected == pushed
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in collected)


def test_live_read_source_tool_walks_the_cursor_to_the_full_body(
    live: Any, pushed: str
) -> None:
    """The model-facing seam that replaced the removed whole-page ``read``
    assembly: one ``read_source`` tool call answers one bounded slice with
    the request identity echoed, and following its cursor reaches the
    complete frozen unit."""
    collected = ""
    position = 0
    calls = 0
    while True:
        result = read_source(_TRIPLE, position, company_reader=live.retrieve)
        assert is_slice_result(result), result
        assert result["source_ref"] == _TRIPLE
        assert len(result["text"]) <= SOURCE_READ_MAX_TEXT_CODE_POINTS
        collected += result["text"]
        if result["next_position"] is None:
            break
        assert result["next_position"] > position
        position = result["next_position"]
        calls += 1
        assert calls < 10
    assert collected == pushed


def test_live_wire_failures_map_to_fin_typed_codes(
    live: Any, pushed: str
) -> None:
    absent = live.retrieve.read_slice(
        {"document_id": "T4-LIVE-ABSENT", "version": _VERSION, "unit": 1}
    )
    assert absent == {"error": UNKNOWN_LOCATOR}

    beyond = live.retrieve.read_slice(_TRIPLE, len(pushed) + 5)
    assert beyond.get("error") == INVALID_POSITION, beyond
    assert "text" not in beyond

    malformed = live.retrieve.read_slice({**_TRIPLE, "unit": 0})
    assert malformed.get("error") == INVALID_LOCATOR, malformed
