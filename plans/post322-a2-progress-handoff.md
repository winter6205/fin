# A2 progress handoff

State at pause. Read this before resuming; it records what exists, what was
verified, and what is blocked, so the next turn does not re-derive any of it.

## Where the work is

| Repo | Branch | Commits | Pushed |
|---|---|---|---|
| fin | `worktree-issue322-sql-tier` (worktree at `.claude/worktrees/issue322-sql-tier`) | plan + fixture + spec + evidence kind: `7281b87` `0d209c1` `f84f33d` `d2c3aa0`; live leg-2 verification: `7cfcc10`; sql 档 opened end to end: `6e8ed97` `9888e9b` `d20a7c5` `1c6d815` `e077d85`; dual-axis review repairs: this branch's follow-up commits (numeric-subset gate, one snapshot owner, source-id escaping, one omit-null serializer, `sql_live` mark, plan/record honesty) | no |
| iknow-memory | `sql-business-query-module` | `805cff2` business-query module · `214c732` timezone fix | no |

Neither repo is pushed. Neither PR exists. The earlier A1 spec work is on
`worktree-issue322-sql-tier` and belongs to PR #331, which is open and mergeable.

## What is done

**T1 (decision) — settled and persisted.** The row is a sibling key on the
retrieve envelope, never in `chunks[]`, because the chunk shape requires
`source_ref` and `valid_window` and a row has neither; `Chunk` and `Locator` are
untouched. A SQL failure reaches the wire only as a widened named token. ADR 0091
is `accepted` and `docs/CONTEXT.md` carries the `database-row` term.

**T2 (decision) — settled and persisted.** The empty SQL result gets its own code
`sql_no_matching_rows`, by the argument the spec already uses for timeout: two
facts with two different remedies may not share a code. v1 carries no scope on
the request at all, which removes the forgery trap rather than guarding it;
`sql_authorization_unverified` is retained and named for the per-identity era.

**T3 (fixture) — done; the boundary is VERIFIED live.** The `fin_sql_reader`
role now exists on the isolated instance, and the §9 leg-2 probes were OBSERVED
at `7cfcc10`: base-table read/write refused 42501, `CREATE TEMP TABLE` refused
25006, VIEW `UPDATE` refused 55000 (a two-table-join view is not auto-updatable
— refused before the privilege check, recorded per target and never collapsed
into the 42501 proof), `SHOW transaction_read_only` reads `on` AND the
`ALTER ROLE` defaults are read from `pg_db_role_setting`, `role_table_grants`
equals the expected list, row counts unchanged after every attempt. 11 boundary
+ 6 driver tests passed against postgres:16-alpine (iknow `d07ef4f`, fin
`0ac256b`), exit 0. Record:
`docs/validation/2026-09-30-post322-a2-leg2-live-ledger.md`.

**T4/T5 (iknow module) — hermetic half done.** The business-query module is
independent of the document index, exposes one named template with bound
parameters only, lazily imports `pg`, and widens the failure-token union from
one member to seven. Typecheck exit 0; 81 tests pass in the two new suites. The
18 pre-existing `tests/config.test.ts` failures are unrelated — reproduced
identically on a clean unrelated worktree at `469f84f`.

**T6 (fin evidence kind) — committed, now reachable through the opened 档.**
The `database-row` kind is threaded through the citation gate, wire projection,
CSV export, run payload, CLI and web client. The review round made the numeric
gate a per-token SUBSET check (one supported figure beside a fabricated one now
refuses), gave the snapshot key and assembly one owner
(`citation_gate.DATABASE_ROW_SNAPSHOT_KEY` / `database_row_snapshot`), gave
source ids delimiter escaping (two records cannot spell one id), and left the
omit-null wire rule in exactly one serializer.

**T7 (wire + refusal seams) — landed; completion NOT met, named gap below.**
`6e8ed97` opened the 档 end to end and consumed row evidence; `9888e9b` and
`1c6d815` rewrote the retired `sql`-refusal pins as opened-档 acceptance
(equal-or-stronger, none deleted); `d20a7c5` retired the stale refusal wording;
`e077d85` completed the E1 row-OBJECT shape (`{schema, rows, result_count}`,
bare array obsolete).

**T8 (live acceptance + record) — leg 2 run and recorded; leg 1 blocked.** See
T3 above and the validation record for the observed leg-2 results.

## Blocked / open

**Known gap (T7 completion is not met because of this):** the graph and the
workspace send door supply NO `sql_window_start` / `sql_window_end` pair, so a
real user `sql` request fail-closes `invalid_business_query` at
`fin.retrieve.port.resolve_sql_window` before transport (the port refuses to
default a window, per contract). The 档 is wired and proven hermetically
through the tool hop; wiring the window from the ask is the next unit of work
before T7's "one entry point answers sql" or the user-reachability clause of
T8 can be claimed.

**Leg 1 (the operator's real ledger) cannot run** until a source is identified;
blocked evidence, not a skip and not a pass.

**A leftover stash in iknow-memory** (`a0cab5e0`, `sql-bq-wip-verify`) holds a
copy of `errors.ts` that is already contained in commit `805cff2`. It is
redundant. `git stash drop` was denied as irreversible destruction, so it
remains. Removing it is safe once authorized.

## What is next

1. Plumb the `sql_window_*` pair from the ask (host decision or pinned 档
   filters) so a real `sql` request reaches the wire with its window — the
   T7/T8 clauses named above close only with this.
2. Code review on the combined diff, then the real interaction test, then
   push and PRs in both repos.

## Traps worth not rediscovering

- **Merge order is correctness, not coordination.** If fin removes its `sql`
  refusal before iknow ships the arm, a `sql` send reaches the wire and the
  server answers with prose, which fin classifies as `retrieve_unavailable`. The
  operator sees a false error.
- **`SHOW transaction_read_only` proves the session, not the `ALTER ROLE`.** The
  application sets the flag on its own pool as well, so a green session
  assertion alone would let a missing provisioning step pass unnoticed. Leg 2
  must assert both — `7cfcc10` added the `pg_db_role_setting` probe for exactly
  this.
- **Dates east of UTC.** `pg` parses `date` and the view's derived timestamp at
  local midnight; `toISOString()` cited the ledger's day one day early. Fixed in
  `214c732`, guarded by a three-zone regression test.
- **The routing refusals are still untokened.** `invalid_search_mode` and
  `empty_query` are message prose, not union members, so they are already
  indistinguishable from a dead server on the wire. T7 touches that path and
  inherits the gap; §7.1 now says so explicitly.
- **The live ledger suites ride their own mark now.** `-m sql_live` selects
  `test_live_sql_business_db.py` and `test_ledger_readonly_boundary.py`;
  `-m mcp` selects only the fixture-server protocol smoke. Do not re-merge
  the two — spec §9 and AGENTS.md require the distinction.
