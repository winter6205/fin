"""P1 T3: real-process batch interruption, resume, and named outage."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest

from fin.retrieve.push_batching import (
    PushJournal,
    expected_block_total,
    plan_batches,
    push_in_batches,
)
from fin.retrieve.push_port import PushError, PushServiceUnavailableError

from live_iknow_support import IKNOW_WORKTREE_ROOT, child_records, live_iknow


pytestmark = pytest.mark.mcp


class _InterruptingPushPort:
    """Fail one batch before forwarding it to the real port."""

    def __init__(self, inner: Any, *, fail_on_call: int) -> None:
        self.inner = inner
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.forwarded: list[tuple[str, ...]] = []

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise PushServiceUnavailableError(
                "simulated interruption before batch forwarding"
            )
        # The wire is the one-envelope-per-record shape
        # (``PushRecord.to_wire()`` after T1 carries the ``chunks[0].id``
        # as the per-child block id); the per-record id is therefore
        # under ``chunks[0]["id"]`` rather than a top-level ``block_id``
        # field.
        self.forwarded.append(
            tuple(str(record["chunks"][0]["id"]) for record in records)
        )
        return self.inner.push(records)


def test_real_iknow_resume_skips_landed_batches_and_outage_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real process resumes the unfinished child batches exactly once."""

    # The envelope-based wire matches the operator-console worktree
    # schema; the main-checkout ``push`` schema predates T1 and rejects
    # the ``chunks`` field. Run against the worktree build.
    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        records = child_records()
        journal = PushJournal()
        expected = expected_block_total(records)
        plans = plan_batches(records, batch_size=1)
        interrupted = _InterruptingPushPort(live.push, fail_on_call=2)

        with pytest.raises(PushServiceUnavailableError) as failure:
            push_in_batches(
                interrupted,
                records,
                journal=journal,
                batch_size=1,
            )

        assert failure.value.code == "push_service_unavailable"
        assert interrupted.forwarded == [
            (plans[0].records[0].chunks[0].id,),
        ]
        assert journal.completed_keys() == (plans[0].key,)
        assert len(journal.failures()) == 1

        resumed = _InterruptingPushPort(live.push, fail_on_call=0)
        outcome = push_in_batches(
            resumed,
            records,
            journal=journal,
            batch_size=1,
        )

        assert outcome.batches_skipped == 1
        assert outcome.batches_pushed == len(plans) - 1
        assert resumed.forwarded
        assert plans[0].records[0].chunks[0].id not in {
            block_id for batch in resumed.forwarded for block_id in batch
        }
        assert journal.failures() == ()

        retrieved = live.retrieve.retrieve(
            "operating revenue",
            limit=expected + 1,
        )
        assert "error" not in retrieved, retrieved
        assert len(retrieved["chunks"]) == expected

        live.session.kill()
        with pytest.raises(PushServiceUnavailableError) as outage:
            live.push.push([records[0].to_wire()])
        assert outage.value.code == "push_service_unavailable"
