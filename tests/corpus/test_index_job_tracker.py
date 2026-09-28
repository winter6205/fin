"""Tracker + ledger tests over the hermetic fake index-job port.

Covers: submit/join normal path, idempotent duplicate rounds, invalid
locators, typed port failures recorded-and-raised, restart resume from
the durable ledger, the graph/C pass-through rules (job state is never
evidence about community state, a failed poll stays stale-visible with
its recorded failure code rather than collapsing to an empty ready
shape, and a fully refused round surfaces its recorded error), and the
two-writer concurrency contract of the process-singleton ledger.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from fin.corpus.index_job import (
    IndexJobLedger,
    IndexJobSubmitError,
    IndexJobTracker,
)
from fin.retrieve.fake_index_job_port import FakeIndexJobPort

SOURCE_A = '{"document_id":"ACME-2025Q1","version":"v1","unit":"u1"}'
SOURCE_B = '{"document_id":"ACME-2025Q1","version":"v1","unit":"u2"}'
IDENTITY_A = "identity-a"
IDENTITY_B = "identity-b"
DOCUMENT = "ACME-2025Q1"


@pytest.fixture()
def port() -> FakeIndexJobPort:
    return FakeIndexJobPort()


@pytest.fixture()
def ledger(tmp_path: Path) -> IndexJobLedger:
    return IndexJobLedger(path=tmp_path / "index_jobs.jsonl")


@pytest.fixture()
def tracker(
    port: FakeIndexJobPort, ledger: IndexJobLedger
) -> IndexJobTracker:
    return IndexJobTracker(port=port, ledger=ledger)


def _entries(ledger: IndexJobLedger) -> dict[str, dict[str, object]]:
    return {entry["source_ref"]: entry for entry in ledger.entries(DOCUMENT)}


# ----- submit -----


def test_submit_after_push_persists_one_job_per_pair(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A, SOURCE_B: IDENTITY_B})
    entries = _entries(ledger)
    assert set(entries) == {SOURCE_A, SOURCE_B}
    assert entries[SOURCE_A]["job_id"] is not None
    assert entries[SOURCE_A]["content_identity"] == IDENTITY_A
    assert entries[SOURCE_A]["last_error"] is None
    assert entries[SOURCE_A]["job_id"] != entries[SOURCE_B]["job_id"]
    assert port.submit_calls == 2


def test_duplicate_submit_round_joins_not_duplicates(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    first = _entries(ledger)[SOURCE_A]["job_id"]
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    second = _entries(ledger)[SOURCE_A]["job_id"]
    assert first == second
    # The port answers the replay as a join (same derived job_id), so
    # no second active job exists for the pair.
    assert port.submit_calls == 2
    poll = port.poll_index_job(first)
    assert poll.record.source_ref == SOURCE_A


def test_empty_locator_is_recorded_and_aggregated(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
) -> None:
    blank_source = "   "
    with pytest.raises(IndexJobSubmitError) as caught:
        tracker.submit_after_push(DOCUMENT, {blank_source: IDENTITY_A, SOURCE_B: ""})
    assert caught.value.failures == {
        blank_source: "index_job_invalid_locator",
        SOURCE_B: "index_job_invalid_locator",
    }
    assert caught.value.codes == ("index_job_invalid_locator",)
    entries = _entries(ledger)
    assert entries[blank_source]["last_error"]["code"] == "index_job_invalid_locator"
    assert entries[SOURCE_B]["last_error"]["code"] == "index_job_invalid_locator"
    assert entries[SOURCE_B]["job_id"] is None


def test_valid_pairs_survive_a_partial_round(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
) -> None:
    with pytest.raises(IndexJobSubmitError):
        tracker.submit_after_push(DOCUMENT, {"  ": IDENTITY_A, SOURCE_B: IDENTITY_B})
    entries = _entries(ledger)
    assert entries[SOURCE_B]["job_id"] is not None
    assert entries[SOURCE_B]["last_error"] is None


def test_port_unavailable_is_recorded_and_raised(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    port.inject_submit_failure("index_job_unavailable", sticky=True)
    with pytest.raises(IndexJobSubmitError) as caught:
        tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    assert caught.value.failures == {SOURCE_A: "index_job_unavailable"}
    entry = _entries(ledger)[SOURCE_A]
    assert entry["job_id"] is None
    assert entry["last_error"]["code"] == "index_job_unavailable"
    # A later round where the service is back settles the pair and
    # clears the error.
    port.clear_injected_failures()
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    entry = _entries(ledger)[SOURCE_A]
    assert entry["job_id"] is not None
    assert entry["last_error"] is None


def test_identity_mismatch_refusal_is_typed(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    port.inject_submit_failure("index_job_identity_mismatch")
    with pytest.raises(IndexJobSubmitError) as caught:
        tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    assert caught.value.failures == {SOURCE_A: "index_job_identity_mismatch"}
    assert (
        _entries(ledger)[SOURCE_A]["last_error"]["code"]
        == "index_job_identity_mismatch"
    )


def test_submit_requires_a_document_id(tracker: IndexJobTracker) -> None:
    with pytest.raises(ValueError):
        tracker.submit_after_push("", {SOURCE_A: IDENTITY_A})


# ----- graph_status -----


def test_graph_status_none_before_any_submit(
    tracker: IndexJobTracker,
) -> None:
    assert tracker.graph_status(DOCUMENT) is None


def test_graph_status_passes_community_state_through_unaltered(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    job_id = ledger.entries(DOCUMENT)[0]["job_id"]
    port.advance_job(job_id, "ready")
    status = tracker.graph_status(DOCUMENT)
    # Job ready, corpus C NOT ready: the two facts never collapse.
    assert status["jobs"] == [
        {
            "job_id": job_id,
            "source_ref": SOURCE_A,
            "status": "ready",
            "failure_code": None,
        }
    ]
    assert status["community_index_state"] == {
        "status": "in_progress",
        "failure_code": None,
    }
    assert status["error"] is None
    # When the service confirms C ready, fin passes it through verbatim.
    port.set_community_index_state("ready")
    status = tracker.graph_status(DOCUMENT)
    assert status["community_index_state"] == {
        "status": "ready",
        "failure_code": None,
    }


def test_community_failure_code_passes_through(
    tracker: IndexJobTracker, port: FakeIndexJobPort
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    port.set_community_index_state("failed", "graph_stage_crashed")
    status = tracker.graph_status(DOCUMENT)
    assert status["community_index_state"] == {
        "status": "failed",
        "failure_code": "graph_stage_crashed",
    }


def test_never_polled_document_has_no_community_claim(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    port.inject_submit_failure("index_job_unavailable", sticky=True)
    with pytest.raises(IndexJobSubmitError):
        tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    status = tracker.graph_status(DOCUMENT)
    assert status is not None  # an entry exists — the refusal did land
    assert status["jobs"] == []
    # No poll ever confirmed C, so the claim is None, not a guess.
    assert status["community_index_state"] is None
    # The refusal is never presented as an error-less empty success.
    assert status["error"] is not None
    assert status["error"]["code"] == "index_job_unavailable"


def test_fully_refused_round_surfaces_recorded_error(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    port.inject_submit_failure("index_job_unavailable", sticky=True)
    with pytest.raises(IndexJobSubmitError):
        tracker.submit_after_push(
            DOCUMENT, {SOURCE_A: IDENTITY_A, SOURCE_B: IDENTITY_B}
        )
    status = tracker.graph_status(DOCUMENT)
    assert status["jobs"] == []
    assert status["error"]["code"] == "index_job_unavailable"
    assert status["error"]["message"]
    # Once a later round settles the pairs, the shape stops claiming it.
    port.clear_injected_failures()
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A, SOURCE_B: IDENTITY_B})
    status = tracker.graph_status(DOCUMENT)
    assert status["error"] is None
    assert len(status["jobs"]) == 2


def test_poll_failure_keeps_last_persisted_state_visible(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    job_id = _entries(ledger)[SOURCE_A]["job_id"]
    port.advance_job(job_id, "ready")
    port.set_community_index_state("in_progress")
    tracker.graph_status(DOCUMENT)  # persists ready / in_progress
    submits_before = port.submit_calls
    port.inject_poll_failure("index_job_unavailable", sticky=True)
    status = tracker.graph_status(DOCUMENT)
    assert port.submit_calls == submits_before  # poll path never submits
    assert status["error"]["code"] == "index_job_unavailable"
    assert status["error"]["message"]
    assert status["jobs"] == [
        {
            "job_id": job_id,
            "source_ref": SOURCE_A,
            "status": "ready",  # stale-visible, not emptied
            "failure_code": None,
        }
    ]
    assert status["community_index_state"] == {
        "status": "in_progress",
        "failure_code": None,
    }


def test_poll_unknown_job_is_typed_and_stays_visible(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    port.inject_poll_failure("index_job_unknown_job")
    status = tracker.graph_status(DOCUMENT)
    assert status["error"]["code"] == "index_job_unknown_job"
    assert status["jobs"][0]["status"] is None  # never polled yet


def test_poll_failure_echoes_last_persisted_failure_code(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    job_id = _entries(ledger)[SOURCE_A]["job_id"]
    port.advance_job(job_id, "failed", failure_code="extract_stage_crashed")
    tracker.graph_status(DOCUMENT)  # persists failed + the failure code
    reloaded = IndexJobLedger(path=ledger.path)
    assert (
        reloaded.entries(DOCUMENT)[0]["last_failure_code"]
        == "extract_stage_crashed"
    )
    port.inject_poll_failure("index_job_unavailable", sticky=True)
    status = tracker.graph_status(DOCUMENT)
    assert status["jobs"] == [
        {
            "job_id": job_id,
            "source_ref": SOURCE_A,
            "status": "failed",
            # stale-visible includes the recorded failure code, not null
            "failure_code": "extract_stage_crashed",
        }
    ]
    assert status["error"]["code"] == "index_job_unavailable"


def test_ledger_replays_snapshots_without_a_failure_code(
    tmp_path: Path,
) -> None:
    # The per-entry failure code was added after the first on-disk
    # snapshot shape; replaying an older line reads it as None instead
    # of declaring the file corrupt.
    path = tmp_path / "index_jobs.jsonl"
    old_entry = {
        "source_ref": SOURCE_A,
        "content_identity": IDENTITY_A,
        "job_id": "job-1",
        "last_polled_status": "ready",
        "last_community_index_state": {"status": "ready", "failure_code": None},
        "last_error": None,
    }
    path.write_text(
        json.dumps(
            {
                "event": "entries_recorded",
                "document_id": DOCUMENT,
                "entries": [old_entry],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = IndexJobLedger(path=path)
    assert ledger.entries(DOCUMENT)[0]["last_failure_code"] is None


# ----- concurrency (the process singleton has two writers) -----


def test_concurrent_submit_and_graph_status_share_one_ledger(
    port: FakeIndexJobPort, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's submit thread and the route's poll threadpool write
    the same ledger concurrently: no lost entries, no iteration errors,
    and the durable file replays to exactly the in-memory state.

    Record is a read-modify-write (mutate, snapshot, append). Without
    one critical section across them, a writer whose snapshot is taken
    before another's mutation still appends *after* it, and
    last-write-wins replay loses that entry. The test reproduces the
    window deterministically by delaying only the first append (its
    snapshot is the stalest) until every other writer has committed;
    with the ledger lock the delay happens while the lock is held, so
    snapshots stay monotone and the replay is complete.
    """
    import time

    import fin.corpus.index_job as index_job_module
    from fin.retrieve._ledger.jsonl import append_jsonl_object as _append

    state = {"delayed": False}
    state_guard = threading.Lock()

    def _delay_first_append(path, payload, **kwargs):
        with state_guard:
            first = not state["delayed"]
            state["delayed"] = True
        if first:
            time.sleep(0.05)
        _append(path, payload, **kwargs)

    monkeypatch.setattr(
        index_job_module, "append_jsonl_object", _delay_first_append
    )

    ledger = IndexJobLedger(path=tmp_path / "index_jobs.jsonl")
    tracker = IndexJobTracker(port=port, ledger=ledger)
    pairs = {
        f'{{"document_id":"ACME-2025Q1","version":"v1","unit":"u{index}"}}': (
            f"identity-{index}"
        )
        for index in range(20)
    }
    thread_errors: list[BaseException] = []

    def _run(action: object) -> None:
        try:
            action()  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001 - reported via assert
            thread_errors.append(exc)

    def _submitter(source_ref: str, identity: str):
        def _action() -> None:
            tracker.submit_after_push(DOCUMENT, {source_ref: identity})

        return _action

    writers = [
        threading.Thread(target=_run, args=(_submitter(source_ref, identity),))
        for source_ref, identity in pairs.items()
    ]
    poll_thread = threading.Thread(
        target=_run,
        args=(lambda: [tracker.graph_status(DOCUMENT) for _ in range(3)],),
    )
    for writer in writers:
        writer.start()
    poll_thread.start()
    for writer in writers:
        writer.join(timeout=60)
    poll_thread.join(timeout=60)

    assert thread_errors == []
    entries = ledger.entries(DOCUMENT)
    assert len(entries) == len(pairs)
    assert all(entry["job_id"] is not None for entry in entries)
    reloaded = IndexJobLedger(path=ledger.path)
    assert {
        entry["source_ref"] for entry in reloaded.entries(DOCUMENT)
    } == set(pairs)


