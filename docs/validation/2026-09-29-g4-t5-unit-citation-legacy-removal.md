# G4 T5 — unit citations, `read_file` legacy removal, and the run-side visual lane

Date: 2026-09-29. Every "Commands" row ran on this branch on this date.

Branch: `worktree-g4-t5-unit-citation-legacy-removal` (base `231493b`). Commits,
in order: `98ded70` the host read, the model-facing tool, the `visual_claims`
channel, persistence, the wire, and the per-run `VisionCallLedger`;
`aab2764` the plan wording that folds the visual clauses back into item 10/10a;
`659d6cc` the web surface (unit-citation rendering plus the run-side visual
lane), landed after two s5-complexity regressions were refactored down;
`8d7a4ba` the repaired host/web delete-door pin; `3542a27` this validation
record, the measured artifacts and the JSONB key-order repair; `903a2bb` and
`9cdab6f` the two review repairs (a pre-migration citation row reaching the
history door and the export door); `f0ce761`, `74cf5f6`, `c9fba6b` and
`42ebed4` the follow-up corrections (documentation, the narrowed export
tolerance, the L1 test-shape note, and the authoritative full-suite run at the
tip). The PR head is `42ebed4`.

Inherited contract: `specs/thread-attachment-source-read.md`, ADR 0088 /
0089, and `plans/thread-attachment-source-read.md` items 10 / 10a. This
record consumes T4's delivered bounded read (`docs/validation/2026-09-28-g4-t4-bounded-source-read.md`).

Scope boundary honored: T4 left `read_file` and the generic `page` DTO alive
for T5 to remove. They are removed here.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | Two revisions, and this record does not blur them. **(a) Round evidence revision `8d7a4ba`** — the measured pipeline legs, the gold cases, the object store / thread store legs, the vision provider legs and the full default suite below were run at this revision, the branch tip that carried them. **(b) Repair revision `549b42d`** — the head after the T1 repair `d380615` (the two `logger.exception` exits) and the T2 crop delivery `0638436`; the local re-runs at "the repair head" recorded in *Local re-verification* were taken here. (a) and (b) are different trees, so a green row below at (a) is NOT evidence for (b) except where a row says it was re-run. |
| Python | 3.12.14; the default suite stayed hermetic (343 live-marked tests deselected) |
| Thread / attachment store | real Postgres `16.15` on `localhost:5433` (`g4-t5-unit-citation-legacy-removal-postgres-1`, healthy), 17 tables including `fin_run.visual_claims` |
| Object store | real SeaweedFS `4.43` S3 on `127.0.0.1:8333` (`fin-g4-t5-s3`), bucket `fin-g4-t2c`; the endpoint is anonymous (verified: `docker inspect` carries no credential variables, unsigned `PUT` returns 200), so the adapter's credentials are non-empty placeholders |
| Pinned pipeline image | rebuilt this session as `sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229`, tesseract `5.5.0`, tessdata gate `chi_sim` + `eng` + `osd` passed (`build.sh` exit 0) |
| Vision provider | real `https://api.minimax.cn`, model `MiniMax-M3`; credentials read by `--dotenv-start /home/winner/projects/fin`, never printed |
| Attachment gold dataset | `data/corpus/g4_attachment_gold/` — `fin-g4-attachment-gold-v1`, schema `1.0.0`, 4 cases / 8 physical pages / 5 pages needing OCR |
| Live iknow-memory | real stdio server at `/home/winner/projects/iknow-memory` @ `08cedb1`, node `v24.21.0`; **in-memory source store only** (see Limitations) |

## What was claimed, and the evidence for each clause

| Clause | Evidence (actual observed) |
| --- | --- |
| Generic `page` locator replaced by `unit`; `read_file` contract removed | `98ded70` — `read_source` is the only registered model tool (`tests/host/test_read_source_tool.py`, 34 test functions), no `page` field survives in the active DTOs |
| Citation gate rejects a quote that lives only in unread text, across an unseen gap, in another thread, or at a stale parsed version | `tests/host/test_citation_gate.py` — including the malformed-evidence and non-positive locator entries |
| Run-side visual lane: persisted observation read into the current run with its provenance token | `tests/host/test_graph_visual_lane.py`, `test_attachment_visual_read.py` — hermetic (Fake vision port). The live leg arrived afterwards as T3 at `f3b8b2e` (`tests/host/test_live_graph_visual_lane.py`), re-run at the R1 head `e22e03d`; see L8 and *At the R1 head*. |
| `visual_claims` reaches the wire end to end | `tests/host/test_visual_claims_wire.py` — SSE frame + DTO + `fin_run` column. The module has **17 test functions**; exactly **one** of them, `test_live_postgres_round_trips_the_claim_channel_without_a_second_shape` (`tests/host/test_visual_claims_wire.py:922`), round-trips the channel against real Postgres. The other 16 are hermetic — they drive the same channel in memory or against a fake connection, so the module total is NOT a real-store count. |
| Real vision provider, product path | `FIN_VISION_LIVE_ROUNDTRIP=1 pytest -k live_round_trip` — **1 passed** (`ChatModelVisionAdapter` against the configured provider; previously never run) |
| Real vision provider, probe | `scripts/g4_vision_probe.py` — **exit 0**, `supports_image_input=true`, `structured_output_ok=true`, `usage_reported=true`, mean answered `94186.50` against the gold expectation. THIS round's run is committed at `scripts/g4_pipeline/results/g4-vision-probe_20260929T124000Z.json` (gold image `g4-image-chart-001.png`, 3 calls). The other probe artifact in `results/` (`...20260928T232755Z.json`, untracked) is an earlier default-page run against different numbers — it is not this round's evidence. |
| Real thread + object store: upload / retrieve / delete | `tests/host/test_live_attachment_upload_door.py` — at `8d7a4ba` 13 passed, 1 failed (Limitations L1), re-run green at `d380615` (14 passed); `tests/host/test_live_attachment_retrieve_mcp.py` — **8 passed** over real SeaweedFS + real Postgres |
| Real bounded read over the live wire | `tests/retrieve/test_live_bounded_read_source.py` — **3 passed** |
| Durable restart / resume over real Postgres | `tests/host/test_attachment_resume_durable_postgres.py`, `test_attachment_jobs_boot_durable_postgres.py` — **3 passed** |
| Pinned page/region/OCR pipeline under the 4 GiB ceiling | 3 of 4 gold cases measured, all `container_exit_code: 0`, `docling_status: SUCCESS`, `oom_kill_events_in_cgroup: 0` |
| Web: unit citations + visual claims render | `web` full suite — **30 files / 505 tests passed** |
| Full default suite | `uv run --no-sync pytest -q` at code revision `74cf5f6` (recorded by `42ebed4`) — **1 failed, 4609 passed, 110 skipped** in 973 s; the one failure is L3, pre-existing and unrelated |

