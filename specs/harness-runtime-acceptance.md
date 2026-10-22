# Spec: Harness Runtime acceptance evidence

**Status:** Draft specification; architecture review and the stated freeze prerequisites are separate from implementation acceptance.
**Date:** 2026-10-21

## Objective

Define the preflight and evidence contract for P4, P8, P9, and P10 so release decisions distinguish durable mechanism behavior, real-browser journeys, and Fin business quality. Acceptance reports preserve every required case and attempt; a retry never turns a failed measured attempt green.

## Boundaries

- **Does:** Specify fail-fast preflight and versioned case requirements, the real multi-process mechanism matrix, Agent UI browser journeys, the Fin case set and frozen rubrics, evidence report fields, and how these results gate P11/P12.
- **Confirms with human:** Playwright Test and any dependency/lockfile change require a separate approved dependency plan before implementation. The closed map/tickets supply the remaining baseline; no new provider, browser matrix, threshold, or quality target is selected here.
- **Out of this spec:** Product implementation, production deployment, adding dependencies, changing project test rules, running a campaign, selecting capacity/SLO values, P11 deployment evidence, and P12 migration/guard-retirement execution. This spec does not claim any lane has passed.

## Assumptions (inherited from closed decisions)

1. Evidence has three separate layers: mechanism tests, real Agent UI journeys, and the versioned Fin business case campaign. They meet at release but do not substitute for one another (ADR 0103; `hr-acceptance-evidence.md`).
2. The user confirms QA Plan 05 T11/T12 implementation is complete as the inherited baseline ([plan](../plans/qa-chain-05-recovery-trace.md), [closeout](../docs/verification/2026-10-19-qa-plan05-closeout.md)). Its database/API/render-seam evidence does not establish browser TaskPanel acceptance, remote Langfuse export, or the broader G1–G3/A01–A20/ZEUS gates; it is separate from Runtime P5.
3. The Fin business set has at least 50 independent cases and remains separate from the authorized real `评测数据集`. Synthetic cases may support acceptance but never increase the real-corpus count.
4. P10.freeze is distinct from P10.business. The freeze gates the business campaign and any joint campaign using business rubrics; it does not create a blanket dependency for mechanism-only P8 or browser-only P9.
5. P11 joins accepted P8, P9, and P10.business evidence. P12 retains migration, compatibility, restore, host-resource, and guard-retirement gates (ADR 0101/0103; rollout ticket).

## Contract

- **P4 preflight:** Before any long campaign, fail fast on required packages, browser availability, and required live model/tool availability for the selected lane; authorized fixtures and rebuild instructions; database name ending in `_test`; actual imports at tested seams; request/event schemas; Runtime readiness; and a short browser/API smoke. Missing prerequisites stop the campaign and are reported as `blocked` or `Not run` with the missing capability.
- **Mechanism evidence (P8):** Exercise at least two Runtime API processes and two graph workers against real PostgreSQL and Redis. Use barrier-controlled races and separate processes for claims that depend on process boundaries; sleeps alone do not establish a race.
- The mechanism matrix covers same-thread FIFO/exclusion and cross-thread overlap; stable operation identity including initial thread creation and duplicate/lost-response delivery; process kill/restart and fresh-process readback; PG commit before Redis publication, Redis interruption, outbox replay, lease takeover and stale-worker fencing; and rejection of stale checkpoint writes through both `put` and `put_writes` after fencing.
- It also covers parser result adoption and readiness compare-and-swap against source replacement, cancellation, and deletion; late/duplicate/out-of-order results; cancellation against a late worker; restart and reconciliation at durable cut points; and typed `404` versus retryable `5xx`/timeout behavior. PG locks never cover external fetch, validation, model/network wait, or indexing.
- **Browser evidence (P9):** Use the built Agent UI against the real application API and durable test services. Include at least three consecutive turns, home navigation/reopen, then at least three further turns with durable prior content; two concurrent threads with out-of-order completion; refresh/reconnect during a run; replay and reset recovery; rapid duplicate activation and lost-response retry; targeted cancellation races; and attachment upload, preparation, readiness, and failure while another thread remains usable.
- Browser checks also prove that malformed local snapshots and stale GET responses do not overwrite newer state, a transient `5xx` is not rendered as “not found,” and each thread retains its own answer, loading state, trace projection/read state, and attachment. Exercise the existing TaskPanel trace read states through the real browser; prior database/API/render-seam evidence is only an implementation baseline. A proxy replay or harness meta-test alone is not real-browser evidence.
- **Business evidence (P10):** Each case has a stable ID and version, one primary category, versioned input/document fixture and provenance, question/action, expected evidence and answer/abstention condition, and a rubric frozen before execution. Minimum primary-category counts are: single-document questions and citations 12; cross-document retrieval/synthesis/conflict 10; tables/numbers/units 8; scanned or mixed PDF 8; multi-turn follow-up/isolation 6; insufficient evidence/no-answer/invalid input 6. Total: at least 50 independent cases.
- Duplicate paraphrases, variants of one assertion, or extra files for the same assertion do not increase the independent count. Every case has one primary category. Authorized synthetic cases may be included with provenance and reproducible fixture instructions but are never counted as a real evaluation corpus.
- P10.freeze may start after P4 and can be prepared early. P8 mechanism-only and P9 browser-only work may proceed without the full P10 freeze; a specific case that uses a P10 rubric waits for that rubric. P10.business requires the frozen set plus integrated P3/P5/P6/P7 and parser fixtures.
- Persist each case result as it completes. Reports are append-only and retain case/run/dataset manifest versions, relevant code/config/model/parser identifiers, attempt number, status, expected rubric version, evidence references, and sanitized failure classification. All attempts remain visible; retries are new attempts and cannot erase failures.
- Every report preserves counts for `passed`, `failed`, `flaky`, `not_run`, `blocked`, and interrupted cases, with stable IDs and reason codes. Missing, blocked, interrupted, or unscored required cases remain in the denominator. A rerun that passes after failure is `flaky`; campaign success requires every required case to pass in the declared measured run.
- Report mechanism, browser, and business results separately. Tool attempt rates and denominators come only from the durable tool outcome ledger; receipts, UI rows, and OpenTelemetry spans are not substitutes. Session records remain authoritative for single-turn troubleshooting; optional traces are diagnostic and do not make an acceptance case pass.
- Session records (thread messages plus receipts) remain always-on, content-complete, and authoritative under existing access, retention, and deletion rules. Traces stay off by default and never copy session text; if enabled, deployment destinations are local PostgreSQL or OTLP, while `console` remains opt-in developer diagnostics. Export failure is best-effort and cannot fail a turn; trace read states remain `off`, `local`, `external`, or `unreadable` (ADR 0102).
- Keep test evidence privacy-safe: do not place credentials, raw environment values, or private document contents in logs, screenshots, traces, or reports. Use authorized fixtures and separate access rules.
- Use Playwright Test fixtures, isolation, and reporters if its dependency plan is approved. Keep product-specific setup and assertions in Fin's fixtures; do not build a second custom browser lifecycle/artifact/report runner.
- P11 cannot pass until accepted P8, P9, and P10.business reports join. P12 cannot retire ADR 0101's guard until these reports, process fencing/recovery, parser recovery, restore/reference checks, and target-resource gates pass.

