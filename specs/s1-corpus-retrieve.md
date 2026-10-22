# Spec: s1-corpus-retrieve

## Objective

Evolve **fin** from the demo frozen-12 PDF path into the S1 research increment: official-filing **product corpus** (and a pipeline-pinned **eval fixture**), research tools that catalog like EDGAR, narrative retrieve via **iknow-memory** MCP, algorithmic ingest OCR, and `get_xbrl_fact` lookup with a numeric gate. The operator uses the existing workspace / CLI **research thread**. Success is default `uv run pytest -q` green with a fake retrieve port, opt-in MCP/OCR/live-fetch tests, and no second ranking stack in `src/fin`.

Quoted from `docs/CONTEXT.md`:

- **fin**: A normal conversational agent whose durable runtime is a LangGraph host graph: each turn is routed to a **track**; only the **research track** specializes in the frozen 10-Q corpus with tools and the citation verifier. Retrieve ranking stays off-graph behind the retrieve port.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out. Same shape as iknow-memory `retrieve`, regardless of transport.
- **retrieve adapter**: The product retrieve implementation behind the retrieve port: an MCP client to **iknow-memory**. Hits map back to fin locators as `source_ref`. In-process BM25 is archived (ADR 0007 superseded by 0030). Retrieve chunking (including later parent/child or section splits) is owned by fin — the retrieval service stores and returns pushed text as-is with no second splitter (ADR 0037 re-rules the ADR 0030/0032 chunking clauses).
- **iknow-memory**: The existing TypeScript MCP memory server (separate repo). It owns vector ingest and cosine / hybrid / RRF ranking. fin v1 is an independent system with its own retrieve adapter; a later adapter may call iknow-memory over MCP without changing the host graph.
- **ingest**: A CLI that writes frozen locator-unit text from official filings (or the existing extracted tree) into an extracted tree. Sliding-window chunking for retrieve is fin's responsibility, not the retrieval service's (ADR 0037 re-rules ADR 0030); it is not fin's ranking stack, which stays outside fin. The ask path does not fetch EDGAR or open source filings.
- **product corpus**: The operator-selected local extracted tree of official 10-Q and 10-K primary HTML. `fin-serve` retrieve, `read_file`, and the citation verifier read this tree under the live locator contract.
- **eval fixture**: A git-pinned snapshot of **ingest** output, same locator contract as **product corpus**. Default CI contract eval reads only this tree.
- **locator**: Cite identity `{document_id, version, unit}`: for the frozen 12, `version` is the PDF sha256 and `unit` is the 1-based pdfplumber `page`; for live EDGAR, `version` hashes the official primary iXBRL `.htm` bytes and `unit` is a deterministic frozen partition of that HTML's stripped text, not an SEC or unofficial-PDF page. The claimed span is a quote string, not a locator field; the citation verifier checks it is a contiguous substring of that unit's frozen text; chunks carry the locator as `source_ref` and are not the identity.
- **list_files**: Catalog tool over **filings** in the extracted tree, keyed like EDGAR submissions: CIK, form, accession, filing_date always exist on a live row; ticker and fiscal period are optional. v1 callers still pass `company` / `period`; additive optional `cik` / `form` / `ticker` / `filing_date`; hits stay `{document_id, company, period, version, page_count}` until output keys are extended; unknown or conflicting filters yield `filings: []`.
- **get_xbrl_fact**: Research-track **lookup** of one tagged financial fact from an ingest-built fact table (concept, period, issuer/document). XBRL is the reporting format, not a website. A numeric citation gate matches the claimed value to that row; the model does not invent the arithmetic. Embeddings may later resolve concept aliases; they do not author the number.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **agent loop**: On the **research track**, the model repeatedly picks `list_files`, `retrieve`, `read_file`, or `get_xbrl_fact` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Research `done` is only after the citation verifier (and numeric fact gate when a fact is claimed) passes; non-research tracks may `done` without tools or citations. Step order is not hardcoded.

## Boundaries

