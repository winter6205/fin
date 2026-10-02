# Issue 323 (B1) — the default OCR import-isolation contract, measured in a clean subprocess

Date: 2026-09-29. Every "Commands" row ran on this branch on this date.

Scope reference: `plans/pr321-merge-followup.md:85` — the follow-up row
"L3: default-suite `pytesseract` collection leak … Observable acceptance:
`uv run pytest -q` exits 0." This record is that follow-up (item B1), not a
new product feature. It discharges the L3 row's observable acceptance and
nothing else in that plan.

Branch: `worktree-issue-323-ocr-live-gate` (base `670f65f`).

Inherited contract: `specs/corpus-serve-ingest.md` — "default pytest does
not require a live OCR engine". The second half of that clause is already
pinned by `tests/corpus/test_ocr.py::test_tesseract_engine_raises_typed_when_pytesseract_missing`;
this record covers the first half.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Python | 3.12.14 |
| Code under test | `670f65f` (base) plus this record's test-only commit. `src/` is **byte-identical to `670f65f`** — the deliverable touches `tests/corpus/test_ocr.py` only; `git diff --stat -- src/` is empty and `sha256(src/fin/corpus/ocr.py)` is `bff74997bd81565fd11436a95c28edde7a7ed71522eaf8e09269056d41a1db52` before and after |
| Optional extra | `pytesseract 0.3.13` installed via `uv sync --frozen --extra ocr` (exit 0). This is the configuration in which the contract is at risk |
| System tesseract | `5.5.0`, leptonica `1.86.0`; `tessdata` at `/usr/share/tesseract-ocr/5/tessdata/` carries `chi_sim`, `eng`, `osd` — i.e. both languages `fin.corpus.ocr.DEFAULT_TESSERACT_LANG` (`chi_sim+eng`) names |
| Attachment gold dataset | `data/corpus/g4_attachment_gold/`, `dataset_id` `fin-g4-attachment-gold-v1`, `schema_version` `1.0.0`, `created` 2026-09-26 / `updated` 2026-09-27, 4 cases / 8 physical pages / 5 pages requiring OCR |
| `manifest.json` digest | `sha256 815262788ea7079784b4ffe48b23ff4760932da055e1d1a9fcb64473954c5ace` |

Per-case digests, re-computed on this host and compared against the
committed `manifest.json` — all four match, so the gold bytes this round
ran against are the committed ones:

| Case | `manifest.json` `sha256` | Re-computed on this host |
| --- | --- | --- |
| `g4-mixed-chart-001.pdf` (35,324 B) | `9b216bd9562edfeb74d11fb8a567ddd67cea2007e4fba4f5292918dc076da49e` | identical |
| `g4-scanned-001.pdf` (677,716 B) | `73c38af71a0fd65b0fb5dc832adbbd12db410cab1e012d0c4da6e0923a31bf00` | identical |
| `g4-image-chart-001.png` (40,419 B) | `2c8b6338d8e95dfa77a35bbbf0dcb003a3518e8ab82a195e9f26064a6f1117e5` | identical |
| `g4-native-text-001.pdf` (3,150 B) | `b4561e23331f0a0df9a05ed011e211e5f4f2f6e145d61d6ae70c1f86abceb011` | identical |

## Root cause

Two distinct leak classes were confirmed, and only one of them was the
cause of the observed failure.

**Class 1 — the asserted one, and it is a test defect, not a product
defect.** `src/fin/corpus/ocr.py:183-192` already imports `pytesseract`
lazily, inside `TesseractEngine.__init__`; importing the module does not
pull it. The assertion, however, was made in the pytest process itself:
`test_default_pytest_does_not_import_pytesseract` did
`import fin.corpus.ocr` and then scanned that process's `sys.modules`.
pytest imports every test module during **collection**, before any test
body runs, so `tests/corpus/test_ocr_tesseract_gold.py:25`'s module-level
`pytest.importorskip("pytesseract", ...)` had already executed the real
import. The assertion therefore measured the suite's collection order
rather than `fin.corpus.ocr`'s import graph, and failed deterministically
whenever the `[ocr]` extra was installed — in any collection order,
including `test_ocr.py` collected first, because collection completes
before the first test executes.

