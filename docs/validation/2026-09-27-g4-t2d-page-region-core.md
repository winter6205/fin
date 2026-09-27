# G4 T2d page and region parse core — fin (2026-09-27)

Validation record for plan
[`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T2d (Parse physical pages with Heron and CPU OCR)** on branch
`feat/g4-attachment-t2d`.

Worktree `/home/winner/projects/fin-wt-t2d-k1-record`, record branch
`docs/g4-t2d-record` based on the merged tip `caaf7c7`. Every "Commands" row
below was executed on this worktree at the claim commit `70d65cd`, whose tree
is identical to `caaf7c7` — this file is the only content the record commit
adds.

Inherited contracts: ADR 0089 (lazy page/region preparation), the T2a record
(page and call budgets, the pinned pipeline, the four measured gold numbers
this ticket chooses against), the T2b record (upload door), and the T2c record
(preparation-service refusal ladder and the live-door harness this record's
door rows build on).

## Scope

What T2d delivers:

* the side-effect-free PDF **page/region extraction core** in
  `src/fin/corpus/parsers/_pages.py` (ports, rasterizer, region OCR gate,
  per-page frozen units) and its pure-Python test surface;
* the **PDF signature authority** — one marker constant and one 1024-byte
  window predicate (`_common.py`) that the lane gate and the public predicate
  share;
* `min_text_chars` as a **required, keyword-only** argument of
  `detect_ocr_required`, with every caller naming its own price;
* the **standalone-image lane**: a bare PNG/JPG/JPEG is one page whose raster
  is the uploaded bytes, recognized once;
* the **Heron layout adapter** (`src/fin/host/attachment_layout.py`): one
  container crossing per document pass, boxed `table`/`figure` regions read
  from the pinned measured record, and a named per-pass **timeout**;
* the attachment adapter's wiring of the core into
  `AttachmentPreparationService` **behind a deliberate refusal door**: an
  unwired deployment answers a pdf-family document with the named code
  `attachment_page_lane_unwired` and spends nothing; image-named payloads
  keep the content-ladder answer (the refusal is judged per document
  family — a standalone image is one page by construction and waits on
  no lane economics). *(Amended after delivery, 2026-09-27: the per-family
  split landed with the r5 repair; before it the refusal preempted
  `attachment_not_text` for every named payload.)*
* gold case 4, `g4-native-text-001`, with a deterministic builder;
* the shipped Tesseract configuration (mode, language set, render DPI) chosen
  by measurement, which closes the configuration half of T2a *Leftovers* 4;
* three review-round corrections: the pre-launch container reap in the
  harness, the web comment's door-exclusivity claim, and the plan prose.

What T2d does **not** deliver: the page lane is not wired at the composition
root — that is T2e's, by decision, and the refusal is the shipped behavior
(see *The refusal door*). Region-raster persistence, the `text`-box shape, the
page-cap pricing at the wiring, and the dedicated-device reading stay with
T2e/T2f (*Limits and leftovers*).

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | Every command in "Commands" ran at `70d65cd` on this worktree (tree content `caaf7c7`). Sub-branch captures quoted by SHA (`cecff0e`, `9459ce3`, `dfcb1f2`, …) are the merged first-parent commits listed in *Branch composition* and are named where a row depends on one |
| Python | 3.12.14, `uv run --no-sync pytest` 9.1.1; the default suite stays hermetic — no network, no live model, no container is required by any row below except where a committed artifact is re-read from disk |
| Gold data | committed `data/corpus/g4_attachment_gold/`, manifest now `"cases": 4` (`manifest.json:25`); `g4-native-text-001.pdf` sha256 `b4561e23331f0a0d…abceb011` (`manifest.json:280`), rebuilt byte-identically by `scripts/build_g4_native_text_gold.py` (command 5) |
| Pinned pipeline | `fin-g4-pipeline:t2a` (docling 2.130.0, Heron `@8f39ad3c`, tesseract 5.5.0, CPU) as recorded by T2a; the timeout and OCR-price rows below re-read that ticket's committed `scripts/g4_pipeline/results/*` artifacts rather than re-running the container |
| Web | `web/` vitest 3.2.7 in the node environment `vitest.config.ts` declares; this worktree had no `node_modules`, so command 4 symlinked the main checkout's (setup action only, removed after; `git status` clean) |

## Branch composition

Merged into `feat/g4-attachment-t2d` first-parent, oldest relevant first:

| merge | content |
| --- | --- |
| `262154c` (`8ca0eed`), `41569a9` (`9556722`), `3541cc7` (`f0f1d5c`) | the page/region core branches (`feat/g4-t2d-core`): ports, renderer, region OCR gate, the name-vs-bytes PDF refusal, the Heron-crossing cost record |
| `72cf872`, `dfcb1f2` (tip `dc986c4` of `feat/g4-t2d-ocr`) | the harness trap repair and the OCR configuration branch: region-capable engine, shipped `--psm`/lang choice, the four committed `ocr_config_measurement_*` sweeps |
| `c6712f4`, `e00593b`, `173431c` | the T2c surface and its repair round, carried in as the base the door rows build on |
| `cecff0e` (`b160ab0`) | the attachment adapter wiring plus the per-arm door-type pins |
| `9459ce3` (`0efde56`) | the Heron layout adapter and its 900 s per-pass timeout |
| `e10deb6` / `084e5e5` / `513627a`, `ec4b466` | the authority merge (`73d65a6`), the required-threshold merge (`5e4562d`), the plan reconciliation merge (`ec05446`), and the ingest-call site that still owed the keyword |
| `e802b94` (`201221b`), `b547cf8` (`8281499`), `84d0262` (`f15b2af`), `caaf7c7` (`169733f`) | the four review corrections: web door-exclusivity, gold case 4, pre-launch reap + docstring, plan prose |

## The delivered pieces, with their anchors

* **Signature authority.** `PDF_MAGIC` (`_common.py:100`) is the one marker
  constant; `_PDF_SIGNATURE_WINDOW = 1024` at `_common.py:115` and the search
  `PDF_MAGIC in raw[:_PDF_SIGNATURE_WINDOW]` at `:127` are its only window and
  reader. The page lane's gate and the public predicate consult this one
  rule, and the gold page's own tests pin that the gate and the predicate
  agree on the window
  (`test_page_region_core.py:614/:656/:724`). The spec permits leading junk
  before the header, which is why the answer is a window search and not an
  offset-0 match.
* **Required `min_text_chars`.** `detect_ocr_required`
  (`src/fin/corpus/ocr.py:273`) takes the threshold as a required
  keyword-only argument; there is no default to inherit, so each lane names
  its own price: `100` at `src/fin/corpus/ingest.py:301`, `100` at
  `src/fin/corpus/parsers/_pages.py:631` (the page lane's "did the text layer
  collapse" question), and `1` at `src/fin/corpus/parsers/_pdf.py:157` (the
  seven-format parse's "is this page worth a scan at all" question). The two
  numbers are different questions, not one forked default, and
  `test_the_page_lane_states_its_own_scan_threshold_at_the_call`
  (`test_page_region_core.py:430`) asserts the page lane asks `[100, 100, 100]`
  per mixed-gold run by recording the calls.
* **Region-scoped OCR and its legibility floor.** `PageRegionParser` reads a
  `figure` region's pixels when the crop is at least
  `MIN_OCR_REGION_EDGE_PX = 160` (`_pages.py:92`) on its short edge, at
  `PAGE_RENDER_DPI = 200` (`_pages.py:84`), and does not re-read a page that
  the whole-page scan already took. This is the answer to T2a's mixed-page-3
  counterexample: `test_the_page_level_scan_rule_alone_cannot_reach_that_page`
  (`test_page_region_core.py:405`) commits the counterexample itself —
  333 native characters clear the page rule at both prices while the chart's
  pixel text is reachable only through the region rule — and
  `test_a_figure_region_on_a_native_text_page_is_read_from_its_pixels`
  (`:378`) plus
  `test_reading_a_figures_pixels_does_not_replace_the_pages_native_text`
  (`:393`) pin that the crop read is independent and additive.
* **Standalone-image lane.** `extract_image` (`_pages.py:540`) treats an
  accepted image as a one-page document whose raster is the uploaded bytes;
  the adapter routes by filename through `_page_lane`
  (`attachment_prepare.py:259-268`) and never counts an image as a
  multi-page source. Pinned by
  `test_a_bare_image_is_one_page_whose_raster_is_the_uploaded_bytes`
  (`test_page_region_core.py:549`),
  `test_a_bare_image_is_recognized_once` (`:571`) and, at the door, by the 12
  cases of `tests/host/test_attachment_prepare_image.py`.
* **Heron adapter and its timeout.** `attachment_layout.py` crosses into the
  pinned container once per document pass and reads only the two boxed kinds
  — `_BOXED_KINDS = ("table", "figure")` at `:82` — because the measured
  record carries text as a count without boxes (the plan's delivered
  amendment names this, and T2e's crop rule is the consumer that will need
  the wider shape). `_DEFAULT_TIMEOUT_S = 900.0` (`:120`) is priced against
  committed runs, re-read from their artifacts in commands 6–7: the
  whole-document pass over the 3-page mixed gold PDF cost 21.4 s at the
  quiet-host re-measure (`…T000242Z.meta.json`, 21,359.1 ms) and 163.6 s
  under host load 21.86–24.28 (`…T042256Z.meta.json`, 163,641.3 ms), so
  900 s is 42× the quiet and 5.5× the loaded figure. The same comment states
  what the bound does *not* cover and does not hide it: the two scanned-case
  runs completed in 64.6 and 69.7 minutes, so a scanned document loses its
  *regions* to this ceiling by design — regions are additive metadata with a
  measured zero recall on gold pages that plainly carry them, and no page's
  text lane may wait an hour for them.
* **The refusal door.** `ATTACHMENT_PAGE_LANE_UNWIRED =
  "attachment_page_lane_unwired"` at `attachment_prepare.py:79`;
  `AttachmentPageLaneUnwiredError` at `:179` (subclassing
  `AttachmentPreparationError`, `code` at `:194`); the raise at `:595`, inside
  `_preflight_page_source`, before any page is counted, boxed, rasterized or
  scanned, whenever `self.budgets is None or self.pages is None`. `_freeze`'s
  catch list (`:441-445`) takes it — it is a preparation exit, not a config
  error — and turns it into a `failed` row carrying the code; the bytes stay
  for the retry a wired deployment can answer. The composition root
  (`workspace_http.py:2198`, constructing
  `AttachmentPreparationService(store=store, objects=objects)` at `:2252`)
  deliberately passes neither port, so this refusal *is* today's shipped
  behavior. Pinned on both faces:
  `test_the_unwired_lane_is_a_caught_preparation_exit_not_a_config_error`
  (`test_attachment_prepare_pages.py:283`) and
  `test_a_page_document_is_named_when_no_page_cap_is_deployed` (`:522`) on the
  service, and the two HTTP cases of
  `tests/host/test_attachment_prepare_door.py` (`:118` the root answers the
  code; `:151` the door returns 200 with the code in the body, never an
  exception).
* **Budget preflight before any page work.** `_page_units` calls
  `preflight_run(budgets, (_page_count(lane, payload),), 0)` at
  `attachment_prepare.py:548`: the page cap is judged from the page *count*,
  so an over-cap document exits `attachment_page_limit_exceeded` whole before
  the first render — pinned with an empty renderer by
  `test_page_overflow_is_named_before_any_page_is_parsed` (`:488`).
* **Shipped Tesseract configuration.** `DEFAULT_TESSERACT_LANG =
  "chi_sim+eng"` (`ocr.py:115`) and `DEFAULT_TESSERACT_PSM = "6"` (`:129`)
  are passed on every call (`:212`), where before this ticket the binary ran
  Tesseract's silent mode-3 default. The mode is decided by the figure lane
  (the ladder record in `ocr.py:117-128`), the language set is kept after
  measurement (same quotes and recall; the extra CJK-model characters
  normalize away, and the second model's cost is CPU, priced below).
* **Web correction.** `web/src/attachFailure.ts:54-58` now states the truth
  the server shows: `attachment_not_found` is answered by the send door
  (`workspace_http.py:2622`), the status/read door (`:3122`) and the retry
  door (`:3157`, `:3165`) — three doors — while the upload door, which mints
  the id itself, can never produce it. The 23 cases of
  `src/attachFailure.test.ts` pin the copy and code mapping.

## What was proven by mutation

| Mutation | Result | What it purchased |
| --- | --- | --- |
| delete the `render_failed` arm of `corpus_failure_to_attachment_error` | session capture at `cecff0e`: **34 tests stayed green** — the ladder's other arms share the generic code so the string alone could not see the loss. **Not re-run at this tip** | the door-type assertion `assert type(mapped) is refusal` at `tests/host/test_attachment_prepare_pages.py:436`, which pins each ladder arm by the exception *type* it returns, not only its code; the full 34-case group passes with it (inside command 1) |
| `MIN_OCR_REGION_EDGE_PX` 160 → 0 in the live tree, then restored | **re-run at this tip** (command 8): 3 failed, exit 1 — `test_a_small_figure_is_not_read_and_keeps_its_box` and the two boundary params of `test_only_a_figure_at_or_over_the_legibility_floor_is_read` (`[…region0-True]`, `[…region1-False]`); after restore, 6 passed, exit 0, `git status` clean | the 160 floor is load-bearing at exactly its boundary (`floor`, `floor−1`, `floor+40`), and the falsification is reproducible by any later reader in one command |

## Closing T2a *Leftovers* 4, 6, 7

* **Row 4** ("Scanned-page OCR is quantified, but the configuration it
  quantifies is one nobody chose"). T2d chooses it and prices the choice. The
  deliberate configuration is named in code — `--psm 6` (`ocr.py:129`),
  `chi_sim+eng` (`:115`), 200 dpi (`_pages.py:84`) — and each choice records
  the measurement beside itself. The price was re-derived from the committed
  artifact in this session (command 9): at exactly the shipped configuration
  the scanned fixture's child CPU (`utime+stime`) is 17.4/21.7/24.5 s on page
  1, 48.0/475.7/743.0 s on page 2, 303.0/638.2/1,678.7 s on page 3 — a
  per-page mean of **438.9 s**, so an 80-page scanned filing prices at
  **≈ 9.8 h** of OCR child CPU (179.5 s/page, ≈ 4.0 h at `eng` alone). That
  is T2a's own page-count extrapolation, never run at 80 pages. The cap does
  not move on this price because an over-budget document never reaches a
  page: `preflight_run` judges the count before the first render (door row
  above). The wall-clock question this leaves — 9.8 h of child CPU against a
  900 s per-pass cap — is the wiring item's, and the plan prose says so
  explicitly.
* **Row 6** ("the quiet-host serial re-measure has been run; a quiet
  *machine* has not … T2d still owns the pick"). The pick was made *against*
  both readings, not one: the timeout comment (`attachment_layout.py:94-119`)
  names the quiet-host 21.4 s and the loaded 163.6 s explicitly and sets
  900 s from their spread (42×/5.5×, re-computed from the two metas in
  command 7), and the OCR sweeps carry their own captured 1-minute load in
  their headers (4.85–14.95 across the four artifacts; command 10 reads the
  finalists header's 6.71). What row 6's residual is — a dedicated 4 GB
  device with nothing else on it — T2a itself assigns to T2f's gold round,
  and nothing here claims that reading.
* **Row 7** ("A bare image has no measured preparation cost … T2d owns the
  non-PDF input path and must measure it"). The path is built (the
  `extract_image` lane above) and the cost is measured on the committed
  fixture, not reused from the PDF figures: the
  `standalone_image` lane of the pinned sweeps
  (`ocr_config_measurement_finalists_20260927T025229Z.jsonl`, 16 rows;
  `…host_20260927T023913Z.jsonl`, 12 rows) re-reads
  `g4-image-chart-001.png` at both DPIs, with child CPU, major faults and
  quote-score per attempt. At the shipped configuration the four repeats
  read **5/5 committed quotes** at 41.1–70.8 s wall / 57.7–98.8 s child
  (command 10). The preparation shape is also deterministic: the raster the
  core recognizes is the uploaded bytes themselves, so the measured file's
  sha pins the input of every such run.

## Commands, output, exit status

Rows 1–10 were executed at `70d65cd` (tree `caaf7c7`) in this session. Rows
11–13 are carried session captures, each labeled with what it is not.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/corpus/test_ingest.py tests/corpus/test_page_region_core.py tests/host/test_attachment_prepare_pages.py tests/host/test_attachment_prepare_door.py tests/host/test_attachment_budgets.py` | 0 | `317 passed in 394.62s (0:06:34)` — reproduces the figure the merged tree reported at `caaf7c7`, now measured at this tip |
| 2 | same five files, `--collect-only` per file | 0 | row composition of the 317: ingest 11 + core 71 + prepare-pages 39 + door 4 + budgets 192; the door file's 4 cases are the two service-side and two HTTP-side un-wired-lane pins |
| 3 | `bash -n scripts/g4_pipeline/run_measured.sh` | 0 | syntax seam of the harness carrying the pre-launch reap at `:256-260` |
| 4 | `cd web && npx vitest run src/attachFailure.test.ts src/attachmentDelete.test.ts` (with the temporary `node_modules` symlink) | 0 | `Tests 45 passed (45)` — `attachFailure.test.ts (23 tests)`, `attachmentDelete.test.ts (22 tests)`; the 23 pin the corrected door-exclusivity copy map |
| 5 | `uv run --no-sync python scripts/build_g4_native_text_gold.py /tmp/g4nt-rebuild.pdf && sha256sum` both files | 0 | `wrote /tmp/g4nt-rebuild.pdf: 3150 bytes, sha256 b4561e23…bceb011` — byte-identical to the committed `g4-native-text-001.pdf` and to `manifest.json:280`; the gold set's fourth case is reproducible from its builder, and `manifest.json:25` now counts `"cases": 4` |
| 6 | python one-liner reading `scripts/g4_pipeline/results/g4-mixed-chart-001_20260926T000242Z.meta.json` and `…T042256Z.meta.json` | 0 | `total_latency_ms` 21,359.1 and 163,641.3 — the quiet and loaded filing-path figures the 900 s rationale quotes are in the tracked artifacts, not only in the comment |
| 7 | same reading of the two scanned metas | 0 | 3,790,099.3 and 4,089,528.9 ms render-plus-OCR — the 64.6/69.7 min pair, re-derived from the committed files |
| 8 | floor mutation: set `MIN_OCR_REGION_EDGE_PX = 0`, run `test_page_region_core.py -k "small_figure or legibility"`, restore, re-run | 1 then 0 | `3 failed, 3 passed, 65 deselected` under the mutation; `6 passed, 65 deselected in 0.90s` restored; `git status --porcelain` empty both before and after |
| 9 | recompute child CPU from `ocr_config_measurement_priced_20260927T031128Z.jsonl` | 0 | page means 17.4/21.7/24.5, 48.0/475.7/743.0, 303.0/638.2/1,678.7 s; per-page mean **438.9 s**, ×80 = **9.8 h** (`chi_sim+eng`), 179.5 s/page / 4.0 h (`eng`) — every figure the plan prose quotes reproduces from the artifact |
| 10 | read the finalists artifact header and its shipped-config `standalone_image` rows | 0 | schema `fin.g4.t2d.ocr_config_measurement/1`, psm ladder `["3","6"]`, dpi `[150,200]`, captured load 6.71; the four `psm 6 / chi_sim+eng` rows score 5/5 oracle quotes, 41.1–70.8 s wall, 57.7–98.8 s child CPU (`utime+stime`), major faults 0–22 |
| 11 | the same five-file run at `caaf7c7` during the merge session | 0 | `317 passed` — this is the figure row 1 independently reproduces at this tip; carried as history, not as double evidence |
| 12 | an earlier 147-case sub-branch run on the pre-merge OCR branch | — | **Not re-run**: the exact file list was not restated at hand-off and the merged tree supersedes it; no claim in this record rests on it |
| 13 | a 369-case "gold surface" run quoted in the session | — | **Not re-run**: the command was not restated at hand-off and no committed file set at this tip reproduces 369 by collection arithmetic; treated as unverified history. The gold contract that *is* evidence here is rows 1, 2 and 5 |
| 14 | review-repair increment r1 (complexity splits + `PAGE_COLLAPSE_TEXT_CHARS` single home): `uv run pytest -q tests/corpus/test_page_region_core.py tests/host/test_attachment_prepare_pages.py tests/host/test_attachment_layout.py tests/corpus/test_ingest.py tests/host/test_attachment_prepare_door.py tests/host/test_attachment_budgets.py` at the merged tip | 0 | `398 passed in 361.88s`; radon reports zero C+ in the touched files; the split preserved every assertion (per-entry equality is stricter than the old pooled comparison) |
| 15 | real-binary OCR acceptance: `uv run --with pytesseract -q pytest tests/corpus/test_ocr_tesseract_gold.py` against system Tesseract 5.5.0 (`eng`+`chi_sim`) | 0 | `7 passed` — the shipped configuration leg exercised on the real engine, not a stub |
| 16 | real MCP round-trips at `694bcd3` (`-m mcp`): live retrieve t2–t5 + read_source, corpus-ingest + research-cite, content-identity + graph-hop + search-modes after starting the iknow PG/ES/Neo4j stack | 0 | `8 + 3 + 5 + 12 passed` — the Wave-2 changes were exercised through the real stdio MCP server against the real backend; the containers were stopped back to their prior state afterwards |
| 17 | r5 door-order repair: `uv run --extra object-store pytest -q -m mcp tests/host/test_live_attachment_upload_door.py` (real PG + SeaweedFS) | 1→0 | pre-fix `1 failed, 13 passed` (`attachment_page_lane_unwired` preempted `attachment_not_text`); post-fix `14 passed`; hermetic door suites `59 passed` (+2 new family-boundary pins) |
| 18 | `jev_live` tier round-trip | — | **BLOCKED, not skipped-pass**: the fixture's `apply_dotenv` only finds `.env` at a worktree's root and Wave-2 worktrees have none, so the key is structurally absent (`tests/conftest.py:224-233`). The changed surface (attachment/corpus) is not the Soft-Track/Jev product path, so ADR 0080/0081 does not make this leg required for this round |

## Limits and leftovers

* **The lane stays unwired at the root.** `workspace_http.py:2252` still
  constructs the service with neither `budgets` nor `pages`, so every real
  pdf-family document answers `attachment_page_lane_unwired` today and
  image-named payloads answer through the content ladder (r5). This is the
  shipped behavior by decision, not a gap: wiring would make every upload
  depend on a container crossing no job yet waits on (the plan's delivered
  amendment carries the full argument). The wiring owner is **T2e**.
* **T2e inherits, by name:** the Heron `text`-box shape (the record counts
  text but does not locate it, so `_BOXED_KINDS` ships the two boxed kinds
  and a question about detail inside a text-labelled region still pays a
  full-page call); region raster persistence and its provenance hash; the
  page-cap-80 pricing at the wired crossing (the 9.8 h child-CPU figure
  above against the 900 s cap is a wiring question); and the retry headroom
  already deferred by T2a *Leftovers* 9.
* **Structural debt in the touched files:** `_pages.py` (977 lines) now
  carries ports, sources, renderer and the parse rules in one module and
  wants a responsibility split; its test file
  (`tests/corpus/test_page_region_core.py`) has grown to 1,352 lines and
  wants splitting along the same seam. Neither is on this ticket's critical
  path and both are listed rather than half-done here.
* `ParsedPage.page_count` carries the document count per page — a naming
  defect with a rename already reasoned out, blocked on
  `tests/host/test_attachment_layout.py:390`
  (`assert [p.page_count for p in result.pages] == [3, 3, 3]`), which pins
  the current shape deliberately. Renaming it is a test-contract change for
  whoever owns the next page-metadata revision.
* `_page_count` (`attachment_prepare.py:272`) opens the PDF with a fresh
  `PdfplumberPageSource` and closes it before the parse opens it again — a
  second parse-tree open per document, accepted to keep the cap decision on
  the same page-count authority the core scans with. The duplicate open is
  the price of that consistency and is named so a later owner can choose
  differently with evidence.
* **The six `FIN_ATTACHMENT_HERON_*` keys** (`SCRIPT`, `STAGING_DIR`,
  `TIMEOUT_S`, `IMAGE`, `MODEL_CACHE`, `SLICE_PASS`;
  `attachment_layout.py:441-460`) are read by `from_environ` with named
  validation and appear **nowhere in `.env.example`**. They are documented
  in the layout module and the plan, not the env template, because the lane
  they configure is unwired — the template rows should arrive with T2e's
  wiring, in the same change that makes a missing or malformed value a boot
  fact rather than a per-call refusal.
* **Harness orphaning is mitigated, not eliminated.** The trap in
  `run_measured.sh` answers INT/TERM and the pre-launch `docker rm -f`
  (`:260`) answers a fixed-name leftover, but a `kill -9` of the measuring
  shell *during* a run still leaves that run's container up until the next
  launch reaps it — the comment at `:256-259` says exactly this. The window
  is bounded (one reaped-name cycle) and the ceiling flags mean the orphan
  holds 4 GiB/4 CPU during it; closing the window needs a supervised runner,
  which is the harness owner's, not this lane's.
* The OCR price rows quote **child CPU**, never wall: one configuration on
  one page spans 214.2 s to 1,670.8 s of wall across its own repeats on this
  host, and none of the sweeps' figures is a device figure (their headers
  carry loads 4.85–14.95 on 4 cores). This is the same bound T2a stated, and
  it is why *Leftovers* row 6's device reading stays open for T2f.
