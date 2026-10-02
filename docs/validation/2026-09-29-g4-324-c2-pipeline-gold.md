# G4 issue #324 C2 — committed pipeline gold under the pinned 4 GiB ceiling

Date: 2026-09-29. Every "Commands" row ran on this branch on this date, in this
worktree.

Branch: `worktree-issue-324-g4-reproducible` at source revision
`670f65f2b8017193cc181173635af74f68820c6b` (local `main`, which carries PR #321
squash `e36999f`).

## What this record is, and what it is not

This record runs the committed G4 pipeline gold
(`data/corpus/g4_attachment_gold/`, `fin-g4-attachment-gold-v1@1.0.0`) through
the pinned harness `scripts/g4_pipeline/run_measured.sh` under its literal
`--memory=4g --memory-swap=4g --cpus=4` ceiling, plus the 80-page attachment
the acceptance names, and records revision, dataset and model hashes, command,
exit code, page/region and OCR outcomes, device identity, peak resource use and
a per-case verdict.

Two boundaries are stated before any number, because they change what the
numbers mean:

1. **This is not a dedicated 4 GB device.** It is a cgroup v2 memory ceiling
   applied on a 7.66 GiB WSL2 VM. Issue #324 states verbatim that "a cgroup
   ceiling on a larger host cannot close that acceptance", and the T2a record
   already carries the same limitation as *Leftovers* 5. The measurements below
   are therefore **bounded and consistent**, not a device reading. **C2 is NOT
   CLOSED by this record** — see *Limitations / blocked evidence* L1.
2. **There is no automated scorer.** `scripts/g4_pipeline/driver.py` never
   reads `data/corpus/g4_attachment_gold/manifest.json` (verified: the only
   occurrence of the string `manifest` in the file is the driver's own
   `pin_manifest` key, `driver.py:1211`). It emits a measurement record and
   nothing compares it to the committed expectations. The expected-vs-observed
   tables below are a **manual comparison by the worker who ran the runs**,
   written against `manifest.json`'s per-page `per_page` entries. The only
   automated oracle in this directory is `measure_ocr_config.py`, which scores
   OCR quotes only and is a separate in-container script, not this harness.
   Every table below is labelled accordingly, and no row in it is a test result.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Source revision | `670f65f2b8017193cc181173635af74f68820c6b`, branch `worktree-issue-324-g4-reproducible` |
| Pinned pipeline image | `fin-g4-pipeline:t2a`, `sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229`, created `2026-09-29T11:26:19.670933011+08:00`, size 681,932,537 B. Used **as found**; this round did not run `build.sh`. This is the digest the L7 record already records as the rebuild's output, NOT the `sha256:8e10185f…` in `PIN_MANIFEST.md` §1 — that conflict is C1's subject and is unresolved here (L2). |
| Pinned runtime (as the driver reports it, `pin_manifest` in the measured JSONL) | `docling 2.130.0`, `docling-core 2.99.0`, `docling-ibm-models 4.0.3`, `docling-parse 7.22.1`, `torch 2.9.1+cpu`, `torchvision 0.24.1+cpu`, `transformers 5.17.0`, `opencv-python 5.0.0.93`, `pypdfium2 5.13.0`, `huggingface-hub 1.33.0`, `numpy 2.5.2`, `pillow 12.3.0`, `pytesseract 0.3.13`, Python `3.12.14` |
| Pipeline identity (as the driver reports it) | `StandardPdfPipeline`, backend `ThreadedDoclingParseDocumentBackend`, layout `layout_heron` from `docling-project/docling-layout-heron` (revision `main`), `do_ocr: false`, `do_table_structure: true`, `device: cpu`, `num_threads: 4`, `layout_batch_size: 4`, `table_batch_size: 4` |
| Model artifacts (as the driver reports them, `heron_artifact` in the measured JSONL) | cache `/models`, 70 files, 701,236,013 B. `docling-project--docling-layout-heron/model.safetensors` 171,658,996 B, `sha256 00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c`; `…heron/config.json` 3,268 B, `sha256 fdea30805ce2f5666b147fca941dcdd27ad468e27d6ed21902207d3da056a97d`; `…heron/preprocessor_config.json` 444 B, `sha256 cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1`; `…heron-onnx/config.json` 878 B, `sha256 c6e67b6cdf64fa245779d0409bb994aa96e8c1790e15ec07a65843628e232180`; TableFormer `accurate` 212,758,388 B and `fast` 145,453,276 B (byte sizes only; the driver does not hash the `.safetensors` tableformer files) |
| Attachment gold dataset | `data/corpus/g4_attachment_gold/` — `fin-g4-attachment-gold-v1`, schema `1.0.0`, 4 cases / 8 physical pages / 5 pages needing OCR. Expectations produced from the fixtures with poppler 26.01.0, tesseract 5.5.0 and Pillow 12.3.0, never from fin's own parser (`manifest.json` `expectation_basis`). |
| The 80-page case | `data/corpus/pdf/2022_Q3_MSFT.pdf`, `sha256 8ae253ddee9624102ce5544c0be9d4283f872694f46132ae1f5609e6ba3538c0`, 1,861,635 B, **80 pages** (`pdfinfo` 26.01.0, agreeing with the committed catalog). `data/corpus/g4_upload_distribution.json:28-31` names it `largest_page_count_observed` at `page_count: 80`. |
| Deployed caps at the time of the run | `max_pdf_pages 80`, `max_vision_crop_calls_per_run 8`, `max_vision_calls_per_run 89` (= 80 + 8 + 1 retry reserve; `attachment_budgets.py:69` `RETRY_RESERVE_CALLS = 1`, `:165-176` `required_run_calls()`), `max_upload_bytes 4194304`. **Unchanged by this round** (L3). |
| Device | see L1 — cgroup ceiling on a 7.66 GiB WSL2 VM, not a dedicated 4 GB device |

