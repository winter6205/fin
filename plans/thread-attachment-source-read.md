# Plan: thread attachment retrieval and source reads (fin)

**Goal:** A research thread can retain, search, read, and cite its own attachments while company-source reads remain bounded through iknow-memory.
**Approach:** Keep delivered T1 storage, replace upload-time parsing with raw-only upload and graph-triggered page/region preparation, and implement the confirmed Heron + CPU Tesseract + configured-vision pipeline. Set resource bounds from real attachment gold results on the 4 GB target before model-dependent implementation; then preserve the existing source-selection, bounded-read, and typed `unit` citation contracts.
**Spec link:** [`specs/thread-attachment-source-read.md`](../specs/thread-attachment-source-read.md)
**Sibling plan:** iknow-memory `plans/bounded-source-read.md`; its T1 blocks this plan's T4. First wave: this plan's T2a runs in parallel with permanent-corpus-upload T1 and iknow-memory corpus-index-job-orchestration T1.
**ACR:** all five yes (copied below from the spec).
**Per-ticket loop (implementation bullets):** tdd → targeted typecheck and tests. After implementation bullets land, run one code-review phase, repair BLOCKED findings, then verification-before-completion and the project commit rule. T2a is measurement/decision work plus the config-validation seam that makes its decided numbers enforceable.
**Golden-set gate (all bullets):** add or update committed cases for changed behavior, run the relevant gold set with the real boundaries that behavior crosses, and record actual per-case results. A missing live prerequisite blocks completion under `.claude/rules/test.md`; scripted/Fake tests are supplemental.

bounded-context-guardian: yes — fin owns attachment bytes, parsing, visual derivatives, and thread-private indexing; iknow-memory remains the company-source boundary.
input-contract-tests: yes — upload/send gating, lifecycle states, page and vision budgets, deletion races, locator bounds, and backend failures have explicit acceptance cases.
error-handling-enforcer: yes — parser, vision, index, budget, upload, and deletion-cleanup failures have typed exits; tombstones continue blocking access during cleanup retry.
complexity-anti-drift: yes — graph nodes orchestrate waits and evidence reads while the fin attachment capability owns parsing, OCR, vision, and indexing.
minimal-change-verifier: yes — scope preserves corpus isolation and company ranking, removes legacy compatibility, and requires real fin/iknow-memory gold evidence.

OVERALL: PASS

## Settled contract and persist list

- Inherits the spec's typed source locators, thread-bound attachment ownership, raw-only `uploaded` state, graph-triggered `parsing`/`ready`/`failed` lifecycle, 8,000-code-point and 32-KiB limits, and this-run text/visual evidence rules (ADRs 0046 and 0083–0089).
- Open for implementation within those constraints: durable job/index schema, parser adapter decomposition, and UI placement for readiness and retry. T2a's measured deployment settings are recorded in [`docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md`](../docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md): a 4 MiB upload cap, an 80-page cap, and a currently recorded per-run budget of 88 visual calls of which up to 8 are crops. T2e0 now sets the retry reserve: at those page/crop caps, T2e must raise the total to 89 and enforce it in deployment configuration and runtime validation. These are deployment settings, not code constants.
- **To persist:** one item, already written. T2a added
  [`docs/context-map.md`](../docs/context-map.md), which names the bounded
  contexts, their ownership edges and the legal dependency direction for
  `src/fin/`; it defines no term and consumes its vocabulary from
  `docs/CONTEXT.md`. The approved vocabulary and lifecycle updates are already
  in `docs/CONTEXT.md` and ADRs 0084, 0086, 0088, and 0089.
- Existing pre-G4 gold output and its limits are recorded in [`docs/validation/2026-09-24-g4-planning-baseline.md`](../docs/validation/2026-09-24-g4-planning-baseline.md); it does not close any G4 real-gold criterion.

## Tasks (ordered by dependency)

