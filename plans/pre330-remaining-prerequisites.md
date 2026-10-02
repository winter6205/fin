# Plan: Remaining prerequisites for #330 real browser acceptance

**Goal:** Organize remaining #330 prerequisite work into execution sessions and let ROOT decide whether B3 is ready to start.
**Approach:** Four sessions cover SQL, pipeline, data-scope, and current-main live evidence; an optional fifth covers zero-baseline comparison only if the selected B3 gold case needs it. ROOT integrates the reports and maps evidence to the integrated candidate's changed paths. This plan does not execute B3.
**Basis:** `docs/handoff/2026-10-02-pre330-repairs.md`; `docs/validation/2026-10-02-pr339-repairs.md`; `docs/validation/2026-09-30-g4-buildkit-reproducibility.md`; `plans/post321-real-sql.md`; `plans/post321-pipeline-reproducibility.md`; `plans/post321-real-browser-acceptance.md`; `plans/post321-parallel-program.md`; `specs/search-mode-business-sql-contract.md`.
**Baseline:** Fin authoring baseline is `ecd3ab4`; reconcile fin and iknow-memory against current merged heads, including iknow-memory `e8165a7` and PR #340 `b140366`. PRs #279, #339, and #340 are merged. Do not redo B1/B2, #334, #335, or Q1. Q1 accepts figure-only crops with honest whole-page fallback for table regions; table cropping remains a backlog item.

**Architecture gate for this planning artifact:**

```text
bounded-context-guardian: N/A — this artifact changes no module boundary.
input-contract-tests: N/A — this artifact changes no executable public contract.
error-handling-enforcer: N/A — this artifact introduces no executable failure path.
complexity-anti-drift: N/A — this artifact introduces no executable logic.
minimal-change-verifier: yes — only this remaining-prerequisites plan is in scope.
```

This planning verdict does not approve future implementation. Each implementation session runs the architecture gate against its actual scope before coding. For code changes, use failing-first tests, focused checks, one end-of-session code-review phase, repair blocked findings, and verification-before-completion. No new GitHub issues or remote publication are part of this plan.

**Execution boundaries:** The operator's instruction to ignore CI for this preparation round remains in force: do not wait on CI, change CI/settings, or claim CI health. No dedicated 4 GB device is required for the current VM; C2 remains deferred and unverified. Active tokens remain unsuffixed with explicit `schema_version`; do not put phase labels in product code. Give each session an isolated worktree, ports, checkpoint schemas, buckets, indices, log directory, and queues; the read-only ledger may be shared. Preserve the six shared containers, never run a global prune, and stop only servers owned by the session. Preserve pre-existing dirty sibling files such as `AGENTS.md`. Use the normal test loader for credentials; never print environment files or include secrets in reports. For live preparation, load the protected `.fin/pre330/environment.sh` without printing its contents, then explicitly set `FIN_LIVE_IKNOW_ROOT` to the session-owned, built producer checkout. Confirm its revision and distribution before running: a detached baseline may also contain `dist/` and is not a safe fallback.

**Per-ticket loop for implementation work:** failing-first test → focused checks → one code-review phase after the session's implementation → (GATE BLOCKED → review-report-repair) → verification-before-completion. Do not turn unavailable evidence into a passing skip.

**Persist list:** Empty. This plan records open choices and evidence work; it changes no domain contract, glossary, or ADR.

## Session map

| Session | Tasks | Order and start condition |
| --- | --- | --- |
| 1 — SQL path | T1–T2 | T1 date-input decision, then T2 end-to-end path |
| 2 — pipeline | T3–T5 | T3 pin policy, then T4 reproducibility, then T5 VM gold |
| 3 — data scope | T6 | Can run in parallel; existing development read-only checks are authorized |
| 4 — current-main evidence | T7 | Reconcile first; rerun only missing or stale cases |
| 5 — zero baseline (optional) | T8–T9 | Only if the selected B3 gold case includes a zero baseline |
| ROOT — readiness | T10 | After required session reports return and candidate changes are integrated |

