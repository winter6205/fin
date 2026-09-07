# Spec: corpus-ops-depth

> 工作区知识库的运营深化：原始字节进 **object store**、可操作 ingest、OCR 写回、在已有切分权威上扩库与分析深度、真 filing 的 in-app 监控。
> 不重开切分归属（ADR 0037 / `specs/corpus-retrieval.md` 已定：解析与父子分块归 fin）。本 spec 接到 **当前 main** 的 `corpus` / `host` / `web`，不引入第二套运行时解析器，不整包 rebase 开着的 #172–#185。

## Assumption gate (confirmed 2026-08-29)

Operator confirmed the numbered gate in-thread. These are in force:

1. Workspace is the only product entry; same **host graph**; **citation verifier** stays a code gate; filing retrieve stays behind the **retrieve port** (ADR 0032).
2. Postgres is the only authority store for metadata; Redis is optional and out of this spec's Must path.
3. This file is independently shippable from `specs/memory-dialogue-platform.md` (memory may stay OFF).
4. Default `uv run pytest -q` stays green with no live SEC and no live memory MCP.
5. Raw bytes go to **object store** (MinIO/S3-compatible); metadata and locators stay in fin/Postgres — no in-repo Elasticsearch + Milvus dual index (ADR 0043).
6. Multi-format parse already landed under `specs/corpus-retrieval.md` (`fin.corpus` parsers). This spec **extends** that path (OCR write-back, object-store pointers, structure-aware locators on the existing parent/child split). It does not add a LlamaIndex/Docling second runtime splitter.
7. Structure-aware / parent-child units remain **extracted + citable locators** only; ranking stays behind the retrieve port.
8. First corpus universe remains SEC filing-shaped; multi-BU process docs are an extension seam, not a Must second library.
9. Analysis depth = paragraph redline + table extract + three-year statements from XBRL; page-image preview is Out.
10. Monitoring = real filing source + in-app alert on top of existing watchlist; outbound email/Slack is Out.
11. Selective algorithmic OCR (write-back into frozen unit text) and ingest state machine + audit are in scope. Existing S1 `ocr_applied` / `ocr_required_unavailable` is **Inherits**, not a parallel OCR product.
12. `#141` / `enterprise-kb-agent` deliveries (compare_facts, watchlist, stop, export, fan-out, health) are **Inherits** — not rebuilt.

## Objective

Grow and operate the **workspace** knowledge library so an operator can ingest filings into a durable extracted tree with citable locators, keep raw bytes out of git, deepen grounded analysis (redline / tables / statements), and get in-app notices when real new filings appear — without waiting on the memory subsystem, and without replacing the corpus-retrieval parse/chunk/push path.

Success is: each Does focus below has binary checks; default pytest never needs live SEC or a live **iknow-memory** process.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: A **workspace** knowledge-base Agent: the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
- **workspace**: A local web UI server: create, list, open, resume, and delete a research thread; **thinking fold** and **tool fold** stream then collapse (`specs/turn-trace-folds.md`); citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **ingest**: A CLI that writes frozen locator-unit text from official filings (or the existing extracted tree) into an extracted tree. 滑动窗口式**切分**是 fin 的职责而非排序栈（ADR 0037），排序仍留在 fin 之外；the ask path does not fetch EDGAR or open source filings.
- **locator**: Cite identity `{document_id, version, unit}`: for the frozen 12, `version` is the PDF sha256 and `unit` is the 1-based pdfplumber `page`; for live EDGAR, `version` hashes the official primary iXBRL `.htm` bytes and `unit` is a deterministic frozen partition of that HTML's stripped text, not an SEC or unofficial-PDF page. The claimed span is a quote string, not a locator field; the citation verifier checks it is a contiguous substring of that unit's frozen text; chunks carry the locator as `source_ref` and are not the identity.
- **object store**: MinIO/S3-compatible storage for raw filing (and later multi-format) bytes. Postgres holds metadata and pointers; the extracted cite tree stays in fin. Git is not the byte warehouse once configured.
- **compare_facts**: Research-track host tool that resolves one XBRL concept over a grid of rows (issuers or documents) x columns (periods) against the same fact table `get_xbrl_fact` reads, and returns the `fin.comparison.v1` envelope. Deltas are computed in the tool, not by the model. Every column is an implicit `(concept, period)` claim for the numeric gate, so an answer quoting figures the table never reported cannot `done`. It contributes no citation evidence: a fact row is not page text.

## Boundaries

