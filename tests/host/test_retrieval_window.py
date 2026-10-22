"""iknow retrieve window on the adapter result and the run row.

The MCP payload's ``retrieval_fingerprint``, ``generation_id``, and
``epoch`` stay on the retrieve result when each value is a string.
The run row stores that object as ``retrieval_window`` and GET
serializes the same object. No docker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.host.thread_store import (
    InMemoryThreadStore,
    PostgresThreadStore,
    RunRecord,
    _SCHEMA_SQL,
)
from fin.host.workspace_http import _serialize_run
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


def _source_ref() -> str:
    return serialize_source_ref(
        UnitLocator(
            document_id="DOC-2023Q1",
            version="v-" + "a" * 60,
            unit=1,
        )
    )


class _Scripted(MCPRetrieveAdapter):
    def __init__(self, response: dict) -> None:
        super().__init__()
        self._response = response

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        return self._response


def _chunks_response(extra: dict) -> dict:
    body = {
        "chunks": [
            {
                "id": "chunk-1",
                "content": "hit",
                "source_ref": _source_ref(),
                "score": 1.0,
            }
        ],
        **extra,
    }
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(body)}],
        }
    }


def test_adapter_keeps_present_window_strings_and_drops_the_rest() -> None:
    full = _Scripted(
        _chunks_response(
            {
                "retrieval_fingerprint": "fp-1",
                "generation_id": "gen-from-iknow",
                "epoch": "7",
            }
        )
    ).retrieve("revenue")
    assert full["retrieval_fingerprint"] == "fp-1"
    assert full["generation_id"] == "gen-from-iknow"
    assert full["epoch"] == "7"
    assert full["chunks"]

    partial = _Scripted(
        _chunks_response(
            {
                "retrieval_fingerprint": "fp-2",
                "generation_id": 9,
            }
        )
    ).retrieve("revenue")
    assert partial["retrieval_fingerprint"] == "fp-2"
    assert "generation_id" not in partial
    assert "epoch" not in partial

    bare = _Scripted(_chunks_response({})).retrieve("revenue")
    assert "retrieval_fingerprint" not in bare
    assert "generation_id" not in bare
    assert "epoch" not in bare


def test_retrieval_window_round_trips_onto_the_run_payload(monkeypatch) -> None:
    window = {
        "retrieval_fingerprint": "fp-1",
        "generation_id": "gen-from-iknow",
        "epoch": "7",
    }
    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="t")
    store.append_run(
        RunRecord(
            id="r-1",
            thread_id="t-1",
            question="q",
            status="done",
            answer="a",
            retrieval_window=dict(window),
        )
    )
    loaded = store.list_runs("t-1")[0]
    assert loaded.retrieval_window == window
    assert _serialize_run(loaded)["retrieval_window"] == window
    bare = _serialize_run(
        RunRecord(id="r-0", thread_id="t-1", question="q", status="done")
    )
    assert bare["retrieval_window"] == {}

    assert (
        "retrieval_window JSONB NOT NULL DEFAULT '{}'::jsonb" in _SCHEMA_SQL
    )

    captured: list[tuple[str, tuple]] = []

    class _Conn:
        def execute(self, sql: str, params=None):
            if params is not None:
                captured.append((sql, tuple(params)))

            class _Result:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return _Result()

        def commit(self) -> None:
            return None

    class _CM:
        def __enter__(self) -> _Conn:
            return _Conn()

        def __exit__(self, *exc: object) -> bool:
            return False

    import fin.host.thread_store as thread_store

    monkeypatch.setattr(thread_store.psycopg, "connect", lambda *a, **k: _CM())
    pg = PostgresThreadStore.__new__(PostgresThreadStore)
    pg._url = "postgresql://stub"
    pg.append_run(
        RunRecord(
            id="r-1",
            thread_id="t-1",
            question="q",
            status="done",
            retrieval_window=dict(window),
        )
    )
    sql, params = captured[-1]
    column_sql = sql.split("(", 1)[1].split(")", 1)[0]
    names = [name.strip() for name in column_sql.split(",")]
    assert json.loads(params[names.index("retrieval_window")]) == window

    stamp = datetime.now(timezone.utc)
    row = (
        "r-1",
        "t-1",
        "q",
        "done",
        "",
        "a",
        "[]",
        "",
        "[]",
        "",
        stamp,
        False,
        0,
        "{}",
        "",
        "[]",
        "[]",
        "[]",
        "[]",
        "[]",
        json.dumps(window),
    )
    assert PostgresThreadStore._row_to_run(row).retrieval_window == window
    assert PostgresThreadStore._row_to_run(row[:20]).retrieval_window == {}
