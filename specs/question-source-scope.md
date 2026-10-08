# Spec: Question experience source scope

> **Status:** Decision-level contract for one of four independently developed modules. It settles product and ownership boundaries; it does not select implementation schemas or claim completed acceptance.
> **Architecture review:** Passed decision-only review; [review record](question-task-runtime.md#architecture-change-review). No implementation approval is implied.

## Objective

Define how a question's source restriction and the host's current source authority combine into one effective source scope. The host must apply that same effective decision before every source operation and before source-derived evidence is admitted. Operators can ask without selecting documents or a retrieval technique; explicit restrictions stay in force unless they change them.

This module decides and enforces source eligibility, identity, and current capability. It does not decide whether evidence supports an answer or whether a source semantically applies. Runtime owns accepted-input versions and persistence; Grounded Answer owns semantic sufficiency; Workspace presents the currently evaluated scope and its truthful capabilities.

## Boundaries

- **Does:**
  - Resolve default, explicit, and inherited source intent already interpreted by the existing unified Runtime/model path.
  - Combine user restrictions with the host's trusted current operator authority, active-thread identity, source versions, readiness, and actual operation capabilities.
  - Enforce the resulting effective selection before source data is read or returned and again before derived evidence is admitted.
  - Return the evaluated current scope, operation capabilities, truthful source outcomes, and a proposed scope change for Runtime to accept and persist.
  - Preserve the neutral retrieve-port boundary so the source module can be enforced without making retrieve depend on host implementation details.
- **Confirms with human:** No additional permission or product-behavior decision remains open in the tracked change through the tracked change.
- **Out of this spec:**
  - Task or input schemas, revision encoding, checkpoint mutation, or independent persistence; Runtime owns these.
  - Natural-language intent classification, query choice, semantic applicability, claim support, evidence sufficiency, or answer completion; the existing unified model path and Grounded Answer own these decisions.
  - Browser controls, displayed source lists, clarification UX, and HTTP/stream projections; Workspace presents the effective outcome, and one host integrator owns shared composition and transport.
  - A new authorization provider, identity system, tenant boundary, source store, ingestion/preparation lifecycle, provider choice, ranking redesign, or source class.
  - A separate integration-acceptance specification; the tracked change remains the acceptance contract.

## Confirmed source-scope decisions

1. **Default source eligibility includes the active knowledge corpus and attachments in the trusted current research thread, subject to existing operator permission.** Eligibility, attachment readiness, and permission are separate facts. An eligible attachment may require the existing preparation lifecycle; this decision does not change how preparation starts, joins, or reaches ready. Provenance: issue , approved scope decision, [ADR 0088](../docs/adr/0088-thread-private-attachment-retrieval.md), [ADR 0089](../docs/adr/0089-lazy-page-region-attachment-preparation.md).
2. **No user preselection or retrieval-technique choice is required for an ordinary question.** The host searches within the effective default scope. Provenance: issue.
3. **An explicit or narrowed user restriction is a durable constraint on the requested selection and is never silently broadened.** A later operation or follow-up must still satisfy both that restriction and current host authority. An inherited restriction means reuse of the same user intent, not reuse of prior permission. Expansion requires explicit operator intent, current authority, and Runtime acceptance. Provenance: issue, issue.
4. **A saved restriction is not an authorization grant.** The host rechecks actual current permission, trusted thread ownership, source identity/version, readiness where relevant, operation capability, and applicable execution limits before every source path and evidence admission. Missing authority cannot become allow-all. Provenance: issue , model/host responsibility decision, [ADR 0088](../docs/adr/0088-thread-private-attachment-retrieval.md).
5. **Enforcement happens before source data crosses each boundary.** It covers enumeration and retrieval, attachment search, direct known-locator reads, visual reads, facts and comparisons, and admission of text, visual, or numeric evidence. Filtering a broad result after unauthorized data was fetched does not satisfy the decision. Provenance: issue, [ADR 0046](../docs/adr/0046-citation-evidence-from-retrieve-payload.md), [ADR 0088](../docs/adr/0088-thread-private-attachment-retrieval.md).
6. **Model and host responsibilities stay separate.** The existing unified model path interprets source-related language, decides semantic applicability, and selects feasible queries/checks. The source module deterministically resolves and enforces the approved selection and reports actual capabilities and outcomes; it does not add a second classifier or judge. Provenance: issue, issue.
7. **Permission denial, unsupported or unavailable capability, dependency failure, and successful lookup with no results remain distinguishable.** Readiness or a healthy collection binding does not grant permission; empty success is not a failed search. Provenance: issue, issue, issue.
8. **Ask about scope only when the operator's answer can change a feasible next action.** Report known access limits directly. Do not present futile expand/upload/continue choices, impossible source selections, or a retrieval-technique picker. Provenance: issue, issue.
9. **Runtime alone accepts and persists a scope change against its current input/task basis.** The source module returns a proposal; it owns no second source of truth or persistence authority. Provenance: issue and the agreed four-module ownership.
10. **the tracked change v1.0 remains the one integrated acceptance contract.** Source-related checks are mapped there; module-level evidence does not replace the final H/M/B gate. Provenance: issue , approved manifest.

## Conceptual module seam

The module receives the accepted source intent or inherited restriction from the existing unified path, its current Runtime basis/reference, and trusted host facts about the operator, active thread, source identities and versions, readiness, permissions, and available capabilities. It resolves these into a proposed restriction for Runtime to accept, or the currently effective authorized selection. Before each retrieval, listing, direct read, visual read, fact/comparison operation, or evidence admission, the host supplies the current selection and the requested source operation. The module returns the permitted selection and actual capability/result boundary, or the truthful reason the operation cannot proceed.

- **Request context:** accepted source intent or existing restriction, its current Runtime basis/reference, and trusted current authority, thread, identity/version, readiness, and capability facts.
- **Resolution result:** the effective authorized selection or a proposed user-requested change for Runtime to accept. A proposed change alone is not authority and is not persisted here.
- **Operation result:** the permitted selection, actual capability and applicable limit, or a distinct permission, unsupported, unavailable, or dependency outcome. A successful empty lookup remains a successful result. Existing retrieval adapters supply the actual result or dependency receipt; the source module does not rewrite it.

The retrieve-owned neutral constraint must be applied before the adapter returns source data. If an operation cannot express the approved restriction, it reports unsupported instead of issuing a broader request. No previous retrieve result or saved selection bypasses revalidation.

This is a conceptual contract only. It intentionally leaves shared representation, exact selector vocabulary, transport encoding, persistence layout, and concrete capability mappings undecided. The eventual retrieve-port contract remains neutral and retrieve-owned: it can express an authorized source constraint without importing host concepts. Existing corpus and attachment ports are reused. One host integrator owns shared graph, HTTP, and stream composition. Before independent implementation begins, the module owners must agree on the concrete shared representation at that seam; this decision spec does not invent it.

Source Scope supplies boundaries and facts, not answer judgment. A sole candidate does not prove sufficiency. The model may continue only within permitted scope and available capabilities; Grounded Answer decides whether the resulting evidence supports the requested conclusion. Workspace displays effective scope and current capability faithfully. A saved user restriction remains visible as intent even if current permission later prevents its use; it must never be represented as a current grant.

## Success Criteria

Each criterion is binary against the tracked change's independent fixtures and expected outcomes. The final integrated acceptance owner applies the manifest's exact H/M/B allocation and repeat rules; this module does not claim that those checks have run.

| Criterion | Pass condition | the tracked change coverage |
| --- | --- | --- |
| SC-1: Default eligibility | The default effective selection includes only the active corpus and current-thread attachments the existing operator authority permits. Attachment readiness is reported separately and follows the existing preparation lifecycle. | J03–J05; H/M/B per manifest. |
| SC-2: Restriction and inheritance | Explicit restrictions remain effective across retrieval, direct reads, and follow-ups; inheritance carries the restriction intent without reusing stale permission. No operation silently falls back to a broader selection. | J03–J05, J15; exact case-level H/M/B assignment per manifest. |
| SC-3: Per-operation authority | Before source data crosses every listed read/retrieval boundary, and before evidence admission, the host verifies current authority, thread ownership, identity/version, and relevant capability. A known locator cannot bypass the current scope. | J05, J08, J10, J15, J22; exact H/M/B mapping per manifest. |
| SC-4: Truthful outcomes | Deterministic observations distinguish denied, unsupported, unavailable, dependency failure, and successful-empty. None is rendered or consumed as another category, and health/readiness does not grant permission. | J09, J11, J12, J20; exact H/M/B mapping per manifest. |
| SC-5: Semantic boundary | No source-intent classifier or retrieval-technique picker is added. Scope questions are asked only when an answer can affect a feasible permitted action; the source module does not declare semantic sufficiency. | J03–J05, J08–J10, J20; H/M/B where required by the manifest. |
| SC-6: Module independence | Scope changes are proposals applied by Runtime; retrieve remains a neutral seam; existing corpus/attachment ports and lifecycle remain in use. No new ACL/tenant/provider/store/ranking system is introduced. | Contract and dependency review before module implementation; integrated consequences covered by the tracked change. |
| SC-7: Full acceptance | Every source-related journey and variant required by the tracked change passes at its assigned evidence level; all required failures are retained and reviewed. | J03–J05, J08–J12, J15, J20, J22, with the exact case-by-case assignments and repetitions in the tracked change. |

## Open Questions

No product-behavior decision remains open. Existing ACL realization, mappings from real capabilities to operations, the neutral selector representation at the retrieve seam, and any latency budget remain technical investigation; this spec does not claim they are already mapped or select new values for them. If discovery requires a new authorization system, tenant model, store, provider, or ranking behavior, the affected decision must return to the operator before implementation.

## Inherits / Changes

### Confirmed inherited assumptions

The preceding numbered decisions are inherited from the canonical issue comments and accepted architecture, not new requests for approval. The source module remains inside the host-agent boundary and depends on the retrieve contract; it does not own ranking or the company source store.

The exact relevant vocabulary from [`docs/CONTEXT.md`](../docs/CONTEXT.md) is:

> **retrieve port**: “The only retrieve dependency fin is allowed to have: `query` (+ optional filters or a typed structured request) in, authorized corpus results with source attribution out — MCP-shaped, transport-agnostic.”

The relevant context-map boundary states:

> **host-agent context**: “The **host agent** boundary: **fin** as the LangGraph runtime that routes tracks and calls grounded-track tools behind the **retrieve port**; it is not the ranking or vector store. Physically `src/fin/host/`.”

**Inherits:** ADR 0001 keeps the host-first retrieve port without compile-time coupling to the external retrieval service. ADRs 0088 and 0089 keep thread-private attachments, trusted active-thread binding, and the existing readiness/preparation lifecycle. ADR 0046 keeps current-run evidence admission on the host. ADR 0095 keeps source versions and historical evidence immutable. These decisions define boundaries; they do not choose this feature's selector encoding or implementation architecture.

**Changes:** Establishes the source-scope product decision and ownership: one effective, permission-constrained selection across retrieval/read/evidence paths; current authority and capability revalidation; no silent broadening; Runtime-applied changes; semantic sufficiency remains with Grounded Answer; Workspace presents the effective result. The concrete shared representation is a downstream contract decision before parallel implementation.

**Persist list:** empty. No glossary term or ADR decision is added by this spec.

## Architecture Change Review

Independent decision-only review passed on 2026-10-09; the five verdicts are recorded in
[Question and task runtime](question-task-runtime.md#architecture-change-review). No implementation approval or product acceptance is asserted here.
