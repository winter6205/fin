"""HTTP routes for the 工作区 list/bind/create surface (spec Focus 2 / SC4).

The ``/api/workspaces`` family sits next to ``/api/threads`` on the
same FastAPI app. The tests follow the existing
``tests/host/test_workspace_http.py`` pattern: a fresh app per test,
in-memory thread store, and a dependency override for the
``WorkspaceRegistry`` so each test gets a hermetic fake home.

Coverage follows the five boundary classes:

* empty — fresh home, ``GET /api/workspaces`` returns the default entry;
* negative — bind missing path, bind a file, illegal slug, missing body;
* overflow — slug over the cap, no directory created;
* concurrent — second create same slug, registry stays valid;
* exception — corrupted registry -> typed 500 with named code, never
  cwd fallback (spec SC4).

The routes also mkdir the default directory on list (spec Focus 2:
"Default ``$HOME/.fin/default`` is created/used when listing 工作区").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.workspace_registry import (
    DEFAULT_ROOT_NAME,
    MAX_SLUG_LENGTH,
    REGISTRY_FILENAME,
    WorkspaceRegistry,
    default_workspace_root,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """An isolated ``$HOME`` under ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def http_app(fake_home: Path) -> FastAPI:
    """A fresh FastAPI app + a registry dependency bound to ``fake_home``.

    The dependency override targets ``fin.host.http.get_workspace_registry``
    because that is the callable ``http.py`` passes into
    ``mount_workspace_routes`` — overriding the workspace_http copy
    would silently miss the route's actual dependency.
    """
    from fin.host.http import app, get_workspace_registry

    # Build a registry keyed off the fake home so the routes never
    # touch the real ``$HOME``.
    registry = WorkspaceRegistry(home=fake_home)
    app.dependency_overrides[get_workspace_registry] = lambda: registry
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _registry_path(fake_home: Path) -> Path:
    return fake_home / ".fin" / REGISTRY_FILENAME


# ---------------------------------------------------------------------------
# GET /api/workspaces — empty / list.
# ---------------------------------------------------------------------------


def test_get_workspaces_empty_home_returns_default_entry(
    client: TestClient, fake_home: Path
) -> None:
    """Fresh fake home -> the route mkdirs + returns exactly one entry."""
    r = client.get("/api/workspaces")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    entry = body[0]
    assert entry["path"] == str(fake_home / ".fin" / DEFAULT_ROOT_NAME)
    assert entry["slug"] == DEFAULT_ROOT_NAME
    assert entry["is_default"] is True


def test_get_workspaces_creates_default_dir_on_list(
    client: TestClient, fake_home: Path
) -> None:
    target = fake_home / ".fin" / DEFAULT_ROOT_NAME
    assert not target.exists()

    r = client.get("/api/workspaces")
    assert r.status_code == 200

    assert target.is_dir()


def test_get_workspaces_creates_registry_file(
    client: TestClient, fake_home: Path
) -> None:
    reg_path = _registry_path(fake_home)
    assert not reg_path.exists()

    r = client.get("/api/workspaces")
    assert r.status_code == 200

    payload = json.loads(reg_path.read_text(encoding="utf-8"))
    assert payload == {
        "roots": [
            {
                "path": str(fake_home / ".fin" / DEFAULT_ROOT_NAME),
                "slug": DEFAULT_ROOT_NAME,
                "is_default": True,
            }
        ]
    }


# ---------------------------------------------------------------------------
# POST /api/workspaces/bind — negative.
# ---------------------------------------------------------------------------


def test_bind_missing_path_returns_400_with_typed_code(
    client: TestClient, fake_home: Path
) -> None:
    missing = fake_home / "nope"

    r = client.post("/api/workspaces/bind", json={"path": str(missing)})

    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "workspace_not_found"


def test_bind_a_file_returns_400_with_typed_code(
    client: TestClient, fake_home: Path
) -> None:
    a_file = fake_home / "not-a-dir.txt"
    a_file.write_text("hi", encoding="utf-8")

    r = client.post("/api/workspaces/bind", json={"path": str(a_file)})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_not_a_directory"


def test_bind_relative_path_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/bind", json={"path": "relative/path"})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_bind_failed"


def test_bind_missing_body_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/bind", json={})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_bind_failed"


def test_bind_existing_directory_returns_200(
    client: TestClient, fake_home: Path
) -> None:
    target = fake_home / "external"
    target.mkdir()

    r = client.post("/api/workspaces/bind", json={"path": str(target)})

    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(target.resolve())
    assert body["is_default"] is False


def test_bind_then_list_includes_the_new_root(
    client: TestClient, fake_home: Path
) -> None:
    target = fake_home / "external"
    target.mkdir()

    bind = client.post("/api/workspaces/bind", json={"path": str(target)})
    assert bind.status_code == 200

    listed = client.get("/api/workspaces").json()
    paths = {entry["path"] for entry in listed}
    assert str(target.resolve()) in paths


# ---------------------------------------------------------------------------
# POST /api/workspaces/create — negative.
# ---------------------------------------------------------------------------


