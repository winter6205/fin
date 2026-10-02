# Issue 323 (B2) — the live attachment graph gate, isolated per run

Date: 2026-09-29. Every "Commands" row ran on this branch on this date.

Scope reference: `plans/post321-stability-browser-acceptance.md:32-39` (item
B2) and the L2 row it inherits from
`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md:144-149`.
This record discharges B2's module-stability acceptance. It does not touch
B3's browser journey, which belongs to issue #330.

Branch: `worktree-issue-323-ocr-live-gate` (base `670f65f`).

## Environment / dependency version

| Component | Value |
| --- | --- |
| Code under test | `670f65f` plus this record's test-only commits. `src/` and `pyproject.toml`/`uv.lock` are **unmodified** — `git diff --stat -- src/` is empty; the deliverable touches `tests/host/test_live_attachment_graph_gate.py` only |
| Postgres | 16-alpine, disposable container owned by this session, published on `127.0.0.1:55433`; credentials are the dev-only `fin:fin` already committed in `docker-compose.yml` |
| Object store | S3-compatible endpoint on `127.0.0.1:8333` (SeaweedFS 4.43), reached over real HTTP. This endpoint is **shared with another session's container**, so isolation is by bucket name, not by endpoint. Buckets `i323-run1`/`i323-run2`/`i323-run3` were created for this work and contain no other session's data |
| Extras | `boto3 1.43.86` via `uv sync --frozen --extra object-store --extra ocr` (exit 0); `durable_object_store` `importorskip("boto3")` therefore resolved |
| Model transport | local scripted OpenAI-compatible server, per the module's own documented substitution; no credential is read |
| Corpus | committed `data/corpus/g4_attachment_gold/` manifest and extracted tree; the grounded leaf reads it through `FakeRetrievePort` |
| Gold / committed dataset | none applies to this item — the module's own six cases are the acceptance set, and they are the real-boundary evidence |

Every run below used a **fresh database** (`i323_runN`) and a **fresh
bucket**, so no run can observe another's rows or objects.

## Root cause

The module was flaky in 2 of 3 runs, with a different case failing each time
and every failure passing in isolation. The mechanism is a job-isolation
defect, not a timing defect.

`service.recover()` is global and runs in **every** child. `_CHILD_SOURCE`
builds the service unconditionally, *before* the `if WORKERS:` guard, and
`_build_default_attachment_service` ends in `service.recover()`. That sweep
has no thread or attachment predicate: `requeue_running()` issues a bare
`UPDATE fin_thread_attachment_job SET state='pending' WHERE state='running'`
over the whole database, and `store.incomplete()` prepares every
`status='parsing'` attachment row in it. So a child the harness describes as
"real but deliberately un-executing" was in fact sweeping the entire shared
database at construction — the `workers=False` posture was a fiction.

Compounding it, the database and bucket were not per-run disposable and
`_cleanup_live` removed only the ids that run minted. Residue from any prior
partial run was therefore requeued and claimed by the next child's boot,
which is what made the failing case differ each time. The repository had
already proven this exact class in the L1 record: two orphaned `uvicorn`
children sharing one unscoped job table made the sibling upload-door module
fail 3/3, and passed once they were removed.

## The fix

Isolation is applied at the **universe** level rather than by changing
recovery semantics. `postgres_url` mints a schema for the run and pins the
DSN's `search_path` to it, so every unqualified statement the shipped code
issues lands in a namespace holding only this run's rows. `recover()`, the
claim, the drain and the doors run **unmodified and unstubbed** — the sweep
still happens, it simply cannot reach anything but this run's own work. No
production code changed.

A minted schema starts empty, which exposed a real ordering dependency:
`PostgresAttachmentStore` creates its own tables but references `fin_thread`
without creating it, and raises `AttachmentStoreError` when that table is
absent. On the shared database `fin_thread` already exists from an earlier
run, so the dependency was satisfied by accident; a fresh schema has no such
luck and every child failed at boot with
`(500, {'error': 'attachment_store_failed'})`. The fixture now runs the
thread store's idempotent DDL inside the minted schema first, which is the
ordering the product's own builder documents.

Alongside that: `live_home` is function-scoped so the eight children no
longer share one `$HOME/.fin/default`; the hygiene case now asserts those
homes are gone (a new assertion — it previously never inspected the
workspace directory); cleanup runs through a context manager that preserves
the real failure instead of raising from a `finally`; and the uvicorn port
is handed to the child as an inherited, already-listening socket, which
removes the bind-close-rebind race structurally rather than retrying it.

## What was claimed, and the evidence for each clause