- **Does:**

  **Focus 1 — Durable raw store and operable ingest**
  - Object store for raw filing bytes (ADR 0043); git stops being the byte warehouse when configured.
  - Ingest state machine with typed transitions and an ingest audit trail (who/what/when at process level — local no-auth).
  - Selective OCR: collapsed/no-text pages with images get algorithmic OCR write-back into the frozen locator unit; vision is not a fact source. Reuse existing `ocr_applied` / `ocr_required_unavailable`; do not fork a second ingest OCR product.

  **Focus 2 — Structure-aware units on the existing splitter**
  - PDF (Must) already parses on main. This spec requires section-aware and/or parent-child **citable locators** that stay compatible with corpus-retrieval SC11 (child ⊆ parent ⊆ frozen unit). Empty/unknown catalog filters still yield empty hits.
  - Word/PPT/Excel: already in corpus-retrieval; this spec does not restage a second engine.

  **Focus 3 — Expandable SEC-shaped library**
  - Grow beyond frozen-12 with additional issuers/forms via controlled ingest (fixtures in default pytest; live SEC only in opt-in lanes).
  - Corpus 首页 / `GET /api/corpus` remain truthful about what is in the library.

  **Focus 4 — Analysis depth beyond compare_facts grids**
  - Paragraph-level cross-period **redline** as a host tool result on the thread.
  - Table extraction into page text and/or structured artifacts bound to locators.
  - Three-year financial statements rendered from XBRL facts (not LLM-authored tables).
  - Numeric/citation gates unchanged in kind.

  **Focus 5 — Real filing monitor (in-app)**
  - Real filing source detection for watched issuers/forms; in-app notice when a new filing is available (reload-safe).
  - Builds on existing watchlist CRUD; no outbound email/Slack.

  **Test allocation (all foci, 5 classes):** empty (empty catalog after filter, empty OCR skip); negative (unknown object key, bad form filter, OCR engine typed failure); overflow (ingest size/page caps if any); concurrent (ingest vs ask 409/typed busy if applicable); exception (object store down, ingest state illegal transition, monitor source unreachable typed degraded).

- **Confirms with human:** none remaining after the 2026-08-29 gate.

- **Out of this spec:** memory subsystem / dream / DST / thumbs; Redis; SSO/RBAC; Public API; fin MCP server; email/Slack; attach_image / vision answers; page-image preview product UI; YAML playbooks; Elasticsearch+Milvus in-repo; LlamaIndex runtime path; rewriting Soft-Track tracks; weakening the citation verifier; merging parked GitHub PRs #172–#185 as the implementation vehicle; changing `specs/corpus-retrieval.md` §B1–§B6.

## Success Criteria

Each line is binary. Sequential plans map onto a subset. Default `uv run pytest -q` stays green with no live SEC and no live memory server.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 when web surfaces change.
2. With object store configured in tests (fake/minio fixture), ingest stores raw bytes off-git and catalog/read_file still resolve locators; with store URL unset, typed degraded or documented local-fallback — never silent byte loss.
3. Ingest exposes a documented state machine; illegal transition or failed step yields a typed error and an audit row; happy path reaches a Ready-equivalent state observable in tests.
4. A fixture page with collapsed text + image receives OCR write-back into the frozen unit; citation verifier can match a span from that text; vision is never required for `done`.
5. A multi-section fixture yields section-aware or parent/child units with stable locators that still satisfy child ⊆ parent ⊆ frozen unit; `list_files` unknown filters still `{filings: []}`.
6. A non-frozen-12 fixture corpus with extra issuer/form appears on `GET /api/corpus` / home cards; default pytest never calls live SEC.
7. A scripted research turn that runs the redline tool shows a structured redline on GET `/api/threads/{id}`; a turn that only invents numbers does not `done`.
8. Table extract for a fixture page lands text/artifact bound to a locator; three-year statement view/API is driven from XBRL facts for a fixture issuer (not model arithmetic).
9. With a scripted monitor source, a watched filing appearing yields an in-app notice after reload; monitor source down is typed degraded, not a blank 500.
10. Citation gate still fills only verified spans; `refuse` / `step_cap` citations `[]` (inherited from workspace / enterprise-kb contracts).

## Open Questions

(none)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; slices `src/fin/corpus/`, `src/fin/retrieve/` (port only), `src/fin/host/`, SPA `web/`; ScriptedChatModel + InMemorySaver default tests; marks `soft_track`, `eval`, `mcp` stay opt-in; typed EXITs `retrieve_unavailable`, `unknown_locator`, `{chunks: []}`, `{filings: []}`, 409 `{error: concurrent_run}`, 503 `{error: ingest_missing}`; S1 OCR `ocr_applied` / `ocr_required_unavailable`; corpus-retrieval parsers + parent/child split + push/invalidation Fake ports; ADR 0020 extracted ingest SSOT; ADR 0021 citations from verifier; ADR 0029 product corpus; ADR 0031 catalog filters + `get_xbrl_fact`; ADR 0032 retrieve adapter; ADR 0037 chunking owned by fin; ADR 0043 object store; `compare_facts` / watchlist / export / fan-out / health from `specs/enterprise-kb-agent.md`. Parked PRs #172–#185 are **reference diffs only**.

**Changes this spec adds:** object store for raw bytes on current main; ingest state machine + audit; OCR write-back as extension of S1 OCR (not a second product); structure-aware locators without a second splitter; expandable SEC-shaped library beyond frozen-12; paragraph redline tool + table extract + XBRL three-year statements; real filing monitor with in-app notice.

## 待写入 (persist)

empty — CONTEXT **object store** and ADR 0043 flushed with this spec on `docs/2026-09-02-product-queue`.

## architecture-change-reviewer

bounded-context-guardian: yes — new work stays in `corpus` / `host` (tools + workspace notice) / `web`; object-store client is a corpus adapter, not a `controllers/` layer; ranking does not move into host; no second parse/chunk authority beside corpus-retrieval.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–9.
error-handling-enforcer: yes — inherit typed EXITs; new paths named (object store down, illegal ingest transition, OCR failure, monitor degraded); no silent empty catch; no silent byte loss.
complexity-anti-drift: yes — five foci, one concern each; redline/table/statements are host tools or corpus writers, not nested in `model_node`.
minimal-change-verifier: yes — one logical program, sequenced as plan P2 tracer bullets on current main; parked PRs are not the merge vehicle.

**OVERALL: PASS — hand to writing-plans P2.** Spec path: `specs/corpus-ops-depth.md`.