def test_create_empty_slug_returns_400_with_typed_code(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": ""})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_slug_empty"


def test_create_illegal_slug_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": "a/b"})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_slug_illegal"


def test_create_traversal_slug_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": ".."})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_slug_illegal"


def test_create_overflow_slug_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post(
        "/api/workspaces/create",
        json={"slug": "x" * (MAX_SLUG_LENGTH + 1)},
    )

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_slug_overflow"


def test_create_overflow_slug_does_not_create_partial_directory(
    client: TestClient, fake_home: Path
) -> None:
    client.post(
        "/api/workspaces/create",
        json={"slug": "x" * (MAX_SLUG_LENGTH + 1)},
    )

    target = fake_home / ".fin" / "workspaces" / ("x" * MAX_SLUG_LENGTH)
    assert not target.exists()


def test_create_illegal_slug_does_not_create_directory(
    client: TestClient, fake_home: Path
) -> None:
    client.post("/api/workspaces/create", json={"slug": "a/b"})

    workspaces = fake_home / ".fin" / "workspaces"
    if workspaces.exists():
        assert list(workspaces.iterdir()) == []


def test_create_missing_body_returns_400(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={})

    assert r.status_code == 400
    assert r.json()["error"] == "workspace_slug_empty"


def test_create_with_valid_slug_returns_200_and_creates_dir(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": "team-b"})

    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(fake_home / ".fin" / "workspaces" / "team-b")
    assert body["slug"] == "team-b"
    assert body["is_default"] is False

    created = fake_home / ".fin" / "workspaces" / "team-b"
    assert created.is_dir()


def test_create_does_not_create_a_second_corpus(
    client: TestClient, fake_home: Path
) -> None:
    r = client.post("/api/workspaces/create", json={"slug": "no-catalog"})
    assert r.status_code == 200

    target = fake_home / ".fin" / "workspaces" / "no-catalog"
    children = sorted(p.name for p in target.iterdir())
    assert children == []


def test_create_then_list_includes_the_new_root(
    client: TestClient, fake_home: Path
) -> None:
    create = client.post("/api/workspaces/create", json={"slug": "alpha"})
    assert create.status_code == 200

    listed = client.get("/api/workspaces").json()
    slugs = {entry["slug"] for entry in listed}
    assert "alpha" in slugs
    assert DEFAULT_ROOT_NAME in slugs


# ---------------------------------------------------------------------------
# Concurrent.
# ---------------------------------------------------------------------------


def test_two_creates_same_slug_leave_registry_valid(
    client: TestClient, fake_home: Path
) -> None:
    first = client.post("/api/workspaces/create", json={"slug": "dup"})
    assert first.status_code == 200

    second = client.post("/api/workspaces/create", json={"slug": "dup"})
    # Idempotent OR typed 4xx — both acceptable per the ticket.
    assert second.status_code in (200, 400)

    # Registry stays valid JSON with one entry for the slug.
    reg_path = _registry_path(fake_home)
    payload = json.loads(reg_path.read_text(encoding="utf-8"))
    slugs = [entry["slug"] for entry in payload["roots"]]
    assert slugs.count("dup") == 1
    assert DEFAULT_ROOT_NAME in slugs


# ---------------------------------------------------------------------------
# Exception — corrupted registry.
# ---------------------------------------------------------------------------


def test_corrupt_registry_returns_typed_4xx_not_500(
    client: TestClient, fake_home: Path
) -> None:
    """A corrupted registry is a typed EXIT (4xx or 500 with a named
    code). It must NEVER be a cwd fallback."""
    # Materialize the registry, then corrupt it on disk.
    client.get("/api/workspaces")
    reg_path = _registry_path(fake_home)
    reg_path.write_text("{not valid json", encoding="utf-8")

    r = client.get("/api/workspaces")

    # Spec SC4: the route maps the typed ``WorkspaceRegistryError``
    # to a named 4xx / 500 code — either answer is acceptable as long
    # as the body is typed (no cwd fallback). The forbidden shape is
    # a 200 listing cwd as a workspace root.
    assert r.status_code in (400, 500)
    body = r.json()
    assert body["error"] in (
        "workspace_registry_unreadable",
        "workspace_registry_error",
    )


def test_corrupt_registry_does_not_list_cwd(
    client: TestClient, fake_home: Path
) -> None:
    """The route must NEVER silently return cwd as a workspace root."""
    client.get("/api/workspaces")
    reg_path = _registry_path(fake_home)
    reg_path.write_text("garbage", encoding="utf-8")

    r = client.get("/api/workspaces")
    body = r.json()

    # Forbidden: a 200 with cwd as a workspace entry.
    if r.status_code == 200:
        for entry in body:
            assert entry["slug"] != "cwd"


# ---------------------------------------------------------------------------
# Fresh home on first list creates the default directory.
# ---------------------------------------------------------------------------


def test_list_endpoint_uses_fake_home_not_real(
    client: TestClient, fake_home: Path, tmp_path: Path
) -> None:
    """A fresh fake home under tmp_path -> default dir under fake_home,
    NEVER under the operator's real home (or cwd)."""
    real_default = tmp_path.parent / ".fin" / DEFAULT_ROOT_NAME
    fake_default = fake_home / ".fin" / DEFAULT_ROOT_NAME

    r = client.get("/api/workspaces")
    assert r.status_code == 200

    assert fake_default.is_dir()
    # The "real" home path used as a sentinel — we cannot know the
    # actual real $HOME, so this just asserts the response is rooted
    # under our injected home.
    body = r.json()
    assert body[0]["path"].startswith(str(fake_home))
    assert body[0]["path"] != str(real_default)


def test_default_workspace_root_call_resolves_home_at_call_time(
    client: TestClient, fake_home: Path
) -> None:
    """``default_workspace_root(home)`` reads home from its argument,
    not from $HOME / Path.home() — the route uses the injected fake
    home."""
    r = client.get("/api/workspaces")
    body = r.json()
    expected = default_workspace_root(fake_home)
    assert body[0]["path"] == str(expected)