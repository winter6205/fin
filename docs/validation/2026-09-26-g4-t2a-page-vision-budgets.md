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
| `2023_FY_GOOG.pdf` | 1 | 624 | 489,889,792 | 384,000,000 | 0 | 0 | 0 | 3,489.8 |
| `2023_FY_GOOG.pdf` | 1 | 624 | 885,321,728 | 937,984,000 | 0 | 0 | 0 | 5,830.8 |
| `2023_FY_GOOG.pdf` | 1 | 624 | 1,301,147,648 | 1,065,414,656 | 0 | 0 | 0 | 15,013.3 |
| `2023_Q1_MSFT.pdf` | 72 | 2,223,272 | 3,324,112,896 | 3,102,502,912 | 182,738,944 | 0 | 0 | 1,528,374.9 |

The 72-page conversion is the load-bearing one: Docling whole-document
`829,935.8 ms`, i.e. `11,526.9 ms/page`; warm steady-state single-page
conversion mean `9,459.3 ms`, median `3,559.1 ms`, max `47,298.0 ms`;
render-plus-OCR pass `17,369.3 ms` (`241 ms/page`, with 0 pages needing OCR in
this native-text file); Heron regions over 72 pages: 790 text, 55 table,
**1 figure**; 222,233 native text characters.

Model load, not page count, dominates the floor: a 1-page run already peaked
between 0.49 and 1.30 GiB.

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
contend 1:1. The same 1-page input measured 3,489.8 ms, 5,830.8 ms, and
15,013.3 ms across three runs — a ~4× spread that is contention, not variance
in the input.

What this does and does not invalidate:

* `memory.max = 4294967296` **is** enforced per container and the OOM counter
  is real, so every resident-peak figure and the byte/page counts stand.
* Global major-fault traffic is not attributable to one container, so **no
  millisecond figure here is a device specification**. They are upper bounds
  measured under load, used only to order the two page-cap options (which
  differ ~2×, outside the spread) and to show that per-page cost is the
  binding constraint, not total bytes.
* `driver.py` did not record load or major faults per run, so the runs that
  mattered were re-measured serially with load captured before and after; see
  the *Re-runs* rows in the command table.

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
| vision, serial | 88 × 2,462 ms = 3.6 min | 48 × 2,462 ms = 2.0 min |
| one max attachment end-to-end | ≈ 19.0 min | ≈ 9.7 min |
| input tokens at the cap | 88 × 1,413 = 124,344 | 48 × 1,413 = 67,824 |
| extrapolated 4 GiB margin | 13.1% | 43% |
| corpus files rejected | 0 of 13 | 4 of 13 (31%: 67, 72, 74, 80 pages) |

`80` was chosen because the excluded set is exactly the SEC 10-Q filings that
this product exists to read, the ticket's own acceptance clause is that the
caps admit the observed corpus, and every bound is a deployment setting with
no code default — an operator on a 2 GiB device lowers all four without a code
change, which is the mitigation `40` would otherwise have provided by fiat.

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

`data/corpus/g4_vision_probe.json` — three real calls against fin's configured
provider (`FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY`, read from the
environment; no provider or model identifier is written into
`attachment_budgets.py`), on a `816 × 1056` PNG of `37,482` bytes:

| call | kind | latency | input tok | output tok | outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | chart presence | 2,294 ms | 1,346 | 26 | correct |
| 2 | chart meaning + mean | 2,462 ms | 1,382 | 463 | correct, recomputed `20.75` |
| 3 | structured JSON | 3,303 ms | 1,413 | 254 | schema-valid |

So the provider takes image input, returns parseable structure, and reports
usage. `max_vision_crop_calls_per_run = 8` is the observed crop demand of the
committed gold set: 2 chart-present pages × 4 numeric anchors, i.e. the number
of question-specific inspections that could each need a higher-resolution look
after the full-page scan. `max_vision_calls_per_run = 88` is `80 + 8`, exactly
the minimum the self-consistency rule allows — one full-page scan for every
admissible page plus that crop headroom.

