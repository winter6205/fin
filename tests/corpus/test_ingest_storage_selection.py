"""Fail-closed selection of the corpus ingest door's accept-time storage.

The door promises "accepted" only for bytes, pointers and job state that
outlive the process, so the configuration decision is made in one place and
has one honest answer: durable triple, declared process-local triple, or a
typed refusal that names every layer the deployment left out.

The second half of this file pins the *boot* wrapper around that decision,
:func:`fin.corpus.ingest_storage.preflight_ingest_storage`, which is what
``fin-serve`` calls before it binds a socket: the same table, plus the
promise that the answer is taken once per process and that a refusal leaves
nothing behind for a later call to mistake for a booted door.

The default suite declares ``FIN_CORPUS_INGEST_STORE=memory`` (root
``tests/conftest.py``), so these cases pass their own environ maps and never
touch a network or a database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any, Final, NoReturn

import pytest

import fin.corpus.ingest_state_env as ingest_state_env
import fin.corpus.ingest_storage as storage
from fin.corpus.ingest_state import (
    INGEST_STATE_DSN_KEYS,
    INGEST_STATE_STORE_DURABLE,
    INGEST_STATE_STORE_KEY,
    INGEST_STATE_STORE_MEMORY,
    InMemoryIngestStateStore,
    IngestStateStoreConfigError,
)
from fin.corpus.ingest_state_errors import IngestStateError
from fin.corpus.ingest_storage import (
    OBJECT_STORE_BUCKET_KEY,
    OBJECT_STORE_URL_KEYS,
    IngestStorageConfigError,
    IngestStoragePorts,
    ingest_storage_from_env,
    preflight_ingest_storage,
    reset_ingest_storage_preflight,
)
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.object_store_errors import ObjectStoreNotConfiguredError

_DSN = "postgresql://fin:fin@127.0.0.1:1/fin"


class _StubPointerStore:
    """A non-in-memory metadata store stand-in for the durable leg."""

    def put_pointer(self, document_id: str, pointer: object) -> None: ...

    def get_pointer(self, document_id: str) -> object: ...


class _StubObjectStore:
    def put(self, key, payload, *, content_type=None): ...

    def get(self, key): ...

    def get_pointer(self, key): ...

    def delete(self, key): ...


def _durable_monkeypatched(
    monkeypatch: pytest.MonkeyPatch,
    *,
    objects: object,
    pointers: object,
    states: object | None = None,
) -> None:
    """Stand in for the three stores whose real constructors reach out.

    The durable leg of the decision is what is under test; opening a socket to
    Postgres or to MinIO belongs to the opt-in suites.
    """
    monkeypatch.setattr(storage, "_durable_object_store", lambda environ: objects)
    monkeypatch.setattr(
        storage, "_durable_pointer_store", lambda environ: pointers
    )
    monkeypatch.setattr(
        storage,
        "_durable_state_store",
        lambda environ: states if states is not None
        else InMemoryIngestStateStore(),
    )


def test_declared_local_boot_gets_the_process_local_triple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = ingest_storage_from_env(
        {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_MEMORY}
    )
    assert isinstance(ports, IngestStoragePorts)
    assert ports.durable is False
    assert isinstance(ports.objects, InMemoryObjectStore)
    assert isinstance(ports.pointers, InMemoryMetadataStore)
    assert isinstance(ports.states, InMemoryIngestStateStore)


def test_durable_boot_gets_three_durable_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects, pointers = _StubObjectStore(), _StubPointerStore()
    _durable_monkeypatched(monkeypatch, objects=objects, pointers=pointers)

    ports = ingest_storage_from_env({"FIN_INGEST_STATE_DSN": _DSN})

    assert ports.durable is True
    assert ports.objects is objects
    assert ports.pointers is pointers


def test_an_unconfigured_deployment_is_refused_naming_every_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap this closes: accept into process memory, silently."""
    with pytest.raises(IngestStorageConfigError) as excinfo:
        ingest_storage_from_env({})

    assert excinfo.value.__cause__ is None or isinstance(
        excinfo.value.__cause__, IngestStateStoreConfigError
    )
    assert "object store" in str(excinfo.value)
    assert "pointer" in str(excinfo.value)
    assert "FIN_CORPUS_INGEST_STORE" in str(excinfo.value.__cause__)