## Measured per-case results (pinned pipeline, `scripts/g4_pipeline/run_measured.sh`)

| Case | Pages | `container_exit_code` | `docling_status` | Regions (text/table/figure) | OCR pages | OCR chars | `cgroup_peak_bytes` | `peak_rss_vmhwm_bytes` | OOM |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `g4-native-text-001.pdf` | 1 | 0 | SUCCESS | 8 / 0 / 0 | 0 | 0 (1 native) | 1285955584 | 1338458112 | 0 |
| `g4-mixed-chart-001.pdf` | 3 | 0 | SUCCESS | 24 / 0 / 2 | 0 | 0 (3 native) | 1311711232 | 1402834944 | 0 |
| `g4-scanned-001.pdf` | 3 | 0 | SUCCESS | 0 / 4 / 0 | 3 / 3 | 15108 | 1382981632 | 1457524736 | 0 |
| `g4-image-chart-001.png` | — | 1 | — | — | — | — | — | — | — |

Ceiling for every measured row: `--memory=4g --memory-swap=4g --cpus=4`,
`cgroup_memory_max_bytes = 4294967296`, `cgroup_memory_swap_max_bytes = 0`.

The PNG is rejected by design — the pinned harness is PDF-only
(`pypdfium2` `PdfDocument` cannot open a PNG), so `container_exit_code = 1` is
this dataset case's expected outcome and its content answer is the vision
leg above, not a Docling conversion.

