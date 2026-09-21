"""Repo-hygiene contracts that a green product suite cannot see.

The suite here exists because of a real incident, not a style preference.
A worktree that shares the main checkout's `node_modules` through a
symlink of that exact name could commit the link: `.gitignore` carried
`node_modules/`, and a **trailing slash matches only a real directory**,
so a symlink by that name was never ignored. a private commit (that pull request) committed
such a link pointing at itself — blob content
`/home/winner/projects/fin/node_modules` — and from then on every
`npm run` in a checkout carrying it died with ELOOP (exit 216), which is
why husky's `pre-commit` failed for reasons that had nothing to do with
the staged change. 082fddf removed the entry from `main`; 44b397f did the
same on one branch.

The two halves below are deliberately separate contracts:

* the RULE ignores a `node_modules` of any shape, at any depth
* no TRACKED path has a `node_modules` component, so removing the
  trailing slash cannot start ignoring something a checkout needs

A fix for one half alone does not hold: the rule alone cannot evict an
entry that is already tracked (`.gitignore` does not apply to tracked
paths), and the tracked-path check alone would not stop the link from
being re-added by the next `git add -A` in a worktree that has one.

Both checks are hermetic: the rule check asks `git check-ignore` about
paths that do not exist, so a developer's local `node_modules` (directory
or symlink) cannot change the answer, and nothing here writes to the
working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Shapes a share-the-main-checkout worktree puts on disk. The first is the
# one that burned: a symlink whose name is exactly a rule in .gitignore.
# The third is the control case — the rule must not stop at the root.
NODE_MODULES_SHAPES = ("node_modules", "web/node_modules", "a/b/node_modules")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _ignore_verdict(relative_path: str) -> str:
    """The rule that ignores ``relative_path``, or ``""`` if none does.

    ``--no-index`` is what makes this hermetic: without it `git
    check-ignore` consults the working tree, and the answer would flip
    with whatever a given machine happens to have installed.
    """
    result = _git("check-ignore", "-v", "--no-index", relative_path)
    return result.stdout.strip()


@pytest.mark.parametrize("shape", NODE_MODULES_SHAPES)
def test_rule_ignores_node_modules_by_name(shape: str) -> None:
    """A `node_modules` symlink must be ignored, not just a real directory.

    Regression pin for the trailing-slash hole: with `node_modules/` the
    verdict for a symlink by that name was "not ignored", which is what
    let a private commit commit it.
    """
    assert _ignore_verdict(shape), (
        f"{shape} is NOT ignored — a worktree symlink under this name could be "
        f"committed again. The rule must match the name, not only a real "
        f"directory (see .gitignore's node_modules comment)."
    )


def test_no_tracked_path_has_a_node_modules_component() -> None:
    """No tracked path may carry a `node_modules` component.

    The rule check above is not enough on its own: `.gitignore` cannot
    evict an entry that is already tracked, and on a branch that still
    tracks the link every `npm run` dies with ELOOP.
    """
    result = _git("ls-files", "-z")
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    offenders = [
        path for path in result.stdout.split("\0") if path and "node_modules" in path
    ]
    assert not offenders, (
        "tracked paths contain a node_modules component: "
        + ", ".join(offenders[:10])
        + ". Remove them (git rm --cached) — `.gitignore` does not apply to "
        "tracked files, and a committed self-referencing symlink breaks "
        "every npm run in the checkout with ELOOP."
    )
