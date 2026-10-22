# Spec: Question experience shared contract

> **Adopted QA-chain amendment (2026-10-16):** The operator accepted the four presented design groups, including the scoped persistence design. The representation below incorporates document-choice replies and versioned source restrictions. [Routing/grounded](qa-chain-grounded-experience.md), [server/receipts](qa-chain-server-runtime.md), and [catalog/retrieval](qa-chain-corpus-retrieval.md) retain their respective behaviour contracts. This is documentation adoption; implementation and the journey acceptance contract's acceptance are not claimed.

> **Status:** Frozen shared wire/schema contract for the four question-experience modules. This is the contract-freezing step [plans/question-task-runtime.md](../plans/question-task-runtime.md) T1 required before any module is implemented; it selects representations the decision-level specs deliberately left open.
> **Authority:** Implements [question-task-runtime.md](question-task-runtime.md), [question-source-scope.md](question-source-scope.md), [question-grounded-answer.md](question-grounded-answer.md), and [question-workspace.md](question-workspace.md). Where this file names a field, that spelling is the one every module uses; the module specs remain authoritative for behaviour.
> **Not an acceptance contract.** The journey acceptance contract remains the only integrated product-acceptance gate.

## Objective

Freeze one representation for the seam the four module specifications deliberately did not model: the wire shapes, the version counters, the failure tokens, and the ownership boundary that let Task Runtime, Source Scope, Grounded Answer, and Workspace be implemented independently without two modules inventing the same fact twice.

## Inventory outcome

The four specs left four technical choices open. Each was resolved against the current code rather than invented:

| Open question | What the inventory found | Decision |
| --- | --- | --- |
| Archive encoding and compatibility representation | The checkpointer already stores arbitrary JSON per key on `AgentState` (`status_snapshots` is the precedent), and ThreadStore has no task table. | Archives live INSIDE the one `task_runtime` envelope on the checkpoint, encoded as plain dicts. No new table, no new store, no new encoding format. |
| Legacy checkpoint population, recoverable original-input sources, migration | Threads predating this work have no `task_runtime` key at all. There is no migration mechanism in this repo and inventing one is out of scope. | **No backfill.** `from_state_dict(None)` reads as the empty envelope, so a legacy thread reads as taskless — unknown, never fabricated. An accepted input whose original was not recoverable stores `payload: null` and stays null. |
| Scheduling accepted inputs through the lock, run control, send/resume, transport | The send door already owns input admission. A bodyless crash resume has no new input; an answer to a document clarification is a real reply. | All user replies pass **send-door admission** first; the accepted record rides **run config**, and the committed envelope rides **run input state**. A validated document reply then resumes its original run. `Command(resume=...)` never creates an accepted input by itself. |
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
                        "choices", "input_version", "purpose",
                        "origin_run_id", "choice_details" } | null,
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

### Document clarification and accepted-input scheduling

The existing `pending_question` is the only question/candidate authority, including while the graph is interrupted. Its extension is:

```jsonc
{
  // Existing question/task/input fields and ordered choices are retained.
  "purpose": "task_clarification" | "document_choice",
  "origin_run_id": string | null,
  "choice_details": [
    { "choice_id": string, "label": string, "summary": string,
      "source_ref": {
        "source_kind": "corpus" | "attachment",
        "source_id": string, "version": string,
        "section": string | null
      } }
  ]
}
```

`purpose` defaults to `task_clarification` on existing questions. Such questions have `choice_details=[]` and may have `origin_run_id=null`. A new `document_choice` requires its original nonempty `origin_run_id`, one to four distinct admitted source choices and exactly one trailing `other` in `choices`. `choice_details` IDs equal the non-Other IDs in the same order; an attachment reference is bound to the trusted current thread. Each choice must identify an admitted hit, not merely an eligible source. Labels and summaries are presentation metadata, not citation evidence or stored permission.

For new document choices, IDs and source versions are nonempty strings of at most 256 UTF-8 bytes; labels and section labels are at most 512 bytes; summaries are nonempty, at most 1,024 bytes and one evidence-supported sentence. The complete question is nonempty and at most 8,192 bytes; reject an oversized proposal before publishing it, without truncating away material meaning. Reject control characters in identifiers/labels; prose may contain ordinary line breaks. These limits do not retroactively truncate unrelated existing task questions or original user input.

The Workspace reply uses the existing `question_id`, `choice_id`, and optional `free_text` fields at the send door. A click has `payload=null`, with its original structured IDs retained; do not fabricate user-authored prose from the label. Other submits `choice_id=other` and preserves the user's exact nonempty `free_text` as the reply payload. Opening the Other input is presentation-only. The UI label for Other on document choices is “换个问法”.

Admission validates the current question/task conditions, originating thread/run/interrupt, candidate membership and current source identity/version/permission before accepting a document reply. A still-applicable question does not expire solely because a later unrelated input advanced `input_version`. An invalid/foreign/expired reply makes no input or scope write. An identical retry is recognized by the original `(thread, question_id, choice_id, free_text)` in accepted history: continue any unfinished reconciliation without incrementing again; if already reconciled, return the existing outcome without launching work. A conflicting second answer is rejected; an old reply outside retained history remains expired, never a new input.

