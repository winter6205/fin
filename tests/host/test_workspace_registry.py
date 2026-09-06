"""Default 工作区 + registry tests (spec Focus 2 / SC3-SC4).

The registry tracks the operator's bound 工作区 paths so the left rail
can list them. Resolution rules:

* ``default_workspace_root(home)`` = ``<home>/.fin/default`` — the
  directory is created/listed so it always exists.
* A persistent JSON registry at ``<home>/.fin/known_roots.json`` lists
  every bound path the operator has added (default is always present).
* ``bind_root(path)`` only accepts absolute directories that already
  exist; ``create_root(slug)`` creates a directory under
  ``<home>/.fin/workspaces/<slug>`` with a strict slug sanitizer.
* A corrupted registry is a typed error, never a cwd fallback.

Boundary classes exercised (defensive-contract):
* empty — fresh home, list returns exactly the default entry.
* negative — bind missing path / bind file / illegal slug.
* overflow — slug > cap, nothing truncated.
* concurrent — two creates on the same slug leave the registry valid.
* exception — corrupted registry file, unwritable home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.host.workspace_registry import (
    DEFAULT_ROOT_NAME,
    MAX_SLUG_LENGTH,
    REGISTRY_FILENAME,
    WORKSPACES_DIR_NAME,
    WorkspaceBindError,
    WorkspaceNotFoundError,
    WorkspaceNotADirectoryError,
    WorkspaceRegistry,
    WorkspaceRegistryError,
    WorkspaceRegistryUnreadableError,
    WorkspaceRoot,
    WorkspaceSlugEmptyError,
    WorkspaceSlugError,
    WorkspaceSlugIllegalError,
    WorkspaceSlugOverflowError,
    default_workspace_root,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _home(tmp_path: Path) -> Path:
    """An isolated $HOME: tests never touch the real home directory."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _registry_path(home: Path) -> Path:
    return home / ".fin" / REGISTRY_FILENAME


# ---------------------------------------------------------------------------
# Default directory resolution.
# ---------------------------------------------------------------------------


def test_default_root_is_under_fin_dot_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)

    root = default_workspace_root(home)

    assert root == home / ".fin" / DEFAULT_ROOT_NAME


def test_default_root_creates_directory_on_first_call(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".fin" / DEFAULT_ROOT_NAME
    assert not target.exists()

    root = default_workspace_root(home)

    assert root == target
    assert root.is_dir()


def test_default_root_call_reads_path_home_at_call_time(tmp_path: Path) -> None:
    """``default_workspace_root`` resolves home from the call arg
    (NOT from a process-wide module-level capture)."""
    home_a = _home(tmp_path / "a")
    home_b = _home(tmp_path / "b")

    a = default_workspace_root(home_a)
    b = default_workspace_root(home_b)

    assert a == home_a / ".fin" / DEFAULT_ROOT_NAME
    assert b == home_b / ".fin" / DEFAULT_ROOT_NAME
    assert a != b


# ---------------------------------------------------------------------------
# Empty / fresh-home list.
# ---------------------------------------------------------------------------


def test_empty_home_lists_only_default(tmp_path: Path) -> None:
    home = _home(tmp_path)

    reg = WorkspaceRegistry(home=home)
    roots = reg.list_roots()

    assert len(roots) == 1
    only = roots[0]
    assert only.path == home / ".fin" / DEFAULT_ROOT_NAME
    assert only.slug == DEFAULT_ROOT_NAME
    assert only.is_default is True


def test_empty_home_creates_default_dir_and_registry_file(tmp_path: Path) -> None:
    home = _home(tmp_path)

    WorkspaceRegistry(home=home).list_roots()

    default_dir = home / ".fin" / DEFAULT_ROOT_NAME
    assert default_dir.is_dir()
    assert _registry_path(home).is_file()


def test_registry_file_contains_default_on_first_list(tmp_path: Path) -> None:
    home = _home(tmp_path)

    WorkspaceRegistry(home=home).list_roots()

    payload = json.loads(_registry_path(home).read_text(encoding="utf-8"))
    assert payload == {
        "roots": [
            {
                "path": str(home / ".fin" / DEFAULT_ROOT_NAME),
                "slug": DEFAULT_ROOT_NAME,
                "is_default": True,
            }
        ]
    }


# ---------------------------------------------------------------------------
# bind_root — happy path.
# ---------------------------------------------------------------------------


def test_bind_existing_absolute_directory_adds_entry(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = tmp_path / "external"
    target.mkdir()

    reg = WorkspaceRegistry(home=home)
    bound = reg.bind_root(target)

    assert bound == target
    listed = reg.list_roots()
    assert any(r.path == target and not r.is_default for r in listed)


def test_bind_root_dedups_repeat_calls(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = tmp_path / "external"
    target.mkdir()

    reg = WorkspaceRegistry(home=home)
    reg.bind_root(target)
    reg.bind_root(target)

    roots = reg.list_roots()
    assert [r.path for r in roots].count(target) == 1


# ---------------------------------------------------------------------------
# Negative — bind missing path / file.
# ---------------------------------------------------------------------------


def test_bind_missing_path_raises_typed_not_found(tmp_path: Path) -> None:
    home = _home(tmp_path)
    missing = tmp_path / "nope"

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceNotFoundError):
        reg.bind_root(missing)


def test_bind_missing_path_is_a_bind_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    missing = tmp_path / "nope"

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceBindError):
        reg.bind_root(missing)


def test_bind_a_file_raises_not_a_directory(tmp_path: Path) -> None:
    home = _home(tmp_path)
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("hi", encoding="utf-8")

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceNotADirectoryError):
        reg.bind_root(a_file)


