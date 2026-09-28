"""In-process contract fake for the index-job seam.

Stands in for iknow-memory's ``submit_index_job`` / ``poll_index_job``
so the default suite can assert the seam without a server process. Like
:class:`~fin.retrieve.fake_push_port.FakePushPort` it is a contract
fake, not a stub: it enforces the service-side half of the contract
itself.

What the fake guarantees:

* **join, not a second job** — the ``job_id`` is derived from the
  ``(source_ref, content_identity)`` pair, so the same pair always
  answers with the same job and ``reused=True``;
* **one active job per source** — a submit for the same ``source_ref``
  with a *different* identity while a job is non-terminal is refused
  with :class:`IndexJobInProgressError` naming the active job;
* **poll is a pure read** — it never advances or mutates any state;
* **deterministic progress** — job and community state move only
  through the explicit test hooks (:meth:`advance_job`,
  :meth:`set_community_index_state`); no threads, no clocks;
* **community state is its own fact** — a poll reports it beside the
  job record, and the fake never derives one from the other;
* **named refusals** — invalid locators are refused by shape, and
  any typed failure (``unknown_source`` / ``identity_mismatch`` /
  ``in_progress`` / ``unavailable`` / ``unknown_job``) can be injected
  for the next call or made sticky, so error paths are testable
  without a hostile server.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from fin.retrieve.index_job_port import (
    INDEX_JOB_ERROR_TYPES,
    INDEX_JOB_STATUSES,
    LOCATOR_CEILING_CODEPOINTS,
    CommunityIndexState,
    IndexJobError,
    IndexJobInvalidLocatorError,
    IndexJobPoll,
    IndexJobSubmission,
    IndexJobRecord,
    index_job_error_for_code,
)

# Fixed, clock-free instants so persisted timestamps are byte-stable
# across runs; the contract cares about null-vs-set, not wall time.
_QUEUED_STARTED_AT: Final[str | None] = None
_RUNNING_STARTED_AT: Final[str] = "2026-01-01T00:00:00.000Z"
_TERMINAL_FINISHED_AT: Final[str] = "2026-01-01T00:01:00.000Z"

_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"ready", "failed", "stale"}
)


def _derived_job_id(source_ref: str, content_identity: str) -> str:
    """The fake's durable id: a stable hash of the pair, so the same
    pair can never name two jobs."""
    digest = hashlib.sha256(
        json.dumps(
            [source_ref, content_identity], ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return f"fake-job-{digest}"


class _FakeJob:
    """Mutable job row; handed out only as frozen record copies."""

    def __init__(self, record: IndexJobRecord) -> None:
        self.record = record


class FakeIndexJobPort:
    """An in-process index-job service satisfying ``IndexJobPort``.

    Tests drive it with :meth:`advance_job` and
    :meth:`set_community_index_state`, and break it with
    :meth:`inject_submit_failure` / :meth:`inject_poll_failure`.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _FakeJob] = {}
        self._job_id_by_pair: dict[tuple[str, str], str] = {}
        self._community = CommunityIndexState("in_progress", None)
        self._submit_failures: list[str] = []
        self._poll_failures: list[str] = []
        self._sticky_submit_failure: str | None = None
        self._sticky_poll_failure: str | None = None
        self.submit_calls = 0
        self.poll_calls = 0

    # ----- test hooks -----

    def inject_submit_failure(
        self, code: str, *, sticky: bool = False
    ) -> None:
        """Make the next (or every following) submit raise the typed
        failure named by ``code``."""
        self._check_injectable(code)
        if sticky:
            self._sticky_submit_failure = code
        else:
            self._submit_failures.append(code)

    def inject_poll_failure(self, code: str, *, sticky: bool = False) -> None:
        """Make the next (or every following) poll raise the typed
        failure named by ``code``."""
        self._check_injectable(code)
        if sticky:
            self._sticky_poll_failure = code
        else:
            self._poll_failures.append(code)

    def clear_injected_failures(self) -> None:
        self._submit_failures.clear()
        self._poll_failures.clear()
        self._sticky_submit_failure = None
        self._sticky_poll_failure = None

    @staticmethod
    def _check_injectable(code: str) -> None:
        if code not in INDEX_JOB_ERROR_TYPES:
            raise ValueError(
                f"{code!r} is not a typed index-job failure code; "
                f"known codes: {sorted(INDEX_JOB_ERROR_TYPES)}"
            )

    def advance_job(
        self,
        job_id: str,
        status: str,
        *,
        failure_code: str | None = None,
    ) -> None:
        """Move one job to a new persisted state (the only way job
        state progresses in the fake)."""
        if status not in INDEX_JOB_STATUSES:
            raise ValueError(f"unknown job status {status!r}")
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no fake job {job_id!r}")
        record = job.record
        started_at = record.started_at
        finished_at = record.finished_at
        if status != "in_progress":
            started_at = started_at or _RUNNING_STARTED_AT
        if status == "in_progress":
            started_at = _QUEUED_STARTED_AT
        if status in _TERMINAL_STATUSES:
            finished_at = _TERMINAL_FINISHED_AT
        job.record = IndexJobRecord(
            job_id=record.job_id,
            source_ref=record.source_ref,
            content_identity=record.content_identity,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            failure_code=failure_code if status == "failed" else None,
        )

    def set_community_index_state(
        self, status: str, failure_code: str | None = None
    ) -> None:
        """Preprogram the corpus-wide graph/C readiness a later poll
        will report."""
        self._community = CommunityIndexState(
            status=status, failure_code=failure_code
        )

    # ----- IndexJobPort surface -----

    def submit_index_job(
        self, source_ref: str, content_identity: str
    ) -> IndexJobSubmission:
        self.submit_calls += 1
        self._consume_failure(submit=True)
        _validate_locator(source_ref, "source_ref")
        _validate_locator(content_identity, "content_identity")
        key = (source_ref, content_identity)
        existing = self._job_id_by_pair.get(key)
        if existing is not None:
            # Same pair: join the durable job, never create a second.
            return IndexJobSubmission(
                record=self._jobs[existing].record, reused=True
            )
        active = self._active_job_for_source(source_ref)
        if active is not None:
            raise _error_for(
                "index_job_in_progress",
                f"a job for {source_ref!r} is already open with a "
                "different content identity",
                active_job_id=active,
            )
        job_id = _derived_job_id(source_ref, content_identity)
        record = IndexJobRecord(
            job_id=job_id,
            source_ref=source_ref,
            content_identity=content_identity,
            status="in_progress",
            started_at=_QUEUED_STARTED_AT,
            finished_at=None,
            failure_code=None,
        )
        self._jobs[job_id] = _FakeJob(record)
        self._job_id_by_pair[key] = job_id
        return IndexJobSubmission(record=record, reused=False)

    def poll_index_job(self, job_id: str) -> IndexJobPoll:
        self.poll_calls += 1
        self._consume_failure(submit=False)
        _validate_locator(job_id, "job_id")
        job = self._jobs.get(job_id)
        if job is None:
            raise _error_for(
                "index_job_unknown_job",
                f"no index job with id {job_id!r}",
            )
        # Pure read: no state advances here.
        return IndexJobPoll(record=job.record, community_index_state=self._community)

    # ----- helpers -----

    def _consume_failure(self, *, submit: bool) -> None:
        queue = self._submit_failures if submit else self._poll_failures
        sticky = (
            self._sticky_submit_failure if submit else self._sticky_poll_failure
        )
        code = sticky or (queue.pop(0) if queue else None)
        if code is not None:
            raise _error_for(code, f"injected failure {code!r}")

    def _active_job_for_source(self, source_ref: str) -> str | None:
        """The job id of a non-terminal job open on ``source_ref``."""
        for job in self._jobs.values():
            record = job.record
            if (
                record.source_ref == source_ref
                and record.status not in _TERMINAL_STATUSES
            ):
                return record.job_id
        return None


def _validate_locator(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > LOCATOR_CEILING_CODEPOINTS
    ):
        raise IndexJobInvalidLocatorError(
            f"{name} is absent, empty, or over the locator ceiling"
        )


def _error_for(code: str, message: str, **kwargs: object) -> IndexJobError:
    return index_job_error_for_code(
        code, message, active_job_id=kwargs.get("active_job_id")  # type: ignore[arg-type]
    )


__all__ = ["FakeIndexJobPort"]
