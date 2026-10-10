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
import json
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Protocol, Sequence

import psycopg

from fin.host.citation_gate import COMPANY_KIND, DATABASE_ROW_KIND
from fin.host.evidence_contract import (
    CITATION_CHANNEL_SCHEMA,
    CITATION_OVERFLOW,
    CITATION_SNAPSHOT_KEY,
    DATABASE_ROW_OVERFLOW,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_FIELD,
    MALFORMED_EVIDENCE_CONTRACT,
    AdmissionContext,
    VISUAL_CLAIM_OVERFLOW,
    seed_context_from_rows,
    validate_evidence,
)
from fin.host.structured_evidence import project_stored_structured_evidence
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
    # structured-corpus-evidence: the run's bounded structured snapshots,
    # one per cited row or count receipt, each a
    # ``fin.evidence.structured_snapshot`` value plus its recomputable
    # digest. Kept SEPARATE from ``citations`` for the same reason visual
    # claims are: a structured value supports a structured claim and can
    # never authorize a quotation, so it must not ride the quote channel.
    # ``[]`` (never ``None``) so a consumer never branches on absence.
    structured_evidence: list[dict[str, Any]] = field(default_factory=list)


# ----- EP9/EP10 invalidation checkpoint state -----


@dataclass(frozen=True)
class EvidenceInvalidationCheckpoint:
    """One consumer's durable position in the EP8 invalidation stream.

    ``applied_checkpoint`` (P2-D1 §2.4 ``applied_checkpoint``) is the cursor
    covering events whose local scrub has COMMITTED; it advances atomically
    with the scrub and never from an acknowledgement. ``acked_checkpoint`` is
    the cursor the backend accepted, so a crash between the two redelivers
    from the right place instead of skipping or replaying from the start.
    ``last_seq`` is the per-consumer gapless sequence D1 §2.2 orders the
    stream by, kept as the decidable form of "did I miss something" that an
    opaque checkpoint cannot express. Empty/zero mean "never drained", never
    a fabricated position.
    """

    consumer_id: str
    acked_checkpoint: str = ""
    applied_checkpoint: str = ""
    last_seq: int = 0


@dataclass(frozen=True)
class EvidenceRecoveryState:
    """Whether this consumer's retained evidence is currently withheld.

    T33's gate. It is DURABLE and it lives on the same row as the watermark,
    for two reasons that are properties rather than preferences:

    * **Durable, because a restart is the case.** The withhold exists to cover
      the window between discovering an event/ack gap and finishing recovery. A
      process-local flag would be lost exactly when it is needed most — a
      restart after a gap would clear the withhold and re-expose the very
      snapshots recovery was supposed to protect.
    * **On the watermark row, so the invariant is checkable in one place.** A
      consumer that is ``withheld`` has, by construction, an un-advanced
      acknowledgement: the drain refuses a stale cursor and never writes
      ``acked_checkpoint`` from a gap, and only the sweep clears this flag.

    ``reason`` is the human-readable cause recorded when the gate closed. It is
    diagnostic text, never an input to a decision.
    """

    consumer_id: str
    withheld: bool = False
    reason: str = ""