**Class 2 — a real runtime leak from a sibling, out of this scope.**
Any test that instantiates `TesseractEngine()` pulls the extra at run
time. `tests/host/test_workspace_http_attachment_lane.py:545` already
repairs that with an autouse `_no_pytesseract_leak` cleanup fixture. That
file is owned by another agent on this branch and was **not** touched here.
The new subprocess assertion is immune to class 2 by construction, so
class 2 is no longer able to break this contract from outside.

## The change

`tests/corpus/test_ocr.py` only. `test_default_pytest_does_not_import_pytesseract`
now runs its `sys.modules` scan in a **fresh interpreter** that imports
nothing but `fin.corpus.ocr`, and reports three values back:
`ISOLATION-LEAKED` (the scan's verdict), `ISOLATION-AVAILABLE` (whether
the `[ocr]` extra is installed here, via `importlib.util.find_spec`, which
locates the module without executing it), and `ISOLATION-CONTROL` (a
**positive control**: the same scan run again after the subprocess has
imported `pytesseract` for real).

The control is what keeps the assertion from being vacuous. An empty leak
list means one of two very different things — the module is clean, or the
scan could not have seen `pytesseract` in the first place. On a host
without the extra the second reading is always available, so the raw
assertion would pass trivially and prove nothing. With the extra
installed, the control must show `'pytesseract'` present, which
establishes that the same scan detects the module when it is there; the
leak list must then still be empty, because the only thing imported
between the two scans is `fin.corpus.ocr`. The at-risk configuration is
therefore the one in which the test is strongest, not the one in which it
is weakest.

This reuses the subprocess pattern already established in this repo
(`tests/corpus/test_window_extract_contract.py:624` `_run_poison_subprocess`),
whose own docstring records the same reasoning about a polluted
`sys.modules`; no new mechanism was invented.

### Nothing was deleted, weakened, skipped, or xfailed

* `test_default_pytest_does_not_import_pytesseract` is **strengthened**, not
  relaxed: the same leak assertion now runs against a clean interpreter
  rather than a contaminated one, and a positive control is added that
  had no counterpart before.
* `tests/corpus/test_ocr_tesseract_gold.py` is **unmodified**
  (`git diff --stat` for that path is empty). Its module-level
  `importorskip` gate, its reason string, and all five of its real-binary
  cases are exactly as they were, and all five still run in the
  opt-in run below.
* No test was removed and no assertion was relaxed anywhere. No
  `skipif`, `xfail`, or `importorskip` was added.

## Load-bearing verification (mutation)

A passing test proves nothing until it is shown to fail when the
contract is broken. `import pytesseract` was added at module scope in
`src/fin/corpus/ocr.py` (line 46, a one-line probe), the isolation test
was run, and the probe was then removed. The production file was
restored byte-exact — `sha256` matches the pristine file
(`bff74997bd81565fd11436a95c28edde7a7ed71522eaf8e09269056d41a1db52`) and
`git status --porcelain` shows `src/` clean.

Observed with the probe present:

```text
uv run --no-sync pytest -q tests/corpus/test_ocr.py -k does_not_import_pytesseract
E   AssertionError: fin.corpus.ocr leaked pytesseract on import: ['pytesseract', 'pytesseract.pytesseract']
E   assert "['pytesserac...pytesseract']" == '[]'
1 failed, 20 deselected in 3.35s
```

The test therefore detects a real module-level import, which is the
exact regression the contract forbids.

## Independently expected vs observed

| Case | Expected (stated from the contract, not from the code) | Observed |
| --- | --- | --- |
| `test_default_pytest_does_not_import_pytesseract`, extra installed | A clean interpreter importing only `fin.corpus.ocr` leaves no `pytesseract*` key in `sys.modules`, and the control scan run after a real import does show one | **matches** — `ISOLATION-LEAKED []`, `ISOLATION-AVAILABLE True`, `ISOLATION-CONTROL ['pytesseract', 'pytesseract.pytesseract']` |
| `test_default_pytest_does_not_import_pytesseract`, with a module-level leak in `fin.corpus.ocr` | the test **fails**, naming the leaked keys | **matches** — `1 failed, 20 deselected`, naming both keys |
| `test_ocr_tesseract_gold.py` | every case runs the real `tesseract` 5.5.0 binary over the committed gold bytes and matches the manifest's quote; **0 skipped** | **matches** — `7 passed, 0 skipped`; engine resolves binary `tesseract` 5.5.0, and an independent direct binary run reproduces `expected_quote` exactly |
| Gold module and `test_ocr.py` in the same pytest process | the isolation assertion still passes, because its subprocess does not share the collector's `sys.modules` | **matches** — `28 passed`, against `1 failed, 27 passed` for the same command before the change |
| All five `tests/corpus/test_ocr*.py` together | pass, with no order dependence | **matches** — `74 passed` |
| Default suite with the extra installed | exit 0 | see *Full default suite* |

