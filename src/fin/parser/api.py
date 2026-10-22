"""The parser service HTTP surface.

(contract section 7 -- docs/decisions/p2-parser-service-contract.md §7)

The surface is one FastAPI router mounted under ``/v1``. It owns the
request contract of ``POST /v1/jobs`` and the durable reads of a job and
its result; everything it answers comes from the parser-owned
PostgreSQL authority, never from Redis and never from another ``fin.*``
package.

Two rules shape the answers:

* A rejection is typed. An empty, missing or malformed identity or input
  is refused BEFORE any write, so a refused request can never leave an
  accepted job behind.
* The body of a failure carries the stable ``{"error": "<code>"}`` token
  only. No internal detail, no filesystem path, no digest beyond the
  caller's own input.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from fin.parser.errors import (
    ParserEnvelopeError,
    ParserError,
    ParserExecutionTimeoutError,
    ParserInvalidRequestError,
    ParserJobCancelledError,
    ParserJobCapacityError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobTombstonedError,
    ParserNotificationError,
    ParserPersistenceError,
    ParserQueueFullError,
    ParserQueueWaitTimeoutError,
    ParserStaleFenceError,
)
from fin.parser.job_store import DSN_ENV_VAR, PostgresParserJobStore
from fin.parser.ports import ParserJobStore

# The request bounds. They are the caller's own input, so the limits
# exist to refuse an unbounded body, not to second-guess the caller.
MAX_IDEMPOTENCY_KEY_LENGTH: Final[int] = 512
MAX_ARTIFACT_REF_LENGTH: Final[int] = 1024
MAX_DECLARED_FORMAT_LENGTH: Final[int] = 64
SHA256_LENGTH: Final[int] = 64

# The failure table. One row per typed error family, walked in order, so
# a route answers a parser failure without a branch per family.
_ERROR_TABLE: Final[tuple[tuple[type[ParserError], int], ...]] = (
    (ParserInvalidRequestError, 400),
    (ParserEnvelopeError, 422),
    (ParserJobNotFoundError, 404),
    (ParserJobConflictError, 409),
    (ParserJobCancelledError, 409),
    (ParserJobTombstonedError, 409),
    (ParserQueueWaitTimeoutError, 409),
    (ParserExecutionTimeoutError, 409),
    (ParserStaleFenceError, 409),
    (ParserJobCapacityError, 429),
    (ParserQueueFullError, 429),
    (ParserPersistenceError, 503),
    (ParserNotificationError, 503),
)


# ----- request and response shapes -----


class JobInput(BaseModel):
    """The input half of a job submission."""

    sha256: str = Field(description="The SHA-256 digest of the source bytes")
    artifact_ref: str | None = Field(default=None)
    declared_format: str | None = Field(default=None)


class JobSubmissionRequest(BaseModel):
    """The body of ``POST /v1/jobs``."""

    idempotency_key: str = Field(description="The caller's idempotency key")
    input: JobInput


class JobAcceptedResponse(BaseModel):
    """The answer of a queued (or replayed) job submission."""

    job_id: str
    status: str
    replayed: bool


class JobStatusResponse(BaseModel):
    """The durable status of one job."""

    job_id: str
    idempotency_key: str
    input_sha256: str
    status: str
    created_at: datetime
    updated_at: datetime


class JobResultResponse(BaseModel):
    """The durable result of one job."""

    job_id: str
    fencing_token: int
    envelope: dict[str, Any]
    result_ref: str
    created_at: datetime


# ----- the store dependency -----


_STORE_CACHE: dict[str, PostgresParserJobStore] = {}
# The lock: FastAPI runs the dependency in its threadpool, so two requests
# can reach the cache at the same time. The lock keeps one store per DSN
# instead of two stores writing the same schema.
_STORE_CACHE_LOCK = threading.Lock()


def get_job_store() -> ParserJobStore:
    """The process-wide parser store, built from the environment DSN.

    The store is built on first use, so importing this module opens no
    connection and a misconfigured deployment fails on the request that
    needs the store rather than at import time.
    """
    dsn = os.environ.get(DSN_ENV_VAR, "").strip()
    if not dsn:
        raise ParserPersistenceError(
            f"the parser store is not configured: set {DSN_ENV_VAR}"
        )
    with _STORE_CACHE_LOCK:
        cached = _STORE_CACHE.get(dsn)
        if cached is None:
            cached = PostgresParserJobStore(dsn)
            _STORE_CACHE[dsn] = cached
        return cached


# ----- the router -----


class _ParserRoute(APIRoute):
    """A route that answers a typed parser failure with its stable code.

    The mapping travels with the router, so a host app that mounts the
    router gets the parser's failure contract without extra wiring.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except ParserError as exc:
                return _error_response(exc)
            except RequestValidationError:
                # EXIT: a body that does not match the request shape answers
                # 400 invalid_request — the shape is the caller's, so it is
                # not a server fault.
                return _error_response(
                    ParserInvalidRequestError("the request body is not valid")
                )

        return handler


router = APIRouter(prefix="/v1", tags=["parser"], route_class=_ParserRoute)


