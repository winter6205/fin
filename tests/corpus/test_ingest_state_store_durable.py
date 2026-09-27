"""The durable ingest job store on real SQL rows, plus its selection.

Two things can only be proven here: that the Postgres store answers to the
SAME :class:`~fin.corpus.ingest_state.IngestStateStore` port contract as the
in-memory one (so a caller cannot tell which backend it was handed), and that
job state and audit written by one process are readable by another one. The
second leg spawns a real child process rather than rebuilding an object in
place, because "survives a restart" is a claim about the rows, not about the
Python instances.

Skipped when ``FIN_CHECKPOINT_URL`` is unset — the repo's convention for
Postgres-backed durability (see
``tests/host/test_thread_attachment_durable_postgres.py``). The byte layer
stays the in-memory object store: the S3 adapter's own contract is covered in
``tests/corpus/test_object_store.py``, and the default suite must not need a
live S3 service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from fin.corpus.ingest_state import (
    INGEST_STATE_STORE_DURABLE,
    INGEST_STATE_STORE_KEY,
    INGEST_STATE_STORE_MEMORY,
    InMemoryIngestStateStore,
    IngestState,
    IngestStateStore,
    PostgresIngestStateStore,
    ingest_state_store_from_env,
)
from fin.corpus.ingest_state_errors import (
    DuplicateJobError,
    IllegalTransitionError,
    IngestBusyError,
    IngestStateStoreConfigError,
    IngestStateStoreUnavailableError,
    IngestStepError,
    UnknownJobError,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"
STATE_DSN_ENV = "FIN_INGEST_STATE_DSN"
DOC = "AAPL-2022Q3"


def _configured_url() -> str:
    """The DSN this suite runs on, or the empty string.

    ``FIN_INGEST_STATE_DSN`` first because the root conftest unsets a
    ``FIN_CHECKPOINT_URL`` its 2-second probe cannot reach, which would turn a
    slow-but-live Postgres into a silent skip of the one leg that has to run.
    """
    return (
        os.environ.get(STATE_DSN_ENV) or os.environ.get(POSTGRES_URL_ENV) or ""
    ).strip()


@pytest.fixture
def postgres_url() -> str:
    url = _configured_url()
    if not url:
        pytest.skip(
            f"neither {STATE_DSN_ENV} nor {POSTGRES_URL_ENV} is set; durable "
            "ingest job-store persistence is untested in this environment"
        )
    return url


@pytest.fixture
def clean_tables(postgres_url: str) -> Iterator[str]:
    """An empty job/audit pair, before and after the case.

    Both sides because a run killed mid-case would otherwise decide the next
    run's row counts.
    """
    _wipe(postgres_url)
    yield postgres_url
    _wipe(postgres_url)


def _wipe(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM fin_ingest_audit")
        conn.execute("DELETE FROM fin_ingest_job")
        conn.commit()


def _durable(url: str) -> PostgresIngestStateStore:
    return PostgresIngestStateStore(url)


# ----- the shared port contract, on both backends ---------------------


def _assert_store_contract(store: IngestStateStore) -> None:
    """Every promise one job store makes to the ingest machine.

    Run unchanged against the in-memory and the Postgres implementation, so a
    durable store cannot quietly satisfy a weaker contract than the one the
    endpoint was written against.
    """
    assert isinstance(store, IngestStateStore)

    job = store.create_job(DOC, actor="pytest")
    assert job.state is IngestState.PENDING
    assert job.document_id == DOC
    with pytest.raises(DuplicateJobError):
        store.create_job(DOC)

    assert store.get_job(DOC).state is IngestState.PENDING
    with pytest.raises(UnknownJobError):
        store.get_job("NOPE-2099Q9")

    # The settled lifecycle, one legal edge at a time.
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
    ):
        assert store.transition(DOC, state, actor="pytest") is state
    with pytest.raises(IllegalTransitionError):
        store.transition(DOC, IngestState.FETCHING)
    assert store.get_job(DOC).state is IngestState.CATALOGING
    assert store.transition(DOC, IngestState.READY) is IngestState.READY

    # A terminal generation: no lifecycle edge leaves it, and only the
    # settled replacement edge opens the next one.
    with pytest.raises(IllegalTransitionError):
        store.transition(DOC, IngestState.FETCHING)
    superseded = store.supersede_job(
        DOC, actor="pytest", metadata={"superseded_version": "v1"}
    )
    assert superseded.state is IngestState.PENDING
    assert store.get_job(DOC).state is IngestState.PENDING

    # A live job is not supersedeable: that is the concurrent case.
    with pytest.raises(IngestBusyError):
        store.supersede_job(DOC)
    with pytest.raises(UnknownJobError):
        store.supersede_job("NOPE-2099Q9")

    # Optimistic guard: a stale expectation is a busy rejection, not a write.
    with pytest.raises(IngestBusyError):
        store.transition(
            DOC,
            IngestState.FETCHING,
            expected_state=IngestState.CATALOGING,
        )
    assert store.get_job(DOC).state is IngestState.PENDING

    cause = ValueError("parse exploded")
    with pytest.raises(IngestStepError) as excinfo:
        store.fail_step(DOC, step="extracting", cause=cause, actor="pytest")
    assert excinfo.value.step == "extracting"
    failed = store.get_job(DOC)
    assert failed.state is IngestState.FAILED
    assert failed.error_type == "ValueError"
    assert failed.error_message == "parse exploded"

    # Retry re-enters at fetching without a re-upload.
    assert store.transition(DOC, IngestState.FETCHING) is IngestState.FETCHING
    assert store.get_job(DOC).error_type is None

    actions = [row.action for row in store.list_audit(DOC)]
    assert actions == [
        "job_created",
        "transition",
        "transition",
        "transition",
        "transition_rejected",
        "transition",
        "transition_rejected",
        "job_superseded",
        "transition_rejected",
        "transition_rejected",
        "step_failed",
        "transition",
    ]
    created = store.list_audit(DOC)[0]
    assert created.actor == "pytest"
    assert created.occurred_at.tzinfo is not None
    rejected = [
        row for row in store.list_audit(DOC) if row.error_type == "IngestBusyError"
    ]
    assert rejected
    step_failed = store.list_audit(DOC)[-2]
    assert step_failed.step == "extracting"
    assert step_failed.error_message == "parse exploded"
    supersede_row = next(
        row for row in store.list_audit(DOC) if row.action == "job_superseded"
    )
    assert supersede_row.metadata == {"superseded_version": "v1"}
    assert [job.document_id for job in store.list_jobs()] == [DOC]


def test_in_memory_store_satisfies_the_shared_contract() -> None:
    _assert_store_contract(InMemoryIngestStateStore())


def test_postgres_store_satisfies_the_shared_contract(clean_tables: str) -> None:
    _assert_store_contract(_durable(clean_tables))


def test_a_failed_transition_is_durable_but_leaves_the_state_alone(
    clean_tables: str,
) -> None:
    store = _durable(clean_tables)
    store.create_job(DOC)

    with pytest.raises(IllegalTransitionError):
        store.transition(DOC, IngestState.READY)

    assert store.get_job(DOC).state is IngestState.PENDING
    rejection = store.list_audit(DOC)[-1]
    assert rejection.action == "transition_rejected"
    assert rejection.from_state is IngestState.PENDING
    assert rejection.to_state is IngestState.READY
    assert rejection.error_type == "IllegalTransitionError"


# ----- restart persistence (genuine second process) -------------------


_CHILD = """
import json, os
from fin.corpus.ingest_state import PostgresIngestStateStore
store = PostgresIngestStateStore(os.environ["FIN_CHECKPOINT_URL"])
store.create_job(json.loads(os.environ["FIN_CASE"])["doc"], actor="child")
store.transition(json.loads(os.environ["FIN_CASE"])["doc"], "fetching",
                 metadata={"worker": "child"})
