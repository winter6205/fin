# T10 — B3 readiness verdict for the tracked change, on the integrated candidate

Date: 2026-10-02. Task: `plans/pre330-remaining-prerequisites.md` T10 (ROOT readiness decision).
Corrected revision of this record: **the tracked change**, rebased onto the final integrated candidate
`2fe787f` (see §3 for the assembly and §3.1 for the seven corrections this revision makes to the
text that that pull request proposed).

This is a **readiness decision only**. No B3 browser leg was executed and no product code was
changed for this record. Test results are attributed to the revision that actually produced them
(§3.1 corrections 2 and 3); where a result was reused rather than re-run, that is stated on the
row. The default-suite regression recorded in §6 ran separately, in the T1/T2 worktree, and was
**not** repeated here — see §6.1 for the exact shape of that number.

**Amended 2026-10-03** by the gold-residual task on base `194d5ba`
(`docs/validation/2026-10-03-c1-gold-residual.md`): the three gold items this record carried as
STILL UNMET in §11.1 are re-derived from measurement, and the per-case verdicts in §11.3 are
re-derived from them. **The §1 verdict is unchanged, and the amendment does not claim otherwise.**

## 1. Verdict

> ## **READY for the bounded development round** — full the tracked change acceptance **BLOCKED**
>
> The bounded development B3 run **may start**. As constituted on candidate `2fe787f` the blocker
> was `g4-mixed-chart-001`, whose page-2 `table` region floor was authored in the
> candidate's own manifest and measured unmet (`table 0`) with no recorded decision scoping it
> out (§11.2). **That gold blocker is closed as of 2026-10-03** — the floor is now met by
> measurement, and all three selected gold cases are READY on the gold-expectation axis
> (§11.3).
>
> The READY covers the operator-approved bounded development round only: **S1, S3, S4,
> S6-mechanism, S7, S8**. The operator ruled that the enterprise ledger and the dedicated 4 GB
> device do not gate that round, so A3b and C2 are open prerequisites of the *enterprise*
> acceptance, not gates on this one.
>
> **Full the tracked change acceptance remains BLOCKED.** A3b is still BLOCKED and UNPASSED, so **S2's
> enterprise-anchored comparison and
> S5b's approved-ledger SQL turn stay UNCLOSED**; real-model date extraction / clarification
> is still unproven; C2 is still deferred with no dedicated 4 GB device measured. Closing a
> gold gap removed one blocker, not the verdict: the amendment ruling in §2 explicitly
> preserves the non-waiver list, and nothing in this amendment waives an enterprise clause.

The amendment's scope ruling is unchanged and is not what blocks: of B3's eight acceptance steps, six
are provable on development read-only data and data-independent mechanics (S1, S3, S4, S6-mechanism,
S7, S8) and the operator authorised their browser execution; the two enterprise-ledger-dependent
clauses — **S2's enterprise-anchored comparison
(`plans/post321-real-browser-acceptance.md:38`) and S5b's approved-ledger SQL turn (`:41`) — remain
UNCLOSED and were not waived**, and **A3b remains BLOCKED and UNPASSED** because no
operator-approved enterprise ledger exists (`plans/post321-real-sql.md:50`, status `[ ] blocked on
real source and access`). The data-independent half of S5 (`:41`) — the the tracked change live MCP / `pg_es`
reconciliation and the Jev round-trip — is discharged as *prerequisite* evidence by
`docs/validation/2026-10-02-pre330-live-evidence.md` (that pull request), not as satisfaction of the enterprise
clause on the same line. Any result a future run produces must be recorded under the amendment's
labelling conditions, **not** as the tracked change acceptance.

Two prerequisites attach to that permission and are not optional. First, the full default
`pytest -q` regression on the T1/T2 candidate has **run**: `1 failed, 5200 passed, 117
skipped, 408 deselected, 8 warnings in 868.85s`, exit 1, and the single failure was an
**environment gap, not a regression** — the declared optional `ocr` extra
(`pyproject.toml:24`, `pytesseract` 0.3.13 in `uv.lock`) was absent from that worktree's venv.
After `uv sync --frozen --extra ocr` the failing case passes and the suite is green at **5201
passed, 0 failed, 117 skipped, 408 deselected**. **That 5201 is a reconciled figure across the first
run plus targeted repairs and reruns, not a second full-suite run** (§6.1), and the 408 deselected
opt-in legs — every B2 and `sql_live` case among them — did not execute in it. The verdict above is
unchanged by this: a discharged recheck is not a change of scope. Second, the T1/T2 change adds two
parameters to a **shared tool signature**, and the default-suite result was the check that discharges
the other host suites' exposure to that signature; that check is now made and is green (§6).

## 2. Accepted scope

The amendment proposed in that pull request and ruled in force by the operator on 2026-10-02, quoted in
substance: the tracked change's B3 acceptance may be recorded as **partially evidenced on development data** when
the database source is the committed read-only fixture ledger under
`tests/retrieve/fixtures/ledger/`, applied to a disposable local Postgres through the read-only
role, **provided** the record

1. labels the source a development substitute and cites the fixture's own declaration that it is
   not a business source;
2. states the independently checkable counts (6 seeded, 5 visible through the view, 4 rows
   expected in the live window query) and the tested revisions;
3. marks every browser SQL turn and every database-row evidence item as synthetic-source; and
4. records S2's enterprise-anchored comparison and S5b's approved-ledger SQL turn as
   **UNPROVED — blocked on A3b**, naming the missing approved ledger as the blocker.

**Non-waiver list, unchanged by the ruling.** A3b stays blocked; a `real_db_not_wired` refusal or
a synthetic-only result does not close the business-source leg; the B3 header bans on scripted
models, Fake ports, fixture MCP servers, mocked routes, API-seeded conversation turns, and skipped
live legs stand unchanged (`plans/post321-real-browser-acceptance.md:33`); no synthetic or public
row becomes a business source; the tracked change is **not** marked accepted or closed by a development run; and
A3a's earlier mechanism demonstration still "cannot close the original company-source leg" (`:35`).
The amendment is bounded to **browser acceptance mechanics only** and grants no permission to
relabel, restate, or report any development result as enterprise evidence. The same ruling records
T8/T9 **out of scope with no blocker**: no zero-baseline B3 gold case is selected, T9 is not
started, and the `"percent": null` conflict is an open backlog item.

## 3. The candidate actually tested

**Final candidate: `2fe787f`.** It is the only revision on which any evidence in this record is
either directly measured or explicitly argued to transfer. `main` was **not** merged and was not
modified.

| # | Merge | PR | Head | Note |
| --- | --- | --- | --- | --- |
| 0 | base | — | `ecd3ab4` | `origin/main` as of this work |
| 1 | `75b22b0` | the tracked change | `a52185f` | T6 development-only scope record, T8 disposition |
| 2 | `1aa1c08` | the tracked change | `25ed6ac` | T7 #276 reconciliation, live MCP/Jev reruns |
| 3 | `4efa787` | the tracked change | `b263123` | T1–T2 model-visible SQL date window, one-hop live proof |
| 4 | `320ea9f` | the tracked change | `a5b1f7a` | T3–T5 C1 reproducibility, baseline adopted, VM gold |
| 5 | `aa41459` | the tracked change | `22070ac` | gold-gap resolution: OCR sidecar + manifest correction |
| 6 | `2fe787f` | the tracked change | `26ff0d6` | B2 live isolation re-verification record |

`git diff --shortstat ecd3ab4 2fe787f` → **`20 files changed, 2786 insertions(+), 64 deletions(-)`**,
merged locally with zero conflicts. `320ea9f` (the the tracked change through the tracked change assembly) is a direct ancestor of
`2fe787f`, so every the tracked change and the tracked change measurement binds to a revision that is inside this candidate.

**Correction to the prior text (the tracked change, correction 2).** The earlier draft of this record named
a "composite candidate" built from the branch heads `b263123` and `a5b1f7a` and claimed a 15-path
combined tree. That was never an assembled or tested revision. `git merge-base --is-ancestor` returns
**1 in both directions** between `b263123` and `a5b1f7a` — they are independent branches, so the
union of their results was never a single identified revision and no test run exercised both. The
real assembly is the table above; the 15-path figure is the `ecd3ab4 → 320ea9f` diff
(`15 files changed, 1769 insertions(+), 60 deletions(-)`), which is a genuine intermediate, and the
candidate's own figure is 20 paths. Only the assembly above is claimed as tested.

### 3.1 The seven corrections this revision makes

1. **the tracked change was already delivered, through the tracked change — not "still unmerged".** the tracked change is **MERGED** at
   `ecd3ab4` (2026-09-02T06:29:19Z, merge commit `ecd3ab44ac1711338a9fb214cc013bd0efccd321`). Its
   tree contains `src/fin/host/evidence_contract.py` (61,334 bytes, present in this worktree), the
   evidence specification, and **98** conformance files under `data/conformance/evidence/` — all
   counted in this worktree. What remains open is **that pull request** (`issue334-evidence-contracts`, still
   OPEN), a historical branch whose different, `v1/`-versioned corpus layout is **not** a reason to
   restore it. The unmerged *branch* and the delivered *functionality* are different facts and are
   now recorded as such (§5, §7 item 10). **that pull request is not merged and must not be merged merely to
   close its tracker status.**
