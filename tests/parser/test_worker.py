"""Worker attempt authority: claim, lease, fence and cancellation (T6/T7).

Every case here runs against a REAL PostgreSQL ``_test`` database and REAL
operating-system processes. Nothing is mocked, no spy stands in for the
database, and no in-process lock is used to manufacture exclusivity: the
database's own row locking is the only thing that decides who wins.

Ordering between the competing children is established by a parent-held
barrier: each child announces readiness and then blocks on stdin, and the
parent releases both only after both have announced. No ``sleep`` sequences
a race anywhere in this file.

The suite shares one database with the rest of the parser work, so every case
creates its own job through the store's public accept path instead of
truncating the parser schema.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.errors import (
    ParserExecutionTimeoutError,
    ParserInvalidRequestError,
    ParserJobCancelledError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobTombstonedError,
    ParserPersistenceError,
    ParserStaleFenceError,
)
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.ports import Attempt, JobRecord
from fin.parser.worker import (
    AttemptResult,
    LeaseHeartbeat,
    ParserWorker,
    ParserWorkerConfig,
    PostgresWorkerAttemptStates,
)
from fin.parser.worker_main import WorkerSettings

_DSN = (os.environ.get("FIN_TEST_DSN") or "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="FIN_TEST_DSN is unset; the worker authority needs a PostgreSQL _test database",
)


# ----- database helpers -------------------------------------------------


def _database_name(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        return str(conn.execute("SELECT current_database()").fetchone()[0])


def _submit_job(store: PostgresParserJobStore) -> str:
    """Accept one job through the store's public path."""
    submission = store.submit_job(
        idempotency_key=f"key-{uuid.uuid4()}",
        input_sha256="0" * 64,
        source_ref=None,
        declared_format=None,
    )
    return submission.job_id


class _Clock:
    """A clock the case moves, so a deadline is crossed without waiting.

    WHY: the store measures its deadlines with this clock, so a case can
    put an attempt past its deadline without sleeping for it.
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def __call__(self) -> datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._moment = self._moment + timedelta(seconds=seconds)


def _job_status(dsn: str, job_id: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT status FROM fin_parser.job WHERE job_id = %s", (job_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def _result_tokens(dsn: str, job_id: str) -> list[int]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT fencing_token FROM fin_parser.result WHERE job_id = %s"
            " ORDER BY fencing_token",
            (job_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _attempt_rows(dsn: str, job_id: str) -> list[tuple[int, int, str]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT attempt_no, fencing_token, state FROM fin_parser.attempt"
            " WHERE job_id = %s ORDER BY attempt_no",
            (job_id,),
        ).fetchall()
    return [(int(row[0]), int(row[1]), str(row[2])) for row in rows]


def _effective_token(dsn: str, job_id: str) -> int:
    """The job's current effective fencing token."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) FROM fin_parser.attempt"
            " WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return int(row[0])


def _expire_lease(dsn: str, job_id: str) -> None:
    """Arrange an expired lease deterministically.

    Waiting for wall-clock expiry would make the case a timing race; the
    lease IS expired as far as the database is concerned once its deadline
    is in the past, which is exactly the state a crashed worker leaves.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE fin_parser.attempt SET lease_expires_at = now() - interval '1 second'"
            " WHERE job_id = %s",
            (job_id,),
        )
        conn.commit()


def _lease_expired(dsn: str, job_id: str, fencing_token: int) -> bool:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT lease_expires_at <= now() AS expired FROM fin_parser.attempt"
            " WHERE job_id = %s AND fencing_token = %s",
            (job_id, fencing_token),
        ).fetchone()
    return bool(row[0]) if row is not None else True


def _wait_for_lease_expiry(
    dsn: str, job_id: str, fencing_token: int, *, timeout: float = 30.0
) -> None:
    """Wait until the database says the lease deadline has passed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _lease_expired(dsn, job_id, fencing_token):
            return
        # A TTL wait, not a race sequencer: nothing else is being ordered here.
        threading.Event().wait(0.05)
    raise AssertionError("the lease did not expire within the deadline")


# ----- child programs (real separate OS processes) ----------------------