print("ok")
"""


def test_a_job_written_by_another_process_is_readable_here(
    clean_tables: str,
) -> None:
    """The restart leg: one process writes, a different one reads.

    The child is a real ``python -c`` process, so nothing in this assertion
    can come from an object the parent kept alive.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env={
            **os.environ,
            "FIN_CHECKPOINT_URL": clean_tables,
            "FIN_INGEST_STATE_DSN": clean_tables,
            "FIN_CASE": json.dumps({"doc": DOC}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    store = _durable(clean_tables)
    job = store.get_job(DOC)
    assert job.state is IngestState.FETCHING
    audit = store.list_audit(DOC)
    assert [row.action for row in audit] == ["job_created", "transition"]
    assert audit[0].actor == "child"
    assert f"pid={os.getpid()}" not in audit[0].actor
    assert audit[1].metadata == {"worker": "child"}


def test_the_status_gate_reports_the_persisted_state_after_a_restart(
    clean_tables: str,
) -> None:
    """What a poller sees after the accepting process is gone."""
    from fin.corpus.ingest_endpoint import IngestEndpoint
    from fin.corpus.metadata_store import PostgresMetadataStore
    from fin.corpus.object_store import InMemoryObjectStore
    from fin.retrieve.fake_push_port import FakePushPort

    def _service() -> IngestEndpoint:
        return IngestEndpoint(
            push_port=FakePushPort(),
            object_store=InMemoryObjectStore(),
            metadata_store=PostgresMetadataStore(clean_tables),
            state_store=_durable(clean_tables),
        )

    submission = _service().submit_durable(
        b"# Heading\n\nDurable body text for the restart case.\n",
        declared_format="md",
        document_id=DOC,
    )
    assert submission.state is IngestState.PENDING
    _advance(clean_tables, IngestState.FETCHING)

    status = _service().job_status(DOC)
    assert status.state is IngestState.FETCHING
    assert status.version == submission.version
    assert status.text_ready is False


def _advance(url: str, state: IngestState) -> None:
    """Move the accepted job forward from a separate store instance."""
    _durable(url).transition(DOC, state)


# ----- selection: durable when configured, closed otherwise ----------


def test_a_configured_dsn_selects_the_durable_store(
    clean_tables: str,
) -> None:
    store = ingest_state_store_from_env({POSTGRES_URL_ENV: clean_tables})
    assert isinstance(store, PostgresIngestStateStore)
    assert isinstance(store, IngestStateStore)


def test_the_explicit_durable_kind_selects_the_durable_store(
    clean_tables: str,
) -> None:
    store = ingest_state_store_from_env(
        {
            INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_DURABLE,
            "FIN_INGEST_STATE_DSN": clean_tables,
        }
    )
    assert isinstance(store, PostgresIngestStateStore)


def test_declared_memory_is_the_only_process_local_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INGEST_STATE_STORE_KEY, INGEST_STATE_STORE_MEMORY)
    monkeypatch.delenv(POSTGRES_URL_ENV, raising=False)
    monkeypatch.delenv(STATE_DSN_ENV, raising=False)
    assert isinstance(
        ingest_state_store_from_env(), InMemoryIngestStateStore
    )


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="no-configuration"),
        pytest.param({POSTGRES_URL_ENV: "   "}, id="blank-dsn"),
        pytest.param(
            {INGEST_STATE_STORE_KEY: INGEST_STATE_STORE_DURABLE}, id="durable-kind-without-dsn"
        ),
        pytest.param(
            {INGEST_STATE_STORE_KEY: "PostgresSQL"}, id="unknown-kind"
        ),
    ],
)
def test_startup_refuses_an_undurable_job_store(
    environ: dict[str, str],
) -> None:
    """No DSN, no door: the factory raises instead of handing back memory."""
    with pytest.raises(IngestStateStoreConfigError) as excinfo:
        ingest_state_store_from_env(environ)
    assert excinfo.value.key in (
        INGEST_STATE_STORE_KEY,
        "FIN_INGEST_STATE_DSN",
    )
    assert excinfo.value.key in str(excinfo.value)


