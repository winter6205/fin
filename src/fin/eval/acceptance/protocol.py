"""Frozen P4 acceptance evidence contract: cases, runs, attempts, and reports.

Identity: a case is ``(case_id, case_version)``, a run is ``run_id``, and the
manifest plus dataset-manifest versions pin the inputs a report rests on.
Append-only: every attempt is retained; a retry is a new attempt, and a pass
after a failure derives ``flaky``, never a clean pass.
Denominator: a report counts every declared case, including missing, blocked,
interrupted, and unscored ones, and ``denominator_total`` must reconcile.
The frozen dataclasses can be built without checks; the ``case_spec`` /
``case_manifest`` / ``run_manifest`` / ``attempt_record`` / ``report`` helpers
and the ``validate_*`` functions are the enforced entry points.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.0.0"
LANES = ("mechanism", "browser", "business")
ATTEMPT_STATUSES = ("passed", "failed", "not_run", "blocked", "interrupted")
CASE_STATUSES = ATTEMPT_STATUSES + ("flaky",)
FAILURE_CLASSES = (
    "none",
    "assertion",
    "harness",
    "environment",
    "timeout",
    "cancelled",
    "invalid_input",
    "unavailable_capability",
)
MAX_COUNT = 100_000
MAX_CASES = 10_000

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?")
_CREDENTIAL_MARKERS = (
    "bearer ",
    "postgres://",
    "postgresql://",
    "redis://",
    "password=",
)
_CREDENTIAL_PATTERN = re.compile(r"(?<![a-z0-9])sk-[a-z0-9]", re.IGNORECASE)
_NON_REPRODUCIBLE_MARKERS = (
    "non-reproducible",
    "non reproducible",
    "not reproducible",
    "irreproducible",
)


class AcceptanceContractError(RuntimeError):
    """Base class for acceptance-contract failures."""


class AcceptanceInputError(AcceptanceContractError):
    """Malformed or contract-invalid acceptance input."""


class ManifestValidationError(AcceptanceInputError):
    """A manifest, run, attempt, or report violates the frozen contract."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        detail = "; ".join(self.problems) if self.problems else "no detail"
        super().__init__(f"{len(self.problems)} contract problem(s): {detail}")


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    case_version: str
    lane: str
    primary_category: str
    fixture_version: str
    provenance: str
    rebuild_instructions: str
    required: bool = True
    rubric_id: str | None = None
    rubric_version: str | None = None


@dataclass(frozen=True)
class CaseManifest:
    manifest_version: str
    lane: str
    dataset_manifest_version: str
    cases: tuple[CaseSpec, ...]
    created_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "lane": self.lane,
            "dataset_manifest_version": self.dataset_manifest_version,
            "cases": [_case_spec_to_dict(case) for case in self.cases],
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CaseManifest:
        data = _payload_mapping(payload, "CaseManifest", _CASE_MANIFEST_KEYS)
        raw_cases = data["cases"]
        if not isinstance(raw_cases, list):
            raise AcceptanceInputError("CaseManifest.cases is not a list")
        manifest = cls(
            manifest_version=_str_field(data, "manifest_version", "CaseManifest"),
            lane=_str_field(data, "lane", "CaseManifest"),
            dataset_manifest_version=_str_field(
                data, "dataset_manifest_version", "CaseManifest"
            ),
            cases=tuple(_case_spec_from_dict(item) for item in raw_cases),
            created_by=_str_field(data, "created_by", "CaseManifest"),
        )
        _raise_if_problems(validate_case_manifest(manifest))
        return manifest


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    lane: str
    manifest_version: str
    dataset_manifest_version: str
    code_version: str
    config_id: str
    model_id: str
    parser_id: str
    measured_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lane": self.lane,
            "manifest_version": self.manifest_version,
            "dataset_manifest_version": self.dataset_manifest_version,
            "code_version": self.code_version,
            "config_id": self.config_id,
            "model_id": self.model_id,
            "parser_id": self.parser_id,
            "measured_run": self.measured_run,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RunManifest:
        data = _payload_mapping(payload, "RunManifest", _RUN_MANIFEST_KEYS)
        run = cls(
            run_id=_str_field(data, "run_id", "RunManifest"),
            lane=_str_field(data, "lane", "RunManifest"),
            manifest_version=_str_field(data, "manifest_version", "RunManifest"),
            dataset_manifest_version=_str_field(
                data, "dataset_manifest_version", "RunManifest"
            ),
            code_version=_str_field(data, "code_version", "RunManifest"),
            config_id=_str_field(data, "config_id", "RunManifest"),
            model_id=_str_field(data, "model_id", "RunManifest"),
            parser_id=_str_field(data, "parser_id", "RunManifest"),
            measured_run=_bool_field(data, "measured_run", "RunManifest"),
        )
        _raise_if_problems(validate_run_manifest(run))
        return run