| Acceptance clause | Status | Evidence |
| --- | --- | --- |
| Identify and correct the demonstrated cause | met | Root cause above; the boot-ordering defect was found by running the module, not by inspection, and is fixed at `tests/host/test_live_attachment_graph_gate.py` in the `postgres_url` fixture |
| Real process, socket, Postgres, durable object store, pause/restart/resume, cleanup assertions retained | met | No mock/patch/stub/fake added; all six cases still assert what they asserted at `670f65f`; `recover()` and the resumer are unstubbed |
| Three consecutive full-module runs, unique database and bucket per run, zero skips | met | Runs table below |
| Record PIDs; prove test-created processes are stopped | met | PID evidence below; no broad kill exists in the module |
| No cross-run rows or objects left behind | met | Residue evidence below |
| Cleanup terminates only processes the test created | met | The module signals only its own `Popen` objects; `pkill`/`killall`/`kill -9 -1` appear nowhere |

## Commands

All runs:

```bash
FIN_CHECKPOINT_URL=postgresql://fin:fin@127.0.0.1:55433/i323_runN \
FIN_OBJECT_STORE_URL=http://127.0.0.1:8333 \
FIN_OBJECT_STORE_BUCKET=i323-runN \
FIN_OBJECT_STORE_ACCESS_KEY=<anonymous> FIN_OBJECT_STORE_SECRET_KEY=<anonymous> \
uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py
```

| Run | Database | Bucket | Exit | Result | Wall clock |
| --- | --- | --- | --- | --- | --- |
| 1 | `i323_run1` | `i323-run1` | 0 | 6 passed, 0 skipped | 40.95s |
| 2 | `i323_run2` | `i323-run2` | 0 | 6 passed, 0 skipped | 38.34s |
| 3 | `i323_run3` | `i323-run3` | 0 | 6 passed, 0 skipped | 34.36s |

The three acceptance runs above were made against the fix as first
committed. The resume deadline was subsequently **re-tightened** from 300s to
60s (the original 120s budget was unsupported by measurement), and the runs
below re-establish three consecutive passes at the revision that is actually
delivered:

| Run | Database | Exit | Result | Wall clock |
| --- | --- | --- | --- | --- |
| 4 | `i323_run4` | 0 | 6 passed, 0 skipped | 37.99s |
| 5 | `i323_run5` | 0 | 6 passed, 0 skipped | 43.83s |
| 6 | `i323_run6` | 0 | 6 passed, 0 skipped | 38.95s |
| 7 | `i323_run7` | 0 | 6 passed, 0 skipped | 36.27s |

Per-case duration, measured with `--durations=10` at the tightened
deadline (exit 0, 6 passed in 35.55s):

| Case | Seconds |
| --- | --- |
| `test_a_run_suspended_on_the_gate_completes_under_a_fresh_process` | 8.88 |
| `test_a_deleted_attachment_can_never_publish_late_to_its_paused_run` | 8.14 |
| `test_bytes_that_cannot_be_read_land_as_a_named_refusal_turn` | 7.73 |
| `test_two_parallel_retry_doors_join_one_durable_job` | 4.33 |
| `test_the_uploaded_bytes_are_whole_in_the_real_object_store_after_the_sender_dies` | 4.23 |

The 60s deadline is roughly 6.7x the slowest observed case, and the whole
module settles in 34-41s. This measurement is the justification the earlier
300s value lacked.

## PID and residue evidence

Child processes were observed live during a run (a `uvicorn` child of this
module, ~4s old at inspection) and none survived their run. After the runs,
`ps` shows no surviving `uvicorn` / `fin.host` child of this module. The only
process-wide kill in the module is `self.process.terminate()` on a `Popen`
it created; no `pkill`, `killall`, `kill -9 -1`, `killpg` or `os.kill` on a
group exists anywhere in the file.

Residue, checked after the runs on the disposable databases:

- `SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'fin_t2f_gate%'` →
  `0` on every database used (`i323_run1` through `i323_run7`); every minted
  schema was dropped at teardown.
- `SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace
  AND relname LIKE 'fin_%'` → `0`. No run wrote through an unscoped
  connection; the pinning held for the whole run.
- Object store: each run's keys are addressed by an `object_key` read from a
  row, and the adapter exposes no enumeration API, so no run can observe or
  orphan another run's objects. The three buckets were created for this work
  and are removed at the end of the session.

## Regression check on the hermetic suite

`uv run --no-sync pytest -q` → exit 0, 4647 passed, 116 skipped, 352
deselected. The pre-existing L3 failure recorded at
`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md:483` is
discharged by the sibling B1 record in this same branch.

## Limitations / blocked evidence

- The original flake was not reproduced before the fix. The L2 record states
  it was not reproducible on demand, and it was not here either. The causes
  above are fixed on the strength of the mechanism analysis plus the boot
  failure that running the module did surface, not on a captured pre-fix
  flake.
- The object-store endpoint is shared with another session's container. This
  record's isolation claim is therefore about the database schema and about
  the bucket being unreachable-by-addressing, not about endpoint exclusivity.
  A fully isolated endpoint would need an S3 container this session was not
  permitted to start.
- The model transport is the module's own documented local scripted server,
  and the grounded leaf reads the committed corpus through `FakeRetrievePort`.
  These substitutions predate this change and are unchanged by it; this
  record does not claim model or retrieve realism.