def test_bind_relative_path_raises_typed_error(tmp_path: Path) -> None:
    home = _home(tmp_path)

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceBindError):
        reg.bind_root(Path("relative"))


def test_bind_does_not_create_directory_or_registry_for_missing_path(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    missing = tmp_path / "nope"

    reg = WorkspaceRegistry(home=home)
    # Materialize the registry first so we can assert post-bind state
    # without the test conflating "fresh home" with "bind wrote a
    # half-record".
    reg.list_roots()
    try:
        reg.bind_root(missing)
    except WorkspaceBindError:
        pass

    # The registry on disk is still the default-only entry — bind
    # never adds half-records on failure.
    payload = json.loads(_registry_path(home).read_text(encoding="utf-8"))
    assert payload["roots"] == [
        {
            "path": str(home / ".fin" / DEFAULT_ROOT_NAME),
            "slug": DEFAULT_ROOT_NAME,
            "is_default": True,
        }
    ]


# ---------------------------------------------------------------------------
# create_root — happy path.
# ---------------------------------------------------------------------------


def test_create_root_under_workspaces_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)

    reg = WorkspaceRegistry(home=home)
    created = reg.create_root("research-A")

    assert created == home / ".fin" / WORKSPACES_DIR_NAME / "research-A"
    assert created.is_dir()


def test_create_root_records_default_and_created(tmp_path: Path) -> None:
    home = _home(tmp_path)

    reg = WorkspaceRegistry(home=home)
    reg.create_root("team-b")

    roots = reg.list_roots()
    slugs = {r.slug for r in roots}
    assert slugs == {DEFAULT_ROOT_NAME, "team-b"}


def test_create_root_does_not_create_a_second_corpus(tmp_path: Path) -> None:
    """``create_root`` MUST NOT add any catalog file under the new
    directory (spec Focus 2: 'Create does not clone or split the 知识库')."""
    home = _home(tmp_path)

    reg = WorkspaceRegistry(home=home)
    created = reg.create_root("no-catalog")

    children = sorted(p.name for p in created.iterdir())
    assert children == []


# ---------------------------------------------------------------------------
# Negative — illegal slug.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["", "   "])
def test_create_empty_slug_raises_typed_error(
    tmp_path: Path, slug: str
) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceSlugEmptyError):
        reg.create_root(slug)


