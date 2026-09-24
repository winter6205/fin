# Spec: search-mode retrieve (档 consumption)

> **Status:** assumption gate = four-spec split confirmed 2026-09-17; this file is spec 3 of 4.
> **Spelling superseded 2026-09-22:** the `filters.search_mode` set this file quotes is now `lexical | graph | global | sql` — `A` became `lexical` and `D` became `sql`, and no letter is read at any seam. The paths are unchanged: the 查找 arm still lexically hits a child and cites the parent **抽图窗** body (ADR 0060), 真数据库 still refuses `real_db_not_wired`. See [`specs/search-mode-lexical-sql-tokens.md`](search-mode-lexical-sql-tokens.md).
> **Sibling (iknow-memory):** [`../../iknow-memory/specs/search-mode-retrieve.md`](../../iknow-memory/specs/search-mode-retrieve.md) (assembler). **Out (fin):** `specs/search-mode-host.md` (cascade / **choice window**); `specs/extract-window-tree.md` (window tree cut); `specs/graph-index-standard-fast.md` (extract / Fast / Leiden write).
> **Prior closed loop:** [`specs/first-closed-loop.md`](first-closed-loop.md) (ingest + single-issuer retrieve + cite; C/D were plan slots).
> **Plan:** not opened until `architecture-change-reviewer` on this spec.

## Objective

Define how **检索档** A/B/C/D **consume** index artifacts **after** a turn is already **grounded**: one **retrieve port** call shape, ranking in **iknow**, fin as host not a second ranker. This spec does not assign tracks and does not write the graph.

**Who:** local operator on the **工作台**; product **知识 MCP** (**iknow-memory**) executes retrieve.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **检索档** (`search mode`): Which retrieve path a knowledge question uses after it has entered **grounded**: A 查找 (lexical-first), B 知识问答 (iknow graph-priority then vector∥BM25 fill), C 综述 (index-time community summaries; report text ES ∥ report vectors, then optional drop to windows — ADR 0070), D 真数据库 (real SQL/business DB; document tables stay B). Assignment reuses the Soft-Track cascade (ADR 0027 / 0062): rules, then structured classify when ambiguous, then inherit, then clarify or typed fail — not a required picker and not silent C. Cascade **clarify** opens a **choice window** (structured options → operator pick → resume execute). Search-mode copy is call-scoped (ADR 0061): inject once in near context, never rewrite history; after **session compaction**, re-inject the mode this turn needs.
- **社区**: Entity-graph clusters from **hierarchical Leiden** after extract and merge (ADR 0056 / 0065), on the **largest connected component** by default (ADR 0074), split until **max_cluster_size** (default 10, tunable — ADR 0072), with a **fixed default seed** (ADR 0075). Graph changes **rebuild** Leiden and summaries; C is not-ready until that succeeds (ADR 0064 / 0078); B does not wait on communities.
- **抽图窗**: Title-scoped extract-and-cite unit: current heading plus a body span (≤1200 tokens one window; overflow splits body only, ~8% overlap between those windows). It is the parent of in-window **父子** keys (ADR 0060 / 0063), derived at extract from frozen text (ADR 0054), not a second document cut.
- **父子分块**: Search keys inside one **抽图窗**: children are the lexical/ES hit set and the **Fast** co-occurrence scope (ADR 0077), not standard LLM extract input; each is a contiguous substring of its parent window, returned for extract, citation, and A after a hit (ADR 0060). Default child overlap is 0; narrative may overlap at most one sentence; not the window 8% (ADR 0063).
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **具名失败**: A typed product failure that names empty-bound 资料, mapping-drop, citation-verifier refuse, 档未开, 真库未接, or community-summary index failure as distinct exits — not one `unknown_locator` plus empty citations.
- **grounded**: The Soft-Track leaf that binds corpus tools and the **citation verifier** (ADR 0061). Not a product name, not **search mode**, and not the `retrieve` tool. Wire value `research` is an alias for this leaf.

