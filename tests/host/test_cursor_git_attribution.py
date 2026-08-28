"""cursor-git-attribution: worktree .husky must not get *.cursor.* files."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cursor-git-attribution.sh"


def _git_env(home: Path) -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GIT_CONFIG_GLOBAL"] = str(home / "gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def test_apply_writes_strip_hooks_not_cursor_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    env = _git_env(home)
    subprocess.run(["git", "init"], cwd=repo, env=env, check=True, capture_output=True)
    husky = repo / ".husky"
    husky.mkdir()
    (husky / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )

    names = [p.name for p in husky.iterdir()]
    assert not any("cursor" in name for name in names), names
    commit_msg = husky / "commit-msg"
    prepare = husky / "prepare-commit-msg"
    assert commit_msg.is_file()
    assert prepare.is_file()
    assert (husky / "pre-commit").is_file()
    assert "drop platform lines from COMMIT_EDITMSG" in commit_msg.read_text(
        encoding="utf-8"
    )
