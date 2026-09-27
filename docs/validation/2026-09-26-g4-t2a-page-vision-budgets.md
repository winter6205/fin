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

Two of the four rows are not measured values. The crop headroom is arithmetic on
the gold set — 2 chart-bearing pages × 4 labelled values — and no caller has
ever wanted a crop (*Leftovers* 8, T2e); the run total is `80 + 8`, which is
`validate()`'s exact floor, so a deployment running at the page cap has **no**
spare call for a retried full-page scan and `88` must rise before T2e can retry
one (*Leftovers* 9, T2e). Both facts belong to whoever lowers these numbers, so
they are stated in the `.env.example` block as well as here.

### Decision follow-up — 2026-09-27 (issue)

This follow-up supersedes the open T2e decisions below; the original `88` value
and calculations remain the historical T2a record.

- **Observation reuse:** persist structured coverage details and image scope.
  During the current run, the host reads the stored observation and decides
  whether known coverage answers the requested detail; unknown or uncovered
  detail triggers another inspection. T2e implements this host-side predicate.
- **Retry budget:** allow one retryable full-page transport retry per run. At
  80 pages plus 8 crop calls, set the policy allowance to `89`; keep the crop
  sub-cap at `8`. T2e counts retries in the run total and enforces the
  one-retry limit. `89` is a
  policy allowance, not measured demand or a shipped setting; it supersedes `88`
  as the deployment target.
- **Vision configuration:** reuse `FIN_MODEL`, `FIN_BASE_URL`, and
  `FIN_API_KEY` through the vision-specific port.
- **Monetary ceiling:** deferred. T2e records provider usage and any reported
  cost, marking cost unavailable when omitted, without enforcing a per-turn
  monetary limit.

**These values are provisional, not a shipped default.** One of the three gold
cases has no committed pipeline measurement here — the bare PNG, which the
harness's PDF-only input path refuses (*Gold cases under the pinned
pipeline*) — and the scanned-page OCR quantity that could invalidate
`max_pdf_pages = 80` is now measured: ~22 min of CPU OCR per page on this host.
That figure does not move the cap. It binds every corpus-covering cap the same
way, because no page limit that still admits this corpus makes a scanned filing
fast, so the lever is T2d's OCR configuration rather than a number in this
table. What this ticket delivers is a validated, enforced,
measured-where-measurable starting configuration plus the list of what must be
measured before an operator authorises it as the product default
(*Leftovers* 1, 4, 6, 7, 9).

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

The spec asks for peak *host/GPU* memory, and the GPU half of that is **not
applicable to this pin** rather than unmeasured: the image installs
`torch 2.9.1+cpu` from the CPU wheel index, `onnxruntime` is absent so
Docling's ONNX layout override cannot load, and every committed record carries
`pipeline_identity.device = cpu`. There is no device memory in this pipeline to
peak. The one GPU that could appear in a G4 preparation run is the vision
provider's, and it is inside the provider, outside fin's process, reported only
as the token usage in *Basis: the configured vision provider*.

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

Model load, not page count, dominates the floor: the seven 1-page runs that
completed Docling peaked between 1,234,628,608 and 1,309,020,160 B
(`2023_FY_GOOG_…T170956Z`, `…T184410Z`, `…T195408Z`, `…T204930Z`, `…T220516Z`,
`…T231119Z`, `…T000155Z`), i.e. within 6.0% of each other and 1.15–1.22 GiB
before the per-page term is worth anything.

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

Four unrelated agent processes (`qoderclicn`, 0.4–1.0 GiB each) and the
iknow-memory Elasticsearch/Neo4j containers share those four cores, and each
pipeline container was given `--cpus=4`, i.e. one whole host, so two concurrent
runs contend 1:1. The same 1-page input measured 15,013.3 ms, 29,171.3 ms,
16,407.2 ms, and 10,402.8 ms across four of those seven runs (`…T170956Z`,
`…T184410Z`, `…T195408Z`, `…T204930Z`) — a 2.8× spread on
identical input, and a 1.8× spread on the warm per-page conversion
inside those runs (3,571.1 / 6,074.1 / 5,942.4 / 3,374.5 ms). Contention, not
variance in the input; `driver.py` records no load figure for any of them.

The captured load around a run does not order its total either, which is why no
millisecond figure here is promoted to a rate: `…T231119Z` measured 17,540.6 ms
at a 1-minute load of 2.15 before and 3.41 after, while `…T220516Z` measured
9,891.4 ms at a *higher* captured 2.78, and the `/3` re-run measured 25,747.5 ms
at 3.61 before and 5.73 after. A 1-minute average is not the quantity that
determines how four cores are shared over a 26-second run.

### The quiet-host re-measure, and how quiet this host gets