def test_a_missing_object_store_alone_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointers = _StubPointerStore()
    _durable_monkeypatched(monkeypatch, objects=None, pointers=pointers)
    monkeypatch.setattr(
        storage,
        "_durable_object_store",
        lambda environ: (_ for _ in ()).throw(
            IngestStorageConfigError(
                "object store is not configured", missing=["FIN_OBJECT_STORE_URL"]
            )
        ),
    )

    with pytest.raises(IngestStorageConfigError) as excinfo:
        ingest_storage_from_env({"FIN_INGEST_STATE_DSN": _DSN})
    assert "FIN_OBJECT_STORE_URL" in excinfo.value.missing


def test_a_missing_pointer_store_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = _StubObjectStore()
    _durable_monkeypatched(monkeypatch, objects=objects, pointers=None)
    monkeypatch.setattr(storage, "_durable_pointer_store", lambda environ: None)

    with pytest.raises(IngestStorageConfigError) as excinfo:
        ingest_storage_from_env({"FIN_INGEST_STATE_DSN": _DSN})
    assert "object pointer store" in str(excinfo.value)
    assert "FIN_INGEST_STATE_DSN" in excinfo.value.missing


def test_the_ports_are_a_frozen_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = ingest_storage_from_env(
        {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_MEMORY}
    )
    with pytest.raises(Exception):
        ports.states = InMemoryIngestStateStore()  # type: ignore[misc]


# ---------- preflight: the boot decision, taken once per process ----------


class _StubStateStore:
    """A non-in-memory job-store stand-in for the durable leg."""

    def create_job(self, document_id: str, *, actor: str | None = None): ...

    def get_job(self, document_id: str): ...


#: Passed to :func:`_patch_legs` to leave that leg's production builder alone.
_REAL: Final[Any] = object()


def _durable_deployment(**overrides: "str | None") -> dict[str, str]:
    """A fully configured durable boot, with named keys overridden.

    ``None`` removes a key; ``""`` / whitespace stands for a key the
    deployment exported empty. Either way the map is passed in explicitly, so
    the operator ``.env`` that ``tests/conftest.py`` loads can never decide a
    case for us.
    """
    values = {
        INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_DURABLE,
        "FIN_OBJECT_STORE_URL": "http://127.0.0.1:9000",
        OBJECT_STORE_BUCKET_KEY: "fin-corpus",
        "FIN_INGEST_STATE_DSN": _DSN,
    }
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def _patch_legs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    objects: Any = _REAL,
    pointers: Any = _REAL,
    states: Any = _REAL,
) -> None:
    """Replace only the legs named; leave the rest on the production path.

    A configuration gap is proven by the real builder answering ``None`` or
    raising its typed error, which is why the legs are patched selectively:
    :func:`_durable_object_store` and the two DSN legs are hermetic while the
    configuration is absent, and only a leg that *would* open a socket needs a
    stand-in.
    """
    if objects is not _REAL:
        monkeypatch.setattr(storage, "_durable_object_store", lambda environ: objects)
    if pointers is not _REAL:
        monkeypatch.setattr(storage, "_durable_pointer_store", lambda environ: pointers)
    if states is not _REAL:
        monkeypatch.setattr(storage, "_durable_state_store", lambda environ: states)


def _must_not_build_a_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any in-process memory store a hard failure for this case.

    Asserting the typed error alone would still pass if the door handed back
    a process-local triple on its way to refusing, and that hand-back is the
    exact bug this module exists to rule out. Every module that can name a
    memory job store is trapped: the door builds one directly on its
    declared-local leg, and the job-store factory (now in
    ``fin.corpus.ingest_state_env``) builds one on the ``memory`` branch.
    """

    def _refuse(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError(
            "a refused boot must never fall back to a process-local store"
        )

    monkeypatch.setattr(storage, "InMemoryObjectStore", _refuse)
    monkeypatch.setattr(storage, "InMemoryMetadataStore", _refuse)
    monkeypatch.setattr(storage, "InMemoryIngestStateStore", _refuse)
    monkeypatch.setattr(ingest_state_env, "InMemoryIngestStateStore", _refuse)


def _no_leg_may_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trap every storage leg, for a refusal that must happen before them."""

    def _unreachable(environ: Mapping[str, str]) -> NoReturn:
        raise AssertionError("a refused boot must not reach a storage leg")

    monkeypatch.setattr(storage, "_durable_object_store", _unreachable)
    monkeypatch.setattr(storage, "_durable_pointer_store", _unreachable)
    monkeypatch.setattr(storage, "_durable_state_store", _unreachable)


