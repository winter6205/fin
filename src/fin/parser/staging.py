"""The parser staging root and the orphan-collection pass.

A parser attempt writes its derived output to a staging path before it
commits a result. An attempt that dies mid-parse leaves that output behind
with nobody to claim it, so a collection pass identifies it and reclaims it.

Collection is SAFE, and the safety rule is the whole point of the pass:

* Output a COMMITTED RESULT references is never removed. A committed result
  is the durable claim on its derived output; collecting it would destroy the
  result's own content. The protection is the committed result itself, not
  only the artifact's ``referenced`` flag — a worker that committed and
  crashed before marking the flag must still be safe.
* Output a CURRENT attempt is still writing is never removed. That attempt
  still holds the job's effective fencing token, so it may yet commit.
* Output outside the injected root is never removed. The parser writes under
  the root it was given and nowhere else.

A FILESYSTEM FAULT is never one of those rules. A path the filesystem cannot
resolve, or a file the filesystem refuses to remove, is reported as a fault
with its own reason and its own typed error: it is never dressed up as the
policy decision "outside the root", and the artifact is never dropped as if
the policy had been applied. That typed error is ``ParserPersistenceError``:
the staging area is part of the parser-owned durable area — the
``fin_parser.staging_artifact`` rows and the files under the injected root —
so a filesystem refusal there is a persistence failure of the parser's own
store, not a new kind of failure. The reason says which fault it was.

The root is always injected (``tmp_path`` in tests). The parser never falls
back to the operator's home, and a root inside ``~/.fin`` is refused.

The artifact lifecycle and the ownership rule the policy needs belong to the
store port; this module owns the root and the collection policy.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from fin.parser.errors import ParserInvalidRequestError, ParserPersistenceError
from fin.parser.ports import ParserJobStore, StagingArtifact

logger = logging.getLogger(__name__)

__all__ = [
    "CollectionReport",
    "FaultedArtifact",
    "SkippedArtifact",
    "StagingCollector",
    "StagingRoot",
]

#: Skip reasons, stable so a caller can read them without parsing prose.
#: Every one of them is a deliberate decision of the collection policy.
SKIP_MARKED_REFERENCED: Final[str] = "marked_referenced"
SKIP_REFERENCED_BY_RESULT: Final[str] = "referenced_by_result"
SKIP_CURRENT_ATTEMPT: Final[str] = "current_attempt"
SKIP_OUTSIDE_ROOT: Final[str] = "outside_root"

#: Fault reasons, kept apart from the skip reasons: a fault is the filesystem
#: refusing to answer, never a decision the policy made.
FAULT_PATH_UNRESOLVABLE: Final[str] = "path_unresolvable"
FAULT_REMOVE_REFUSED: Final[str] = "remove_refused"


def _refuse_operator_home(root: Path) -> None:
    """Refuse a staging root inside the operator's home.

    The parser's output belongs to a job, never to the operator's home
    directory, and a wrong root must fail loudly instead of writing there.
    """
    try:
        home = Path.home()
    except RuntimeError as exc:
        # EXIT: the home cannot be determined — the root is refused, because
        # a root that cannot be checked may not be accepted.
        raise ParserInvalidRequestError(
            "the operator home could not be determined, so the parser "
            "staging root cannot be checked",
            root=str(root),
        ) from exc
    operator_home = home / ".fin"
    if root == operator_home or operator_home in root.parents:
        raise ParserInvalidRequestError(
            "the parser staging root may not live inside the operator home",
            root=str(root),
        )


@dataclass(frozen=True, slots=True)
class SkippedArtifact:
    """An artifact the collection pass deliberately left in place."""

    artifact_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class FaultedArtifact:
    """An artifact the collection pass could not judge, because of a fault.

    A fault is not a policy decision: the artifact was kept because the
    filesystem refused to answer, not because a rule says to keep it.
    """

    artifact_id: str
    reason: str
    error_code: str


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What one collection pass reclaimed, kept, and could not judge."""

    removed_artifact_ids: tuple[str, ...]
    removed_paths: tuple[str, ...]
    skipped: tuple[SkippedArtifact, ...]
    faults: tuple[FaultedArtifact, ...] = ()


class StagingRoot:
    """The injected directory the parser writes its attempt output under."""

    def __init__(self, root: Path | str) -> None:
        try:
            resolved = Path(root).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            # EXIT: the root cannot be resolved — it is refused instead of
            # guessed, because an unusable root may not be written to.
            raise ParserInvalidRequestError(
                "the parser staging root could not be resolved", root=str(root)
            ) from exc
        _refuse_operator_home(resolved)
        self._root = resolved

    @property
    def root(self) -> Path:
        """The resolved root every artifact path must live under."""
        return self._root

    def attempt_dir(self, job_id: str, fencing_token: int) -> Path:
        """Where one attempt's output lives."""
        return self._root / str(job_id) / str(int(fencing_token))

    def allocate(self, job_id: str, fencing_token: int, name: str) -> Path:
        """Reserve a file path under this attempt's directory."""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ParserInvalidRequestError(
                "a staging artifact name must be a single file name", name=name
            )
        directory = self.attempt_dir(job_id, fencing_token)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def contains(self, path: Path) -> bool:
        """Whether a path lies inside this root.

        A path the filesystem cannot resolve is a FAULT, not an answer: it
        raises ``ParserPersistenceError`` instead of answering ``False``,
        so a broken path is never read as "outside the root".
        """
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            # EXIT: the path cannot be resolved — a symlink loop, a name too
            # long, a refused lookup. The fault is raised, never answered as
            # "outside the root".
            raise ParserPersistenceError(
                "the staging path could not be resolved",
                reason=FAULT_PATH_UNRESOLVABLE,
                path=str(path),
            ) from exc
        return resolved == self._root or self._root in resolved.parents


