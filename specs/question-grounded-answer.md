# Spec: Question grounded answer

> **Adopted QA-chain amendment (2026-10-16):** The operator accepted the routing, retrieval, lifecycle and scoped persistence proposals. This canonical module contract incorporates the relevant behaviour below; the [shared wire contract](question-task-runtime-contract.md) freezes representation. [Routing/grounded](qa-chain-grounded-experience.md), [server/receipts](qa-chain-server-runtime.md), and [catalog/retrieval](qa-chain-corpus-retrieval.md) own their deltas. The journey acceptance contract remains the sole integrated acceptance gate. No implementation or acceptance pass is claimed.

> **Status:** Decision-level module specification; concrete interfaces and implementation choices remain downstream.
> **Architecture review:** Passed decision-only review; [review record](question-task-runtime.md#architecture-change-review). No implementation approval is implied.

## Objective

Define the decision boundary for evidence-grounded answers in fin's existing host-agent flow. The model evaluates whether available evidence supports
the user's particular conclusion, which material gaps remain, and whether a feasible next check or user clarification can change the result. The host
retains authority over source access, evidence identity, mechanical verification, execution limits, and persisted task state.

The user receives conclusions tied to the asked subject, applicability, time, scope, and conditions. The answer distinguishes source statements from
calculation or inference, covers the whole goal, and reports unsupported parts honestly. A successful search, citation pass, completed TODO, or ended
turn cannot by itself establish task completion.

## Boundaries

- **Does:**
  - Define the semantic responsibility and conceptual result of grounding: supported conclusions, goal coverage, material gaps, feasible next action,
    and proposed answer/task outcome.
  - Let the model choose useful queries and checks from the host-provided capabilities, current effective source scope, available evidence, and
    truthful tool receipts.
  - Require evidence to support the specific conclusion and asked applicability relationship; a caveat cannot rescue an unsupported assertion.
  - Keep the assessment in the existing unified model/agent flow and return its proposal to Runtime for validation and state application.

- **Confirms with human:** No product behavior remains undecided in the canonical decisions. A materially new behavior or a contradiction in the
  approved decisions returns to the operator before implementation.

- **Out of this spec:**
  - Task identity and revision, accepted-input reconciliation, checkpoint persistence, task lifecycle, and authoritative
    task outcomes; Runtime owns these.
  - The current permitted source selection, permission decisions, and source-scope enforcement; Source Scope owns them.
  - Presentation, source inspection, clarification interaction, and browser behavior; Workspace owns them.
  - Evidence serialization, citation or numeric verification redesign, retrieval ranking/provider selection, a new verifier, a second LLM judge, a
    separate intent classifier, or an independent model/agent loop.
  - Provider choices, global performance budgets, and deployment guarantees not selected by the issue map.

## Decision contract

### Inputs and result

Grounding consumes the current user goal and task context, the already committed task/input basis and source scope, the evidence actually available to
this answer, host-reported capabilities, and actual success or failure receipts. It does not treat an unavailable source as an empty successful
search.

The model returns a conceptual grounded assessment to Runtime: what conclusions are supported and how, which goal portions are covered, what material
gaps or conflicts remain, and whether to continue, ask an actionable question, deliver a supported result, or report unresolved work. It may also
propose the resulting task and turn disposition. The [shared contract](question-task-runtime-contract.md) and
[QA-chain grounded contract](qa-chain-grounded-experience.md) freeze the shared fields, wire encoding and admission rules before parallel implementation.

An assessment uses only the currently committed task/input and source-scope basis. Runtime first reconciles each accepted user input once. Grounded
assessment begins only after the required task and scope state is committed; the bootstrap or a same-decision proposed transition cannot claim
grounded completion. Later retrieval and assessment iterations use that committed current state without repeating input reconciliation. Runtime
validates the proposal against the current basis and host checks, then alone applies and persists the outcome, preserving the last consistent state if
application fails. This module has no authoritative task state.

### Semantic decisions

1. **Support is specific.** Each material conclusion must be supported by evidence for its meaning, subject, applicability, source scope, version,
   time, and material conditions. Related evidence is not support by association. Narrow or withhold an unsupported assertion.
2. **Source and derivation stay distinct.** Identify what a source states directly. Label calculation, synthesis, and inference as such and make the
   supporting premises clear. A user's confirmed situation may be a task condition; it is not evidence for a policy fact.
3. **Assess the whole goal.** Evaluate every material requested part. Deliver supported portions as partial progress when other material parts remain
   unresolved. Do not treat an optional task decomposition or generated TODO list as the goal's authority.
4. **Name material gaps and conflicts.** Explain how missing evidence, unestablished applicability, an unknown necessary user condition, or
   conflicting sources can change the requested result. Preserve a conflict unless evidence establishes which source applies or prevails. Explaining a
   conflict can satisfy a goal asking what conflicts; it does not resolve a request for the operative rule.
5. **Continue only when useful and permitted.** Propose another check only for a named gap, a currently available permitted capability, and a
   reasonable prospect of obtaining useful evidence. Repeating an exhausted direction without a new basis is not progress. Stop when the goal is
   supported or feasible directions are exhausted or blocked by existing host execution limits.
6. **Clarify only actionable conditions.** Ask only for user-held information that can change a feasible next step or conclusion. A source-coverage
   gap alone does not justify a futile question.
7. **Keep failure classes distinct.** Successful lookup without support, permission limits, unsupported conditions, dependency failure, and exhausted
   recovery remain distinct. Preserve supported findings if another part fails.
8. **Separate task completion from turn ending.** Complete only when supported conclusions and their conditions satisfy the user's requested result.
   Partial or unresolved work remains incomplete even if the turn ends, a citation/numeric check passes, all available tools have been tried, or every
   TODO is cleared. A turn may end with supported partial findings and an explicit unresolved task.

### Host guarantees and module ownership

The host supplies current permissions and source identity/version, validates evidence serialization and admission, applies existing citation and
numeric checks, reports truthful capabilities and failures, and enforces execution limits. A failed host check constrains or removes the affected
assertion under its existing contract. The model cannot verify its own evidence, claim a host check passed, or commit task state.

Existing evidence serialization remains authoritative in [enterprise-evidence-contract.md](enterprise-evidence-contract.md). Citation verification
remains code-only under [ADR 0082](../docs/adr/0082-citation-verifier-code-only.md). These checks establish mechanical identity, structure,
quote/row, or numeric validity; they do not establish semantic support, applicability, complete goal coverage, or task completion. In particular, a
quote check passing does not mean the evidence answers the asked question.

Source Scope owns the current restriction for corpus-derived text, attachments, XBRL facts, and comparisons. Previously archived assistant text is not
independent evidence: current claims require available source evidence to be rehydrated and revalidated under the current task and source scope.
Workspace owns the user's presentation and interaction. A single host integration owner composes this semantic responsibility with the existing model
flow; this spec selects no prompt-file or function decomposition.

### Adopted QA-chain answer and clarification behaviour

The [routing/grounded contract](qa-chain-grounded-experience.md) adds one decision/synthesis per retrieved evidence set through this existing semantic seam, not a second judge. Multiple documents alone do not establish ambiguity. A supported synthesis may use them together; only a material, actionable interpretation difference produces at most four hit-backed document choices. Operational failure remains distinct from evidence refusal; evidence refusal requires an actual retrieval attempt in the logical turn.

For document ambiguity, return a proposal for Runtime's single pending question. Runtime owns acceptance/reconciliation of the reply and the source snapshot, then continues the same run/receipt. An original question, candidate click and Other reply retain their exact input associations under the [shared contract](question-task-runtime-contract.md#document-clarification-and-accepted-input-scheduling).

Malformed structured output sends its original text through the existing R3 verifier and semantic support boundary. Admitted fallback records `fallback_merge` with verified references; failed admission records `fallback_refuse` with no citations. Deterministic presentation may remove technical labels without adding facts or changing evidence bytes; it must not start a second repair/synthesis model call. Hold unadmitted answer bytes on every stream/history/export/resume path. Mechanical R3 success still cannot establish semantic support or task completion.

### Incremental publication amendment (2026-10-21)

The existing Grounded Answer decision may expose a structured answer incrementally, but it remains one decision/synthesis call per evidence set. For each factual block, the existing decision must make support, applicability, and consistency with earlier admitted blocks available before that closed block can publish. The host continues to enforce current evidence identity/version, reference closure, applicable R3 checks, safe deterministic presentation, and immutable admitted-prefix behavior. Mechanical R3 success remains insufficient for semantic admission.

If the single existing decision cannot make a block and its support/applicability result available until the full response ends, early factual publication remains blocked; a second judge or repair call is prohibited. Exact block encoding and parser limits belong to the publication admission gate in P1, not to a new Grounded Answer module or model loop. See [ordered assistant stream publication](assistant-stream-publication.md) for the host interface and delivery contract. This dated amendment does not alter the earlier integrated acceptance results or claim implementation.

## Success Criteria

The following outcomes are binary against the independent expected result for each acceptance journey. H means deterministic host behavior, M means the real
configured model, and B means the real application journey.

| Criterion | Required decision | Acceptance evidence |
| --- | --- | --- |
| Claim-specific support | Supported claims match their subject and material applicability; unsupported claims are narrowed or withheld. | J01, J08, J22 |
| Provenance and conditions | J02 reports E1=80 and E2=100 with effective dates and employee scope, labels the 20-unit delta as calculated, and does not infer contractor/department coverage. J06 asks for the missing amount only when it changes the judgment; 1,000 and 1,001 produce their supported approval bands. J13 treats 1,500 as a user condition, not policy evidence. | J02, J06, J13 |
| Insufficiency and conflict | Employee-only evidence does not establish contractor eligibility (J08); J09 reports only what the checked scope supports; J10 can explain an evidenced conflict but leaves the operative rule unresolved when evidence cannot resolve it. | J08, J09, J10 |
| Failure and feasible continuation | J11 preserves valid partial findings across recoverable failure; J12 distinguishes failure classes and does not claim completion after unavailable/exhausted recovery; J20 continues only for a named gap and feasible useful check. | J11, J12, J20 |
| Multi-part completion | J21 separates the supported employee-cap part from unresolved contractor applicability and does not complete the task because one part or citation passed. Turn ending, tool exhaustion, and TODO completion likewise do not promote unresolved work. | J21 and applicable H cases above |
| Existing hard checks remain independent | J22 applies existing identity/version/quote/numeric checks while separately rejecting semantic support by association, such as employee evidence for a contractor claim. | J22 |

For each journey, the independent oracle and expected outcomes are established before candidate results are reviewed. Required M cases use the real
configured model and at least three clean independent journey starts; controlled or canned outputs do not count as live-model evidence. Required H
cases pass deterministically. Required B cases include a complete observed run through the actual application and persistence path. H, M, and B
evidence are distinct; none substitutes for another. These are future acceptance requirements, not claims that any case has run or passed.

Local module verification later checks the semantic contract against independent examples. Final integrated acceptance remains solely in the journey acceptance
manifest; this work adds no fifth acceptance spec. No test command or live-model result is selected or asserted here.

## Open Questions

No T0 product or shared representation decision remains open. The shared and QA-chain grounded contracts freeze evidence/result encoding;
the server contract freezes operational budgets. Existing provider configuration is retained. H/M/B runner selection and private implementation
remain downstream work and do not alter these semantic decisions or replace the integrated acceptance.

## Inherits / Changes

### Confirmed inherited assumptions

1. fin remains an enterprise knowledge-QA entry point; finance is the first corpus domain, not the product boundary. **Confirmed by** [ADR
   0090](../docs/adr/0090-product-identity-knowledge-qa-entry-point.md) and QE-01.
2. Representative goals include fact/rule lookup, comparison or synthesis, and judgments with user-specific conditions. **Confirmed by** QE-01,
   QE-04, and QE-05.
3. Model semantics and host guarantees are separate: the model judges support, applicability, coverage, and useful next steps; the host enforces
   permissions, identity/version, mechanical verification, limits, and persistence. **Confirmed by** QE-04 and QE-05.
4. Runtime owns task/input state and transitions; Source Scope owns current source permission/restriction; Workspace owns presentation and
   interaction. This module consumes those decisions without a second state or permission authority.
5. Evidence serialization and admission remain under the existing [enterprise evidence contract](enterprise-evidence-contract.md); citation
   verification stays code-only under [ADR 0082](../docs/adr/0082-citation-verifier-code-only.md). A mechanical pass does not decide semantic
   sufficiency. **Confirmed by** ADR 0082, and QE-04.
6. The minimum representative journey set and independent H/M/B boundaries are defined by QE-06 and the journey acceptance contract. They
   are requirements, not evidence of implementation success.

### Existing vocabulary

This spec consumes this exact **agent loop** definition from [docs/CONTEXT.md](../docs/CONTEXT.md):

> **agent loop**: On the **grounded** track, the model repeatedly picks `list_files`, `retrieve`, `read_source`, or `get_xbrl_fact` until it stops
> calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Grounded `done` is only after the citation verifier (and numeric
> fact gate when a fact is claimed) passes; other tracks may `done` without tools or citations. Step order is not hardcoded.

The [context map](../docs/context-map.md) keeps this behavior in the host-agent context with retrieval behind the retrieve port. This spec adds no
bounded context, glossary term, or persisted domain concept.

### Changes

Defines the semantic grounding and answer decisions returned to Runtime; preserves existing host evidence checks and Runtime's authority over
committed state; adds no evidence schema, verifier, provider choice, retrieval mode, second judge, or independent model loop.

**Persist list:** empty.

## Architecture Change Review

Independent decision-only review passed on 2026-10-09; the five verdicts are recorded in
[Question and task runtime](question-task-runtime.md#architecture-change-review). No implementation approval or product acceptance is asserted here.
