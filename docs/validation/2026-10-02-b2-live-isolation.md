# B2 live attachment graph gate — current-candidate re-verification (issue #346)

Date: 2026-10-02. Session: worker, task #346 only.

Scope: `tests/host/test_live_attachment_graph_gate.py` and the fixtures /
process lifecycle it owns, per `plans/post321-stability-browser-acceptance.md`
item B2 (lines 30-37). This record does **not** execute B3 / issue #330, does
not touch B1 or C1, and makes no claim about CI health.

## Candidate revision

| Item | Value |
| --- | --- |
| Revision under test | `320ea9f394277d3d6ec342112cf2bbb47270864d` |
| Branch / worktree | `pre330/i346` at `/home/winner/projects/fin-wt-i346` |
| Base | `origin/main` `ecd3ab4` |
| Assembly | Four sequential local merges onto `ecd3ab4`: `75b22b0` (PR #341, `a52185f`), `1aa1c08` (PR #342, `25ed6ac`), `4efa787` (PR #343, `b263123`), `320ea9f` (PR #344, `a5b1f7a`). Zero conflicts. |
| Ancestry | `git merge-base --is-ancestor` returns 0 for all four of `a52185f`, `25ed6ac`, `b263123`, `a5b1f7a` — the combined tree really contains all four PRs. |
| Diff vs base | `git diff --shortstat ecd3ab4 320ea9f` → `15 files changed, 1769 insertions(+), 60 deletions(-)` |

This is the "explicitly identified candidate" the issue requires. `main` was
not merged first and was not modified.

## Environment

| Component | Value |
| --- | --- |
| Interpreter | worktree venv, `uv sync --frozen --extra ocr --extra object-store` → exit 0. `boto3 1.43.86`, `psycopg 3.3.4`, `pytesseract` importable, so the `durable_object_store` adapter resolved to the real S3 client instead of skipping. |
| Lockfile | `git status --short -- pyproject.toml uv.lock` is empty after the sync. `--frozen` held; neither file changed. |
| Config | `/home/winner/projects/fin/.fin/pre330/environment.sh` sourced with `set -a; source …; set +a` and never printed. Verified exported: `FIN_CHECKPOINT_URL`, `FIN_LIVE_IKNOW_DB_URL`, `FIN_LIVE_IKNOW_ES_URL`, `FIN_LIVE_IKNOW_NEO4J_*`, `FIN_OBJECT_STORE_ACCESS_KEY`, `FIN_OBJECT_STORE_BUCKET`, `FIN_OBJECT_STORE_ENDPOINT`, `FIN_OBJECT_STORE_SECRET_KEY`, `FIN_SQL_READER_URL`, `FIN_SQL_DB_CONTAINER`, `FIN_BUSINESS_LEDGER_URL`. |
| Producer checkout | session-owned worktree `/home/winner/projects/iknow-wt-i346` at `e8165a704c7b0e733744c01de77322622e3be03a`, built in place (`npm install` then `npm run build`, `BUILD_EXIT=0`). `sha256sum dist/index.js` → `1beb2c1b550a868b0a0d5a349578ce22c2a469bc15cbb2ab73e6fd23c60510f6`. Selected explicitly with `FIN_LIVE_IKNOW_ROOT=/home/winner/projects/iknow-wt-i346`; the hardcoded default `/home/winner/projects/iknow-memory` (`tests/retrieve/live_iknow_support.py:45`) was never relied on. |
| Shared resources | all six `fin-pre330-*` containers used in place and never stopped, restarted, or reconfigured. No `docker prune`. |
| Sibling checkout | `/home/winner/projects/iknow-memory` left with its pre-existing ` M AGENTS.md` and nothing else. |

### Name discrepancy in the issue's command

The issue names `FIN_OBJECT_STORE_URL`; the protected config exports
`FIN_OBJECT_STORE_ENDPOINT`. The object-store layer reads **either**, in that
precedence order — `src/fin/corpus/object_store.py:326-330` does
`values.get("FIN_OBJECT_STORE_URL") or values.get("FIN_OBJECT_STORE_ENDPOINT")`.
`tests/host/test_live_attachment_graph_gate.py:286` documents both. The config
is therefore honored as-is; the harness additionally set `FIN_OBJECT_STORE_URL`
to the same endpoint value so both spellings resolve identically. **No product
or config change was made for this**, and none is needed — the fallback is the
shipped contract.

### Producer consumption, stated plainly

This B2 module does not load the iknow producer. Its only mention is prose at
`tests/host/test_live_attachment_graph_gate.py:52`; the module uses its own
scripted model transport and `FakeRetrievePort`, a substitution that predates
this change. `FIN_LIVE_IKNOW_ROOT` was still set explicitly as the issue
requires, but these three runs are **not** producer evidence.

## Silent-pass hazard: DSN liveness proved before trusting any run

`tests/conftest.py:91-113` unsets `FIN_CHECKPOINT_URL` when it cannot connect,
which would degrade Postgres-backed cases to in-memory. Proof taken first:

| Probe | Observed |
| --- | --- |
| `docker exec fin-pre330-pg pg_isready -U fin -d pre330` | `/var/run/postgresql:5432 - accepting connections` |
| `SELECT 1` through the protected `FIN_CHECKPOINT_URL` | `(1,)`, connect+query **0.0538 s** |
| Dead DSN, refused port (`127.0.0.1:59299`) | `OperationalError` in **0.199 s**, then `FIN_CHECKPOINT_URL present after pop? False` — the documented pop path |
| Unroutable DSN, real timeout (`10.255.255.1`) | `ConnectionTimeout` in **2.006 s** — the `connect_timeout=2` cap |

Because a dead DSN would surface as **named skips** (the `postgres_url`
fixture's `pytest.skip("FIN_CHECKPOINT_URL is not set…")` at
`tests/host/test_live_attachment_graph_gate.py:229-233`) and never as passes,
and because every run below additionally re-proved `SELECT 1` through its own
per-run DSN immediately before pytest started, no run in this record is a
degraded in-memory pass.

## Isolation actually asserted by B2 (what had to be preserved)

Read from `plans/post321-stability-browser-acceptance.md:33-35` and the
module's own docstring. The universe-level boundary is a **minted schema with
pinned `search_path`**, because `service.recover()` runs in every child and
sweeps every `running` / `parsing` row in the database
(`tests/host/test_live_attachment_graph_gate.py:211-278`). The contract also
requires a disposable unique database and a unique bucket per run, recorded
PIDs, and proof that test-created processes are stopped.

## Three consecutive acceptance runs

Command, identical for all three (the issue's command, with the per-run
resources substituted):

```bash
FIN_CHECKPOINT_URL=postgresql://fin:<redacted>@127.0.0.1:55480/i346_runN \
FIN_OBJECT_STORE_URL="$FIN_OBJECT_STORE_ENDPOINT" \
FIN_OBJECT_STORE_BUCKET=i346-runN \
FIN_LIVE_IKNOW_ROOT=/home/winner/projects/iknow-wt-i346 \
uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py
```

| Run | Database | Bucket | Exit | Result | Wall clock |
| --- | --- | --- | --- | --- | --- |
| 1 | `i346_run1` | `i346-run1` | **0** | **6 passed**, 0 failed, 0 skipped, 0 deselected | 43 s (pytest 38.67 s) |
| 2 | `i346_run2` | `i346-run2` | **0** | **6 passed**, 0 failed, 0 skipped, 0 deselected | 35 s (pytest 33.71 s) |
| 3 | `i346_run3` | `i346-run3` | **0** | **6 passed**, 0 failed, 0 skipped, 0 deselected | 35 s (pytest 33.17 s) |

Counts are the literal pytest terminal lines: `6 passed in 38.67s`,
`6 passed in 33.71s`, `6 passed in 33.17s`. Nothing was suppressed, xfailed,
or filtered.

`--collect-only` under the same marker reports `6 tests collected` and names
all six cases, so "6 passed" is 6 of 6 collected and **0 deselected** in these
invocations:

- `test_a_run_suspended_on_the_gate_completes_under_a_fresh_process`
- `test_the_uploaded_bytes_are_whole_in_the_real_object_store_after_the_sender_dies`
- `test_bytes_that_cannot_be_read_land_as_a_named_refusal_turn`
- `test_a_deleted_attachment_can_never_publish_late_to_its_paused_run`
- `test_two_parallel_retry_doors_join_one_durable_job`
- `test_module_leaves_no_live_state_behind`

The 408 deselections cited in the issue belong to the *default* regression
invocation, which is a different command and was not repeated here.

### Supplementary run (evidence only, not one of the three)

The new child-PID ledger added since the old record is captured by pytest and
hidden without `-s`. One extra run against its own `i346_run4` database and
`i346-run4` bucket, same candidate, `pytest -q -m mcp -s …`, exit **0**,
`6 passed in 34.32s`, printed:

```text
live-gate children created by this run (7): pid=272874 exit=-15, pid=272962 exit=-15,
pid=273095 exit=-15, pid=273193 exit=-15, pid=273426 exit=-15, pid=273514 exit=-15,
pid=273610 exit=-15
```

Seven children started, all seven reaped by SIGTERM (`-15`), none still
running.

## Per-run resource identities and cleanup evidence

| Item | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| Checkpoint DSN | `postgresql://fin:<redacted>@127.0.0.1:55480/i346_run1` | `…/i346_run2` | `…/i346_run3` |
| DSN liveness before pytest | `SELECT 1 → (1,)`, 0.0258 s | `(1,)`, 0.0217 s | `(1,)`, 0.0209 s |
| Baseline in fresh db | 0 `fin_t2f_gate*` schemas, 0 `fin_*` relations | same | same |
| Bucket (endpoint `http://127.0.0.1:8490`, shared `fin-pre330-s3`) | `i346-run1` | `i346-run2` | `i346-run3` |
| Ports owned by the module | 12 new `127.0.0.1` ephemeral ports, e.g. 32965, 33345, 33663, 34487, 34493, 35513, 36235, 39887, 43127, 43635, 45879, 52191 | 12, e.g. 35915, 39629, 39663, 43809, 44063, 44227, 45413, 46491, 50309, 54319, 58229, 60387 | 12, e.g. 33169, 36991, 39029, 39245, 39463, 41839, 45595, 46141, 46239, 49879, 53765, 57455 |
| Surviving `uvicorn` / `fin.host` children | none | none | none |
| `fin_t2f_gate*` schemas left | 0 | 0 | 0 |
| `fin_*` relations left, whole server | 0 | 0 | 0 |
| Objects left in that run's bucket (`list_objects_v2`) | `KeyCount = 0` | `KeyCount = 0` | `KeyCount = 0` |
| Cleanup performed | `delete_bucket(i346-run1)`, `DROP DATABASE i346_run1` | `delete_bucket(i346-run2)`, `DROP DATABASE i346_run2` | `delete_bucket(i346-run3)`, `DROP DATABASE i346_run3` |

The module owns its ports structurally: it binds `("127.0.0.1", 0)`, listens,
marks the socket inheritable, and hands the already-listening descriptor to the
child (`tests/host/test_live_attachment_graph_gate.py:1048-1056`), so the
reservation *is* the server and there is no bind-close-rebind race. Every port
above is OS-assigned and disappears with its run; none is a fixed service port
and none belongs to another session.

**What this session created, and removed.** One venv in its own worktree; one
producer worktree; three disposable databases and three disposable buckets on
the shared containers; one scratch tree `/tmp/i346/`. The databases and buckets
were dropped after their own run. **No container was created, stopped, or
reconfigured** — `docker ps -a | grep i346` returns nothing, and all six
`fin-pre330-*` containers report `Up 16 hours` both before and after. The
`/home/winner/projects/iknow-memory` checkout still shows only its pre-existing
` M AGENTS.md`.

## Reconciliation: what changed since the old B2 record

The old record is `docs/validation/2026-09-29-issue-323-b2-live-gate-isolation.md`
(base `670f65f` + test-only commits) as re-established by
`docs/validation/2026-09-30-issue-323-b2-verification-isolated-rerun.md` at
revision **`a2e5f65`**. That is the comparison point.

`git diff --stat a2e5f65 320ea9f -- tests/host/test_live_attachment_graph_gate.py`
→ `1 file changed, 25 insertions(+), 7 deletions(-)`. That is the **entire**
change to the module since the old record, and it is confined to two places:

1. **Module docstring (lines ~76-105).** Reworded from "the database and the
   bucket **are** SHARED, non-disposable" to "**MAY be** shared …
   the acceptance runs for this module additionally minted a per-run disposable
   database and bucket". This aligns the prose with how the runs were actually
   conducted. It documents the mechanism (`recover()` having no ownership
   predicate) more explicitly than before. It changes no executable line and
   asserts nothing weaker — the schema/`search_path` boundary it describes is
   the same one the fixture still builds. (There is a cosmetic `are are`
   duplication at the line break; a prose typo, not an assertion.)
2. **`test_module_leaves_no_live_state_behind` (lines ~1826-1846).** The
   `still_alive` computation was rewritten from
   `[process.pid for process in registry["children"] if process.poll() is None]`
   to a single snapshot
   `outcomes = [(process.pid, process.poll()) for process in registry["children"]]`
   which is then reused by the assertion **and** printed as a PID ledger.

**Fixtures: unchanged.** `tests/conftest.py`, `tests/host/conftest.py` and
`tests/retrieve/live_iknow_support.py` are all byte-identical between `a2e5f65`
and `320ea9f` (empty `git diff --stat` for each). The `postgres_url` minted
schema, the `PostgresThreadStore(scoped)` boot-ordering DDL, the `_scoped_url`
`search_path` pinning, the function-scoped `live_home`, and the
`durable_object_store` `importorskip` gate are exactly as the old record
described them. The module history is `51b4fe6` → `e36999f` → `ecd3ab4`, and
`ecd3ab4` (already in `main`) is the last commit to touch it — the candidate
itself changed it not at all (`git diff --stat ecd3ab4 320ea9f -- <module>` is
empty).

**Verdict on weakening: the change strengthens, it does not weaken.** In the
old form each child's `poll()` was called exactly once, inside the list
comprehension; in the new form each child's `poll()` is still called exactly
once, in the `outcomes` snapshot, and the assertion
`assert still_alive == []` is unchanged in meaning — a child is "still alive"
iff `poll()` returned `None`. What is new is that the recorded list and the
proof are derived from one shared snapshot, so they cannot diverge (the old
code had a latent read-the-list-twice hazard once a print was added), and the
PID/exit-status ledger becomes part of the gated run's own output rather than
prose methodology. The supplementary run above shows it firing: 7 children,
all `exit=-15`, assertion satisfied. **No isolation assertion was removed,
loosened, converted to a skip, or replaced by an in-memory stand-in.**

The module's real-boundary content is intact: real `subprocess.Popen` child,
real inherited listening socket, real `psycopg` connection, real S3 over real
HTTP, `RESUME_DEADLINE_S = 60.0` still applied at all six of its call sites
(lines 1413, 1439, 1452, 1585, 1594, 1693), `recover()` and the resumer still
unstubbed, and the
only process-wide signal in the file remains `self.process.terminate()` on a
`Popen` the module created — no `pkill`, `killall`, `kill -9 -1`, or `killpg`.

## Conclusion on the question the issue asked

The gap was **missing current-revision evidence, not an isolation defect.** The
only change to the module since the old record strengthens its cleanup
assertion, the fixtures are byte-identical, and the module now passes three
consecutive isolated live runs on the assembled candidate with zero skipped
required cases.

## NEW results vs REUSED historical evidence

**New (measured in this session, on `320ea9f`):** the three-run table; all
per-run resource identities, residue counts and cleanup; the DSN-liveness and
2 s-timeout probes; the assertion/fixture reconciliation against `a2e5f65`;
the PID-ledger output; the `a52185f`/`25ed6ac`/`b263123`/`a5b1f7a` ancestry
proofs; the producer build hash.

**Reused (not re-measured here):** the 2026-09-29 root-cause analysis of the
flake; the 34-44 s module timing band; the 5201-pass default regression; all
B1, C1, C2 and pipeline evidence; the 408 default-suite deselections. Their
revisions are **not** `320ea9f` unless stated, and they are not re-asserted by
this record.

## Changed paths

`docs/validation/2026-10-02-b2-live-isolation.md` (this file) is the only
change in `pre330/i346`. **No product code, no test code, and no `pyproject.toml`
/ `uv.lock` change was made**, so this record carries no code diff and no
failing-first test — no failure occurred to fix.

## Remaining blockers

None for B2 at `320ea9f`. Two boundaries, recorded so they are not mistaken for
B2 results:

1. These runs prove the **candidate's** live gate only. They do not prove B3 /
   issue #330, the real browser journey, real-model date extraction, or CI
   health.
2. The six shared `fin-pre330-*` containers and the `fin-pre330-gate` bucket /
   `pre330` database are shared with other sessions. Isolation here is by
   per-run database, per-run bucket and per-run ephemeral ports; the endpoint
   itself is not exclusively owned, exactly as the old record stated.