2. **The composite candidate did not exist** — corrected to `320ea9f`, now final `2fe787f` (§3).
3. **Test attribution.** The pipeline identity test in `b263123` is byte-identical to `main`'s older
   test (`git diff --quiet ecd3ab4 b263123 -- tests/host/test_g4_pipeline_builder_identity_contract.py`
   → exit **0**), while `a5b1f7a` changes it (same predicate vs `a5b1f7a` → exit **1**). Therefore
   **the tracked change's new assertion did not run in the tracked change's default regression.** The default regression's
   coverage of that row is attributed to `b263123`, and the tracked change's focused result is attributed to
   the tracked change (§3, row 9; §6). Converse, also recorded: the tracked change's focused result did **not** exercise the SQL
   path, and the tracked change's default regression did **not** contain the tracked change's new test. Neither result is
   relabelled as the other's.
4. **Not all ten tasks were implemented.** T8 is an **exclusion** (no zero-baseline gold case
   selected) and T9 explicitly **remains not started** (its condition is out of scope). T5 is a
   **completed measurement record**, not a declaration that every gold expectation passed. Task
   disposition is kept separate from acceptance success throughout (§4, §6.2, §10).
5. **The default-suite total is reconciled, not a second full run** (§6.1). 5201 = the initial 5200
   passes + the repaired optional-OCR failure + targeted reruns. The real 117 skipped and 408
   deselected counts are preserved. It is **not** presented as one clean full run.
6. **B1 and B2 are not conflated** (§5). B1's current fresh-interpreter import-isolation assertion
   and its positive control were exercised in the current-version runs, including the 39 passing
   OCR-sensitive tests after installing the declared extra; an older measurement's revision being
   stale does not invalidate a newly passing current assertion, and **no redundant B1 rerun is
   required**. B2 was previously deselected by its `mcp` marker — that gap is now closed on the
   current candidate by **the tracked change** (§6, §6.3).
7. **The SQL live test uses a scripted model** (§4 S5b row, §7 item 8). It proves argument transport
   through the real adapter to the real producer and independently expected rows — **not**
   real-model date extraction or clarification. The latter belongs to the real browser run and is
   **not** proven here.

## 4. Changed-path-to-evidence map for `2fe787f`

The four that pull request–the tracked change branches each fast-forward from base `ecd3ab4` with **zero path overlap**
between them, and the tracked change/the tracked change add five more paths on top. **20 changed paths in total.** Only one is
product code. Rows 16–20 are the the tracked change and the tracked change paths, measured on `320ea9f`/`2fe787f`.

| # | Changed path | PR | What evidence now covers it | Recheck required by this change? | What was run |
| --- | --- | --- | --- | --- | --- |
| 1 | `src/fin/host/graph.py` (+31) | the tracked change | T1 contract + T2 end-to-end. Two model-visible args `sql_window_start` / `sql_window_end` added to `_make_retrieve_tool`, each with an `Annotated` description; both forwarded to the existing `resolve_sql_window` gate, which stays the single pre-transport owner. | **Yes — shared tool signature.** | `-m soft_track` 27 passed / 75 deselected, exit 0; focused 75 passed / 27 deselected, exit 0; live `-m sql_live` 1 passed, exit 0; pyright 3 errors, all pre-existing, 0 added. **Default-suite regression run** on the T1/T2 candidate `b263123`: `1 failed, 5200 passed, 117 skipped, 408 deselected` exit 1, the one failure an absent-optional-extra environment gap (§6), resolved by `uv sync --frozen --extra ocr`; default-suite result **green at 5201 passed, 0 failed, 117 skipped, 408 deselected**. |
| 2 | `tests/host/test_assigned_mode_reaches_port.py` (+215/−1) | the tracked change | T1 refusal taxonomy and the host seam (missing / incomplete / malformed / non-ISO / reversed). | No — it is the change. | Row 1 and row 2 of the PR's verification table. |
| 3 | `tests/retrieve/test_live_sql_window_user_path.py` (new, +169) | the tracked change | T2 one-hop live proof: model tool args → real `MCPRetrieveAdapter` → real producer → `result_count=4`, ids `C-2315, C-2302, C-2291, C-2024Q1`, end boundary `2026-04-30` proving inclusive `BETWEEN`. Fail-closed guards observed by breaking each precondition. | No — it is the change. | `-m sql_live` 1 passed, exit 0; `--collect-only` proves marker routing. |
| 4 | `docs/validation/2026-10-02-sql-date-input-contract.md` (+116) | the tracked change | T1 contract decision and its explicit non-claims. | No. | — |
| 5 | `scripts/g4_pipeline/Dockerfile` (+32/−6) | the tracked change | T4 root-cause repair. `PYTHONDONTWRITEBYTECODE=1` and apt-layer purges of the 8 measured generated files. | **Yes — the built image.** | Two independent clean builds, **identical identity `sha256:b1b5270c…`, 11/11 layers**; `pin.py self-check` exit 0. |
| 6 | `scripts/g4_pipeline/image-pin.json` (+14/−11) | the tracked change | C1 adoption. `expected_image_id` = `b1b5270c…`, `status: BASELINE_ADOPTED`, `8e10185f…` retained in `blocking_reason` as history. | **Yes — the identity gate.** | Gate proven still non-vacuous: match exit 0, non-match exit 75, self-check exit 0; a new test asserts the refusal survives re-baselining. |
| 7 | `scripts/g4_pipeline/build.sh` (+4/−4) | the tracked change | Comment-only refresh of the note pointing at the adopted baseline; **the gate itself is unchanged**. | No — no logic change. | Verified by diff: 4 comment lines, 0 code lines. |
| 8 | `scripts/g4_pipeline/PIN_MANIFEST.md` (+25/−14) | the tracked change | Reconciles the manifest's image-identity section with the adopted baseline. | No — prose only. | — |
| 9 | `tests/host/test_g4_pipeline_builder_identity_contract.py` (+64/−9) | the tracked change | The adopted baseline is the declared identity; the gate still refuses a non-declared image. | No — it is the change. | **the tracked change's own focused result**, not the tracked change's default regression: `b263123` carries this file **byte-identical to main** (§3.1 correction 3). Re-verified on `2fe787f` in §6.2. |
| 10 | `tests/host/test_g4_pipeline_pin_contract.py` (+25/−15) | the tracked change | Pin-file contract against the repaired Dockerfile. | No — it is the change. | `pin.py self-check` exit 0. |
| 11 | `docs/validation/2026-10-02-c1-pin-policy.md` (+286) | the tracked change | T3 policy (Rules 1 and 2) and the T4 two-build proof. | No. | — |
| 12 | `docs/validation/2026-10-02-c1-layer-diffids.txt` (+128) | the tracked change | Per-layer diff ids and the per-file regeneration recipe behind the root cause. | No. | — |
| 13 | `docs/validation/2026-10-02-c1-vm-gold.md` (+368) | the tracked change | T5 VM measurement of all four committed PDFs, **with expectation mismatches recorded, not smoothed**. | No. | Four `run_measured.sh` runs, all exit 0, one JSON line each. |
| 14 | `docs/validation/2026-10-02-pre330-data-scope.md` (+193) | the tracked change | T6 proposal + operator ruling; the per-step S1–S8 provability table; A3b restated. | No. | — |
| 15 | `docs/validation/2026-10-02-pre330-live-evidence.md` (+99) | the tracked change | T7 case-by-case the tracked change reconciliation and the A/B/B2/C/D reruns. | No. | See §6. |
| 16 | `docs/validation/2026-10-02-b2-live-isolation.md` (+267) | the tracked change | B2 live-isolation re-verification on the current candidate: three consecutive isolated runs, exit 0 each. Documentation only. | No — documentation only. | Three runs, `6 passed` each, 0 skipped, 0 deselected (§6.3). Reused, not re-run. |
| 17 | `scripts/g4_pipeline/driver.py` (+36) | the tracked change | **Product-adjacent.** Persists full per-page OCR text as a `page_NNNN.ocr.txt` sidecar with `ocr_text_file` / `ocr_text_sha256`; the 160-char JSON-line `ocr_text_sample` cap is unchanged. | **Yes — harness code and a gold manifest changed.** | Failing-first `3 failed, 3 passed` → `6 passed`; re-verified on `2fe787f` in §6.2. |
| 18 | `data/corpus/g4_attachment_gold/manifest.json` (+10/−4) | the tracked change | Scanned p2 floor **raised** `["text"]` → `["text","table"]` from the AAPL p9 source; two `known_gaps` notes appended. | **Yes — an authored gold floor changed.** | Every existing test that reads this manifest re-run on `2fe787f` (§6.2). |
| 19 | `tests/host/test_g4_pipeline_ocr_evidence_contract.py` (+228, new) | the tracked change | The sidecar contract, loaded by path, pure functions against local doubles. | No — it is the change. | 6 passed, and inside the §6.2 recheck. |
| 20 | `docs/validation/2026-10-02-c1-gold-gap-resolution.md` (+480) | the tracked change | The four gold gaps: one product defect fixed, three dispositioned from source. Documentation only. | No — documentation only. | See §6.4 and the per-case acceptance table in §10. |

All 20 paths are mapped. None is unmapped. Row 18 is the only **authored gold expectation** the
candidate changes, and it is raised, not lowered.

## 5. Per-B3-step readiness

Step line references are `plans/post321-real-browser-acceptance.md:37-44`; the step boundaries and
the enterprise anchoring are those of the B3 acceptance block, not of this record.