@dataclass(frozen=True)
class EvidenceScrubOutcome:
    """What storage removed, plus the confirmation the EP9 ack depends on.

    ``remaining_entries`` is read back INSIDE the same transaction as
    the scrub: a non-zero count means storage could not confirm the
    payload is gone, so the caller must refuse rather than acknowledge.
    """

    scrubbed_entries: int
    scrubbed_runs: int
    remaining_entries: int


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

    # -- EP9/EP10 invalidation checkpoint (structured_corpus_evidence) --

    def read_invalidation_checkpoint(self, consumer_id: str) -> EvidenceInvalidationCheckpoint:
        """This consumer's durable stream position, empty strings for a
        consumer that has never drained. A lookup miss is NOT an error:
        "no checkpoint yet" is the normal first-drain state.
        """
        ...

    def scrub_structured_evidence_payloads(
        self,
        *,
        consumer_id: str,
        evidence_refs: Sequence[str],
        next_checkpoint: str,
        last_seq: int,
    ) -> EvidenceScrubOutcome:
        """Remove every stored ``structured_evidence`` entry whose snapshot
        names one of ``evidence_refs``, then advance the watermark — all in
        ONE transaction, so a failure leaves both untouched.

        The write is scoped to the evidence column on purpose: routing it
        through a whole-row rewrite would re-run the durable write guard
        over the entire run record and could refuse (or mangle) an
        unrelated lane's payload.
        """
        ...

    def advance_invalidation_acknowledgement(
        self, consumer_id: str, checkpoint: str
    ) -> None:
        """Record that the backend accepted ``checkpoint``. Called only
        AFTER the scrub committed, never inside the same transaction.
        """
        ...

    # -- EP10 recovery gate (T33) --

    def read_recovery_state(self, consumer_id: str) -> EvidenceRecoveryState:
        """This consumer's recovery gate; never withheld for a consumer that
        has never seen a gap.

        A lookup miss is NOT an error, for the same reason the checkpoint's is
        not: "no gate yet" is the normal pre-gap state, and it is the SAFE one
        because it means no gap has been observed.
        """
        ...

    def set_recovery_withheld(self, consumer_id: str, reason: str) -> None:
        """Close the gate: every retained reference for this consumer is
        unavailable until a sweep releases it.

        Idempotent, and it does NOT touch either watermark. The gap has already
        been refused by the time this is called, so the resume cursor must stay
        exactly where the previous acknowledgement left it — a stale position
        may never advance acknowledgement, and closing a gate is not a reason to
        move one.
        """
        ...

    def set_recovery_released(self, consumer_id: str) -> None:
        """Open the gate. Called by the revalidation sweep and by nothing else,
        after storage confirmed the denied payloads are gone.
        """
        ...

    def scrub_retained_evidence_payloads(
        self, evidence_refs: Sequence[str]
    ) -> EvidenceScrubOutcome:
        """Remove every stored ``structured_evidence`` entry naming one of
        ``evidence_refs``, and confirm it — WITHOUT touching any watermark.

        WHY this is separate from :meth:`scrub_structured_evidence_payloads`:
        that method is half of ADR 0099's two-phase machine, and its
        ``next_checkpoint`` / ``last_seq`` arguments are the batch's own
        watermark values. A recovery sweep has no batch, so it has no
        checkpoint to write and no ``seq`` it can honestly claim to have
        applied. Reusing that method here would mean inventing a cursor and
        advancing a watermark the sweep never earned — the exact defect T33
        forbids. Scrubbing is the only half the sweep needs, and taking only
        that half is what leaves the acknowledgement entirely in the drain's
        hands.
        """
        ...


