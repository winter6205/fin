"""The corpus ingest door's accept-time storage, chosen once and fail-closed.

``POST /api/corpus/ingest`` answers ``accepted`` only for a raw object, an
object pointer and a job row that outlive the process that accepted them.
Which storage answers that promise is therefore a boot decision, not a
request-time one, and it lives here so there is exactly one place that can
answer it. A boot that cannot promise durability is refused, never degraded,
and the refusal has two shapes because it is raised at two layers: an
unrecognized ``FIN_CORPUS_INGEST_STORE`` value is refused by the job store's
own :class:`~fin.corpus.ingest_state_errors.IngestStateStoreConfigError`,
which names that single key and is raised before any layer is resolved, while
every other gap — a missing or half-configured object store, pointer store or
job store — is aggregated into one :class:`IngestStorageConfigError` naming
every key the deployment left out. Neither is caught by the boot path
(``fin.host.http.serve`` calls :func:`preflight_ingest_storage` and lets it
raise), so a mis-deployment stops before it can serve an upload.

The success outcomes are the durable triple, or the process-local triple of a
boot that *declares* itself local. What this module does not choose is the
invalidation port a replacement accept uses to close the superseded version's
stale blocks
(:meth:`fin.corpus.ingest_endpoint.IngestEndpoint._invalidate_replaced`): a
deployment can pass this preflight and still be refused at accept time when it
has stale blocks to close and no invalidation port wired.

Silent degradation to process memory is the failure mode this module exists
to rule out — an upload that answers ``accepted`` and is gone after a restart
is worse than an upload that is refused.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeVar

from fin.corpus.ingest_state_env import (
    INGEST_STATE_DSN_KEYS,
    INGEST_STATE_STORE_KEY,
    INGEST_STATE_STORE_MEMORY,
    configured_job_store_dsn,
    ingest_state_store_from_env,
    validated_store_kind,
)
from fin.corpus.ingest_state_errors import (
    IngestStateError,
    IngestStateStoreConfigError,
)
from fin.corpus.ingest_state_memory import InMemoryIngestStateStore
from fin.corpus.ingest_state_model import IngestStateStore
from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataStore,
    PostgresMetadataStore,
)
from fin.corpus.object_store import (
    InMemoryObjectStore,
    ObjectStore,
    create_object_store,
)
from fin.corpus.object_store_errors import ObjectStoreNotConfiguredError

logger = logging.getLogger(__name__)

OBJECT_STORE_URL_KEYS: Final[tuple[str, ...]] = (
    "FIN_OBJECT_STORE_URL",
    "FIN_OBJECT_STORE_ENDPOINT",
)
OBJECT_STORE_BUCKET_KEY: Final[str] = "FIN_OBJECT_STORE_BUCKET"
_OBJECT_STORE_KEYS: Final[tuple[str, ...]] = (
    *OBJECT_STORE_URL_KEYS,
    OBJECT_STORE_BUCKET_KEY,
)


class IngestStorageConfigError(IngestStateError):
    """The deployment cannot promise a durable accept.

    A mis-deployment is not a client error, so it carries no wire code. It
    lists every key that has to be set in one message: an operator fixing a
    container should learn about all three gaps from the first refused boot
    instead of paying one restart per layer.
    """

    def __init__(self, message: str, *, missing: Sequence[str] = ()) -> None:
        self.missing = tuple(dict.fromkeys(missing))
        super().__init__(message)


@dataclass(frozen=True)
class IngestStoragePorts:
    """The three accept-time ports, plus how durable they actually are.

    ``durable`` is stated rather than inferred so a caller (and a test) can
    tell a process-local boot apart from a real deployment without
    introspecting the backends.
    """

    objects: ObjectStore
    pointers: MetadataStore
    states: IngestStateStore
    durable: bool


def _durable_object_store(environ: Mapping[str, str]) -> ObjectStore | None:
    """The configured raw-byte store, or ``None`` when nothing is configured.

    Absent keys are a gap the caller aggregates; configured-but-dead storage
    is an outage that propagates, because a boot must not serve uploads it
    cannot store.
    """
    if not any((environ.get(key) or "").strip() for key in OBJECT_STORE_URL_KEYS):
        return None
    try:
        return create_object_store(environ=environ)
    except ObjectStoreNotConfiguredError as exc:
        raise IngestStorageConfigError(
            f"the raw object store is half-configured: {exc}",
            missing=(OBJECT_STORE_BUCKET_KEY,),
        ) from exc  # EXIT: an endpoint without a bucket stores nothing


def _durable_pointer_store(environ: Mapping[str, str]) -> MetadataStore | None:
    """The Postgres object-pointer rows, or ``None`` without a DSN."""
    dsn = configured_job_store_dsn(environ)
    return None if not dsn else PostgresMetadataStore(dsn)


def _durable_state_store(environ: Mapping[str, str]) -> IngestStateStore:
    """The durable job store, normalized onto this module's error type.

    The job-store factory is the one that knows the DSN resolution order and
    the declared-``memory`` exception; its config error is kept as the
    aggregate's cause because it is the error that names the switch.
    """
    try:
        return ingest_state_store_from_env(environ)
    except IngestStateStoreConfigError as exc:
        raise IngestStorageConfigError(
            str(exc),
            missing=(*INGEST_STATE_DSN_KEYS, INGEST_STATE_STORE_KEY),
        ) from exc


def _blank_keys(values: Mapping[str, str], keys: Sequence[str]) -> tuple[str, ...]:
    return tuple(key for key in keys if not (values.get(key) or "").strip())


_PortT = TypeVar("_PortT")


@dataclass
class _GapLedger:
    """Accumulates the per-layer config gaps of one durable-boot attempt.

    A mis-deployment should cost the operator one restart, so every layer
    that cannot answer durability is resolved before the refusal: the labels
    name what is missing, :attr:`missing` unions the keys to set, and
    :attr:`cause` keeps the first typed error as the raised error's cause.
    """

    values: Mapping[str, str]
    labels: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    cause: BaseException | None = None

    def resolve(
        self,
        builder: Callable[[Mapping[str, str]], _PortT | None],
        *,
        label: str,
        keys: Sequence[str],
    ) -> _PortT | None:
        try:
            built = builder(self.values)
        except IngestStorageConfigError as exc:
            built = None
            self.cause = self.cause or exc.__cause__ or exc
            self.missing.extend(exc.missing or keys)
        if built is None:
            self.labels.append(label)
            self.missing.extend(keys)
        return built

    def refusal(self) -> IngestStorageConfigError:
        """The one error naming every gap, so a fix takes one restart."""
        return IngestStorageConfigError(
            "the corpus ingest door cannot promise a durable accept: no "
            + ", ".join(self.labels)
            + " — set "
            + ", ".join(dict.fromkeys(self.missing))
            + f", or boot with {INGEST_STATE_STORE_KEY}="
            + INGEST_STATE_STORE_MEMORY
            + " to declare process-local uploads",
            missing=self.missing,
        )


def _local_ports() -> IngestStoragePorts:
    """The declared process-local triple (a boot that says so out loud)."""
    return IngestStoragePorts(
        objects=InMemoryObjectStore(),
        pointers=InMemoryMetadataStore(),
        states=InMemoryIngestStateStore(),
        durable=False,
    )


def _durable_ports(values: Mapping[str, str]) -> IngestStoragePorts:
    """Resolve the durable triple, or refuse once with every gap named."""
    ledger = _GapLedger(values)
    objects = ledger.resolve(
        _durable_object_store,
        label="raw object store",
        keys=_blank_keys(values, _OBJECT_STORE_KEYS) or _OBJECT_STORE_KEYS,
    )
    pointers = ledger.resolve(
        _durable_pointer_store,
        label="object pointer store",
        keys=INGEST_STATE_DSN_KEYS,
    )
    states = ledger.resolve(
        _durable_state_store,
        label="durable ingest job store",
        keys=(*INGEST_STATE_DSN_KEYS, INGEST_STATE_STORE_KEY),
    )
    if ledger.labels:
        # EXIT: an undurable accept is refused, never degraded to memory
        raise ledger.refusal() from ledger.cause
    assert objects is not None and pointers is not None and states is not None
    return IngestStoragePorts(
        objects=objects,
        pointers=pointers,
        states=states,
        durable=True,
    )  # EXIT: the durable triple — the only success shape for a real boot


def ingest_storage_from_env(
    environ: Mapping[str, str] | None = None,
) -> IngestStoragePorts:
    """Choose the ingest door's storage, refusing an undurable deployment.

    Durability is the default: with no explicit statement every layer has to
    be configured, or this raises instead of handing back stores whose
    contents die with the process. An explicit statement that names no known
    storage raises before this function looks at any layer, and raises the job
    store's own error rather than the aggregate — that key is the only thing
    wrong with such a boot, and naming it alone is the shorter fix.
    """
    values = os.environ if environ is None else environ
    if validated_store_kind(values) == INGEST_STATE_STORE_MEMORY:
        return _local_ports()
    return _durable_ports(values)


_preflight_snapshot: IngestStoragePorts | None = None
_preflight_lock = threading.Lock()


def preflight_ingest_storage(
    environ: Mapping[str, str] | None = None,
) -> IngestStoragePorts:
    """Resolve the door's storage once per process, before anything is served.

    A boot obligation, not a request-time decision: the promise behind
    ``accepted`` is that the upload outlives this process, so an undurable
    deployment has to be refused at startup rather than degraded per upload.
    The snapshot is cached because building it opens the durable connections
    the ingest service then uses; a second resolution would hold a second set.
    """
    global _preflight_snapshot
    with _preflight_lock:
        if _preflight_snapshot is None:
            ports = ingest_storage_from_env(environ)
            if not ports.durable:
                logger.warning(
                    "%s=%s: corpus uploads are process-local and are gone "
                    "after a restart",
                    INGEST_STATE_STORE_KEY,
                    INGEST_STATE_STORE_MEMORY,
                )
            _preflight_snapshot = ports
        return _preflight_snapshot


def reset_ingest_storage_preflight() -> None:
    """Drop the process snapshot (test / teardown reset of the boot decision).

    Boundary: this is the only supported way to watch a process decide twice,
    and it exists for tests. It is not a reconfiguration API — the ports it
    discards may already be held by the ingest service, so calling it while
    uploads are being served would hand the next accept a second set of
    durable connections and a different answer than the boot that was audited.
    A production boot needs it for no case: a deployment that cannot decide
    refuses, and a refusal caches nothing.
    """
    global _preflight_snapshot
    with _preflight_lock:
        _preflight_snapshot = None


__all__ = [
    "INGEST_STATE_DSN_KEYS",
    "OBJECT_STORE_BUCKET_KEY",
    "OBJECT_STORE_URL_KEYS",
    "IngestStorageConfigError",
    "IngestStoragePorts",
    "ingest_storage_from_env",
    "preflight_ingest_storage",
    "reset_ingest_storage_preflight",
]
