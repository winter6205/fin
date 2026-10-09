# Plan: Question Source Scope Completion

**Tracker:** the tracked change. This issue contains the complete plan; task labels are internal to this plan.
**Goal:** Close the remaining source-scope gaps from PR while keeping the approved source contract and the tracked change v1.0 behavior intact.
**Approach:** Materialize the approved the tracked change acceptance assets once, settle the current-authority and operation-capability facts, then enforce and verify the effective selection at existing source seams. Keep the Source Scope interface small: callers provide accepted intent, trusted current authority, and an operation; the module resolves one effective selection and returns a typed decision before an adapter reads data. This concentrates the policy behind the existing host seam without adding an authorization system, a second classifier, or a new acceptance contract.
**Spec link:** `specs/question-source-scope.md`
**Related plans:** [Runtime completion](question-task-runtime-completion.md), [Grounded Answer completion](question-grounded-answer-completion.md), and [Workspace completion](question-workspace-completion.md) consume the the tracked change assets materialized in this plan's T1. Grounded Answer and Workspace also consume this plan's current-authority and operation-capability facts at their named integration handoffs; their independent module implementation need not wait for this entire plan to finish. The original [Runtime plan](question-task-runtime.md) owns the shared scope representation in its T1 and the final integrated the tracked change gate.
**Dependencies:** Before any `src/` implementation, resolve the tracked change independently and prove that a clean default `uv sync` checkout passes the existing staged typecheck and pre-commit/CI path. That separate issue owns its tooling diff and verification; no `uv.lock` edit is expected because the declared extras are already pinned. The original Runtime T1 shared-contract decision remains the prerequisite for Source Scope implementation.
**Per-ticket loop (implementation bullets):** tdd → relevant typecheck/tests → verification-before-completion.
**Persist list:** empty. No new domain term or ADR decision is proposed.

## ACR

- **bounded-context-guardian: yes.** Source policy remains in the host Source Scope module; retrieve remains neutral; Runtime accepts and persists scope changes; Workspace presents the evaluated result.
- **input-contract-tests: yes.** Changed public operation contracts record applicability for empty, invalid/negative, overflow, concurrent authority/version change, and exception classes; each applicable class has an observable check, and any N/A has a concrete reason.
- **error-handling-enforcer: yes.** Existing typed outcomes remain distinct; changed failures use the named result vocabulary, and every new fallback path has a precise `// EXIT:` reason.
- **complexity-anti-drift: yes.** The plan reuses existing source/read seams and does not prescribe a general capability registry or second classifier.
- **minimal-change-verifier: yes.** the tracked change is an independent prerequisite, not a tooling diff in this plan. No new auth, tenant, store, provider, dependency, or lockfile change is included. Missing permission evidence blocks the affected implementation instead of being inferred as a grant.

**Overall: PASS for this implementation plan.** This verdict does not approve implementation or establish product acceptance.

## Entry prerequisite

**the tracked change — clean toolchain before Source Scope code.** The fix belongs to the tracked change and must land independently. From a fresh checkout, the documented default `uv sync` must be sufficient for an unrelated staged `src/` change to pass the existing `npm run typecheck:staged` and `.husky/pre-commit` path, with the same clean-bootstrap result in CI. Do not use `--no-verify` or blanket missing-import suppression. Use the already locked `ocr` and `object-store` extras if that is the selected fix; do not change `uv.lock` unless dependency pins actually change. Missing this evidence blocks Source Scope code acceptance.

## Tasks (ordered by dependency)

