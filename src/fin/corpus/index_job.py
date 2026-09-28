"""Durable index-job ledger and the tracker that drives the seam.

After a successful push the worker submits (or joins) an iknow-memory
index job per ``(source_ref, content_identity)`` pair — the identity
being the value read back from the push response, never a fin-side
recomputation — and stores the returned ``job_id`` here so a process
restart resumes by re-polling the same job instead of re-pushing
(``specs/corpus-index-job-orchestration.md`` §Cross-repository seam
with fin, item 5).

The tracker's two obligations that the tests pin hardest:

* **no swallowed failure** — a typed submit failure is recorded
  per-pair in the ledger *and* surfaced to the caller as one
  aggregated :class:`IndexJobSubmitError`, so an audit sees both the
  durable trace and the raised error;
* **graph/C readiness is pass-through only** — ``graph_status`` reports
  the polled ``community_index_state`` verbatim beside the per-source
  job rows, never derived from them, and an unavailable poll degrades
  to the last persisted state with ``error`` set (stale-visible),
  never to an empty dict presented as ready. A pair whose submit was
  refused has no job to poll; its recorded ``last_error`` is what the
  status surfaces, so a fully-refused document is never the
  error-less empty shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any, Final

from fin.retrieve._ledger.jsonl import (
    append_jsonl_object,
    replay_jsonl_objects,
)
from fin.retrieve.index_job_port import (
    CommunityIndexState,
    IndexJobError,
    IndexJobPort,
)

EVENT_ENTRIES_RECORDED: Final[str] = "entries_recorded"

INDEX_JOBS_FILE: Final[str] = "index_jobs.jsonl"

_ENTRY_KEYS: Final[tuple[str, ...]] = (
    "source_ref",
    "content_identity",
    "job_id",
    "last_polled_status",
    "last_community_index_state",
    "last_failure_code",
    "last_error",
)


class IndexJobLedgerUnwritableError(Exception):
    code = "index_job_ledger_unwritable"


class IndexJobLedgerUnreadableError(Exception):
    code = "index_job_ledger_unreadable"


class IndexJobLedgerCorruptError(Exception):
    code = "index_job_ledger_corrupt"


class IndexJobSubmitError(Exception):
    """Aggregated failure of one ``submit_after_push`` round.

    :attr:`failures` maps each refused ``source_ref`` to the stable
    code of the typed failure it hit; :attr:`codes` is the same
    information as the plain set of codes for audit consumers. The
    per-pair detail stays durable in the ledger — this error exists so
    a refused round is never a swallowed empty success.
    """

    code = "index_job_submit_failed"

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures: Mapping[str, str] = dict(failures)
        self.codes: tuple[str, ...] = tuple(sorted(set(self.failures.values())))
        detail = ", ".join(
            f"{source_ref}: {code}" for source_ref, code in self.failures.items()
        )
        super().__init__(
            f"index-job submit refused for {len(self.failures)} pair(s): "
            f"{detail}"
        )


def _entry_to_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    state = entry["last_community_index_state"]
    error = entry["last_error"]
    return {
        "source_ref": entry["source_ref"],
        "content_identity": entry["content_identity"],
        "job_id": entry["job_id"],
        "last_polled_status": entry["last_polled_status"],
        "last_community_index_state": (
            {
                "status": state["status"],
                "failure_code": state["failure_code"],
            }
            if state is not None
            else None
        ),
        "last_failure_code": entry["last_failure_code"],
        "last_error": (
            {"code": error["code"], "message": error["message"]}
            if error is not None
            else None
        ),
    }


def _payload_to_entry(payload: object, path: Path, number: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IndexJobLedgerCorruptError(
            f"index jobs {path} line {number} entry is not an object"
        )
    missing = (set(_ENTRY_KEYS) - {"last_failure_code"}) - set(payload)
    if missing:
        raise IndexJobLedgerCorruptError(
            f"index jobs {path} line {number} entry lacks {sorted(missing)}"
        )
    for key in ("last_community_index_state", "last_error"):
        value = payload[key]
        if value is not None and not isinstance(value, dict):
            raise IndexJobLedgerCorruptError(
                f"index jobs {path} line {number} entry {key} is not an "
                "object or null"
            )
    # ``last_failure_code`` joined the shape after the first on-disk
    # snapshots; an older line replays with it as None, not as corrupt.
    return {key: payload.get(key) for key in _ENTRY_KEYS}


class IndexJobLedger:
    """Durable per-document job ledger, one JSONL snapshot event per
    write; replay is last-write-wins per document, so the file survives
    a restart with exactly the state the last write committed.

    A process-wide singleton with two writers — the worker's submit
    thread and the status route's threadpool poll — means each
    record is a read-modify-write (mutate, snapshot, append) that must
    commit as one critical section; otherwise a slow writer appends a
    stale snapshot after a fresher line already landed and
    last-write-wins replay loses the newer entries.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        # document_id -> {source_ref -> entry}
        self._documents: dict[str, dict[str, dict[str, Any]]] = {}
        # Reentrant so future locked internal reads compose with a
        # locked write; held for the mutate-snapshot-append section only.
        self._lock = RLock()
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    def entries(self, document_id: str) -> list[dict[str, Any]]:
        """Copies of one document's per-source entries, first-seen order."""
        with self._lock:
            return [
                dict(entry) for entry in self._documents.get(document_id, {}).values()
            ]

    def record_entry(self, document_id: str, entry: Mapping[str, Any]) -> None:
        """Merge one pair's entry into the document and persist it.

        The appended event is the whole merged list for the document:
        last-write-wins on replay keeps the append atomic at the event
        level (mirrors the pushed-records store), so a torn final line
        can never half-apply an entry.
        """
        if not document_id:
            raise ValueError("document_id is required")
        source_ref = entry.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError("entry source_ref is required")
        payload = _entry_to_payload(
            {key: entry.get(key) for key in _ENTRY_KEYS}
        )
        with self._lock:
            merged = self._documents.setdefault(document_id, {})
            merged[source_ref] = {key: payload[key] for key in _ENTRY_KEYS}
            append_jsonl_object(
                self._path,
                {
                    "event": EVENT_ENTRIES_RECORDED,
                    "document_id": document_id,
                    "entries": list(merged.values()),
                },
                unwritable=IndexJobLedgerUnwritableError,
                message=f"cannot append index jobs at {self._path}",
            )

    def _replay(self, path: Path) -> None:
        replay_jsonl_objects(
            path,
            lambda event, number: self._apply_or_corrupt(event, number, path),
            on_read_oserror=lambda exc: IndexJobLedgerUnreadableError(
                f"cannot read index jobs at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_unicode=lambda exc: IndexJobLedgerCorruptError(
                f"index jobs at {path} is not valid UTF-8: {exc}"
            ),
            on_json=lambda exc, number: IndexJobLedgerCorruptError(
                f"index jobs {path} line {number} is not valid JSON: {exc}"
            ),
        )

    def _apply_or_corrupt(
        self, event: object, number: int, path: Path
    ) -> None:
        try:
            self._apply_replayed_event(event, number, path)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise IndexJobLedgerCorruptError(
                f"index jobs {path} line {number} is not an entries event: "
                f"{exc}"
            ) from exc

    def _apply_replayed_event(
        self, event: object, number: int, path: Path
    ) -> None:
        if not isinstance(event, dict):
            raise TypeError(f"not an object: {type(event).__name__}")
        if event.get("event") != EVENT_ENTRIES_RECORDED:
            raise ValueError(f"unknown event {event.get('event')!r}")
        document_id = str(event["document_id"])
        rows = event["entries"]
        if not isinstance(rows, list):
            raise TypeError("entries is not a list")
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = _payload_to_entry(row, path, number)
            merged[str(entry["source_ref"])] = entry
        # A later snapshot replaces the earlier one for this document
        # wholesale — a removed source stays removed.
        with self._lock:
            self._documents[document_id] = merged