class StagingCollector:
    """Reclaims staging output that no live attempt and no result claims."""

    def __init__(self, store: ParserJobStore, root: StagingRoot) -> None:
        self._store = store
        self._root = root

    def collect(self, *, now: datetime | None = None) -> CollectionReport:
        """Reclaim every collectible artifact and report what was kept.

        The file goes before its row: a pass that dies in between leaves a row
        pointing at a missing file, and the next pass converges on it.
        """
        moment = now if now is not None else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        artifacts = self._store.list_staging_artifacts()
        protected_refs = self._store.committed_result_refs()
        protected_tokens = self._store.committed_result_tokens()
        live_tokens = self._store.live_attempt_tokens(now=moment)

        removed_ids: list[str] = []
        removed_paths: list[str] = []
        skipped: list[SkippedArtifact] = []
        faults: list[FaultedArtifact] = []
        for artifact in artifacts:
            try:
                reason = self._skip_reason(
                    artifact,
                    protected_refs=protected_refs,
                    protected_tokens=protected_tokens,
                    live_tokens=live_tokens,
                )
            except ParserPersistenceError as exc:
                # EXIT: a filesystem fault keeps the artifact and is reported
                # as a fault — never as the policy skip "outside the root".
                faults.append(self._fault_entry(artifact, exc))
                continue
            if reason is not None:
                skipped.append(
                    SkippedArtifact(artifact_id=artifact.artifact_id, reason=reason)
                )
                continue
            try:
                removed = self._remove_output(artifact.path)
            except ParserPersistenceError as exc:
                # EXIT: the filesystem refused the removal — the file stays
                # and the pass reports a fault, not a policy skip.
                faults.append(self._fault_entry(artifact, exc))
                continue
            if not removed:
                skipped.append(
                    SkippedArtifact(
                        artifact_id=artifact.artifact_id, reason=SKIP_OUTSIDE_ROOT
                    )
                )
                continue
            self._store.drop_staging_artifact(artifact.artifact_id)
            removed_ids.append(artifact.artifact_id)
            removed_paths.append(artifact.path)
        return CollectionReport(
            removed_artifact_ids=tuple(removed_ids),
            removed_paths=tuple(removed_paths),
            skipped=tuple(skipped),
            faults=tuple(faults),
        )

    @staticmethod
    def _fault_entry(
        artifact: StagingArtifact, exc: ParserPersistenceError
    ) -> FaultedArtifact:
        """The fault entry of one artifact the pass could not judge."""
        reason = exc.details.get("reason")
        if not isinstance(reason, str):
            raise ParserPersistenceError(
                "the staging fault carries no reason",
                artifact_id=artifact.artifact_id,
            ) from exc
        logger.warning(
            "the staging collection could not judge artifact %s: %s (%s)",
            artifact.artifact_id,
            reason,
            exc.code,
        )
        return FaultedArtifact(
            artifact_id=artifact.artifact_id,
            reason=reason,
            error_code=exc.code,
        )

    def _skip_reason(
        self,
        artifact: StagingArtifact,
        *,
        protected_refs: set[str],
        protected_tokens: set[tuple[str, int]],
        live_tokens: set[tuple[str, int]],
    ) -> str | None:
        """Why this artifact must be kept, or ``None`` when it is collectible.

        Raises ``ParserPersistenceError`` when the artifact's path cannot
        be resolved: a filesystem fault is not a policy answer.
        """
        token = (artifact.job_id, artifact.fencing_token)
        if not self._root.contains(Path(artifact.path)):
            return SKIP_OUTSIDE_ROOT
        if artifact.referenced:
            return SKIP_MARKED_REFERENCED
        if artifact.path in protected_refs or token in protected_tokens:
            return SKIP_REFERENCED_BY_RESULT
        if token in live_tokens:
            return SKIP_CURRENT_ATTEMPT
        return None

    def _remove_output(self, path: str) -> bool:
        """Remove one artifact's file.

        True when the file is gone. False when the DOMAIN keeps it: the
        path is not the parser's staging output. Raises
        ``ParserPersistenceError`` when the FILESYSTEM refused the
        removal, so a fault is never read as a domain outcome.
        """
        target = Path(path)
        if not self._root.contains(target):
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            # EXIT: the file is already gone — the artifact counts as
            # collected and its row is dropped.
            return True
        except OSError as exc:
            # EXIT: the filesystem refused the removal — the file stays, and
            # the pass reports a fault instead of a policy skip.
            raise ParserPersistenceError(
                "the staging output could not be removed",
                reason=FAULT_REMOVE_REFUSED,
                path=str(path),
            ) from exc
        with contextlib.suppress(OSError):
            # EXIT: the attempt directory is left behind when it is not empty
            # or cannot be removed — the file itself is already gone.
            target.parent.rmdir()
        return True
