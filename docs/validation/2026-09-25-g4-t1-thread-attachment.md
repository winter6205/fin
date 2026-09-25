# G4 T1 durable text attachment — fin (2026-09-25)

Validation record for plan [`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T1 (Durable text attachment through a thread)** on branch
`feat/g4-t1-durable-text-attachment`.

Identity of what was measured:

- Base (spec/ADR/docs only): `7a30e84`.
- Implementation commits under review: `b1a97bf` (host persistence), `19f9b27` (web status surface).
- This record is written after the dual-axis code review and its repair round;
  the repair is `ead5e26`.
- The tree the closing runs (8–15) measured is the post-merge tip `9bbcae2`,
  not the pre-merge tip; see *PR base state* for why the merge was re-verified
  instead of trusted.
- Worktree: `/home/winner/projects/fin-wt-g4-t1`.

Scope honesty: T1 owns the durable text-attachment slice only. Classified
multi-format preparation (T2), thread-bound retrieval selection (T3), typed
bounded `read_source` (T4), and `unit` citations plus legacy `page` removal
(T5) are **not** delivered here, so spec Success Criteria 5–12 and the SC12
committed G4 gold set are not closed by this record.

## Commands and results

Default-suite runs on this branch are hermetic except for the `$HOME` leak
tracked as B6 — three `test_clarify_prompt.py` cases read the operator's real
`~/.fin/user.md`. That fix shipped as PR #299 and is now on `main` (`eeafbe1`);
it was re-confirmed live on 2026-09-25 as run 27 below. The
Postgres-backed runs at 1 and 16 used a
throwaway container started for this ticket (`fin-g4-t1-pg`, `postgres:16`,
host port 5433 → container 5432, DEV-only credentials taken from the committed
`docker-compose.yml`). That container has since been **removed** — it held host
`5433`, which is exactly the port `docker-compose.yml` publishes the project's
own dev Postgres on (deliberately `5433:5433`, so `5432` stays free for the
iknow-memory server), so it was blocking `scripts/dev-up.sh`. Runs 17+ use the
project's own Compose-managed Postgres instead. See *Environment unblock*.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `FIN_CHECKPOINT_URL=<throwaway pg> uv run pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_thread_attachment_durable_postgres.py tests/host/test_attachment_store.py` | 0 | 77 passed, 0 failed, 0 skipped |
| 2 | `uv run pytest -q tests/host/test_thread_attach.py tests/host/test_agent_instructions.py tests/host/test_attachment_index.py tests/corpus` | 0 | 613 passed, 0 failed, 0 skipped |
| 3 | `uv run pytest -q tests/host/test_workspace_http.py` | 0 | 91 passed, 0 failed, 2 skipped |
| 4 | `cd web && npx tsc --noEmit` | 0 | no type errors |
| 5 | `cd web && npx vitest run` | 0 | 24 files, 326 tests passed |
| 6 | `uv run pytest -m mcp -v tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_corpus_ingest.py` | 0 | 6 passed (real iknow-memory process) |
| 7 | `uv run pytest -q -m mcp tests/retrieve --ignore=tests/retrieve/test_live_search_modes_pg_es.py` | 1 | 31 passed, 1 error (blocked prerequisite, item B1 below) |

The two skips in run 3 are environment gates, not passes:
`test_workspace_http.py:1110` requires `FIN_CHECKPOINT_URL` from the project
service (deliberately unset for that run so it would not collide with the
throwaway container), and `test_workspace_http.py:1321` needs the gitignored
`web/dist` build.

## T1 acceptance, per case

Every row is an executed test id from runs 1–3, with what it observed.

| Plan T1 acceptance clause | Case | Observed |
| --- | --- | --- |
| Upload text bytes → logical id + `parsing`; `ready` only once index data exists | `test_upload_returns_parsing_and_becomes_ready_with_searchable_index` | POST answers `parsing`; status read is `ready`, `searchable: true`, `unit_count >= 1` |
| `ready` never reported stale | `test_an_unprepared_attachment_never_reads_as_ready`, `test_the_list_door_reports_a_row_that_lost_its_index_as_parsing` | A published row whose index was cleared reads `parsing` / `searchable: false` on both doors |
| Same thread inspects status on another turn | `test_thread_lists_its_own_attachments`, `test_a_later_turn_can_reference_the_same_attachment_again` | Second send referencing the same id succeeds; id is not consumed |
| Cross-thread id reveals nothing | `test_another_thread_cannot_read_or_reference_the_attachment` | Another thread's id yields the named `attachment_not_found` on read and send, with no text or hit |
| Restart retains bytes and parsed version, index usable before `ready` | `test_restart_keeps_bytes_and_version_and_restores_index_before_ready`, `test_ready_attachment_is_read_back_by_a_new_process`, `test_a_parsing_row_is_resumed_by_the_next_process`, `test_a_lost_index_is_rebuilt_at_the_same_version` | Fresh store instances over real SQL rows rebuild at the **same** version; `search(postings, "revenue")` hits unit 1 again |
| Failed/incomplete rebuild is a named non-ready state, never an empty hit set | `test_object_store_outage_during_preparation_is_typed`, `test_rebuilding_without_its_bytes_names_the_exit`, `test_whitespace_attachment_fails_with_a_named_reason` | `failed` + `source_unavailable` / `attachment_empty_text`; rebuilt row keeps no index postings |
| Thread delete removes metadata, bytes, units, index | `test_deleting_the_thread_removes_metadata_bytes_units_and_index`, `test_deleting_the_thread_removes_every_attachment_row` | Raw SQL counts `(fin_thread_attachment, _unit, _index) = (0, 0, 0)` via FK cascade |
| Delete racing preparation cannot resurrect an index | `test_delete_during_preparation_cannot_resurrect_the_index` | Late derived write raises `AttachmentThreadGoneError`; no index row survives |
| Cleanup failure typed, observable, retriable | `test_cleanup_failure_is_typed_observable_and_retriable` | 500 `attachment_cleanup_failed`, thread intact, retry succeeds |
| Old `attachment_id` after delete | `test_deleted_attachment_id_yields_unknown_locator_on_a_new_thread` | Reuse yields the unknown-locator surface, never another thread's text |
| Concurrent retry creates at most one active index version (SC11 concurrency class) | `test_concurrent_repreparation_publishes_one_index_version` | Four barrier-released preparations converge on one indexed version whose postings serve the expected hit |
| The one-use attachment assertion is replaced by the thread-lifetime contract | `test_attachment_remains_referenceable_for_the_thread_lifetime` | Replaces the retired single-use case; the id stays referenceable for the thread |
| Public-entry input classes (empty, invalid, overflow, unknown, concurrent) | `test_empty_body_is_rejected`, `test_non_utf8_body_is_rejected`, `test_unknown_thread_upload_is_404`, `test_unknown_attachment_status_is_typed`, `test_every_attachment_door_answers_with_one_shape` | Named 400/404 codes; all four attachment doors return one field set with an integer `version` |
| Model-facing copy matches the thread-lifetime contract | `test_attachment_context_message_states_the_thread_lifetime` | Block states the file belongs to the thread and no longer claims the text vanishes next turn |
| Web send gate has one reference authority | `uploadDoors.test.ts` → `send gate: what a send may reference` (3 cases) | Only `ready` ids reach the wire, so an unrelated question is never refused; every withheld record is returned for display instead of being dropped |

Static sweep: no bare `except` without a named outcome in
`attachment_prepare.py` / `attachment_store.py` (each swallow carries an
`EXIT:` comment naming the condition), no `attachments.store` reach-through
left in the HTTP routes, and no test-only helper added to a public barrel.

## Review gate and how its findings were closed

The plan's per-bullet loop requires one code-review phase after the bullets, with
BLOCKED findings repaired rather than patched at the symptom. Pinned refs: base
`7a30e84`, HEAD `19f9b27`, dual-axis (Standards + Spec) sub-agents.

| Round | Aggregate | Gate |
| --- | --- | --- |
| 1, on `7a30e84..19f9b27` | 6 High across both axes: S-1 `mark_ready` cross-process publish race; S-2/S-3 `delete_thread` double byte-delete and late-upload orphan; P1-1 cross-thread lookup answered 400 vs 404 inconsistently; P1-2 run history still held a resolvable `attachment_id` after thread deletion; P2-1 dependency note | `GATE: BLOCKED` → `review-report-repair` |
| 2, same refs after repair work | Standards 1 High / Spec 0 High (the web send door silently dropping `parsing` attachments, which also made the host's typed `attachment_not_ready` unreachable) | `GATE: BLOCKED: 1 High unresolved` |

Both rounds' High findings close in one commit, `ead5e26`, whose body maps each fix
to its root-cause cluster rather than to a line: `retry()` deleted outright so a
published version can no longer be destroyed by a retry (its race and the 400/404
split go with it), empty derived text raises `AttachmentNotReadyError` instead of
being injected as content, the four attachment doors share one wire shape and the
list door goes through `list_for_thread` instead of reaching into the store,
`delete_thread` no longer deletes every key twice, the model-facing copy stops
claiming the text vanishes next turn, and the web send rule collapses to one
authority, `attachmentsForSend`, so a withheld attachment is named rather than
dropped and an unrelated question is never blocked by a `parsing` chip.

**Stated plainly: there was no third dual-axis pass on the `ead5e26` diff.** The
second round's High is closed by the same change, and what backs it is execution,
not another opinion — the 142-case attachment run and the web gate cases below. A
re-review of a repair diff is the operator's call, not this ticket's default.

## Real-boundary evidence — iknow-memory MCP

Run 6 was taken pre-merge; runs 13–15 repeat the same boundary on the merged
tree, where `src/fin/retrieve/mcp_adapter.py` (+76/−13) and
`src/fin/retrieve/port.py` (+27) actually changed, so the live evidence below
is about the tree this PR ships.

Dependency configuration: real `node` process over
`/home/winner/projects/iknow-memory/dist/index.js`, `storage: "memory"`,
MCP stdio transport, scripted model. Dataset: committed
`data/corpus/manifest.json` fixtures.

| Case group | Observed |
| --- | --- |
| `research_done_cites_this_runs_retrieved_text` | PASS |
| `ungrounded_answer_over_the_same_live_evidence_is_not_done` | PASS |
| `test_live_mcp_read_source` (3 cases) | PASS |
| `test_live_corpus_ingest` (1 case) | PASS |
| `test_live_mcp_t2..t5` (5 cases: push, retrieve, idempotency, continuation, invalidation, degradation) | PASS |
| `test_mcp_smoke` (20 cases) | PASS — fixture server, protocol level only |

What this proves for T1: the live MCP push/retrieve/`read_source` path still
behaves after the attachment slice landed *and* after main's adapter/port
refactor merged into it, i.e. the new thread-attachment layer did not disturb
the company-source boundary. What it does not prove: attachment semantics over
the real store. T1's thread-attach door resolves no body through MCP by
contract, and that is pinned by `test_attach_does_not_trigger_mcp_read_source`
(run 15), which installs a spy on `MCPRetrieveAdapter.read`, wires the MCP
adapter as the graph's real `read_port`, and asserts zero calls. The `pg_es`
retrieval tiers were blocked when this section was first written and now run
green (runs 20, 23), so they are no longer a gap in the live evidence — they are
just not what T1 changes. No G4 attachment gold case over the real fin object
store plus live iknow-memory store exists yet; that is SC12's end-of-round
obligation.

### Browser-level pass — the DOM gap closed (B4)

The web suite runs in a node environment with no DOM, so until this pass nothing
about chip rendering, the withheld line, or the retry control had been *seen*.
Stack: real `fin-serve` on `:8000` against the project's Compose Postgres on
`:5433`, real `vite` dev on `:5173`, driven by playwright-cli. Uploaded file is
committed representative data (`data/corpus/extracted/0000320193-24-000006/passages-001.json`,
76 KB of real filing passages), not invented toy text.

| Case | Observed |
| --- | --- |
| upload → `parsing` → `ready` unaided | chip painted `解析中…` at **172 ms**, `可引用` at **2316 ms** with no click between; reproduced at 116 ms → 2207 ms. So the readiness transition is **polled, not user-triggered** — the store's 2 s list poll, one `GET /attachments` per tick. Door reported `unit_count: 1, searchable: true, version: 1` |
| same-thread reuse across turns | two sends on one thread, both `POST /messages` **200** carrying the identical id pair; both chips still `ready` after — the id is not consumed |
| unrelated question while a file is `parsing` — the behavior round 2 of the review flagged High | send button enabled; withheld line read `本轮不会引用：page-001.json（解析中…）。就绪后它会自动随后续问题带上。`; the send went out with only the 4 ready ids, parsing id excluded, withheld line still visible, no refusal notice — browser agrees with `uploadDoors.test.ts` send-gate |
| failed path + retry control | whitespace-only file → `failed`, chip `解析失败（没有解析出文本内容）`, door `failure_reason: "attachment_empty_text"`; retry present, visible, not disabled, aria-labelled; clicking it returns **200** with `status: failed, version: 0, unit_count: 0` and the same reason — correct, since retry re-runs preparation over still-empty bytes. **`retry → ready` was not observed**: no transient-failure fixture exists, so that edge stays store-level |
| cross-thread | only the current thread's chips in `body.innerText`; **no select/listbox/combobox affordance exists at all**, only the hidden file input, so a foreign reference cannot be constructed; navigating away and back re-scoped the list (5 chips → 1) |
| console / network hygiene | 108 requests, zero app-originated 4xx/5xx, zero console errors, zero unhandled rejections; no `object_key`, bucket, `s3://`, `local://`, `file://`, `/home/`, `/tmp/`, `/var/` in any client payload — the doors expose exactly the 9 pinned fields |

**Narrowed, not glossed:** two halves of a browser send were *not* observable on
this box, and the pass reports them rather than claiming them. (a) The worktree has
no `.env`, so every model call answers 401 and each turn ends
`status: refuse / status_classified: auth` — the attachment door still admits the
send (200), which is what B4 is about, but the **answer** half is unproven here.
(b) The backend logged `FIN_OBJECT_STORE_URL/BUCKET are not set; thread attachments
keep their raw bytes in process memory`, so **restart durability is not
exercisable in this configuration**; it remains SQL-seam evidence (runs 1/16/25),
not a browser-proven property.

Three by-products, of which the most important one is a **false positive I am
retracting**:

- **F-B4-1, reported by the browser worker as a client-surface leak — retracted
  after reading the code.** It observed `GET /api/threads/{id}` putting provider
  prose into each turn's `status_reason`, e.g.
  `stream raised mid-flight: OpenAIAuthenticationError: Error code: 401 - …` cutting
  off at `Please carry the API secret key in the 'Author`, and called it a hygiene
  leak against the project's 错误信息不泄露敏感细节 rule. It is not. That string is
  already post-sanitizer output: `_sanitize_reason_text`
  (`src/fin/host/workspace_http.py:455`) applies the five `_SENSITIVE_PATTERNS`
  redactions (`:442` — `authorization: bearer <token>`, `sk-…`, `api_key=/token=/
  secret=`, ≥32-hex blobs, ≥40-char high-entropy runs) and caps the result at 200
  characters, and the call site at `:1341` composes the reason as
  `f"stream raised mid-flight: {type(exc).__name__}: {exc}"` and **then** passes it
  through the sanitizer before anything is wire-bound. The contract is stated in
  those words at `:330` — *"single-line, <=max_len chars, no tokens, no headers"* —
  with hand-written Chinese operator copy keyed off a closed `kind` enum
  (`_STREAM_FAILURE_USER_COPY`, `:344`) as the second layer, so the user-facing
  bubble never reads provider text even if the redactor missed. The cut
  mid-header-name the worker photographed is the length cap
  working, not a secret escaping. The worker had already conceded "no credential
  value, no path"; what it got wrong was naming a documented field a defect. **No
  finding, nothing to fix** — and I removed F3 from the open list rather than
  carrying a made-up security item into the merge.
- **F-B4-4 (coverage note, real)** — because `attachmentsForSend` strips non-ready
  ids client-side (`web/src/components/Composer.tsx:126`, defined at
  `web/src/store.ts:1208`), the host's typed `attachment_not_ready` is unreachable
  from the browser; the on-screen withheld line is the only signal, and
  `uploadDoors.test.ts:477` is the sole cover for that refusal. Consistent with
  spec, but worth knowing the host code path has no UI traffic behind it. (The
  worker cited `web/src/Composer.tsx`; the real path carries `components/`, and the
  line number was right — corrected here rather than passed through.)
- **F-B4-2 / F-B4-3** — the two unobservable halves named above (no `.env` on this
  box; `FIN_OBJECT_STORE_URL` unset).

Residue from the pass, left deliberately: 5 threads and 9 attachment rows in the
**dev** Postgres. The operator may want them cleaned; deleting data was not this
worker's call. The 11 per-case screenshots are session-ephemeral under
`/tmp/b4-browser/` and are deliberately **not** committed — this record's standing
claim is the observed text above, and a PNG that cannot be re-derived from the
commands would only pretend to be evidence.

## Blocked items — status after the environment unblock

Closed by runs 17–26, so they are no longer open questions:

- **B4 closed with two named limits** — the Composer attachment surface was driven
  in a real browser (run 26); all six cases in *Browser-level pass* observed,
  including the polled `parsing`→`ready` transition and the withheld-attachment
  line that round 2 of the review turned into a High fix. Not proven there, and
  recorded as such: the answer half of a send (no `.env` on this box → every model
  call 401s) and restart durability (object store unset → bytes live in process
  memory).

- **B1 closed** — `test_live_host_graph_hop.py` passes (run 19) against real
  stdio + real Elasticsearch, with the qualification recorded under
  *Correction to runs 19 and 23*.
- **B3 closed** — `jq` and the Compose plugin were installed; `test_fin_launcher`
  29 passed, `tests/host/test_compile.py` 21 passed (run 17, 18). This also
  corrected a wrong path in an earlier draft of this record: the compose
  contract case lives at `tests/host/test_compile.py`.
- **B2a closed** — `tests/retrieve/test_live_search_modes_pg_es.py` is at
  **11 passed, 0 failed** (run 23), with its four neighbor live groups re-run
  green in the same session. One correction to the earlier draft: that file
  *collects* 11 cases, not 12; the "12" in runs 9 and 20 was this file's 11 plus
  `test_live_host_graph_hop.py`'s 1. The repair was a rebuild of the read-only
  sibling's `dist/`, which the operator authorized for this ticket.
- **B6 closed on its own PR, [#299](https://github.com/winter6205/fin/pull/299)**
  — the default suite's `$HOME` leak is fixed at the shared boundary: an autouse
  `tests/conftest.py` fixture points `HOME` at an empty per-run directory,
  following the file's existing `_strip_operator_llm_and_mcp_cwd_env` convention
  rather than special-casing the three tests. Proven RED first (`3 failed` with a
  planted `~/.fin/user.md`, and `3 failed` with the operator's real 508-byte one),
  then GREEN with that real home still present (`11 passed`), then
  `uv run --no-sync pytest -q` → **2962 passed, 47 skipped, 299 deselected, exit
  0**. Deliberately **not** folded into this PR: one file, `+38/-0`, test
  infrastructure only. It was re-proven from scratch on the current `main`
  (`7907453`, commit `edc1a2f`, zero-conflict cherry-pick) rather than reusing the
  numbers above: baseline without the fix `3 failed, 8 passed` → with it
  `11 passed`, `pytest -q tests/host` 1651 passed, full default suite identical at
  2962/47/299. Independently re-checked in this session: `~/.fin/user.md` is still
  that 508-byte file, and `test_clarify_prompt.py` passes 11 on the branch — the
  fix removes the dependency, it does not hide the file.

Still open, with the reason:

- **B2b attempted (run 24), still blocked on one named cause.** The two
  candidate stops listed here before the run were resolved differently than
  expected. (i) The teardown hazard is real — the suite's `fin down` did delete
  three Compose containers — and it is survivable, so it is not a blocker; see
  *What run 24 cost the machine*. (ii) The name collision is the blocker, and
  `docker stop` does **not** clear it: `Error response from daemon: Conflict.
  The container name "/iknow-memory-postgres" is already in use by container
  "9824bfa07bc2…". You have to remove (or rename) that container`. The operator
  must either bring that hand-run Postgres under the sibling's Compose project
  or point `FIN_IKNOW_ROOT` at a self-consistent checkout; renaming or removing
  someone else's container is outside this ticket's authority, so the worker
  stopped and reported instead of forcing it. What is now proven about the
  launcher: its hermetic layer (29 cases, run 17) plus preflight, build,
  fin-owned Postgres health, `fin check`, and MCP reachability on the real box.
  What remains unproven: full-stack `fin up`, `fin status` consistency, the
  `.fin/ready` verdict, `fin logs`, and the 409-then-admit path.
- **B5** Not closed: SC12's committed G4 gold set (PDF-text, scanned PDF,
  image, 8,000-code-point continuation, cross-thread denial at retrieve,
  attachment/company comparison) — belongs to T2–T5 and the end-of-round run.
- **F1a** New, and the only finding worth filing from the whole unblock: the
  live-iknow harness launches the sibling server on an `is_file()` check alone,
  so a gitignored artifact whose state cannot be reliably *observed* — two of my
  own workers measured the same `dist/` paths minutes apart and reported
  contradictory contracts — is indistinguishable from a violated wire contract.
  Fix is recorded build provenance asserted before launch, not a freshness
  comparison. Owner: `tests/retrieve/live_iknow_support.py`, and it belongs on
  the already-open [#276](https://github.com/winter6205/fin/issues/276), whose
  prerequisites section currently asks a human to "先确认主 checkout 已 pull +
  rebuild" — the mechanical assertion is exactly what that issue lacks. This
  attachment ticket does not own it and has not opened a new issue for it.

- **F2** Found while running the context-loop checks, and worth more attention than
  the ADR items: the archived `pre-context-write-guard.cjs` fails *open*. Its
  `require('./lib/read-stdin-with-timeout.cjs')` target does not exist under
  `archive/`, the outer `catch` swallows it, and the script **exits 0** — a guard
  that reports success while doing nothing. Anyone trusting that path gets a
  false-green CONTEXT/ADR gate. Owner: the arthurpower plugin's archived hook, not
  this repository. Not fixed here: it is plugin-external and a validation hook is a
  high-risk area under `.claude/rules/security-boundaries.md`.

## PR base state

`origin/main` had moved ahead of the merge-base `2307e6b` (10 commits on
`main`, 11 on this branch), and exactly two files collided:
`.claude/rules/test.md` and `.codex/rules/test.md`. Both sides had
independently translated that Chinese file to English, so the merge is a
content collision, not a semantic one: our version is a strict superset (it
adds the *Golden-set acceptance for every change* section), and
`origin/main` contributed no line that our version lacked — checked by
diffing each side's file against the other and finding no main-only line.
The conflict was therefore resolved by keeping ours.

A merge commit (`9bbcae2`) was chosen over an 11-commit rebase deliberately:
rebase would re-apply each commit against new main code without any evidence
that the combined tree still passes, whereas a merge puts the two changesets
together under one test run — the run recorded in the next section. After it,
the branch is 12 ahead / 0 behind `origin/main`, so the request opens clean.

## Post-merge re-verification

`main` landed a search-mode job-fact refactor that touched the same two files
this ticket modified (`src/fin/host/graph.py`,
`src/fin/host/workspace_http.py`) plus `src/fin/retrieve/*`; git auto-merged
them without a conflict, which is exactly the case where a merge can be
textually clean and semantically wrong. Every run below is on the merged tree.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 8 | `uv run --no-sync pytest tests/host/test_thread_attachment_durable.py tests/host/test_attachment_store.py tests/host/test_thread_attach.py tests/host/test_agent_instructions.py -q` | 0 | 98 passed, 0 failed, 6 skipped (all 6 are `FIN_CHECKPOINT_URL` gates on `PostgresAttachmentStore`) |
| 9 | `uv run --no-sync pytest tests/host tests/retrieve -q` | 1 | 2380 passed, 16 failed, 58 skipped, 271 deselected (7m31s, run to completion). **Zero failures in `tests/retrieve`**; the 16 are box prerequisites, itemized below |
| 10 | `cd web && npm run typecheck` (`tsc --noEmit`) | 0 | no diagnostics |
| 11 | `cd web && npm test` (`vitest run`) | 0 | 24 files, 326 tests passed; the `send gate: what a send may reference` cases were re-confirmed as collected and passing under `--reporter=verbose`, and file collection was checked against `find src -name "*test*"` so nothing was silently dropped |
| 12 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_mcp_smoke.py` | 0 | 20 passed — MCP 2.0 protocol against the local fixture server only |
| 13 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py test_live_mcp_t3.py test_live_mcp_t4.py test_live_mcp_t5.py` | 0 | 5 passed against a real `iknow-memory` node process (push / retrieve / idempotency / continuation / invalidation / degradation), `storage: "memory"`, with BM25, graph and rerank explicitly off in the harness |
| 14 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_corpus_ingest.py tests/retrieve/test_live_research_cite_on_ingest.py` | 0 | 6 passed — live `read_source` contract plus ingest→research→cite round trip |
| 15 | `uv run --no-sync pytest -q tests/host/test_thread_attach.py::test_attach_does_not_trigger_mcp_read_source` | 0 | 1 passed — the thread-attach door installs a spy on `MCPRetrieveAdapter.read`, wires the MCP adapter as the graph's real `read_port`, and asserts zero calls |
| 16 | `FIN_CHECKPOINT_URL=<throwaway pg> uv run --no-sync pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_thread_attachment_durable_postgres.py tests/host/test_attachment_store.py` | 0 | 77 passed, 0 skipped — the same real-SQL scope as pre-merge run 1, re-run here so the restart-rebuild, FK-cascade and `incomplete()` evidence belongs to the shipped tree rather than to the pre-merge tip |

### Why run 9's 16 failures are not merge damage

Each was traced to this machine, not to `9bbcae2`:

- 12 × `tests/host/test_fin_launcher.py` — `stderr='fin: jq is required'`;
  `command -v jq` reports nothing installed.
- 1 × `tests/host/test_compile.py::test_compose_config_exits_zero_when_docker_available`
  — `docker compose version` answers `docker: unknown command: docker compose`.
- 3 × `tests/host/test_clarify_prompt.py` — a **suite hermeticity leak** rather
  than a product bug: those cases call `compile_graph()` without injecting a
  `user_profile_store`, so `_read_profile_text()` reads the operator's real
  `$HOME/.fin/user.md` (present here, 508 bytes) and the expected prompt gains
  a trailing `[FIN_USER_PROFILE]` block. Reproduced and isolated in one step:
  `HOME=$(mktemp -d) uv run --no-sync pytest -q tests/host/test_clarify_prompt.py`
  → 11 passed. The code path predates this merge (commits `273dbaf` and
  `b6d01b5` are ancestors of *both* merge parents), so the branch neither
  caused nor can silently absorb it; it is item B6, fixed on PR #299.

`tests/retrieve` — the directory whose `port.py` / `mcp_adapter.py` /
`tiers.py` this merge actually changed — reported zero failures, and the eight
merge-touched search-mode and named-failure files were re-run standalone green
(48 passed). The one hand-check worth recording: our
`RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY` branch in `graph.py` was correctly replaced
by main's `copy_recorded_job_facts` helper rather than left duplicated.

These runs supersede runs 1–7 above, which were taken on the pre-merge tip.

## Environment unblock, and what it then proved

The 16 failures and the blocked items were traced to missing packages and
services, so the box was repaired and the previously-unrunnable suites were run
for the first time. Toolchain added: Docker Compose v2 `2.40.3` and `jq`
`1.8.1`. Services: `fin-g4-t1-pg` removed and the project's own
Compose-managed dev Postgres brought up on `5433` (healthcheck `healthy`);
Elasticsearch (`9200`) and Neo4j (`7687`) built and started from the sibling
`/home/winner/projects/iknow-memory/docker-compose.yml`; a pgvector Postgres on
host `5434` so that the pre-existing **non-Compose** `iknow-memory-postgres` on
`5432` would never need touching. That last clause held for runs 17–23 and then
**stopped holding** at run 24, whose attempt did stop and restart that container
(without renaming, removing, or recreating it) — the full accounting is under
*What run 24 cost the machine*.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 17 | `uv run --no-sync pytest -q tests/host/test_fin_launcher.py` | 0 | 29 passed — all 12 cases that had failed on `fin: jq is required` now run |
| 18 | `uv run --no-sync pytest -q tests/host/test_compile.py` | 0 | 21 passed, including the `docker compose config` contract case (correction: this file lives at `tests/host/test_compile.py`, not `tests/test_compile.py` as an earlier draft of this record said) |
| 19 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_host_graph_hop.py` | 0 | 1 passed against real stdio + real Elasticsearch — closes B1 |
| 20 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_host_graph_hop.py tests/retrieve/test_live_search_modes_pg_es.py` | 1 | 11 passed, 1 failed — the `pg_es` tier is reachable for the first time; the single failure was attributed to a stale sibling build (item F1) and superseded by run 23 |
| 21 | `uv run --no-sync pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_thread_attach.py` | 0 | 44 passed — the toolchain and service changes did not disturb the attachment slice |
| 22 | `bash scripts/dev-up.sh --check` | 0 | `dev-up: check ok` — the launcher's toolchain gate is no longer a blocker |
| 23 | `npm run build` (that repo's own `"build": "tsc -p tsconfig.json"`) in the sibling, then `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py` and the four neighbor groups | 0 | the build was a **byte-level no-op** (identical sha256 before/after on the three inspected files, mtime only); `test_live_search_modes_pg_es.py` is now **11 passed, 0 failed**, and the neighbors show no regression: graph hop 1, read_source + corpus_ingest + research_cite 6, mcp t2–t5 5 |
| 24 | `uv run --no-sync pytest -q -m mcp tests/host/test_live_fin_launcher_acceptance.py` | 1 | **5 errors, 0 failures**, all from one fixture cause (21s): `fin up` exits 1 before serving, at the iknow-memory compose step. Everything of the launcher that does not depend on that step proved to work on the real box — preflight gates, build, fin's own Postgres `:5433` reported `Healthy`, `fin check`, and a direct `probe_knowledge_mcp()` call answering `reachable` over real stdio + ES + Neo4j. Classified as un-wired environment, not a product defect |
| 25 | Closing-turn re-verification on the shipped tip `5c926cb`: `FIN_CHECKPOINT_URL=<project compose pg :5433> uv run --no-sync pytest -q` over the six attachment files, then `cd web && npx tsc --noEmit` and `npx vitest run -t "send gate"` | 0 | **142 passed** on real Postgres (the durable, Postgres, store, thread-attach, agent-instructions and index suites together); `tsc` exit 0; the 3 web send-gate cases pass. All 27 test ids cited in *T1 acceptance, per case* were also checked to exist in `tests/` by name — `id_check_missing=0` |
| 26 | Real browser: `FIN_CHECKPOINT_URL=<compose pg :5433> uv run --no-sync fin-serve` on `:8000` + `cd web && npm run dev` on `:5173`, driven by playwright-cli over committed corpus data | 0 | 6/6 cases observed — see *Browser-level pass* below. 108 requests, zero app-originated 4xx/5xx, zero console errors, no storage locator in any client payload. Both servers stopped and ports verified closed |

### What run 24 cost the machine, and how it was proven restored

The teardown risk this section previously warned about **materialized and was
recovered**, so it is recorded as an event rather than a hypothesis. The suite's
own `fin down` deleted the three Compose-managed containers
(`fin-wt-g4-t1-postgres-1`, `iknow-memory-es`, `iknow-memory-neo4j`); all three
were brought back with identical names, ports, project labels and health, on
**their original named volumes** (`…_postgres-data`,
`iknow-memory-infra_iknow-memory-es`, `-neo4j` — each created before this
session, and ES still holds the `iknow-memory-chunks` index). Container ids
changed; data did not.

Two residues were left deliberately, because removing storage is not this
ticket's call: the empty volume `iknow-memory-infra_iknow-memory-pg` created by
the failed `up` at 01:56, and an anonymous Neo4j `/logs` volume. Verified after
the fact: `docker inspect iknow-memory-postgres` still reports id
`9824bfa07bc2…` and creation `2026-09-24T14:32:03Z` with volume
`iknow-memory-pg-test` — the operator's own container was **stopped and started,
never renamed or removed**.

Restore was proven by execution, not by `docker ps`:
`tests/host/test_thread_attach.py` → 19 passed, and
`tests/retrieve/test_live_host_graph_hop.py` → 1 passed.

### Correction to runs 19 and 23: the graph-hop pass needed an explicit DB URL

Run 19 was recorded as "passes against real stdio + real Elasticsearch", which
is true, but incomplete in a way that matters. With **no** env override that
case targets `:5432`, where the hand-run `iknow-memory-postgres` has only the
superuser `postgres` role — no `iknow_memory` role or database — and it fails
`FATAL: role "iknow_memory" does not exist`. That fact predates every operation
in this ticket. Runs 19 and 23 passed because they set
`FIN_LIVE_IKNOW_DB_URL=…@localhost:5434/iknow_memory`, i.e. against the separate
pgvector instance this ticket started. B1's closure therefore stands as
*live-stdio-plus-live-Elasticsearch coverage on a box configured for it*, not as
"a default-configuration pass".

### The two workers disagreed about `dist/`, and that is the real F1a evidence

One unresolved contradiction, recorded rather than smoothed over. The
diagnosing worker observed `dist/core/errors.js` containing **zero**
occurrences of `failureToken`, a **two**-argument `GraphragError` throw at
`mode-c-arm.js:152`, and `dist/index.js` dated 2026-09-24 09:22. The rebuilding
worker, a few minutes later, inspected the **same paths** and found **six**
occurrences of `failureToken`, a **three**-argument throw matching
`mode-c-arm.ts:243-255`, and an mtime of 2026-09-25 01:26 — a *different* mtime
for a file neither worker changed in between, and one already on the far side of
`068da4e`. Its rebuild then changed no bytes.

So one of those two measurements does not describe the file it claims to, and
this record cannot say which. What is not in doubt: the suite is green now,
`failureToken` and the three-argument form are present in the artifact now, and
the sibling checkout's `HEAD` (`2553e7d`) has contained `068da4e` throughout.
The earlier "stale build produced a false cross-repo contract violation"
conclusion was sound against the evidence available when it was drawn, but it is
**not** confirmed by a controlled before/after, because the two observations
that would have formed it came from different workers with no shared snapshot.

That is a stronger case for **F1a** than a clean before/after would have been:
an unlabelled, gitignored build product with only an `is_file()` guard is not
just able to *be* stale — it is not reliably *observable*, so two workers on the
same machine can report different contracts and neither can be refuted from the
artifact alone. The guard the harness needs is not a freshness check against
`HEAD` but a recorded identity: build provenance written into `dist/` (source sha
plus timestamp) and asserted by `tests/retrieve/live_iknow_support.py` before it
launches a server from it.

### F1 — the `global` refusal was not a contract defect; the stale-build cause is narrowed, not proven

*Status after run 23:* this case is green and B2a is closed. What follows is kept
as the diagnosis of the run-20 failure, with one narrowing: the rebuild changed
no bytes, so the section below documents a real observed divergence between the
artifact and `src/`, but this record has **no controlled before/after** proving
the rebuild is what turned it green (see the two subsections above). The
conclusion that *does* hold is F1a.

`test_a_global_send_reaches_the_server_and_surfaces_the_runtime_index_failure`
fails deterministically (reproduced three times, byte-identical). The server
answers a `global` send with a **prose** text block,
`retrieve search_mode=global is not ready: community_summary_index_failure
(no community_index_state row yet)`, while
`tests/retrieve/test_live_search_modes_pg_es.py:387` parses that block as JSON
and reads `{"error": ...}`, giving
`json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.

**An earlier draft of this record blamed `main`'s commit `2701923` for this. That
accusation was wrong and is retracted.** The contract is settled and *both*
sides already honor it:

- `specs/search-mode-graph-global.md:63` pins the reader to "the `error` field
  of the `isError` JSON body … **never a prose match**", and says the producer
  lands in iknow-memory #273.
- `iknow-memory/src/index.ts:535-545` emits exactly
  `textResult(JSON.stringify({ error: failureToken, message }), true)`, shipped
  by `068da4e` ("publish community-summary not-ready as a structured wire
  field").
- `src/fin/retrieve/mcp_adapter.py:480` (`_named_error_code_of_body`) reads
  `body.get("error")` structurally — the same expectation from the other side.

The prose came from the *artifact under test*, not from anyone's source: the
harness runs `/home/winner/projects/iknow-memory/dist/index.js`, built
2026-09-24 09:22, while `068da4e` landed 2026-09-24 19:00. Three observations
taken at the time — two of them contradicted minutes later, as recorded above:
`dist/core/errors.js` contains zero occurrences of `failureToken`;
`dist/core/retrieval/mode-c-arm.js:152` throws `GraphragError` with two
arguments (token-less); and the sibling's own TS test
`npx vitest run tests/index.test.ts -t "named failure on the wire"` passes 2
cases from current source. The remedy was `npm run build` in that checkout — a
rebuild, not a code change — which the operator then authorized for this ticket
(run 23), after this record first declined to touch a read-only dependency that
other live suites share. That the rebuild changed no bytes is the open part of
this story, recorded above.

**The finding that is real (F1a, harness hygiene, not product):**
`tests/retrieve/live_iknow_support.py` selects the sibling server after only an
`is_file()` check (`tests/retrieve/test_live_search_modes_pg_es.py:149`), so a
stale build is indistinguishable from a violated wire contract, and a machine
that never ran the suite before will report a false cross-repo defect. The guard
belongs there, and not as a freshness comparison against `HEAD`: a gitignored
artifact has no reliable clock, as the contradictory measurements above show. It
needs recorded provenance — the source sha and build time stamped into `dist/` by
the build itself, asserted before the harness launches a server from it.

No relationship to the attachment slice: the failing test imports only
`fin.corpus.chunking` / `_common` / `push`, `fin.host.named_failures` and
`fin.retrieve.port`, and `grep -i attachment` over `src/fin/retrieve/*` and
`live_iknow_support.py` is empty.


## Context-loop hooks — because this PR's diff touches `docs/adr/`

The PR diff is 38 files including 9 under `docs/adr/` (2 modified, 6 added), so the
four context-loop checks fire. A worker ran them against
`hooks/pre-context-write-guard.ts` and the ADR tree. **Provenance first, because it
decides what this run owns:** every ADR/CONTEXT edit here was authored
2026-09-24 **15:23–21:05 +0800** (commits `52343f4`, `57ffdea`, `4f4ad7e`, author
`winter6205`/`Codex`), while this session's transcript starts 22:01 +0800. This run
wrote no ADR; it inherited these files into its PR.

| # | Hook | Result | Evidence |
| --- | --- | --- | --- |
| 1 | CONTEXT/ADR schema | **FAIL on one file, from a hook-vs-format conflict** | `docs/adr/0009:4` `Status: accepted; superseded-by ADR-0087 §…` exits 2; the other 7 changed ADRs and `docs/CONTEXT.md` exit 0. `ADR-FORMAT.md:42` says that combination *is legal*, and `main` already carries the same exit-2 shape at 0053 and 0066 — so the conflict predates this PR and fixing 0009 alone would deviate from the documented format. Also: the archived `pre-context-write-guard.cjs` is unrunnable (`require('./lib/read-stdin-with-timeout.cjs')` missing → outer catch → **exit 0 false green**); the `.ts` version was used instead |
| 2 | Write authority (`domain-modeling` routing) | **N/A for this run; unverifiable for the authoring session** | Zero `Write`/`Edit` events hit `docs/adr` or `docs/CONTEXT.md` in this session's transcript; the 9 `docs/adr`-touching shell calls are read-only. The guard's identity check is a hardcoded constant (`pre-context-write-guard.ts:710-718`) and no `hooks` key exists in `~/.qoder-cn/settings.json`, so the hook never ran this session and cannot be asserted from a session log either |
| 3 | Read-side freshness | **FAIL, base-authored** | Annotated by amendment: 0009, 0047. Unannotated contradictions: `0006` (`{document_id, version, page}` + "do not put … character offsets … in the locator") is overturned by 0088's typed `unit` and code-point `next_position`; `0005`/`0021`/`0046`/`0082` still name the tool `read_file`, which 0087 renames to `read_source`. None of those five files is in this diff. The repo's own convention is `Amendment (date)`; the blockquote form the hook demands appears **0** times in `docs/adr/`, and `post-conflict-annotation.ts` only scans `## Decision` sections, which fin ADRs do not have — so that detector is structurally always-empty here. The spec's own claim (*"0083/0085/0086 point forward without rewriting historical decisions"*) **is** true in the diff |
| 4 | ADR three-condition gate | **PARTIAL, repo-wide convention** | All 6 added ADRs record `Considered Options` (real trade-off), and 0083–0086 add recovery/decision-source; hard-to-reverse and surprising-without-context are recorded **nowhere**, in any of the 88 ADRs — so these 6 are consistent with the collection, not worse than it |

None of the four is closed by this ticket, and none is a behavior defect: 1 and 3
are ADR-governance conflicts that predate the run, 2 is out of its own reach, and 4
is a convention question for the whole ADR set. They are named rather than silently
carried into the merge, and resolving 1 or 3 means editing someone else's ADR text
or a validation hook — both outside an attachment ticket's authority.

## Closeout after the merge (runs 27–29, 2026-09-25)

Merging #299 was decided on a fresh controlled comparison, not on the numbers
carried above.

- **Run 27 — #299 is still needed on today's `main`, and it works.** Two trees,
  one command (`uv run --no-sync pytest -q tests/host/test_thread_attach.py
  tests/host/test_clarify_prompt.py`): main tip `5de7120` without the fixture →
  **3 failed, 27 passed**, all three in `test_clarify_prompt.py`; the same tip
  with `edc1a2f` stacked on it (`git merge --no-commit`, and `diff -rq` against
  the tip shows the tree differs by exactly `tests/conftest.py`, plus
  `web/dist`/`node_modules`) → **30 passed**. The leak is on the *read* side:
  `~/.fin/user.md` kept its 508 bytes and its 2026-09-24 02:17 mtime across both
  runs.
- **Run 28 — a false alarm about #299, retracted in the same turn.** In the
  merged tree, `test_ready_attachment_is_read_back_by_a_new_process` failed at
  `tests/host/test_thread_attachment_durable_postgres.py:108`
  (`assert _counts(stack)[0] == 1`), and the first comparison seemed to blame the
  `$HOME` fixture. It does not. Seeding one leftover `fin_thread_attachment` row
  on a tree **without** #299 reproduced the failure at the same line; seeding only
  a `fin_thread` row did not. The real cause is this suite's own cleanup: `stack`
  emptied the database at teardown only, so the first case's global row count
  depended on whatever an earlier interrupted run had left. The comparison had not
  controlled for that, so "#299 broke a T1 test" was wrong and is not carried.
- **Run 29 — the cause fixed as its own change, RED first.** Added
  `test_an_interrupted_run_leaves_attachment_rows` (plants the rows a killed
  process would leave, taking `postgres_url` only so no fixture cleans behind it)
  and `test_a_fresh_run_clears_rows_left_behind` (asserts a new `stack` starts at
  `(0, 0, 0)`): **RED** at line 224 with the leftover present, then **GREEN** after
  `_drop_all_threads()` runs on both sides of the fixture — **7 passed**, run twice
  in a row, and 7 passed again from a deliberately dirty start. Neighbours
  `test_thread_attachment_durable.py` + `test_thread_attach.py` → 44 passed. No
  assertion was removed or weakened; `ruff` is not installed in this venv, so no
  lint gate ran and the new code matches the file's existing shape by hand.

## Delivery state

- **PR #298** (T1): **merged** into `main` as `5de7120`.
- **PR #299** (B6, split out on the operator's request): **merged** into `main` as
  `eeafbe1`, after run 27 showed it fixes three red cases on that tip. 1 file,
  `+38/−0`.
- The start-state defect found in run 28 is delivered as PR #300 (branch
  `fix/g4-t1-suite-restartable`): test-only, and it marks T1 delivered in
  `plans/thread-attachment-source-read.md`.
- Both merged branches were deleted locally and on the remote on the operator's
  2026-09-25 "做完完整合并就行" instruction.
- Not delivered by any of them: T2–T5, and with them spec Success Criteria 5–12
  plus SC12's committed G4 gold set. Still open: B2b's five launcher-acceptance
  cases (one operator container action away), F1a on #276, and F2's fail-open
  archived guard.
