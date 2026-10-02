# C1 gold gap resolution — the four presence/OCR/quote gaps from #344

Date: 2026-10-02. Task: **#347**. Closes the four gaps named in
`docs/validation/2026-10-02-c1-vm-gold.md` Findings 3, 4 and 5 and in
`docs/validation/2026-10-02-pre330-delivery-audit.md` (*Pipeline result and
outstanding acceptance*).

Every gap is classified from the **source** — the committed filing, the fixture's
own content stream, and the product's committed contract — never from whether the
output happened to agree. One gap was a product defect and is fixed; three are
expectation/calibration defects and are dispositioned with their independent
source recorded. No expectation was weakened to match output, and one was
*corrected upward* against its own source.

**An exit code of 0 is not a pass here.** `run_measured.sh` gates on
`json_lines == 1` and container exit status, and it returned 0 on every run below
including the probe that carries gap 1's counter-evidence. Each gold expectation
is reported against the region/OCR/quote values actually read out of the record.

## Revisions under test

| | |
| --- | --- |
| Worktree | `/home/winner/projects/fin-wt-i347` (isolated; no other worktree touched) |
| Branch / candidate | `pre330/i347` @ **`320ea9f`** (= origin/main `ecd3ab4` + PRs #341/#342/#343/#344; includes #344 head `a5b1f7a`) |
| Measured image | `fin-g4-pipeline:r1` = **`sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296`** |
| Image identity disturbed? | **NO** |
| Builds run | none. `build.sh` was not invoked; `docker image inspect` returns the adopted digest before and after this work. |

**Why no rebuild was needed, and why the digest could not move.**
`scripts/g4_pipeline/Dockerfile` states "*driver.py is NOT baked: it lives in the
worktree, which run_measured.sh mounts read-only. Baking a second copy would
create a stale source of truth*", and `scripts/g4_pipeline/run_measured.sh:327-333`
runs the container with `--volume "${WORKTREE}:/worktree:ro"`,
`--env PYTHONPATH=/worktree/src` and the entrypoint
`/worktree/scripts/g4_pipeline/driver.py`. The driver is therefore **bind-mounted
source**: the driver change in this record is a worktree change, not an image
change, and the adopted baseline's image identity is untouched by construction.
`pyproject.toml`, `uv.lock`, `scripts/g4_pipeline/Dockerfile` and
`scripts/g4_pipeline/image-pin.json` are all unmodified (verified in *Verification*).

## Input hashes (measured, not quoted)

`sha256sum` over every gold input this record touches. The three PDFs and the PNG
match the digests their manifest cases declare, so each run measured the committed
bytes.

| input | sha256 (measured) | manifest agreement |
| --- | --- | --- |
| `g4-native-text-001.pdf` | `b4561e23331f0a0df9a05ed011e211e5f4f2f6e145d61d6ae70c1f86abceb011` | matches case `sha256` |
| `g4-mixed-chart-001.pdf` | `9b216bd9562edfeb74d11fb8a567ddd67cea2007e4fba4f5292918dc076da49e` | matches case `sha256` |
| `g4-scanned-001.pdf` | `73c38af71a0fd65b0fb5dc832adbbd12db410cab1e012d0c4da6e0923a31bf00` | matches case `sha256` |
| `g4-image-chart-001.png` | `2c8b6338d8e95dfa77a35bbbf0dcb003a3518e8ab82a195e9f26064a6f1117e5` | matches case `sha256` |
| `manifest.json` (edited by this record) | `43aea8af1d2dab4538438a11e5d1ce8cfec746d0e9c6f24fbe444d910b786d5b` | — (was `a19d…` pre-edit; see *Manifest change*) |

## Gap 1 — mixed-chart p2: no table region despite the manifest's presence floor

**Classification: expectation/calibration defect (fixture representativeness). The
floor is NOT weakened and the expectation remains recorded as UNMET by
measurement.**

*Independent source.* The fixture's own page-2 content stream (object 9, 5785
bytes inflated) is **98 positioned `Tj` text runs and zero path operators** — no
`re`/`l`/`m`/`S`/`f`, so there is not one ruling line on the page. The page is a
*borderless* financial statement rebuilt by a hand-written minimal PDF 1.4
writer. A 110 dpi render (`pdftoppm`) shows the Condensed Consolidated Statements
of Operations with its 5 aligned columns, and
`pdftotext -layout -f 4 -l 4 data/corpus/pdf/2023_Q3_AAPL.pdf` shows the same
table in the source filing. **The manifest's floor is correct about the page: it
does contain a table.**

