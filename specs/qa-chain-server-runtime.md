# Spec: QA-chain server lifecycle and receipts

**Status:** Original lifecycle/receipt contract remains adopted; QA Plan 05 T11/T12 recovery and tracing implementation is delivered at its documented scope. The target Runtime amendment is an architecture-reviewed draft with explicit pre-freeze requirements; Runtime P0–P12 implementation and batch acceptance remain pending.
**Date:** 2026-10-16

## Adopted Runtime target amendment (2026-10-21)

This amendment adds the accepted Fin Runtime target from [ADR 0103](../docs/adr/0103-durable-runtime-and-parser-ownership.md) and the canonical [reliability map](../docs/wayfinder/harness-runtime-reliability-map.md). The receipt schema and its measurement/redaction contract below remain in force. The original sections remain the historical 2026-10-16 lifecycle contract; the supersession list below limits which original clauses describe the new target.

### Assumptions

1. **Confirmed:** Scope remains one operator with multiple concurrent research threads; tenant isolation and hosted-service authorization remain out of scope. Evidence: the accepted reliability map, `hr-concurrency-contract.md`, and `hr-runtime-selection.md`.
2. **Confirmed:** PostgreSQL is durable authority for accepted Runtime operations; Redis Streams carries bounded notifications; parser-job authority remains in the independent parser service. Evidence: ADR 0103 and `hr-durable-ownership.md`.
3. **Confirmed:** LangGraph remains the graph engine and its synchronous saver interface is preserved; Runtime business state and checkpoint state remain separate authorities. Evidence: ADRs 0015/0026 and 0103.
4. **Confirmed:** The current single-process guard remains until the accepted rollout evidence passes; this amendment does not remove it. Evidence: ADR 0101 and `hr-rollout-guard-retirement.md`.
5. **Confirmed by user:** QA Plan 05 T11/T12 implementation is complete as the inherited recovery/tracing baseline. Its scoped evidence is documented in [QA Plan 05](../plans/qa-chain-05-recovery-trace.md) and the [closeout](../docs/verification/2026-10-19-qa-plan05-closeout.md); broader G1–G3/full acceptance remains open, and this does not satisfy new Runtime P5.

### Inherited Plan 05 baseline

Preserve the existing receipt identity, durable store/export lifecycle, five allowlisted/redacted spans, acknowledgement, and bounded drain across P1/P7. The existing local PostgreSQL sink and TaskPanel render seam have database/API/render-seam evidence; browser TaskPanel acceptance and remote Langfuse remain open. P7 deliberately changes legacy disconnect-as-stop to subscriber detach-only while retaining explicit run-targeted stop, fencing, and cleanup.

### Objective

Specify durable acceptance, ownership, recovery, and event persistence for the multi-process Runtime while preserving the existing graph/checkpoint and receipt contracts. The Runtime separates a user operation and logical run from each worker attempt, so a lost response or process restart does not create a second operation or let an old worker write current state.

### Boundaries

- **Does:** P0 capability boundaries and public compatibility; P1 PostgreSQL-backed acceptance, reservation, fencing, terminal persistence, and event projection; P7 API/graph-worker ownership and cancellation semantics.
- **Confirms with human:** No new architecture or numeric target is introduced. The assumptions above inherit closed decisions; any missing per-tool, deployment, or migration choice remains for its owning spec/plan.
- **Out of this spec:** Redis/SSE wire and client details (S2), parser adapters and full readiness contract (S3), tool-attempt ledger (S4), unified acceptance harness (S5), deployment/cutover execution (S6), and code or migration implementation.

### Target contract

- PostgreSQL commits the stable operation identity, accepted input, per-thread FIFO position, run record, and notification intent before returning acceptance. A same-key/same-payload resubmission returns the durable prior result; same-key/different-payload conflicts. Acceptance order and client-visible `thread_event_seq` are distinct counters. Acceptance and run reservation are not graph checkpoints.
- Multiple Runtime API processes and graph workers coordinate through PostgreSQL; Redis Streams remains notification/wakeup transport, not operation truth. Runtime state, thread reservation, worker-attempt lease, and LangGraph checkpoint are distinct facts. A waiting run retains its thread reservation while its worker lease is released; a new ordinary queued run cannot use that thread until the reserved run ends or is explicitly cancelled/abandoned and fenced. Different threads may execute concurrently within configured capacity.
- Every mutating synchronous checkpointer path, including `put`, `put_writes`, batch writes, and any other mutation, passes through one generation-aware adapter. On the same PostgreSQL connection and transaction as the checkpoint write, it validates the thread reservation, run/attempt lease, attempt and thread generations, cancellation, and non-tombstone state. Claims, renewals, cancellation, deletion, and checkpoint writes use the fixed lock order thread reservation → run/attempt authority → checkpoint record. Transactions do not span model or network work. No direct or fail-open stock-saver path is permitted; an async graph/saver rewrite is not required.
- Runtime owns durable terminal messages/evidence and the outbox. Commit the final assistant message and its evidence, terminal state, thread event sequence, and outbox intent before publishing a terminal notification. PostgreSQL owns the monotonic thread-wide event sequence; projection/revision, sequence allocation, and outbox row commit together. A snapshot reads projection, revision, and sequence high-water mark from one consistent PostgreSQL snapshot. Redis delivery is bounded and at least once; stable identities and idempotent consumers handle duplicates, with no end-to-end exactly-once claim. A gap or unprovable continuity requires `reset_required` and snapshot recovery. S2 consumes this contract for Redis/SSE delivery and client projections.
- Parser completion is a notification, not parser truth or readiness. The parser service owns job status and results; fin records completion in its durable inbox as pending consumption and reconciles from the parser authority. S3 owns result validation and the two caller-generation compare-and-swap transactions.
- Browser disconnect only detaches that reader; it does not cancel or terminate the graph worker. Explicit stop targets the intended `run_id`. Late work is fenced from changing newer state. Clarification is question-kind-specific: content clarification is a terminal question answered by a new message/new run; document-choice and Fast proposal choice are genuine paused runs resumed on the same run. This matches [ADR 0104](../docs/adr/0104-internal-search-mode-automatic-fallback.md) and the accepted concurrency/client decisions.
- Recovery resumes a checkpoint only when all intervening effects are proven safe to replay. Lease expiry or renewal uncertainty does not prove that an external call failed: stop starting new effects, reconcile by stable operation identity, and expose `reconciliation_required` or `interrupted` when the outcome cannot be proven. Never blindly replay an uncertain model/provider or parser effect.
- P0 uses business-capability boundaries, not generic technical directories as primary ownership. A capability may contain its transport adapters or shared UI primitives. Preserve public imports and behavior; optional local graph tooling is separately scoped and reviewed, with no production Agent Server.

