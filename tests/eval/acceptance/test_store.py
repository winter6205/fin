"""Hermetic tests for the append-only campaign evidence store.

Every store instance writes under ``tmp_path``; nothing here touches the
real ``~/.fin`` tree, PostgreSQL, or the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fin.eval.acceptance import (
    AcceptanceInputError,
    AttemptRecord,
    CaseManifest,
    RunManifest,
    attempt_record,
    case_manifest,
    case_spec,
    run_manifest,
)
from fin.eval.acceptance.store import CampaignStartBlocked, CampaignStore


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


def _manifest(case_ids: tuple[str, ...] = ("case-001", "case-002")) -> CaseManifest:
    return case_manifest(
        manifest_version="1.0.0",
        lane="mechanism",
        dataset_manifest_version="1.0.0",
        cases=[_case(case_id) for case_id in case_ids],
        created_by="acceptance-test",
    )


def _run(run_id: str = "run-1", lane: str = "mechanism") -> RunManifest:
    return run_manifest(
        run_id=run_id,
        lane=lane,
        manifest_version="1.0.0",
        dataset_manifest_version="1.0.0",
        code_version="deadbeef",
        config_id="config-1",
        model_id="model-1",
        parser_id="parser-1",
        measured_run=True,
    )


def _attempt(
    case_id: str = "case-001",
    attempt: int = 1,
    status: str = "passed",
    failure_class: str = "none",
    run_id: str = "run-1",
    finished_at: str = "2026-10-21T10:00:05Z",
    **overrides: Any,
) -> AttemptRecord:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "case_id": case_id,
        "case_version": "1.0.0",
        "manifest_version": "1.0.0",
        "attempt": attempt,
        "status": status,
        "reason_code": "ok" if status == "passed" else "case_failed",
        "failure_class": failure_class,
        "evidence_refs": (f"evidence/{run_id}/{case_id}/attempt-{attempt}.log",),
        "execution_ids": {"process_id": "4242"},
        "started_at": "2026-10-21T10:00:00Z",
        "finished_at": finished_at,
        "details": {},
    }
    fields.update(overrides)
    return attempt_record(**fields)


def _opened_store(tmp_path: Path) -> CampaignStore:
    store = CampaignStore(tmp_path / "acceptance")
    store.write_manifest(_manifest())
    store.open_campaign(_run())
    return store


# ---------------------------------------------------------------------------
# Run manifests
# ---------------------------------------------------------------------------


def test_open_campaign_writes_the_run_manifest(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    path = store.open_campaign(_run())

    assert isinstance(path, str)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["measured_run"] is True
    assert store.load_run("run-1") == _run()


def test_open_campaign_is_idempotent_for_an_identical_payload(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    first = store.open_campaign(_run())
    before = Path(first).read_text(encoding="utf-8")
    second = store.open_campaign(_run())

    assert second == first
    assert Path(first).read_text(encoding="utf-8") == before


def test_open_campaign_rejects_a_different_payload_for_the_same_run_id(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    first = store.open_campaign(_run())
    changed = run_manifest(
        run_id="run-1",
        lane="mechanism",
        manifest_version="1.0.0",
        dataset_manifest_version="1.0.0",
        code_version="newcode",
        config_id="config-1",
        model_id="model-1",
        parser_id="parser-1",
        measured_run=True,
    )

    with pytest.raises(AcceptanceInputError):
        store.open_campaign(changed)
    # The original manifest is untouched.
    stored = json.loads(Path(first).read_text(encoding="utf-8"))
    assert stored["code_version"] == "deadbeef"


def test_open_campaign_rejects_an_unsafe_run_id(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    unsafe = run_manifest(
        run_id="../../escape",
        lane="mechanism",
        manifest_version="1.0.0",
        dataset_manifest_version="1.0.0",
        code_version="deadbeef",
        config_id="config-1",
        model_id="model-1",
        parser_id="parser-1",
    )

    with pytest.raises(AcceptanceInputError) as info:
        store.open_campaign(unsafe)
    assert "escape" not in str(info.value)


def test_list_runs_filters_by_lane(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    store.open_campaign(_run("run-1", "mechanism"))
    store.open_campaign(_run("run-2", "mechanism"))
    store.open_campaign(
        run_manifest(
            run_id="run-3",
            lane="business",
            manifest_version="1.0.0",
            dataset_manifest_version="1.0.0",
            code_version="deadbeef",
            config_id="config-1",
            model_id="model-1",
            parser_id="parser-1",
        )
    )

    assert store.list_runs() == ("run-1", "run-2", "run-3")
    assert store.list_runs("mechanism") == ("run-1", "run-2")
    assert store.list_runs("business") == ("run-3",)
    assert store.list_runs("browser") == ()


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------


def test_attempts_load_in_append_order(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt("case-002", finished_at="2026-10-21T10:00:01Z"))
    store.record_attempt(_attempt("case-001", finished_at="2026-10-21T10:00:02Z"))
    store.record_attempt(
        _attempt("case-001", attempt=2, finished_at="2026-10-21T10:00:03Z")
    )

    loaded = store.load_attempts("run-1")

    assert [(rec.case_id, rec.attempt) for rec in loaded] == [
        ("case-002", 1),
        ("case-001", 1),
        ("case-001", 2),
    ]


def test_attempt_is_on_disk_when_record_attempt_returns(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt())

    lines = store.attempt_log_path("mechanism").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1
    assert json.loads(lines[0])["case_id"] == "case-001"


def test_fresh_process_reads_back_the_attempt_log(tmp_path: Path) -> None:
    root = tmp_path / "acceptance"
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt())

    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from fin.eval.acceptance.store import CampaignStore\n"
        "store = CampaignStore(Path(sys.argv[1]))\n"
        "print(len(store.load_attempts(sys.argv[2])))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), "run-1"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "1"


def test_duplicate_attempt_is_rejected_and_never_rewritten(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt())

    with pytest.raises(AcceptanceInputError):
        store.record_attempt(_attempt(status="failed", failure_class="assertion"))

    loaded = store.load_attempts("run-1")
    assert len(loaded) == 1
    assert loaded[0].status == "passed"


def test_rerun_appends_a_second_attempt(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt(status="failed", failure_class="assertion"))
    store.record_attempt(_attempt(attempt=2))

    loaded = store.load_attempts("run-1")

    assert [rec.attempt for rec in loaded] == [1, 2]
    assert [rec.status for rec in loaded] == ["failed", "passed"]


def test_record_attempt_requires_an_open_run(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    store.write_manifest(_manifest())

    with pytest.raises(AcceptanceInputError):
        store.record_attempt(_attempt(run_id="unknown-run"))


def test_record_attempt_requires_the_registered_manifest(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    store.open_campaign(_run())

    with pytest.raises(AcceptanceInputError):
        store.record_attempt(_attempt())


def test_record_attempt_rejects_an_unknown_case(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)

    with pytest.raises(AcceptanceInputError):
        store.record_attempt(_attempt("case-999"))


def test_record_attempt_rejects_a_case_version_mismatch(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)

    with pytest.raises(AcceptanceInputError):
        store.record_attempt(_attempt(case_version="2.0.0"))


def test_malformed_line_is_rejected_with_its_number_and_without_content(
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    log = store.attempt_log_path("mechanism")
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"broken": SHOULD_NOT_LEAK\n')

    with pytest.raises(AcceptanceInputError) as info:
        store.load_attempts("run-1")

    message = str(info.value)
    assert "line 2" in message
    assert "SHOULD_NOT_LEAK" not in message


def test_schema_invalid_line_is_rejected_with_its_number(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    log = store.attempt_log_path("mechanism")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"run_id": "run-1"}\n', encoding="utf-8")

    with pytest.raises(AcceptanceInputError) as info:
        store.load_attempts("run-1")

    assert "line 1" in str(info.value)


def test_load_attempts_of_an_unknown_run_is_empty(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)

    assert store.load_attempts("never-opened") == ()


def test_one_lane_log_holds_two_runs_and_each_run_reads_back_only_its_own(
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    store.open_campaign(_run("run-2"))
    store.record_attempt(_attempt("case-001"))
    store.record_attempt(_attempt("case-002", run_id="run-2"))

    lines = store.attempt_log_path("mechanism").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert [rec.run_id for rec in store.load_attempts("run-1")] == ["run-1"]
    assert [rec.run_id for rec in store.load_attempts("run-2")] == ["run-2"]


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_pending_case_ids_lists_unfinished_cases_in_required_order(
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt("case-002"))

    pending = store.pending_case_ids("run-1", ("case-001", "case-002"))

    assert pending == ("case-001",)


def test_pending_case_ids_after_a_partial_campaign_then_resume(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    store.record_attempt(_attempt("case-001"))
    pending = store.pending_case_ids("run-1", ("case-001", "case-002"))

    for case_id in pending:
        store.record_attempt(_attempt(case_id))

    assert store.pending_case_ids("run-1", ("case-001", "case-002")) == ()
    assert [(rec.case_id, rec.attempt) for rec in store.load_attempts("run-1")] == [
        ("case-001", 1),
        ("case-002", 1),
    ]


def test_pending_case_ids_requires_an_open_run(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")

    with pytest.raises(AcceptanceInputError):
        store.pending_case_ids("unknown-run", ("case-001",))


def test_pending_case_ids_rejects_a_case_outside_the_run_manifest(
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)

    with pytest.raises(AcceptanceInputError):
        store.pending_case_ids("run-1", ("case-001", "case-999"))


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


def test_competing_campaign_start_is_blocked_then_allowed_after_release(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "acceptance")

    with store.campaign_lock("mechanism"):
        with pytest.raises(CampaignStartBlocked) as info:
            with store.campaign_lock("mechanism"):
                pytest.fail("a second campaign must not start")
        assert "mechanism" in str(info.value)

    with store.campaign_lock("mechanism"):
        pass

    assert store.campaign_lock("mechanism") is not None


def test_lane_locks_do_not_block_each_other(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")

    with store.campaign_lock("mechanism"):
        with store.campaign_lock("browser"):
            with store.campaign_lock("business"):
                pass


def test_campaign_lock_rejects_an_unknown_lane(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")

    with pytest.raises(AcceptanceInputError):
        with store.campaign_lock("release"):
            pass


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def test_whole_file_writes_leave_no_temp_files(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    store.write_manifest(_manifest())
    store.open_campaign(_run())
    store.record_attempt(_attempt())

    leftovers = [path for path in store.root.rglob("*.tmp")]

    assert leftovers == []


def test_write_manifest_is_idempotent_and_rejects_a_changed_payload(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "acceptance")
    first = store.write_manifest(_manifest())

    assert store.write_manifest(_manifest()) == first
    with pytest.raises(AcceptanceInputError):
        store.write_manifest(_manifest(("case-001", "case-003")))
    assert store.load_manifest("1.0.0") == _manifest()