- **Does:**
  - Ingest CLI: simulated/local path over existing `data/corpus` extracted tree; live EDGAR fetch of official primary iXBRL `.htm` only when `FIN_EDGAR_USER_AGENT` is set (shape `fin <email>`); missing UA → no request to SEC hosts.
  - Live cite unit = official HTML freeze (ADR 0028); eval fixture = same ingest pipeline pin (ADR 0029); first intended pin AAPL+TSLA 10-Q/10-K × two periods when live fetch is used.
  - Product retrieve = MCP client to **iknow-memory** behind the retrieve port (ADR 0030). Archive in-process BM25 from the product compile path. Default pytest uses a fake RetrievePort, not product BM25 and not live MCP.
  - MCP hits map to fin **locator**; mapping failure drops the hit (`chunks: []` contribution), never passes iknow ids to the citation gate.
  - Additive **list_files** / **retrieve** filters per ADR 0031: keep `company`/`period`; optional `cik`, `form`, `ticker`, exact `filing_date` when present. Empty hits not exceptions. CIK is canonical issuer key; ticker not required.
  - Bind `get_xbrl_fact` on research track; ingest-built fact table; numeric gate when a fact is claimed. Embeddings must not author the number.
  - Algorithmic ingest OCR: detect collapsed/no text-layer + images → write text into the frozen unit; `ocr_applied` on manifest. Not vision as fact source.
  - Host graph stays one compiled graph (ADR 0013); Soft-Track tracks unchanged; non-research still skips citation and fact gates.
  - Typed EXITs (no empty catch, no BM25 fallback):
    - **retrieve_unavailable**: product MCP transport/process down or protocol fail → retrieve tool returns `{error: "retrieve_unavailable"}`; does not return `{chunks:[]}` as if a miss; does not bind archived BM25 (ADR 0030).
    - **sec_http_fail**: UA is set but live SEC HTTP fails → ingest CLI non-zero exit; no catalog row marked complete for that accession.
    - **ocr_required_unavailable**: unit needs OCR (collapsed/no text-layer + images) and engine/extra is missing or OCR fails → ingest does not succeed that unit with empty frozen text; typed non-zero / error, not silent skip.
  - Contract tests — five classes, not borrowed from workspace 409:
    - empty: empty query/filters → empty hits; empty fact key → fact error dict.
    - negative: unknown cik / conflicting ticker+cik → `{filings:[]}`; mapper-drop → that hit omitted; unknown fact → error dict; unknown locator → `{error: "unknown_locator"}`.
    - overflow: retrieve `limit` still hard-capped at 20 (existing port).
    - concurrent: two overlapping `retrieve` calls on one adapter instance each return the port/error dict shape without raising; two overlapping `get_xbrl_fact` lookups are read-only and independent. Ingest/OCR: **N/A single-writer** — concurrent writers on the same extracted tree are undefined; default suite does not claim a lock.
    - exception: UA unset (no SEC request); `retrieve_unavailable`; `sec_http_fail`; `ocr_required_unavailable`. Workspace 409 stays an existing host contract, not a substitute for these classes.