ADR 0055: extract windows are graph provenance, not the four retrieve 档. ADR 0058: index embeddings for 抽图窗, entity descriptions, and community summaries. ADR 0059: retrieve 档 consume those artifacts without renaming 档. ADR 0060: A lexical-hits **children** then returns the parent **抽图窗**. ADR 0070: C report text ES ∥ report vectors, then optional drop to windows. ADR 0061: ranking stays behind the retrieve port; **检索档** is not four host-graph retrieve nodes. ADR 0061 injection (call-scoped near context) is **host** — out of this spec (mention only).

## Confirmed assumptions (gate)

1. Four-spec split **2026-09-17**: this file is **spec 3 of 4** (retrieve 档 consumption only).
2. Ranking stays behind **retrieve port** (**iknow**). fin does not reimplement graph-priority, ES, BM25, or vector fill, and does **not** add four LangGraph retrieve nodes.
3. Default `uv run pytest -q` stays **Fake** retrieve; Fake is not a 档. Live MCP is opt-in.
4. C is **not-ready** when the **社区** job failed or summaries are incomplete (CONTEXT: C is not-ready until Leiden + summaries succeed; partial community success does not open C).
5. D is **真库未接** until a real SQL/business DB is wired; document tables stay B (no fake SQL retrieve).
6. A/B/C/D **assignment** (cascade / **choice window**) is `search-mode-host.md`. Window packing / child overlap is `extract-window-tree.md`. Extract / Fast / Leiden **write** is `graph-index-standard-fast.md`.
7. `first-closed-loop.md` remains the ingest + cite closed loop; this spec adds 档-shaped consumption and named 档 exits on the same port.

## Boundaries

- **Does:**
  - **A 查找:** lexical-first on **in-window children**; a hit returns the parent **抽图窗** body as the citable `chunks[]` text; the hitting child span **may** ride as extra evidence metadata but is not a second SoR.
  - **B 知识问答:** graph-priority over entities and relations, then fill remaining slots from **抽图窗** vector∥BM25 (ADR 0059). B does not wait on **社区**.
  - **C 综述:** **社区** report text ES ∥ report embeddings in parallel (ADR 0070); hits are those reports; **default: after at least one report hit, may drop to citable 抽图窗**. Caller may set `filters.c_drop_windows=0` to skip the drop. C must not become “ES the windows and ignore reports.”
  - **D 真数据库:** real SQL/business DB path; until wired, **具名失败** **真库未接**. Figures that live only in filings stay on B.
  - fin **grounded** `retrieve` tool keeps one **retrieve port**. Mode is MCP `filters.search_mode` ∈ `A|B|C|D` (iknow `retrieve` already has `filters: record<string,string>`). Missing `search_mode` on the port defaults to **B** (backward compatible). Invalid value is negative class (`tier_not_open` or documented invalid-mode code), not silent C. **Amended by `specs/search-mode-graph-global.md` (landed):** the wire set is `A | graph | global | D` and a missing / `None` `search_mode` sends **no key**, so iknow's default **graph** answers — not `B`; `B` and `C` are retired letters that the host normalizes to `graph` / `global` **before** the port, and anything else reaching the port is the typed `invalid_search_mode` (`src/fin/retrieve/tiers.py` is fin's SSOT). `tier_not_open` gates `global`, `real_db_not_wired` gates `D`. Throughout this file, `B` means `graph` and `C` means `global`. **Amended by PR #295 / issue #297 (pending merge):** the gating just above is superseded by that PR — `global` left the closed table when 综述 opened, so `TIER_UNAVAILABLE_CODE` gates only `sql` (the retokenized `D`, as `real_db_not_wired`) and `tier_not_open` has no 档 row left to gate.
  - Distinguish **档未开** (`tier_not_open`) / C not-ready (`community_summary_index_failure`) / **真库未接** (`real_db_not_wired`) / empty-bound (`corpus_not_ready`) / mapping-drop (`mapping_drop`) / verifier refuse as **具名失败**. First four codes already live in `src/fin/host/named_failures.py`; `community_summary_index_failure` is the new public token for failed community-summary jobs (not `tier_not_open`, which remains “档 never shipped / operator named a closed 档”).
  - Test classes on the port: empty (no ingest / empty chunks); negative (档未开, C not-ready, D 未接, unnamed mode); overflow (existing push batch cap unchanged); concurrent (existing 409 `concurrent_run`); exception (MCP down already 409 图 1; Fake vs live).