# ----- restart -----


def test_restart_resumes_by_polling_stored_job_ids(
    port: FakeIndexJobPort, ledger: IndexJobLedger, tracker: IndexJobTracker
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A, SOURCE_B: IDENTITY_B})
    job_a = ledger.entries(DOCUMENT)[0]["job_id"]
    port.advance_job(job_a, "ready")
    # A new process: new tracker over the SAME ledger file, same port.
    resumed_ledger = IndexJobLedger(path=ledger.path)
    resumed = IndexJobTracker(port=port, ledger=resumed_ledger)
    submits_before = port.submit_calls
    polls_before = port.poll_calls
    status = resumed.graph_status(DOCUMENT)
    assert port.submit_calls == submits_before  # no re-submit, no re-push
    assert port.poll_calls == polls_before + 2
    by_id = {job["job_id"]: job for job in status["jobs"]}
    assert by_id[job_a]["status"] == "ready"
    assert status["community_index_state"] == {
        "status": "in_progress",
        "failure_code": None,
    }


def test_ledger_persists_polled_state_across_instances(
    tracker: IndexJobTracker,
    ledger: IndexJobLedger,
    port: FakeIndexJobPort,
) -> None:
    tracker.submit_after_push(DOCUMENT, {SOURCE_A: IDENTITY_A})
    job_id = ledger.entries(DOCUMENT)[0]["job_id"]
    port.advance_job(job_id, "failed", failure_code="extract_stage_crashed")
    port.set_community_index_state("ready")
    tracker.graph_status(DOCUMENT)
    reloaded = IndexJobLedger(path=ledger.path)
    entry = reloaded.entries(DOCUMENT)[0]
    assert entry["last_polled_status"] == "failed"
    assert entry["last_community_index_state"] == {
        "status": "ready",
        "failure_code": None,
    }
    assert entry["last_error"] is None
