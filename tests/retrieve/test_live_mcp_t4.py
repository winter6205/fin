"""P1 T4: real-process content invalidation is idempotent."""

from __future__ import annotations

from typing import Mapping

import pytest

from fin.corpus.invalidation import (
    REASON_UNIT_VERSION_CHANGED,
    InvalidationTarget,
)

from live_iknow_support import IKNOW_WORKTREE_ROOT, child_records, live_iknow


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

    # The envelope-based wire matches the operator-console worktree
    # schema; the main-checkout ``push`` schema predates T1 and rejects
    # the ``chunks`` field. Run against the worktree build.
    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        records = child_records()
        live.push.push([record.to_wire() for record in records])
        # The live fixture now models the T1 contract: one record per
        # distinct frozen unit (4 units → 4 records, each with its
        # own ``source_ref``). Invalidation fans out per
        # ``source_ref`` on the receiving side, so closing the whole
        # document requires one target per record — invalidating only
        # ``records[0]`` would close that one unit and leave the
        # other three visible to retrieve, breaking the
        # ``after.chunks == []`` assertion that ends the test.
        targets = [_target_of(record) for record in records]

        before = live.retrieve.retrieve(
            "operating revenue",
            limit=len(records) + 1,
        )
        assert len(before["chunks"]) == len(records)

        first = live.invalidation.invalidate(targets)
        second = live.invalidation.invalidate(targets)

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
