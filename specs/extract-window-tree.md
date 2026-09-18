# Spec: extract window tree (fin corpus)

> **Status:** assumption gate confirmed operator 2026-09-17.
> **Four-spec split:** this is **spec 1 of 4**. Siblings: [`graph-index-standard-fast.md`](graph-index-standard-fast.md) (2), [`search-mode-retrieve.md`](search-mode-retrieve.md) (3), [`search-mode-host.md`](search-mode-host.md) (4).
> **Plan:** not this turn (`writing-plans` after `architecture-change-reviewer`).

## Confirmed assumptions (gate)

1. Four-spec split is in force; this document is only spec 1 of 4 (fin corpus window tree).
2. Default `uv run pytest -q` stays **Fake** retrieve; live MCP / live extract-model is not a default-pytest gate.
3. Spec and later plan files use **local markdown only** — no GitHub issue tracker for tracer bullets.
4. In-scope ADRs are **0053**, **0054**, **0060**, **0063** (accepted). OCR write-back and ingest `TesseractEngine` default language `chi_sim+eng` are already ADR 0054; this spec does **not** reopen tessdata language or OS tessdata packaging.
5. Company-corpus text is **one window tree**, not two document cutters. Frozen locator text is the source string, not a third overlap policy (ADR 0063).
6. Overflow ~8% overlap exists **only** between adjacent **抽图窗** of the same heading body. ES / lexical **父子** children default **0** overlap; narrative table rows may overlap **at most one sentence**; table rows and atomic clauses stay 0. Do not copy the 8% onto ES keys.
7. **场景适配颗粒度** packs in-window children and overflow slices under the 1200-token cap; it must not shrink the extract window to child size (ADR 0060).
8. Extract records are derived at extract time from already frozen text (ADR 0054). Extract does not persist a second section/row table at ingest, does not OCR again, and does not use a vision model to invent headings.

## Objective

Make fin’s ingest-side **切分** produce **one** title-scoped window tree so extract, window embeddings, B/C provenance, and the citable body after a hit share the same **抽图窗** parent id, while lexical/ES keys stay **in-window children** with a **different** overlap identity.

**Who:** implementers of fin **ingest endpoint** / corpus **切分** (local operator markdown corpus). Not the retrieve ranking host and not the extract-method (Fast vs standard) switch.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **冻结单元**: fin 解析产出的最小可引用文本单位；一经产出即冻结。切出的块是其连续子串并 push；**citation verifier** 在运行时对的是本轮 retrieve/`read_file` 正文（ADR 0046）。
- **切分**: fin 把冻结单元文本切成块的过程与规则，是 fin 的职责；切分必须确定性可复现，同一（字节输入 + 规则版本 + 参数表版本）两次切分产物逐字节相同。检索服务对推送文本不做二次切分、截断、重组或归一化改写。
- **抽图窗**: Title-scoped extract-and-cite unit: current heading plus a body span (≤1200 tokens one window; overflow splits body only, ~8% overlap between those windows). It is the parent of in-window **父子** keys (ADR 0060 / 0063), derived at extract from frozen text (ADR 0054), not a second document cut.
- **父子分块**: Search keys inside one **抽图窗**: children are the lexical/ES hit set and the **Fast** co-occurrence scope (ADR 0077), not standard LLM extract input; each is a contiguous substring of its parent window, returned for extract, citation, and A after a hit (ADR 0060). Default child overlap is 0; narrative may overlap at most one sentence; not the window 8% (ADR 0063).
- **场景适配颗粒度**: Deterministic (document type × section × density) lookup that sizes in-window child packing, child overlap (0 or one sentence), and overflow packing under the 1200-token cap, not a second extract splitter (ADR 0060 / 0063). Same triad, same row; table version in metadata, bump rebuilds in place.

## Boundaries

- **Does:**
  - One tree: **抽图窗** is the parent (ADR 0053 + ADR 0060). Each extract call sees the current heading plus a body span. If that heading’s body is at most 1200 tokens (extract-model tokenizer, ADR 0053), the whole body is one window and window overlap is zero. If the body is larger, keep the same heading and split **body only** to the 1200 cap with about 8% overlap between adjacent overflow windows — the overflow branch of the same rule, not a second splitter.
  - Table rows and clauses are title-scoped bodies that almost always fit in one window (ADR 0053).
  - In-window **父子分块**: children exist only inside a window as lexical search keys; each child is a contiguous substring of its parent window; children are not standard LLM extract input (ADR 0060). Default child overlap is 0; narrative density-table rows may overlap at most one sentence; table rows and atomic clauses stay 0 (ADR 0063).
  - **场景适配颗粒度** lookup sizes child packing, child overlap (0 or one sentence), and overflow packing under the 1200-token cap. Same (document type × section × density) triad → same row; table version in **结构化元数据**; bump rebuilds in place. Density must not shrink the extract window to child size.
  - Derive extract records at extract time from frozen locator text: **资料主体**, section title, body or table row, column names as fields (not extract-body text). HTML/Markdown headings are the title source (ADR 0054).
  - Scanned or textless pages stay on the existing ingest OCR path (`OCREngine` write-back into the **冻结单元**). Extract reads that same frozen text as the body field. If the frozen OCR page has no heading tags, title may be empty and overflow 1200-token splits still share that unit as source plus **资料主体** (ADR 0052 / 0053 / 0054).
  - **切分** remains deterministic: same (byte input + rule version + parameter-table version) → byte-identical windows and children.
  - Test classes: empty (empty frozen unit / heading with empty body); negative (unknown density-triad → named miss or documented fallback row, not a model-chosen cut); overflow (heading body > 1200 tokens → window 8% identity, children still 0 or ≤1 sentence); concurrent (two 切分 runs on the same frozen bytes yield identical trees); exception (OCR ingest already typed `ocr_required_unavailable` on failure — extract does not invent a second OCR EXIT).
  - Tests (existing or planned) **lock the two overlap identities**: window overflow ~8% vs child overlap (default 0 / narrative ≤1 sentence). Frozen text is asserted as the source string, not as a third overlap percent.

