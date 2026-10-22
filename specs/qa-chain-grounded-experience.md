# Spec: QA-chain routing and grounded presentation

**Status:** Adopted contract; T0 frozen after operator confirmation and fresh architecture review on 2026-10-16. Product implementation and batch acceptance remain pending.
**Date:** 2026-10-16

## Objective

Route ordinary knowledge questions into authorized retrieval, then produce either one admitted answer or an actionable document choice with readable verified sources. Preserve existing semantic grounding and current task/source authority.

## Boundaries

- **Does:** Route intent/source, post-retrieval semantic branch, document-choice pause/resume, thread restriction and admitted Chinese presentation.
- **Confirms with human:** The operator accepted all four presented design groups on 2026-10-16, including Jev fallback, deterministic projection, persistent thread restrictions and document/attachment checkpoint storage. This task finalizes documents only. New providers, dependencies or changes beyond the adopted behaviour/storage scope require a separate decision.
- **Out of this spec:** A second semantic judge, task/source state store, citation parser, ranking engine, or replacement of the four question-module contracts. The four existing question-module specifications and their shared wire contract remain authoritative. The journey acceptance contract remains their integrated product-acceptance gate; this delta adds no fifth question-experience acceptance oracle.

## Assumptions and adopted choices

1. **Inherited, not newly assumed:** The existing grounded module assesses support and applicability; Runtime commits state; Workspace presents; code-only R3 verifies references.
2. **Explicit current-task requirement:** Four routing exits, the fast path, missing/error Jev falls back to retrieval, one decision call per evidence set, ≤4 document candidates, same-turn continuation and readable references.
3. **Confirmed by the operator's “同意”:** Route intent distinct from track; multiple documents alone do not imply ambiguity; deterministic safe projection instead of extra model repair; thread restriction persists until an explicit accepted replacement/clear. The approved persistence design covers the existing pending question and versioned source scope for both corpus documents and current-thread attachments.
4. **Removed assumptions:** No new provider, dependency, persistence framework, parallel task/source authority or speculative async isolation rewrite is assumed. Checkpoint fields are selected in the existing shared wire contract; runtime isolation remains the T1 evidence-driven implementation decision.

## Contract delta

### Existing admission seam

The interface admits a decision against one retrieved evidence set and produces a verified answer, a validated document choice, or evidence refusal. Its implementation owns structured parsing, fallback admission, candidate membership, citation admission, and the final readable projection. The graph owns orchestration, interrupt/resume, and snapshot updates; ranking remains in iknow.

Reuse the current citation verifier, numeric checks, and evidence authorization/expiry machinery. Do not implement a second citation parser in the renderer. Internal seams may isolate parsing and rendering without turning each helper into a new externally configured module.

### Routing requirements

- Route intent has exactly four wire values: `retrieve`, `overview`, `chat`, `policy_refuse`. Record its source as `rule`, `jev`, or `fallback`; existing finer decision signals may remain diagnostics. Policy checks cannot be bypassed by a Jev outage. The table below maps product actions to the existing graph instead of inventing a fifth track.
- Preserve the routing rules as fast paths. Catalog requests such as “随便查一下”, “看看”, and “有啥” produce overview without asking for financial slots. An ordinary unscoped question enters retrieval. Jev absence, error, malformed output or low-confidence/unusable hint defaults to retrieval rather than pre-retrieval evidence refusal. A usable hint must be one existing track label and have finite confidence in `[0,1]` and meet the existing `0.6` threshold (`track_decision._MIN_JEV_CONFIDENCE`); no new classifier is introduced. Uncertainty alone and missing financial slots cannot force pre-retrieval clarification of an ordinary question.
- Evidence refusal requires at least one actual retrieval attempt in the logical turn. An index/busy/transport failure is not evidence that the corpus lacks an answer.

