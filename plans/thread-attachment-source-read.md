# Plan: thread attachment retrieval and source reads (fin)

**Goal:** A research thread can retain, search, read, and cite its own attachments while company-source reads remain bounded through iknow-memory.
**Approach:** Establish one durable text-attachment slice, then add classified asynchronous preparation and retrieval selection. Migrate the known-locator read and citation path to typed `unit` identities after the iknow-memory bounded-read wire lands; remove the development-stage `read_file`/`page` contract in this round.
**Spec link:** [`specs/thread-attachment-source-read.md`](../specs/thread-attachment-source-read.md)
**Sibling plan:** iknow-memory `plans/bounded-source-read.md`; its T1 blocks this plan's T4.
**ACR:** all five yes (copied below from the spec).
**Per-ticket loop (all bullets):** tdd → targeted typecheck and tests. After all bullets, run one code-review phase, repair BLOCKED findings, then verification-before-completion and the project commit rule.
**Golden-set gate (all bullets):** add or update committed cases for changed behavior, run the relevant gold set with the real boundaries that behavior crosses, and record actual per-case results. A missing live prerequisite blocks completion under `.claude/rules/test.md`; scripted/Fake tests are supplemental.

bounded-context-guardian: yes — thread attachment indexing stays in fin; company source storage stays in iknow-memory; source-specific adapters cross the boundary.
input-contract-tests: yes — success criteria include empty, invalid, overflow, concurrent, and exception classes, plus locator and cursor failures.
error-handling-enforcer: yes — failure paths map to structured codes, with source-language `EXIT` comments for early returns and fallbacks.
complexity-anti-drift: yes — capability allocation keeps parsing, indexing, and slicing out of HTTP routes and graph nodes.
minimal-change-verifier: yes — one G4 outcome excludes accounts, permanent ingest, new ranking, and legacy compatibility.

## Settled contract and persist list

- Inherits the spec's typed source locators, thread-bound attachment ownership, asynchronous `parsing`/`ready`/`failed` lifecycle, 8,000-code-point and 32-KiB limits, and this-run evidence rule (ADRs 0046 and 0083–0088).
- Open for implementation within those constraints: durable schema and derived-index engine; parser adapter decomposition; UI placement for readiness and retry.
- **To persist:** empty. The spec-driven pass updated `docs/CONTEXT.md` and ADRs 0083/0085/0086 before this plan.
- Existing pre-G4 gold output and its limits are recorded in [`docs/validation/2026-09-24-g4-planning-baseline.md`](../docs/validation/2026-09-24-g4-planning-baseline.md); it does not close any G4 real-gold criterion.

## Tasks (ordered by dependency)

1. **Durable text attachment through a thread** — tag: `[implementation]`
   - **Inherits:** spec: "Fin owns each thread's attachment source and index" and "`ready` means both text and index are usable"; ADR 0083 keeps the existing binary attachment door and separate corpus ingest.
   - **Surface:** fin thread attachment capability, object store, thread store, workspace HTTP and web attachment state.
   - **Acceptance:** upload a UTF-8 text file as bytes; the API returns a logical attachment ID and `parsing`, then `ready` only after searchable index data exists; the same thread can inspect status on another turn; process restart retains bytes and parsed version and restores index availability before `ready`; deleting the thread removes metadata, bytes, parsed units, and index entries. A concurrent delete and preparation cannot resurrect the index; cleanup failure is typed, observable, and retriable rather than acknowledged as complete. The existing one-use attachment assertion is replaced by the thread-lifetime contract.
   - Status: [ ] pending

2. **Classified async preparation and retry** — tag: `[implementation]`
   - **Inherits:** ADR 0084 closed type set `text`, `pdf-text`, `pdf-scanned`, `image`, `unsupported`; ADR 0086 asynchronous parse/index, `attachment_not_ready`, named render/vision failures and retry.
   - **Surface:** fin attachment preparation, workspace HTTP status/retry, and web attachment display.
   - **Acceptance:** text, PDF-text, scanned PDF, and image fixtures become searchable frozen units through their selected parser paths; unsupported bytes receive `attachment_type_unsupported`; empty parsed text and render/vision failures enter named `failed` states; retry can move a failed item to `ready`; concurrent retries create at most one active index version. A restart during preparation never reports stale `ready` and can resume or rebuild.
   - `[blocks: T1]`
   - Status: [ ] pending