def _state_to_payload(state: CommunityIndexState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"status": state.status, "failure_code": state.failure_code}


class IndexJobTracker:
    """Submits jobs after a push and turns polls into the pass-through
    graph/C status shape.

    The tracker holds no clock and no threads: every poll it performs
    happens inside ``graph_status``, and everything it learns is
    persisted through the ledger before being returned.
    """

    def __init__(self, port: IndexJobPort, ledger: IndexJobLedger) -> None:
        self._port = port
        self._ledger = ledger

    def submit_after_push(
        self, document_id: str, identities: Mapping[str, str]
    ) -> None:
        """Submit-or-join one index job per ``source_ref`` →
        ``content_identity`` pair and persist the returned job ids.

        A duplicate round joins at the port (same pair → same
        ``job_id``) and rewrites the same entry — it never creates a
        second active job. Every typed failure (including an
        unavailable service) is recorded on that pair's entry as
        ``last_error`` and the round then raises one aggregated
        :class:`IndexJobSubmitError`, so a partial submission is
        visible to both the caller and the durable audit trail.
        """
        if not document_id:
            raise ValueError("document_id is required")
        prior = {
            entry["source_ref"]: entry for entry in self._ledger.entries(document_id)
        }
        failures: dict[str, str] = {}
        for source_ref, content_identity in identities.items():
            try:
                submission = self._port.submit_index_job(
                    source_ref, content_identity
                )
            except IndexJobError as exc:
                failures[source_ref] = exc.code
                self._record_refusal(
                    document_id, prior.get(source_ref), source_ref,
                    content_identity, exc,
                )
                continue
            self._record_submission(document_id, submission)
        if failures:
            raise IndexJobSubmitError(failures)

    def _record_submission(
        self, document_id: str, submission: Any
    ) -> None:
        """Persist a settled pair: the service's job id, no error, and
        no polled state yet."""
        self._ledger.record_entry(
            document_id,
            {
                "source_ref": submission.record.source_ref,
                "content_identity": submission.record.content_identity,
                "job_id": submission.record.job_id,
                "last_polled_status": None,
                "last_community_index_state": None,
                "last_failure_code": None,
                "last_error": None,
            },
        )

    def _record_refusal(
        self,
        document_id: str,
        previous: Mapping[str, Any] | None,
        source_ref: str,
        content_identity: str,
        exc: IndexJobError,
    ) -> None:
        """Persist a refused pair, keeping what still describes it.

        The carried-job decision is made once: an entry whose re-submit
        fails keeps the ``job_id`` of the *same* pair it already had —
        that job is still the durable fact — together with its polled
        state; a pair that changed identity loses the old job
        reference, because the old row no longer describes this content
        version.
        """
        carried = (
            previous
            if previous is not None
            and previous["job_id"] is not None
            and previous["content_identity"] == content_identity
            else None
        )
        self._ledger.record_entry(
            document_id,
            {
                "source_ref": source_ref,
                "content_identity": content_identity,
                "job_id": carried["job_id"] if carried is not None else None,
                "last_polled_status": (
                    carried["last_polled_status"] if carried is not None else None
                ),
                "last_community_index_state": (
                    carried["last_community_index_state"]
                    if carried is not None
                    else None
                ),
                "last_failure_code": (
                    carried["last_failure_code"] if carried is not None else None
                ),
                "last_error": {"code": exc.code, "message": str(exc)},
            },
        )

    def graph_status(self, document_id: str) -> dict[str, Any] | None:
        """Poll every stored job (a pure read), persist the fresh
        state, and return the pass-through shape.

        ``None`` means nothing was ever submitted for this document —
        no jobs, no graph/C claim. The returned ``community_index_state``
        is the value confirmed by a poll in this round, or, when no
        poll succeeded, the last value the ledger persisted; it comes
        *only* from poll responses and is never computed from job
        statuses. A poll failure keeps the stale per-job state and
        failure code visible and sets ``error``; a pair with no
        pollable job surfaces its recorded refusal instead of the
        shape ever reading as an error-less empty success.
        """
        entries = self._ledger.entries(document_id)
        if not entries:
            return None
        jobs, error, fresh_state = self._poll_round(document_id, entries)
        if error is None:
            error = _first_refusal_error(entries)
        return {
            "jobs": jobs,
            "community_index_state": _carried_community_state(entries, fresh_state),
            "error": error,
        }

    def _poll_round(
        self, document_id: str, entries: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None, dict[str, Any] | None]:
        """Poll each durable job once, persisting every fresh answer.

        Returns the job rows, the first poll error (stale rows kept
        visible beside it), and the community state confirmed this
        round — pass-through, never derived from the job statuses.
        """
        jobs: list[dict[str, Any]] = []
        error: dict[str, str] | None = None
        fresh_state: dict[str, Any] | None = None
        for entry in entries:
            job_id = entry["job_id"]
            if job_id is None:
                # Nothing durable to poll for this pair; its refusal
                # is already recorded in ``last_error``.
                continue
            try:
                poll = self._port.poll_index_job(job_id)
            except IndexJobError as exc:
                if error is None:
                    error = {"code": exc.code, "message": str(exc)}
                jobs.append(
                    {
                        "job_id": job_id,
                        "source_ref": entry["source_ref"],
                        "status": entry["last_polled_status"],
                        "failure_code": entry["last_failure_code"],
                    }
                )
                continue
            self._ledger.record_entry(
                document_id,
                {
                    **entry,
                    "last_polled_status": poll.record.status,
                    "last_community_index_state": _state_to_payload(
                        poll.community_index_state
                    ),
                    "last_failure_code": poll.record.failure_code,
                    "last_error": None,
                },
            )
            jobs.append(
                {
                    "job_id": job_id,
                    "source_ref": poll.record.source_ref,
                    "status": poll.record.status,
                    "failure_code": poll.record.failure_code,
                }
            )
            fresh_state = _state_to_payload(poll.community_index_state)
        return jobs, error, fresh_state


def _first_refusal_error(
    entries: list[dict[str, Any]]
) -> dict[str, str] | None:
    """The recorded error of the first pair with no pollable job, so a
    refused round is named in the status instead of reading as an
    error-less empty success."""
    for entry in entries:
        if entry["job_id"] is None and entry["last_error"] is not None:
            return dict(entry["last_error"])
    return None


def _carried_community_state(
    entries: list[dict[str, Any]], fresh_state: dict[str, Any] | None
) -> dict[str, Any] | None:
    """This round's confirmed community state, else the last one the
    ledger persisted — never a guess, never an empty ready shape."""
    if fresh_state is not None:
        return fresh_state
    for entry in entries:
        if entry["last_community_index_state"] is not None:
            return entry["last_community_index_state"]
    return None


__all__ = [
    "IndexJobLedger",
    "IndexJobLedgerCorruptError",
    "IndexJobLedgerUnreadableError",
    "IndexJobLedgerUnwritableError",
    "IndexJobSubmitError",
    "IndexJobTracker",
]
