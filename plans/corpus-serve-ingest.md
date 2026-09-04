# Plan: corpus-serve-ingest

**Goal:** Serve 不再把本地 extracted 树当公司资料 SSOT；拖进对话只读本轮；另入口入库经切分端点 push MCP。
**Approach:** 先堵住产品路径对本地树的依赖（Fake 可测），再入库端点，最后 workspace 两扇门。切分表数值沿用 `specs/corpus-retrieval.md` headroom。
**Spec link:** `specs/corpus-serve-ingest.md`
**Tracker:** FALLBACK — local markdown only. Operator: 本程序文件不开 GitHub issue tracker. Out-of-scope items are filed as issues separately and must not appear as ticket ids in this file.
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（`uv run pytest -q`；web：`npx tsc --noEmit`）→ verification-before-completion → one commit on the ticket branch
**End-of-round:** 全部子弹落地后对整轮 diff 做一次 code-review（Standards + Spec）；单弹 loop 不重复整轮 review。
**待写入:** empty — CONTEXT + ADR 0047 flushed with the spec.

## ACR（自 `specs/corpus-serve-ingest.md` 原样粘贴）

bounded-context-guardian: yes — ingest and 切分 stay a corpus/host capability; ranking stays behind **retrieve port**; web only offers attach vs ingest; no `controllers/` layer.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–8.
error-handling-enforcer: yes — named ingest/MCP/unsupported-type failures; no silent skip-as-success; attach failure must not look like ingest success.
complexity-anti-drift: yes — three foci (serve evidence, ingest endpoint, two doors); writing-plans must not dump connectors or memory-dream into this plan.
minimal-change-verifier: yes — one logical program, not one commit; writing-plans sequences one wave = one tracer bullet.

**OVERALL: PASS**

## Harvest（已决 vs 开放）

**已决：**

- 公司资料经 retrieve 后端查询；dream 不是入库（ADR 0045）。
- 引用门对这轮 retrieve/`read_file` 正文（ADR 0046）；serve 不另养 extracted 树 SSOT（ADR 0047）。
- 切分只在 ingest endpoint；格式表；研究模型不切任意字符串（ADR 0047）。
- thread attach ≠ corpus ingest（ADR 0047）。
- 改版/失效调 MCP。

**开放（implementer headroom）：**

- 切分表具体数值与未知格式兜底行 vs 具名拒绝（spec 允许具名失败）。
- Fake retrieve 如何在无本地树时提供 read 正文。
- 两扇门的具体控件位置（拖进 composer vs 资料库页）。

## Tasks (ordered by dependency)

1. **Serve research path does not use extracted-tree SSOT** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC2–SC3；ADR 0046 / 0047。
   - **Surface:** host retrieve/`read_file` / citation verifier + tests
   - **Acceptance:** scripted research can cite only from this-run payloads; missing local tree does not silent-succeed as if the tree were SSOT; `uv run pytest -q` exits 0。
   - **Completion:** Fake adapter shape open.
   - Status: [x]

2. **list_files catalogs pushed/ingested filings** — tag: `[implementation]`
   - **Inherits:** spec Focus 1；CONTEXT **list_files**。
   - **Surface:** host catalog + tests
   - **Acceptance:** catalog empty/miss is `filings: []` without requiring a serve-time HTML tree SSOT; `uv run pytest -q` exits 0。
   - **Completion:** whether catalog is MCP list vs host metadata table open — resolved: host metadata table `data/corpus/catalog.json`（iknow-memory 无 list 类 MCP 端点，见 docs/handoff/2026-09-04-cross-repo-status.md）；写侧 seam `fin.corpus.catalog.record_filings`（document_id+version upsert）留给 T3 接线。
   - Status: [x]
   - [blocks: T1]

3. **Ingest endpoint: table 切分 then MCP push/invalidate** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 / SC5 / SC7 / SC8；ADR 0047；`specs/corpus-retrieval.md` 场景适配颗粒度。
   - **Surface:** corpus ingest + host HTTP + tests
   - **Acceptance:** supported fixture ingest is observable as push (or Fake record); unsupported type named failure; MCP down typed; no research-graph 切分 tool; `uv run pytest -q` exits 0。
   - **Completion:** HTTP path vs CLI-only first slice open if HTTP lands in T5.
   - Status: [ ]
   - [blocks: T1]

4. **Thread attach does not push** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC4；ADR 0047 **thread attach**。
   - **Surface:** host workspace HTTP + tests
   - **Acceptance:** attaching a fixture file exposes text this turn and does not ingest/push; `uv run pytest -q` exits 0。
   - **Completion:** attach storage (turn blob vs temp) open.
   - Status: [ ]
   - [blocks: T1]

5. **Corpus-ingest control on workspace** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC5；ADR 0047 **corpus ingest**。
   - **Surface:** web + host HTTP + tests
   - **Acceptance:** ingest door triggers endpoint from T3; drag-to-thread still does not; `uv run pytest -q` exits 0；`web/` `tsc --noEmit` exits 0。
   - **Completion:** control placement open (not composer drag).
   - Status: [ ]
   - [blocks: T3, T4]
