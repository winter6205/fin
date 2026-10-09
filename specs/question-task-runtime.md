# Spec: Question and task runtime

> **Status:** Decision-level module specification for independent development. The shared wire contract and implementation details remain downstream decisions.
> **Architecture review:** Passed decision-level review; see the review record below. This is not implementation approval.
> **Integration:** One of four module specifications. Issue #392 remains the only integrated product-acceptance contract.

**Decision basis:** [issue map #386](https://github.com/oocang/fin/issues/386) and its approved QE-01–QE-06 decisions.

## Objective

Define the decisions for how the host runtime recognizes a task, preserves user input and question history, switches or restores tasks, and
distinguishes task completion from a turn ending. The runtime must remain recoverable across sends, clarification pauses, compaction, task switches,
and resume.

This document fixes behavior and ownership. It deliberately leaves concrete wire shapes, serialization, scheduler plumbing, and shared schema
alignment for a later contract-freezing step before parallel implementation.

## Confirmed decisions and provenance

1. **Existing host state remains the only task authority.** The current LangGraph AgentState and configured checkpointer hold the authoritative task
   state; ThreadStore terminal rows, run status, and research snapshots are projections or separate concerns. Provenance: [issue #390 task-state
   decision](https://github.com/oocang/fin/issues/390#issuecomment-6059300396); [ADR 0002](../docs/adr/0002-langgraph-runtime.md), [ADR
   0005](../docs/adr/0005-agent-loop-verifier-langgraph-durability.md), and [ADR 0015](../docs/adr/0015-postgres-checkpointer.md).
2. **A thread has one current task and may keep locatable archived tasks.** Switching preserves a historical task snapshot and its run/evidence
   references. Explicit restore rechecks current conditions, source authority, versions, and evidence applicability. Provenance: [issue
   #390](https://github.com/oocang/fin/issues/390#issuecomment-6059300396); [ADR 0026](../docs/adr/0026-workspace-streaming-resume.md).
3. **Task and input changes have distinct versions.** Every accepted message, valid clarification reply, or explicit restore request advances input
   history once. A task version changes only when its goal, expected result, or necessary conditions materially change. A revision mismatch alone does
   not invalidate unchanged work. Provenance: [issue #390](https://github.com/oocang/fin/issues/390#issuecomment-6059300396); [issue #392
   J13–J18](https://github.com/oocang/fin/issues/392#issuecomment-6061003025).
4. **Every accepted input receives one semantic reconciliation before dependent work.** The model proposes the decision; the host validates and
   persists a consistent task transition, reconciliation marker, and execution disposition together. No separate classifier or second task store is
   introduced. Provenance: [issue #390](https://github.com/oocang/fin/issues/390#issuecomment-6059300396); existing routing in
   [graph.py](../src/fin/host/graph.py).
5. **Clarification is for actionable user-held information, not operational failure.** Preserve the full question and choices, provide an Other path,
   and retain the selected choice and any original free text. Dismissal supplies no answer; obsolete replies cannot revive another task. Provenance:
   [issue #389](https://github.com/oocang/fin/issues/389#issuecomment-6059301489); [issue #392
   J07](https://github.com/oocang/fin/issues/392#issuecomment-6061003025).
6. **Task outcome and turn outcome are separate.** A turn may end with an answer, partial result, clarification, cancellation, or system failure while
   a task remains active. A run ending successfully does not by itself establish task completion. Provenance: [issue
   #389](https://github.com/oocang/fin/issues/389#issuecomment-6059301489); [issue
   #390](https://github.com/oocang/fin/issues/390#issuecomment-6060725856); [issue #392
   J12/J19/J20](https://github.com/oocang/fin/issues/392#issuecomment-6061003025).
7. **Source selection and grounded assessment belong to peer modules.** Runtime consumes the source-scope decision and the evidence-grounding
   assessment, but does not redefine their meaning, duplicate their policy, or treat a saved source selection as current permission. Provenance:
   [issue #390 responsibility decision](https://github.com/oocang/fin/issues/390#issuecomment-6060725856);
   [source-scope spec](question-source-scope.md); [grounded-answer spec](question-grounded-answer.md).
8. **Host application integration has one owner.** The host integration owner wires graph compilation, HTTP entry points, prompts, stream/payload
   mapping, and history projections. Peer module development returns decisions or proposals and does not co-edit those integration files. Provenance:
   approved four-module ownership decision in this task.
9. **The four module specs are independently developed; integration is covered by #392.** There is no fifth integration-acceptance spec. Concrete
   shared wire contracts must be frozen downstream before parallel implementation. Provenance: user's direction in this task and [issue
   #392](https://github.com/oocang/fin/issues/392#issuecomment-6061003025).
10. **No new provider, retrieval backend, service target, or numeric execution budget is selected here.** Continue to respect existing host
    capabilities and limits. Provenance: [issue #390](https://github.com/oocang/fin/issues/390#issuecomment-6060725856); [issue
    #392](https://github.com/oocang/fin/issues/392#issuecomment-6061003025).

## Boundaries

- **Does:** Decide task/input authority and version semantics; original-input provenance; mandatory reconciliation; question and reply lifecycle;
  archive, restore, and history recovery; active-run input ordering; cancellation and late-result handling; task/turn outcome distinction; and failure
  responsibilities.
- **Confirms with human:** No behavior decision remains open in this draft. Technical choices listed under Open Questions remain unselected by the
  issue map and require inventory or later contract alignment before implementation.
- **Out of this spec:** Exact field names and payload shapes; endpoint signatures; checkpoint serialization, migration mechanism, and archive
  encoding; source-scope policy and UI presentation; grounding/evidence sufficiency rules; graph scheduler internals; numeric budgets; retrieval
  strategy; and implementation plans, fixtures, or test code.

## Runtime decision contract

Runtime is the host's decision boundary for the current task, accepted input history, and pending questions. It does not replace the configured
checkpoint authority. Run-scoped resets must not erase thread-scoped task history.

For each input that the host accepts, Runtime records the original user payload and its provenance before semantic work. It advances input history
once and asks for one model decision against the current task and the new input. The host checks that decision against the latest state, current
permissions, actual capabilities, and receipts, then commits a consistent transition, reconciliation progress, pending-question changes, and execution
disposition before starting dependent retrieval, tools, or answer formation. Model and tool iterations after that decision do not re-run input
reconciliation.

A caller may submit a new message, a reply to a current question, or an explicit choice to restore an archived task. Runtime returns whether the input
was durably accepted and whether its meaning has been reconciled, together with a bounded current view or a typed failure. A resumed execution
continues from existing state without manufacturing another user input. Readers can obtain current-task and archived-task summaries, and request
bounded original-input history subject to thread and source permissions. A missing original remains unknown; summaries or assistant prose never become
user confirmation.

When there is no current task, ordinary dialogue such as a greeting or off-topic turn may remain taskless. It receives a conversational response
without fabricating a task or task outcome. An initial actionable request that needs clarification establishes a task and its question in the same
committed transition. Clarification of an already-current task remains attached to that task. Questions created during a task transition bind to the
resulting task state, not a previous or null task basis.

A clarification preserves the complete question, its choices, and exactly one Other choice. A reply preserves the selected choice and any free text as
submitted. Dismissal leaves the question pending and neither completes nor cancels the task. A reply is valid only for the still-current question and
relevant task conditions; a newer unrelated input alone does not expire it. If the question or task is obsolete, Runtime rejects the reply and
explains that it has expired without silently reopening an archived task.

A switch archives the outgoing task in the same authoritative history and leaves exactly one current task. Restore is an explicit user request
directed at a listed archived task and reconciled through the same model decision boundary. It returns the selected task to current work only after
the host rechecks the current conditions, source authority and scope, source versions, and evidence applicability. Old results remain historical until
a current assessment supports their adoption. A saved source choice is not itself live authorization; callers use the source-scope module's current-
authority view.

An input arriving during a delayed operation must be durably accepted and sequenced through the existing per-thread execution. The host maintains one
active graph execution per thread; a busy graph does not justify a second invocation, blanket rejection, or automatic cancellation. A progress
decision preserves unaffected work. A validated switch or cancellation interrupts only affected work where supported. Every in-flight operation and
late receipt stays tied to its originating task and execution; adoption requires current permission, applicability, and evidence checks. A newer input
version alone does not invalidate unchanged results.

Cancellation follows reconciled user intent. The host records what cancellation was requested and what the run control actually reported; it never
claims an external effect was undone without evidence. Dependency failure, access denial, missing capability, insufficient evidence, unresolved
applicability, missing user intent, execution limit, and checkpoint or history failure remain distinct. Operational failure is reported as a system
outcome, not converted into a clarification to keep work alive. Failed checkpoint application leaves the last consistent task state authoritative, and
already-completed external effects are not rolled back by assertion.

GroundingAssessment and SourceScope remain peer-owned concepts. Runtime accepts their proposals only against a committed current task and source
scope. For task transitions or source-scope changes, the host commits first; grounded assessment and any task outcome then use the resulting current
state without repeating input reconciliation. Runtime maps the assessment's task and turn conclusions consistently into the task outcome. A complete
outcome requires supported coverage of the requested goal, no material unresolved gap, and accepted host hard gates for permissions, source
identity/version, receipts, and persistence. Host mechanics do not certify semantic support. Without a grounded assessment, only constrained, truthful
cancellation or execution-failure outcomes are allowed; no current task means no task outcome.

## Module interface and ownership

The module boundary describes what callers provide and receive: original input, question reply, explicit restore choice, current task/archives,
bounded history, reconciliation status, outcome, or typed failure. It does not require callers to coordinate model calls, checkpoint writes, graph
nodes, or transport details themselves.

Runtime owns the current-task and pending-question lifecycle and consistent application of peer proposals. The source-scope module owns source
selection, enforcement, and the view of current effective authority. The grounded-answer module owns evidence assessment and its semantic gaps.
Workspace owns question UI and source controls. Peers return proposals and values through the shared contract; they do not write host graph state or
edit host integration surfaces.

One host integration owner alone adapts module decisions to graph.py, compile.py, workspace_http.py, prompts, stream and payload projections, and
ThreadStore history. The existing AgentState/checkpointer remains the single authority. The current per-thread graph lock and run control are
constraints to compose with; they are not permission to introduce conflicting lock ownership, a concurrent graph run, a second classifier, or another
task store.

## Success Criteria

1. Accepted messages, valid replies, and explicit restore requests retain original payload and provenance and advance input history once. Dependent
   work starts only after that input has one committed reconciliation decision.
2. A progress-only input can preserve the current task version and unaffected work. A mismatch in input version alone does not discard otherwise
   applicable work.
3. A taskless greeting or off-topic turn remains taskless and has no task outcome. An initial actionable request needing clarification commits a task
   and its question together.
4. Question dismissal, Other text, stale replies, and question binding follow the lifecycle above; stale replies never mutate or restore another task.
5. Task switch leaves one current task and a locatable archive. Restore is explicit, host-validated, and rechecks conditions, permissions, source
   versions, and evidence before adopting old results.
6. During a delayed operation, progress, switch, or cancel is accepted and sequenced without another graph execution, blanket busy rejection, or
   automatic cancellation. Late results cannot overwrite current task state and are adopted only after revalidation.
7. Turn completion remains distinct from task completion. A task is complete only when the grounded assessment supports the whole requested goal, no
   material gap remains, and host hard gates pass. Partial work and system failure leave an unresolved task active.
8. Input, permission, dependency, checkpoint, history, cancellation, and execution-limit failures remain distinguishable and truthful. Failed
   operations preserve the last consistent checkpoint and do not become user clarification.
9. Runtime is independently verified against its mapped issue #392 cases J06/J07 and J11–J20, including J12's applicable variants. This module does
   not claim full product acceptance. At final four-module integration, the complete H/M/B matrix from [issue
   #392](https://github.com/oocang/fin/issues/392#issuecomment-6061003025) is required: H for J01–J22; M for the 19 mapped cases, with J12
   requiring M only when a real responding model owns failure interpretation; B for J01–J21, with J22 browser inspection covered through J08/J21. J17
   and J19 require H+B. No fifth integration spec is added.

## Open Questions

These are technical choices not selected by the issue map, not unresolved user behavior or permission to add new product choices:

- Archive encoding and compatibility representation within the existing checkpoint.
- The legacy checkpoint population, recoverable original-input sources, and migration/data inventory. No migration has been validated by this spec.
- How accepted inputs and decisions are scheduled through the existing graph lock, run control, send/resume paths, and host transport surfaces.
- How existing execution limits are exposed and enforced. No new numeric budget or service target is selected.

Before implementation, the owner must inventory current behavior and data that affect these choices, then freeze the shared wire/schema contract
across the four module specifications. That is a downstream technical alignment step, not an implementation plan included here.

## Inherits / Changes

**Exact context vocabulary inherited from docs/CONTEXT.md:**

> **host agent**:
> fin as the LangGraph runtime that routes tracks and calls grounded-track tools behind the **retrieve port**; it is not the ranking or vector store.

The [context map](../docs/context-map.md) places the host-agent context in src/fin/host. This specification keeps that bounded context and the status
snapshot distinct from task authority.

**Inherits:** the existing AgentState and configured LangGraph checkpointer (ADRs 0002, 0005, 0015); existing thread/run resume and history identity
(ADR 0026); run-scoped reset behavior; per-thread graph locking and run control; and the approved #389/#390 decisions.

**Changes:** makes the host task lifecycle, original-input provenance, unified reconciliation order, question/restore behavior, task/turn distinction,
and module ownership explicit. It introduces no second store, classifier, glossary term, or new ADR.

For question-experience behavior, these approved decisions supersede older requirements in
[soft-track-judge-route.md](soft-track-judge-route.md) and [search-mode-host.md](search-mode-host.md) that turn operational failures into clarification
or require the operator to choose a retrieval technique. Internal retrieval capabilities remain available to the model within host constraints.

**Persist list:** empty.

## Architecture Change Review

Independent review of all four decision-level specifications on 2026-10-09:

- **bounded-context-guardian: yes.** Runtime owns task state and host integration; peer modules retain separate responsibilities.
- **input-contract-tests: N/A.** No concrete API or product code is introduced. Public-entry contracts and tests belong to downstream implementation.
- **error-handling-enforcer: yes.** Permission, capability, dependency, evidence, checkpoint, and history failures remain distinct.
- **complexity-anti-drift: yes.** One integration owner and distinct module policies avoid duplicated responsibility; implementation complexity is not measured here.
- **minimal-change-verifier: yes.** Four decision specs only; #392 remains the sole integrated acceptance contract.

**Overall: PASS for architectural decisions only.** This review neither approves an implementation nor establishes product acceptance.
