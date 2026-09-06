"""Default 工作区 resolution (spec Focus 2, Round 3 single-default).

There is exactly ONE 工作区: ``$HOME/.fin/default`` (mkdir if
missing). The T2 multi-root registry (a persisted JSON file of bound
roots with bind / create operations) was unshipped per the
2026-09-06 amendment (spec SC4 / ADR 0049 amendment): multiplicity
is research threads, not filesystem folders. This module now only
owns the SSOT for the default-root shape that the HTTP/CLI inject
seam resolves (``default_workspace_root_for_process`` in
``fin.host.workspace_http`` and ``fin.host.cli`` both delegate
here).

The functions in this module never call ``Path.cwd()`` (spec Focus 2
+ SC3: never `Path.cwd()`). They take an explicit ``home: Path``
argument so the suite can fully isolate from the real home.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (single source of truth for the on-disk layout).
# ---------------------------------------------------------------------------

#: Directory under ``<home>/`` that holds fin config.
_FIN_DIR: str = ".fin"

#: Directory name of the default 工作区 under ``<home>/.fin/``.
DEFAULT_ROOT_NAME: str = "default"


# ---------------------------------------------------------------------------
# Default 工作区 resolution.
# ---------------------------------------------------------------------------


def default_workspace_root(home: Path | str) -> Path:
    """Return the default 工作区 directory under ``<home>/.fin/default``.

    The directory is created if missing so callers can write into it
    without a separate mkdir (spec Focus 2: "missing bind → default,
    mkdir").

    ``home`` is an explicit argument; the function NEVER reads
    ``$HOME`` or calls ``Path.home()``. Tests inject ``tmp_path`` so
    the real home directory is never touched.
    """
    path = Path(home) / _FIN_DIR / DEFAULT_ROOT_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "DEFAULT_ROOT_NAME",
    "default_workspace_root",
]