*Leftovers* 6's owed evidence was run after the scanned case finished, serially
and with the load captured around it
(`scripts/g4_pipeline/results/serial_remeasure_20260925T220516Z.txt`, one
`[run] summary:` line per run, so the latencies below are the tracked metas' own):

| run | 1-min load before / after | total ms | Docling whole doc | warm per-page ms | `VmHWM` |
| --- | --- | --- | --- | --- | --- |
| `2023_FY_GOOG.pdf` `…T220516Z` | 2.78 / 3.01 | 9,891.4 | 8,077.2 | 1,679.9 | 1,304,858,624 |
| `g4-mixed-chart-001.pdf` `…T220537Z` | 3.01 / 3.25 | 20,099.9 | 12,005.2 | 2,568.1 | 1,450,414,080 |

What it buys and what it does not:

* The 1-page total, 9,891.4 ms, is the fastest of the five runs of that input and
  sits just under the quietest contended one (10,402.8 ms), so the contended set
  was not inflated much at the *whole-run* level — model load is not what the
  cores were fighting over.
* The **warm per-page** term is where contention showed: 1,679.9 ms at load 2.78
  against a 3,374.5 ms best and 6,074.1 ms worst under load 15.9, i.e. the
  contended runs overstated the steady-state page by roughly 2–3.6×. Every
  per-page millisecond figure in this record is therefore an **upper bound**,
  including the `11,526.9 ms/page` that produces the `≈ 19.9 min` end-to-end
  estimate, and the estimate is conservative by a factor of about two rather
  than optimistic.
* Content metrics did not move: the mixed re-run again returned `24 / 0 / 2`
  regions and `1,908` native characters, identical to both earlier runs of that
  file.
* This is **not** the dedicated 4 GB device reading. The captured window shows a
  1-minute load of 2.78 to 3.25 on 4 cores with **4,096 MiB of swap already in
  use** and 936,751 major faults since boot on the backing VM — the quietest this
  machine gets while a session is open, not an idle machine and not a 4 GB
  device. *Leftovers* 5 and 6 keep that distinction.

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
* `driver.py` records no load or major-fault figure per run, so the runs above
  cannot be corrected after the fact. The serial re-measure with load captured
  around it **has now been run** (*The quiet-host re-measure, and how quiet this
  host gets*): it puts the contended per-page figures at roughly 2–3.6× the
  quiet-host rate, which is why they are used only to order the two page-cap
  options and never as a device specification. What remains open is the part a
  shared host cannot settle — *Leftovers* 6.

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

The baseline is the largest peak among the four runs made under load
(`…T170956Z`). Substituting the quiet-host run's marginally larger
`1,304,858,624` moves the plus-swap total by ~412 KB against a 563 MB margin —
0.01% of the ceiling — so which 1-page run is subtracted is not what the decision
rests on. The same holds in the other direction and for a larger reason: the
largest 1-page peak now committed, `1,309,020,160` (`…T000155Z`), raises the
baseline by 7,872,512 B but shrinks the per-page term derived from the same
subtraction, so the 80-page total it produces is 874,679 B *lower*, not higher. A
baseline is subtracted twice in this arithmetic, once as a floor and once inside
the slope, which is why the band above moves the margin by at most 0.02% of the
ceiling.

So `max_pdf_pages = 80` fits a 4 GiB device with ~13% margin on the strict(plus-swap) reading, **but the 80-page point itself was never run** — it is
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
exclude OCR, because the file that reached 72 pages needed none; the scanned
case, which does, cost `≈ 21.9 min` of render-plus-OCR per page at the mean of
its two runs (*Gold cases under the pinned pipeline*), so an 80-page scanned
filing is `80 × 21.9 min ≈ 29 h` and a 10-page one is already `≈ 3.6 h`. A page
cap is therefore not the lever for OCR: no value in the corpus-covering range
makes a scanned filing feasible, and the fix belongs to T2d's render DPI,
`--psm`, language set, and skip-OCR-when-native-text choices. The cap was not
set from this quantity in any case — it rests on the two things that do bound it,
the enforced 4 GiB resident ceiling and the real page and byte distribution, both
measured.

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
`page_image_png_sha256 = 72b000ce49688014…`. Every re-run since carries that same
digest — `…T220537Z`, `…T231154Z` and the `/3` run `…T000242Z` — so the pin holds
across each harness revision on this branch rather than resting on one rendering.
The probe's subject and that hash name the same bytes because renderings of the
same fixture at the same DPI agreed, not because one file was hashed twice.

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

Tests: `tests/host/test_attachment_budgets.py`, 157 cases. The four
input-class tables run against every one of the four keys rather than against
one representative, so a rule enforced on three of them cannot pass. The
deployed-bound cases bind `.env.example` and the gold manifest to the measured
data files behind their numbers, so the numbers cannot drift from their evidence
unnoticed:

* `test_deployed_bounds_are_literals_with_no_shell_fallback`
* `test_corpus_distribution_records_describe_the_committed_files`
* `test_gold_manifest_describes_the_committed_fixtures`
* `test_ink_screen_artifact_is_what_the_gold_manifest_cites`
* `test_deployed_bounds_admit_every_file_in_the_measured_distribution`
* `test_deployed_bounds_prepare_every_gold_case_in_one_run`

The two `…describe_the_committed_…` cases hash the files behind the recorded
sizes, because the caps were chosen from recorded byte counts and page counts,
and a page count is only a statement about the bytes it was read out of.

The two harness seams the review round touched got their own tests, since both
are contracts the record quotes numbers through:
`tests/host/test_g4_pipeline_gap_contract.py` (11 cases) drives the driver with
doubles and asserts that an unanswered page rule, a failed OCR pass, a
region-less document and an absent model cache each produce a `null` with a named
error rather than a `false`, a `0` or a crash, and that a Docling page size is
read from the document's page *values* rather than guessed;
`tests/host/test_g4_vision_probe_classification.py` (19 cases) pins the probe's
error classes, including that an unrelated 400 is never classified as an image
refusal, and pins which prompt kind may carry which answer.

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
| 22 | `scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-scanned-001.pdf` | 0 | first scanned run to complete under the ceiling (`…T185551Z`), 64.6 min; produced by the harness one revision before the gap-semantics repair, which is why it is kept as the pair's second witness rather than as the primary record |
| 23 | same command again, detached so the 10-minute tool timeout could not kill it mid-run | 0 | `results/g4-scanned-001_20260925T205348Z*`, 69.7 min; every content field identical to run 22, and its meta carries `json_lines` and `state_error`, which run 22's predates |
| 24 | `scripts/g4_pipeline/run_measured.sh data/corpus/pdf/2023_FY_GOOG.pdf` then `… data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf`, serially, each with `/proc/loadavg`, `free -m` and `/proc/vmstat` captured around it | 0 / 0 | `results/serial_remeasure_20260925T220516Z.txt` plus the two run dirs it names; the quiet-host figures in *The quiet-host re-measure* |
| 25 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py` then `uv run --no-sync python -m py_compile scripts/g4_pipeline/driver.py scripts/g4_vision_probe.py scripts/g4_upload_distribution.py && bash -n scripts/g4_pipeline/run_measured.sh scripts/g4_pipeline/build.sh`, after committing the scanned and serial artifacts | 0 / 0 | 109 passed; the round's diff is records and artifacts only, so this is the unchanged-module guard rather than new evidence |
| 26 | python one-liner: flatten each scanned run's first `measured.jsonl` tree to leaf paths, intersect, drop paths matching `memory_checkpoints\|latency\|t_monotonic\|vmhwm\|vmrss\|vmpeak\|vmss\|vmswap\|cgroup\|oom\|peak_rss\|swap`, compare the rest | 0 | 436 and 433 leaves, 312 content leaves, 0 differing — the measurement behind *measured twice* above |
| 27 | `uv run --no-sync pytest -q tests/host/test_g4_pipeline_gap_contract.py tests/host/test_g4_vision_probe_classification.py` with both harness files swapped for their `HEAD` versions (the pre-repair `driver.py` and `g4_vision_probe.py`, restored and hash-checked after) | 1 | **RED**: 8 failed, 12 passed — the six gap-contract cases above (`AttributeError: module 'g4_vision_probe' has no attribute 'IMAGE_INPUT_REJECTED'` for the two probe cases) fail on `false` and `0` where the repair records a gap; the control case, which pins that the rule *is* asked with `image_count=0`, passes before and after, which is what makes the other five about the gap rather than about the call |
| 28 | same two files against the repaired harness | 0 | 20 passed |
| 29 | `uv run --no-sync pytest -q tests/host/test_attachment_budgets.py` | 0 | 156 passed, after the F-matrix was spread over all four keys and the two recorded-bytes guards were added |
| 30 | `uv run --no-sync pytest -q tests/host` | 0 | 1927 passed, 60 skipped, 227 deselected in 97.13 s; the skips are the pre-existing `FIN_CHECKPOINT_URL` and `web/dist` gates |
| 31 | `uv run --no-sync python -m py_compile scripts/g4_pipeline/driver.py scripts/g4_vision_probe.py scripts/g4_upload_distribution.py src/fin/host/attachment_budgets.py tests/host/test_attachment_budgets.py tests/host/test_g4_pipeline_gap_contract.py tests/host/test_g4_vision_probe_classification.py && bash -n scripts/g4_pipeline/run_measured.sh scripts/g4_pipeline/build.sh` | 0 | the repo's typecheck seam, after this round |
| 32 | AST walk over the four changed modules counting control-flow depth and function length, with ESLint's `max-depth` statement set (`If`, `For`, `While` — not `Try`) | 0 | deepest nesting `3` in `driver.py:memory_snapshot()`, `2` in `attachment_budgets.py:preflight_run()`; no function in `attachment_budgets.py`, the module under test, is over 4 parameters. The S5 hard gate is nesting ≤ 4. *Leftovers* 15 is the whole-branch re-measurement of this row, and it is the one that carries the parameter counts for the scripts |
| 33 | `scripts/g4_pipeline/run_measured.sh` on `data/corpus/pdf/2023_FY_GOOG.pdf`, then on `data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf` | 0 / 0 | `results/2023_FY_GOOG_20260925T231119Z*` and `results/g4-mixed-chart-001_20260925T231154Z*`, the two `/2` runs above, under the same `--memory=4g --memory-swap=4g --cpus=4` ceiling |
| 34 | `scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png` | 1 | the exit-code contract: the rejection meta is written as before and the script now exits non-zero instead of 0 |
| 35 | `docker run --rm --entrypoint /bin/cat fin-g4-pipeline:t2a /opt/g4/tesseract_version.txt` (the same command `build.sh` now runs) | 0 | `tesseract 5.5.0`, `leptonica-1.84.1` → `results/image_tesseract_version.txt` |
| 36 | `pdfimages -list` over all 13 `data/corpus/pdf/*.pdf`, and a re-render of all 629 pages at 50 dpi screened for fully-dark 5×5 and 9×9 blocks with numpy | 0 | one embedded image in each of the 4 AAPL filings and none in the other 9; 4 pages of 629 with a solid block, all four an AAPL cover page — the check that corrected the filing count in the gold manifest's `known_gaps[0]` |
| 37 | `uv run --no-sync python -m py_compile scripts/g4_ink_screen.py && uv run --no-sync python scripts/g4_ink_screen.py`, twice | 0 / 0 | `data/corpus/g4_ink_screen.json`: 629 pages of 13 filings rendered at 50 dpi, 627 with material ink, 4 with any solid block, max 0.000398 (5×5) and 0.000116 (9×9), all four the AAPL cover pages. Both runs produced the same summary, so the screen is reproducible rather than a one-off reading |
| 38 | `docker run --rm --entrypoint python fin-g4-pipeline:t2a -c "…print(DoclingDocument.model_fields['pages'].annotation, PageItem.model_fields, Size.model_fields)"` | 0 | `dict[int, PageItem]`, `['size', 'image', 'page_no']`, `['width', 'height']` — the pinned shape that made the old `for p in doc.pages: p.number` read nothing, and the field names the repaired reader uses |
| 39 | `scripts/g4_pipeline/run_measured.sh` on `2023_FY_GOOG.pdf`, then on `g4-mixed-chart-001.pdf`, serially, each with `/proc/loadavg` captured around it | 0 / 0 | `results/2023_FY_GOOG_20260926T000155Z*` and `results/g4-mixed-chart-001_20260926T000242Z*`, the two `/3` runs; load 3.61 at entry, 5.73 and 6.37 at the two exits |
| 40 | python one-liner: flatten each `/3` tree and its `/2` predecessor to leaf paths, drop timings and memory counters, compare the rest | 0 | 363 → 365 and 425 → 429 leaves; 246 and 300 content leaves; **0 differing** in both. The arriving keys are `docling_page_size_pt[i]` and `totals.wall_clock_ms` |
| 41 | `scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-image-chart-001.png`, exit status observed directly rather than through a wrapper | 1 | `results/g4-image-chart-001.png_20260926T001448Z.meta.json`: `json_lines = 0`, `container_exit_code = 1`, `docker_run_exit_code = 1`, every measurement `null` — the exit-code contract, re-observed after this round's edits to the same script |
| 42 | `uv run --no-sync pytest -q tests/host/test_g4_pipeline_gap_contract.py tests/host/test_g4_vision_probe_classification.py tests/host/test_attachment_budgets.py` | 0 | 187 passed (11 + 19 + 157) |
| 43 | `uv run --no-sync pytest -q tests/host` | 0 | 1938 passed, 60 skipped, 227 deselected in 108.46 s; the skips are the pre-existing `FIN_CHECKPOINT_URL` and `web/dist` gates |
| 44 | `uv run --no-sync python -m py_compile scripts/g4_pipeline/driver.py scripts/g4_vision_probe.py scripts/g4_upload_distribution.py scripts/g4_ink_screen.py src/fin/host/attachment_budgets.py tests/host/test_attachment_budgets.py tests/host/test_g4_pipeline_gap_contract.py tests/host/test_g4_vision_probe_classification.py && bash -n scripts/g4_pipeline/run_measured.sh scripts/g4_pipeline/build.sh` | 0 | the repo's typecheck seam, after this round |
| 45 | `python3 scripts/g4_ink_screen.py` again after decomposing `aggregate()` for the CC gate, then a `json.tool` diff of the old and new artifacts, then command 42's three files again | 0 / 0 / 0 | the only line that differs across the whole artifact is `measured_at_utc`; `summary` and all 629 `pages` entries compare equal, and 187 tests still pass. The decomposition is therefore shown to be complexity-only, which is the claim *Leftovers* 15 makes about it |

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
| `g4-scanned-001.pdf` | 3 | 677,716 | 1,337,163,776 | 1,568,075,776 | 0 / 0 | 4,179,289.7 | 0 / 4 / 0 | measured, twice |
| `g4-image-chart-001.png` | 1 | 40,419 | — | — | 1 (not OOM) | — | — | **rejected by the harness** |

* `g4-mixed-chart-001.pdf` is the load-bearing gold case for the crop bound:
  Heron returned `figure = 2` over its 3 pages, and `pages_needing_ocr = 0`. Its
  chart-page count is what `max_vision_crop_calls_per_run = 8` is priced against.

#### Expected versus actual on the mixed case

Expectations are the committed `per_page` entries of
`data/corpus/g4_attachment_gold/manifest.json`; actuals are the page records of
`scripts/g4_pipeline/results/g4-mixed-chart-001_20260926T000242Z/g4-mixed-chart-001.measured.jsonl`,
the current-schema run, whose per-page `region_counts`,
`native_text_char_count` and `pdf_image_objects` are identical to those of the
first `/1` record `…T174114Z`.

The two character columns are **not the same extraction**, which is why the ±11,
−70 and ±3 differences below are not misses: the expected count is
`native_text_chars`, defined by the manifest's `field_definitions` as the length
of `pdftotext -f N -l N <fixture> -` output stripped of leading and trailing
whitespace (poppler 26.01.0), while the actual count is the driver's
`native_text_char_count`, the length of pypdfium2 5.13.0's `get_text_range()` for
the same page. Two independent text extractors agree on all three pages to within
6.0% (449 → 460, 1,181 → 1,111, 334 → 337); neither number is a claim about what
*should* be extracted, so the comparison that matters is presence, not count.

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
* `g4-scanned-001.pdf` is measured twice, and the two runs disagree only about
  time and about where in the run the peak landed.
  `…T185551Z` was produced by the harness before the gap-semantics repair
  and `…T205348Z` after it. Every content metric is identical: page PNG sha256
  `4cea21df…`, `1bd502f7…`, `a5d35ac2…`, `ocr_text_char_count`
  5,570 / 3,842 / 5,978 (15,390 for the file), Heron regions `0 / 4 / 0`, and
  `pages_needing_ocr = 3`. The render-plus-OCR pass cost 3,790,099.3 ms and
  4,089,528.9 ms, i.e. `≈ 21.9 min` per page at the mean of the two. Neither run
  came near the ceiling — `VmHWM` 1,444,216,832 and 1,568,075,776 with `VmSwap`
  0 and `oom_kill_events_in_cgroup = 0` — so at this size OCR is a time cost, not
  a memory cost. `VmHWM` is the driver process's own high-water mark and does not
  cover the Tesseract child that does the OCR work; `cgroup_peak_bytes` counts
  the whole tree, which is why both are quoted and why the margin arithmetic in
  *Extrapolation* uses `VmHWM + VmSwap` only as the strictest reading available
  to it. The two runs are committed as a pair so the claim can be checked
  field by field rather than read from this summary: flattened, their
  `measured.jsonl` trees have 436 and 433 leaf paths, and 312 of those are
  content once timings and memory counters are excluded — all 312 identical.
  What does differ is harness-side: the older tree carries
  `pages[].native_text_codepoints`, a field the driver no longer emits, and its
  `oom_killed_source` still reads
  `docker inspect .State.OOMKilled (cgroup counter kept alongside)` where the
  gap-semantics repair replaced that guess with
  `cgroup memory.events oom_kill counter`. The older meta likewise carries 33
  keys instead of 35, `json_lines` and `state_error` being harness-side and
  postdating it.
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

#### Expected versus actual on the scanned case

Expectations are the same committed `per_page` entries; actuals are the page
records of both `scripts/g4_pipeline/results/g4-scanned-001_20260925T*` runs,
which agree exactly on every field below, so one row describes two runs.

| page | expected | actual | verdict |
| --- | --- | --- | --- |
| 1 | no native text, OCR expected, text + table, quote `CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)` | native 0, `ocr_required_by_repo_rule` true, `ocr_used` true (trigger `repo_rule`), 5,570 OCR chars whose committed sample opens with the quote verbatim, text **0** / table 1 | OCR as expected; **text-region miss** |
| 2 | no native text, OCR expected, **text only**, quote `Notes to Condensed Consolidated Financial Statements (Unaudited)` | native 0, `ocr_used` true, 3,842 chars with the quote verbatim in the sample, text **0** / table **1** | OCR as expected; **two-way region miss** — a prose page returned a table and no text |
| 3 | no native text, OCR expected, text + table, quote `Total net sales $ 81,797 $ 82,959 $ [private source value removed] $ 304,182` | native 0, `ocr_used` true, 5,978 chars, text **0** / table 2 | table as expected, text missing; **quote not checkable here** — the record keeps a 160-char sample and the row sits below it |

Three things this establishes, none of them a budget question:

* The repo's OCR trigger behaves correctly on a genuinely scanned page: every
  page has `native_text_char_count = 0`, so `detect_ocr_required` fires and OCR
  runs. That is the opposite of the mixed case's page 3, where a real text layer
  hid pixel text — the two together are why the rule needs a region-level
  counterpart (T2d) rather than a different threshold.
* Heron's `text`-class recall on a rasterised page is **0 of 3** at this pin, and
  it declares a table on a page the manifest says has none. The readable payload
  of a scanned page came from OCR alone, so T2d cannot use region kinds as the
  signal that a scanned page has been read.
* The expensive call is named, not guessed: `src/fin/corpus/ocr.py:126` passes
  the rendered PNG to `image_to_string(source, lang="chi_sim+eng")` with **no
  `config` argument**, so Tesseract's default page-segmentation mode 3 runs on a
  1700 × 2200, 200 dpi bitmap. That parameterisation is what the ~22 min/page
  figure prices. The manifest's expected quotes were pinned at 150 dpi with
  `--psm 6`, which is a different call from the one the shipped engine makes — so
  the ~22 min/page figure and the reproduced quotes are both true of the pipeline
  as configured, and neither is a bound on what T2d will choose to run.

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

### Review round: the gap rule applied to the harness's own three remaining gaps

The rule "a reading that never happened is a gap, never a value" was already
written into the driver, and the review found three places where a specific
reading could still come out as a number:

* A page whose image-object scan failed recorded `ocr_required_by_repo_rule =
  false`, which reads as "the rule answered: no OCR needed" when the rule was
  never asked. It is now tri-state — `true`, `false`, or `null` with the scan's
  named error beside it — and `totals.harness_failures` names the page, because a
  coverage count built on a page the rule could not see would silently under-price
  the OCR term this record quotes for the scanned case.
* A failed OCR pass left `ocr_text_char_count` at its initialised `0`, so one
  page's failure read as an empty page. It is now `null` with `ocr_error_code`
  and `ocr_error` carrying the reason, and the three totals that sum page values
  (`pages_needing_ocr`, `native_text_chars_total`, `ocr_text_chars_total`) are
  `null` when any page contributing to them is `null`: a total of a set with a
  hole in it is not a total.
* Counting Heron regions reached through `doc.texts / doc.tables /
  doc.pictures` directly, so a docling version that moved a section off the
  `Document` object would raise inside the counting code and take the whole
  conversion record with it. The three reads are now the one place the pass can
  fail, and a failure resets `status`, `page_count`, the region counts and the
  per-page sizes to `null` rather than emitting a partial count as a zero. It is
  the same shape of fix the model-cache identity needed: an absent cache reported
  `cache_total_bytes = 0` for a directory that was never walked.

Because those three change what a field *means*, the record's own schema string
went from `fin.g4.t2a.pipeline_measurement/1` to `…/2`. The committed `/1`
artifacts are not restated: every number this record quotes from them is a field
whose value the repaired driver would produce identically, because in each of
those runs the image-object scan succeeded, OCR either ran or was not triggered,
and the region pass did not fail. The two re-runs below are the check on that
claim rather than an argument for it.

Two runs and one rejection under the repaired harness, at 1-minute load 2.15
before, 2.84 between and 3.41 after — the floor band this host sits in while a
session is open, the same band the serial re-measure was captured in.

* `results/2023_FY_GOOG_20260925T231119Z*` (exit 0) against the recorded
  `…T204930Z`: 363 leaf paths in each, none missing and none extra, and exactly
  one content difference between the two trees — the `schema` string itself.
  Every other difference is a timing or a memory counter.
* `results/g4-mixed-chart-001_20260925T231154Z*` (exit 0) against the recorded
  `…T204952Z`: 425 leaf paths in each, again differing only in `schema`.
  `regions_total 24 / 0 / 2`, `pdf_image_objects 1 / 0 / 1`,
  `native_text_chars_total = 1,908` and `pages_needing_ocr = 0` carry the same
  values the `/1` record carries; total latency is `19,769.1 ms` against
  `19,082.5 ms`, i.e. this host's usual spread.
* `results/g4-image-chart-001.png_20260925T231351Z.meta.json` — the same non-PDF
  rejection as `…T195253Z`, now with the exit-code contract attached to it: the
  meta is still written with `json_lines = 0` and `null` for every unread field,
  and the script exits 1 rather than 0.

The last two findings of the round were about the harness's outward face rather
than its fields. `run_measured.sh` wrote a measured record and exited 0 even when
the container had failed on the way out, so a scripted caller could not tell a
completed measurement from a captured partial one without opening the meta; it now
names `docker_run_exit`, `container_exit` and any `state_error` on stderr and
exits 1 while still writing the record, because under this harness a failed
measurement is a result. And Tesseract, an apt package that appears nowhere in
`pip freeze`, had no committed artifact for the `5.5.0` this record and
`PIN_MANIFEST.md` both cite: `build.sh` now copies the image's own
`/opt/g4/tesseract_version.txt` to
`scripts/g4_pipeline/results/image_tesseract_version.txt`, which reads
`tesseract 5.5.0` over `leptonica-1.84.1`.

### Review round (2): a size lane that answered nothing

The second dual-axis pass found four defects in the two harness scripts. None of
them moves a bound, and the first of them is the reason this record had been
citing one page-size extractor while a second, independent one sat empty:

* `docling_page_sizes()` iterated `doc.pages` and read `.number` off each
  element. `DoclingDocument.pages` is `dict[int, PageItem]`, so iterating the
  mapping yields `int` page numbers, which have neither `number` nor `size`;
  every attribute read fell back to `None` and the function returned `{}` for a
  document that had converted successfully. Both `/2` runs show the result:
  `totals.docling_page_count` answers `1` and `3` while every page's
  `docling_page_size_pt` is `null`. The repaired function takes the mapping's
  *values* and keys them by `page_no`, and reads `size.width` / `size.height` —
  which is what the pinned library actually exposes, printed inside
  `fin-g4-pipeline:t2a` rather than assumed: `DoclingDocument`'s `pages` field is
  annotated `dict[int, PageItem]`, `PageItem.model_fields` is
  `['size', 'image', 'page_no']` and `Size.model_fields` is
  `['width', 'height']`. A page whose `size` is genuinely absent is left
  unanswered and the gap is named in `totals.harness_failures` with how many
  pages it covers, because "Docling found no size" and "this page's size was not
  read" are different findings.
* `verdicts()` gated the chart answers on "any kind that is not presence", so the
  text-only control call — the one sent with no image, whose answer says nothing
  about charts — could be recorded as `chart_meaning_answer`. The four prompt
  kinds are now named constants and the chart-bearing pair is one tuple,
  `_CHART_ANSWER_KINDS`, so the control cannot be mistaken for a witness.
* `classify_error()` was one function of 17 branches, and its output is control
  flow: one class aborts, one authorises the single retry, one sets
  `supports_image_input`. It is now an ordered table of frozen `_ErrorRule` rows
  (status set, exception-name cues, message cues, modality requirement),
  first-match-wins, so a new class is a new row. The 11 pinned inputs classify
  identically before and after, which is the check that this was a decomposition
  rather than a change of meaning.
* `memory_snapshot()` read four lanes in one body. It is now three helpers over
  one shared `read_errors` list, bound in the original order, because the order
  those messages appear in is part of a committed record's leaves.

Two provenance fixes came with them. `totals.wall_clock_ms` records the span
between the first and last memory snapshot, so the three-phase
`total_latency_ms` sum cannot be read as the cost of a run: the `/3` GOOG run
sums 25,747.5 ms inside a 38,299.0 ms span, and converter construction, model
load and the file open are what the 12,551.5 ms between them is.
`run_measured.sh` now writes the file's own name into `meta.pdf`
(`file_info["name"]`, falling back to the basename) instead of the caller's
absolute host path, which is a fact about this machine rather than about the run,
and it carries four more mirrored keys (`oom_killed_source`,
`slice_pass_enabled`, `harness_failures`, `wall_clock_ms`) through the existing
`driver_keys` assertion, so a renamed total fails the run instead of summarising
as `null`.

Because a field that used to answer nothing now answers, and one field arrived,
the record's schema went from `…/2` to `…/3`. Both changes were re-measured
against the `/2` records, and the claim that the bump moved no measurement is a
leaf-by-leaf comparison rather than an argument:

| run | leaves `/2` → `/3` | content leaves | differing |
| --- | --- | --- | --- |
| `2023_FY_GOOG…T000155Z` | 363 → 365 | 246 | **0** |
| `g4-mixed-chart-001…T000242Z` | 425 → 429 | 300 | **0** |

The only key differences are the expected ones: `pages[i].docling_page_size_pt`
leaves `null` (one leaf) for two leaves per page, and `totals.wall_clock_ms`
arrives.

What the revived lane answers is worth stating, because it is the corroboration
this record's page-cap arithmetic has been missing: on every page of both runs
Docling's own `docling_page_size_pt` is `[612.0, 792.0]`, exactly the `size_pt`
pypdfium2 reported for the same pages, and the rendered raster is 1700 × 2200 px
on both. Two libraries that share no code agree on the medium the DPI arithmetic
rests on.

Both runs were made at a host the previous sections already describe as
contended — 1-minute load 3.61 at the start, 5.73 and 6.37 at the two exits — so
they are cited here for the **size lane and the key tree**, and their millisecond
figures are added to the population in *Measurement conditions*, not used to
order anything. The rejection case was re-run in the same batch and the script's
own exit status observed directly: `json_lines = 0`, `container_exit_code = 1`,
and `run_measured.sh` exits 1 (`…T001448Z`).

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
* OCR cost for a **corpus-sized** scanned filing. What is measured is 3 pages,
  twice, agreeing to the character; the `≈ 29 h` for an 80-page scanned filing in
  *Extrapolation to the 80-page cap* is that per-page figure multiplied out, not
  a run that happened. No scanned document of more than 3 pages was run here, so
  the OCR term is extrapolated exactly as the 80-page memory point is
  (*Leftovers* 1 and 4), with the same shape of risk: linear in pages, no
  super-linear term observed anywhere in the four measured files, one fixture.

  Two time figures were observed while earlier attempts were being killed, and
  they are now replaced by committed runs rather than withdrawn: CPU Tesseract at
  the pinned configuration appeared to burn of the order of ten minutes of CPU on
  page 1 of 3 and was still running when it was killed, which the measured
  render-plus-OCR pass confirms and sharpens — `…T185551Z` and `…T205348Z` cost
  3,790,099.3 ms and 4,089,528.9 ms over 3 pages, i.e. 21.1 and 22.7 min per page.
  A separate single-image run on a 1200 × 746 PNG is committed with more than its
  command line and exit status: under
  `scripts/g4_pipeline/results/g4-image-chart-001_tesseract_20260925T174249Z/`,
  `command.txt` ends with `TESSERACT_EXIT=0` and `ocr.txt` carries what that run
  answered — all four plotted values (`117,154 / 94,836 / 82,959 / 81,797`), both
  sets of period labels and the source line. It carries no timing, and it is a
  bare-image OCR path, which the PDF-only harness does not drive at all
  (*Leftovers* 7), so the two scanned-page runs do not speak for the wall time and
  that stays an observation, labelled as one.

  What is not an observation is that this answer exists, because it is the only
  committed witness for the *other* page-segmentation mode: the run passed
  `--psm 6`, the mode the shipped engine does not use
  (`src/fin/corpus/ocr.py:126` passes no `config`, so Tesseract's default mode 3
  runs), and it read a filing chart completely from a crop-resolution raster in
  one call. That is the shape of the choice *Leftovers* 4 owes T2d, and it is
  evidence for the cheap side of it.
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
4. **Scanned-page OCR is now quantified, but the configuration it quantifies is
   one nobody chose.** Two runs of the 3-page scanned fixture agree to the
   character and cost `≈ 21.9 min` of render-plus-OCR per page, which prices an
   80-page scanned filing at `≈ 29 h` and a 10-page one at `≈ 3.6 h`. That number
   is a measurement of `lang="chi_sim+eng"` passed with **no `--psm` argument**
   (Tesseract's own default, mode 3) over a 200 dpi bitmap: the call
   `src/fin/corpus/ocr.py:126` makes today, not a configuration T2d picked. What
   T2d owes, before any of the four bounds is authorised as a shipped default:
   re-measure one scanned page at a deliberate render DPI, `--psm`, and language
   set, with per-process CPU time and major faults recorded (the harness reads
   the driver's own `VmHWM`, which does not cover the Tesseract child), and then
   decide whether an OCR-bearing filing is bounded by the 80 pages this corpus
   justifies, by a smaller page cap of its own, or by a budget error raised
   before the first page is rendered. The `≈ 19.9 min` end-to-end figure in the
   cap comparison is a parse-plus-full-page-scan extrapolation for a file that
   needed **no** OCR, and is explicitly not the scanned-case figure.
5. This record's "4 GB device" is a cgroup ceiling on a 7.7 GiB WSL2 VM that was
   itself swapping. The peaks and OOM counts are valid; the wall-clock figures
   are not comparable to a dedicated 4 GB machine, and the operator who
   authorises these four values as the shipped default should read them as
   *bounded and consistent*, not as *fast*. T2f's gold round is the run that
   owes the dedicated-device reading, because the End-of-round record claims
   measured peaks and latency for a device, not for a cgroup.
6. **The quiet-host serial re-measure has been run; a quiet *machine* has not.**
   Both re-measure runs (`…T220516Z`, `…T220537Z`) executed alone with the
   1-minute load captured before, between and after in
   `scripts/g4_pipeline/results/serial_remeasure_20260925T220516Z.txt`, at load
   2.78–3.25 — the floor of this host while a session is open, not zero. What
   that settles and what it does not:

   ```text
   Validation:
   - Run: two serial re-measures at captured load 2.78 / 3.01 / 3.25, both exit 0,
     both recorded in the file above and in their own metas.
   - Not run: the same inputs on a machine with nothing else on it, or on a real
     4 GB device. 4,096 MiB of swap was already in use across the window, so the
     host was still memory-bound.
   - Blocking issue: no second host and no dedicated 4 GB device available to
     this ticket.
   ```

   T2d still owns the pick: `--psm`, render DPI, language set and the skip-OCR
   decision are chosen from these figures, and the quiet-host warm per-page rate
   (`1,679.9 ms`, against `3,374.5–6,074.1 ms` under load 15.9) is now the figure
   to choose against. The device-level reading stays with T2f's gold round
   (*Leftovers* 5).

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
10. **The probe's own spend rule is untested.** `scripts/g4_vision_probe.py`
    caps a run at `MAX_REAL_CALLS = 8` and retries once, on a transport failure
    only. Both are control flow over paid calls, and the 19 classification cases
    pin *which* class a failure receives, not what the caller then does with it:
    nothing asserts that an 9th call is refused or that
    `auth_or_permission` aborts without retrying. T2e owns it, because T2e
    re-runs the probe for the first time in a product path and can drive both
    rules against a fake client without spending a call.
11. **The structured answer shares a field with the prose answer.**
    `chart_meaning_answer` carries whatever the meaning *and* the structured-JSON
    calls returned, because the two kinds are one carrier
    (`_CHART_ANSWER_KINDS`). Splitting them into a `structured_answer` field is
    what would let a reader compare the model's prose against its own JSON, but
    it changes a committed artifact's key tree and costs three paid calls to
    re-record — and the probe's output is the evidence three budget rows are
    priced from. T2e makes that trade when it re-runs the probe anyway.
12. **`rendered_dpi` is a permanently empty lane, not a gap being repaired.**
    pypdfium2 5.13.0 exposes no dpi on a `PdfBitmap`, so the field reads `null`
    with `rendered_dpi_error` on every page of every future run, and the real
    scale is the arithmetic `rendered_width_px / (size_pt / 72)`. The honest
    options are to rename the field to what it is (a harness *input*, recorded
    beside the pass rather than per page) or to derive it; either moves the key
    tree again, so it is T2d's, which already owes this harness a region-scoped
    OCR path.
13. **A host that enforces swap will need this projection revisited.** The driver
    reads cgroup v2 scalar limits and writes `-1` when the file says `max`, which
    is how "unlimited" reaches the record as an integer. It is indistinguishable
    from a literal `-1`, and every meta committed here carries `-1` for exactly
    that reason (this WSL2 kernel does not enforce the swap half). On T2f's
    device, `memory.swap.max` will answer a number; if the same writer keeps
    running there, `-1` should become either that number or a named gap rather
    than staying an integer standing for "unknown".
14. **Two kinds of duplication are left standing, both pre-existing in shape.**
    This diff adds a fourth `_sha256` helper (`tests/host/test_attachment_budgets.py`,
    beside three in `src/fin/corpus/`) and a third `importlib` script loader
    (the two new harness test files, beside `tests/host/test_jev_live_gate.py`).
    Neither has a shared seam to point at: promoting a corpus digest helper for a
    test's use would widen the product surface for test-only reasons, which is the
    one thing the review's own hygiene rule forbids. The loaders belong in
    `tests/conftest.py` and the digests in whichever module `src/fin/corpus/`
    unifies them into — T2d touches both directories for other reasons and owns
    the cleanup.
15. **Two soft S5 review triggers are tripped, in two files, and no hard gate is.**
    AST re-measurement of every script this diff ships or changes:

    | file | lines | max CC | functions over 4 parameters |
    | --- | --- | --- | --- |
    | `scripts/g4_pipeline/driver.py` | 1,361 | 9 | `build_record` 15, `render_pages` 9, `run_slice_pass` 8, `_apply_page_ocr` 7, `build_totals` 6, `_slice_one_page` 6 |
    | `scripts/g4_vision_probe.py` | 938 | **10** (`main`, `parse_args`) | `_record` 7, `summary_line` 5 |
    | `scripts/g4_ink_screen.py` | 267 | 8 | none |
    | `src/fin/host/attachment_budgets.py` | 226 | 6 | none |

    `driver.py` is past the 1,000-line soft trigger and has been decomposed as far
    as an honest field order allows: the remaining length is the record's own key
    tree, which *is* the artifact, and the six wide-parameter functions are the
    record builders and the measurement passes that thread a page index, a raster,
    a ceiling and a failure list through to it. Nesting is 3 and no function is
    over CC 10, so both hard gates hold. T2d's region-scoped OCR work is the next
    hand on that file and owns splitting `build_record` for real reasons.

    `g4_vision_probe.py:main` and `:parse_args` sit *exactly at* the CC 10 limit,
    which is a threshold, not a margin: the next `if` in either is a hard-gate
    failure, and T2e is about to add the retry and monetary-ceiling branches to
    `main`. The same gate is why `g4_ink_screen.py:aggregate` was decomposed into
    `_has_solid_block` and `_peaks` during this round — it measured CC 12 against
    the limit of 10 when it was written, and the decomposition is the reason the
    row above reads 8. The regeneration that followed it is command 45.
