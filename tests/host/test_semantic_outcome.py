"""Semantic outcome on a run: the column, not the technical status.

A5: write ``insufficient_evidence`` only for a valid assessment with no
host-gate failure and a ``material_gap`` or ``unsupported`` reason.
Anything else stays unknown (``None``, stored as NULL). No live model.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import fin.host.thread_store as thread_store
from fin.host.graph import apply_task_outcome
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input, decide, semantic_outcome
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    TASK_ACTIVE,
    TASK_CANCELLED,
    TASK_CAPABILITY_UNSUPPORTED,
    TASK_CHECKPOINT_FAILED,
    TASK_COMPLETE,
    GroundingAssessment,
    ReconciliationProposal,
    TaskRuntimeState,
)
from fin.host.thread_store import PostgresThreadStore, RunRecord, RunStatus, _SCHEMA_SQL

_GATES_PASSED = {
    "permission": True,
    "source_identity_version": True,
    "receipts": True,
    "persistence": True,
}


def test_material_gap_with_gates_passed_is_insufficient_evidence() -> None:
    column = semantic_outcome(
        assessment=GroundingAssessment(
            supported=True,
            material_gaps=("2023 segment missing",),
        ),
        assessment_valid=True,
        gate_failures=(),
    )
    assert column == "insufficient_evidence"


def test_unsupported_with_gates_passed_is_insufficient_evidence() -> None:
    column = semantic_outcome(
        assessment=GroundingAssessment(supported=False),
        assessment_valid=True,
        gate_failures=(),
    )
    assert column == "insufficient_evidence"


def test_gate_failure_stays_unknown_even_when_the_assessment_is_a_gap() -> None:
    column = semantic_outcome(
        assessment=GroundingAssessment(
            supported=True,
            material_gaps=("2023 segment missing",),
        ),
        assessment_valid=True,
        gate_failures=("scope_source_ref_unreadable",),
    )
    assert column is None


def test_a_supported_assessment_with_no_gap_is_not_insufficient_evidence() -> None:
    column = semantic_outcome(
        assessment=GroundingAssessment(supported=True),
        assessment_valid=True,
        gate_failures=(),
    )
    assert column is None


def test_a_missing_or_invalid_assessment_is_unknown() -> None:
    missing = semantic_outcome(
        assessment=None,
        assessment_valid=False,
        gate_failures=(),
    )
    invalid = semantic_outcome(
        assessment=GroundingAssessment(supported=False),
        assessment_valid=False,
        gate_failures=(),
    )
    assert missing is None
    assert invalid is None


def _active_runtime(reason: str) -> TaskRuntimeState:
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="compare revenue",
        provenance="send-door",
    )
    decided = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue"),
    )
    assert decided.current_task is not None
    return replace(
        decided,
        current_task=replace(decided.current_task, outcome_reason=reason),
    )


def test_missing_assessment_stays_unknown_and_keeps_the_task_reason() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "scope_source_ref_unreadable",
        }
    )
    assert update.get("semantic_outcome") is None
    assert "status" not in update
    assert "status_reason" not in update
    assert "task_runtime" not in update
    assert runtime.current_task is not None
    assert runtime.current_task.outcome_reason == "prior-reason"
    assert runtime.current_task.outcome == TASK_ACTIVE


def test_malformed_assessment_stays_unknown_and_keeps_the_task_reason() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {"supported": "yes", "material_gaps": []},
            "host_gates": dict(_GATES_PASSED),
        }
    )
    assert update.get("semantic_outcome") is None
    assert "task_runtime" not in update
    assert "status" not in update
    assert runtime.current_task is not None
    assert runtime.current_task.outcome_reason == "prior-reason"


def test_scope_unreadable_writes_unknown_and_keeps_the_run_reason() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "scope_source_ref_unreadable",
            "grounding_assessment": {"supported": False},
            "host_gates": {**_GATES_PASSED, "source_identity_version": False},
        }
    )
    assert update["semantic_outcome"] is None
    assert "status" not in update
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome == TASK_ACTIVE
    assert committed.current_task.outcome_reason == TASK_CAPABILITY_UNSUPPORTED
    assert committed.current_task.outcome_reason != "insufficient_evidence"


def test_checkpoint_write_failure_writes_unknown_not_insufficient_evidence() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "checkpoint_write_failed",
            "grounding_assessment": {
                "supported": True,
                "material_gaps": ["2023 segment missing"],
            },
            "host_gates": {**_GATES_PASSED, "persistence": False},
        }
    )
    assert update["semantic_outcome"] is None
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome_reason == TASK_CHECKPOINT_FAILED


def test_material_gap_through_the_node_is_insufficient_evidence_and_incomplete() -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {
                "supported": True,
                "material_gaps": ["2023 segment missing"],
            },
            "host_gates": dict(_GATES_PASSED),
        }
    )
    assert update["semantic_outcome"] == "insufficient_evidence"
    assert "status" not in update
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.task_outcome == TASK_ACTIVE
    assert committed.current_task.outcome_reason == "material_gap"


def test_a_question_with_evidence_does_not_get_insufficient_evidence() -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {"supported": True, "material_gaps": []},
            "host_gates": dict(_GATES_PASSED),
        }
    )
    assert update["semantic_outcome"] is None
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.task_outcome == TASK_COMPLETE
    assert committed.current_task is not None
    assert committed.current_task.outcome == TASK_COMPLETE


def test_fresh_and_existing_tables_add_a_nullable_column_without_a_string_default() -> None:
    create, _, alters = _SCHEMA_SQL.partition("CREATE TABLE IF NOT EXISTS fin_evidence")
    assert "semantic_outcome TEXT," in create or "semantic_outcome TEXT\n" in create
    assert "semantic_outcome TEXT NOT NULL" not in _SCHEMA_SQL
    assert "semantic_outcome TEXT NOT NULL" not in alters
    assert "ADD COLUMN IF NOT EXISTS semantic_outcome TEXT;" in _SCHEMA_SQL
    assert "DEFAULT 'unknown'" not in _SCHEMA_SQL
    assert "DEFAULT 'insufficient_evidence'" not in _SCHEMA_SQL


def test_opening_the_store_runs_the_alter_twice(monkeypatch) -> None:
    seen: list[str] = []

    class _Conn:
        def execute(self, sql, params=None):
            seen.append(sql)

            class _Result:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return _Result()

        def commit(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(thread_store.psycopg, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(
        thread_store, "ensure_instance_identity", lambda *_args, **_kwargs: ""
    )
    PostgresThreadStore("postgresql://stub")
    PostgresThreadStore("postgresql://stub")
    alters = [
        sql for sql in seen if "ADD COLUMN IF NOT EXISTS semantic_outcome TEXT" in sql
    ]
    assert len(alters) == 2


class _SharedRuns:
    def __init__(self) -> None:
        self.rows: list[tuple] = []


def _bind_shared_store(monkeypatch, db: _SharedRuns):
    class _Conn:
        def execute(self, sql, params=None):
            if params is not None and str(sql).startswith("INSERT INTO fin_run"):
                db.rows.append(tuple(params))

            class _Result:
                def fetchone(_self):
                    if "FROM fin_thread" in str(sql):
                        return (1,)
                    return None

                def fetchall(_self):
                    if "FROM fin_run" in str(sql):
                        return list(db.rows)
                    return []

            return _Result()

        def commit(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(thread_store.psycopg, "connect", lambda *a, **k: _Conn())

    def open_store() -> PostgresThreadStore:
        store = PostgresThreadStore.__new__(PostgresThreadStore)
        store._url = "postgresql://stub"
        return store

    return open_store


def test_semantic_outcome_survives_a_new_store_after_the_checkpoint_is_cleared(
    monkeypatch,
) -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {
                "supported": True,
                "material_gaps": ["2023 segment missing"],
            },
            "host_gates": dict(_GATES_PASSED),
        }
    )
    checkpoint = {"semantic_outcome": update["semantic_outcome"], "task_runtime": update["task_runtime"]}
    open_store = _bind_shared_store(monkeypatch, _SharedRuns())
    writer = open_store()
    writer.append_run(
        RunRecord(
            id="r-1",
            thread_id="t-1",
            question="what was segment revenue",
            status="done",
            status_reason="done",
            semantic_outcome=update["semantic_outcome"],
        )
    )
    checkpoint.clear()
    loaded = open_store().list_runs("t-1")[0]
    assert checkpoint == {}
    assert loaded.semantic_outcome == "insufficient_evidence"
    assert loaded.semantic_outcome is not None


def test_a_null_column_reads_as_unknown_not_a_backfilled_string(monkeypatch) -> None:
    open_store = _bind_shared_store(monkeypatch, _SharedRuns())
    writer = open_store()
    writer.append_run(
        RunRecord(
            id="r-old",
            thread_id="t-1",
            question="q",
            status="done",
            semantic_outcome=None,
        )
    )
    loaded = open_store().list_runs("t-1")[0]
    assert loaded.semantic_outcome is None


def test_a_row_from_before_the_column_reads_as_unknown() -> None:
    stamp = datetime.now(timezone.utc)
    legacy = (
        "r",
        "t",
        "q",
        "done",
        "",
        "",
        "[]",
        "",
        "[]",
        "",
        stamp,
        False,
        0,
        "{}",
        "",
        "[]",
        "[]",
        "[]",
        "[]",
        "[]",
        "{}",
    )
    assert PostgresThreadStore._row_to_run(legacy).semantic_outcome is None
    assert PostgresThreadStore._row_to_run(legacy + (None,)).semantic_outcome is None


def test_five_situations_do_not_share_one_stored_pair(monkeypatch) -> None:
    """Technical status is (status, status_reason). Stop and a backend
    failure both have semantic NULL; ``stopped`` keeps them apart.
    """
    open_store = _bind_shared_store(monkeypatch, _SharedRuns())
    writer = open_store()
    pairs: list[tuple[str, str, str | None, str]] = []
    for index, (status, reason, state, task_outcome) in enumerate(_five_situations()):
        update = apply_task_outcome(state)
        run_id = f"r-{index}"
        writer.append_run(
            RunRecord(
                id=run_id,
                thread_id="t-five",
                question=f"q-{index}",
                status=status,
                status_reason=reason,
                semantic_outcome=update.get("semantic_outcome"),
            )
        )
        committed = state["task_runtime"]
        if "task_runtime" in update:
            committed = update["task_runtime"]
        task = TaskRuntimeState.from_state_dict(committed)
        assert task.current_task is not None
        pairs.append(
            (status, reason, update.get("semantic_outcome"), task.current_task.outcome)
        )
        assert task.task_outcome == task_outcome or task.current_task.outcome == task_outcome
    loaded = open_store().list_runs("t-five")
    stored = [
        (run.status, run.status_reason, run.semantic_outcome) for run in loaded
    ]
    assert len(set(stored)) == 5
    assert stored == [
        ("done", "done", None),
        ("done", "done", "insufficient_evidence"),
        ("refuse", "citation_repair_cap", "insufficient_evidence"),
        ("refuse", "scope_source_ref_unreadable", None),
        ("refuse", "stopped", None),
    ]
    assert pairs[0][3] == TASK_COMPLETE
    assert pairs[1][3] == TASK_ACTIVE
    assert pairs[0][2] is None
    assert pairs[1][2] == "insufficient_evidence"


def _five_situations() -> list[tuple[RunStatus, str, dict[str, Any], str]]:
    runtime = _active_runtime("").to_state_dict()

    def state(assessment, gates, status, reason) -> dict:
        body = {
            "task_runtime": runtime,
            "status": status,
            "status_reason": reason,
            "host_gates": gates,
        }
        if assessment is not None:
            body["grounding_assessment"] = assessment
        return body

    passed = dict(_GATES_PASSED)
    return [
        (
            "done",
            "done",
            state({"supported": True, "material_gaps": []}, passed, "done", "done"),
            TASK_COMPLETE,
        ),
        (
            "done",
            "done",
            state(
                {"supported": True, "material_gaps": ["2023 segment missing"]},
                passed,
                "done",
                "done",
            ),
            TASK_ACTIVE,
        ),
        (
            "refuse",
            "citation_repair_cap",
            state({"supported": False}, passed, "refuse", "citation_repair_cap"),
            TASK_ACTIVE,
        ),
        (
            "refuse",
            "scope_source_ref_unreadable",
            state(
                {"supported": False},
                {**passed, "source_identity_version": False},
                "refuse",
                "scope_source_ref_unreadable",
            ),
            TASK_ACTIVE,
        ),
        (
            "refuse",
            "stopped",
            state(None, {}, "refuse", "stopped"),
            TASK_ACTIVE,
        ),
    ]


def test_a_malformed_gate_snapshot_does_not_become_insufficient_evidence() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "scope_source_ref_unreadable",
            "grounding_assessment": {"supported": False},
            "host_gates": {"permission": "yes"},
        }
    )
    assert update["semantic_outcome"] is None
    assert "status_reason" not in update


def test_each_send_clears_the_semantic_channel() -> None:
    assert reset_input_state("q")["semantic_outcome"] is None


def test_absent_snapshot_material_gap_is_insufficient_evidence() -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {
                "supported": True,
                "material_gaps": ["2023 segment missing"],
            },
        }
    )
    assert update["semantic_outcome"] == "insufficient_evidence"
    assert "status" not in update
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome == TASK_ACTIVE
    assert committed.current_task.outcome_reason == "material_gap"


def test_empty_snapshot_citation_refuse_is_insufficient_evidence() -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "citation_repair_cap",
            "grounding_assessment": {"supported": False},
            "host_gates": {},
        }
    )
    assert update["semantic_outcome"] == "insufficient_evidence"
    assert "status" not in update
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome_reason == "unsupported"


def test_absent_snapshot_supported_answer_completes_the_task() -> None:
    runtime = _active_runtime("")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {"supported": True, "material_gaps": []},
        }
    )
    assert update["semantic_outcome"] is None
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.task_outcome == TASK_COMPLETE
    assert committed.current_task is not None
    assert committed.current_task.outcome == TASK_COMPLETE


def test_absent_snapshot_scope_unreadable_stays_unknown() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "scope_source_ref_unreadable",
            "grounding_assessment": {"supported": False},
        }
    )
    assert update["semantic_outcome"] is None
    assert "status_reason" not in update
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome == TASK_ACTIVE
    assert committed.current_task.outcome_reason == TASK_CAPABILITY_UNSUPPORTED


def test_absent_snapshot_checkpoint_failure_stays_unknown() -> None:
    runtime = _active_runtime("prior-reason")
    update = apply_task_outcome(
        {
            "task_runtime": runtime.to_state_dict(),
            "status": "refuse",
            "status_reason": "checkpoint_write_failed",
            "grounding_assessment": {
                "supported": True,
                "material_gaps": ["2023 segment missing"],
            },
        }
    )
    assert update["semantic_outcome"] is None
    committed = TaskRuntimeState.from_state_dict(update["task_runtime"])
    assert committed.current_task is not None
    assert committed.current_task.outcome_reason == TASK_CHECKPOINT_FAILED


def test_cancelled_task_does_not_overwrite_semantic_outcome() -> None:
    runtime = _active_runtime("prior-reason")
    assert runtime.current_task is not None
    cancelled = replace(
        runtime,
        current_task=replace(runtime.current_task, outcome=TASK_CANCELLED),
    )
    update = apply_task_outcome(
        {
            "task_runtime": cancelled.to_state_dict(),
            "status": "done",
            "status_reason": "done",
            "grounding_assessment": {"supported": False},
        }
    )
    assert update == {}
    assert "semantic_outcome" not in update


def test_turn_payload_keeps_semantic_outcome_after_the_checkpoint_is_gone() -> None:
    from fin.host.workspace_http import _serialize_run

    partial = _serialize_run(
        RunRecord(
            id="r-partial",
            thread_id="t-1",
            question="q",
            status="done",
            answer="only part of the figure",
            semantic_outcome="insufficient_evidence",
        )
    )
    complete = _serialize_run(
        RunRecord(
            id="r-complete",
            thread_id="t-1",
            question="q",
            status="done",
            answer="the full figure",
            semantic_outcome=None,
        )
    )
    assert partial["semantic_outcome"] == "insufficient_evidence"
    assert complete["semantic_outcome"] is None
