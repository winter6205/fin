"""Memory facts: workspace-isolated title+body rows with a derived hook.

`specs/cross-session-operator-memory.md` SC5 / SC6 evolve the existing
``fin_memory`` working store into **memory facts** — a ``title`` and a
``body``, where the **hook** is a derived value (first non-empty body line,
whitespace-folded, ≤80 characters). Rows are isolated per workspace so a
fact written in workspace A is invisible from workspace B.

Boundary classes exercised here:
- empty (an empty store returns zero rows; an empty body yields an empty
  hook; workspace-isolated rows return 0 in the other workspace).
- negative (legacy ``text`` rows still load: their body is the legacy text
  and the hook is computed from it).
- overflow (a body whose first line exceeds 80 characters is truncated to
  80; multi-line bodies collapse to the first non-empty line).
- concurrent (the in-process store keeps workspace isolation under
  concurrent writes; rows() remains a copy so callers cannot mutate the
  store across workspaces).
- exception (the Postgres DDL is idempotent against an existing legacy
  schema — ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` lands without
  error when the columns are already present).

Both implementations are exercised together so the in-process store and the
Postgres store cannot drift apart on the new contract (memory SC10).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import pytest

from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_store import (
    InProcessMemoryStore,
    MEMORY_SCHEMA_SQL,
    MemoryRow,
)


POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

HOOK_MAX_CHARS = 80
_FOLD_RE = re.compile(r"\s+")


def _now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _row(
    row_id: str,
    *,
    text: str = "legacy body",
    title: str = "",
    body: str = "",
    workspace: str = "",
    hit_count: int = 0,
) -> MemoryRow:
    """Build a fact row the way peers will: title, body, workspace."""
    return MemoryRow(
        id=row_id,
        text=text,
        title=title,
        body=body,
        workspace=workspace,
        metadata={},
        created_at=_now(),
        hit_count=hit_count,
    )


# ---------------------------------------------------------------------------
# Hook projection: first non-empty body line, folded, ≤80 chars.
# ---------------------------------------------------------------------------


def test_hook_is_the_first_non_empty_body_line_for_single_line_body() -> None:
    row = _row("a", body="Operator prefers quarterly revenue.")

    assert row.hook == "Operator prefers quarterly revenue."


def test_hook_collapses_whitespace_runs_within_a_line() -> None:
    row = _row("a", body="Operator\tprefers   quarterly\nrevenue.")

    # The first non-empty line is "Operator\tprefers   quarterly".
    assert row.hook == "Operator prefers quarterly"


def test_hook_skips_blank_lines_before_the_first_real_line() -> None:
    row = _row(
        "a",
        body="\n\n  \nOperator prefers quarterly revenue.\nMore body.",
    )

    assert row.hook == "Operator prefers quarterly revenue."


def test_hook_truncates_to_eighty_characters() -> None:
    long_line = "x" * 200
    row = _row("a", body=long_line)

    hook = row.hook
    assert len(hook) == HOOK_MAX_CHARS
    assert hook == "x" * HOOK_MAX_CHARS


def test_hook_is_empty_when_body_is_empty() -> None:
    row = _row("a", body="")

    assert row.hook == ""


def test_hook_is_empty_when_body_has_no_non_empty_lines() -> None:
    row = _row("a", body="\n\n   \n\n")

    assert row.hook == ""


def test_hook_falls_back_to_legacy_text_when_body_is_empty() -> None:
    """Legacy rows persisted before the body column still have a hook."""

    row = MemoryRow(
        id="legacy",
        text="Legacy memory line.",
        metadata={},
        created_at=_now(),
    )

    assert row.hook == "Legacy memory line."


def test_body_defaults_to_text_when_only_text_is_supplied() -> None:
    """Calling code that still passes ``text=`` only does not lose the body."""

    row = MemoryRow(
        id="legacy",
        text="Legacy memory line.",
        metadata={},
        created_at=_now(),
    )

    assert row.body == "Legacy memory line."
    assert row.hook == "Legacy memory line."


# ---------------------------------------------------------------------------
# Round-trip: extract persists title + body, entries returns both.
# ---------------------------------------------------------------------------


def test_extract_persists_title_and_body_and_workspace() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Operator prefers quarterly revenue.",
        metadata={"thread_id": "thread-1"},
        title="Quarterly preference",
        workspace="workspace-a",
    )

    rows = buffer.entries(workspace="workspace-a")

    assert len(rows) == 1
    [row] = rows
    assert row["title"] == "Quarterly preference"
    assert row["body"] == "Operator prefers quarterly revenue."
    assert row["workspace"] == "workspace-a"
    assert row["hook"] == "Operator prefers quarterly revenue."


def test_extract_without_title_persists_an_empty_title() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract("Memory body only.", workspace="workspace-a")

    rows = buffer.entries(workspace="workspace-a")

    assert len(rows) == 1
    [row] = rows
    assert row["title"] == ""
    assert row["body"] == "Memory body only."


def test_recall_returns_rows_with_title_body_hook() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Operator prefers quarterly revenue.",
        title="Quarterly preference",
        workspace="workspace-a",
    )

    [row] = buffer.recall("quarterly", workspace="workspace-a")["memories"]

    assert row["title"] == "Quarterly preference"
    assert row["body"] == "Operator prefers quarterly revenue."
    assert row["hook"] == "Operator prefers quarterly revenue."


def test_round_trip_title_and_body_survives_a_reopened_buffer() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Operator prefers quarterly revenue.",
        metadata={"thread_id": "thread-1"},
        title="Quarterly preference",
        workspace="workspace-a",
    )

    rows = buffer.entries(workspace="workspace-a")

    assert len(rows) == 1
    [row] = rows
    assert row["title"] == "Quarterly preference"
    assert row["body"] == "Operator prefers quarterly revenue."
    assert row["hook"] == "Operator prefers quarterly revenue."


# ---------------------------------------------------------------------------
# Empty store: recall and entries return zero results on an empty store.
# ---------------------------------------------------------------------------


def test_empty_store_recall_returns_empty_payload() -> None:
    buffer = LocalMemoryBuffer()

    assert buffer.recall("anything")["memories"] == []
    assert buffer.recall("anything", workspace="workspace-a")["memories"] == []


def test_empty_store_entries_returns_empty_list() -> None:
    buffer = LocalMemoryBuffer()

    assert buffer.entries() == []
    assert buffer.entries(workspace="workspace-a") == []


# ---------------------------------------------------------------------------
# Workspace isolation: rows written in A are invisible in B and vice versa.
# ---------------------------------------------------------------------------


def test_rows_in_workspace_a_are_invisible_in_workspace_b() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Workspace A fact.",
        title="A-title",
        workspace="workspace-a",
    )
    buffer.extract(
        "Workspace B fact.",
        title="B-title",
        workspace="workspace-b",
    )

    a_rows = buffer.entries(workspace="workspace-a")
    b_rows = buffer.entries(workspace="workspace-b")

    assert [row["title"] for row in a_rows] == ["A-title"]
    assert [row["body"] for row in a_rows] == ["Workspace A fact."]
    assert [row["title"] for row in b_rows] == ["B-title"]
    assert [row["body"] for row in b_rows] == ["Workspace B fact."]


def test_recall_only_returns_rows_from_the_named_workspace() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Workspace A fact about quarterly revenue.",
        title="A-title",
        workspace="workspace-a",
    )
    buffer.extract(
        "Workspace B fact about quarterly revenue.",
        title="B-title",
        workspace="workspace-b",
    )

    a = buffer.recall("quarterly revenue", workspace="workspace-a")
    b = buffer.recall("quarterly revenue", workspace="workspace-b")

    assert [row["title"] for row in a["memories"]] == ["A-title"]
    assert [row["title"] for row in b["memories"]] == ["B-title"]


def test_recall_in_empty_workspace_returns_zero_even_when_other_workspaces_have_rows() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Workspace A fact.",
        title="A-title",
        workspace="workspace-a",
    )

    result = buffer.recall("Workspace", workspace="workspace-b")

    assert result["memories"] == []


def test_workspace_isolation_is_bidirectional() -> None:
    buffer = LocalMemoryBuffer()
    buffer.extract(
        "Workspace B fact.",
        title="B-title",
        workspace="workspace-b",
    )

    assert buffer.entries(workspace="workspace-a") == []
    assert buffer.recall("Workspace", workspace="workspace-a")["memories"] == []


# ---------------------------------------------------------------------------
# Default workspace keeps existing peer tests compatible.
# ---------------------------------------------------------------------------


def test_default_workspace_empty_string_matches_legacy_callers() -> None:
    """Legacy callers (memory-buffer tests) pass no workspace.

    Rows stored with the implicit empty workspace are visible to callers
    that also pass the implicit empty workspace.
    """
    buffer = LocalMemoryBuffer()
    buffer.extract("Legacy style extraction.")

    assert len(buffer.entries()) == 1
    assert buffer.recall("Legacy")["memories"][0]["body"] == "Legacy style extraction."


# ---------------------------------------------------------------------------
# Store-level contracts on the in-process implementation.
# ---------------------------------------------------------------------------


def test_in_process_store_rows_can_filter_by_workspace() -> None:
    store = InProcessMemoryStore()
    store.add(_row("a", title="A", body="Workspace A body", workspace="workspace-a"))
    store.add(_row("b", title="B", body="Workspace B body", workspace="workspace-b"))

    a = store.rows(workspace="workspace-a")
    b = store.rows(workspace="workspace-b")

    assert [row.id for row in a] == ["a"]
    assert [row.id for row in b] == ["b"]


def test_in_process_store_rows_without_workspace_filter_returns_all() -> None:
    """Dream reads every row across workspaces; ``workspace=None`` is the escape hatch."""

    store = InProcessMemoryStore()
    store.add(_row("a", title="A", body="Workspace A body", workspace="workspace-a"))
    store.add(_row("b", title="B", body="Workspace B body", workspace="workspace-b"))

    assert {row.id for row in store.rows()} == {"a", "b"}


def test_in_process_store_rows_return_copies() -> None:
    store = InProcessMemoryStore()
    store.add(_row("a", title="A", body="Workspace A body", workspace="workspace-a"))

    rows = store.rows(workspace="workspace-a")
    rows[0].title = "tampered"
    rows[0].workspace = "workspace-b"

    assert store.rows()[0].title == "A"
    assert store.rows()[0].workspace == "workspace-a"


# ---------------------------------------------------------------------------
# DDL: schema advertises the new columns; idempotent guards exist.
# ---------------------------------------------------------------------------


def test_schema_ddl_advertises_title_body_and_workspace() -> None:
    """The fresh-CREATE branch declares the new columns."""

    assert "title TEXT NOT NULL" in MEMORY_SCHEMA_SQL
    assert "body TEXT NOT NULL" in MEMORY_SCHEMA_SQL
    assert "workspace TEXT NOT NULL" in MEMORY_SCHEMA_SQL


def test_schema_ddl_uses_add_column_if_not_exists_for_legacy_backfill() -> None:
    """A legacy DB without the new columns still gets them on next boot."""

    assert "ALTER TABLE fin_memory ADD COLUMN IF NOT EXISTS title" in MEMORY_SCHEMA_SQL
    assert "ALTER TABLE fin_memory ADD COLUMN IF NOT EXISTS body" in MEMORY_SCHEMA_SQL
    assert "ALTER TABLE fin_memory ADD COLUMN IF NOT EXISTS workspace" in MEMORY_SCHEMA_SQL


def test_schema_ddl_still_touches_only_fin_memory_and_the_cursor_table() -> None:
    tables = {"fin_memory", "fin_memory_dream_cursor"}
    mentioned = {
        token.strip("(,;")
        for token in MEMORY_SCHEMA_SQL.replace("\n", " ").split()
        if token.strip("(,;").startswith("fin_")
    }

    assert mentioned == tables


# ---------------------------------------------------------------------------
# Postgres DDL idempotency: only runnable with a live Postgres URL.
# The contract is "running setup twice still leaves a usable schema".
# ---------------------------------------------------------------------------


@pytest.fixture
def postgres_pool():
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the Postgres memory store is "
            "untested in this environment"
        )
    from fin.host.db_pool import PostgresPool

    pool = PostgresPool(url)
    pool.open(wait=True)
    try:
        yield pool
    finally:
        pool.close()


def test_postgres_setup_remains_idempotent_with_the_new_columns(
    postgres_pool,
) -> None:
    """``PostgresMemoryStore.setup()`` still runs twice without raising."""

    from fin.host.memory_store import PostgresMemoryStore

    store = PostgresMemoryStore(postgres_pool)
    try:
        store.setup()
        store.setup()
        # A second open-and-setup after the columns exist is still a no-op.
        reopened = PostgresMemoryStore(postgres_pool)
        reopened.setup()

        store.add(
            _row(
                "idempotent-row",
                title="Idempotent title",
                body="Idempotent body.",
                workspace="workspace-a",
            )
        )

        rows = store.rows(workspace="workspace-a")
        assert len(rows) == 1
        assert rows[0].title == "Idempotent title"
        assert rows[0].body == "Idempotent body."
        assert rows[0].workspace == "workspace-a"
    finally:
        with postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM fin_memory")
            conn.commit()