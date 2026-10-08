# Plan: Question Source Scope

**Goal:** Every source operation and evidence admission uses the current effective scope formed from accepted user intent and trusted host authority.
**Approach:** First map the existing permission and capability facts, then resolve the effective selection and enforce it before source data or derived evidence crosses a boundary. Runtime owns accepted-input state, the shared contract, and host integration; Workspace owns presentation and clarification; Grounded Answer owns semantic sufficiency.
**Spec link:** `specs/question-source-scope.md`
**Dependencies:** Runtime T1 in `plans/question-task-runtime.md` freezes the shared representation at the retrieve seam before Source Scope implementation. This plan consumes that contract without selecting its encoding. Runtime owns host composition and the integrated the tracked change acceptance gate.
**Per-ticket loop (implementation bullets):** tdd → relevant typecheck/tests → verification-before-completion.
**Persist list:** empty.

## ACR

- **bounded-context-guardian: yes.** Runtime owns task state and host integration; peer modules retain separate responsibilities.
- **input-contract-tests: N/A.** No concrete API or product code is introduced. Public-entry contracts and tests belong to downstream implementation.
- **error-handling-enforcer: yes.** Permission, capability, dependency, evidence, checkpoint, and history failures remain distinct.
- **complexity-anti-drift: yes.** One integration owner and distinct module policies avoid duplicated responsibility; implementation complexity is not measured here.
- **minimal-change-verifier: yes.** Four decision specs only; the tracked change remains the sole integrated acceptance contract.

**Overall: PASS for architectural decisions only.** This review neither approves an implementation nor establishes product acceptance.

## Tasks (ordered by dependency)

1. **Map current authority and capability facts** — tag: `[decision]`
   - **Inherits:** Spec: “Eligibility, attachment readiness, and permission are separate facts.” Spec: “A saved restriction is not an authorization grant.” The existing ACL realization and mappings from real capabilities to operations remain technical investigation; this plan does not introduce new authority systems or values.
   - **Surface:** host-agent context, attachment capability, and retrieve context.
   - **Acceptance:** The decision identifies how existing facts establish current operator permission, trusted active-thread ownership, source identity/version, readiness, and operation capability; it names gaps instead of treating missing authority as allow-all. Any gap that requires a new authorization system, tenant model, store, provider, or ranking behavior is returned for operator decision. No new latency budget is assumed.
   - Status: [ ] pending

2. **Resolve requested intent into an effective scope or proposal** — tag: `[implementation]`
   - **Inherits:** Spec: “No user preselection or retrieval-technique choice is required for an ordinary question.” Spec: “An explicit or narrowed user restriction is a durable constraint on the requested selection and is never silently broadened.” Spec: “An inherited restriction means reuse of the same user intent, not reuse of prior permission.” Spec: “Runtime alone accepts and persists a scope change against its current input/task basis.”
   - **Surface:** host-agent context and retrieve context, using the Runtime-owned shared contract.
   - **Acceptance:** An ordinary question resolves to only the active corpus and current-thread attachments allowed by current authority; explicit and inherited restrictions remain constraints across operations and follow-ups; a requested change is returned for Runtime acceptance and is not persisted here. The result exposes current feasible capabilities and known limits for Workspace and reports known access limits directly. Workspace presents a scope question only when the answer could change a feasible permitted action, with no futile expand, upload, or continue choice. No classifier, semantic judge, retrieval-technique picker, or clarification UI is added here.
   - **Depends on:** Source T1 and Runtime T1.
   - Status: [ ] pending

3. **Enforce the current scope at every source and evidence boundary** — tag: `[implementation]`
   - **Inherits:** Spec: “Enforcement happens before source data crosses each boundary.” Spec: “The host rechecks actual current permission, trusted thread ownership, source identity/version, readiness where relevant, operation capability, and applicable execution limits before every source path and evidence admission.” The retrieve port remains neutral and retrieve-owned; existing corpus and attachment ports and preparation lifecycle remain in use.
   - **Surface:** host-agent context, attachment capability, and retrieve context.
   - **Acceptance:** Current authority and scope are checked before enumeration/retrieval, attachment search, direct known-locator reads, visual reads, facts/comparisons, and admission of text, visual, or numeric evidence. An operation that cannot express the approved restriction reports unsupported before returning source data; prior results or saved permission do not bypass revalidation. Permission denial, unsupported or unavailable capability, dependency failure, and successful-empty remain distinct, and readiness or backend health never grants permission. Source-side behavior maps to the tracked change; its case assignments, evidence levels, and repetitions remain in the Runtime-owned integrated gate.
   - **Depends on:** Source T2 and Runtime T1.
   - Status: [ ] pending

## Contract and success-criteria coverage

| Spec item | Plan coverage |
| --- | --- |
| [Decision 1](../specs/question-source-scope.md#confirmed-source-scope-decisions): default corpus and current-thread attachment eligibility; readiness separate from permission | T1, T2 |
| [Decision 2](../specs/question-source-scope.md#confirmed-source-scope-decisions): ordinary questions need no source or technique preselection | T2 |
| [Decision 3](../specs/question-source-scope.md#confirmed-source-scope-decisions): explicit and inherited restrictions are never silently broadened | T2 |
| [Decision 4](../specs/question-source-scope.md#confirmed-source-scope-decisions): saved intent is not current authority; recheck current facts | T1, T3 |
| [Decision 5](../specs/question-source-scope.md#confirmed-source-scope-decisions): enforce before every source read and evidence admission | T3 |
| [Decision 6](../specs/question-source-scope.md#confirmed-source-scope-decisions): model interprets intent; host deterministically enforces; no second classifier or judge | T2, T3 |
| [Decision 7](../specs/question-source-scope.md#confirmed-source-scope-decisions): denied, unsupported, unavailable, dependency failure, and successful-empty stay distinct | T3 |
| [Decision 8](../specs/question-source-scope.md#confirmed-source-scope-decisions): known access limits are direct; ask only when an answer changes a feasible permitted action; no futile expand/upload/continue choices | T2; [Workspace plan](question-workspace.md) presents the returned facts |
| [Decision 9](../specs/question-source-scope.md#confirmed-source-scope-decisions): Runtime alone accepts and persists scope changes | T2 |
| [Decision 10](../specs/question-source-scope.md#confirmed-source-scope-decisions): the tracked change is the sole integrated acceptance contract | T3; final gate owned by [Runtime](question-task-runtime.md) |
| [SC-1](../specs/question-source-scope.md#success-criteria): default eligibility | T1, T2 |
| [SC-2](../specs/question-source-scope.md#success-criteria): restriction and inheritance | T2, T3 |
| [SC-3](../specs/question-source-scope.md#success-criteria): per-operation authority | T1, T3 |
| [SC-4](../specs/question-source-scope.md#success-criteria): truthful operation outcomes | T3 |
| [SC-5](../specs/question-source-scope.md#success-criteria): semantic boundary and actionable scope question | T2; presentation remains in the [Workspace plan](question-workspace.md) |
| [SC-6](../specs/question-source-scope.md#success-criteria): module independence and neutral retrieve seam | T1–T3; shared representation comes from Runtime T1 |
| [SC-7](../specs/question-source-scope.md#success-criteria): full integrated acceptance | Runtime's single the tracked change gate; no module-level H/M/B repetition is added here |

## End-of-round code-review phase

After all implementation bullets, run one `code-review` phase over the Source Scope change. If `GATE: BLOCKED`, follow with `review-report-repair`. The final the tracked change observation remains owned by Runtime.
