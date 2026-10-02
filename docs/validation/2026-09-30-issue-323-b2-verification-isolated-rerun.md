# the tracked change (B2) — live attachment graph gate: isolated verification rerun

Date: 2026-09-30. Session: verification worker, B2 only (the tracked change, that pull request).
Scope: `tests/host/test_live_attachment_graph_gate.py` and the fixtures /
process lifecycle it owns. No product code, no `tests/corpus`, no
`pyproject.toml` / `uv.lock` change — `git diff --stat a2e5f65 -- src/ tests/`
is empty for this record's commit.

Purpose: re-establish B2's module-stability acceptance at the PR revision using
**this session's own disposable resources**. The 2026-09-29 record
(`docs/validation/2026-09-29-issue-323-b2-live-gate-isolation.md`) proved the
isolation fix, but it shared the object-store endpoint with another session's
container and recorded that limitation itself; that gap is closed here.

Revision under verification: `a2e5f65fa24062f107fb40c1b35341677fbf3553`
(`test(corpus,host): restore the OCR import-isolation contract and the live
gate`), branch `worktree-issue-323-ocr-live-gate`. No code change was made by
this session — see "One failed invocation before the streak" for why.

## Resources owned by this session

| Resource | Name | Port | Created as |
| --- | --- | --- | --- |
| Postgres 16 (`postgres:16-alpine`) | `i323-b2-pg`, id `0826d5c34f5792e6a15f2ab281fc51d12be587ef22cd186ee85fa70e08298a94` | `127.0.0.1:55455` | `-e POSTGRES_DB=b2root -e POSTGRES_USER=fin -e POSTGRES_PASSWORD=fin … postgres -p 55455` (dev-only local values, not real credentials) |
| SeaweedFS 4.43 S3 gateway (`chrislusf/seaweedfs:4.43`) | `i323-b2-s3`, id `4aad5d162677024eba31ecf7294daf545777cd62c0ada273b2a861c6a815de76` | `127.0.0.1:8466` | `server -s3 -s3.port=8466 -s3.ip.bind=0.0.0.0 -ip=127.0.0.1 -dir=/data`, no S3 auth config |

Ports 55455 and 8466 were confirmed unused before either container started
(`ss -ltn` listed only 53, 5433, 5434, 8333, 41825). Shared resources belonging
to other sessions were never reused, connected to, or stopped:
`fin-sql-business-db` (:5434), `fin-g4-t5-s3` (:8333),
`g4-t5-unit-citation-legacy-removal-postgres-1` (:5433).

Per-run resources, created fresh for each acceptance run:
databases `i323_streak1` / `i323_streak2` / `i323_streak3` (dropped and
recreated immediately before their run) and buckets
`i323-b2-streak1` / `i323-b2-streak2` / `i323-b2-streak3`.

Interpreter: the worktree venv, `boto3 1.43.86` present, so the
`durable_object_store` adapter resolved to the real S3 client rather than
skipping. Every invocation used `uv run --no-sync`; nothing in this session
mutated the environment.

## Command

```bash
FIN_CHECKPOINT_URL=postgresql://fin:fin@127.0.0.1:55455/<run-db> \
FIN_OBJECT_STORE_URL=http://127.0.0.1:8466 \
FIN_OBJECT_STORE_BUCKET=<run-bucket> \
FIN_OBJECT_STORE_ACCESS_KEY=<local-test-value> \
FIN_OBJECT_STORE_SECRET_KEY=<local-test-value> \
uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py
```

## Three consecutive full-module runs at `a2e5f65`

| Run | Database | Bucket | Exit | Result | Wall clock (command total / pytest self-report) |
| --- | --- | --- | --- | --- | --- |
| 1 | `i323_streak1` | `i323-b2-streak1` | 0 | 6 passed, 0 skipped | 38.08s / 36.34s |
| 2 | `i323_streak2` | `i323-b2-streak2` | 0 | 6 passed, 0 skipped | 37.56s / 35.83s |
| 3 | `i323_streak3` | `i323-b2-streak3` | 0 | 6 passed, 0 skipped | 37.69s / 35.85s |

All three ran against real Postgres over a real socket, real S3 over real HTTP,
and real child processes; zero skips, zero failures, zero xfail. These were
within the 34–44s band the 2026-09-29 record measured, so the 60s
`RESUME_DEADLINE_S` holds with the same margin (~6.7x the slowest case).

Concurrency context, recorded because it cuts in favour of the result rather
than against it: the peer session's default suite ran for the whole streak and
exited at 02:36:00, after the third run finished at 02:30:21. This module's
processes, database and bucket never touched that session's resources, and the
isolation held anyway.

## Residue and hygiene, checked after every run

| Run | `fin_t2f_gate*` schemas left | `fin_*` relations left anywhere in the run db | Relations in the run db vs. a fresh empty db | Keys left in that run's bucket | Owned PIDs surviving |
| --- | --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 80 = 80 (baseline) | 0 | none |
| 2 | 0 | 0 | 80 = 80 (baseline) | 0 | none |
| 3 | 0 | 0 | 80 = 80 (baseline) | 0 | none |