| Step | Line | Provable now? | On what evidence | What remains unclosed |
| --- | --- | --- | --- | --- |
| S1 — real browser session, thread create/open, composer upload, send from snapshot-derived controls | `:37` | **Yes** | Data-independent. Operator amendment authorises it (that pull request). | Requires a real `playwright-cli` session at execution time; not exercised here. |
| S2 — compare visible answers, citation locators, SQL result shape and visual claim scope against manifests **and the settled A3b enterprise-ledger contract** | `:38` | **Partly** | The document/attachment half is provable: citation opens the cited source and the quote matches the expected span from committed manifests. | **The enterprise-anchored comparison is UNCLOSED** — the A3b anchor does not exist. Not waived by the amendment. |
| S3 — first visual turn refreshes evidence via the real provider into real Postgres + real object store; later turn reuses without re-upload, provider call, or version increment | `:39` | **Yes** | Data-independent of the ledger; fixture gold is manifest-pinned. | — |
| S4 — reopen after a real server restart; durable history, attachment state, citation navigation, visual reuse; race and empty/invalid/overflow/concurrent/exception exits | `:40` | **Yes** | Data-independent. | — |
| S5 — reconcile the tracked change live MCP / `pg_es` and run the Jev round-trip | `:41` | **Data-independent half discharged as prerequisite** | that pull request: MCP 11 passed exit 0 (10 genuinely live), Jev 8 + B2 1 = **9 of 9**, live `pg_es` 12 passed exit 0, checkpoint pair 23 passed exit 0. | This is a *baseline* proof, explicitly not automatically accepted for T10 after a contract change; ROOT's §5/§6 binding applies. |
| S5b — the final business-source browser SQL turn executes against the approved ledger delivered by A3b | `:41` | **No** | — | **UNCLOSED.** No operator-approved ledger exists. A `real_db_not_wired` refusal or a synthetic-only result explicitly does not close it. |
| S6 — Session E evidence-schema contract through a real browser answer and citation open/reopen | `:42` | **Yes, mechanism only** | Proves the schema and access mechanism over development row values: original bounded snapshot and `queried_at` without implicit re-query, explicit refresh creates new evidence, historic snapshot rechecks current access. | Row *values* are development values. Tenant isolation and department authorisation are **not** proven — `owner_dept` is a free-text label, not an enforced scope. |
| S7 — present rule in the reviewed expense-policy document yields a grounded cited answer; absent rule yields explicit insufficient-evidence, not `off_topic` or invention | `:43` | **Yes** | `data/corpus/dataset/t1/expense-policy-001.md` supplies both surfaces; its monetary amounts also exercise the clause that an amount alone must not select the financial/XBRL path. | — |
| S8 — record commands, exits, session name, revisions, gold hashes, expected vs observed, deltas, durable counts, and **all** failures | `:44` | **Yes** | Data-independent recording discipline. | — |

**Coverage: 6 of 8 steps are provable and authorised (S1, S3, S4, S6-mechanism, S7, S8). S2 is
partly provable with its enterprise half closed-open. S5's data-independent half is discharged as
prerequisite evidence. S5b stays closed.**

## 6. Currency check of prior evidence

Done by reasoning about what changed, using read-only revision diffs. **No completed work was
redone.** The candidate's entire product-code footprint is one file (§3 row 1), so the question for
each prior record is whether that one file — or the intervening `ecd3ab4` history — invalidates its
claim.

**B1 — `docs/validation/2026-09-29-issue-323-b1-ocr-import-isolation.md`.** B1's claim is a
collection/import contract: default `pytest` must not import `pytesseract`. Its deliverable touched
`tests/corpus/test_ocr.py` only, and the candidate changes **neither** that file nor any import
boundary. B1 was measured at base `670f65f`, and `tests/corpus/test_ocr.py` has since changed on
`main` (+94/−9), so B1's *original tree* does not transfer by identity. **That is a statement about
the old measurement's revision, not about the contract's current validity.** The contract itself is
**currently exercised and passing**: B1's contract test
`test_default_pytest_does_not_import_pytesseract` (`tests/corpus/test_ocr.py:583`) carries no opt-in
marker, so it is collected and run in the default regression, and `tests/corpus/test_ocr.py` is
green. The positive control was exercised too — **39 passing OCR-sensitive tests** after installing
the declared extra (§6.1). **Status: current.** An older measurement's revision being stale does not
invalidate a newly passing current assertion, and **no redundant B1 rerun is required**; requiring
one would only re-measure a passing current contract to preserve an old assertion tree.

The one nuance that must not be lost: the default regression's single failure was **not** a B1
violation. It came from `tests/host/test_workspace_http_attachment_lane.py`, which by design builds
a real `TesseractEngine` and carries an autouse fixture
(`tests/host/test_workspace_http_attachment_lane.py:545`) precisely to pop the extra from
`sys.modules` so this sibling contract test stays valid. B1 governs `fin.corpus.ocr` not pulling the
extra on module import, not a sibling file instantiating an engine.

**B2 — `docs/validation/2026-09-29-issue-323-b2-live-gate-isolation.md`, rerun
`docs/validation/2026-09-30-issue-323-b2-verification-isolated-rerun.md` (`a2e5f65`).** B2's claim
is per-run resource isolation for the live attachment graph gate; its deliverable touched
`tests/host/test_live_attachment_graph_gate.py` only, which the candidate does not touch. The same
caveat as B1 applies and is stronger: that test file changed on `main` (+344/−128) after B2's
revision, and `tests/retrieve/conftest.py` changed too (+40/−2) — B2's isolation argument is
partly a conftest-level argument. Its deliverable is `pytestmark = pytest.mark.mcp`
(`tests/host/test_live_attachment_graph_gate.py:194`), so the default regression **does not bear
on B2 at all**: all six of its cases sit inside the 408 deselected and never executed.

**Correction (the tracked change, correction 6): that deselection gap is now CLOSED, by the tracked change — on the
current candidate, and without re-running anything here.** `docs/validation/2026-10-02-b2-live-isolation.md`
records three consecutive runs on candidate `320ea9f` (a direct ancestor of `2fe787f`), each
**`6 passed / 0 failed / 0 skipped / 0 deselected`**, exit **0** (`6 passed in 38.67s`,
`6 passed in 33.71s`, `6 passed in 33.17s`), each against its own disposable checkpoint database
(`i346_run1/2/3`), its own S3 bucket (`i346-run1/2/3`) and 12 OS-assigned ephemeral ports, with
`SELECT 1` DSN liveness re-proved immediately before each run (0.0258 s / 0.0217 s / 0.0209 s) so no
run can be a silent in-memory pass, zero residue after each (0 schemas, 0 relations, `KeyCount = 0`,
no surviving children), and all six `fin-pre330-*` containers untouched.

**Why the tracked change's evidence survives the combination and needs no re-run here** (this reasoning is the
substance of the reuse, not a bare "it should be fine"): the gate reads only
`data/corpus/manifest.json` and `g4-image-chart-001.png`. It **never** reads
`data/corpus/g4_attachment_gold/manifest.json` — the single corpus file the tracked change edited — and it does
not load the G4 driver at all. Of the candidate's 20 changed paths, the only ones that could reach
this module are the harness/driver and gold-manifest paths (rows 17–18), and both are outside its
read set. The result therefore binds to `2fe787f` on the read-set argument, not on a hopeful
assumption. This reuse was checked directly rather than inherited. **B2 is current on the
candidate**; its isolation contract is no longer a stale assumption.

**the tracked change — delivered through the tracked change, not outstanding.** (Corrected; the tracked change correction 1.) the tracked change's
functionality **is** on `main`: **that pull request is MERGED** at `ecd3ab4`, and its tree carries
`src/fin/host/evidence_contract.py` (61,334 bytes), the evidence specification, and **98**
conformance files under `data/conformance/evidence/` — all three counted in this worktree. The
numeric-only `percent` check this record relies on is at `src/fin/host/evidence_contract.py:1272`.
What is still open is **that pull request** (`issue334-evidence-contracts`), a historical branch against an
old SQL base carrying a `v1/`-versioned corpus layout instead of main's un-versioned one. **That
is a branch-status fact, not missing functionality**, and its different corpus layout is not a
reason to restore it. `main`'s conformance corpus is present and the candidate changes none of it
(`git diff --stat ecd3ab4 2fe787f -- data/ src/fin/host/evidence_contract.py` is empty).
**that pull request is not merged by this record and must not be merged to make its tracker status look
complete.** Status: **delivered via the tracked change; unaffected by the candidate.**

**the tracked change — live MCP / `pg_es`.** Reconciled case-by-case in that pull request and found **genuinely stale**:
`08cedb1` *is* an ancestor of `e8165a7`, but one intervening commit changes 13 files (+1263/−48) in
`src/`, including `core/retrieval/wire-filters.ts` and `core/retrieval/retrieve.ts` — the exact
retrieval and MCP-tool paths under test. Reruns: A 11 passed exit 0, B 8 passed / 1 deselected,
B2 1 passed (closing the dual-mark gap, so Jev is 9/9), C 12 passed exit 0, D 23 passed exit 0
after an environment-only fix. **Status: current on producer `e8165a7` with a self-built,
hash-verified `dist/`.** Two precision caveats that must travel with it: (i) the 11-test MCP count
overstates genuine live coverage by one — `tests/retrieve/test_live_mcp_t5.py:45` is fake-backed, so
the true live MCP count is **10 of 11**; (ii) the T7 completion clause states this parallel baseline
is **not** automatically accepted for T10 after a contract change. the tracked change concerns the *producer*
side and B3's data-independent MCP/Jev leg; the candidate's change is on the *fin* host side and
does not touch the producer, the MCP client, or the `wire-filters` translation, so the binding holds
on the producer-revision argument rather than requiring a rerun. **What would invalidate it:** any
producer change to `wire-filters.ts` or the retrieve tool, or any fin change to the MCP adapter's
filter translation.