**Supersession scope:** For the adopted target only, this amendment supersedes the original Assumptions item 2's “disconnect cancellation”; process-local admission and the original single-process-only execution-owner boundary; any reading of the lifecycle's “stop/disconnect propagation” as cancelling graph work when a socket closes; and blanket same-run continuation for every paused clarification. The original receipt schema, receipt invariants, trace contract, and historical evidence remain intact. Task Runtime retains product task identity, semantic task-state/input-version reconciliation, and task outcomes; the checkpointer owns graph state. Fin Runtime owns durable operation acceptance, accepted-input receipts/queue, reservation, and dispatch; checkpoint copies of accepted-operation facts are projections rather than another acceptance authority. The old success criteria below are historical for their original scope; the target criteria here govern the new Runtime ownership and question-kind distinctions. The current process-count guard still remains until rollout evidence passes.

### Success Criteria

- **RT-1 — Durable acceptance:** Real PostgreSQL tests against an isolated `_test` database prove commit-before-acceptance, same-key replay, conflicting-payload rejection, lost-response recovery, and strict per-thread FIFO; a fresh process reads the same accepted operation and queue position. Empty, missing, malformed, and invalid request identities/payloads, configured queue-capacity boundaries, concurrent duplicate/conflicting intents, and database exceptions have typed outcomes and cannot leave a falsely accepted operation. Recognize a previously accepted ID before capacity rejection.
- **RT-2 — Reservation and attempts:** Barrier-controlled process tests against an isolated `_test` database prove one effective reserved run per thread, a distinct renewable/fenced worker attempt, reservation retention while paused/waiting, and permitted cross-thread overlap within configured capacity.
- **RT-3 — Checkpoint fencing:** Integration tests against an isolated `_test` database exercise every mutating saver path. After takeover, cancellation, or deletion wins the shared locks, stale `put`, `put_writes`, batch, and other mutation attempts are rejected in the same transaction; legitimate later writes succeed. Missing adapter wiring fails closed.
- **RT-4 — Event and terminal recovery:** Fault injection against an isolated `_test` database between PostgreSQL commit and Redis publication proves terminal message/evidence and event sequence survive, outbox replay is idempotent, and snapshots read projection, revision, and high-water mark consistently. Unprovable gaps produce `reset_required`.
- **RT-5 — Ownership boundary:** Parser API/worker restart and lost/duplicate notification evidence against an isolated `_test` database shows parser job state remains parser-owned, while fin's inbox/reconciliation never treats notification alone as readiness.
- **RT-6 — Detach, stop, and clarification:** API/browser evidence proves disconnect leaves the run owned and resumable, explicit stop targets one `run_id`, and late worker output cannot overwrite terminal/newer state. Content clarification starts a new run; accepted document-choice/Fast-choice resumes the same paused run.
- **RT-7 — Rollout guard:** The guard-removal gate is not met until the rollout matrix passes with at least two Runtime API processes and two graph workers against real PostgreSQL (`_test` database) and Redis, stale checkpoint writes rejected, parser restart evidence, required browser journeys and Fin cases, matching database/artifact restore, and measured target-resource headroom. This spec does not remove the guard.

- **RT-8 — Safe recovery:** Lease-expiry and process-termination tests at known and uncertain external-effect boundaries prove safe work can resume, a recorded effect is reconciled before another execution, and an unresolved effect remains explicitly interrupted/reconciliation-required without duplicate execution.

### Open Questions

No new ownership choice is introduced. Before target contract freeze, align the generic clarification wording in the concurrency, durable-ownership, client-reconnection, and runtime-selection tickets with ADR 0104 and this amendment. Implementation plans must resolve only choices not already fixed by the linked contracts; this amendment adds no timeout, capacity, schema, or provider selection.

### Inherits / Changes

Use the current **run** and **turn receipt** definitions below verbatim from [CONTEXT](../docs/CONTEXT.md). The old `run` quote later in this spec is retained as historical text; the current target definition here governs this amendment.

> **run**: One logical graph turn (`run_id`) accepted for a thread: `done` / `refuse` / `step_cap` end it; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Under the adopted target contract, a run may be queued or waiting without occupying a worker, and takeover resumes the same run only when its checkpoint and effects are safe; each worker claim is a separate fenced **worker attempt**.

> **turn receipt**: One logical operational summary of an accepted answer turn, correlated across pause and continuation and distinct from task state, task completion, and per-tool-attempt measurement records.

This amendment inherits ADRs 0015/0026, 0101, 0102, and 0103; [current ADR 0104](../docs/adr/0104-internal-search-mode-automatic-fallback.md) supplies the content-clarification/document-choice distinction. The target does not add a bounded context or a new persisted domain term; **persist list: empty**.

### Architecture Change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review. The existing 2026-10-16 review below remains historical for the original lifecycle/receipt contract.

## Objective

Give HTTP and background callers one application-owned execution interface, with bounded cancellation and a durable, truthful receipt for each logical answer turn. Resource coordination belongs behind the runtime seam; callers observe typed lifecycle events and a status projection.

## Boundaries

