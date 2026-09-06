"""Default 工作区 + registry of bound 工作区 paths (spec Focus 2).

The registry is the on-disk source of truth for the operator's known
工作区: a small JSON file at ``$HOME/.fin/known_roots.json``. The
**default** 工作区 at ``$HOME/.fin/default`` is always present in that
file (created on first ``list_roots``).

Why JSON in $HOME: every other fin file already lives under
``$HOME/.fin`` (the global ``user.md``, ``watchlist.json``, the user
profile), so a dedicated file next to them costs nothing and stays
out of the working tree. A two-line format — list of
``{path, slug, is_default}`` entries — is enough for the left rail.

The functions in this module never call ``Path.cwd()`` (spec Focus 2
+ SC3: never `Path.cwd()`). They take an explicit ``home: Path``
argument so the suite can fully isolate from the real home.

Boundary classes covered in the test suite:
* empty — fresh home, list returns exactly the default entry;
* negative — bind missing path / bind a file / illegal slug;
* overflow — slug > MAX_SLUG_LENGTH, no truncation;
* concurrent — two creates on the same slug, registry stays valid;
* exception — corrupted JSON / unwritable registry file.

Every typed failure is one of the ``Workspace*Error`` classes below;
the HTTP layer maps them to 4xx with a stable ``error`` code.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (single source of truth for the on-disk layout).
# ---------------------------------------------------------------------------

#: Filename of the JSON registry under ``<home>/.fin/``.
REGISTRY_FILENAME: str = "known_roots.json"

#: Directory under ``<home>/`` that holds fin config + the registry.
_FIN_DIR: str = ".fin"

#: Directory under ``<home>/.fin/`` that ``create_root`` populates.
WORKSPACES_DIR_NAME: str = "workspaces"

#: Directory name of the default 工作区 under ``<home>/.fin/``.
DEFAULT_ROOT_NAME: str = "default"

#: Hard cap on a 工作区 slug. Overflow is a typed reject, not a
#: truncate-into-cwd (spec Focus 2 / SC4 overflow class).
MAX_SLUG_LENGTH: int = 64

#: Slug must match this regex (start + end anchored).
#: ASCII letters, digits, ``-``, ``_``, ``.``; no separators, no ``..``,
#: no whitespace, no Unicode.
_SLUG_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Typed errors (mapped to 4xx in the HTTP layer).
# ---------------------------------------------------------------------------


class WorkspaceRegistryError(Exception):
    """Base class for registry failures.

    Subclasses split into the per-axis typed errors the HTTP layer
    maps to named 4xx codes. ``WorkspaceRoot`` and ``WorkspaceSlug``
    families are siblings — a registry may surface a bind OR a slug
    failure, but never both at once.
    """


class WorkspaceBindError(WorkspaceRegistryError):
    """Bind-time failure: missing path, file-not-dir, etc."""


class WorkspaceNotFoundError(WorkspaceBindError):
    """The bound path does not exist on disk."""


class WorkspaceNotADirectoryError(WorkspaceBindError):
    """The bound path exists but is a file, not a directory."""


class WorkspaceSlugError(WorkspaceRegistryError):
    """Slug sanitizer failure."""


class WorkspaceSlugEmptyError(WorkspaceSlugError):
    """Empty / whitespace-only slug."""


class WorkspaceSlugIllegalError(WorkspaceSlugError):
    """Slug contains separators, traversal, or non-allowed characters."""


class WorkspaceSlugOverflowError(WorkspaceSlugError):
    """Slug is longer than ``MAX_SLUG_LENGTH``."""


class WorkspaceRegistryUnreadableError(WorkspaceRegistryError):
    """The registry file exists but is not valid JSON / not readable.

    Spec Focus 2 / SC4: this is a typed EXIT, NEVER a cwd fallback.
    """


class WorkspaceRegistryUnwritableError(WorkspaceRegistryError):
    """The registry file is not writable (e.g. read-only home)."""


# ---------------------------------------------------------------------------
# Data model.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WorkspaceRoot:
    """One 工作区 entry in the registry.

    ``path`` is the resolved absolute path. ``slug`` is the operator's
    name for it (``default`` for the default entry). ``is_default`` is
    True for the entry whose path matches ``default_workspace_root``.
    """

    path: Path
    slug: str
    is_default: bool


# ---------------------------------------------------------------------------
# Module-level helpers.
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


def _slugify_path(path: Path) -> str:
    """Derive a slug from a bound directory's path.

    The slug is informational only — used for ``WorkspaceRoot.slug``
    and the registry JSON. ``Path.name`` is a stable filesystem-level
    label for an absolute path. Empty names (trailing separator) are
    rejected so the registry never carries an empty slug.
    """
    name = path.name
    if not name:
        raise WorkspaceBindError(f"cannot derive slug from path {path!r}")
    return name


def _validate_slug(slug: str) -> str:
    """Return ``slug`` if it passes the sanitizer; else raise a typed error.

    Order matters for the typed-error contract:

    1. empty / whitespace-only → ``WorkspaceSlugEmptyError``;
    2. over the cap → ``WorkspaceSlugOverflowError``;
    3. illegal characters → ``WorkspaceSlugIllegalError``.

    Overflow before legality: a >64-char string with separators still
    trips the overflow check first, so an over-cap slug never leaks a
    partial path through the legality check.
    """
    if not isinstance(slug, str):
        raise WorkspaceSlugEmptyError(f"slug must be a string, got {type(slug).__name__}")
    stripped = slug.strip()
    if not stripped:
        raise WorkspaceSlugEmptyError("slug is empty")
    if len(slug) > MAX_SLUG_LENGTH:
        raise WorkspaceSlugOverflowError(
            f"slug length {len(slug)} exceeds {MAX_SLUG_LENGTH}"
        )
    if not _SLUG_PATTERN.match(slug):
        raise WorkspaceSlugIllegalError(
            f"slug {slug!r} contains illegal characters; allowed: "
            f"[A-Za-z0-9._-]"
        )
    if slug == ".." or slug == ".":
        raise WorkspaceSlugIllegalError(f"slug {slug!r} is a traversal token")
    return slug


def _write_registry(path: Path, roots: list[WorkspaceRoot]) -> None:
    """Serialize ``roots`` to ``path``; raise typed on failure."""
    payload = {
        "roots": [
            {
                "path": str(r.path),
                "slug": r.slug,
                "is_default": r.is_default,
            }
            for r in roots
        ]
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except (OSError, PermissionError) as exc:
        raise WorkspaceRegistryUnwritableError(
            f"cannot write registry at {path}: {exc}"
        ) from exc


def _read_registry(path: Path) -> list[WorkspaceRoot]:
    """Read ``path`` into a list of ``WorkspaceRoot``; raise typed on failure.

    A missing file is NOT a failure — the caller treats that as
    "fresh home, fall through to the default entry". An existing but
    unreadable / corrupt file IS a failure (typed, never cwd).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, PermissionError) as exc:
        raise WorkspaceRegistryUnreadableError(
            f"cannot read registry at {path}: {exc}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkspaceRegistryUnreadableError(
            f"registry at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict) or "roots" not in payload:
        raise WorkspaceRegistryUnreadableError(
            f"registry at {path} missing required 'roots' key"
        )
    entries = payload["roots"]
    if not isinstance(entries, list):
        raise WorkspaceRegistryUnreadableError(
            f"registry at {path} 'roots' is not a list"
        )
    out: list[WorkspaceRoot] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or "path" not in entry
            or "slug" not in entry
            or "is_default" not in entry
        ):
            raise WorkspaceRegistryUnreadableError(
                f"registry at {path} has malformed entry: {entry!r}"
            )
        out.append(
            WorkspaceRoot(
                path=Path(entry["path"]),
                slug=str(entry["slug"]),
                is_default=bool(entry["is_default"]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Registry class.
# ---------------------------------------------------------------------------


class WorkspaceRegistry:
    """One operator's registry of bound 工作区.

    ``home`` is an explicit argument; ``Path.home()`` and ``$HOME`` are
    never read. The constructor does not touch disk; the first
    ``list_roots`` creates the registry file with the default entry.
    """

    def __init__(self, *, home: Path | str) -> None:
        self._home = Path(home)

    # ----- layout -----

    @property
    def home(self) -> Path:
        return self._home

    def _fin_dir(self) -> Path:
        return self._home / _FIN_DIR

    def _registry_path(self) -> Path:
        return self._fin_dir() / REGISTRY_FILENAME

    def _workspaces_dir(self) -> Path:
        return self._fin_dir() / WORKSPACES_DIR_NAME

    # ----- list / bind / create -----

    def list_roots(self) -> list[WorkspaceRoot]:
        """Return every known 工作区. Default is always first.

        On a fresh home (no registry file) this creates the default
        directory + the registry file with the default entry; the
        caller never has to mkdir before listing.
        """
        default = default_workspace_root(self._home)
        existing = _read_registry(self._registry_path())
        # Ensure the default entry is present (and first). A corrupted
        # registry raises BEFORE we reach this line.
        if not any(r.is_default for r in existing):
            existing = [
                WorkspaceRoot(path=default, slug=DEFAULT_ROOT_NAME, is_default=True)
            ] + existing
            _write_registry(self._registry_path(), existing)
        return existing

    def bind_root(self, absolute_path: Path | str) -> Path:
        """Bind an existing absolute directory; return the resolved path.

        Re-binding an already-listed path is a no-op (idempotent).
        Missing path → ``WorkspaceNotFoundError``; file-not-dir →
        ``WorkspaceNotADirectoryError``; relative path →
        ``WorkspaceBindError``. None of these mutate the registry.
        """
        path = Path(absolute_path)
        if not path.is_absolute():
            raise WorkspaceBindError(
                f"bind path must be absolute, got {absolute_path!r}"
            )
        if not path.exists():
            # Validation failures must not side-effect (no half-write).
            # The registry file is read here only to short-circuit
            # when the path is already listed — that's still a no-op
            # call when the file is missing.
            raise WorkspaceNotFoundError(f"path does not exist: {path}")
        if not path.is_dir():
            raise WorkspaceNotADirectoryError(f"path is not a directory: {path}")
        resolved = path.resolve()
        # ``list_roots`` materializes the registry file on a fresh
        # home so the subsequent ``_write_registry`` has a target.
        roots = self.list_roots()
        if any(r.path == resolved for r in roots):
            return resolved
        roots.append(
            WorkspaceRoot(
                path=resolved,
                slug=_slugify_path(resolved),
                is_default=False,
            )
        )
        _write_registry(self._registry_path(), roots)
        return resolved

    def create_root(self, slug: str) -> Path:
        """Create ``<home>/.fin/workspaces/<slug>`` and register it.

        Slug validation is strict (no empty / separators / ``..`` /
        > MAX_SLUG_LENGTH). The created directory MUST NOT contain
        any catalog file (spec Focus 2: "Create does not clone or
        split the 知识库").

        Two creates with the same slug are tolerated as a no-op: the
        second call returns the existing path without rewriting the
        registry. The two-create race in tests asserts the registry
        stays valid JSON — that contract holds whether the second
        call surfaces a typed 4xx OR returns the existing path, per
        the ticket's "both acceptable" note.
        """
        validated = _validate_slug(slug)
        target = self._workspaces_dir() / validated
        # Idempotent re-create of an existing slug returns the
        # existing path without raising. The test asserts the
        # registry stays valid; this contract satisfies it.
        if target.exists() and target.is_dir():
            roots = self.list_roots()
            if not any(r.path == target for r in roots):
                roots.append(
                    WorkspaceRoot(path=target, slug=validated, is_default=False)
                )
                _write_registry(self._registry_path(), roots)
            return target
        if target.exists():
            # Same name, but it's a file — refuse as illegal to avoid
            # silently clobbering.
            raise WorkspaceBindError(
                f"refusing to clobber non-directory at {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=False)
        roots = self.list_roots()
        if not any(r.path == target for r in roots):
            roots.append(
                WorkspaceRoot(path=target, slug=validated, is_default=False)
            )
            _write_registry(self._registry_path(), roots)
        return target


__all__ = [
    "DEFAULT_ROOT_NAME",
    "MAX_SLUG_LENGTH",
    "REGISTRY_FILENAME",
    "WORKSPACES_DIR_NAME",
    "WorkspaceBindError",
    "WorkspaceNotADirectoryError",
    "WorkspaceNotFoundError",
    "WorkspaceRegistry",
    "WorkspaceRegistryError",
    "WorkspaceRegistryUnreadableError",
    "WorkspaceRegistryUnwritableError",
    "WorkspaceRoot",
    "WorkspaceSlugEmptyError",
    "WorkspaceSlugError",
    "WorkspaceSlugIllegalError",
    "WorkspaceSlugOverflowError",
    "default_workspace_root",
]