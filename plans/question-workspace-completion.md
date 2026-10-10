# Plan: Question Workspace Completion

**Tracker:** the tracked change. This issue contains the complete plan; T1–T5 are internal tasks.
**Goal:** The research-thread workspace presents authoritative task, source, clarification, and input outcomes and restores archived work only after Runtime proves adoption.
**Approach:** Close the remaining Workspace gaps from #410 by first freezing how the existing authoritative projection and operation results reach the UI, then completing source/task visibility, clarification lifecycle, active input, and archive restore in the existing thread surface. Workspace implementation may proceed against the frozen Runtime T1 seam; integrated acceptance waits for the specific Runtime, Source Scope, and Grounded Answer facts each behavior consumes. Runtime remains the task authority and owns the single final the tracked change integration gate.
**Spec link:** [specs/question-workspace.md](../specs/question-workspace.md)
**Related issues:** archive restore state machine, active input decision and notices
**Related plans:** [Runtime completion (the tracked change)](question-task-runtime-completion.md), [Source Scope completion (the tracked change)](question-source-scope-completion.md), [Grounded Answer completion (the tracked change)](question-grounded-answer-completion.md)
**Implementation entry vs integrated acceptance:** Freeze the original Runtime T1 shared seam before Workspace implementation. Do not wait for all Runtime or Source Scope completion work before implementing the Workspace against that seam. Each bullet states its producer prerequisites for integrated acceptance separately; these dependencies do not make Workspace a prerequisite of Runtime or Source Scope.
**Per-bullet loop (implementation bullets):** TDD → relevant scoped typecheck/tests → verification-before-completion.
**Persist list:** empty. This plan uses existing product terms and introduces no glossary or ADR decision.

## Architecture Change Review

- bounded-context-guardian: yes — Workspace spec leaves task authority and persistence with Runtime and presentation with Workspace.
- input-contract-tests: N/A — the plan reuses the frozen Runtime T1 seam and selects no new public API or payload contract. Relevant browser normal/failure/stale/concurrent behavior remains covered under `.claude/rules/test.md`.
- error-handling-enforcer: yes — Workspace contract requires authoritative accepted, rejected, and failed outcomes; restore keeps the current task until correlated Runtime adoption.
- complexity-anti-drift: yes — one production send policy and notice path removes the duplicated gate, while restore state remains tied to thread and operation.
- minimal-change-verifier: yes — the plan addresses Workspace input and restore under the tracked change/the tracked change; Runtime retains restore adoption and final the tracked change evidence gate.

## Tasks (ordered by dependency)

1. **T1 — Freeze the Workspace projection and operation identity** — tag: `[decision]`
   - **Inherits:** “The conceptual UI seam is an authoritative current projection for display and a user-intent submission that preserves original text and question association.” “Before parallel implementation begins, the module owners must freeze one concrete shared representation for this seam.”
   - **Surface:** The existing research-thread Workspace presentation/input seam and the Runtime-owned projection.
   - **Acceptance:** The decision specifies how each submission result is associated with its originating thread and operation, so switching threads or a late result cannot repaint another operation's state. It names the existing authoritative projection fact that proves a requested archive became current. A send acceptance alone never proves restore adoption; a refused request outranks an older confirmation, and a failed or unreadable projection remains unresolved. The decision uses Runtime-owned facts and adds no Workspace task authority. If the frozen Runtime contract cannot prove adoption, the missing producer fact is assigned to Runtime T6 before restore acceptance is claimed.
   - **Completion:** A second implementer may choose a different internal state shape if it preserves thread/operation association and the same authoritative restore proof.
   - **Depends on:** The original Runtime T1 shared contract.
   - [blocks: T2, T3, T4, T5]
   - **Status:** [x] done

2. **T2 — Use one active-input decision for the composer and submission notices** — tag: `[implementation]`
   - **Inherits:** “While work is active, the operator can submit compatible progress, added context, task-change, or cancellation input through Runtime's serialized acceptance path.” “Keep input available and reflect Runtime's acceptance outcome. Do not start concurrent graph work for the same thread or automatically cancel current work whenever new input arrives. Runtime decides whether unaffected work continues or affected work is interrupted.”
   - **Surface:** The existing Composer and task-region surfaces, using the Runtime-owned input/run-control result.
   - **Acceptance:** Composer's submit control and its submit handler use the same active-input decision. An active run alone does not hold input; current host locks and in-flight uploads do. The task region displays pending, accepted, rejected, and unknown/failed submission outcomes from the operation associated with the current thread. Task outcome and clarification completion come only from the authoritative projection, and cancellation is shown only when run control reports it. The shipped path has one send policy and one production notice path, including the existing named refusal copy.
   - **Completion:** A second implementer may arrange the computation and rendering differently while Composer and the task region share one decision and do not infer host outcomes.
   - **Depends on:** T1 and the original Runtime T1 shared contract for implementation. Integrated SC-5 acceptance additionally requires Runtime completion T2–T4, including T3's active-run sequencing tracked by the tracked change.
   - [blocks: T3, T5]
   - **Status:** [x] implemented; integrated acceptance waits on the producers named in **Depends on**

