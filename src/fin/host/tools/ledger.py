"""P3 T4: the immutable tool outcome ledger.

One append-only PostgreSQL table holds every validation and execution
observation the graph makes about a model-requested tool call. A row
carries typed codes and DECLARED field paths only — never a submitted
value, raw kwargs, exception text, a credential, or source content.

Writes are fenced: :meth:`PostgresToolOutcomeLedger.record` asserts the
P1 worker attempt's ownership on the SAME cursor and in the SAME
transaction as its inserts, so a stale or fenced worker commits nothing.
The table rejects UPDATE and DELETE through a trigger, so an observation
is never rewritten after it is committed.

The ledger's runtime invariants are typed errors
(:class:`ToolOutcomeLedgerError`), a delivery that added fewer rows than
it carried is logged as a count and never as a value, and the outcome-code
constraint is created once instead of being rewritten on every boot.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Iterator, Literal, Mapping, Protocol, Sequence

from fin.host.runtime.schema import RuntimeUnavailable
from fin.host.runtime.store import assert_ownership

logger = logging.getLogger(__name__)

#: The nine canonical observations a tool call can produce.
ToolOutcomeKind = Literal[
    "schema_rejected",
    "correction_offered",
    "valid_input_accepted",
    "execution_started",
    "succeeded",
    "known_failed",
    "outcome_unknown",
    "reconciled",
    "correction_exhausted",
]

TOOL_OUTCOME_KINDS: Final[tuple[ToolOutcomeKind, ...]] = (
    "schema_rejected",
    "correction_offered",
    "valid_input_accepted",
    "execution_started",
    "succeeded",
    "known_failed",
    "outcome_unknown",
    "reconciled",
    "correction_exhausted",
)

#: The safe typed codes a fact may carry. Nothing else is storable, so a
#: free-text reason can never reach the table.
OutcomeCode = Literal[
    "invalid_tool_arguments",
    "tool_validation_exhausted",
    "tool_result_error",
    "result_missing",
]

OUTCOME_CODES: Final[tuple[OutcomeCode, ...]] = (
    "invalid_tool_arguments",
    "tool_validation_exhausted",
    "tool_result_error",
    "result_missing",
)

#: The value-free resolutions a reconciled fact carries (T5): the proof
#: that an uncertain effect either did happen or did not.
ResolutionCode = Literal["proven_not_applied", "proven_applied"]

RESOLUTION_CODES: Final[tuple[ResolutionCode, ...]] = (
    "proven_not_applied",
    "proven_applied",
)

#: Every code the ledger stores.
FactCode = OutcomeCode | ResolutionCode

_ALL_CODES: Final[frozenset[str]] = frozenset((*OUTCOME_CODES, *RESOLUTION_CODES))

_KINDS_SQL: Final[str] = ", ".join(f"'{kind}'" for kind in TOOL_OUTCOME_KINDS)
_CODES_SQL: Final[str] = ", ".join(
    f"'{code}'" for code in (*OUTCOME_CODES, *RESOLUTION_CODES)
)


class ToolOutcomeLedgerError(ValueError):
    """An observation or identity the ledger refuses to store.

    The ledger's runtime invariants are typed errors, not assertions: an
    interpreter running with assertions stripped must not turn a rejected
    observation into a stored one.
    """


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ToolOutcomeLedgerError(f"{field} must be a non-empty string")


#: The ledger's own DDL. Idempotent: ``setup()`` may run on every boot.
SCHEMA_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS fin_tool_outcome_fact (
    fact_seq BIGSERIAL PRIMARY KEY,
    fact_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    logical_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    model_attempt INTEGER NOT NULL,
    execution_attempt INTEGER,
    provider_tool_call_id TEXT,
    runtime_attempt_id TEXT NOT NULL,
    runtime_attempt_generation INTEGER NOT NULL,
    thread_generation INTEGER NOT NULL,
    business_operation_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL})),
    outcome_code TEXT,
    field_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The code vocabulary is owned by this module, so its check is added under
-- its own name. A steady-state boot must not rewrite it: dropping and
-- re-adding the constraint re-validates every row of an append-only table
-- under an ACCESS EXCLUSIVE lock. The catalog guard adds the constraint
-- once, when the table is created or the constraint is missing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'fin_tool_outcome_fact'::regclass
          AND conname = 'fin_tool_outcome_fact_outcome_code_check'
    ) THEN
        ALTER TABLE fin_tool_outcome_fact
            ADD CONSTRAINT fin_tool_outcome_fact_outcome_code_check
            CHECK (outcome_code IS NULL OR outcome_code IN ({_CODES_SQL}));
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS fin_tool_outcome_fact_run_idx
    ON fin_tool_outcome_fact (run_id, fact_seq);

-- Immutability is a DATABASE rule, not a convention: a later code path
-- cannot rewrite or drop an observation that is already committed.
CREATE OR REPLACE FUNCTION fin_tool_outcome_fact_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'fin_tool_outcome_fact is append-only: % is not allowed', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS fin_tool_outcome_fact_append_only
    ON fin_tool_outcome_fact;

CREATE TRIGGER fin_tool_outcome_fact_append_only
    BEFORE UPDATE OR DELETE ON fin_tool_outcome_fact
    FOR EACH ROW EXECUTE FUNCTION fin_tool_outcome_fact_immutable();
"""

