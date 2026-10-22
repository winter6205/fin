"""HTTP tests for the parser job surface (contract section 7).

The surface answers with a stable ``{"error": "<code>"}`` token only: no
internal detail, no filesystem path and no digest the caller did not
send. A rejection must never leave an accepted job behind, and an
acceptance must never be answered before the job row is durable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.api import (
    MAX_ARTIFACT_REF_LENGTH,
    MAX_DECLARED_FORMAT_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
)
from fin.parser.app import create_app
from fin.parser.job_store import PostgresParserJobStore

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

# The child process reads the job over HTTP from a fresh interpreter with
# an unreachable Redis endpoint in its environment.
_CHILD_READ = """
import json, os, sys

from fastapi.testclient import TestClient

from fin.parser.app import create_app

client = TestClient(create_app())
response = client.get("/v1/jobs/" + os.environ["FIN_JOB_ID"])
print(json.dumps({
    "status_code": response.status_code,
    "body": response.json(),
    "redis_loaded": "redis" in sys.modules,
    "pid": os.getpid(),
}))
"""


def _body(key: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "idempotency_key": key,
        "input": {
            "sha256": DIGEST_A,
            "artifact_ref": "artifact://one",
            "declared_format": "pdf",
        },
    }
    payload.update(overrides)
    return payload


def _rows_for_key(dsn: str, key: str) -> int:
    with psycopg.connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_parser.job WHERE idempotency_key = %s", (key,)
            ).fetchone()[0]
        )


def _rows_for_key_prefix(dsn: str, prefix: str) -> int:
    """The rows whose idempotency key starts with the prefix."""
    with psycopg.connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_parser.job "
                "WHERE position(%s in idempotency_key) = 1",
                (prefix,),
            ).fetchone()[0]
        )


@pytest.fixture
def client(store: PostgresParserJobStore) -> TestClient:
    return TestClient(create_app(store=store))


# ----- acceptance -----


def test_post_jobs_answers_202_after_a_durable_acceptance(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    response = client.post("/v1/jobs", json=_body(job_key))

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["replayed"] is False
    record = store.get_job(payload["job_id"])
    assert record is not None
    assert record.status == "queued"


def test_post_jobs_replays_the_same_key_and_input_with_200(
    client: TestClient, job_key: str
) -> None:
    first = client.post("/v1/jobs", json=_body(job_key))
    second = client.post("/v1/jobs", json=_body(job_key))

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["replayed"] is True


def test_post_jobs_conflicts_when_the_key_is_reused_with_a_different_input(
    client: TestClient, job_key: str
) -> None:
    client.post("/v1/jobs", json=_body(job_key))

    response = client.post(
        "/v1/jobs",
        json=_body(
            job_key,
            input={"sha256": DIGEST_B, "artifact_ref": None, "declared_format": None},
        ),
    )

    assert response.status_code == 409
    assert response.json() == {"error": "job_conflict"}


# ----- rejection classes -----


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"idempotency_key": ""}, "the identity is empty"),
        ({"idempotency_key": "   "}, "the identity is blank"),
        (
            {"input": {"sha256": "", "artifact_ref": None, "declared_format": None}},
            "the digest is empty",
        ),
        (
            {"input": {"sha256": "not-a-digest", "artifact_ref": None, "declared_format": None}},
            "the digest is malformed",
        ),
        (
            {"input": {"sha256": DIGEST_A[:-1], "artifact_ref": None, "declared_format": None}},
            "the digest is truncated",
        ),
        (
            {"input": {"sha256": "z" * 64, "artifact_ref": None, "declared_format": None}},
            "the digest holds a character that is not a hex digit",
        ),
        (
            {"input": {"artifact_ref": None, "declared_format": None}},
            "the input digest is missing",
        ),
        ({"input": {"sha256": DIGEST_A, "artifact_ref": 17}}, "the artifact ref is not text"),
    ],
)
def test_post_jobs_rejects_a_malformed_request(
    client: TestClient, job_key: str, overrides: dict[str, object], reason: str
) -> None:
    response = client.post("/v1/jobs", json=_body(job_key, **overrides))

    assert response.status_code == 400, reason
    assert response.json() == {"error": "invalid_request"}, reason


@pytest.mark.parametrize(
    ("digest", "reason"),
    [
        (DIGEST_A.upper(), "the digest is upper-case hex"),
        ("a" * 63 + "A", "the digest holds one upper-case hex digit"),
    ],
)
def test_post_jobs_refuses_a_digest_that_is_not_lower_case_hex(
    client: TestClient,
    store: PostgresParserJobStore,
    job_key: str,
    digest: str,
    reason: str,
) -> None:
    """The contract asks for exactly 64 lower-case hex characters."""
    response = client.post(
        "/v1/jobs",
        json=_body(
            job_key,
            input={"sha256": digest, "artifact_ref": None, "declared_format": None},
        ),
    )

    assert response.status_code == 400, reason
    assert response.json() == {"error": "invalid_request"}, reason
    assert _rows_for_key(store.connection_dsn, job_key) == 0, reason


@pytest.mark.parametrize(
    ("control", "reason"),
    [
        ("\x07", "the identity holds a BEL control character"),
        ("\x00", "the identity holds a NUL control character"),
        ("\n", "the identity holds a newline"),
        ("\x7f", "the identity holds a DEL character"),
    ],
)
def test_post_jobs_refuses_an_identity_that_holds_a_control_character(
    client: TestClient,
    store: PostgresParserJobStore,
    job_key: str,
    control: str,
    reason: str,
) -> None:
    """The contract refuses any control character in the identity."""
    response = client.post(
        "/v1/jobs", json=_body(job_key, idempotency_key=f"{job_key}{control}tail")
    )

    assert response.status_code == 400, reason
    assert response.json() == {"error": "invalid_request"}, reason
    assert _rows_for_key_prefix(store.connection_dsn, job_key) == 0, reason


def test_post_jobs_accepts_a_lower_case_digest_and_stores_it_as_given(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    """The conforming digest is accepted, and the row carries it unchanged."""
    digest = "0123456789abcdef" * 4

    response = client.post(
        "/v1/jobs",
        json=_body(
            job_key,
            input={"sha256": digest, "artifact_ref": None, "declared_format": None},
        ),
    )

    assert response.status_code == 202
    record = store.get_job(response.json()["job_id"])
    assert record is not None
    assert record.input_sha256 == digest


@pytest.mark.parametrize(
    "case",
    [
        {
            "overrides": {
                "idempotency_key": "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1),
            },
            "reason": "the identity is one character too long",
        },
        {
            "overrides": {
                "input": {
                    "sha256": DIGEST_A,
                    "artifact_ref": "r" * (MAX_ARTIFACT_REF_LENGTH + 1),
                    "declared_format": None,
                }
            },
            "reason": "the artifact ref is one character too long",
        },
        {
            "overrides": {
                "input": {
                    "sha256": DIGEST_A,
                    "artifact_ref": None,
                    "declared_format": "f" * (MAX_DECLARED_FORMAT_LENGTH + 1),
                }
            },
            "reason": "the declared format is one character too long",
        },
    ],
)
def test_post_jobs_rejects_an_over_long_value(
    client: TestClient,
    store: PostgresParserJobStore,
    job_key: str,
    case: dict[str, object],
) -> None:
    """A value past its bound is refused before anything is written."""
    reason = str(case["reason"])
    response = client.post("/v1/jobs", json=_body(job_key, **case["overrides"]))

    assert response.status_code == 400, reason
    assert response.json() == {"error": "invalid_request"}, reason
    assert _rows_for_key(store.connection_dsn, job_key) == 0, reason


def test_post_jobs_accepts_a_value_that_reaches_its_bound_exactly(
    client: TestClient, job_key: str
) -> None:
    """The bound is the last accepted length, not the first refused one."""
    # The key keeps this run's prefix, so the case is a new acceptance and
    # not a replay of an earlier run's row.
    key = job_key + "k" * (MAX_IDEMPOTENCY_KEY_LENGTH - len(job_key))
    response = client.post(
        "/v1/jobs",
        json=_body(
            key,
            input={
                "sha256": DIGEST_A,
                "artifact_ref": "r" * MAX_ARTIFACT_REF_LENGTH,
                "declared_format": "f" * MAX_DECLARED_FORMAT_LENGTH,
            },
        ),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_post_jobs_rejects_a_body_that_is_not_an_object(client: TestClient) -> None:
    response = client.post("/v1/jobs", json=["not", "an", "object"])

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_post_jobs_rejects_a_missing_body(client: TestClient) -> None:
    response = client.post("/v1/jobs")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_post_jobs_rejects_a_missing_identity(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"input": {"sha256": DIGEST_A}})

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_a_rejected_request_leaves_no_accepted_job(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    client.post("/v1/jobs", json=_body(job_key, idempotency_key=""))

    assert _rows_for_key(store.connection_dsn, job_key) == 0


# ----- reads -----


def test_get_job_answers_the_durable_status(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job_id
    assert payload["status"] == "queued"
    assert payload["input_sha256"] == DIGEST_A


def test_get_job_answers_404_for_an_unknown_job(client: TestClient) -> None:
    response = client.get("/v1/jobs/11111111-1111-1111-1111-111111111111")

    assert response.status_code == 404
    assert response.json() == {"error": "job_not_found"}


def test_get_job_rejects_a_malformed_job_id(client: TestClient) -> None:
    response = client.get("/v1/jobs/not-a-uuid")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_get_result_answers_404_when_the_job_has_no_result(
    client: TestClient, job_key: str
) -> None:
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}/result")

    assert response.status_code == 404
    assert response.json() == {"error": "job_not_found"}


def test_get_result_answers_the_committed_result(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]
    attempt = store.claim_attempt(job_id=job_id, worker_id="w1", lease_seconds=60)
    assert attempt is not None
    store.commit_result(
        job_id=job_id,
        fencing_token=attempt.fencing_token,
        envelope={"schema_version": "1", "units": []},
        result_ref="derived://job/out.json",
    )

    response = client.get(f"/v1/jobs/{job_id}/result")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job_id
    assert payload["fencing_token"] == attempt.fencing_token
    assert payload["envelope"]["schema_version"] == "1"
    assert payload["result_ref"] == "derived://job/out.json"


# ----- cancellation and deletion -----


def test_cancel_answers_the_cancelled_status(client: TestClient, job_key: str) -> None:
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]

    response = client.post(f"/v1/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "cancelled"


def test_delete_answers_204_and_blocks_a_replay(client: TestClient, job_key: str) -> None:
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]

    response = client.delete(f"/v1/jobs/{job_id}")

    assert response.status_code == 204
    assert response.content == b""
    replay = client.post("/v1/jobs", json=_body(job_key))
    assert replay.status_code == 409
    assert replay.json() == {"error": "job_tombstoned"}


def test_delete_answers_404_for_an_unknown_job(client: TestClient) -> None:
    response = client.delete("/v1/jobs/11111111-1111-1111-1111-111111111111")

    assert response.status_code == 404
    assert response.json() == {"error": "job_not_found"}


def test_delete_answers_204_when_the_job_was_already_deleted(
    client: TestClient, job_key: str
) -> None:
    """The delete is idempotent, and the tombstone still blocks a replay."""
    job_id = client.post("/v1/jobs", json=_body(job_key)).json()["job_id"]

    first = client.delete(f"/v1/jobs/{job_id}")
    second = client.delete(f"/v1/jobs/{job_id}")

    assert first.status_code == 204
    assert second.status_code == 204
    assert second.content == b""
    replay = client.post("/v1/jobs", json=_body(job_key))
    assert replay.status_code == 409
    assert replay.json() == {"error": "job_tombstoned"}


# ----- the failure surface -----


def test_a_postgres_failure_answers_503_and_accepts_nothing(
    monkeypatch: pytest.MonkeyPatch, parser_dsn: str, parser_database: str, job_key: str
) -> None:
    info = conninfo_to_dict(parser_dsn)
    info["port"] = "1"
    monkeypatch.setenv("FIN_PARSER_DSN", make_conninfo(**info))
    client = TestClient(create_app())

    response = client.post("/v1/jobs", json=_body(job_key))

    assert response.status_code == 503
    assert response.json() == {"error": "persistence_unavailable"}
    assert _rows_for_key(parser_database, job_key) == 0


def test_an_unconfigured_store_answers_503(
    monkeypatch: pytest.MonkeyPatch, job_key: str
) -> None:
    monkeypatch.delenv("FIN_PARSER_DSN", raising=False)
    client = TestClient(create_app())

    response = client.post("/v1/jobs", json=_body(job_key))

    assert response.status_code == 503
    assert response.json() == {"error": "persistence_unavailable"}


def test_the_error_body_carries_only_the_stable_code(
    client: TestClient, store: PostgresParserJobStore, job_key: str
) -> None:
    response = client.post("/v1/jobs", json=_body(job_key, idempotency_key=""))

    assert set(response.json()) == {"error"}
    assert "detail" not in response.json()
    assert "/" not in response.json()["error"]


# ----- a fresh process, no Redis -----


def test_a_fresh_process_reads_the_durable_status_while_redis_is_unreachable(
    tmp_path: Path, store: PostgresParserJobStore, parser_dsn: str, job_key: str
) -> None:
    job_id = store.submit_job(
        idempotency_key=job_key,
        input_sha256=DIGEST_A,
        source_ref=None,
        declared_format=None,
    ).job_id
    env = {
        **os.environ,
        "FIN_PARSER_DSN": parser_dsn,
        "FIN_JOB_ID": job_id,
        # The wakeup channel is unreachable: the read path must not need it.
        "FIN_REDIS_URL": "redis://127.0.0.1:1",
    }

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_READ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status_code"] == 200
    assert payload["body"]["status"] == "queued"
    assert payload["body"]["job_id"] == job_id
    assert payload["redis_loaded"] is False
    assert payload["pid"] != os.getpid()