@dataclass(frozen=True)
class AttemptRecord:
    run_id: str
    case_id: str
    case_version: str
    manifest_version: str
    attempt: int
    status: str
    reason_code: str
    failure_class: str
    evidence_refs: tuple[str, ...]
    execution_ids: Mapping[str, str]
    started_at: str
    finished_at: str
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "case_version": self.case_version,
            "manifest_version": self.manifest_version,
            "attempt": self.attempt,
            "status": self.status,
            "reason_code": self.reason_code,
            "failure_class": self.failure_class,
            "evidence_refs": list(self.evidence_refs),
            "execution_ids": dict(self.execution_ids),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: object) -> AttemptRecord:
        data = _payload_mapping(payload, "AttemptRecord", _ATTEMPT_RECORD_KEYS)
        raw_refs = data["evidence_refs"]
        if not isinstance(raw_refs, list) or not all(
            isinstance(ref, str) for ref in raw_refs
        ):
            raise AcceptanceInputError(
                "AttemptRecord.evidence_refs is not a list of strings"
            )
        raw_ids = data["execution_ids"]
        if not isinstance(raw_ids, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_ids.items()
        ):
            raise AcceptanceInputError(
                "AttemptRecord.execution_ids is not a mapping of strings"
            )
        raw_details = data["details"]
        if not isinstance(raw_details, Mapping):
            raise AcceptanceInputError("AttemptRecord.details is not a mapping")
        record = cls(
            run_id=_str_field(data, "run_id", "AttemptRecord"),
            case_id=_str_field(data, "case_id", "AttemptRecord"),
            case_version=_str_field(data, "case_version", "AttemptRecord"),
            manifest_version=_str_field(data, "manifest_version", "AttemptRecord"),
            attempt=_int_field(data, "attempt", "AttemptRecord"),
            status=_str_field(data, "status", "AttemptRecord"),
            reason_code=_str_field(data, "reason_code", "AttemptRecord"),
            failure_class=_str_field(data, "failure_class", "AttemptRecord"),
            evidence_refs=tuple(raw_refs),
            execution_ids=dict(raw_ids),
            started_at=_str_field(data, "started_at", "AttemptRecord"),
            finished_at=_str_field(data, "finished_at", "AttemptRecord"),
            details=dict(raw_details),
        )
        _raise_if_problems(_attempt_local_problems(record))
        return record


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    case_version: str
    status: str
    attempts: int
    latest_reason_code: str
    failure_class: str
    rubric_version: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    report_id: str
    contract_version: str
    lane: str
    run_id: str
    manifest_version: str
    case_outcomes: tuple[CaseOutcome, ...]
    counts: Mapping[str, int]
    denominator_total: int
    accepted: bool
    gate: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "contract_version": self.contract_version,
            "lane": self.lane,
            "run_id": self.run_id,
            "manifest_version": self.manifest_version,
            "case_outcomes": [
                {
                    "case_id": outcome.case_id,
                    "case_version": outcome.case_version,
                    "status": outcome.status,
                    "attempts": outcome.attempts,
                    "latest_reason_code": outcome.latest_reason_code,
                    "failure_class": outcome.failure_class,
                    "rubric_version": outcome.rubric_version,
                    "evidence_refs": list(outcome.evidence_refs),
                }
                for outcome in self.case_outcomes
            ],
            "counts": dict(self.counts),
            "denominator_total": self.denominator_total,
            "accepted": self.accepted,
            "gate": self.gate,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Report:
        data = _payload_mapping(payload, "Report", _REPORT_KEYS)
        raw_outcomes = data["case_outcomes"]
        if not isinstance(raw_outcomes, list):
            raise AcceptanceInputError("Report.case_outcomes is not a list")
        raw_counts = data["counts"]
        if not isinstance(raw_counts, Mapping):
            raise AcceptanceInputError("Report.counts is not a mapping")
        built = cls(
            report_id=_str_field(data, "report_id", "Report"),
            contract_version=_str_field(data, "contract_version", "Report"),
            lane=_str_field(data, "lane", "Report"),
            run_id=_str_field(data, "run_id", "Report"),
            manifest_version=_str_field(data, "manifest_version", "Report"),
            case_outcomes=tuple(
                _case_outcome_from_dict(item) for item in raw_outcomes
            ),
            counts=dict(raw_counts),
            denominator_total=_int_field(data, "denominator_total", "Report"),
            accepted=_bool_field(data, "accepted", "Report"),
            gate=_str_field(data, "gate", "Report"),
        )
        _raise_if_problems(_report_problems(built))
        return built


