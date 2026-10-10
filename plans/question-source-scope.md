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
   - **T1 outcome.** Mapped against `src/fin`; there is no ACL, identity, or tenant layer anywhere in the tree:
     - **Operator permission has no substrate.** The only access control is trusted active-thread binding: `src/fin/host/graph.py:340` (`_bound_thread_id` — the host fact a model argument cannot widen), `src/fin/host/attachment_retrieval.py:250` (`_candidates` — re-checks `thread_id` rather than trusting the store), `src/fin/host/attachment_source_read.py`, and `src/fin/host/attachment_visual_read.py`.
     - **The company corpus is process-wide and shared by every thread.** `docs/CONTEXT.md:276` defines the one 知识库 "shared by every **research thread**"; `src/fin/host/collection_bind.py` binds the single process-wide 资料集合 (ADR 0049).
     - **Effective current authority is therefore:** trusted thread ownership + attachment identity/version + readiness + operation capability. Readiness and backend/collection health NEVER grant permission: readiness is a separate per-operation fact that yields `unavailable`, not access.
     - **NAMED GAP (returned for operator decision, not silently resolved).** Because no operator identity or permission layer exists, the spec's "current operator permission" (`specs/question-source-scope.md:33`) has no implementation today; introducing one is explicitly out of this spec (`specs/question-source-scope.md:25`, `:71`).
   - Status: [x]

2. **Resolve requested intent into an effective scope or proposal** — tag: `[implementation]`
   - **Inherits:** Spec: “No user preselection or retrieval-technique choice is required for an ordinary question.” Spec: “An explicit or narrowed user restriction is a durable constraint on the requested selection and is never silently broadened.” Spec: “An inherited restriction means reuse of the same user intent, not reuse of prior permission.” Spec: “Runtime alone accepts and persists a scope change against its current input/task basis.”
   - **Surface:** host-agent context and retrieve context, using the Runtime-owned shared contract.
   - **Acceptance:** An ordinary question resolves to only the active corpus and current-thread attachments allowed by current authority; explicit and inherited restrictions remain constraints across operations and follow-ups; a requested change is returned for Runtime acceptance and is not persisted here. The result exposes current feasible capabilities and known limits for Workspace and reports known access limits directly. Workspace presents a scope question only when the answer could change a feasible permitted action, with no futile expand, upload, or continue choice. No classifier, semantic judge, retrieval-technique picker, or clarification UI is added here.
   - **Depends on:** Source T1 and Runtime T1.
   - **T2 outcome.** Delivered in `src/fin/host/source_scope.py`:
     - **Scope resolution.** `resolve_scope` combines an explicit or inherited `ScopeRestriction` with trusted `AuthorityFacts` into the `EffectiveScope` eligibility set. It narrows attachments only; `company_allowed` is always `True`. An inherited restriction resolves by the same rules as an explicit one and is never broadened.
     - **Proposal producer.** `propose_scope_change` returns a `ScopeProposal` for Runtime to accept and persist, or `None` when nothing would change. It persists nothing and grants nothing on its own.
     - **Capability/limit exposure.** `ScopeGate.report()` returns a `ScopeReport` carrying the SAME `EffectiveScope`, the operations the gate would permit right now, and `known_limits`. Feasible capabilities are derived by DRIVING `ScopeGate.check` over `ATTACHMENT_SCOPED_OPERATIONS` plus the company token `COMPANY_CAPABILITY`, so the report can never list an operation the gate would refuse. `known_limits` is a tuple of `KnownLimit` whose codes are from the closed `KNOWN_LIMIT_CODES` set; an EMPTY tuple means no limit applies. It names: an unbound thread; an attachment an in-force restriction named that current authority no longer stands behind (recorded as `EffectiveScope.excluded_ids`); an in-scope-but-not-ready attachment; and an authority that could not be consulted. No new code was needed — the existing refusal codes fit, so the closed set gained none.
     - **Not closed here (owned elsewhere).** Runtime owns accepting/persisting a scope change and its persistence across follow-ups. Workspace owns presentation, including asking a scope question only when the answer changes a feasible permitted action and offering no futile expand/upload/continue choice. No classifier, semantic judge, retrieval-technique picker, or clarification UI was added.
     - **Verified:** `uv run pytest tests/host/test_source_scope.py tests/host/test_source_scope_enforcement.py tests/host/test_source_scope_interaction.py tests/host/test_graph_attachment_retrieve.py -q` (108 passed); `uv run pyright src/fin` (only the two pre-existing `corpus/` import errors).
   - Status: [x]

3. **Enforce the current scope at every source and evidence boundary** — tag: `[implementation]`
   - **Inherits:** Spec: “Enforcement happens before source data crosses each boundary.” Spec: “The host rechecks actual current permission, trusted thread ownership, source identity/version, readiness where relevant, operation capability, and applicable execution limits before every source path and evidence admission.” The retrieve port remains neutral and retrieve-owned; existing corpus and attachment ports and preparation lifecycle remain in use.
   - **Surface:** host-agent context, attachment capability, and retrieve context.
   - **Acceptance:** Current authority and scope are checked before enumeration/retrieval, attachment search, direct known-locator reads, visual reads, facts/comparisons, and admission of text, visual, or numeric evidence. An operation that cannot express the approved restriction reports unsupported before returning source data; prior results or saved permission do not bypass revalidation. Permission denial, unsupported or unavailable capability, dependency failure, and successful-empty remain distinct, and readiness or backend health never grants permission. Source-side behavior maps to the tracked change; its case assignments, evidence levels, and repetitions remain in the Runtime-owned integrated gate.
   - **Depends on:** Source T2 and Runtime T1.
   - **T3 outcome.** Wired attachment boundaries (gated BEFORE data crosses each one, whenever `attachment_service` wires a store): the `read_source` attachment locator, the `retrieve` attachment branch (pre-search narrowing of the effective selection and the per-hit `admit_evidence` re-gate), and the visual read. Company boundaries (`read_source` for a company locator, the `retrieve` company chunk admission, and the `get_xbrl_fact` / `compare_facts` fact reads) consult the same gate, but the company branch is never narrowed (ADR 0088:10), so they are permitted by construction and cannot refuse until a capability registry exists.
     - **Deliberately NOT wired: `list_files` (the catalog enumeration).** The only enumeration tool bound in `compile_graph` is `list_files`, which enumerates the SHARED company corpus (ADR 0047), not this thread's attachments; the scope narrows ATTACHMENTS ONLY, so there is nothing for it to narrow. No attachment-enumeration boundary is exposed to the model at all (the thread's own id list is internal to scope resolution). `ATTACHMENT_SCOPED_OPERATIONS` therefore names only `search_attachments`; the previous `enumerate_attachments` member was vocabulary no caller could ever pass and has been removed. `Successful-empty` remains reachable through `search_attachments`.
     - **T1 gap still open: operation capability.** `operation_capable` answers `True` for every operation because no capability registry exists, so the company fact / comparison seam is an INJECTION POINT that cannot refuse today. Its docstrings state this plainly rather than claiming a guarantee the code cannot deliver. Discovering the real capability mapping remains the returned-for-operator-decision gap noted under T1.
   - Status: [x]

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