| Route intent | Existing graph action | Authority / trigger |
| --- | --- | --- |
| `retrieve` | `grounded` with authorized retrieval | Ordinary knowledge question or unusable optional Jev hint; existing routing rules remain primary |
| `overview` | `grounded` with catalog enumeration | Existing catalog/discovery fast rules; no financial-slot prerequisite |
| `chat` | Existing `dialogue` or `off_topic` leaf | Greeting, conversation or the existing soft capability-boundary response; off-topic is not a policy refusal |
| `policy_refuse` | Existing system/model-policy refusal disposition, no new Track | An explicit refusal under the existing system/provider policy boundary; never inferred from Jev outage, off-topic classification, permission error, citation failure or missing evidence |

There is no new content-policy taxonomy or pre-route safety classifier. The four values describe the product disposition; an explicit system/model-policy refusal can refine the initially selected intent when observed. Until such a refusal is established, the receipt must not invent it. Retain the `clarify` track only for an actionable, identified missing user condition under the canonical task contract; it retains the underlying intent (`retrieve` for a knowledge task), is not a fifth product intent and cannot be a judge-outage response. If a usable Jev `clarify` hint identifies no such condition, take `retrieve`; hints never bypass host policy or source checks.

### Grounded and presentation requirements

- One decision call chooses merged answer or at most four document candidates for a retrieved evidence set. Candidate identity must come from admitted hits; labels are “document title · section” plus a supported one-sentence hit summary. The final action is “换个问法”.
- Do not infer ambiguity merely from multiple documents. A supported multi-document answer is valid. Clarification is for materially different interpretations that the evidence and question cannot resolve.
- On parse failure, send the original text through the same R3 verifier. Verified output records `fallback_merge` and `citation_verified=true`; failed admission records `fallback_refuse` with no citations. Do not salvage unverified citation strings or stream raw failed-parse text first.
- The one-call budget is for decision/synthesis. Final copy rewriting/filtering is deterministic, with no extra model repair call in this slice. Safe source-label/citation projection may replace internal representations, but cannot add facts or alter evidence bytes. If readable verified output cannot be produced safely, return controlled refusal with no unverified references. Existing model-based citation bounce must not silently add a second synthesis call to this new path.
- Pending candidates and tokens belong to the paused run checkpoint. Resume validates thread/run ownership, interrupt kind, and candidate membership; invalid or stale picks preserve/reopen the same choice rather than accepting an arbitrary ID. Double clicks must not launch duplicate work.
- Confirmed document scope is an append-only thread snapshot. The chosen scope replaces the previous scope and constrains the resumed answer and relevant short follow-ups. Pending candidates are not durable user preferences. Only an explicit user scope change accepted by Runtime may replace or clear the prior restriction through a new snapshot; changing only search mode or sending a fresh unscoped question does not silently broaden it. Whole-corpus default applies when no accepted restriction exists. Source Scope rechecks current permission, identity and version on every path; invalid scope is reported, never widened automatically. Runtime/host is the single snapshot writer after accepting the Source Scope proposal.
- The original and resumed execution retain the same logical turn/run identity. Reload restores the pending choice; other threads do not inherit it. Neither explicit pinning nor automatic memory extraction may persist document-choice control state to long-term memory.
- All answer paths, including streaming, history, export, and resume, use the same admitted readable projection. Hold unverified generated prose/citations until admission; a later reset cannot retract already leaked text. Retain responsive progress events while final answer admission is pending.
- Show inline `[n]` markers and a readable trailing sources list linked to the same admitted references. Internal names, engine/tool names, IDs, and slot labels must not leak through default source labels, candidates, errors, or progress. Preserve original evidence bytes for verification and source viewing. Use generic Chinese ordinary-person copy; diagnostics remain separately access-controlled.
- Distinct copy: index work “资料仍在整理中”; busy “检索繁忙，请稍后重试”; evidence refusal states insufficient evidence and supplies two supported ways to rephrase without inventing facts. Attachments state “仅在本对话中可用”.

This decision remains the existing Grounded Answer module's support/applicability assessment and answer generation, not an additional judge after it. Mechanical R3 success alone is insufficient to claim semantic support or task completion. Host-validated document-choice proposals go through existing Runtime/Source Scope ownership. Preserve the existing general Other/original-input contract; the displayed “换个问法” action must retain that original-input association rather than silently losing the user's question.

## Frozen clarification and scope contract