*Decisive counter-measurement.* The real source page was put through the **same
pinned image and the same driver** — page 4 of the committed filing, extracted
with `pdfseparate` to a 67,797-byte single-page PDF
(`sha256 b6f732eb1fc7cfb53cbe9db21ca7edd9875695c563e9b27bc00c299b480e1ebc`):

```
regions_total {"text": 7, "table": 1, "figure": 0}   docling_status SUCCESS
harness_failures []   json_lines 1   exit 0
pipeline_identity: layout_heron_default, StandardPdfPipeline,
                   docling-project/docling-layout-heron, do_table_structure true
```

The pinned layout stage **does** return a table region for this content when the
content is a real filing page. It returns `table 0` only for the hand-written
reproduction. The disagreement is therefore with the fixture's synthetic geometry,
not with the product's ability to find a table in a real filing — and
`known_gaps[8]` already records that these fixtures are "built by tools on this
host (poppler + Pillow + a hand-written minimal PDF writer)".

*Disposition.* No expectation changed. `known_gaps[9]` was appended recording
that mixed p2 is not a valid carrier for a `table`-region expectation, naming
the probe as the counter-evidence, and stating that the floor is deliberately
left as authored. Changing the floor to `["text"]` would have been exactly the
forbidden "change the expectation to match the output".

*Acceptance impact.* **The `table` region on mixed p2 is still not produced by
the pipeline and this record does not call it passing.** What changes is that the
gap is now attributed correctly: it is a question about the pinned layout stage's
recall on synthetic borderless geometry, and the browser prerequisite "a table
region comes back for a real financial statement page" is carried by the
source-page measurement above rather than by this fixture. Resolving it inside
this lane would mean changing the ADR 0089 layout pin, which the issue's *Preserve
settled decisions* clause forbids and which would move the adopted baseline.

## Gap 2 — mixed-chart p3: expected figure-scoped OCR not applied

**Classification: expectation/calibration defect (lane scope). The manifest's
`ocr_expected: true` is left in place; it is a statement about the *product's*
figure rule, not about the lane this driver measures.**

*Independent source — the product's own committed contract.*
`src/fin/corpus/parsers/_pages.py:29-33` states:

> the *figure* rule (`crop_is_legible`) asks whether one region is a figure whose
> box is big enough to hold text drawn as pixels, **which is how text inside a
> chart on a page that also has a text layer is reached at all.**

and `tests/corpus/test_ocr_region.py:3-6` states the other half: "On a page with a
healthy text layer it can never answer 'yes', so text drawn inside a figure on
such a page is reached by the core's own figure rule." Running whole-page OCR on
a page that has a text layer is the *documented wrong behaviour*, not a miss.

*That the product actually reaches the content.* Evaluated against the figure box
this lane actually measured, through the product's own public rule:

| region | measured box | at the core's 200 dpi | shorter edge | `crop_is_legible` |
| --- | --- | --- | --- | --- |
| mixed p3 chart figure | `l 64.9, t 646.5, r 553.5, b 382.8` | 488.6 × 263.7 pt → 1357 × 732 px | **732 px** ≥ 160 | **True** — the product crops and recognizes it |
| mixed p1 logotype | `l 288.8, t 470.0, r 322.1, b 429.5` | 33.3 × 40.5 pt → 93 × 112 px | 93 px < 160 | False — correctly skipped, a logo holds no text |

The manifest's own scoping note ("the figure region only … the chart content
requires OCR") is therefore **satisfied by the product**; it is simply not a lane
the driver measures. The driver runs the page rule only.

*Disposition.* `known_gaps[10]` appended: `ocr_expected` is a page-rule
expectation and is not a driver-lane expectation on a page that has a text layer,
with the contract quote, the measured box and the computed verdict.

