# Spec: graph index (standard LLM extract vs Fast NLP)

> **Status:** assumption gate confirmed 2026-09-17 (four-spec split; this file is **spec 2 of 4**).
> **Joint map:** fin freeze-text / **抽图窗** is the extract input; graph extract, merge, Leiden, community reports, and the three index embeddings are owned by **iknow-memory** (separate repo).
> **Sibling spec (iknow-memory):** [`../../iknow-memory/specs/graph-index-standard-fast.md`](../../iknow-memory/specs/graph-index-standard-fast.md) (index-job implementation). Retrieve 档: [`../../iknow-memory/specs/search-mode-retrieve.md`](../../iknow-memory/specs/search-mode-retrieve.md).
> **Plan:** later slot (`writing-plans` after `architecture-change-reviewer`).

## Objective

Lock the **index-time graph** contract between fin (input: frozen window text after **切分** / **入库推送**) and the retrieve backend (owner: **iknow-memory**): two **extract methods** — **standard** (LLM entities/relations) vs **Fast 抽图** (NLP / co-occurrence) — then canonical merge, Leiden **社区**, index-time summaries, and three embedding classes. Query **检索档** assignment and hit assembly stay other specs. Operators get an explicit method switch (default **standard**); the host may **suggest** Fast in a **choice window** and must not silent-apply Fast.

**Who:** local operator building a **资料集合** version; **iknow-memory** as 库 / index-job owner; fin as 入口 that supplies freeze-text / **抽图窗** and must not re-rank.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **社区**: Entity-graph clusters from **hierarchical Leiden** after extract and merge (ADR 0056 / 0065), on the **largest connected component** by default (ADR 0074), split until **max_cluster_size** (default 10, tunable — ADR 0072), with a **fixed default seed** (ADR 0075). Graph changes **rebuild** Leiden and summaries; C is not-ready until that succeeds (ADR 0064 / 0078); B does not wait on communities.
- **Fast 抽图**: The NLP / co-occurrence **extract method** (index job): mixed **Chinese/English noun-phrase regex** (ADR 0076), co-occurrence on **in-window children** then **prune** and Leiden, community reports from **抽图窗** text (ADR 0067 / 0073 / 0077). An **operator switch** selects it (default **standard**, **one method per corpus version** — ADR 0079); the model may propose Fast in a **choice window** and must not silent-apply (ADR 0068).
- **实体描述**: One index-time summary of an entity’s merged description list on the **standard** extract job, written after **canonical merge** (duplicate gates first — ADR 0052 / 0069) and before Leiden. It is the text ADR 0058 embeds. Fast has none; query and memory dream do not write it.
- **claim**: A time-bounded factual assertion extracted from a **抽图窗** on the **standard** extract job (ADR 0071), after merge and before Leiden. Default on for standard; Fast skips. Not an XBRL fact row and not D.
- **抽图窗**: Title-scoped extract-and-cite unit: current heading plus a body span (≤1200 tokens one window; overflow splits body only, ~8% overlap between those windows). It is the parent of in-window **父子** keys (ADR 0060 / 0063), derived at extract from frozen text (ADR 0054), not a second document cut.
- **父子分块**: Search keys inside one **抽图窗**: children are the lexical/ES hit set and the **Fast** co-occurrence scope (ADR 0077), not standard LLM extract input; each is a contiguous substring of its parent window, returned for extract, citation, and A after a hit (ADR 0060). Default child overlap is 0; narrative may overlap at most one sentence; not the window 8% (ADR 0063).
- **具名失败**: A typed product failure that names empty-bound 资料, mapping-drop, citation-verifier refuse, 档未开, 真库未接, or community-summary index failure as distinct exits — not one `unknown_locator` plus empty citations.

## Confirmed assumptions (gate)

ASSUMPTIONS CONFIRMED 2026-09-17 (four-spec split). This is **spec 2 of 4**. Spec 1 / 3 / 4 are out of this file (see Boundaries).

