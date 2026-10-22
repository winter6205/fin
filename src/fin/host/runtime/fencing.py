"""Generation-aware checkpoint fencing (P1 T3, ADR 0103).

``PostgresSaver`` writes checkpoints on its own connection without knowing
whether the worker still owns the thread. :class:`FencedPostgresSaver` keeps
the synchronous saver interface but runs the Runtime ownership assertion on
the same connection and inside the same transaction as every mutation, so a
stale attempt can never commit a checkpoint row.

Identity comes from ``config["configurable"]``::

    runtime_run_id, runtime_attempt_id,
    runtime_attempt_generation, thread_generation

All four present selects the strict check (:func:`assert_ownership`). Some
but not all is a wiring bug and fails closed. None selects maintenance mode:
a caller such as ``finalize_abandoned_run``, ``compact_thread`` or the CLI
may still write, but only while the thread has no tombstone, no reserved
reservation and no running run. A stale worker always carries its identity,
so it never reaches maintenance mode.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import tuple_row

from fin.host.runtime.schema import (
    OWNERSHIP_THREAD_TOMBSTONED,
    RESERVATION_RESERVED,
    RUN_RUNNING,
    CheckpointOwnershipLost,
)
from fin.host.runtime.store import PostgresRuntimeStore, assert_ownership

#: The config keys a fenced worker attempt carries.
RUNTIME_IDENTITY_KEYS: tuple[str, ...] = (
    "runtime_run_id",
    "runtime_attempt_id",
    "runtime_attempt_generation",
    "thread_generation",
)

#: The four identity keys are only meaningful together.
IDENTITY_INCOMPLETE: str = "runtime_identity_incomplete"

#: A maintenance write has no identity to prove ownership with.
IDENTITY_MISSING: str = "runtime_identity_missing"

#: A mutation this adapter has no fenced equivalent for.
RUNTIME_MUTATION_NOT_SUPPORTED: str = "runtime_mutation_not_supported"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeOwnership:
    """The strict identity one fenced worker attempt carries."""

    thread_id: str
    run_id: str
    attempt_id: str
    attempt_generation: int
    thread_generation: int


def _configurable(config: Any) -> Mapping[str, Any] | None:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, Mapping) else None


def resolve_ownership(config: Any) -> RuntimeOwnership | None:
    """Return the strict identity, ``None`` for maintenance mode.

    A partially present identity can never fall back to maintenance: that
    would let a mis-wired worker write as an operator-initiated caller.
    Public so other fenced writers (the tool outcome ledger) resolve the
    identity exactly as the checkpoint adapter does.
    """
    configurable = _configurable(config)
    if configurable is None:
        return None
    present = [
        key
        for key in RUNTIME_IDENTITY_KEYS
        if key in configurable and configurable[key] is not None
    ]
    if not present:
        return None
    if len(present) != len(RUNTIME_IDENTITY_KEYS):
        raise CheckpointOwnershipLost(IDENTITY_INCOMPLETE)

    thread_id = configurable.get("thread_id")
    run_id = configurable["runtime_run_id"]
    attempt_id = configurable["runtime_attempt_id"]
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(attempt_id, str)
        or not attempt_id
    ):
        raise CheckpointOwnershipLost(IDENTITY_INCOMPLETE)
    try:
        attempt_generation = int(configurable["runtime_attempt_generation"])
        thread_generation = int(configurable["thread_generation"])
    except (TypeError, ValueError) as exc:
        raise CheckpointOwnershipLost(IDENTITY_INCOMPLETE) from exc
    return RuntimeOwnership(
        thread_id=thread_id,
        run_id=run_id,
        attempt_id=attempt_id,
        attempt_generation=attempt_generation,
        thread_generation=thread_generation,
    )


def _maintenance_thread_id(config: Any) -> str:
    configurable = _configurable(config)
    thread_id = configurable.get("thread_id") if configurable is not None else None
    if not isinstance(thread_id, str) or not thread_id:
        # Without a thread identity nothing can be asserted; fail closed
        # instead of letting the stock saver raise a KeyError downstream.
        raise CheckpointOwnershipLost(IDENTITY_MISSING)
    return thread_id


def assert_maintenance_write_allowed(cur: Any, *, thread_id: str) -> None:
    """Allow a non-worker write only on a thread with nothing live.

    Runs on the caller's cursor, inside the checkpoint transaction. The
    lock order matches :func:`assert_ownership`: thread reservation, then
    the run/attempt authority. A tombstoned thread is reported as such; a
    reserved reservation or a running run makes the missing identity the
    reason, because a worker that belongs there should have carried one.
    """
    fenced = cur.execute(
        "SELECT tombstoned FROM fin_runtime_thread_state WHERE thread_id = %s",
        (thread_id,),
    ).fetchone()
    if fenced is not None and bool(fenced[0]):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_TOMBSTONED)
    reservation = cur.execute(
        "SELECT state FROM fin_runtime_thread_reservation WHERE thread_id = %s "
        "FOR UPDATE",
        (thread_id,),
    ).fetchone()
    if reservation is not None and reservation[0] == RESERVATION_RESERVED:
        raise CheckpointOwnershipLost(IDENTITY_MISSING)
    running = cur.execute(
        "SELECT 1 FROM fin_runtime_run WHERE thread_id = %s AND state = %s LIMIT 1",
        (thread_id, RUN_RUNNING),
    ).fetchone()
    if running is not None:
        raise CheckpointOwnershipLost(IDENTITY_MISSING)
    # Repeat the fence under a share lock, as ``assert_ownership`` does, so
    # a tombstone that landed while this transaction ran is still seen.
    fenced = cur.execute(
        "SELECT tombstoned FROM fin_runtime_thread_state WHERE thread_id = %s "
        "FOR SHARE",
        (thread_id,),
    ).fetchone()
    if fenced is not None and bool(fenced[0]):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_TOMBSTONED)


class FencedPostgresSaver(PostgresSaver):
    """``PostgresSaver`` whose mutations assert Runtime ownership first."""

    store: PostgresRuntimeStore
    # The base annotates a ``threading.Lock``; the fencing transaction
    # re-enters this lock through ``_cursor`` and production wraps it in
    # ``_RunAwareCheckpointLock``, so the type is deliberately open.
    lock: Any

    def __init__(
        self, conn: psycopg.Connection[Any], store: PostgresRuntimeStore
    ) -> None:
        super().__init__(conn)
        self.store = store
        # Production wraps this lock in ``_RunAwareCheckpointLock``; the
        # outer transaction re-enters it through ``_cursor``.
        self.lock = threading.RLock()
        # ``_cursor(pipeline=True)`` then joins the enclosing transaction
        # instead of opening a pipeline on the same connection.
        self.supports_pipeline = False

    def attach_runtime(self, store: PostgresRuntimeStore) -> None:
        """Attach ownership state after a production ``__class__`` swap.

        ``from_conn_string`` already ran the base initializer, so the
        reentrant lock and the pipeline flag must be installed here too.
        """
        self.store = store
        if not hasattr(self.lock, "_is_owned"):
            self.lock = threading.RLock()
        self.supports_pipeline = False

    @contextmanager
    def _write_connection(self) -> Iterator[psycopg.Connection[Any]]:
        conn = self.conn
        if isinstance(conn, psycopg.Connection):
            yield conn
            return
        raise TypeError(
            "FencedPostgresSaver requires a single psycopg connection, "
            f"not {type(conn).__name__}"
        )

    def _assert_write_allowed(self, cur: Any, config: Any) -> None:
        ownership = resolve_ownership(config)
        if ownership is None:
            assert_maintenance_write_allowed(
                cur, thread_id=_maintenance_thread_id(config)
            )
            return
        assert_ownership(
            cur,
            thread_id=ownership.thread_id,
            run_id=ownership.run_id,
            attempt_id=ownership.attempt_id,
            attempt_generation=ownership.attempt_generation,
            thread_generation=ownership.thread_generation,
        )

    def _fenced_mutation(self, config: Any, mutate: Any) -> Any:
        with self.lock, self._write_connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=tuple_row) as cur:
                    self._assert_write_allowed(cur, config)
                return mutate()

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        return self._fenced_mutation(
            config,
            lambda: super(FencedPostgresSaver, self).put(
                config, checkpoint, metadata, new_versions
            ),
        )

    def put_writes(
        self,
        config: Any,
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._fenced_mutation(
            config,
            lambda: super(FencedPostgresSaver, self).put_writes(
                config, writes, task_id, task_path
            ),
        )

    def delete_thread(self, thread_id: str) -> None:
        """Tombstone first, so writers are fenced before rows are removed."""
        with self.lock:
            self.store.tombstone_thread(thread_id=str(thread_id))
            super().delete_thread(thread_id)

    def prune(
        self, thread_ids: Sequence[str], *, strategy: str = "keep_latest"
    ) -> None:
        """Fence a prune: only a thread with nothing live may be pruned.

        The fence runs in the prune's own transaction, so a thread that
        gained a reservation or a running run while the prune waited is
        still refused.
        """
        with self.lock, self._write_connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=tuple_row) as cur:
                    for thread_id in thread_ids:
                        assert_maintenance_write_allowed(
                            cur, thread_id=str(thread_id)
                        )
                try:
                    super().prune(thread_ids, strategy=strategy)
                except NotImplementedError:
                    # EXIT: the installed saver implements no pruning, so
                    # the fence (proved above) is the whole effect.
                    logger.info(
                        "prune: the installed checkpointer implements no "
                        "pruning; the ownership fence is the whole effect"
                    )

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Fence a copy: the target thread must have nothing live."""
        with self.lock, self._write_connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=tuple_row) as cur:
                    assert_maintenance_write_allowed(
                        cur, thread_id=str(target_thread_id)
                    )
                try:
                    super().copy_thread(source_thread_id, target_thread_id)
                except NotImplementedError:
                    # EXIT: the installed saver implements no thread copy,
                    # so the fence (proved above) is the whole effect.
                    logger.info(
                        "copy_thread: the installed checkpointer implements "
                        "no copy; the ownership fence is the whole effect"
                    )

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        """Refuse: a run-scoped delete has no fenced equivalent here."""
        raise CheckpointOwnershipLost(RUNTIME_MUTATION_NOT_SUPPORTED)


def fence_postgres_saver(
    saver: PostgresSaver, store: PostgresRuntimeStore
) -> FencedPostgresSaver:
    """Turn an already-open production saver into its fenced subclass."""
    setattr(saver, "__class__", FencedPostgresSaver)
    fenced = saver
    assert isinstance(fenced, FencedPostgresSaver)
    fenced.attach_runtime(store)
    return fenced


__all__ = [
    "FencedPostgresSaver",
    "IDENTITY_INCOMPLETE",
    "IDENTITY_MISSING",
    "RUNTIME_IDENTITY_KEYS",
    "RUNTIME_MUTATION_NOT_SUPPORTED",
    "RuntimeOwnership",
    "assert_maintenance_write_allowed",
    "fence_postgres_saver",
    "resolve_ownership",
]