**Q1 — figure-only crop with honest whole-page fallback**
(`plans/post321-real-browser-acceptance.md:25-30`, ruling 2026-09-30; canonical scope in the O1
resolution of `docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md`). Q1 is a *decision*
ruling, not a measurement, so it has no revision to drift. The candidate changes no crop code
(`src/fin/host/attachment_visual_read.py`: zero changes). **Status: current and unaffected.** One
interaction worth carrying into B3: T5's VM measurement found `g4-mixed-chart-001.pdf` p2 produced
**no table region at all** (measured `table 0`, reproduced in the tracked change and re-read from its run
artifacts). That is the exact shape Q1's honest whole-page fallback covers — so Q1's fallback is
doing real work in the case B3 will exercise. It remains an **unmet floor**, not a pass; §10 scopes
it and states its effect on the verdict.

## 7. Targeted rechecks

Required by the changed paths, run and complete:

| Recheck | Justification | Result |
| --- | --- | --- |
| Focused `tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` | the tracked change changes the retrieve tool signature | 75 passed / 27 deselected, exit 0 |
| `-m soft_track` on the same two files | the host file is `soft_track`; the bare run deselects it, so this is the row that actually proves the host seam | 27 passed / 75 deselected, exit 0 |
| `-m sql_live tests/retrieve/test_live_sql_window_user_path.py` | T2 one-hop live producer proof | 1 passed, exit 0 |
| `pyright` on the three touched Python paths | signature change is type-visible | 3 errors, **all 3 pre-existing**, 0 added |
| Two independent clean image builds | the tracked change changes the built image | identical `sha256:b1b5270c…`, 11/11 layers |
| Identity-gate non-vacuity (match / non-match / self-check) | the tracked change re-baselines the pin | exit 0 / exit 75 / exit 0 |
| Four `run_measured.sh` PDF gold runs | T5, after T4 | all exit 0, one JSON line each |
| T7 reruns A / B / B2 / C / D | the tracked change evidence stale on the producer revision | 11 / 8+1 / 1 / 12 / 23 passed, exit 0 |
| Full default `pytest -q` regression on `b263123` | discharges the other host suites' exposure to the new shared tool signature | **5201 passed, 0 failed**, 117 skipped, 408 deselected — reconciled, not a second full run (§6.1) |

### 7.1 The default-suite total is reconciled across runs, not a second full run