_CASE_MANIFEST_KEYS = (
    "manifest_version",
    "lane",
    "dataset_manifest_version",
    "cases",
    "created_by",
)
_CASE_SPEC_KEYS = (
    "case_id",
    "case_version",
    "lane",
    "primary_category",
    "fixture_version",
    "provenance",
    "rebuild_instructions",
    "required",
    "rubric_id",
    "rubric_version",
)
_RUN_MANIFEST_KEYS = (
    "run_id",
    "lane",
    "manifest_version",
    "dataset_manifest_version",
    "code_version",
    "config_id",
    "model_id",
    "parser_id",
    "measured_run",
)
_ATTEMPT_RECORD_KEYS = (
    "run_id",
    "case_id",
    "case_version",
    "manifest_version",
    "attempt",
    "status",
    "reason_code",
    "failure_class",
    "evidence_refs",
    "execution_ids",
    "started_at",
    "finished_at",
    "details",
)
_CASE_OUTCOME_KEYS = (
    "case_id",
    "case_version",
    "status",
    "attempts",
    "latest_reason_code",
    "failure_class",
    "rubric_version",
    "evidence_refs",
)
_REPORT_KEYS = (
    "report_id",
    "contract_version",
    "lane",
    "run_id",
    "manifest_version",
    "case_outcomes",
    "counts",
    "denominator_total",
    "accepted",
    "gate",
)


# ---------------------------------------------------------------------------
# Constructor helpers (the enforced entry points)
# ---------------------------------------------------------------------------


def case_spec(
    *,
    case_id: str,
    case_version: str,
    lane: str,
    primary_category: str,
    fixture_version: str,
    provenance: str,
    rebuild_instructions: str,
    required: bool = True,
    rubric_id: str | None = None,
    rubric_version: str | None = None,
) -> CaseSpec:
    spec = CaseSpec(
        case_id=case_id,
        case_version=case_version,
        lane=lane,
        primary_category=primary_category,
        fixture_version=fixture_version,
        provenance=provenance,
        rebuild_instructions=rebuild_instructions,
        required=required,
        rubric_id=rubric_id,
        rubric_version=rubric_version,
    )
    _raise_if_problems(_case_problems(spec, "case"))
    return spec


def case_manifest(
    *,
    manifest_version: str,
    lane: str,
    dataset_manifest_version: str,
    cases: Sequence[CaseSpec],
    created_by: str,
) -> CaseManifest:
    manifest = CaseManifest(
        manifest_version=manifest_version,
        lane=lane,
        dataset_manifest_version=dataset_manifest_version,
        cases=tuple(cases),
        created_by=created_by,
    )
    _raise_if_problems(validate_case_manifest(manifest))
    return manifest


def run_manifest(
    *,
    run_id: str,
    lane: str,
    manifest_version: str,
    dataset_manifest_version: str,
    code_version: str,
    config_id: str,
    model_id: str,
    parser_id: str,
    measured_run: bool = False,
) -> RunManifest:
    run = RunManifest(
        run_id=run_id,
        lane=lane,
        manifest_version=manifest_version,
        dataset_manifest_version=dataset_manifest_version,
        code_version=code_version,
        config_id=config_id,
        model_id=model_id,
        parser_id=parser_id,
        measured_run=measured_run,
    )
    _raise_if_problems(validate_run_manifest(run))
    return run


def attempt_record(
    *,
    run_id: str,
    case_id: str,
    case_version: str,
    manifest_version: str,
    attempt: int,
    status: str,
    reason_code: str,
    failure_class: str,
    evidence_refs: Sequence[str],
    execution_ids: Mapping[str, str],
    started_at: str,
    finished_at: str,
    details: Mapping[str, object] | None = None,
) -> AttemptRecord:
    record = AttemptRecord(
        run_id=run_id,
        case_id=case_id,
        case_version=case_version,
        manifest_version=manifest_version,
        attempt=attempt,
        status=status,
        reason_code=reason_code,
        failure_class=failure_class,
        evidence_refs=tuple(evidence_refs),
        execution_ids=dict(execution_ids),
        started_at=started_at,
        finished_at=finished_at,
        details=dict(details or {}),
    )
    _raise_if_problems(_attempt_local_problems(record))
    return record


