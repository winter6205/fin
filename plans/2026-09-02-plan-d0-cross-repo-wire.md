# Plan: D0 — 跨仓缺口 1–6 联调（fin 侧执行切片）

**Goal:** 让 fin 的 retrieve / 推送 / 失效可以打到真实 **iknow-memory** 进程：请求不再被静默剥键、响应按 `chunks` 映射、进程能启动，并在对方仓落地 push/失效与 B4 字段后，用**现有**评测样本做一次幂等推送与「召回文本 = 推送文本」对账。
**Approach:** 与 Plan C **并行**。本文件只写 fin 仓子弹；iknow 仓对侧在 `iknow-memory` 的同名计划。先 1–3（起得来、形状对），再接 4–6。不新开清洗中心、不重排 host 的 `list_files` / `read_file` / `get_xbrl_fact`。默认第一刀：retrieve 请求只发对侧已公布字段，堵住静默失败。
**Spec link:** `specs/corpus-retrieval.md` §B2–§B4、retrieve port（ADR 0032）、ADR 0037
**配对计划:** iknow-memory `plans/2026-09-02-plan-d0-cross-repo-wire.md`（分支 `docs/plan-d0-cross-repo-wire`）
**Tracker:** FALLBACK — 本地本文件。不开 GitHub issue。
**Per-ticket loop (all bullets):** tdd → `uv run pytest -q`（默认 Fake port，不启真实检索进程）→ 本弹声明的 opt-in 联调用例 → verification-before-completion → one commit

## ACR

bounded-context-guardian: yes — 改动停在 retrieve 适配与 host 启动常量；排序仍在 port 外；不 vendoring iknow-memory。
defensive-contract-validator: yes — 剥键 / 坏信封 / 不可达走既有具名失败；opt-in 联调不进默认 pytest。
error-handling-enforcer: yes — 禁止把降级当空结果；推送失败具名；无空 `except`。
complexity-anti-drift: yes — 适配器做字段映射，不在 host 再写一套检索。
minimal-change-verifier: yes — 一弹一逻辑任务；不改引用门规则；不改 §B1–§B6 正文。

## 已锁定决策

1. 与 Plan C 并行；联调用现有样本，不等 pdf≥20 / chunk_total≥20000。
2. retrieve 线上字段以**对侧已公布的 retrieve 工具 schema**为准，fin 适配器做映射；禁止再发会被 zod 默默丢掉的键。
3. 响应以对侧 `chunks[]` 为命中列表，不再要求 `hits`。
4. 不新增 host 工具集合、不做 MCP 结构重构；只补对侧 push / invalidation 的**客户端调用**（对侧工具落地后）。
5. B6 基线措辞另开，不阻塞本 plan。

## Tasks (ordered by dependency)

1. **retrieve 请求不再静默剥键** — tag: `[implementation]`
   - **Inherits:** 对齐提案缺口 1；retrieve port（`query` + 可选过滤）。对侧当前收 `limit` / `filters` 且 schema 无 `.strict()`。fin 不得再把 `top_k` / `filter` / `document_id` 当作会到达对侧的键，除非对侧显式公布同名输入。
   - **Surface:** `src/fin/retrieve` 的 MCP 适配（既有 `mcp_adapter`）。
   - **Acceptance:** 适配器发出的 retrieve 输入键 ⊆ 对侧公布 schema；脚本化 / Fake 路径下未知键条数为 0；默认 `uv run pytest -q` 不启真实进程。
   - Status: [ ] pending
   - **默认第一刀**
   - 配对：iknow I-T1

2. **retrieve 响应按 `chunks` 映射 locator** — tag: `[implementation]`
   - **Inherits:** 缺口 2；命中须带 B4 `source_ref`；fin 不得把「`hits` 非 list」当成唯一合法信封。
   - **Surface:** 既有 hit mapping / MCP 适配。
   - **Acceptance:** 对侧形 `{ chunks: [...] }`（块上 `content` + `source_ref`）能映射进 fin locator；缺 `source_ref` 走既有 mapping 失败计数，不把降级当空成功。
   - Status: [ ] pending
   - [blocks: T1]
   - 配对：iknow I-T2

