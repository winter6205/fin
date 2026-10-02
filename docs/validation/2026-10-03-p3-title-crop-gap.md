# P3 chart-title crop gap — closed in the layout path, by measurement

Date: 2026-10-03. Task: **p3-title-crop** (ROOT direction on the residual of the tracked change / #350).
Base: **`a95b77b`** (merged main, which contains #350). Branch: `pre330/p3-title-crop`.

Closes the one item `docs/validation/2026-10-03-c1-gold-residual.md` left open: the mixed
gold case's primary `expected_ocr_quote` — the chart **title** — was unreachable, because
the figure region the driver cropped was inset from the artwork it named. This record fixes
it **in the production layout path** and re-measures. Nothing is scoped out, and the
existing title assertion is kept.

**An exit code of 0 is not a pass here.** The claim under test is a literal string being
present in the recovered OCR text, and that is shown below.

## Root cause

The layout stage returns a **detection** box. The PDF states where its artwork **is**.
On `g4-mixed-chart-001.pdf` page 3 those two disagree, and the disagreement is where the
title lives.

| reading | source | l | t | r | b | size |
| --- | --- | --- | --- | --- | --- | --- |
| layout **detection** box | Heron/Docling `prov.bbox`, via `driver.py:region_counts_from_document` → `charge(doc.pictures, "figure", …)` | 64.9 | 646.5 | 553.5 | 382.8 | 488.6 × 263.7 pt |
| **image placement** | `pypdfium2` `PdfImage.get_bounds()` on the same page | 42.0 | 692.0 | 570.0 | 364.0 | **528 × 328 pt** |

The placement is exactly the raster this case declares (`figure_regions[0].displayed_pt`:
*"528 x 328 (x=42, top y=100)"*), so the fixture is not the problem. The detection box is
inset ~23 pt left, ~46 pt top, ~16 pt right, ~19 pt bottom, and **the chart title, its
subtitle and the source line sit in the inset band above the box**. The driver then
cropped the detection box, so OCR read the axis and the data labels and could never reach
the title.

Measured, not inferred: the previous record cut the box at 200 dpi and looked at it — the
crop begins at the top of the y-axis and contains no title.

## The fix — in the layout path, derived from the page

`scripts/g4_pipeline/driver.py` is **bind-mounted**, not baked (`Dockerfile:141`,
`run_measured.sh:327-333`), so this is production code for the product's own Heron lane and
needs no image rebuild.

Two functions, and the wiring that connects them to the layout output:

- **`image_placements(page)`** — reads each image XObject's placement from the page with
  the pinned library's own `get_objects(filter=[FPDF_PAGEOBJ_IMAGE])` and
  `get_bounds()`, in the same bottom-left origin the record already uses. It sits beside
  `_page_image_objects`, because both come from the same object scan. An object the library
  will not place, and a page whose object list cannot be read, both yield **nothing** —
  neither is turned into a guess or a zero.
- **`artwork_extent(box, placements)`** — when a detection box overlaps an image placement,
  the region becomes that placement. The layout stage's own box is preserved beside it in
  **`figure_boxes_detected`**, so the record carries both readings and a reviewer can see
  what the model said and what the document said.
- **`attach_regions`** — the one place the layout pass's boxes are written onto a page, and
  therefore the one place the widening is applied. That is what makes this the *layout
  path* rather than a constant: the box the driver acts on is still the box the layout
  stage produced, widened by what the page reports about that same artwork.

**Why overlap and not strict containment.** Measured on this very fixture: page 1's
logotype placement is `l 289.5` while the layout box is `l 288.8` — the detector's box
starts 0.7 pt *left* of the artwork, so no strict containment would ever match it and the
region would stay inset, which is the defect being fixed. Among overlapping placements the
**tightest** wins, so a full-page raster cannot absorb a region on a scanned page and a
second figure that merely brushes the box loses to the one the box is inside. A box
overlapping no image at all — a vector chart, a drawn diagram — is left exactly as the
model reported it.

**No constant.** Nothing here names page 3, the chart, or any coordinate. The only literals
are in the tests, as the *measured case*. A box that no image backs comes out unchanged.

## Failing-first

`tests/host/test_g4_pipeline_figure_artwork_extent_contract.py` (new; loads the driver
**by path**, calls only its pure functions and `attach_regions`, against local doubles).

```
uv run --no-sync python -m pytest tests/host/test_g4_pipeline_figure_artwork_extent_contract.py -q
```

| stage | result |
| --- | --- |
| **RED** (before the change) | **`7 failed in 0.24s`** — every one `AttributeError: module 'g4_driver_artwork_extent' has no attribute 'image_placements'` / `'artwork_extent'`. The layout path did not exist. |
| **GREEN** (after) | **`10 passed in 0.11s`** |

The title assertion itself is proved by the re-measurement below, through the real harness.

## Re-measurement — mixed gold, real pinned harness

```
G4_IMAGE=fin-g4-pipeline:r1 G4_WORKTREE=/home/winner/projects/fin-wt-gold \
  bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf
```

exit **0**, `json_lines` 1, `container_exit` 0, `docker_run_exit` 0, `oom_killed` false,
`harness_failures` `[]`, `docling_status` `ConversionStatus.SUCCESS`.
Artefact: `scripts/g4_pipeline/results/g4-mixed-chart-001_20261002T144358Z/`
(**not committed** — `scripts/g4_pipeline/results/` is tracked and no result file is added).

### The recovered OCR text, literally

`pages/page_0003.figure-ocr.txt`, whitespace-normalised:

```
Total net sales by fiscal quarter (US$ millions, three months ended) 120,000 117,154
100,000 94,836 82,959 81,797 80,000 60,000 40,000 20,000 0 2022 Q3 2023 Q1 2023 Q2
2023 Q3 June 25, 2022 December 31, 2022 April 1, 2023 July 1, 2023 Source: Apple Inc.
Form 10-Q, Condensed Consolidated Statements of Operations
```

### Expected vs observed

| manifest expectation | observed | |
| --- | --- | --- |
| `expected_ocr_quote` = `Total net sales by fiscal quarter` | **present verbatim, offset 0** | **MET** |
| alt `117,154` | present verbatim, offset 77 | MET |
| alt `2022 Q3 2023 Q1 2023 Q2 2023 Q3` | present verbatim, offset 144 | MET |
| alt `Source: Apple Inc. Form 10-Q, Condensed Consolidated Statements of Operations` | present verbatim, offset 235 | MET |
| `ocr_expected: true` on p3 | `figure_ocr_regions_read` **1**, `figure_ocr_text_char_count` **608** (was 425) | MET |

The **title and all three alternatives hit**, so the acceptance bar is cleared on both
halves, not one. The subtitle, the full 0–120,000 axis, all four value labels and all four
period-end dates are recovered too.

### What changed in the record

| field | before | after |
| --- | --- | --- |
| p3 `figure_boxes[0]` | `l 64.9, t 646.5, r 553.5, b 382.8` | **`l 42.0, t 692.0, r 570.0, b 364.0`** |
| p3 `figure_boxes_detected[0]` | (key absent) | `l 64.9, t 646.5, r 553.5, b 382.8` — the layout stage's own box, kept |
| p3 crop at 200 dpi | 1357 × 732 px | **1467 × 911 px** |
| p3 figure OCR characters | 425 | **608** |
| `figure_ocr_text_chars_total` | 425 | **608** |

## Regression checks

| invariant | result |
| --- | --- |
| **p1 logo still skipped** | p1 box widened to its own placement `l 289.5, t 470.0, r 322.5, b 430.0` (33 × 40 pt), `_legible_figure_box` → **False**, `figure_ocr_regions_read` **0**. Widening to the true artwork did not make a logo legible |
| **no new regions** | per-page `region_counts` **identical** on all three pages: `14/0/1`, `3/1/0`, `5/0/1`; `regions_total {text 22, table 1, figure 2}` unchanged |
| **page rule unchanged** | `pages_needing_ocr` 0, `pages_with_ocr_applied` 0, `native_text_chars_total` **1908**, `ocr_text_chars_total` 0 — all identical |
| **scanned skip invariant** | re-measured — see below |
| ITEM 1's `table 1` on p2 | unchanged |

### `g4-scanned-001` — re-measured, and why it was in scope

The change adds two record keys (`image_placements`, `figure_boxes_detected`) and touches
the `figure_boxes` loop. Scanned pages report `figure 0` and `figure_boxes: []`, so the
widening loop never runs there — but the **record shape** changes, and a shape change is
observable, so it was measured rather than argued:

exit **0**, `harness_failures` `[]`, `docling_status` `SUCCESS`, `regions_total
{text 0, table 4, figure 0}` (p1/p2/p3 = **1 / 1 / 2**), `pages_needing_ocr` **3**,
`pages_with_ocr_applied` **3**, `native_text_chars_total` **0**, `ocr_text_chars_total`
**15108** (5565 / 3683 / 5860) — **every value identical to the prior committed record** —
and all three `expected_quote` values found **verbatim** in the `page_NNNN.ocr.txt`
sidecars at offsets **11 / 11 / 479**. The skip invariant holds:
`figure_ocr_skipped_reason: page_ocr_already_read_whole_page` with
`figure_ocr_regions_read 0` on all three pages, so a scanned page is still recognized
once. Its full-page raster placements are read and recorded, but with no figure box to
widen they change nothing.

**`2022_Q3_MSFT.pdf` was deliberately NOT re-run**: it carries no gold page expectations,
and the change is inert for it by the same argument as the scanned case (the widening acts
only on figure boxes, and its committed record reports no figure region).

## A defect this record found and fixed

The first measured run **failed with exit 1**:
`AttributeError: 'NoneType' object has no attribute 'append'` at
`driver.py:1432` — `figure_boxes_detected` is declared `null` (a reading that did not
happen is never a zero), and `dict.setdefault` returns an existing `None` rather than a
fresh list. `json_lines` was **0**.

The fix is the call site, and the guard is
`test_attach_regions_widens_a_measured_figure_box`, which drives the widening through
`attach_regions` itself rather than through `artwork_extent` alone — the pure function
passed while the entry point was broken. Recorded because it is the same failure mode
"exit 0 is not proof" exists to catch, in the other direction.

## Baseline undisturbed — **YES**

| check | result |
| --- | --- |
| `docker image inspect fin-g4-pipeline:r1 --format '{{.Id}}'` | `sha256:b1b5270c…9296` — unchanged |
| `pin.py self-check --pipeline-dir scripts/g4_pipeline` | **exit 0** |
| `git diff --stat` over `pyproject.toml`, `uv.lock`, `Dockerfile`, `image-pin.json`, `build.sh` | **empty** |
| `build.sh` invoked? | **no** |
| `g4-mixed-chart-001.pdf` sha256 | `0d6d4713…` — unchanged, this record touches no fixture |

## Manifest

`known_gaps[10]` rewritten from "the title quote is not reachable through that box" to
the resolution: the detection box and the page's placement are both named with their
measured coordinates, the fix is described as the driver widening a figure box to the
placement the page reports, and the recovered 608-character read is recorded together with
the title at offset 0 and all three alternatives at 77 / 144 / 235. No expectation was
weakened, removed, or re-scored to fit an output; `expected_ocr_quote` is unchanged and is
now satisfied.

## END STATE — **MET BY MEASUREMENT**

The title is recovered through the production layout path, together with every alternative
quote, and no invariant regressed.
