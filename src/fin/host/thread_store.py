"""Thread / run metadata storage for the workspace surface (T1, #28).

The workspace HTTP routes need a place to keep the product-level
metadata that does NOT belong to the LangGraph checkpointer:

* thread rows (``id``, ``title``, ``created_at``) — the sidebar and
  the turn table's identity;
* run rows (one per send: question, status, citations, ...) — the
  GET ``/api/threads/{id}`` turn table without dumping checkpoint
  internals.

Two implementations live behind one duck-typed protocol
(``ThreadStore``):

* :class:`InMemoryThreadStore` — plain dicts + a lock. Used by tests
  (no Postgres needed) and by dev boots with no
  ``FIN_CHECKPOINT_URL`` set.
* :class:`PostgresThreadStore` — ``psycopg`` directly against the
  SAME Postgres the durable graph uses (ADR 0015). Schema setup is
  idempotent; tables are host-owned (``fin_thread`` / ``fin_run``),
  NOT LangGraph checkpointer tables.

Why psycopg directly instead of through LangGraph? The checkpointer
owns *graph state*; this store owns *product presentation state*
(titles, turn rows). Mixing them into the checkpoint tuples would
couple two lifecycles (delete-thread must remove titles even when
the checkpoint write failed, and vice versa). A separate side-table
keeps each lifecycle explicit.

The default selector :func:`default_thread_store` mirrors the
existing ``compile_for_runtime`` pattern: read
``FIN_CHECKPOINT_URL`` from the environment and pick accordingly.

Known debt (review note, deliberate deferral — see spec §9): this
module carries both store backends plus the §7 replay projection and
the durable-write evidence guard in one file (1,200+ lines). The
Session-E changes are localized on purpose; a backend/guard split is a
standalone refactor so its diff never mixes with contract work.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Protocol

import psycopg

from fin.host.citation_gate import COMPANY_KIND, DATABASE_ROW_KIND
from fin.host.evidence_contract import (
    CITATION_CHANNEL_SCHEMA,
    CITATION_OVERFLOW,
    CITATION_SNAPSHOT_KEY,
    DATABASE_ROW_OVERFLOW,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_FIELD,
    AdmissionContext,
    VISUAL_CLAIM_OVERFLOW,
    seed_context_from_rows,
    validate_evidence,
)
from fin.host.visual_evidence import VISUAL_CLAIM_FIELDS, VISUAL_EVIDENCE_KIND

# WHY the §7 frozen-shape grace at the durable write does NOT cover these:
# the legacy-shapes escape exists for a pre-cutover writer's STRUCTURE
# (absent kind defaulting, the page->unit rename) — the §2.3 nearest-older-
# parser concern. A §3 content bound (quote length, snapshot size, visual
# detail size) applied to every writer generation, so a row that is
# shape-legal yet over-bound is corruption the guard must stop HERE, not
# smuggle into the JSONB column for every later replay to re-refuse.
_SHAPE_GRACE_EXCEPTIONS: frozenset[str] = frozenset(
    {CITATION_OVERFLOW, DATABASE_ROW_OVERFLOW, VISUAL_CLAIM_OVERFLOW}
)


# ----- typed errors -----


class ThreadStoreError(Exception):
    """Base class for thread-store typed errors."""


class ThreadNotFoundError(ThreadStoreError):
    """A route addressed a thread_id the store does not know.

    The HTTP layer translates this to 404. The store raises it on
    operations that REQUIRE an existing row (append_run, list_runs,
    delete_thread) so misconfigured routes fail loudly.
    """


class PersistenceUnavailableError(ThreadStoreError):
    """The store itself could not be read or written.

    Distinct from every contract failure: the evidence may be perfectly
    well-shaped and the database still be down. Spec §6 names this
    outcome ``persistence_unavailable`` so an outage is never a bare
    driver crash, and never silently served as empty history.
    """

    code = "persistence_unavailable"


class EvidenceContractWriteError(ThreadStoreError):
    """Outgoing evidence matched neither the v1 channel nor the frozen
    legacy shape, so it is refused BEFORE durable storage.

    The code carried from the validator names which contract failed; a
    corrupt row never reaches the JSONB column to be replayed later.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ----- the frozen legacy (v0) citation shape -----

# The nine names a stored v0 citation row may carry: the eight slots the
# wire knows plus the pre-rename ``page`` key still living in old JSONB
# rows. Any key outside this set is corruption, not an older generation.
_V0_CITATION_NAMES: frozenset[str] = frozenset(
    {
        "kind",
        "document_id",
        "attachment_id",
        "version",
        "unit",
        "display",
        "filename",
        "quote",
        "page",
    }
)

# The eight slots every decoded citation must end up filling.
_V0_CITATION_SLOTS: tuple[str, ...] = (
    "kind",
    "document_id",
    "attachment_id",
    "version",
    "unit",
    "display",
    "filename",
    "quote",
)


