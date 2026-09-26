# G4 T2b raw upload and the send gate — fin (2026-09-26)

Validation record for plan
[`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T2b (Store uploads raw and gate sends)**.

Worktree `/home/winner/projects/fin/.qoder/worktrees/g4-attachment-t2b`,
branch `worktree-g4-attachment-t2b`, base `c8eeb52`
(`Merge that pull request from winter6205/feat/g4-corpus-upload-parser`).
Every command below was run against the working tree on that base; the tree is
now committed on the branch (`001d5da`), and the two sections marked *Defect*
record what the run found and what closed it.

## Scope: what T2b delivers, and what it deliberately does not

Delivered by this change, and proved below against real storage:

* `POST /api/threads/{id}/attachments` stores **durable raw bytes only** and
  answers `uploaded`. No background task, no parse, no OCR, no layout, no
  vision, no index (`src/fin/host/workspace_http.py:2907` — `attach_to_thread`
  lost its `BackgroundTasks` parameter and its `prepare_async` call, and the
  non-text `400` branch is gone).
* Unsupported bytes are still uploadable; only preparation names them. The
  typed rejection `attachment_not_text` now comes from
  `fin.host.attachment_prepare`, never from the door.
* The byte cap is the **deployed** `AttachmentBudgets.max_upload_bytes`, read
  through the `get_attachment_budgets` dependency
  (`src/fin/host/http.py:226`). The 2 MiB route constant
  (`ATTACHMENT_MAX_BYTES`) no longer exists in the module.
* Boot wiring: `serve()` reads all four `FIN_ATTACHMENT_*` bounds immediately
  after `apply_dotenv()` and before `publish_knowledge_mcp()`,
  `compile_for_http()` and `uvicorn.run()` (`src/fin/host/http.py:642-650`), so
  an un-stated bound aborts the boot rather than serving without one.
* `uploaded` is not a recovery candidate: `PostgresAttachmentStore.incomplete()`
  (`src/fin/host/attachment_store.py:916`) selects only `parsing` or a torn
  `ready`, and its in-memory twin is `attachment_store.py:491`.
* The composer holds send and new-conversation while any upload is in flight
  (`web/src/store.ts:481,521`, `web/src/components/Composer.tsx:62`).
* The host holds the same condition: `AttachmentUploadInFlight`
  (`src/fin/host/workspace_http.py:1911`) counts an in-flight upload per thread,
  the upload door enters it before anything else happens
  (`workspace_http.py:2965`) and leaves it in a `finally`
  (`workspace_http.py:3005`), and the send route answers
  `409 attachment_upload_in_progress` (`workspace_http.py:2487`). See
  *The host-side gate*.

**Not** delivered here, by design: no graph-triggered preparation. The
`ensure_attachment_ready` node, the `uploaded → parsing` transition, the run
suspension and the auto-resume are **T2f**; nothing in this record observes a
preparation being *started by the graph*. What T2b does own, and what this
record closes, is the other half — the raw-bytes door and the send gate.

This record also exists to close a sentence the previous ticket left open.
[`2026-09-26-g4-t2a-page-vision-budgets.md`](2026-09-26-g4-t2a-page-vision-budgets.md)
command 6: *"the budget module has no production caller yet, so no case here
exercises `attachment_budgets` across a real boundary — that becomes possible
when T2b wires the upload door."* Commands 3, 5 and 9 below are that exercise:
the deployed bounds now drive a real HTTP door, a real Postgres table, and the
real `serve()` process.

## What is real here, and what is not

| seam | real? | note |
| --- | --- | --- |
| Postgres | yes | `fin-postgres-1`, `localhost:5433`, real `PostgresAttachmentStore` / `PostgresThreadStore`, real `fin_thread_attachment`, `…_unit`, `…_index` rows |
| HTTP door | yes | the deployed `fin.host.http.app` routes, driven through `TestClient` (in-process) **and** through a real `uvicorn` child process (out-of-process) |
| Server process | yes | `python -c "from fin.host.http import serve; serve()"`, a real PID, a real socket on `0.0.0.0:8000`, real SIGTERM shutdown |
| Payloads | yes | the committed gold files and real corpus filings, byte-for-byte; boundary bodies are built from the real filing bytes, not from filler |
| Byte adapter | **as the product chose** | see below |
| iknow-memory | yes | the compiled `/home/winner/projects/iknow-memory/dist/index.js`, via the live MCP regression (command 10) |
| Graph | no | `get_graph` is the only replaced dependency; T2b never touches the graph, so a `_NullGraph` that raises if called doubles as the "upload starts no work" assertion |

**The durable byte adapter could not be exercised, and that is recorded as
blocked, not as a pass.** `_build_default_attachment_service`
(`src/fin/host/workspace_http.py:2143-2177`) asks `object_store_from_env()` for
the real store and falls back to `InMemoryObjectStore()` with a `WARNING` at
`workspace_http.py:2172` when `FIN_OBJECT_STORE_URL` /
`FIN_OBJECT_STORE_BUCKET` are unset. This machine has no S3-compatible service
(see *Blocked leg* below). Rather than install a dependency or stand up a fake
S3 and call it durable, the live module **drives that factory itself** and
asserts what it selected, so the degradation is observed evidence in the run
rather than an assumption:

```
WARNING fin.host.workspace_http:workspace_http.py:2172 FIN_OBJECT_STORE_URL/FIN_OBJECT_STORE_BUCKET
are not set; thread attachments keep their raw bytes in process memory and will report
source_unavailable after a restart
```

Everything the module proves about *rows, statuses, caps and the door* is
therefore real; what it proves about *byte durability across a process
restart* is limited to the adapter the deployment actually configured.

## Old versus new byte cap, on the real files

The retired route constant was `ATTACHMENT_MAX_BYTES = 2 * 1024 * 1024`
= **2,097,152 B**. The deployed bound
(`.env.example:85`, read by `from_environ`, never restated in a test) is
**4,194,304 B**.

| file | bytes | under the retired 2 MiB | under the deployed 4 MiB | observed |
| --- | --- | --- | --- | --- |
| `data/corpus/pdf/2023_FY_GOOG.pdf` | 624 | accepted | accepted | `uploaded` |
| `g4-mixed-chart-001.pdf` | 35,324 | accepted | accepted | `uploaded` |
| `g4-image-chart-001.png` | 40,419 | accepted (as text only — see below) | accepted | `uploaded`, raw non-UTF-8 bytes |
| `g4-scanned-001.pdf` | 677,716 | accepted | accepted | `uploaded` |
| `data/corpus/pdf/2023_Q1_MSFT.pdf` | 2,223,272 | **rejected 413** | accepted | `uploaded` |

`2023_Q1_MSFT.pdf` is the file that makes the change visible: it sits between
the two numbers, so the same request that the T1 door refused is the request
T2b accepts. It is asserted both in-process and against the live server.

The exact boundary is asserted at the deployed value, not at a shrunk
override: a body of exactly `max_upload_bytes` (4,194,304 B, built by repeating
real filing bytes) is accepted, and `max_upload_bytes + 1` returns
`413 attachment_too_large` and leaves **no object and no row**. The negative
assertion is non-vacuous because the byte store is wrapped in a recording lens:
`lens.puts` is empty after the refusal, so the 413 is shown to have reached
neither store rather than merely asserted not to have.

## Commands and results

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/host/test_thread_attach.py tests/host/test_attachment_store.py tests/host/test_thread_attachment_durable.py tests/host/test_attachment_budgets.py tests/host/test_scripted_boot.py tests/host/test_operator_env.py` | 0 | `335 passed, 7 skipped, 7 warnings in 8.53s`; the 7 skips are the pre-existing `FIN_CHECKPOINT_URL` gate (`test_attachment_store.py:696,721,742,771,779,798,814`) |
| 2 | `uv run --no-sync pytest -q tests/host -k "attach or budget or boot or serve"` | 0 | `412 passed, 14 skipped, 1860 deselected in 19.20s` — containment: nothing outside the attachment/budget/boot surface moved |
| 3 | same six files **plus** `tests/host/test_thread_attachment_durable_postgres.py`, with `FIN_CHECKPOINT_URL` exported from the `.env.example:23` template value | 0 | `349 passed, 7 warnings in 12.33s`, no skip: **14 previously-hidden cases became executed and all 14 pass.** The first run of this command was red — see *Defect 1* |
| 4 | `uv run --no-sync pytest -q tests/host/test_live_attachment_upload_door.py` (no `-m`) | 5 | `11 deselected in 0.25s`; `--collect-only` agrees: `no tests collected (11 deselected)` — the default hermetic suite is untouched by the new file |
| 5 | `uv run --no-sync pytest -m mcp tests/host/test_live_attachment_upload_door.py -v` | 0 | `11 passed in 6.44s`, with `FIN_CHECKPOINT_URL` exported and the real `PostgresAttachmentStore` asserted by type. Per case in *The live door module, per case* |
| 6 | `env -u FIN_ATTACHMENT_MAX_BYTES -u FIN_ATTACHMENT_MAX_PDF_PAGES -u FIN_ATTACHMENT_VISION_CALLS_PER_RUN -u FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN uv run --no-sync python -c "from fin.host.http import serve; serve()"` | **1** | aborts before binding; last stderr line `fin.host.attachment_budgets.AttachmentBudgetConfigError: FIN_ATTACHMENT_MAX_BYTES must be set to a positive integer, got None`; first stderr line is the benign `SPA dist directory …/web/dist not found` warning; traceback `http.py:643 serve → http.py:226 get_attachment_budgets → attachment_budgets.py:153 → :117`; `ss -ltn` shows **no listener on 8000** |
| 7 | the same command four times, dropping exactly one of the four keys each time | 1 / 1 / 1 / 1 | each aborts naming **the key it dropped**: `FIN_ATTACHMENT_MAX_BYTES`, `FIN_ATTACHMENT_MAX_PDF_PAGES`, `FIN_ATTACHMENT_VISION_CALLS_PER_RUN`, `FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN`, all `must be set to a positive integer, got None`. Port 8000 never bound (0 listeners after the four) |
| 8 | the same command with `FIN_ATTACHMENT_MAX_BYTES` set to `0`, `-1`, `abc`, `""` (other three at the deployed values) | 1 / 1 / 1 / 1 | four named refusals, quoted verbatim in *The boot gate, per case* |
| 9 | four bounds at the deployed values, no `FIN_CHECKPOINT_URL` | 1 | gets **past** the bound gate — `grep -c AttachmentBudgetConfigError` = `0` — and dies later at the next requirement: `fin.host.compile.MissingCheckpointURLError: FIN_CHECKPOINT_URL is required for the durable v1 compile (ADR 0015)` |
| 10 | four bounds at the deployed values **and** the real `FIN_CHECKPOINT_URL`, under `timeout 30` | **124** | the bound gate is passed (`0` `AttachmentBudgetConfigError` mentions) and the real server binds: `INFO: Started server process [3714145]`, `INFO: Uvicorn running on http://0.0.0.0:8000`; on the timeout `INFO: Shutting down` → `INFO: Finished server process [3714145]`. Exit 124 is the harness cutting a **successful** boot, not a failure. No process and no listener left: `ss -ltn \| grep -c ':8000'` = `0` |
| 11 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py … t5.py tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_host_graph_hop.py tests/host/test_live_attachment_upload_door.py` | 0 | `22 passed in 28.47s` — the 11 live-MCP cases T2a recorded as `11 passed in 36.65s`, still passing with the compiled iknow-memory server, plus the 11 live upload-door cases of command 5 in the same run |
| 12 | `cd web && npx vitest run src/uploadDoors.test.ts src/attachFailure.test.ts src/sendFailure.test.ts` | 0 | `Test Files 3 passed (3)`, `Tests 69 passed (69)`, `Duration 1.58s` — `sendFailure.test.ts (12)`, `attachFailure.test.ts (22)`, `uploadDoors.test.ts (35)` |
| 13 | `cd web && npm run typecheck` | 0 | `tsc --noEmit`, no diagnostics |
| 14 | `uv run --no-sync pytest -q tests/host` (whole host surface, new file present) | 0 | `1987 passed, 61 skipped, 238 deselected, 8 warnings in 113.47s` — see *Whole-surface guards* |

Commands 1-3, 5, 11 and 14 were re-run against the final tree and the tail
lines above are copied from that output; commands 6-9 are the real-process boot
matrix recorded per case below, and 12-13 are the web gates.

## The real boundary, in detail

### The live door module, per case (`tests/host/test_live_attachment_upload_door.py`, command 5)

One new opt-in file, `pytestmark = pytest.mark.mcp` plus the repo's
`FIN_CHECKPOINT_URL` skip convention, shaped like
`tests/retrieve/test_live_corpus_ingest.py` and
`tests/host/test_live_fin_launcher_acceptance.py`. Two harnesses, because the
claims need different ones: an in-process `TestClient` over the real app (so
the byte store and the derived tables can be looked at directly), and a
separate `uvicorn` child process plus a second SQL connection (so cross-process
durability is not an artefact of shared memory).

| case | what it shows |
| --- | --- |
| `test_upload_of_each_gold_payload_lands_as_durable_bytes_with_nothing_derived` | all four gold payloads → `200` with `status == "uploaded"`; the object store holds exactly the file's bytes (hash-compared); `fin_thread_attachment_unit` and `fin_thread_attachment_index` hold **zero** rows for that attachment; no `parsing`/`ready`/`failed` observed anywhere |
| `test_byte_adapter_is_the_one_the_product_itself_selected` | the store under test is the one `_build_default_attachment_service()` returns, with the `WARNING` from `workspace_http.py:2172` captured as the reason; a durable S3 adapter would be selected by the same call, unchanged |
| `test_refusal_at_the_deployed_cap_leaves_no_object_and_no_row` | `4,194,304` B accepted; `4,194,305` B → `413 attachment_too_large`, recording lens saw no `put`, table row count unchanged; also covers the `Content-Length`-over path and the streamed-overflow path |
| `test_upload_starts_no_work_behind_the_response` | `_NullGraph` is never touched, and a settle delay after the `200` still shows zero units and zero postings — nothing lands behind the response |
| `test_unsupported_bytes_upload_and_only_preparation_names_them` | the non-UTF-8 PNG is accepted as raw bytes with a `200`/`uploaded`; the named rejection `attachment_not_text` appears only when `accept()`/`prepare()` is driven, and only *after* the bytes were read back out of the byte store — which is itself proof the store held them |
| `test_empty_body_and_unknown_thread_stay_named_and_store_nothing` | `400 empty_attachment` and the unknown-thread path stay named and store nothing; a malformed request cannot leave a row |
| `test_concurrent_and_duplicate_uploads_land_as_distinct_rows` | the same bytes posted twice concurrently produce two distinct `attachment_id`s, both `uploaded`, both durable — completion is not deduplicated into a lost upload |
| `test_live_server_uploads_every_gold_payload_as_uploaded` | the out-of-process server accepts all four and each reads back as `uploaded` from a **second** SQL connection |
| `test_live_server_accepts_the_filing_the_retired_constant_rejected` | `2023_Q1_MSFT.pdf` (2,223,272 B) → `uploaded` across the real socket |
| `test_live_server_uploaded_row_is_not_a_recovery_candidate` | a real `PostgresAttachmentStore.incomplete()` never lists an `uploaded` row, with a `parsing` control row inserted first so the assertion discriminates rather than passing on an empty table |
| `test_module_leaves_the_real_tables_as_it_found_them` | row counts of all three tables after the module equal the counts before it: every row and every object the module created is gone, so the run is re-runnable — verified by running command 5 twice (16.87 s then 6.44 s, both `11 passed`) |

Teardown deletes every object key the lens recorded and asserts
`UnknownObjectKeyError` on re-read, and drops every thread it created (the
attachment tables are `ON DELETE CASCADE` from `fin_thread`).

### The Postgres twin, per case (command 3)

The 14 cases that the `FIN_CHECKPOINT_URL` gate hides:

* `tests/host/test_attachment_store.py:696,721,742,771,779,798,814` — 7 cases,
  all pass on real SQL rows.
* `tests/host/test_thread_attachment_durable_postgres.py:112,134,153,174,194,215,238`
  — 7 cases, all pass. Passing ones include
  `test_postgres_round_trip_uploaded_to_ready`,
  `test_postgres_incomplete_after_index_loss` and
  `test_postgres_incomplete_does_not_list_uploaded_rows`, i.e. the `uploaded`
  baseline and the non-recovery rule both hold on real rows. The first run of
  this list was 6/1: `test_a_parsing_row_is_resumed_by_the_next_process` was red
  for the reason in *Defect 1*, and is now green for the reason in that section.

### The boot gate, per case (commands 6-10, all out of process)

No `serve()` monkeypatch anywhere in this section: each row is a real child
process whose exit status and stderr were captured.

| env | exit | observed |
| --- | --- | --- |
| all four unset | 1 | `FIN_ATTACHMENT_MAX_BYTES must be set to a positive integer, got None` — the first key checked, and no bind |
| one unset ×4 | 1 each | names the dropped key; `…_VISION_CROP_CALLS_PER_RUN must be set to a positive integer, got None` is the fourth |
| `0` | 1 | `FIN_ATTACHMENT_MAX_BYTES must be between 1 and 9223372036854775807, got 0` |
| `-1` | 1 | `… must be an unsigned ASCII decimal integer, got '-1'` |
| `abc` | 1 | `… must be an unsigned ASCII decimal integer, got 'abc'` |
| `""` | 1 | `… must be set to a positive integer, got ''` |
| four valid, no DSN | 1 | 0 bound-gate hits; fails later at `MissingCheckpointURLError` |
| four valid + real DSN | 124 (timeout) | `Started server process [3714145]`, `Uvicorn running on http://0.0.0.0:8000`, graceful `Finished server process`; port free afterwards, no orphan |

The in-process twin of this, `test_attachment_budgets.py:940
test_serve_aborts_boot_on_an_unusable_bound_and_never_serves` (with
`:969 test_serve_boots_and_serves_when_all_four_bounds_are_finite`), is the
caller test plan line 75 asks for; commands 6-10 are its real-process witness,
which is where `uvicorn.run` is no longer a recorder.

### Whole-surface guards (command 14)

`1987 passed, 61 skipped, 238 deselected in 113.47s`, exit 0 — the 238
deselections include this module's 11 (command 4), so the new file contributes
nothing to the default run. The skips are the pre-existing `FIN_CHECKPOINT_URL`
and `web/dist` gates. Also re-checked: the delivered test deltas are additive
on the T1 evidence the plan asks to preserve
([`2026-09-25-g4-t1-thread-attachment.md`](2026-09-25-g4-t1-thread-attachment.md)).

## Web-side gates

Read from `web/src/store.ts` and `web/src/components/Composer.tsx`, then run:

* **Where send is blocked.** `store.ts:521` — `sendQuestion` calls
  `refuseUploadInFlight` (`store.ts:1457`), which throws
  `api.AttachmentUploadInProgressError` (client-decided, `409
  attachment_upload_in_progress`, `web/src/api.ts:714`) whenever
  `hasUploadInFlight(get().pendingUploads)` (`store.ts:1450`) is true. The
  counter is bumped in `attachFile` (`store.ts:~1008`) and released in a
  `finally` (`store.ts:~1033-1039`), so a failed upload cannot wedge the door.
* **Where a new conversation is blocked.** Same call at `store.ts:481`
  in `createThread`, so the gate is global to the composer rather than scoped to
  the thread the file is landing in — which is what spec line 25 requires
  ("even if the pending file is not referenced by the message").
* **What the user sees.** `Composer.tsx:59` selects the in-flight state, `:62`
  derives `held`/`sendLabel` through the pure `sendGate` (`Composer.tsx:33`),
  `:108` refuses an attach while one is
  already running, `:386` renders the `上传中…` label, and `:269-275`
  renders `ATTACH_UPLOAD_IN_FLIGHT_HINT` (`attachFailure.ts:119`,
  "附件正在上传，上传完成前不能发送。") under `data-testid="attach-upload-pending"`.
  `uploaded` is displayed distinctly from `ready` (`attachFailure.ts:97`
  `uploaded: "已上传，待解析"`).
* **T2a *Leftovers* 3 is carried.** The composer does display the
  one-large-filing-per-turn guidance: `ATTACH_ONE_FILING_HINT`
  (`web/src/attachFailure.ts:114`) is rendered at `Composer.tsx:360-361` under
  `data-testid="attach-one-filing-hint"` with the copy "一份大文件一轮更稳妥：同一轮里
  放两份最大尺寸的附件，会超出本轮视觉解析预算。" That closes the hand-off T2a
  recorded, as guidance only — there is still no per-message attachment-count
  limit anywhere, and none was claimed.
* **Verification.** command 12, `69 passed` across the three files that own
  these doors (`uploadDoors.test.ts` 35, `attachFailure.test.ts` 22,
  `sendFailure.test.ts` 12), and command 13, `tsc --noEmit` clean.

## The host-side gate

The composer half is client state and can be walked around by any other client,
so the server decides the same condition for itself. `AttachmentUploadInFlight`
(`src/fin/host/workspace_http.py:1911`) is a per-mount counter keyed by thread:
the upload door calls `enter(thread_id)` as its first statement after the thread
is resolved and `exit(thread_id)` in a `finally`, so every way out of the door —
`200`, `400`, `404`, `413`, `500`, or a client that hangs up mid-body — hands the
thread back. The send route refuses with `409
{"error": "attachment_upload_in_progress"}` at `workspace_http.py:2487`, after
the unknown-thread `404` and before tier, MCP status, `/compact`, the title
rewrite and the run-lock probe, so a gated send starts no run and writes no row.

Three cases in `tests/host/test_thread_attach.py` hold it, driven by a byte
store whose `put` blocks on an `Event` (`_BlockingPutStore`, `:559`) — no
sleeps, so the assertion is about the window, not about timing:

| case | what it shows |
| --- | --- |
| `test_send_is_refused_while_an_upload_is_in_flight` (`:602`) | while the bytes are still landing, `_model_saw_sentinel` is impossible: `409` with the named error, `model.seen_calls == []`, and the same send returning `200` with its attachment after the upload completes |
| `test_every_upload_refusal_leaves_the_send_gate_closed_again` (`:668`) | each named refusal (`400` empty, `413` over the cap, `404` unknown thread, `500` store failure) is followed by a `200` send, so one leaked counter cannot wedge a thread for the life of the process |
| `test_an_upload_in_flight_does_not_hold_another_thread` (`:734`) | the gate is per thread: a second thread sends `200` while the first is blocked, and the blocked thread releases to `200` once its own upload lands |

These are in the default hermetic run, so command 1's `335 passed` and command
2's `412 passed` include them; the live-door module does not exercise the gate
because its uploads all complete before any send.

## Defects found by the run, and what closed them

**Defect 1 — the Postgres twin of the durability suite asserted the
pre-change baseline.**
`tests/host/test_thread_attachment_durable_postgres.py`
(`test_a_parsing_row_is_resumed_by_the_next_process`) expected
`status == "parsing"` from an upload, and the first command-3 run answered:

```
>       assert stack["service"].status(attachment_id).status == "parsing"
E       AssertionError: assert 'uploaded' == 'parsing'
```

The in-memory twin was updated by the change; its Postgres mirror was not. This
is invisible in the default run because the file is `FIN_CHECKPOINT_URL`-gated,
which is precisely the mechanism the ticket exists to exercise: **a red that only
real storage can show.**

Closed by moving the premise, not the assertion. The case now drives a real
`prepare()` and then tears the index down, so the row a new process must resume
is a genuinely torn `parsing` row — the state that test exists to reach — and it
still asserts `restarted.recover() == 1`. Nothing was deleted or weakened: with
the old one-line premise the test would have passed for the wrong reason, and
with a weakened assertion it would pass for no reason.

**Defect 2 — the host half of the send gate was not delivered.**
The first `grep -rn "attachment_upload_in_progress" src/` returned nothing; the
flag existed only under `web/src/`. But
`web/src/api.ts:711-713` states *"the host enforces the same condition, so a
second tab cannot slip a send past a running upload"*, spec line 25 says *"The
host enforces the same condition"*, and plan line 75 requires *"both composer
and host block a send while any upload is pending"*. With enforcement only in
the browser, a second tab, a second client, or a direct `POST /ask` could start a
run while an upload was still writing its bytes.

Closed by `AttachmentUploadInFlight` — see *The host-side gate*. The design
question the previous version of this record left open (a pending-upload marker
vs. a lease) resolved to a process-scoped counter rather than a stored flag,
because "in flight" is a property of the request that is currently reading the
body, not of the attachment row: the row does not exist until the bytes land, and
a durable marker would need cleaning up after a crash that happened *before* it
was written. The consequence, stated plainly: **the gate is per uvicorn
process.** A `--workers N` deployment lets a send through on a worker that is not
running the in-flight upload, which is why *Multi-worker deployment* stays under
*Not covered*.

## Not covered by this record

* **A durable object-store adapter.** No S3-compatible bytes were involved in
  any assertion. See *Blocked leg* below: this is blocked evidence, not a green
  skip and not a Fake pass.
* **Graph-triggered preparation** — the `ensure_attachment_ready` node, the
  `uploaded → parsing` transition, run suspension, thread-lock release and
  auto-resume. That is **T2f**, and nothing here exercises it. Likewise the
  `parsing`/`ready` product path is only observed through the existing unit
  suites, never started by this ticket's door.
* **Page, vision-call and crop budgets at the door.** Only
  `max_upload_bytes` has a production caller now; `max_pdf_pages`,
  `max_vision_calls_per_run` and `max_vision_crop_calls_per_run` are read at
  boot and otherwise unused — their consumers are T2e/T2f, and the boot gate
  proves they are *deployed*, not that they *bind a run*.
* **Streamed overflow against a real socket with a chunked body that never
  declares `Content-Length`.** The 413-after-partial-read path is asserted
  through `_read_bounded_body` in-process; the out-of-process cases use declared
  lengths. A large real upload over a slow link is not reproduced.
* **The browser-level end-to-end gate.** The web evidence is vitest against the
  store and the failure mappers; no Playwright/lane-e2e run drove a real upload
  racing a real send button.
* **Restart durability of the bytes themselves.** `test_a_parsing_row_is_…`
  and `…_uploaded_row_is_not_a_recovery_candidate` prove row durability across a
  process boundary; with the in-memory adapter, cross-process *byte* durability
  is impossible by construction, and this record does not pretend otherwise
  (`source_unavailable` after restart is the documented consequence,
  `workspace_http.py:2172-2176`).
* **Multi-worker deployment.** One uvicorn process was booted, not the
  `--workers N` shape a real deployment uses.

### Blocked leg

```
Validation:
- Not run: a live upload against the durable S3-compatible byte adapter
- Expected command: FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… \
    uv run --no-sync pytest -m mcp tests/host/test_live_attachment_upload_door.py
- Blocking issue: no durable object store exists on this machine. The `object-store`
  extra (boto3) is not installed and adding it would be a new dependency, which this
  ticket does not authorise; no MinIO/S3 container is running (`docker ps` shows only
  fin-postgres-1, iknow-memory-postgres/es/neo4j, fin-g4-t1-iknowpg) and
  `docker pull minio/minio:latest` is refused ("pull access denied … repository does
  not exist or may require 'docker login'"). A locally invented fake S3 server was
  deliberately not substituted, because the claim being tested is durability, and a
  fake cannot witness it.
```

The consequence is bounded and named: every row-count, status, cap and
door-semantics assertion in *The live door module* is real; the assertion that
would read a stored object back from durable external storage after a restart
is the one that is missing, and it is owed to the next ticket that can reach a
real bucket.

## Leftovers

1. **The durable byte adapter leg.** Owner: **T2d** (the `docling`/runtime and
   deployment-configuration ticket) or the operator ticket that owns
   `FIN_OBJECT_STORE_URL`. It should re-run command 5 unchanged against a real
   bucket and add the cross-process byte read-back this one cannot make; the
   live module already drives `_build_default_attachment_service`, so only the
   environment is missing, not a harness.
2. **The `uploaded → parsing → ready` product path across the same real door**
   — including that `incomplete()` picks a torn `ready` up and that a resumed
   preparation rebuilds units and postings. Owner: **T2f**.
3. **The gate's process scope.** `AttachmentUploadInFlight` counts uploads in the
   process that received them, so a `--workers N` or multi-replica deployment has
   a window the host does not close. Owner: the ticket that moves the counter
   into shared state (a Postgres advisory lock or a row on the thread would both
   do); until then the deployment is single-process or accepts the window.
   Recorded here rather than fixed because the storage shape is a door the whole
   run-control surface shares.
4. **Per-run recording of page count, vision calls, usage/cost, latency and the
   4 GB resource peak** at a real preparation, which T2a priced but nobody has
   produced from the product. Owner: **T2e**.
5. **No attachment-count limit exists**; the one-large-filing guidance is now in
   the composer copy (T2a *Leftovers* 3 → delivered, see *Web-side gates*), but
   a turn that carries two max-size filings is still only refused late, by the
   vision budget, once preparation runs. Whether that deserves a door-side rule
   is an open product call. Owner: **T2e** (first ticket with a real call
   budget to enforce) with the decision recorded before the caps ship.
6. **Command 14's whole-suite figure is a wall-clock number from one host** and
   moves with load (the containment pair measured 18.30 s then 19.20 s across
   two runs in this session). Owner: none — recorded so a later reader does not
   treat a timing in this document as an assertion.