3. **T3 — Keep clarification replies bound to their question through reload and refusal** — tag: `[implementation]`
   - **Inherits:** “Preserve the exact text, selected option, and association with the original question and its originating basis.” “A rejected stale reply does not mutate, reactivate, or answer another task. Preserve the original question association and the user's draft; never silently rebase the old reply onto a new question.”
   - **Surface:** The existing clarification card and Workspace submission/result presentation, with Runtime owning question lifecycle and reconciliation.
   - **Acceptance:** The full question and all options remain available with no preselected answer; Other text stays within the selected Other option. Dismissal submits no answer. A pending reply is not shown as accepted or answered; an accepted reply is reflected only after the authoritative projection clears that question. A stale refusal stays attached to the original question, preserves its draft, and cannot overwrite a newer question. Reload restores the authoritative pending question without an empty or bodyless resume request.
   - **Completion:** A second implementer may use a different card/store arrangement as long as the original association and draft survive refusal and reload with no optimistic answer.
   - **Depends on:** T1 and T2. Full SC-3/SC-4 acceptance additionally requires Runtime completion T2; the active-run reply variant also requires Runtime T3.
   - **Status:** [x] implemented; integrated acceptance waits on the producers named in **Depends on**

4. **T4 — Present task, source, answer-support, and gap facts from their owners** — tag: `[implementation]`
   - **Inherits:** “The current task goal, latest task outcome, latest turn result, and material unresolved gaps as reported by Runtime.” “The user's current source restriction and the host's freshly evaluated effective source set and actual operation capabilities.” “The sources actually attached to the current answer, separately from sources merely eligible to be searched.”
   - **Surface:** The existing task panel and research-thread projection; Runtime, Source Scope, and Grounded Answer retain their respective fact ownership.
   - **Acceptance:** The thread surface keeps task outcome, turn result, run status, archive status, and material gaps distinct, and never infers task completion from a run ending, citation, lookup, or response. It presents saved source restriction as intent, current effective scope/capabilities from fresh host evaluation, and answer sources separately from eligible sources. Accepted restriction changes pass through Runtime and only host-evaluated results change the displayed effective selection. Missing source evaluation remains unavailable rather than becoming a grant; unreported gaps remain unreported rather than becoming “none.” The existing thread surface presents source controls without a retrieval-technique picker or impossible access choices.
   - **Completion:** A second implementer may choose another mapping or rendering within the existing thread surface while preserving owner-provided facts and distinctions.
   - **Depends on:** T1 and the original Runtime T1 projection seam for implementation. Integrated SC-1/SC-2 acceptance additionally requires current authority, capability, and effective-selection facts from Source Scope completion T2–T4. Task-completion display additionally requires the Grounded Answer assessment producer and Runtime completion T5.
   - [parallel] — may proceed alongside T2 and T3 after T1.
   - [blocks: T5]
   - **Status:** [x] implemented; integrated acceptance waits on the producers named in **Depends on**

5. **T5 — Render archive restore from a correlated Runtime decision** — tag: `[implementation]`
   - **Inherits:** “Restore is explicit; Runtime rechecks current permission, versions, and evidence applicability before making it current. Until Runtime confirms restoration, keep showing the existing current task. Historical text and citations remain historical evidence and are not promoted to current support by restoration.”
   - **Surface:** The existing archive list, task region, and Runtime-owned restore/projection path.
   - **Acceptance:** The production archive surface consumes the shared restore decision. It offers restore only for a listed, restorable archive and sends only an explicit user action. While Runtime checks, the existing current task remains visible. Only the correlated fresh-projection proof selected in T1 marks the archive restored; a durable request acceptance alone does not. A current refusal wins over stale confirmation, a request Runtime can no longer hear is refused, and historical answers/citations remain historical. Refusals and unresolved projection refreshes are visible without claiming adoption. Repeated requests and thread switches cannot make one archive's status appear on another operation or thread.
   - **Completion:** A second implementer may choose a different view/store composition as long as adoption requires the same correlated Runtime fact and all non-adopted states keep the current task and history truthful.
   - **Depends on:** T1, T2, and T4 for the shared operation result and current-task presentation. Workspace implementation may use the frozen seam before upstream completion; integrated SC-6 restore acceptance additionally requires Runtime completion T2/T6 and Source Scope's current permission, capability, and effective-selection facts from T2–T4.
   - **Status:** [x] implemented; integrated acceptance waits on the producers named in **Depends on**

## T1 frozen seam

**Operation identity.** Each user-intent submission is recorded per thread with a browser-minted attempt counter used as a DISPLAY-ONLY correlation key: it is never sent to the host and never used to derive task truth, because the wire carries no operation id, request id, revision, or sequence. A result belongs to an operation only when it names that thread's current attempt, alongside the Runtime-owned facts the projection already carries (`task_id`, `question_id`, `archive_id`, `run_id`). A result is written to the record of the thread it names and is displayable only while that thread is open, so a thread switch or a late answer cannot repaint another operation's state.

