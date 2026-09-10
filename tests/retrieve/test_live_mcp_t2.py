"""P1 T2: real iknow stdio push/retrieve and idempotency acceptance."""

from __future__ import annotations

import pytest

from live_iknow_support import (
    IKNOW_WORKTREE_ROOT,
    child_records,
    live_iknow,
)


pytestmark = pytest.mark.mcp


def test_real_iknow_round_trip_is_byte_identical_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real process stores fin child text verbatim and replays ids."""

    # The envelope-based wire matches the operator-console worktree
    # schema; the main-checkout ``push`` schema predates T1 and rejects
    # the ``chunks`` field. Run against the worktree build.
    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        records = child_records()
        wire_records = [record.to_wire() for record in records]

        first = live.push.push(wire_records)
        assert first["written"] == len(records)
        assert first["duplicates"] == 0

        retrieved = live.retrieve.retrieve(
            "operating revenue",
            limit=len(records) + 1,
        )
        assert "error" not in retrieved, retrieved
        retrieved_texts = [chunk.text for chunk in retrieved["chunks"]]
        assert retrieved_texts == [record.text for record in records]

        second = live.push.push(wire_records)
        third = live.push.push(wire_records)
        assert second["written"] == 0
        assert third["written"] == 0
        assert second["duplicates"] == len(records)
        assert third["duplicates"] == len(records)
        assert second["block_ids"] == first["block_ids"]
        assert third["block_ids"] == first["block_ids"]

        after_replays = live.retrieve.retrieve(
            "operating revenue",
            limit=len(records) + 1,
        )
        assert len(after_replays["chunks"]) == len(records)