### Input hashes (measured on this host before each run)

| input | bytes | `sha256` on disk | manifest's `sha256` | agree |
| --- | --- | --- | --- | --- |
| `g4-native-text-001.pdf` | 3,150 | `b4561e23331f0a0df9a05ed011e211e5f4f2f6e145d61d6ae70c1f86abceb011` | `b4561e23…` | yes |
| `g4-mixed-chart-001.pdf` | 35,324 | `9b216bd9562edfeb74d11fb8a567ddd67cea2007e4fba4f5292918dc076da49e` | `9b216bd9…` | yes |
| `g4-scanned-001.pdf` | 677,716 | `73c38af71a0fd65b0fb5dc832adbbd12db410cab1e012d0c4da6e0923a31bf00` | `73c38af7…` | yes |
| `g4-image-chart-001.png` | 40,419 | `2c8b6338d8e95dfa77a35bbbf0dcb003a3518e8ab82a195e9f26064a6f1117e5` | `2c8b6338…` | yes |
| `2022_Q3_MSFT.pdf` | 1,861,635 | `8ae253ddee9624102ce5544c0be9d4283f872694f46132ae1f5609e6ba3538c0` | (not in the gold manifest; `data/corpus/manifest.json` corpus entry) | yes |

The driver independently re-hashes each input and echoes it into the record's
`file.sha256`; every measured row below carries the manifest digest in both
places.

## Commands

| # | Command | Result |
| --- | --- | --- |
| 1 | `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-native-text-001.pdf` | **exit 0**, `json_lines: 1`, `container_exit_code: 0` |
| 2 | `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf` | **exit 0**, `json_lines: 1`, `container_exit_code: 0` |
| 3 | `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-scanned-001.pdf` | **exit 0**, `json_lines: 1`, `container_exit_code: 0` |
| 4 | `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png` | **exit 1** — the expected, correct PDF-only rejection (§*`g4-image-chart-001.png`*) |
| 5 | `bash scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2022_Q3_MSFT.pdf` | **exit 0**, `json_lines: 1`, `container_exit_code: 0`, `page_count: 80` |

Every row is the same literal command shape, printed in full by
`run_measured.sh` into the run's own log:

```
docker run --name fin-g4-t2a-<base>-<pid> \
  --memory=4g --memory-swap=4g --cpus=4 --user 1000:1000 \
  --env HOME=/tmp --env HF_HOME=/models/hf --env TORCH_HOME=/models/torch \
  --env DOCLING_ARTIFACTS_PATH=/models --env HF_HUB_DISABLE_TELEMETRY=1 \
  --env HF_HUB_DISABLE_XET=1 --env PYTHONPATH=/worktree/src \
  --volume <worktree>:/worktree:ro \
  --volume <results>/<run>:/out --volume ~/.cache/fin-g4-docling-models:/models \
  --entrypoint python fin-g4-pipeline:t2a \
  /worktree/scripts/g4_pipeline/driver.py --pdf <pdf> --artifacts /models \
  --write-images --image-dir /out/pages
```

The ceiling was never raised, and the driver was never run on the host.

## Per-case results

Every "expected" column below is quoted from
`data/corpus/g4_attachment_gold/manifest.json` (`per_page[]`); every "observed"
column is read from the run's own
`scripts/g4_pipeline/results/<run>/<base>.measured.jsonl`. The comparison is
**manual** (see *What this record is, and what it is not*, item 2).

### `g4-native-text-001.pdf` — expected vs observed

