# Spec: Runtime event stream and client reconnection

**Status:** Draft specification; architecture review and the stated freeze prerequisites are separate from implementation acceptance.
**Date:** 2026-10-21

## Objective

Define the Runtime snapshot and subscription contract that lets the Agent UI show each research thread correctly across concurrent threads, tabs, reconnects, and process restarts. PostgreSQL remains the durable source for client-visible state and event order; Redis Streams and SSE deliver bounded live updates.

## Boundaries

- **Does:** Specify per-thread snapshots, thread-wide event cursors, Redis/SSE delivery, replay and reset behavior, independent client projections, operation-ID retention on retries, and question-kind-specific pause behavior.
- **Confirms with human:** The closed map and tickets are the approved contract baseline. Any change to ordering, continuation, or retention policy must be resolved before the implementation plan is frozen.
- **Out of this spec:** Runtime persistence and fencing (S1/P1), parser job authority/readiness (S3), tool behavior (S4), acceptance harness implementation (S5), deployment and guard retirement (S6), and implementation plans. This spec does not certify current behavior.

## Assumptions (inherited from closed decisions)

1. The application-owned Fin Runtime accepts client operations and exposes snapshots/subscriptions; PostgreSQL owns durable state and event order, while Redis Streams carries bounded notifications and live delivery (ADR 0103).
2. The user confirms QA Plan 05 T11/T12 is complete as an implementation baseline; its scoped evidence is in the [plan](../plans/qa-chain-05-recovery-trace.md) and [closeout](../docs/verification/2026-10-19-qa-plan05-closeout.md). It does not satisfy Runtime P5 or broader G1–G3/full acceptance.
3. A snapshot is authoritative; SSE and local browser state are projections that can be refreshed from it. A terminal answer and its evidence are durable before its terminal event is published.
4. Content clarification is a terminal question answered by a new message/new run. Document-choice and Fast proposal-choice questions pause and resume the same run. The relevant closed tickets must agree with this distinction before S2 freezes (ADR 0104; Runtime map).
5. Redis replay retention uses the ticket's configurable 24-hour/10,000-event-per-run starting window. It is an operational default to validate, not a durable-history promise.

## Contract

- Reuse the existing turn-span read route and TaskPanel per-thread trace projection. Runtime P5 adds thread snapshots, event cursors, and Redis/SSE delivery; trace state remains a per-thread projection, never event-order authority. Existing database/API/render-seam evidence does not replace P9 browser validation.

- A thread snapshot returns the thread generation and revision, persisted messages and terminal evidence, attachment/readiness projection, queued and active or waiting run summaries, the thread event high-water mark, and an opaque cursor bound to that thread generation. Projection, revision, and high-water mark come from one consistent PostgreSQL snapshot.
- Every client-visible run or thread-lifecycle event has a stable event ID, thread ID, optional run ID, thread generation, thread revision, kind, bounded payload or durable payload reference, and monotonically increasing `thread_event_seq`. A per-run sequence may be included but is not thread-wide ordering authority.
- A projection/revision change, sequence allocation, and corresponding outbox row commit atomically. Redis stream IDs are delivery details and never substitute for the thread cursor. The Runtime subscription hides Redis keys and per-run stream fan-in from the SPA.
- A client reads a snapshot, then subscribes after its high-water cursor. The handoff guarantees each concurrent mutation appears either in that snapshot or as the next event. Consumers deduplicate stable event IDs and advance only across a provably contiguous thread sequence.
- A missing sequence, unresolved out-of-order event, trimmed range, wrong generation, or other unprovable continuity produces `reset_required` and a current snapshot reference. The client replaces its projection only with a snapshot at least as new as its current revision, then subscribes after that snapshot's cursor; it never guesses ordering from a per-run sequence.
- Each tab reads independently; browsers do not share a Redis consumer group. A delayed event cannot overwrite a newer durable snapshot. Refresh, focus, thread entry, and explicit recovery hydrate from the Runtime snapshot.
- If Redis is unavailable, the Runtime still serves PostgreSQL-backed snapshots; live delivery is reported as degraded and pending outbox notifications reconcile after recovery. A missing live event never makes an accepted operation or committed answer disappear.
- Partial token output is replayable only within bounded retention and is never treated as a completed answer. If execution ends before the terminal commit, the view reports interruption or reconciliation required. Disconnecting, closing a tab, route changes, aborting a read, and losing SSE detach the viewer; only an explicit command for the exact run requests cancellation.
- Client projections are keyed by thread and run. Revision/generation checks prevent stale events, stale GET responses, or malformed local snapshots from erasing newer state or a newly created thread. A `404` means an absent or tombstoned thread; a `5xx`, timeout, or unavailable store remains a retryable/error state.
- The client preserves the same stable operation ID across duplicate activation, lost-response HTTP retries, refresh recovery, and initial thread creation before a thread ID exists. The Runtime returns the original accepted operation for the same ID and payload; a changed payload conflicts. Multiple HTTP attempts may represent one intent.
- Ordinary messages accepted behind a paused run remain queued. Content clarification ends its run and its answer is a new run; document-choice and Fast proposal-choice resume the paused run. A transport disconnect never releases the thread slot or starts queued work.

