# Spec: Tool argument validation and outcome ledger

**Status:** Draft target contract; implementation and acceptance remain pending.
**Date:** 2026-10-21

## Assumptions

1. **Confirmed:** This is S4/P3 in the reviewed [Runtime reliability map](../docs/wayfinder/harness-runtime-reliability-map.md); the accepted tool-validation decision is restated in this spec's Contract. The local `hr-tool-validation-observability.md` ticket records decision provenance.
2. **Confirmed:** Each tool has one canonical Pydantic argument schema shared by model exposure and runtime validation, and invalid input is rejected before side effects.
3. **Confirmed:** The default correction bound is one per logical tool call. Correction, execution retry, and uncertain-effect reconciliation are separate paths.
4. **Confirmed:** The canonical outcome ledger is the measurement source; the turn receipt and optional traces are not per-attempt stores. Runtime attempt identity and fencing come from [S1](qa-chain-server-runtime.md#adopted-runtime-target-amendment-2026-10-21).

These assumptions inherit closed operator decisions and the current spec authorization; no new dependency, tool, numerical quality target, persistence schema, or provider is selected here.

## Objective

Specify how model-requested tool arguments are validated, how one safe correction is bounded, how execution outcomes are recorded under Runtime ownership, and how tool-use measurements retain the correct logical and per-attempt denominators.

## Boundaries

- **Does:** One canonical argument contract at the shared invocation boundary; validation before effects; safe bounded correction; distinction among validation failure, known execution failure, and uncertain effect; durable attempt/outcome facts and their metrics.
- **Confirms with human:** No new choice is required for the accepted contract. Any missing tool-specific field bound, authorization rule, or retry policy must be grounded in that tool's existing domain contract before implementation; this spec invents none.
- **Out of this spec:** Implementing validators, graph wrappers, prompts, tool descriptions, storage DDL, metrics dashboards, model/provider selection, OTel configuration, or changing the turn receipt. Case-level selection and task-quality scoring remain governed by the versioned acceptance rubric.

## Contract

- A tool has one canonical Pydantic schema for both model-facing exposure and runtime validation. Simple arguments may derive from the tool's parameter annotations; nested or nontrivial arguments use an explicit schema. Do not add a second hand-copied validator.
- Validate and normalize input at the shared invocation boundary before the domain operation can cause side effects. Keep authorization and semantic preconditions in their domain checks; do not disguise them as schema errors or ask the model to guess protected values.
- An eligible schema failure may return one bounded correction to the model. It may identify a field path, expected shape/type, and safe constraint, but must omit submitted values, raw kwargs, exception representation, stack trace, credentials, and source content. A second invalid response is a typed terminal validation failure for that logical call.
- Keep three paths distinct: invalid-before-execution may be corrected; a known execution failure follows the operation's declared retry policy; an uncertain outcome after a side effect may have happened must be reconciled by stable operation identity before another execution. Never represent an unknown effect as if no effect occurred.
- Preserve separate identities for the run, logical tool call/correction loop, each model-emitted attempt, and each execution attempt, together with the current Runtime ownership/fencing identity. A provider tool-call ID alone does not identify the logical call. Retries may create additional attempts without creating another durable business operation for the same intent.
- The ledger records canonical facts sufficient to distinguish at least: schema rejected, correction offered, valid input accepted, execution started, succeeded, known failed, outcome unknown, reconciled, and correction exhausted. Facts are immutable observations; duplicate delivery cannot create another logical result, and a stale/fenced worker cannot commit a newer outcome. This contract does not prescribe a table, field layout, or migration.
- Compute attempt and logical-call measures from the canonical ledger. Keep unknown execution outcomes visible and separate from success. A zero eligible denominator is `N/A`; report retries per attempt and per logical operation so retries cannot inflate quality.
- OpenTelemetry spans remain optional diagnostic detail and may be sampled or absent. Neither spans, UI rows, SSE frames, nor the turn receipt supply attempt totals, rates, evaluation scores, or billing denominators. A trace may link to ledger identities when available; export failure cannot fail an Agent run.

| Measure | Numerator / denominator |
|---|---|
| First-attempt schema-valid rate | First model-emitted attempts accepted by the canonical schema / first attempts reaching that validator. |
| Correction recovery rate | Invalid logical calls corrected to valid input within the bound / invalid logical calls eligible for correction. |
| Execution success rate | Logical calls whose started operation is known successful or successfully reconciled / logical calls whose execution began; report unknown outcomes separately. |
| Expected-tool selection | Gold-labeled opportunities where the expected tool was selected with valid arguments / gold-labeled opportunities requiring that tool. |
| Unnecessary-call rate | Scored cases containing a prohibited or unnecessary call / scored cases labeled as no-tool or unnecessary-tool opportunities. |
| Tool-use rate (descriptive) | Scored cases with at least one model-emitted tool call / scored cases whose frozen rubric declares a tool opportunity; report required, optional, and prohibited opportunities separately. |

## Success Criteria

- **TV-1 — One schema:** For every registered tool, schema-contract evidence shows model exposure and runtime validation come from the same canonical Pydantic contract. Empty values where prohibited, missing/extra fields, wrong types, invalid enums, nested values, and declared-bound violations produce the contract's typed outcome.
- **TV-2 — No invalid side effects:** Invocation-boundary tests prove invalid input never reaches a side-effect boundary. Applicable public-input cases cover empty, malformed/negative, at/beyond declared bounds, concurrent duplicate intent, and raised exceptions without inventing new field limits.
- **TV-3 — Bounded safe correction:** Graph-level evidence shows at most one correction attempt by default for an eligible logical call; corrected input remains correlated to that call; exhaustion becomes a typed outcome. Submitted values and exception text are absent from model feedback, durable facts, logs, and trace attributes.
- **TV-4 — Retry and uncertainty separation:** Fault/idempotency evidence distinguishes invalid-before-execute, known failure, duplicate delivery, declared retry, and uncertain post-effect outcome. An uncertain effect is reconciled before replay, and fencing rejects a late outcome from an old Runtime attempt; PostgreSQL-backed tests use an isolated database whose name ends in `_test`.
- **TV-5 — Complete measurement facts:** Ledger evidence distinguishes the required outcome states and the run/logical-call/model-attempt/execution-attempt identities. Duplicate delivery cannot add a logical result; retries remain visible as attempts; unknown outcomes are not counted as success. Persistence integration evidence uses an isolated `_test` database.
- **TV-6 — Stable denominators:** Metric evidence derives first-attempt schema validity, correction recovery, and execution success from the ledger with the ticket-defined numerators/denominators. Zero eligible cases report `N/A`; missing or sampled spans do not change totals. Expected-tool selection, unnecessary-call, and descriptive tool-use measures use the frozen rubric and their distinct units/denominators; tool-use reporting separates required, optional, and prohibited opportunities. A higher call rate is not a quality target. S5's campaign task-success denominator remains every required case, including blocked or unscored cases.

## Open Questions

No architecture decision remains open. Before implementing a particular tool, inventory its existing declared bounds, authorization/semantic checks, and retry/reconciliation policy; an absent policy stays open for that tool rather than receiving a new default here.

## Inherits / Changes

The following definitions are copied verbatim from [CONTEXT](../docs/CONTEXT.md):

> **logical tool call**: One model-requested tool action and its bounded argument-correction loop, tracked separately from each model-emitted attempt and each execution attempt.

> **schema correction**: The single bounded model feedback opportunity for a tool call rejected by its canonical Pydantic argument schema before side effects; execution failures and uncertain effects follow separate policies.

> **tool outcome ledger**: The canonical durable record of tool-call validation and execution outcomes from which tool-quality rates and denominators are computed; traces and UI rows may link to it but are not substitutes for it.

> **operational trace**: The optional, best-effort five-span OpenTelemetry view adopted in ADR 0102 for diagnosing selected execution seams; it may be sampled or incomplete and does not count all tool attempts or establish evaluation outcomes.

> **turn receipt**: One logical operational summary of an accepted answer turn, correlated across pause and continuation and distinct from task state, task completion, and per-tool-attempt measurement records.

This spec inherits ADR 0102 and 0103 and the [Runtime ownership/fencing contract](qa-chain-server-runtime.md#adopted-runtime-target-amendment-2026-10-21). It adds no bounded context, domain term, or one-way-door decision; **persist list: empty**.

## Architecture Change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review.

## Plan handoff

This is the S4/P3 contract. After independent architecture review, the later plan must inventory the existing tool schemas and policies before assigning implementation work; this spec makes no implementation or test-pass claim.