*Acceptance impact.* The expectation is not weakened and not marked met by this
lane. The manifest keeps `ocr_expected: true` on p3; the *driver* correctly
reports `pages_needing_ocr 0` there. Anyone scoring `ocr_expected` must score it
against the product's two-rule contract, not against `run_measured.sh` output.

## Gap 3 — scanned pages: no text regions despite the manifest's text/table floor

**Classification: expectation/calibration defect, in two parts.** Neither part is
a product defect, for two different and separately sourced reasons.

**3a — the `text` kind is not in the product's region vocabulary.**
`src/fin/host/attachment_layout.py:85` is the whole argument, and it is in the
code's own comment:

```python
# Kinds the record carries coordinates for. It counts text regions too, but a
# text region arrives as a count with no box, so it cannot become a Region.
_BOXED_KINDS: Final[tuple[str, ...]] = ("table", "figure")
```

`_regions_of` builds the product's `Region` list from `_BOXED_KINDS` alone
(`attachment_layout.py:963-981`), and the driver charges Docling's text items
with `boxed=False` (`driver.py:826`), so a text region exists only as a number.
A count cannot be cropped, sent to a vision call, or extracted from. The driver
measures raw Docling/Heron output, and the product's Heron path invokes this very
harness (`src/fin/host/workspace_http.py:3369`) — so **"zero text regions on a
raster page" changes nothing the product can do with the page.** The floor was
authored in a "what does this rendered page contain" vocabulary; the product's
region set is a "what has coordinates" vocabulary. Recording the disagreement
against the harness's text counter as a product gap would have been a
misclassification.

*Acceptance impact:* the `text` entry in a raster page's `region_kinds_expected`
is dispositioned as **not scorable against the product's region set**. Page
payload for those pages is verified through OCR and quotes instead — and all three
quotes now verify (§Gap 4). No floor value was deleted.

**3b — scanned p2's "no table" claim is contradicted by its own source.**
The manifest scored p2 as `region_kinds_expected: ["text"]` with the sentence
"No table and no graphic region exists on it". The fixture rasterises
**AAPL-2023Q3 page 9**, and `pdftotext -layout -f 9 -l 9
data/corpus/pdf/2023_Q3_AAPL.pdf` prints, in full:

> **Earnings Per Share** — The following table shows the computation of basic and
> diluted earnings per share for the three- and nine-month periods ended July 1,
> 2023 and June 25, 2022 … Net income $ 19,881 $ 19,442 $ 74,039 $ 79,082 …
> Basic earnings per share $ 1.27 $ 1.20 $ 4.69 $ 4.86 …

The page carries both prose and a table. The manifest entry was **factually
wrong**, and the observed `table 1` region on p2 is *correct against the source*.
This is the one case where the manifest was wrong and the measurement was right.

*Manifest change (the only one), with acceptance impact.* In the
`g4-scanned-001` p2 entry: `region_kinds_expected` `["text"]` → `["text","table"]`;
`role` and `chart_meaning` rewritten to describe the EPS table and to record, in
the file itself, that the entry was corrected on 2026-10-02 because the source
page contradicts it. **This raises the floor, it does not lower it** — and it is
a correction of a stale claim about the *page*, made from the committed filing,
not a re-scoring of a measurement. `dataset_id`, `schema_version`, `known_gaps[0]`
and all four case identities are unchanged (the ink-screen test pins
`known_gaps[0]` by content and still passes).

*After the correction, every **boxed** kind in the scanned floor is met:*

| page | expected (after correction) | observed `table` boxes | observed `text` count | boxed floor met? |
| --- | --- | --- | --- | --- |
| 1 | `["text", "table"]` | **1** | 0 | **yes** |
| 2 | `["text", "table"]` (was `["text"]`) | **1** | 0 | **yes** — and now agrees with the source |
| 3 | `["text", "table"]` | **2** | 0 | **yes** |

## Gap 4 — one scanned-page quote unverified behind a 160-character sample

**Classification: product defect.** The harness's persisted evidence could not
verify a gold expectation the repository had committed. Not a calibration problem:
the quote exists, the source supports it, and the artefact simply did not carry
enough of the OCR output to check it.