- The "anywhere" check is stronger than the record's earlier "in `public`":
  `SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relname LIKE 'fin_%'` → `0` on each run database, so no run wrote
  through an unscoped connection either.
- The 80-relation figure is the baseline of a brand-new empty database on the
  same server, measured with a control (`i323_empty_probe`, never used by any
  run) — i.e. each run database was returned to its as-created state. Every
  minted schema was dropped at teardown.
- Object store: `list_objects_v2` on each run bucket → `KeyCount = 0`. This
  session owned the endpoint, so enumeration was possible here and finds
  nothing; the 2026-09-29 record could not make that claim on a shared endpoint.
- Processes: `ps -eo pid,cmd` captured immediately before and ~2s after each
  run and diffed by PID. The only entry in any capture was the driver's own
  bucket-creation helper (`python -c "import boto3 …"`) before run 1, which had
  already exited. After every run: 0 entries matching `uvicorn`, the child
  source, or this module — no process this module created survived its run. The
  module's only signal is `terminate()` on the `Popen` objects it created; no
  `pkill`, `killall`, `kill -9 -1` or `killpg` was used by the module or by this
  session against anything it did not start.

## One failed invocation before the streak

Before the acceptance streak this session made three exploratory invocations on
databases `i323_run1` / `i323_run2` / `i323_run3` (buckets `i323-b2-run1` /
`-run2` / `-run3`). The results matter only for the second one, which failed:

| When | Database | Result |
| --- | --- | --- |
| 02:19:31 → 02:20:15 | `i323_run1` | 6 passed, 0 skipped, 42.03s |
| 02:20:42 | `i323_run2` | **5 failed, 1 skipped, 13.46s** |
| 02:22:03 → 02:22:48 | `i323_run2` (rerun, same db/bucket) | 6 passed, 0 skipped, 44.71s |
| 02:25:20 → 02:25:59 | `i323_run3` | 6 passed, 0 skipped, 37.14s |

Shape of the failure:

- The durable-byte leg skipped with `pytest.importorskip("boto3")` → "the
  configured byte adapter needs the object-store extra".
- All five boot-dependent cases failed, and the hygiene case failed on its own
  `assert []` ("the registry lost the threads this module created") — meaning
  no case had reached the point of minting a thread. Every case failed at boot,
  simultaneously, in a sixth of the normal module runtime.

Only the tail of that invocation's report was retained (the first two
invocations were piped to `tail`), so the individual tracebacks of the five
cases were not captured; the mechanism below is established by elimination and
by the immediate rerun, not by a captured stack.

Alternatives considered and excluded (by inspection and by the reruns, not by a
captured traceback):

- **Environment mutation.** `boto3 1.43.86` imports fine before and after the
  failed invocation, and `.venv/lib/python3.12/site-packages` carries an
  unchanged mtime of `2026-09-29 21:36` — nothing was added or removed there
  during the run. `uv sync` was never invoked by this session (`--no-sync`).
- **The object store or the database.** Both containers were up and reachable
  throughout (the endpoint answered `head_bucket` on all three buckets before
  and after, and Postgres accepted connections on 55455 the whole time), and the
  invocation immediately afterward on the *same* database and bucket passed 6/6
  with zero skips. A one-line residue count on `i323_run2` right after the
  failure returned 0 for `fin_%` relations in `public`; the paired schema-count
  query was lost to a `psql -c` flag quirk (only the last `-c` runs), so that
  figure is recorded as partial rather than as a full residue proof — the
  acceptance streak below carries the complete per-run checks.
- **Cross-run residue / the B2 job-isolation defect.** That is precisely what
  the schema pinning prevents, and the rerun against the *same* database and
  bucket — the configuration the defect would poison — passed 6/6, 0 skipped.
- **Disk.** `/` (ext4 on `/dev/sdd`, 1007 GB) at 9% used, 878 GB available;
  `/tmp` tmpfs (3.9 GB) at 62% with 1.5 GB free.
- **Port collision inside the module.** `_start_live_server` binds the child's
  listener itself and hands the already-listening socket to the child as an
  inherited fd, so the bind–close–rebind window that a third party could win is
  closed structurally; a stolen port is not available as a cause.
- **The 60s deadline.** The invocation died in 13.46s; no case reached a
  deadline.

Remaining cause, consistent with all six outcomes at once: a transient
host-resource exhaustion event. The peer session's default suite
(`uv run --no-sync pytest -q`, ~4650 tests, pid 2391712) was in its heaviest
phase on the same 4-CPU / 7.8 GB machine — `/proc/loadavg` 5.46–5.62,
`free` reporting 0 GB free with tmpfs holding 2.4 GB of that RAM. Booting eight
real child processes on top of that makes `fork` + a heavy import the two
things most likely to fail, and both failing explains the skip and the five
simultaneous boot failures. No OOM-killer line exists in `dmesg` for that
window, so the allocation failed in-process rather than via a kill.

