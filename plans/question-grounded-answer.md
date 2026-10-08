# Plan: Question grounded answer

**Goal:** The existing grounded model flow returns a support-aware assessment that covers the user's goal, names material gaps, selects only useful feasible next actions, and proposes completion only when the requested result is supported.
**Approach:** Implement the settled semantics as four outcomes in the existing grounded assessment flow: claim support and provenance, whole-goal coverage and conflicts, useful continuation and actionable clarification, then task-completion judgment. The assessment consumes the committed Runtime and Source Scope basis and returns a proposal; Runtime retains validation, state application, persistence, and shared host integration.
**Spec link:** `specs/question-grounded-answer.md`
**External prerequisite:** Runtime T1 in `plans/question-task-runtime.md` freezes the shared proposal contract before these implementation bullets begin. Runtime owns shared graph, prompt, transport, and state integration. This plan depends on no peer plan drafts.
**Per-ticket loop (all implementation bullets):** TDD → relevant module-local typecheck+tests → verification-before-completion. Issue #392 H/M/B integrated acceptance belongs to the Runtime integration round and is not repeated per module.

## ACR

- **bounded-context-guardian: yes.** Runtime owns task state and host integration; peer modules retain separate responsibilities.
- **input-contract-tests: N/A.** No concrete API or product code is introduced. Public-entry contracts and tests belong to downstream implementation.
- **error-handling-enforcer: yes.** Permission, capability, dependency, evidence, checkpoint, and history failures remain distinct.
- **complexity-anti-drift: yes.** One integration owner and distinct module policies avoid duplicated responsibility; implementation complexity is not measured here.
- **minimal-change-verifier: yes.** Four decision specs only; #392 remains the sole integrated acceptance contract.

**Overall:** PASS for architectural decisions only. This review does not approve an implementation or establish product acceptance.

## Tasks (ordered by dependency)

1. **Judge support and provenance for each material conclusion** — tag: `[implementation]`
   - **Inherits:** “Each material conclusion must be supported by evidence for its meaning, subject, applicability, source scope, version, time, and material conditions. Related evidence is not support by association. Narrow or withhold an unsupported assertion.” Also: “Identify what a source states directly. Label calculation, synthesis, and inference as such and make the supporting premises clear. A user's confirmed situation may be a task condition; it is not evidence for a policy fact.” The model cannot verify its own evidence or claim that a host check passed; archived assistant text is not independent evidence for a current claim.
   - **Surface:** Existing host-agent grounded assessment flow; shared graph and prompt integration remain with Runtime.
   - **Acceptance:** Independent module-local examples show that a conclusion with mismatched subject or applicability is withheld or narrowed, source statements remain distinguishable from derived claims, and a user condition is not promoted into policy evidence. Current claims rely on evidence admitted for the current task and scope; a failed host check constrains or removes the affected assertion. Existing identity, citation, and numeric checks remain independent of this semantic judgment.
   - **Depends on:** Runtime T1 in `plans/question-task-runtime.md` (external shared-contract freeze).
   - Status: [ ] pending
   - [blocks: T2]

2. **Assess the whole goal and preserve material gaps or conflicts** — tag: `[implementation]`
   - **Inherits:** “Evaluate every material requested part. Deliver supported portions as partial progress when other material parts remain unresolved. Do not treat an optional task decomposition or generated TODO list as the goal's authority.” Also: “Preserve a conflict unless evidence establishes which source applies or prevails.”
   - **Surface:** Existing host-agent grounded assessment flow; presentation remains with Workspace.
   - **Acceptance:** The assessment accounts for every material requested part, can return supported portions while leaving unresolved portions explicit, explains how missing evidence, unestablished applicability, or an unknown necessary user condition can affect the requested result, and leaves an operative-source conflict unresolved until the available evidence establishes which source applies or prevails. An answer asking what the conflict is may receive the evidenced conflict without implying that its operative rule was resolved.
   - **Depends on:** Runtime T1 in `plans/question-task-runtime.md` and T1.
   - Status: [ ] pending
   - [blocks: T3]

3. **Choose only useful checks and actionable clarification** — tag: `[implementation]`
   - **Inherits:** “Propose another check only for a named gap, a currently available permitted capability, and a reasonable prospect of obtaining useful evidence. Repeating an exhausted direction without a new basis is not progress.” Also: “Ask only for user-held information that can change a feasible next step or conclusion. A source-coverage gap alone does not justify a futile question.” Successful lookup without support, permission limits, unsupported conditions, dependency failure, and exhausted recovery remain distinct; supported findings are preserved when another part fails.
   - **Surface:** Existing host-agent grounded assessment flow; capability truth comes from the host and current permitted scope comes from Source Scope. Workspace owns question presentation.
   - **Acceptance:** An assessment proposes a check only when it names the gap and has a permitted, available capability with a reasonable prospect of useful evidence; it asks only for user-held information that can change the result or feasible next step; and it distinguishes the listed failure outcomes while retaining supported findings across a failure in another part. It stops when the goal is supported or feasible checks are exhausted or blocked by existing host limits.
   - **Depends on:** Runtime T1 in `plans/question-task-runtime.md` and T2.
   - Status: [ ] pending
   - [blocks: T4]

4. **Propose completion only when the user's requested result is supported** — tag: `[implementation]`
   - **Inherits:** “Complete only when supported conclusions and their conditions satisfy the user's requested result. Partial or unresolved work remains incomplete even if the turn ends, a citation/numeric check passes, all available tools have been tried, or every TODO is cleared. A turn may end with supported partial findings and an explicit unresolved task.” Assessment begins only after the required task/input and scope basis is committed; Runtime validates the proposal against the current basis and host checks, then alone applies and persists the outcome.
   - **Surface:** Existing host-agent grounded assessment flow returning its proposal through the Runtime-owned shared contract; this task does not own task state.
   - **Acceptance:** A supported requested result may be proposed complete, while a partial or unresolved result remains incomplete even when the turn ends or mechanical checks pass; an uncommitted bootstrap or same-decision transition cannot be proposed as grounded completion. The assessment can propose ending a turn with supported partial findings and unresolved work, and it never represents its proposal as persisted task state.
   - **Depends on:** Runtime T1 in `plans/question-task-runtime.md` and T3.
   - Status: [ ] pending

## Contract coverage

Decision clauses 1–2 map to T1; 3–4 to T2; 5–7 to T3; and 8 to T4. Across all bullets, the assessment uses the committed task/input and source-scope basis, actual available evidence, host-reported capabilities, and truthful operation receipts; Runtime reconciles each accepted input once, and an unavailable source is not treated as a successful empty search. Runtime alone validates, applies, and persists a proposed outcome. The six success criteria map as follows: claim-specific support, provenance, and independent hard checks to T1; insufficiency and conflict to T2; failure and feasible continuation to T3; multi-part completion and turn/task separation to T2 and T4. Their #392 journey references remain coverage anchors, not a new module acceptance gate.

**Persist list:** empty. This plan adds no glossary term or ADR decision.

## End-of-round review

After all bullets, run code-review once. If the gate is blocked, use the next slot for review-report-repair; then run final verification-before-completion for the delivered plan implementation. Runtime owns the integrated #392 H/M/B acceptance round.
