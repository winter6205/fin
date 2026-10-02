# T7 current-main live evidence reconciliation (MCP, Jev, PG/ES, checkpoint)

Basis: task 7 of `plans/pre330-remaining-prerequisites.md` ("Live evidence — reconcile #276 and run only missing or stale MCP/Jev proof") and `plans/post321-real-browser-acceptance.md`. Remote #276 being OPEN is status, not a failed test, and the 115 default skips of the hermetic suite are not live proof. This record does not broaden into B3 and does not accept B3.

The plan file is tracked only on `docs/pre330-remaining-plan` (`7dcfce7`) and is absent from this branch, so no T-status line is edited here; ROOT applies status lines centrally.

## Tested revisions and environment

| Item | Value |
| --- | --- |
| fin branch / base | `pre330/s4-evidence` at `ecd3ab4` (origin/main) |
| fin code identity | `src/` and `tests/` trees byte-identical to `53d592a`; only 3 docs files differ |
| producer revision | `e8165a704c7b0e733744c01de77322622e3be03a` (merged head) |
| producer worktree | `/home/winner/projects/iknow-wt-s4`, created by this session |
| `dist/index.js` sha256 | `1beb2c1b550a868b0a0d5a349578ce22c2a469bc15cbb2ab73e6fd23c60510f6` |
| ES cluster reached | `docker-cluster` / node `eecd3f6c00a5`, ES 8.19.20, port 59200 |
| Checkpoint DSN | `postgresql://***:***@127.0.0.1:55480/pre330` |
| Live ledger / iknow DB | `127.0.0.1:55481` / `127.0.0.1:55482` |

Credentials come from the protected `.fin/pre330/environment.sh` and the repo `.env` through the normal loader. No DSN, key, or bucket value is reproduced in this record.

## F1a disposition — distribution provenance

`dist/` is gitignored and the only built-artifact assertion is `is_file()` (`tests/retrieve/live_iknow_support.py:412-416`), so a stale build is indistinguishable from a fresh one. This session therefore built its own worktree at the merged head rather than reusing a checkout that merely happened to contain `dist/`.

`dist/index.js` did not exist in the new worktree before the build (`ls` returned "No such file or directory"), so `npm run build` was a genuine build and not the byte-level no-op recorded at `docs/validation/2026-09-25-g4-t1-thread-attachment.md:403`. `npm install` and `npm run build` both exited 0.

The fresh build hash is byte-identical to the shared checkout's `dist/index.js`. Together with `git diff --stat eea1e07 e8165a7` being empty over the whole tree, this shows the producer revision difference was a squash-merge identity difference only. Consequence: the `FIN_LIVE_IKNOW_ROOT=/home/winner/projects/iknow-memory` run in `docs/validation/2026-10-02-pr339-repairs.md:31` was **not** running a stale distribution. F1a's risk did not materialize for that run, but the process gap remains open — `is_file()` alone would not have detected it, and only the hash comparison does.

## Case-by-case reconciliation

| Case | Prior evidence | Prior tested revisions | Verdict | Action |
| --- | --- | --- | --- | --- |
| 7-file MCP list (11 tests) | `docs/validation/2026-09-30-pre330-integration.md:77` "11 passed, zero skips" | producer `08cedb1` | **STALE** — `08cedb1` is an ancestor of `e8165a7`, but 1 intervening commit changes 13 files (+1263/−48) in `src/`, including `core/retrieval/types.ts`, `core/retrieval/wire-filters.ts`, `tools/retrieve.ts`, `index.ts` — the exact retrieval and MCP-tool paths under test. The same record's full PG/ES/Neo4j run exited 1. | Rerun as COMMAND A |
| 3-file Jev list (9 tests) | `docs/validation/2026-09-30-pre330-integration.md:78` "8 passed, 1 deselected" | producer `08cedb1` | **STALE** — same material `src/` drift. Earlier 2026-09-27 / 2026-09-29 records list `jev_live` as blocked or never run and cite no SHA, so they add nothing. | Rerun as COMMAND B, plus B2 for the dual-marked gap |
| Live ES / search-mode suite (12 tests) | `docs/validation/2026-10-02-pr339-repairs.md:31` "12 passed" | fin `53d592a`, producer `eea1e07`, merged `e8165a7`/`b140366` | **REUSABLE** — fin `src/`+`tests/` byte-identical to `ecd3ab4`; producer trees fully identical; `dist` hash identical. | Rerun as confirmation (C) |
| Checkpoint pair (23 tests) | `docs/validation/2026-10-02-pr339-repairs.md:32` "23 passed" | fin `53d592a` | **REUSABLE** on the same tree-identity argument | Rerun as confirmation (D) |
| ADR 0093 `-m sql_live` 6 passed / `-m mcp` 11 passed | `docs/adr/0093-evidence-token-version-carrier.md:387-401` | not pinned to a merged head | **SUPERSEDED** by the 2026-10-02 pg_es record; outside the T7 case list | Not reused |
| Evidence JSON artifact for #276 | none exists | — | **MISSING** — every #276 record is markdown; no machine-checkable artifact | None produced here; this record is the tracked replacement |