## Tasks (ordered by dependency)

1. **SQL — settle how user requests supply the date window** — tag: `[decision]`
   - **Inherits:** `specs/search-mode-business-sql-contract.md` §2.3: “WHERE expiry_date BETWEEN $1::date AND $2::date”. How a user's wording reaches those two bounds remains open.
   - **Surface:** Existing user request, host retrieve tool, and SQL date-window contract.
   - **Acceptance:** Record when the user-facing path can provide a complete inclusive `YYYY-MM-DD` pair and what happens before query execution when bounds are missing, malformed, reversed, relative, or ambiguous. Either explicit natural-language extraction or a clarification step may satisfy the contract; if relative dates are supported, define their timezone and mapping. Do not infer an implicit date window from the current date, locale, or timezone. Keep the choice at contract level so implementation may use different UI or tool details.
   - **Completion:** A later implementer can submit a user request with explicit dates and verify the exact pair and inclusive semantics at the SQL call; incomplete input has a named refusal or clarification outcome.
   - **Dependency:** None; may run alongside T3, T6, and T7.
   - **Status:** [x] done — contract recorded in `docs/validation/2026-10-02-sql-date-input-contract.md` (PR #343)

2. **SQL — connect user dates through the real producer path** — tag: `[implementation]`
   - **Inherits:** T1's accepted date-input behavior and the contract query: “WHERE expiry_date BETWEEN $1::date AND $2::date”.
   - **Surface:** User-to-host tool input, retrieve port, real SQL adapter, and database-row producer/evidence path.
   - **Acceptance:** A user request entered through the host-tool path reaches the real adapter and producer with the selected bounds; the query uses those exact inclusive values and produces independently expected row evidence. Missing or invalid bounds follow T1 without defaults. Run `uv run --no-sync pytest -q tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` and `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py`; report pass/fail/skip/deselected counts. The fixed-date live suite, a diagnostic invocation, or a narrow UI smoke does not alone prove user-to-producer reachability or formal B3.
   - **Completion:** Sanitized evidence connects the entered request, host arguments, adapter bounds, producer result, and independent expected values on the tested revisions. No `real_db_not_wired` gate is reintroduced where the merged producer path already exists.
   - **Dependency:** Blocks on T1. After T1, may run alongside T3, T6, and T7.
   - **Status:** [x] done — user-to-producer path proven live, one hop; evidence in the same doc (PR #343)

3. **Pipeline — settle the C1 pin policy against the accepted operator ruling** — tag: `[decision]`
   - **Inherits:** `docs/validation/2026-09-30-g4-buildkit-reproducibility.md`: “move to BuildKit, establish a new baseline, keep `8e10185f…` as history, and prove it with two independent clean builds.”
   - **Surface:** Existing G4 pin manifest, committed image identity, C1 plan, and reproducibility record.
   - **Acceptance:** Reconcile the stale C1 plan wording with the accepted pin policy and current merged artifacts. Record that `8e10185f…` is historical and that no new image identity is a baseline until T4 proves reproducibility. Current full builds `f896…` and `8f000…` differ in two layers; do not silently adopt either. Record `build.sh` exit 75 as an identity-gate refusal: C1 remains blocked and has no reproducibility pass. Any proposed change to the accepted pin policy must be recorded as an explicit operator decision before T4 adopts it.
   - **Completion:** The tracked C1 policy names the identity to prove and the rule for withholding a new baseline while builds disagree.
   - **Dependency:** None; may run alongside T1, T6, and T7. T4 blocks on this decision.
   - **Status:** [x] done — C1 policy reconciled and recorded in `docs/validation/2026-10-02-c1-pin-policy.md` (PR #344)

4. **Pipeline — repair C1 reproducibility and prove the agreed identity twice** — tag: `[implementation]`
   - **Inherits:** T3's pin policy and L7's acceptance: “a rebuild from the pin reproduces the recorded `sha256` byte-for-byte, with versions pinned mechanically rather than in prose.”
   - **Surface:** Existing G4 build inputs, identity artifacts, reproducibility checks, and C1 evidence.
   - **Acceptance:** Investigate the measured layer differences and repair the demonstrated source of nondeterminism; prior cache or metadata explanations remain hypotheses until verified. Then run two independent clean builds from the agreed immutable inputs and prove they produce the same image identity. Keep `memory_ceiling` as a build input distinct from runtime measurement caps. Do not silently change the expected identity or declare a new baseline before both clean builds match; if the proof still fails, leave C1 pending with the exact cause and evidence.
   - **Completion:** The policy-approved image identity is reproduced by two independent clean builds, or C1 remains explicitly blocked without an adopted mismatching baseline.
   - **Dependency:** Blocks on T3; T5 blocks on T4.
   - **Status:** [x] done — two independent clean builds agreed on `b1b5270c…`, 11/11 layers; baseline adopted by operator ruling (PR #344)

5. **Pipeline — measure committed gold on the current development VM** — tag: `[implementation]`
   - **Inherits:** `plans/post321-pipeline-reproducibility.md`: “Dedicated 4 GB hardware is not a prerequisite for this environment preparation or B3's browser acceptance. Retain the target-device measurement as deferred deployment evidence, not a passing result.”
   - **Surface:** Existing `scripts/g4_pipeline` harness, committed attachment gold, and validation evidence.
   - **Acceptance:** After T4, run `bash scripts/g4_pipeline/run_measured.sh` on each of `data/corpus/g4_attachment_gold/g4-native-text-001.pdf`, `data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf`, `data/corpus/g4_attachment_gold/g4-scanned-001.pdf`, and `data/corpus/pdf/2022_Q3_MSFT.pdf`. Compare with independent expectations; record revision, hashes, exit codes, page/region/OCR outcomes, actual runtime caps, and peaks. Use existing `G4_CEILING_MEMORY`, `G4_CEILING_MEMORY_SWAP`, and `G4_CEILING_CPUS` only for the VM's documented runtime limits; do not invent flags or call a default 4 GB container cap dedicated hardware. The PNG's expected PDF-only exit 1 is a rejection, not a pass. Keep builder pins separate from gold runtime caps.
   - **Completion:** All four PDFs have comparable VM evidence, or each missing case remains pending with its expected result, command, exit, and prerequisite. C2 remains deferred, not passed.
   - **Dependency:** Blocks on T4; is a prerequisite for T10. May run alongside T6 and T7 after T4.
   - **Status:** [x] done, with recorded mismatches — four PDFs measured on this VM in `docs/validation/2026-10-02-c1-vm-gold.md`; C2 stays deferred (PR #344)

6. **Data scope — record the limits of development-only evidence and ask for any scope amendment** — tag: `[decision]`
   - **Inherits:** `plans/post321-real-sql.md` A3b: “A synthetic ledger cannot close this leg” and “If no approved ledger exists, leave A3b explicitly blocked.” The operator has confirmed that no enterprise data exists and development resources are available.
   - **Surface:** A3b source boundary and #330's accepted browser scope.
   - **Acceptance:** Use already-authorized development read-only fixtures and resources when preparing a concrete proposal; do not request permission again to verify those fixtures. State exactly which browser behaviors the proposed development-only scope can prove and which enterprise-ledger claims remain unproved. Obtain an operator ruling only on whether to amend #330's acceptance to allow that bounded development scope. Keep original enterprise A3b deferred and not passed. Do not imply synthetic or public rows meet the enterprise contract.
   - **Completion:** The proposal and any scope ruling are recorded in tracked artifacts. If no amendment is accepted, name the unavailable source/access as a blocker; in either case, original enterprise A3b remains pending.
   - **Dependency:** None; may run alongside T1, T3, and T7. T10 consumes this outcome.
   - **Status:** [x] done — proposal and the operator scope ruling recorded in `docs/validation/2026-10-02-pre330-data-scope.md` (PR #341); A3b still blocked

7. **Live evidence — reconcile #276 and run only missing or stale MCP/Jev proof** — tag: `[implementation]`
   - **Inherits:** `plans/post321-real-browser-acceptance.md`: “Reconcile issue #276's live MCP and `pg_es` evidence against the current merged iknow build, and run the required Jev live round-trip.” Remote #276 being OPEN is status, not a failed test; 115 default skips are not live proof.
   - **Surface:** Existing #276 evidence, live MCP/pg_es suites, Jev live suites, and current merged distribution.
   - **Acceptance:** Reconcile case-by-case before running; reuse only evidence whose tested revisions, built distribution, and cases match current merged fin/iknow. For missing or stale cases, use an isolated owned sibling worktree and rebuilt distribution. MCP command: `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py tests/retrieve/test_live_mcp_t3.py tests/retrieve/test_live_mcp_t4.py tests/retrieve/test_live_mcp_t5.py tests/retrieve/test_live_corpus_ingest.py tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_mcp_read_source.py`; also cover the existing live ES suite and relevant PostgreSQL checkpoint cases. Jev command: `uv run --no-sync pytest -q -m jev_live tests/host/test_live_jev_search_mode.py tests/host/test_live_jev_track.py tests/retrieve/test_live_jev_tier_reaches_wire.py`. Preserve dirty sibling `AGENTS.md`. Report every pass/fail/skip count and distinguish reused evidence from reruns.
   - **Completion:** Every required case is mapped to tested revisions and evidence, or remains explicitly pending. This parallel baseline proof is not automatically accepted for T10 after SQL or contract changes; ROOT binds evidence to the integrated candidate and reruns only affected cases.
   - **Dependency:** None; may run alongside T1, T3, and T6. T10 consumes the reconciliation.
   - **Status:** [x] done — #276 reconciled and stale cases rerun in `docs/validation/2026-10-02-pre330-live-evidence.md` (PR #342)

8. **Optional zero baseline — decide whether the selected B3 gold case needs it** — tag: `[decision]`
   - **Inherits:** none — open. B3 does not require a zero-baseline gold case; this choice follows the selected B3 gold case, not the A3b or SQL scope decision.
   - **Surface:** Selected #330 browser gold case, fact-delta result, and enterprise-evidence schema.
   - **Acceptance:** If the selected B3 gold case does not include a zero baseline, record that it is out of scope and creates no blocker. If it does, settle an explicit versioned result shape and backward-compatibility behavior before implementation. The current producer's `percent: null` conflicts with the frozen numeric-only §4.3 validator; do not fabricate zero or infinity percentages or weaken strict guards.
   - **Completion:** The case is explicitly excluded, or an approved contract is ready for T9.
   - **Dependency:** Conditional on the selected B3 gold case; independent of T6. T9 blocks on an affirmative T8 decision and is needed for T10 only if the case is selected.
   - **Status:** [x] out of scope by operator ruling — no zero-baseline gold case is selected; `percent: null` vs the frozen numeric-only §4.3 conflict is an open backlog item

9. **Optional zero baseline — implement only the approved contract** — tag: `[implementation]`
   - **Inherits:** T8's accepted schema-version and backward-compatibility decision.
   - **Surface:** Coordinated fact-delta producer, evidence contract, validator, and affected result path.
   - **Acceptance:** Focused contract coverage proves that the selected zero-baseline result follows the approved schema, remains distinguishable from invalid or unavailable comparisons, and preserves specified consumer compatibility. Do not run or claim formal B3 as part of this task; do not fabricate a percentage or weaken a guard.
   - **Completion:** The focused producer/schema checks pass on agreed revisions, or the optional case stays outside #330.
   - **Dependency:** Conditional; blocks on affirmative T8.
   - **Status:** [ ] not started — conditional on T8, which was ruled out of scope

10. **ROOT — make the final B3 readiness decision from returned evidence** — tag: `[decision]`
    - **Inherits:** `plans/post321-real-browser-acceptance.md`: “C2's dedicated 4 GB measurement is deferred to target-deployment validation and does not block VM preparation or B3.” Q1 accepts figure-only crop with honest whole-page fallback. Original enterprise A3b remains separate from any approved development-only scope.
    - **Surface:** #330 prerequisite graph and returned reports from Sessions 1–4; Session 5 only if T8 selected zero-baseline acceptance.
    - **Acceptance:** Integrate the session work, map evidence to the integrated candidate's actual changed paths and revisions, and run only rechecks made necessary by those changes. Session 4 may prepare baseline proofs in parallel; main evidence is not automatically valid after SQL/contract changes, and every heavy suite need not be repeated unconditionally. Record either that B3 may start under an operator-approved bounded development scope, or that it remains blocked with each missing decision/evidence named. Verify existing B1/B2, #334, #276, and Q1 evidence for currency without redoing completed work. This is a readiness decision only; do not execute B3.
    - **Completion:** ROOT records a reviewable ready/blocked verdict, accepted scope, changed-path-to-evidence mapping, targeted recheck results, evidence paths, and remaining prerequisites. Do not infer enterprise acceptance, dedicated-device evidence, live success from skips, or CI health.
    - **Dependency:** Blocks on T2, T5, T6, and T7; also T9 only if the selected B3 gold case includes zero baseline. ROOT performs this after integration.
       - **Status:** [x] done — readiness verdict recorded in `docs/validation/2026-10-02-pre330-b3-readiness.md`

## Session launch prompts

**Session 1 — SQL (T1–T2):** “Settle how a user request supplies the inclusive date window, allowing either explicit extraction or clarification for incomplete or ambiguous bounds. Then carry the accepted dates through the user-to-host, real-adapter, and producer path. Run the named focused and live SQL suites, record pass/fail/skip/deselected counts and independently expected versus observed rows, and return sanitized evidence. Do not claim B3.”

**Session 2 — pipeline (T3–T5):** “Reconcile the C1 pin policy with the 2026-09-30 BuildKit decision and current merged evidence; do not adopt a mismatching image identity. Repair the named nondeterministic layers and prove two independent clean builds, then run the four PDF gold cases on the current VM. Return root cause, identities, commands/exits, expected versus observed gold, runtime caps, resource peaks, and evidence paths. Keep dedicated-device C2 deferred.”

**Session 3 — data scope (T6):** “Use the already-authorized development read-only fixtures to prepare a concrete development-only #330 evidence proposal. State exactly what it proves and what enterprise A3b still requires. Ask for an operator ruling only on changing the browser acceptance scope; do not request renewed permission to inspect or verify the existing development fixtures.”

**Session 4 — current-main evidence (T7):** “Reconcile #276 and current-main MCP/Jev evidence before running. Reuse only revision-matching proofs; run only missing or stale cases with the rebuilt distribution in an isolated worktree. Return exact commands/exits, all pass/fail/skip/deselected counts, tested revisions, and evidence paths. ROOT will bind this baseline evidence to the integrated candidate and rerun only affected cases.”

**Session 5 — zero baseline (T8–T9, conditional):** “Start only if the selected B3 gold case includes a zero baseline. Decide the versioned result and compatibility contract, then implement focused producer/schema checks if the choice is affirmative. Do not treat this task as B3 or as a universal #330 blocker.”

**ROOT — readiness (T10):** “After required sessions return, map reports to the integrated candidate's actual changed paths. Run only targeted rechecks justified by those changes. Record whether B3 may start under an explicitly approved development-only scope or remains blocked, while keeping enterprise A3b and dedicated-device C2 separate and unpassed. Do not execute B3.”

## Progress return template

Each session returns this record to ROOT:

```text
Session / task IDs:
Branch, tested revision, and commit (if any):
Changed artifact paths:
Commands and exit codes:
Pass / fail / skip / deselected counts, including skipped cases:
Independent expected values versus observed values:
Evidence paths (sanitized):
Decisions made and choices still pending:
Completion state and remaining blockers:
Merge readiness:
```

Session reports are evidence, not permission to publish. Do not create issues, push, merge, release, or deploy under this plan.
