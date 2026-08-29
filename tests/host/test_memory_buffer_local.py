"""T5 local memory-buffer contracts.

Memory results are advisory context only.  They are deliberately not shaped
like filing evidence and cannot supply citation verification on their own.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fin.host.memory_buffer import LocalMemoryBuffer, MemoryBufferInputError
from fin.host.memory_port import DEFAULT_MEMORY_LIMIT, MEMORY_LIMIT_CAP


def test_extract_returns_an_advisory_memory_without_citation_fields() -> None:
    buffer = LocalMemoryBuffer()

    result = buffer.extract(
        "The operator prefers quarterly revenue comparisons.",
        metadata={"thread_id": "thread-1", "track": "dialogue"},
    )

    assert result["status"] == "ok"
    assert len(result["memories"]) == 1
    memory = result["memories"][0]
    assert memory["text"] == (
        "The operator prefers quarterly revenue comparisons."
    )
    assert memory["metadata"] == {
        "thread_id": "thread-1",
        "track": "dialogue",
    }
    assert memory["advisory"] is True
    assert memory["citation_eligible"] is False
    assert "citations" not in memory
    assert "evidence" not in memory
    assert "source_ref" not in memory


def test_empty_extract_and_recall_do_not_create_or_return_memory() -> None:
    buffer = LocalMemoryBuffer()

    assert buffer.extract(" \n\t") == {"status": "empty", "memories": []}
    assert buffer.recall("   ") == {"status": "empty", "memories": []}
    assert buffer.entries() == []


def test_recall_returns_matching_active_memory_as_advisory_content() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract("Use quarterly revenue comparisons for this thread.")
    buffer.extract("The preferred output is a short explanation.")

    result = buffer.recall("quarterly revenue")

    assert result["status"] == "ok"
    assert [item["text"] for item in result["memories"]] == [
        "Use quarterly revenue comparisons for this thread."
    ]
    assert result["memories"][0]["advisory"] is True
    assert result["memories"][0]["citation_eligible"] is False


def test_gc_soft_disables_expired_and_superseded_rows_without_deleting_them() -> None:
    buffer = LocalMemoryBuffer()
    now = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc)
    expired = buffer.extract(
        "Old preference.",
        metadata={"expires_at": now - timedelta(seconds=1)},
    )["memories"][0]
    superseded = buffer.extract("Old company name.")["memories"][0]
    current = buffer.extract(
        "New company name.",
        metadata={"supersedes": superseded["id"]},
    )["memories"][0]

    result = buffer.gc(now=now)

    assert result == {"status": "ok", "removed": 2}
    rows = {row["id"]: row for row in buffer.entries()}
    assert set(rows) == {expired["id"], superseded["id"], current["id"]}
    assert rows[expired["id"]]["status"] == "disabled"
    assert rows[expired["id"]]["disabled_reason"] == "expired"
    assert rows[superseded["id"]]["status"] == "disabled"
    assert rows[superseded["id"]]["disabled_reason"] == "superseded"
    assert buffer.recall("old preference")["memories"] == []
    recalled_ids = {
        item["id"] for item in buffer.recall("company")["memories"]
    }
    assert superseded["id"] not in recalled_ids
    assert current["id"] in recalled_ids


def test_negative_and_overflow_limits_are_bounded_like_the_memory_port() -> None:
    buffer = LocalMemoryBuffer()
    for index in range(MEMORY_LIMIT_CAP + 2):
        buffer.extract(f"shared memory {index}")

    assert len(buffer.recall("shared memory", limit=-1)["memories"]) == (
        DEFAULT_MEMORY_LIMIT
    )
    assert len(
        buffer.recall("shared memory", limit=MEMORY_LIMIT_CAP + 100)["memories"]
    ) == MEMORY_LIMIT_CAP
    assert buffer.gc(limit=MEMORY_LIMIT_CAP + 100)["removed"] == 0


def test_concurrent_extracts_keep_all_unique_rows() -> None:
    buffer = LocalMemoryBuffer()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda index: buffer.extract(f"concurrent memory {index}"),
                range(32),
            )
        )

    ids = [result["memories"][0]["id"] for result in results]
    assert len(ids) == len(set(ids)) == 32
    assert len(buffer.entries()) == 32


@pytest.mark.parametrize(
    ("text", "metadata"),
    [
        (None, None),
        ("valid", {"expires_at": "not-a-date"}),
        ("valid", {"supersedes": ""}),
    ],
)
def test_invalid_extract_input_raises_a_typed_buffer_error(
    text: object,
    metadata: object,
) -> None:
    buffer = LocalMemoryBuffer()

    with pytest.raises(MemoryBufferInputError):
        buffer.extract(text, metadata=metadata)  # type: ignore[arg-type]