**This is not one clean full-suite run and is not reported as one** (the tracked change, correction 5). The
first run of `uv run --no-sync pytest -q` on `b263123` reported `1 failed, 5200 passed, 117 skipped,
408 deselected, 8 warnings in 868.85s (0:14:28)`, exit 1. The single failure was
`tests/host/test_workspace_http_attachment_lane.py::test_a_pass_leaves_its_page_boxes_in_the_bound_sink`,
raising `ModuleNotFoundError: No module named 'pytesseract'` from `src/fin/corpus/ocr.py:186` — the
lazy in-`__init__` import, whose captured error is what the engine re-raises as
`OcrRequiredUnavailableError` (`src/fin/corpus/ocr.py:214`). This was an **environment gap, not a
regression**: `pytesseract>=0.3.10` is the declared optional `ocr` extra (`pyproject.toml:24`) and is
in `uv.lock`, but was not installed in that worktree's venv. The fix was `uv sync --frozen --extra
ocr` — a *declared* extra under `--frozen`, leaving `pyproject.toml` and `uv.lock` unchanged.

Under the operator's retest-only-failures rule, only the failing case was re-run (`1 passed in 2.34s`,
exit 0); because installing a package can only affect tests that import it, the two pytesseract-
sensitive suites were re-run as well (`tests/host/test_workspace_http_attachment_lane.py
tests/corpus/test_ocr.py` → **`39 passed in 40.34s`**, exit 0).

**5201 = 5200 (first run's passes) + 1 (the repaired case), with the 39-case OCR rerun confirming the
repair's blast radius.** **No second full run was made.** The **117 skipped** and **408 deselected**
counts are the real counts of the first run and are preserved unchanged; the 408 deselected opt-in
legs (including every B2 and `sql_live` case) did **not** run and are **not** counted as successes
anywhere in this record.

### 7.2 Rechecks run on the final candidate `2fe787f`

Justified because the tracked change changed product-adjacent harness code (`scripts/g4_pipeline/driver.py`) and an
authored gold manifest (`data/corpus/g4_attachment_gold/manifest.json`). Environment:
`uv sync --frozen --extra ocr --extra object-store` (exit 0, no lockfile change), `uv run --no-sync`.

| # | Command | Exit | Observed |
| --- | --- | --- | --- |
| 1 | `pytest -q` over the 7 G4 contract files (`ocr_evidence`, `pin`, `builder_identity`, `declaration_crosscheck`, `artifact_hash`, `gap`, `model_fetch_binding`) | **0** | **150 passed** in 3.85s, 0 failed, 0 skipped |
| 2 | `pytest -q` over every existing test that reads the manifest the tracked change edited: `tests/corpus/test_page_region_core.py`, `test_ocr_tesseract_gold.py`, `test_ocr_region.py`, `tests/host/test_attachment_prepare_image.py`, `test_attachment_budgets.py`, `test_g4_vision_probe_classification.py` | **0** | **361 passed** in 19.33s, 0 failed, 0 skipped |
| 3 | `python3 scripts/g4_pipeline/pin.py self-check --pipeline-dir scripts/g4_pipeline` | **0** | all pin blocks agree; declared expected id `sha256:b1b5270c…`; no digest contradiction |
| 4 | Identity gate, match / non-match (`pin.py compare-image-id`) | **0** / **75** | match → `[pin] image id matches the declaration`; non-match → `[pin] FATAL: image identity drift …` |
| 5 | SQL seam, bare: `pytest -q tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` | **0** | **75 passed, 27 deselected** in 0.23s |
| 6 | SQL seam, marked: `pytest -q -m soft_track` on the same two files | **0** | **27 passed, 75 deselected** in 1.61s |

Rows 1–2 are the recheck the tracked change requires; rows 5–6 exist to show the tracked change disturbed nothing on the SQL
seam. `tests/host/test_assigned_mode_reaches_port.py` is `soft_track`-marked
(`:62`) and `tests/retrieve/test_sql_wire.py` is unmarked, which is why the two invocations report
**complementary** counts — the bare run deselects the host file's 27 cases and the marked run
deselects the retrieve file's 75. Reporting only one of them would understate host-seam coverage;
neither is a live SQL result (§4 S5b).

**The identity gate's non-vacuity is proven by the exit code, not asserted:** the non-match
observation deliberately carried a zeroed `sha256:` digest and was refused with **75**, so a
green self-check is not a gate that passes everything.

### 7.3 What was deliberately NOT re-run, and why that is evidence, not deferral

| Not re-run | Reason it is not needed |
| --- | --- |
| B2 live gate (3 runs) | the tracked change measured it on `320ea9f`, a direct ancestor of `2fe787f`; the gate's read set is `data/corpus/manifest.json` + `g4-image-chart-001.png` and never the manifest the tracked change changed, and it loads no G4 driver. Read-set argument, checked in §5. |
| G4 image rebuild / `run_measured.sh` | `scripts/g4_pipeline/driver.py` is **bind-mounted** (`run_measured.sh:327-333`, `--volume "${WORKTREE}:/worktree:ro"`), not baked; the driver change is a worktree change, so the adopted baseline `sha256:b1b5270c…` is untouched by construction and the two affected cases were already rerun through the real pinned harness by the tracked change. |
| Full default `pytest -q` | The reconciled 5201 (§6.1) plus the tracked change's `1 failed, 4162 passed, 117 skipped, 301 deselected` over `tests/host tests/corpus` already cover the unit surface. A repeat would add no evidence about the only two changed surfaces, which rows 1–2 targeted directly. |
| `-m sql_live` / `-m mcp` / `-m eval` live legs | Expensive, live, and untouched by the tracked change's changed paths; their recorded results are reused on the producer-revision argument in §5. |

**What would invalidate the reuse above:** any change to the B2 gate's read set or fixtures, any
baking of the driver into the image, a producer change to `wire-filters.ts` or the retrieve tool, or
a fin change to the MCP adapter's filter translation.

### 7.4 Gold outcomes carried by the tracked change, bound to this candidate

the tracked change's four gaps, at the level this record needs them — each is an **expectation outcome**, and the
distinction between "met" and "dispositioned" is preserved verbatim. Per-case verdicts are in §10.

- **Gap 4 (product defect) — FIXED.** The 160-char `ocr_text_sample` cap
  (`scripts/g4_pipeline/driver.py:536`) put a committed gold quote at offset 479 out of reach. The fix
  persists full per-page OCR text via a sidecar carrying `ocr_text_file` / `ocr_text_sha256` and
  keeps the JSON-line sample capped. Failing-first: **`3 failed, 3 passed` → `6 passed`**. **Verified
  again in this worktree** against the tracked change's own run artifacts: all three scanned quotes are present
  **verbatim** in the sidecars at offsets **11 / 11 / 479**.
- **Gap 1 (mixed p2 table floor) — CLOSED 2026-10-03, met by measurement.** The fixture page
  was re-authored into the ruled table its source page is; the pinned stage now returns `table 1`
  for p2 against an **unchanged** floor. See §11.2 and
  `docs/validation/2026-10-03-c1-gold-residual.md`.
- **Gap 2 (mixed p3 figure OCR) — CLOSED 2026-10-03 for the behaviour, with one named residual.**
  The driver now applies the product's own `crop_is_legible` / `crop_png`; measured
  `figure_ocr_regions_read 1`, 425 chars, chart labels read. The chart *title* quote stays
  unreachable through the returned box; recorded in `known_gaps[10]`, owned by ADR 0089's stage.
- **Gap 3a (scanned text regions) — CLOSED 2026-10-03 as a recorded decision with measurement.**
  **ADR 0096** accepts that a text region is a count, not a box, and cites where the product reads
  page text instead; the floor is carried there and verified (0 native chars, OCR 3/3, 3/3 quotes
  verbatim). Nothing deleted, no floor lowered.
- **Gap 3b (scanned p2 floor) — raised and met by measurement**, not merely asserted.


**What the shared tool signature implies for other host suites.** `src/fin/host/graph.py` is the
only product file changed, and it changes the *public tool signature* of `retrieve` — it gains two
optional model-visible arguments. Every test that builds the graph, reads the retrieve tool's
`args_schema`, or asserts on the tool's parameter set is downstream of that signature. At least
these files reference the retrieve tool or its schema and are **not** covered by the focused runs
above: `tests/retrieve/test_port_signature.py`, `tests/retrieve/test_compile_graph_adapter_swap.py`,
`tests/retrieve/test_mcp_smoke.py`, `tests/retrieve/test_fake_port.py`, `tests/retrieve/test_bm25.py`,
`tests/host/test_graph_attachment_retrieve.py`, `tests/host/test_named_failure_retrieve.py`,
`tests/host/test_input_validation_exit.py`, `tests/host/test_issuer_fanout_in_thread.py`,
`tests/host/test_read_source_tool.py`, `tests/host/test_evidence_admission.py`,
`tests/host/test_memory_gold_set.py`. That list is the concrete reason the default regression was a
required recheck and not a nicety: it is the only run that discharges those files against the new
signature, and it has now discharged them. The argument is additive and both parameters default to
`None`, so a silent behavioural break would most likely appear as a schema/signature assertion
rather than as a value error.

**Not rechecked, deliberately:** the pipeline image is not rebuilt for a B3 readiness decision
beyond what the tracked change already proved and what the tracked change's bind-mount argument makes unnecessary (§6.3);
`run_measured.sh` is not re-run (its driver input is bind-mounted and its two affected cases were
rerun by the tracked change); the B2 live gate is not re-run (the tracked change measured it on this candidate's ancestor, §5);
and the T7 live suites are not re-run against the fin host change, for the producer-revision reason
stated in §5. The rechecks that **were** justified by the candidate's changed paths are in §6.2,
with their exits and real counts.

## 8. Remaining prerequisites and blockers

Named precisely. None of these is closed by this verdict.

1. **A3b is BLOCKED and UNPASSED.** No operator-approved enterprise ledger exists. Its own status
   line reads `[ ] blocked on real source and access` (`plans/post321-real-sql.md:50`), and the
   completion clause requires it be left explicitly blocked absent an approved ledger (`:49`).
   **This is the blocker behind S2 and S5b.**
2. **S2's enterprise-anchored comparison (`:38`) is UNCLOSED.** Its second anchor — "the settled A3b
   enterprise-ledger contract" — does not exist. Not waivable by the development amendment.
3. **S5b's approved-ledger browser SQL turn (`:41`) is UNCLOSED.** A `real_db_not_wired` refusal or
   a synthetic-only result explicitly does not close it.
4. **C2 is DEFERRED and UNPASSED.** No dedicated 4 GB device measurement exists. The T5 runs used
   the harness's own default `G4_CEILING_*` values; those are a harness default, not a host
   property, and may not be used to close C2 or assert a 4 GB cap.
5. **Mixed-chart p2's `table` region floor — CLOSED 2026-10-03, by measurement.** It was
   `table 0` against an authored floor of `["text","table"]`; the fixture page has since been
   re-authored into the ruled table its source page is and the pinned stage returns **`table 1`**
   for it, floor unchanged. **What remains open from this item is narrower and is not this
   floor:** the chart *title* quote on p3 is unreachable through the layout stage's returned box
   (inset from the declared raster), owned by the Heron stage ADR 0089 pins. Recorded in
   `known_gaps[10]`. See `docs/validation/2026-10-03-c1-gold-residual.md`.
6. **`snapshot.debian.org` is a live third-party dependency for C1's repeatability.** 2 of 6
   clean-build attempts failed in the apt step. `apt-packages.lock` records the ~188 transitive
   packages but does not gate them. C1's byte-for-byte claim is unaffected; **repeatability** is
   conditional on the mirror.
7. **`percent: null` vs the frozen numeric-only §4.3 validator is an open backlog item.**
   `src/fin/host/tools/compare_facts.py` emits `null` at a zero baseline; the validator
   (`src/fin/host/evidence_contract.py`, and `specs/schemas/evidence/comparison.schema.json`, whose
   `percent` type is `["number","integer"]`) accepts numbers only. Not resolved, not weakened, no
   percentage fabricated. Non-blocking for B3 only because T8 ruled the zero-baseline case out of
   scope.
8. **A live LLM's date-argument behaviour is unproven.** T2's proof is the *arg plumbing* through
   the real transport to the real producer in one invocation, driven by a `ScriptedChatModel`
   double (`tests/retrieve/test_live_sql_window_user_path.py:43`, `:133`). It proves **argument
   transport and independently expected rows** — nothing about real-model date extraction or
   clarification. Rule 5's "no inferred window" guard is **instructional, not structural**: no code
   refuses an invented-but-bindable well-formed pair, and no test claims it does. **This is a
   per-item STILL UNMET acceptance item** (§11.1), not a caveat.
9. **Mark-expression gaps in the plan's own live commands remain open as a process finding.** The
   T7 commands each miss the dual-marked `test_live_jev_tier_reaches_wire.py`; B2 was added to
   close the Jev leg, but the underlying command wording in the plan was not corrected.
10. **that pull request (`issue334-evidence-contracts`) is still an open historical branch — but the tracked change's
    functionality is delivered and is not on this list as a gap.** the tracked change is MERGED at `ecd3ab4` and
    `main` carries `src/fin/host/evidence_contract.py` plus 98 conformance files. The open branch
    carries a superseded `v1/`-versioned corpus layout. **It is not merged by this record and must
    not be merged to close a tracker status.** Recorded so the branch is not mistaken for a
    functional gap, and so nobody restores a superseded layout on its account.
11. **T5's remaining measured divergences are recorded, not closed.** Native-text 1882 observed vs
    1860 expected (+22); mixed-chart per-page counts diverge (p1 460 vs 449, p2 1111 vs 1181,
    p3 337 vs 334). `2022_Q3_MSFT.pdf` has no page expectations in the manifest, so **none were
    invented** — the 80-page result stays a **prior-run comparator, not a committed gold pass**.

The default `pytest -q` regression is **not** in this list: it is discharged (§6, §8), and it is
T1/T2-candidate-scoped, which is recorded there rather than here.

## 9. What is explicitly NOT claimed

- **Not** enterprise acceptance. No synthetic, fixture, or public row is a business source. the tracked change is
  **not** accepted, closed, or marked done by this verdict or by any development run.
- **Not** dedicated-device evidence. C2 remains deferred and unpassed; no 4 GB device was measured.
- **Not** live success inferred from skips. The 117 default skips of the T1/T2 candidate run are
  not live proof, and no green exit code here rests on a skip. The T7 checkpoint DSN hazard is
  specifically recorded: the fixture silently drops an unreachable `FIN_CHECKPOINT_URL` and degrades
  to in-memory storage, so a DSN proof precedes trusting that pair.
- **Not** CI health. This repository has **no `.github` directory at all**, so health was neither
  observed nor inferred. Nothing in this record should be read as a CI statement.
- **Not** B3 execution. No browser leg was run. S1–S8 are readiness assessments, not results.
- **Not** a full-suite pass, only a default-suite one. The §6 run is claimed with its exact shape:
  5201 passed / 0 failed, 117 skipped, 408 deselected, on the T1/T2 candidate alone. The 408
  deselected opt-in legs did not run, and the run covered neither the G4 pipeline change nor any
  other candidate branch.
- **Not** a live LLM behavioural result, a complete C1, a second independent C2, or a repair of the
  T5 gold mismatches.

## 10. Evidence index

`✓` marks a citation re-read directly in this worktree during T10; unmarked rows are taken from a
session record and are cited, not re-verified, here.

| Claim | Evidence | Re-read |
| --- | --- | --- |
| B3 acceptance steps S1–S8 | `plans/post321-real-browser-acceptance.md:37-44` | ✓ |
| B3 bans scripted models / Fake ports / fixture MCP / mocked routes / API-seeded turns / skipped legs | `plans/post321-real-browser-acceptance.md:33` | ✓ |
| A3a on synthetic rows cannot close the company-source leg | `plans/post321-real-browser-acceptance.md:35` | ✓ |
| Q1 ruling — figure-only crop with honest whole-page fallback | `plans/post321-real-browser-acceptance.md:25-30` | ✓ |
| A3b requires an operator-approved ledger | `plans/post321-real-sql.md:48` | ✓ |
| A3b stays explicitly blocked absent an approved ledger | `plans/post321-real-sql.md:49` | ✓ |
| A3b status line — blocked on real source and access | `plans/post321-real-sql.md:50` | ✓ |
| `rows returned` cap is 20 | `specs/search-mode-business-sql-contract.md:475`; constant at `src/fin/retrieve/mcp_adapter.py:171` | ✓ |
| Permitted template `contracts_expiring` | `specs/search-mode-business-sql-contract.md:201` | ✓ |
| Named refusal token and its operator copy | `src/fin/host/stream_map.py:777` | ✓ |
| `resolve_sql_window` is the single pre-transport window gate | `src/fin/retrieve/port.py:241`; forwarded at `src/fin/host/graph.py:1184` | ✓ |
| Numeric-only `percent` check in the validator | `src/fin/host/evidence_contract.py:1272` | ✓ |
| `percent` schema type is `["number","integer"]` | `specs/schemas/evidence/comparison.schema.json:76` | ✓ |
| Zero-baseline `percent` producer behaviour and its locked test | `src/fin/host/tools/compare_facts.py:275`; `tests/host/test_compare_facts.py:261` | ✓ |
| Fixture declares itself a development fixture | `tests/retrieve/fixtures/ledger/001_schema.sql:1-7` | — |
| `owner_dept` is a free-text label, not enforced scope | `tests/retrieve/fixtures/ledger/001_schema.sql:45` | ✓ |
| Enforced view predicate `status = 'active'` | `tests/retrieve/fixtures/ledger/001_schema.sql:84` | ✓ |
| Seed counts checkable by hand: 4 / 6 / 5 | `tests/retrieve/fixtures/ledger/002_seed.sql:58-59` | ✓ |
| Seeded / visible counts and their enforcement | `tests/retrieve/test_ledger_readonly_boundary.py:84-85`, `:266` | ✓ |
| Live window query expects 4 rows, named ids | `tests/retrieve/test_live_search_modes_pg_es.py:491` | ✓ |
| One MCP "live" test is fake-backed | `tests/retrieve/test_live_mcp_t5.py:45` | ✓ |
| Dual-mark deselection hook | `tests/retrieve/conftest.py:39` | ✓ |
| Checkpoint-URL silent-drop hazard | `tests/conftest.py:91` | ✓ |
| Built-artifact assertion is `is_file()` only | `tests/retrieve/live_iknow_support.py:412` | ✓ |
| `boto3` optional dependency, extra `object-store` | `pyproject.toml:22-23` | ✓ |
| `pytesseract` optional dependency, extra `ocr`; pinned 0.3.13 in the lock | `pyproject.toml:24`; `uv.lock:886` | ✓ |
| `pytesseract` is a lazy in-`__init__` import, re-raised as a typed error on use | `src/fin/corpus/ocr.py:186`; `:214` | ✓ |
| B1's contract test carries no opt-in marker, so the default run collects it | `tests/corpus/test_ocr.py:583` | ✓ |
| The lane suite builds a real engine and scrubs the extra to protect that contract | `tests/host/test_workspace_http_attachment_lane.py:545` | ✓ |
| B2's deliverable is `mcp`-marked, hence deselected by default | `tests/host/test_live_attachment_graph_gate.py:194` | ✓ |
| Prior live counts — ES/`pg_es` 12, checkpoint 23 | `docs/validation/2026-10-02-pr339-repairs.md:31-32` | ✓ |
| Prior 7-file MCP count under producer `08cedb1` | `docs/validation/2026-09-30-pre330-integration.md:77` | ✓ |
| Identity gate location and exit 75 | `scripts/g4_pipeline/build.sh:206` | ✓ |
| T1/T2 contract, refusal taxonomy, verification table | `docs/validation/2026-10-02-sql-date-input-contract.md` (that pull request) | — |
| T3 policy + T4 two-build proof, root cause per file | `docs/validation/2026-10-02-c1-pin-policy.md` (that pull request) | — |
| Per-layer diff ids and regeneration recipe | `docs/validation/2026-10-02-c1-layer-diffids.txt` (that pull request) | — |
| T5 VM gold, four PDFs, mismatches, C2 deferred | `docs/validation/2026-10-02-c1-vm-gold.md` (that pull request) | — |
| T6 amendment, per-step table, operator ruling, A3b restated | `docs/validation/2026-10-02-pre330-data-scope.md` (that pull request) | — |
| T7 #276 reconciliation, A/B/B2/C/D runs, DSN proof | `docs/validation/2026-10-02-pre330-live-evidence.md` (that pull request) | — |
| B1 import-isolation contract | `docs/validation/2026-09-29-issue-323-b1-ocr-import-isolation.md` | — |
| B2 gate isolation + isolated rerun at `a2e5f65` | `docs/validation/2026-09-29-issue-323-b2-live-gate-isolation.md`; `docs/validation/2026-09-30-issue-323-b2-verification-isolated-rerun.md` | — |
| Integrated candidate = `ecd3ab4` + `b263123` + `a5b1f7a` + `a52185f` + `25ed6ac`; 15 paths, zero overlap | read-only `git diff` against `ecd3ab4` | ✓ |
| the tracked change conformance corpus on `main` is the un-versioned layout (98 files); branch `issue334-evidence-contracts` unmerged | read-only `git ls-tree` / `git diff` | ✓ |

### Citation audit

25 `file:line` references were re-read directly in this worktree, plus 7 more added for §6's
regression record; all 32 resolved to the cited content. One correction of substance, and two of
form:

- **Substantive:** that pull request's evidence doc names its verified fin revision as the pre-squash commit
  `9fa5840`, which is **not an ancestor** of the branch head, and records the branch as "subsequently
  squashed to `3e34956`". The branch was squashed and its evidence doc amended twice more; the head is now `b263123`
  (parent `ecd3ab4`, same four changed paths, same per-path diffstats). Re-checked read-only, `src/`
  and `tests/` are **byte-identical** across `9fa5840` → `3e34956` and across `9fa5840` → `b263123`,
  and `3e34956` → `b263123` differs in one line of that same evidence doc and nothing else. The
  T1/T2 verification therefore transfers to the candidate's code as it now stands, and the default
  regression in §6 ran on `b263123`; what is stale in that record is its revision label, not its
  results.
- **Form:** the spec's own `rows returned` row points at `src/fin/retrieve/mcp_adapter.py:159` for
  `_LIMIT_CAP`; the constant is actually at `:171`. that pull request already records this; it is repeated
  here so the reader is not misled by the spec line.
- **Form:** `specs/search-mode-business-sql-contract.md:475` and `:201` were checked and are
  correct as cited. No citation in this record was found to be wrong.

### Citation audit for the the tracked change revision

Every `file:line` citation **added or rewritten by this revision** was re-read in this worktree.
All 30 resolved to the cited content; **none was found wrong**. Verified in this revision:
`src/fin/host/graph.py:1184`, `src/fin/retrieve/port.py:241`, `src/fin/host/attachment_layout.py:85`,
`src/fin/corpus/parsers/_pages.py:29-33`, `src/fin/corpus/ocr.py:186` and `:214`,
`src/fin/host/evidence_contract.py:1272`, `src/fin/host/tools/compare_facts.py:275`,
`src/fin/retrieve/mcp_adapter.py:171`, `src/fin/host/stream_map.py:777`,
`tests/corpus/test_ocr.py:583`, `tests/corpus/test_ocr_region.py:3-6`,
`tests/host/test_live_attachment_graph_gate.py:194`, `tests/host/test_assigned_mode_reaches_port.py:62`,
`tests/host/test_workspace_http_attachment_lane.py:545`, `tests/retrieve/conftest.py:39`,
`tests/conftest.py:91`, `tests/retrieve/test_live_mcp_t5.py:45`,
`tests/retrieve/test_live_sql_window_user_path.py:43`, `:73`, `:133`, `:166`,
`tests/retrieve/fixtures/ledger/001_schema.sql:1-7`, `:45`, `:84`,
`tests/retrieve/fixtures/ledger/002_seed.sql:58-59`,
`tests/retrieve/test_ledger_readonly_boundary.py:84-85`, `scripts/g4_pipeline/driver.py:536`,
`scripts/g4_pipeline/build.sh:206`, `scripts/g4_pipeline/run_measured.sh:28-30`,
`specs/schemas/evidence/comparison.schema.json:76`, `pyproject.toml:22-24`.

**One deviation from the task brief, recorded rather than worked around:** the brief stated this
readiness file "IS in your worktree". It was not present on `pre330/i348` at `2fe787f`; it existed
only on `docs/pre330-remaining-plan` / `docs/pre330-delivery-audit` at `b6b3c4f` and at that pull request's
head `a1c675f`. The correction was therefore applied to **`a1c675f`'s text — the version that pull request
actually proposes — restored into this worktree**, rather than to `b6b3c4f`, which is 88 insertions
behind it. The audit named in the brief likewise lives on branch `docs/pre330-delivery-audit` at
`5f70772`, not at the filesystem path given. No other file in this worktree was modified.

## 11. Per-item acceptance status, and the verdict derived from it

**Dispositions are not outcomes.** "Dispositioned", "recorded", "classified as an
expectation/calibration defect" and "known gap" all describe what a piece of work *did with a
question*. None of them answers whether a requirement is met. Every acceptance item below therefore
carries exactly one status — **PASSED** or **STILL UNMET** — and a floor that measurement does not
satisfy is STILL UNMET whatever its classification. The verdict in §11.3 is derived from the weakest
link across the cases B3 intends to run, **not** from a count of dispositions.

### 11.1 Item-level table (every item, no omissions)

| item | status | blocks the selected B3 case? | basis |
| --- | --- | --- | --- |
| the tracked change evidence contract delivered on `main` via merged the tracked change | PASSED | no | the tracked change MERGED at `ecd3ab4`; `src/fin/host/evidence_contract.py` present (61,334 B); 98 files under `data/conformance/evidence/`, counted in this worktree |
| that pull request branch status | PASSED (as a recorded fact) | no | Still OPEN; superseded `v1/`-versioned corpus layout; not merged, not to be merged. Status is correctly recorded, not a functional gap. |
| Candidate assembled as one identified revision | PASSED | no | `2fe787f`; `git diff --shortstat ecd3ab4 2fe787f` = 20 files, +2786/−64; zero conflicts; `320ea9f` is an ancestor |
| the tracked change identity assertion bound to a revision containing it | PASSED | no | `git diff --quiet ecd3ab4 a5b1f7a -- tests/host/test_g4_pipeline_builder_identity_contract.py` → 1 (changed); vs `b263123` → 0 (identical). Re-verified on `2fe787f`: 150 passed (§7.2 row 1) |
| G4 pin self-check | PASSED | no | exit 0 (§7.2 row 3) |
| G4 identity gate non-vacuity (match → 0, non-match → 75) | PASSED | no | exit 0 / exit 75 observed (§7.2 row 4) |
| Default unit suite (reconciled) | PASSED | no | 5201 = 5200 + repaired case; 117 skipped, 408 deselected preserved; reconciled, not a second full run (§7.1) |
| B1 import-isolation contract + positive control | PASSED | no | `tests/corpus/test_ocr.py:583` unmarked, collected and green; 39 OCR-sensitive tests passed after `uv sync --frozen --extra ocr`. No redundant rerun required (§6) |
| B2 live isolation on the current candidate | PASSED | no | the tracked change, three runs on `320ea9f`: 6 passed / 0 skipped / 0 deselected each, exit 0; gate's read set excludes the manifest the tracked change changed (§6) |
| SQL date-window arg transport (T1) | PASSED | no | `-m soft_track` 27 passed / 75 deselected, exit 0; bare 75 passed / 27 deselected, exit 0 (§7.2 rows 5–6) |
| SQL live window rows independently expected (T2) | PASSED | no | `tests/retrieve/test_live_sql_window_user_path.py:166` asserts `result_count == 4`; ids `C-2315, C-2302, C-2291, C-2024Q1` (`:73`); recorded `-m sql_live` 1 passed, exit 0 |
| **Real-model date extraction / clarification** | **STILL UNMET** | **yes — for every SQL leg of B3** | Live test uses `ScriptedChatModel` (`test_live_sql_window_user_path.py:43`, `:133`). No live LLM produced those arguments. Belongs to the real browser run; not proven here (§8 item 8) |
| Scanned p1/p2/p3 OCR applied (3 pages) | PASSED | no | `pages_needing_ocr 3`; re-read from the tracked change's run artifact |
| Scanned p1/p2/p3 quotes verbatim | PASSED | no | Re-verified in this worktree against the sidecars: offsets **11 / 11 / 479**, all verbatim |
| **Scanned p2 raised floor `["text","table"]` — `table` component** | **PASSED (met by measurement)** | no | Measured `table 1` on p2 (read from the tracked change's run artifact: p1/p2/p3 = 1/1/2), matching the raised floor from the AAPL p9 source. Not merely asserted |
| **Scanned p1/p2/p3 `text` floor entries** | **PASSED (met by measurement, in the channel the product reads)** | no | Re-derived 2026-10-03 by `docs/validation/2026-10-03-c1-gold-residual.md` (ITEM 3). The `text` **box** is out of the product's region vocabulary — `_BOXED_KINDS = ("table","figure")` (`src/fin/host/attachment_layout.py:85`) — and that is now an **accepted decision** with its code path cited (**ADR 0096**), not a silence. Text presence is carried where the product consumes it (`_pages.py` `_one_pdf_page` reads `native_text`, `_page` builds the unit from the OCR read) and is **measured** there: `native_text_chars_total 0`, `pages_with_ocr_applied 3/3`, `ocr_text_chars_total 15108`, 3/3 quotes verbatim. **No floor was deleted or lowered** — the `text` entries remain in `region_kinds_expected`, and `known_gaps[11]` records the scoping in the manifest itself |
| **Mixed p3 `ocr_expected: true` (figure-scoped OCR)** | **PASSED (met by measurement)** — including the title quote | **no** | Re-derived 2026-10-03 (ITEM 2) and **closed 2026-10-03 by ROOT direction** (`docs/validation/2026-10-03-p3-title-crop-gap.md`). The driver applies the product's own figure rule (`crop_is_legible`) and cut (`crop_png`); it asked only the page rule before, which can never answer "yes" on a page with a text layer. The **root cause of the title miss** was that the region was a layout **detection** box (`l 64.9 t 646.5 r 553.5 b 382.8`) inset from where the page places its chart raster (`l 42.0 t 692.0 r 570.0 b 364.0`, measured with pypdfium2 `get_bounds`, equal to the declared `x 42–570, y 100–428`), leaving the title in the band above the crop. Fixed **in the layout path**: `driver.py` reads the image XObject placement from the page (`image_placements`) and widens an overlapping figure box to it (`artwork_extent`), applied where the layout output enters the record (`attach_regions`), keeping the model's own box in `figure_boxes_detected`. Measured: `figure_ocr_regions_read 1`, **608 chars** (was 425), title `Total net sales by fiscal quarter` **verbatim at offset 0** and **all three** alternatives verbatim (77 / 144 / 235). p1's 93 px logotype still not cropped; no region count changed on any page; scanned pages still skip. RED `7 failed` → GREEN `10 passed` |
| **Mixed p2 `table` region floor** | **PASSED (met by measurement)** | **no** | Re-derived 2026-10-03 (ITEM 1). The fixture page was re-authored to be the **ruled** table its source page is (the source carries 154 rules / 76 column separators / 240 band fills; the borderless reproduction carried 0). Same 98 text runs, byte-reproducible rebuild (`scripts/build_g4_mixed_ruled_page2.py --check`), new sha256 `0d6d4713…`. Measured **`table 1`** on p2 (box `l 38.5 t 654.3 r 595.0 b 293.0`), floor unchanged and not weakened; see §11.2 |
| Mixed p1 figure region (logotype) | PASSED | no | measured `figure 1` on p1 |
| Mixed p1/p2/p3 native text layers | PASSED | no | `native_text_chars_total` **1908** reproduced exactly after the p2 re-authoring, which is the proof the 98 text runs were not altered. Region `text` counts after the re-authoring are `14 / 3 / 5`: p2's move from 5 to 3 is the two rows the model now reads inside the table region, and `text` is a presence floor (`known_gaps[3]), so it stays met |
| T8 zero-baseline exclusion honoured | PASSED (as an exclusion) | no | No zero-baseline gold case introduced; the AAPL p4 probe was a temporary diagnostic, not a committed case |
| 80-page MSFT case | PASSED (as a comparator only) | no | No page expectations in the manifest; none invented. It is **not** a gold pass |
| C1 two-clean-build byte identity | PASSED | no | `sha256:b1b5270c…`, 11/11 layers (the tracked change); driver is bind-mounted, so the tracked change could not move it (`run_measured.sh:327-333`) |
| **`snapshot.debian.org` availability** | **STILL UNMET** | no — availability, not correctness | 2 of 6 clean-build attempts failed in the apt step. Byte identity unaffected; **repeatability** is conditional |
| C2 dedicated 4 GB device | STILL UNMET | no — deferred | No dedicated device measured. `run_measured.sh:28-30` shows 4g/4g/4 is a **harness default**, not a host property |
| `percent: null` vs numeric-only validator | STILL UNMET | no — T8 ruled the zero-baseline case out of scope | `src/fin/host/evidence_contract.py:1272` accepts numbers only; `compare_facts.py:275` emits null at a zero baseline. Open backlog; no percentage fabricated |
| `rendered_dpi` null on every page | STILL UNMET | no | C1 Finding 6; `AttributeError: 'PdfBitmap' object has no attribute 'get_dpi'`. Ticket against the pypdfium2 pin |
| Host-side cgroup peak null on WSL2 | STILL UNMET | no | C1 Finding 7; the driver-side `cgroup_peak_bytes` is the reading that stands |
| Exit-code contract on paths 64/66/65/75 | STILL UNMET | no | the tracked change exercised only the success path and one failure (exit 1). Nothing in evidence covers 64, 66, 65, 75 |
| A3b enterprise ledger | **STILL UNMET / BLOCKED** | **yes — S2 and S5b** | `plans/post321-real-sql.md:50` status `[ ] blocked on real source and access`; `:49` requires it be left explicitly blocked |

