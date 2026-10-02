# Plan: Reproducible G4 pipeline and device acceptance

**Goal:** Make the G4 PDF pipeline reproducible from declared inputs, then establish its gold and resource behavior on the required 4 GB device.
**Approach:** C1 reconciles the current pin evidence and fixes the build-input contract, retaining the existing exact-image identity requirement unless the operator explicitly changes it. Verify the committed pipeline gold on the current development VM before the integrated browser run. C2's dedicated 4 GB measurement follows C1 in the later target-deployment validation phase; a cgroup ceiling on a larger host cannot close that hardware acceptance.
**Operator phase ruling (2026-09-30):** the current VM is for development and integration, with its actual resource configuration. Dedicated 4 GB hardware is not a prerequisite for this environment preparation or B3's browser acceptance. Retain the target-device measurement as deferred deployment evidence, not a passing result. C1, pipeline gold, and functional/browser acceptance remain required independently; full G4 target-deployment completion retains the original hardware clause.
**Spec links:** [`plans/thread-attachment-source-read.md`](thread-attachment-source-read.md) (T2a, T2f, End-of-round); [`specs/thread-attachment-source-read.md`](../specs/thread-attachment-source-read.md) success criteria 6 and 12; [T2a measurement record](../docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md); [merged that pull request validation record](../docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md) (L5, L7).
**Evidence baseline:** Inspected local `main` at `670f65f`. The validation record reports L7 open. `PIN_MANIFEST.md` records image ID `8e10185f…`, while the committed `results/image_identity.txt` records `0f278c22…`; the committed package snapshot also contains the three drifts named in the L7 record. Reconcile these artifacts before treating either digest as the canonical target.
**ACR:** N/A for this planning-only change; the five lines below do not approve future implementation.
```text
bounded-context-guardian: N/A — plan only; no context boundaries change.
input-contract-tests: N/A — plan only; no executable public contract changes.
error-handling-enforcer: N/A — plan only; no failure paths change.
complexity-anti-drift: N/A — plan only; no executable logic changes.
minimal-change-verifier: N/A — plan only; no dependencies or implementation files change.
```
Apply the architecture and verification gates to the implementation diff. Use the normal failing-first test, focused checks, one required code-review phase, repair if blocked, and verification-before-completion. This plan does not change the project runtime dependency declaration (`pyproject.toml` / `uv.lock`).

## Tasks (ordered by dependency)

1. **C1 — Make the pipeline build reproducible from its pin** — tag: `[implementation]`
   - **Inherits:** L7's recorded acceptance: “a rebuild from the pin reproduces the recorded `sha256` byte-for-byte, with versions pinned mechanically rather than in prose”; `PIN_MANIFEST.md` keeps Docling outside fin's declared runtime dependencies.
   - **Surface:** Existing `scripts/g4_pipeline` build inputs, identity snapshots, pin manifest, and reproducibility checks.
   - **Acceptance:** Reconcile the manifest and committed identity snapshot, then mechanically pin and verify every input that determines the image or measured pipeline, including the base image, OS packages, the complete Python resolution and wheel sources/hashes, builder/target metadata, and externally fetched model revisions and content hashes. Account for timestamps and mutable network references so two clean builds from the same declared inputs produce the required byte-identical image identity. Separately identify runtime-mounted models, source revision, and gold bytes because they do not contribute to the image digest. Preserve the current L7 exact-digest requirement: if the recorded `8e10185f…` identity cannot be reproduced, record the concrete cause and leave C1 blocked for an explicit operator decision; do not silently replace the expected digest or weaken the contract. A changed tool/model version may establish a new baseline only after that decision, with its own immutable inputs and measured-output expectations.
   - **Completion:** A clean, independently repeated build proves the agreed identity and emits artifacts whose package, OS, base, builder, model, and source provenance can be checked without prose-only pins; any unresolvable exact-digest conflict remains an explicit decision blocker.
   - Status: [ ] pending

2. **C2 — Complete pipeline gold and resource acceptance on the 4 GB device** — tag: `[implementation]`
   - **Inherits:** Spec success criteria 6 and 12 require the pinned Docling/Heron and CPU Tesseract path, versioned gold, expected-versus-observed results, and resource peaks on the 4 GB target. T2a/T2f require the 80-page point to be measured before its page and call caps are treated as shipped defaults.
   - **Surface:** `scripts/g4_pipeline` measurement harness and results, committed G4 gold inputs and expectations, T2a/T2f validation records, and the corresponding deployment bounds only if measurements require their established adjustment.
   - **Acceptance:** After C1, run the committed native-text, mixed, and scanned PDF gold through the pinned harness on dedicated 4 GB hardware, plus the required 80-page attachment at the current caps. Verify the image case retains the harness's documented PDF-only rejection and is not counted as an image-pipeline pass; its product-path result belongs to the integrated G4 acceptance. Compare each run with independent committed expectations and record revision, dataset and model hashes, command, exit code, page/region and OCR outcomes, device identity, peak resource use, and per-case result. If the 80-page case exceeds the device limit, lower the page and call budgets together under the existing T2a rule and repeat the affected gold run. If the required device is unavailable, record the expected command and missing prerequisite as blocked evidence; do not substitute a larger host with cgroup limits or close C2. Do not require byte-identical telemetry such as timestamps or observed resource peaks; preserve the gold contract and report measured variance explicitly.
   - **Completion:** The versioned pipeline gold and 80-page case have passing, independently comparable results from the real target device, or C2 remains explicitly blocked with its missing hardware prerequisite and recorded evidence.
   - [blocks: C1]
   - **Dependency:** B3 requires C1 and pipeline gold on the current VM; C2's dedicated-device leg is deferred to target-deployment validation under the operator phase ruling above. Pipeline-only results do not prove model, storage, SQL, or browser behavior.
   - Status: [ ] pending

**Persist list:** Empty. No domain term or ADR decision is introduced by this plan.
