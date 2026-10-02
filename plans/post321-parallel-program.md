# Post-321 parallel work program

**Goal:** Deliver the bounded SQL tier, stabilize acceptance, make the pipeline reproducible, validate typed evidence and Python code, and verify the product through real browser interactions.
**Basis:** The operator requested parallel subagent-authored plans and corresponding GitHub issues after that pull request merged as `e36999f`.
**Original authoring baseline:** Local `main` at `670f65f`; verify the current merged baseline before execution. This later planning revision was checked on local `main` at `28fa4da`.

## Work allocation

| Lane | Plan | Responsibility | Start condition |
| --- | --- | --- | --- |
| A | `plans/post321-real-sql.md` | Land the settled SQL contract, implement the bounded synthetic-ledger mechanism, then verify synthetic and enterprise-ledger boundaries separately | A2 can start from the that pull request contract on isolated PostgreSQL; production binding and real-source acceptance wait for an approved ledger |
| B | `plans/post321-stability-browser-acceptance.md` | Repair L3 in corpus tests and L2 in host live-gate tests | B1 and B2 can start independently with disjoint ownership |
| C | `plans/post321-pipeline-reproducibility.md` | Fix build reproducibility, then measure the committed gold pipeline and resource limits | Can start independently of A and B |
| D | `plans/post321-real-browser-acceptance.md` | Resolve crop scope, then run the final real user journey | Q1 can start now; B3 waits for A/B/C, the tracked change, and Session E |
| E | `plans/post321-evidence-schema-route.md` | Establish versioned evidence contracts and domain-neutral grounded QA across source, ingress, transport, and UI boundaries | Start now in parallel with A/B/C; SQL-specific conformance consumes A2's producer |
| F | `plans/post321-python-typecheck.md` | Select, baseline, and enforce a scoped Python static type checker | Start now independently; toolchain rollout has its own acceptance and does not impersonate runtime schema validation |

Each execution session uses an isolated worktree. Shared entry points, routing contracts, fixtures, and build artifacts have one owner at a time; reserve actual paths after exploration. Parallel services use separate databases, buckets, ports, browser sessions, and artifact directories. Stop only processes owned by the executing session. Never let differently configured workers share a job queue.

Each session plan owns its detailed tracer bullets. Sessions E and F own the cross-boundary contract work and toolchain rollout separately, so neither waits for Session D's browser blockers. This coordination plan records only dependencies and tracker projection.

## Architecture gate for this planning delivery

```text
bounded-context-guardian: N/A — plans and issue descriptions only; no module boundaries change.
input-contract-tests: N/A — no executable public contract changes in this delivery.
error-handling-enforcer: N/A — no executable failure path changes in this delivery.
complexity-anti-drift: N/A — no executable logic is introduced by this delivery.
minimal-change-verifier: yes — the scope is parallel planning and its authorized issue projection.
```

These exemptions are not architecture approval for future implementation. Apply the architecture gate to the actual multi-file/cross-module implementation scope before coding. The prior waiver concerned a second review of that pull request; it does not waive the normal review of new work. Use failing-first tests where behavior changes, relevant checks and real-boundary acceptance, one required review phase, repair blocked findings, and verification before completion.

**Persist list:** Empty. This delivery records open decisions; it does not change an ADR, glossary, or business contract.

## Browser acceptance policy

Use the `playwright-cli` skill to automate the actual browser interaction layer. Start from the visible workspace, upload committed public fixtures through the composer, enter questions, send, inspect displayed results and source links, reload/reopen for durability, and verify visual refresh/reuse and the SQL product path. Snapshot-derived selectors, named browser sessions, trace/screenshot evidence, and console/network checks must identify the tested revision and scenario.

The final happy path uses the deployed application and real model, database, object storage, and MCP dependencies. API-seeded setup, mocked browser routes, fixture servers, ScriptedChatModel, and Fake ports may support diagnostic tests but cannot replace this final evidence. Durable rows, provider-call counts, observation versions, and source identities corroborate visible browser behavior. Browser automation cannot establish a 4 GB hardware result or fully replace subjective usability assessment.