3. **[parallel] 产品默认启动命令与对侧入口一致** — tag: `[implementation]`
   - **Inherits:** 缺口 3。Plan A A7：两处 `DEFAULT_MCP_COMMAND` → `("iknow-memory",)` 当且仅当对侧 bin 是无 `serve` 子命令的 stdio 入口；若对侧落地的是 `iknow-memory serve`，则常量与之逐字一致。兜底非契约，不入 spec。
   - **Surface:** `src/fin/host` 既有默认 MCP 命令常量。
   - **Acceptance:** 两处常量与对侧 I-T3 公布的 argv 一致；不改其他 host 文件；默认套件不启真实进程。
   - Status: [ ] pending
   - [parallel]
   - [blocks: none] — 可与 T1 并行，但对账以 I-T3 为准
   - 配对：iknow I-T3

4. **客户端接 push（幂等键与必填元数据）** — tag: `[implementation]`
   - **Inherits:** 缺口 4；§B4 幂等键 = (`source_ref`, 内容标识)；缺结构化元数据被拒。fin 侧 `push_port` 已实现，本弹只把真实 MCP 调用对上对侧新工具（名称由对侧 I-T4 公布）。
   - **Surface:** 既有 push port / batching，不新开清洗或第二套切分。
   - **Acceptance:** Fake 路径行为不变；opt-in 测试用**当前**数据集中的少量块走真实工具时，重复推送块总数增量 = 0（§B1 幂等基准语义）；默认 pytest 不跑真实 push。
   - Status: [ ] pending
   - [blocks: T3]
   - 配对：iknow I-T4

5. **客户端接内容失效** — tag: `[implementation]`
   - **Inherits:** §B4 失效 = 关区间、不物理删除、幂等、留记录。fin `invalidation_port` 已实现。
   - **Surface:** 既有 invalidation port。
   - **Acceptance:** opt-in 下对同一旧块两次失效不报错且仍不可被 retrieve 在有效时刻命中；默认 pytest 仍 Fake。
   - Status: [ ] pending
   - [blocks: T4]
   - 配对：iknow I-T5

6. **消费 B4 信封字段与 8 能力键** — tag: `[implementation]`
   - **Inherits:** 缺口 5；§B3 `degraded` / `degraded_reasons` 逐字；§B4 八键 `vector` / `keyword_bm25` / `cjk_segmentation` / `phrase` / `field_boost` / `graph_ppr` / `rrf` / `rerank`。产品路径不带关闭键（ADR 0039）。
   - **Surface:** 既有 degradation 消费与 capability 快照。
   - **Acceptance:** 对侧若置 `degraded=true`，fin 不把该次当完整结果；8 键与 `data/retrieval/capability_keys.json` 仍一致；不在 fin 另立内部名（`bm25` / `graphEnabled`）映射表作为权威。
   - Status: [ ] pending
   - [blocks: T2]
   - 配对：iknow I-T6

7. **opt-in 不二次切分对账（现有样本）** — tag: `[implementation]`
   - **Inherits:** §B4 不二次切分；ADR 0037。用 Plan C **之前**已有的真实块，不依赖 A8。
   - **Surface:** 既有 push + retrieve 适配；测试标 opt-in / 网络+真实进程，不进默认 `uv run pytest -q`。
   - **Acceptance:** 推送一批现有 child 块后 retrieve 命中文本与推送文本逐字节相同；对侧不得再经滑动窗口改写该文本。
   - Status: [ ] pending
   - [blocks: T4, T6]
   - 配对：iknow I-T7

## 不在本计划范围

- Plan C 的 EDGAR 扩充与 §B1 规模下限
- 阶段 2（D）全量批量/断点/120s 并发达标（D0 只打通契约）
- 阶段 3 E1/E2、A9、B6 措辞、清洗中心、host 工具重构
- 在默认 pytest 引入真实 iknow-memory 进程

## 关联工件

| 类型         | 路径                                                        |
| ------------ | ----------------------------------------------------------- |
| 本仓语料计划 | `plans/2026-09-02-plan-c-eval-dataset.md`                   |
| 缺口表       | `docs/cross-repo/alignment-proposal.md`                     |
| 总计划 D     | `plans/retrieval-upgrade.md`                                |
| Spec / ADR   | `specs/corpus-retrieval.md` §B2–§B4；ADR 0032 / 0037 / 0039 |

## End of round

fin 子弹全部落地且对侧 I-T1–I-T7 可对账后，对照缺口 1–6 做 code-review，再 `verification-before-completion`。正式阶段 2（D）另开，不在本文件扩写。
