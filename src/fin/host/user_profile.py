"""Two-layer user-profile file store for the host graph's edit tool.

``specs/cross-session-operator-memory.md`` SC13: ``$HOME/.fin/user.md`` is the
global layer and ``<workspace>/.fin/user.md`` is the workspace overlay. The
merged read shows workspace-over-global: a non-empty workspace file replaces
the global read, so the operator sees the most specific intent on the active
workspace. Writing one layer never touches the other.

The store is pure file IO. No graph import, no memory port, no thread-locking
beyond the single write call. Paths are injected at construction so tests
never touch the real ``$HOME``; :meth:`from_env` resolves the same paths from
the environment for the production entry point.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

# Same filename on both layers per spec SC13.
GLOBAL_FILENAME = "user.md"
WORKSPACE_FILENAME = "user.md"

# Both layers live one directory down from their root so they cannot collide
# with workspace roots that contain a literal ``.fin`` folder.
_LAYER_DIR = ".fin"

LayerName = Literal["global", "workspace"]
_LAYERS: tuple[LayerName, ...] = ("global", "workspace")


class UserProfileError(Exception):
    """Base class for typed user-profile failures."""


class UserProfileUnknownLayerError(UserProfileError):
    """A ``write_layer`` call passed a name that is neither ``global`` nor
    ``workspace``. The error names the offending layer so the operator
    sees exactly which argument failed.
    """

    def __init__(self, layer: str) -> None:
        super().__init__(
            f"unknown user-profile layer {layer!r}; expected one of "
            f"{list(_LAYERS)}"
        )
        self.layer = layer


class UserProfileStore:
    """Two-layer user-profile file store.

    Construction takes explicit paths so tests never touch the real ``$HOME``
    and the workspace layer is keyed off the resolved workspace root. Both
    files are independent on disk: writing one never rewrites the other.
    """

    def __init__(
        self,
        *,
        global_path: Path | str,
        workspace_path: Path | str,
    ) -> None:
        self.global_path = Path(global_path)
        self.workspace_path = Path(workspace_path)

    # ----- read -----

    def read_merged(self) -> str:
        """Return the workspace-overlay-global view as a single text blob.

        Contract: a non-empty workspace file replaces the global read; an
        empty workspace falls back to the global; both empty → ``""``.
        Whitespace-only files are treated as empty so a stray newline does
        not silently shadow the global layer (both sides are trimmed
        uniformly — a whitespace-only workspace falls back to global, and
        a whitespace-only global reads as ``""`` rather than a string of
        spaces).

        The returned text is bytes-exact from disk when the layer was
        selected — no header, no marker, no merge separator — so the
        model-facing payload stays stable across identical writes
        (SPEC SC13).
        """
        workspace_text = _read_text_or_empty(self.workspace_path)
        if workspace_text.strip():
            return workspace_text
        global_text = _read_text_or_empty(self.global_path)
        if global_text.strip():
            return global_text
        return ""

    # ----- write -----

    def write_layer(self, layer: LayerName, text: str) -> None:
        """Write ``text`` to one layer's file; the other layer is untouched.

        The parent directory is created if missing so the tool can land a
        first write on a fresh checkout. ``text`` is written verbatim
        (``encoding="utf-8"``); the operator controls trailing newlines.
        """
        if layer not in _LAYERS:
            raise UserProfileUnknownLayerError(str(layer))
        if not isinstance(text, str):
            raise UserProfileError("text must be a string")
        path = self._path_for(layer)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ----- layer dispatch -----

    def _path_for(self, layer: LayerName) -> Path:
        if layer == "global":
            return self.global_path
        if layer == "workspace":
            return self.workspace_path
        # Defensive: ``write_layer`` already type-guards; this branch keeps
        # the helper typed so a refactor cannot accidentally widen the
        # layer name to an arbitrary string.
        raise UserProfileUnknownLayerError(str(layer))

    # ----- env helper -----

    @classmethod
    def from_env(
        cls,
        workspace_root: Path | str | None = None,
        *,
        home: Path | str | None = None,
    ) -> "UserProfileStore":
        """Build a store from environment-derived paths.

        ``workspace_root`` defaults to :func:`pathlib.Path.cwd` (a stable
        key for the current workspace). ``home`` defaults to ``$HOME``;
        tests pass an explicit ``tmp_path`` to keep the real home directory
        out of the suite.
        """
        resolved_home = Path(home) if home is not None else Path(
            os.environ.get("HOME") or str(Path.home())
        )
        resolved_workspace = (
            Path(workspace_root) if workspace_root is not None else Path.cwd()
        )
        return cls(
            global_path=resolved_home / _LAYER_DIR / GLOBAL_FILENAME,
            workspace_path=resolved_workspace / _LAYER_DIR / WORKSPACE_FILENAME,
        )


def _read_text_or_empty(path: Path) -> str:
    """Read ``path`` as UTF-8; missing file → empty string.

    Pure file IO — no graph, no memory port. A present-but-empty file is
    empty, not absent: missing and empty have different semantics for the
    overlay logic (an empty workspace still falls back to global, the
    same as a missing workspace).
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


__all__ = [
    "GLOBAL_FILENAME",
    "WORKSPACE_FILENAME",
    "UserProfileError",
    "UserProfileStore",
    "UserProfileUnknownLayerError",
]