- **Does:** Application construction and lifespan, closeable graph ownership, one lifecycle for HTTP and background execution, cancellation, receipt persistence and recovery, local status, bounded trace export.
- **Confirms with human:** The operator accepted all four decision groups on 2026-10-16. For this spec that adopts app-owned runtime lifetime; one lifecycle for ordinary requests, background recovery and clarification continuation; one receipt across a logical turn; and the separate receipt table plus the catalog and ingest publication records described by the sibling contracts.
- **Out of this spec:** Retrieval ranking and catalog metadata details, grounded-answer semantics and browser presentation. The sibling QA-chain specs and the existing question-module contracts own those details. The journey acceptance contract remains the integrated product-acceptance gate. This spec adds no new acceptance oracle.

## Assumptions and decisions

1. **Inherited:** ADR 0015/0026 retain the synchronous durable graph and PostgresSaver. The existing Task Runtime envelope and checkpoint remain the authority for task identity, accepted input, current state and task outcome.
2. **Requirement:** The server owns resource lifetime, disconnect cancellation, bounded blocking operations, truthful failure accounting and receipt export.
3. **Adopted on 2026-10-16:** The application owns one closeable runtime; HTTP, attachment auto-resume, recovery and clarification continuation use one run lifecycle; one receipt remains keyed to the original run through accepted clarification replies; the approved persistence design is specified below.
4. **Removed:** This adoption does not select a new model provider, change durable graph storage to async, or choose a new concurrency-isolation mechanism. Use the existing model and graph contracts and the trace transport below. The **fin_turn_receipt** table shape is an adopted decision and remains specified here.
5. **Adopted on 2026-10-19 (ADR 0102), amended 2026-10-20:** Tracing has two layers. The **session-record layer** — thread messages plus receipts — is always on, content-complete, and authoritative for single-turn troubleshooting. The **trace layer** — spans carrying structure, timing and allowlisted attributes only, never raw text — is off by default and, when enabled, writes spans to local PostgreSQL or exports them over OTLP. Trace export uses the OpenTelemetry API, SDK and OTLP/HTTP exporter with standard `OTEL_*` selection, and the `pg` sink additionally requires the fin-specific `FIN_TRACE_SPAN_DSN`; it instruments five hand-written execution spans instead of a single receipt-projection span. This replaces the earlier dependency-free hand-rolled OTLP encoder and its "no SDK, no package, no lockfile change" clause. The allowlist discipline, redaction boundary, bounded queue, acknowledgement cursor and shutdown drain are unchanged; the two layers are correlated by **thread_id**, **turn_id** and **trace_id**, and cross-turn aggregation and alerting belong to the middle platform (中台).

## Contract

### Application ownership

Application construction receives explicit dependencies and settings. Application lifespan owns composition, readiness, worker supervision, shared control resources, startup rollback and shutdown order. It publishes one app-owned closable runtime handle containing the graph and its cleanup operation; HTTP code does not need PostgresSaver details to close it.

The server stops dependent workers before closing the runtime exactly once, including after partial startup failure. HTTP execution and attachment auto-resume receive the same application-owned cancellation/control instance. A compatibility application may defer construction to lifespan, but importing it does not start workers or connect to a database. Preserve **fin-serve**, **fin.host.http:app** and FastAPI **app.dependency_overrides**. Removing this module must return resource lifetime, shutdown ordering and injection coordination to several callers; a forwarding wrapper around **build_router** is insufficient.

HTTP handlers translate requests, admission outcomes and lifecycle events. They do not construct retrieval adapters, own process-global run state, or implement finalization branches. Dependency injection keeps two application instances isolated within one process. Existing composition roots remain the selectors of concrete adapters.

The execution-owner boundary in this design is single-process. One process may serve concurrent requests and worker tasks, with a per-thread execution owner serializing operations for each thread. There is no cross-process ownership guarantee: deployment configuration must declare the process count, and application construction rejects a count other than one unless a separately reviewed database-backed ownership implementation is selected. Multiple service processes or server worker processes may not silently share this in-process owner.

Retain the synchronous durable graph and PostgresSaver under ADR 0015/0026. An HTTP bridge may coordinate cancellation around that graph. Changing the durable graph or persistence stack to async requires a separate decision.

### One run lifecycle

HTTP requests, interactive continuation, attachment auto-resume and crash recovery use the same lifecycle interface. It accepts a start or resume intent plus trusted thread/run identity, and exposes typed events, outcomes and cancellation. Callers do not coordinate graph iterator cleanup, disconnect propagation, deadlines, receipt finalization, thread occupancy or persistence independently.

The lifecycle owns graph consumption, stop/disconnect propagation, deadline tracking, bounded cleanup, late-result fencing and one logical receipt. HTTP encodes SSE and observes the socket; background recovery can discard live events. A paused clarification restores and continues the original checkpoint with the same **thread_id**, **run_id** and receipt. A bodyless transport or crash resume does not create an accepted input.

A valid document choice or rephrase is one accepted reply: Task Runtime advances its current **input_version** exactly once, then continues the same run and updates the same receipt. **accepted_input_count** increases once for that newly accepted reply. At that transition, **execution_state** returns to **running** and **semantic_result** is cleared until the continued run establishes its next result. Duplicate delivery is idempotent. A pending question does not expire solely because an unrelated accepted input advanced the thread's **input_version**. A fresh user turn starts a new run and a new receipt. Receipt count, graph-run count, accepted-input count, HTTP request count and exported span count are distinct.

