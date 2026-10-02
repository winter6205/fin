# Pre-330 local integration and environment preparation

Date: 2026-09-30. Basis: the operator asked to combine the three prior execution sessions and prepare the environment before fin issue #330, report functional issues, and handle non-functional issues autonomously. The operator subsequently confirmed that there is no enterprise ledger yet and authorized building or finding development resources.

**Status: local integration candidate; B3 has not run. The original real-enterprise A3b requirement is not satisfied by development rows. Neither repository's main nor remote PR state is advanced by this record.**

## Revisions and integration

Baseline: local fin main `f1712c83404481034d37804e41192392d26cf40d` (five local planning commits above origin main `670f65f`). Worktree was clean at entry.

| Delivery | Source tip | Local candidate commit |
| --- | --- | --- |
| #323 OCR/import and live gate isolation | `2283fa3` | `7dfc7e8` |
| #324 pinned build and measured pipeline | `bfbfff6` | `dd674e8` |
| #322 SQL consumer | `37da55a` | `b522c2e` |
| #334 evidence contracts | `3283ed4` | `91df7ce` |
| #335 type gate and Q1 ruling | `a692426` | `4a2f5db` |

All five were squash-combined on `integration/pre330`, one commit per delivery. SQL tip `37da55a` is the merge-base of the SQL and evidence branches: E already contains the complete A delivery. Conflicts caused by squash ancestry were resolved to E's successor files. F's real conflict was resolved by retaining its typed `refused.reason` together with E's `track_decision`; the Fake port retains A's SQL window parameters. No new SQL-window behavior was implemented.

The first three explorer attempts failed with an account usage limit. Retry reports were received for SQL/evidence and type/Q1; the stability/pipeline explorer supplied its findings before being stopped to free a review slot. A test engineer supplied failing snapshot tests and later repaired the stale live-visual test. No LSP or Exa callable tool was exposed; repository navigation used Git and text inspection. This limitation is not presented as an LSP run.

## Non-functional repairs

- Two new E persistence-outage branches returned `JSONResponse` under F's `dict` return annotation. Applied the same runtime-inert casts already used by F; the actual response object, status, and body remain unchanged. Pyright changed from two diagnostics to zero, and the existing HTTP outage/history cases pass.
- `build.sh::snapshot` did not remove its destination argument before executing the command. The success case attempted to execute the destination file; the failure case never executed its supplied failing command. Two independently written Bash behavioral tests reproduced both failures. One `shift` repaired the command dispatch; replacement, preservation on failure, and temporary-file cleanup now pass.
- `build.sh` supplied `PIP_FIND_LINKS`, but Dockerfile never declared the build argument. Added its `ARG`; the actual pinned-build pip output now explicitly lists the supplied local wheel index. Package versions and hash locks are unchanged. Listing a wheel index is not a claim that pip selected that index for every package.
- The existing no-opt-in corpus CLI test inherited the operator adapter setting even though its case requires neither CLI nor environment opt-in. Cleared only that setting in that test; retained the `SystemExit` refusal assertion. Product code is unchanged. The full first run reproduced the failure; the five-case file passes after repair.
- E changed the visual wire projection to `(rows, refusals)`; the live visual acceptance still read it as a list. The test repair unpacks the result and requires no refusals while retaining the row, persistence, and quote checks. Actual live results are recorded below.

## Development resources

Six session-owned containers use only localhost ports and the label `fin.pre330=true`; they do not reuse another session's data or ports.

| Resource | Container | Host port | Scope |
| --- | --- | --- | --- |
| Fin checkpoint | `fin-pre330-pg` | 55480 | database `pre330`, graph/visual live checks |
| S3-compatible bytes | `fin-pre330-s3` | 8490 | SeaweedFS 4.43; bucket `fin-pre330-gate` |
| Development business ledger | `fin-pre330-ledger` | 55481 | PostgreSQL 16.15; database `fin_business`; independent from checkpoint/corpus |
| iknow corpus | `fin-pre330-iknow-pg` | 55482 | pgvector/pg16; disposable per-run schemas |
| Neo4j/GDS | `fin-pre330-neo4j` | 57687 / 57474 | Neo4j 5.26.0 / GDS 2.13.2 |
| ES/IK | `fin-pre330-es` | 59200 | ES 8.19.20 with analysis-ik; health green |

Protected, ignored shell configuration: `.fin/pre330/environment.sh` (directory mode 0700, file 0600). It contains only disposable local-development settings, never production credentials. Load it with `source .fin/pre330/environment.sh`; it selects the read-only ledger for the SQL producer. It does not select an iknow revision or impersonate an approved enterprise source.

The ledger is seeded from the committed `tests/retrieve/fixtures/ledger/` files, not from model-generated expected results. It contains four counterparties and six contracts, five visible through the authorized active-contract view. Reader `fin_sql_reader` has SELECT only on `ledger_ro.contract_expiry`, no base-table grants, transaction read-only, and bounded timeouts. The fixed development question window is 2026-01-01 through 2026-04-30; dates are deliberately stable, not relative to the wall clock.

