"""Durable parse-result existence, the commit fence, and deletion tombstones.

T8/T9 evidence against REAL PostgreSQL and REAL OS processes: a controlled
result commits through the accepted fence, is readable from a fresh process
after a restart with Redis unavailable, and a deleted job leaves a durable
tombstone that blocks a replay and rejects a late worker commit.

The controlled result is a JOB LIFECYCLE demonstration only. No adapter is
integrated, no provider is selected, and no real file is parsed; the source
bytes below are synthetic and exist only so the "the row holds no source
bytes" assertion has something concrete to look for.

The cases drive the REAL store and the REAL result boundary, not a
stand-in: a submission, a claim, a cancellation, a commit, a read and a
delete all go through ``PostgresParserJobStore``, and the envelope contract
is enforced by ``fin.parser.results.ResultBoundary``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any

import psycopg
import pytest

from fin.parser.envelope import ENVELOPE_VERSION, ParsedDocument, parse_envelope
from fin.parser.errors import (
    ParserEnvelopeError,
    ParserJobCancelledError,
    ParserJobTombstonedError,
    ParserPersistenceError,
    ParserStaleFenceError,
)
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.results import (
    ResultBoundary,
    find_tombstone_by_idempotency_key,
    parser_read_connection,
    read_tombstone,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FIN_TEST_DSN") or "").strip(),
    reason="FIN_TEST_DSN is unset; the parser result authority is PostgreSQL-only",
)

#: Synthetic source bytes. They must never appear in a result row.
SOURCE_BYTES = b"%PDF-1.7 controlled source bytes for the parser result case.\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _controlled_envelope(
    *, result_ref: str, source: bytes = SOURCE_BYTES
) -> dict[str, Any]:
    """A declared controlled result: valid, referenced, and provider-free."""
    return {
        "envelope_version": ENVELOPE_VERSION,
        "source_sha256": _sha(source),
        "declared_format": "pdf",
        "detected_format": "pdf",
        "parser_build": {
            "parser": "controlled",
            "provider": "controlled",
            "version": "controlled-1",
        },
        "model_fingerprint": "controlled-model-fingerprint",
        "config_fingerprint": "controlled-config-fingerprint",
        "parse_rule_version": "controlled-rule-1",
        "result_ref": result_ref,
        "units": [
            {
                "index": 0,
                "kind": "page",
                "text_ref": "fin-parser/results/controlled/units/0.txt",
                "char_count": 24,
                "locator": "page 1",
            }
        ],
        "assets": [],
        "coverage": {
            "status": "complete",
            "scopes": [{"scope": "text", "status": "complete", "required": True}],
        },
        "diagnostics": [],
    }


def _accept(
    store: PostgresParserJobStore, *, idempotency_key: str
) -> tuple[str, int]:
    """Accept one job and claim one attempt, the way the worker does."""
    submission = store.submit_job(
        idempotency_key=idempotency_key,
        input_sha256=_sha(SOURCE_BYTES),
        source_ref=None,
        declared_format="pdf",
    )
    attempt = store.claim_attempt(
        job_id=submission.job_id, worker_id="worker-a", lease_seconds=60
    )
    assert attempt is not None, "a freshly accepted job must be claimable"
    return submission.job_id, attempt.fencing_token


def _count(dsn: str, sql: str, job_id: str) -> int:
    with psycopg.connect(dsn) as conn:
        return int(conn.execute(sql, (job_id,)).fetchone()[0])


def _result_rows(dsn: str, job_id: str) -> int:
    return _count(dsn, "SELECT count(*) FROM fin_parser.result WHERE job_id = %s", job_id)


def _tombstone_rows(dsn: str, job_id: str) -> int:
    return _count(
        dsn, "SELECT count(*) FROM fin_parser.tombstone WHERE job_id = %s", job_id
    )


def _tombstone_rows_for_key(dsn: str, idempotency_key: str) -> int:
    with psycopg.connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_parser.tombstone WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()[0]
        )


def _attempt_rows(dsn: str, job_id: str) -> int:
    return _count(
        dsn, "SELECT count(*) FROM fin_parser.attempt WHERE job_id = %s", job_id
    )


# ----- cross-process readers ---------------------------------------------


_READ_CHILD = """
import json
import os
import socket
import sys
from urllib.parse import urlparse

from fin.parser.job_store import PostgresParserJobStore
from fin.parser.results import ResultBoundary

store = PostgresParserJobStore(os.environ["FIN_TEST_DSN"])
boundary = ResultBoundary(store)
record = boundary.read_result(os.environ["FIN_JOB_ID"])
document = boundary.read_document(os.environ["FIN_JOB_ID"])

url = urlparse(os.environ["FIN_REDIS_URL"])
reachable = True
try:
    with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=1):
        pass
except OSError:
    reachable = False

print(json.dumps({
    "pid": os.getpid(),
    "job_id": record.job_id if record else None,
    "envelope": record.envelope if record else None,
    "result_ref": record.result_ref if record else None,
    "document": document.model_dump(mode="json") if document else None,
    "redis_reachable": reachable,
    "redis_imported": "redis" in sys.modules,
}))
"""

_DELETE_CHILD = """
import json
import os

