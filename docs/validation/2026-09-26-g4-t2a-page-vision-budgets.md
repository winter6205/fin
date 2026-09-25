# G4 T2a page and visual-call budgets — fin (2026-09-26)

Validation record for plan
[`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T2a (Measure and decide page and visual-call defaults)** on branch
`feat/g4-attachment-t2a`.

Worktree `/home/winner/projects/fin-wt-g4-attachment`, base `1700527`.

Scope: T2a is a measurement and decision ticket plus the validation seam that
makes the decided numbers enforceable. It delivers **no** preparation, parser,
vision, or index execution — those are T2b–T2f. It also does not add `docling`
to `pyproject.toml`: the pipeline was run from a pinned image precisely so the
runtime-dependency decision stays with T2d.

## What was decided

| env key | field | decided value |
| --- | --- | --- |
| `FIN_ATTACHMENT_MAX_BYTES` | `max_upload_bytes` | `4194304` (4 MiB) |
| `FIN_ATTACHMENT_MAX_PDF_PAGES` | `max_pdf_pages` | `80` |
| `FIN_ATTACHMENT_VISION_CALLS_PER_RUN` | `max_vision_calls_per_run` | `88` |
| `FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN` | `max_vision_crop_calls_per_run` | `8` |

**These values are provisional, not a shipped default.** Two of the three gold
cases have no committed pipeline measurement here (*Gold cases under the pinned
pipeline*), and the scanned-page OCR quantity that could invalidate
`max_pdf_pages = 80` is unmeasured here. What this ticket delivers is a
validated, enforced, measured-where-measurable starting configuration plus the
list of what must be measured before an operator authorises it as the product
default (*Leftovers* 1, 4, 6, 7, 9).

The values are persisted **only** in `.env.example`. `attachment_budgets.py`
carries no numeric default: the spec requires a missing limit to fail
validation, so a default baked into code would be the guessed number the
ticket exists to avoid. `docker-compose.yml`, `web/`, and the HTTP route
constant are untouched — replacing `ATTACHMENT_MAX_BYTES`
(`src/fin/host/workspace_http.py:600`) is T2b's write, and T2b is blocked on
this record.

## Basis: the measured upload distribution

`data/corpus/g4_upload_distribution.json`, 13 real filings from
`data/corpus/`, `pdfinfo` page counts cross-checked against the corpus catalog
(zero disagreements):

| | min | p50 | p90 | max |
| --- | --- | --- | --- | --- |
| bytes | 624 | 501,518 | 2,141,685 | 2,223,272 |
| pages | 1 | 51 | 74 | 80 |

Per-page density ranges from 624 B (the 1-page GOOG file) to 30,878.8 B
(`2023_Q1_MSFT.pdf`, 72 pages / 2,223,272 B).

* **Byte cap.** `4194304 = 2**22`, the smallest power-of-two boundary at or
  above the observed maximum `2,223,272`. It is also above the density
  worst case for the page cap: `80 × 30,878.8 = 2,470,304` bytes. The current
  route constant `2 * 1024 * 1024 = 2,097,152` rejects two real filings
  (`2023_Q1_MSFT.pdf` at 2,223,272 and `2023_Q2_MSFT.pdf` at 2,141,685), which
  is why T2b replaces it rather than reusing it.
* **Page cap.** `80` is the observed corpus maximum, so no file in the measured
  distribution is rejected on pages. The alternative — `40` — is costed below.

## Basis: the pinned pipeline under a hard 4 GiB ceiling

Image `fin-g4-pipeline:t2a`
(`sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800`,
python:3.12-slim), docling `2.130.0`, torch `2.9.1+cpu`, torchvision
`0.24.1+cpu`, transformers `5.17.0`, opencv-python `5.0.0.93`, pypdfium2
`5.13.0`, tesseract `5.5.0` (`eng` + `chi_sim` present), Heron layout
`docling-project/docling-layout-heron@8f39ad3c` (torch engine — `onnxruntime`
is not installed, so the ONNX override cannot be used). Full provenance with
per-artifact sha256 and Hub ETag verification:
[`scripts/g4_pipeline/PIN_MANIFEST.md`](../scripts/g4_pipeline/PIN_MANIFEST.md).

Ceiling as run, verbatim:

```
docker run --memory=4g --memory-swap=4g --cpus=4 ...
```

**Device limitation, recorded verbatim because it bounds the claim.** This
WSL2 kernel does not enforce the swap half of that ceiling:

```
WARNING: Your kernel does not support swap limit capabilities or the cgroup is not
mounted. Memory limited without swap.
WARNING: Your kernel does not support memory swappiness capabilities for the
following metrics, or the cgroup is not mounted: memory.swappiness, memory.use_hierarchy
```

Inside the container `memory.max = 4294967296` while `memory.swap.max = max`.
So every run below is *resident* memory capped at 4 GiB with swap available,
not a no-swap 4 GB device measurement. Margins are therefore computed with
`VmSwap` added to resident peak, which is the strictest reading available on
this host.

Measured runs (`scripts/g4_pipeline/results/*.meta.json`):

| file | pages | bytes | `VmHWM` | cgroup peak | `VmSwap` | OOM | exit | total ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2023_FY_GOOG.pdf` | 1 | 624 | 489,889,792 | 384,000,000 | 0 | 0 | 0 † | 3,489.8 |
| `2023_FY_GOOG.pdf` | 1 | 624 | 885,321,728 | 937,984,000 | 0 | 0 | 0 † | 5,830.8 |
| `2023_FY_GOOG.pdf` | 1 | 624 | 1,301,147,648 | 1,065,414,656 | 0 | 0 | 0 | 15,013.3 |
| `2023_Q1_MSFT.pdf` | 72 | 2,223,272 | 3,324,112,896 | 3,102,502,912 | 182,738,944 | 0 | 0 | 1,528,374.9 |

† `…T164419Z` and `…T170133Z` are not measurements of the pipeline. Each
carried a named `slice_pass_error` — `FileNotFoundError: Model
'docling-project/docling-layout-heron' not found in artifacts_path` and
`ValueError: Not able to find a model file for TableModel04_rs`, the two
incomplete-cache states the fetch preamble in `run_measured.sh` exists to
prevent — so `regions_total` is `0 / 0 / 0` and `docling_status` is `null` in
both, and the exit is 0 because the driver writes a slice-pass failure into the
record rather than failing on it. Their `VmHWM` is torch plus the weights each pass
reached before the named model was missed — 489,889,792 B when Heron itself was
absent from the cache, 885,321,728 B when only TableFormer was — and their
timings are an aborted conversion, not a completed one. The rows stay because
that ramp is a real measurement and because a run can exit 0 having done almost
none of the work. The three later 1-page runs, `…T184410Z`, `…T195408Z`, and
`…T204930Z`, are the harness reproducing this input after each of its own
repairs and are recorded with their fields under *Two field-provenance
corrections*, *Splitting the harness without moving a field*, and *Repairing the
gap semantics without moving a measurement*.

The 72-page conversion is the load-bearing one: Docling whole-document
`829,935.8 ms`, i.e. `11,526.9 ms/page`; warm steady-state single-page
conversion mean `9,459.3 ms`, median `3,559.1 ms`, max `47,298.0 ms`;
render-plus-OCR pass `17,369.3 ms` (`241 ms/page`, with 0 pages needing OCR in
this native-text file); Heron regions over 72 pages: 790 text, 55 table,
**1 figure**; 222,233 native text characters.

Model load, not page count, dominates the floor: the four 1-page runs that
completed both passes peaked between 1,234,628,608 and 1,301,147,648 B, i.e.
within 5.4% of each other and 1.15–1.21 GiB before the per-page term is worth
anything.

### Measurement conditions (they bound every latency figure here)

The host is a 4-core WSL2 VM with **7,846 MiB of total RAM** — it is not a 4 GB
box, and it cannot back a 4 GiB container plus page cache without swapping. At
the time of the gold runs:

```
$ nproc; cat /proc/loadavg
4
15.88 15.80 13.14
$ free -m
Mem:   total 7846  used 4906  free 732  buff/cache 2452  available 2940
Swap:  total 15360 used 4003
$ grep -E "^pswpout|^pgmajfault" /proc/vmstat
pswpout 4408871          # ≈ 16.8 GiB written out since boot
pgmajfault 817456
```

Four unrelated agent processes (`qodercn`, 0.5 GiB each) and the iknow-memory
Elasticsearch/Neo4j containers share those four cores, and each pipeline
container was given `--cpus=4`, i.e. one whole host, so two concurrent runs
contend 1:1. The same 1-page input measured 15,013.3 ms, 29,171.3 ms,
16,407.2 ms, and 10,402.8 ms across the four runs that completed both model
passes (`…T170956Z`, `…T184410Z`, `…T195408Z`, `…T204930Z`) — a 2.8× spread on
identical input, and the same 1.8× spread on the warm per-page conversion
inside those runs (3,571.1 / 6,074.1 / 5,942.4 / 3,374.5 ms). Contention, not
variance in the input; `driver.py` records no load figure for any of them,
which is *Leftovers* 6.

What this does and does not invalidate:

* `memory.max = 4294967296` **is** enforced per container and the OOM counter
  is real, so every resident-peak figure and the byte/page counts stand.
* Global major-fault traffic is not attributable to one container, so **no
  millisecond figure here is a device specification**. They are upper bounds
  measured under load, used only to order the two page-cap options, and that
  ordering is arithmetic rather than a comparison of two measurements: the
  `40` row is the same per-page rate over half the pages, so contention moves
  both options together. What the figures do establish is that per-page cost is
  the binding constraint, not total bytes.
* `driver.py` records no load or major-fault figure per run, so the runs that
  mattered cannot be corrected after the fact. A serial re-measure with load
  captured before and after is the only clean evidence available and **it has
  not been run** — *Leftovers* 6 carries it.

### Extrapolation to the 80-page cap (labelled as extrapolation)

Linear in page count, taking the largest 1-page resident peak as the
model-load baseline:

```
page-attributable  = 3,324,112,896 − 1,301,147,648 = 2,022,965,248 B over 72 pages
per page           = 28,096,739 B            ≈ 26.8 MiB/page
80 pages, resident = 1,301,147,648 + 80 × 28,096,739 = 3,548,886,768 B  = 3.31 GiB
+ swap as resident = 3,548,886,768 + 182,738,944      = 3,731,625,712 B  = 3.48 GiB
ceiling                                                = 4,294,967,296 B  = 4.00 GiB
margin                                                 =   563,341,584 B  = 13.1%
```

So `max_pdf_pages = 80` fits a 4 GiB device with ~13% margin on the strict
(plus-swap) reading, **but the 80-page point itself was never run** — it is
11% beyond the largest measurement. That is the residual risk of this ticket
and is listed under *Leftovers*.

`40` pages, the rejected alternative, costs:

| | cap 80 (chosen) | cap 40 |
| --- | --- | --- |
| vision calls/run | 88 | 48 |
| parse, extrapolated | 80 × 11,526.9 ms = 15.4 min | 40 × 11,526.9 ms = 7.7 min |
| vision, serial | 88 × 3,058 ms = 4.5 min | 48 × 3,058 ms = 2.4 min |
| one max attachment end-to-end | ≈ 19.9 min | ≈ 10.1 min |
| input tokens at the cap | 88 × 4,275 = 376,200 | 48 × 4,275 = 205,200 |
| 4 GiB margin, plus-swap reading | 13.1% | 39.3% |
| corpus files rejected | 0 of 13 | 9 of 13 (69%: 46, 50, 51, 51, 52, 67, 72, 74, 80 pages) |

The vision rows use the mean of the three calls on the pipeline-rendered
`g4-mixed-chart-001.pdf` page 3 (`3,496 / 3,094 / 2,585` ms,
`4,237 / 4,279 / 4,309` input tokens), not the earlier drawn-page probe: a page
of a real filing is what a scan actually sends, and it costs about three times
the tokens (*Basis: the configured vision provider*). Both token columns price
**every** call, crops included, at the full-page rate, which overstates a max
run by roughly 23K tokens (~6%) because a crop costs about 1,400; the upper
bound is kept deliberately, since a run budget is spent in worst case. Both
margin columns use the strict plus-swap reading of the extrapolation above
(`1,301,147,648 + pages × 28,096,739 + 182,738,944` against the 4 GiB ceiling).

`80` was chosen because `40` does not cut a fringe. It rejects 9 of the 13
measured filings — the four MSFT 10-Qs (67, 72, 74, 80 pages), the four AMZN
10-Qs (50, 51, 51, 52 pages), and `2023_Q1_AAPL.pdf` (46 pages) — and keeps
only the three shortest AAPL 10-Qs and the 624-byte GOOG fixture, so the
excluded set is most of the quarterly-filing population this product exists to
read rather than one issuer's long tail. The ticket's own acceptance clause is
that the caps admit the observed corpus, and every bound is a deployment
setting with no code default — an operator on a 2 GiB device lowers all four
without a code change, which is the mitigation `40` would otherwise have
provided by fiat.

Two rows of that table need a caveat rather than a number. Both latency columns
exclude OCR, because the file that reached 72 pages needed none; and the
scanned-page OCR cost is unmeasured on this host (*Not covered*). A page cap is
not the lever for OCR: if a scanned page really costs minutes of CPU, no value
in the corpus-covering range makes an 80-page — or a 10-page — scanned filing
feasible, and the fix belongs to T2d's render DPI, `--psm`, and
skip-OCR-when-native-text choices. Setting `max_pdf_pages` from an unmeasured
quantity would be exactly the guessed limit this ticket forbids, so the cap
rests on the two things that were measured: the enforced 4 GiB resident ceiling
and the real page/byte distribution.

## Basis: the configured vision provider

Three committed probe artifacts, all against fin's configured provider
(`FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY`, read from the environment; no
provider or model identifier is written into `attachment_budgets.py`):

| artifact | image | shape | bytes | what it prices |
| --- | --- | --- | --- | --- |
| `data/corpus/g4_vision_probe.json` | drawn page in `/tmp` | 816 × 1056 | 37,482 | the script's own capability probe |
| `data/corpus/g4_vision_probe_page.json` | `g4-mixed-chart-001.pdf` page 3, rendered by the pinned pipeline at 200 dpi | 1700 × 2200 | 205,088 | a real full-page scan |
| `data/corpus/g4_vision_probe_gold.json` | `g4-image-chart-001.png` (committed gold) | 1200 × 746 | 40,419 | the crop / figure-only shape |

The middle row's subject is a page bitmap under `pages/`, which is gitignored, so
it is pinned by records rather than by the file: the probe artifact carries
`1700 × 2200` over `205,088` bytes, the committed `…T174114Z` page-3 record
carries the same dimensions and byte count, and the re-render under the repaired
harness (`…T204952Z`) produced `205,088` bytes again with
`page_image_png_sha256 = 72b000ce49688014…`. The probe's subject and that hash
name the same bytes because two renderings of the same fixture at the same DPI
agreed, not because one file was hashed twice.

Each artifact is three calls — chart presence, chart meaning, structured JSON —
and every one of the nine calls succeeded. Per-call latency and provider-reported
usage:

| image | presence | meaning | structured | input tok | output tok |
| --- | --- | --- | --- | --- | --- |
| drawn 816 × 1056 | 2,318 ms | 1,820 ms | 2,763 ms | 1,343 / 1,385 / 1,413 | 16 / 355 / 305 |
| rendered page 1700 × 2200 | 3,496 ms | 3,094 ms | 2,585 ms | 4,237 / 4,279 / 4,309 | 31 / 589 / 478 |
| gold crop 1200 × 746 | 1,322 ms | 2,626 ms | 2,573 ms | 1,366 / 1,408 / 1,438 | 72 / 585 / 612 |

The checkable witness is the mean of the plotted series, computed outside the
script and never stated in a prompt: `20.75` for the drawn page, `94,186.50` for
both Apple-chart images. On the two gold-shaped probes the provider named all
four bar values correctly (`82,959 / 117,154 / 94,836 / 81,797`) against the
manifest's independent expectation, and its structured answer carried every key
with `mean_of_series: 94186.5`. So the provider takes a real rendered filing
page, reads a filing chart correctly at both page and crop resolution, returns
parseable structure, and reports usage.

The drawn-page artifact was regenerated by the current script (command 14) once
the probe output carried the `image` and witness block, so all three rows come
from one emitter contract rather than two. That re-run again named `20.75` and
again read all four of the page's own bar values (`12 / 17 / 23 / 31`).

Two consequences for the budget:

* **Token cost is driven by page pixels, not by the prompt.** The rendered filing
  page costs about three times the input tokens of either smaller image
  (≈4,270 versus ≈1,400) on a prompt difference of a few words. A run budget
  priced on the drawn probe would have understated a real page by that factor,
  which is why `max_vision_calls_per_run = 88` is spent in calls rather than
  tokens but was set after seeing this spread.
* **Crop cost is now measured, and it is the cheaper axis.** A crop-shaped
  attachment (1200 × 746) is billed at roughly the same input tokens as the
  drawn page, well under a full-page scan, so the crop headroom below is not
  priced against a per-call cost higher than a page scan.

`max_vision_crop_calls_per_run = 8` remains **a planning count, not an observed
demand**. The committed gold set has 2 chart-present pages
(`g4-mixed-chart-001` page 3, `g4-image-chart-001` page 1) and each of those
charts carries 4 labelled numeric values that a question could need a
higher-resolution look at after the full-page scan, so `2 × 4 = 8`. What is now
measured rather than assumed is the *shape and cost* of such a call, on the
committed chart PNG; what is still a plan is *how many* crops a run will want,
because no production caller asks yet (*Not covered*).

`88 = 80 + 8` is `validate()`'s exact floor, not a cushion: the rule allows
equality, so a run that admits the page cap has spent 80 calls before anyone
asks for a crop and holds exactly the declared 8. Those 8 are crop headroom, and
a retried crop draws from them, which is what keeps the pair self-consistent at
the minimum. A retried **full-page** scan has no line item of its own —
`preflight_run` prices one scan per admitted page plus `requested_crop_calls` —
so on a page-cap run that also wants its 8 crops it is the 89th call and the run
fails `attachment_vision_budget_exceeded`. Crops therefore have headroom and
page-scan retries have none: the spec's page-at-a-time rule ("pages submitted
singly so resource use is bounded and failures can be retried by page") is
honoured only while `88 − pages` is spare, i.e. below the page cap. Whether a
retry borrows the crop bound or the run budget rises above the floor is
undecided, and it is T2e's to price: T2e is the first ticket that issues a retry
at all (*Leftovers* 9).

## Validation seam

`src/fin/host/attachment_budgets.py` (new, 226 lines): a frozen four-field
`AttachmentBudgets`, `from_environ` requiring all four keys, `validate`
enforcing positivity, an upper bound at `sys.maxsize`, and
`calls >= pages + crop`, and `preflight_run(budgets, admitted_pages,
requested_crop_calls)` which checks the page cap per attachment, then the crop
headroom on its own, then the run total — all **before** any page is skipped —
and maps the two violations onto the only outward codes the module owns:
`attachment_page_limit_exceeded` and `attachment_vision_budget_exceeded`. A
run asking for more crops than the headroom is refused even when `pages +
crops` still fits the run budget, because the fourth bound is a deployed price,
not a suggestion. The spec asks for one finite run budget with crops counting
against it; pricing crops separately is this ticket's decision, so a deployment
can cut crop work without cutting page admission, and `validate` keeps the pair
self-consistent. A config failure has no outward code (spec's closed list), so
`AttachmentBudgetConfigError` is internal by contract.

Tests: `tests/host/test_attachment_budgets.py`, 109 cases. The three
deployed-bound cases bind `.env.example` to the two measured data files, so
the numbers cannot drift from their evidence unnoticed:

* `test_deployed_bounds_are_literals_with_no_shell_fallback`
* `test_deployed_bounds_admit_every_file_in_the_measured_distribution`
* `test_deployed_bounds_prepare_every_gold_case_in_one_run`

## Commands and results

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py` (RED, before the module existed) | 1 | `ModuleNotFoundError` |
| 2 | same, after `attachment_budgets.py` | 0 | 102 passed |
| 3 | same, before `.env.example` carried the four keys (RED for deployed bounds) | 1 | 3 failed, 102 passed |
| 4 | same, after `.env.example` | 0 | 105 passed |
| 5 | `uv run --no-sync python -m py_compile src/fin/host/attachment_budgets.py tests/host/test_attachment_budgets.py scripts/g4_upload_distribution.py scripts/g4_vision_probe.py` | 0 | the repo has no ruff/mypy gate; compile is the typecheck seam |
| 6 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py tests/retrieve/test_live_mcp_t3.py tests/retrieve/test_live_mcp_t4.py tests/retrieve/test_live_mcp_t5.py tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_host_graph_hop.py` | 0 | 11 passed in 36.65 s |
| 7 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py -k crop` (RED: the fourth bound was documented but not enforced) | 1 | 3 failed `DID NOT RAISE AttachmentVisionBudgetExceeded`, 1 passed |
| 8 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py` | 0 | 109 passed |
| 9 | `uv run --no-sync pytest -q tests/host` | 0 | 1860 passed, 60 skipped, 227 deselected in 124.08 s; the skips are the pre-existing `FIN_CHECKPOINT_URL` and `web/dist` gates |
| 10 | `uv run --no-sync python -m py_compile scripts/g4_pipeline/driver.py scripts/g4_vision_probe.py scripts/g4_upload_distribution.py && bash -n scripts/g4_pipeline/run_measured.sh` | 0 | after the harness repair |
| 11 | `scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2023_FY_GOOG.pdf` | 0 | post-repair reproduction of the recorded 1-page run; fields compared in *Two field-provenance corrections* |
| 12 | `uv run --no-sync python scripts/g4_vision_probe.py --dotenv-start /home/winner/projects/fin --image scripts/g4_pipeline/results/g4-mixed-chart-001_20260925T174114Z/pages/page_0003.png --series "Total net sales" --mean 94186.5 --out data/corpus/g4_vision_probe_page.json` | 0 | 3 real calls on a pipeline-rendered filing page; `supports_image_input=true structured_output_ok=true usage_reported=true mean_answered=true` |
| 13 | `uv run --no-sync python scripts/g4_vision_probe.py --dotenv-start /home/winner/projects/fin --image data/corpus/g4_attachment_gold/g4-image-chart-001.png --series "Total net sales" --mean 94186.5 --out data/corpus/g4_vision_probe_gold.json` | 0 | 3 real calls on the committed crop-shaped gold image; same three verdicts true |
| 14 | `uv run --no-sync python scripts/g4_vision_probe.py --dotenv-start /home/winner/projects/fin` | 0 | 3 real calls on the drawn page, re-recorded under the current artifact contract; `supports_image_input=true structured_output_ok=true usage_reported=true mean_answered=true`, `image.bytes=37482` |
| 15 | `scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2023_FY_GOOG.pdf` | 0 | after the decomposition; `results/2023_FY_GOOG_20260925T195408Z*`, 186-key tree identical to `…T184410Z` (see *Splitting the harness without moving a field*) |
| 16 | `scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png` | 1 | the non-PDF rejection re-recorded under the unified meta schema; `results/g4-image-chart-001.png_20260925T195253Z.meta.json`, `json_lines=0`, unread fields `null` |
| 17 | `docker run --rm --entrypoint python fin-g4-pipeline:t2a -c "import inspect, pypdfium2, pypdfium2.raw as raw; print(inspect.signature(pypdfium2.PdfPage.get_objects)); print(int(raw.FPDF_PAGEOBJ_IMAGE))"` | 0 | `(self, filter=None, max_depth=15, form=None, level=0, textpage=None)` and `3` — the pinned signature and kind the image-object scan now calls, and the reason `image_only=` was removed |
| 18 | `uv run --no-sync python -m py_compile scripts/g4_pipeline/driver.py scripts/g4_vision_probe.py scripts/g4_upload_distribution.py && bash -n scripts/g4_pipeline/run_measured.sh scripts/g4_pipeline/build.sh` | 0 | after this round's harness edits |
| 19 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py` | 0 | 109 passed, after the harness edits (the module under test is untouched by them) |
| 20 | `scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2023_FY_GOOG.pdf` | 0 | after the gap-semantics repair; `results/2023_FY_GOOG_20260925T204930Z*`, 186 field paths against `…T195408Z` with exactly one out and one in |
| 21 | `scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf` | 0 | `results/g4-mixed-chart-001_20260925T204952Z*`; `pdf_image_objects 1 / 0 / 1`, page PNG bytes and `regions_total 24 / 0 / 2` identical to `…T174114Z` |

Run 6 is the real-boundary check: it spawns the compiled iknow-memory stdio
server (`/home/winner/projects/iknow-memory/dist/index.js`, built 2026-09-25)
and drives a real `initialize` → `tools/list` → `tools/call` push, retrieve,
resume, invalidation, `read_source`, and citation round trip, plus the
`档`-assignment hop through the host graph. Per case, all passed:
`test_real_iknow_round_trip_is_byte_identical_and_idempotent`,
`test_real_iknow_resume_skips_landed_batches_and_outage_is_named`,
`test_real_iknow_invalidation_is_idempotent_and_hides_old_text`,
`test_degraded_empty_response_is_named_failure_not_empty_success`,
`test_compiled_product_retrieve_sends_no_capability_off_keys`,
`test_live_iknow_tools_list_includes_read_source`,
`test_live_iknow_read_source_returns_pushed_body_by_ref_and_id`,
`test_live_iknow_read_source_unknown_key_is_a_named_miss`,
`test_research_done_cites_this_runs_retrieved_text`,
`test_ungrounded_answer_over_the_same_live_evidence_is_not_done`,
`test_the_host_run_sends_its_assigned_档_to_the_real_server_which_answers`.

What run 6 does and does not prove for this ticket: the budget module has no
production caller yet, so no case here exercises `attachment_budgets` across a
real boundary — that becomes possible when T2b wires the upload door. Run 6 is
evidence that this worktree's real MCP path is intact after the change and that
nothing in T2a regressed it. A budget-specific live case is deliberately not
invented here, and this is not the G4 gold run the plan's *End of round*
requires.

### Gold cases under the pinned pipeline

`scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/<case>` for
each of the three committed gold cases, in the same ceiling as the corpus runs
above. The numbers below are read out of the tracked
`scripts/g4_pipeline/results/*.meta.json`, and nothing untracked — a run
directory, an aggregate file — is evidence for a figure in this document.

| case | pages | bytes | cgroup peak | `VmHWM` | OOM / exit | total ms | text / table / figure | state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `g4-mixed-chart-001.pdf` | 3 | 35,324 | 1,671,122,944 | 1,402,855,424 | 0 / 0 | 31,823.1 | 24 / 0 / 2 | measured |
| `g4-scanned-001.pdf` | 3 | 677,716 | — | — | — | — | — | **no committed measurement** |
| `g4-image-chart-001.png` | 1 | 40,419 | — | — | 1 (not OOM) | — | — | **rejected by the harness** |

* `g4-mixed-chart-001.pdf` is the load-bearing gold case for the crop bound:
  Heron returned `figure = 2` over its 3 pages, and `pages_needing_ocr = 0`. Its
  chart-page count is what `max_vision_crop_calls_per_run = 8` is priced against.

#### Expected versus actual on the one measured case

Expectations are the committed `per_page` entries of
`data/corpus/g4_attachment_gold/manifest.json`; actuals are the page records of
`scripts/g4_pipeline/results/g4-mixed-chart-001_20260925T174114Z/g4-mixed-chart-001.measured.jsonl`.

| page | expected | actual | verdict |
| --- | --- | --- | --- |
| 1 | text + figure, no OCR, 449 native chars | text 14 / figure 1, `ocr_used` false, 460 native chars | as expected |
| 2 | text + **table**, no OCR, 1,181 native chars | text 5 / **table 0**, `ocr_used` false, 1,111 native chars | **miss** — Heron found no table region on a condensed statement page |
| 3 | text + figure, **OCR expected** for the figure only (chart title, 4 period labels, 4 value labels and the source line are pixels), 334 native chars | text 5 / figure 1, **`ocr_required_by_repo_rule` false, `ocr_used` false**, 337 native chars | **miss** — the chart's pixel text was never read |

Neither miss is a budget problem, and both belong to T2d rather than being
silently absorbed here:

* Page 3 is a rule conflict, not a model failure.
  The harness asked the repo rule at its recorded parameterisation
  `detect_ocr_required(min_text_chars=1)` — i.e. "does this page have no text
  layer at all" — and the page has 337 characters, so it returns false. Under
  the product default `min_text_chars = 100`
  (`src/fin/corpus/ocr.py:141-152`) the answer is the same, and the rule still
  cannot express what the manifest's `ocr_scope` asks for: OCR of the figure
  region only, on a page that also carries a real text layer. Region-scoped OCR
  is therefore T2d's to build, not a threshold to tune.
* Page 2 is the same recall problem already recorded as `known_gaps[0]`: one
  figure in 72 pages of `2023_Q1_MSFT.pdf`, and now 0 tables on a statement page
  that is one. This is further reason the spec's full-page scan rule is not
  optional.
* `g4-scanned-001.pdf` has no committed measurement. Attempts on this host got
  as far as rendering page bitmaps, and what they left under `results/` is
  untracked — one meta with `json_lines: 0` and no peak, plus a later directory
  whose meta and JSONL do carry figures — so **no pipeline-measured cost of any
  kind — wall time, CPU time, or resident peak — is claimed for a scanned
  page.** The two scanned-page time figures that *do* appear, in *Not covered*,
  are stopwatch observations of a killed attempt with no artifact behind them,
  and are labelled there as such. The bitmaps under `pages/` are gitignored
  derivatives reproducible from the committed fixture; *Leftovers* 4 stays open
  until a scanned-page run's meta and timer artifact are committed together.
* `g4-image-chart-001.png` failed in the harness, not in the ceiling:
  `run_measured.sh` hands its argument to `pypdfium2.PdfDocument`, which raises
  `PdfiumError: Failed to load document (PDFium: Data format error)` on a bare
  PNG. The pinned pipeline as driven here takes PDF input only, so a
  non-PDF attachment has no measured **preparation** cost. That is T2d's
  input-path decision, recorded as *Leftovers* 7; it does not move any of the
  four bounds, because an image is one page and its upload size is covered by
  the byte distribution. Its **vision** cost is measured, on this same committed
  file, in *Basis: the configured vision provider*.

All three cases together are 7 pages, so
`test_deployed_bounds_prepare_every_gold_case_in_one_run` prices
`7 + 8 = 15` calls against the `88` run budget and passes.

### Two field-provenance corrections found while repairing the harness

* `rendered_dpi` in the committed page records is **not a measurement**.
  pypdfium2 `5.13.0` gives `PdfBitmap` no `get_dpi()`, so the harness's
  read-fallback fired on every page of every recorded run and echoed the
  requested DPI. The delivered scale is still 200 dpi by arithmetic —
  `rendered_width_px = 1700` over `size_pt = 612` is
  `1700 / (612 / 72) = 200.0` — so no number in this record moves, but the
  field's provenance does: the repaired driver records `null` plus
  `rendered_dpi_error` instead of an echo.
* A swallowed harness failure was a zero or an absent key. Every catch is now
  paired with a named field (`read_errors`, `pdf_image_objects_error`,
  `rendered_dpi_error`, `totals.harness_failures`), pages after a slice-pass
  break are marked `not_measured_after_slice_failure` rather than dropped, and
  an unmeasured region pass totals `null` instead of `0`.

The repaired harness was then re-run on the smallest committed input,
`scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2023_FY_GOOG.pdf`
(`results/2023_FY_GOOG_20260925T184410Z*`, exit 0) and compared with the
recorded 1-page run `…T170956Z`: same `page_count`, `regions_total` `1 / 0 / 0`,
`native_text_chars_total = 49`, `pdf_image_objects = 0`,
`docling_status = SUCCESS`, a byte-identical rendered PNG
(`page_image_png_bytes = 31,454`), and `VmHWM` within 1% (1,288,630,272 against
1,301,147,648). Two fields differ, both explained above or by load: the page's
`rendered_dpi` is now `null`, and `cgroup_peak_bytes` came out *above* `VmHWM`
(1,678,012,416 against 1,288,630,272) where the recorded run had it below —
container-charged page cache is not ordered against process RSS, so the margin
arithmetic in the extrapolation section keeps using the larger reading available
for a run, which for the 72-page file is `VmHWM + VmSwap`. Per-page latency is
not comparable (6,074.1 ms against 3,571.1 ms) with the host at load 8.5 on four
cores.

### Splitting the harness without moving a field

`driver.py` and `run_measured.sh` were then decomposed for complexity only —
`main()` from 393 lines / CC 57 to 55 lines / CC 4, the three measurement passes
into frozen dataclasses — with the emitted field names frozen as the contract.
`run_measured.sh` folded its two `.meta.json` paths (measured and failed) into
one `emit_meta` serializer, which made three duplicate names on one quantity
visible: `docker_run_exit`, `container_exit`, and `host_cgroup_memory_peak_bytes`
said the same thing as `docker_run_exit_code`, `container_exit_code`, and
`host_side_cgroup_memory_peak_bytes`. The bare three are dropped, so
`driver_keys` is the single source of meta field names.

Two runs prove the split changed no measurement:

* measured — `results/2023_FY_GOOG_20260925T195408Z*` (exit 0) against the
  recorded `…T184410Z`: 186 keys, 0 missing, 0 extra, identical order, and every
  contract field intact (`page_image_png_bytes = 31,454`, `regions_total`
  `1 / 0 / 0`, `native_text_chars_total = 49`, `docling_status = SUCCESS`,
  `rendered_dpi = null` with its named error). Only timings and peaks moved, and
  this time the driver's `cgroup_peak_bytes` (1,125,564,416) fell back *below*
  `VmHWM` (1,296,621,568) — the reversal of the single field that differed
  before, which confirms the ordering caveat above rather than settling it.
* failed — `results/g4-image-chart-001.png_20260925T195253Z.meta.json` (exit 1,
  `json_lines = 0`) reproduces the non-PDF rejection under the unified schema,
  with `null` where values were never read and typed `1` / `false` where the
  harness did read them. The pre-unification meta for the same rejection wrote
  `""` for the unread peak and `"false"` as a string for `oom_killed`; it is
  deleted at this branch's head and is evidence only as of `d6b222a`, the commit
  that removed it, so the schema it demonstrated is quoted here rather than
  pointed at.

### Repairing the gap semantics without moving a measurement

The next finding was in the harness again, and this one made two artifacts of
one run disagree: `run_measured.sh` wrote its own `oom_killed` and
`oom_killed_source` over the driver's, while `peak_fields` and `oom_fields`
reported `max(…, default=0)` for a counter no snapshot had managed to read. One
rule covers both: a reading that never happened is a gap, never a value.

* The driver owns the OOM verdict; docker's opinion is recorded beside it as
  `oom_killed_docker_inspect` and no longer overwrites it.
  `oom_killed_source` names the counter only when a snapshot read it, and says
  `"not read; see oom_killed_error"` when none did.
* An unread peak is `null` plus a `<key>_error` naming its source and pointing at
  `memory_checkpoints[].read_errors`. The error sibling appears exactly when the
  value is `null`, so the key tree of a run that did read its snapshots is
  unchanged.
* `driver_keys` in `run_measured.sh` is now asserted against the record it
  projects: a totals key renamed in `driver.py` fails the run with
  `FATAL: driver totals lack …` rather than quietly summarising a real
  measurement as `null`.

Two duplications went with it. `native_text_codepoints` was a second name for
`native_text_char_count` and is deleted. The page-bitmap writer is one function
that records bytes *and* sha256 for every kept page: until this round the hash
was written only on pages that went through OCR, so a text page's payload was
identified by size alone.

The image-object scan also lost its two-level fallback ladder, and the first
run after the deletion is the reason the ladder's *shape* mattered. What the
scan assumed was the pinned library's signature — `get_objects(image_only=True)`
— and `pypdfium2 5.13.0` answers
`TypeError: PdfPage.get_objects() got an unexpected keyword argument
'image_only'`; its parameter is `filter`. Every page of that run recorded
`pdf_image_objects_error` and a `null` count instead of a spurious `0`, so the
rule that selects OCR could see "no images" on a page where the scan had simply
failed — and that is the only thing the run is quoted for: it is a working-tree
state between two edits, and its artifacts are not committed. The scan is now
one call that takes its kind constant from the same library version the record
pins:

```python
objects = page.get_objects(filter=[int(raw.FPDF_PAGEOBJ_IMAGE)])
```

Two runs prove this round moved no measurement:

* measured — `results/2023_FY_GOOG_20260925T204930Z*` (exit 0) against the
  recorded `…T195408Z`: 186 field paths in each, one leaving and one arriving
  (`pages[].native_text_codepoints` out, `pages[].page_image_png_sha256` in),
  with every contract field intact — `page_image_png_bytes = 31,454`,
  `pdf_image_objects = 0`, `regions_total 1 / 0 / 0`,
  `native_text_chars_total = 49`, `oom_killed false` on a counter that read `0`.
* measured — `results/g4-mixed-chart-001_20260925T204952Z*` (exit 0) against the
  recorded `…T174114Z`: `pdf_image_objects 1 / 0 / 1`,
  `page_image_png_bytes 148,581 / 244,555 / 205,088` and
  `regions_total 24 / 0 / 2` all identical, i.e. the single filtered call counts
  exactly what the ladder counted. Its total (19,082.5 ms against 31,823.1 ms)
  is the same host-load spread as the 1-page runs above, not a speed-up.

## Not covered by this record

* Preparation execution itself: no parse job, no thread lock release, no index
  write — T2f.
* **Boot wiring of the bounds.** `from_environ` has no production caller yet,
  deliberately: calling it from `fin-serve` in this ticket would fail every
  existing deployment's startup before any code reads a bound. The first
  consumer wires it — T2b at the upload door for `max_upload_bytes`, T2e/T2f
  for the page and call budgets. The spec clause "missing limits fail
  startup/config validation" is satisfied here by the validator and its tests,
  and becomes a boot failure from T2b on.
* The spec's per-run recording fields (page count, full-page and crop calls,
  image dimensions, provider usage/cost, latency, retry count) are produced by
  this ticket's measurement harness (`driver.py` metas, the three
  `g4_vision_probe*.json` artifacts); the product-side record of the same fields
  is T2e/T2f work.
* Whether a full-page scan already answers a question-specific detail, and
  therefore whether the crop trigger fires at all: the crop *cost* is measured on
  the committed chart image, the crop *trigger* is not, because nothing asks a
  question of a page yet.
* OCR cost on a genuinely scanned file at scale: the 72-page measurement had
  `pages_needing_ocr = 0`, so the per-page parse figure above never included
  OCR. No scanned-case run on this host left a committed meta, and
  **scanned-page OCR cost is therefore unmeasured here** — the largest open
  quantity in this ticket (*Leftovers* 4).

  Two time figures were observed while those attempts were running, and neither
  has a committed timer artifact behind it, so they are stated here as
  observations that motivated the abort, not as measurements: CPU Tesseract at
  the pinned configuration appeared to burn of the order of ten minutes of CPU
  on page 1 of 3 and was still running when it was killed, and a separate
  single-image run on a 1200 × 746 PNG took of the order of fifteen to twenty
  minutes of wall time at about half of one core. What *is* committed for the
  second one is its command line and exit status
  (`scripts/g4_pipeline/results/g4-image-chart-001_tesseract_20260925T174249Z/command.txt`);
  no timing was captured. With 3.9 GiB of swap in use and 817,456 major faults
  on the backing VM, even the order of magnitude cannot be attributed to the OCR
  algorithm rather than to the host.
* Whether Heron's figure boxes cover the charts a filing actually contains.
  `1 figure in 72 pages` of `2023_Q1_MSFT.pdf` against `55` table regions says
  the answer is *no* for this corpus, which is why the spec's full-page scan
  rule ("including pages where Heron found no figure box") is not optional and
  why `max_vision_calls_per_run` must scale with pages rather than with figure
  regions. Recorded as `known_gaps[0]` in the gold manifest.
* `docling` as a declared runtime dependency (T2d's decision).

## Leftovers

1. The 80-page point is extrapolated 11% beyond the largest measurement
   (~13% predicted margin at the strict reading). T2f's gold round must run
   one 80-page attachment at these four caps before the values ship as the
   product default; if it does not fit, lower `FIN_ATTACHMENT_MAX_PDF_PAGES`
   and the run budget together.
2. One vision scan per admissible page, serial, costs ≈ 4.5 min and ≈ 376K
   input tokens for a max-size attachment. Nobody has decided a per-turn
   monetary ceiling; that is an operator/product call this ticket can only
   price, not make. T2e is the first ticket whose run record carries provider
   usage per call against a total, so it is where the price becomes readable
   and where the operator's ceiling has to be applied.
3. A turn with two max-size attachments cannot fit `88` calls (2 × 80 pages),
   so it fails `attachment_vision_budget_exceeded` by design. There is no
   per-message attachment-count limit in the product, so the guidance to send
   one large filing per turn belongs to T2b's composer work.
4. **Scanned-page OCR is unquantified**, and it is the only cost in the
   pipeline that could make the 80-page cap unusable in practice. Re-measure one
   scanned page on an idle 4 GB device — with `VmHWM`, CPU-time, major faults,
   render DPI, and `--psm` recorded, and with a timer artifact committed
   alongside the command line — before T2d picks a rendering configuration from
   these numbers. The `≈ 19.9 min` end-to-end figure in the cap comparison is a
   parse-plus-full-page-scan extrapolation for a file that needed **no** OCR, so
   it does not cover an OCR-bearing file at all.
5. This record's "4 GB device" is a cgroup ceiling on a 7.7 GiB WSL2 VM that was
   itself swapping. The peaks and OOM counts are valid; the wall-clock figures
   are not comparable to a dedicated 4 GB machine, and the operator who
   authorises these four values as the shipped default should read them as
   *bounded and consistent*, not as *fast*. T2f's gold round is the run that
   owes the dedicated-device reading, because the End-of-round record claims
   measured peaks and latency for a device, not for a cgroup.
6. **The latency re-measure under a quiet host has not been run.** The pinned
   runs above share four cores with other work, so no millisecond figure here is
   a device specification.

   ```text
   Validation:
   - Not run: host load at 8.5 on 4 cores at record time; a serial re-run would
     be contaminated by the same contention that produced the 1.9× spread on
     identical input.
   - Expected command: scripts/g4_pipeline/run_measured.sh \
       data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf
     with `cat /proc/loadavg` and `grep -E "^pswpout|^pgmajfault" /proc/vmstat`
     captured immediately before and after, on an otherwise idle machine.
   - Blocking issue: no idle 4-core host available to this ticket.
   ```

   T2d owns it: `--psm`, render DPI, and the skip-OCR decision are picked from
   these figures, and picking them from contended numbers is the guess this
   ticket refuses.

7. A bare image has no measured preparation cost, because the harness only
   drives PDF inputs (see the `g4-image-chart-001.png` row above). T2d owns the
   non-PDF input path — whether an image is wrapped into a one-page document
   before Docling or sent straight to the OCR/vision step — and must measure it
   rather than reuse these PDF figures.
8. `max_vision_crop_calls_per_run = 8` is priced against a measured per-call
   cost on a crop-shaped image, but the *demand* side is still arithmetic: no
   code asks a question of a page yet, so nothing has ever wanted a crop. T2e
   should re-read this number against the first production run that records
   crops actually issued, and lower it if the observed demand is smaller.
9. **Page-scan retry headroom is undecided.** `88 = 80 + 8` is the exact
   `validate()` floor, so a run at the page cap has nothing spare: its 80 scans
   plus the 8 crops the deployment declares fill 88, and a second scan of one
   page is then the 89th call, failing the whole run with
   `attachment_vision_budget_exceeded`. That is precisely the call the spec's
   page-at-a-time rule exists to make possible — "pages submitted singly so
   resource use is bounded and failures can be retried by page" — and it is the
   one a failed first scan needs. Either `preflight_run` prices a retry term and
   `validate()`'s floor moves with it, or `max_vision_calls_per_run` stops being
   the minimum and rises above `pages + crops`; both touch the enforcement seam
   and at least one deployed bound, so neither is a free edit. T2e owns the
   decision: it is the first ticket that issues a retry call, and it must price
   retries against the run budget before any of the four values is authorised as
   the shipped default.
