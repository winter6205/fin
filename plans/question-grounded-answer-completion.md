# Plan: Question Grounded Answer Completion

**Tracker:** [#413](https://github.com/oocang/fin/issues/413). This issue contains the complete plan; T1–T5 are internal tasks.
**Goal:** Deliver the Grounded-owned semantic assessment contract and behavior that the existing host model loop can consume, while Runtime remains the authority for current-basis validation and task outcomes.
**Approach:** Grounded owns its proposal/result schema, internal evidence conversion, semantic rules, and module-local evidence. The host integrator wires one post-commit assessment into the existing model loop under Runtime completion T5; Grounded adds no independent classifier or model loop. The Source Scope completion plan owns the sole #392 v1.0 acceptance package, and Runtime owns the one final H/M/B integration gate.
**Spec links:** [Grounded Answer](../specs/question-grounded-answer.md), [Task Runtime](../specs/question-task-runtime.md), [Source Scope](../specs/question-source-scope.md)
**Related gap issues:** [#408 — default pytest behavior without extras](https://github.com/oocang/fin/issues/408), [#409 — graph helper ownership](https://github.com/oocang/fin/issues/409)
**Related plans:** [Runtime completion (#406)](question-task-runtime-completion.md), [Source Scope completion (#405)](question-source-scope-completion.md), [Workspace completion (#414)](question-workspace-completion.md)
**External prerequisites:** The shared contract in T1 of [the original Runtime plan](question-task-runtime.md) must be accepted before implementation. Source Scope completion T2/T3 freezes the current-authority and operation-capability representation Grounded consumes. [Source Scope completion T1](question-source-scope-completion.md) owns the canonical #392 v1.0 assets and independent expected records; this is the formal expected-evidence gate, not a prerequisite for coding. Integrated Grounded acceptance also needs Source Scope T4's enforced current-authority/capability facts, and T5 when claiming an unsupported-capability path. The Grounded producer handoff unblocks Runtime completion T5; Grounded module delivery does not wait for all Runtime tasks. Grounded's final product evidence depends on Runtime T5 and the required current Source Scope facts, but the final #392 gate remains one Runtime-owned observation.
**Test-environment prerequisite:** Resolve [#408](https://github.com/oocang/fin/issues/408) separately to establish default-suite behavior and explicit OCR prerequisites, preserving the sibling no-extra assertion for `test_a_standalone_image_without_ocr_stages_no_heron_document`. Do not assume the resolution must make OCR-dependent tests opt-in. Do not absorb #400's pre-commit/Pyright tooling work into this plan.
**Per-ticket loop (implementation bullets):** TDD → relevant module-local typecheck and tests → verification-before-completion. Do not repeat the full #392 suite per bullet.

## Architecture Change Review

- bounded-context-guardian: yes — Grounded Answer owns semantic judgment, Source Scope owns source authority, and Runtime owns state application; conversion ownership keeps `graph.py` as wiring.
- input-contract-tests: N/A — the plan reuses the frozen Runtime T1 seam without selecting a new public payload contract; evidence conversion is internal. Implementation still covers relevant normal, failure, and boundary classes under `.claude/rules/test.md`.
- error-handling-enforcer: yes — Grounded Answer preserves distinct failure classes, while Runtime T5 retains validation and state application.
- complexity-anti-drift: yes — the producer stays in the existing host loop, with conversion owned by the feature module and no duplicated serializer or graph split.
- minimal-change-verifier: yes — the plan closes Grounded Answer behavior and #409 helper ownership; Source Scope alone materializes #392 assets under its completion T1.

**Overall:** PASS for this bounded completion plan. Producer handoff is a deliverable before Runtime T5; full product acceptance remains at the shared #392 gate.

## Tasks (ordered by dependency)

1. **T1 — Decide helper ownership and node-test seam before the next host capability** — tag: `[decision]`
   - **Inherits:** “A single host integration owner composes this semantic responsibility with the existing model flow; this spec selects no prompt-file or function decomposition.” [#409](https://github.com/oocang/fin/issues/409) records the open helper-placement and node-test-seam choices to settle before the next host capability.
   - **Surface:** Grounded's assessment schema and record conversion, plus the existing `graph.py` host wiring.
   - **Acceptance:** Record one rule for feature-schema/helper ownership and payload-to-record conversion. Prefer the Grounded feature module as the schema authority and home for pure helpers/conversion, while retaining implementation headroom for evidence from the concrete code seam; `graph.py` remains orchestration/wiring and is not split in this plan. Record whether node-level tests need a seam and why, based on whether behavior can be exercised through the feature boundary without graph/model/network setup.
   - **Depends on:** Original Runtime T1 shared-contract acceptance.
   - **Completion:** The next host capability can place schema-associated helpers and conversion consistently without reopening the ownership debate or requiring a graph refactor.
   - Status: [ ] pending
   - [blocks: T2, T3, T4, T5]

2. **T2 — Align the assessment schema and internal conversion with host-owned facts** — tag: `[implementation]`
   - **Inherits:** “The host supplies current permissions and source identity/version, validates evidence serialization and admission, applies existing citation and numeric checks, reports truthful capabilities and failures, and enforces execution limits. A failed host check constrains or removes the affected assertion under its existing contract.” Grounded's concrete fields reuse the Runtime T1 shared contract and the existing enterprise evidence contract.
   - **Surface:** The Grounded Answer module and its existing Runtime consumer seam.
   - **Acceptance:** One Grounded-owned schema is the authority for candidate fields and their internal parsing/record conversion, so adding a field cannot silently omit it from one representation. The candidate can refer to evidence kinds under the existing evidence contract, keeping a locator distinct from a `database-row`. Current permission, capability, committed-basis, and operation-receipt facts come from their owning host modules rather than model-asserted flags. Conversion preserves every accepted field and evidence reference without creating a second public contract.
   - **Depends on:** T1's ownership decision and original Runtime T1 shared-contract acceptance. Source Scope T1 assets are not required to implement this conversion.
   - **Completion:** A second implementer can choose another internal conversion arrangement while preserving one schema authority and the owner-supplied evidence and basis facts.
   - Status: [ ] pending
   - [blocks: T3, T4, T5]

3. **T3 — Assess claim-specific support, provenance, whole-goal coverage, and conflicts** — tag: `[implementation]`
   - **Inherits:** “Each material conclusion must be supported by evidence for its meaning, subject, applicability, source scope, version, time, and material conditions. Related evidence is not support by association. Narrow or withhold an unsupported assertion.” “Evaluate every material requested part. Deliver supported portions as partial progress when other material parts remain unresolved. Do not treat an optional task decomposition or generated TODO list as the goal's authority.”
   - **Surface:** Grounded Answer's semantic assessment contract and validator; prompt requirements are supplied for the existing model loop, whose host wiring remains Runtime-owned.
   - **Acceptance:** Grounded's handoff specifies a model-produced assessment that distinguishes source statements from derivations and names their premises, ties claims to admitted evidence of the correct kind, covers the committed goal rather than treating a generated task list as its authority, preserves unresolved gaps and conflicts, and supports partial results. Module-local tests exercise representative grounded cases; formal expected-record evidence uses the canonical #392 package owned by Source Scope T1, without duplicating or rewriting those records. The host integrator can consume this output in its existing model loop without a second classifier or model loop.
   - **Depends on:** T2 for implementation. Source Scope completion T1 supplies the formal expected-record gate but does not block coding.
   - **Completion:** A second implementer can use a different internal representation or example layout while preserving claim-level support, complete goal coverage, and the canonical expected outcomes.
   - Status: [ ] pending
   - [blocks: T4, T5]

4. **T4 — Qualify useful checks, actionable clarification, and truthful failures** — tag: `[implementation]`
   - **Inherits:** “Propose another check only for a named gap, a currently available permitted capability, and a reasonable prospect of obtaining useful evidence.” “Ask only for user-held information that can change a feasible next step or conclusion.” “Successful lookup without support, permission limits, unsupported conditions, dependency failure, and exhausted recovery remain distinct. Preserve supported findings if another part fails.”
   - **Surface:** Grounded Answer's semantic assessment contract, using current authority and operation facts supplied by Source Scope and the Runtime host loop.
   - **Acceptance:** A proposed check is retained only for a named gap, an actually permitted and available host-reported capability, and a reasonable prospect; a clarification survives only when the answer can change a feasible action or conclusion. The model cannot create permission or capability by setting candidate fields. Permission denial, unsupported or unavailable capability, dependency failure, exhausted recovery, and successful-empty lookup remain distinct at the handoff; supported findings survive failure in another part. The Grounded module consumes those facts and does not duplicate Source Scope enforcement.
   - **Depends on:** T2 and T3. Implementation may use the frozen shared representation and permission/capability mapping from Source Scope completion T2/T3. Integrated acceptance additionally needs Source Scope T4's current-authority/capability enforcement facts, and T5 when the claimed path includes an unsupported capability; these facts do not block coding against the frozen representation.
   - **Completion:** A second implementer may change the internal fact carrier while preserving host ownership of permission/capability and the distinct outcomes.
   - Status: [ ] pending
   - [blocks: T5]

5. **T5 — Return a task-completion proposal without taking Runtime authority** — tag: `[implementation]`
   - **Inherits:** “Complete only when supported conclusions and their conditions satisfy the user's requested result. Partial or unresolved work remains incomplete even if the turn ends, a citation/numeric check passes, all available tools have been tried, or every TODO is cleared.” “Runtime validates the proposal against the current basis and host checks, then alone applies and persists the outcome, preserving the last consistent state if application fails.”
   - **Surface:** The Grounded Answer result boundary and the existing Runtime outcome application seam.
   - **Acceptance:** Grounded can propose completion only for a supported whole goal with no material gap, using trusted committed-basis and host-gate facts; an absent or stale basis cannot become complete through a model field. Partial progress can be returned while the task remains incomplete even when the turn ends. The output remains a proposal with its originating task, scope, and receipt basis available to Runtime; Runtime alone validates, applies, and persists it. This producer handoff is consumable by Runtime completion T5 after task and scope commitment; no task in this plan separately owns graph or prompt wiring.
   - **Depends on:** T2–T4 and the Runtime-owned committed-basis/result contract. The handoff itself does not wait for Runtime completion T5 or later Runtime tasks.
   - **Unblocks:** Runtime completion T5 (#406).
   - **Completion:** A second implementer may choose a different proposal/result representation within the shared contract while preserving Grounded's semantic ownership and Runtime's sole task authority.
   - Status: [ ] pending

## Cross-plan ownership and acceptance

The Grounded handoff consists of the feature-owned semantic input/result contract, internal conversion, prompt requirements, and module-local evidence. Runtime completion T5 owns the one host-loop connection after reconciliation and source-scope commitment, validation against current task/version/scope and observed gates, and task outcome application. Source Scope completion supplies current permission/capability facts and owns the canonical #392 package. This allows Grounded's producer deliverable to unblock Runtime T5 without waiting for Runtime T3, T4, or T6; Grounded's integrated product evidence still waits for Runtime T5 and the required Source Scope facts.

## Cross-plan dependencies and evidence ownership

| Producer / outcome | Grounded relationship |
| --- | --- |
| Original Runtime T1 shared contract | Implementation entry for Grounded T1/T2; Grounded does not select a second public payload contract. |
| Source Scope completion T2/T3 representation and decisions | Grounded T4 may be implemented against these frozen inputs; it need not wait for Source Scope T1 assets. |
| Source Scope completion T1 canonical #392 package | Formal expected-evidence gate owned once by Source Scope; not a Grounded coding prerequisite. |
| Source Scope completion T4 enforcement facts; T5 when an unsupported-capability path is claimed | Required for integrated evidence that Grounded consumes current authority/capability truthfully. |
| Grounded T2–T5 producer handoff | Unblocks Runtime #406 T5, which owns the existing-loop connection, current-basis/host-gate validation, and outcome application. Grounded does not duplicate that wiring. |
| Runtime #406 T5 plus required Source Scope facts and the canonical #392 package | Runtime runs the one final #392 H/M/B gate. This is acyclic: Grounded producer delivery precedes and unblocks Runtime T5; Grounded does not require all Runtime completion tasks before handing off. |

Use the canonical package produced by Source Scope completion T1. Reconcile the existing `data/grounded_assessment` materials into that asset by identity and version, preserving the independent expected outcomes and their review history. The current README says package version 1.0 while the manifest says 1.1; settle that metadata under the canonical asset owner rather than silently rewriting an oracle or keeping two package authorities.

After the relevant module and host-owner outcomes are ready, Runtime runs issue #392 v1.0 once with its full assigned H/M/B cases, clean-start repetitions, and expected records. A module-local pass, fixture-only producer, skipped/unavailable boundary, or passing H result cannot stand in for required M or B evidence. The Grounded plan does not add another acceptance suite or repeat the full gate per task.

## Contract coverage

| Grounded Answer contract | Plan coverage |
| --- | --- |
| D1–D2: claim-specific support, source/derivation distinction, premises, and user conditions | T2–T3 |
| D3–D4: whole-goal coverage, partial findings, gaps, and unresolved conflicts | T3 |
| D5–D7: useful permitted continuation, actionable clarification, distinct failures, and retained partial findings | T2, T4 |
| D8: task completion remains distinct from turn ending and host application | T2, T5; Runtime completion T5 validates and applies |
| SC-1–SC-2: support, provenance, applicability, and independent hard checks | T2–T3 |
| SC-3: insufficiency and conflict | T3 |
| SC-4: distinct failures and feasible continuation | T4 |
| SC-5: multi-part completion | T3, T5 |
| SC-6: existing mechanical checks remain independent; full #392 evidence | T2–T5; Runtime's single integrated gate |

**Persist list:** empty. No new domain term or ADR decision is introduced.

## End-of-round review

After all Grounded implementation bullets, run one `code-review` phase over the complete Grounded diff. If `GATE: BLOCKED`, use the next slot for `review-report-repair`, then run final `verification-before-completion`. Runtime owns the final integrated #392 H/M/B acceptance; Grounded module delivery and handoff do not claim that product acceptance has passed.
