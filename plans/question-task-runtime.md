# Plan: Question and task runtime

**Goal:** Make the host runtime preserve and reconcile accepted question inputs, recover current and archived tasks, and report task outcomes independently from turn outcomes.
**Approach:** Freeze the shared contract after the required behavior and data inventory, then implement Runtime behavior in ordered end-to-end slices. Each slice includes its host integration at the relevant send, resume, question, recovery, execution, or outcome boundary; peer modules consume the shared contract and do not duplicate host integration ownership.
**Spec link:** [specs/question-task-runtime.md](../specs/question-task-runtime.md)
**ACR:** PASS for architectural decisions only; this is not implementation approval. The shared-contract decision in T1 is a prerequisite for implementation in all four module plans.
**Implementation workflow:** For each implementation bullet, use TDD, run only relevant typecheck/tests, and perform one `verification-before-completion` for that slice. The end-of-round code-review phase and final integration observation are recorded below; there is no per-bullet full review gate.

## Tasks (ordered by dependency)

1. **T1 — Inventory and freeze the shared contract** — `[decision]`
   - **Inherits:** “Before implementation, the owner must inventory current behavior and data that affect these choices, then freeze the shared wire/schema contract across the four module specifications.”
   - **Surface:** host-agent context and the four module boundaries.
   - **Acceptance:** The contract owners record the inventory outcome and agree on one shared representation and ownership boundary that all four plans can implement. The inventory covers archive encoding and checkpoint compatibility; legacy checkpoint population, recoverable original-input sources, and migration/data inventory; scheduling accepted input through the existing graph lock, run control, send/resume, and host transport; and exposure/enforcement of existing execution limits. These remain technical choices until the inventory resolves them; no new limit or product behavior is introduced.
   - Status: [x] done — inventory resolved into [specs/question-task-runtime-contract.md](../specs/question-task-runtime-contract.md): archive encoding, legacy-checkpoint reading, scheduling seams, and limit exposure. Representation, closed vocabularies, the wire projection, the named-failure set, and the module ownership table are frozen there.
   - [blocks: T2, T3, T4, T5, T6; blocks implementation in all peer plans]

2. **T2 — Accept and reconcile ordinary inputs** — `[implementation]`
   - **Inherits:** “the current LangGraph AgentState and configured checkpointer hold the authoritative task state”; “Every accepted input receives one semantic reconciliation before dependent work.” No separate classifier or second task store is introduced.
   - **Surface:** host-agent context; the existing send and resume flow.
   - **Acceptance:** An accepted message retains its original payload and provenance, advances input history once, and has one validated, persisted task transition, reconciliation marker, and execution disposition before dependent work begins. Resume continues from existing state without manufacturing another accepted input, and later model/tool iterations do not repeat reconciliation. A task version changes only when its goal, expected result, or necessary conditions materially change; progress that leaves them unchanged preserves applicable work. Taskless greeting or off-topic turns remain taskless and produce no task outcome.
   - Status: [x] done — `accept_input` is the single place `input_version` moves; the send door commits the accepted input before the graph runs; `reconcile_task` commits the task transition, marker, and disposition together and is idempotent per input; taskless turns stay taskless.
   - [blocks: T3, T4, T5, T6]

3. **T3 — Preserve the pending-question lifecycle** — `[implementation]`
   - **Inherits:** “Clarification is for actionable user-held information, not operational failure.” A clarification keeps its full question and choices, includes an Other path, and does not treat dismissal as an answer.
   - **Surface:** host-agent context; the agreed caller and question-reply boundary with Workspace.
   - **Acceptance:** An actionable request that needs clarification commits its task and question together. A clarification on an existing task stays attached to that task; a question created during a task transition binds to the resulting task state. The question preserves the full prompt and choices with exactly one Other choice. A valid reply retains the selected choice and submitted free text and advances input history once; dismissal leaves the question pending without completing or cancelling the task; a newer unrelated input alone does not expire the question; an obsolete reply is reported as expired and rejected without changing or restoring another task. Operational failures remain system outcomes rather than clarification prompts.
   - Status: [x] done (module + reply door) — `PendingQuestion` preserves prompt and choices with exactly one trailing Other; a question opened during a transition binds to the resulting task; a newer unrelated input no longer expires it; obsolete replies are refused as `task_reply_expired`. The question is opened from the track's own missing research slots — no model-driven clarification yet, since that is the grounded-answer peer's decision.
   - [blocks: T4]

4. **T4 — Archive, restore, and recover task history** — `[implementation]`
   - **Inherits:** “A thread has one current task and may keep locatable archived tasks.” Restore is explicit and rechecks current conditions, source authority, versions, and evidence applicability.
   - **Surface:** host-agent context; the existing checkpoint and history projections.
   - **Acceptance:** Run-scoped resets, resume, and compaction preserve thread-scoped task history. Reload recovers the persisted current task and pending question. Switching leaves one current task and a locatable archived snapshot with its run/evidence references. An explicit restore of a listed archive advances input history once, is reconciled through the same decision boundary, and makes the selected task current only after rechecking current conditions, source authority and scope, source identity/version, and evidence applicability; saved source scope is never treated as current permission, and old results remain historical until checks support adoption. Readers can obtain current and archived summaries and bounded original-input history subject to thread and source permissions; a missing original remains unknown and is never inferred from a summary.
   - Status: [x] done (archive + restore contract; host wiring partial) — switch archives the outgoing task and leaves exactly one current task; restore is explicit and passes four current rechecks before adoption; the envelope is thread-scoped and omitted from `reset_input_state`, so reset, resume, and compaction preserve it; legacy checkpoints read as empty with no backfill. The send door accepts an explicit `restore_archive_id`, and the graph refuses adoption until the source-authority seam supplies the rechecks. Switch detection compares this utterance's company against the company the COMMITTED task was opened for; it reuses the host's existing company-change signal rather than a second classifier, and is not yet model-driven (the tracked change).