_CLAIM_CHILD = """
import os, sys
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_TEST_DSN"])
job_id = os.environ["FIN_JOB_ID"]
lease = int(os.environ["FIN_LEASE_SECONDS"])
print("READY " + str(os.getpid()), flush=True)
sys.stdin.readline()
attempt = store.claim_attempt(
    job_id=job_id, worker_id=os.environ["FIN_WORKER_ID"], lease_seconds=lease
)
print("CLAIM " + (str(attempt.fencing_token) if attempt is not None else "none"), flush=True)
"""

_PROBE_CHILD = """
import json, os
from fin.parser.errors import ParserJobCancelledError
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_TEST_DSN"])
job_id = os.environ["FIN_JOB_ID"]
job = store.get_job(job_id)
try:
    attempt = store.claim_attempt(
        job_id=job_id, worker_id="fresh-process-probe", lease_seconds=30
    )
    claim = "claimed" if attempt is not None else "none"
except ParserJobCancelledError:
    claim = "refused"
print(json.dumps({
    "pid": os.getpid(),
    "status": None if job is None else job.status,
    "claim": claim,
}), flush=True)
"""


def _child_env(dsn: str, **extra: str) -> dict[str, str]:
    return {**os.environ, "FIN_TEST_DSN": dsn, **extra}