# ----- in-memory implementation -----


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stored_evidence_ref(row: Any) -> str | None:
    """The ``evidence_ref`` one stored structured row names, else ``None``.

    Read BY SHAPE, not through ``project_stored_structured_evidence``:
    a corrupt row (wrong digest, off-shape selector) must still be
    scrubbable by an invalidation event, and projecting it would return
    ``None`` for exactly the rows an invalidation most needs to remove.
    """
    if not isinstance(row, Mapping):
        return None
    snapshot = row.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    ref = snapshot.get("evidence_ref")
    return ref if isinstance(ref, str) and ref else None


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
        self._invalidation_checkpoints: dict[str, EvidenceInvalidationCheckpoint] = {}
        self._recovery_states: dict[str, EvidenceRecoveryState] = {}

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

    # -- EP9/EP10 invalidation checkpoint --

    def read_invalidation_checkpoint(self, consumer_id: str) -> EvidenceInvalidationCheckpoint:
        with self._lock:
            stored = self._invalidation_checkpoints.get(consumer_id)
        if stored is None:
            return EvidenceInvalidationCheckpoint(consumer_id=consumer_id)
        return stored

    def scrub_structured_evidence_payloads(
        self,
        *,
        consumer_id: str,
        evidence_refs: Sequence[str],
        next_checkpoint: str,
        last_seq: int,
    ) -> EvidenceScrubOutcome:
        targets = set(evidence_refs)
        # WHY plan-then-apply: the durable path does this inside one
        # transaction, and a mid-apply failure here must not leave a
        # half-scrubbed store either. Nothing is mutated until every
        # run row has been inspected.
        plan: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        scrubbed_entries = 0
        scrubbed_runs = 0
        with self._lock:
            for thread in self._threads.values():
                for run in thread.runs:
                    kept: list[dict[str, Any]] = []
                    removed = 0
                    for row in run.structured_evidence:
                        ref = _stored_evidence_ref(row)
                        if ref is not None and ref in targets:
                            removed += 1
                            continue
                        kept.append(row)
                    if removed:
                        plan.append((run.structured_evidence, kept))
                        scrubbed_entries += removed
                        scrubbed_runs += 1
            for current, kept in plan:
                current[:] = kept
            # WHY zero without a re-read: ``kept`` was built by dropping every
            # row whose ref is a target, so nothing matching can survive the
            # apply above. Only the durable store can fail to confirm, which is
            # why it re-counts inside the transaction.
            remaining_entries = 0
            previous = self._invalidation_checkpoints.get(consumer_id)
            # WHY max() on last_seq: a crash between the two phases makes the
            # backend redeliver events whose seq is already applied. The drain
            # refuses a batch ending behind this watermark, so this only
            # defends the orderable column against a caller that bypassed it;
            # ``applied_checkpoint`` is opaque and cannot be ordered here at
            # all.
            self._invalidation_checkpoints[consumer_id] = EvidenceInvalidationCheckpoint(
                consumer_id=consumer_id,
                acked_checkpoint=previous.acked_checkpoint if previous is not None else "",
                applied_checkpoint=next_checkpoint,
                last_seq=max(last_seq, previous.last_seq if previous is not None else 0),
            )
        return EvidenceScrubOutcome(
            scrubbed_entries=scrubbed_entries,
            scrubbed_runs=scrubbed_runs,
            remaining_entries=remaining_entries,
        )

    def advance_invalidation_acknowledgement(
        self, consumer_id: str, checkpoint: str
    ) -> None:
        with self._lock:
            previous = self._invalidation_checkpoints.get(consumer_id)
            self._invalidation_checkpoints[consumer_id] = EvidenceInvalidationCheckpoint(
                consumer_id=consumer_id,
                acked_checkpoint=checkpoint,
                applied_checkpoint=previous.applied_checkpoint if previous is not None else "",
                last_seq=previous.last_seq if previous is not None else 0,
            )

    # -- EP10 recovery gate (T33) --

    def read_recovery_state(self, consumer_id: str) -> EvidenceRecoveryState:
        with self._lock:
            stored = self._recovery_states.get(consumer_id)
        if stored is None:
            return EvidenceRecoveryState(consumer_id=consumer_id)
        return stored

    def set_recovery_withheld(self, consumer_id: str, reason: str) -> None:
        with self._lock:
            self._recovery_states[consumer_id] = EvidenceRecoveryState(
                consumer_id=consumer_id, withheld=True, reason=reason
            )

    def set_recovery_released(self, consumer_id: str) -> None:
        with self._lock:
            self._recovery_states[consumer_id] = EvidenceRecoveryState(
                consumer_id=consumer_id, withheld=False, reason=""
            )

    def scrub_retained_evidence_payloads(
        self, evidence_refs: Sequence[str]
    ) -> EvidenceScrubOutcome:
        """Delete the named payloads and confirm. No watermark is read or
        written here — see the protocol docstring for why the sweep may not
        touch one.
        """

        targets = set(evidence_refs)
        plan: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        scrubbed_entries = 0
        scrubbed_runs = 0
        with self._lock:
            for thread in self._threads.values():
                for run in thread.runs:
                    kept: list[dict[str, Any]] = []
                    removed = 0
                    for row in run.structured_evidence:
                        ref = _stored_evidence_ref(row)
                        if ref is not None and ref in targets:
                            removed += 1
                            continue
                        kept.append(row)
                    if removed:
                        plan.append((run.structured_evidence, kept))
                        scrubbed_entries += removed
                        scrubbed_runs += 1
            for current, kept in plan:
                current[:] = kept
        return EvidenceScrubOutcome(
            scrubbed_entries=scrubbed_entries,
            scrubbed_runs=scrubbed_runs,
            # WHY zero without a re-read: ``kept`` was built by dropping every
            # entry whose ref is a target, so nothing matching survives the
            # apply above. Only the durable store can fail to confirm, which is
            # why it re-counts inside its own transaction.
            remaining_entries=0,
        )


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
    visual_claims     JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- structured-corpus-evidence: the run's bounded structured-result
    -- snapshots, kept off the citations channel for the same reason
    -- visual claims are.
    structured_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- structured-corpus-evidence EP9/EP10: the per-consumer position in the
