# the tracked change A2 — §9 leg-2 live ledger-boundary and executor run (synthetic dev instance)

Date: 2026-09-30. Spec: `specs/search-mode-business-sql-contract.md` §9, plan
`plans/post322-a2-sql-slice.md` T3/T8. This record covers the A3a live
observation landed in fin `7cfcc10`, whose committed evidence was the commit
message only — spec §9 requires the record itself, and the the tracked change dual-axis
review named the missing record as a pre-merge gap.

## Environment / dataset version (sanitized)

| Component | Value |
| --- | --- |
| Code under test (original run) | fin `7cfcc10` (tree base `0ac256b`), per the commit's own record line |
| Code under test (re-run, this record) | branch `worktree-issue322-sql-tier` at tip `e077d85` + the review-repair working tree (retrieval suites byte-equal to the tip; only the opt-in mark was renamed `mcp` → `sql_live`) |
| iknow executor | `d07ef4f` build (original run); sibling checkout at `620f135` with its existing `dist/` (re-run) — the Node driver subprocess, never a fin import |
| Database | dedicated isolated container `fin-sql-business-db`, image `postgres:16-alpine`, server `16.15`, port 5434 — NOT fin's checkpoint DB and NOT iknow's corpus/index Postgres (spec §2.4 substitutions stay forbidden) |
| Credential | `fin_sql_reader` (created by the committed `003_readonly_role.sql`), loaded from `~/.fin_dev/sql_reader.env` into the process environment only; no DSN, password, or credential appears in this file, in test output, or in the driver JSON |
| Fixture (dataset version) | `tests/retrieve/fixtures/ledger/` — `001_schema.sql` (sha256 `a925b4e5e16b0364…`), `002_seed.sql` (`e38f1dd6abfa66ea…`), `003_readonly_role.sql` (`c7d78e215f20ed20…`); 6 contracts seeded, 5 active visible through the view |
| Expected values | derived from the fixture DDL text inside the test file, never read back from the database |

## Commands

```bash
# at the original run (7cfcc10) and again at this record's revision the same
# two suites ran under the renamed opt-in mark:
set -a; source ~/.fin_dev/sql_reader.env; set +a
uv run pytest -q -m sql_live \
  tests/retrieve/test_ledger_readonly_boundary.py \
  tests/retrieve/test_live_sql_business_db.py -v
# at 7cfcc10 the same files carried `-m mcp`; the mark rename is this round's
# review fix (#8) — `sql_live` keeps the live legs distinguishable from the
# fixture-server protocol smoke, as spec §9 and AGENTS.md require.
```

## Per-probe outcomes

Original run (`7cfcc10`): **11 boundary + 6 driver = 17 passed, exit 0** (the
commit's own record line). Re-run at this record's revision: **17 passed in
12.18 s, exit 0**. Per probe (both runs; the assertions are fixed, the
observed outcomes match):

| Probe | Observed |
| --- | --- |
| `test_permitted_view_is_readable_and_matches_the_seed` | view `ledger_ro.contract_expiry` returns the DDL-declared rows |
| `test_window_bound_is_a_bound` | the 2026-Q1 window returns exactly the four expected rows; `C-2291.notice_by` = 2026-02-12; terminated row absent |
| `test_notice_arithmetic_is_in_band` | seed-side arithmetic band holds |
| `test_base_table_is_unreadable` | direct `ledger.contract` read refused **42501** (privilege layer) |
| `test_write_is_refused` | base-table `INSERT`/`UPDATE` refused **42501**; VIEW `UPDATE` refused **55000** — a two-table-join view is not auto-updatable, so PostgreSQL refuses the write shape BEFORE any privilege check; the per-target distinction is recorded and never collapsed (a 55000 here must not be misread as a grant leak) |
| `test_temp_table_is_refused` | `CREATE TEMP TABLE` refused **25006** (`default_transaction_read_only`) |
| `test_transaction_read_only_is_observed_from_inside_the_connection` | `SHOW transaction_read_only` reads `on` in the application connection (the session guarantee) |
| `test_role_default_settings_are_provisioned_in_the_catalog` | the `ALTER ROLE` defaults read from `pg_db_role_setting` (cluster-wide row, `setdatabase=0`) — the §9 correction: the session value alone does not prove provisioning; BOTH are asserted |
| `test_row_count_is_unchanged_after_every_write_attempt` | counts identical before/after every refusal attempt |
| `test_role_holds_exactly_the_expected_grants` | `role_table_grants` equals the expected list exactly (views only, no base tables, no `ALL`) |
| `test_per_run_schema_is_isolated` | scratch-schema work does not touch the shared seeded instance |
| `test_expected_rows_are_derived_from_ddl_not_the_database` | expectation source is fixture text |
| `test_executor_returns_the_ddl_declared_window_rows` | the real iknow `BusinessQueryExecutor` (Node subprocess) returns exactly the DDL rows for `contracts_expiring` |
| `test_zero_row_window_is_the_legitimate_empty_outcome` | empty window = `sql_no_matching_rows`, observed distinct from `retrieve_unavailable` (dead-port probe) and from denials |
| `test_out_of_template_query_is_refused_before_a_connection` | out-of-template name against an unreachable instance answers the REFUSAL token, not an unavailability — proof the refusal precedes connect |
| `test_rebuilding_the_fixture_produces_the_same_rows` | 001+002 re-applied into two fresh scratch schemas reproduce identical rows |
| `test_record_versions` | driver records code/db/fixture versions in sanitized form |

No failure and no skip occurred in either run; a missing live prerequisite in
this suite is designed to FAIL loudly (blocked evidence), and none did.

## What this record does NOT prove

- **Not the enterprise ledger (spec §9 leg 1).** The operator's real system of
  record has not been identified or authorized; leg 1 is BLOCKED evidence —
  not skipped, not passed. A green synthetic run says nothing about the real
  ledger's own access controls.
- **Not the fin → iknow live round trip.** The driver covers the
  `BusinessQueryExecutor` → PostgreSQL seam. The fin host graph, the retrieve
  port, and the MCP transport above iknow were NOT exercised live here.
- **Not the browser journey.** B3 owns it (`plans/post321-real-sql.md:39`);
  `GET /api/query` still bypasses the 档 table (plan "Not in scope").
- **Not that the query-template allowlist is complete**, nor that the view's
  predicate is semantically the intended scope — both are asserted against
  this fixture only.
- **Not user-reachability of the `sql` 档.** The graph and workspace send no
  `sql_window_*` pair yet, so a real `sql` ask fail-closes
  `invalid_business_query` before transport; the T7 known gap in
  `plans/post322-a2-sql-slice.md` records it.