**Reproducibility against the 2026-09-25 committed artifacts:**
`g4-scanned-001` region counts match exactly (`text 0 / table 4 / figure 0`,
3 of 3 pages OCR'd); OCR text length moved 15390 → 15108 chars (0.5%), which
is the expected jitter from a rebuilt image. The 09-25 run recorded
`cgroup_memory_swap_max_bytes = -1` (swap unconstrained); this run recorded
`0`, so **the resource peaks above are the first ones taken under a genuinely
enforced swap ceiling** and the earlier numbers are not comparable to them.

## Commands

| Command | Result |
| --- | --- |
| `uv run --no-sync pytest -q` (round run at code revision `74cf5f6`) | 1 failed, 4609 passed, 110 skipped, 343 deselected — exit 1 (L3 only) |
| `cd web && npx vitest run` | 30 files / 505 tests passed — exit 0 |
| `cd web && npm run typecheck && npm run build` | exit 0 / `✓ built` |
| `node .../s5-complexity/check.mjs --changed --base 231493b` | 58 touched functions within baseline — exit 0 |
| `G4_IMAGE=fin-g4-pipeline:t2a bash scripts/g4_pipeline/build.sh` | `[build] ok` — exit 0 |
| `run_measured.sh` × native-text / mixed-chart / scanned / image-chart | 0 / 0 / 0 / 1 (PNG by design) |
| `scripts/g4_vision_probe.py --dotenv-start … --image g4-image-chart-001.png` | exit 0, mean answered 94186.50 |
| `FIN_VISION_LIVE_ROUNDTRIP=1 pytest -q tests/host/test_attachment_vision.py -k live_round_trip` | 1 passed — exit 0 |
| `pytest -q -m mcp tests/host/test_live_attachment_retrieve_mcp.py` | 8 passed — exit 0 |
| `pytest -q -m mcp tests/host/test_live_attachment_upload_door.py` | at `8d7a4ba`: 13 passed, 1 failed — exit 1 (L1). Re-run at `d380615` after the orphan children were removed: **14 passed — exit 0** (real Postgres + real SeaweedFS) |
| `pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py` | 5–6 passed, 0–1 failed — **flaky** (L2) |
| `pytest -q tests/host/test_attachment_resume_durable_postgres.py …` | 3 passed — exit 0 |
| `pytest -q -m mcp tests/retrieve/test_live_bounded_read_source.py` | 3 passed — exit 0 |

## Limitations / blocked evidence

Per `.claude/rules/test.md` these are blocked evidence — not passes, not skips.

**L1 — `test_live_single_attachment_delete_voids_the_real_bytes` failed with
`failure_reason: source_unavailable`; root cause is ENVIRONMENTAL, established
after this round was first written (`d380615`).** The paragraph this replaces
claimed the cause was undetermined and that "branch changes are ruled out";
both statements are withdrawn — the supporting reasoning (a source-diff reading,
not a reproduction) never established what it asserted, and the record's own
"the originating exception cannot be recovered" is precisely the diagnostic
gap that repair closed.

The mechanism, with evidence:

* Two **stale orphan uvicorn children** from an earlier session were still
  alive and sharing the SAME Postgres job table as the test's own child, but
  configured with a **DIFFERENT object-store bucket**. The row was claimed by
  a loser: the orphan's process read the key from its own (wrong) bucket and
  marked the row `source_unavailable` with an `UnknownObjectKeyError`, while
  the test's child — which had the right bucket — never got the job.
* Proof by process control, all three steps on the same command
  (`uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_upload_door.py`):
  `SIGSTOP` the orphans → the L1 door test **passes**; `SIGCONT` → it **fails 3/3**;
  remove the orphans → the whole module is **14 passed, exit 0** against real
  Postgres and real SeaweedFS.
* Base `231493b` is **not an equivalent baseline** for this test, so no
  branch-regression conclusion was ever supported by either direction of the
  comparison: at `231493b` the test asserted `'parsing' == 'ready'` — a stale
  assertion, since the door only queues and returns `parsing` (the T2f
  contract) — and the base's own child harness never started the prep worker,
  so the base run could not have produced a `ready` row either.

**A real product diagnostic defect was found and fixed in the same task**,
independent of the environmental root cause: both failure exits of the durable
preparation path discarded the exception they caught, so a row landing
`failed/source_unavailable` could not distinguish a missing object key from a
dead endpoint or a crashed adapter. `d380615` adds `logger.exception` at both
exits in `src/fin/host/attachment_prepare.py`, with attachment-scoped context
only — the typed `reason` on the row is unchanged, and no exception text,
token, or credential value is written. Failing-first tests pin both legs:
`test_a_crashed_job_logs_the_originating_exception` and
`test_unreadable_bytes_log_the_store_exception_with_a_traceback`
(`tests/host/test_attachment_prepare_jobs.py:352`, `:391`).

**L2 — `tests/host/test_live_attachment_graph_gate.py` is flaky, 2 of 3 module
runs.** The failing test differed on every run
(`test_bytes_that_cannot_be_read_land_as_a_named_refusal_turn`, then
`test_a_run_suspended_on_the_gate_completes_under_a_fresh_process`, then a
clean 6/6), and each failure passed in isolation. Order/timing dependent, not
reproducible on demand.

**L3 — `tests/corpus/test_ocr.py::test_default_pytest_does_not_import_pytesseract`
fails in the default suite.** Pre-existing and unrelated to this branch:
`tests/corpus/test_ocr_tesseract_gold.py:25` calls
`pytest.importorskip("pytesseract")` at COLLECTION time, so the sibling's
collection imports the package before the "no leak" assertion runs. Only
exposed because this session installed the optional `ocr` extra; the test and
its polluter are unchanged on `231493b`.

**L4 — no live iknow-memory STORE path.** Ports 5432 / 9200 / 7687 are all
closed, so only the in-process memory store was exercised. Per
`plans/thread-attachment-source-read.md`:190 the baseline
`test_live_research_cite_on_ingest.py` (verified: scripted model, in-memory
storage, real stdio session) is the substitution the plan refuses, and this
run is subject to the same limit for the company-source leg. Bringing up
`../iknow-memory`'s compose needs operator authorization.

**L5 — no dedicated 4 GB device.** The host is a 7.66 GiB WSL2 VM that was
swapping during the earlier session. Every measured row is a cgroup ceiling on
that VM, not a device reading; `docs/validation/2026-09-26-g4-t2a-*.md`
Leftovers 5 states this substitution is owed by a gold round on real 4 GB
hardware. The swap ceiling WAS enforced this run
(`cgroup_memory_swap_max_bytes: 0`), which the 09-25 artifact did not do.

**L6 — the live JSONB round trip had a wrong assertion, now repaired.**
`tests/host/test_visual_claims_wire.py:922` skips without `FIN_CHECKPOINT_URL`,
so this round ran it against real Postgres and it failed: the test asserted
`list(row) == list(VISUAL_CLAIM_FIELDS)`, i.e. JSONB key ORDER, but Postgres
normalizes object keys (length, then lexicographic) and does not preserve
insertion order. The value-equality assertion on the line above passed, so the
round trip itself was intact. The order assertion now uses a key SET on this
one leg only (`:982`) — the module still asserts the declared order in **nine**
other places, at `tests/host/test_visual_claims_wire.py:377`, `:413`, `:513`,
`:568`, `:631`, `:681`, `:759`, `:775` and `:886`, because there the guarantee
actually holds — so a row carrying a field the contract does not list still
fails. After the fix the single live leg passes against real Postgres; that
count is the one live case, not the module's 17 test functions (the other 16
never leave memory).

**L7 — the pinned image is not reproducible from the pin.**
`PIN_MANIFEST.md` and the committed `image_identity.txt` recorded
`sha256:8e10185f…`; rebuilding produced `sha256:0f278c22…` with three packages
drifted (`docling-parse` 7.21.0→7.22.1, `Faker` 40.39.0→40.40.0, `regex`
2026.9.10→2026.9.29). The pin names versions in prose but does not pin them
mechanically, so `build.sh` resolves newer wheels.

**L8 — the run-side visual lane had no live leg in this round; T3 closed
that at `f3b8b2e`.** As this round wrote it, the real provider was exercised
twice, but both crossings stop short of the lane that item 10a is about:
`FIN_VISION_LIVE_ROUNDTRIP=1` drives `ChatModelVisionAdapter.inspect`
directly, and `scripts/g4_vision_probe.py` is a script. Neither ran
`read_visual_detail` through the graph, so no run here had crossed a real
provider call, a real `fin_thread_attachment_observation` write, or the
re-render-proves-the-stored-hash reuse path against real stored bytes. Every
lane test in this round used the Fake vision port, so a deployment where the
persisted hash and the run lane's re-render disagree (say, a parse at one DPI
and a re-render at another) would have passed every test recorded here.

T3 then ran exactly that end-to-end leg. `tests/host/test_live_graph_visual_lane.py`
(`f3b8b2e`, opt-in under the existing `mcp` mark so the default run stays
hermetic) drives `read_visual_detail` through `compile_graph` with the tool
bound as the deployment binds it, `ChatModelVisionAdapter` (no fake port
anywhere in the module), `create_object_store` over the configured SeaweedFS
endpoint, `PostgresThreadStore` / `PostgresAttachmentStore` on the live
`FIN_CHECKPOINT_URL`, the production pypdfium2 renderer, and the committed
gold bytes with `sha256` asserted against the manifest. Command and result:

```text
FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… \
FIN_LIVE_VISION_ENV_FILE=… uv run --no-sync pytest -m mcp \
  tests/host/test_live_graph_visual_lane.py -v
→ 8 passed in 214.39s — exit 0
```

Dataset `fin-g4-attachment-gold-v1@1.0.0`, case `g4-mixed-chart-001`; real
provider `https://api.minimax.cn` model `MiniMax-M3`; real SeaweedFS `4.43`
bucket `fin-g4-t2c`; real Postgres `16.15` on `localhost:5433`. Page 3's
figure is a 528 × 328 pt box on a 612 × 792 pt page, so the inspected scope is
`[0.0686, 0.1263, 0.9314, 0.5404]`; the crop sent to the provider is 126,053 B
with `sha256` `61a02094d4dab021…`, and the whole-page raster is 1700 × 2200 px
at 200 dpi, 205,088 B, `sha256` `72b000ce49688014…` — the same digest the
T2a record already pins, re-rendered independently here and matching the row
the lane wrote.

The eight legs of that `f3b8b2e` run, all against those real boundaries
(the module carries 9 legs from `647bc12` onward, which split one of these
in two — so this list is the f3b8b2e run's, not the current module's):

1. **Insufficient evidence ⇒ inspection.** Exactly 1 provider call, on the
   crop (never the page: the sent bytes hash differs from the whole-page
   raster's), answering with the real plotted value `81,797`; the run ledger
   charged once and only for the crop (`calls_spent=1`, `crop_calls_spent=1`,
   `full_page_calls_spent=0`, `retries_spent=0`); a real observation row
   written at `observation_version=1`, read back both through the store and
   over a second SQL connection, carrying the same independently re-rendered
   page hash.
2. **Later turn ⇒ reuse.** A SEPARATE run with its own service, graph and
   ledger, same thread and same stored bytes: 0 provider calls, 0 charges,
   still exactly 1 observation row (`SELECT count(*)` = 1), `reused=True`,
   `outbound_calls=0`, `observation_version` still 1.
3. **A wider ask is not reuse.** A second ask naming no region is wider than
   the granted crop, so the lane re-inspected and persisted
   `observation_version=2`, and reported the truthful scope actually sent.
4. **The claim reaches the wire without becoming a quote.** The claim reaches
   `fin_run.visual_claims` on real Postgres and the wire projection
   (`_visual_claims_wire_payload`) with the full `VISUAL_CLAIM_FIELDS` shape,
   while `citations` stays `[]`, `state["evidence"]` is empty, and the claim's
   `kind` is not in `QUOTABLE_EVIDENCE_KINDS`.
5. **Hash mismatch ⇒ refuse and re-inspect.** The stored row's
   `page_image_sha256` was tampered with **in real SQL** to a digest no
   re-render can produce; the lane did not reuse it — 1 further provider call,
   `reused=False`, `observation_version=2`, and the fresh version's hash equals
   an independent re-render of the real bytes.
6. **Provider failure.** A real adapter with a deliberately invalid credential
   got a real 401 back: the tool payload is `{"error":
   "vision_call_failed"}`, no claim row, the attempt still charged
   (`calls_spent >= 1`), and nothing persisted.
7. **Budget exhaustion.** With the deployed bounds read from `.env.example`
   (`FIN_ATTACHMENT_VISION_CALLS_PER_RUN=89`,
   `FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN=8`), the run made exactly 8 real
   provider calls; the 9th ask was refused at the charge door with
   `visual_budget_exceeded` before any request, and exactly 8 observations
   were persisted.
8. **Cross-thread + tombstone.** Another real thread's attachment and this
   thread's tombstoned row both returned `attachment_unavailable`, drew 0
   pages, claimed nothing, and the tombstone really exists in real Postgres
   owning the object key.

The same run also executed T2's `record_regions` / `regions` /
`clear_regions` SQL and DDL, which no earlier run had:
`uv run --no-sync pytest -q tests/host/test_attachment_store.py -k region`
against real Postgres → **12 passed** (of 87 in the module), and
`uv run --no-sync pytest -q tests/host/` → **2870 passed, 100 skipped**, exit 0.

**What T3 declared non-real, and this record keeps visible:** the run's *plan*
is a `ScriptedChatModel`, because the object under test is the lane the run
takes, not the planner that chooses to take it, and the model-facing retrieve
is the `FakeRetrievePort` — the visual lane never reaches it. Every face the
lane itself touches (provider, bytes, store, renderer) is real, but no reading
of this paragraph may be taken to mean the planner, the router, or the
retrieve path was exercised against a real model or a live iknow-memory.

**L9 — the crop half of "the scope actually looked at" was not delivered in
this round; delivered by T2 afterwards (`0638436`).** As this round wrote it,
the lane always inspected the whole page (`crop=None`) because, as its own
comment records, it had no persisted region boxes to crop from. A caller that
supplied a valid `scope` therefore got a whole-page scan and a `scope=None`
token, so the token understated nothing but also did not report the region the
request named. T2d's plan text records that the figure/table boxes DO exist in
the layout record (`plans/thread-attachment-source-read.md`:100), so this was
a gap between the acceptance wording and the implementation, not a documented
limitation. `0638436` closed it: the store now persists one version's page
boxes, the crop lane selects the tightest qualifying figure box inside the
run's named scope and cuts it with the core's own `crop_png`, and with no
qualifying box it inspects the whole page the spec permits and records
`scope=None` — so a fallback is never readable as a crop. The scope a claim
carries is the region actually sent.

## OPEN OPERATOR DECISION — not resolved by this round

Nothing in this section is a pass, a skip, or a scope reduction. Both items
are recorded so the operator decides, and neither is treated as closed by any
test in the tree.

**O1 — A persisted `table` region can never be cropped; the plan's acceptance
wording says "a reliable persisted table/figure region" and was not narrowed.**

* **Where the behaviour lives:** `src/fin/corpus/parsers/_pages.py:850` —
  `if region.kind != "figure": return False`, reached from the public
  `crop_is_legible` at `src/fin/corpus/parsers/_pages.py:870-881` and from
  the lane's box filter `_reliable_boxes` at
  `src/fin/host/attachment_visual_read.py:370-400` (the drop is at
  `attachment_visual_read.py:396`, `if not crop_is_legible(region): continue`).
  Note the review that raised this cited the drop as a `kind != "figure"`
  predicate in `attachment_visual_read.py`; that predicate is not there — the
  exclusion lives in the shared core rule and the lane inherits it. The
  behaviour is the same; the file:line was not.
* **Observed, not inferred:** a `Region(kind="table", 10, 10, 500, 400)` —
  680 x 1083 px at the 200 dpi the core renders, far above the 160 px floor —
  returns `crop_is_legible(...) is False`, while the byte-identical box with
  `kind="figure"` returns `True`. `REGION_KINDS` is
  `('text', 'table', 'figure')`, so the store accepts and persists a `table`
  box; it is then un-croppable forever.
* **The conflict:** `plans/pr321-merge-followup.md:47` (T2 Acceptance) reads
  "A valid request with a reliable persisted **table/figure** region inspects
  that crop", and its Alternative clause states that reducing scope "requires
  an explicit operator decision and recorded spec/plan changes. A limitations
  paragraph alone does not satisfy the existing crop contract." The same
  section's Status is `[x] done — 0638436`.
* **Why the current behaviour is defensible but not a resolution:** the
  detail contract is a plotted value — `VisualDetail` is
  `(chart, series, category)` and its `target_key()` is
  `plotted_value_key(...)` (`attachment_visual_read.py:113-127`). A table box
  holds no plotted value today, so cropping one would send a provider pixels
  whose answer the ask cannot key on, and the core's own comment states the
  reason for the rule: "only a figure can hold text drawn as pixels; a text or
  table region's own layer, when the page has one, was read page-level"
  (`_pages.py:852-853`). The fallback is honest: with no qualifying box the
  lane inspects the whole page and records `scope=None`, so a fallback is
  never readable as a crop.
* **What is therefore undecided:** whether the plan wording should be narrowed
  to "figure region" (recording a spec/plan change, per its own Alternative
  clause), or whether a `table` crop should be implemented once a detail
  contract can key on a table's values. `plans/thread-attachment-source-read.md`
  item 10/10a is left **pending** and untouched by this round. The file
  `src/fin/host/attachment_visual_read.py` and
  `tests/host/test_attachment_visual_read.py` are owned by another agent on
  this branch and were not edited here, so no implementation or test change
  accompanies this note.

### O1 resolution — operator ruling recorded 2026-09-30

The operator accepted figure-only crop with the current honest whole-page
fallback for table regions. The contract/status wording that overstated table
crop was reconciled in `plans/pr321-merge-followup.md` (T2 Acceptance, T2
Status, L9 row) and `plans/post321-real-browser-acceptance.md` (Q1, B3
dependencies); the contradictory T2 done marker was corrected so no artifact
claims table crop is delivered. Table crop is retained as a recorded BACKLOG
enhancement — explicitly not required acceptance and not a B3/Session-D
blocker. O1 is closed by this recorded decision. The spec was not narrowed:
the ruling matches `specs/thread-attachment-source-read.md`:55-58 as written
(chart/region-centric — "a high-resolution crop of each relevant
chart/region (or the full page if no reliable box exists)").

This subsection is the canonical home of the backlog scope; other artifacts
reference it rather than restate it. A future bounded table-crop ticket needs:

1. a table-value detail key contract — `plotted_value_key(...)` in
   `src/fin/host/attachment_observations.py` and the chart/series/category
   `VisualDetail` it keys in `src/fin/host/attachment_visual_read.py`
   (`:121`); a table box holds no keyable detail today;
2. legibility-rule widening in `src/fin/corpus/parsers/_pages.py`
   (the figure-only `region.kind` rule behind `crop_is_legible`);
3. crop selection in `src/fin/host/attachment_visual_read.py`
   (`_inspected_crop` / `_reliable_boxes`);
4. independently expected real table gold data (current measured table
   recall is 0 on `g4-mixed-chart-001` p2);
5. failing-first host tests plus a live-lane leg.

## Merge gates

Every row names the code revision its result was taken at. `round` = `8d7a4ba`
(the round evidence revision above); `repair` = `549b42d`. A `round` row is
**not** evidence for the repair head unless it is marked as re-run there.

| Gate | Revision | State |
| --- | --- | --- |
| Full default suite | round (`74cf5f6`), re-run at repair (`549b42d`) | 1 pre-existing unrelated failure at both (L3); **not green at either revision** |
| Full web suite | round | green (505) |
| s5-complexity gate | round | green on `231493b..HEAD` |
| Real object store + thread store | round, delete leg re-run at `d380615` | green on the retrieve leg; the delete leg failed 1 at `8d7a4ba` and passes 14/14 at `d380615` after the stale orphan children were removed (L1) |
| Real vision provider (adapter + probe) | round | green — first time exercised live |
| Real vision provider through the run-side lane | T3 at `f3b8b2e` (after the repair head), re-run at the R1 head `e22e03d` | **satisfied** — 8 passed in 214.39 s, exit 0, over real provider / SeaweedFS / Postgres at `f3b8b2e`; re-run at `e22e03d` as **9 passed, 0 skipped** in 68.57 s, exit 0, after `647bc12` split one leg in two (L8) |
| Real bounded read over live MCP | round | green |
| Real Postgres round trip of the claim channel | round | green after the L6 assertion repair — the single live case `test_live_postgres_round_trips_the_claim_channel_without_a_second_shape`; the module's other 16 test functions are in-memory |
| Restart / resume over real Postgres | round | green |
| Pinned page/region/OCR pipeline | round | green on 3 of 4 cases; the PNG is out of the harness by design |
| Live iknow store path | round | **not satisfied** (L4) |
| 4 GB device reading | round | **not satisfied** (L5) |
| `read_file` / generic `page` removal | round | green |
| Jev live round-trip (`-m jev_live`) | never run | **not exercised** — see *Search-mode and Jev state* below |
| `pg_es` global / real-SQL search modes | never run | **not exercised** — see *Search-mode and Jev state* below |

Status of all nine limitations, reconciled with
`plans/pr321-merge-followup.md`'s follow-up table:

* **L1 — resolved as environmental, after this round was first written.** No
  product defect remains open here; the repair `d380615` closed the diagnostic
  gap that blocked the diagnosis. T1's evidence is in the L1 paragraph above.
* **L2 — open.** `tests/host/test_live_attachment_graph_gate.py` is flaky
  (order/timing); an isolated rerun does not establish module stability. A
  named follow-up is required if it blocks the real lane.
* **L3 — open, pre-existing, inherited.** The default suite does not exit 0
  (re-confirmed at the repair head: 1 failed, 4635 passed, exit 1), so item
  10's "exit 0" acceptance is UNMET. `git diff 231493b` over
  `tests/corpus/test_ocr.py` and `tests/corpus/test_ocr_tesseract_gold.py` is
  EMPTY — the test and its polluter are byte-identical on the base, so the
  cause is established and unrelated to this branch. The fix is a separate
  narrow slice, and this record does not accept the exception on the branch's
  behalf.
* **L4 — open (blocked evidence).** No live iknow `pg_es` store leg; the
  in-process memory store is a substitution the plan refuses. Needs the
  iknow-memory compose up (operator action).
* **L5 — open (blocked evidence).** A swap-enforced cgroup run on a 7.66 GiB
  WSL2 VM is not a 4 GB device measurement. Owed by a gold round on real
  hardware.
* **L6 — closed within this round** (the wrong key-order assertion was found
  and repaired; see the L6 paragraph). Nothing outstanding.
* **L7 — open.** `PIN_MANIFEST.md` pins versions in prose only, so a rebuild
  drifts. Follow-up for mechanically reproducible pinning with rebuild
  evidence.
* **L8 — closed by T3** (`f3b8b2e`) after this round was first written. The
  run-side visual lane now has a live leg: `read_visual_detail` driven
  through the shipped graph against a real provider, real SeaweedFS bytes and
  a real Postgres, 8 passed in 214.39 s (re-run at the R1 head `e22e03d` as
  9 passed in 68.57 s — see *At the R1 head* below). The residual, stated rather than
  hidden: that run's PLAN is a `ScriptedChatModel` and its model-facing
  retrieve is the Fake port, so this leg proves the lane, not the planner or
  the router. That is T3's declared scope, and it is why L8 stops being an
  open product defect while items 10/10a stay pending — the full original
  acceptance is wider than this lane.
* **L9 — closed by T2** (`0638436`) after this round was first written: a valid
  request with a reliable persisted figure region now inspects that crop and reports
  the actual scope; a table region uses the permitted whole-page fallback and
  reports that actual scope (operator ruling 2026-09-30 — see the O1 resolution
  above; table crop is recorded backlog, not delivered).

Item 10 / 10a status lines in `plans/` are NOT flipped by this record — and
not by T3's `f3b8b2e` either, which proves the LIVE LANE, not the full
original acceptance. The green rows above are PARTIAL delivery: the unit
locator, the `read_file` removal, the wire/persistence of `visual_claims`,
the measured pipeline and (since `f3b8b2e`) the live visual lane are in, while
L3 leaves the default suite non-zero and L4 / L5 / the `pg_es` and `jev_live`
legs leave `## End of round` clauses without the evidence they require.

## Search-mode and Jev state (folded in from the PR description)

The PR #321 description carried the only copy of this state, and a PR body is
not a tracked artifact. It is recorded here so a successor does not have to
read the PR to find it. None of it was exercised in this round.

**The four 检索档 (search modes).** The mode tier itself (Soft-Track cascade:
rules → **Jev** Choice → inherit → clarify/typed fail) is implemented and the
hermetic layer was green (145 tests this round).

| Mode | Product path | Committed live coverage | Gap |
| --- | --- | --- | --- |
| A 查找 (lexical-first) | implemented | `tests/retrieve/test_live_search_modes_pg_es.py::test_lexical_tier_real_es_hit_cites_the_parent_body` | none in product |
| graph 知识问答 | implemented (default when no key) | same suite, `test_graph_tier_sends_search_mode_on_the_wire_and_matches_the_default` | graph-priority *reordering* is validated on iknow's side (iknow #242 task 3), not in fin |
| global 综述 | **not built on fin's side** — fin refuses `global` 「档未开」 before the wire, while iknow itself accepts the token and answers `community_summary_index_failure` | partial (server-side only) | the 综述 product path (index-time community summaries behind fin's port) does not exist |
| D 真数据库 (sql) | **not implemented** — only the correctly named refusal `real_db_not_wired` | refusal verified with the full stack live | the whole real-SQL tier is future work |

The `pg_es` live suite itself was NOT run — it needs the iknow-memory PG+ES+
Neo4j compose, and ports 5432 / 9200 / 7687 were all closed. The legs in the
table are the suite's committed coverage, not this round's runs.

**`jev_live`.** ADR 0080 / 0081 make the live Jev round-trip required for the
档 judge's product path. The suite (`tests/host/test_live_jev_search_mode.py`,
marked `jev_live`) makes missing credentials a named FAILURE, not a skip. It
is opt-in via `FIN_JEV_API_KEY` / `FIN_JEV_MODEL` and **was not run this
round** — the default suite only injects the shared Fake Choice. One command
once credentials exist:

```text
uv run pytest -q -m jev_live tests/host/test_live_jev_search_mode.py \
  tests/host/test_live_jev_track.py tests/retrieve/test_live_jev_tier_reaches_wire.py
```

This is an existing search-mode obligation, not a new prerequisite for the
crop work. The global 综述 and D 真数据库 tiers are separate product work and
are not implementation scope for this branch.

## Local re-verification by revision

Run by the PR-321 T4 reconciliation task, which changed documentation only. No
code, lockfile, or settings file was touched, so these are confirmation runs,
not new product evidence; the real-boundary legs in the tables above still
stand only as recorded there. Results are grouped by the revision they were
taken at, and a row is evidence only for its own revision.

### At the repair head (`549b42d`)

| Command | Result |
| --- | --- |
| `uv run --no-sync pytest -q` | **1 failed, 4635 passed, 117 skipped, 350 deselected** in 901.66 s — exit 1. The single failure is `tests/corpus/test_ocr.py::test_default_pytest_does_not_import_pytesseract` (L3), unchanged. The counts differ from the round's `74cf5f6` run (4609 / 110 / 343) because this head also carries the T1 and T2 work. **Item 10's `uv run pytest -q` exit-0 acceptance therefore remains UNMET at the repair head**, for the same pre-existing and unrelated L3 cause; it is recorded, not accepted. |
| `cd web && npx vitest run` | 30 files / 505 tests passed — exit 0 (identical to the round's count) |
| `cd web && npm run typecheck` | `tsc --noEmit` — exit 0 |
| `cd web && npm run build` | `✓ built in 2.56s` — exit 0 |

`web/node_modules` is a symlink to the main checkout's, which `.gitignore`:75
documents as the worktree pattern; it is ignored and was not added.

Validation:
- Not run: `node .../s5-complexity/check.mjs --changed --base 231493b`
- Expected command: as above
- Blocking issue: no `s5-complexity` skill exists in this checkout
  (`find` over the repo returns no `check.mjs`), so the complexity gate is
  **carried forward from the round at `8d7a4ba`, not re-verified here**. This
  delivery is documentation-only and adds no executable logic, so the gate's
  subject matter is unchanged.
- Not run: the real-boundary legs (live Postgres / SeaweedFS / vision provider /
  `pg_es` / `jev_live`). Expected commands are in the *Commands* table above.
  Blocking issue: T4 is local verification of a documentation change, and the
  live legs were already exercised at the revisions named there; the
  run-side visual lane's live leg was T3's subject and landed after this
  re-verification, at `f3b8b2e` (see the L8 section above).

### At T3's head (`f3b8b2e`)

Run by the T3 acceptance task that added
`tests/host/test_live_graph_visual_lane.py`; recorded here so the revision and
its result are not confused with the repair head above. T3's own write-up is
the commit message of `f3b8b2e`; the L8 section above carries the leg-by-leg
detail.

| Command | Result |
| --- | --- |
| `FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… FIN_LIVE_VISION_ENV_FILE=… uv run --no-sync pytest -m mcp tests/host/test_live_graph_visual_lane.py -v` | **8 passed** in 214.39 s — exit 0, over real MiniMax-M3, real SeaweedFS `fin-g4-t2c` and real Postgres `localhost:5433` |
| `uv run --no-sync pytest -q tests/host/test_attachment_store.py -k region` | 12 passed — exit 0 against real Postgres (T2's previously unexecuted region DDL/SQL) |
| `uv run --no-sync pytest -q tests/host/` | **2870 passed, 100 skipped** — exit 0 |

This delivery (the L8 fold-in) is documentation-only and adds no executable
logic, so it neither re-ran nor re-validates those legs; the rows above stand
as T3 recorded them at `f3b8b2e`.

**The leg count above is scoped to `f3b8b2e`, where it was 8.** Commit
`647bc12` split the 146-line leg into two through a `crop_run` fixture, so the
module carries **9** test legs from `647bc12` onward. The `f3b8b2e` run below
is not restated as 9 — it measured 8 at the revision it was taken at.

### At the R1 head (`e22e03d`)

Independent re-verification of the live visual lane after `647bc12` and
`1a88c7b` landed, run by the R1 acceptance task. The module's 9 legs were
re-run live against real boundaries, and the three High repairs from `1a88c7b`
were checked on real data. This delivery is documentation-only; no code or
test was changed here.

| Command | Result |
| --- | --- |
| `FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… FIN_LIVE_VISION_ENV_FILE=… uv run --no-sync pytest -m mcp tests/host/test_live_graph_visual_lane.py -v` | **9 passed, 0 skipped** in 68.57 s — exit 0. The provider was genuinely remote: `resolve_vision_model_config()` returned `MiniMax-M3` at `api.minimax.cn`. Real SeaweedFS `fin-g4-t2c`, real Postgres `localhost:5433`. |

The 9 vs 8 difference is the `crop_run` split from `647bc12`, not a dropped
leg: the `f3b8b2e` run above still reads 8 because that is what it measured at
its own revision.

**Real MCP session through the production composition root.** The full door
chain ran end to end — upload → parsing → **ready v1, `unit_count=3`,
`searchable=true`** in 55 s. A real SSE stream followed, whose `visual_claims`
frame carried `observation_version=2`,
`scope=[0.106046, 0.183712, 0.904412, 0.516667]`, `provider=MiniMax-M3`,
`page_image_sha256=72b000ce49688014…`, `reused=false`, `outbound_calls=1`.
Real Postgres then showed 4 observation rows, 2 region rows,
`fin_run.visual_claims` with 1 entry, and `citations` as `[]`.

**Byte identity of the crop.** The independently cut crop and the
provider-sent crop are both `sha256` `61a02094d4dab021…`, 126,053 B,
1466 × 911 px. What the provider was shown is the crop the record's crop
produced, not a re-derivation of it.

**The three High repairs, on real data.**

* A 159 px edge → **not** cropped.
* A 161 px edge → cropped, with scope `(0.163399, 0.126263, 0.258105,
  0.199444)` — the first case above the threshold, confirming the rule has a
  real edge and not a blanket accept or reject.
* A box belonging to a superseded version → **not** cropped; cropped exactly
  once the owning version's regions were recorded, and the resulting scope
  matched the expected value within `1e-6`.
* A real PDF with `CropBox` ≠ `MediaBox` rastered at 1112 × 1667 against a
  1 px tolerance → the typed `page_unavailable` refusal, 0 provider calls, 0
  evidence rows, warning logged. The page box is now load-bearing, not
  decorative.
* A `/Rotate 90` control: both authorities agree to 0.00 px and the crop
  proceeds normally.

**`clear_regions` removal confirmed.** Zero references remain in `src/` and
`tests/`, the attribute is absent at runtime, and cascade cleanup was
verified live — attachment, unit, index, observation and region rows all went
to 0.

**Two environment facts that will bite the next operator.** The worktree venv
was missing BOTH the `[object-store]` extra (`boto3` absent — the live object
store cannot run without it) AND the `[ocr]` extra (`pytesseract`).
`uv sync --frozen --extra object-store --extra ocr` fixes both and leaves
`uv.lock` unchanged. Recorded so a future live run is not silently skipped
for a missing dependency.

**One provider flake, not reproducible, recorded as a non-defect
observation.** The rotated control returned `vision_call_failed` once and
passed on rerun; a direct provider call on the same crop succeeded both
times. This is neither counted as a pass for that attempt nor reported as a
product defect — the module result above is the clean rerun, and this line
keeps the flake visible rather than dropping it.

Validation:
- Not run: the remaining live legs (`pg_es`, `jev_live`, the live iknow store
  path, the 4 GB device).
- Expected command: the commands in the *Commands* table above.
- Blocking issue: L3, L4, L5, `jev_live` and `pg_es` are unchanged by this
  revision and remain unsatisfied; this run does not discharge them, does not
  claim full G4 acceptance, and does not flip items 10/10a, which stay
  `Status: [ ] pending`.

## Merge recommendation

**Squash merge, with the bounded scope and evidence stated below. This is a
recommendation only — it is NOT permission to merge, and no push, merge,
release, or deployment is authorized by this record.**

**Scope of what is being merged, accurately bounded.** Plan items 10 and 10a
are **PARTIAL delivery, not complete**. What is in and evidenced: the generic
`page` → `unit` locator with the legacy `read_file` contract removed; the
citation gate; the `visual_claims` channel across wire, DTO, `fin_run` column
and export; web unit-citation rendering and visual-observation display; the
pinned pipeline measured on 3 of 4 gold cases; the real vision adapter and
probe; the real object-store and Postgres legs; the T1 diagnostic repair; the
T2 crop delivery; and T3's live visual-lane leg at `f3b8b2e`. Items 10 and 10a
stay `Status: [ ] pending` in
`plans/thread-attachment-source-read.md`, and neither this record nor T3
flips them.

**Product defects that remain.**

* **L2.** `tests/host/test_live_attachment_graph_gate.py` is order/timing
  flaky; each failing test passed in isolation and a clean 6/6 was also
  observed, so module stability is not established.
* **L7.** The pipeline image is not reproducible from its own pin.

**L8 is no longer an open product defect.** It was listed as one because no
run had crossed the run-side visual lane against real boundaries. T3
(`f3b8b2e`, `tests/host/test_live_graph_visual_lane.py`, 8 passed in 214.39 s,
exit 0; re-run at the R1 head `e22e03d` as 9 passed in 68.57 s, exit 0, over
the same real boundaries — see *At the R1 head* above) now has, and it closes
exactly the concern this record raised: a
deployment whose persisted `page_image_sha256` disagreed with the run lane's
re-render would NOT have passed T3's run, because the reuse leg re-proves the
hash against real stored bytes and a row tampered with in real SQL is refused
and re-inspected. **What T3 proved:** `read_visual_detail` through the shipped
graph against a real provider, real SeaweedFS bytes and a real Postgres, on
the committed gold dataset — inspection on insufficient evidence with a
charged ledger entry and a real observation write; later-turn reuse with 0
provider calls and no new version; a wider ask correctly refused reuse; the
claim reaching `fin_run.visual_claims` and the wire without becoming a source
quote; and four failure legs live (hash mismatch, provider 401, budget
exhaustion, cross-thread + tombstone). **What T3 did NOT prove:** the run's
plan is a `ScriptedChatModel` and its model-facing retrieve is the Fake port,
so the planner and the router were not exercised against a real model or a
live iknow-memory. Full G4 acceptance is therefore still outstanding and is
not claimed here: L3, L4, L5, `jev_live`, `pg_es`, L2's graph-gate flakiness
and L7's image pin all remain exactly as described above.

L1 is **not** an open product defect: its cause was environmental (stale
orphan uvicorn children racing the shared job table against a different
bucket), and the one genuine product defect it exposed — both preparation
failure exits discarding the exception — is fixed in `d380615` with
failing-first tests.

**Evidence gaps that remain.** L3 (the default suite does not exit 0, so item
10's stated "exit 0" acceptance is unmet — pre-existing and inherited, not
accepted here); L4 (no live iknow `pg_es` store leg); L5 (no dedicated 4 GB
device); and the `jev_live` round-trip and `pg_es` search-mode legs, which
were never run. The `## End of round` gold set is therefore **not** complete:
four of its clauses have no required evidence. T3's leg discharges the L8
clause among them; it discharges none of the other three.

**If the operator prefers a staged merge** while the G4 evidence is
outstanding, the correct description is exactly the above — partial delivery of
items 10/10a with 10/10a still pending — not "G4 T5 complete". A full G4
completion claim is not supportable at this head.