3. **Thread-bound retrieval and mixed source selection** — tag: `[implementation]`
   - **Inherits:** spec: "Ready attachments in the active thread and authorized company material are eligible by default"; naming one narrows the attachment branch while an explicit company request remains eligible.
   - **Surface:** fin retrieve capability and grounded host tool.
   - **Acceptance:** on a later turn, an attachment hit carries `{kind:"attachment",attachment_id,version,unit}`; a query in another thread cannot retrieve it; a model-supplied thread ID cannot widen scope; naming one attachment excludes other attachments, and a comparison request can return it alongside company hits. A required pending attachment is typed not-ready; an index outage is typed unavailable rather than an empty success. Company ranking remains behind the existing port.
   - `[blocks: T1]`
   - `[parallel]` with T2 after T1
   - Status: [ ] pending

4. **Typed bounded source read** — tag: `[implementation]`
   - **Inherits:** ADRs 0087/0088: a known locator can be read directly; one frozen unit per call; `unit` identifies source; `position` is a zero-based Unicode-code-point offset; text ≤8,000 code points and complete serialized response ≤32 KiB. The sibling iknow-memory plan provides bounded company reads before this adapter consumes them.
   - **Surface:** fin read port/adapters, attachment read boundary, grounded host tool and evidence collection.
   - **Acceptance:** direct company and thread-owned attachment locators return bounded contiguous slices with advancing `next_position`; concatenating slices reproduces source text including supplementary-plane characters. Cross-thread and stale attachment locators reveal no text; malformed/oversized backend results yield `source_backend_contract_error`, transport faults yield `source_unavailable`, and invalid positions/locators are typed. Only returned text enters this-run evidence. No model-facing arbitrary path read is introduced.
   - `[blocks: T1, iknow-memory bounded-source-read T1]`
   - Status: [ ] pending

5. **Unit citations and legacy contract removal** — tag: `[implementation]`
   - **Inherits:** spec: "all generic `page` locator and citation fields become `unit` across the active product path"; only returned text supports a citation, and adjacent slices join only without an unseen gap. No old `page` compatibility layer.
   - **Surface:** fin locator mapping, citation gate, host/API DTOs, web citation rendering and model tool surface.
   - **Acceptance:** retrieved and read company/attachment evidence can produce `unit` citations end-to-end; PDF units may display as pages, non-PDF units do not; attachment citations show a safe filename but retain ID identity. The verifier rejects a citation carrying another thread's attachment ID or a stale parsed version, even if the quoted text matches. A quote solely in unread text or across an unseen gap fails; a quote across adjacent returned slices passes. The model tool list contains `read_source`, not `read_file`; active DTOs and tests contain no generic `page` locator field. `uv run pytest -q`, web `npm run typecheck`, and web `npm test` exit 0.
   - `[blocks: T2, T3, T4]`
   - Status: [ ] pending

## End of round

After T1–T5 land, run the committed G4 gold set over the real fin thread store/object store and live iknow-memory MCP/store path, plus live vision/model providers for dependent cases. Reuse the existing company-source gold dataset and add committed attachment cases. Record actual statuses, `source_ref`s, text-slice bounds, citations, commands, dependency configuration, dataset version, and per-case pass/fail in a validation artifact. The current `uv run pytest -q -m mcp tests/retrieve/test_live_research_cite_on_ingest.py` is a useful real-MCP baseline but uses in-memory iknow storage and a scripted model; it cannot replace this G4 gold run. Then run one code-review phase against the spec; repair any BLOCKED findings, run verification-before-completion, and commit by the project rule. No push without operator authorization.