- **Confirms with human:** none remaining (wayfinder map fin S1 wayfinder: real EDGAR corpus you can ask and cite cleared 2026-08-25; operator ordered spec+plan+push).
- **Out of this spec:**
  - Universal multi-format parse (PDF/Word/PPT/Excel) in this increment — per ADR 0037, parse and chunking are owned by fin (see specs/corpus-retrieval.md), not iknow-memory ingest.
  - Compile-time coupling or vendoring iknow-memory / a different repo named iknow into `src/fin`.
  - Moving the extracted cite tree into iknow-memory.
  - SSO / RBAC / multi-tenant; dialogue image attach; vision as evidence; web-search as citations.
  - On-demand EDGAR fetch as a host-graph node; catalog-miss as a new track.
  - Date-range filter as a hard contract; renaming `company` → `ticker`; dual gold tool schemas.
  - Locator = whole Item 1A/MD&A; section-aware chunking in this increment (chunking itself is fin's per ADR 0037); hybrid/RRF implemented in fin.
  - Watchlist, redline, peer matrix, multi-agent, LoRA, embedding fine-tune.
  - Making live SEC HTTP part of default `uv run pytest -q`.

## Success Criteria

- `uv run pytest -q` exits 0; default suite does not call `data.sec.gov` / `www.sec.gov` and does not require a live iknow-memory process.
- Product `compile_graph` (non-test) does not bind in-process BM25 as the retrieve adapter (ADR 0030).
- Scripted retrieve with a fake port still returns `{chunks:[]}` for empty query, unknown `document_id`, and mapper-drop; never raises on those misses (`src/fin/retrieve/port.py` contract).
- `list_files(company="AAPL")` still returns the v1-shaped filings list; `list_files` with unknown `cik` or conflicting `ticker`+`cik` returns `{filings:[]}`; optional `form` accepts values other than only the demo 10-Q when rows exist.
- Ingest CLI with unset `FIN_EDGAR_USER_AGENT` does not open a network connection to SEC hosts (unit/scripted test).
- Ingest CLI over existing corpus can emit/update catalog fields for `AAPL-2023Q3` without SEC GET (the HTML-ingest pipeline proven in the throwaway S1 spike is preserved as product CLI behaviour).
- `get_xbrl_fact` miss returns a structured error dict (not throw); claimed numeric answer without a matching evidenced fact does not `done` as a passing research cite.
- Product retrieve with scripted MCP-down returns `{error: "retrieve_unavailable"}` (not `{chunks:[]}`, not BM25).
- Live ingest with UA set and simulated SEC HTTP fail exits non-zero and does not mark that accession complete (`sec_http_fail`).
- OCR-required unit with engine/extra unavailable fails that unit (`ocr_required_unavailable`); OCR-available fixture writes non-empty frozen text and `ocr_applied`.
- Two overlapping retrieve calls on one adapter do not raise and each return chunks-or-`retrieve_unavailable`.
- `uv run pytest -q -m soft_track` still exits 0 (Soft-Track unmarked by this spec's default path).
- No LlamaIndex / Milvus / CrossEncoder product dependency added under `src/fin` for retrieve ranking.

## Open Questions

(none — assumption gate cleared by map 58 + operator 2026-08-25; OCR engine and MCP stdio vs HTTP transport are implementer headroom)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; `src/fin/corpus`, `src/fin/retrieve`, `src/fin/host` capability slices; retrieve port empty-miss contract; ADR 0001 port, 0013 same graph, 0020 extracted SSOT, 0021 citation gate, 0027 Soft-Track, 0028 HTML cite unit, 0029 product/eval trees, 0030 MCP retrieve, 0031 filters + fact tool; `FIN_EDGAR_USER_AGENT` recorded in the upstream project; sibling iknow-memory MCP `retrieve` tool; default pytest marks `eval` and `soft_track`.

**Changes this spec adds:** MCP retrieve adapter + BM25 archive from product path; additive catalog/retrieve filters; ingest CLI (local + gated live HTML); algorithmic OCR write-back; fact table + `get_xbrl_fact` + numeric gate; contract coverage for the new APIs.

**待写入 (persist after ACR):** empty — CONTEXT/ADR 0028–0031 already flushed.

## Architecture change reviewer

Planned blast (not filenames frozen): `corpus/` ingest+catalog+facts, `retrieve/` adapter+port filters, `host/` tool bind + numeric gate, tests under those trees, `pyproject.toml` optional OCR extra.

bounded-context-guardian: yes — filing freeze/catalog/facts stay in `corpus`; ranking client stays behind retrieve port in `retrieve`; graph bind stays in `host`; iknow-memory remains a sibling MCP, no `src/fin` vendoring; no `controllers/`/`services/` layer packages.
defensive-contract-validator: yes — empty/negative/overflow named on filters+port; concurrent = overlapping retrieve + overlapping fact lookup (ingest/OCR N/A single-writer); exception = UA / retrieve_unavailable / sec_http_fail / ocr_required_unavailable; workspace 409 is not a substitute class.
error-handling-enforcer: yes — named EXITs retrieve_unavailable (no BM25), sec_http_fail, ocr_required_unavailable, UA-no-request, mapper-drop, fact-miss dict, unknown_locator; no empty catch swallowing ingest failure.
complexity-anti-drift: yes — ingest CLI vs ask path stay split; retrieve adapter does not inline OCR/XBRL/parse; fact lookup is not a second retriever; no LlamaIndex god-ingest in fin.
minimal-change-verifier: yes — one S1 ship contract; writing-plans splits tracer-bullet commits; no auth/universal-parse-in-fin/watchlist scope creep.

All 5 verdicts yes.
