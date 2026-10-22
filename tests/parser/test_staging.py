"""Orphan collection for parser staging output (T10).

An attempt that dies mid-parse leaves derived output on disk. The collection
pass identifies that output and reclaims it — and it must never reclaim
output a committed result still references, nor output a CURRENT attempt is
still writing.

Every root in this file is an injected ``tmp_path``; the parser never touches
developer data and never touches ``~/.fin/``. The crash leg is a real OS
process that dies without committing, so the "interrupted attempt" is a real
interruption and not a simulated one.

The cases drive the REAL store for the artifact lifecycle and the REAL
collector for the policy, so the safety rule under test is production code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from test_results import _controlled_envelope  # noqa: E402  (sibling module)

from fin.parser.errors import ParserInvalidRequestError, ParserPersistenceError
from fin.parser.job_store import PostgresParserJobStore
from fin.parser.results import ResultBoundary
from fin.parser.staging import (
    CollectionReport,
    FaultedArtifact,
    StagingCollector,
    StagingRoot,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FIN_TEST_DSN") or "").strip(),
    reason="FIN_TEST_DSN is unset; the parser staging authority is PostgreSQL-only",
)

#: The crash leg: a worker process writes staging output and dies WITHOUT
#: committing. ``os._exit`` skips every cleanup handler, so nothing is
#: returned to the store — exactly what an interrupted attempt leaves behind.
_CRASH_CHILD = """
import os
import sys
from pathlib import Path

from fin.parser.job_store import PostgresParserJobStore
from fin.parser.staging import StagingRoot

root = StagingRoot(os.environ["FIN_STAGING_ROOT"])
path = root.allocate(
    os.environ["FIN_JOB_ID"], int(os.environ["FIN_TOKEN"]), "derived.json"
)
Path(path).write_bytes(b"derived output that was never committed\\n")

store = PostgresParserJobStore(os.environ["FIN_TEST_DSN"])
artifact_id = store.register_staging_artifact(
    job_id=os.environ["FIN_JOB_ID"],
    fencing_token=int(os.environ["FIN_TOKEN"]),
    path=str(path),
)
sys.stdout.write(artifact_id)
sys.stdout.flush()
os._exit(0)
"""


@pytest.fixture
def staging_rows(store: PostgresParserJobStore) -> list[str]:
    """Drop this case's staging rows: the shared test database keeps no leftovers."""
    job_ids: list[str] = []
    yield job_ids
    for job_id in job_ids:
        for artifact in store.list_staging_artifacts(job_id=job_id):
            store.drop_staging_artifact(artifact.artifact_id)


def _accept(
    store: PostgresParserJobStore, *, idempotency_key: str
) -> tuple[str, int]:
    """Accept one job and claim one attempt, the way the worker does."""
    submission = store.submit_job(
        idempotency_key=idempotency_key,
        input_sha256="b" * 64,
        source_ref=None,
        declared_format="pdf",
    )
    attempt = store.claim_attempt(
        job_id=submission.job_id, worker_id="worker-a", lease_seconds=60
    )
    assert attempt is not None, "a freshly accepted job must be claimable"
    return submission.job_id, attempt.fencing_token