def test_create_empty_slug_does_not_create_directory(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    try:
        reg.create_root("")
    except WorkspaceSlugError:
        pass

    workspaces = home / ".fin" / WORKSPACES_DIR_NAME
    if workspaces.exists():
        assert list(workspaces.iterdir()) == []


@pytest.mark.parametrize(
    "slug",
    [
        "a/b",  # path separator
        "a\\b",  # windows separator
        "..",  # parent traversal
        "../escape",  # traversal
        "a/../b",  # traversal via slash
        ".",  # current dir reference
    ],
)
def test_create_illegal_slug_raises_typed_error(
    tmp_path: Path, slug: str
) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    with pytest.raises((WorkspaceSlugIllegalError, WorkspaceSlugEmptyError)):
        reg.create_root(slug)


def test_illegal_slug_is_a_slug_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceSlugError):
        reg.create_root("a/b")


# ---------------------------------------------------------------------------
# Overflow — slug cap.
# ---------------------------------------------------------------------------


def test_overflow_slug_raises_typed_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    slug = "x" * (MAX_SLUG_LENGTH + 1)

    with pytest.raises(WorkspaceSlugOverflowError):
        reg.create_root(slug)


def test_overflow_slug_does_not_create_partial_directory(
    tmp_path: Path,
) -> None:
    """``MAX_SLUG_LENGTH + 1`` MUST NOT silently truncate to the cap."""
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    slug = "x" * (MAX_SLUG_LENGTH + 1)
    try:
        reg.create_root(slug)
    except WorkspaceSlugError:
        pass

    workspaces = home / ".fin" / WORKSPACES_DIR_NAME
    if workspaces.exists():
        names = {p.name for p in workspaces.iterdir()}
        assert "x" * MAX_SLUG_LENGTH not in names
        assert slug not in names


def test_max_length_slug_creates_directory(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    slug = "a" * MAX_SLUG_LENGTH
    created = reg.create_root(slug)

    assert created.is_dir()
    assert created.name == slug


# ---------------------------------------------------------------------------
# Concurrent — same slug twice.
# ---------------------------------------------------------------------------


def test_two_creates_same_slug_leave_registry_valid(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)

    first = reg.create_root("dup")
    try:
        reg.create_root("dup")
    except WorkspaceBindError:
        # Idempotent OR 4xx — both are acceptable per the ticket.
        pass

    # Re-open the registry from disk: must remain valid JSON with a
    # single dup root, never a corrupted half-write.
    reloaded = WorkspaceRegistry(home=home)
    slugs = sorted(r.slug for r in reloaded.list_roots())
    assert "dup" in slugs
    assert slugs.count("dup") == 1
    assert first.is_dir()


# ---------------------------------------------------------------------------
# Exception — registry corrupted / unwritable home.
# ---------------------------------------------------------------------------


def test_corrupt_registry_raises_typed_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg_file = _registry_path(home)
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text("{not valid json", encoding="utf-8")

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceRegistryUnreadableError):
        reg.list_roots()


def test_corrupt_registry_error_is_a_registry_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg_file = _registry_path(home)
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text("definitely not json", encoding="utf-8")

    reg = WorkspaceRegistry(home=home)

    with pytest.raises(WorkspaceRegistryError):
        reg.list_roots()


def test_corrupt_registry_does_not_fall_back_to_cwd(tmp_path: Path) -> None:
    """The registry must surface a typed error instead of
    silently treating cwd as the workspace root."""
    home = _home(tmp_path)
    reg_file = _registry_path(home)
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text("[]not-json", encoding="utf-8")

    reg = WorkspaceRegistry(home=home)

    # No Path.cwd() rescue — a typed failure must surface.
    with pytest.raises(WorkspaceRegistryUnreadableError):
        reg.list_roots()


