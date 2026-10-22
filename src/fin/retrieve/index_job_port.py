"""Index-job port: the durable submit/poll seam and its named failures.

The port is the fin-side half of the cross-repo contract pinned by
iknow-memory's ``submit_index_job`` / ``poll_index_job`` tools
(``specs/mcp-tool-surface.md`` §Index-job tool contract,
``specs/corpus-index-job-orchestration.md`` §Cross-repository seam with
fin). fin submits (or joins) an index job per ``(source_ref,
content_identity)`` pair after a successful push, then polls the
persisted job state.

Contract highlights fin must honor:

* ``content_identity`` is the value the service reported in its push
  response — fin never recomputes it, so this module only carries it
  as an opaque string;
* submit is an idempotent join per pair: the same pair yields the same
  durable ``job_id``, never a second active job;
* poll is a pure read: it never re-queues or mutates job or community
  state;
* ``community_index_state`` (corpus-wide graph/C readiness) rides in
  every poll answer as a field separate from the per-source job
  status, and fin passes it through verbatim — it is never derived
  from job status and never collapsed with per-source text readiness.
  An explicit ``null`` there means the C singleton has never been
  written and stays an absent claim, not a readiness one.

Failures are typed and carry a stable ``code`` read off the wire's
JSON body — never matched against prose (mirroring the push seam's
style in :mod:`fin.retrieve.push_port`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Protocol, runtime_checkable

# The job-row states the service persists; anything else on the wire is
# off-contract. ``in_progress`` with a null ``started_at`` is queued,
# with a non-null one running.
INDEX_JOB_STATUSES: Final[frozenset[str]] = frozenset(
    {"in_progress", "ready", "failed", "stale"}
)

# The locator budget submit and poll share with ``read_source``: an
# over-ceiling value is a named invalid locator, never echoed back.
LOCATOR_CEILING_CODEPOINTS: Final[int] = 1024


class IndexJobError(Exception):
    """Base class for the index-job seam's named failures.

    Every wire-visible failure has a subclass with a distinct stable
    :attr:`code`; callers branch on ``exc.code`` instead of matching
    message text, and the transport never reports a refusal as an
    empty success.
    """

    code: str = "index_job_error"


class IndexJobInvalidLocatorError(IndexJobError):
    """``source_ref`` / ``content_identity`` / ``job_id`` absent, empty,
    or over the locator ceiling."""

    code = "index_job_invalid_locator"


class IndexJobUnknownSourceError(IndexJobError):
    """The submitted ``source_ref`` has no 资料 row on the service."""

    code = "index_job_unknown_source"


class IndexJobIdentityMismatchError(IndexJobError):
    """The submitted ``content_identity`` is not that source row's
    current one — the pair names a content version the service does
    not hold."""

    code = "index_job_identity_mismatch"


class IndexJobUnknownJobError(IndexJobError):
    """A poll asked for a ``job_id`` the service has no job row for —
    the poll-side twin of a named not-found, raised rather than
    answered with an empty record."""

    code = "index_job_unknown_job"


class IndexJobInProgressError(IndexJobError):
    """A second submit for the same ``source_ref`` with a *different*
    ``content_identity`` while a job for that source is still
    non-terminal.

    :attr:`active_job_id` names the job that holds the source open,
    exactly as the wire body's ``active_job_id`` field carries it; it
    is ``None`` when the answer did not name one.
    """

    code = "index_job_in_progress"

    def __init__(
        self,
        message: str,
        *,
        active_job_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.active_job_id = active_job_id


class IndexJobUnavailableError(IndexJobError):
    """The index-job service could not be reached, or answered in a way
    that cannot be trusted (transport failure, JSON-RPC error, or an
    off-contract body).

    Nothing about the job's state is known in this case, so the caller
    must keep whatever it last persisted — this failure never stands in
    for a settled job outcome.
    """

    code = "index_job_unavailable"


_WIRE_CODES: Final[Mapping[str, type[IndexJobError]]] = {
    IndexJobInvalidLocatorError.code: IndexJobInvalidLocatorError,
    IndexJobUnknownSourceError.code: IndexJobUnknownSourceError,
    IndexJobIdentityMismatchError.code: IndexJobIdentityMismatchError,
    IndexJobUnknownJobError.code: IndexJobUnknownJobError,
    IndexJobInProgressError.code: IndexJobInProgressError,
}

# Every typed failure of this seam, for tests and dispatch tables.
INDEX_JOB_ERROR_TYPES: Final[Mapping[str, type[IndexJobError]]] = {
    **_WIRE_CODES,
    IndexJobUnavailableError.code: IndexJobUnavailableError,
}


def index_job_error_for_code(
    code: str,
    message: str,
    *,
    active_job_id: str | None = None,
) -> IndexJobError:
    """Project a wire error literal onto its typed failure.

    The code is read off the JSON body's ``error`` field by the
    caller; an unrecognized literal degrades to
    :class:`IndexJobUnavailableError` because an answer fin cannot
    name is an answer fin cannot trust — never a swallowed success.
    """
    error_type = _WIRE_CODES.get(code)
    if error_type is None:
        unavailable = IndexJobUnavailableError(
            f"index-job service answered with unknown error {code!r}: "
            f"{message}"
        )
        return unavailable
    if error_type is IndexJobInProgressError:
        return IndexJobInProgressError(
            message, active_job_id=active_job_id
        )
    return error_type(message)


@dataclass(frozen=True)
class CommunityIndexState:
    """Corpus-wide graph/C readiness, passed through from the service.

    fin reports this sub-object verbatim; it never derives it from a
    job's own ``status`` and never merges it with per-source text
    readiness into one boolean.
    """

    status: str
    failure_code: str | None


@dataclass(frozen=True)
class IndexJobRecord:
    """One persisted job row as the service reported it.

    ``started_at`` null with ``in_progress`` means queued; a terminal
    row carries ``finished_at``, and ``failure_code`` only carries a
    value when ``status`` is ``failed``.
    """

    job_id: str
    source_ref: str
    content_identity: str
    status: str
    started_at: str | None
    finished_at: str | None
    failure_code: str | None


@dataclass(frozen=True)
class IndexJobSubmission:
    """The answer to one submit: the job row plus whether this call
    joined an existing job (same pair → same ``job_id``)."""

    record: IndexJobRecord
    reused: bool


@dataclass(frozen=True)
class IndexJobPoll:
    """The answer to one poll: the job row and, in a separate field,
    the corpus-wide community index state — the two readiness facts
    never collapse into one.

    ``community_index_state`` is ``None`` exactly when the service
    answered an explicit ``null``, i.e. the corpus-wide C singleton
    has never been written. That is an absent readiness claim, never a
    ``ready`` one, and it is never derived from the job's own status.
    """

    record: IndexJobRecord
    community_index_state: CommunityIndexState | None


@runtime_checkable
class IndexJobPort(Protocol):
    """Structural protocol for index-job adapters.

    ``submit_index_job`` takes the ``(source_ref, content_identity)``
    pair fin read off the push response (it never recomputes the
    identity) and joins or creates the durable job; ``poll_index_job``
    is a pure read of the persisted state. Implementations raise an
    :class:`IndexJobError` subclass on failure and never report a
    refused call as an empty success.
    """

    def submit_index_job(
        self, source_ref: str, content_identity: str
    ) -> IndexJobSubmission:  # pragma: no cover - protocol
        ...

    def poll_index_job(self, job_id: str) -> IndexJobPoll:  # pragma: no cover - protocol
        ...


__all__ = [
    "INDEX_JOB_ERROR_TYPES",
    "INDEX_JOB_STATUSES",
    "LOCATOR_CEILING_CODEPOINTS",
    "CommunityIndexState",
    "IndexJobError",
    "IndexJobIdentityMismatchError",
    "IndexJobInProgressError",
    "IndexJobInvalidLocatorError",
    "IndexJobPoll",
    "IndexJobPort",
    "IndexJobRecord",
    "IndexJobSubmission",
    "IndexJobUnknownJobError",
    "IndexJobUnknownSourceError",
    "IndexJobUnavailableError",
    "index_job_error_for_code",
]