## Validation seam

`src/fin/host/attachment_budgets.py` (new, 222 lines): a frozen four-field
`AttachmentBudgets`, `from_environ` requiring all four keys, `validate`
enforcing positivity, an upper bound at `sys.maxsize`, and
`calls >= pages + crop`, and `preflight_run(budgets, admitted_pages,
requested_crop_calls)` which refuses a run **before** any page is skipped and
maps the two violations onto the only outward codes the module owns:
`attachment_page_limit_exceeded` and `attachment_vision_budget_exceeded`. A
config failure has no outward code (spec's closed list), so
`AttachmentBudgetConfigError` is internal by contract.

Tests: `tests/host/test_attachment_budgets.py`, 105 cases. The three
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
| 5 | `python -m py_compile src/fin/host/attachment_budgets.py tests/host/test_attachment_budgets.py scripts/g4_upload_distribution.py scripts/g4_vision_probe.py` | 0 | the repo has no ruff/mypy gate; compile is the typecheck seam |
| 6 | `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_mcp_t2.py tests/retrieve/test_live_mcp_t3.py tests/retrieve/test_live_mcp_t4.py tests/retrieve/test_live_mcp_t5.py tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_host_graph_hop.py` | 0 | 11 passed in 36.65 s |

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

Pipeline and provider commands and their per-case results are recorded by
`scripts/g4_pipeline/` (§Basis above for the recorded metas).

<!-- GOLD-ROWS: filled from scripts/g4_pipeline/results/g4_t2a_gold_summary.json -->

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
  this ticket's measurement harness (`driver.py` metas, `g4_vision_probe.json`);
  the product-side record of the same fields is T2e/T2f work.
* Vision on pipeline-rendered real filing pages, and whether a full-page scan
  already answers a question-specific detail (the crop trigger): measured here
  only on the synthetic chart image.
* OCR cost on a genuinely scanned file at scale: the 72-page measurement had
  `pages_needing_ocr = 0`, so the per-page parse figure above never included
  OCR. The one scanned case this ticket attempted did not complete in a bounded
  time on this host — CPU Tesseract at the pinned configuration burned
  **≥ 10:51 of CPU time on page 1 of 3** and was still running when the record
  was written, and a separate single-image run on a 1200 × 746 PNG took 17 min
  wall at ~52 % average CPU. Given 3.9 GiB of swap in use and 817,456 major
  faults on the backing VM, that time cannot be attributed to the OCR algorithm
  rather than to the host, so **scanned-page OCR cost is unmeasured here**. It
  is the largest open quantity in this ticket: see *Leftovers*.
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
2. One vision scan per admissible page, serial, costs ≈ 3.6 min and ≈ 124K
   input tokens for a max-size attachment. Nobody has decided a per-turn
   monetary ceiling; that is an operator/product call this ticket can only
   price, not make.
3. A turn with two max-size attachments cannot fit `88` calls (2 × 80 pages),
   so it fails `attachment_vision_budget_exceeded` by design. There is no
   per-message attachment-count limit in the product, so the guidance to send
   one large filing per turn belongs to T2b's composer work.
4. **Scanned-page OCR is unquantified**, and it is the only cost in the
   pipeline that could make the 80-page cap unusable in practice. Re-measure one
   scanned page on an idle 4 GB device — with `VmHWM`, CPU-time, major faults,
   render DPI, and `--psm` recorded — before T2d picks a rendering configuration
   from these numbers. Do not read the 19-minute estimate above as covering an
   OCR-bearing file.
5. This record's "4 GB device" is a cgroup ceiling on a 7.7 GiB WSL2 VM that was
   itself swapping. The peaks and OOM counts are valid; the wall-clock figures
   are not comparable to a dedicated 4 GB machine, and the operator who
   authorises these four values as the shipped default should read them as
   *bounded and consistent*, not as *fast*.