### 11.2 The mixed-chart p2 table floor: is it in the acceptance contract, or scoped out?

**It is in the contract, it was unmet, and as of 2026-10-03 it is met by measurement.** The
analysis below is the 2026-10-02 finding and is kept because the 2026-10-03 resolution is
only meaningful against it.

**It is in the contract. No recorded decision scopes it out.** Checked directly, not inferred:

1. The candidate's own manifest still authors `region_kinds_expected: ["text","table"]` for
   `g4-mixed-chart-001` page 2 — read from
   `data/corpus/g4_attachment_gold/manifest.json` in this worktree. The floor was left as authored.
2. `known_gaps[9]` **records** the disagreement; it does not remove the floor. The gap contract test
   (`tests/host/test_g4_pipeline_gap_contract.py`) asserts nothing that would treat a `known_gaps`
   entry as a scoring exclusion.
3. **No scorer consumes `region_kinds_expected` at all** (grep across `*.py` returns no reader), so
   there is no mechanism by which the note is being honoured as a scoping decision.
4. **ADR 0089** (`docs/adr/0089-lazy-page-region-attachment-preparation.md`, Status: **accepted**)
   pins the Heron layout stage. It says nothing about waiving this page's floor, and changing it is
   precisely what the tracked change recorded as forbidden in this lane.
