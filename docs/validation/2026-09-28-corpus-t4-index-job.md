# Validation: corpus T4 — queue and report the iknow index job separately

**Ticket:** `plans/permanent-corpus-upload.md` T4 (queue and report iknow-memory graph indexing separately), branch `worktree-feat-corpus-t4-index-job`.
**Code under test:** `eee7cb6` (T4 delivery); base `0cf633f` (T3).
**Spec basis:** `specs/corpus-serve-ingest.md` "Graph/C readiness" (lines 167-173) + Success Criterion 8; sibling `../../iknow-memory/specs/corpus-index-job-orchestration.md` / `specs/mcp-tool-surface.md` §Index-job tool contract (referenced, not restated).

## What was delivered (one line per T4 acceptance clause)

- Submit/join after push, never inline: the worker hands off each `(source_ref, content_identity)` pair read back off the `cataloging→ready` audit edge through `MCPIndexJobPort.submit_index_job` (`src/fin/retrieve/mcp_index_job_port.py`, `src/fin/corpus/index_job.py` `IndexJobTracker.submit_after_push`); the HTTP accept path runs zero graph extraction (`_submit_index_jobs` lives only in `IngestWorker`).
- Separate readiness in status/UI: `GET /api/corpus/ingest/{job_id}` carries a distinct graph/C sub-object pass-through of the polled iknow shape (job row and `community_index_state` answered as separate fields; pinned live in `test_poll_answers_job_row_and_community_state_as_separate_fields`); the web status line renders it (`web/src/uploadDoors.test.ts` §"T4: the pass-through graph/C report shapes").
- Graph/C-ready only on iknow confirmation: no path derives `ready` from push success or job state — a never-polled / fully-refused document reports `community_index_state=None`, never a guessed `ready` (`IndexJobTracker.graph_status`, committed tracker cases inside A1/A5).
- Typed failures, never empty success: an unavailable/stale/malformed submit or poll records a per-pair named code in the durable ledger and surfaces as `IndexJobSubmitError` / logged worker failure while the landed text state stays `ready` (`test_submit_failure_keeps_ready_and_records_per_pair`, live `test_submit_with_a_wrong_identity_is_refused_by_name`, `test_poll_of_an_unknown_job_id_raises_the_typed_unknown_job`).
- Idempotent join + restart resume: duplicate submit joins the same durable job (live `test_duplicate_submit_joins_the_same_durable_job` against real iknow Postgres); resume over the same path-backed `IndexJobLedger` re-submits a ready-but-untracked job without re-pushing and sweeps to a network no-op once entries exist (`test_index_job_wiring.py`, 8 committed cases).
- Thread attachments never trigger the company index job: attachment door stays on its own path (A1 host suite + `uploadDoors` 41 web cases).

## Environment recipe (values redacted)

```text
set -a; . /home/winner/projects/fin/.env; set +a     # FIN_CHECKPOINT_URL only (Postgres on localhost:5433)
FIN_OBJECT_STORE_URL=http://127.0.0.1:8333           # SeaweedFS (host `weed server -s3`, pid 229209)
FIN_OBJECT_STORE_BUCKET=fin-g4-t2c                   # pre-existing bucket
FIN_OBJECT_STORE_ACCESS_KEY / _SECRET_KEY            # local throwaway test strings, not operator secrets
IKNOW_NODE=~/.nvm/versions/node/v24.21.0/bin/node    # live MCP legs spawn the real stdio server from /home/winner/projects/iknow-memory/dist/index.js
```

Worktree venv: `uv sync --frozen --extra object-store` (`uv.lock` unchanged). Backend probes before any run: ports 5433 / 5432 / 9200 / 7687 / 8333 all `UP`; `iknow-memory/dist/index.js` present.

