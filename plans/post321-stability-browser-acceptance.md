# Plan: Post-321 stability acceptance

**Goal:** Restore a green hermetic test baseline and remove live attachment graph-gate flakiness.
**Approach:** Fix the two inherited test gaps in isolated work. B1 and B2 can proceed independently with separate module ownership.
**Coordination:** [`plans/post321-parallel-program.md`](post321-parallel-program.md)
**Spec / acceptance sources:** [`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md`](../docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md) (L2 and L3).

**ACR:** N/A for this documentation plan only; each implementation task must run the architecture gate against its actual change scope before coding.

```text
bounded-context-guardian: N/A — this artifact changes no module boundaries.
input-contract-tests: N/A — this artifact changes no executable public contract.
error-handling-enforcer: N/A — this artifact adds no executable failure path.
complexity-anti-drift: N/A — this artifact adds no executable logic.
minimal-change-verifier: yes — this artifact owns only the B stability lane plan.
```

**Per-ticket loop for code changes:** failing-first test → targeted tests and checks → one code-review phase for the new work → repair blocked findings → verification-before-completion. The earlier that pull request second-review waiver applies only to that merged PR; it does not waive review of these changes. Do not weaken assertions or turn required live evidence into a skip.

## Tasks (ordered by dependency)

1. **B1 — Restore the default OCR import-isolation contract** — tag: `[implementation]`
   - **Inherits:** The default `uv run pytest -q` suite is hermetic and must exit 0. Importing `fin.corpus.ocr` does not import optional `pytesseract`; the real Tesseract gold coverage remains meaningful when the optional extra and binary are installed. Preserve the existing assertions and real-engine cases.
   - **Surface:** `tests/corpus` OCR import contract and optional real-binary gold coverage.
   - **Acceptance:** With the optional OCR package installed, the import-isolation assertion remains independent of pytest collection order and the default `uv run --no-sync pytest -q` exits 0. The opt-in `uv run --with pytesseract -q pytest tests/corpus/test_ocr_tesseract_gold.py` still exercises the committed OCR gold with the real binary and passes; no affected case is deleted, weakened, or silently skipped.
   - **Completion:** Record the root cause, the default-suite result, and the real OCR gold result at the implementation revision.
   - Status: [ ] pending
   - [parallel with B2]

2. **B2 — Stabilize the live attachment graph gate** — tag: `[implementation]`
   - **Inherits:** L2 reports order/timing failures in `tests/host/test_live_attachment_graph_gate.py` across 2 of 3 module runs, while each observed individual failure passed in isolation. A clean isolated rerun does not establish module stability. The live gate must keep its real process, socket, Postgres, durable object-store, pause/restart/resume, and cleanup assertions.
   - **Surface:** The live attachment graph-gate test module and only the fixtures/process lifecycle it owns.
   - **Acceptance:** Identify and correct the demonstrated timing, ordering, or job-isolation cause. Run the complete live module three consecutive times against a disposable unique Postgres database and a unique object-store bucket, with unique/reserved ports per run and zero skips; every case passes each run. Record process IDs and prove all processes created by the test are stopped. Cleanup may terminate only processes the test created; never use broad process-kill commands or touch unrelated jobs. A missing live prerequisite is blocked evidence, not a pass.
   - **Existing command:** `FIN_CHECKPOINT_URL=<disposable-db> FIN_OBJECT_STORE_URL=<endpoint> FIN_OBJECT_STORE_BUCKET=<unique-bucket> uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_graph_gate.py` (add the configured endpoint variable required by the selected object-store adapter without recording credentials).
   - **Completion:** Three consecutive full-module runs pass at one recorded revision, with no cross-run rows/objects or owned child processes left behind.
   - Status: [ ] pending
   - [parallel with B1]

## Planning baseline

The coordination plan records the hermetic gold baseline on local `main` at `28fa4da`: `uv run --no-sync pytest -q -m eval tests/eval/test_gold.py` → **9 passed in 1.25 s**, exit 0. This scripted baseline is not evidence that B1's OCR import-isolation and real-binary gold acceptance passes, or that B2's live graph gate is stable. Recheck the current merged baseline and prerequisites before implementation.