1. **Materialize the approved the tracked change v1.0 acceptance assets once** — tag: `[implementation]`
   - **Inherits:** approved v1.0 manifest: “Fixture authors create the material and its reference expectations from this contract before collecting candidate outputs.” It also states: “Skipped, unavailable, ambiguous, failed or deferred required evidence means ‘not accepted’ for that boundary/journey.” This materialization closes the tracked change.
   - **Surface:** The the tracked change acceptance contract and the four question-module acceptance assets.
   - **Acceptance:** The repository contains a versioned, reviewable copy of the approved J01–J22 manifest, its synthetic fixture materials, variant IDs, independently authored expected records, and the required H/M/B evidence and repeat rules. H covers J01–J22; M covers the 19 mapped cases, with J12 requiring M only when a real responding model owns failure interpretation; B covers J01–J21, with J22 inspected through J08/J21, and J17/J19 requiring H+B. A separate reviewer checks the expected records against the approved decisions and fixture text before candidate results are collected. The materials pin document identity, content hash, version, effective date, source scope, and permission/fault profile as applicable. H, M, and B remain separate required evidence classes; each M-required variant has three clean independent journey starts, each required B variant has one complete recorded execution, and a required skipped/unavailable/ambiguous/deferred result is blocked rather than accepted. Any changed expected behavior or case material gets a new version with prior results retained. The asset is linked by the Runtime completion plan; no fifth product spec or duplicate manifest is created.
   - **Headroom:** The acceptance location and fixture encoding remain implementation choices as long as the approved manifest, independent oracle, and evidence contract are executable and reviewable offline.
   - Status: [ ] pending

2. **Resolve what proves current operator permission** — tag: `[decision]`
   - **Inherits:** Source Scope decision 4: “A saved restriction is not an authorization grant. The host rechecks actual current permission, trusted thread ownership, source identity/version, readiness where relevant, operation capability, and applicable execution limits before every source path and evidence admission.” Issue says: “fin remains one operator per product.”
   - **Surface:** Existing host-agent authority facts, trusted thread/attachment ownership, shared corpus access, and the related the tracked change, the tracked change, and the tracked change decisions.
   - **Acceptance:** The decision identifies the existing trusted fact, if any, that establishes the single product operator’s current permission for the company corpus and current-thread attachments, and records whether the tracked change or the tracked change already owns any part of that decision. Thread binding, readiness, collection health, and attachment identity/version are not treated as operator permission by themselves. The decision maps an absent grant to denial and an inability to consult current authority to dependency failure using existing typed outcomes. No new identity, RBAC, tenant, provider, or permission store is introduced. If the existing facts cannot establish a current grant or the existing outcomes cannot represent the result, the permission-recheck implementation remains blocked and the unresolved product decision returns to the operator; absence of evidence is never allow-all.
   - **Headroom:** The decision settles the fact and its failure meaning without prescribing a new authority object or storage layout.
   - [parallel]
   - Status: [ ] pending

3. **Define restriction expressibility separately from source availability** — tag: `[decision]`
   - **Inherits:** Source Scope decision 5: “An operation that cannot express the approved restriction reports unsupported before returning source data.” Decision 7 keeps permission denial, unsupported or unavailable capability, dependency failure, and successful-empty distinct. This resolves the capability reachability gap tracked by the tracked change.
   - **Surface:** Existing host Source Scope checks, the neutral retrieve port, current corpus and attachment adapters, and named source failures.
   - **Acceptance:** The decision maps each current source operation to the adapter fact that proves it can express the approved restriction before data is returned. An adapter’s inability to express a restriction maps to the existing typed `unsupported` outcome; an unconfigured or unhealthy deployment, not-ready attachment, and successful-empty result retain their existing distinct outcomes. A general registry is introduced only if discovery shows multiple real adapters with independently varying capability maps; a hypothetical mapping is not sufficient. If no current operation can produce a truthful `unsupported` result, record that fact and keep the tracked change / SC-4 unaccepted rather than representing an outage as unsupported.
   - **Headroom:** The decision freezes the observable capability contract and ownership, not a registry shape, enum, or new failure vocabulary.
   - [parallel]
   - Status: [ ] pending

