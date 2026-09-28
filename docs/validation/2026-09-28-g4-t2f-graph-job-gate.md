# G4 A.T2f — durable graph job gate: pause, restart, and auto-resume of attachment preparation

Date: 2026-09-28 (all rows of "Commands" were executed on this date at the
content tip `618c039` unless a row names its tree; the live suite ran twice
and both runs are recorded)
Branch: `worktree-t2f-graph-job-gate` (base `main` `35ca6bd`, the merged T2e
tip). Commits, in order: `c0db6db` the T2f implementation snapshot taken for
the first review, `555fdab` the first-round repairs (1 High + the Medium
clusters, listed below), `a8f3c76` the second-round repair (typed race catch),
and `618c039` the live process-boundary suite — the tip every number below was
taken at except where a row says otherwise.

Inherited contract: `specs/thread-attachment-source-read.md` (graph order
through `ensure_attachment_ready`, the wait/resume rule, and the typed exits)
and ADR 0089 (preparation starts only from an eligible grounded turn; the run
waits and resumes itself). Records inherited and exercised here:
[2026-09-26-g4-t2b-raw-upload-send-gate.md](2026-09-26-g4-t2b-raw-upload-send-gate.md)
(raw-only `uploaded`, the live uvicorn harness convention) and
[2026-09-27-g4-t2c-single-attachment-tombstone.md](2026-09-27-g4-t2c-single-attachment-tombstone.md)
(the void-before-cleanup order and the `fin-g4-t2c-s3` byte store this record
reuses), plus
[2026-09-28-g4-t2e-vision-observations-budgets.md](2026-09-28-g4-t2e-vision-observations-budgets.md)
for the observation and budget layer the drain executes.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | `618c039` (branch tip). Diff `35ca6bd..618c039`: 26 files, +8800/−299 — new `src/fin/host/attachment_job_store.py` (624 lines) and `attachment_prep_worker.py` (240), rewritten gate in `graph.py` (+283), send/wait wiring in `workspace_http.py` (+781), `attachment_prepare.py` (+314), `attachment_store.py` (`mark_parsing`), `compile.py`/`http.py` boot wiring, five new host test modules plus the live module, and the web composer/store consumption of the `attachment_wait` frame |
| Python | 3.12.14 (`uv run --no-sync pytest`); default suite stays hermetic |
| Durable metadata | Postgres 16 (`fin-postgres-1`, published `localhost:5433/fin`), `FIN_CHECKPOINT_URL` set in the shell only; the variable NAME is the only identifier used here. Tables exercised: `fin_thread`, `fin_thread_attachment`, `fin_thread_attachment_unit`, `fin_thread_attachment_index`, `fin_thread_attachment_tombstone`, the new `fin_thread_attachment_job`, `fin_run`, and the `PostgresSaver` checkpoint tables |
| Byte store (live suite) | SeaweedFS S3 gateway 4.43, container `fin-g4-t2c-s3` (started for the T2c record, still serving `127.0.0.1:8333`), bucket `fin-g4-t2c` (`head_bucket` 200 before the run; four pre-existing keys from that record untouched by these cases and removed again by their own threads' cleanup paths — the run added none that survived), `S3CompatibleObjectStore` through the product's own `create_object_store()`; local throwaway test credentials exported in the shell only, never written to a tracked file |
| Model transport (live suite) | no provider credential: the child builds the product's own `ChatOpenAI` via `_build_default_model` with `env={}` and points its `base_url` at a scripted OpenAI-compatible server inside the test; the scripted server records every request, which is how "the paused run never reached the model" is measured |
| Gold data | committed `data/corpus/g4_attachment_gold/g4-image-chart-001.png` (undecodable-as-text leg), committed text fixture `data/corpus/dataset/t1/compliance-001.md`, committed `data/corpus/manifest.json` + `extracted/` (the scripted leaf's `read_file` body and the citation-gate span) |
| Web | `web/` `npx tsc --noEmit` clean; `npx vitest run` 391 passed / 26 files, re-run at this tip (`uploadDoors.test.ts` carries the composer's `attachment_wait`/`uploaded`-vs-`ready` cases) |

## What was claimed, and the evidence for each clause

Acceptance clauses are the plan's T2f item
(`plans/thread-attachment-source-read.md`:118).

| Clause | Evidence | Actual result |
| --- | --- | --- |
| durable job keyed by attachment content version | `attachment_job_store` PK `(attachment_id, content_sha256)`; `test_attachment_job_store.py` (idempotent enqueue join, CAS claim `FOR UPDATE SKIP LOCKED`, reopen rule, `requeue_running`, `forget`), Postgres twin `test_attachment_job_store_postgres.py`; live case 1 read the job row over a second SQL connection and pinned `content_sha256` to the payload's own SHA-256; live case 6 pinned `target_version = 1` | hermetic twins + Postgres twin pass; live: exactly one `pending`/`running` row per content version, hash equal |
| job invokes page parsing, vision, indexing; `ready` only when all are usable | `test_attachment_prepare_jobs.py` — drain publishes units+index then flips `ready`; a drain that reaches the terminal else without usable data lands `attachment_index_unavailable` with `searchable False` and zero units, never a `ready` row; succeeded→damaged→re-enqueue reopens and re-publishes | 600-line suite passes; no path writes `ready` with missing units/postings |
| pending graph work persists `run_id` + wait state, releases the thread lock, resumes itself after completion **or process restart**, no resend | graph `interrupt({"kind":"attachment_preparation", ...})` on `PostgresSaver`; `test_attachment_send_wait.py` (frame + lock release), `test_attachment_resume_worker.py` (sweep resumes the same run id, idempotent), `test_attachment_jobs_boot_durable_postgres.py` (claim survives a second store instance); SC2 chain `test_suspended_run_and_durable_job_resume_over_a_fresh_postgres_process` (real Postgres checkpoint, honest connection death via `stack.close()`, fully rebuilt process B, `recover()` re-adopts the orphaned `running` claim exactly once, same run id completes, exactly one turn row, re-sweep adds none); live cases 1+2 run the same chain across two real uvicorn processes with a SIGTERM between | SC2 chain: 1 passed on real Postgres; live case 1: server-minted run id survived the kill, the fresh process produced the turn row `done` with the extractive answer, one turn row on the thread, and the scripted transport logged exactly 2 model calls (the resumed remainder only) — a resend would have minted a second run id |
| ineligible attachments do not trigger work | `test_graph_attachment_gate.py`: non-grounded and no-attachment turns never call the service; `attachment_service=None` keeps today's routing and topology exactly; edges `compaction_gate → ensure_attachment_ready → model` asserted | passes |
| concurrent triggers/retries join one active job/version | enqueue-join tests in `test_attachment_job_store.py`; `_ask_or_join` is the single work-asking block for both doors; live case 5 fired two simultaneous real retry POSTs at a process with no drain, then a third sequentially | live: `[200, 200]` with `status "parsing"`, one `pending` job row, third trigger joined it — still one row |
| tombstones prevent late publication | late-result version/tombstone checks in `test_attachment_prepare_jobs.py`; single-attachment delete retires the job row; live case 4 deleted while the run was suspended, then started a fresh process with the drain + resumer running | live: job rows `[]` at deletion, every read door answered `attachment_not_found`, the restarted resumer drove the SAME suspended run to `refuse / unknown_locator` with an empty answer and zero model calls, and units/postings stayed `(0, 0)` — nothing resurrected |
| typed exits, never empty-success or stale `ready` | live case 3: undecodable bytes named `.txt` → upload accepted raw, job failed `attachment_not_text`, turn row `refuse` carrying that reason, attachment row `failed`, `indexed False`, zero derived rows, no `done` row on the thread, model never called; hermetic budget legs (`attachment_page_limit_exceeded`, `attachment_vision_budget_exceeded`) and `attachment_upload_in_progress` send-gate cases in `test_attachment_send_wait.py` / `test_attachment_prepare_door.py` | passes; the refusal is a structured code read off the job row, not prose |
| raw bytes cross the process boundary | live case 2: after the sending process died, a fresh process (`create_object_store()` against the real SeaweedFS) fetched the stored key byte-for-byte and sha256-for-sha256; key shape pinned to `thread-attachments/{thread_id}/{attachment_id}` | 0-exit read-back child, `{"ok": true, size/sha equal}` |

## Commands

| # | Command | Exit | Actual output |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/host/test_graph_attachment_gate.py tests/host/test_attachment_prepare_jobs.py tests/host/test_attachment_resume_worker.py` | 0 | `61 passed, 1 skipped` — the skip is the `FIN_CHECKPOINT_URL` opt-in twin, executed in row 3 |
| 2 | `uv run --no-sync pytest -q` (default hermetic suite) | 1 | `1 failed, 4103 passed, 111 skipped, 326 deselected in 917.50s` — the single failure is the docker-permission launcher case named in "Known environment limits"; it fails identically on a clean tree. No T2f-touched test failed |
| 3 | `FIN_CHECKPOINT_URL=… uv run --no-sync pytest -q tests/host/test_attachment_resume_durable_postgres.py tests/host/test_attachment_prepare_jobs.py` | 0 | `29 passed` — the Postgres twins ran (no skips reported), including the SC2 chain test and the gate's stored `parsing` transition |
| 4 | `FIN_CHECKPOINT_URL=… … -v tests/host/test_attachment_resume_durable_postgres.py` | 0 | `1 passed` — the restart-chain case itself |
| 5 | `FIN_CHECKPOINT_URL=… uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py` WITHOUT `FIN_OBJECT_STORE_*` | 1 | first run failed exactly where the contract predicts: the cross-process byte leg rode the degraded in-process store, so the fresh finisher answered `refuse / source_unavailable` instead of `done`. Named here because it is the negative control: without a durable byte store the gate refuses rather than faking success |
| 6 | same command WITH `FIN_OBJECT_STORE_URL=http://127.0.0.1:8333 FIN_OBJECT_STORE_BUCKET=fin-g4-t2c` (+ throwaway local credentials, env names only) | 0 | `6 passed in 35.88s` (first configured run, tree `555fdab` + live module) |
| 7 | same as row 6, re-run on the final tree (`a8f3c76` race catch present) | 0 | `6 passed in 34.37s` — the six per-case results are the clause table above |
| 8 | `uv run --no-sync pytest -q tests/host --deselect …launcher…` | 0 | `2508 passed, 94 skipped, 248 deselected` |
| 9 | `cd web && npx tsc --noEmit && npx vitest run` | 0 | typecheck clean; `391 passed (391)` |

## Known environment limits, named not hidden

- `tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory`
  fails in this session because the shell user is not in the `docker` group
  (`getent group docker` lists no members; the daemon itself answers). The
  same command fails identically on a clean tree (`git stash -u` re-run), so
  it is an environment condition of this run, not a T2f regression; every
  other `tests/host` case ran in row 8.
- The live suite substitutes exactly two boundaries, both disclosed in the
  module docstring: the model transport (no provider credential exists on this
  host; scripted OpenAI-compatible server, real `ChatOpenAI`) and the evidence
  source for the grounded leaf (iknow-memory is not deployed here; the
  committed extracted tree is served through the factory's own documented
  seams). Everything under T2f's claim — process, socket, Postgres stores,
  checkpoint, gate node, `attachment_wait` frame, drain, resumer, object
  store, citation gate — is the real one.
- Scope split against the second review round's Spec Low: the SC2
  `test_attachment_resume_durable_postgres.py` chain crosses the process
  boundary in SQL for checkpoint/job/attachment-unit state while raw bytes
  ride a shared in-memory store inside that one test; the real byte boundary
  for the same pause/resume story is live case 2 (row 7), and the visual
  products' restart leg is the T2e record's own gold evidence. No single test
  claims all three at once.

## Review rounds

- Round 1, pinned `35ca6bd..c0db6db`: Standards 1 High / 5 Medium / 6 Low,
  Spec 0 High / 1 Medium / 2 Low → `GATE: BLOCKED` (1 High: the service module
  reached into the store's private lock and copied its liveness SQL — a dual
  source of truth). Repaired in `555fdab` by root-cause cluster: a public
  `mark_parsing` on the store Protocol with both twins owning their liveness
  SQL; one `_ask_or_join` behind both doors; consumer Protocols replacing
  `Any`+`getattr` at the graph seam; one `PeriodicDrainWorker` replacing the
  third hand-rolled singleton loop; `EXIT:` markers; the web store consuming
  the `attachment_wait` frame instead of a heuristic.
- Round 2, pinned `c0db6db..555fdab`: Standards 0 High / 1 Medium / 3 Low,
  Spec 0 High / 1 Medium / 2 Low. The Standards Medium and the Spec Low-2
  shared one root — the repair replaced a private-attribute ladder with a
  class-NAME frozenset catch — and were fixed in `a8f3c76`: the typed pair is
  resolved lazily on the exception path (a module-level import is illegal:
  `attachment_prepare → attachment_budgets → attachment_vision → compile →
  graph`) and matched with `isinstance`; the gate test raises the real
  product classes. The Spec Medium (the opt-in Postgres chain was skipped, not
  run) is closed by rows 3–4 and 7. Deferred Lows, each with its reason:
  the two test-only wrapper functions (`_drain_attachment_queue`,
  `_check_attachment_resume_waits`) stay until a test-wiring ticket retargets
  them to the shipped lambdas; the liveness subquery's fourth inline copy
  matches the store's existing style and moves with the next store change; the
  unified loop's log-word drift ("drain failed") is not grepped anywhere.
- Gate after round-2 repair: no unresolved High on either axis; the one
  remaining Medium pair was fixed at the same root, so `GATE: PASS` stands for
  the landing round at `618c039`.