def test_an_unreachable_dsn_fails_closed() -> None:
    """A configured-but-dead store is a typed failure, never memory."""
    with pytest.raises(IngestStateStoreUnavailableError):
        PostgresIngestStateStore("postgresql://fin:nope@127.0.0.1:1/fin")


def test_the_dsn_resolution_order_is_fixed(clean_tables: str) -> None:
    """The ingest-specific DSN wins over the process-wide checkpoint URL."""
    environ = {
        "FIN_INGEST_STATE_DSN": clean_tables,
        "FIN_CHECKPOINT_URL": "postgresql://fin:fin@127.0.0.1:1/fin",
    }
    assert isinstance(
        ingest_state_store_from_env(environ), PostgresIngestStateStore
    )


def test_a_durable_store_needs_a_usable_url() -> None:
    for bad in ("", "   ", None):
        with pytest.raises(IngestStateStoreConfigError):
            PostgresIngestStateStore(bad)  # type: ignore[arg-type]


def test_empty_or_blank_document_id_is_refused_by_both_stores(
    clean_tables: str,
) -> None:
    """One identity rule across backends: a non-blank document id.

    Converged: the memory store used to validate only ``create_job``; the
    durable store refused a blank id at every entry that reads one. Both
    stores now raise ``ValueError`` from all of them, so a caller never
    sees one backend's ``UnknownJobError`` where the other says ``bad
    input``.
    """
    stores: list[Any] = [InMemoryIngestStateStore(), _durable(clean_tables)]
    for store in stores:
        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                store.create_job(bad)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                store.get_job(bad)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                store.supersede_job(bad)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                store.list_audit(bad)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                store.fail_step(  # type: ignore[arg-type]
                    bad, step="fetch", cause=RuntimeError("x")
                )