def _run_crash_child(*, staging_root: Path, job_id: str, token: int) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD],
        env={
            **os.environ,
            "FIN_STAGING_ROOT": str(staging_root),
            "FIN_JOB_ID": job_id,
            "FIN_TOKEN": str(token),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _reasons(report: CollectionReport, artifact_ids: set[str]) -> dict[str, str]:
    """The skip reasons of THIS case's artifacts only.

    The test database is shared, so one collection pass also sees other
    runs' artifacts. Those are skipped as outside this case's root and are
    never removed, so the assertions look only at this case's rows.
    """
    return {
        entry.artifact_id: entry.reason
        for entry in report.skipped
        if entry.artifact_id in artifact_ids
    }


def _faults(
    report: CollectionReport, artifact_ids: set[str]
) -> dict[str, FaultedArtifact]:
    """The fault entries of THIS case's artifacts only."""
    return {
        entry.artifact_id: entry
        for entry in report.faults
        if entry.artifact_id in artifact_ids
    }


def test_a_crashed_attempt_leaves_output_that_collection_reclaims(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    staging_rows.append(job_id)
    root = StagingRoot(tmp_path / "staging")

    artifact_id = _run_crash_child(
        staging_root=root.root, job_id=job_id, token=token
    )

    artifacts = store.list_staging_artifacts(job_id=job_id)
    assert [artifact.artifact_id for artifact in artifacts] == [artifact_id]
    written = Path(artifacts[0].path)
    assert written.exists()

    # a takeover raises the effective token, so the crashed attempt is stale
    takeover = store.claim_attempt(
        job_id=job_id, worker_id="worker-b", lease_seconds=60
    )
    assert takeover is not None

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == (artifact_id,)
    assert report.removed_paths == (str(written),)
    assert not written.exists()
    assert store.list_staging_artifacts(job_id=job_id) == []

def test_output_of_a_current_attempt_is_not_collected(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    staging_rows.append(job_id)
    root = StagingRoot(tmp_path / "staging")
    path = root.allocate(job_id, token, "derived.json")
    Path(path).write_bytes(b"output of the attempt that is still current\n")

    artifact_id = store.register_staging_artifact(
        job_id=job_id, fencing_token=token, path=str(path)
    )

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == ()
    assert Path(path).read_bytes() == b"output of the attempt that is still current\n"
    assert [
        artifact.artifact_id for artifact in store.list_staging_artifacts(job_id=job_id)
    ] == [artifact_id]
    assert _reasons(report, {artifact_id}) == {artifact_id: "current_attempt"}


def test_output_referenced_by_a_committed_result_survives_collection(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    """The safety rule: committed output is never collected.

    Two jobs cover the two ways a committed result protects its output: the
    ordinary flow marks the artifact referenced, and a worker that committed
    without marking it is still protected by the committed result itself. A
    third artifact that no result claims is the control: it IS collected, so
    the pass is not simply keeping everything.
    """
    root = StagingRoot(tmp_path / "staging")
    boundary = ResultBoundary(store)

    marked_job, marked_token = _accept(store, idempotency_key=f"{job_key}-marked")
    staging_rows.append(marked_job)
    marked_path = root.allocate(marked_job, marked_token, "derived.json")
    Path(marked_path).write_bytes(b"committed derived output\n")
    marked_artifact = store.register_staging_artifact(
        job_id=marked_job, fencing_token=marked_token, path=str(marked_path)
    )
    store.mark_staging_referenced(marked_artifact)
    boundary.commit_result(
        job_id=marked_job,
        fencing_token=marked_token,
        envelope=_controlled_envelope(result_ref=str(marked_path)),
        result_ref=str(marked_path),
    )

    unmarked_job, unmarked_token = _accept(store, idempotency_key=f"{job_key}-unmarked")
    staging_rows.append(unmarked_job)
    unmarked_path = root.allocate(unmarked_job, unmarked_token, "derived.json")
    Path(unmarked_path).write_bytes(b"committed derived output\n")
    unmarked_artifact = store.register_staging_artifact(
        job_id=unmarked_job, fencing_token=unmarked_token, path=str(unmarked_path)
    )
    boundary.commit_result(
        job_id=unmarked_job,
        fencing_token=unmarked_token,
        envelope=_controlled_envelope(result_ref=str(unmarked_path)),
        result_ref=str(unmarked_path),
    )

    control_job, control_token = _accept(store, idempotency_key=f"{job_key}-control")
    staging_rows.append(control_job)
    control_path = root.allocate(control_job, control_token, "derived.json")
    Path(control_path).write_bytes(b"output no result claims\n")
    control_artifact = store.register_staging_artifact(
        job_id=control_job, fencing_token=control_token, path=str(control_path)
    )
    # the control attempt is stale, so nothing but the result rules could
    # have kept it: the pass must reclaim it
    control_takeover = store.claim_attempt(
        job_id=control_job, worker_id="worker-b", lease_seconds=60
    )
    assert control_takeover is not None

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == (control_artifact,)
    assert Path(marked_path).read_bytes() == b"committed derived output\n"
    assert Path(unmarked_path).read_bytes() == b"committed derived output\n"
    assert not control_path.exists()
    reasons = _reasons(report, {marked_artifact, unmarked_artifact, control_artifact})
    assert reasons[marked_artifact] == "marked_referenced"
    assert reasons[unmarked_artifact] == "referenced_by_result"
    assert control_artifact not in reasons


def test_collection_never_removes_an_artifact_outside_the_injected_root(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    job_id, token = _accept(store, idempotency_key=job_key)
    staging_rows.append(job_id)
    root = StagingRoot(tmp_path / "staging")
    outside = tmp_path / "elsewhere" / "derived.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"not the parser's staging output\n")

    artifact_id = store.register_staging_artifact(
        job_id=job_id, fencing_token=token, path=str(outside)
    )

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == ()
    assert outside.exists()
    assert _reasons(report, {artifact_id}) == {artifact_id: "outside_root"}


def test_a_staging_root_inside_the_operator_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".fin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ParserInvalidRequestError):
        StagingRoot(home / ".fin" / "parser-staging")


def test_a_path_the_filesystem_cannot_resolve_is_a_fault_not_an_outside_root_skip(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    """A filesystem fault is never reported as the policy "outside the root".

    A symlink loop makes the artifact's path unresolvable. The pass must
    report a FAULT with its own reason and the parser-owned persistence
    failure, and it must keep the artifact and its row: the operator has to
    see a broken filesystem, never a decision the policy made.
    """
    job_id, token = _accept(store, idempotency_key=job_key)
    staging_rows.append(job_id)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    root = StagingRoot(staging_dir)
    loop = root.root / "loop"
    os.symlink(loop, loop)
    unreachable = loop / "derived.json"

    # the taxonomy class, not a class of the staging module
    with pytest.raises(ParserPersistenceError):
        root.contains(unreachable)

    artifact_id = store.register_staging_artifact(
        job_id=job_id, fencing_token=token, path=str(unreachable)
    )
    # the attempt is stale, so only a fault can keep the artifact
    takeover = store.claim_attempt(
        job_id=job_id, worker_id="worker-b", lease_seconds=60
    )
    assert takeover is not None

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == ()
    assert _reasons(report, {artifact_id}) == {}
    faults = _faults(report, {artifact_id})
    assert faults[artifact_id].reason == "path_unresolvable"
    assert faults[artifact_id].error_code == "persistence_unavailable"
    assert [
        artifact.artifact_id for artifact in store.list_staging_artifacts(job_id=job_id)
    ] == [artifact_id]


def test_a_filesystem_refusal_to_remove_is_a_fault_not_a_policy_skip(
    store: PostgresParserJobStore, job_key: str, tmp_path: Path, staging_rows: list[str]
) -> None:
    """The filesystem refused the removal: the file stays and the pass says so.

    A directory stands where the derived file belongs: the path resolves,
    but the filesystem refuses to unlink it. The pass must report a FAULT
    with its own reason and the parser-owned persistence failure, not the
    domain outcome, and it must keep the artifact and its row.
    """
    job_id, token = _accept(store, idempotency_key=job_key)
    staging_rows.append(job_id)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    root = StagingRoot(staging_dir)
    blocked = root.root / "blocked"
    blocked.mkdir()
    (blocked / "child").write_bytes(b"not empty\n")

    artifact_id = store.register_staging_artifact(
        job_id=job_id, fencing_token=token, path=str(blocked)
    )
    # the attempt is stale, so only a fault can keep the artifact
    takeover = store.claim_attempt(
        job_id=job_id, worker_id="worker-b", lease_seconds=60
    )
    assert takeover is not None

    collector = StagingCollector(store, root)
    report = collector.collect()

    assert report.removed_artifact_ids == ()
    assert blocked.exists()
    assert _reasons(report, {artifact_id}) == {}
    faults = _faults(report, {artifact_id})
    assert faults[artifact_id].reason == "remove_refused"
    assert faults[artifact_id].error_code == ParserPersistenceError.code
    assert [
        artifact.artifact_id for artifact in store.list_staging_artifacts(job_id=job_id)
    ] == [artifact_id]
