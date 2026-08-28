"""T8 thumbs feedback contract tests.

The workspace HTTP route is intentionally not wired in this ticket.  These
tests pin the persistence seam that the later route wiring will inject.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest


def test_feedback_round_trip_and_flip_does_not_store_citation_data() -> None:
    from fin.host.feedback import InMemoryFeedbackStore

    store = InMemoryFeedbackStore()

    first = store.set_feedback("run-1", "up")
    flipped = store.set_feedback("run-1", "down")

    assert first.run_id == flipped.run_id == "run-1"
    assert first.value == "up"
    assert flipped.value == "down"
    assert store.get_feedback("run-1") == flipped
    assert not hasattr(flipped, "citations")


def test_feedback_rejects_empty_run_id() -> None:
    from fin.host.feedback import InvalidFeedbackError, InMemoryFeedbackStore

    with pytest.raises(InvalidFeedbackError, match="run_id"):
        InMemoryFeedbackStore().set_feedback("   ", "up")


def test_feedback_accepts_negative_thumb_value() -> None:
    from fin.host.feedback import InMemoryFeedbackStore

    saved = InMemoryFeedbackStore().set_feedback("run-negative", "down")

    assert saved.value == "down"


def test_feedback_rejects_overflow_run_id() -> None:
    from fin.host.feedback import (
        MAX_RUN_ID_LENGTH,
        InvalidFeedbackError,
        InMemoryFeedbackStore,
    )

    with pytest.raises(InvalidFeedbackError, match="run_id"):
        InMemoryFeedbackStore().set_feedback("r" * (MAX_RUN_ID_LENGTH + 1), "up")


def test_feedback_concurrent_flips_leave_one_consistent_record() -> None:
    from fin.host.feedback import InMemoryFeedbackStore

    store = InMemoryFeedbackStore()
    barrier = threading.Barrier(12)
    errors: list[BaseException] = []

    def submit(value: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.set_feedback("run-concurrent", value)
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    threads = [
        threading.Thread(target=submit, args=("up" if i % 2 else "down",))
        for i in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    saved = store.get_feedback("run-concurrent")
    assert saved is not None
    assert saved.value in {"up", "down"}
    assert len(store.list_feedback()) == 1


def test_feedback_wraps_storage_exception_as_typed_error() -> None:
    from fin.host.feedback import (
        FeedbackPersistenceError,
        PostgresFeedbackStore,
    )

    class BrokenPool:
        def connection(self):
            raise RuntimeError("database unavailable")

    store = PostgresFeedbackStore(BrokenPool(), initialize=False)

    with pytest.raises(FeedbackPersistenceError, match="feedback"):
        store.set_feedback("run-1", "up")


def test_postgres_feedback_store_uses_injected_pool_and_upserts_latest_thumb() -> None:
    from fin.host.feedback import PostgresFeedbackStore

    now = datetime.now(timezone.utc)
    calls: list[tuple[str, object]] = []

    class Result:
        def fetchone(self):
            return ("run-pg", "down", now, now)

        def fetchall(self):
            return [("run-pg", "down", now, now)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            calls.append((statement, params))
            return Result()

        def commit(self):
            calls.append(("commit", None))

    class Pool:
        def connection(self):
            return Connection()

    store = PostgresFeedbackStore(Pool())
    saved = store.set_feedback("run-pg", "down")

    assert saved.run_id == "run-pg"
    assert saved.value == "down"
    assert any("ON CONFLICT (run_id)" in statement for statement, _ in calls)
    assert store.get_feedback("run-pg").value == "down"


def test_set_feedback_accepts_an_explicit_store_dependency() -> None:
    from fin.host.feedback import InMemoryFeedbackStore, set_feedback

    store = InMemoryFeedbackStore()

    saved = set_feedback("run-injected", "up", store=store)

    assert saved == store.get_feedback("run-injected")
