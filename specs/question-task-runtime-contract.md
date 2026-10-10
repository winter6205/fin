# Spec: Question experience shared contract

> **Status:** Frozen shared wire/schema contract for the four question-experience modules. This is the contract-freezing step [plans/question-task-runtime.md](../plans/question-task-runtime.md) T1 required before any module is implemented; it selects representations the decision-level specs deliberately left open.
> **Authority:** Implements [question-task-runtime.md](question-task-runtime.md), [question-source-scope.md](question-source-scope.md), [question-grounded-answer.md](question-grounded-answer.md), and [question-workspace.md](question-workspace.md). Where this file names a field, that spelling is the one every module uses; the module specs remain authoritative for behaviour.
> **Not an acceptance contract.** Issue #392 remains the only integrated product-acceptance gate.

## Objective

Freeze one representation for the seam the four module specifications deliberately did not model: the wire shapes, the version counters, the failure tokens, and the ownership boundary that let Task Runtime, Source Scope, Grounded Answer, and Workspace be implemented independently without two modules inventing the same fact twice.

## Inventory outcome

The four specs left four technical choices open. Each was resolved against the current code rather than invented:

| Open question | What the inventory found | Decision |
| --- | --- | --- |
| Archive encoding and compatibility representation | The checkpointer already stores arbitrary JSON per key on `AgentState` (`status_snapshots` is the precedent), and ThreadStore has no task table. | Archives live INSIDE the one `task_runtime` envelope on the checkpoint, encoded as plain dicts. No new table, no new store, no new encoding format. |
| Legacy checkpoint population, recoverable original-input sources, migration | Threads predating this work have no `task_runtime` key at all. There is no migration mechanism in this repo and inventing one is out of scope. | **No backfill.** `from_state_dict(None)` reads as the empty envelope, so a legacy thread reads as taskless — unknown, never fabricated. An accepted input whose original was not recoverable stores `payload: null` and stays null. |
| Scheduling accepted inputs through the lock, run control, send/resume, transport | The send door already runs before the graph and already owns the 409 lock probe; a resume carries no new user input. | Acceptance happens in the **send door**, the accepted record rides **run config**, and the committed envelope rides **run input state**. Resume passes no accepted input, so it cannot manufacture one. |
| How execution limits are exposed and enforced | `STEP_CAP` / `REPAIR_CAP` / `RECURSION_LIMIT` already exist in `fin.host.state`; no numeric budget is selected by the specs. | Reuse those caps unchanged. `task_execution_limit` is a reserved named failure with **no new numeric budget**, so the cap value stays owned by `state.py` alone. |

## Representation

### Thread-scoped envelope

One key on `AgentState`, thread-scoped, omitted from `reset_input_state`:

```jsonc
{
  "current_task": { "task_id", "goal", "expected_result", "task_version",
                    "necessary_conditions", "outcome", "outcome_reason",
                    "created_at_input_version" } | null,
  "archived_tasks": [ { "archive_id", "task", "archived_at_input_version",
                        "run_id", "evidence_refs" } ],
  "input_history": [ { "input_version", "kind", "payload", "provenance",
                       "question_id", "archive_id", "choice_id", "run_id" } ],
  "input_version": int,
  "pending_question": { "question_id", "task_id", "task_version", "prompt",
                        "choices", "input_version" } | null,
  "reconciliation": { "reconciled_input_version", "task_id", "task_version",
                      "disposition", "reason" } | null,
  "task_outcome": "active" | "complete" | "cancelled" | null,
  "cancellations": [ { "requested_reason", "run_control_reported",
                       "at_input_version", "task_id", "effect_undone" } ]
}
```

**Why one key and not sibling channels:** the task transition, its reconciliation marker, its pending question, and its execution disposition commit together. Sibling keys would let a partial write look like a committed decision — the exact failure the contract forbids.

### Closed vocabularies

| Vocabulary | Values |
| --- | --- |
| input kind | `message`, `reply`, `restore` |
| execution disposition | `none`, `start`, `continue`, `switch`, `restore`, `await_reply`, `cancel` |
| task outcome | `active`, `complete`, `cancelled` |
| Other choice | exactly one, id `other`, always trailing |

### The two counters

`input_version` advances exactly once per accepted input — message, valid reply, or explicit restore — and never moves for anything else. `task_version` moves only when `goal`, `expected_result`, or `necessary_conditions` materially change. They are deliberately separate: a progress input advances the first and preserves the second, and a `task_version` mismatch alone never invalidates work computed under an unchanged goal.

### Wire projection

`GET /api/threads/{id}` gains `task_runtime`, carrying `current_task`, `archived_tasks`, `pending_question`, `reconciliation`, `task_outcome`, `input_version`, and `input_history`. It is **separate from `turns` and `status`** — a turn can end with an answer while its task stays active, so neither is derivable from the other.

`input_history` is the bounded ORIGINAL accepted-input window (the last 200, newest last). It is here because the decision spec requires readers to obtain bounded original-input history, not only the current task; the bound is applied by the reader so asking cannot widen it, and an input whose original was never recoverable reads as `payload: null` rather than being reconstructed.

### Named failures

One closed set in `fin.host.task_runtime_types.TASK_FAILURE_KINDS`: `task_input_invalid`, `task_input_unreconciled`, `task_already_reconciled`, `task_permission_denied`, `task_dependency_failure`, `task_capability_unavailable`, `task_capability_unsupported`, `task_insufficient_evidence`, `task_applicability_unresolved`, `task_intent_missing`, `task_execution_limit`, `task_checkpoint_failed`, `task_history_failed`, `task_reply_expired`, `task_reply_unknown`, `task_restore_unknown`, `task_restore_not_adopted`.

These join the existing 具名失败 family in `fin.host.named_failures` rather than replacing it. The rule every module inherits: **an operational failure is reported as a system outcome and is never converted into a clarification.** That is the shared red line that supersedes `specs/soft-track-judge-route.md`'s fail-to-clarify rule.

## Ownership

| Module | Owns | Consumes | Never does |
| --- | --- | --- | --- |
| Task Runtime | the envelope above; current task, archives, accepted input, reconciliation, pending question, task outcome, cancellation record | `GroundingAssessment`, `RestoreRecheck`, `HostGates` as proposals | decide what evidence supports; read or persist sources |

| Source Scope | source selection, enforcement, the current-authority view | task basis | write host graph state; store scope in a second store |
| Grounded Answer | whether evidence supports the goal; material gaps | committed task and scope basis | own task identity; persist; add a second judge |
| Workspace | question UI, source controls, thread presentation | the wire projection | change task state without an explicit user action |

Task Runtime is the single writer of the envelope. Peers return proposals; Runtime validates and commits them.

A proposal the seam has not yet produced is read as its conservative default, never as permission: with no `GroundingAssessment` recorded, the outcome boundary leaves the task ACTIVE; with no `RestoreRecheck`, a restore is refused `task_applicability_unresolved`. Silence from a peer module is never a licence to complete or adopt anything.

## The two rules every module inherits

1. **A saved source choice is user intent, not current authorization.** `RestoreRecheck.source_authority_current` cannot be satisfied by pointing at an archive; Workspace never presents a saved restriction as a live grant.
2. **Task outcome is not turn outcome.** Only `apply_assessment` — a supported assessment with no material gap plus all host gates passing — reaches `complete`.

## Persist list

Empty, inherited from the four module specs. The new terms (`pending question`, `task outcome`, `input version`, `effective scope`) are contract field names, not new CONTEXT.md glossary entries; `task outcome` in particular must never be read as the existing Soft-Track `track` label or as a run status.