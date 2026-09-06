"""The ``/api/workspaces`` surface is UNSHIPPED (spec SC4 / SC7, Round 3).

The 2026-09-06 single-default amendment (ADR 0049 amendment) retracted
the T2 multi-root API: no ``GET /api/workspaces`` list, no
``POST /api/workspaces/bind``, no ``POST /api/workspaces/create``.
There is one global default 工作区 (``$HOME/.fin/default``) and
multiplicity is research threads, not filesystem folders. The SPA
must not grow a picker either.

What this suite pins:

* absence — the three routes answer 404 with a JSON error body (not
  the SPA catch-all's 200-with-index.html), and bind/create do NOT
  create ``$HOME/.fin/workspaces/<slug>`` or ``known_roots.json`` for
  ANY request shape;
* presence — ``default_workspace_root`` still resolves + mkdirs
  ``$HOME/.fin/default`` (kept helper, spec Focus 2 / SC4);
* inject seam end-to-end (spec SC8) — one scripted send injects the
  resolved default 工作区 into ``configurable.workspace_root``.

Boundary classes: empty (fresh home -> default created), negative
(routes absent / 404), exception (no slug path ever created).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.workspace_registry import DEFAULT_ROOT_NAME, default_workspace_root


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """An isolated ``$HOME`` under ``tmp_path``.

    The suite never touches the operator's real home: the
    ``$HOME``-reading dependencies resolve via
    ``default_workspace_root_for_process``, which reads ``HOME`` at
    call time — monkeypatched here.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def http_app(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The prod app with ``HOME`` pointed at ``fake_home``."""
    from fin.host.http import app

    monkeypatch.setenv("HOME", str(fake_home))
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


# ---------------------------------------------------------------------------
# Negative — the bind/create/list surface is gone (404, not SPA 200).
# ---------------------------------------------------------------------------


def test_get_workspaces_is_absent(client: TestClient) -> None:
    """``GET /api/workspaces`` no longer exists. The SPA catch-all is
    mounted only when ``web/dist`` exists, so in this suite the
    unmounted path must answer 404 with FastAPI's JSON error body —
    never a 200 and never the old JSON list contract."""
    r = client.get("/api/workspaces")
    assert r.status_code == 404
    body = r.json()
    assert not isinstance(body, list)
    assert body != {"error": "workspace_list"}


def test_bind_route_is_absent(client: TestClient, fake_home: Path) -> None:
    target = fake_home / "external"
    target.mkdir()

    r = client.post("/api/workspaces/bind", json={"path": str(target)})

    assert r.status_code == 404
    # The old named-4xx contract must not survive under any answer.
    assert r.json() != {"error": "workspace_not_found"}


def test_create_route_is_absent(client: TestClient, fake_home: Path) -> None:
    r = client.post("/api/workspaces/create", json={"slug": "team-b"})

    assert r.status_code == 404
    assert r.json() != {"error": "workspace_slug_empty"}


def test_bind_with_traversal_slug_creates_nothing(
    client: TestClient, fake_home: Path
) -> None:
    """No slug path may be created under ``$HOME/.fin/workspaces`` for
    any request — including traversal attempts."""
    r = client.post("/api/workspaces/bind", json={"path": "../escape"})
    assert r.status_code == 404

    assert not (fake_home / ".fin" / "workspaces").exists()


def test_create_with_traversal_slug_creates_nothing(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": "../escape"})
    assert r.status_code == 404

    assert not (fake_home / ".fin" / "workspaces").exists()


def test_create_with_valid_slug_creates_nothing(
    client: TestClient, fake_home: Path
) -> None:
    """Even a perfectly legal slug must not materialize a directory:
    the route itself is gone, not merely restricted."""
    r = client.post("/api/workspaces/create", json={"slug": "team-b"})
    assert r.status_code == 404

    assert not (fake_home / ".fin" / "workspaces").exists()


def test_no_registry_file_is_created_by_any_workspaces_request(
    client: TestClient, fake_home: Path
) -> None:
    """``known_roots.json`` is unshipped: no workspaces request may
    create it."""
    client.get("/api/workspaces")
    client.post("/api/workspaces/bind", json={"path": str(fake_home)})
    client.post("/api/workspaces/create", json={"slug": "alpha"})

    assert not (fake_home / ".fin" / "known_roots.json").exists()


def test_no_slug_route_on_the_app(client: TestClient) -> None:
    """No route accepts a slug path parameter (SC4: absent, not
    restricted)."""
    from fin.host.http import app

    paths = [getattr(r, "path", "") for r in app.routes]
    assert not any("slug" in p for p in paths), (
        f"route with a slug parameter found: {[p for p in paths if 'slug' in p]}"
    )


# ---------------------------------------------------------------------------
# Empty — the default 工作区 still resolves + mkdirs (kept helper).
# ---------------------------------------------------------------------------


def test_default_workspace_root_resolves_and_mkdirs(fake_home: Path) -> None:
    target = fake_home / ".fin" / DEFAULT_ROOT_NAME
    assert not target.exists()

    root = default_workspace_root(fake_home)

    assert root == target
    assert target.is_dir()


# ---------------------------------------------------------------------------
# Inject seam end-to-end (SC8) — one scripted send injects the
# resolved default 工作区 from the process seam.
# ---------------------------------------------------------------------------


class _ConfigCapturingGraph:
    def __init__(self) -> None:
        self.configs: list[dict] = []

    def invoke(self, input: dict, config: dict | None = None, **kwargs):
        self.configs.append(config or {})
        return {"status": "done", "answer": "", "citations": ()}

    def stream(self, input, config: dict | None = None, **kwargs):
        self.configs.append(config or {})
        return iter(())


def test_send_injects_default_workspace_root_from_process_home(
    client: TestClient,
    http_app: FastAPI,
    fake_home: Path,
) -> None:
    """No dependency override for the root: the send route resolves
    ``$HOME/.fin/default`` through the real process seam
    (``default_workspace_root_for_process`` -> ``default_workspace_root``)
    and stamps it into ``configurable.workspace_root``."""
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    g = _ConfigCapturingGraph()
    http_app.dependency_overrides[get_graph] = lambda: g
    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store

    record = store.create_thread(title="inject-default")
    thread_id = record.id

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "hello"},
    ) as response:
        for _ in response.iter_text():
            pass

    assert g.configs, "graph must have been invoked at least once"
    expected = default_workspace_root(fake_home)
    for config in g.configs:
        configurable = config.get("configurable") or {}
        assert configurable.get("workspace_root") == str(expected)