1. **Durable text attachment through a thread** — tag: `[implementation]`
   - **Inherits:** delivered T1 historical acceptance: "Fin owns each thread's attachment source and index" and "`ready` means both text and index are usable"; ADR 0083 keeps the existing binary attachment door and separate corpus ingest. T2 supersedes T1's upload-time parse trigger and extends current `ready` to include required visual observations.
   - **Surface:** fin thread attachment capability, object store, thread store, workspace HTTP and web attachment state.
   - **Acceptance:** upload a UTF-8 text file as bytes; the API returns a logical attachment ID and `parsing`, then `ready` only after searchable index data exists; the same thread can inspect status on another turn; process restart retains bytes and parsed version and restores index availability before `ready`; deleting the thread removes metadata, bytes, parsed units, and index entries. A concurrent delete and preparation cannot resurrect the index; cleanup failure is typed, observable, and retriable rather than acknowledged as complete. The existing one-use attachment assertion is replaced by the thread-lifetime contract.
   - Status: [x] delivered — merged to `main` as `5de7120` (that pull request); per-clause evidence in
     [`docs/validation/2026-09-25-g4-t1-thread-attachment.md`](../docs/validation/2026-09-25-g4-t1-thread-attachment.md)

2. **T2a — Measure and decide page and visual-call defaults** — tag: `[decision]`
   - **Inherits:** spec: “The plan must set the initial finite defaults before implementation using the real gold suite and a 4 GB device measurement.” The first preparation scans every physical page once; up to eight crop inspections and one full-page transport retry per run consume the same per-run call budget.
   - **Surface:** fin attachment preparation, the committed G4 attachment gold cases, Docling/Heron, CPU Tesseract, fin's configured vision provider, and the 4 GB target device.
   - **Acceptance:** commit representative mixed text/chart PDF, scanned-page, and image gold cases with independent page/region, OCR, chart-presence, and chart-meaning expectations; record the real file-size distribution and run the pinned candidate pipeline, capturing actual per-case results, image dimensions, peak host/GPU memory, latency, and provider usage. *(Scoped after review, 2026-09-26: "per-case" covers the two PDF cases — the pinned harness takes PDF input only, and its one attempt at the bare image is a committed rejection with* `container_exit_code = 1`*, while that image's OCR side is measured and committed as* `results/g4-image-chart-001_tesseract_…/ocr.txt`*. The image case's pinned-pipeline per-case result is T2d's, whose acceptance names the standalone-image gold case.)* Select finite upload-byte, PDF-page, crop-call, and total-call caps so every gold file fits. Each admitted physical page receives one initial full-page call; all calls, including failed attempts, count against the run total. T2e0 sets one full-page transport retry for the entire run and retains the crop cap of eight, so the required total is `80 + 8 + 1 = 89` at the current 80-page cap. This is a single run-wide retry reserve, not one retry per page. T2e implements and enforces this relationship; T2a's existing `88` setting is a historical measured value, not the settled runtime budget. Missing, non-positive, or unbounded configured limits fail validation. An over-budget request fails with its typed limit error before any page is silently skipped. Record the measured defaults and evidence before model-dependent implementation proceeds; do not substitute a guessed or hardcoded limit.
   - Status: [x] decided — **provisional**, from partial measurement with named
     gaps: `FIN_ATTACHMENT_MAX_BYTES=4194304`,
     `FIN_ATTACHMENT_MAX_PDF_PAGES=80`, `FIN_ATTACHMENT_VISION_CALLS_PER_RUN=88`,
     `FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN=8`, persisted as deployment settings
     in `.env.example` (no code default). All three gold cases are committed and fit
     one run; `g4-mixed-chart-001` and `g4-scanned-001` have committed runs under
     the pinned pipeline — the scanned case twice, with content that agrees to the
     character — while the bare image remains outside the harness's PDF-only
     input path. Per-case numbers, the extrapolation to 80 pages, the rejected
     40-page option, and the 4 GB device limitation are in
     [`docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md`](../docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md).
     Five of its *Leftovers* bound this decision rather than polish it, and each
     one has an owning ticket there: the 80-page point was never run at the 4 GiB
     ceiling (1, T2f), scanned-page OCR is quantified at ≈ 21.9 min per page but
     only at a Tesseract configuration nobody chose (4, T2d), no millisecond
     figure is a device specification because the serial re-measure ran at this
     host's load floor of 2.78–3.25 on a memory-bound VM (6, T2d), a bare image
     has no measured preparation cost (7, T2d), and the recorded `88 = 80 + 8`
     has no retry headroom. T2e0 resolves the policy at `80 + 8 + 1 = 89`; T2e
     still must update `.env.example`, boot validation, and run accounting to
     enforce it (9). A per-turn monetary ceiling is deliberately deferred and
     does not block T2e; the provider-reported usage and cost remain recorded
     for later budgeting (2). The record's other items are owned there too: the
     observed crop demand remains visible in the G4 run accounting (8), the
     one-filing-per-turn guidance by T2b (3), the dedicated-device reading by T2f
     (5). T2b–T2f build against these four deployment keys; T2e must replace
     the historical 88-call value with 89 before vision retries are enabled.
     None may present the provisional values as approved shipped defaults until
     1, 4, 6, 7, and 9 close. The enforcement seam is `fin.host.attachment_budgets` (157
     cases).

3. **T2b — Store uploads raw and gate sends** — tag: `[implementation]`
   - **Inherits:** spec: upload stores durable raw bytes only and returns `uploaded`; preparation starts only from an eligible grounded graph turn; no conversation starts or sends while any composer upload is in flight. T1's 2 MiB route constant is historical and is replaced by the finite, measured deployment setting from T2a.
   - **Surface:** fin thread attachment capability, workspace HTTP, and web composer/status.
   - **Acceptance:** upload completion is reported only after durable storage; it does not invoke parsing, OCR, layout, vision, or indexing; unsupported bytes remain uploadable for typed rejection during graph-triggered preparation. The current 2 MiB constant is replaced by T2a's finite measured setting: an upload at the cap is accepted, while declared or streamed bytes over it return named `attachment_too_large` (413) and leave neither a partial object nor attachment row. The composer displays `uploaded` distinctly from `ready`, and both composer and host block a send while any upload is pending, including a file not named in the message. Public-entry tests cover empty/malformed requests, unsupported bytes accepted as raw input, exact and one-byte-over size boundaries, duplicate/concurrent upload completion, and object-store exceptions with typed outcomes. T2b is also the boot-wiring obligation T2a deliberately left unconnected: the composition root reads all four bounds through `fin.host.attachment_budgets.from_environ`, so a missing, non-positive, or unbounded key fails startup, and a caller test asserts that startup aborts rather than serving with no bound. Preserve the delivered T1 validation evidence at [`docs/validation/2026-09-25-g4-t1-thread-attachment.md`](../docs/validation/2026-09-25-g4-t1-thread-attachment.md).
   - `[blocks: T1, T2a]`
   - Status: [x] delivered — merged to `main` as `fd5a1d5` (that pull request); per-clause evidence in
     [`docs/validation/2026-09-26-g4-t2b-raw-upload-send-gate.md`](../docs/validation/2026-09-26-g4-t2b-raw-upload-send-gate.md).
     The configured S3-compatible byte leg was subsequently exercised against a
     local SeaweedFS service: after upload through a real Uvicorn process, a fresh
     process read back the original bytes and SHA-256 (12/12 live door cases;
     see the 2026-09-27 follow-up in the validation record). Deployment-specific
     object-store configuration and service restart durability remain to be
     verified by the ticket owning `FIN_OBJECT_STORE_URL`. `uploaded` also has no
     waiter yet — nothing advances it to `parsing`/`ready` until T2f.

4. **T2c — Tombstone single-attachment deletion** — tag: `[implementation]`
   - **Inherits:** spec: deletion tombstones the attachment version before cleanup; late parse/vision results cannot recreate data or `ready`; failed cleanup remains blocked and retriable.
   - **Surface:** fin thread attachment capability, workspace HTTP, and web attachment chip.
   - **Acceptance:** the chip's `×` calls a single-attachment delete path that invalidates access before deleting bytes and derivatives; late jobs cannot publish units, observations, index entries, or readiness. Cross-thread, unknown, and stale attachment identities return `unknown_locator`; malformed requests have typed validation errors. Repeated deletion, concurrent deletion versus preparation, and cleanup exceptions cannot expose data; cleanup failure returns `attachment_cleanup_failed` while retaining the tombstone for retry. Thread deletion keeps the same invalidation guarantee.
   - `[blocks: T2b]`
   - Status: [ ] pending

5. **T2d — Parse physical pages with Heron and CPU OCR** — tag: `[implementation]`
   - **Inherits:** ADR 0089 and spec: Docling standard PDF pipeline with Heron layout, Tesseract on CPU for scanned text, and per-page/region processing; no whole-file text/scanned dispatch.
   - **Surface:** fin attachment preparation capability, its parser/rendering adapters, and the shared `fin.corpus.parsers` page/region extraction core.
   - **Acceptance:** representative native-text, mixed, scanned, and standalone-image gold cases preserve native text, use Heron for text/table/figure regions and coordinates, run Tesseract on CPU for scanned text, and retain rendered physical-page images for vision. Each PDF page maps to one frozen text unit with physical page and region metadata; mixed pages do not lose native text when OCR is used. The reusable, side-effect-free PDF page/region extraction core is owned by `fin.corpus.parsers`; the thread attachment preparation adapter consumes its result without moving thread lifecycle, private indexing, or visual evidence ownership into corpus. The core does not perform permanent-corpus chunking, push, or graph indexing. The downstream [`permanent-corpus-upload.md`](permanent-corpus-upload.md) plan consumes this core only after T2d lands. The attachment preparation adapter, not the corpus core, maps the core's outcome onto the attachment codes: unsupported input returns `attachment_type_unsupported`; empty extracted text returns `attachment_empty_text`; page overflow returns `attachment_page_limit_exceeded`; render, OCR, and malformed-parser failures are typed, with `render_failed` for a render failure. No local VLM or separate picture-classifier model is loaded. Two quantities measured by T2a constrain this ticket: OCR has to be able to read pixel text inside a figure region on a page that also carries native text (`g4-mixed-chart-001` page 3, 337 native characters, is a committed counterexample to the page-level `detect_ocr_required` threshold at `src/fin/corpus/ocr.py:141`), and Heron's region recall is measured at 0 tables on `g4-mixed-chart-001` page 2 and 1 figure across 72 pages of `2023_Q1_MSFT.pdf`, so the full-page scan rule cannot be replaced by a figure-box-driven one. T2f carries the other half of T2a's deferral: it calls the budget check before any page is scanned and records the page and call counts per run.
   - `[blocks: T2a, T2b, T2c]`
   - Status: [ ] pending

5a. **T2e0 — Decide visual observation reuse and vision spending** — tag: `[decision]`
   - **Inherits:** T2a's provisional page/call measurements and the visual-evidence boundary in ADR 0089.
   - **Surface:** the observation contract, vision-call budgets, provider configuration, and the fin attachment preparation port.
   - **Decision:** the executable observation predicate, conservative unknown handling, one run-wide retry reserve (`80 + 8 + 1 = 89`), and the separate vision port reusing `FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY` are recorded in the spec. A per-turn monetary ceiling is explicitly deferred; it is not a T2e prerequisite, while provider-reported usage/cost remains part of run accounting.
   - `[parallel]` with T2c and T2d; this decision does not wait for their code.
   - Status: [x] decided — issue decision questions resolved 2026-09-27. T2e remains blocked by T2d only; the 89-call configuration and runtime enforcement are implementation obligations of T2e.

6. **T2e — Persist reusable full-page and crop observations** — tag: `[implementation]`
   - **Inherits:** spec and ADR 0089: fin's configured vision provider scans each rendered full page; a later detail request inspects the stored original page/crop only when its observation lacks that detail; observations are versioned derived material with original-image provenance.
   - **Surface:** fin attachment preparation and visual-evidence capability.
   - **Acceptance:** persist a versioned observation envelope with `coverage_schema_version`, `observation_version`, attachment content version, source unit and physical page, original page-image hash, provider/model version, and scope (`whole_page` or normalized page-image bounds). Its `covered_details` entries pair a structured detail key with the scope actually observed. For each visual question, after the candidate observation is explicitly read into the current run, the grounded model supplies structured `required_details` and requested scope. The host reuses evidence only when source/version/hash match, the coverage schema is supported, every required key has an exact covered-key match, and the observed scope contains the requested scope. Value details must bind an exact chart and series and category, or an equivalent complete target key; a generic `plotted_value` facet, missing/ambiguous target, missing detail, or unknown scope is insufficient. The host does no semantic/fuzzy comparison. Insufficient or unknown coverage inspects the stored original at high resolution, using a reliable relevant crop or the full page when target/region is unknown, then persists a new observation version. The first preparation calls vision once per physical page, including pages without Heron figure boxes. T2e supplies an injected fin vision port separate from the conversational model port, configured from the existing `FIN_MODEL`, `FIN_BASE_URL`, and `FIN_API_KEY`; it adds no separate vision credentials/model settings. At most eight crop calls are allowed and one full-page transport retry slot is reserved per run: retry only adapter-classified retryable transport failures, never non-retryable errors, malformed/empty output, or crop calls; every outbound attempt consumes one of the 89 calls at the current 80-page cap. T2e updates `.env.example` to 89 and enforces the page + crop + retry relationship during boot/preflight. No monetary ceiling is required; record provider-reported usage and cost per attempt and run, marking cost unavailable when the provider omits it. Provider failure returns `vision_call_failed`, empty/malformed output returns `vision_empty_text`, and call-budget overflow returns `attachment_vision_budget_exceeded`. No provider/model ID is hardcoded and no local VLM or separate picture classifier is introduced.
   - `[blocks: T2d]`
   - Status: [ ] pending

7. **T2f — Gate preparation through a durable graph job** — tag: `[implementation]`
   - **Inherits:** spec and ADR 0089: after classification/choice handling and compaction, `ensure_attachment_ready` starts or joins preparation only for an eligible attachment; pending work releases the thread lock and resumes the same run after completion.
   - **Surface:** fin attachment capability, host graph/checkpoint, workspace attachment status, and the thread-private index boundary.
   - **Acceptance:** the durable job is keyed by attachment content version, invokes page parsing, vision, and indexing, and reports `ready` only when parsed units, required observations, and the thread-private index are usable. Pending graph work persists the `run_id` and wait state, releases the thread lock, then resumes that run automatically after completion or process restart without asking the operator to resend. Ineligible attachments do not trigger work; concurrent triggers/retries join one active job/version; tombstones prevent late publication. Empty/invalid/stale public attachment references, `attachment_upload_in_progress`, `attachment_page_limit_exceeded`, `attachment_vision_budget_exceeded`, duplicate concurrent starts, restart, and parser/provider/`attachment_index_unavailable`/storage exceptions produce the specified typed result, never an empty-success or stale `ready` state.
   - `[blocks: T2a, T2b, T2c, T2d, T2e]`
   - Status: [ ] pending

8. **T3 — Thread-bound retrieval and mixed source selection** — tag: `[implementation]`
   - **Inherits:** spec: "Ready attachments in the active thread and authorized company material are eligible by default"; naming one narrows the attachment branch while an explicit company request remains eligible.
   - **Surface:** fin retrieve capability and grounded host tool.
   - **Acceptance:** on a later turn, an attachment hit carries `{kind:"attachment",attachment_id,version,unit}`; a query in another thread cannot retrieve it; a model-supplied thread ID cannot widen scope; naming one attachment excludes other attachments, and a comparison request can return it alongside company hits. If an eligible required attachment is `uploaded` or `parsing`, the same run waits for preparation and resumes automatically when ready; parse failures exit with their named typed failure. An index outage is typed unavailable rather than an empty success. Company ranking remains behind the existing port.
   - `[blocks: T1, T2f]`
   - Status: [ ] pending

9. **T4 — Typed bounded source read** — tag: `[implementation]`
   - **Inherits:** ADRs 0087/0088: a known locator can be read directly; one frozen unit per call; `unit` identifies source; `position` is a zero-based Unicode-code-point offset; text ≤8,000 code points and complete serialized response ≤32 KiB. The sibling iknow-memory plan provides bounded company reads before this adapter consumes them.
   - **Surface:** fin read port/adapters, attachment read boundary, grounded host tool and evidence collection.
   - **Acceptance:** direct company and thread-owned attachment locators return bounded contiguous slices with advancing `next_position`; concatenating slices reproduces source text including supplementary-plane characters. Cross-thread and stale attachment locators reveal no text; malformed/oversized backend results yield `source_backend_contract_error`, transport faults yield `source_unavailable`, and invalid positions/locators are typed. Only returned text enters this-run evidence. No model-facing arbitrary path read is introduced.
   - `[blocks: T1, iknow-memory bounded-source-read T1]`
   - `[parallel]` with T3 after T2f and iknow-memory bounded-source-read T1
   - Status: [ ] pending

10. **T5 — Unit citations and legacy contract removal** — tag: `[implementation]`
   - **Inherits:** spec: "all generic `page` locator and citation fields become `unit` across the active product path"; only returned text supports a citation, and adjacent slices join only without an unseen gap. No old `page` compatibility layer.
   - **Surface:** fin locator mapping, citation gate, host/API DTOs, web citation rendering and model tool surface.
   - **Acceptance:** retrieved and read company/attachment evidence can produce `unit` citations end-to-end; PDF units may display as pages, non-PDF units do not; attachment citations show a safe filename but retain ID identity. The verifier rejects a citation carrying another thread's attachment ID or a stale parsed version, even if the quoted text matches. A quote solely in unread text or across an unseen gap fails; a quote across adjacent returned slices passes. For visual claims, the current run explicitly reads the matching persisted observation; the evidence token validates attachment/page/crop provenance and links to the original location. A sufficient observation is reused on later turns without re-upload or a vision call; missing detail triggers inspection of the stored original and a new observation version. A generated observation never becomes a source quote. The model tool list contains `read_source`, not `read_file`; active DTOs and tests contain no generic `page` locator field. `uv run pytest -q`, web `npm run typecheck`, and web `npm test` exit 0.
   - `[blocks: T2e, T2f, T3, T4]`
   - Status: [ ] pending

## End of round

After T1, T2a–T2f, and T3–T5 land, run the committed G4 gold set over the real fin thread/object stores, pinned Docling/Heron and CPU Tesseract, fin's configured vision provider, and live iknow-memory MCP/store path for company-source cases. Reuse the existing company-source gold dataset and the committed attachment cases from T2a. Record expected and actual statuses, page/region coverage, OCR and visual observations, `source_ref`s, text-slice bounds, whether visual evidence was reused or refreshed, citations, page/call budgets, measured resource peaks on the 4 GB device, commands, provider/artifact versions, dataset version, and per-case pass/fail in a validation artifact. Include restart/resume, concurrent preparation, send/upload, delete/parse/vision races, and typed empty/invalid/overflow/concurrent/exception cases at applicable public entries. The current `uv run pytest -q -m mcp tests/retrieve/test_live_research_cite_on_ingest.py` is a useful real-MCP baseline but uses in-memory iknow storage and a scripted model; it cannot replace this G4 gold run. Then run one code-review phase against the spec; repair any BLOCKED findings, run verification-before-completion, and commit by the project rule. No push without operator authorization.