_INSERT_SQL: Final[str] = """
INSERT INTO fin_tool_outcome_fact (
    fact_id, run_id, thread_id, logical_call_id, tool_name, model_attempt,
    execution_attempt, provider_tool_call_id, runtime_attempt_id,
    runtime_attempt_generation, thread_generation, business_operation_id,
    kind, outcome_code, field_paths
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (fact_id) DO NOTHING
"""

_SELECT_SQL: Final[str] = """
SELECT fact_seq, fact_id, run_id, thread_id, logical_call_id, tool_name,
       model_attempt, execution_attempt, provider_tool_call_id,
       runtime_attempt_id, runtime_attempt_generation, thread_generation,
       business_operation_id, kind, outcome_code, field_paths, created_at
FROM fin_tool_outcome_fact
WHERE run_id = %s
ORDER BY fact_seq
"""


def fact_id(
    *,
    run_id: str,
    logical_call_id: str,
    kind: str,
    provider_tool_call_id: str | None,
    execution_attempt: int | None,
) -> str:
    """The deterministic identity of one observation.

    The hash covers the run, the LOGICAL call, the observation kind, the
    provider tool-call id and the execution attempt — never the thread or
    the worker attempt, so a duplicate delivery of the same observation
    lands on the same row instead of a second one.

    The identity parts are checked before hashing, so an observation that
    could never be stored is refused with a typed error instead of a
    silently meaningless hash.
    """
    _require_text(run_id, "run_id")
    _require_text(logical_call_id, "logical_call_id")
    if kind not in TOOL_OUTCOME_KINDS:
        raise ToolOutcomeLedgerError(f"unknown outcome kind: {kind!r}")
    if provider_tool_call_id is not None:
        _require_text(provider_tool_call_id, "provider_tool_call_id")
    if execution_attempt is not None and execution_attempt < 0:
        raise ToolOutcomeLedgerError("execution_attempt must not be negative")
    payload = json.dumps(
        [
            run_id,
            logical_call_id,
            kind,
            provider_tool_call_id,
            execution_attempt,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolOutcomeObservation:
    """One observation about one logical tool call.

    The observation carries no value: only the declared field paths the
    schema rejected and a typed outcome code.
    """

    logical_call_id: str
    tool_name: str
    model_attempt: int
    kind: ToolOutcomeKind
    provider_tool_call_id: str | None = None
    execution_attempt: int | None = None
    business_operation_id: str | None = None
    outcome_code: FactCode | None = None
    field_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.logical_call_id, "logical_call_id")
        _require_text(self.tool_name, "tool_name")
        if self.kind not in TOOL_OUTCOME_KINDS:
            raise ToolOutcomeLedgerError(f"unknown outcome kind: {self.kind!r}")
        if self.outcome_code is not None and self.outcome_code not in _ALL_CODES:
            raise ToolOutcomeLedgerError(
                f"unknown outcome code: {self.outcome_code!r}"
            )
        if self.model_attempt < 0:
            raise ToolOutcomeLedgerError("model_attempt must not be negative")
        if self.execution_attempt is not None and self.execution_attempt < 0:
            raise ToolOutcomeLedgerError("execution_attempt must not be negative")
        for path in self.field_paths:
            _require_text(path, "field_path")


@dataclass(frozen=True)
class ToolOutcomeFact:
    """One committed observation, read back from the ledger."""

    fact_seq: int
    fact_id: str
    run_id: str
    thread_id: str
    logical_call_id: str
    tool_name: str
    model_attempt: int
    execution_attempt: int | None
    provider_tool_call_id: str | None
    runtime_attempt_id: str
    runtime_attempt_generation: int
    thread_generation: int
    business_operation_id: str | None
    kind: str
    outcome_code: str | None
    field_paths: tuple[str, ...]
    created_at: datetime


class ToolOutcomeLedgerPort(Protocol):
    """The seam the graph writes its observations through."""

    def record(
        self,
        observations: Sequence[ToolOutcomeObservation],
        *,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        attempt_generation: int,
        thread_generation: int,
    ) -> int:
        """Append the observations; return how many rows were added."""
        ...

    def read_run(self, run_id: str) -> Sequence[ToolOutcomeFact]:
        """Every committed fact of one run, in commit order."""
        ...


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeUnavailable("tool outcome ledger import") from exc
    return psycopg


def _dict_row() -> Any:
    from psycopg.rows import dict_row

    return dict_row


def _jsonb(value: object) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _row_to_fact(row: Mapping[str, Any]) -> ToolOutcomeFact:
    """Build one fact from a NAMED row.

    The row is keyed by column name, so a column that moves in the SELECT
    cannot assign a value to the wrong field.
    """
    paths = row["field_paths"]
    execution_attempt = row["execution_attempt"]
    provider_tool_call_id = row["provider_tool_call_id"]
    business_operation_id = row["business_operation_id"]
    outcome_code = row["outcome_code"]
    return ToolOutcomeFact(
        fact_seq=int(row["fact_seq"]),
        fact_id=str(row["fact_id"]),
        run_id=str(row["run_id"]),
        thread_id=str(row["thread_id"]),
        logical_call_id=str(row["logical_call_id"]),
        tool_name=str(row["tool_name"]),
        model_attempt=int(row["model_attempt"]),
        execution_attempt=(
            None if execution_attempt is None else int(execution_attempt)
        ),
        provider_tool_call_id=(
            None if provider_tool_call_id is None else str(provider_tool_call_id)
        ),
        runtime_attempt_id=str(row["runtime_attempt_id"]),
        runtime_attempt_generation=int(row["runtime_attempt_generation"]),
        thread_generation=int(row["thread_generation"]),
        business_operation_id=(
            None if business_operation_id is None else str(business_operation_id)
        ),
        kind=str(row["kind"]),
        outcome_code=None if outcome_code is None else str(outcome_code),
        field_paths=tuple(str(path) for path in paths or ()),
        created_at=row["created_at"],
    )


class PostgresToolOutcomeLedger:
    """The durable, append-only tool outcome ledger.

    Construction does not touch the database; :meth:`setup` creates the
    idempotent DDL. Every public call opens one short-lived connection
    and runs its statements in one transaction.
    """

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise ValueError("connection_url is required")
        self._url = connection_url

    @property
    def connection_url(self) -> str:
        return self._url

    @contextmanager
    def _connection(self, action: str) -> Iterator[Any]:
        psycopg = _psycopg()
        try:
            with psycopg.connect(self._url) as conn:
                yield conn
        except psycopg.Error as exc:
            raise RuntimeUnavailable(action) from exc

    def setup(self) -> None:
        """Create the ledger table, its index and its trigger."""
        with self._connection("tool outcome ledger setup") as conn:
            conn.execute(SCHEMA_SQL)

    def record(
        self,
        observations: Sequence[ToolOutcomeObservation],
        *,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        attempt_generation: int,
        thread_generation: int,
    ) -> int:
        """Append one step's observations under the caller's attempt.

        Ownership is asserted on the SAME cursor and inside the SAME
        transaction as the inserts: a stale or fenced worker raises
        :class:`~fin.host.runtime.schema.CheckpointOwnershipLost` before
        any fact row commits. A repeated delivery of an observation lands
        on the same ``fact_id`` and adds no second row.

        The return value is how many rows were added, so a delivery that
        added fewer rows than it carried is visible to the caller and in
        the ledger's own log.
        """
        if not observations:
            # EXIT: there is no observation to append — the call is a no-op
            # and no connection is opened.
            return 0
        rows = [
            (
                fact_id(
                    run_id=run_id,
                    logical_call_id=observation.logical_call_id,
                    kind=observation.kind,
                    provider_tool_call_id=observation.provider_tool_call_id,
                    execution_attempt=observation.execution_attempt,
                ),
                run_id,
                thread_id,
                observation.logical_call_id,
                observation.tool_name,
                observation.model_attempt,
                observation.execution_attempt,
                observation.provider_tool_call_id,
                attempt_id,
                attempt_generation,
                thread_generation,
                observation.business_operation_id,
                observation.kind,
                observation.outcome_code,
                _jsonb(list(observation.field_paths)),
            )
            for observation in observations
        ]
        inserted = 0
        with self._connection("tool outcome ledger append") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    assert_ownership(
                        cur,
                        thread_id=thread_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        attempt_generation=attempt_generation,
                        thread_generation=thread_generation,
                    )
                    for row in rows:
                        cur.execute(_INSERT_SQL, row)
                        inserted += cur.rowcount
        if inserted < len(observations):
            # EXIT: the observations the ledger did not add are already
            # committed under the same identity — this delivery is a
            # duplicate and adds no second logical result.
            logger.info(
                "tool outcome ledger: %d of %d observation(s) were a "
                "duplicate delivery and added no row",
                len(observations) - inserted,
                len(observations),
            )
        return inserted

    def read_run(self, run_id: str) -> tuple[ToolOutcomeFact, ...]:
        """Every fact of one run, in commit order."""
        with self._connection("tool outcome ledger read") as conn:
            with conn.cursor(row_factory=_dict_row()) as cur:
                cur.execute(_SELECT_SQL, (run_id,))
                rows = cur.fetchall()
        return tuple(_row_to_fact(row) for row in rows)


__all__ = [
    "OUTCOME_CODES",
    "RESOLUTION_CODES",
    "SCHEMA_SQL",
    "TOOL_OUTCOME_KINDS",
    "FactCode",
    "OutcomeCode",
    "ResolutionCode",
    "PostgresToolOutcomeLedger",
    "ToolOutcomeFact",
    "ToolOutcomeKind",
    "ToolOutcomeLedgerError",
    "ToolOutcomeLedgerPort",
    "ToolOutcomeObservation",
    "fact_id",
]