## Commands

| Command | Result |
| --- | --- |
| `uv sync --frozen --extra ocr` | exit 0; `pytesseract 0.3.13` importable |
| `tesseract --version` / `--list-langs` | `5.5.0` / leptonica `1.86.0`; `chi_sim`, `eng`, `osd` |
| `sha256sum data/corpus/g4_attachment_gold/*` | 4/4 case digests identical to `manifest.json` |
| **RED, before the change:** `uv run --no-sync pytest -q tests/corpus/test_ocr_tesseract_gold.py tests/corpus/test_ocr.py` | **1 failed, 27 passed** in 175.21 s — `AssertionError: fin.corpus.ocr leaked pytesseract on import: ['pytesseract', 'pytesseract.pytesseract']` |
| **Mutation (proves load-bearing):** `uv run --no-sync pytest -q tests/corpus/test_ocr.py -k does_not_import_pytesseract` with a module-level `import pytesseract` in `src/fin/corpus/ocr.py` | 1 failed, 20 deselected in 3.35 s — the leak is detected |
| `uv run --no-sync pytest -q tests/corpus/test_ocr.py -k pytesseract` | 2 passed, 19 deselected — exit 0 |
| **Same-process pollution:** `uv run --no-sync pytest -q tests/corpus/test_ocr_tesseract_gold.py tests/corpus/test_ocr.py` | **28 passed** in 89.67 s — exit 0. This is the exact command that was **1 failed, 27 passed** above |
| **Order independence, five files together:** `uv run --no-sync pytest -q tests/corpus/test_ocr.py tests/corpus/test_ocr_branch.py tests/corpus/test_ocr_region.py tests/corpus/test_ocr_tesseract_gold.py tests/corpus/test_ocr_writeback.py` | **74 passed** in 81.02 s — exit 0 |
| `uv run --no-sync --with pytesseract -q pytest -v tests/corpus/test_ocr_tesseract_gold.py` | **7 passed, 0 skipped** in 25.24 s — exit 0. Every case `PASSED` (no `SKIPPED` line anywhere), i.e. the real-binary coverage still runs |
| `uv run --no-sync pytest -q` (full default suite, extra installed) | **4647 passed, 116 skipped, 352 deselected** in 1034.01 s — **exit 0**. See *Full default suite* |

### The isolation subprocess's own verdict, printed

Run on the branch tip with the `[ocr]` extra installed, so all three
values are the at-risk configuration's:

```text
ISOLATION-LEAKED []
ISOLATION-AVAILABLE True
ISOLATION-CONTROL ['pytesseract', 'pytesseract.pytesseract']
```

Read in order: the extra **is** installed (`AVAILABLE True`), the same scan
**does** see pytesseract once something imports it (`CONTROL` lists both
keys), and the leak list after importing only `fin.corpus.ocr` is **empty**.
The middle line is what makes the first one evidence rather than a
tautology.

### That the gold run used the real binary, not a fake

Two independent checks, because "7 passed" alone does not distinguish a
real OCR call from a mock.

1. The gold module constructs the production engine
   (`tests/corpus/test_ocr_tesseract_gold.py:84`, `:111`, `:197` all pass
   `TesseractEngine()`); there is no fake engine in the module. The
   resolved engine reports:

   ```text
   engine class: TesseractEngine
   pytesseract module: pytesseract file: .../site-packages/pytesseract/__init__.py
   binary path: tesseract
   reported version: 5.5.0
   ```