| field | expected (manifest) | observed (this run) | verdict |
| --- | --- | --- | --- |
| `page_count` | 1 | 1 | agree |
| `file.sha256` | `b4561e23…` | `b4561e23…` | agree |
| page 1 `native_text_present` | `true` | `native_text_char_count: 1882` (driver, pypdfium2 `get_text_range()`) | agree |
| page 1 `native_text_chars` | `1860` (manifest: poppler `pdftotext -f 1 -l 1`, stripped) | 1882 (driver) | **1.2% apart — not a miss**; the two are different extractors, and the manifest itself defines the expectation as poppler's number |
| page 1 `ocr_expected` | `false` ("the whole payload is the text layer; OCR must not run") | `ocr_required_by_repo_rule: false`, `ocr_used: false`, `ocr_trigger: null`, `ocr_text_char_count: 0` | agree — the gold's own negative assertion holds |
| page 1 `region_kinds_expected` | `["text"]` | `region_counts: {text: 8, table: 0, figure: 0}` | agree — text present, and **no** table or figure kind, which is the shape the manifest denies |
| page 1 `figure_regions` | `[]` | `figure_boxes: []`, `pdf_image_objects: 0` | agree |
| page 1 `expected_quote` | "The Company has considerable trade receivables outstanding with its third-party cellular network carriers, wholesalers, retailers, resellers, small and mid-sized businesses and education, enterprise and government customers." | re-verified **independently of the pipeline** with `pdftotext -layout -f 1 -l 1 g4-native-text-001.pdf -` (poppler 26.01.0), whitespace-normalised: **present** | agree |
| page 1 `chart_present` | `false` | not measured by this harness (no vision leg; see *Not covered by this record*) | not scored here |

Region presence/absence is the only kind-level comparison the manifest itself
supports: `known_gaps[3]` states `region_kinds_expected` "is a human-verified
floor from the rendered page, not a box-level ground truth. No page/region
coordinate annotations are committed, so region-count or IoU comparisons cannot
be scored against this manifest". `text: 8` is therefore not scored against any
count; only the presence of `text` and the absence of `table`/`figure` is.

### `g4-mixed-chart-001.pdf` — expected vs observed

| page | expected (manifest) | observed (this run) | verdict |
| --- | --- | --- | --- |
| 1 | `native_text_present: true`, `native_text_chars: 449`, `ocr_expected: false`, `region_kinds_expected: ["text","figure"]`, `figure_regions`: 1 raster XObject 46×56 px (Apple logotype), `chart_present: false` | `native_text_char_count: 460`, `ocr_used: false`, `region_counts: {text: 14, table: 0, figure: 1}`, `pdf_image_objects: 1`, `figure_boxes` 1 box | **as expected**: figure kind found, OCR correctly not run. Char count 449 vs 460 = 2.4% apart, two extractors (poppler vs pypdfium2); the presence assertion, which is the scored one, agrees |
| 2 | `native_text_chars: 1181`, `ocr_expected: false`, `region_kinds_expected: ["text","table"]`, `figure_regions: []` | `native_text_char_count: 1111`, `ocr_used: false`, `region_counts: {text: 5, table: **0**, figure: 0}` | **MISS — reproduced.** Heron found **no table region** on a page the manifest labels text + table. Identical to the miss the T2a record already recorded (`known_gaps[0]` recall gap), so this is a carried, not a new, defect |
| 3 | `native_text_chars: 334`, `ocr_expected: **true**`, `ocr_scope`: "the figure region only … exist inside the raster image and have no text layer", `region_kinds_expected: ["text","figure"]`, `chart_present: true` | `native_text_char_count: 337`, `ocr_required_by_repo_rule: **false**`, `ocr_used: **false**`, `ocr_text_char_count: 0`, `region_counts: {text: 5, table: 0, figure: 1}` | **MISS — reproduced, and it is a rule conflict, not a model failure.** The harness asked `fin.corpus.ocr.detect_ocr_required(min_text_chars=1)`; the page carries 337 characters, so the rule returns false. The chart's pixel labels (title, 4 period labels, 4 value labels, source line) were never read. This is the T2d region-scoped-OCR gap, unchanged |

Three `expected_quote`s on this file were re-verified on the host with poppler
26.01.0, independently of any fin component, and all three are present in the
fixture's own text layer exactly as the manifest states: page 1
`SECURITIES AND EXCHANGE COMMISSION`, page 2
`Total net sales 81,797 82,959 [private source value removed] 304,182`, page 3
`2023 Q2 (April 1, 2023) 94,836; 2023 Q3 (July 1, 2023) 81,797.`
The page-3 `expected_ocr_quote` ("Total net sales by fiscal quarter") is an OCR
expectation over the chart raster; the harness did not read it (§L4/L5).

**Two of three pages match the manifest's kind-level expectation; the two known
misses reproduce exactly as the T2a record states they do. The mixed case is
therefore a reproduced result, not a regression and not a new failure.**

### `g4-scanned-001.pdf` — expected vs observed