def _run_child(program: str, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


class _ClaimRace:
    """Two children held at a parent-controlled barrier.

    Each child announces readiness on stdout and blocks on stdin; the parent
    releases both only after both announcements arrived, so the two claims are
    genuinely concurrent instead of accidentally sequential.
    """

    def __init__(self, dsn: str, job_id: str, worker_ids: tuple[str, str]) -> None:
        self._lines: dict[str, list[str]] = {worker_id: [] for worker_id in worker_ids}
        self._procs: list[subprocess.Popen[str]] = []
        self._threads: list[threading.Thread] = []
        self._events: queue.Queue[str] = queue.Queue()
        for worker_id in worker_ids:
            env = _child_env(
                dsn,
                FIN_JOB_ID=job_id,
                FIN_WORKER_ID=worker_id,
                FIN_LEASE_SECONDS="60",
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", _CLAIM_CHILD],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self._procs.append(proc)
            self._threads.append(
                threading.Thread(target=self._read, args=(proc, worker_id), daemon=True)
            )
        for thread in self._threads:
            thread.start()

    def _read(self, proc: subprocess.Popen[str], worker_id: str) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            self._lines[worker_id].append(text)
            self._events.put(text)

    def release(self) -> list[int]:
        """Wait for both readiness announcements, then release both children."""
        pids: list[int] = []
        deadline = time.monotonic() + 120.0
        while len(pids) < len(self._procs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("the children did not reach the barrier")
            event = self._events.get(timeout=remaining)
            if event.startswith("READY "):
                pids.append(int(event.split(" ", 1)[1]))
        for proc in self._procs:
            assert proc.stdin is not None
            proc.stdin.write("GO\n")
            proc.stdin.flush()
        return pids

    def collect(self) -> dict[str, dict[str, Any]]:
        outcome: dict[str, dict[str, Any]] = {}
        for worker_id, proc in zip(self._lines, self._procs):
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            assert proc.wait(timeout=120) == 0, stderr
            for thread in self._threads:
                thread.join(timeout=30)
            lines = self._lines[worker_id]
            claim = [line for line in lines if line.startswith("CLAIM ")]
            assert len(claim) == 1, lines
            token_text = claim[0].split(" ", 1)[1]
            ready = [line for line in lines if line.startswith("READY ")]
            outcome[worker_id] = {
                "token": None if token_text == "none" else int(token_text),
                "pid": int(ready[0].split(" ")[1]),
            }
        return outcome

    def kill(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


# ----- fixtures ---------------------------------------------------------


@pytest.fixture
def dsn() -> str:
    if not _DSN:
        pytest.skip("FIN_TEST_DSN is unset")
    name = _database_name(_DSN)
    assert name.endswith("_test"), f"tests must only touch a _test database, got {name!r}"
    return _DSN


@pytest.fixture
def store(dsn: str) -> PostgresParserJobStore:
    # A high open-job limit: the capacity guard belongs to the accept lane,
    # and this suite only measures ownership.
    return PostgresParserJobStore(dsn, max_open_jobs=10_000)


@pytest.fixture
def states(dsn: str) -> PostgresWorkerAttemptStates:
    return PostgresWorkerAttemptStates(dsn)


# ----- injected doubles --------------------------------------------------


class _UnrecordableAttemptStates:
    """Attempt states whose failure record cannot be written.

    The store is unreachable, so recording the failed attempt fails with the
    typed persistence error the taxonomy names.
    """

    def __init__(self) -> None:
        self.failed: list[tuple[str, int]] = []

    def mark_running(self, *, job_id: str, fencing_token: int) -> bool:
        return True

    def release_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        return True

    def fail_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        self.failed.append((job_id, fencing_token))
        raise ParserPersistenceError("the parser store could not be reached")


class _FaultingAuthority:
    """An authority whose renewal fails with a fault the taxonomy does not name."""

    connection_dsn = ""

    def renew_attempt(
        self, *, job_id: str, fencing_token: int, lease_seconds: float
    ) -> bool:
        raise RuntimeError("the controlled authority exploded")


class _UnfinalizableCancelledAuthority:
    """An authority whose cancelled job cannot be finalized.

    The claim reports the cancellation, and the job read the finalization
    needs answers "no such row": finalizing therefore fails after the claim
    already named the failure the caller must see.
    """

    def claim_attempt(
        self, *, job_id: str, worker_id: str, lease_seconds: float
    ) -> Attempt | None:
        raise ParserJobCancelledError(f"parser job {job_id} was cancelled")

    def get_job(self, job_id: str) -> Any:
        return None


class _UnfinalizableCancelRequestAuthority:
    """An authority whose recorded cancellation request cannot be finalized.

    The claim finds no claimable work, the job read reports the recorded
    cancellation request, and turning that request into the cancelled
    status fails: the cancellation the caller must see still propagates.
    """

    def __init__(self) -> None:
        moment = datetime.now(timezone.utc)
        self._job = JobRecord(
            job_id="parser-job-2",
            idempotency_key="key-parser-job-2",
            input_sha256="0" * 64,
            status="cancel_requested",
            created_at=moment,
            updated_at=moment,
        )

    def claim_attempt(
        self, *, job_id: str, worker_id: str, lease_seconds: float
    ) -> Attempt | None:
        return None

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._job

    def cancel_job(self, job_id: str) -> JobRecord:
        raise ParserJobConflictError(f"parser job {job_id} awaits no cancellation")


# ----- fencing counter ---------------------------------------------------


def test_each_claim_strictly_increases_the_job_fencing_counter(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)

    first = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert first is not None
    assert first.fencing_token == 1
    assert first.attempt_no == 1

    second = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert second is not None
    assert second.fencing_token == 2
    assert second.attempt_no == 2

    third = store.claim_attempt(job_id=job_id, worker_id="worker-c", lease_seconds=60)
    assert third is not None
    assert third.fencing_token == 3

    assert _attempt_rows(dsn, job_id) == [
        (1, 1, "claimed"),
        (2, 2, "claimed"),
        (3, 3, "claimed"),
    ]
    assert _effective_token(dsn, job_id) == 3


# ----- two competing processes ------------------------------------------


def test_two_competing_worker_processes_cannot_both_hold_the_current_attempt(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    worker_ids = ("worker-a", "worker-b")
    race = _ClaimRace(dsn, job_id, worker_ids)
    try:
        pids = race.release()
        outcome = race.collect()
    finally:
        race.kill()

    parent_pid = os.getpid()
    for worker_id in worker_ids:
        assert outcome[worker_id]["pid"] != parent_pid
        assert outcome[worker_id]["pid"] in pids
    assert len(set(pids)) == 2

    tokens = sorted(
        int(outcome[worker]["token"])
        for worker in worker_ids
        if outcome[worker]["token"] is not None
    )
    assert len(tokens) == 2, outcome
    assert tokens[0] != tokens[1]

    # Exactly one of them is the job's current effective attempt.
    assert _effective_token(dsn, job_id) == tokens[1]
    assert _attempt_rows(dsn, job_id) == [(1, 1, "claimed"), (2, 2, "claimed")]

    # The other one is stale from the moment the later claim committed.
    with pytest.raises(ParserStaleFenceError):
        store.commit_result(
            job_id=job_id,
            fencing_token=tokens[0],
            envelope={"source_sha256": "0" * 64},
            result_ref="ref://stale",
        )
    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "running"


# ----- the fence rejects a late commit ----------------------------------


def test_a_later_claim_fences_the_earlier_attempt_and_its_commit_writes_nothing(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    first = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert first is not None

    second = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert second is not None
    assert second.fencing_token > first.fencing_token

    with pytest.raises(ParserStaleFenceError):
        store.commit_result(
            job_id=job_id,
            fencing_token=first.fencing_token,
            envelope={"source_sha256": "0" * 64},
            result_ref="ref://stale",
        )

    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "running"

    record = store.commit_result(
        job_id=job_id,
        fencing_token=second.fencing_token,
        envelope={"source_sha256": "0" * 64},
        result_ref="ref://current",
    )
    assert record.fencing_token == second.fencing_token
    assert _result_tokens(dsn, job_id) == [second.fencing_token]
    assert _job_status(dsn, job_id) == "succeeded"


# ----- lease expiry ------------------------------------------------------


def test_a_claim_after_the_lease_expired_advances_the_fencing_token(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    first = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert first is not None

    _expire_lease(dsn, job_id)

    second = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert _effective_token(dsn, job_id) == second.fencing_token


def test_a_lease_that_expires_on_the_wall_clock_can_be_taken_over(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    first = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=0.2)
    assert first is not None

    _wait_for_lease_expiry(dsn, job_id, first.fencing_token)

    second = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert second is not None
    assert second.fencing_token > first.fencing_token


# ----- renewal -----------------------------------------------------------


def test_renew_succeeds_while_the_lease_is_live(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None

    assert (
        store.renew_attempt(
            job_id=job_id, fencing_token=attempt.fencing_token, lease_seconds=120
        )
        is True
    )


def test_renew_fails_once_the_lease_expired(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None

    _expire_lease(dsn, job_id)

    assert (
        store.renew_attempt(
            job_id=job_id, fencing_token=attempt.fencing_token, lease_seconds=120
        )
        is False
    )


def test_renew_fails_once_a_later_claim_advanced_the_token(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    first = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert first is not None
    second = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert second is not None

    assert (
        store.renew_attempt(
            job_id=job_id, fencing_token=first.fencing_token, lease_seconds=120
        )
        is False
    )
    assert (
        store.renew_attempt(
            job_id=job_id, fencing_token=second.fencing_token, lease_seconds=120
        )
        is True
    )


# ----- cancellation ------------------------------------------------------


def test_cancellation_is_durable_and_visible_from_a_fresh_process(
    dsn: str, store: PostgresParserJobStore
) -> None:
    """A cancellation request is durable and survives a restart."""
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None

    cancelled = store.cancel_job(job_id)
    # The attempt is still live, so the request is recorded first and the
    # worker finalizes it.
    assert cancelled.status == "cancel_requested"
    assert cancelled.job_id == job_id

    completed = _run_child(_PROBE_CHILD, _child_env(dsn, FIN_JOB_ID=job_id))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["pid"] != os.getpid()
    assert payload["status"] == "cancel_requested"
    assert payload["claim"] == "refused"


def test_a_cancelled_job_cannot_regain_worker_ownership(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    store.cancel_job(job_id)

    with pytest.raises(ParserJobCancelledError):
        store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)

    assert _attempt_rows(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "cancelled"


def test_an_in_flight_attempt_cannot_commit_after_cancellation(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None

    store.cancel_job(job_id)

    with pytest.raises(ParserJobCancelledError):
        store.commit_result(
            job_id=job_id,
            fencing_token=attempt.fencing_token,
            envelope={"source_sha256": "0" * 64},
            result_ref="ref://cancelled",
        )
    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "cancel_requested"
    assert (
        store.renew_attempt(
            job_id=job_id, fencing_token=attempt.fencing_token, lease_seconds=120
        )
        is False
    )


def test_the_worker_finalizes_an_observed_cancellation_request(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    """A job the worker finds cancelled becomes durably ``cancelled``."""
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None
    assert store.cancel_job(job_id).status == "cancel_requested"
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-b", lease_seconds=60
        )
    )

    with pytest.raises(ParserJobCancelledError):
        worker.claim(job_id)

    assert _job_status(dsn, job_id) == "cancelled"
    assert _result_tokens(dsn, job_id) == []


def test_the_cancellation_survives_a_failure_to_finalize_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cancellation is the error the caller sees.

    Finalizing the cancellation fails, because the job read finds no row.
    That failure is logged, and the cancellation the caller must see is the
    one that propagates.
    """
    authority = _UnfinalizableCancelledAuthority()
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=authority,  # type: ignore[arg-type]
            states=_UnrecordableAttemptStates(),
            worker_id="worker-a",
            lease_seconds=60,
        )
    )

    with caplog.at_level(logging.WARNING, logger="fin.parser.worker"):
        with pytest.raises(ParserJobCancelledError) as excinfo:
            worker.claim("parser-job-1")

    assert excinfo.value.code == "job_cancelled"
    assert any(
        "could not be finalized" in record.getMessage() for record in caplog.records
    )


def test_the_recorded_cancellation_survives_a_failure_to_finalize_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cancellation request seen at claim time is the error the caller sees.

    Turning the recorded request into the cancelled status fails.  That
    failure is logged, and the cancellation the caller must see is the one
    that propagates.
    """
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=_UnfinalizableCancelRequestAuthority(),  # type: ignore[arg-type]
            states=_UnrecordableAttemptStates(),
            worker_id="worker-a",
            lease_seconds=60,
        )
    )

    with caplog.at_level(logging.WARNING, logger="fin.parser.worker"):
        with pytest.raises(ParserJobCancelledError) as excinfo:
            worker.claim("parser-job-2")

    assert excinfo.value.code == "job_cancelled"
    assert any(
        "could not be finalized" in record.getMessage() for record in caplog.records
    )


def test_a_cancellation_during_the_run_is_durable_and_the_commit_is_refused(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    """A cancellation that arrives while the producer runs wins."""
    job_id = _submit_job(store)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        # The cancellation arrives while this attempt is still working.
        assert store.cancel_job(attempt.job_id).status == "cancel_requested"
        return AttemptResult(
            envelope={"source_sha256": "0" * 64},
            result_ref=f"ref://{attempt.job_id}",
        )

    with pytest.raises(ParserJobCancelledError):
        worker.process(job_id, produce)

    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "cancelled"


def test_the_worker_surfaces_the_execution_deadline_as_a_typed_error(
    dsn: str, states: PostgresWorkerAttemptStates
) -> None:
    """An attempt that outruns its deadline ends as a typed failure."""
    clock = _Clock(datetime.now(timezone.utc))
    store = PostgresParserJobStore(
        dsn,
        queue_wait_seconds=3600,
        execution_seconds=60,
        clock=clock,
    )
    job_id = _submit_job(store)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        clock.advance(61.0)
        return AttemptResult(
            envelope={"source_sha256": "0" * 64},
            result_ref=f"ref://{attempt.job_id}",
        )

    with pytest.raises(ParserExecutionTimeoutError) as excinfo:
        worker.process(job_id, produce)

    assert excinfo.value.code == "parser_execution_timeout"
    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) == "timed_out"
    assert _attempt_rows(dsn, job_id) == [(1, 1, "failed")]


def test_the_execution_timeout_survives_a_failure_to_record_it(
    dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The timeout is the error the caller sees.

    Recording the failed attempt fails too, because the store is
    unreachable.  That second failure is attached to the timeout instead of
    replacing it, and it is reported rather than swallowed.
    """
    clock = _Clock(datetime.now(timezone.utc))
    store = PostgresParserJobStore(
        dsn,
        queue_wait_seconds=3600,
        execution_seconds=60,
        clock=clock,
    )
    job_id = _submit_job(store)
    states = _UnrecordableAttemptStates()
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        clock.advance(61.0)
        return AttemptResult(
            envelope={"source_sha256": "0" * 64},
            result_ref=f"ref://{attempt.job_id}",
        )

    with caplog.at_level(logging.ERROR, logger="fin.parser.worker"):
        with pytest.raises(ParserExecutionTimeoutError) as excinfo:
            worker.process(job_id, produce)

    assert excinfo.value.code == "parser_execution_timeout"
    assert states.failed == [(job_id, 1)]
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("persistence_unavailable" in note for note in notes)
    assert any(
        "could not be recorded" in record.getMessage() for record in caplog.records
    )


def test_a_tombstoned_job_refuses_a_late_commit(
    dsn: str, store: PostgresParserJobStore
) -> None:
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None

    store.delete_job(job_id)

    with pytest.raises(ParserJobTombstonedError):
        store.commit_result(
            job_id=job_id,
            fencing_token=attempt.fencing_token,
            envelope={"source_sha256": "0" * 64},
            result_ref="ref://deleted",
        )
    assert _result_tokens(dsn, job_id) == []
    assert _job_status(dsn, job_id) is None


def test_the_cancellation_command_is_a_durable_cli_operation(dsn: str) -> None:
    job_id = _submit_job(PostgresParserJobStore(dsn, max_open_jobs=10_000))

    completed = subprocess.run(
        [sys.executable, "-m", "fin.parser.worker_main", "cancel", "--job-id", job_id],
        env=_child_env(dsn, FIN_PARSER_DSN=dsn),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert dsn not in completed.stdout
    assert dsn not in completed.stderr
    assert _job_status(dsn, job_id) == "cancelled"

    probe = _run_child(_PROBE_CHILD, _child_env(dsn, FIN_JOB_ID=job_id))
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout.strip().splitlines()[-1])
    assert payload["status"] == "cancelled"
    assert payload["claim"] == "refused"


# ----- the worker seam ---------------------------------------------------


def test_the_worker_commits_the_produced_result_under_its_fencing_token(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    job_id = _submit_job(store)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        return AttemptResult(
            envelope={"source_sha256": "0" * 64, "attempt": attempt.fencing_token},
            result_ref=f"ref://{attempt.job_id}",
        )

    record = worker.process(job_id, produce)
    assert record.fencing_token == 1
    assert _result_tokens(dsn, job_id) == [1]
    assert _job_status(dsn, job_id) == "succeeded"
    assert _attempt_rows(dsn, job_id) == [(1, 1, "succeeded")]


def test_the_worker_marks_the_attempt_failed_when_the_producer_raises(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    job_id = _submit_job(store)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        raise RuntimeError("the controlled producer exploded")

    with pytest.raises(RuntimeError):
        worker.process(job_id, produce)

    assert _result_tokens(dsn, job_id) == []
    assert _attempt_rows(dsn, job_id) == [(1, 1, "failed")]
    assert _job_status(dsn, job_id) == "running"


def test_the_producer_error_survives_a_failure_to_record_it(
    dsn: str, store: PostgresParserJobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The producer's error is the error the caller sees.

    Recording the failed attempt fails too, because the store is unreachable.
    That second failure is attached to the producer's error instead of
    replacing it, and it is reported rather than swallowed.
    """
    job_id = _submit_job(store)
    states = _UnrecordableAttemptStates()
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        raise RuntimeError("the controlled producer exploded")

    with caplog.at_level(logging.ERROR, logger="fin.parser.worker"):
        with pytest.raises(RuntimeError) as excinfo:
            worker.process(job_id, produce)

    assert str(excinfo.value) == "the controlled producer exploded"
    # the attempt was marked failed, and the failure to mark it did not
    # replace the error the caller sees
    assert states.failed == [(job_id, 1)]
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("persistence_unavailable" in note for note in notes)
    assert any(
        "could not be recorded" in record.getMessage() for record in caplog.records
    )
    assert _result_tokens(dsn, job_id) == []


def test_the_worker_releases_an_attempt_so_another_worker_may_take_over(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    job_id = _submit_job(store)
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )
    attempt = worker.claim(job_id)

    assert worker.release(attempt) is True
    assert _attempt_rows(dsn, job_id) == [(1, 1, "expired")]
    assert _lease_expired(dsn, job_id, attempt.fencing_token) is True

    successor = store.claim_attempt(job_id=job_id, worker_id="worker-b", lease_seconds=60)
    assert successor is not None
    assert successor.fencing_token > attempt.fencing_token


# ----- the lease heartbeat ------------------------------------------------


def test_a_lost_lease_stops_the_worker_with_a_typed_error(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    """A lease that is gone is a typed failure, not a silent commit.

    The lease expires while the producer runs, so the attempt may no
    longer act on the job: the result is not stored, and the failure is a
    typed stale fence instead of a silent pass.
    """
    job_id = _submit_job(store)
    # A renewal interval no test run reaches: the loss is decided by the
    # confirmation the heartbeat makes when the producer returns, not by
    # which side of a timer the two threads land on.
    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store,
            states=states,
            worker_id="worker-a",
            lease_seconds=60,
            renew_interval=30,
        )
    )

    def produce(attempt: Any) -> AttemptResult:
        _expire_lease(dsn, job_id)
        return AttemptResult(
            envelope={"source_sha256": "0" * 64},
            result_ref=f"ref://{attempt.job_id}",
        )

    with pytest.raises(ParserStaleFenceError):
        worker.process(job_id, produce)

    assert _result_tokens(dsn, job_id) == []
    assert _attempt_rows(dsn, job_id) == [(1, 1, "failed")]
    assert _job_status(dsn, job_id) == "running"


def test_the_heartbeat_reports_a_lease_it_can_no_longer_renew(
    dsn: str, store: PostgresParserJobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A renewal that fails is visible, and the loss is reported.

    The lease is gone before the heartbeat starts, so no renewal may
    succeed: the heartbeat says so instead of leaving the worker to
    discover it later.
    """
    job_id = _submit_job(store)
    attempt = store.claim_attempt(job_id=job_id, worker_id="worker-a", lease_seconds=60)
    assert attempt is not None
    _expire_lease(dsn, job_id)

    with caplog.at_level(logging.WARNING, logger="fin.parser.worker"):
        heartbeat = LeaseHeartbeat(
            store, attempt, interval_seconds=0.05, lease_seconds=60
        )
        with heartbeat:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not heartbeat.lost:
                # A TTL wait, not a race sequencer: the loss is what is
                # awaited here.
                threading.Event().wait(0.05)
            assert heartbeat.lost is True
            # The renewal itself reported the loss: the confirmation the
            # heartbeat makes when the block ends has not run yet.
            reported = [record.getMessage() for record in caplog.records]
            assert any(
                "no longer the effective one" in message for message in reported
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any(job_id in message for message in messages)
    assert all(dsn not in message for message in messages)


def test_a_renewal_failure_is_logged_with_its_kind_and_without_the_dsn(
    dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A store that cannot be reached is a logged failure, not a silent one.

    The database the store names is dropped, so the renewal fails at the
    driver: the log names the kind of failure and never the connection
    string.
    """
    info = conninfo_to_dict(dsn)
    name = f"fin_parser_unreachable_{uuid.uuid4().hex[:10]}_test"
    admin = make_conninfo(**{**info, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    unreachable_dsn = make_conninfo(**{**info, "dbname": name})
    store = PostgresParserJobStore(unreachable_dsn)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
    attempt = Attempt(
        job_id=str(uuid.uuid4()),
        attempt_no=1,
        fencing_token=1,
        worker_id="worker-a",
        lease_expires_at=datetime.now(timezone.utc),
    )

    with caplog.at_level(logging.WARNING, logger="fin.parser.worker"):
        heartbeat = LeaseHeartbeat(
            store, attempt, interval_seconds=0.05, lease_seconds=60
        )
        with heartbeat:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not heartbeat.lost:
                # A TTL wait, not a race sequencer: the failure is awaited.
                threading.Event().wait(0.05)
            assert heartbeat.lost is True

    messages = [record.getMessage() for record in caplog.records]
    assert any("could not be renewed" in message for message in messages)
    # the typed failure is reported with its stable code, not with a bare
    # class name: an unreachable store is not read as a superseded lease
    assert any("persistence_unavailable" in message for message in messages)
    assert all(unreachable_dsn not in message for message in messages)


def test_an_unknown_renewal_fault_is_reported_by_its_class_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fault the taxonomy does not name is reported by its class name.

    The typed branch answers with a stable code; a genuinely unknown fault
    keeps its class name, so the two are never confused.
    """
    attempt = Attempt(
        job_id=str(uuid.uuid4()),
        attempt_no=1,
        fencing_token=1,
        worker_id="worker-a",
        lease_expires_at=datetime.now(timezone.utc),
    )

    with caplog.at_level(logging.WARNING, logger="fin.parser.worker"):
        heartbeat = LeaseHeartbeat(
            _FaultingAuthority(), attempt, interval_seconds=0.05, lease_seconds=60
        )
        with heartbeat:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not heartbeat.lost:
                # A TTL wait, not a race sequencer: the failure is awaited.
                threading.Event().wait(0.05)
            assert heartbeat.lost is True

    messages = [record.getMessage() for record in caplog.records]
    assert any("RuntimeError" in message for message in messages)
    assert all("persistence_unavailable" not in message for message in messages)


# ----- input contracts ---------------------------------------------------


def test_claim_cancel_and_commit_reject_an_unknown_job(
    dsn: str, store: PostgresParserJobStore, states: PostgresWorkerAttemptStates
) -> None:
    unknown = str(uuid.uuid4())

    # The frozen protocol answers "no claimable work" with None; the worker
    # turns that into a typed error, because an unknown job is a different
    # failure than a contended one.
    assert store.claim_attempt(job_id=unknown, worker_id="worker-a", lease_seconds=60) is None
    with pytest.raises(ParserJobNotFoundError):
        store.cancel_job(unknown)
    with pytest.raises(ParserJobNotFoundError):
        store.commit_result(
            job_id=unknown,
            fencing_token=1,
            envelope={"source_sha256": "0" * 64},
            result_ref="ref://unknown",
        )
    assert (
        store.renew_attempt(job_id=unknown, fencing_token=1, lease_seconds=60) is False
    )

    worker = ParserWorker(
        ParserWorkerConfig(
            authority=store, states=states, worker_id="worker-a", lease_seconds=60
        )
    )
    with pytest.raises(ParserJobNotFoundError):
        worker.claim(unknown)


def test_the_worker_settings_come_from_the_environment_and_require_a_dsn() -> None:
    dsn = "postgresql://example.invalid/parser_test"
    settings = WorkerSettings.from_env(
        {
            "FIN_PARSER_DSN": dsn,
            "FIN_REDIS_URL": "redis://localhost:6379/0",
            "FIN_PARSER_WORKER_ID": "worker-a",
            "FIN_PARSER_LEASE_SECONDS": "45",
        }
    )
    assert settings.dsn == dsn
    assert settings.redis.url == "redis://localhost:6379/0"
    assert settings.worker_id == "worker-a"
    assert settings.lease_seconds == 45.0

    with pytest.raises(ParserInvalidRequestError):
        WorkerSettings.from_env({})

    with pytest.raises(ParserInvalidRequestError):
        WorkerSettings.from_env({"FIN_PARSER_DSN": dsn, "FIN_PARSER_LEASE_SECONDS": "0"})


# ----- process lifetime --------------------------------------------------


_PRODUCER_SOURCE = '''
from fin.parser.worker import AttemptResult


def produce(attempt):
    return AttemptResult(
        envelope={"source_sha256": "0" * 64},
        result_ref="ref://controlled-" + str(attempt.fencing_token),
    )
'''


def test_the_worker_process_shuts_down_cleanly_on_sigterm(
    dsn: str, tmp_path: Path
) -> None:
    job_id = _submit_job(PostgresParserJobStore(dsn, max_open_jobs=10_000))
    (tmp_path / "controlled_producer.py").write_text(_PRODUCER_SOURCE, encoding="utf-8")
    worker_id = "sigterm-worker"
    # The application name rides on the DSN, so every connection the child
    # opens is identifiable after it exits.
    child_dsn = make_conninfo(dsn, application_name=f"fin-parser-worker:{worker_id}")
    env = _child_env(
        dsn,
        FIN_PARSER_DSN=child_dsn,
        FIN_PARSER_WORKER_ID=worker_id,
        FIN_PARSER_PRODUCER="controlled_producer:produce",
        FIN_PARSER_POLL_SECONDS="0.2",
        FIN_PARSER_LEASE_SECONDS="30",
        PYTHONPATH=str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "fin.parser.worker_main", "run", "--job-id", job_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if _job_status(dsn, job_id) == "succeeded":
                break
            threading.Event().wait(0.1)
        else:
            raise AssertionError("the worker never committed the controlled result")
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    assert proc.returncode == 0, stderr
    assert dsn not in stdout
    assert dsn not in stderr
    assert _result_tokens(dsn, job_id) == [1]

    with psycopg.connect(dsn) as conn:
        sessions = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
            (f"fin-parser-worker:{worker_id}",),
        ).fetchone()[0]
    assert int(sessions) == 0