- **Confirms with human:** none remaining after the 2026-09-17 gate. Numerical density-table cell values (exact child widths) stay implementer / existing corpus-retrieval table headroom unless a later spec reopens them.

- **Out of this spec:**
  - Fast extract (method switch, regex co-occurrence, prune).
  - Leiden / **社区** clustering and community reports.
  - Search-mode host classify (A/B/C/D cascade, **choice window**, prompt inject).
  - Retrieve **检索档** ranking and assemble (lexical-first A, graph-priority B, C report path, D SQL).
  - Four LangGraph retrieve nodes.
  - Reopening ingest tessdata language or OS tessdata packaging (ADR 0054 closed).
  - Serve-time citation SSOT (still this-run retrieve/`read_file`, ADR 0046); two upload doors (`specs/corpus-serve-ingest.md`); first operator closed loop (`specs/first-closed-loop.md`).
  - Live EDGAR identity vs unofficial PDF (ADR 0028); **入库推送** MCP transport details already owned by ingest/retrieve specs.
  - Default pytest live MCP; product ranking inside fin.

## Success Criteria

Each line is binary. `writing-plans` may map new tests 1:1 onto SC 2–8.

1. `uv run pytest -q` exits 0.
2. A fixture **冻结单元** whose title-scoped body is ≤1200 extract-model tokens yields **exactly one** **抽图窗**; measured overlap between windows for that heading is 0.
3. A fixture heading body that exceeds 1200 tokens yields **two or more** **抽图窗** that share the same heading; adjacent windows overlap by about 8% of the window body (tolerance documented in the test: e.g. overlap length / window length ∈ [0.06, 0.10] or an equivalent token-count band). Non-adjacent windows of the same heading are not required to overlap. Heading text is not split as a second body cutter.
4. Tests distinguish **window overflow overlap** from **child overlap**: the same overflow fixture’s **父子** children default to 0 overlap; a narrative density-table fixture may overlap children by at most one sentence; table-row and atomic-clause fixtures stay 0 child overlap. No test accepts copying the 8% onto children as passing.
5. Every child in the tree is a contiguous substring of its parent **抽图窗** text; no child spans two parents.
6. Two **切分** calls on the same (frozen bytes + rule version + parameter-table version) produce byte-identical window ids/text and child ids/text.
7. Extract-record derivation on a frozen HTML/Markdown fixture uses heading tags as title; on a frozen OCR-write-back fixture with no heading tags, title may be empty and windows still share that **冻结单元** plus **资料主体**. Tests must not require extract to call OCR or a vision heading model.
8. A **场景适配颗粒度** fixture with high density packs **more or smaller children inside the same 1200-token window**; the parent window token cap stays 1200 — density does not retarget the extract window to child size.
9. Frozen locator text is the only overlap source string: tests do not introduce a third overlap percent (neither “frozen-page overlap” nor a global overlap applied to both layers).
10. `web/` `tsc --noEmit` is **N/A** unless this spec’s plan later touches `web/` (window tree is corpus **切分**, not 工作台 chrome).

## Open Questions

(none — 2026-09-17 gate cleared stack, Fake pytest, local markdown, ADR set, and the four-spec split.)

## Inherits / Changes

**Inherits:** ADR 0053 title-scoped **抽图窗** and overflow-only ~8%; ADR 0054 extract-from-frozen-text and OCR write-back (tessdata language closed); ADR 0060 one window tree and density packing in-window only; ADR 0063 window overflow overlap ≠ ES child overlap; ADR 0037 **切分** in fin; ADR 0046 this-run cite evidence; ADR 0052 **资料主体** / **指示语**; `specs/corpus-serve-ingest.md` ingest endpoint as the only product **切分** caller; `specs/corpus-retrieval.md` format×section×density table as the numerical headroom surface; default pytest Fake retrieve.

**Changes this spec adds:** an implementation contract for spec 1 of 4 — lock one tree and the two overlap identities in tests; density must not shrink windows; extract must not re-OCR. No new glossary terms. No new ADRs.

## 待写入 (persist after ACR)

empty — ADRs 0053 / 0054 / 0060 / 0063 already flushed; CONTEXT **冻结单元** / **切分** / **抽图窗** / **父子分块** / **场景适配颗粒度** already SSOT.

## architecture-change-reviewer

Affected surfaces: `src/fin/corpus/chunking.py`, `params.py`, ingest endpoint, corpus tests.

bounded-context-guardian: yes — 切分/抽图窗 stay fin corpus; retrieve/search-mode/Fast/Leiden out.
input-contract-tests: yes — Boundaries name empty / negative / overflow / concurrent / exception plus two overlap identities.
error-handling-enforcer: yes — named miss/fallback; ingest OCR EXIT reused; no second extract OCR.
complexity-anti-drift: yes — window overflow vs 父子 packing stay separate rules, not one god cutter.
minimal-change-verifier: yes — spec 1 of 4; siblings 2–4 out.

OVERALL: PASS — 2026-09-17 ACR. Next: writing-plans (this spec only).