Document choices and rephrases enter the existing send door and are parsed there as accepted replies using **question_id**, **choice_id**, and optional **free_text**. The send door does not treat them as fresh user messages. A per-thread execution owner serializes the complete sequence: read current pending-question state and accepted-input history; first recognize an exact accepted-history retry even if the pending question has since cleared, returning its existing outcome or continuing only its unfinished reconciliation; reject a different reply to a question that already has an accepted answer; for a genuinely new reply, validate the current question, task, paused-run owner, candidate membership, current source identity/version/permission and **origin_run_id** (which must identify the current paused run for a document choice); accept and durably record the input; let Task Runtime advance its current **input_version** once; finish Runtime reconciliation and the checked Source Scope proposal; commit the reconciled Runtime envelope and accepted scope snapshot together in the checkpoint; then continue the original run with **Command(resume=...)**. If reconciliation or checkpoint commit fails, the accepted input remains explicitly unreconciled and no dependent retrieval or answer work starts. An old reply whose acceptance record has fallen outside retained history, or a reply absent from history that no longer matches a current pending question, is rejected as expired.

The exact retry key is the original (**thread_id**, **question_id**, **choice_id**, **free_text**) tuple. Concurrent identical retries converge on that one accepted input and one continuation; a conflicting reply cannot also succeed. The original **origin_run_id**, graph **run_id**, **turn_id** and receipt remain unchanged throughout. The continuation must not mint or substitute a new run identity. The existing send path's pre-lock UUID mint and the **is_locked** probe are not atomic admission or ownership mechanisms. All entry paths use the same execution owner. The existing graph lock is acquired exactly once by the layer that owns that responsibility: the execution owner must not wrap a graph API that acquires the lock internally, nor call a second lock-taking API while already holding it. The design assumes no reentrant graph lock.

Task Runtime's existing one reconciliation per accepted input remains in force. Each such reconciliation is counted as a model invocation. Runtime reconciliation and grounded decision counts are separate views: reconciliation is not a grounded decision. Tool-planning model calls also count as model calls. A grounded decision/synthesis invocation is counted for each evidence set and cannot hide a second answer synthesis or repair invocation. The grounded flow has at most one decision/synthesis invocation per evidence set.

Cleanup cannot release ownership while old work could still mutate a newer run. Clearing an active flag is not sufficient. Process recovery uses durable checkpoint facts and the receipt; an in-memory map alone cannot guarantee a durable receipt.

### Receipt schema

**fin_turn_receipt** is the durable operational summary. **fin_run** remains the existing question/answer/citation history and is not reused as a telemetry payload. The in-memory store implements the same keying, revision and idempotency behavior.

Inventory basis: **RunRecord** and the in-memory/Postgres **append_run** implementations in **src/fin/host/thread_store.py** persist transcript rows; their append semantics do not provide receipt upsert/idempotency. The current HTTP resume paths are in **src/fin/host/workspace_http.py**. This inventory grounds the storage delta; it does not establish that lifecycle budgets or disconnect recovery already pass.

The table has exactly these columns:

| Column | PostgreSQL type and bound | Meaning |
| --- | --- | --- |
| **thread_id** | **TEXT NOT NULL CHECK (octet_length(thread_id) BETWEEN 1 AND 256)** | Existing thread identity and first key component. |
| **turn_id** | **TEXT NOT NULL CHECK (octet_length(turn_id) BETWEEN 1 AND 256)** | Stable graph **run_id**; second key component. |
| **schema_version** | **SMALLINT NOT NULL CHECK (schema_version = 1)** | Receipt representation version. |
| **revision** | **BIGINT NOT NULL CHECK (revision >= 1)** | Monotonic receipt content revision. |
| **execution_state** | **TEXT NOT NULL** | **running**, **paused** or **terminal**. |
| **technical_result** | **TEXT NULL** | **done**, **stopped**, **timeout** or **failed**; null while nonterminal. |
| **semantic_result** | **TEXT NULL** | **answered**, **policy_refuse**, **evidence_refuse**, **clarify**, **overview** or **not_ready**; null until actually established. |
| **accepted_input_count** | **INTEGER NOT NULL CHECK (accepted_input_count BETWEEN 1 AND 2147483647)** | Accepted user inputs attached to this logical turn. Starts at one; increments once for each valid clarification reply. |
| **accepted_input_version** | **BIGINT NOT NULL CHECK (accepted_input_version BETWEEN 0 AND 9223372036854775807)** | Latest Task Runtime **input_version** represented by this receipt. It is a projection, not a second input authority; it may already be greater than zero when a new logical turn starts. |
| **route_source** | **TEXT NULL** | **rule**, **jev** or **fallback**. |
| **route_intent** | **TEXT NULL** | **retrieve**, **overview**, **chat** or **policy_refuse**. |
| **grounded_decision** | **TEXT NULL** | **answer**, **document_choice**, **clarify**, **evidence_refuse**, **not_ready** or **continue**; the latest observed grounded decision. |
| **decision_parse** | **TEXT NULL** | **structured**, **fallback_merge**, **fallback_refuse**, **invalid** or **unavailable**. |
| **citation_verified** | **BOOLEAN NULL** | Null if verification did not run; otherwise the observed pass/fail result. |
| **not_ready_cause** | **TEXT NULL** | **index** or **busy**; null if no not-ready result was established. |
| **not_ready_stage** | **TEXT NULL** | **index**, **queue**, **startup** or **response**; preserves the exact Corpus readiness stage when **semantic_result=not_ready**. |
| **failure_kind** | **TEXT NULL** | One allowlisted typed failure token listed below. Never raw exception text. |
| **cancellation_reason** | **TEXT NULL** | **user_stop**, **client_disconnect**, **thread_deleted**, **application_shutdown**, **phase_deadline** or **ownership_fenced**; null if no cause was established. |
| **started_at** | **TIMESTAMPTZ NOT NULL** | Original start time; unchanged across continuation and recovery. |
| **updated_at** | **TIMESTAMPTZ NOT NULL** | Latest committed receipt content update. |
| **terminal_at** | **TIMESTAMPTZ NULL** | Set only when execution becomes terminal. |
| **work_elapsed_ms** | **BIGINT NOT NULL CHECK (work_elapsed_ms >= 0)** | Durably observed active work time, excluding deliberate user wait. |
| **user_wait_ms** | **BIGINT NOT NULL CHECK (user_wait_ms >= 0)** | Durably observed time in deliberate clarification or attachment wait. |
| **accounting_complete** | **BOOLEAN NOT NULL DEFAULT FALSE** | True only after all turn accounting is closed. False marks partial/lower-bound measurements after interruption or crash. |
| **stage_metrics** | **JSONB NOT NULL** | Fixed object described below; unknown or incomplete measurements remain explicit. |
| **catalog_warning_count** | **INTEGER NOT NULL CHECK (catalog_warning_count BETWEEN 0 AND 2147483647)** | Observed unresolved document-publication warnings at the last observation: documents with a pending or failed publication and no later success, not attempts. A later success removes that document from the count. Incomplete accounting reports only the last observation, not a guaranteed current total. |
| **token_usage** | **JSONB NULL** | Optional object with only **input_tokens**, **output_tokens** and **total_tokens**; each key may be absent, and each present value is an integer in 0..2147483647. Missing values are never estimated. |
| **exported_revision** | **BIGINT NOT NULL DEFAULT 0 CHECK (exported_revision BETWEEN 0 AND revision)** | Export acknowledgement cursor; never increments **revision**. |