4. **Apply the complete accepted source selection before every existing source boundary** — tag: `[implementation]`
   - **Inherits:** Source Scope decisions 1, 3, 4, and 5: default eligibility is constrained by current authority; an explicit restriction is never silently broadened; authority is rechecked; enforcement precedes source data and evidence admission. Runtime alone accepts and persists a scope change against its current task/input basis. The missing-service bypass is part of the tracked change's named authority gap.
   - **Surface:** Source Scope in the host-agent context, the Runtime-owned shared scope contract, the neutral retrieve port, and existing attachment, corpus, locator, visual, fact/comparison, and evidence-admission operations.
   - **Acceptance:** The accepted selection is enforced before enumeration/retrieval, attachment search, direct known-locator reads, visual reads, fact/comparison reads, and derived-evidence admission. J03’s explicit “Use only the attached A1 policy” excludes the unrestricted corpus and other attachments; a follow-up gap stays within A1. A bare attachment mention still narrows the attachment branch only, consistent with ADR 0088: “Naming one attachment limits the attachment branch to it; when the question also asks for company material, the company branch remains eligible.” Missing attachment authority/service cannot bypass an explicit restriction into the pre-scope broad path; any independently authorized source that can still be used must remain independently scoped. Current permission, thread ownership, identity/version, readiness where relevant, and operation capability are rechecked at each boundary. The source module returns a proposal for Runtime to accept; it does not persist a scope change. No second classifier, semantic judge, or retrieval-technique picker is added.
   - **Input-contract evidence:** For each changed public operation/module contract, record and exercise applicable empty selection, invalid/negative reference or operation, oversized/overflow value within the existing input domain, concurrent authority/source-version change, and raised dependency exception cases. Mark a class N/A only with the unchanged contract/domain and reason; do not invent a numeric limit or test path.
   - **Error behavior:** Failures use existing typed outcomes. Denied, unsupported, unavailable, dependency failure, and successful-empty remain distinct. Every new fallback has a concrete `// EXIT:` condition and reason; an exception or missing authority never falls through to permitted.
   - **Headroom:** The observable restriction and pre-read guarantees are fixed; the implementation may choose how existing seams carry those facts while preserving the Runtime and retrieve ownership contracts.
   - **Depends on:** Original Runtime T1 shared contract, T1, T2, and T3.
   - **Blocks:** T5 and T6.
   - Status: [ ] pending

5. **Make unsupported reachable only when an adapter cannot express the restriction** — tag: `[implementation]`
   - **Inherits:** The T3 capability decision and Source Scope decision 7. Existing named availability failures remain the authority for `real_db_not_wired`, `tier_not_open`, community-summary failure, and other deployment/service outcomes. This closes the production reachability gap in the tracked change.
   - **Surface:** Existing Source Scope operation checks and the current adapter/retrieve seams that serve company and attachment operations.
   - **Acceptance:** A real host operation reaches `unsupported` before source data is returned when its adapter cannot express the approved restriction; the supported path still returns only in-scope results. Dependency outage, missing backend, readiness, and empty success continue to produce their existing distinct outcomes. The executable the tracked change profile for unsupported retrieval exercises the actual host decision and records the refusal before data access; an out-of-band unit-only gate drive does not establish this acceptance. No duplicate “unavailable” vocabulary or unconditional `True` capability wiring remains on the covered operations. If the capability decision finds no actual refusal case, this bullet remains blocked and no end-to-end unsupported claim is made.
   - **Input-contract evidence:** Apply the same per-contract applicability record for empty, invalid/negative, overflow, concurrency, and exception classes to any changed adapter-facing contract; preserve current adapter input domains.
   - **Error behavior:** Use the existing typed result and named failure. New catch/fallback paths require `// EXIT:` evidence and cannot convert exceptions to permission or unsupported.
   - **Headroom:** The adapter supplies the capability fact at the existing seam; its internal mechanism is left to implementation unless T3 identifies real variation requiring a shared interface.
   - **Depends on:** T3 and T4.
   - Status: [ ] pending

