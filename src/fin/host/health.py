"""``GET /health`` — process health for the workspace (#135, SC9).

Health answers one question: can this process reach the durable
dependency it was configured with? ``FIN_CHECKPOINT_URL`` is that
dependency — the Postgres behind LangGraph's ``PostgresSaver`` (ADR
0015) and behind ``PostgresThreadStore``. When it does not answer,
runs are not durable and the workspace turn table is not persisted,
which is exactly what an operator needs a health route to say.

Why not ``GET /api/status``: ADR 0033 fixes that surface as
**boot-scoped configuration plus retrieve reachability**, and
explicitly keeps a health-check RPC out of it. Reachability of a
database changes while the process runs, so it is a health concern,
not a boot concern; folding it into ``/api/status`` would also make
the Sidebar strip pay for a connection attempt on every poll.

Three typed states, two of them degraded:

* ``reachable``    — the URL is set and Postgres answered.
* ``unconfigured`` — the URL is unset. The process still serves; run
  state lives in memory and dies with it.
* ``unreachable``  — the URL is set and the connection attempt
  failed.

Both degraded states are 200 payload data with hand-written operator
copy. A blank 500 would tell the operator nothing and would make a
monitoring probe unable to distinguish "fin is down" from "fin is up
without a database". The copy is keyed by ``reason`` and never
includes driver text or any fragment of the URL — that string
carries the operator's Postgres password.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Final, Literal, Mapping

import psycopg
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel


logger = logging.getLogger(__name__)


# ----- typed vocabulary -----

CHECKPOINT_REACHABLE: Final[str] = "reachable"
CHECKPOINT_UNREACHABLE: Final[str] = "unreachable"
CHECKPOINT_UNCONFIGURED: Final[str] = "unconfigured"

REASON_CHECKPOINT_URL_UNSET: Final[str] = "checkpoint_url_unset"
REASON_CHECKPOINT_UNREACHABLE: Final[str] = "checkpoint_unreachable"

HEALTH_OK: Final[str] = "ok"
HEALTH_DEGRADED: Final[str] = "degraded"

# Seconds libpq waits before giving up on the probe connection. Short
# on purpose: ``/health`` is polled, and a hung probe would turn a
# degraded dependency into a hung route.
CONNECT_TIMEOUT_SECONDS: Final[int] = 2

# Operator copy keyed by ``reason``. Hand-written, so no provider
# error text and no piece of ``FIN_CHECKPOINT_URL`` can reach the
# wire (the URL embeds the password).
_CHECKPOINT_DETAIL: Final[dict[str, str]] = {
    "": "Postgres answered the health probe on FIN_CHECKPOINT_URL.",
    REASON_CHECKPOINT_URL_UNSET: (
        "FIN_CHECKPOINT_URL is not set: this process keeps run state in "
        "memory and loses it on exit. Set the URL to run durably."
    ),
    REASON_CHECKPOINT_UNREACHABLE: (
        "FIN_CHECKPOINT_URL is set but Postgres did not answer: runs are "
        "not durable and the workspace turn table is not persisted. Check "
        "that the database is up and reachable from this host."
    ),
}


@dataclass(frozen=True)
class CheckpointProbe:
    """One probe result: the typed state plus its reason code.

    ``reason`` is ``""`` on the reachable path — the closed set is
    ``checkpoint_url_unset`` / ``checkpoint_unreachable``, and a
    healthy dependency has no reason to report.
    """

    state: str
    reason: str

    @property
    def detail(self) -> str:
        return _CHECKPOINT_DETAIL[self.reason]


# ----- response schema -----


class CheckpointHealth(BaseModel):
    state: Literal["reachable", "unreachable", "unconfigured"]
    reason: Literal["", "checkpoint_url_unset", "checkpoint_unreachable"]
    detail: str


class HealthResponse(BaseModel):
    """The whole ``GET /health`` body.

    Deliberately absent: everything ``GET /api/status`` already owns
    (retrieve reachability, adapter kind, model slug, corpus count —
    ADR 0033) and every run-scoped counter. Health is dependency
    reachability, not a second status page.
    """

    status: Literal["ok", "degraded"]
    checkpoint: CheckpointHealth


# ----- probe -----


def _connect_and_ping(url: str) -> None:
    """Open a short-lived connection and issue the cheapest query.

    A successful TCP connect is not enough: libpq would report a
    reachable socket for a server that then refuses authentication.
    ``SELECT 1`` proves the process can actually use the database it
    was configured with.
    """
    with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        conn.execute("SELECT 1")


def probe_checkpoint(
    *,
    environ: Mapping[str, str] | None = None,
    connect: Callable[[str], Any] | None = None,
) -> CheckpointProbe:
    """Resolve ``FIN_CHECKPOINT_URL`` and probe it.

    A blank / whitespace-only value counts as unset — the same
    reading ``default_thread_store`` and ``compile_for_runtime``
    apply, so the three surfaces cannot disagree about whether this
    process is durable.

    Failures are caught and turned into the ``unreachable`` state,
    with a log line carrying the traceback: the route must not 500,
    and the operator still needs the driver's message somewhere. The
    broad second branch exists because the failure modes here are not
    all ``psycopg.Error`` (DNS, TLS, a driver bug); a health route
    that raises on an unexpected exception is the failure mode this
    function is meant to remove.
    """
    import os

    source: Mapping[str, str] = os.environ if environ is None else environ
    url = (source.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        return CheckpointProbe(
            state=CHECKPOINT_UNCONFIGURED, reason=REASON_CHECKPOINT_URL_UNSET
        )

    connector = connect or _connect_and_ping
    try:
        connector(url)
    except psycopg.Error:
        logger.warning(
            "health: FIN_CHECKPOINT_URL is set but Postgres did not answer",
            exc_info=True,
        )
        return CheckpointProbe(
            state=CHECKPOINT_UNREACHABLE, reason=REASON_CHECKPOINT_UNREACHABLE
        )
    except Exception:
        logger.exception(
            "health: the checkpoint probe failed with a non-psycopg error"
        )
        return CheckpointProbe(
            state=CHECKPOINT_UNREACHABLE, reason=REASON_CHECKPOINT_UNREACHABLE
        )
    return CheckpointProbe(state=CHECKPOINT_REACHABLE, reason="")


def health_report(
    *,
    environ: Mapping[str, str] | None = None,
    connect: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Build the ``GET /health`` body. ``ok`` only when Postgres answered."""
    probe = probe_checkpoint(environ=environ, connect=connect)
    return {
        "status": (
            HEALTH_OK if probe.state == CHECKPOINT_REACHABLE else HEALTH_DEGRADED
        ),
        "checkpoint": {
            "state": probe.state,
            "reason": probe.reason,
            "detail": probe.detail,
        },
    }