def project_stored_citation(row: Any) -> dict[str, Any] | None:
    """Match one untrusted stored row against the frozen v0 shape.

    Stored citation rows span three real writer generations and no
    migration was ever shipped, so a replay read cannot take JSONB
    content on faith. A row is v0-valid iff its keys are a subset of
    the nine names, it carries ``document_id``, ``version`` and
    ``quote``, and EXACTLY ONE of ``unit`` or ``page`` names its
    ordinal — both is a contradiction, neither is corruption. The
    defaults are then frozen facts of that shape: an absent ``kind``
    reads as the document kind (legal only while no lane marker rides
    the row, which is what keeps this from being the forbidden
    row→citation coercion), a ``page`` maps identically into ``unit``,
    an absent ``display`` reads as ``page``, and absent
    ``attachment_id`` / ``filename`` read as ``""``.

    A row carrying the ``database_row`` lane marker is not a v0
    candidate at all: it passes through unchanged only when it already
    states the row kind and fills every slot — historical snapshots
    keep their original values, and defaulting a marked row would let
    a ledger record pose as a filing citation.

    ``None`` means the row matches no frozen shape. The caller keeps it
    verbatim so the read boundary refuses it per-record — visible,
    typed, never defaulted and never dropped.
    """
    if not isinstance(row, Mapping) or not row:
        return None
    keys = frozenset(row)
    if CITATION_SNAPSHOT_KEY in keys:
        if not keys <= (_V0_CITATION_NAMES | {CITATION_SNAPSHOT_KEY}):
            return None
        if row.get("kind") != DATABASE_ROW_KIND:
            return None
        if any(slot not in row for slot in _V0_CITATION_SLOTS):
            return None
        return dict(row)
    if not keys <= _V0_CITATION_NAMES:
        return None
    if any(
        slot not in row or not isinstance(row[slot], str)
        for slot in ("document_id", "version", "quote")
    ):
        return None
    has_unit = "unit" in row
    has_page = "page" in row
    if has_unit == has_page:
        return None
    ordinal = row["unit"] if has_unit else row["page"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        return None
    kind = row.get("kind", COMPANY_KIND)
    display = row.get("display", "page")
    attachment_id = row.get("attachment_id", "")
    filename = row.get("filename", "")
    if not all(
        isinstance(value, str) for value in (kind, display, attachment_id, filename)
    ):
        return None
    return {
        "kind": kind,
        "document_id": row["document_id"],
        "attachment_id": attachment_id,
        "version": row["version"],
        "unit": ordinal,
        "display": display,
        "filename": filename,
        "quote": row["quote"],
    }


# ----- records -----


@dataclass(frozen=True)
class ThreadRecord:
    """One research thread's metadata row."""

    id: str
    title: str
    created_at: datetime


@dataclass(frozen=True)
class RunRecord:
    """One run (= one user send) on a research thread.

    This is the assistant-turn row for GET ``/api/threads/{id}``.
    T3 populates ``thinking_text`` (the streamed thinking prose)
    and ``tool_summaries`` (one ``{name, summary, tool_call_id}``
    dict per tool call observed this run — the SPA renders
    ``summary`` and correlates by ``tool_call_id``; spec line 32).
    Raw ToolMessage JSON never enters a summary string.
    ``citations`` is a list of plain dicts with
    keys ``kind`` / ``document_id`` / ``attachment_id`` / ``version`` /
    ``unit`` / ``display`` / ``filename`` / ``quote`` (the same shape the
    citation gate emits).

    ``status`` is a closed enum — see :data:`RunStatus` — so
    downstream code (``AskResponse``, ``TurnRow``, the gate) can
    pattern-match without re-validating.

    ``assistant_message`` is the ordinary-person copy the SPA
    renders when ``status`` is ``refuse`` or ``step_cap`` (T2). It
    is the same string the workspace SSE ``status`` event carries
    in ``assistant_message``; the turn-row mirror keeps GET
    history consistent with the live surface so a reload does not
    lose the copy. Empty on ``done`` (the run produced a real
    ``answer``).

    T4: ``in_flight`` is NOT persisted on this record's storage
    columns. A stored run row is by definition terminal (the
    route appends it only after the stream completes), so any
    value read back from storage would always be ``False``. The
    live running signal comes from the per-thread lock probe at
    serialization time (see ``_serialize_run`` in
    ``fin.host.workspace_http``); this dataclass field exists so
    that seam has an explicit, typed slot to fill instead of the
    HTTP layer bolting an untyped key onto the dict.

    Soft-Track (T5): ``track_decision`` is the auditable classify
    outcome for this ``run_id`` — at least ``track``, ``confidence``,
    ``decision_tier``, and structured ``signals``. GET history
    reads it as the presentation SSOT for past turns; empty when
    the run never reached classify (e.g. mid-flight refuse before
    a values frame).

    T3 (enterprise-kb Focus 1): ``comparisons`` holds the
    ``fin.comparison`` envelopes this run's ``compare_facts``
    calls produced — the STRUCTURE the turn table renders as a
    table instead of re-parsing prose. Shape SSOT is
    ``fin.host.tools.compare_facts``; stored as JSONB so the
    round-trip needs no codec. Non-empty on ``refuse`` rows too:
    the table is tool output, not model prose.
    """

    id: str
    thread_id: str
    question: str
    status: "RunStatus"
    status_reason: str = ""
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str = ""
    # T4 (#37, spec line 31): total streamed-thinking duration in
    # milliseconds. Accumulated by the route's ThinkingClock during
    # the run; persisted so a reload restores the collapsed
    # one-line duration label. 0 when the model surfaced no
    # thinking.
    thinking_duration_ms: int = 0
    # One ``{name, summary, tool_call_id}`` dict per tool call
    # observed so far in the run; appended by ``tool_node`` and
    # persisted as JSONB. Typed as ``dict[str, str]`` (not a
    # nested dataclass) so the JSONB round-trip needs no codec.
    tool_summaries: list[dict[str, str]] = field(default_factory=list)
    assistant_message: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # T4 (spec line 38): live running marker stamped at GET
    # serialization time from ``graph.is_locked(thread_id)``,
    # never persisted. Defaults ``False``.
    in_flight: bool = False
    # Soft-Track: {track, confidence, decision_tier, signals}.
    track_decision: dict[str, Any] = field(default_factory=dict)
    # Mid-flight stream error taxonomy (auth / network / timeout
    # / rate_limit / not_found / server / unknown). Empty on rows
    # whose refusal came from the gate rather than the stream
    # engine. Persisted so a reload restores the label the SPA
    # rendered live.
    status_classified: str = ""
    # T3 (enterprise-kb Focus 1): ``fin.comparison`` envelopes,
    # one per ``compare_facts`` call this run made. Nested plain
    # JSON (rows / cells / deltas / locators), not a dataclass, so
    # the JSONB round-trip is lossless without a codec.
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    # Trust (#135, spec SC8): the locators this run retrieved or
    # read, one ``{document_id, version, unit}`` dict per distinct
    # unit, in first-touch order. Wider than ``citations`` on
    # purpose — a refused run records what it looked at, so the
    # turn stays auditable even when no span passed the verifier.
    # Run-scoped: the send route copies this run's evidence before
    # the next run resets it (ADR 0016), so a follow-up row never
    # lists the previous run's pages.
    evidence_locators: list[dict[str, Any]] = field(default_factory=list)
    # T6: structured redline / table / statement envelopes from
    # host analysis tools, persisted for GET history and reload.
    analysis_results: list[dict[str, Any]] = field(default_factory=list)
    # ADR 0089 / spec thread-attachment-source-read: the visual claims this
    # run read, one provenance row per observation returned to the current
    # run (shape SSOT: ``fin.host.visual_evidence.VISUAL_CLAIM_FIELDS``).
    # Kept SEPARATE from ``citations`` on purpose — a generated visual
    # observation is never a source quote — so history and a restart still
    # show the claim link without the quote channel. Persisted as JSONB so
    # the round-trip needs no codec; ``[]`` (never ``None``) when the run
    # produced none, so consumers never branch on absence.
    visual_claims: list[dict[str, Any]] = field(default_factory=list)


# ----- enum + validation -----


RunStatus = Literal["done", "refuse", "step_cap"]
"""Closed status enum for one run. Mirrors the run-status enum
emitted by the citation gate (``done`` / ``refuse`` / ``step_cap``)
plus the HTTP layer's ``AskResponse`` / ``TurnRow`` literal types."""


def _validate_status(value: object) -> "RunStatus":
    """Normalize ``value`` to a closed ``RunStatus``.

    Why this seam: ``RunRecord.status`` is the contract between
    the graph layer (gate result), the persistence layer
    (ThreadStore.append_run), and the HTTP layer (AskResponse /
    TurnRow). A typo at any call site (e.g. ``"refuss"``) would
    otherwise pass through unchecked and only fail at the
    Pydantic serializer on read. Failing at the seam keeps the
    invariant local to one helper.

    The default branch collapses anything outside the closed
    set to ``"refuse"`` — the same defensive default the
    ``/ask`` handler uses (see ``fin.host.http``). The caller
    still needs to deal with the answer / citations, but the
    status enum is now consistent.
    """
    if value in ("done", "refuse", "step_cap"):
        return value  # type: ignore[return-value]
    raise ThreadStoreError(
        f"RunRecord.status must be one of 'done'|'refuse'|'step_cap', got {value!r}"
    )


# ----- protocol -----


class ThreadStore(Protocol):
    """Duck-typed contract both implementations satisfy."""

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        """Atomically allocate a new thread row and return its
        ``ThreadRecord``. ``id`` is caller-supplied UUID; when
        ``None`` the store mints one (the v1 /api/threads route
        relies on this so thread ids are stable across the
        HTTP layer and the persistence layer). ``title`` is
        pre-derived from the first user message by the HTTP
        layer (first line, 50-char cap, ellipsis on overflow).
        Idempotent on duplicate ``id`` — raises
        ``ThreadStoreError`` rather than silently overwriting."""
        ...

    def list_threads(self) -> list[ThreadRecord]:
        """Return all thread metadata rows newest-first
        (``created_at`` descending). The HTTP layer serializes
        this for the sidebar; ordering matters so the freshest
        thread surfaces at the top. Returns fresh copies so a
        caller mutating the list cannot corrupt the store."""
        ...

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        """Lookup miss returns ``None`` (NOT an exception): the
        HTTP layer maps ``None`` to a 404 with a stable error
        code. ``ThreadNotFoundError`` is reserved for operations
        that REQUIRE an existing row (``append_run`` /
        ``list_runs``) so misconfigured routes fail loudly.
        Lock covers the dict read so a concurrent
        ``delete_thread`` does not produce a torn view."""
        ...

    def delete_thread(self, thread_id: str) -> bool:
        """Drop the metadata row AND every associated run row.
        In-memory: a single ``del self._threads[thread_id]``
        (runs live on the thread cell). Postgres: the FK has
        ``ON DELETE CASCADE`` so the run rows disappear with
        the thread. Returns ``True`` on success, ``False`` if
        the thread id is unknown (the HTTP layer maps that to
        404 — same shape as a lookup miss)."""
        ...

    def append_run(self, record: RunRecord) -> None:
        """Append a finished run to its thread's run list.

        Why the per-thread lock covers BOTH the dict lookup
        AND the list append: a concurrent ``delete_thread`` on
        the same id must not produce a partial run record
        (``ThreadRecord`` gone, ``RunRecord`` orphaned). The
        Postgres path uses an FK so the cascade is enforced by
        the database; the in-memory path enforces the same
        invariant by ordering operations under one lock.

        ``record.status`` is validated at this seam (closed
        enum) so a typo at the call site fails here, not at
        the next reader.
        """
        ...

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        """Return the runs for ``thread_id`` in ``created_at``
        ascending order (oldest first — the same order the
        chat UI renders the turn table).

        Returns DEEPCOPIES of the persisted records. The
        docstring on ``RunRecord`` warns callers not to mutate
        the returned objects; ``list_runs`` enforces that by
        handing back independent copies so a stray
        ``run.citations.append(...)`` cannot reach into store
        state.
        """
        ...

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        """Set ``title`` on the thread row; return the refreshed
        ``ThreadRecord``.

        Raises ``ThreadNotFoundError`` when ``thread_id`` is
        unknown — the workspace HTTP layer translates that to 404
        the same way ``append_run`` does (lookups via
        ``get_thread`` map miss -> ``None`` -> 404; writes via
        this method raise because a write to a missing row is
        a misconfigured caller, not a user-visible state).

        Why the explicit method (vs. ``get_thread`` + ``create_thread``):
        the workspace send path calls this only on the "first
        real send updates the default-empty-thread title" branch
        (T2 acceptance); a generic rename surface is out of scope
        (spec forbids thread rename chrome).
        """
        ...


# ----- in-memory implementation -----


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryThreadStore:
    """Dict-backed store guarded by a single ``threading.Lock``.

    The lock covers BOTH the dict mutation and any read that walks
    the dict (list/get), so a concurrent delete cannot produce a
    torn view to a reader. Runs live on the thread record itself so
    delete cascades naturally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, _MutableThread] = {}

    # -- threads --

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        # WHY the lock spans (id-mint, duplicate-check, dict-write):
        # two callers racing with the SAME id would otherwise both
        # pass the `not in` check and one would silently overwrite
        # the other. Locking the read + write together makes the
        # duplicate-id path loud (ThreadStoreError) instead of
        # silent corruption.
        with self._lock:
            thread_id = id or str(uuid.uuid4())
            if thread_id in self._threads:
                raise ThreadStoreError(f"thread {thread_id!r} already exists")
            record = _MutableThread(id=thread_id, title=title, created_at=_now())
            self._threads[thread_id] = record
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def list_threads(self) -> list[ThreadRecord]:
        # WHY copy semantics: the returned list is a fresh
        # list of fresh ``ThreadRecord`` (frozen dataclasses).
        # A caller cannot mutate the store by mutating the
        # return value. Sort happens under the lock so the
        # ordering reflects a consistent snapshot.
        with self._lock:
            rows = sorted(
                self._threads.values(), key=lambda r: r.created_at, reverse=True
            )
            return [
                ThreadRecord(id=r.id, title=r.title, created_at=r.created_at)
                for r in rows
            ]

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        # WHY the lock covers the dict lookup: a concurrent
        # ``delete_thread`` for the same id could otherwise race
        # the .get() and let the caller see a row that is about
        # to be deleted. Holding the lock for the duration of
        # the lookup keeps the miss/hit decision atomic.
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                return None
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        # WHY the lock spans (lookup, mutate, return): a
        # concurrent ``delete_thread`` for the same id must NOT
        # race the lookup and produce a half-applied title on
        # a doomed cell. Same invariant the rest of the store
        # holds — one lock, atomic read-then-write.
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                raise ThreadNotFoundError(
                    f"cannot update title on unknown thread {thread_id!r}"
                )
            record.title = title
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def delete_thread(self, thread_id: str) -> bool:
        # WHY a single ``del`` (no run cleanup): runs live on the
        # ``_MutableThread`` cell, so dropping the cell drops
        # everything atomically — no partial state where the
        # metadata is gone but a run is still attached. The
        # Postgres path achieves the same invariant via FK
        # ``ON DELETE CASCADE``.
        with self._lock:
            if thread_id not in self._threads:
                return False
            del self._threads[thread_id]
            return True

    # -- runs --

    def append_run(self, record: RunRecord) -> None:
        # WHY the lock covers BOTH the dict lookup AND the
        # list append: a concurrent ``delete_thread`` on the
        # same id must NOT leave a partial run record (the
        # thread gone, the run orphaned). Holding the lock
        # across the two operations serializes delete vs
        # append. ``_validate_status`` enforces the closed
        # enum at this seam so a typo at the call site fails
        # here, not at the next reader (Standards #9).
        _validate_status(record.status)
        with self._lock:
            thread = self._threads.get(record.thread_id)
            if thread is None:
                raise ThreadNotFoundError(
                    f"cannot append run to unknown thread {record.thread_id!r}"
                )
            thread.runs.append(record)

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        # WHY deepcopy (not just ``list(thread.runs)``):
        # ``RunRecord`` is a frozen dataclass, but its
        # ``citations`` and ``tool_summaries`` fields are
        # mutable lists. A caller could mutate those lists
        # through the returned record and corrupt the store.
        # ``copy.deepcopy`` hands back an independent object
        # so the docstring promise — "caller cannot mutate
        # store state through the copy" — is enforced, not
        # merely stated.
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            return [copy.deepcopy(run) for run in thread.runs]


@dataclass
class _MutableThread:
    """Internal mutable cell so runs can be appended post-create."""

    id: str
    title: str
    created_at: datetime
    runs: list[RunRecord] = field(default_factory=list)


# ----- postgres implementation -----


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_thread (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_run (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    question          TEXT NOT NULL,
    status            TEXT NOT NULL,
    status_reason     TEXT NOT NULL DEFAULT '',
    answer            TEXT NOT NULL DEFAULT '',
    citations         JSONB NOT NULL DEFAULT '[]'::jsonb,
    thinking_text     TEXT NOT NULL DEFAULT '',
    thinking_duration_ms INTEGER NOT NULL DEFAULT 0,
    tool_summaries    JSONB NOT NULL DEFAULT '[]'::jsonb,
    assistant_message TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- T4 (spec line 38): persisted rows are by definition terminal;
    -- the column exists so the storage shape matches RunRecord. The
    -- LIVE running signal is the per-thread lock probe, never this.
    in_flight         BOOLEAN NOT NULL DEFAULT FALSE,
    -- Soft-Track T5: auditable track decision for this run_id.
    track_decision    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Mid-flight stream error taxonomy on refuse rows
    -- (auth / network / timeout / rate_limit / not_found /
    -- server / unknown). Empty on rows whose refusal came from
    -- the gate rather than the stream engine.
    status_classified TEXT NOT NULL DEFAULT '',
    -- T3 (enterprise-kb Focus 1): fin.comparison envelopes from
    -- this run's compare_facts calls. JSONB so the turn table can
    -- render structure instead of re-parsing prose.
    comparisons       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Trust (#135): the locators this run retrieved or read, not
    -- only the verifier-passed citations.
    evidence_locators JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- T6: structured host analysis envelopes.
    analysis_results  JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- ADR 0089: the run's visual claims, kept off the citations channel
    -- (a visual observation is never a source quote).
    visual_claims     JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- T2 column migration: add ``assistant_message`` to existing
-- ``fin_run`` tables without breaking fresh boots. ``ADD COLUMN
-- IF NOT EXISTS`` is supported since Postgres 9.6; the column is
-- NULL-safe via the table default so old rows read back as the
-- empty string.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS assistant_message TEXT NOT NULL DEFAULT '';

-- T3 column migration: pre-T3 tables may lack ``thinking_text`` /
-- ``tool_summaries``. Both are listed in the CREATE TABLE above
-- for fresh installs; the ALTERs cover existing prod tables so
-- the durable runtime does not raise on the first T3 INSERT.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS thinking_text TEXT NOT NULL DEFAULT '';
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS tool_summaries JSONB NOT NULL DEFAULT '[]'::jsonb;

-- T4 column migration: pre-T4 tables lack ``in_flight``. Persisted
-- rows are terminal so the default ``FALSE`` is always correct.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS in_flight BOOLEAN NOT NULL DEFAULT FALSE;

-- T4 (#37) column migration: thinking duration for the post-run
-- collapse label. 0 = no thinking streamed.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS thinking_duration_ms INTEGER NOT NULL DEFAULT 0;

-- Soft-Track T5: track_decision on existing tables.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS track_decision JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Stream error taxonomy: classify mid-flight failures (auth /
-- network / timeout / rate_limit / not_found / server / unknown)
-- so the SPA can render a precise label rather than one opaque
-- English sentence. Empty on rows that did not fail mid-stream.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS status_classified TEXT NOT NULL DEFAULT '';

-- T3 (enterprise-kb Focus 1) column migration: structured
-- comparison envelopes on existing tables. Pre-T3 rows read back
-- as ``[]`` — those turns never called compare_facts.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS comparisons JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Trust (#135, spec SC8): run-scoped evidence locators on existing
-- tables. Pre-migration rows read back as ``[]`` — the run happened
-- before the column existed, so "no recorded locators" is the only
-- honest value.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS evidence_locators JSONB NOT NULL DEFAULT '[]'::jsonb;

-- T6: analysis-depth envelopes on existing tables.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS analysis_results JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ADR 0089: visual-claim rows on existing tables. Pre-migration rows read
-- back as ``[]`` — the run predates the typed visual-evidence surface, so
-- "no claim" is the only honest value. ``[]`` (not NULL) keeps every
-- consumer free of an absence branch.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS visual_claims JSONB NOT NULL DEFAULT '[]'::jsonb;
"""


# ----- durable-write evidence guard -----


def _store_admission_context(record: "RunRecord") -> AdmissionContext:
    """Seed the context-dependent checks from the record's own rows.

    Delegates to the validator home's :func:`seed_context_from_rows` —
    the posture (and its never-hash rule) has one authority, not one
    copy per boundary. See that function for WHY self-seeding is right
    at a durable-write replay.
    """
    return seed_context_from_rows(
        citation_rows=record.citations,
        visual_claim_rows=record.visual_claims,
    )


def _is_pre_e5_visual_row(row: Any) -> bool:
    """The frozen legacy visual shape: exactly the fourteen keys the
    visual-claim SSOT names, under the visual kind. Writers before the
    schema cutover legitimately emit this today, so the durable write
    must not refuse it."""
    return (
        isinstance(row, Mapping)
        and frozenset(row) == frozenset(VISUAL_CLAIM_FIELDS)
        and row.get("kind") == VISUAL_EVIDENCE_KIND
    )


def _outgoing_evidence_error(record: "RunRecord") -> "EvidenceContractWriteError | None":
    """Validate the run's evidence at the durable boundary before storage.

    Each row is first held to the home contract (spec §4.4 channel
    rules), then — only when the home refuses it — to the frozen legacy
    shapes §7 settles for pre-cutover writers. Admission already gates
    fresh runs against the same rules, so this guard refuses nothing
    today's admitted runs persisted; what it stops are corrupt or
    out-of-set rows that would otherwise live in the JSONB column and
    poison every later replay.
    """
    ctx = _store_admission_context(record)
    for index, row in enumerate(record.citations):
        result = validate_evidence(
            {
                "evidence_schema": CITATION_CHANNEL_SCHEMA,
                EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
                "citations": [row],
            },
            ctx,
        )
        if result.accepted:
            continue
        # The §7 grace covers a legacy SHAPE, never a §3 content bound
        # (see _SHAPE_GRACE_EXCEPTIONS): a shape-legal row that overflows
        # is refused here, not persisted for every replay to re-refuse.
        if (
            project_stored_citation(row) is not None
            and (result.refusal is None or result.refusal.code not in _SHAPE_GRACE_EXCEPTIONS)
        ):
            continue
        if result.refusal is None:
            # Unreachable by construction (not accepted ⇒ a typed
            # refusal); an explicit raise keeps the invariant enforced
            # under ``python -O`` where ``assert`` vanishes.
            raise RuntimeError(f"citation {index}: refused outcome carries no code")
        return EvidenceContractWriteError(
            result.refusal.code, f"citation {index}: {result.refusal.message}"
        )
    for index, row in enumerate(record.visual_claims):
        result = validate_evidence({"visual_claims": [row]}, ctx)
        if result.accepted:
            continue
        if (
            _is_pre_e5_visual_row(row)
            and (result.refusal is None or result.refusal.code not in _SHAPE_GRACE_EXCEPTIONS)
        ):
            continue
        if result.refusal is None:
            raise RuntimeError(f"visual claim {index}: refused outcome carries no code")
        return EvidenceContractWriteError(
            result.refusal.code, f"visual claim {index}: {result.refusal.message}"
        )
    return None


class PostgresThreadStore:
    """psycopg-backed store against the shared ``FIN_CHECKPOINT_URL``.

    Each public call opens its own short-lived connection — the v1
    workspace is a local operator server with low traffic, and this
    avoids holding a second long-lived pool next to the checkpointer's.
    Schema creation is idempotent and runs once per construction.
    """

    def __init__(self, connection_url: str) -> None:
        self._url = connection_url
        self._setup_lock = threading.Lock()
        with self._setup_lock:
            with self._connect() as conn:
                conn.execute(_SCHEMA_SQL)
                conn.commit()

    def _connect(self) -> Any:
        return psycopg.connect(self._url)

    @contextmanager
    def _guarded(self, action: str) -> Iterator[Any]:
        """Every public call's driver work runs through here.

        A store failure the contract does not name becomes the typed
        ``persistence_unavailable`` outcome instead of a bare driver
        crash. FK violations keep their operation's own meaning (the
        thread is gone) and pass through for the caller to map.
        """
        try:
            with self._connect() as conn:
                yield conn
        except psycopg.errors.ForeignKeyViolation:
            raise
        except psycopg.Error as exc:
            raise PersistenceUnavailableError(
                f"{action}: the thread store could not be reached"
            ) from exc

    @staticmethod
    def _row_to_thread(row: Any) -> ThreadRecord:
        return ThreadRecord(id=row[0], title=row[1], created_at=row[2])

    @staticmethod
    def _row_to_run(row: Any) -> RunRecord:
        import json

        # WHY ``list(...)`` on the already-deserialized
        # path: the rows coming back from psycopg can be
        # either JSON strings (driver default) or already
        # ``list`` instances depending on the codec; both
        # branches coerce to a fresh list so the
        # caller cannot reach into the driver's buffer.
        # ``list_runs`` follows up with a deepcopy on the
        # outer record for full isolation.
        stored_citations = json.loads(row[6]) if isinstance(row[6], str) else list(row[6])
        # WHY stored evidence is untrusted here: the JSONB column keeps
        # rows from three writer generations and a blind coercion would
        # default a contradictory or corrupt row into looking valid. A
        # row matching the frozen v0 shape is projected to what it
        # names itself as; a row matching no frozen shape is kept
        # verbatim so the read boundary refuses it per-record, never
        # silently dropping or blanking it.
        citations = copy.deepcopy(
            [
                projected
                if (projected := project_stored_citation(entry)) is not None
                else entry
                for entry in stored_citations
            ]
        )
        return RunRecord(
            id=row[0],
            thread_id=row[1],
            question=row[2],
            status=_validate_status(row[3]),
            status_reason=row[4],
            answer=row[5],
            citations=citations,
            thinking_text=row[7],
            tool_summaries=copy.deepcopy(
                json.loads(row[8]) if isinstance(row[8], str) else list(row[8])
            ),
            # ``assistant_message`` defaults to "" on the Python
            # side if the row predates the T2 migration; the SQL
            # column also defaults to '' for fresh installs.
            # We coerce ``None`` to ``""`` defensively in case
            # an older row predates the ADD COLUMN.
            assistant_message=row[9] or "",
            created_at=row[10],
            # T4: coerce a pre-migration NULL to False defensively.
            in_flight=bool(row[11]) if len(row) > 11 else False,
            # T4 (#37): duration column; 0 when the row
            # predates the migration (no measurable thinking).
            thinking_duration_ms=int(row[12] or 0) if len(row) > 12 else 0,
            # Soft-Track T5: empty dict when pre-migration / unset.
            track_decision=copy.deepcopy(
                (
                    json.loads(row[13])
                    if isinstance(row[13], str)
                    else dict(row[13] or {})
                )
                if len(row) > 13
                else {}
            ),
            # Stream error taxonomy (auth / network / ... / unknown).
            # Empty when pre-migration; rows that did not fail
            # mid-flight never set this column.
            status_classified=(row[14] or "") if len(row) > 14 else "",
            # T3: ``[]`` when the row predates the migration or the
            # run never called ``compare_facts``.
            comparisons=copy.deepcopy(
                (
                    json.loads(row[15])
                    if isinstance(row[15], str)
                    else list(row[15] or [])
                )
                if len(row) > 15
                else []
            ),
            # Trust (#135): empty list when pre-migration / the run
            # touched no locator.
            evidence_locators=copy.deepcopy(
                (
                    json.loads(row[16])
                    if isinstance(row[16], str)
                    else list(row[16] or [])
                )
                if len(row) > 16
                else []
            ),
            analysis_results=copy.deepcopy(
                (
                    json.loads(row[17])
                    if isinstance(row[17], str)
                    else list(row[17] or [])
                )
                if len(row) > 17
                else []
            ),
            # ADR 0089: empty list when pre-migration / the run produced
            # no visual claim. Coerced to ``[]`` (never ``None``) so the
            # citations-vs-claims separation holds for every reader.
            visual_claims=copy.deepcopy(
                (
                    json.loads(row[18])
                    if isinstance(row[18], str)
                    else list(row[18] or [])
                )
                if len(row) > 18
                else []
            ),
        )

    # -- threads --

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        # WHY ``ON CONFLICT DO NOTHING`` would mask the
        # duplicate-id case in the in-memory store. The v1
        # caller never reuses an id (id is UUID4), so the
        # simpler INSERT is sufficient; PK violation here is
        # a programmer error and surfaces as an exception.
        with self._guarded("create thread") as conn:
            thread_id = id or str(uuid.uuid4())
            row = conn.execute(
                "INSERT INTO fin_thread (id, title) VALUES (%s, %s) "
                "RETURNING id, title, created_at",
                (thread_id, title),
            ).fetchone()
            conn.commit()
            if row is None:
                # An INSERT ... RETURNING that yields no row is a driver
                # anomaly, not a contract outcome: name it as the store
                # outage it is. WHY an explicit raise and not an assert:
                # ``python -O`` strips asserts, and a silently-massaged
                # None would reach ``_row_to_thread`` unpacking a wrong
                # arity further down.
                raise PersistenceUnavailableError(
                    "create thread: INSERT ... RETURNING yielded no row"
                )
            return self._row_to_thread(row)

    def list_threads(self) -> list[ThreadRecord]:
        # WHY ``ORDER BY created_at DESC``: matches the
        # in-memory implementation's ordering so the sidebar
        # is rendered the same way across deployments.
        # Fresh-copy semantics: each row is mapped to a new
        # ``ThreadRecord`` dataclass, so a caller cannot
        # mutate the row by mutating the return value.
        with self._guarded("list threads") as conn:
            rows = conn.execute(
                "SELECT id, title, created_at FROM fin_thread "
                "ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_thread(r) for r in rows]

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        # WHY ``None`` on miss (vs ``ThreadNotFoundError``):
        # ``get_thread`` is the lookup-or-miss seam; the HTTP
        # layer translates miss to 404. Reserve the exception
        # for operations that REQUIRE an existing row
        # (``append_run`` / ``list_runs``).
        with self._guarded("get thread") as conn:
            row = conn.execute(
                "SELECT id, title, created_at FROM fin_thread WHERE id = %s",
                (thread_id,),
            ).fetchone()
            return self._row_to_thread(row) if row else None

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        # WHY ``UPDATE ... RETURNING``: a single round-trip
        # both mutates the row and yields the refreshed
        # ``ThreadRecord`` so the HTTP layer can serialize the
        # updated title without a follow-up ``get_thread``.
        # Unknown id maps to ``ThreadNotFoundError`` so the
        # caller sees the same exception the in-memory store
        # raises (the HTTP layer translates it to 404).
        with self._guarded("update thread title") as conn:
            row = conn.execute(
                "UPDATE fin_thread SET title = %s WHERE id = %s "
                "RETURNING id, title, created_at",
                (title, thread_id),
            ).fetchone()
            conn.commit()
            if row is None:
                raise ThreadNotFoundError(
                    f"cannot update title on unknown thread {thread_id!r}"
                )
            return self._row_to_thread(row)

    def delete_thread(self, thread_id: str) -> bool:
        # WHY FK cascade does the run cleanup: the schema
        # declares ``fin_run.thread_id`` REFERENCES
        # ``fin_thread(id) ON DELETE CASCADE``, so the row
        # count returned by this DELETE only reflects the
        # thread row, not the cascaded run rows. The HTTP
        # layer relies on the cascade to keep
        # ``GET /api/threads/{id}`` from resurrecting orphan
        # runs after a delete.
        with self._guarded("delete thread") as conn:
            cur = conn.execute(
                "DELETE FROM fin_thread WHERE id = %s", (thread_id,)
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    # -- threads (continued) --

    # -- runs --

    def append_run(self, record: RunRecord) -> None:
        # WHY validate the status at the seam: the in-memory
        # and Postgres implementations share the
        # ``RunStatus`` enum (``Literal["done","refuse","step_cap"]``)
        # via ``_validate_status``. Failing fast here keeps
        # a typo at the call site from silently writing a
        # bogus status row.
        # WHY the FK violation is mapped to ``ThreadNotFoundError``:
        # the in-memory store raises that for an unknown
        # ``thread_id``; surfacing the same exception type
        # across implementations lets the HTTP layer
        # uniformly treat it as "thread gone" without
        # branching on the underlying engine.
        # WHY evidence is validated here, before storage: durable
        # storage is the last point a corrupt row can be stopped
        # cheaply. Once it is in the JSONB column every replay has to
        # carry a refusal for it; refused here it never enters history.
        import json

        _validate_status(record.status)
        refusal = _outgoing_evidence_error(record)
        if refusal is not None:
            raise refusal
        try:
            with self._guarded("append run") as conn:
                conn.execute(
                    "INSERT INTO fin_run (id, thread_id, question, status, "
                    "status_reason, answer, citations, thinking_text, "
                    "tool_summaries, assistant_message, created_at, in_flight, "
                    "thinking_duration_ms, track_decision, status_classified, "
                    "comparisons, evidence_locators, analysis_results, "
                    "visual_claims) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s)",
                    (
                        record.id,
                        record.thread_id,
                        record.question,
                        record.status,
                        record.status_reason,
                        record.answer,
                        json.dumps(record.citations),
                        record.thinking_text,
                        json.dumps(record.tool_summaries),
                        record.assistant_message,
                        record.created_at,
                        # Persisted rows are terminal; always False.
                        False,
                        record.thinking_duration_ms,
                        json.dumps(record.track_decision),
                        record.status_classified,
                        json.dumps(record.comparisons),
                        json.dumps(record.evidence_locators),
                        json.dumps(record.analysis_results),
                        json.dumps(record.visual_claims),
                    ),
                )
                conn.commit()
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ThreadNotFoundError(
                f"cannot append run to unknown thread {record.thread_id!r}"
            ) from exc

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        # WHY existence check FIRST: ``fin_run.thread_id`` is
        # a FK so an unknown thread yields zero rows. The
        # explicit ``SELECT 1`` lets us raise
        # ``ThreadNotFoundError`` (matching the in-memory
        # store's contract) instead of returning ``[]``,
        # which the HTTP layer would translate to a 200 with
        # an empty turn table — a different shape than 404.
        # WHY deepcopy on the returned rows: same invariant
        # as the in-memory path — caller cannot mutate
        # store state through the returned records.
        with self._guarded("list runs") as conn:
            exists = conn.execute(
                "SELECT 1 FROM fin_thread WHERE id = %s", (thread_id,)
            ).fetchone()
            if exists is None:
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            rows = conn.execute(
                "SELECT id, thread_id, question, status, status_reason, "
                "answer, citations, thinking_text, tool_summaries, "
                "assistant_message, created_at, in_flight, "
                "thinking_duration_ms, track_decision, status_classified, "
                "comparisons, evidence_locators, analysis_results, "
                "visual_claims "
                "FROM fin_run WHERE thread_id = %s ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
            # ``_row_to_run`` already deep-copies every mutable field
            # off the driver buffer (citations / tool_summaries /
            # track_decision / comparisons / evidence_locators), so
            # one outer deepcopy per row is enough.
            return [copy.deepcopy(self._row_to_run(r)) for r in rows]


# ----- env-based default selection -----


# Module-level cache so ``default_thread_store()`` returns the
# SAME store across calls within a process. The in-memory store is
# pure state; without caching, every FastAPI dependency resolution
# would mint a fresh dict and threads created in one request would
# vanish from the next. The Postgres store caches against URL
# identity so two callers asking for the same URL share the
# already-set-up schema state.
_DEFAULT_STORE_CACHE: dict[str, ThreadStore] = {}


def default_thread_store(env: Any = None) -> ThreadStore:
    """Pick the right store implementation based on the environment.

    Matches the existing ``compile_for_runtime`` pattern:
    ``FIN_CHECKPOINT_URL`` set -> Postgres store against that URL;
    unset -> in-memory store. Tests inject via FastAPI's
    ``dependency_overrides`` so neither branch matters there.

    The selection is CACHED: subsequent calls return the same
    instance so thread writes from one request are visible to
    reads from the next. This matches the v1 process model where
    one ``uvicorn fin.host.http:app`` process owns the metadata
    store for its lifetime.
    """
    import os

    source = env if env is not None else os.environ
    url = (source.get("FIN_CHECKPOINT_URL") or "").strip()
    cache_key = url or "<memory>"
    cached = _DEFAULT_STORE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if not url:
        cached = InMemoryThreadStore()
    else:
        cached = PostgresThreadStore(url)
    _DEFAULT_STORE_CACHE[cache_key] = cached
    return cached


# Re-exported so callers can ``isinstance`` without importing psycopg.
__all__ = [
    "EvidenceContractWriteError",
    "InMemoryThreadStore",
    "PersistenceUnavailableError",
    "PostgresThreadStore",
    "RunRecord",
    "RunStatus",
    "ThreadNotFoundError",
    "ThreadRecord",
    "ThreadStore",
    "ThreadStoreError",
    "default_thread_store",
    "project_stored_citation",
]