## Success Criteria

1. For a snapshot raced against a client-visible commit, the commit appears in the snapshot with its new high-water mark or as the first subsequent event; no cursor skips a projection change. **Evidence:** barrier-controlled PostgreSQL snapshot/subscription race.
2. A subscription delivers the next contiguous thread sequence across runs; duplicates are ignored and a gap, trimmed range, or unresolved ordering inversion returns `reset_required` with a usable current snapshot. **Evidence:** event-order contract tests with duplicate, gap, out-of-order, and expired-cursor cases.
3. Independent tabs each receive the complete events for their thread, and events or delayed responses for another thread or older generation cannot alter the selected thread. **Evidence:** multi-tab browser assertions and concurrent thread event tests.
4. A terminal message and evidence survive a fresh request/process read even when the terminal event is missed; incomplete token output never appears as a terminal answer. **Evidence:** fresh-process database readback plus browser reload assertions.
5. Disconnect and reconnect within retention restore the same run without cancellation; reconnect after retention rehydrates from a snapshot. Explicit stop affects only the named run. **Evidence:** worker-running disconnect and targeted cancellation browser/mechanism journeys.
6. During Redis unavailability, a fresh snapshot still returns committed PG state, and pending notifications reconcile after Redis returns. **Evidence:** Redis outage/recovery process test with fresh-process snapshot readback.
7. Initial thread creation and later sends retain one operation ID across retries. A repeated ID/payload returns one durable operation; a changed payload conflicts. **Evidence:** lost-response and duplicate-activation tests, including creation before a thread ID is available.
8. Malformed local state, stale GET, invalid/empty/foreign-generation cursor, and storage exception inputs never erase newer client state or become a false `404`; cursor validation cannot wrap on numeric overflow. **Evidence:** applicable public-entry contract tests and browser state-recovery assertions.
9. Content clarification starts a new run after a terminal question; document-choice and Fast proposal-choice resume the same paused run, with queued ordinary messages still waiting. **Evidence:** run identity and queue-order assertions for each question kind.

## Open Questions

- Before S2 contract freeze, reconcile `hr-concurrency-contract.md`, `hr-durable-ownership.md`, `hr-client-reconnection.md`, and `hr-runtime-selection.md` with the question-kind-specific clarification behavior recorded in the map. No new user-visible continuation choice is introduced here.

## Inherits / Changes

- **Fin Runtime:** “The application-owned authority that accepts durable client operations, orders work per **research thread**, manages run reservations and fenced **worker attempts**, and exposes thread snapshots and subscriptions. It dispatches the host graph but does not own graph checkpoints or **parser job** state.” (`docs/CONTEXT.md`)
- **thread event cursor:** “An opaque resume position backed by a monotonically increasing PostgreSQL sequence over client-visible events for one thread, including events across runs and thread lifecycle changes; its snapshot reads the projection, revision, and high-water mark from one consistent database snapshot, and per-run sequence is auxiliary.” (`docs/CONTEXT.md`)
- Inherits ADR 0101 lifecycle ownership, ADR 0103 durable event continuity and authority, and ADR 0104's clarification distinction. ADR 0102 remains the current tracing contract; no trace fields become event-order authority.
- **Changes:** None to CONTEXT or ADRs. Persist list: empty.

## Architecture-change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review.