1. Implementation owner of extract, merge, prune, Leiden, community-summary jobs, and the three index embeddings is **iknow-memory**. Fin’s input to that job is freeze-text / **抽图窗** (and in-window children for Fast co-occurrence). This spec is the **joint contract** living in fin.
2. iknow-memory sibling specs: `specs/graph-index-standard-fast.md` (this job) and `specs/search-mode-retrieve.md` (档 consumption). Two-repo sync 2026-09-17.
3. Two extract methods exist on the **index job**, not as **检索档**: **standard** (LLM entities/relations + **实体描述** + **claim**) and **Fast 抽图** (NLP noun-phrases + co-occurrence). ADR 0067; ADR 0066 is **superseded**.
4. **Operator switch** chooses the method; **default is standard**. AI may propose Fast via a **choice window**; no silent Fast (ADR 0068). Ambiguous or no pick keeps standard.
5. **One extract method per corpus version** (ADR 0079). Switching rebuilds that version (re-extract + Leiden + reports). Dual graphs on one version are rejected. Query A/B/C do not pick a second graph.
6. Fast noun-phrase regex is **mixed Chinese/English**, not product-default `regex_english` (ADR 0076). An empty Fast graph on **Chinese 资料** is a **具名失败**, not a silent empty index.
7. Fast co-occurrence is on **in-window children** (**父子分块**), not a second document cut and not “share the whole 1200-token window” as the edge rule (ADR 0077). Standard LLM extract still runs **per 抽图窗**.
8. Fast **prunes** (rare/hub nodes, weak edges; PMI-style; default weak = 40th percentile of remaining edges) **then** Leiden. **Standard does not prune** by default (ADR 0073). Prune is not canonical merge and not a 档.
9. **Canonical merge** (normalized same name and type, then type and embedding gates; **指示语** bind to **资料主体** — ADR 0052) runs **before** **实体描述**. Fast has no **实体描述**. Query and dream do not write descriptions (ADR 0069). One entity-summary miss skips that node’s vector; it does not fail C the way missing community summaries do.
10. **claim** extract is **ON** for standard (after merge, before Leiden) and **OFF** for Fast (ADR 0071). Claims are not XBRL rows and not 档 D. One claim miss may skip that row; it does not fail C.
11. Leiden is **hierarchical** (ADR 0065). **max_cluster_size** default **10**, tunable (ADR 0072). **use_lcc** default **on** (ADR 0074). **Fixed default seed** (ADR 0075). Isolated specks are not community-report rows when lcc is on.
12. Community **summaries are written at index** (ADR 0057). Summary job error / timeout / empty text for any community → typed fail; **C is not ready** (ADR 0064). B may use the entity graph without waiting on **社区**. Query must not backfill summaries.
13. **Graph change** (new 资料, invalidate, or standard/Fast re-extract) **rebuilds Leiden and reports** (ADR 0078). Do not serve the previous report set for that corpus version. C stays not-ready until rebuild succeeds. B may use the updated graph before C is ready.
14. After community summaries, index writes **three embeddings**: **抽图窗** body, entity descriptions (standard survivors), community summary text (ADR 0058). Query is not the first embed of those classes. Fast has no entity-description class to embed.
15. Default `uv run pytest -q` stays **Fake retrieve**; live / opt-in graph index jobs are **documented in the later plan**, not a default-pytest requirement.
16. This spec does **not** implement ranking in fin. Search-mode cascade in the host and retrieve **档** hit assembly are **other specs**.

## Boundaries

- **Does:**
  - Name the joint index pipeline: freeze **抽图窗** in (fin) → extract method (standard **or** Fast, one per version) → canonical merge → (standard only: **实体描述** + **claim**) → (Fast only: prune) → hierarchical Leiden **社区** → index-time community summaries → three embedding classes (as applicable) → C-ready only when summaries succeed.
  - Require iknow-memory to implement that job (sibling spec in that repo).
  - Require operator switch default **standard**; AI suggest + **choice window**; no silent Fast.
  - Require Fast mixed zh/en regex; Fast co-occur on in-window children; empty Fast graph on Chinese **资料** as **具名失败**.
  - Require graph-change rebuild of Leiden + reports; C not-ready on summary-job fail.
  - Test classes **in the later plan** (iknow sibling + opt-in): empty (no extract / empty Fast on Chinese **资料**); negative (two methods on one version, silent Fast, `regex_english` as product Fast default); overflow (max_cluster_size split); concurrent (rebuild vs B-using-updated-graph); exception (community-summary job fail → C not ready, B still usable).

- **Confirms with human:** public error **code strings** for empty Fast graph vs community-summary index failure vs 档未开; numeric Leiden default seed; embedding model id / dimension for the three classes.

- **Out of this spec:**
  - Search-mode cascade in the **host** (other spec of the four).
  - Retrieve **档** hit assembly (other spec of the four).
  - Implementing ranking in **fin**.
  - Product names of third-party graph-RAG stacks in this spec body.
  - 抽图窗 sizing / **父子分块** parameter table authorship (inherits ADR 0060 / 0063; not reopened here).
  - Default pytest live MCP or live graph jobs.
  - **检索档** D, XBRL fact table, dream, session memory, web search as cite evidence.
  - Host search-mode HTTP (fin spec 4).

## Success Criteria