def report(
    *,
    report_id: str,
    lane: str,
    run_id: str,
    manifest_version: str,
    case_outcomes: Sequence[CaseOutcome],
    gate: str,
    counts: Mapping[str, int] | None = None,
    denominator_total: int | None = None,
    accepted: bool = False,
    contract_version: str = CONTRACT_VERSION,
) -> Report:
    """Build a report; counts and denominator default to the declared outcomes.

    The caller supplies one ``CaseOutcome`` per case it has observed, including
    the ones that never ran (``not_run``). An explicit ``denominator_total`` may
    exceed the recorded outcomes for a truthful interim report, but it can never
    be smaller, and ``accepted`` requires every declared case to be recorded and
    passed.
    """
    outcomes = tuple(case_outcomes)
    resolved_counts = (
        _tally_outcomes(outcomes) if counts is None else dict(counts)
    )
    built = Report(
        report_id=report_id,
        contract_version=contract_version,
        lane=lane,
        run_id=run_id,
        manifest_version=manifest_version,
        case_outcomes=outcomes,
        counts=resolved_counts,
        denominator_total=(
            len(outcomes) if denominator_total is None else denominator_total
        ),
        accepted=accepted,
        gate=gate,
    )
    _raise_if_problems(_report_problems(built))
    return built


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_case_manifest(manifest: CaseManifest) -> list[str]:
    """Return manifest problems; an empty list means the manifest is clean."""
    if not isinstance(manifest, CaseManifest):
        return ["manifest is not a CaseManifest"]
    problems: list[str] = []
    if not _non_empty_str(manifest.manifest_version):
        problems.append("manifest_version is empty")
    elif not _is_version(manifest.manifest_version):
        problems.append("manifest_version is not a dotted numeric version")
    if manifest.lane not in LANES:
        problems.append(f"lane is not one of {LANES}")
    if not _non_empty_str(manifest.dataset_manifest_version):
        problems.append("dataset_manifest_version is empty")
    elif not _is_version(manifest.dataset_manifest_version):
        problems.append(
            "dataset_manifest_version is not a dotted numeric version"
        )
    if not _non_empty_str(manifest.created_by):
        problems.append("created_by is empty")

    cases = manifest.cases
    if not isinstance(cases, tuple):
        problems.append("cases is not a tuple")
        return problems
    if not cases:
        problems.append("cases is empty; a manifest must declare at least one case")
        return problems
    if len(cases) > MAX_CASES:
        problems.append(
            f"cases declares {len(cases)} cases, above MAX_CASES={MAX_CASES}"
        )
    seen: set[str] = set()
    for index, spec in enumerate(cases):
        prefix = f"cases[{index}]"
        problems.extend(_case_problems(spec, prefix))
        if not isinstance(spec, CaseSpec):
            continue
        if _non_empty_str(spec.case_id):
            if spec.case_id in seen:
                problems.append(f"{prefix}.case_id duplicates an earlier case_id")
            seen.add(spec.case_id)
        if spec.lane in LANES and spec.lane != manifest.lane:
            problems.append(f"{prefix}.lane differs from the manifest lane")
    return problems


def validate_run_manifest(run: RunManifest) -> list[str]:
    """Return run-manifest problems; an empty list means the run is clean."""
    if not isinstance(run, RunManifest):
        return ["run is not a RunManifest"]
    problems: list[str] = []
    if not _non_empty_str(run.run_id):
        problems.append("run_id is empty")
    if run.lane not in LANES:
        problems.append(f"lane is not one of {LANES}")
    for field_name in ("manifest_version", "dataset_manifest_version"):
        value = getattr(run, field_name)
        if not _non_empty_str(value):
            problems.append(f"{field_name} is empty")
        elif not _is_version(value):
            problems.append(f"{field_name} is not a dotted numeric version")
    for field_name in ("code_version", "config_id", "model_id", "parser_id"):
        if not _non_empty_str(getattr(run, field_name)):
            problems.append(f"{field_name} is empty")
    if not isinstance(run.measured_run, bool):
        problems.append("measured_run is not a bool")
    return problems


def manifest_run_mismatches(run: RunManifest, manifest: CaseManifest) -> list[str]:
    """Return how ``run`` and ``manifest`` disagree; empty means they agree.

    A run is bound to the case manifest it was opened against: the lane and the
    manifest / dataset-manifest versions must match.
    """
    problems: list[str] = []
    if run.lane != manifest.lane:
        problems.append(
            f"run lane {run.lane!r} differs from the manifest lane {manifest.lane!r}"
        )
    if run.manifest_version != manifest.manifest_version:
        problems.append(
            f"run manifest_version {run.manifest_version!r} differs from the "
            f"manifest {manifest.manifest_version!r}"
        )
    if run.dataset_manifest_version != manifest.dataset_manifest_version:
        problems.append(
            "run dataset_manifest_version "
            f"{run.dataset_manifest_version!r} differs from the manifest "
            f"{manifest.dataset_manifest_version!r}"
        )
    return problems


def validate_attempt_record(
    rec: AttemptRecord, manifest: CaseManifest
) -> list[str]:
    """Return attempt problems against its manifest; empty list means clean."""
    if not isinstance(rec, AttemptRecord):
        return ["record is not an AttemptRecord"]
    problems = _attempt_local_problems(rec)
    if not isinstance(manifest, CaseManifest):
        problems.append("manifest is not a CaseManifest")
        return problems
    if rec.manifest_version != manifest.manifest_version:
        problems.append("manifest_version differs from the manifest")
    match: CaseSpec | None = None
    for spec in manifest.cases:
        if isinstance(spec, CaseSpec) and spec.case_id == rec.case_id:
            match = spec
            break
    if match is None:
        problems.append("case_id is not in the manifest")
    elif match.case_version != rec.case_version:
        problems.append("case_version differs from the manifest case version")
    return problems


