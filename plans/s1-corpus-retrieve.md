# Plan: s1-corpus-retrieve

**Goal:** S1 research increment: ingest-owned official HTML corpus, EDGAR-shaped catalog, MCP retrieve, ingest OCR, and XBRL fact lookup with a numeric gate — default pytest still fake-port, no second ranking stack in fin.
**Approach:** Local ingest first so catalog and facts exist without SEC. Then filters and MCP retrieve can proceed in parallel. OCR and the fact tool hang off ingest. Contract remaining (unavailable EXITs, overlapping retrieve) lands with those bullets, not a leftover “polish” ticket.
**Spec link:** `specs/s1-corpus-retrieve.md`
**Parent map:** the tracked change
**Tracker:** GitHub `winter6205/fin` (main path). Spec the tracked change ; T1–T5 are #74–#78 with native blocking edges (T2/T3/T4/T5 ← T1).
**ACR:** all-yes after spec patch (copied below; writing-plans owns commit split)

bounded-context-guardian: yes — filing freeze/catalog/facts stay in `corpus`; ranking client stays behind retrieve port in `retrieve`; graph bind stays in `host`; iknow-memory remains a sibling MCP, no `src/fin` vendoring; no `controllers/`/`services/` layer packages.
defensive-contract-validator: yes — empty/negative/overflow named on filters+port; concurrent = overlapping retrieve + overlapping fact lookup (ingest/OCR N/A single-writer); exception = UA / retrieve_unavailable / sec_http_fail / ocr_required_unavailable; workspace 409 is not a substitute class.
error-handling-enforcer: yes — named EXITs retrieve_unavailable (no BM25), sec_http_fail, ocr_required_unavailable, UA-no-request, mapper-drop, fact-miss dict, unknown_locator; no empty catch swallowing ingest failure.
complexity-anti-drift: yes — ingest CLI vs ask path stay split; retrieve adapter does not inline OCR/XBRL/parse; fact lookup is not a second retriever; no LlamaIndex god-ingest in fin.
minimal-change-verifier: yes — one S1 ship contract; writing-plans splits tracer-bullet commits; no auth/universal-parse-in-fin/watchlist scope creep.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** after T5, one code-review pass on the whole S1 diff (not per-commit during WIP). Live SEC HTTP stays out of default `uv run pytest -q`.

**待写入:** (empty — CONTEXT terms and ADR 0028–0031 already flushed)

## Tasks (ordered by dependency)

1. **Local ingest writes live catalog + fact table without SEC** — tag: `[implementation]` (#74)
   - **Inherits:** spec ingest / ADR 0028 HTML unit / ADR 0029 product vs eval trees / ADR 0031 live row always CIK, form, accession, filing_date; UA unset → no SEC request; `sec_http_fail` when UA set and HTTP fails; existing `AAPL-2023Q3` tree is enough for the default path; ingest/OCR concurrent write N/A single-writer
   - **Surface:** `corpus` (+ CLI entry the implementer names)
   - **Acceptance:** CLI over existing corpus updates catalog fields for `AAPL-2023Q3` without SEC GET; unset `FIN_EDGAR_USER_AGENT` never opens SEC hosts; simulated live fetch with UA + HTTP fail exits non-zero and does not mark that accession complete; ingest can persist a fact table row the later lookup tool can read
   - Status: [ ] pending

2. **[parallel] Catalog and retrieve filters are EDGAR-shaped** — tag: `[implementation]` (#75)
   - **Inherits:** ADR 0031: keep `company`/`period`; additive optional `cik`/`form`/`ticker`/exact `filing_date`; unknown or conflicting filters → empty hits not exceptions; `company` is not forever ticker
   - **Surface:** `corpus` catalog + `host` `list_files` + retrieve port extra kwargs
   - **Acceptance:** `list_files(company="AAPL")` still v1-shaped; unknown `cik` and conflicting `ticker`+`cik` → `{filings:[]}`; `form` is not hard-coded to demo 10-Q when other form rows exist; empty filter values behave as empty hits
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] with T3 after T1

3. **[parallel] Product retrieve is MCP; BM25 archived; fake port in default pytest** — tag: `[implementation]` (#76)
   - **Inherits:** ADR 0030 / retrieve port empty-miss; mapper-drop omits that hit; `retrieve_unavailable` on MCP down (not `{chunks:[]}`, not BM25); default pytest fake port, no live iknow-memory; overlapping retrieve calls do not raise
   - **Surface:** `retrieve` + `host` `compile_graph` bind
   - **Acceptance:** product `compile_graph` (non-test) does not bind in-process BM25; fake-port empty query / unknown `document_id` / mapper-drop → `{chunks:[]}` without raise; scripted MCP-down → `{error: "retrieve_unavailable"}`; two overlapping retrieve calls each return chunks-or-`retrieve_unavailable`; `uv run pytest -q` still exits 0 without talking to SEC or a live MCP process
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] with T2 after T1

4. **[parallel] Algorithmic ingest OCR write-back** — tag: `[implementation]` (#77)
   - **Inherits:** spec: detect collapsed/no text-layer + images → write text into frozen unit + `ocr_applied`; `ocr_required_unavailable` when OCR is required and engine/extra missing or OCR fails (no successful empty unit); not vision as fact source
   - **Surface:** `corpus` ingest
   - **Acceptance:** OCR-available fixture writes non-empty frozen text and records `ocr_applied`; OCR-required + engine unavailable fails that unit; default pytest does not require a live OCR engine if the miss path is scripted
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] with T2 / T3 after T1

5. **`get_xbrl_fact` lookup + numeric gate** — tag: `[implementation]` (#78)
   - **Inherits:** ADR 0031 / CONTEXT **get_xbrl_fact**: ingest-built tagged fact lookup, not a retriever; miss → structured error dict; numeric gate vs claimed value; embeddings must not author the number; overlapping lookups independent; non-research skips the fact gate
   - **Surface:** `corpus` facts + `host` research tools / research done path
   - **Acceptance:** miss returns error dict without throwing out of the loop; research `done` does not pass when a claimed numeric fact has no matching evidenced row; two overlapping lookups do not raise; `uv run pytest -q -m soft_track` still exits 0
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] with T2 / T3 / T4 after T1
