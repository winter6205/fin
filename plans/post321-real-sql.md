# Plan: Open the real SQL search mode

**Goal:** Make `search_mode=sql` answer one contract-expiry question through the existing grounded retrieval path with bounded read access and database-row evidence; keep production binding blocked until an approved enterprise ledger exists.
**Approach:** The A1 contract in PR permits implementation and mechanism tests against an isolated PostgreSQL with a versioned synthetic ledger. A2 builds that vertical path, A3a proves the mechanism on the real database engine, and A3b separately proves it against the approved enterprise source when one exists. Browser-level interaction belongs to B3 in `plans/post321-parallel-program.md`.
**Spec links:** `specs/search-mode-retrieve.md`; `specs/search-mode-lexical-sql-tokens.md`; `docs/CONTEXT.md`; [that pull request business SQL contract at the reviewed head](https://github.com/winter6205/fin/blob/a5a21be2928c1ffd8bdeabd069279e36635c24da/specs/search-mode-business-sql-contract.md). The latter is a proposed tracked spec, not a file present on this local baseline.
**Per-ticket loop (A2):** tdd → relevant typecheck/tests → one required code-review phase after the implementation slice → (GATE BLOCKED → review-report-repair) → verification-before-completion. A3 adds live-boundary evidence; it does not replace B3.

## Architecture gate for this planning delivery

```text
bounded-context-guardian: N/A — this delivery adds a plan only; A1 must name the owning bounded context before implementation.
input-contract-tests: N/A — no executable public contract changes in this delivery; assess A2 after A1 settles the SQL input contract.
error-handling-enforcer: N/A — no failure path changes in this delivery; assess A2 after A1 settles typed database failures.
complexity-anti-drift: N/A — no executable logic is introduced in this delivery; assess A2 against the selected implementation scope.
minimal-change-verifier: yes — only this lane plan is in scope.
```

This is not architecture approval for A2. Run the architecture gate against the accepted A1 contract and actual cross-repository implementation scope before coding. PostgreSQL is the explicit development-engine assumption in that pull request; the operator's actual ledger, its engine and its tenant/department scope remain unknown.

## Tasks (ordered by dependency)

1. **A1 — Reconcile and land the business SQL contract** — tag: `[decision]`
   - **Inherits:** `specs/search-mode-retrieve.md`: “D 真数据库: real SQL/business DB path; until wired, 具名失败 真库未接. Figures that live only in filings stay on B.” `docs/CONTEXT.md` also says document tables stay on `graph`.
   - **Surface:** Existing search-mode/retrieve contract, iknow-memory business-query ownership, and fin's existing retrieve seam. Reconcile the tracked change as evidence/prerequisite; do not duplicate its live MCP work.
   - **Acceptance:** Land the settled PR  contract: contract expiry only, fixed parameterized query, trusted application identity with database-enforced read scope, independent `database-row` evidence and snapshot semantics, and two explicit verification legs. Before A2 closes, settle the spec's still-open SQL zero-row operator outcome and the concrete attestation of trusted authorization context; an unverified context must be refused. Production source, owner, engine adaptation, and read grant remain explicit blockers for A3b, not blockers for A2 or A3a. Keep `real_db_not_wired` until A2 lands; never use checkpoint or corpus/index Postgres as the enterprise ledger.
   - **Completion:** The committed contract supports an implementer building and verifying a bounded path against an isolated PostgreSQL without claiming a real enterprise source or production acceptance.
   - Status: [ ] pending

2. **A2 — Implement one bounded SQL vertical slice** — tag: `[implementation]`
   - **Inherits:** A1's bounded contract-expiry query, authorization chain, limits, `database-row` evidence, snapshot semantics, and named failures; the existing one grounded `retrieve` seam and per-call search-mode assignment.
   - **Surface:** iknow-memory's independent business-query module and retrieve dispatcher, fin's existing retrieve port and SQL-open gate, and the evidence/citation path. Preserve one retrieve tool and keep `graph`/`global` behavior intact.
   - **Acceptance:** A grounded `sql` request executes only the fixed contract-expiry query against the isolated, versioned synthetic ledger in a real PostgreSQL. Results carry structured row evidence traceable to this run, never fabricated document spans. Unknown templates, writes, untrusted context, over-limit/timeout and unavailable cases have distinct typed outcomes; a legitimate zero-row query is reported accurately. Existing default tests stay green. Coordinate the database-row validator shape with the schema plan before the fin-facing wire shape freezes; do not gate this mechanism work on the tracked change or the tracked change.
   - **Completion:** A reviewer can demonstrate the one approved query against independently known synthetic rows and show that database privileges deny writes and over-reads. This proves the mechanism, not production binding.
   - Status: [ ] pending
   - [blocks: A1]

3. **A3a — Verify the SQL mechanism on synthetic rows in real PostgreSQL** — tag: `[implementation]`
   - **Inherits:** A1's leg-2 contract and A2's shipped execution/evidence shape.
   - **Surface:** Opt-in live retrieve/MCP integration for the isolated ledger and fin's existing live retrieve driver.
   - **Acceptance:** A non-mocked run reaches PostgreSQL, matches independently authored synthetic contract values and query timestamp, proves database-enforced read-only and view scope, and distinguishes legitimate zero rows from invalid, denied, timed-out and unavailable paths. Record sanitized code/database/fixture versions, commands and outcomes. A fake port, scripted planner, or green default suite cannot satisfy this leg.
   - **Completion:** The measured mechanism works with a real database and independently known synthetic rows; the evidence explicitly says it does not prove enterprise-ledger access or full browser behavior.
   - Status: [ ] pending
   - [blocks: A2]

4. **A3b — Verify the approved enterprise-ledger boundary** — tag: `[implementation]`
   - **Inherits:** A1's leg-1 contract and A3a's proven mechanism. The real ledger, owner, authorization and scope must be identified first.
   - **Surface:** Authorized source mapping, read-only role, and opt-in live retrieve/MCP driver; B3 owns the final browser journey.
   - **Acceptance:** Against the operator-approved ledger, a non-mocked query matches values independently visible in its system of record; role, view and current permission checks deny writes and over-reads. Record source owner approval, tested revision, engine adaptation, sanitised evidence and gaps. A synthetic ledger cannot close this leg.
   - **Completion:** A reviewer can verify the source and DB-enforced read boundary without relying on model prose. If no approved ledger exists, leave A3b explicitly blocked.
   - Status: [ ] blocked on real source and access
   - [blocks: A3a]

## Evidence and ownership notes

- `src/fin/retrieve/tiers.py` keeps `sql` in the closed token set and currently maps it to `real_db_not_wired`; `src/fin/retrieve/port.py` refuses it before transport. These are fin's host-side seams, not the business database.
- `../iknow-memory/src/core/retrieval/search-mode.ts` currently maps `sql` to a typed not-wired arm. that pull request settles an independent `database-row` evidence kind; implementing its producer and verifying the wire contract remain pending.
- `../iknow-memory/docs/runbooks/sql-graph-arm-archived.md` says the old one-hop Postgres graph arm was removed from production wiring and that re-enabling it requires a spec. That archive is not evidence of a business SQL path.
- `plans/post321-parallel-program.md` assigns the final browser journey to B3. `plans/post321-evidence-schema-route.md` owns cross-boundary evidence-schema validation; coordinate the SQL wire shape with it rather than duplicating a validator. the tracked change is a live search-mode MCP follow-up and evidence source; inspect its latest state, then avoid duplicating it.

**Persist list:** Empty. A1 owns any domain or ADR changes required by its approved contract; do not invent or persist new SQL terminology while authoring this plan.