def derive_case_status(
    attempts: Sequence[AttemptRecord], *, declared_measured_run: bool
) -> str:
    """Derive a case status from its attempts, ordered by attempt then finish.

    No attempts is ``not_run``. The latest attempt is the base status, except
    that any passed attempt after a failed one is ``flaky``. A pass that was
    not made in a declared measured run cannot be a clean pass.
    """
    if not isinstance(declared_measured_run, bool):
        raise AcceptanceInputError("declared_measured_run is not a bool")
    records = tuple(attempts)
    if not records:
        return "not_run"
    for record in records:
        if not isinstance(record, AttemptRecord):
            raise AcceptanceInputError("attempts contains a non-AttemptRecord")
        if record.status not in ATTEMPT_STATUSES:
            raise AcceptanceInputError("attempts contains an unknown status")
        if not isinstance(record.attempt, int) or isinstance(record.attempt, bool):
            raise AcceptanceInputError("attempts contains a non-integer attempt")
        if not isinstance(record.finished_at, str):
            raise AcceptanceInputError("attempts contains a non-string finished_at")
    ordered = sorted(records, key=lambda record: (record.attempt, record.finished_at))
    for index, record in enumerate(ordered):
        if record.status == "passed" and any(
            earlier.status == "failed" for earlier in ordered[:index]
        ):
            return "flaky"
    latest = ordered[-1].status
    if latest == "passed" and not declared_measured_run:
        return "not_run"
    return latest


def count_statuses(outcomes: Sequence[CaseOutcome]) -> dict[str, int]:
    """Tally outcomes, always emitting every case status and ``total``."""
    counts = {status: 0 for status in CASE_STATUSES}
    counts["total"] = 0
    for outcome in outcomes:
        if not isinstance(outcome, CaseOutcome):
            raise AcceptanceInputError("outcomes contains a non-CaseOutcome")
        if outcome.status not in CASE_STATUSES:
            raise AcceptanceInputError("outcomes contains an unknown status")
        counts[outcome.status] += 1
        counts["total"] += 1
    return counts


def ensure_sanitized(text: str) -> str:
    """Return ``text`` unchanged, or raise when it could leak or break a record."""
    if not isinstance(text, str):
        raise AcceptanceInputError("text is not a string")
    problem = _sanitized_problem("text", text)
    if problem is not None:
        raise AcceptanceInputError(problem)
    return text


# ---------------------------------------------------------------------------
# Per-record problem collection
# ---------------------------------------------------------------------------


def _case_problems(spec: object, prefix: str) -> list[str]:
    if not isinstance(spec, CaseSpec):
        return [f"{prefix} is not a CaseSpec"]
    problems: list[str] = []
    text_fields = (
        "case_id",
        "case_version",
        "lane",
        "primary_category",
        "fixture_version",
        "provenance",
    )
    for field_name in text_fields:
        if not _non_empty_str(getattr(spec, field_name)):
            problems.append(f"{prefix}.{field_name} is empty")
    case_version = spec.case_version
    if _non_empty_str(case_version) and not _is_version(case_version):
        problems.append(f"{prefix}.case_version is not a dotted numeric version")
    # fixture_version is a fixture identity plus version (e.g.
    # ``corpus-single-doc@1.0.0``): required, but not a dotted-numeric string.
    if _non_empty_str(spec.lane) and spec.lane not in LANES:
        problems.append(f"{prefix}.lane is not one of {LANES}")
    if not isinstance(spec.required, bool):
        problems.append(f"{prefix}.required is not a bool")
    rebuild = spec.rebuild_instructions
    if not isinstance(rebuild, str):
        problems.append(f"{prefix}.rebuild_instructions is not a string")
    elif not rebuild.strip():
        if spec.required is True:
            problems.append(
                f"{prefix}.rebuild_instructions is empty for a required case"
            )
        elif not _provenance_is_non_reproducible(spec.provenance):
            problems.append(
                f"{prefix}.rebuild_instructions is empty and provenance does "
                "not declare the case non-reproducible"
            )
    rubric_id, rubric_version = spec.rubric_id, spec.rubric_version
    if (rubric_id is None) != (rubric_version is None):
        problems.append(
            f"{prefix}.rubric_id and {prefix}.rubric_version must be set together"
        )
    else:
        if rubric_id is not None and not _non_empty_str(rubric_id):
            problems.append(f"{prefix}.rubric_id is empty")
        if rubric_version is not None and not _non_empty_str(rubric_version):
            problems.append(f"{prefix}.rubric_version is empty")
        elif rubric_version is not None and not _is_version(rubric_version):
            problems.append(
                f"{prefix}.rubric_version is not a dotted numeric version"
            )
    return problems