@pytest.fixture(autouse=True)
def _preflight_cache_isolated() -> Iterator[None]:
    """Start and end every case with an empty process snapshot.

    ``preflight_ingest_storage`` caches for the life of the process, and the
    snapshots built here hold *stub* stores; leaking one would hand a later
    module's ingest door objects that answer no call at all.
    """
    reset_ingest_storage_preflight()
    try:
        yield
    finally:
        reset_ingest_storage_preflight()


def test_preflight_admits_a_fully_configured_durable_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _must_not_build_a_memory_store(monkeypatch)
    objects, pointers, states = (
        _StubObjectStore(),
        _StubPointerStore(),
        _StubStateStore(),
    )
    _patch_legs(
        monkeypatch, objects=objects, pointers=pointers, states=states
    )

    ports = preflight_ingest_storage(_durable_deployment())

    assert ports.durable is True
    assert ports.objects is objects
    assert ports.pointers is pointers
    assert ports.states is states


def test_preflight_refuses_a_missing_object_store_naming_only_that_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No endpoint configured: the raw bytes have nowhere durable to go."""
    _must_not_build_a_memory_store(monkeypatch)
    _patch_legs(
        monkeypatch,
        pointers=_StubPointerStore(),
        states=_StubStateStore(),
    )

    with pytest.raises(IngestStorageConfigError) as excinfo:
        preflight_ingest_storage(
            _durable_deployment(FIN_OBJECT_STORE_URL=None)
        )

    assert "raw object store" in str(excinfo.value)
    assert set(excinfo.value.missing) >= set(OBJECT_STORE_URL_KEYS)
    # The bucket WAS configured; telling the operator to set it again would
    # send them hunting the wrong key.
    assert OBJECT_STORE_BUCKET_KEY not in excinfo.value.missing


def test_preflight_refuses_an_endpoint_without_a_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-configured object storage stores nothing, so it is refused."""
    _must_not_build_a_memory_store(monkeypatch)
    _patch_legs(
        monkeypatch,
        pointers=_StubPointerStore(),
        states=_StubStateStore(),
    )

    with pytest.raises(IngestStorageConfigError) as excinfo:
        preflight_ingest_storage(
            _durable_deployment(**{OBJECT_STORE_BUCKET_KEY: "   "})
        )

    # The converted error's own gap leads; the still-unset sibling endpoint key
    # rides along from the blank-key sweep, while the URL that WAS configured
    # is never re-listed at the operator.
    assert excinfo.value.missing[0] == OBJECT_STORE_BUCKET_KEY
    assert set(excinfo.value.missing) == {
        OBJECT_STORE_BUCKET_KEY,
        "FIN_OBJECT_STORE_ENDPOINT",
    }
    assert "FIN_OBJECT_STORE_URL" not in excinfo.value.missing
    assert isinstance(excinfo.value.__cause__, ObjectStoreNotConfiguredError)
    assert "FIN_OBJECT_STORE_BUCKET" in str(excinfo.value.__cause__)
    assert "no raw object store" in str(excinfo.value)


def test_preflight_refuses_a_missing_metadata_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured bytes with no Postgres still cannot promise an accept."""
    _must_not_build_a_memory_store(monkeypatch)
    _patch_legs(monkeypatch, objects=_StubObjectStore())

    with pytest.raises(IngestStorageConfigError) as excinfo:
        preflight_ingest_storage(
            _durable_deployment(FIN_INGEST_STATE_DSN=None)
        )

    message = str(excinfo.value)
    assert "object pointer store" in message
    assert "durable ingest job store" in message
    assert set(excinfo.value.missing) >= set(INGEST_STATE_DSN_KEYS) | {
        INGEST_STATE_STORE_KEY
    }
    cause = excinfo.value.__cause__
    assert isinstance(cause, IngestStateStoreConfigError)
    assert cause.key == INGEST_STATE_DSN_KEYS[0]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "space", "tab"])
def test_preflight_refuses_a_blank_dsn(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """An exported-but-empty DSN is a gap, not a durable configuration.

    Whitespace passes ``environ.get(key)`` and would boot a door whose rows
    die with the process if the blank check were dropped.
    """
    _must_not_build_a_memory_store(monkeypatch)
    _patch_legs(monkeypatch, objects=_StubObjectStore())

    with pytest.raises(IngestStorageConfigError) as excinfo:
        preflight_ingest_storage(
            _durable_deployment(FIN_INGEST_STATE_DSN=blank)
        )

    assert set(excinfo.value.missing) >= set(INGEST_STATE_DSN_KEYS)


def test_preflight_refuses_a_store_key_that_names_no_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently pick a backend -- and must not pick one loudly either.

    The typo is refused by the job-store layer's own typed error, which names
    the key it rejected, before this module aggregates anything: the
    distinction is pinned rather than smoothed over, because the two error
    types carry different machine-readable fields (``key`` vs ``missing``).
    """
    _must_not_build_a_memory_store(monkeypatch)
    _no_leg_may_be_built(monkeypatch)

    with pytest.raises(IngestStateStoreConfigError) as excinfo:
        preflight_ingest_storage(
            _durable_deployment(**{INGEST_STATE_STORE_KEY: "postgre"})
        )

    assert isinstance(excinfo.value, IngestStateError)
    assert excinfo.value.key == INGEST_STATE_STORE_KEY
    assert INGEST_STATE_STORE_DURABLE in str(excinfo.value)
    assert INGEST_STATE_STORE_MEMORY in str(excinfo.value)