The [shared question wire contract](question-task-runtime-contract.md#document-clarification-and-accepted-input-scheduling) is the sole authority for field spellings, bounds, versioned source identities and checkpoint representation. It incorporates this adopted behaviour without a second task or pending-question authority.

### One pending question, one accepted reply, same run

- A grounded document choice resolves materially different interpretations of the user's question. It is a Task Runtime clarification, not merely a search-mode control. The existing `task_runtime.pending_question` owns the typed purpose, original run association and at most four admitted choice details. Its ordered `choices` retains exactly one trailing `other`; the interrupt references this question instead of storing another candidate list.
- Submit a pick through existing send-door reply admission using `question_id` and `choice_id`. Submit a rephrase with the same fields, `choice_id=other`, and the user's exact `free_text`. Before accepting anything, validate current question/task basis, paused run ownership, membership, and current source identity/version/permission. A successful reply advances `input_version` exactly once, preserves original input history, and is reconciled by Runtime before scope or execution changes commit.
- The committed Runtime envelope and accepted-input config then continue the original checkpoint through `Command(resume=...)`, retaining the original `run_id` and receipt. Transport alone does not create an accepted input. A bodyless crash resume carries no new input; this is the branch to which the existing shared contract's no-new-input rule applies.
- A choice click has no user-authored free text: keep `payload` absent instead of inventing prose, while retaining its original structured `question_id`/`choice_id` and run association. Clicking “换个问法” only opens the Other input; submitting that text becomes the valid reply. A duplicate request must resolve idempotently against the already accepted question/reply before accepting another input. Stale or foreign input changes neither history nor scope.

### Accepted versioned scope

The existing append-only snapshot's `source_scope` records an explicit unrestricted/restricted selection, typed source identities/versions and the accepted input establishing the intent. Source Scope validates identities; Runtime remains the single writer. This supports both corpus documents and the active thread's attachments without publishing attachments or creating another source store. `document_ids` is a derived legacy corpus projection only, never a second authority or a way to infer unrestricted access from an attachment-only selection. The shared wire contract owns the exact shape and conservative legacy-read rules.

| Event | Required transition |
| --- | --- |
| No accepted restriction | Search the currently authorized corpus and eligible thread-local sources |
| New unscoped question, short follow-up or search-mode-only change | Retain and revalidate the accepted restriction |
| Pending document question | Keep current scope; candidates remain in the single pending question |
| Valid accepted candidate reply | Append the selected source restriction and continue the original run/receipt |
| Explicitly accepted scope replacement/clear | Append replacement or explicit unrestricted scope; never infer clear from missing input |
| Stale/foreign/duplicate pick or invalid source authority/version | No widening; reopen the applicable question or return its typed failure |
| Other thread or long-term memory | Receive no document-choice control state |
| Archived intent restoration | Recheck current authority, identity/version and evidence; old citations are not current evidence |

The operator adopted this scoped checkpoint design together with the separate receipt and catalog/audit designs. This documentation task makes no checkpoint mutation. An implementation request still precedes code execution; a larger storage change or additional source class requires a new decision.

## Success Criteria

These are required observations, not test results. Each must pass at the named interface; failure keeps its implementation ticket open.

- **GE-1:** Route tests and browser evidence for “随便查一下”, “看看”, “有啥” show overview; an ordinary question with Jev absent/error retrieves and cites; policy checks still run. Every receipt records rule/jev/fallback. Maps to A03/A04.
- **GE-2:** Grounding cases distinguish supported multi-document synthesis, genuine ambiguity, insufficient evidence and operational failure. Q6 records evidence_refuse after ≥1 actual retrieval with no invented facts. Instrumented evidence-set tests show ≤1 decision/synthesis call and no hidden repair call. Maps to A05/A06/A16.
- **GE-3:** Malformed structured output sends original text through R3 and existing semantic-support rules: admitted fallback_merge has verified references; fallback_refuse emits none. Capture SSE to prove no unadmitted text leaked before fallback. Maps to A07.
- **GE-4:** Browser reload/pick/follow-up preserves the original run/receipt and current source authority; ≤4 hit-backed candidates have title/section/summary and trailing rephrase. Stale/foreign/double picks cannot change another run or widen scope; other threads and long-term memory receive no control state. Maps to A05/A14/A20.
- **GE-5:** Browser/SSE/history/export use the same admitted projection, inline [n] and readable clickable source tail. Source evidence bytes remain unchanged; index/busy/insufficient-evidence/attachment copy are distinct. The six ZEUS questions expose no forbidden technical labels and general Chinese copy is used. Maps to A08/A11/A13.
- **GE-6:** Existing question-module source, task/turn and clarification semantics remain valid; telemetry and route labels cannot infer permission or task completion. Maps to A20.

## Open Questions

No T0 behaviour or representation choice remains open. The route table, scope transition table and shared wire contract freeze the choices; ADR 0027/0080 and the soft-track spec record the adopted fallback. Real model support, browser reload/pick behaviour, SSE admission and current source authorization remain implementation acceptance, not assumed passes. T1 still diagnoses blocked execution; it does not reopen these input/source ownership decisions by default.

## Inherits / Changes

This spec consumes the following definition verbatim from [CONTEXT](../docs/CONTEXT.md):

> **grounded**: The Soft-Track leaf that binds corpus tools and the **citation verifier** (ADR 0061). Not a product name, not **search mode**, and not the `retrieve` tool. Wire value `research` is an alias for this leaf.

The selected project term is also consumed verbatim from CONTEXT:

> **route intent**: The product action selected for an input before execution, distinct from the graph **track** and the eventual answer outcome.

It inherits the [context map](../docs/context-map.md), [Task Runtime](question-task-runtime.md), [Source Scope](question-source-scope.md), [Grounded Answer](question-grounded-answer.md), [Workspace](question-workspace.md), and [shared wire contract](question-task-runtime-contract.md). Existing task identity, input history, current authority and source admission are retained. No receipt, snapshot, route label or server object becomes a second task authority.

Replaces the old judge-down-to-clarify clause while retaining mandatory policy/evidence admission. Adopted amendments in ADR 0080/0027 record the trade-off. Existing question specs and the reconciled shared wire contract remain authoritative for support, accepted-input history, task outcomes and current permissions.

**Selected persist list:** Adopt ADR 0080/0027 amendments for optional Jev routing, track versus route intent and post-retrieval decisions; adopt route intent and reconcile Soft-Track/list_files vocabulary; retain existing track/run definitions and source/task ownership.

**Persist result:** Following fresh ACR PASS, domain-modeling accepted ADR 0027/0080 amendments, adopted route intent and reconciled Soft-Track/list_files/Jev vocabulary. Existing track/run definitions and source/task ownership are retained. No runtime change is implied by design adoption.

## Architecture Change Review

Fresh independent review on 2026-10-16 assessed the concrete adopted contracts, including reply admission/ownership, versioned scope, bounded inputs, receipt recovery, publication partial effects and release gates.

- Responsibilities, interfaces, and dependencies: **yes** — existing task/source/grounding authorities remain; app lifespan and execution ownership are explicit.
- Input contracts and test scenarios: **yes** — bounds and invalid, duplicate, stale, concurrent and permission cases are fixed at public seams.
- Failure propagation and partial effects: **yes** — checkpoint ordering, durable redo, causal failures, idempotent recovery and late-result fencing are explicit.
- Structural simplicity and caller burden: **yes** — existing checkpoint, pending question, snapshot, retrieve port and audit table are reused; no new provider or dependency.
- Task scope and migration: **yes** — financial compatibility, upstream-first expansion, supported-caller migration and existing acceptance / G1–G3 gates remain.

**OVERALL: PASS — 0 unresolved applicable concerns.** This is a design verdict, not product implementation or runtime acceptance.

## Plan handoff

[Plan 04](../plans/qa-chain-04-grounded-experience.md), after [Plan 03](../plans/qa-chain-03-corpus-retrieval.md). The [program map](../plans/qa-chain-generalization.md) owns delivery dependencies and the operator's A01–A20 verification ledger, not a competing module contract.
