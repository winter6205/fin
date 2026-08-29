"""Memory-buffer port contracts, shared by both implementations.

``specs/memory-dream.md`` SC10: the process-local buffer and the Postgres
buffer implement the same ``MemoryPort`` and are exercised by one suite, so
the two cannot drift. The Postgres half is opt-in — without
``FIN_CHECKPOINT_URL`` those parameterisations skip, which keeps the default
``pytest -q`` hermetic (SC1, SC2).

T5 wrote this file against the process-local buffer alone; T6a
parameterises it because ADR 0035 makes Postgres the working store.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fin.host.memory_buffer import (
    LocalMemoryBuffer,
    MemoryBufferInputError,
    PostgresMemoryBuffer,
)
from fin.host.memory_port import DEFAULT_MEMORY_LIMIT, MEMORY_LIMIT_CAP
from fin.host.memory_store import MEMORY_TABLE

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


def _configured_postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the Postgres memory buffer is "
            "untested in this environment"
        )
    return url


@pytest.fixture
def postgres_pool():
    from fin.host.db_pool import PostgresPool

    pool = PostgresPool(_configured_postgres_url())
    pool.open(wait=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def postgres_buffer(postgres_pool):
    buffer = PostgresMemoryBuffer(postgres_pool)
    try:
        yield buffer
    finally:
        with postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {MEMORY_TABLE}")
            conn.commit()


@pytest.fixture(params=["in-process", "postgres"])
def buffer(request: pytest.FixtureRequest):
    """Parameterise the port contract over both buffers."""
    if request.param == "in-process":
        return LocalMemoryBuffer()
    return request.getfixturevalue("postgres_buffer")


def test_extract_returns_an_advisory_memory_without_citation_fields(
    buffer,
) -> None:
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
    assert memory["hit_count"] == 0
    assert "citations" not in memory
    assert "evidence" not in memory
    assert "source_ref" not in memory


def test_empty_extract_and_recall_do_not_create_or_return_memory(
    buffer,
) -> None:
    assert buffer.extract(" \n\t") == {"status": "empty", "memories": []}
    assert buffer.recall("   ") == {"status": "empty", "memories": []}
    assert buffer.entries() == []


def test_recall_returns_matching_active_memory_as_advisory_content(
    buffer,
) -> None:
    buffer.extract("Use quarterly revenue comparisons for this thread.")
    buffer.extract("The preferred output is a short explanation.")

    result = buffer.recall("quarterly revenue")

    assert result["status"] == "ok"
    assert [item["text"] for item in result["memories"]] == [
        "Use quarterly revenue comparisons for this thread."
    ]
    assert result["memories"][0]["advisory"] is True
    assert result["memories"][0]["citation_eligible"] is False


def test_gc_soft_disables_expired_and_superseded_rows_without_deleting_them(
    buffer,
) -> None:
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


def test_negative_and_overflow_limits_are_bounded_like_the_memory_port(
    buffer,
) -> None:
    for index in range(MEMORY_LIMIT_CAP + 2):
        buffer.extract(f"shared memory {index}")

    assert len(buffer.recall("shared memory", limit=-1)["memories"]) == (
        DEFAULT_MEMORY_LIMIT
    )
    assert len(
        buffer.recall("shared memory", limit=MEMORY_LIMIT_CAP + 100)["memories"]
    ) == MEMORY_LIMIT_CAP
    assert buffer.gc(limit=MEMORY_LIMIT_CAP + 100)["removed"] == 0


def test_concurrent_extracts_keep_all_unique_rows(buffer) -> None:
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


# ----- T6a: hit counting feeds the T6b usefulness score (SC7) -----


def test_recall_increments_hit_count_once_per_hit(buffer) -> None:
    buffer.extract("Use quarterly revenue comparisons.")

    buffer.recall("quarterly revenue")
    buffer.recall("quarterly revenue")

    assert [row["hit_count"] for row in buffer.entries()] == [2]


def test_recall_only_counts_the_rows_it_returned(buffer) -> None:
    buffer.extract("Quarterly revenue comparisons.")
    buffer.extract("Unrelated onboarding preference.")

    buffer.recall("quarterly revenue")

    hits = {row["text"]: row["hit_count"] for row in buffer.entries()}
    assert hits == {
        "Quarterly revenue comparisons.": 1,
        "Unrelated onboarding preference.": 0,
    }


def test_disabled_rows_are_excluded_from_entries_when_asked(buffer) -> None:
    buffer.extract("Quarterly revenue comparisons.")
    buffer.extract(
        "Old preference.", metadata={"expires_at": "2020-01-01T00:00:00Z"}
    )

    buffer.gc()

    assert len(buffer.entries(include_disabled=False)) == 1
    assert len(buffer.entries()) == 2


# ----- T6a: wiring (SC4, SC5, SC6) -----


def test_memory_on_without_postgres_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host.memory_port import (
        MemoryBackendUnavailableError,
        build_memory_port,
    )

    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    with pytest.raises(MemoryBackendUnavailableError):
        build_memory_port(env={"FIN_MEMORY_ENABLED": "true"})


def test_memory_on_with_postgres_needs_no_mcp(postgres_pool) -> None:
    """SC6: memory ON requires Postgres only, never an MCP server."""
    from fin.host.memory_port import build_memory_port

    url = _configured_postgres_url()

    port = build_memory_port(
        env={"FIN_MEMORY_ENABLED": "true", "FIN_CHECKPOINT_URL": url}
    )

    assert isinstance(port, PostgresMemoryBuffer)
    assert port.enabled is True
    reopened = None
    try:
        port.extract("Durable across instances.", metadata={"thread_id": "t-1"})
        reopened = build_memory_port(
            env={"FIN_MEMORY_ENABLED": "true", "FIN_CHECKPOINT_URL": url}
        )
        assert [row["text"] for row in reopened.entries()] == [
            "Durable across instances."
        ]
    finally:
        if reopened is not None:
            reopened.close()
        port.close()
        with postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {MEMORY_TABLE}")
            conn.commit()
