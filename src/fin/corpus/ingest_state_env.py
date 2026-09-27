"""Boot-time selection of the ingest job store from the environment.

The kind vocabulary and its one validation live here: the job-store
factory and the storage-door factory both resolve ``FIN_CORPUS_INGEST_STORE``
through :func:`validated_store_kind`, so the switch cannot grow two answers
to "is this a known store?".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from fin.corpus.ingest_state_errors import IngestStateStoreConfigError
from fin.corpus.ingest_state_memory import InMemoryIngestStateStore
from fin.corpus.ingest_state_model import IngestStateStore
from fin.corpus.ingest_state_postgres import (
    INGEST_STATE_DSN_KEYS,
    PostgresIngestStateStore,
)

INGEST_STATE_STORE_KEY: Final[str] = "FIN_CORPUS_INGEST_STORE"
INGEST_STATE_STORE_DURABLE: Final[str] = "postgres"
INGEST_STATE_STORE_MEMORY: Final[str] = "memory"


def validated_store_kind(values: Mapping[str, str]) -> str:
    """The declared store kind, or the typed refusal of an unknown one."""
    kind = (values.get(INGEST_STATE_STORE_KEY) or "").strip().lower()
    if kind not in (
        INGEST_STATE_STORE_DURABLE,
        INGEST_STATE_STORE_MEMORY,
        "",
    ):
        raise IngestStateStoreConfigError(
            f"{INGEST_STATE_STORE_KEY}={kind!r} names no known ingest job "
            f"store; expected {INGEST_STATE_STORE_DURABLE!r} (durable, the "
            f"default) or {INGEST_STATE_STORE_MEMORY!r} (declared "
            "process-local)",
            key=INGEST_STATE_STORE_KEY,
        )  # EXIT: a typo must not silently pick a backend
    return kind


def configured_job_store_dsn(values: Mapping[str, str]) -> str:
    """The first non-blank job-store DSN, or ``""`` when there is none."""
    for key in INGEST_STATE_DSN_KEYS:
        dsn = (values.get(key) or "").strip()
        if dsn:
            return dsn
    return ""  # EXIT: no DSN configured; the caller decides if that is fatal


def ingest_state_store_from_env(
    environ: Mapping[str, str] | None = None,
) -> IngestStateStore:
    """Select the job store the deployment configured.

    Durability is the default: with no explicit statement the process must
    have a DSN, or this raises instead of handing back a store whose jobs
    die with the process. ``FIN_CORPUS_INGEST_STORE=memory`` is the one
    declared exception — a local or scripted boot that says out loud that
    its accepted uploads are process-local.
    """
    values = os.environ if environ is None else environ
    kind = validated_store_kind(values)
    if kind == INGEST_STATE_STORE_MEMORY:
        return InMemoryIngestStateStore()
    dsn = configured_job_store_dsn(values)
    if not dsn:
        raise IngestStateStoreConfigError(
            "the corpus ingest door needs a durable job store: set "
            + " or ".join(INGEST_STATE_DSN_KEYS)
            + " to the Postgres URL the ingest jobs live in, or set "
            f"{INGEST_STATE_STORE_KEY}=memory to accept process-local jobs "
            "on a declared local boot",
            key=INGEST_STATE_DSN_KEYS[0],
        )  # EXIT: accepting an upload into process memory is the gap here
    return PostgresIngestStateStore(dsn)


__all__ = [
    "INGEST_STATE_DSN_KEYS",
    "INGEST_STATE_STORE_DURABLE",
    "INGEST_STATE_STORE_KEY",
    "INGEST_STATE_STORE_MEMORY",
    "configured_job_store_dsn",
    "ingest_state_store_from_env",
    "validated_store_kind",
]