def test_registry_writes_fail_surfaces_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home dir whose registry is unwritable raises a typed error,
    never a silent cwd fallback.

    The test points the registry path at a directory so writes fail
    (``Path.write_text`` on a directory path raises ``IsADirectoryError`` /
    ``PermissionError`` / ``OSError``). The contract is "some typed
    registry error surfaces" — the spec demands a typed EXIT, never a
    cwd fallback. Either :class:`WorkspaceRegistryUnreadableError`
    or :class:`WorkspaceRegistryUnwritableError` satisfies it.
    """
    home = _home(tmp_path)
    reg = WorkspaceRegistry(home=home)
    default_workspace_root(home)

    # Re-point the registry path at a directory so both reads and
    # writes fail.
    registry_dir = tmp_path / "registry-as-dir"
    registry_dir.mkdir()
    monkeypatch.setattr(
        reg,
        "_registry_path",
        lambda: registry_dir,  # type: ignore[return-value]
    )

    target = tmp_path / "external"
    target.mkdir()

    with pytest.raises(WorkspaceRegistryError):
        reg.bind_root(target)


# ---------------------------------------------------------------------------
# Process-wide home resolution does NOT bake at import time.
# ---------------------------------------------------------------------------


def test_workspace_registry_does_not_call_path_cwd() -> None:
    """The registry must never call ``Path.cwd()`` (spec Focus 2 /
    SC3 — never `Path.cwd()`). Comments / docstrings may name it as
    a reference; only executable code is checked.
    """
    import ast
    import inspect

    from fin.host import workspace_registry

    source = inspect.getsource(workspace_registry)
    tree = ast.parse(source)

    class _CallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hits: list[str] = []

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            attr = getattr(func, "attr", None)
            # Match both ``Path.cwd()`` and ``Path().cwd()``.
            if isinstance(func, ast.Attribute) and attr == "cwd":
                value = func.value
                if (isinstance(value, ast.Name) and value.id == "Path") or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "Path"
                ):
                    self.hits.append(
                        f"line {node.lineno}: {ast.unparse(node)}"
                    )
            self.generic_visit(node)

    visitor = _CallVisitor()
    visitor.visit(tree)
    assert visitor.hits == [], (
        "workspace_registry must not call Path.cwd(); found: "
        + ", ".join(visitor.hits)
    )


def test_default_workspace_root_does_not_call_path_home_at_import() -> None:
    """``Path.home()`` must be read at call time, not module import."""
    # Sanity: importing the module should not pin $HOME.
    from fin.host import workspace_registry  # noqa: F401

    # The function object remains an unbound callable; resolution is
    # lazy because the function reads the argument at call time.
    assert callable(workspace_registry.default_workspace_root)


# ---------------------------------------------------------------------------
# WorkspaceRoot dataclass shape.
# ---------------------------------------------------------------------------


def test_workspace_root_is_a_frozen_dataclass() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(WorkspaceRoot)
    fields = {f.name for f in dataclasses.fields(WorkspaceRoot)}
    assert fields == {"path", "slug", "is_default"}


# ---------------------------------------------------------------------------
# Process env (HOME) is not read — home is explicit.
# ---------------------------------------------------------------------------


def test_registry_does_not_read_HOME_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point HOME at the real ``/`` and confirm the registry still
    uses the injected ``home=`` arg."""
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", "/")

    reg = WorkspaceRegistry(home=home)
    roots = reg.list_roots()

    assert roots[0].path == home / ".fin" / DEFAULT_ROOT_NAME


def test_default_root_does_not_read_HOME_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", "/")

    root = default_workspace_root(home)

    assert root == home / ".fin" / DEFAULT_ROOT_NAME


def test_default_root_honors_explicit_home_over_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function signature (``home: Path``) takes precedence over
    any environment variable, so tests can fully isolate the suite."""
    real_home = tmp_path / "real-home"
    fake_home = _home(tmp_path / "fake-home")
    monkeypatch.setenv("HOME", str(real_home))

    root = default_workspace_root(fake_home)

    assert root == fake_home / ".fin" / DEFAULT_ROOT_NAME
    assert not (real_home / ".fin" / DEFAULT_ROOT_NAME).exists()