| page | expected (manifest) | observed (this run) | verdict |
| --- | --- | --- | --- |
| 1 | `native_text_present: false`, `native_text_chars: 0`, `ocr_expected: true`, `region_kinds_expected: ["text","table"]`, `figure_regions: []`, `page_image_xobjects`: one 1275×1650 full-page raster, `chart_present: false` | `native_text_char_count: 0`, `ocr_required_by_repo_rule: true`, `ocr_used: true`, `ocr_trigger: "repo_rule"`, `ocr_lang: "chi_sim+eng"`, `ocr_text_char_count: 5565`, `region_counts: {text: **0**, table: 1, figure: 0}`, `pdf_image_objects: 1` | OCR **as expected** (the repo rule fired on a page with genuinely no text layer, and the read sample opens with the manifest's `expected_quote` `CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)` verbatim). Table kind found. **text-region miss** |
| 2 | `native_text_chars: 0`, `ocr_expected: true`, `region_kinds_expected: ["text"]` (prose only, "No table and no graphic region exists on it"), `chart_present: false` | `native_text_char_count: 0`, `ocr_used: true`, `ocr_text_char_count: 3683`, sample opens with `Notes to Condensed Consolidated Financial Statements (Unaudited)` — the manifest's quote, verbatim. `region_counts: {text: **0**, table: **1**, figure: 0}` | OCR as expected. **two-way region miss** — Heron returned a table on a page the manifest says has none, and returned no text on a prose page |
| 3 | `native_text_chars: 0`, `ocr_expected: true`, `region_kinds_expected: ["text","table"]`, `chart_present: false` | `native_text_char_count: 0`, `ocr_used: true`, `ocr_text_char_count: 5860`, `region_counts: {text: **0**, table: 2, figure: 0}` | Table found, OCR as expected. **text-region miss.** The `expected_quote` (`Total net sales $ 81,797 $ 82,959 $ [private source value removed] $ 304,182`) is **not checkable against this run's artifact** — the record keeps only a 160-character OCR sample per page and the quote sits far below it on the page |

All three of the manifest's OCR `expected_quote`s were additionally
re-verified **on this host, outside the pipeline**, with the manifest's own
prescribed toolchain: `pdftoppm -png -r 150` then
`tesseract … -l eng --psm 6` (tesseract 5.5.0, leptonica 1.86.0 — the exact
versions `manifest.json` `expectation_basis.tools` names). All three are present
in the fixture's OCR text, so the expectations themselves are sound;
`pdftotext` on the fixture yields **0 characters** across all three pages,
confirming the `native_text_present: false` rows.

The page-bitmap digests (`page_image_png_sha256` `4cea21df…`, `1bd502f7…`,
`a5d35ac2…`) and the region counts (`text 0 / table 4 / figure 0`, 3 of 3 pages
OCR'd) are **byte-identical to the 2026-09-25 committed runs** the T2a record
cites, and `ocr_text_chars_total` is 15,108 — also identical to the 2026-09-29
T5 run's 15,108 and 0.5% from the 2026-09-25 pair's 15,390. This is the most
reproducible case in the set: three independent runs, three different days, one
content answer.

**Every OCR expectation is met and every region-kind expectation except `text`
is met. The one systematic finding is carried, not new: Heron's `text`-class
recall on a rasterised page is 0 of 3 at this pin, and it declares a table on
mixed page 2 and scanned page 2 — the two places the manifest says there is
none. `known_gaps[0]` already records this. The readable payload of a scanned
page came from OCR alone, so T2d cannot use region kinds as the signal that a
scanned page has been read.**

**Wall-clock caveat on this one run, self-reported.** The independent
host-side `tesseract` verification of the three OCR quotes above was launched
while this container was still running, and competed with it for the same 4
vCPUs before being killed. Every **content** field on this run is unaffected
(OCR text, region counts, page digests are deterministic functions of the
bytes), but this run's `render_and_ocr_latency_ms` 825,008.9 and
`total_latency_ms` 941,408.6 are **contaminated by host CPU contention** and
must not be compared with the T2a record's 3,790,099.3 / 4,089,528.9 ms
renders. The honest statement is that the run completed in 15.7 min wall clock
under a 4 vCPU ceiling that was shared, against a historical ≈ 21.9 min/page
per two undisturbed runs — i.e. the contention did not dominate, but the figure
is not a clean measurement and is **not** used to re-derive any per-page rate.
The peak-memory and swap columns are not affected by CPU contention.

### `g4-image-chart-001.png` — the negative assertion (expected: REJECTED)

Run: `run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png`.
**Exit 1 is the expected, correct result.** This is a negative assertion about
the harness's PDF-only input contract, never an image-pipeline pass.

| field | expected | observed |
| --- | --- | --- |
| harness accepts the file | **no** — the pinned pipeline as driven here takes PDF input only | rejected |
| mechanism | a non-PDF attachment has no measured preparation cost | `pypdfium2._helpers.misc.PdfiumError: Failed to load document (PDFium: Data format error).`, raised from `pypdfium2/_helpers/document.py:74 → :585` in `PdfDocument(str(pdf_path))` |
| is it an OOM? | no | `oom_killed_docker_inspect: false`, `state_error: null` — the failure is a data-format rejection inside the ceiling, not the ceiling |
| recorded shape | a `failed` meta: every driver field null | `json_lines: 0` and every driver total `null`; `ceiling` still recorded as `{memory: "4g", memory_swap: "4g", cpus: "4"}`; `container_exit_code: 1`, `docker_run_exit_code: 1` |
| manifest content answer for this case | OCR-only chart, `chart_present: true` | **not produced by this harness**, and not claimed. Its content answer belongs to the integrated G4 acceptance (spec criterion 12, the vision leg), not to this pipeline run |

The rejection is **emergent, not a guard**: there is no `if not …: reject`
anywhere in `run_measured.sh` or `driver.py`; the path is handed straight to
`--pdf` and the PDF backend refuses it. Two consequences are recorded as L6:
no test in the repository asserts this rejection, and the same refusal would be
produced by any non-PDF bytes, not only by a PNG.

### `2022_Q3_MSFT.pdf` (80 pages) at the current caps — **no committed expectation exists**

**This case cannot be gold-verified, and the reason is worth stating plainly
rather than papering over with a manufactured expectation.** The gold manifest
`data/corpus/g4_attachment_gold/manifest.json` has `max_pages_in_one_attachment: 3`
and its `known_gaps` list the absence of a multi-page attachment as a gap. There
is **no `per_page` entry, and no `expected_*` field of any kind, for this file**
in any committed oracle. `data/corpus/manifest.json` pins its bytes and its
80-page count, and that is the full extent of its committed expectations.

So this row is a **measurement against the T2a EXTRAPOLATION**, quoted here
because the extrapolation is what the number was predicted by:

> "So `max_pdf_pages = 80` fits a 4 GiB device with ~13% margin on the
> strict(plus-swap) reading, **but the 80-page point itself was never run** — it
> is 11% beyond the largest measurement. That is the residual risk of this
> ticket and is listed under *Leftovers*."
> — `docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md`

and its arithmetic, which predicted **3,548,886,768 B resident** for 80 pages
(`1,301,147,648` baseline + `80 × 28,096,739` B/page), giving
`3,731,625,712 B = 3.48 GiB` with swap and a 13.1% margin.

| quantity | T2a extrapolation (from the 72-page run) | **measured, 80 pages** | delta |
| --- | --- | --- | --- |
| resident peak | 3,548,886,768 B | **`cgroup_peak_bytes` 3,382,202,368 B** (3.150 GiB) | **−166,684,400 B (−4.70%)** — the real thing is *below* the prediction |
| margin vs the 4 GiB ceiling, strict plus-swap reading (+182,738,944 B) | 563,341,584 B = 13.1% | **730,025,984 B = 17.00%** | +3.9 points |
| margin vs the ceiling, measured peak alone | — | 912,764,928 B = 21.25% | — |
| margin vs the ceiling, strictest reading (`VmHWM` alone) | — | 1,032,933,376 B = 24.05% | — |
| ceiling | 4,294,967,296 B | 4,294,967,296 B | agree |

**The 80-page attachment fits the current caps.** `max_pdf_pages = 80` is not
over the device limit by any reading available to this round, so the acceptance's
conditional does not fire: the page and call budgets are **not** lowered,
`.env.example` is untouched (L3), and the T2a *Leftovers* 1 residual risk is
**discharged for the cgroup reading** — while remaining open for the device
reading (L1).

Other measured facts about this run, none of which is a manifest expectation:

| field | value |
| --- | --- |
| `file.sha256` | `8ae253ddee9624102ce5544c0be9d4283f872694f46132ae1f5609e6ba3538c0` (agrees with the committed corpus manifest) |
| `page_count` / `docling_page_count` | 80 / 80, `docling_status: ConversionStatus.SUCCESS` |
| `page_limit_exceeded` | `false` (`--max-pages` was not passed, so the driver applied no page limit) |
| pages with native text | **80 of 80**; `pages_needing_ocr: 0`, `pages_with_ocr_applied: 0`, `ocr_text_chars_total: 0` |
| `native_text_chars_total` | 250,253 |
| regions | `text 844 / table 56 / figure 0`, recomputed page by page and matching `regions_total` |
| `harness_failures` | `[]`; `slice_pass_error: null`; the slice pass ran to page 80 without a break |
| latency | `total_latency_ms` 1,003,027.5 (16.7 min), `render_and_ocr_latency_ms` 15,718.4, `docling_whole_doc_latency_ms` 476,852.7, warm per-page mean 6,380.7 ms (median 3,540.9, p95 18,107.8, max 29,937.6) |
| swap | `cgroup_memory_swap_max_bytes: 0` (ceiling **enforced**), `cgroup_memory_swap_current_bytes: 0`, `proc_vmswap_bytes: 0` |
| OOM | `oom_kill_events_in_cgroup: 0`, `oom_killed: false`, `oom_killed_docker_inspect: false` |

The memory shape is worth one line because it is the thing the extrapolation
could not have known: the peak is reached in a **single step**, at the
`after_docling_whole_doc` checkpoint (`VmHWM` 3,262,033,920), and every
subsequent slice-pass checkpoint holds the same high-water mark while
`memory.current` *falls* from 2,064,592,896 to 1,870,585,856. The growth is
therefore driven by the whole-document Docling pass, not by accumulating page
state, which is consistent with the extrapolation's linear model being a
conservative simplification rather than a tight fit.

**Because there is no committed expectation for this case, "the 80-page case
passed" means precisely: it completed under the ceiling with no OOM, no harness
failure, and a peak 4.7% below the T2a prediction. It is not gold-verified, and
nothing in this record should be read as having verified content that no
committed oracle states.**

## Resource use and device identity

Per-case peak resource use, from each run's `.meta.json`:

| case | `peak_rss_vmhwm_bytes` | `cgroup_peak_bytes` | `cgroup_memory_max_bytes` | `cgroup_memory_swap_max_bytes` | `cgroup_memory_swap_current_bytes` | `proc_vmswap_bytes` | `oom_kill_events_in_cgroup` | `oom_killed` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `g4-native-text-001.pdf` | 1,168,064,512 | 1,281,961,984 | 4,294,967,296 | 0 | 0 | 0 | 0 | false |
| `g4-mixed-chart-001.pdf` | 1,294,700,544 | 1,376,219,136 | 4,294,967,296 | 0 | 0 | 0 | 0 | false |
| `g4-image-chart-001.png` | — | — | — | — | — | — | — | — (rejected, not run) |
| `g4-scanned-001.pdf` | 1,344,282,624 | 1,640,247,296 | 4,294,967,296 | 0 | 0 | 0 | 0 | false |
| `2022_Q3_MSFT.pdf` (80 pages) | 3,262,033,920 | 3,382,202,368 | 4,294,967,296 | 0 | 0 | 0 | 0 | false |

Swap is read, not assumed: on the two completed PDF runs the cgroup swap ceiling
was **enforced** (`cgroup_memory_swap_max_bytes: 0` — the kernel applied
`memory.swap.max = 0`, so no byte of the run could be swapped out of the
cgroup) and `proc_vmswap_bytes` is 0 at every checkpoint of both runs. This is
a change from the 2026-09-25 artifacts, which recorded
`cgroup_memory_swap_max_bytes: -1` (swap limit not applied on this kernel); the
2026-09-29 T5 record observed the same enforced-`0` reading. It means these
peaks are **not** comparable to the T2a record's `+ swap` arithmetic, because
there was no swap to add. The T2a margin calculation adds
`182,738,944 B` of swap read off the 72-page `2023_Q1_MSFT.pdf` run; this round
measured `proc_vmswap_bytes: 0` on its own runs, which is a fact about this
round's enforcement, not a license to drop the swap term from the cap decision.

`host_side_cgroup_memory_peak_bytes` and `host_side_cgroup_memory_swap_peak_bytes`
are **`null` on every run**, because the out-of-band cgroup paths
`/sys/fs/cgroup/docker/<id>` and `/sys/fs/cgroup/system.slice/docker-<id>.scope`
are not readable from this WSL2 host. `null` here means "the file answered
nothing", which the harness's serializer distinguishes from `-1` ("no limit").
This is expected on this host, not a gap introduced by these runs.

### Device identity — the honest limitation

| property | value | how it was read |
| --- | --- | --- |
| kernel | `Linux LAPTOP-DIF7442G 6.18.33.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:23 UTC 2026 x86_64` | `uname -a` |
| RAM | `MemTotal: 8034928 kB` = 7,846.6 MiB = **7.66 GiB** — the same figure the earlier records use; the issue's "7,846 MiB (7.66 GiB)" reading is confirmed | `/proc/meminfo` |
| vCPU | 4 | `nproc` |
| hardware virtualisation | **absent** — `/dev/kvm` does not exist; nested virtualisation is not available | `ls -la /dev/kvm` |
| second host | none; no SSH configuration is present for one | checked by the parent session before this round |
| cgroup v2 | `memory.max` on the WSL2 root scope is not readable, so the only enforced bound is the per-container one | checked |
| what the container actually got | `cgroup_memory_max_bytes: 4294967296` read **from inside the container** by the driver on every measured run | `totals.cgroup_memory_max_bytes` in each `.measured.jsonl` |

**This is a cgroup ceiling on a 7.66 GiB WSL2 VM, not a dedicated 4 GB device
reading.** The honest reading of these numbers is *bounded and consistent*, not
*fast*, and not *measured on the acceptance's target hardware*.

## Not covered by this record

* **The vision leg.** `chart_present` and `chart_meaning` are manifest
  expectations that this pipeline harness does not produce at all — the driver
  runs Docling/Heron plus CPU Tesseract and makes no model provider call. Rows
  in the tables above that name a `chart_present` expectation are therefore
  **not scored**, not "failed". Spec criterion 12's vision half is the
  integrated G4 acceptance, which this session does not run.
* **Region coordinates / IoU.** No coordinate annotations are committed
  (`known_gaps[3]`), so no box-level comparison is made or possible.
* **The dedicated 4 GB device round.** See L1.
* **The C1 pin reconciliation.** The image identity conflict between
  `PIN_MANIFEST.md` (`8e10185f…`) and the rebuild (`0f278c22…`) is C1's
  subject. These runs used `0f278c22…` as found and did not attempt a rebuild.

## Limitations / blocked evidence

Per `.claude/rules/test.md`, everything in this section is **blocked evidence —
not a pass, not a skip, not a green substitute**.

**L1 — the acceptance's target hardware does not exist here, so C2 is not
closed by this record.** Issue #324 states verbatim: "If the required device is
unavailable, record the expected command and missing prerequisite as blocked
evidence; do not substitute a larger host with cgroup limits or close C2", and
in the Approach section: "a cgroup ceiling on a larger host cannot close that
acceptance". This host is a 7.66 GiB WSL2 VM with 4 vCPU, **no `/dev/kvm`**
(so no nested virtualisation), and no second host. The runs below were taken
under the literal `--memory=4g --memory-swap=4g --cpus=4` ceiling and are
recorded because bounded, consistent measurements are worth having — but they
are a cgroup ceiling, not a device reading, and they do not close C2. This is
the same limitation already carried at
`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md:167` (L5) and
`docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md:1043-1065`
(*Leftovers* 5), which the T2a record explicitly says is "the run that owes the
dedicated-device reading".

```text
Validation:
- Run: 5 invocations of `bash scripts/g4_pipeline/run_measured.sh <input>` under
  the literal 4 GiB cgroup ceiling; exits 0 / 0 / 0 / 1 / 0; every result
  committed under `scripts/g4_pipeline/results/`. Those five exits are read
  back from the committed `container_exit_code` of the five `*.meta.json`
  artifacts, in the order of the Commands table above.
- Not run: the same five inputs on a dedicated 4 GB device.
- Expected command: identical to the five above, executed on 4 GB hardware.
- Blocking issue: no dedicated 4 GB device, no `/dev/kvm` for a nested VM, and
  no second host is reachable from this session. The 4 GiB ceiling here is a
  cgroup limit on a 7.66 GiB WSL2 VM.
```

**L2 — the image identity is unreconciled; C1 is not a precondition this round
could satisfy.** `PIN_MANIFEST.md` §1 records
`sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800`;
the image present locally is
`sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229`.
Issue #324's C2 states it runs "after C1", and C1's own acceptance leaves the
unreproducible-digest conflict as an explicit operator decision. **The runs
below were therefore taken against an image whose identity is still in dispute.**
They are valid as measurements of *this* image; they are not evidence that the
*pinned* image was measured, and they do not satisfy C2's "After C1" ordering.
`build.sh` was not run in this round (a sibling worker owns builds).

**L3 — the deployed caps were not lowered, because the 80-page measurement did
not require it.** The acceptance says "If the 80-page case exceeds the device
limit, lower the page and call budgets together under the existing T2a rule and
repeat the affected gold run." It did not: the case completed at
`cgroup_peak_bytes` 3,382,202,368 B against the 4,294,967,296 B ceiling, with
`oom_kill_events_in_cgroup: 0` and `harness_failures: []`, so the conditional
never fired and `.env.example` is **untouched by this round**. Whether a
dedicated 4 GB device (L1) agrees with that margin is the open question this
record cannot answer. If a later dedicated-device round does force a reduction,
the rule is arithmetic and not a judgment call:
`attachment_budgets.py:165-176` `required_run_calls()` returns
`max_pdf_pages + max_vision_crop_calls_per_run + RETRY_RESERVE_CALLS (1)`, and
`validate()` rejects anything lower, so the page cap and the run cap must be
lowered **together**, keeping the difference at `crop + 1` — currently
`80 + 8 + 1 = 89`. Lowering either alone makes the deployment fail `validate()`
at boot. Two tests pin that: `tests/host/test_attachment_budgets.py:1220-1234`
and `:513-533`. Neither was run this round because neither `.env.example` nor
`attachment_budgets.py` changed.

**L4 — the scanned case's latency figure is contaminated; every other field is
clean, and the contamination is fully described where the figure appears.** The
host-side tesseract process described in the scanned section was the only
interference with any run in this record; the native, mixed, PNG and 80-page
runs had no competing host process started by this worker. The two long runs
completed in 15.7 and 16.7 minutes of wall clock respectively.

**L5 — two of the mixed case's three pages are known misses, reproduced not
resolved.** Page 2's missing table region and page 3's unread chart-raster text
are both the defects the T2a record already attributes to `known_gaps[0]` and
to T2d's region-scoped OCR. They are not a C2 blocker, and they are not closed
by this record. Page 3's `expected_ocr_quote` was therefore never read by this
run, and no OCR quote on the mixed case is scored here.

**L6 — the PNG rejection is unasserted by any test.** No test in the repository
asserts the harness's PDF-only rejection (zero hits for `PdfiumError`), and the
rejection is emergent from the PDF backend rather than an explicit guard. The
`exit 1` recorded above is therefore a measurement of a behaviour nothing in the
tree pins, and the image case's product-path result is explicitly not delivered
by it.

**L7 — out-of-band cgroup peaks are unreadable on this host.**
`host_side_cgroup_memory_peak_bytes` / `…swap_peak…` are `null` on every run
(L1's host, §*Resource use*). The in-container readings the driver takes
(`memory.peak`, `VmHWM`, `VmSwap`, `memory.swap.current`) are present and are
what the tables above use.

## Merge gates

| Gate | State | Note |
| --- | --- | --- |
| C2 acceptance — gold run on **dedicated 4 GB hardware** | **NOT SATISFIED** | L1. The runs are a cgroup ceiling on a 7.66 GiB WSL2 VM. |
| C2 acceptance — runs "After C1" | **NOT SATISFIED** | L2. C1 is unresolved; the image identity is in dispute and the run used `0f278c22…` as found. |
| C2 acceptance — native-text, mixed and scanned PDF gold compared with independent committed expectations | **SATISFIED as a comparison, with two carried misses** | all three compared page by page above against `manifest.json` — manually, not by an automated scorer. Native-text: full agreement. Scanned: every OCR expectation met; the `text`-region misses are `known_gaps[0]`, already recorded. Mixed: two of three pages agree; pages 2 and 3 reproduce the T2a record's two recorded misses without adding a new one. |
| C2 acceptance — 80-page attachment at the current caps | **SATISFIED for the cgroup reading; NOT SATISFIED as a device reading** | measured: fits with 21.25% margin, 4.7% below the T2a prediction, no cap change forced (L3). But there is no committed content expectation for this case (§*`2022_Q3_MSFT.pdf`*), and it is a cgroup ceiling, not the device (L1). |
| C2 acceptance — image case retains the PDF-only rejection, not counted as an image-pipeline pass | **SATISFIED** | exit 1, `PdfiumError: Data format error`, `oom_killed_docker_inspect: false`, recorded as a negative assertion only. |
| C2 acceptance — record revision, dataset and model hashes, command, exit code, page/region and OCR outcomes, device identity, peak resource use, per-case result | **SATISFIED** | §*Environment / dataset version*, §*Commands*, §*Per-case results*, §*Resource use and device identity*. |
| C2 acceptance — "If the required device is unavailable, record the expected command and missing prerequisite as blocked evidence; do not … close C2" | **SATISFIED** | L1's `Validation:` block, verbatim template. C2 is left explicitly open. |
| C2 acceptance — "If the 80-page case exceeds the device limit, lower the page and call budgets together" | **NOT TRIGGERED** | the case did not exceed the limit under the cgroup ceiling, so no cap moved (L3). Whether the dedicated device agrees is L1's open question. |
| T2a rule — 80-page point measured before the page and call caps are treated as shipped defaults | **MEASURED on the cgroup reading; the device reading is still owed** | the extrapolation's residual risk (*Leftovers* 1) is discharged for the cgroup reading and remains open for the device reading (L1). |
| Budget-coupling rule (page cap and run cap move together at `crop + 1`) | **N/A this round** | L3 — no cap was changed, so no test was owed. |
| T2a *Leftovers* 5 (dedicated-device reading owed by a gold round) | **STILL OPEN** | This round is that gold round's **cgroup** half. The device half remains outstanding. |

**C2 STATUS: the gold contract itself held — under the literal
`--memory=4g --memory-swap=4g --cpus=4` cgroup ceiling at source revision
`670f65f`, pin image `0f278c22…`, dataset `fin-g4-attachment-gold-v1@1.0.0`:
the native-text case matched every manifest expectation including the "OCR must
not run" negative assertion; the mixed case reproduced its two known T2a-record
misses and nothing new; the scanned case met every OCR expectation with byte
and character-level agreement to two prior runs; the PNG case was rejected with
the expected `PdfiumError` and a non-OOM exit 1; and the 80-page case fit the
current caps with 21.25% margin, 4.7% below the T2a extrapolation, so no budget
was lowered. C2 is nonetheless **NOT CLOSED BY THIS RECORD**: the acceptance
requires a DEDICATED 4 GB DEVICE, this host is a 7.66 GiB WSL2 VM whose only
claim to the limit is a cgroup ceiling, and the issue states verbatim that a
cgroup ceiling on a larger host cannot close C2. Outstanding, recorded as
blocked evidence: the dedicated-device round (L1); C1's unreconciled image
identity, which these runs predate (L2); and the integrated vision/G4
acceptance the image case's content answer belongs to. No plan item status is
changed by this record.**

## Validation

```text
Validation:
- Run: `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-native-text-001.pdf` — exit 0
- Run: `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf` — exit 0
- Run: `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-scanned-001.pdf` — exit 0 (latency contaminated by this worker's own host-side tesseract check; content fields clean)
- Run: `bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png` — exit 1 (expected PDF-only rejection)
- Run: `bash scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2022_Q3_MSFT.pdf` — exit 0 (80 pages, fits the caps)
- Not run: the same five inputs on a dedicated 4 GB device.
- Expected command: identical, on a dedicated 4 GB device.
- Blocking issue: no dedicated 4 GB device is reachable from this session (L1);
  the 4 GiB ceiling here is a cgroup limit on a 7.66 GiB WSL2 VM with no
  /dev/kvm and no second host.
```