No T7 test file and not `tests/retrieve/live_iknow_support.py` has changed since 2026-09-29 on the current head, so no test-side drift exists; the staleness is entirely in the producer revision and in the absence of a post-merge run of the 7-file and 3-file lists.

## Commands, exits, and counts

All runs used `uv run --no-sync` from this worktree with the protected environment sourced and `FIN_LIVE_IKNOW_ROOT=/home/winner/projects/iknow-wt-s4`.

| ID | Command | Exit | Result |
| --- | --- | --- | --- |
| A | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py tests/retrieve/test_live_mcp_t3.py tests/retrieve/test_live_mcp_t4.py tests/retrieve/test_live_mcp_t5.py tests/retrieve/test_live_corpus_ingest.py tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_mcp_read_source.py` | 0 | 11 passed, 0 failed, 0 skipped, 0 deselected in 13.60s |
| B | `uv run --no-sync pytest -q -m jev_live tests/host/test_live_jev_search_mode.py tests/host/test_live_jev_track.py tests/retrieve/test_live_jev_tier_reaches_wire.py` | 0 | 8 passed, 0 failed, 0 skipped, 1 deselected in 16.34s |
| B2 | `uv run --no-sync pytest -q -m "mcp or jev_live" tests/retrieve/test_live_jev_tier_reaches_wire.py` | 0 | 1 passed, 0 failed, 0 skipped, 0 deselected in 5.68s |
| C | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py` | 0 | 12 passed, 0 failed, 0 skipped, 0 deselected in 4.05s |
| D | `uv run --no-sync pytest -q tests/corpus/test_ingest_durable_restart.py tests/corpus/test_ingest_state_store_durable.py` | 1, then 0 | first attempt 2 failed / 21 passed; after the fix below 23 passed in 7.02s |

### Mark-expression scope gaps (both plan commands are individually incomplete)

- COMMAND A's `-m mcp` does not collect `tests/retrieve/test_live_jev_tier_reaches_wire.py`, because that file is dual-marked `pytest.mark.mcp` + `pytest.mark.jev_live` and the Jev deselection hook drops it unless the expression names `jev_live`.
- COMMAND B's `-m jev_live` deselects that same test, because `tests/retrieve/conftest.py:39-62` drops any `mcp` item unless the expression names `mcp`.

Verified by collection: A collects 11/11; B collects 8 of 9 with exactly that one case deselected; `-m "mcp or jev_live"` collects it. B2 exists only to close the gap, so the Jev leg is proven at 9 of 9 rather than the 8 the plan command reaches.

### Live-coverage caveat inside the 11 MCP tests

`tests/retrieve/test_live_mcp_t5.py:45` `test_degraded_empty_response_is_named_failure_not_empty_success` uses a local `_DegradedEmptyAdapter` with no subprocess, so it is fake-backed. The 11-passed count overstates genuine live coverage by one; the true live MCP count is 10 of 11.

## Checkpoint DSN proof (silent-pass hazard)

`tests/conftest.py:91-113` `_drop_unreachable_checkpoint_url()` silently pops `FIN_CHECKPOINT_URL` when it cannot connect within 2s. Every Postgres-backed case then degrades to in-memory storage. The DSN was therefore proven live before trusting COMMAND D:

- `pg_isready` inside `fin-pre330-pg`, `fin-pre330-iknow-pg`, `fin-pre330-ledger` all report accepting connections.
- `SELECT 1` through each DSN returned 1 in 0.01–0.06s, far inside the 2s pop window.
- The failing first COMMAND D run printed its `clean_tables` fixture as `postgresql://***:***@127.0.0.1:55480/pre330`, proving the DSN was **not** popped and real Postgres was exercised.

Observed degradation behaviour, measured rather than assumed: with the DSN repointed to a dead port, `psycopg.connect` raised `OperationalError` in 0.00s and the same pair reported **9 passed, 14 skipped, exit 0** instead of 23 passed. The hazard is therefore real but manifests as a reduced pass count plus 14 *named* skips, not as an unchanged green run. A reader scanning only for `passed` or for the zero exit code would still misread it as green, which is why the explicit DSN proof above is recorded.

## Independent expected values versus observed

| Case | Expected independently of the test's own assertion | Observed |
| --- | --- | --- |
| A (7-file MCP) | 11 collected, exit 0, per `docs/validation/2026-09-30-pre330-integration.md:77` "11 passed" | 11 passed, exit 0 |
| B + B2 (Jev) | 9 live round-trips against the real vendor; historical record reached only 8 | 8 + 1 = 9 passed, exit 0; no `jev_live_key_missing` failure, so the real credential was used |
| C (PG/ES/Neo4j) | 12 passed, exit 0, per `docs/validation/2026-10-02-pr339-repairs.md:31` | 12 passed, exit 0; ES answered on 59200 with 0 shards left, so per-run indices were dropped |
| D (checkpoint pair) | 23 passed, exit 0, per `docs/validation/2026-10-02-pr339-repairs.md:32` | 23 passed, exit 0 after the dependency fix below |

### The one real failure and its cause

COMMAND D first exited 1 with 2 failed / 21 passed, both failures in `test_original_bytes_are_read_by_a_fresh_process_from_the_configured_store` (`binary-pdf-35324-bytes`, `real-filing-markdown-57547-bytes`). The child process traceback was `fin.corpus.object_store_errors.ObjectStoreUnavailableError: boto3 is not installed`.

This is an environment gap in this worktree's virtualenv, not a code regression. `boto3>=1.43.83` is a declared optional dependency (`pyproject.toml:22-23`, extra `object-store`), is already present in `uv.lock`, and is installed at `1.43.86` in the main checkout's virtualenv. The fix was `uv sync --frozen --extra object-store`, which installed the already-locked extra and left `uv.lock` and `pyproject.toml` unmodified — no lockfile or dependency change was authored. The rerun then matched the recorded 23.

## Choices and residual risk

- **"Relevant PostgreSQL checkpoint cases"** in the T7 acceptance is ambiguous. The pair named as "Two repaired full files against configured checkpoint storage / 23 passed" at `docs/validation/2026-10-02-pr339-repairs.md:32` was used: `tests/corpus/test_ingest_durable_restart.py` (gates on `FIN_CHECKPOINT_URL`) and `tests/corpus/test_ingest_state_store_durable.py`. This is the only checkpoint set with an existing recorded live count, so it is the reading that makes the T7 mapping checkable. A different reading would need naming by ROOT.
- **F1a** is closed for these runs by self-building `e8165a7` and recording the hash, and shown not to have invalidated the 2026-10-02 record. The underlying `is_file()`-only assertion is unchanged and remains a process gap for future runs.
- This is parallel baseline proof. Per the T7 completion clause it is **not** automatically accepted for T10 after SQL or contract changes; ROOT must bind it to the integrated candidate and rerun only affected cases.
- Per-run isolation was structural, so no bucket, index, queue, or checkpoint cleanup was needed; the ES cluster reported 0 shards after the run.
- The six shared containers `fin-pre330-pg`, `fin-pre330-s3`, `fin-pre330-ledger`, `fin-pre330-iknow-pg`, `fin-pre330-neo4j`, `fin-pre330-es` were left untouched, and the sibling producer checkout's dirty `AGENTS.md` was preserved unmodified.
- `.env` was installed into this worktree at mode 0600 (gitignored) so the normal `apply_dotenv` loader could supply the live Jev credential. No credential was invented, printed, or written to a tracked file.
- No CI claim is made: this repository has no `.github/workflows`, so health was not observed and not inferred.