1. `uv run pytest -q` in **fin** exits 0 with the Fake retrieve adapter; this spec does **not** add a default-pytest live graph job.
2. Live / opt-in graph index commands (standard extract, Fast extract, Leiden rebuild, C-ready check) are **named in the later plan** and are **not** required on default pytest.
3. A corpus **version** records **exactly one** extract method. A second method on the same version is rejected (typed fail), not merged into a dual graph.
4. Index-job default extract method is **standard**. Applying Fast without an operator switch pick (or with an empty/ambiguous pick) must **not** occur.
5. Fast product default noun-phrase extractor is **not** `regex_english`. A Fast index of Chinese **资料** that yields an empty entity-relation graph is a **具名失败**, not a silent empty graph and not a healthy C/B miss.
6. Fast co-occurrence edges are created from **in-window children**, not from a second 50–100 token cut of the frozen unit.
7. Fast runs prune **before** Leiden; standard index of the same fixture does **not** run that prune by default.
8. On standard, **实体描述** exists only **after** canonical merge and **before** Leiden; Fast writes **no** **实体描述**. Query-time description write is absent.
9. On standard, **claim** extract is enabled by default after merge and before Leiden; Fast writes **no** claims.
10. Leiden is hierarchical; default **max_cluster_size** is 10 and overridable; default **use_lcc** is on; default clustering uses a **fixed seed** (not unseeded random).
11. Community summaries are stored at index. If the summary job errors, times out, or emits empty text for any community, C stays **not ready** (typed fail). B may still use the entity graph.
12. After extract/merge that **changes** the graph, Leiden and community reports are rebuilt before C is marked ready for that version; the previous report set is not served as current C for that version.
13. Index writes embeddings for **抽图窗** text, (standard) entity descriptions, and community summaries; query is not the first embed of those classes. Omitting a class disables that retrieve arm (stated for the sibling; not assembled in this spec).
14. Fin contains **no** new ranking implementation as part of this contract.

## Open Questions

1. Public JSON/SSE/MCP **error code** strings for: empty Fast graph on Chinese **资料**; community-summary index failure; C not-ready during Leiden/report rebuild — vs existing 档未开 / 资料未就绪.
2. Integer value of Leiden’s **fixed default seed** (ADR 0075 requires fixed; does not name the integer).
3. Embedding **model id / dimension** for the three index embedding classes (ADR 0058 requires the three classes, not the model).

## Inherits / Changes

**Inherits:** `docs/CONTEXT.md` terms quoted above; ADR 0052 (merge + 指示语 / 资料主体); ADR 0056 (community after extract); ADR 0057 (summaries at index); ADR 0058 (three embedding classes); ADR 0064 (summary job fail → C not ready); ADR 0065 (hierarchical Leiden); ADR 0066 **superseded** by 0067; ADR 0067 (Fast is extract method); ADR 0068 (operator switch, no silent Fast); ADR 0069 (**实体描述** after merge); ADR 0071 (claims ON standard / OFF Fast); ADR 0072 (`max_cluster_size` default 10 tunable); ADR 0073 (Fast prune then Leiden; standard no prune); ADR 0074 (`use_lcc` default on); ADR 0075 (fixed default seed); ADR 0076 (mixed zh/en regex); ADR 0077 (Fast co-occur on children); ADR 0078 (rebuild on graph change); ADR 0079 (one method per version); ADR 0060 / 0063 (**抽图窗** / **父子分块** input shape); `specs/first-closed-loop.md` document shape; retrieve port ranking stays outside fin (CONTEXT **retrieve adapter**).

**Changes:** joint index contract is now an explicit fin spec (spec 2 of 4); Fast is on the map as a second extract method (0067), not “later / not required” (0066); empty Fast on Chinese **资料** is typed fail; iknow implementation spec is `iknow-memory/specs/graph-index-standard-fast.md`.

## 待写入 (persist after ACR)

empty

## architecture-change-reviewer

Affected surfaces: fin joint contract only (no ranking in `src/fin`); iknow implements `specs/graph-index-standard-fast.md`.

bounded-context-guardian: yes — extract/merge/Leiden/embeddings in iknow; fin freeze-text / 抽图窗 only.
input-contract-tests: N/A — five classes allocated to iknow opt-in plan, not fin default pytest.
error-handling-enforcer: yes — typed exits named (empty Fast, dual method, silent Fast, summary fail → C not-ready); public strings Open Question on iknow job codes.
complexity-anti-drift: N/A — no fin implementation modules in this spec.
minimal-change-verifier: yes — spec 2 of 4; host cascade and retrieve ranking out.

OVERALL: PASS — 2026-09-17 ACR (N/A dims are not fails). Next: writing-plans sequenced after iknow sibling ACR.