def _failure_class_problems(
    status: str, failure_class: str, prefix: str, subject: str
) -> list[str]:
    """Problems for the status/failure_class pairing, worded for ``subject``.

    A pass must carry ``none``; a failure must carry a real class. ``prefix``
    labels the field and is empty for a bare attempt record.
    """
    label = f"{prefix}: " if prefix else ""
    if status == "passed" and failure_class != "none":
        return [f"{label}a passed {subject} must carry failure_class none"]
    if status == "failed" and failure_class == "none":
        return [
            f"{label}a failed {subject} must carry a failure class other than none"
        ]
    return []


def _attempt_local_problems(rec: object) -> list[str]:
    if not isinstance(rec, AttemptRecord):
        return ["record is not an AttemptRecord"]
    problems: list[str] = []
    for field_name in (
        "run_id",
        "case_id",
        "case_version",
        "manifest_version",
        "reason_code",
        "started_at",
        "finished_at",
    ):
        if not _non_empty_str(getattr(rec, field_name)):
            problems.append(f"{field_name} is empty")
    for field_name in ("case_version", "manifest_version"):
        value = getattr(rec, field_name)
        if _non_empty_str(value) and not _is_version(value):
            problems.append(f"{field_name} is not a dotted numeric version")
    if rec.status not in ATTEMPT_STATUSES:
        problems.append("status is not a known attempt status")
    if rec.failure_class not in FAILURE_CLASSES:
        problems.append("failure_class is not a known failure class")
    if (
        not isinstance(rec.attempt, int)
        or isinstance(rec.attempt, bool)
        or not 1 <= rec.attempt <= MAX_COUNT
    ):
        problems.append(f"attempt is not an integer in [1, {MAX_COUNT}]")
    problems.extend(
        _failure_class_problems(rec.status, rec.failure_class, "", "attempt")
    )
    problems.extend(_sanitized_field_problems("reason_code", rec.reason_code))
    refs = rec.evidence_refs
    if not isinstance(refs, tuple):
        problems.append("evidence_refs is not a tuple")
    else:
        for index, ref in enumerate(refs):
            label = f"evidence_refs[{index}]"
            if not isinstance(ref, str) or not ref.strip():
                problems.append(f"{label} is not a non-empty string")
                continue
            problems.extend(_sanitized_field_problems(label, ref))
    ids = rec.execution_ids
    if not isinstance(ids, Mapping):
        problems.append("execution_ids is not a mapping")
    else:
        for index, (key, value) in enumerate(ids.items()):
            if not isinstance(key, str) or not key.strip():
                problems.append(f"execution_ids[{index}] has an empty key")
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    f"execution_ids[{index}] is not a non-empty string value"
                )
            elif isinstance(value, str):
                problems.extend(
                    _sanitized_field_problems(f"execution_ids[{index}]", value)
                )
    details = rec.details
    if not isinstance(details, Mapping):
        problems.append("details is not a mapping")
    else:
        problems.extend(_detail_problems(details, "details"))
    return problems


def _report_problems(built: object) -> list[str]:
    if not isinstance(built, Report):
        return ["report is not a Report"]
    problems: list[str] = []
    problems.extend(_sanitized_field_problems("report_id", built.report_id))
    if not _non_empty_str(built.report_id):
        problems.append("report_id is empty")
    if built.contract_version != CONTRACT_VERSION:
        problems.append(f"contract_version is not {CONTRACT_VERSION}")
    if built.lane not in LANES:
        problems.append(f"lane is not one of {LANES}")
    if not _non_empty_str(built.run_id):
        problems.append("run_id is empty")
    if not _non_empty_str(built.manifest_version):
        problems.append("manifest_version is empty")
    elif not _is_version(built.manifest_version):
        problems.append("manifest_version is not a dotted numeric version")
    if not _non_empty_str(built.gate):
        problems.append("gate is empty")
    problems.extend(_sanitized_field_problems("gate", built.gate))
    if not isinstance(built.accepted, bool):
        problems.append("accepted is not a bool")

    outcomes = built.case_outcomes
    if not isinstance(outcomes, tuple):
        problems.append("case_outcomes is not a tuple")
        outcomes = ()
    for index, outcome in enumerate(outcomes):
        problems.extend(_case_outcome_problems(outcome, f"case_outcomes[{index}]"))

    problems.extend(_counts_problems(built.counts))
    if isinstance(built.counts, Mapping):
        tally = _tally_outcomes(outcomes)
        for status in CASE_STATUSES:
            recorded = built.counts.get(status)
            if isinstance(recorded, int) and not isinstance(recorded, bool):
                if recorded != tally[status]:
                    problems.append(
                        "counts do not reconcile with case outcomes: "
                        f"{status} recorded={recorded} observed={tally[status]}"
                    )
        recorded_total = built.counts.get("total")
        if (
            isinstance(recorded_total, int)
            and not isinstance(recorded_total, bool)
            and recorded_total != len(outcomes)
        ):
            problems.append(
                "counts do not reconcile with case outcomes: "
                f"total recorded={recorded_total} observed={len(outcomes)}"
            )
    if not isinstance(built.denominator_total, int) or isinstance(
        built.denominator_total, bool
    ):
        problems.append("denominator_total is not an int")
    elif not 0 <= built.denominator_total <= MAX_COUNT:
        problems.append(f"denominator_total is outside [0, {MAX_COUNT}]")
    elif len(outcomes) > built.denominator_total:
        problems.append(
            "denominator_total is below the recorded outcomes, which would drop "
            f"cases: denominator_total={built.denominator_total} "
            f"recorded={len(outcomes)}"
        )
    if built.accepted is True:
        if len(outcomes) < built.denominator_total:
            problems.append(
                "accepted is true but recorded outcomes cover only "
                f"{len(outcomes)} of {built.denominator_total} declared cases"
            )
        not_passed = sum(1 for outcome in outcomes if outcome.status != "passed")
        if not outcomes:
            problems.append("accepted is true but no case outcome is declared")
        elif not_passed:
            problems.append(
                f"accepted is true but {not_passed} case outcome(s) did not pass"
            )
    return problems


