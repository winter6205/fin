"""Ingest state machine and process-level audit trail.

The ingest pipeline follows::

    PENDING -> FETCHING -> EXTRACTING -> OCR -> CATALOGING -> READY
                   |            |          |
                   +------------+----------+----> FAILED

``EXTRACTING`` may transition directly to ``CATALOGING`` when OCR is not
required. A failed job may retry from ``FAILED`` to ``FETCHING``. ``READY``
ends one version's lifecycle — the only way out of it is a settled source
replacement, which :meth:`IngestStateStore.supersede_job` records as a new
``PENDING`` generation of the same document job. Every accepted or rejected
operation records an audit row with a process actor, action, metadata, and
UTC timestamp.

The implementation is split along its seams:

- :mod:`fin.corpus.ingest_state_model` — the shared vocabulary and the one
  guard/audit rule set both stores run.
- :mod:`fin.corpus.ingest_state_memory` — the process-local store.
- :mod:`fin.corpus.ingest_state_postgres` — the durable store.
- :mod:`fin.corpus.ingest_state_env` — boot-time store selection.
- :mod:`fin.corpus.ingest_state_errors` — the typed failures.

This module re-exports the public names so every existing import path stays
stable; import from here (or from the seam you own), not by copy.
"""

from __future__ import annotations

from fin.corpus.ingest_state_env import (
    INGEST_STATE_DSN_KEYS,
    INGEST_STATE_STORE_DURABLE,
    INGEST_STATE_STORE_KEY,
    INGEST_STATE_STORE_MEMORY,
    configured_job_store_dsn,
    ingest_state_store_from_env,
)
from fin.corpus.ingest_state_errors import IngestStateStoreConfigError
from fin.corpus.ingest_state_memory import InMemoryIngestStateStore
from fin.corpus.ingest_state_model import (
    IngestAuditRow,
    IngestJob,
    IngestState,
    IngestStateStore,
    TransitionRequest,
)
from fin.corpus.ingest_state_postgres import PostgresIngestStateStore

default_ingest_state_store = ingest_state_store_from_env
create_ingest_state_store = ingest_state_store_from_env


__all__ = [
    "INGEST_STATE_DSN_KEYS",
    "INGEST_STATE_STORE_DURABLE",
    "INGEST_STATE_STORE_KEY",
    "INGEST_STATE_STORE_MEMORY",
    "InMemoryIngestStateStore",
    "IngestAuditRow",
    "IngestJob",
    "IngestState",
    "IngestStateStore",
    "IngestStateStoreConfigError",
    "PostgresIngestStateStore",
    "TransitionRequest",
    "configured_job_store_dsn",
    "create_ingest_state_store",
    "default_ingest_state_store",
    "ingest_state_store_from_env",
]
