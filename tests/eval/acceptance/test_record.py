"""Hermetic tests for building and persisting acceptance reports.

Reports are derived only from persisted attempts; nothing here reads
receipts, UI rows, traces, or a database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fin.eval.acceptance import (
    AcceptanceInputError,
    AttemptRecord,
    CaseManifest,
    CaseOutcome,
    Report,
    RunManifest,
    attempt_record,
    case_manifest,
    case_spec,
    run_manifest,
)
from fin.eval.acceptance.record import (
    build_report,
    load_report,
    summarize,
    write_report,
)
from fin.eval.acceptance.store import CampaignStore

RUN_ID = "run-1"


def _case(case_id: str, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "case_id": case_id,
        "case_version": "1.0.0",
        "lane": "mechanism",
        "primary_category": "same_thread_order",
        "fixture_version": "fixtures-single-doc@1.0.0",
        "provenance": "authorized synthetic fixture",
        "rebuild_instructions": "uv run python scripts/rebuild_fixture.py",
        "required": True,
    }
    fields.update(overrides)
    return case_spec(**fields)


def _manifest(
    case_ids: tuple[str, ...] = ("case-001", "case-002"), lane: str = "mechanism"
) -> CaseManifest:
    return case_manifest(
        manifest_version="1.0.0",
        lane=lane,
        dataset_manifest_version="1.0.0",
        cases=[_case(case_id, lane=lane) for case_id in case_ids],
        created_by="acceptance-test",
    )


def _run(lane: str = "mechanism", measured: bool = True) -> RunManifest:
    return run_manifest(
        run_id=RUN_ID,
        lane=lane,
        manifest_version="1.0.0",
        dataset_manifest_version="1.0.0",
        code_version="deadbeef",
        config_id="config-1",
        model_id="model-1",
        parser_id="parser-1",
        measured_run=measured,
    )


def _attempt(
    case_id: str = "case-001",
    attempt: int = 1,
    status: str = "passed",
    failure_class: str = "none",
    finished_at: str = "2026-10-21T10:00:05Z",
    reason_code: str | None = None,
    evidence_refs: tuple[str, ...] | None = None,
) -> AttemptRecord:
    return attempt_record(
        run_id=RUN_ID,
        case_id=case_id,
        case_version="1.0.0",
        manifest_version="1.0.0",
        attempt=attempt,
        status=status,
        reason_code=reason_code or ("ok" if status == "passed" else "case_failed"),
        failure_class=failure_class,
        evidence_refs=(
            evidence_refs
            if evidence_refs is not None
            else (f"evidence/{RUN_ID}/{case_id}/attempt-{attempt}.log",)
        ),
        execution_ids={"process_id": "4242"},
        started_at="2026-10-21T10:00:00Z",
        finished_at=finished_at,
        details={},
    )


def _store(
    tmp_path: Path,
    *,
    lane: str = "mechanism",
    measured: bool = True,
    case_ids: tuple[str, ...] = ("case-001", "case-002"),
) -> CampaignStore:
    store = CampaignStore(tmp_path / "acceptance")
    store.write_manifest(_manifest(case_ids, lane=lane))
    store.open_campaign(_run(lane=lane, measured=measured))
    return store


def _build(store: CampaignStore, **overrides: Any) -> Report:
    kwargs: dict[str, Any] = {
        "gate": "campaign",
    }
    kwargs.update(overrides)
    return build_report(store, _run(), _manifest(), **kwargs)


def _outcome(built: Report, case_id: str) -> CaseOutcome:
    matches = [item for item in built.case_outcomes if item.case_id == case_id]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------------------
# Denominators and missing cases
# ---------------------------------------------------------------------------


def test_every_required_case_gets_one_outcome_in_manifest_order(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-002"))

    built = _build(store)

    assert [item.case_id for item in built.case_outcomes] == ["case-001", "case-002"]
    assert built.denominator_total == 2
    assert built.counts["total"] == 2


def test_a_case_with_no_attempt_is_not_run_with_a_reason_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-002"))

    built = _build(store)
    missing = _outcome(built, "case-001")

    assert missing.status == "not_run"
    assert missing.latest_reason_code == "no_attempt"
    assert missing.attempts == 0
    assert built.counts["not_run"] == 1
    assert built.accepted is False


def test_case_with_no_attempt_keeps_spec_rubric_and_empty_evidence(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    manifest = case_manifest(
        manifest_version="1.0.0",
        lane="mechanism",
        dataset_manifest_version="1.0.0",
        cases=[
            _case("case-001", rubric_id="rubric-table", rubric_version="1.0.0"),
        ],
        created_by="acceptance-test",
    )
    store.write_manifest(manifest)
    store.open_campaign(_run())

    built = build_report(store, _run(), manifest, gate="campaign")
    missing = _outcome(built, "case-001")

    assert missing.status == "not_run"
    assert missing.rubric_version == "1.0.0"
    assert missing.evidence_refs == ()


def test_case_outcome_evidence_refs_are_sorted_deduplicated_union(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.record_attempt(
        _attempt(
            "case-001",
            attempt=1,
            evidence_refs=("evidence/b.log", "evidence/a.log"),
        )
    )
    store.record_attempt(
        _attempt(
            "case-001",
            attempt=2,
            evidence_refs=("evidence/c.log", "evidence/a.log"),
        )
    )
    store.record_attempt(_attempt("case-002"))

    built = _build(store)
    outcome = _outcome(built, "case-001")

    assert outcome.evidence_refs == (
        "evidence/a.log",
        "evidence/b.log",
        "evidence/c.log",
    )


def test_case_without_a_spec_rubric_version_round_trips_empty(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))

    built = _build(store)
    restored = Report.from_dict(json.loads(json.dumps(built.to_dict())))

    assert _outcome(built, "case-001").rubric_version == ""
    assert _outcome(restored, "case-001").rubric_version == ""


def test_report_round_trip_preserves_rubric_version_and_evidence_refs(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    manifest = case_manifest(
        manifest_version="1.0.0",
        lane="mechanism",
        dataset_manifest_version="1.0.0",
        cases=[_case("case-001", rubric_id="rubric-table", rubric_version="2.1.0")],
        created_by="acceptance-test",
    )
    store.write_manifest(manifest)
    store.open_campaign(_run())
    store.record_attempt(
        _attempt(
            "case-001",
            evidence_refs=("evidence/one.log", "evidence/two.log"),
        )
    )

    built = build_report(store, _run(), manifest, gate="campaign")
    restored = Report.from_dict(json.loads(json.dumps(built.to_dict())))

    assert restored == built
    restored_outcome = _outcome(restored, "case-001")
    assert restored_outcome.rubric_version == "2.1.0"
    assert restored_outcome.evidence_refs == (
        "evidence/one.log",
        "evidence/two.log",
    )


def test_report_lane_follows_the_manifest_lane(tmp_path: Path) -> None:
    store = _store(tmp_path, lane="browser")
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))

    built = build_report(
        store, _run(lane="browser"), _manifest(lane="browser"), gate="campaign"
    )

    assert built.lane == "browser"


def test_build_report_rejects_a_run_from_another_lane(tmp_path: Path) -> None:
    store = _store(tmp_path, lane="browser")

    with pytest.raises(AcceptanceInputError):
        build_report(store, _run(), _manifest(lane="browser"), gate="campaign")


def test_build_report_rejects_a_manifest_that_differs_from_the_stored_one(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(AcceptanceInputError):
        build_report(
            store, _run(), _manifest(("case-001",)), gate="campaign"
        )


def test_build_report_rejects_an_empty_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(AcceptanceInputError):
        _build(store, gate="")


def test_build_report_rejects_a_credential_bearing_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(AcceptanceInputError) as info:
        _build(store, gate="postgres://user@host/db")
    assert "postgres://" not in str(info.value)


# ---------------------------------------------------------------------------
# Accepted is false for every non-passed state
# ---------------------------------------------------------------------------


_NON_PASSED = (
    ("failed", "assertion", None),
    ("blocked", "unavailable_capability", None),
    ("interrupted", "cancelled", None),
    ("not_run", "none", "fixture_missing"),
)


@pytest.mark.parametrize(("status", "failure_class", "reason_code"), _NON_PASSED)
def test_report_is_not_accepted_for_each_single_attempt_non_pass(
    tmp_path: Path, status: str, failure_class: str, reason_code: str | None
) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(
        _attempt(
            "case-002",
            status=status,
            failure_class=failure_class,
            reason_code=reason_code,
        )
    )

    built = _build(store)

    assert _outcome(built, "case-002").status == status
    assert built.counts[status] == 1
    assert built.counts["passed"] == 1
    assert built.accepted is False


def test_rerun_after_failure_reports_flaky_and_is_not_accepted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(
        _attempt("case-002", status="failed", failure_class="assertion")
    )
    store.record_attempt(_attempt("case-002", attempt=2))

    built = _build(store)

    assert _outcome(built, "case-002").status == "flaky"
    assert built.counts["flaky"] == 1
    assert built.counts["passed"] == 1
    assert built.accepted is False


def test_flaky_case_keeps_both_attempts_visible(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001", status="failed", failure_class="assertion"))
    store.record_attempt(_attempt("case-001", attempt=2))
    store.record_attempt(_attempt("case-002"))

    built = _build(store)
    flaky = _outcome(built, "case-001")

    assert flaky.status == "flaky"
    assert flaky.attempts == 2
    assert len(store.load_attempts(RUN_ID)) == 3
    assert built.accepted is False


def test_unmeasured_run_cannot_accept_even_when_every_case_passed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, measured=False)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))

    built = build_report(
        store, _run(measured=False), _manifest(), gate="campaign"
    )

    assert built.accepted is False
    assert built.counts["passed"] == 0


def test_full_measured_pass_is_accepted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))

    built = _build(store)

    assert built.accepted is True
    assert built.counts["passed"] == 2
    assert built.denominator_total == 2
    assert built.contract_version == "1.0.0"


def test_only_required_cases_count_toward_the_denominator(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    manifest = case_manifest(
        manifest_version="1.0.0",
        lane="mechanism",
        dataset_manifest_version="1.0.0",
        cases=[
            _case("case-001"),
            _case(
                "case-optional",
                required=False,
                provenance="recorded non-reproducible incident",
                rebuild_instructions="",
            ),
        ],
        created_by="acceptance-test",
    )
    store.write_manifest(manifest)
    store.open_campaign(_run())
    store.record_attempt(_attempt("case-001"))

    built = build_report(store, _run(), manifest, gate="campaign")

    assert [item.case_id for item in built.case_outcomes] == ["case-001"]
    assert built.denominator_total == 1
    assert built.accepted is True


# ---------------------------------------------------------------------------
# Report identity
# ---------------------------------------------------------------------------


def test_report_ids_are_unique_per_run_and_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))

    first = build_report(store, _run(), _manifest(), gate="campaign")
    write_report(store, first)
    second = build_report(store, _run(), _manifest(), gate="campaign")
    assert second.report_id != first.report_id

    write_report(store, second)
    third = build_report(store, _run(), _manifest(), gate="campaign")

    assert {first.report_id, second.report_id, third.report_id} == {
        f"mechanism:{RUN_ID}:report-1",
        f"mechanism:{RUN_ID}:report-2",
        f"mechanism:{RUN_ID}:report-3",
    }


def test_report_ids_are_scoped_to_the_lane_and_run(tmp_path: Path) -> None:
    store = _store(tmp_path, lane="business")
    store.record_attempt(_attempt("case-001"))

    built = build_report(
        store, _run(lane="business"), _manifest(lane="business"), gate="campaign"
    )

    assert built.report_id == f"business:{RUN_ID}:report-1"


def test_write_report_refuses_to_overwrite_an_existing_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))
    built = build_report(store, _run(), _manifest(), gate="campaign")
    path = write_report(store, built)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(AcceptanceInputError):
        write_report(store, built)

    assert json.loads(path.read_text(encoding="utf-8"))["report_id"] == built.report_id
    assert path.read_text(encoding="utf-8") == before


def test_written_report_round_trips_through_load_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))
    built = build_report(store, _run(), _manifest(), gate="campaign")

    path = write_report(store, built)

    assert load_report(path) == built


def test_atomic_report_write_leaves_no_temp_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002"))
    write_report(store, build_report(store, _run(), _manifest(), gate="campaign"))

    assert list(store.root.rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def test_summarize_reports_every_status_denominator_and_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002", status="failed", failure_class="assertion"))
    built = build_report(store, _run(), _manifest(), gate="campaign")

    summary = summarize(built)

    assert summary["report_id"] == built.report_id
    assert summary["lane"] == "mechanism"
    assert summary["run_id"] == RUN_ID
    assert summary["gate"] == "campaign"
    assert summary["denominator_total"] == 2
    assert summary["accepted"] is False
    assert summary["counts"] == {
        "passed": 1,
        "failed": 1,
        "not_run": 0,
        "blocked": 0,
        "interrupted": 0,
        "flaky": 0,
        "total": 2,
    }