## Runs

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| A1 | `uv run pytest -q tests/corpus tests/host/test_workspace_http_ingest.py tests/host/test_ingest_worker_boot.py tests/host/test_ingest_service_wiring.py tests/retrieve/test_index_job_port.py tests/retrieve/test_mcp_index_job_port.py` | 0 | `880 passed, 17 skipped in 444.32s` — the 17 skips are the DSN-/object-store-gated durable cases (skip reason printed: `neither FIN_INGEST_STATE_DSN nor FIN_CHECKPOINT_URL is set`); collected at 03:58 on the clean `eee7cb6` tree |
| A2 | `uv run pytest -q -rs tests/corpus/test_ingest_durable_restart.py tests/corpus/test_ingest_worker_durable.py tests/corpus/test_ingest_state_store_durable.py tests/corpus/test_ingest_storage_selection.py` (DSN + object store set) | 0 | `43 passed in 11.25s`, 0 skipped — real Postgres rows and the raw-byte leg through real SeaweedFS (with the throwaway local credentials above) exercised the same durable seams T4 hands off from |
| A3 | `uv run pytest -q -rs -m mcp tests/retrieve/test_live_index_job_handoff.py tests/retrieve/test_live_content_identity.py tests/retrieve/test_live_durable_worker.py tests/retrieve/test_source_read_alignment.py` (live env, real stdio iknow) | 0 | `13 passed, 15 deselected in 17.74s`, 0 skipped — 6 real-stdio index-job cases (submit answers the persisted row with a durable job_id, duplicate joins, wrong identity refused by name, unknown job typed, poll returns job row + community state as separate fields, invalid locator named), plus 5 content-identity + 1 durable-worker + 1 read-alignment live legs |
| A4 | `cd web && npx tsc --noEmit` then `npx vitest run src/uploadDoors.test.ts` | 0 | `tsc_exit=0` (re-run un-piped for an unambiguous code); `Test Files 1 passed (1) / Tests 41 passed (41)` — includes the graph-line `community_index_state` pass-through shapes |
| A5 | `uv run pytest -q -rs tests/corpus/test_index_job_wiring.py tests/corpus/test_index_job_tracker.py` under the live env, executed from a `git archive eee7cb6` snapshot at `/tmp/t4-eee7cb6` (`PYTHONPATH=/tmp/t4-eee7cb6/src`, worktree venv) | 0 | `27 passed in 0.31s` — committed tracker (19) + wiring (8) cases green with the DSN set. A browser-inclusive UI roundtrip is **Not run** (see below); the graph handoff through the real route + real port is covered by A1 host cases + A3 live cases |

Timeline note (honest attribution): from 04:09:17 a parallel repair agent began **uncommitted** edits to `mcp_index_job_port.py`, `mcp_push_port.py`, `mcp_session.py`, `test_index_job_tracker.py`, `test_index_job_wiring.py` (file mtimes; `git diff` +353/−161). A1–A4 (finished 04:06:01–04:08:15) all ran on the clean `eee7cb6` tree. An in-place A5 attempt on the mid-repair tree failed (`6 failed, 21 passed`, then `7 failed, 20 passed`) — the failures are assertions that exist only in the uncommitted new test lines (e.g. `last_failure_code`) whose `src/fin/corpus/index_job.py` implementation has not landed (`index_job.py` untouched, mtime 03:09:58); they are **not** evidence against `eee7cb6` — hence the archive-snapshot A5 row. Those in-flight changes were subsequently validated on the post-repair tree (see "Review gate" and "Repair re-validation" below).

