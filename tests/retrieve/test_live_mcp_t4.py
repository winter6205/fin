"""P1 T4: real-process content invalidation is idempotent."""

from __future__ import annotations

from typing import Mapping

import pytest

from fin.corpus.invalidation import (
    REASON_UNIT_VERSION_CHANGED,
    InvalidationTarget,
)

from live_iknow_support import child_records, live_iknow


pytestmark = pytest.mark.mcp


def _target_of(record) -> InvalidationTarget:
    locator = record.metadata["locator"]
    assert isinstance(locator, Mapping)
    return InvalidationTarget(
        source_ref=record.source_ref,
        content_id=record.content_id,
        block_id=record.block_id,
        document_id=str(locator["document_id"]),
        version=str(locator["version"]),
        unit=int(locator["unit"]),
        reason=REASON_UNIT_VERSION_CHANGED,
    )


def test_real_iknow_invalidation_is_idempotent_and_hides_old_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeating one old-unit invalidation neither errors nor reopens hits."""

    with live_iknow(monkeypatch) as live:
        records = child_records()
        live.push.push([record.to_wire() for record in records])
        target = _target_of(records[0])

        before = live.retrieve.retrieve(
            "operating revenue",
            limit=len(records) + 1,
        )
        assert len(before["chunks"]) == len(records)

        first = live.invalidation.invalidate([target])
        second = live.invalidation.invalidate([target])

        assert first.closed == len(records)
        assert first.already_closed == 0
        assert second.closed == 0
        assert second.already_closed == len(records)
        assert second.closed_at == first.closed_at

        after = live.retrieve.retrieve(
            "operating revenue",
            limit=len(records) + 1,
        )
        assert "error" not in after, after
        assert after["chunks"] == []