@router.post("/jobs", response_model=JobAcceptedResponse, status_code=202)
def create_job(
    payload: JobSubmissionRequest,
    response: Response,
    store: Annotated[ParserJobStore, Depends(get_job_store)],
) -> JobAcceptedResponse:
    """Accept one parse job durably.

    202 answers a new acceptance, 200 answers a replay of the same key
    with the same input. The answer is written only after the job row is
    committed, so a client that holds a 202 knows the job is durable.
    """
    key = _require_identity(payload.idempotency_key)
    digest = _require_digest(payload.input.sha256)
    artifact_ref = _optional_text(
        payload.input.artifact_ref, "artifact_ref", MAX_ARTIFACT_REF_LENGTH
    )
    declared_format = _optional_text(
        payload.input.declared_format, "declared_format", MAX_DECLARED_FORMAT_LENGTH
    )
    source_ref = {"artifact_ref": artifact_ref} if artifact_ref is not None else None

    submission = store.submit_job(
        idempotency_key=key,
        input_sha256=digest,
        source_ref=source_ref,
        declared_format=declared_format,
    )

    response.status_code = 200 if submission.replayed else 202
    return JobAcceptedResponse(
        job_id=submission.job_id, status=submission.status, replayed=submission.replayed
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def read_job(
    job_id: str, store: Annotated[ParserJobStore, Depends(get_job_store)]
) -> JobStatusResponse:
    """The durable status of one job."""
    record = store.get_job(_require_job_id(job_id))
    if record is None:
        raise ParserJobNotFoundError("read job: no job carries that id")
    return JobStatusResponse(
        job_id=record.job_id,
        idempotency_key=record.idempotency_key,
        input_sha256=record.input_sha256,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/jobs/{job_id}/result", response_model=JobResultResponse)
def read_result(
    job_id: str, store: Annotated[ParserJobStore, Depends(get_job_store)]
) -> JobResultResponse:
    """The durable result of one job."""
    record = store.read_result(_require_job_id(job_id))
    if record is None:
        raise ParserJobNotFoundError("read result: the job carries no result")
    return JobResultResponse(
        job_id=record.job_id,
        fencing_token=record.fencing_token,
        envelope=dict(record.envelope),
        result_ref=record.result_ref,
        created_at=record.created_at,
    )


@router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
def cancel_job(
    job_id: str, store: Annotated[ParserJobStore, Depends(get_job_store)]
) -> JobStatusResponse:
    """Cancel a job durably."""
    record = store.cancel_job(_require_job_id(job_id))
    return JobStatusResponse(
        job_id=record.job_id,
        idempotency_key=record.idempotency_key,
        input_sha256=record.input_sha256,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(
    job_id: str, store: Annotated[ParserJobStore, Depends(get_job_store)]
) -> Response:
    """Delete a job and leave a replay-protection tombstone.

    The delete is idempotent: a job that was already deleted answers 204
    as well, and a job id that never carried a job answers 404.
    """
    store.delete_job(_require_job_id(job_id))
    return Response(status_code=204)


# ----- helpers -----


def _error_response(exc: ParserError) -> JSONResponse:
    """Answer a typed parser failure with its stable code only."""
    for family, status in _ERROR_TABLE:
        if isinstance(exc, family):
            return JSONResponse(status_code=status, content={"error": exc.code})
    # EXIT: a family the table does not map answers 500 with its own code —
    # a typed failure never leaves the surface without a status.
    return JSONResponse(status_code=500, content={"error": exc.code})


def _require_identity(value: str) -> str:
    """The caller's idempotency key, or a typed rejection."""
    if not isinstance(value, str):
        raise ParserInvalidRequestError("idempotency_key must be a string")
    key = value.strip()
    if not key:
        raise ParserInvalidRequestError("idempotency_key must not be empty")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ParserInvalidRequestError("idempotency_key is too long")
    if any(_is_control(character) for character in key):
        raise ParserInvalidRequestError("idempotency_key holds a control character")
    return key


def _is_control(character: str) -> bool:
    """A C0 control character or DEL."""
    code = ord(character)
    return code < 0x20 or code == 0x7F


def _require_digest(value: str) -> str:
    """The caller's input digest, or a typed rejection.

    The contract asks for exactly 64 lower-case hex characters, so the
    digest is taken as the caller sent it and never folded to another
    spelling.
    """
    if not isinstance(value, str):
        raise ParserInvalidRequestError("input.sha256 must be a string")
    digest = value.strip()
    if len(digest) != SHA256_LENGTH:
        raise ParserInvalidRequestError("input.sha256 must be a SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ParserInvalidRequestError("input.sha256 must be a SHA-256 digest")
    return digest


def _optional_text(value: str | None, field: str, limit: int) -> str | None:
    """An optional caller-supplied text, or a typed rejection."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParserInvalidRequestError(f"input.{field} must be a string")
    text = value.strip()
    if not text:
        raise ParserInvalidRequestError(f"input.{field} must not be empty")
    if len(text) > limit:
        raise ParserInvalidRequestError(f"input.{field} is too long")
    return text


def _require_job_id(job_id: str) -> str:
    """The job id as a UUID, or a typed rejection.

    A malformed id is a caller mistake, not a store outage, so it is
    refused here instead of reaching the driver as a bad cast.
    """
    try:
        return str(uuid.UUID(job_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ParserInvalidRequestError("the job id must be a UUID") from exc


__all__ = [
    "JobAcceptedResponse",
    "JobInput",
    "JobResultResponse",
    "JobStatusResponse",
    "JobSubmissionRequest",
    "get_job_store",
    "router",
]
