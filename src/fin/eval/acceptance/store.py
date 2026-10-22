"""Append-only campaign evidence store.

Layout under an injected root::

    manifests/<manifest_version>.json     case manifests (write once)
    runs/<run_id>.json                    run manifests (write once)
    attempts/<lane>.jsonl                 append-only attempt log per lane
    reports/<lane>/<run_id>/report-<n>.json
    locks/<lane>.lock                     single-flight campaign lock

Attempt lines are appended and fsynced one at a time and are never
rewritten. Whole-file writes go through a temp file and ``os.replace``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from fin.eval.acceptance.protocol import (
    LANES,
    AcceptanceInputError,
    AttemptRecord,
    CaseManifest,
    RunManifest,
    ensure_sanitized,
    validate_attempt_record,
    validate_case_manifest,
    validate_run_manifest,
)

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REPORT_NAME = re.compile(r"report-([1-9][0-9]*)")


class CampaignStartBlocked(RuntimeError):
    """A competing campaign already holds the lane lock."""

    def __init__(self, lane: str) -> None:
        self.lane = lane
        super().__init__(f"campaign lane {lane} is already running")


def _safe_component(value: str, label: str) -> str:
    """Return ``value`` if it can name a single path component, else raise."""
    ensure_sanitized(value)
    if _SAFE_COMPONENT.fullmatch(value) is None:
        raise AcceptanceInputError(f"{label} is not a usable path component")
    return value


def _require_lane(lane: str) -> str:
    if lane not in LANES:
        raise AcceptanceInputError(f"lane is not one of {LANES}")
    return lane


def _stage_json(path: Path, payload: Mapping[str, object]) -> Path:
    """Write ``payload`` as canonical JSON to a fresh temp file beside ``path``.

    Returns the staged temp path for the caller to finalize; a failure while
    staging removes the temp file before propagating.
    """
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as staged:
            staged.write(encoded)
            staged.flush()
            os.fsync(staged.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged_name)
        raise
    return Path(staged_name)


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> Path:
    """Write ``payload`` as canonical JSON via temp file plus ``os.replace``."""
    staged = _stage_json(path, payload)
    try:
        os.replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise
    return path


def read_json(path: Path, label: str) -> object:
    """Read a JSON document, naming only the failure class on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AcceptanceInputError(
            f"{label} is unreadable: {type(exc).__name__}"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AcceptanceInputError(f"{label} is not valid JSON") from exc


def write_json_exclusive(path: Path, payload: Mapping[str, object]) -> Path:
    """Write ``payload`` only if ``path`` does not exist, atomically.

    The staged file is hard-linked into place, so two writers cannot
    clobber the same report; ``FileExistsError`` means the id is taken.
    """
    staged = _stage_json(path, payload)
    try:
        os.link(staged, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staged)
    return path


class CampaignStore:
    """File-backed, append-only campaign evidence store."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- paths ------------------------------------------------------------

    def attempt_log_path(self, lane: str) -> Path:
        return self.root / "attempts" / f"{_require_lane(lane)}.jsonl"

    def run_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{_safe_component(run_id, 'run_id')}.json"

    def manifest_path(self, manifest_version: str) -> Path:
        return (
            self.root
            / "manifests"
            / f"{_safe_component(manifest_version, 'manifest_version')}.json"
        )

    def reports_dir(self, lane: str, run_id: str) -> Path:
        return (
            self.root
            / "reports"
            / _require_lane(lane)
            / _safe_component(run_id, "run_id")
        )

    def report_path(self, lane: str, run_id: str, report_id: str) -> Path:
        ensure_sanitized(report_id)
        parts = report_id.split(":")
        if len(parts) != 3:
            raise AcceptanceInputError(
                "report_id is not of the form <lane>:<run_id>:report-<n>"
            )
        id_lane, id_run, name = parts
        if id_lane != _require_lane(lane) or id_run != run_id:
            raise AcceptanceInputError("report_id does not match its lane and run")
        if _REPORT_NAME.fullmatch(name) is None:
            raise AcceptanceInputError(
                "report_id is not of the form <lane>:<run_id>:report-<n>"
            )
        return self.reports_dir(lane, run_id) / f"{name}.json"

    # -- case manifests ---------------------------------------------------

    def write_manifest(self, manifest: CaseManifest) -> str:
        """Register a case manifest once; an identical rewrite is a no-op."""
        problems = validate_case_manifest(manifest)
        if problems:
            raise AcceptanceInputError(
                f"case manifest violates the contract ({len(problems)} problem(s))"
            )
        payload = manifest.to_dict()
        path = self.manifest_path(manifest.manifest_version)
        existing = self._existing_payload(path, "case manifest")
        if existing is not None and existing != payload:
            raise AcceptanceInputError(
                "a different case manifest is already stored for this version"
            )
        if existing is None:
            write_json_atomic(path, payload)
        return str(path)

    def load_manifest(self, manifest_version: str) -> CaseManifest:
        path = self.manifest_path(manifest_version)
        if not path.is_file():
            raise AcceptanceInputError("case manifest is not registered")
        return CaseManifest.from_dict(read_json(path, "case manifest"))

    # -- run manifests ----------------------------------------------------

    def open_campaign(self, run: RunManifest) -> str:
        """Write the run manifest once and return its path."""
        problems = validate_run_manifest(run)
        if problems:
            raise AcceptanceInputError(
                f"run manifest violates the contract ({len(problems)} problem(s))"
            )
        payload = run.to_dict()
        path = self.run_path(run.run_id)
        existing = self._existing_payload(path, "run manifest")
        if existing is not None and existing != payload:
            raise AcceptanceInputError(
                "a different run manifest is already stored for this run_id"
            )
        if existing is None:
            write_json_atomic(path, payload)
        return str(path)

    def load_run(self, run_id: str) -> RunManifest:
        path = self.run_path(run_id)
        if not path.is_file():
            raise AcceptanceInputError("run is not open")
        return RunManifest.from_dict(read_json(path, "run manifest"))

    def list_runs(self, lane: str | None = None) -> tuple[str, ...]:
        if lane is not None:
            _require_lane(lane)
        directory = self.root / "runs"
        if not directory.is_dir():
            return ()
        run_ids: list[str] = []
        for path in directory.glob("*.json"):
            run = RunManifest.from_dict(read_json(path, "run manifest"))
            if lane is None or run.lane == lane:
                run_ids.append(run.run_id)
        return tuple(sorted(run_ids))

    # -- attempts ---------------------------------------------------------

    def record_attempt(self, rec: AttemptRecord) -> None:
        """Validate, then append and fsync one attempt line."""
        run = self.load_run(rec.run_id)
        manifest = self.load_manifest(run.manifest_version)
        if manifest.lane != run.lane:
            raise AcceptanceInputError("run lane differs from its case manifest lane")
        problems = validate_attempt_record(rec, manifest)
        if problems:
            raise AcceptanceInputError(
                f"attempt violates the contract ({len(problems)} problem(s))"
            )
        existing = self.load_attempts(rec.run_id)
        if any(
            item.case_id == rec.case_id and item.attempt == rec.attempt
            for item in existing
        ):
            raise AcceptanceInputError(
                "an attempt with this case_id and attempt number is already recorded"
            )

        path = self.attempt_log_path(run.lane)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def load_attempts(self, run_id: str) -> tuple[AttemptRecord, ...]:
        """Replay one run's attempts in append order."""
        safe_run_id = _safe_component(run_id, "run_id")
        run_path = self.root / "runs" / f"{safe_run_id}.json"
        if not run_path.is_file():
            return ()
        run = RunManifest.from_dict(read_json(run_path, "run manifest"))
        path = self.attempt_log_path(run.lane)
        if not path.is_file():
            return ()
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AcceptanceInputError(
                f"attempt log is unreadable: {type(exc).__name__}"
            ) from exc

        records: list[AttemptRecord] = []
        for number, line in enumerate(contents.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AcceptanceInputError(
                    f"attempt log line {number} is not valid JSON"
                ) from exc
            try:
                record = AttemptRecord.from_dict(payload)
            except AcceptanceInputError as exc:
                raise AcceptanceInputError(
                    f"attempt log line {number} is not a valid attempt record: {exc}"
                ) from exc
            if record.run_id == run_id:
                records.append(record)
        return tuple(records)

    def pending_case_ids(
        self, run_id: str, required: Sequence[str]
    ) -> tuple[str, ...]:
        """Required case ids with no attempt yet, in ``required`` order."""
        run = self.load_run(run_id)
        known = {
            spec.case_id for spec in self.load_manifest(run.manifest_version).cases
        }
        if any(case_id not in known for case_id in required):
            raise AcceptanceInputError("required case_id is not in the run's manifest")
        recorded = {rec.case_id for rec in self.load_attempts(run_id)}
        return tuple(case_id for case_id in required if case_id not in recorded)

    # -- reports ----------------------------------------------------------

    def count_reports(self, lane: str, run_id: str) -> int:
        directory = self.reports_dir(lane, run_id)
        if not directory.is_dir():
            return 0
        return sum(
            1
            for path in directory.glob("report-*.json")
            if _REPORT_NAME.fullmatch(path.stem) is not None
        )

    # -- single flight ----------------------------------------------------

    @contextlib.contextmanager
    def campaign_lock(self, lane: str) -> Iterator[None]:
        """Hold an exclusive per-lane ``flock`` for a campaign's duration."""
        _require_lane(lane)
        lock_path = self.root / "locks" / f"{lane}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CampaignStartBlocked(lane) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- helpers ----------------------------------------------------------

    def _existing_payload(self, path: Path, label: str) -> object | None:
        if not path.is_file():
            return None
        return read_json(path, label)


__all__ = [
    "CampaignStartBlocked",
    "CampaignStore",
    "read_json",
    "write_json_atomic",
    "write_json_exclusive",
]