def _case_outcome_problems(outcome: object, prefix: str) -> list[str]:
    if not isinstance(outcome, CaseOutcome):
        return [f"{prefix} is not a CaseOutcome"]
    problems: list[str] = []
    if not _non_empty_str(outcome.case_id):
        problems.append(f"{prefix}.case_id is empty")
    if not _non_empty_str(outcome.case_version):
        problems.append(f"{prefix}.case_version is empty")
    elif not _is_version(outcome.case_version):
        problems.append(f"{prefix}.case_version is not a dotted numeric version")
    if outcome.status not in CASE_STATUSES:
        problems.append(f"{prefix}.status is not a known case status")
    if (
        not isinstance(outcome.attempts, int)
        or isinstance(outcome.attempts, bool)
        or not 0 <= outcome.attempts <= MAX_COUNT
    ):
        problems.append(f"{prefix}.attempts is not an integer in [0, {MAX_COUNT}]")
    if outcome.failure_class not in FAILURE_CLASSES:
        problems.append(f"{prefix}.failure_class is not a known failure class")
    if not isinstance(outcome.latest_reason_code, str):
        problems.append(f"{prefix}.latest_reason_code is not a string")
    else:
        problems.extend(
            _sanitized_field_problems(
                f"{prefix}.latest_reason_code", outcome.latest_reason_code
            )
        )
    if not isinstance(outcome.rubric_version, str):
        problems.append(f"{prefix}.rubric_version is not a string")
    refs = outcome.evidence_refs
    if not isinstance(refs, tuple):
        problems.append(f"{prefix}.evidence_refs is not a tuple")
    else:
        for index, ref in enumerate(refs):
            label = f"{prefix}.evidence_refs[{index}]"
            if not isinstance(ref, str) or not ref.strip():
                problems.append(f"{label} is not a non-empty string")
                continue
            problems.extend(_sanitized_field_problems(label, ref))
    problems.extend(
        _failure_class_problems(outcome.status, outcome.failure_class, prefix, "case")
    )
    return problems


def _counts_problems(counts: object) -> list[str]:
    if not isinstance(counts, Mapping):
        return ["counts is not a mapping"]
    problems: list[str] = []
    expected = set(CASE_STATUSES) | {"total"}
    missing = sorted(expected - set(counts))
    unexpected = sorted(set(counts) - expected)
    if missing:
        problems.append(f"counts is missing the keys: {', '.join(missing)}")
    if unexpected:
        problems.append(
            f"counts has {len(unexpected)} key(s) outside the case status set"
        )
    for key, value in counts.items():
        if not isinstance(key, str):
            problems.append("counts has a non-string key")
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"counts[{key}] is not an int")
        elif not 0 <= value <= MAX_COUNT:
            problems.append(f"counts[{key}] is outside [0, {MAX_COUNT}]")
    return problems


def _tally_outcomes(outcomes: Sequence[CaseOutcome]) -> dict[str, int]:
    """Lenient tally for report reconciliation: unknown entries are skipped.

    The counting itself is ``count_statuses``; this only drops the entries that
    would make it raise, so reconciliation reports a problem instead.
    """
    return count_statuses(
        [
            outcome
            for outcome in outcomes
            if isinstance(outcome, CaseOutcome) and outcome.status in CASE_STATUSES
        ]
    )