The primary key is (**thread_id**, **turn_id**). The row also has a foreign key from **thread_id** to **fin_thread(id) ON DELETE CASCADE**. String enums are checked against the exact values in this spec. **not_ready_cause** and **not_ready_stage** are both null unless an exact Corpus **not_ready** result was observed. Valid pairs are (**index**, **index**) and (**busy**, **queue**|**startup**|**response**); do not infer **index** from a generic empty result or recode another named error as **not_ready**. The failure-token allowlist is **task_input_invalid**, **task_input_unreconciled**, **task_already_reconciled**, **task_permission_denied**, **task_dependency_failure**, **task_capability_unavailable**, **task_capability_unsupported**, **task_insufficient_evidence**, **task_applicability_unresolved**, **task_intent_missing**, **task_execution_limit**, **task_checkpoint_failed**, **task_history_failed**, **task_reply_expired**, **task_reply_unknown**, **task_restore_unknown**, **task_restore_not_adopted**, **invalid_filter**, **invalid_search_mode**, **unsupported_filter_mode**, **unsupported_source_scope**, **retrieve_unavailable**, **retrieve_protocol_error**, **authorization_denied**, **retrieve_degraded**, **model_timeout**, **jev_timeout**, **retrieval_timeout**, **source_read_timeout**, **database_timeout**, **persistence_failed**, **checkpoint_failed**, **graph_failed**, **worker_failed**, **recovery_inconsistent** and **internal**. Preserve each typed Task Runtime cause as its exact **task_*** token and each named Corpus failure as its exact wire token in **failure_kind**; never collapse them to **internal** or **graph_failed**. An observed Corpus **not_ready** result is represented by the semantic result and cause/stage fields, not by a generic failure token.

State invariants are enforced at the Runtime/store boundary: **terminal** requires a non-null **technical_result** and **terminal_at**; nonterminal states require both to be null. A technical failure kind is present only for **failed** or **timeout**; Task Runtime failure tokens remain typed semantic causes and do not select the semantic result. A cancellation reason is present only when cancellation or fencing actually occurred; **thread_deleted** is recorded only in the pre-delete transaction. Null semantic fields remain valid for terminal operational failure.

**policy_refuse** is reserved for an explicit refusal from the existing system/model policy path. Off-topic questions, permission errors and operational failures do not use that route intent or semantic result. A missing, failed, malformed, low-confidence or unusable Jev hint falls back to **retrieve**; it cannot produce evidence refusal before an actual retrieval attempt. Routing and Jev fallback never widen, clear or replace the accepted source scope.

The receipt contains no source IDs or source-scope payload. Source identity and restriction remain in the existing Task Runtime/thread snapshot authority.

**stage_metrics** contains exactly these twelve keys: **model**, **runtime_reconciliation**, **grounded_decision**, **tool_planning**, **other_model**, **jev**, **retrieve**, **source_read**, **database_acquire**, **persistence**, **cleanup** and **disconnect_recovery**. Every key is required and each value has exactly **calls**, **elapsed_ms** and **complete**: **calls** is an integer in 0..2147483647; **elapsed_ms** is an integer in 0..9223372036854775807; **complete** is a boolean. **calls** counts each actually started invocation, including failed, cancelled and timed-out calls; only durably observed starts are counted. **elapsed_ms** sums durably observed elapsed time. An unstarted stage is zero with **complete: true** only when its absence is known. After interruption, **complete: false** marks observed lower bounds, not an invented zero total. **model** is the inclusive total; its reconciliation, grounded-decision, tool-planning and other-model entries are overlapping breakdowns and must not be added again. Trace-export attempts are exporter diagnostics, not answer-work counts. **runtime_reconciliation** counts the existing one Runtime reconciliation per accepted input; it contributes to **model** calls but not to **grounded_decision** calls. Each tool-planning invocation contributes to **model** calls and **tool_planning**; it cannot disguise an additional grounded answer synthesis. Corpus **not_ready** cause/stage are preserved separately from this duration accounting.

Unobserved semantic decisions, causes, token usage and timings remain null or incomplete. A timeout or crash must not turn an unknown answer into **evidence_refuse**, infer an exact total from a partial counter, or synthesize provider usage.

### Receipt transactions and recovery

A receipt is created once with **schema_version=1**, **revision=1**, **execution_state=running**, **accepted_input_count=1** and the current accepted input version. The count is scoped to accepted inputs attached to this logical turn; the projected Task Runtime version is a nonnegative signed 64-bit thread-history counter and can already exceed one when the turn begins. Repeating creation for the same (**thread_id**, **turn_id**) is a no-op if the identity agrees; it never appends or counts a second turn.

