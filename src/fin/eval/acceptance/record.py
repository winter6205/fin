"""Report construction from stored campaign evidence.

A report counts every required case in its manifest: a case with no
attempt is ``not_run`` with reason ``no_attempt``, so the denominator is
the manifest, never the number of rows that happened to run. Reports are
built only from attempts already persisted in the store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from fin.eval.acceptance.protocol import (
    AcceptanceInputError,
    AttemptRecord,
    CaseManifest,
    CaseOutcome,
    CaseSpec,
    Report,
    RunManifest,
    count_statuses,
    derive_case_status,
    ensure_sanitized,
    manifest_run_mismatches,
    report,
)
from fin.eval.acceptance.store import (
    CampaignStore,
    read_json,
    write_json_exclusive,
)

NO_ATTEMPT_REASON = "no_attempt"

_SUMMARY_KEYS = (
    "report_id",
    "lane",
    "run_id",
    "gate",
    "denominator_total",
    "accepted",
)


def _required_cases(manifest: CaseManifest) -> tuple[CaseSpec, ...]:
    return tuple(spec for spec in manifest.cases if spec.required)


def _outcome(
    spec: CaseSpec, attempts: Sequence[AttemptRecord], *, measured: bool
) -> CaseOutcome:
    evidence_refs = tuple(
        sorted({ref for record in attempts for ref in record.evidence_refs})
    )
    if not attempts:
        return CaseOutcome(
            case_id=spec.case_id,
            case_version=spec.case_version,
            status="not_run",
            attempts=0,
            latest_reason_code=NO_ATTEMPT_REASON,
            failure_class="none",
            rubric_version=spec.rubric_version or "",
            evidence_refs=evidence_refs,
        )
    latest = sorted(attempts, key=lambda rec: (rec.attempt, rec.finished_at))[-1]
    return CaseOutcome(
        case_id=spec.case_id,
        case_version=spec.case_version,
        status=derive_case_status(attempts, declared_measured_run=measured),
        attempts=len(attempts),
        latest_reason_code=ensure_sanitized(latest.reason_code),
        failure_class=latest.failure_class,
        rubric_version=spec.rubric_version or "",
        evidence_refs=evidence_refs,
    )


def _checked_gate(run: RunManifest, manifest: CaseManifest, gate: str) -> str:
    mismatches = manifest_run_mismatches(run, manifest)
    if mismatches:
        raise AcceptanceInputError(mismatches[0])
    if not gate.strip():
        raise AcceptanceInputError("gate is empty")
    return ensure_sanitized(gate)


def build_report(
    store: CampaignStore,
    run: RunManifest,
    manifest: CaseManifest,
    *,
    gate: str,
    measured_run_id: str | None = None,
) -> Report:
    """Derive one report for ``run`` from the attempts persisted in ``store``.

    One outcome is emitted per required case in manifest order; a case
    with no attempt is ``not_run`` with reason ``no_attempt``. ``accepted``
    requires every required case to pass in a declared measured run.
    """
    checked_gate = _checked_gate(run, manifest, gate)
    if measured_run_id is not None:
        if ensure_sanitized(measured_run_id) != run.run_id:
            raise AcceptanceInputError(
                "measured_run_id differs from the run being reported"
            )
    if store.load_run(run.run_id) != run:
        raise AcceptanceInputError("run manifest differs from the stored run")
    if store.load_manifest(manifest.manifest_version) != manifest:
        raise AcceptanceInputError(
            "case manifest differs from the registered manifest"
        )

    required = _required_cases(manifest)
    by_case: dict[str, list[AttemptRecord]] = {spec.case_id: [] for spec in required}
    for record in store.load_attempts(run.run_id):
        if record.case_id in by_case:
            by_case[record.case_id].append(record)

    outcomes = tuple(
        _outcome(spec, by_case[spec.case_id], measured=run.measured_run)
        for spec in required
    )
    every_passed = bool(outcomes) and all(
        outcome.status == "passed" for outcome in outcomes
    )
    return report(
        report_id=(
            f"{manifest.lane}:{run.run_id}:"
            f"report-{store.count_reports(manifest.lane, run.run_id) + 1}"
        ),
        lane=manifest.lane,
        run_id=run.run_id,
        manifest_version=manifest.manifest_version,
        case_outcomes=outcomes,
        gate=checked_gate,
        counts=count_statuses(outcomes),
        denominator_total=len(required),
        accepted=every_passed and run.measured_run,
    )


def write_report(store: CampaignStore, built: Report) -> Path:
    """Atomically write ``built``; refuse to overwrite an existing report id."""
    path = store.report_path(built.lane, built.run_id, built.report_id)
    try:
        return write_json_exclusive(path, built.to_dict())
    except FileExistsError as exc:
        raise AcceptanceInputError("a report with this id already exists") from exc


def load_report(path: Path) -> Report:
    """Read back a written report."""
    return Report.from_dict(read_json(Path(path), "report"))


def summarize(built: Report) -> dict[str, object]:
    """Compact operator summary: id, gate, denominator, acceptance, counts."""
    summary: dict[str, object] = {key: getattr(built, key) for key in _SUMMARY_KEYS}
    summary["counts"] = dict(built.counts) if isinstance(built.counts, Mapping) else {}
    return summary


__all__ = [
    "NO_ATTEMPT_REASON",
    "build_report",
    "load_report",
    "summarize",
    "write_report",
]