*Where the truncation is.* `scripts/g4_pipeline/driver.py:536` capped the sample
at 160 characters ("Short sample so the OCR output is auditable without shipping
the whole page text in the measurement line"). The p3 quote
`Total net sales $ 81,797 $ 82,959 $ [private source value removed] $ 304,182` sits at **offset 479** of
the whitespace-normalised page text (measured), so it was 319 characters past the
end of the window. The prior record called this "a limitation of the record
shape"; that is a defect in the shape.

*Fix — a sidecar, not a wider line.* The cap exists because `run_measured.sh`
fails the run unless stdout carries **exactly one** JSON line, so widening
`ocr_text_sample` would have traded one problem for another. The run already
persists each page bitmap beside its record, so the full recognized text is now
written beside it:

- `_ocr_text_path(page_no, image_dir)` — `page_NNNN.ocr.txt` beside
  `page_NNNN.png`; `None` when nothing is kept.
- `_save_ocr_text(...)` — writes the text and records `ocr_text_file` (name) and
  `ocr_text_sha256` (digest), the same two-key treatment the bitmap already gets.
- `_page_structure_entry` now initialises `ocr_text_file` and `ocr_text_sha256`
  to `None`, so "not measured" stays spelled as `null` rather than inferred from a
  missing key.
- It is written **only on a successful** OCR pass, and only where the bitmap is
  written: a failed pass must not leave an empty file that reads as "OCR read
  nothing", and a run with no `--write-images` must not persist text while
  persisting no pixels.

*Failing-first.* `tests/host/test_g4_pipeline_ocr_evidence_contract.py`, new,
following `tests/host/test_g4_pipeline_gap_contract.py`'s convention of loading
the driver **by path** and calling only its pure functions against local doubles.
Before the fix: **3 failed, 3 passed**. After: **6 passed**.

*Acceptance impact.* `ocr_text_sample` is still capped at 160 (asserted), so the
one-JSON-line contract is untouched; the widened evidence is additive and lives
beside the line. Every OCR-bearing page of a measured run now yields a
source-checkable artefact.

*Verification — the quote, checked against real output, not inferred.* All three
scanned quotes, read from the new sidecars and matched after whitespace
normalisation:

| page | manifest `expected_quote` | in `page_NNNN.ocr.txt`? | offset |
| --- | --- | --- | --- |
| 1 | `CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)` | **verbatim** | 11 |
| 2 | `Notes to Condensed Consolidated Financial Statements (Unaudited)` | **verbatim** | 11 |
| 3 | `Total net sales $ 81,797 $ 82,959 $ [private source value removed] $ 304,182` | **verbatim** | **479** |

The p3 quote is additionally cross-checked against its own independent source,
`pdftotext -layout -f 10 -l 10 data/corpus/pdf/2023_Q3_AAPL.pdf` line 12:

```
Total net sales      $   81,797    $   82,959    $  [private source value removed]    $  304,182
```

**OCR did not drop digits.** This is the fixture-independent check the manifest
named, and it is now **verified** rather than unverified. The sidecar digests are
in the record (`ocr_text_sha256`) and the sidecar byte sizes are 5573 / 3696 /
5914 against `ocr_text_char_count` 5565 / 3683 / 5860 — the +8 / +13 / +54 is
UTF-8 encoding of the curly quotes and dashes the source filing uses, not extra
or missing text.

## Reruns — commands, exits, expected versus observed

All runs through the real pinned harness, with `G4_CEILING_*` left at the
harness defaults:

```
G4_IMAGE=fin-g4-pipeline:r1 G4_WORKTREE=/home/winner/projects/fin-wt-i347 \
  bash scripts/g4_pipeline/run_measured.sh <pdf>
```

**`4g` / `4g` / `4` is a harness default (`run_measured.sh:28-30`), not a property
of this host.** This host is 4 CPUs / 7.66 GiB total; the 4 GiB is a `--memory`
argument the harness supplies by default, and it was left alone so these runs stay
comparable with the prior committed record. Nothing here measures a dedicated 4 GB
device, and **C2 stays deferred and unpassed**.

| # | input | exit | `json_lines` | container / docker exit | oom |
| --- | --- | --- | --- | --- | --- |
| 1 | `g4-scanned-001.pdf` (after fix) | **0** | 1 | 0 / 0 | false |
| 2 | `g4-mixed-chart-001.pdf` (after fix) | **0** | 1 | 0 / 0 | false |
| 3 | AAPL-2023Q3 page 4 probe (gap 1 evidence) | **0** | 1 | 0 / 0 | false |

### `g4-scanned-001.pdf` — expected vs observed, per page

| field | expected | observed | |
| --- | --- | --- | --- |
| `page_count` / `docling_page_count` | 3 / 3 | 3 / 3 | match |
| `docling_status` | `SUCCESS` | `ConversionStatus.SUCCESS` | match |
| `harness_failures` | `[]` | `[]` | match |
| `native_text_chars_total` | 0 | 0 | match |
| `pages_needing_ocr` | 3 | 3 | match |
| `pages_with_ocr_applied` | 3 | 3 | match |
| `ocr_text_chars_total` | (not fixed by manifest) | 15108 | 5565 / 3683 / 5860 |
| `regions_total` | — | `{text 0, table 4, figure 0}` | boxed floor met per page (table 1/1/2) |
| p1 / p2 / p3 quote | verbatim, per §Gap 4 | **verbatim ×3** | match |

### `g4-mixed-chart-001.pdf` — adopted baseline re-established exactly

| field | adopted baseline | this rerun | |
| --- | --- | --- | --- |
| `page_count` / `docling_page_count` | 3 / 3 | 3 / 3 | same |
| `regions_total` | `{text 24, table 0, figure 2}` | `{text 24, table 0, figure 2}` | **same** |
| per page `region_counts` | p1 14/0/1, p2 5/0/0, p3 5/0/1 | p1 14/0/1, p2 5/0/0, p3 5/0/1 | **same** |
| `pages_needing_ocr` | 0 | 0 | same |
| `native_text_chars_total` | 1908 | 1908 | **same** |
| `ocr_text_chars_total` | 0 | 0 | same |
| `docling_status` | `SUCCESS` | `SUCCESS` | same |
| `harness_failures` | `[]` | `[]` | same |
| `slice_pass_error` | `null` | `null` | same |
| new keys | — | `ocr_text_file: null`, `ocr_text_sha256: null` on all 3 | additive only |

**The driver change did not disturb the adopted baseline.** No OCR fires on this
case, so no sidecar is written (`pages/` holds the three PNGs only) and every
content-bearing field is bit-identical to the prior record. `regions_total` still
carries `table 0`, which is gap 1's unmet expectation and is reported as unmet.

**The 80-page `2022_Q3_MSFT.pdf` case was deliberately NOT rerun.** The change
adds two record keys and writes a sidecar only on a page where the repo OCR rule
fired; that case reports `pages_needing_ocr 0` over all 80 pages, so the change is
inert for it by construction, and re-running a 10-minute case to restate unchanged
numbers is what the issue asks not to do. No other change touches layout, regions
or the page rule.

## Selected development browser gold cases

The browser prerequisites are carried by the three committed PDFs. Every line is
a value measured on candidate `320ea9f` through the pinned harness, so a reviewer
can check it against a rerun rather than against a description.

| # | case (sha256) | expected content / regions / OCR / quote | observed | status |
| --- | --- | --- | --- | --- |
| 1 | `g4-native-text-001.pdf` (`b4561e23…`) | 1 page, text-only, `ocr_expected false`, quote from the text layer. Purpose: OCR must not run when a text layer exists. | 1 page, 8 text / 0 table / 0 figure, 0 OCR | not rerun this round (unaffected by the change; recorded from the adopted baseline) |
| 2 | `g4-mixed-chart-001.pdf` (`9b216bd9…`) | p1 figure 1 (33×40 pt logotype), p2 `table` 1 (**unmet — see Gap 1**), p3 figure 1 and figure-scoped crop legible (732 px ≥ 160), page-level OCR off on all 3 | p1 14/0/1, p2 5/0/0, p3 5/0/1, `pages_needing_ocr 0` | **partially unmet**, precisely attributed |
| 3 | `g4-scanned-001.pdf` (`73c38af7…`) | 3 image-only pages, 0 native chars, OCR applied on all 3, boxed kinds `text`-dispositioned / `table` present, 3 quotes verbatim | table 1/1/2, OCR 15108 chars, **3/3 quotes verbatim** | **pass** on every expectation the product can express |
| — | AAPL-2023Q3 page 4 (`b6f732eb…`, probe, **not a gold case**) | carries the table-region prerequisite that mixed p2 cannot | `regions_total {text 7, table 1, figure 0}`, `SUCCESS` | evidence for Gap 1; deliberately **not** committed as a new gold case |

**T8 exclusion honoured:** no zero-baseline financial gold case was introduced.
The page-4 probe is a diagnostic input built in a temporary directory, run
through the harness, and deleted; `git status` carries no new fixture.

## Evidence paths

Run artefacts are **uncommitted by design** — `scripts/g4_pipeline/results/` is a
tracked directory and none of these were added:

- `scripts/g4_pipeline/results/g4-scanned-001_20261002T121638Z.meta.json`
  (+ `.jsonl`, and the run directory with `pages/page_000{1,2,3}.png` and the new
  `pages/page_000{1,2,3}.ocr.txt` sidecars)
- `scripts/g4_pipeline/results/g4-mixed-chart-001_20261002T121910Z.meta.json`
  (+ `.jsonl`, and the run directory with `pages/`)
- `scripts/g4_pipeline/results/AAPL-2023Q3-p4-probe-4_20261002T121356Z.meta.json`
  (+ `.jsonl`, and the run directory) — the gap 1 counter-measurement

Prior baseline for the scanned case, still on disk from the interrupted run and
consistent with the committed record: `g4-scanned-001_20261002T120734Z.*`
(identical regions, OCR totals and quotes; its harness `exit` never completed, so
it is a reference, not a pass record).

Expectations cited: `data/corpus/g4_attachment_gold/manifest.json`,
`data/corpus/pdf/2023_Q3_AAPL.pdf` pages 4, 9 and 10. Product contract cited:
`src/fin/host/attachment_layout.py:85`, `src/fin/corpus/parsers/_pages.py:29-33`,
`src/fin/corpus/ocr.py:283`, `tests/corpus/test_ocr_region.py:3-6`.
Harness contract: `scripts/g4_pipeline/run_measured.sh:28-30,327-333`.

## Unresolved items

1. **Mixed-chart p2 still produces no `table` region.** Not waived, not called
   passing. Now attributed to the pinned Heron layout stage's recall on a
   hand-written borderless reproduction, with the real source page measured at
   `table 1` as counter-evidence. Deciding it means either changing the ADR 0089
   layout pin (moves the adopted baseline; forbidden here) or re-authoring the
   fixture page with real filing geometry. **Owner: layout-model selection /
   fixture authoring, not this issue.**
2. **The `text` entry in a raster page's `region_kinds_expected` is dispositioned,
   not satisfied.** It is not expressible in the product's region set
   (`_BOXED_KINDS = ("table", "figure")`). If a future contract wants text regions
   scored, they need coordinates first — that is a product change, not a gold change.
3. **C1 Finding 6 — `rendered_dpi` is `null` on every page** of every run
   (`AttributeError: 'PdfBitmap' object has no attribute 'get_dpi'`). Untouched
   here; still a ticket against the pypdfium2 pin.
4. **C1 Finding 7 — the host-side cgroup peak is `null`** on this WSL2 host. The
   driver-side `cgroup_peak_bytes` is the reading that stands.
5. **C2 remains DEFERRED and UNPASSED.** The dedicated 4 GB device is still the
   missing prerequisite. Nothing in this record closes it, and the 4 GiB cap in
   every run above is a harness default argument.
6. **The exit-code contract is observed on the success path only, except for one
   path.** The measured runs all returned 0. One *failed* probe — the first
   attempt to run the page-4 counter-measurement with the PDF left outside the
   worktree, so the read-only mount could not see it — did exercise the documented
   `1`: the driver printed `[driver] no such PDF`, stdout carried 0 JSON lines,
   and the harness reported `FAIL: expected exactly 1 JSON line on stdout, got 0`
   with `container_exit_code: 2`, `docker_run_exit_code: 2` and exit `1`
   (`results/AAPL-2023Q3-p4-probe-4_20261002T121405Z.meta.json`). That confirms
   the count gate fires. **No run here exercised 64, 66, 65 or 75**, so nothing in
   this record is evidence about those paths.

## Verification actually run

```
$ sha256sum data/corpus/g4_attachment_gold/*
b4561e23…  g4-native-text-001.pdf
9b216bd9…  g4-mixed-chart-001.pdf
73c38af7…  g4-scanned-001.pdf
2c8b6338…  g4-image-chart-001.png
43aea8af…  manifest.json

$ G4_IMAGE=fin-g4-pipeline:r1 G4_WORKTREE=/home/winner/projects/fin-wt-i347 \
    bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-scanned-001.pdf
EXIT=0   json_lines 1   regions_total {text 0, table 4, figure 0}
         pages_needing_ocr 3   ocr_text_chars_total 15108   harness_failures []
         quotes: p1 verbatim @11, p2 verbatim @11, p3 verbatim @479

$ G4_IMAGE=fin-g4-pipeline:r1 G4_WORKTREE=/home/winner/projects/fin-wt-i347 \
    bash scripts/g4_pipeline/run_measured.sh data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf
EXIT=0   json_lines 1   regions_total {text 24, table 0, figure 2}
         pages_needing_ocr 0   native_text_chars_total 1908   harness_failures []
         (adopted baseline reproduced exactly)

$ uv run --no-sync pytest -q tests/host/test_g4_pipeline_ocr_evidence_contract.py
RED (before the driver fix):  3 failed, 3 passed
GREEN (after the driver fix): 6 passed

$ uv run --no-sync pytest -q tests/host/test_attachment_budgets.py \
    tests/corpus/test_page_region_core.py \
    tests/host/test_g4_pipeline_gap_contract.py \
    tests/host/test_g4_pipeline_ocr_evidence_contract.py \
    tests/host/test_g4_pipeline_pin_contract.py \
    tests/host/test_g4_pipeline_artifact_hash_contract.py \
    tests/host/test_g4_pipeline_declaration_crosscheck.py
313 passed, exit 0   (the g4 set alone: 103 passed, exit 0)

$ uv run --no-sync pytest -q tests/host/ tests/corpus/
1 failed, 4162 passed, 117 skipped, 301 deselected, 8 warnings in 552.76s
(the one failure is the pre-existing [ocr]-extra failure documented below)
$ docker image inspect fin-g4-pipeline:r1 --format '{{.Id}}'
sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296   (unchanged)

$ docker ps --format '{{.Names}}' | grep fin-pre330-
fin-pre330-es  fin-pre330-neo4j  fin-pre330-iknow-pg
fin-pre330-ledger  fin-pre330-s3  fin-pre330-pg        (all Up, untouched)
```

`pyproject.toml`, `uv.lock`, `scripts/g4_pipeline/Dockerfile` and
`scripts/g4_pipeline/image-pin.json` are **unmodified** (`git status --short` on
those four paths is empty). No `docker prune` was run; the only containers stopped
were the harness's own per-run containers, which is its existing behaviour. No
environment file was read or printed — only variable **names** are referenced.
`/home/winner/projects/iknow-memory` was not touched. No push, no merge, no PR.

### One pre-existing failure in this environment, not caused by this change

`tests/host/test_workspace_http_attachment_lane.py::test_a_pass_leaves_its_page_boxes_in_the_bound_sink`
fails here with
`OcrRequiredUnavailableError: pytesseract is not installed; install the [ocr]
extra or use --ocr-engine none/mocked` (raised from
`src/fin/corpus/ocr.py:215` while reading a figure region).

**Proven pre-existing**: with this record's four changed/new files stashed away
and the tree back at clean `320ea9f`, the same test fails identically
(`1 failed in 0.95s`). It is the missing `[ocr]` extra in this `.venv`, the same
environment limitation the delivery audit describes for B1 ("39 passing
OCR-sensitive tests **after installing the declared extra**"). It is **not**
counted as a pass here and is not waived by this record; it needs the extra
installed, which is a dependency-state change outside this issue's scope.