6. **Verify the visual scope decision across the real attachment path** — tag: `[implementation]`
   - **Inherits:** Source Scope decision 5 and [ADR 0088](../docs/adr/0088-thread-private-attachment-retrieval.md)'s trusted active-thread rule. the tracked change requires source denial and permitted positive cases to be evidenced separately; the tracked change records this live chain as unverified until it runs.
   - **Surface:** The existing live graph visual lane, durable Postgres attachment/thread stores, configured object store, production PDF renderer, and configured vision adapter.
   - **Acceptance:** Through the real host path, an out-of-scope and a cross-thread attachment are denied after only the authority metadata needed to identify the target has been checked, but before object-store bytes are fetched, the PDF renderer runs, page content is read, or the vision provider is called. The run proves zero object-byte reads, renderer calls, and vision calls for each refusal; necessary host metadata checks are allowed. A permitted in-scope attachment still reads real stored bytes through the renderer and produces the expected visual evidence, preventing an all-deny result from passing. The run records the real store, renderer, provider, source identity/version, host refusal, and operation receipts. Missing live services or provider configuration is recorded as blocked evidence, never as a pass or a green skip.
   - **Headroom:** The existing live adapter seams and assertions are required; fixture arrangement and evidence capture format remain implementation choices.
   - **Depends on:** T1, T2, T4, and T5; live prerequisites in the tracked change.
   - Status: [ ] pending

## Contract and success-criteria coverage

| Spec item | Plan coverage |
| --- | --- |
| [Decision 1](../specs/question-source-scope.md#confirmed-source-scope-decisions): corpus/current-thread eligibility; readiness separate from permission | T2, T4, T6 |
| [Decision 2](../specs/question-source-scope.md#confirmed-source-scope-decisions): ordinary question needs no source/technique preselection | T4; no new picker |
| [Decision 3](../specs/question-source-scope.md#confirmed-source-scope-decisions): explicit/inherited restrictions are never broadened | T1, T4 |
| [Decision 4](../specs/question-source-scope.md#confirmed-source-scope-decisions): saved intent is not current authority | T2, T4, T6 |
| [Decision 5](../specs/question-source-scope.md#confirmed-source-scope-decisions): enforce before reads and evidence admission | T3–T6 |
| [Decision 6](../specs/question-source-scope.md#confirmed-source-scope-decisions): model interprets; host enforces; no second classifier | T4, T5 |
| [Decision 7](../specs/question-source-scope.md#confirmed-source-scope-decisions): denied/unsupported/unavailable/dependency/empty stay distinct | T3, T5, T6 |
| [Decision 8](../specs/question-source-scope.md#confirmed-source-scope-decisions): direct limits; ask only when answer changes a feasible action | T4; Workspace presentation remains owned by its existing plan |
| [Decision 9](../specs/question-source-scope.md#confirmed-source-scope-decisions): Runtime accepts and persists scope changes | Original Runtime T1; T4 consumes that contract |
| [Decision 10](../specs/question-source-scope.md#confirmed-source-scope-decisions): the tracked change is the sole integrated acceptance contract | T1; Runtime retains the final gate |
| [SC-1](../specs/question-source-scope.md#success-criteria): default eligibility | T2, T4, T6 |
| [SC-2](../specs/question-source-scope.md#success-criteria): restriction and inheritance | T1, T4 |
| [SC-3](../specs/question-source-scope.md#success-criteria): current authority per operation | T2, T4–T6 |
| [SC-4](../specs/question-source-scope.md#success-criteria): truthful outcomes | T3, T5, T6 |
| [SC-5](../specs/question-source-scope.md#success-criteria): semantic boundary/actionable scope question | T4, with presentation in the existing Workspace plan |
| [SC-6](../specs/question-source-scope.md#success-criteria): module independence and neutral retrieve seam | Original Runtime T1; T3–T5 preserve the seam |
| [SC-7](../specs/question-source-scope.md#success-criteria): complete integrated acceptance | T1 materializes assets; Runtime owns the single final H/M/B gate |

## ADR 0088 interpretation

This plan preserves the accepted distinction between explicitly restricting a source set and merely naming an attachment. J03’s “Use only the attached A1 policy” is an explicit restriction; a bare mention of A1 does not exclude company material, especially when the same question asks for it. This does not reopen ADR 0088. An implementation that misses the distinction is a defect to fix under the existing contract; reopen ADR 0088 only if independent evidence shows its accepted rule itself cannot coexist with J03's explicit restriction.

## End-of-round code-review phase

After all implementation bullets, run one `code-review` phase over the Source Scope completion change. If `GATE: BLOCKED`, follow with `review-report-repair`. Runtime remains the owner of the final integrated the tracked change acceptance gate; all required evidence must be present, and any live prerequisite unavailable at review time remains blocked rather than accepted.