Every content update uses compare-and-swap on the expected **revision**. A committed content change increments **revision** by exactly one. Replaying the same lifecycle update or accepted input version is a no-op and does not increment either count or revision. For each genuinely new accepted clarification, Task Runtime advances its own pre-admission **input_version** exactly once; the receipt records that authoritative resulting version within the signed 64-bit bound and increments this logical turn's **accepted_input_count** once. Do not require the receipt's prior projected version plus one: intervening accepted inputs may create a version gap, while still-applicable pending questions remain eligible. Deduplicate by accepted-history identity/version, not by assuming contiguous receipt versions. The count is scoped to this turn and need not equal the absolute Runtime version. Transport resume without a new accepted input changes neither field. Task Runtime remains authoritative if checkpoint and receipt writes are separated; recovery reconciles the projection using (**thread_id**, **turn_id**, **accepted_input_version**) and never accepts an input solely because a receipt changed.

When both a terminal receipt and its **fin_run** transcript row are due, they commit in one store transaction. The transcript write is keyed by the same run identity and is idempotent. A deliberate pause does not require a transcript row. If the checkpoint store and thread store cannot share one transaction, durable checkpoint facts are reconciled into the receipt idempotently after restart. Recovery resumes a durable in-flight checkpoint or restores its pending clarification under the same run and receipt. If durable state cannot establish the semantic result or all measurements, leave those fields null/incomplete; if continuation is impossible, record terminal **failed** with **failure_kind=recovery_inconsistent** and keep the semantic result unknown. Preserve the original start time and accumulate only observed work and wait.

Thread deletion first blocks admission and fences the old writer, records **thread_deleted** as the cancellation cause while the row still exists, then deletes the thread, history and receipt rows in one store transaction. The foreign-key cascade removes the receipt. After successful deletion there are zero durable receipts for that thread; queued exports for it are discarded. No tombstone or new retention policy is added. This does not retract traces already exported before deletion, and deletion must not allow a late run to recreate its receipt.

### Bounded execution budgets

These are hard maximum budgets. Existing model and Jev limits are 120 and 20 seconds. New limits below are selected design defaults, not measured runtime results. Explicit settings may tighten them but may not raise them. Invalid, non-finite or nonpositive values fail application construction before resources are acquired.

| Phase | Maximum | Contract |
| --- | --- | --- |
| Model invocation | 120 seconds per call | Retain zero automatic model retries and existing graph execution caps. All model invocations count, including Runtime reconciliation and tool planning. |
| Jev | 20 seconds per call | Missing, failed, malformed, low-confidence or unusable hints follow the adopted retrieval fallback in the grounded/routing spec. |
| Retrieve | 5 seconds total | One monotonic deadline begins before I/O-lock acquisition and includes queue wait, session startup/handshake and response. |
| Source read | 5 seconds per call | Includes backend transport. |
| Database acquisition | 2 seconds per acquisition | Connection and lock acquisition are bounded. |
| Persistence phase | 5 seconds per phase | Includes bounded statements and commit; each transaction phase is subject to one deadline. |
| Cancellation cleanup | 1 second grace | Work surviving grace is fenced and remains supervised; clearing an active flag cannot release ownership. |
| Disconnect recovery | 5 seconds | Acceptance target from disconnect to responsive thread reads and a subsequent turn without stale ownership, including cleanup. |
| Trace request | 2 seconds per request | Background only; never an answer or status dependency. |
| Shutdown trace drain | 2 seconds total | Stop export after this bound; durable unacknowledged revisions are rediscovered after restart. |

No execution deadline runs during deliberate user choice or attachment preparation. The lifecycle reports this separately in **user_wait_ms** and excludes it from suspected-stuck counts. Cancellation does not restart a phase budget. A blocked operation that cannot be stopped is fenced so its late result cannot mutate current state.

### Status and stuck-run definition

**/api/status** reports active, paused and suspected-stuck counts from local lifecycle state. It does not wait on the graph, MCP or checkpoint locks. A run is suspected stuck only after an executing phase exceeds its deadline plus the one-second cleanup grace without progress. Deliberate clarification and attachment-wait pauses are excluded. Recovery includes worker exhaustion and lock release.

### Trace transport and spans

The **trace layer** is the second of two layers. The **session-record layer** — thread messages plus receipts — is always on, content-complete, and authoritative for single-turn troubleshooting. The **trace layer** carries structure, timing and allowlisted attributes only, never raw text; it is off by default, and when enabled it may write spans to local PostgreSQL or export them over OTLP. The two layers share **thread_id**, **turn_id** and **trace_id**. Cross-turn aggregation and alerting belong to the middle platform (中台), not to this server.