from fin.parser.errors import ParserError
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.results import read_tombstone

store = PostgresParserJobStore(os.environ["FIN_TEST_DSN"])
job_id = os.environ["FIN_JOB_ID"]
tombstone = read_tombstone(os.environ["FIN_TEST_DSN"], job_id)
out = {
    "pid": os.getpid(),
    "tombstone_key": tombstone.idempotency_key if tombstone else None,
    "commit_error": None,
    "replay_error": None,
}

try:
    store.commit_result(
        job_id=job_id,
        fencing_token=int(os.environ["FIN_TOKEN"]),
        envelope=json.loads(os.environ["FIN_ENVELOPE"]),
        result_ref=os.environ["FIN_RESULT_REF"],
    )
except ParserError as exc:
    out["commit_error"] = exc.code

try:
    store.submit_job(
        idempotency_key=os.environ["FIN_IDEMPOTENCY_KEY"],
        input_sha256=os.environ["FIN_INPUT_SHA"],
        source_ref=None,
        declared_format=None,
    )
except ParserError as exc:
    out["replay_error"] = exc.code

print(json.dumps(out))
"""


def _run_child(program: str, env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", program],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# ----- T8: the fenced commit and the durable read -------------------------


def test_a_controlled_result_is_readable_from_a_fresh_process_after_restart(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """The restart leg: a real second process reads the committed result.

    The child holds nothing but the DSN and the job id, and its Redis
    endpoint is unreachable — the result authority is PostgreSQL alone.
    """
    job_id, token = _accept(store, idempotency_key=job_key)
    derived_ref = f"fin-parser/results/{job_id}/derived.json"
    boundary = ResultBoundary(store)

    record = boundary.commit_result(
        job_id=job_id,
        fencing_token=token,
        envelope=_controlled_envelope(result_ref=derived_ref),
        result_ref=derived_ref,
    )
    assert record.job_id == job_id
    assert record.fencing_token == token
    assert record.result_ref == derived_ref

    child = _run_child(
        _READ_CHILD,
        {
            **os.environ,
            "FIN_JOB_ID": job_id,
            # deliberately unreachable: the read must not need Redis
            "FIN_REDIS_URL": "redis://127.0.0.1:1/0",
        },
    )
    assert child["pid"] != os.getpid()
    assert child["job_id"] == job_id
    assert child["result_ref"] == derived_ref
    assert child["redis_reachable"] is False
    assert child["redis_imported"] is False

    document = parse_envelope(child["envelope"])
    assert isinstance(document, ParsedDocument)
    assert document.source_sha256 == _sha(SOURCE_BYTES)
    assert document.result_ref == derived_ref
    assert parse_envelope(child["document"]) == document


def test_a_stale_fencing_token_cannot_commit_and_writes_nothing(
    store: PostgresParserJobStore, job_key: str
) -> None:
    job_id, first = _accept(store, idempotency_key=job_key)
    takeover = store.claim_attempt(
        job_id=job_id, worker_id="worker-b", lease_seconds=60
    )
    assert takeover is not None
    assert takeover.fencing_token > first

    boundary = ResultBoundary(store)
    with pytest.raises(ParserStaleFenceError):
        boundary.commit_result(
            job_id=job_id,
            fencing_token=first,
            envelope=_controlled_envelope(result_ref="fin-parser/results/stale.json"),
            result_ref="fin-parser/results/stale.json",
        )

    assert store.read_result(job_id) is None
    assert _result_rows(store.connection_dsn, job_id) == 0


def test_a_cancelled_job_cannot_commit_and_writes_nothing(
    store: PostgresParserJobStore, job_key: str
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    store.cancel_job(job_id)

    boundary = ResultBoundary(store)
    with pytest.raises(ParserJobCancelledError):
        boundary.commit_result(
            job_id=job_id,
            fencing_token=token,
            envelope=_controlled_envelope(result_ref="fin-parser/results/cancelled.json"),
            result_ref="fin-parser/results/cancelled.json",
        )

    assert store.read_result(job_id) is None
    assert _result_rows(store.connection_dsn, job_id) == 0


def test_a_result_that_breaks_the_envelope_contract_is_not_written(
    store: PostgresParserJobStore, job_key: str
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    broken = _controlled_envelope(result_ref="fin-parser/results/broken.json")
    broken.pop("coverage")

    boundary = ResultBoundary(store)
    with pytest.raises(ParserEnvelopeError):
        boundary.commit_result(
            job_id=job_id,
            fencing_token=token,
            envelope=broken,
            result_ref="fin-parser/results/broken.json",
        )

    assert store.read_result(job_id) is None
    assert _result_rows(store.connection_dsn, job_id) == 0


def test_the_read_connection_refuses_a_write(
    store: PostgresParserJobStore,
) -> None:
    """The read connection is read-only, and the claim is enforced.

    A write through it is refused by PostgreSQL and surfaces as the
    typed persistence failure, so no read path can write by accident.
    """
    with pytest.raises(ParserPersistenceError):
        with parser_read_connection(store.connection_dsn) as conn:
            conn.execute("CREATE TEMP TABLE parser_read_only_probe (x int)")


def test_the_result_row_references_derived_output_and_never_holds_the_source_bytes(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """The reference rule, asserted structurally rather than by convention."""
    job_id, token = _accept(store, idempotency_key=job_key)
    derived_ref = f"fin-parser/results/{job_id}/derived.json"
    ResultBoundary(store).commit_result(
        job_id=job_id,
        fencing_token=token,
        envelope=_controlled_envelope(result_ref=derived_ref),
        result_ref=derived_ref,
    )

    with psycopg.connect(store.connection_dsn) as conn:
        columns = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'fin_parser' AND table_name = 'result'"
            " ORDER BY column_name"
        ).fetchall()
        assert {row[0] for row in columns} == {
            "created_at",
            "envelope",
            "fencing_token",
            "job_id",
            "result_ref",
        }
        assert all(row[1] != "bytea" for row in columns)
        raw = conn.execute(
            "SELECT row_to_json(t)::text FROM fin_parser.result t WHERE t.job_id = %s",
            (job_id,),
        ).fetchone()[0]

    assert SOURCE_BYTES.decode() not in raw
    assert derived_ref in raw
    # the digest of the source is the only trace of the source in the row
    assert _sha(SOURCE_BYTES) in raw


# ----- T9: the deletion tombstone -----------------------------------------


def test_a_deleted_job_leaves_one_tombstone_and_blocks_a_replay(
    store: PostgresParserJobStore, job_key: str
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    derived_ref = f"fin-parser/results/{job_id}/derived.json"
    ResultBoundary(store).commit_result(
        job_id=job_id,
        fencing_token=token,
        envelope=_controlled_envelope(result_ref=derived_ref),
        result_ref=derived_ref,
    )

    store.delete_job(job_id)

    tombstone = read_tombstone(store.connection_dsn, job_id)
    assert tombstone is not None
    assert tombstone.idempotency_key == job_key
    assert tombstone.input_sha256 == _sha(SOURCE_BYTES)
    assert find_tombstone_by_idempotency_key(store.connection_dsn, job_key) is not None
    assert _tombstone_rows(store.connection_dsn, job_id) == 1
    assert _tombstone_rows_for_key(store.connection_dsn, job_key) == 1
    assert store.get_job(job_id) is None
    assert store.read_result(job_id) is None
    assert _result_rows(store.connection_dsn, job_id) == 0
    assert _attempt_rows(store.connection_dsn, job_id) == 0

    with pytest.raises(ParserJobTombstonedError):
        store.submit_job(
            idempotency_key=job_key,
            input_sha256=_sha(SOURCE_BYTES),
            source_ref=None,
            declared_format="pdf",
        )

    assert store.get_job(job_id) is None
    assert store.read_result(job_id) is None
    assert _tombstone_rows(store.connection_dsn, job_id) == 1
    assert _tombstone_rows_for_key(store.connection_dsn, job_key) == 1


def test_a_late_worker_commit_after_deletion_is_rejected(
    store: PostgresParserJobStore, job_key: str
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)

    store.delete_job(job_id)

    boundary = ResultBoundary(store)
    with pytest.raises(ParserJobTombstonedError):
        boundary.commit_result(
            job_id=job_id,
            fencing_token=token,
            envelope=_controlled_envelope(
                result_ref=f"fin-parser/results/{job_id}/late.json"
            ),
            result_ref=f"fin-parser/results/{job_id}/late.json",
        )

    assert _result_rows(store.connection_dsn, job_id) == 0
    assert store.get_job(job_id) is None
    assert _tombstone_rows(store.connection_dsn, job_id) == 1


def test_deletion_stays_authoritative_after_a_process_restart(
    store: PostgresParserJobStore, job_key: str
) -> None:
    """The tombstone is durable authority, not an in-process fact."""
    job_id, token = _accept(store, idempotency_key=job_key)
    derived_ref = f"fin-parser/results/{job_id}/derived.json"
    ResultBoundary(store).commit_result(
        job_id=job_id,
        fencing_token=token,
        envelope=_controlled_envelope(result_ref=derived_ref),
        result_ref=derived_ref,
    )
    store.delete_job(job_id)

    child = _run_child(
        _DELETE_CHILD,
        {
            **os.environ,
            "FIN_JOB_ID": job_id,
            "FIN_TOKEN": str(token),
            "FIN_RESULT_REF": derived_ref,
            "FIN_ENVELOPE": json.dumps(_controlled_envelope(result_ref=derived_ref)),
            "FIN_IDEMPOTENCY_KEY": job_key,
            "FIN_INPUT_SHA": _sha(SOURCE_BYTES),
        },
    )

    assert child["pid"] != os.getpid()
    assert child["tombstone_key"] == job_key
    assert child["commit_error"] == "job_tombstoned"
    assert child["replay_error"] == "job_tombstoned"

    assert store.get_job(job_id) is None
    assert _result_rows(store.connection_dsn, job_id) == 0
    assert _tombstone_rows(store.connection_dsn, job_id) == 1
