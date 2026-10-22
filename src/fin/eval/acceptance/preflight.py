"""Fail-fast lane preflight and campaign-start input contract for P4.

One entry point, :func:`run_preflight`, decides whether a long campaign may
start. Every required check fails closed: a check that could not run is a
failure, never an assumed pass. Details name settings, database names, and
module names only; they never carry a DSN, a credential, or an environment
value.

The ``fin.host`` seams are imported inside the check that exercises them so an
import failure is observed and reported instead of crashing the preflight.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from fin.eval.acceptance.protocol import (
    LANES,
    MAX_COUNT,
    AcceptanceInputError,
    CaseManifest,
    CaseSpec,
    RunManifest,
    ensure_sanitized,
    manifest_run_mismatches,
    validate_case_manifest,
    validate_run_manifest,
)

# Probe keys. A probe returns ``None`` (or an empty string) when ready and a
# short reason string otherwise; the caller owns any timeout it needs.
PROBE_PLAYWRIGHT = "playwright_test_dependency"
PROBE_RUNTIME_READINESS = "runtime_readiness"
PROBE_BROWSER_SMOKE = "browser_smoke"
PROBE_REQUEST_EVENT_SCHEMAS = "request_event_schemas"
PROBE_MODEL_AND_TOOLS = "model_and_tools"

Probe = Callable[[], str | None]

EXIT_READY = 0
EXIT_INPUT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_USAGE = 64

# Check ids. These are the ``check_id`` values a campaign-start record carries.
CHECK_REQUIRED_PACKAGES = "required_packages"
CHECK_DATABASE_TARGET = "database_target"
CHECK_FIXTURE_PROVENANCE = "fixture_provenance"
CHECK_SEAM_IMPORTS = "seam_imports"
CHECK_REQUEST_EVENT_SCHEMAS = "request_event_schemas"
CHECK_RUNTIME_READINESS = "runtime_readiness"
CHECK_BROWSER_SMOKE = "browser_smoke"
CHECK_MODEL_AND_TOOLS = "model_and_tools"
CHECK_CASE_MANIFEST = "case_manifest"

# The capability reported when a check fails. ``required_packages`` carries two
# of them because the browser lane resolves its runner inside that check.
CAPABILITY_PACKAGES = "required_packages"
CAPABILITY_PLAYWRIGHT = "playwright_test_dependency"
CAPABILITY_DATABASE = "test_database"
CAPABILITY_FIXTURES = "fixture_provenance"
CAPABILITY_SEAMS = "seam_imports"
CAPABILITY_SCHEMAS = "request_event_schemas"
CAPABILITY_READINESS = "runtime_readiness"
CAPABILITY_BROWSER_SMOKE = "browser_smoke"
CAPABILITY_MODEL = "live_model"

_NODE_PLAYWRIGHT_PROBE = "import('@playwright/test')"
_NODE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Lane:
    """What a lane needs before its long campaign may start."""

    name: str
    requires_database: bool
    requires_live_model: bool
    requires_browser: bool
    required_packages: tuple[str, ...]
    seam_modules: tuple[str, ...]


_MECHANISM = Lane(
    name="mechanism",
    requires_database=True,
    requires_live_model=False,
    requires_browser=False,
    required_packages=("fin.host.http", "fin.eval.acceptance.protocol"),
    seam_modules=(
        "fin.host.workspace_http",
        "fin.host.http",
        "fin.host.thread_store",
    ),
)
_BROWSER = Lane(
    name="browser",
    requires_database=False,
    requires_live_model=False,
    requires_browser=True,
    required_packages=("fin.host.http", "fin.eval.acceptance.protocol"),
    seam_modules=("fin.host.workspace_http", "fin.host.http"),
)
_BUSINESS = Lane(
    name="business",
    requires_database=True,
    requires_live_model=True,
    requires_browser=False,
    required_packages=("fin.host.http", "fin.eval.acceptance.protocol"),
    seam_modules=("fin.host.workspace_http", "fin.host.http"),
)

# Key order mirrors the frozen ``protocol.LANES``; a test pins the equality.
LANES_BY_NAME: dict[str, Lane] = {
    lane.name: lane for lane in (_MECHANISM, _BROWSER, _BUSINESS)
}


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    ok: bool
    detail: str
    required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "ok": self.ok,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class PreflightResult:
    lane: str
    checks: tuple[PreflightCheck, ...]
    command: str
    exit_code: int
    missing_capability: str | None

    @property
    def passed(self) -> bool:
        return self.exit_code == EXIT_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "checks": [check.to_dict() for check in self.checks],
            "command": self.command,
            "exit_code": self.exit_code,
            "missing_capability": self.missing_capability,
        }


@dataclass(frozen=True)
class RecordedRun:
    """A run already on record: its id, and its payload when the caller has it."""

    run_id: str
    payload: Mapping[str, object] | None = None


@dataclass(frozen=True)
class _Finding:
    """A check verdict plus the capability a failure is reported under."""

    capability: str
    check: PreflightCheck


def select_lane(name: str) -> Lane:
    """Return the lane declaration for ``name`` or reject the invocation."""
    if not isinstance(name, str) or name not in LANES_BY_NAME:
        raise AcceptanceInputError(f"unknown lane {name!r}; expected one of {LANES}")
    return LANES_BY_NAME[name]


def run_preflight(
    lane: str,
    *,
    root: Path,
    command: str,
    case_manifest: CaseManifest,
    requires_live_model: bool = False,
    probes: Mapping[str, Probe] | None = None,
) -> PreflightResult:
    """Evaluate every required check for ``lane``; never raise for a check."""
    selected = select_lane(lane)
    supplied: Mapping[str, Probe] = probes or {}
    needs_model = selected.requires_live_model or requires_live_model

    findings = (
        _guarded(
            CHECK_REQUIRED_PACKAGES,
            CAPABILITY_PACKAGES,
            True,
            lambda: _check_required_packages(selected, root, supplied),
        ),
        _guarded(
            CHECK_DATABASE_TARGET,
            CAPABILITY_DATABASE,
            selected.requires_database,
            lambda: _check_database_target(selected),
        ),
        _guarded(
            CHECK_FIXTURE_PROVENANCE,
            CAPABILITY_FIXTURES,
            True,
            lambda: _check_fixture_provenance(selected, case_manifest),
        ),
        _guarded(
            CHECK_SEAM_IMPORTS,
            CAPABILITY_SEAMS,
            True,
            lambda: _check_seam_imports(selected),
        ),
        _guarded(
            CHECK_REQUEST_EVENT_SCHEMAS,
            CAPABILITY_SCHEMAS,
            True,
            lambda: _check_request_event_schemas(supplied),
        ),
        _guarded(
            CHECK_RUNTIME_READINESS,
            CAPABILITY_READINESS,
            True,
            lambda: _check_runtime_readiness(supplied),
        ),
        _guarded(
            CHECK_BROWSER_SMOKE,
            CAPABILITY_BROWSER_SMOKE,
            selected.requires_browser,
            lambda: _check_browser_smoke(selected, supplied),
        ),
        _guarded(
            CHECK_MODEL_AND_TOOLS,
            CAPABILITY_MODEL,
            needs_model,
            lambda: _check_model_and_tools(needs_model, supplied),
        ),
    )
    missing = next(
        (
            finding.capability
            for finding in findings
            if finding.check.required and not finding.check.ok
        ),
        None,
    )
    return PreflightResult(
        lane=selected.name,
        checks=tuple(finding.check for finding in findings),
        command=command,
        exit_code=EXIT_READY if missing is None else EXIT_BLOCKED,
        missing_capability=missing,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_required_packages(
    lane: Lane, root: Path, probes: Mapping[str, Probe]
) -> _Finding:
    failed = _unimportable(lane.required_packages)
    if failed:
        return _check(
            CHECK_REQUIRED_PACKAGES,
            False,
            "required package does not import: " + ", ".join(failed),
            CAPABILITY_PACKAGES,
        )
    detail = f"{len(lane.required_packages)} required package(s) resolve"
    if not lane.requires_browser:
        return _check(CHECK_REQUIRED_PACKAGES, True, detail, CAPABILITY_PACKAGES)
    reason = _playwright_reason(root, probes)
    if reason is not None:
        return _check(CHECK_REQUIRED_PACKAGES, False, reason, CAPABILITY_PLAYWRIGHT)
    return _check(
        CHECK_REQUIRED_PACKAGES,
        True,
        f"{detail}; @playwright/test resolves",
        CAPABILITY_PACKAGES,
    )


def _playwright_reason(root: Path, probes: Mapping[str, Probe]) -> str | None:
    """Run the injected dependency probe, or the real node resolution."""
    override = probes.get(PROBE_PLAYWRIGHT)
    if override is not None:
        return _probe_reason(override, "Playwright dependency probe")
    try:
        completed = subprocess.run(
            ["node", "-e", _NODE_PLAYWRIGHT_PROBE],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_NODE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "node is not on PATH; @playwright/test cannot be resolved"
    except subprocess.TimeoutExpired:
        return "node dependency probe timed out"
    if completed.returncode != 0:
        # Node's own output carries absolute paths; report the fact only.
        return "@playwright/test does not resolve in the repository root"
    return None


def _check_database_target(lane: Lane) -> _Finding:
    if not lane.requires_database:
        return _check(
            CHECK_DATABASE_TARGET,
            True,
            "lane does not require PostgreSQL",
            CAPABILITY_DATABASE,
            required=False,
        )
    dsn = (os.environ.get("FIN_TEST_DSN") or "").strip()
    if not dsn:
        return _check(
            CHECK_DATABASE_TARGET, False, "FIN_TEST_DSN is unset", CAPABILITY_DATABASE
        )
    database = _database_name(dsn)
    if database is None:
        return _check(
            CHECK_DATABASE_TARGET,
            False,
            "FIN_TEST_DSN is not a PostgreSQL connection setting",
            CAPABILITY_DATABASE,
        )
    if not database:
        return _check(
            CHECK_DATABASE_TARGET,
            False,
            "FIN_TEST_DSN does not name a database",
            CAPABILITY_DATABASE,
        )
    if not database.endswith("_test"):
        return _check(
            CHECK_DATABASE_TARGET,
            False,
            f"FIN_TEST_DSN names database {database!r}, which does not end in _test",
            CAPABILITY_DATABASE,
        )
    return _check(
        CHECK_DATABASE_TARGET,
        True,
        f"FIN_TEST_DSN names database {database!r}",
        CAPABILITY_DATABASE,
    )


def _database_name(dsn: str) -> str | None:
    """Extract the database NAME from a URI or keyword connection setting.

    Returns ``""`` when the setting names no database and ``None`` when it is
    not a PostgreSQL setting at all. Parsing only: no connection is opened and
    no other field is retained.
    """
    text = dsn.strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("postgres", "postgresql"):
            return None
        return unquote(parts.path.lstrip("/"))
    name = ""
    for token in text.split():
        key, separator, value = token.partition("=")
        if separator and key.lower() == "dbname":
            name = value.strip().strip("'\"")
    return name


def _check_fixture_provenance(lane: Lane, manifest: CaseManifest) -> _Finding:
    if not isinstance(manifest, CaseManifest):
        return _check(
            CHECK_FIXTURE_PROVENANCE,
            False,
            "case manifest is not a CaseManifest",
            CAPABILITY_FIXTURES,
        )
    problems = validate_case_manifest(manifest)
    if manifest.lane != lane.name:
        problems.append(
            f"case manifest lane {manifest.lane!r} is not the selected lane "
            f"{lane.name!r}"
        )
    required_cases = sum(
        1
        for case in manifest.cases
        if isinstance(case, CaseSpec) and case.required
    )
    if problems:
        return _check(
            CHECK_FIXTURE_PROVENANCE,
            False,
            _join_problems(problems),
            CAPABILITY_FIXTURES,
        )
    return _check(
        CHECK_FIXTURE_PROVENANCE,
        True,
        f"{required_cases} required case(s) carry fixture provenance and "
        "rebuild instructions",
        CAPABILITY_FIXTURES,
    )


def _check_seam_imports(lane: Lane) -> _Finding:
    failed = _unimportable(lane.seam_modules)
    if failed:
        return _check(
            CHECK_SEAM_IMPORTS,
            False,
            "seam import failed: " + ", ".join(failed),
            CAPABILITY_SEAMS,
        )
    return _check(
        CHECK_SEAM_IMPORTS,
        True,
        f"{len(lane.seam_modules)} seam module(s) import",
        CAPABILITY_SEAMS,
    )


def _unimportable(module_names: Sequence[str]) -> list[str]:
    """Return the modules that do not import, in declaration order."""
    failed: list[str] = []
    for module_name in module_names:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - an unresolvable import is the finding
            failed.append(module_name)
    return failed


def _check_request_event_schemas(probes: Mapping[str, Probe]) -> _Finding:
    return _probe_check(
        CHECK_REQUEST_EVENT_SCHEMAS,
        CAPABILITY_SCHEMAS,
        probes,
        "Request/event schema probe",
    )


def _check_runtime_readiness(probes: Mapping[str, Probe]) -> _Finding:
    return _probe_check(
        CHECK_RUNTIME_READINESS,
        CAPABILITY_READINESS,
        probes,
        "Runtime readiness probe",
    )


def _check_browser_smoke(lane: Lane, probes: Mapping[str, Probe]) -> _Finding:
    if not lane.requires_browser:
        return _check(
            CHECK_BROWSER_SMOKE,
            True,
            "lane does not require a browser",
            CAPABILITY_BROWSER_SMOKE,
            required=False,
        )
    return _probe_check(
        CHECK_BROWSER_SMOKE,
        CAPABILITY_BROWSER_SMOKE,
        probes,
        "Browser smoke probe",
    )


def _check_model_and_tools(needs_model: bool, probes: Mapping[str, Probe]) -> _Finding:
    if not needs_model:
        return _check(
            CHECK_MODEL_AND_TOOLS,
            True,
            "lane does not require live model or tools",
            CAPABILITY_MODEL,
            required=False,
        )
    # Presence only: the setting value is never read or echoed.
    missing = [
        name
        for name in ("FIN_API_KEY", "FIN_MODEL")
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        return _check(
            CHECK_MODEL_AND_TOOLS,
            False,
            "missing setting(s): " + ", ".join(missing),
            CAPABILITY_MODEL,
        )
    return _probe_check(
        CHECK_MODEL_AND_TOOLS,
        CAPABILITY_MODEL,
        probes,
        "Live model/tool probe",
    )


def _probe_check(
    check_id: str,
    capability: str,
    probes: Mapping[str, Probe],
    label: str,
) -> _Finding:
    probe = probes.get(check_id)
    if probe is None:
        return _check(
            check_id,
            False,
            f"{label} was not supplied; readiness is never assumed",
            capability,
        )
    reason = _probe_reason(probe, label)
    if reason is not None:
        return _check(check_id, False, reason, capability)
    return _check(check_id, True, f"{label} reported ready", capability)


def _probe_reason(probe: object, label: str) -> str | None:
    """Return a failure detail, or ``None`` when the probe reported ready."""
    if not callable(probe):
        return f"{label} is not callable"
    try:
        outcome = probe()
    except Exception as exc:  # noqa: BLE001 - reported, never propagated
        return f"check raised {type(exc).__name__}"
    if outcome is None:
        return None
    if not isinstance(outcome, str):
        return f"{label} returned {type(outcome).__name__}, not a reason"
    reason = outcome.strip()
    if not reason:
        return None
    try:
        ensure_sanitized(reason)
    except AcceptanceInputError:
        return f"{label} reported a sensitive reason (withheld)"
    return reason


def _check(
    check_id: str, ok: bool, detail: str, capability: str, *, required: bool = True
) -> _Finding:
    return _Finding(capability, PreflightCheck(check_id, ok, detail, required))


def _guarded(
    check_id: str,
    capability: str,
    required: bool,
    run: Callable[[], _Finding],
) -> _Finding:
    """Run one check; an unexpected exception becomes a failed check."""
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - a preflight must not crash the start
        # The class name only: a message can carry a DSN or a file path.
        return _check(
            check_id,
            False,
            f"check raised {type(exc).__name__}",
            capability,
            required=required,
        )


def _join_problems(problems: Sequence[str], limit: int = 5) -> str:
    shown = list(problems[:limit])
    if len(problems) > limit:
        shown.append(f"... {len(problems) - limit} more")
    return "; ".join(shown)


# ---------------------------------------------------------------------------
# Campaign-start input contract
# ---------------------------------------------------------------------------


def validate_campaign_inputs(
    manifest: CaseManifest,
    run: RunManifest,
    existing_runs: Sequence[RecordedRun],
    *locked_run_ids: Sequence[str],
) -> list[str]:
    """Return campaign-start problems; an empty list means the start is clean.

    ``existing_runs`` carries one :class:`RecordedRun` per run already on
    record: a run id may be reused only when its payload is absent or matches
    the new run exactly. A non-empty ``locked_run_ids`` sequence means another
    campaign holds the start.
    """
    problems: list[str] = []
    if not isinstance(manifest, CaseManifest):
        return ["manifest is not a CaseManifest"]
    if not manifest.cases:
        problems.append("manifest declares no cases")
    problems.extend(validate_case_manifest(manifest))

    if not isinstance(run, RunManifest):
        problems.append("run is not a RunManifest")
        return problems
    problems.extend(validate_run_manifest(run))
    problems.extend(manifest_run_mismatches(run, manifest))

    for entry in existing_runs:
        if _malformed_recorded_run(entry):
            # A malformed record must not be skipped silently.
            problems.append("a recorded run entry is malformed")
            continue
        # A bare id is the same run continuing; only a differing payload is a
        # second start under an identity that already exists.
        if entry.run_id != run.run_id or entry.payload is None:
            continue
        if dict(entry.payload) != run.to_dict():
            problems.append(
                f"run_id {run.run_id!r} is already recorded with a different "
                "payload; a rerun must append an attempt under the recorded identity"
            )
            break

    for run_ids in locked_run_ids:
        if run_ids:
            problems.append(
                "another campaign start holds an active lock for run(s): "
                + ", ".join(sorted(str(run_id) for run_id in run_ids))
            )
    return problems


def _malformed_recorded_run(entry: object) -> bool:
    """True when a recorded-run entry does not carry the declared shape."""
    if not isinstance(entry, RecordedRun) or not isinstance(entry.run_id, str):
        return True
    return entry.payload is not None and not isinstance(entry.payload, Mapping)


def parse_count(text: str) -> int:
    """Parse a non-negative count; reject negatives, non-integers, overflow."""
    if not isinstance(text, str):
        raise AcceptanceInputError("count is not a string")
    stripped = text.strip()
    if not stripped or not stripped.isascii() or not stripped.isdigit():
        raise AcceptanceInputError(
            f"count {stripped!r} is not a non-negative integer"
        )
    value = int(stripped)
    if value > MAX_COUNT:
        raise AcceptanceInputError(f"count {value} is above MAX_COUNT={MAX_COUNT}")
    return value