Export spans with the OpenTelemetry API, SDK and OTLP/HTTP exporter (`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, with a pinned `opentelemetry-semantic-conventions` version). Selection and OTLP configuration come from the standard `OTEL_*` environment variables, selected by `OTEL_TRACES_EXPORTER`. No exporter is selected by default; the supported choices are **pg**, **console** and **otlp**, and each stays off unless explicitly selected. **console** writes spans to the process output, and **otlp** exports them to any OTLP/HTTP backend, Langfuse included, by configuration alone. **pg** writes spans to the local `fin_trace_span` table in the database named by the fin-specific `FIN_TRACE_SPAN_DSN`, and application startup refuses **pg** without that variable rather than failing later in the export worker; its retention window is `FIN_TRACE_SPAN_RETENTION_DAYS` (default 14 days; `0` keeps rows), pruned by an at most hourly `DELETE` on the export worker. Apart from the fin-owned PostgreSQL sink, write no backend-specific code. See [ADR 0102](../docs/adr/0102-opentelemetry-standard-instrumentation-for-turn-receipt-tracing.md).

Instrument exactly five spans by hand, over the existing execution seams: **classify_node**, **retrieve_tool**, **model_node**, **citation_gate_node** and **admit_decision**. No automatic instrumentation and no general callback handler; the set does not grow beyond these five without reopening this contract.

Correlate deterministically. Derive **traceId** from (**thread_id**, **turn_id**) so every span and every receipt revision of one turn shares a trace. Derive each **spanId** from (**thread_id**, **turn_id**, **segment_seq**). Each execution segment is a root span with no parent; the receipt-projection span and the segment root spans are siblings in the same trace, so no span ever references a missing parent. **segment_seq** is a durable receipt column added by the expand migration with default 0. This module applies schema as idempotent inline DDL on store construction; a new column is added as `ALTER TABLE fin_turn_receipt ADD COLUMN IF NOT EXISTS` next to the existing statements, because `CREATE TABLE IF NOT EXISTS` alone would never reach an already-created table. It advances in the same transaction as the accepted reply that resumes a clarification; no other write inside a segment changes it, and neither receipt `revision` nor an in-process counter substitutes for it. Retrieval runs in a worker thread through `asyncio.run` in the MCP adapter, so the trace context is passed in explicitly rather than inherited.

The **session-record layer** stays authoritative for accounting and for single-turn troubleshooting. Keep **exported_revision**, the startup scan, the periodic reconciliation resend, and the coalescing behaviour described below. A span count that disagrees with receipt `calls` is exporter diagnostics and never a reason to void a trace or fail an evaluation; receipts remain the accounting authority.

The operator read door is `GET /api/threads/{thread}/turns/{turn}/spans`. It always answers 200 — an empty trace is an ordinary state, not a refusal — and its payload is `thread_id`, `turn_id`, `spans` and `trace_state`. `trace_state` replaced the earlier boolean `trace_enabled` and states exactly one of four values, each with one cause:

| `trace_state` | Meaning |
| --- | --- |
| `off` | The boot selected no exporter, so no span is written. |
| `local` | The boot selected the `pg` sink and the read succeeded. Only here can an empty `spans` list be read as "this turn has no span". |
| `external` | The boot selected `console` or `otlp`, so tracing is on but its spans are not written where this view reads; the route does not read the local store and returns an empty list. |
| `unreadable` | The `pg` sink is selected and the store read failed; the view claims nothing about this turn's spans. |

The state is derived from the exporter selection the application boot resolved once and published on its resources; the route must not read `OTEL_TRACES_EXPORTER` itself, because a second reader can disagree with the worker that is actually running. An exporter token this build does not know is a configuration error, not a fifth state. The read returns at most 200 spans for the turn, oldest first. The operator view built on this route is documented in [the tracing guide](../docs/guides/receipt-tracing.md).

Export only these allowlisted attributes, using these exact names and OTLP scalar types:

| Attribute | Type | Value |
| --- | --- | --- |
| **fin.thread_id**, **fin.turn_id** | string | Opaque correlations only. |
| **fin.segment_seq** | integer | Durable execution segment of this observation. |
| **fin.receipt.schema_version**, **fin.receipt.revision** | integer | Persisted version and revision. |
| **fin.receipt.execution_state**, **fin.receipt.technical_result**, **fin.receipt.semantic_result** | string | Persisted enums; omit null values. |
| **fin.receipt.terminal** | boolean | Present as true only for a terminal receipt; omit otherwise. |
| **fin.receipt.accepted_input_count**, **fin.receipt.accepted_input_version** | integer | Persisted input projection. |
| **fin.route.source**, **fin.route.intent**, **fin.grounded.decision**, **fin.grounded.decision_parse**, **fin.not_ready.cause**, **fin.not_ready.stage** | string | Persisted enums; omit null values. |
| **fin.citation.verified** | boolean | Persisted observed verifier result; omit null. |
| **fin.failure.kind**, **fin.cancellation.reason** | string | Persisted allowlisted tokens; omit null. |
| **fin.metrics.work_elapsed_ms**, **fin.metrics.user_wait_ms**, **fin.metrics.catalog_warning_count** | integer | Observed counters. |
| **fin.metrics.accounting_complete** | boolean | Whether terminal accounting is complete. |
| **fin.metrics.<stage>.calls**, **fin.metrics.<stage>.elapsed_ms**, **fin.metrics.<stage>.complete** | integer, integer, boolean | The exact **stage_metrics** keys and values above. |
| **fin.tokens.input**, **fin.tokens.output**, **fin.tokens.total** | integer | Include only values actually supplied by provider metadata. |
| **fin.retrieve.rerank_outcome**, **fin.retrieve.rerank_skipped_reason**, **fin.retrieve.rerank_fallback**, **fin.retrieve.rerank_effective_limit** | string, string, boolean, integer | Reserved retrieval rerank pass-through onto the **retrieve_tool** span. The receipt carries no rerank fields today, so this row emits nothing and requires no code until upstream adds them; when they appear, emit only those the receipt actually carries. |

Do not export prompts, answers, source excerpts, source labels or IDs, raw tool arguments, raw exception text, credentials, connection strings or generic model callbacks. Export failure remains exporter diagnostics and cannot affect answer or status behavior.

The exporter has a nonblocking queue of at most 256 unique (**thread_id**, **turn_id**) identities; duplicate notifications coalesce. When full, leave the receipt durable and rely on a startup scan and a one-second reconciliation scan for rows where **exported_revision < revision**. Intermediate receipt revisions are not an event log: the exporter may coalesce to the latest durable revision. Each captured payload is immutable and retried at most three times total, with a two-second request timeout and fixed waits of 250 milliseconds then 1 second between attempts. A successful response advances **exported_revision** to the exported payload revision without changing **revision** or **updated_at**; an ambiguous or failed response does not advance it. A delayed older acknowledgement cannot move the cursor backward. Duplicate remote delivery is possible, so no exactly-once remote-export guarantee is made. After saturation, restart or failure, the next scan reads the latest durable revision. Queue saturation and retry state never block answers. Shutdown drain is bounded by two seconds total.

After a client disconnect the segment root span still ends normally rather than being left open; T11's run-call supervision owns that guarantee, and the same effect is required of any later cancellation path.

## Success Criteria

These are required observations, not test results. Each must pass at the named interface; failure keeps its implementation ticket open.

- **SR-1:** Factory/lifespan evidence proves no import-time database or worker I/O, two applications isolate state, HTTP and auto-resume share the creating application's control, and acquired resources close once after injected startup failures and repeated shutdown. Preserve CLI, app import and dependency overrides. Maps to A17.
- **SR-2:** Start, interactive continuation, attachment auto-resume and crash recovery use the same typed lifecycle seam. Characterization proves the send-door reply critical section is serialized per thread from pending/history read through accepted-history retry recognition, conflict rejection, new-reply validation, Task Runtime pre-admission input-version increment, receipt projection, Runtime/Source Scope checkpoint commit and original-run resume; it covers retries after pending-question clear, concurrent identical retries causing one acceptance/continuation, conflicting replies never both succeeding, expired replies, still-applicable questions across unrelated input-version advancement, version gaps in the receipt projection, and no nested acquisition of a non-reentrant graph lock. It preserves send/resume history, accepts a valid clarification once on the same run and receipt, rejects stale/concurrent execution, and prevents duplicate cleanup from releasing a newer owner. Deployment rejects multiple process owners unless separately reviewed DB-backed ownership is selected. Maps to A14/A17.
- **SR-3:** Accepted-turn success, failure, pause, stop and crash-reconciliation cases leave one truthful receipt; duplicate finalizers and export retries do not double-count. Verify reopen behavior, null/incomplete measurements and exact observed stage/call counts. Successful thread deletion leaves zero receipt rows and no late recreation. Maps to A14.
- **SR-4:** Reproduce disconnect with sanitized stacks, database activity metadata and status timing. After repair, GET threads returns within five seconds, the next turn has no stale 409, and old work cannot append late output. Status excludes deliberate pauses and never waits on external or graph locks. Maps to A12.
- **SR-5:** Both modes are observed. With no exporter selected, a turn writes no span rows in local PostgreSQL while its answer and receipt still succeed. With `OTEL_TRACES_EXPORTER` selecting an exporter, inspect outgoing payloads and exporter-failure evidence: only allowlisted receipt and retrieval-rerank attributes, no user/source text or credentials, bounded failure and no answer-path dependency. Exactly the five named spans are present, trace and span ids are deterministic from the documented inputs, and no span references a missing parent. A span count that disagrees with receipt `calls` is recorded as diagnostics, not treated as a failure. Enabled live trace evidence is required; absence in the enabled mode is not a pass. Maps to A15.
- **SR-6:** Task outcomes and accepted-input histories satisfy the existing question-module contracts after extraction. A successful turn or receipt never independently marks a task complete. Maps to A20.

## Open Questions

No T0 design choices remain. The separate T1 runtime diagnostic must reproduce the actual hang using sanitized stacks, database activity metadata and status timing before selecting a bridge/isolation implementation. Source inspection alone does not identify the root cause or prove that the selected budgets are enforced. This spec makes no runtime repair or pass claim.

## Inherits / Changes

This spec consumes the following definition verbatim from [CONTEXT](../docs/CONTEXT.md):

> **run**: One graph `invoke` or `stream` on a thread (`run_id`): `done` / `refuse` / `step_cap` end the run; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Crash resume continues that in-flight `run_id` from the last complete host-graph node; at most one run is active per thread.

The adopted **turn receipt** entry in [CONTEXT](../docs/CONTEXT.md) defines one logical operational summary of an accepted answer turn, correlated across pause and continuation and distinct from task state and task completion.

This spec inherits the [context map](../docs/context-map.md), [Task Runtime](question-task-runtime.md), [Source Scope](question-source-scope.md), [Grounded Answer](question-grounded-answer.md), [Workspace](question-workspace.md) and [shared wire contract](question-task-runtime-contract.md). Task/checkpoint authority remains with Task Runtime; source authority remains with Source Scope; the host Runtime is the single writer of operational receipt projections. A receipt, snapshot, route label or server object is not a second task authority.

Retains ADR 0015/0026 and the existing synchronous durable graph. The accepted app-owned runtime lifetime and single per-run lifecycle are recorded in [ADR 0101](../docs/adr/0101-server-owns-runtime-lifetime.md). No accepted task/run definition changes.

**Selected persist list:** ADR 0101 for app-owned runtime lifetime and lifecycle ownership; the **turn receipt** glossary term. **Persist result:** Following fresh ACR PASS, domain-modeling accepted ADR 0101 and the turn receipt glossary entry and reconciled the existing context map; this records design ownership, not implemented resource behavior.

## Architecture Change Review

Fresh independent review on 2026-10-16 assessed the concrete adopted contracts, including reply admission/ownership, versioned scope, bounded inputs, receipt recovery, publication partial effects and release gates.

- Responsibilities, interfaces, and dependencies: **yes** — existing task/source/grounding authorities remain; app lifespan and execution ownership are explicit.
- Input contracts and test scenarios: **yes** — bounds and invalid, duplicate, stale, concurrent and permission cases are fixed at public seams.
- Failure propagation and partial effects: **yes** — checkpoint ordering, durable redo, causal failures, idempotent recovery and late-result fencing are explicit.
- Structural simplicity and caller burden: **yes** — existing checkpoint, pending question, snapshot, retrieve port and audit table are reused; no new provider or dependency.
- Task scope and migration: **yes** — financial compatibility, upstream-first expansion, supported-caller migration and existing acceptance / G1–G3 gates remain.

**OVERALL: PASS — 0 unresolved applicable concerns.** This is a design verdict, not product implementation or runtime acceptance.

## Plan handoff

[Plan 02](../plans/qa-chain-02-server-runtime.md) and [Plan 05](../plans/qa-chain-05-recovery-trace.md). The [program map](../plans/qa-chain-generalization.md) owns delivery dependencies and the operator's A01–A20 verification ledger, not a competing module contract.