| Fixture | SHA-256 |
| --- | --- |
| `001_schema.sql` | `a925b4e5e16b0364011fe91dc62df5c9a49919e2c6d6d05c2c9a73f229fa751a` |
| `002_seed.sql` | `e38f1dd6abfa66ea8e8e6d68b8353b5a3fb0ee6be652c95078c467a13121ecd6` |
| `003_readonly_role.sql` | `c7d78e215f20ed201dc45f8ab9d4f9038c392b95c51a7b46d164e5aac9bdc5d2` |

A public-source candidate was also fetched from the UK government's [Contracts Finder API](https://www.contractsfinder.service.gov.uk/apidocumentation), using the public [award release](https://www.contractsfinder.service.gov.uk/Published/Notice/releases/070edbca-cc1c-4d2f-a998-b6c683f20686.json). Raw JSON and provenance are in `.fin/pre330/public-contract-release.json` and `.provenance.json`. SHA-256: `e1055bba9a52163096fc65abd0042ae1a8117e21ffc5b10a53142b43b94204ab`; 7,818 bytes; one award release; Open Government Licence v3. It provides suppliers, value, and contract period, but no structured renewal-notice rule. The mapping assessment leaves notice days, automatic renewal, and renewal term unknown. It is not imported as a fabricated complete ledger row, and is not the operator's enterprise ledger.

## Actual verification

Commands below refer to the candidate with the stated repairs. Provider credentials were loaded only by the existing application/test seams and never printed or committed. Local passwords/DSNs are omitted from this record.

| Command / boundary | Observed | Exit |
| --- | --- | --- |
| `uv sync --locked --extra ocr --extra object-store` | locked environment synchronized; no declaration/lockfile rewrite | 0 |
| `uv run --no-sync pytest -q tests/corpus/test_ocr.py tests/host/test_live_attachment_graph_gate.py -m 'not mcp'` | 21 passed, 6 live cases deliberately deselected | 0 |
| `uv run --no-sync pytest -q -m eval tests/eval/test_gold.py` | versioned baseline: 9 passed, done=6/refuse=3 | 0 |
| `npm test -- --reporter=dot` in web | 584 passed, 32 files | 0 |
| `npm run build` in web | TypeScript and Vite production build successful | 0 |
| `uv run --no-sync pyright` after repair | 0 errors | 0 |
| HTTP targeted persistence/history cases | 3 passed | 0 |
| Snapshot tests before repair | 2 failed: destination executed, failing command not executed | 1 |
| Snapshot plus relevant pipeline/layout suites after repair | 203 passed | 0 |
| Corpus CLI file after environment repair | 5 passed, refusal assertion preserved | 0 |
| Post-restart CLI, snapshot, pin and builder contract files | 100 passed in 1.53s; pyright also rechecked with zero errors | 0 |
| Post-restart baseline gold for the phase-clarification record | 9 passed; done=6/refuse=3 | 0 |
| First `uv run --no-sync pytest -q` | 1 ambient-adapter failure, 5071 passed, 115 skipped, 398 deselected in 1029.63s | 1 |
| Final `uv run --no-sync pytest -q` | interrupted by the host/WSL restart after approximately 81%; no final result | interrupted |
| Live graph gate on owned Postgres/S3, `-m mcp tests/host/test_live_attachment_graph_gate.py` | 6 passed, zero skips; seven child PIDs all exited -15 | 0 |
| Current iknow main `08cedb1`, rebuilt in detached `iknow-memory-pre330-main`, seven issue-276 MCP files | 11 passed, zero skips | 0 |
| Jev three required files with `-m jev_live` | 8 passed, 1 deselected, real provider calls | 0 |
| SQL readonly/executor two files with `-m sql_live` on seeded independent ledger, producer `620f135` | 17 passed, zero skips in 12.14s | 0 |
| Producer `620f135`: `npm run build`, four business-query/config/mode-d test files | build successful; 69 passed | 0 |
| Full PG/ES/Neo4j search-mode suite against current main `08cedb1` | 10 passed, SQL case failed `retrieve_unavailable` | 1 |
| Same full suite against rebuilt producer `620f135` plus seeded readonly ledger | 10 passed, SQL case failed `malformed_evidence_contract` | 1 |
| Real provider/PG/S3 visual lane before stale-test repair | 8 passed, 1 failed because tuple projection was treated as list | 1 |
| Real visual lane after stale-test repair | 9 passed, zero skips in 80.46s | 0 |
| `bash scripts/fin check` | prerequisites resolved | 0 |
| `playwright-cli -s=fin-pre330-env open about:blank`, snapshot, close | browser started and closed; CLI 0.1.8 | 0 |

The MCP harness uses real MCP/PG/ES/Neo4j but deterministic embeddings. The graph-gate model transport and visual-lane planner are scripted as documented by those suites. These tests are component-boundary evidence, not B3's real-model, visible-browser journey. Browser availability was checked on `about:blank`; no thread was created, uploaded, or answered for B3.