**Restore adoption proof.** Adoption is proven by the existing authoritative projection alone: after a fresh projection reload for the same thread, `current_task.task_id` equals the `task_id` of the archive row the operator selected AND that `archive_id` is absent from `archived_tasks`. A durable request acceptance NEVER proves adoption — an accepted restore request reports only the request's own state. Both fields are already carried by the projection, so no additional producer fact is owed to Runtime T6 before restore acceptance is claimed.

**Precedence.** A refusal for the current attempt outranks an older confirmation: once a refusal is recorded for an attempt, a later accepted verdict cannot turn it into a confirmation, while a refusal arriving after a confirmation replaces it. Results whose thread or attempt is not the current one are dropped. A failed, unreadable, or refresh-failed projection stays `unresolved` and keeps the existing current task visible.

**Ownership.** Every fact is Runtime-owned; Workspace gains no task authority. Any fact the projection does not carry — material gaps, fresh source evaluation — is reported as unreported or unavailable and is never inferred.

**Portability and placement.** A second implementer may choose a different internal state shape provided it preserves this thread/attempt association and the same authoritative restore proof. The browser side of the seam is implemented in `web/src/workspaceOperation.ts` (identity and association only); the projection read stays in `web/src/taskRuntime.ts` and restore adoption stays with Runtime.

## Contract and acceptance coverage

| Workspace contract / criterion | Plan coverage | Integrated acceptance dependency |
| --- | --- | --- |
| Current task, turn result, and material gaps; no inferred completion (SC-1) | T4 | Runtime T5 and Grounded Answer assessment for task completion |
| Current source intent, effective scope/capability, and actual answer sources (SC-2) | T4 | Source Scope completion T2–T4; Grounded Answer source/evidence result |
| Complete clarification, exact association, dismissal, retry, and recovery (SC-3) | T3 | the tracked change J06/J07; Runtime T2 |
| Stale reply refusal preserves original association and draft (SC-4) | T3 | the tracked change J17; Runtime T2; active-run variant also Runtime T3 |
| Compatible input during active work without concurrent execution or automatic cancellation (SC-5) | T2 | the tracked change J14/J16; Runtime T2–T4, including the tracked change / Runtime T3 |
| Reload recovers current task and pending question; restore waits for current applicability and preserves history (SC-6) | T3, T4, T5 | the tracked change J15 (switch/explicit restore), J18 (compaction/history), J19 (checkpoint), and their applicable reload observations; Runtime T2/T5/T6 and current Source Scope facts |
| Required integrated browser coverage (SC-7) | T2–T5 contribute Workspace behavior | The approved the tracked change v1.0 gate, owned once by Runtime |

## Acceptance dependencies and evidence ownership

The frozen original Runtime T1 seam is the implementation-entry prerequisite. Workspace UI slices can be built and locally checked against it while peer completion work proceeds; full acceptance is gated by the producers of each displayed fact.

| Workspace workstream | Implementation may start after | Integrated acceptance requires |
| --- | --- | --- |
| T2: active input and notices | Original Runtime T1 and Workspace T1 | Runtime completion T2–T4, including T3 / the tracked change active-run sequencing |
| T3: clarification lifecycle | Workspace T1–T2 | Runtime completion T2; T3 for the active-run reply variant |
| T4: task/source/gap display | Original Runtime T1 and Workspace T1 | Source Scope completion T2–T4; Grounded Answer assessment; Runtime completion T5 for task-completion display |
| T5: archive restore | Workspace T1, T2, and T4 | Runtime completion T6 and current Source Scope facts from T2–T4; Runtime T6's dependencies on Runtime T2–T5 remain governed by the tracked change |
| SC-7 final integration | T2–T5 contribute the Workspace behavior | Source Scope completion T1 materializes the approved the tracked change v1.0 assets once; Runtime owns the single full the tracked change H/M/B gate |

Source Scope completion T1 owns the one-time materialization of the approved the tracked change v1.0 assets (tracked by the tracked change). Do not create a second manifest or repeat the full H/M/B contract per Workspace bullet. Workspace coverage maps SC-3 to J06/J07, SC-4 to J17, SC-5 to J14/J16, and SC-6 to J15 (switch/explicit restore), J18 (compaction/history), J19 (checkpoint), and the applicable reload observations attached to those journeys. These mappings use only the variants and repetitions required by the tracked change. The Runtime completion plan carries the mapped Runtime journeys, including J17/J19's H+B requirement. The final integrated gate retains the tracked change's full H/M/B assignments and repetitions and is owned by Runtime.

## End-of-round closeout

After T1–T5, run one code-review phase over the complete Workspace completion diff. If the gate is blocked, use the next slot for review-report-repair, then final verification-before-completion. Workspace-local evidence establishes only its presentation/input behavior; it does not replace the single integrated the tracked change observation.
