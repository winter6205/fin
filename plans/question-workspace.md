# Plan: Question Workspace

**Goal:** Operators can see authoritative question state, provide correctly associated input, and recover or restore work in the existing research-thread surface.
**Approach:** Present the current task, source evaluation, answer support, and gaps from their owning modules; then complete clarification, active-input, reload, and explicit-restore flows through the frozen shared seam. Runtime owns all host mapping, persistence, and the final integration observation.
**Spec link:** `specs/question-workspace.md`
**Dependencies:** Runtime T1 in `plans/question-task-runtime.md` freezes the shared contract and is the only cross-plan prerequisite before implementation. The Runtime projection, question, input, and restore capabilities named below are seam contracts, not dependencies on completed Runtime or peer implementations; each module can implement against the agreed boundary independently. Real provider outcomes are required only for Runtime's final integrated production observation.
**Implementation choices left open:** Screen layout, visual hierarchy, copy, accessibility implementation, component boundaries, and transport/retry details remain with implementation within the observable contracts.
**Per-ticket loop (implementation bullets):** tdd → relevant typecheck/tests → verification-before-completion.
**Persist list:** empty.

## ACR (decision-only)

- **bounded-context-guardian: yes.** Runtime owns task state and host integration; peer modules retain separate responsibilities.
- **input-contract-tests: N/A.** No concrete API or product code is introduced. Public-entry contracts and tests belong to downstream implementation.
- **error-handling-enforcer: yes.** Permission, capability, dependency, evidence, checkpoint, and history failures remain distinct.
- **complexity-anti-drift: yes.** One integration owner and distinct module policies avoid duplicated responsibility; implementation complexity is not measured here.
- **minimal-change-verifier: yes.** Four decision specs only; the tracked change remains the sole integrated acceptance contract.

**Overall: PASS for architectural decisions only.** This review neither approves an implementation nor establishes product acceptance.

## Tasks (ordered by dependency)

1. **[parallel] W1 — Present current task, source, and answer state** — tag: `[implementation]`
   - **Inherits:** “The current task goal, latest task outcome, latest turn result, and material unresolved gaps as reported by Runtime.” “The user's current source restriction and the host's freshly evaluated effective source set and actual operation capabilities.” “The sources actually attached to the current answer, separately from sources merely eligible to be searched.”
   - **Surface:** Existing research-thread workspace and Runtime-owned current projection.
   - **Acceptance:** Through the frozen seam, the thread surface shows task outcome, turn result, and material gaps separately from run status; it shows the saved restriction as user intent rather than current permission, and reports source evaluation as unavailable without presenting a source as available. Sources used by the answer remain separate from eligible sources. A run ending, lookup succeeding, answer arriving, or citation appearing never makes the workspace infer task completion. Ordinary questions need no source or retrieval-technique picker, and impossible source choices are absent.
   - **Depends on:** Runtime T1; the current projection is a capability of the frozen shared contract.
   - Status: [ ] pending

2. **[parallel] W2 — Complete and safely associate clarification replies** — tag: `[implementation]`
   - **Inherits:** “Show the complete question before its options.” The operator may choose an offered option or Other; free text appears inside the selected Other option. Preserve “the exact text, selected option, and association with the original question and its originating basis.” Dismissing a clarification “supplies no default answer” and leaves it pending. A transport retry resends “the identical reply with its original question association.”
   - **Surface:** Existing research-thread workspace and Runtime-owned question submission/result boundary.
   - **Acceptance:** The complete question appears before all options and no answer is preselected; Other text stays within the selected Other choice. The exact choice and free text are submitted with their original question association, and the workspace shows an answer only after Runtime accepts it. Dismissing or collapsing leaves the question pending without answering or cancelling the task. A retry preserves the same reply and association. A rejected stale reply remains tied to its original question, keeps the user's draft, and is never rebased onto a newer question; the returned current state governs the rest of the display.
   - **Depends on:** Runtime T1; question acceptance and rejection are represented by the frozen boundary.
   - Status: [ ] pending

3. **[parallel] W3 — Keep compatible input available during active work** — tag: `[implementation]`
   - **Inherits:** “While work is active, the operator can submit compatible progress, added context, task-change, or cancellation input through Runtime's serialized acceptance path.” The workspace keeps input available and reflects authoritative outcomes; it does not start concurrent graph work or automatically cancel current work when new input arrives.
   - **Surface:** Existing research-thread workspace and Runtime-owned active-input/run-control boundary.
   - **Acceptance:** While a turn is visibly active, an operator can submit compatible input and observe pending submission followed by Runtime's accepted, rejected, or failed result. The workspace does not optimistically mark a task changed, a clarification answered, or work cancelled; it neither launches concurrent work nor assumes Runtime cancelled unaffected work.
   - **Depends on:** Runtime T1; serialized input and run-control outcomes are represented by the frozen boundary.
   - Status: [ ] pending

4. **W4 — Recover the thread presentation after reload** — tag: `[implementation]`
   - **Inherits:** “On opening or reloading a thread, the authoritative projection provides the current task, pending clarification, current source evaluation, outcome, and archive summaries needed for display.” Reopening a pending question “does not require an empty or bodyless resume request.” Display recovery is separate from resuming graph execution.
   - **Surface:** Existing research-thread workspace and Runtime-owned persisted current projection.
   - **Acceptance:** Reload restores the current task, pending question, source evaluation, outcome, and archive summaries from authoritative state without submitting empty input or triggering graph execution. A pending clarification remains recoverable and its original association remains intact.
   - **Depends on:** W1, W2, and Runtime T1; persisted display projection is a capability of the frozen shared contract.
   - Status: [ ] pending

5. **W5 — Restore archived work only after Runtime confirms** — tag: `[implementation]`
   - **Inherits:** “Archiving is separate from completing or cancelling a task.” Restore is explicit; Runtime rechecks current permission, versions, and evidence applicability. “Until Runtime confirms restoration, keep showing the existing current task.” Historical text and citations remain historical evidence and are not promoted to current support by restoration.
   - **Surface:** Existing research-thread workspace and Runtime-owned archive/restore boundary.
   - **Acceptance:** An archived task can retain its outcome from when it was archived, while archive status stays separate from completion or cancellation. Restore requires an explicit user action; while Runtime is checking it, the existing current task remains current in the workspace. The workspace switches only after Runtime confirms restoration, and historical answers or citations remain labeled as history rather than current support or current authorization.
   - **Depends on:** W4 and Runtime T1; restore confirmation is represented by the frozen boundary.
   - Status: [ ] pending

## Contract and success-criteria coverage

| Workspace contract / criterion | Plan coverage |
| --- | --- |
| Current task, turn, gaps, current source scope, and actual answer sources | W1; SC-1, SC-2 |
| Complete clarification, Other text placement, exact association, dismissal, retry, stale reply and draft | W2; SC-3, SC-4 |
| Compatible input while active; authoritative outcome; no concurrent work or automatic cancellation | W3; SC-5 |
| Persisted reload, pending-question recovery without bodyless resume, archive summaries | W4; SC-6 |
| Explicit restore waits for Runtime and preserves historical evidence | W5; SC-6 |
| Required end-to-end browser journeys | W1–W5; SC-7, with final the tracked change evidence owned by Runtime |

## End-of-round code-review phase

After all Workspace implementation bullets, run one `code-review` phase over the Workspace change. If `GATE: BLOCKED`, follow with `review-report-repair`. Runtime owns the single final the tracked change integration observation; this plan adds no per-slice H/M/B repetitions.
