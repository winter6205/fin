"""Two-process concurrency contracts for the parser job store.

The same idempotency key with a DIFFERENT input is a conflict, and the
conflict must be decided the same way no matter which process asks first.
A single-process case cannot prove that: the two submissions never meet.
So every case here runs two REAL operating-system processes against the REAL
PostgreSQL ``_test`` database, held at a parent-controlled barrier.

Each child announces its readiness on stdout and blocks on stdin; the parent
releases both only after both announcements arrived, so the two submissions
are genuinely concurrent instead of accidentally sequential.  No ``sleep``
sequences a race anywhere in this file, and nothing is mocked.

The API level is included on purpose: the typed store failure below it must
surface as the stable ``{"error": "job_conflict"}`` token, and the durable
row must belong to exactly one of the two processes.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

import psycopg

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

# The store level: the child submits through the store's public path and
# reports the typed outcome it got.
_STORE_CHILD = """
import json, os, sys

from fin.parser.errors import ParserJobConflictError
from fin.parser.job_store import PostgresParserJobStore

store = PostgresParserJobStore(os.environ["FIN_PARSER_DSN"])
print("READY " + str(os.getpid()), flush=True)
sys.stdin.readline()
try:
    submission = store.submit_job(
        idempotency_key=os.environ["FIN_KEY"],
        input_sha256=os.environ["FIN_SHA"],
        source_ref=None,
        declared_format=None,
    )
except ParserJobConflictError as exc:
    print(json.dumps({"outcome": "conflict", "code": exc.code}), flush=True)
else:
    print(json.dumps({
        "outcome": "accepted",
        "job_id": submission.job_id,
        "status": submission.status,
        "replayed": submission.replayed,
    }), flush=True)
"""

# The HTTP level: the child posts the same key with the same input class
# and reports the answer it got.
_API_CHILD = """
import json, os, sys

from fastapi.testclient import TestClient

from fin.parser.app import create_app

client = TestClient(create_app())
print("READY " + str(os.getpid()), flush=True)
sys.stdin.readline()
response = client.post("/v1/jobs", json={
    "idempotency_key": os.environ["FIN_KEY"],
    "input": {
        "sha256": os.environ["FIN_SHA"],
        "artifact_ref": None,
        "declared_format": None,
    },
})
print(json.dumps({
    "status_code": response.status_code,
    "body": response.json(),
}), flush=True)
"""


def _rows_for_key(dsn: str, key: str) -> list[tuple[str, str]]:
    """The durable rows that carry the key, with the input they hold."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT job_id, input_sha256 FROM fin_parser.job "
            "WHERE idempotency_key = %s ORDER BY job_id",
            (key,),
        ).fetchall()
        conn.commit()
    return [(str(row[0]), str(row[1])) for row in rows]


class _SubmissionRace:
    """Two children held at a parent-controlled barrier.

    Each child announces readiness on stdout and blocks on stdin; the parent
    releases both only after both announcements arrived, so the two
    submissions overlap instead of accidentally running one after the other.
    """

    def __init__(self, program: str, envs: list[dict[str, str]]) -> None:
        self._procs: list[subprocess.Popen[str]] = []
        self._threads: list[threading.Thread] = []
        self._events: queue.Queue[str] = queue.Queue()
        self._output: list[list[str]] = [[] for _ in envs]
        for index, env in enumerate(envs):
            proc = subprocess.Popen(
                [sys.executable, "-c", program],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, **env},
            )
            self._procs.append(proc)
            self._threads.append(
                threading.Thread(target=self._read, args=(proc, index), daemon=True)
            )
        for thread in self._threads:
            thread.start()

    def _read(self, proc: subprocess.Popen[str], index: int) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            self._output[index].append(text)
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

    def collect(self) -> list[dict[str, Any]]:
        """The JSON answer of every child."""
        answers: list[dict[str, Any]] = []
        for index, proc in enumerate(self._procs):
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            assert proc.wait(timeout=120) == 0, stderr
            lines = [line for line in self._output[index] if not line.startswith("READY ")]
            assert lines, "the child printed no answer"
            answers.append(json.loads(lines[-1]))
        return answers


def _child_env(dsn: str, key: str, digest: str) -> dict[str, str]:
    return {
        "FIN_PARSER_DSN": dsn,
        "FIN_KEY": key,
        "FIN_SHA": digest,
    }


def test_two_processes_with_the_same_key_and_a_different_input_produce_one_job(
    parser_database: str, job_key: str
) -> None:
    """Exactly one submission becomes durable; the other is refused."""
    race = _SubmissionRace(
        _STORE_CHILD,
        [
            _child_env(parser_database, job_key, DIGEST_A),
            _child_env(parser_database, job_key, DIGEST_B),
        ],
    )

    pids = race.release()
    answers = race.collect()

    assert len(set(pids)) == 2
    assert all(pid != os.getpid() for pid in pids)
    outcomes = sorted(str(answer["outcome"]) for answer in answers)
    assert outcomes == ["accepted", "conflict"]
    conflict = next(answer for answer in answers if answer["outcome"] == "conflict")
    assert conflict["code"] == "job_conflict"

    # The durable row belongs to exactly one of the two submissions.
    rows = _rows_for_key(parser_database, job_key)
    assert len(rows) == 1
    assert rows[0][1] in (DIGEST_A, DIGEST_B)
    accepted = next(answer for answer in answers if answer["outcome"] == "accepted")
    assert accepted["job_id"] == rows[0][0]
    assert accepted["status"] == "queued"
    assert accepted["replayed"] is False


def test_two_processes_with_the_same_key_and_a_different_input_answer_409_at_the_api(
    parser_database: str, job_key: str
) -> None:
    """The conflict below the store is the stable token at the surface."""
    race = _SubmissionRace(
        _API_CHILD,
        [
            _child_env(parser_database, job_key, DIGEST_A),
            _child_env(parser_database, job_key, DIGEST_B),
        ],
    )

    race.release()
    answers = race.collect()

    codes = sorted(int(answer["status_code"]) for answer in answers)
    assert codes == [202, 409]
    refused = next(answer for answer in answers if answer["status_code"] == 409)
    assert refused["body"] == {"error": "job_conflict"}

    rows = _rows_for_key(parser_database, job_key)
    assert len(rows) == 1
    accepted = next(answer for answer in answers if answer["status_code"] == 202)
    assert accepted["body"]["job_id"] == rows[0][0]
    assert accepted["body"]["status"] == "queued"
    assert accepted["body"]["replayed"] is False