5. **T5 — Sequence active-run input and handle cancellation truthfully** — `[implementation]`
   - **Inherits:** “The host maintains one active graph execution per thread”; a delayed operation does not justify a second invocation, blanket rejection, or automatic cancellation.
   - **Surface:** host-agent context; the existing per-thread execution and run-control boundary.
   - **Acceptance:** An input during delayed work is durably accepted and sequenced through the active execution. Progress preserves unaffected work; a validated switch or cancellation affects only the relevant work where supported. Late receipts remain tied to their originating task and execution and cannot overwrite current state before current permission, applicability, and evidence checks support adoption. Cancellation records the requested intent and actual run-control report without claiming an external effect was undone without evidence. Input, permission, dependency, capability, insufficient-evidence, unresolved-applicability, missing-intent, execution-limit, checkpoint, and history failures remain distinguishable; failed checkpoint application preserves the last consistent state.
   - Status: [ ] partial (cancellation and late-receipt binding done; SC-6 sequencing NOT done) — an input during an active run is still rejected by the pre-existing per-thread 409 guard, so the accept-and-sequence acceptance point is UNMET. Changing the lock contract is a host-wide behaviour change outside this module's scope; tracked as the tracked change. What IS delivered: late receipts stay bound to their originating task and execution via `run_id` and `evidence_refs`; `decide_cancellation` records requested intent and the run-control report without claiming an undid effect.

6. **T6 — Apply grounded task outcomes separately from turn outcomes** — `[implementation]`
   - **Inherits:** “Task outcome and turn outcome are separate.” Runtime accepts the peer-owned grounded assessment only against the committed current task and source scope.
   - **Surface:** host-agent context; the agreed Runtime and Grounded Answer result boundary.
   - **Acceptance:** A turn can end with an answer, partial result, clarification, cancellation, or system failure while its task remains active. Runtime applies the peer assessment only against committed task and source-scope state, and marks a task complete only when the assessment supports the requested goal, has no material unresolved gap, and host gates for permission, source identity/version, receipts, and persistence pass. Without a grounded assessment, only truthful cancellation or execution-failure outcomes are allowed; a taskless turn has no task outcome.
   - Status: [x] done (boundary wired; peer not yet built) — `apply_assessment` is the only path to `complete` and requires a supported assessment, no material gap, and all four host gates; a turn ending never completes a task on its own; a taskless turn has no task outcome. The `task_outcome` graph node applies the grounded-answer peer's assessment at the turn's terminal status; with no peer wired it records the task as still ACTIVE rather than complete, so SC-7's completion path stays unreachable until the peer lands (the tracked change).

## Runtime contract coverage

| Runtime success criterion | Plan coverage |
| --- | --- |
| SC-1: Accepted input, original provenance, one input-history advance, and committed reconciliation | T2–T4 |
| SC-2: Progress may preserve task version and applicable work | T2, T5 |
| SC-3: Taskless dialogue and atomic initial clarification | T2, T3 |
| SC-4: Dismissal, Other text, stale replies, and question binding | T3 |
| SC-5: Single current task, archive, and validated restore | T4 |
| SC-6: Delayed-operation sequencing and late-result revalidation | T5 |
| SC-7: Grounded task completion remains distinct from turn completion | T6 |
| SC-8: Distinct failures, truthful cancellation, and last-consistent checkpoint | T3, T5, T6 |
| SC-9: Runtime module cases and final product acceptance | T2–T6; final the tracked change observation below |

**Final four-module integration observation (once):** After all four plans' outcomes are integrated, apply the tracked change's full case assignments, variants, and repetition rules: H for J01–J22; M for its 19 mapped cases, with J12 requiring M only when a real responding model owns failure interpretation; B for J01–J21, with J22 browser inspection covered through J08/J21. J17 and J19 require H+B. Establish independent expected outcomes before reviewing candidate results; retain and review required failures. H is deterministic host evidence; M uses the real configured model across at least three clean independent journey starts; B observes the actual application and persistence path. These evidence levels do not substitute for each other, and canned outputs do not count as live-model evidence. Runtime's independent evidence covers its mapped J06/J07 and J11–J20 cases, including applicable J12 variants; it does not replace this final gate.

**Persist list:** empty.

## Architecture Change Review

- bounded-context-guardian: **yes** — Runtime owns task state and host integration; peer modules retain their boundaries.
- input-contract-tests: **yes** — `task_runtime` accepts three public inputs (message / reply / restore). The boundary-class allocation is in [specs/question-task-runtime-contract.md](../specs/question-task-runtime-contract.md) and is exercised by `tests/host/test_task_runtime_contract.py` (empty, negative, overflow), `test_task_runtime_lifecycle.py` (expired/stale/unknown), and `test_task_runtime_host.py` (concurrent, exception, legacy checkpoint).
- error-handling-enforcer: **yes** — permission, capability, dependency, evidence, checkpoint, and history failures remain distinct.
- complexity-anti-drift: **yes** — ownership is separated; implementation complexity is not measured at this decision stage.
- minimal-change-verifier: **yes** — four decision specifications only; the tracked change remains the sole integrated acceptance contract.

**Overall: PASS for architectural decisions only.** This review neither approves implementation nor establishes product acceptance.

## End-of-round closeout

After T2–T6, run one code-review phase over the Runtime change. If the gate is blocked, the next slot is `review-report-repair`; verify the resulting repairs before closing the task. The final the tracked change observation above remains a single four-module integration gate.