Review gate: `code-review` on `35ca6bd..eee7cb6` — Standards `1 High / 4 Medium / 3 Low`, Spec `0 High / 3 Medium / 2 Low` → `GATE: BLOCKED` (1 High). Repaired in the follow-up commit by three root-cause clusters:
R1 ledger concurrency + error surface (`High` singleton double-writer → `RLock`-guarded mutate+snapshot+append; `Spec M1` resume predicate now "pairs owe a job_id"; `Spec M2/M3` refused pairs' named error surfaces through `graph_status`; function splits under 60 lines) — RED-before-fix evidence including a deterministic lost-update reproduction (`reloaded {...} != 20 pairs`, 3/3 before fix, 13/13 stable after).
R2 status-route poll-failure hygiene (`Standards L2`): a raising `graph_status` renders `graph_status_failed` beside an empty job list instead of a bare null; `Standards L1` redundant conjuncts removed; `Standards L3` last polled `failure_code` persisted and echoed; `Standards Low dead-constant` → `INDEX_JOBS_FILE` exported as the single filename source.
R3 shared MCP wire (`Standards M2`): handshake frame builders / parse / validate de-duplicated into `fin.retrieve.mcp_session` consumed by push + index-job ports (2 of the 3 flagged copies eliminated; `mcp_adapter` and the pre-existing invalidation copy left per scope guard).
Deferred: `Standards M4` (dict→frozen-dataclass ledger currency) — same-file collision with R1's restructure in the same landing round; recorded as maintainability debt for the next pass. `Spec M3` (this artifact) — delivered by the live run above.

Repair re-validation (post-repair tree): targeted `uv run pytest -q tests/corpus/test_index_job_tracker.py tests/corpus/test_index_job_wiring.py tests/host/test_workspace_http_ingest.py tests/host/test_ingest_worker_boot.py tests/host/test_ingest_service_wiring.py` → `76 passed`, exit 0; real-stdio re-run `uv run pytest -q -m mcp tests/retrieve/test_live_index_job_handoff.py tests/retrieve/test_live_content_identity.py` → `11 passed in 10.22s`, exit 0 (proves the R3 shared-wire bytes against the live server); durable+index legs under DSN + real object store on the post-repair tree `uv run pytest -q -rs tests/corpus/test_ingest_durable_restart.py tests/corpus/test_ingest_worker_durable.py tests/corpus/test_index_job_wiring.py tests/corpus/test_index_job_tracker.py` → `36 passed`, 0 skipped; merge gate `uv run pytest -q` → exit 1 with exactly one failure, `tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory` — `docker info` does not answer in this WSL session, and the identical case fails on the `eee7cb6` snapshot with this diff touching no launcher/script path: pre-existing environment failure, not this round's regression (`4082 passed, 103 skipped, 326 deselected`).

## Cleanup

Verified after live legs: fin ingest tables on 5433 all `0` rows (`fin_ingest_job 0 / fin_ingest_audit 0 / fin_corpus_object_pointer 0` via psycopg counts); no leftover `fin*` schemas on the iknow Postgres 5432 (`iknow schemas: []`); no `fin_*` ES indices (`ES fin indices: []`); bucket `fin-g4-t2c` unchanged at 4 pre-existing `thread-attachments/*` keys from an earlier session (4 before, 4 after — this round's tests self-removed their objects). iknow dev containers + host `weed` left running (machine-normal). `/tmp/t4-eee7cb6` archive snapshot is disposable.

## Not run

- Browser/UI interactive roundtrip (upload → status poll → graph line render) against a live served app: no committed harness exists, and the route+contract+render halves are each pinned (A1 host route cases, A3 live poll case, A4 41 web cases) — a manual path would add presentation-layer evidence only.
- A completed real iknow graph-extraction job reaching confirmed corpus-wide `community_index_state=ready` on live Neo4j: the live suite pins submit/join/refuse/typed-poll semantics and the separate-field pass-through shape, not a finished full-stage graph build.
- Full repo `uv run pytest -q` merge gate: run after the repair landed (see the repair re-validation paragraph: one pre-existing docker-environment failure, everything else green).

## Coverage inventory

| Lane | File / surface | Cases | Run |
| --- | --- | --- | --- |
| hermetic port contract | `tests/retrieve/test_index_job_port.py` | 12 | A1 |
| hermetic MCP-stdio contract (fixture server) | `tests/retrieve/test_mcp_index_job_port.py` | 22 | A1 |
| tracker + ledger | `tests/corpus/test_index_job_tracker.py` | 19 | A1, A5 |
| worker handoff wiring | `tests/corpus/test_index_job_wiring.py` | 8 | A1, A5 |
| route/boot/wiring (incl. graph sub-object, attachment door) | `tests/host/test_workspace_http_ingest.py`, `test_ingest_worker_boot.py`, `test_ingest_service_wiring.py` | within 880 | A1 |
| durable resume seams | `tests/corpus/test_ingest_durable_restart.py` et al. | 43 | A2 |
| live real-stdio index-job handoff | `tests/retrieve/test_live_index_job_handoff.py` | 6 | A3 |
| live content-identity / durable worker / read alignment | three live files | 5 + 1 + 1 | A3 |
| web upload doors + graph line | `web/src/uploadDoors.test.ts` + `tsc --noEmit` | 41 / 0 errors | A4 |

## What this record does NOT prove

- That the repair clusters were validated on the same tree as A1–A5: they were re-run separately (repair re-validation rows above) on the post-repair tree, with the real-stdio MCP re-run covering the R3 wire change.
- End-to-end operator-visible graph/C `ready` on a live completed job (see Not run).
- Live-Neo4j behaviour of `test_mcp_index_job_port.py` cases (fixture-server contract only) and browser-level rendering.
- That `eee7cb6` will survive squash-merge rebases unchanged; the evidence pins this exact tree.