-- backend invalidation stream. ``applied_checkpoint`` is P2-D1 §2.4's
-- watermark and moves only with a committed scrub; ``acked_checkpoint`` is
-- the cursor the backend accepted, which is the resume point after a crash
-- between the two phases; ``last_seq`` carries D1 §2.2's per-consumer
-- gapless order so a skipped window is decidable without parsing an opaque
-- checkpoint.
CREATE TABLE IF NOT EXISTS fin_evidence_invalidation_checkpoint (
    consumer_id         TEXT PRIMARY KEY,
    applied_checkpoint  TEXT NOT NULL DEFAULT '',
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seq            BIGINT NOT NULL DEFAULT 0,
    acked_checkpoint    TEXT NOT NULL DEFAULT ''
);

-- T33 column migration: the recovery gate. A gap closes it and only the
-- revalidation sweep opens it, so it rides the same row as the watermark it
-- constrains. The default is FALSE (not withheld) because "never drained" is
-- not "mid-recovery": a consumer that has not seen a gap has nothing to
-- withhold.
ALTER TABLE fin_evidence_invalidation_checkpoint
    ADD COLUMN IF NOT EXISTS recovery_withheld BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fin_evidence_invalidation_checkpoint
    ADD COLUMN IF NOT EXISTS recovery_reason TEXT NOT NULL DEFAULT '';

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

-- structured-corpus-evidence: same rule as visual_claims — a pre-lane row
-- reads back as ``[]``, which is the only honest value for "this run
-- carried no structured snapshot".
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS structured_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;
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


def _jsonb_list(row: Sequence[object], index: int) -> list[Any]:
    """One JSONB list column, decoded, deep-copied, never ``None``.

    Four list columns share this shape, and they previously each carried
    their own copy of the same nine lines. The two branches are both
    needed: a fake driver yields a JSON string, psycopg yields an already
    decoded list. A short row from a pre-migration SELECT that did not
    know the column reads as ``[]``; an existing column with a NULL or
    non-list value is persisted corruption, not evidence that the run
    carried none.
    """
    if len(row) <= index:
        # EXIT: legacy SELECT tuples omit columns added after their schema cutover.
        return []
    raw = row[index]
    try:
        decoded: Any = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise PersistenceUnavailableError(
            f"list runs: JSONB list column at row index {index} could not be decoded"
        ) from exc
    if not isinstance(decoded, list):
        raise PersistenceUnavailableError(
            f"list runs: JSONB list column at row index {index} is not an array"
        )
    return copy.deepcopy(decoded)


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
    for validator in (_outgoing_citation_error, _outgoing_visual_error):
        error = validator(record, ctx)
        if error is not None:
            return error
    return _outgoing_structured_error(record)


def _outgoing_citation_error(
    record: "RunRecord", ctx: AdmissionContext,
) -> "EvidenceContractWriteError | None":
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
    return None