def test_preflight_admits_only_a_declared_local_boot_and_says_so_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``memory`` is the one exception, and it has to be visible in the log."""
    with caplog.at_level(
        logging.WARNING, logger="fin.corpus.ingest_storage"
    ):
        ports = preflight_ingest_storage(
            {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_MEMORY}
        )

    assert ports.durable is False
    assert isinstance(ports.objects, InMemoryObjectStore)
    assert isinstance(ports.pointers, InMemoryMetadataStore)
    assert isinstance(ports.states, InMemoryIngestStateStore)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("process-local" in message for message in warnings)


class _CountingResolver:
    """A stand-in ``ingest_storage_from_env`` that records each resolution.

    Each answer is a fresh object, so identity distinguishes "served from the
    snapshot" from "decided again" without depending on how many stores a
    resolution builds.
    """

    def __init__(self) -> None:
        self.environ_seen: list[Mapping[str, str]] = []

    def __call__(
        self, environ: Mapping[str, str] | None = None
    ) -> IngestStoragePorts:
        self.environ_seen.append(dict(environ or {}))
        return IngestStoragePorts(
            objects=_StubObjectStore(),
            pointers=_StubPointerStore(),
            states=_StubStateStore(),
            durable=True,
        )


def test_preflight_decides_once_so_a_second_environ_cannot_redecide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot is the promise: one resolution per process.

    A second resolution would open a second set of durable connections, and a
    mid-process re-decision would let the storage behind ``accepted`` change
    while the door is serving.
    """
    resolver = _CountingResolver()
    monkeypatch.setattr(storage, "ingest_storage_from_env", resolver)
    first_environment = _durable_deployment()

    first = preflight_ingest_storage(first_environment)
    second = preflight_ingest_storage(
        {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_MEMORY}
    )

    assert second is first
    assert len(resolver.environ_seen) == 1
    assert resolver.environ_seen[0] == first_environment


def test_reset_makes_the_next_call_decide_from_its_own_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache is a snapshot, not a one-way door for the test process."""
    resolver = _CountingResolver()
    monkeypatch.setattr(storage, "ingest_storage_from_env", resolver)
    cached = preflight_ingest_storage(_durable_deployment())
    assert len(resolver.environ_seen) == 1

    reset_ingest_storage_preflight()
    local = {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_MEMORY}
    decided_again = preflight_ingest_storage(local)

    assert decided_again is not cached
    assert [dict(env) for env in resolver.environ_seen] == [
        _durable_deployment(),
        local,
    ]


def test_a_refusal_caches_nothing_so_a_corrected_boot_can_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused boot never serves, and a refusal is not memoized.

    The first call below is the real ``fin-serve`` path on a mis-deployment:
    it raises, so nothing is published. The second call is the operator's
    fix -- it has to be allowed to decide again rather than inherit an
    answer from the failed boot.
    """
    _must_not_build_a_memory_store(monkeypatch)
    _patch_legs(
        monkeypatch,
        pointers=_StubPointerStore(),
        states=_StubStateStore(),
    )
    with pytest.raises(IngestStorageConfigError):
        preflight_ingest_storage(
            _durable_deployment(FIN_OBJECT_STORE_URL=None)
        )

    _patch_legs(monkeypatch, objects=_StubObjectStore())
    ports = preflight_ingest_storage(_durable_deployment())

    assert ports.durable is True