Operator phase ruling (2026-09-30): the current VM is the development and integration environment; use its actual configuration. C1 and pipeline gold remain prerequisites for B3. C2's dedicated 4 GB measurement is deferred to later target-deployment validation, not a prerequisite for VM preparation or B3 and not a passed check. Full G4 target-deployment completion still requires all original contract clauses. Unavailable credentials, services, or later target hardware are missing evidence, never a passing skip. Keep item 10/10a pending while required clauses remain open. Existing backend-only work in the tracked change is a prerequisite/evidence source to reconcile, not work to duplicate in a new backend ticket.

## Publication and progress

The operator clarified the tracking grain: **one issue per execution session**. Internal tracer bullets remain steps inside their session, not separate issues. Sessions A (SQL), B (stability), C (pipeline), E (evidence schema and routing), and F (Python type checker) can start in parallel, while D's Q1 can also start now. D's integrated browser acceptance waits for A/B/C, E, and existing backend the tracked change. F is a separate quality lane, not a browser-runtime prerequisite. SQL A2 can start against an isolated versioned synthetic ledger using that pull request's pinned contract; A3b still waits for a real enterprise ledger and its authorized read access. Each session issue carries its scope, steps, acceptance and ownership so it can execute without this chat.

The local markdown plans are the authoring artifacts. Publishing issues is explicitly authorized; push, merge, release, and deployment are not authorized by this request. Include the local plan revision and the full ticket text in each issue because the new plan commit may not be pushed.

After each execution, report the issue number, PR/commit, independently expected versus observed results, actual commands and exit codes, artifacts, and remaining blockers. Issue closure requires its acceptance evidence; a code merge alone does not close broader gold acceptance.

## Tracker projection — one issue per parallel session

| Session | Issue | When to execute | Internal work |
| --- | --- | --- | --- |
| A — SQL | the tracked change | Start now, parallel with B/C/E/F | A1 contract landing, A2 implementation on synthetic PostgreSQL, A3a mechanism acceptance, then A3b approved-ledger acceptance when the source exists |
| B — stability | the tracked change | Start now, parallel with A/C | B1 OCR isolation and B2 live-gate stability; one session owns the work, with disjoint modules if it delegates internally |
| C — pipeline | the tracked change | C1 and VM pipeline gold now; dedicated-device measurement in the later deployment phase | Reproducible build and pipeline gold; retain C2 target-device evidence as deferred |
| D — real user journey | the tracked change | Q1 starts now; B3 closure waits for A3b, B1/B2, C1 and VM pipeline gold, E and the tracked change | Q1 crop ruling, then B3 real-model browser/MCP/SQL/storage acceptance |
| E — evidence and route | the tracked change | Start contract/routing work now; producer conformance in B3 waits for A2 | Versioned per-kind evidence schema, public-boundary tests, and domain-neutral source-backed QA |
| F — Python type checker | the tracked change | Start now independently | Tool selection, measured baseline, scoped static-analysis gate |

All session issues carry `ready-for-agent`. the tracked change has native blockers the tracked change, the tracked change, the tracked change, existing the tracked change, and Session E for final integrated acceptance. Under the phase ruling above, the tracked change's current B3 prerequisite covers C1 and VM pipeline gold; dedicated-device evidence remains a later requirement. Remote issue content and metadata are unchanged by this local clarification. Its Q1 decision can start despite those blockers. Session F remains an independent quality lane. Internal ordering remains in the session bodies and lane plans; it is not projected as extra issues.

The earlier step the tracked change/#329 were consolidated into the tracked change; #327 into the tracked change; #328 into the tracked change; #325 into the tracked change. They are closed administratively as superseded, **not completed**. Their acceptance remains pending in the retained session issues. Q1's recorded ruling (2026-09-30) accepts figure-only crop with the honest whole-page fallback for table regions; table crop is a backlog enhancement and is not required before Session D can close. The decision is recorded in the O1 resolution of `docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md` and the updated Session D plan, not in the superseded decision issue.

The local plan commits have not been pushed; the issues carry actionable session content. This planning revision passed its five-axis architecture review and `git diff --check`. The repository gold command `uv run --no-sync pytest -q -m eval tests/eval/test_gold.py` returned **9 passed in 1.25 s, exit 0** on local main `28fa4da`. That scripted baseline is not evidence of SQL implementation, image rebuild, real browser/model/MCP acceptance, or a 4 GB device measurement.