Graph-gate checkpoint scratch schemas and full-stack MCP scratch schemas were both counted as zero after teardown. Ledger contract/view counts are 6/5. A host/WSL restart interrupted the final default suite and second build and cleared their `/tmp` logs; neither is reported as a completed pass. The six named containers were restarted without recreating their data. Subsequent logs are stored under ignored `.fin/pre330/logs/`. Test child servers and the temporary wheel HTTP server are stopped; the six named development infrastructure containers are intentionally retained for the next session. Stop/start only those named containers to retain their data; do not run a global cleanup.

## Open prerequisites and functional findings

1. **SQL user-window source is absent.** `src/fin/host/graph.py`'s retrieve tool sends the mode but no `sql_window_start/end`; the real adapter validates both before transport and refuses with `invalid_business_query`. Fake records the optional values without enforcing them, so the mode-only graph case is not user-reachability evidence. A bounded functional follow-up must define explicit date extraction/clarification and its tests; no silent default is introduced here.
2. **A/E producer-consumer success has not passed its real boundary.** The live full-stack SQL case reaches the latest producer and the actual read-only ledger, then returns `malformed_evidence_contract`. E3 calls `_validate_snapshot` on ingress rows, requiring a per-row `schema` even though spec §4.4 puts that marker on E5 egress snapshots. The actual producer executor output was captured in ignored `.fin/pre330/producer-executor-window.json`: rows have exactly `fields`, `queried_at`, `record_key`, `source`, and `summary`. Validation of an actual row returns `database_row snapshot: missing ['schema']`. A diagnostic copy with only the egress schema added then returns `queried_at is not a strict timestamp`: the producer emits ISO UTC milliseconds (observed `2026-09-30T12:22:14.741Z`) while E's regex admits whole seconds only. No producer row was rewritten or accepted by this diagnostic. Ingress test fixtures contain the extra schema and therefore missed the real incompatibility. Both schema placement and timestamp precision require functional contract reconciliation; neither the lower executor's 17 passes nor static envelope inspection closes this failure.
3. **Upstream merge order remains mandatory.** iknow #279 is OPEN at `620f135`. The detached main build `08cedb1` lacks the new SQL producer, and the actual suite proves the incompatibility with the newly opened fin SQL consumer. The #279 `test-fast`/`test-es` jobs never started: GitHub annotations cite failed account payments or a spending limit, with runner_id=0 and no steps. This is an external CI prerequisite, not a product-test failure or a passing check. No account settings are changed.
4. **No approved enterprise source.** Development rows and a public award release cannot close original A3b. The operator confirmed that enterprise data does not exist in this development phase; source/ownership/access/version acceptance remains explicit rather than silently replaced.
5. **C1 reproducibility remains open; C2 target-device measurement is scheduled for a later phase.** The first new full no-cache build completed, verified tessdata and all four model artifacts, then exited 75 because its identity (`sha256:d3bae2c0de4907b34f6fd83939b69b1c87f0aa4f4c05c75046c023938fc77ccc`) differs from the declared pin. The second build was interrupted by the host/WSL restart before producing an image, so there is no completed two-build comparison and no new identity is declared.

   **Operator phase clarification (this session):** the current virtual machine is the development and integration environment. It does not need to be a dedicated 4 GB device, and missing that device is not a blocker for the present environment-preparation task. C2's low-memory measurement is retained for the later target-deployment validation phase; it is deferred, not passed. The historical cgroup-limited measurements remain valid for their stated scope. This clarification updates the timing of the device requirement, not the test results, and does not claim that the SQL or C1 prerequisites have passed. A future B3 acceptance checklist must reflect this explicit phase decision rather than mechanically treating the missing device as a present development-environment defect.
6. **Q1 is resolved.** The existing 2026-09-30 operator ruling accepts figure-only crop and honest whole-page fallback for tables; table crop is backlog, not a B3 prerequisite. This record does not change that decision.

## Pinned review

Initial independent Standards and Spec reviews used base `f1712c83404481034d37804e41192392d26cf40d` and Git tree `e5b1426788114111289ea75ae773f736883460ca`, so build output changes could not move the review target. Standards: `OVERALL: 0 High / 0 Medium / 0 Low`. Spec: `OVERALL: 0 High / 2 Medium / 0 Low`; the Mediums requested this consolidated record and actual evidence that the build argument reaches pip. This record and the pinned-build log address them. Later live failures remain runtime integration blockers independent of that initial static verdict. The candidate is not promoted to main merely because static review found no High.

Final delta reviews: Standards reported zero findings on the visual test and validation record. Spec reported zero remaining findings after recording the interrupted runs and aligning the three plans with the operator's C2 phase ruling. B3 still requires C1 and VM pipeline gold; only the dedicated-device measurement is deferred. The final documentation gold run returned 9 passed (done=6/refuse=3), exit 0.

## Delivery verification scope

Basis: `plans/pre330-integration-repairs.md` and the operator's request to prepare the environment before B3, report functional issues, and handle non-functional integration defects. Local integration, bounded non-functional repairs, development ledger/resources, and functional-gap reporting are delivered with the component evidence above. Final all-suite confirmation and C1's second full-build comparison are missing because the host restarted; C2 device measurement is explicitly deferred by the operator. B3 is not run. This is a verified local preparation increment, not a claim that every original #330 prerequisite is satisfied. No push, default-branch merge, release, or GitHub state change is performed.
