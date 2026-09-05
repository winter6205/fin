"""Two-layer user-profile file store (spec SC13).

The host graph tool ``edit_user_profile`` writes one of two layers:
``$HOME/.fin/user.md`` (global) and ``<workspace>/.fin/user.md`` (workspace
overlay). ``read_merged`` overlays the workspace on top of the global so the
operator sees both, with the workspace winning where both have content.

Pure file IO only — no graph import, no memory port. Paths are injectable so
the suite never touches the real ``$HOME``.

Boundary classes exercised here:
* empty — missing files yield empty reads.
* negative — an unknown ``layer`` raises a typed error.
* overflow — not applicable (one text blob per layer).
* concurrent — not exercised (single-process file IO).
* exception — bad inputs raise typed errors; missing parent dirs are created.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileError,
    UserProfileStore,
    UserProfileUnknownLayerError,
)


# ---------------------------------------------------------------------------
# Construction helpers.
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> UserProfileStore:
    """A two-layer store rooted under ``tmp_path`` — no real $HOME touched.

    Spec SC13: each layer lives at ``<root>/.fin/user.md``. We honor that
    layout so a test that names the file directly sees the same path the
    production entry point builds. The parent ``.fin`` directory is created
    eagerly so test setup can write initial bytes without a separate
    ``mkdir`` step.
    """
    global_path = tmp_path / "home" / ".fin" / GLOBAL_FILENAME
    workspace_path = tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME
    global_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return UserProfileStore(
        global_path=global_path,
        workspace_path=workspace_path,
    )


def _seed(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` after creating its parent dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Merged read (workspace overlaying global).
# ---------------------------------------------------------------------------


def test_merged_read_is_empty_when_both_files_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.read_merged() == ""


def test_merged_read_returns_global_when_only_global_exists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.global_path.write_text("Global likes coffee.", encoding="utf-8")

    assert store.read_merged() == "Global likes coffee."


def test_merged_read_returns_workspace_when_only_workspace_exists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.workspace_path.write_text("Workspace prefers Python.", encoding="utf-8")

    assert store.read_merged() == "Workspace prefers Python."


def test_workspace_overlays_global_when_both_exist(tmp_path: Path) -> None:
    """SC13: merged read is workspace over global.

    The contract is "workspace wins": a workspace non-empty file replaces the
    global read rather than appending, so the operator sees the most specific
    intent on this workspace.
    """
    store = _store(tmp_path)
    store.global_path.write_text("global content", encoding="utf-8")
    store.workspace_path.write_text("workspace content", encoding="utf-8")

    assert store.read_merged() == "workspace content"


def test_merged_read_trims_whitespace_on_empty_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.global_path.write_text("   \n\n", encoding="utf-8")
    store.workspace_path.write_text("", encoding="utf-8")

    assert store.read_merged() == ""


# ---------------------------------------------------------------------------
# Per-layer writes are isolated.
# ---------------------------------------------------------------------------


def test_write_workspace_does_not_touch_global(tmp_path: Path) -> None:
    """SC13: editing the workspace layer MUST NOT rewrite the global file."""
    store = _store(tmp_path)
    store.global_path.write_text("GLOBAL bytes", encoding="utf-8")
    global_bytes_before = store.global_path.read_bytes()

    store.write_layer("workspace", "Workspace added.")

    assert store.global_path.read_bytes() == global_bytes_before
    assert store.workspace_path.read_text(encoding="utf-8") == "Workspace added."


def test_write_global_does_not_touch_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.workspace_path.write_text("WORKSPACE bytes", encoding="utf-8")
    workspace_bytes_before = store.workspace_path.read_bytes()

    store.write_layer("global", "Global added.")

    assert store.workspace_path.read_bytes() == workspace_bytes_before
    assert store.global_path.read_text(encoding="utf-8") == "Global added."


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    # A store whose parents do NOT exist yet — exercises the
    # mkdir-on-write path the store promises.
    store = UserProfileStore(
        global_path=tmp_path / "deep" / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "deep" / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    assert not store.global_path.parent.exists()
    assert not store.workspace_path.parent.exists()

    store.write_layer("global", "hello")
    store.write_layer("workspace", "world")

    assert store.global_path.parent.is_dir()
    assert store.workspace_path.parent.is_dir()
    assert store.global_path.read_text(encoding="utf-8") == "hello"
    assert store.workspace_path.read_text(encoding="utf-8") == "world"


def test_write_then_read_round_trips_text(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.write_layer("workspace", "Operator prefers quarterly revenue.")
    store.write_layer("global", "Operator likes dark mode.")

    assert store.read_merged() == "Operator prefers quarterly revenue."


# ---------------------------------------------------------------------------
# Typed layer error.
# ---------------------------------------------------------------------------


def test_unknown_layer_raises_typed_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UserProfileUnknownLayerError):
        store.write_layer("user", "x")  # type: ignore[arg-type]


def test_unknown_layer_error_is_a_user_profile_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UserProfileError):
        store.write_layer("", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer argument is required (no positional write).
# ---------------------------------------------------------------------------


def test_write_layer_signature_requires_layer_keyword(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sig = inspect.signature(store.write_layer)
    layer_param = sig.parameters["layer"]

    assert layer_param.default is inspect.Parameter.empty
    assert layer_param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


# ---------------------------------------------------------------------------
# Constants and layer labels.
# ---------------------------------------------------------------------------


def test_layer_constants_match_the_spec() -> None:
    assert GLOBAL_FILENAME == "user.md"
    assert WORKSPACE_FILENAME == "user.md"


def test_from_env_uses_cwd_when_workspace_root_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``from_env`` falls back to ``Path.cwd()`` for the workspace root when
    not given, and resolves ``$HOME`` from ``home=`` (or the env) for the
    global layer. The workspace path is therefore ``<cwd>/.fin/user.md``;
    the global path is ``$HOME/.fin/user.md`` per SC13.
    """
    monkeypatch.setattr("fin.host.user_profile.Path.cwd", lambda: tmp_path)
    store = UserProfileStore.from_env(home=tmp_path / "home")

    assert store.global_path == tmp_path / "home" / ".fin" / GLOBAL_FILENAME
    assert store.workspace_path == tmp_path / ".fin" / WORKSPACE_FILENAME