5. The manifest's own `expectation_basis` states every expectation was authored from the fixture or
   its source filing — so the floor is an authored contract value, not an implementation output that
   can be re-derived from the result.

**Therefore the floor is part of the acceptance contract for the selected B3 browser gold case
`g4-mixed-chart-001`, and it is unmet: measured `table 0` against a required `table`.**

the tracked change's attribution is real evidence — and it is evidence **about the fixture**, not a satisfied
requirement. The page is a borderless statement rebuilt by a hand-written minimal PDF writer (98
positioned `Tj` runs, **zero** path operators, so not one ruling line), while the same pinned Heron
stage returns `table 1` on the real AAPL source page 4. That establishes the disagreement is with
the fixture's synthetic geometry. It does **not** answer the contract question, and it is not a
waiver: the manifest note says the floor "is NOT weakened", and the tracked change's own acceptance-impact line
says the record "does not call it passing". **This record does not treat it as a pass either.**

**Consequence at the time: `g4-mixed-chart-001` was BLOCKED.** Two named unblockers, neither
performed then: (a) **re-author the fixture page** with real filing geometry, or (b) an explicit
**ADR-0089 layout-pin decision**. No re-authoring was performed, the manifest note was not treated
as a waiver, and no expectation was changed to fit an output.

**Resolution 2026-10-03 (unblocker (a), taken).** The fixture page was re-authored into the
**ruled** table its source page is, and re-measured: p2 now returns **`table 1`**. The floor is
**unchanged** and was never weakened, and no ADR-0089 layout-pin decision was taken or needed.
Full record, including the source-page rule census (154 rules / 76 column separators / 240 band
fills vs the reproduction's 0), the byte-reproducible rebuild, the new fixture digest, the
expected-vs-observed table, and the honest deltas: `docs/validation/2026-10-03-c1-gold-residual.md`
(ITEM 1).

### 11.3 Per-case verdicts, and the weakest link

| selected B3 browser gold case | verdict | why |
| --- | --- | --- |
| 1. `g4-native-text-001` | **READY** | 1 page, `text` floor met, `ocr_expected false` correctly, quote from the text layer. Its committed record has `figure 0` and `figure_boxes: []`, so the figure-rule lane introduced on 2026-10-03 selects nothing on it and costs it no render and no OCR. |
| 2. `g4-mixed-chart-001` | **READY** | Re-measured 2026-10-03 twice (`docs/validation/2026-10-03-c1-gold-residual.md`, then `…-p3-title-crop-gap.md`): p2 `table 1` against its unchanged `["text","table"]` floor, after the fixture page was re-authored into the ruled table its source page is; p1/p3 regions and the 1908-character native text total bit-identical. p3 `ocr_expected` is met by the product's own figure rule (`figure_ocr_regions_read 1`, **608 chars**), and the **title quote now hits** — the region was widened from the layout *detection* box to the page's own image placement, which is what put the title inside the crop. No residual remains on this case. |
| 3. `g4-scanned-001` | **READY** | Every expectation the product can express is met by measurement, re-measured 2026-10-03 twice: `table` 1/1/2, OCR applied on 3/3 pages, `ocr_text_chars_total` 15108 identical to the prior record, 3/3 quotes verbatim (offsets 11/11/479), and the figure pass correctly reports `page_ocr_already_read_whole_page` so a scanned page is still recognized once. The `text` floor entries are carried by the channel the product reads, under an **accepted** decision with its code path cited (**ADR 0096**), and verified there. The prior **USABLE-WITH-NAMED-EXCLUSION** verdict is superseded: the exclusion is a recorded contract decision rather than a named-but-unrecorded one, and its content is measured. |

**The overall verdict is the weakest link across the cases B3 intends to run.** Re-derived
2026-10-03: `g4-mixed-chart-001` — the case that carried the blocking item — is no longer
blocked, and its last residual (the chart title quote) is now closed **in the production
layout path** rather than carried: the region the figure rule acts on is the page's own
image placement, so the title is inside the crop and is read verbatim. **All three selected
cases are READY.**

**Scope of that statement, as the operator has since ruled (2026-10-03).** READY here is READY
**for the bounded development round** — S1, S3, S4, S6-mechanism, S7, S8, on development
read-only data and data-independent mechanics. It is **not** readiness for full the tracked change
acceptance, which **stays BLOCKED** on its own terms: A3b is still BLOCKED and UNPASSED (no
operator-approved enterprise ledger), so **S2's enterprise-anchored comparison and S5b's
approved-ledger SQL turn remain UNCLOSED**, and **no enterprise acceptance is claimed**. The
real-model date extraction / clarification gap is **not** a blocker for the development round
— it is verified **inside** B3, with a live model, and belongs to that run. C2 remains
deferred: no dedicated 4 GB device was measured, and the `4g/4g/4` these runs used is the
harness default, not a host property — C2 does not block the development scope either.

### 11.4 What is provable under the operator-approved development-only amendment

Unchanged by this revision, and independent of §11.3's gold-case blocking:

- **Provable and authorised** under `docs/validation/2026-10-02-pre330-data-scope.md` (that pull request):
  **S1, S3, S4, S6-mechanism, S7, S8** — six of eight steps, on development read-only data and
  data-independent mechanics.
- **S2 and S5b stay CLOSED.** S2's enterprise-anchored comparison
  (`plans/post321-real-browser-acceptance.md:38`) and S5b's approved-ledger SQL turn (`:41`) have no
  anchor because no approved enterprise ledger exists. **A3b remains BLOCKED and UNPASSED.**
- **Non-waiver list, unchanged:** the B3 bans on scripted models, Fake ports, fixture MCP servers,
  mocked routes, API-seeded turns and skipped live legs stand (`:33`); a `real_db_not_wired` refusal
  or a synthetic-only result does not close the business-source leg; no synthetic or public row
  becomes a business source; A3a on synthetic rows "cannot close the original company-source leg"
  (`:35`); the tracked change is **not** accepted or closed by a development run; T8/T9 remain out of scope.

**Independently expected browser cases** (values a reviewer can check against a rerun, from
`docs/validation/2026-10-02-c1-gold-gap-resolution.md` and re-read here from the committed manifest):

| case | independent expectation | measured |
| --- | --- | --- |
| `g4-native-text-001` | 1 page; `["text"]`; no OCR | matches the adopted baseline |
| `g4-mixed-chart-001` p1 | `figure 1` (33×40 pt logotype) | `text 14 / table 0 / figure 1` — **met** |
| `g4-mixed-chart-001` p2 | `["text","table"]` — `table` required | `text 3 / **table 1** / figure 0` — **met** (was `table 0`; floor unchanged) |
| `g4-mixed-chart-001` p3 | `figure 1`; `ocr_expected true` | `text 5 / table 0 / figure 1`; `figure_ocr_regions_read 1`, 425 chars — **met**; the *title* quote is not reachable through the returned box, recorded in `known_gaps[10]` |
| `g4-scanned-001` p1/p2/p3 | `table` present; 3 quotes verbatim | `table` 1 / 1 / 2; quotes verbatim at offsets 11 / 11 / 479 — **met** |
| SQL window (4 rows) | `result_count == 4`, ids `C-2315, C-2302, C-2291, C-2024Q1`, end boundary `2026-04-30` | recorded `-m sql_live` 1 passed — transport and rows only, **scripted model** |

**Final verdict (2026-10-03 amendment):**

> ## **READY for the bounded development round** — the full the tracked change acceptance remains **BLOCKED**
>
> **The gold blocker is closed.** The mixed p2 `table` floor that blocked this verdict is now **met
> by measurement** against an unchanged floor (`table 1`); p3's figure-scoped OCR now runs through
> the product's own rule and reads the chart's labels; and the scanned case's `text` floor is
> carried — and verified — in the channel the product reads, under accepted **ADR 0096**. All three
> selected gold cases are **READY** (§11.3). No floor was lowered or deleted to achieve this.
>
> **Scope of this READY.** It covers the operator-approved bounded development round only:
> **S1, S3, S4, S6-mechanism, S7, S8**. The operator ruled that the enterprise ledger and the
> dedicated 4 GB device do not gate that round, so A3b and C2 are carried as open prerequisites of
> the *enterprise* acceptance, not as gates on this one.
>
> **The full the tracked change acceptance is still BLOCKED.** A3b is BLOCKED and UNPASSED, so **S2's
> enterprise-anchored comparison and S5b's approved-ledger SQL turn stay UNCLOSED and no
> enterprise acceptance is claimed**. C2 remains deferred and unpassed — the `4g/4g/4` these runs
> use is the harness default, not dedicated hardware. Real-model date extraction / clarification
> is also unproven and belongs to the real browser run. Closing the gold gap removed the one
> blocker that gated the development round; it did not close the enterprise legs.
>
> One residual is carried, named and owned: the mixed p3 chart **title** quote is unreachable
> through the layout stage's returned figure box, which is inset from the raster the manifest
> declares. Owner: the Heron stage pinned by ADR 0089. It is recorded in `known_gaps[10]` and is
> **not** counted as a pass.
