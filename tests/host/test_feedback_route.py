"""Workspace thumbs-feedback route tests (T8 product wiring).

Pins the ``POST /api/threads/{thread_id}/runs/{run_id}/feedback``
route, the ``feedback`` field on GET-history turn rows, and the
ADR 0035/0036 invariant: setting or flipping a thumb NEVER mutates
the run's persisted citations.

The five boundary classes (empty / oversized run_id, invalid value,
unknown ids, concurrent flip, store outage) are covered with fake
stores installed via ``app.dependency_overrides`` — the same
pattern ``test_workspace_http.py`` uses. No real Postgres here.
"""

from __future__ import annotations

import contextlib
import types
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.feedback import (
    FeedbackPersistenceError,
    InMemoryFeedbackStore,
    MAX_RUN_ID_LENGTH,
)


class _StubGraph:
    """Graph stand-in with only the seams GET-history probes."""

    def is_locked(self, thread_id: str) -> bool:
        return False

    def pending_nodes(self, thread_id: str) -> tuple:
        return ()


@pytest.fixture
def http_app() -> FastAPI:
    """Fresh app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def wired(http_app: FastAPI):
    """In-memory thread store + in-memory feedback store overrides."""
    from fin.host.http import get_feedback_store, get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    feedback = InMemoryFeedbackStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_feedback_store] = lambda: feedback
    http_app.dependency_overrides[get_graph] = lambda: _StubGraph()
    return store, feedback


def _seed_thread_with_run(
    store: Any,
    *,
    run_id: str = "run-1",
    citations: list[dict[str, Any]] | None = None,
) -> None:
    """One thread (``t-1``) with one terminal ``done`` run row."""
    from fin.host.thread_store import RunRecord

    store.create_thread(id="t-1", title="x")
    store.append_run(
        RunRecord(
            id=run_id,
            thread_id="t-1",
            question="what was revenue?",
            status="done",
            status_reason="",
            answer="a1",
            citations=citations if citations is not None else [],
            thinking_text="",
            thinking_duration_ms=0,
            tool_summaries=[],
            created_at=datetime.now(tz=timezone.utc),
        )
    )


def _feedback_url(run_id: str = "run-1", thread_id: str = "t-1") -> str:
    return f"/api/threads/{thread_id}/runs/{run_id}/feedback"


# ----- happy path -----


def test_post_feedback_round_trip_persists_and_echoes_snake_case(
    client: TestClient, wired
) -> None:
    store, feedback = wired
    _seed_thread_with_run(store)

    r = client.post(_feedback_url(), json={"value": "up"})

    assert r.status_code == 200
    body = r.json()
    # The wire keys are exactly what web/src/feedback.ts parses.
    assert set(body.keys()) == {"run_id", "value", "created_at", "updated_at"}
    assert body["run_id"] == "run-1"
    assert body["value"] == "up"
    assert feedback.get_feedback("run-1").value == "up"


def test_get_history_surfaces_feedback_on_the_matching_turn(
    client: TestClient, wired
) -> None:
    store, _feedback = wired
    _seed_thread_with_run(store, run_id="run-a")
    _seed_thread_with_run_second(store, run_id="run-b")

    client.post(_feedback_url("run-a"), json={"value": "down"})
    detail = client.get("/api/threads/t-1").json()

    by_run = {t["run_id"]: t for t in detail["turns"]}
    assert by_run["run-a"]["feedback"] == "down"
    # A turn without a thumb stays null — the field is additive, so
    # older clients that ignore it keep working.
    assert by_run["run-b"]["feedback"] is None


def _seed_thread_with_run_second(store: Any, *, run_id: str) -> None:
    from fin.host.thread_store import RunRecord

    store.append_run(
        RunRecord(
            id=run_id,
            thread_id="t-1",
            question="follow-up?",
            status="refuse",
            status_reason="unknown_locator",
            answer="",
            citations=[],
            thinking_text="",
            thinking_duration_ms=0,
            tool_summaries=[],
            created_at=datetime.now(tz=timezone.utc),
        )
    )


def test_history_stays_backward_compatible_without_feedback(
    client: TestClient, wired
) -> None:
    store, _feedback = wired
    _seed_thread_with_run(store)

    turn = client.get("/api/threads/t-1").json()["turns"][0]

    # The pre-T8 wire keys are all still present.
    for key in ("run_id", "question", "status", "answer", "citations", "created_at"):
        assert key in turn
    assert turn["feedback"] is None


# ----- citations invariant (ADR 0035/0036, spec SC10) -----


def test_setting_and_flipping_feedback_never_mutates_citations(
    client: TestClient, wired
) -> None:
    store, _feedback = wired
    citations = [
        {
            "document_id": "AAPL-2022Q3",
            "version": "a" * 64,
            "unit": 4,
            "quote": "revenue was up five percent year over year",
        }
    ]
    _seed_thread_with_run(store, citations=citations)

    before = client.get("/api/threads/t-1").json()["turns"][0]["citations"]
    client.post(_feedback_url(), json={"value": "up"})
    client.post(_feedback_url(), json={"value": "down"})
    after = client.get("/api/threads/t-1").json()["turns"][0]["citations"]

    assert after == before


# ----- boundary class: invalid value / malformed body -----


def test_post_feedback_invalid_value_is_a_typed_400(
    client: TestClient, wired
) -> None:
    store, feedback = wired
    _seed_thread_with_run(store)

    r = client.post(_feedback_url(), json={"value": "sideways"})

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_feedback"}
    assert feedback.list_feedback() == []


def test_post_feedback_malformed_body_is_4xx(
    client: TestClient, wired
) -> None:
    store, _feedback = wired
    _seed_thread_with_run(store)

    r = client.post(_feedback_url(), json={"val": "up"})

    assert 400 <= r.status_code < 500


# ----- boundary class: empty / oversized run_id -----


def test_post_feedback_oversized_run_id_is_a_typed_400(
    client: TestClient, wired
) -> None:
    store, feedback = wired
    _seed_thread_with_run(store)

    oversized = "r" * (MAX_RUN_ID_LENGTH + 1)
    r = client.post(_feedback_url(oversized), json={"value": "up"})

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_feedback"}
    assert feedback.list_feedback() == []


def test_post_feedback_whitespace_run_id_is_a_typed_400(
    client: TestClient, wired
) -> None:
    store, feedback = wired
    _seed_thread_with_run(store)

    r = client.post(_feedback_url("%20%20"), json={"value": "up"})

    assert r.status_code == 400
    assert r.json() == {"error": "invalid_feedback"}
    assert feedback.list_feedback() == []


# ----- boundary class: unknown ids -----


def test_post_feedback_unknown_thread_is_404(client: TestClient, wired) -> None:
    _store, feedback = wired

    r = client.post(_feedback_url(thread_id="t-missing"), json={"value": "up"})

    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}
    assert feedback.list_feedback() == []


def test_post_feedback_unknown_run_is_404(client: TestClient, wired) -> None:
    store, feedback = wired
    _seed_thread_with_run(store)

    r = client.post(_feedback_url("run-missing"), json={"value": "up"})

    assert r.status_code == 404
    assert r.json() == {"error": "run_not_found"}
    assert feedback.list_feedback() == []


# ----- boundary class: concurrent flip (last write wins) -----


def test_post_feedback_concurrent_flips_leave_last_write_wins(
    client: TestClient, wired
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store, feedback = wired
    _seed_thread_with_run(store)
    barrier_start = threading_barrier()

    def submit(value: str) -> int:
        barrier_start.wait(timeout=5)
        r = client.post(_feedback_url(), json={"value": value})
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(submit, ["up", "down"]))

    assert codes == [200, 200]
    saved = feedback.get_feedback("run-1")
    assert saved is not None
    assert saved.value in {"up", "down"}
    turn = client.get("/api/threads/t-1").json()["turns"][0]
    assert turn["feedback"] == saved.value
    # The flip never grew a second record.
    assert len(feedback.list_feedback()) == 1


def test_post_feedback_rapid_sequential_flip_last_write_wins(
    client: TestClient, wired
) -> None:
    store, _feedback = wired
    _seed_thread_with_run(store)

    first = client.post(_feedback_url(), json={"value": "up"})
    second = client.post(_feedback_url(), json={"value": "down"})

    assert first.status_code == second.status_code == 200
    assert second.json()["value"] == "down"
    turn = client.get("/api/threads/t-1").json()["turns"][0]
    assert turn["feedback"] == "down"


def threading_barrier():
    import threading

    return threading.Barrier(2)


# ----- boundary class: store outage -> typed 5xx -----


def test_post_feedback_store_outage_is_a_typed_503(
    http_app: FastAPI, client: TestClient, wired
) -> None:
    from fin.host.http import get_feedback_store

    store, _feedback = wired

    class BrokenStore:
        def set_feedback(self, run_id: str, value: str) -> Any:
            raise FeedbackPersistenceError("feedback storage could not persist")

        def get_feedback(self, run_id: str) -> None:
            return None

        def list_feedback(self) -> list:
            return []

    http_app.dependency_overrides[get_feedback_store] = lambda: BrokenStore()
    _seed_thread_with_run(store)

    r = client.post(_feedback_url(), json={"value": "up"})

    assert r.status_code == 503
    assert r.json() == {"error": "feedback_persistence_failed"}


def test_get_history_degrades_gracefully_when_feedback_store_fails(
    http_app: FastAPI, client: TestClient, wired
) -> None:
    from fin.host.http import get_feedback_store

    store, _feedback = wired

    class BrokenStore:
        def set_feedback(self, run_id: str, value: str) -> Any:
            raise FeedbackPersistenceError("feedback storage could not persist")

        def get_feedback(self, run_id: str) -> None:
            return None

        def list_feedback(self) -> list:
            raise FeedbackPersistenceError("feedback storage could not list")

    http_app.dependency_overrides[get_feedback_store] = lambda: BrokenStore()
    _seed_thread_with_run(store)

    r = client.get("/api/threads/t-1")

    # A thumbs outage must not blank the turn table: the history
    # still serves, with feedback simply absent (null).
    assert r.status_code == 200
    assert r.json()["turns"][0]["feedback"] is None


# ----- default store selection (mirrors default_thread_store) -----


def test_default_feedback_store_env_unset_is_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host import feedback as feedback_module

    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.setattr(feedback_module, "_DEFAULT_FEEDBACK_STORE_CACHE", {})

    store = feedback_module.default_feedback_store()

    assert isinstance(store, InMemoryFeedbackStore)


def test_default_feedback_store_env_set_is_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host import db_pool
    from fin.host import feedback as feedback_module
    from fin.host.feedback import PostgresFeedbackStore

    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setattr(feedback_module, "_DEFAULT_FEEDBACK_STORE_CACHE", {})

    class FakePool:
        def __init__(self, url: str) -> None:
            self.url = url
            self.opened = False

        def open(self) -> None:
            self.opened = True

        def close(self) -> None:
            self.opened = False

        def connection(self):
            # Satisfy ``PostgresFeedbackStore(initialize=True)``'s
            # schema DDL without a real database.
            return contextlib.nullcontext(
                types.SimpleNamespace(execute=lambda *a, **k: None, commit=lambda: None)
            )

    created: list[FakePool] = []

    def fake_create_pool(url: str, **_kwargs: Any) -> FakePool:
        pool = FakePool(url)
        created.append(pool)
        return pool

    monkeypatch.setattr(db_pool, "create_postgres_pool", fake_create_pool)

    store = feedback_module.default_feedback_store()

    assert isinstance(store, PostgresFeedbackStore)
    assert created and created[0].opened