# ----- route -----


def build_router() -> APIRouter:
    """Build the health router.

    No ``prefix``: ``/health`` is a process-level route, not part of
    the workspace ``/api`` surface, and monitoring probes expect it at
    the root. No ``Depends`` either — the probe reads the environment
    and dials the configured URL; it needs neither the compiled graph
    nor the thread store.
    """
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    def health() -> dict[str, Any]:
        return health_report()

    return router


def mount(app: FastAPI) -> None:
    """Mount ``GET /health`` on an existing FastAPI app.

    Call this BEFORE ``mount_spa``: the SPA catch-all matches every
    remaining path, and Starlette resolves routes in registration
    order, so a later registration would never be reached on a boot
    that serves ``web/dist``.
    """
    app.include_router(build_router())


__all__ = [
    "CHECKPOINT_REACHABLE",
    "CHECKPOINT_UNCONFIGURED",
    "CHECKPOINT_UNREACHABLE",
    "CheckpointHealth",
    "CheckpointProbe",
    "HEALTH_DEGRADED",
    "HEALTH_OK",
    "HealthResponse",
    "REASON_CHECKPOINT_UNREACHABLE",
    "REASON_CHECKPOINT_URL_UNSET",
    "build_router",
    "health_report",
    "mount",
    "probe_checkpoint",
]