An accepted reply advances `input_version` once and durably preserves the original payload/provenance. Runtime performs its existing one reconciliation decision per accepted input. Source Scope returns a checked proposal; Runtime alone commits the reconciled envelope and any accepted scope snapshot together in the same checkpoint update. Only after this commit may the original paused run continue through `Command(resume=...)`. Keep the same `run_id`, logical answer turn and receipt; a new accepted input is not necessarily a new graph run. A checkpoint or reconciliation failure leaves the accepted input explicitly unreconciled and prevents dependent retrieval/answer work; retry reuses that input version. A commit-before-resume crash resumes the committed continuation without accepting the reply again.

Bodyless crash resume, display reload, search-mode-only control, and attachment auto-resume create no user input. A document-choice or Other reply does. An accepted scope change made by a real message/reply follows ordinary reconciliation; an unchecked token cannot mutate the snapshot. Task/input and scope writes must respect the thread's current execution ownership so a late older continuation cannot overwrite a newer accepted decision.

The Grounded Answer budget of one decision/synthesis per evidence set is separate from Runtime input reconciliation and existing tool planning. Count every actual model call; do not disguise a second answer synthesis as reconciliation, repair or tool planning. Reconciliation must finish before grounding against the resulting task/scope basis.

### Versioned source intent in the existing snapshot

`StatusSnapshot` gains one `source_scope` value; it is part of the existing append-only checkpoint history, not a second store:

```jsonc
{
  "schema_version": 1,
  "mode": "unrestricted" | "restricted",
  "sources": [
    { "source_kind": "corpus" | "attachment",
      "source_id": string, "version": string }
  ],
  "accepted_input_version": int
}
```

`unrestricted` requires `sources=[]` and means the currently authorized corpus plus eligible sources in the trusted current thread. `restricted` requires one to 200 distinct source identities. Source IDs and versions are nonempty strings of at most 256 UTF-8 bytes with no control characters. The counter is a nonnegative signed 64-bit integer naming the accepted message/reply that established this intent; it never confers permission. An empty restricted selection, unknown kind, malformed value or overflow is `task_input_invalid`, with no source I/O or scope write. An attachment source ID must resolve to the active thread under current host authority. Source identity/version mismatch is `task_applicability_unresolved`; denied access is `task_permission_denied`. Missing capability and dependency failures retain their existing distinct failures. None broadens the scope.

Runtime is the single writer after accepting a Source Scope proposal. The newest snapshot carries forward the last accepted `source_scope` across mode-only changes, company changes and fresh unscoped questions. Only explicit, reconciled user scope intent replaces it. A clear writes `mode=unrestricted`, not an omitted key. Pending candidates do not change it. Every list, retrieval, direct/visual read, fact/comparison and evidence admission intersects the requested restriction with freshly checked authority and version/capability facts; no source bytes are fetched beyond that boundary. Restricting to a corpus document also excludes unrelated attachments; choosing an attachment never publishes it to the corpus.

`document_ids` is a legacy derived projection only: for a new restricted snapshot it contains the selected corpus IDs, and for unrestricted it is empty. It cannot express attachment-only restrictions; all supported consumers must use `source_scope` before attachment choices are enabled. Never infer unrestricted access from `document_ids=[]` when `source_scope` is present. Do not dual-write two independently interpreted selections. A legacy nonempty document restriction without versioned `source_scope` remains restrictive and requires current source confirmation before producing a versioned snapshot; missing version/provenance is not an allow-all fallback. No backfill or migration is required; new seed generation writes the selected representation.

### Wire projection

`GET /api/threads/{id}` gains `task_runtime`, carrying `current_task`, `archived_tasks`, `pending_question`, `reconciliation`, `task_outcome`, `input_version`, and `input_history`. It is **separate from `turns` and `status`** — a turn can end with an answer while its task stays active, so neither is derivable from the other.

`input_history` is the bounded ORIGINAL accepted-input window (the last 200, newest last). It is here because the decision spec requires readers to obtain bounded original-input history, not only the current task; the bound is applied by the reader so asking cannot widen it, and an input whose original was never recoverable reads as `payload: null` rather than being reconstructed.

The existing thread projection also exposes `source_scope` from the latest snapshot, together with Source Scope's freshly evaluated capability/authority view. The saved value alone cannot be presented as a current grant. `task_runtime.pending_question` carries the complete document-choice projection above, so reload needs no bodyless execution resume. Existing SSE `run_id` continues to correlate the original and resumed answer. Receipt fields are separately defined by [the server contract](qa-chain-server-runtime.md); no telemetry field can create a task or infer completion.

### Named failures

One closed set in `fin.host.task_runtime_types.TASK_FAILURE_KINDS`: `task_input_invalid`, `task_input_unreconciled`, `task_already_reconciled`, `task_permission_denied`, `task_dependency_failure`, `task_capability_unavailable`, `task_capability_unsupported`, `task_insufficient_evidence`, `task_applicability_unresolved`, `task_intent_missing`, `task_execution_limit`, `task_checkpoint_failed`, `task_history_failed`, `task_reply_expired`, `task_reply_unknown`, `task_restore_unknown`, `task_restore_not_adopted`.

These join the existing 具名失败 family in `fin.host.named_failures` rather than replacing it. The rule every module inherits: **an operational failure is reported as a system outcome and is never converted into a clarification.** The amended [route spec](soft-track-judge-route.md) now agrees with this boundary; its historical judge-outage-to-clarify requirement is superseded.

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