## Success Criteria

1. A failed preflight prevents a long campaign from starting and records the missing capability as blocked or `Not run`. **Evidence:** preflight cases for each prerequisite and campaign-start audit record.
2. The 2 API × 2 worker matrix runs against real PostgreSQL and Redis, and stale checkpoint `put` and `put_writes` operations are rejected after fencing. **Evidence:** process IDs, barrier schedule, database readback, and rejected-write records.
3. Every listed mechanism failure boundary is exercised with process restart/reconciliation, including parser CAS, cancellation, deletion, Redis/outbox faults, and duplicate or late outcomes. **Evidence:** cut-point matrix with durable before/after assertions.
4. The Agent UI completes the required continuity, concurrent-thread, reconnect/reset, duplicate-intent, cancellation, and attachment journeys with the specified visible state. **Evidence:** browser artifacts tied to stable journey IDs plus durable readback.
5. The frozen Fin case manifest contains at least 50 independent cases and meets every primary-category minimum. **Evidence:** manifest validation and provenance review; synthetic cases are excluded from the real-corpus count.
6. A business campaign passes only when all required cases pass against their predeclared rubric in the declared measured run; every failure, flaky attempt, blocked case, interruption, and not-run case remains countable. **Evidence:** append-only ledger and campaign denominator reconciliation.
7. Tool metrics reproduce their numerators and denominators from canonical ledger records; removing or sampling spans does not alter them. **Evidence:** ledger-based metric reconciliation, including unknown outcomes and zero-denominator cases.
8. P10.freeze is not a blanket prerequisite for mechanism-only P8 or browser-only P9, while P10.business waits for P3/P5/P6/P7 integration and parser fixtures. **Evidence:** dependency-gate review against plan manifests.
9. No P11/P12 release report marks its gate satisfied when required P8/P9/P10.business evidence, restore references, or target-resource evidence is missing. **Evidence:** release-gate checklist and linked report IDs.
10. Invalid/empty case manifests, malformed records, negative or overflowing count fields, concurrent campaign starts, and exceptions are rejected or reported without silently dropping required cases or converting failure to pass. **Evidence:** applicable harness input-contract and fault tests.
11. With no trace exporter selected, trace state is `off` while session-record readback remains available; enabled traces omit session text, and exporter failure does not fail the turn or alter ledger metrics. **Evidence:** default/opt-in trace configuration, redaction, failure, and ledger-reconciliation checks.

## Open Questions

- Playwright Test dependency/lockfile approval remains a separate implementation prerequisite. No other acceptance target is left for this spec to choose.

## Inherits / Changes

- **Fin acceptance case set:** “The evolving, versioned set of at least 50 independent Fin business cases with frozen rubrics, reported separately from mechanism tests, browser journeys, and the real authorized **评测数据集**.” (`docs/CONTEXT.md`)
- **tool outcome ledger:** “The canonical durable record of tool-call validation and execution outcomes from which tool-quality rates and denominators are computed; traces and UI rows may link to it but are not substitutes for it.” (`docs/CONTEXT.md`)
- Inherits ADR 0101's guard-retirement gate, ADR 0102's two-layer session-record/diagnostic-trace contract, and ADR 0103's Runtime ownership and acceptance requirements. Relevant evidence sources are `hr-acceptance-evidence.md`, `hr-tool-validation-observability.md`, and `hr-rollout-guard-retirement.md`.
- **Changes:** None to CONTEXT or ADRs. Persist list: empty.

## Architecture-change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review.
