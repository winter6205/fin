"""Resolve the one runtime home for settings, profile, logs, and ledgers.

An empty or relative ``FIN_HOME`` is an error. Falling back to ``~/.fin``
would write a linked worktree's state into the operator's production home.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


class FinHomeError(Exception):
    """``FIN_HOME`` is set but is not an absolute path."""


def checkout_root() -> Path:
    """The checkout that contains this module."""
    return _CHECKOUT_ROOT


def is_linked_worktree(root: Path) -> bool:
    """A linked worktree's ``.git`` is a file, not the primary git directory."""
    return (Path(root) / ".git").is_file()


def resolve_fin_home(
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    user_home: Path | None = None,
) -> Path:
    """Absolute ``FIN_HOME``, else the worktree home, else ``<user>/.fin``."""
    source = os.environ if environ is None else environ
    if "FIN_HOME" in source:
        return _absolute_home(source["FIN_HOME"])
    root = Path(repo_root) if repo_root is not None else checkout_root()
    if is_linked_worktree(root):
        return root / ".fin-home"
    return _user_home(user_home) / ".fin"


def profile_path(fin_home: Path | str | None = None) -> Path:
    """``<FIN_HOME>/user.md``. The graph home block can call this directly."""
    root = _given_or_resolved(fin_home)
    return root / "user.md"


def run_dir(fin_home: Path | str | None = None) -> Path:
    """Pid, log, and ready files live here, not in the checkout."""
    return _given_or_resolved(fin_home) / "run"


def corpus_dir(fin_home: Path | str | None = None) -> Path:
    """Ledger defaults under a home. Callers must not copy an existing corpus."""
    return _given_or_resolved(fin_home) / "corpus"


def _absolute_home(raw: str) -> Path:
    text = raw.strip()
    if text == "":
        raise FinHomeError("FIN_HOME is empty")
    path = Path(text)
    if not path.is_absolute():
        raise FinHomeError("FIN_HOME must be an absolute path")
    return path


def _user_home(user_home: Path | None) -> Path:
    if user_home is not None:
        return Path(user_home)
    return Path.home()


def _given_or_resolved(fin_home: Path | str | None) -> Path:
    if fin_home is not None:
        return Path(fin_home)
    return resolve_fin_home()


__all__ = [
    "FinHomeError",
    "checkout_root",
    "corpus_dir",
    "is_linked_worktree",
    "profile_path",
    "resolve_fin_home",
    "run_dir",
]