2. The system binary was invoked directly, following the manifest's own
   `quote_verification` recipe for `g4-image-chart-001`
   (`tesseract <png> stdout -l eng --psm 6`), and the committed
   expectation was checked against what came back:

   ```text
   expected_quote  : 'Total net sales by fiscal quarter'
   binary first line: 'Total net sales by fiscal quarter'
   MATCH: True
   chars returned: 314
   ```

   This reproduces a `manifest.json` expectation with a tool outside fin,
   which is the basis the manifest's `expectation_basis.rule` claims for
   its own numbers.

## Full default suite

The core acceptance for this item is `plans/pr321-merge-followup.md:85`:
"`uv run pytest -q` exits 0". **Met**, with the optional `[ocr]` extra
installed — the exact configuration in which this contract previously
failed:

```text
uv run --no-sync pytest -q --basetemp=/home/winner/pytest-tmp-b1/run
→ 4647 passed, 116 skipped, 352 deselected, 8 warnings in 1034.01s (0:17:14)
→ exit 0
```

Zero failures and zero errors. The baseline this fixes, on the same host
with the same extra installed and the same command, was:

```text
1 failed, 4646 passed, 116 skipped, 352 deselected, 8 warnings in 2946.76s
→ exit 1
→ FAILED tests/corpus/test_ocr.py::test_default_pytest_does_not_import_pytesseract
```

The pass/skip/deselect counts differ slightly between the two runs only
because the earlier one aborted several `tests/corpus/test_ingest.py`
cases at teardown (below); the suite's population is otherwise the same,
and `tests/corpus/test_ocr.py` is no longer among the failures.

One environment note that belongs in the record rather than being
quietly dropped: the first attempt at this run reported
`4642 passed, 116 skipped, 352 deselected, 5 errors` with all five errors
being `OSError: [Errno 28] No space left on device` in
`tests/corpus/test_ingest.py`. The cause is the host's `/tmp` (a 3.9 GiB
tmpfs) filling up, not a test failure — **no OCR test errored or failed in
that run**, and the isolation test passed there too. The run was repeated
with `--basetemp` on the large filesystem (`/`, 902 GiB free) to remove
the environmental variable. `--basetemp` changes only where pytest puts
per-test scratch directories, so the second run is the admissible one.

## Limitations

**L1 — the two full-suite runs that were in flight during the mutation
probe were discarded and re-run.** A `uv run --no-sync pytest -q` and a
gold+ocr run were started before the probe was introduced and overlapped
it, so neither is admissible evidence for any row above. Only runs
started after `src/fin/corpus/ocr.py` was restored byte-exact are
reported. The RED reproduction, by contrast, predates the probe and is
clean evidence: it was run on the unmodified tree.

**L2 — a subprocess-based assertion is not the same measurement as an
in-process one, and the difference is deliberate.** The new test no longer
observes the pytest process's `sys.modules`, so it cannot catch a *sibling*
that pollutes the suite's import state. It is not meant to: the contract
is about `fin.corpus.ocr`'s own import graph. The suite-wide consequence
of a leaky sibling is now the sibling's own responsibility
(`tests/host/test_workspace_http_attachment_lane.py`'s autouse cleanup
fixture), and this record does not weaken or remove that fixture.

**L3 — the `chi_sim` traineddata is present on this host, so the shipped
default `chi_sim+eng` path is what the gold run exercised.** The gold
cases themselves assert English quotes; they do not assert Chinese
recognition quality. That is a property of the committed gold set
(`manifest.json` `known_gaps` item 4), not of this change, and is not
altered by it.

**L4 — nothing outside `tests/corpus` was run live.** No Postgres,
SeaweedFS, vision-provider, `pg_es`, or `jev_live` leg was exercised,
and none is required by this fix: the contract under repair is a pure
import-graph property with no service, storage, model, or MCP boundary in
it.

Validation:
- Not run: the `pg_es` / `jev_live` / live-store legs.
- Expected command: the commands in
  `docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md`'s
  *Commands* table.
- Blocking issue: unchanged by this work and out of its scope; this fix
  crosses no real external boundary, so per `.claude/rules/test.md` the
  hermetic default suite plus the real-binary gold run are the applicable
  acceptance and both are recorded above.