Outcome: **no code change.** Nothing inside this module's scope — schema
pinning, boot DDL ordering, port inheritance, per-test `$HOME`, cleanup
context manager, resume deadline — was shown to be wrong by this invocation,
and the module passed on every subsequent invocation while the same concurrent
load continued (02:22:48, 02:25:59, then the three-run streak, all with the peer
suite still running). Raising `RESUME_DEADLINE_S`, adding a skip, or relaxing an
assertion would have disguised a host-capacity event as a fix; the 2026-09-29
record reached the same conclusion about the original flake ("not reproducible
on demand"), and it was not reproducible here either, in either direction.

## Same-revision runs outside the acceptance set

See the exploratory table above: three further full-module invocations at
`a2e5f65` (02:20:15, 02:22:48, 02:25:59) all passed with zero skips. Together
with the acceptance streak, the module passed 6 of the 7 invocations this
session made at this revision, and the one failure was not reproducible on the
same resources.

## Cleanup

Both containers were stopped and removed by this session at the end:
`docker stop i323-b2-pg i323-b2-s3 && docker rm i323-b2-pg i323-b2-s3`.
Proof after teardown: `docker ps -a` lists no `i323-*` container, `ss -ltn` no
longer lists 55455 or 8466, and the surviving Postgres/S3 containers are exactly
the other sessions' `fin-sql-business-db`, `g4-t5-unit-citation-legacy-removal-postgres-1`
and `fin-g4-t5-s3`, untouched. No process started by this session survives; the
per-run databases and buckets died with the containers that held them.

## Verdict

B2's acceptance is **met** at `a2e5f65`: three consecutive full-module runs at
one revision, each against its own disposable Postgres database and its own
object-store bucket on this session's own endpoint and ports, zero skips, all
cases passing, no cross-run rows or objects, no surviving owned processes.

Not claimed: model realism (the module's own scripted OpenAI-compatible
transport) and retrieve realism (`FakeRetrievePort`) — both pre-existing,
documented substitutions, unchanged here. B3's browser journey belongs to
the tracked change. The default hermetic suite was not re-run by this session; it is
the peer session's scope and was still executing in this worktree at the time
of writing.

## Post-repair single-run check (repair worker, cluster U3 — 2026-09-30)

Scope: two disclosure-only edits to
`tests/host/test_live_attachment_graph_gate.py`, behaviour-preserving — no
assertion, timing/deadline, or fixture change:

- **L1** — the module docstring asserted the database and bucket "are SHARED,
  non-disposable, and outlive any one run". That contradicted this record's own
  acceptance runs, which used per-run disposable databases/buckets on
  session-owned containers. Reworded to: the operator-supplied DB/bucket *may*
  be shared and outlive a run (which is why schema pinning exists), and
  acceptance runs additionally used per-run disposable databases/buckets.
- **L2** — the acceptance clause "Record process IDs and prove all processes
  created by the test are stopped" was met at methodology level only. The
  hygiene case now enumerates every child PID it created with the exit status
  its `Popen` reports and prints that list, so the gated run's own output
  literally carries the id list (the `poll()` result is captured once and reused
  by the surviving-PID assertion, so record and proof cannot diverge).

This is one full-module run at the post-repair revision, not a re-verification
of the three-run streak above (which stands as recorded). Session-owned
disposable resources, unique ports: Postgres `postgres:16-alpine` container
`u3-b-pg` on `127.0.0.1:55466` (dev-only user/password `fin`, database
`u3_postrepair`), SeaweedFS `chrislusf/seaweedfs:4.43` container `u3-b-s3` on
`127.0.0.1:8477` (bucket `u3-b2-postrepair`, local-test access/secret values, no
real credentials). Interpreter: worktree venv, `boto3 1.43.86` present, so
`durable_object_store` resolved to the real S3 client rather than skipping.

| Exit | Result | Wall clock (command total / pytest self-report) |
| --- | --- | --- |
| 0 | 6 passed, 0 skipped | 40.0s / 38.64s |

The hygiene case printed: `live-gate children created by this run (7): pid=…
exit=-15, … (x7)` — all seven SIGTERMed children recorded and none still
running.

Residue and hygiene checked after the run: `fin_t2f_gate%` schemas left = 0;
`fin_%` relations left anywhere in `u3_postrepair` = 0; bucket `list_objects_v2`
`KeyCount = 0`; `ps` diff before/after the run showed 0 `uvicorn` /
`FIN_LIVE_MODEL` command-line entries surviving. No shared container belonging
to another session was touched.

Cleanup after the check: `docker stop u3-b-pg u3-b-s3 && docker rm u3-b-pg
u3-b-s3`; the per-run database and bucket died with their containers. Proof in
the "Cleanup proof" of the repair commit: neither `u3-b-*` container remains and
ports 55466 / 8477 are released.

Verdict: the two disclosure edits are behaviour-preserving and the module still
passes 6/6 with zero skips against real Postgres, real S3, and real child
processes on this worker's own disposable resources.