def _outgoing_visual_error(
    record: "RunRecord", ctx: AdmissionContext,
) -> "EvidenceContractWriteError | None":
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
    # The structured lane has no grace path: a stored snapshot whose digest
    # does not recompute is corruption, and a corrupted snapshot must never
    # reach a replay that would present it as retained evidence.
    for index, row in enumerate(record.structured_evidence):
        if project_stored_structured_evidence(row) is not None:
            continue
        return EvidenceContractWriteError(
            MALFORMED_EVIDENCE_CONTRACT,
            f"structured evidence {index}: stored snapshot is not the frozen shape",
        )
    return None


def _outgoing_structured_error(record: "RunRecord") -> "EvidenceContractWriteError | None":
    # The structured lane has no grace path: a stored snapshot whose digest
    # does not recompute is corruption, and a corrupted snapshot must never
    # reach a replay that would present it as retained evidence.
    for index, row in enumerate(record.structured_evidence):
        if project_stored_structured_evidence(row) is not None:
            continue
        return EvidenceContractWriteError(
            MALFORMED_EVIDENCE_CONTRACT,
            f"structured evidence {index}: stored snapshot is not the frozen shape",
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
            comparisons=_jsonb_list(row, 15),
            # Trust (#135): empty list when pre-migration / the run
            # touched no locator.
            evidence_locators=_jsonb_list(row, 16),
            analysis_results=_jsonb_list(row, 17),
            # ADR 0089: empty list when pre-migration / the run produced
            # no visual claim. Coerced to ``[]`` (never ``None``) so the
            # citations-vs-claims separation holds for every reader.
            visual_claims=_jsonb_list(row, 18),
            # Same rule as visual_claims: a pre-lane or claim-free row
            # reads back as ``[]``, never ``None``.
            structured_evidence=_jsonb_list(row, 19),
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
                    "visual_claims, structured_evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s)",
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
                        json.dumps(record.structured_evidence),
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
                "visual_claims, structured_evidence "
                "FROM fin_run WHERE thread_id = %s ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
            # ``_row_to_run`` already deep-copies every mutable field
            # off the driver buffer (citations / tool_summaries /
            # track_decision / comparisons / evidence_locators), so
            # one outer deepcopy per row is enough.
            return [copy.deepcopy(self._row_to_run(r)) for r in rows]

    # -- EP9/EP10 invalidation checkpoint --

    def read_invalidation_checkpoint(self, consumer_id: str) -> EvidenceInvalidationCheckpoint:
        with self._guarded("read invalidation checkpoint") as conn:
            row = conn.execute(
                "SELECT consumer_id, acked_checkpoint, applied_checkpoint, last_seq "
                "FROM fin_evidence_invalidation_checkpoint WHERE consumer_id = %s",
                (consumer_id,),
            ).fetchone()
        if row is None:
            return EvidenceInvalidationCheckpoint(consumer_id=consumer_id)
        return EvidenceInvalidationCheckpoint(
            consumer_id=row[0],
            acked_checkpoint=row[1] or "",
            applied_checkpoint=row[2] or "",
            last_seq=int(row[3] or 0),
        )

    def scrub_structured_evidence_payloads(
        self,
        *,
        consumer_id: str,
        evidence_refs: Sequence[str],
        next_checkpoint: str,
        last_seq: int,
    ) -> EvidenceScrubOutcome:
        refs = list(evidence_refs)
        with self._guarded("scrub structured evidence") as conn:
            # WHY these statements share one transaction: the scrub, its
            # confirmation read, and the watermark advance are one unit. A
            # leftover matching entry means storage could not confirm, and
            # then the watermark must NOT move — the caller refuses and the
            # backend redelivers.
            # WHY scoped to the evidence column: a whole-row rewrite would
            # drag every other lane's JSONB through this statement, and the
            # durable write guard has no business here either.
            entries_before = self._matching_entry_count(conn, refs)
            runs_before = self._matching_run_count(conn, refs)
            conn.execute(
                "UPDATE fin_run AS r "
                "   SET structured_evidence = COALESCE("
                "         (SELECT jsonb_agg(elem)"
                "            FROM jsonb_array_elements(r.structured_evidence) AS elem"
                "           WHERE NOT (elem->'snapshot'->>'evidence_ref' = ANY(%s))),"
                "         '[]'::jsonb)"
                " WHERE EXISTS (SELECT 1"
                "                 FROM jsonb_array_elements(r.structured_evidence) AS elem"
                "                WHERE elem->'snapshot'->>'evidence_ref' = ANY(%s))",
                (refs, refs),
            )
            remaining_entries = self._matching_entry_count(conn, refs)
            if remaining_entries == 0:
                self._upsert_checkpoint(
                    conn, consumer_id=consumer_id, applied=next_checkpoint, last_seq=last_seq
                )
            conn.commit()
        return EvidenceScrubOutcome(
            scrubbed_entries=entries_before - remaining_entries,
            scrubbed_runs=runs_before,
            remaining_entries=remaining_entries,
        )

    @staticmethod
    def _matching_entry_count(conn: Any, refs: Sequence[str]) -> int:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_run AS r, "
                "LATERAL jsonb_array_elements(r.structured_evidence) AS elem "
                "WHERE elem->'snapshot'->>'evidence_ref' = ANY(%s)",
                (list(refs),),
            ).fetchone()[0]
        )

    @staticmethod
    def _matching_run_count(conn: Any, refs: Sequence[str]) -> int:
        return int(
            conn.execute(
                "SELECT count(*) FROM fin_run AS r WHERE EXISTS ("
                "SELECT 1 FROM jsonb_array_elements(r.structured_evidence) AS elem "
                "WHERE elem->'snapshot'->>'evidence_ref' = ANY(%s))",
                (list(refs),),
            ).fetchone()[0]
        )

    def _upsert_checkpoint(
        self, conn: Any, *, consumer_id: str, applied: str = "", last_seq: int = 0
    ) -> None:
        # WHY upsert and not UPDATE-then-INSERT: a first drain has no row
        # yet, and two drains racing must not lose a watermark.
        #
        # WHY the conflict branch never writes ``acked_checkpoint``: this runs
        # BEFORE the backend acknowledgement exists, so it must keep the resume
        # cursor the previous acknowledgement left. The acknowledgement path has
        # its own statement.
        #
        # WHY ``GREATEST(last_seq, ...)``: a crash between the two phases makes
        # the backend redeliver events whose seq is already applied. The drain
        # refuses a batch ending behind the stored watermark, so this only
        # defends the orderable column against a caller that bypassed it.
        #
        # WHY ``applied_checkpoint`` is written as-is: it is opaque, so SQL
        # cannot order it and no conflict clause can refuse a regression on it.
        # Its monotonicity rests entirely on the drain's ``last_seq`` guard,
        # which runs before this write.
        conn.execute(
            "INSERT INTO fin_evidence_invalidation_checkpoint "
            "(consumer_id, applied_checkpoint, applied_at, last_seq, acked_checkpoint) "
            "VALUES (%s, %s, now(), %s, '') "
            "ON CONFLICT (consumer_id) DO UPDATE SET "
            "applied_checkpoint = EXCLUDED.applied_checkpoint, "
            "last_seq = GREATEST("
            "    fin_evidence_invalidation_checkpoint.last_seq, EXCLUDED.last_seq), "
            "applied_at = now()",
            (consumer_id, applied, last_seq),
        )

    def advance_invalidation_acknowledgement(
        self, consumer_id: str, checkpoint: str
    ) -> None:
        with self._guarded("advance invalidation acknowledgement") as conn:
            row = conn.execute(
                "INSERT INTO fin_evidence_invalidation_checkpoint "
                "(consumer_id, applied_checkpoint, applied_at, last_seq, acked_checkpoint) "
                "VALUES (%s, '', now(), 0, %s) ON CONFLICT (consumer_id) DO UPDATE SET "
                "acked_checkpoint = EXCLUDED.acked_checkpoint, applied_at = now() "
                "RETURNING consumer_id",
                (consumer_id, checkpoint),
            ).fetchone()
            conn.commit()
            if row is None:
                raise PersistenceUnavailableError(
                    "advance invalidation acknowledgement: INSERT ... RETURNING yielded no row"
                )

    # -- EP10 recovery gate (T33) --

    def read_recovery_state(self, consumer_id: str) -> EvidenceRecoveryState:
        with self._guarded("read recovery state") as conn:
            row = conn.execute(
                "SELECT recovery_withheld, recovery_reason "
                "FROM fin_evidence_invalidation_checkpoint WHERE consumer_id = %s",
                (consumer_id,),
            ).fetchone()
        if row is None:
            return EvidenceRecoveryState(consumer_id=consumer_id)
        return EvidenceRecoveryState(
            consumer_id=consumer_id, withheld=bool(row[0]), reason=row[1] or ""
        )

    def set_recovery_withheld(self, consumer_id: str, reason: str) -> None:
        # WHY this statement names NO watermark column: closing the gate must
        # leave the resume cursor exactly where the last acknowledgement put it.
        # A stale position is the reason the drain refused; advancing it here
        # would be the defect T33 forbids.
        with self._guarded("set recovery withheld") as conn:
            conn.execute(
                "INSERT INTO fin_evidence_invalidation_checkpoint "
                "(consumer_id, applied_checkpoint, applied_at, last_seq, "
                " acked_checkpoint, recovery_withheld, recovery_reason) "
                "VALUES (%s, '', now(), 0, '', TRUE, %s) "
                "ON CONFLICT (consumer_id) DO UPDATE SET "
                "recovery_withheld = TRUE, recovery_reason = EXCLUDED.recovery_reason",
                (consumer_id, reason),
            )
            conn.commit()

    def set_recovery_released(self, consumer_id: str) -> None:
        with self._guarded("set recovery released") as conn:
            conn.execute(
                "INSERT INTO fin_evidence_invalidation_checkpoint "
                "(consumer_id, applied_checkpoint, applied_at, last_seq, "
                " acked_checkpoint, recovery_withheld, recovery_reason) "
                "VALUES (%s, '', now(), 0, '', FALSE, '') "
                "ON CONFLICT (consumer_id) DO UPDATE SET "
                "recovery_withheld = FALSE, recovery_reason = ''",
                (consumer_id,),
            )
            conn.commit()

    def scrub_retained_evidence_payloads(
        self, evidence_refs: Sequence[str]
    ) -> EvidenceScrubOutcome:
        """Delete the named payloads and re-count, in one transaction.

        No checkpoint statement appears here at all, on purpose: the sweep has
        no batch, so it has no cursor to apply and no ``seq`` to claim. The
        acknowledgement stays entirely the drain's to write.
        """

        refs = list(evidence_refs)
        if not refs:
            return EvidenceScrubOutcome(
                scrubbed_entries=0, scrubbed_runs=0, remaining_entries=0
            )
        with self._guarded("scrub retained structured evidence") as conn:
            entries_before = self._matching_entry_count(conn, refs)
            runs_before = self._matching_run_count(conn, refs)
            conn.execute(
                "UPDATE fin_run AS r "
                "   SET structured_evidence = COALESCE("
                "         (SELECT jsonb_agg(elem)"
                "            FROM jsonb_array_elements(r.structured_evidence) AS elem"
                "           WHERE NOT (elem->'snapshot'->>'evidence_ref' = ANY(%s))),"
                "         '[]'::jsonb)"
                " WHERE EXISTS (SELECT 1"
                "                 FROM jsonb_array_elements(r.structured_evidence) AS elem"
                "                WHERE elem->'snapshot'->>'evidence_ref' = ANY(%s))",
                (refs, refs),
            )
            # The confirmation read is INSIDE the same transaction as the
            # delete, so a non-zero count means the scrub could not be backed
            # and the caller must not open the gate.
            remaining_entries = self._matching_entry_count(conn, refs)
            conn.commit()
        return EvidenceScrubOutcome(
            scrubbed_entries=entries_before - remaining_entries,
            scrubbed_runs=runs_before,
            remaining_entries=remaining_entries,
        )


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
    "EvidenceInvalidationCheckpoint",
    "EvidenceScrubOutcome",
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