- **Confirms with human:** none remaining for wire codes / C drop / A cite (pinned 2026-09-17 ACR repair).

- **Out of this spec:**
  - Host cascade / **choice window** / call-scoped injection (`search-mode-host.md`; ADR 0061 injection is host).
  - Window tree cut, **场景适配颗粒度**, child overlap table (`extract-window-tree.md`).
  - Extract / Fast / Leiden / community **write** path (`graph-index-standard-fast.md`).
  - Independent 数据中台; Soft-Track 业务意图桶; Fake as product; web search as cite evidence; query cache; 图 1 chrome; iknow 资料页 layout.

## Success Criteria

1. `uv run pytest -q` exits 0 with default **Fake** retrieve (no live MCP required). `web/` `tsc --noEmit` exits 0 when web changes for this task (skip if no web diff).
2. Product path (live opt-in, documented in the later plan): a **grounded** retrieve with mode **A** that lexically hits an in-window child returns at least one chunk whose `source_ref` (or documented window id field) is the **parent 抽图窗**, not a child-only locator as the cite body.
3. Same live opt-in: mode **B** uses iknow graph-priority then vector∥BM25 fill; fin does not assemble that ranking in-repo (no hybrid/RRF module in `src/fin` for this task).
4. Mode **C** when **社区** job failed or summaries incomplete: research/retrieve surfaces **具名失败** for community-summary index failure (C not-ready) — not empty `chunks` as success, not silent B, not Fake health.
5. Mode **C** when reports exist (live opt-in): retrieve hits **社区** report rows via report-text ES ∥ report vectors; optional drop to **抽图窗** does not replace report search (ADR 0070).
6. Mode **D** without a wired SQL/business DB: **具名失败** **真库未接**; no fake SQL retrieve; document tables are not served as D.
7. Invoking an unopened 档 (product config or named 档未开) yields **具名失败** **档未开**, not a silent default B and not verifier-only `unknown_locator`.
8. Default pytest **Fake** does not assert product A/B/C/D ranking; Fake tests cover port contract + named-exit mapping only.
9. No four LangGraph retrieve nodes for A/B/C/D in the host graph (ADR 0061). Injection rewrite of checkpoint history is out (host spec).
10. Citation still uses this-run retrieve/`read_file` text (`first-closed-loop.md` / ADR 0046); this spec does not replace the **citation verifier**.

## Open Questions

(none — public codes, C drop default, A cite payload, and `filters.search_mode` pinned 2026-09-17 ACR repair.)

## Inherits / Changes

**Inherits:** CONTEXT terms above; ADR 0055 / 0058 / 0059 / 0060 / 0070 / 0061 (ranking behind port; no four host retrieve nodes); ADR 0032 retrieve MCP; `specs/first-closed-loop.md` Fake default + ingest/cite; `specs/workbench-retrieve-product-seam.md` 进对话硬拦.

**Changes:** 档 consumption contract on the existing **retrieve port** (A child→parent window, B graph-then-fill, C report ES∥vectors then optional window drop, D named 未接); C not-ready when **社区** job failed; fin does not add four retrieve nodes.

## 待写入 (persist after ACR)

empty

## architecture-change-reviewer

Affected surfaces: `src/fin/retrieve` port/tool, `named_failures.py`, Fake retrieve tests (ranking stays iknow).

bounded-context-guardian: yes — one retrieve port; ranking in iknow; no four host retrieve nodes.
input-contract-tests: yes — Boundaries name all five classes; Fake covers contract + named exits.
error-handling-enforcer: yes — codes pinned (`corpus_not_ready` / `mapping_drop` / `tier_not_open` / `real_db_not_wired` / `community_summary_index_failure`); missing mode → B; invalid not silent C.
complexity-anti-drift: yes — consume existing port; no fin hybrid/RRF assembler.
minimal-change-verifier: yes — spec 3 of 4 only.

OVERALL: PASS — 2026-09-17 ACR (second pass after pinning codes). Next: writing-plans.