def _detail_problems(value: object, path: str) -> list[str]:
    if value is None or isinstance(value, (bool, str)):
        return []
    if isinstance(value, int):
        if not 0 <= value <= MAX_COUNT:
            return [f"{path} is a count outside [0, {MAX_COUNT}]"]
        return []
    if isinstance(value, float):
        if not math.isfinite(value):
            return [f"{path} is not a finite number"]
        return []
    if isinstance(value, Mapping):
        problems: list[str] = []
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str) or not key:
                problems.append(f"{path}[{index}] has a non-string key")
                continue
            problems.extend(_detail_problems(item, f"{path}.{key}"))
        return problems
    if isinstance(value, (list, tuple)):
        problems = []
        for index, item in enumerate(value):
            problems.extend(_detail_problems(item, f"{path}[{index}]"))
        return problems
    return [f"{path} is not a JSON-safe value"]


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _case_spec_to_dict(spec: CaseSpec) -> dict[str, Any]:
    return {
        "case_id": spec.case_id,
        "case_version": spec.case_version,
        "lane": spec.lane,
        "primary_category": spec.primary_category,
        "fixture_version": spec.fixture_version,
        "provenance": spec.provenance,
        "rebuild_instructions": spec.rebuild_instructions,
        "required": spec.required,
        "rubric_id": spec.rubric_id,
        "rubric_version": spec.rubric_version,
    }


def _case_spec_from_dict(payload: object) -> CaseSpec:
    data = _payload_mapping(payload, "CaseSpec", _CASE_SPEC_KEYS)
    return CaseSpec(
        case_id=_str_field(data, "case_id", "CaseSpec"),
        case_version=_str_field(data, "case_version", "CaseSpec"),
        lane=_str_field(data, "lane", "CaseSpec"),
        primary_category=_str_field(data, "primary_category", "CaseSpec"),
        fixture_version=_str_field(data, "fixture_version", "CaseSpec"),
        provenance=_str_field(data, "provenance", "CaseSpec"),
        rebuild_instructions=_str_field(data, "rebuild_instructions", "CaseSpec"),
        required=_bool_field(data, "required", "CaseSpec"),
        rubric_id=_optional_str_field(data, "rubric_id", "CaseSpec"),
        rubric_version=_optional_str_field(data, "rubric_version", "CaseSpec"),
    )


def _case_outcome_from_dict(payload: object) -> CaseOutcome:
    data = _payload_mapping(payload, "CaseOutcome", _CASE_OUTCOME_KEYS)
    raw_refs = data["evidence_refs"]
    if not isinstance(raw_refs, list) or not all(
        isinstance(ref, str) for ref in raw_refs
    ):
        raise AcceptanceInputError(
            "CaseOutcome.evidence_refs is not a list of strings"
        )
    return CaseOutcome(
        case_id=_str_field(data, "case_id", "CaseOutcome"),
        case_version=_str_field(data, "case_version", "CaseOutcome"),
        status=_str_field(data, "status", "CaseOutcome"),
        attempts=_int_field(data, "attempts", "CaseOutcome"),
        latest_reason_code=_str_field(data, "latest_reason_code", "CaseOutcome"),
        failure_class=_str_field(data, "failure_class", "CaseOutcome"),
        rubric_version=_str_field(data, "rubric_version", "CaseOutcome"),
        evidence_refs=tuple(raw_refs),
    )


def _payload_mapping(
    payload: object, label: str, expected: Sequence[str]
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise AcceptanceInputError(f"{label} payload is not a mapping")
    keys = set(payload)
    expected_keys = set(expected)
    if keys != expected_keys:
        missing = len(expected_keys - keys)
        unexpected = len(keys - expected_keys)
        raise AcceptanceInputError(
            f"{label} payload fields differ: {missing} missing, "
            f"{unexpected} unexpected"
        )
    return payload  # type: ignore[return-value]


def _str_field(payload: Mapping[str, object], key: str, label: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise AcceptanceInputError(f"{label}.{key} is not a string")
    return value


def _optional_str_field(
    payload: Mapping[str, object], key: str, label: str
) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise AcceptanceInputError(f"{label}.{key} is not a string or null")
    return value


def _bool_field(payload: Mapping[str, object], key: str, label: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise AcceptanceInputError(f"{label}.{key} is not a bool")
    return value


def _int_field(payload: Mapping[str, object], key: str, label: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise AcceptanceInputError(f"{label}.{key} is not an int")
    return value


# ---------------------------------------------------------------------------
# Shared predicates
# ---------------------------------------------------------------------------


def _raise_if_problems(problems: Sequence[str]) -> None:
    if problems:
        raise ManifestValidationError(problems)


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_version(value: str) -> bool:
    return _VERSION_RE.fullmatch(value) is not None


def _provenance_is_non_reproducible(provenance: object) -> bool:
    if not isinstance(provenance, str):
        return False
    lowered = provenance.lower()
    return any(marker in lowered for marker in _NON_REPRODUCIBLE_MARKERS)


def _sanitized_problem(label: str, text: object) -> str | None:
    if not isinstance(text, str):
        return f"{label} is not a string"
    if "\n" in text or "\r" in text:
        return f"{label} contains a newline"
    lowered = text.lower()
    if _CREDENTIAL_PATTERN.search(lowered):
        return f"{label} looks like a credential"
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return f"{label} looks like a credential"
    return None


def _sanitized_field_problems(label: str, text: object) -> list[str]:
    problem = _sanitized_problem(label, text)
    return [problem] if problem is not None else []
