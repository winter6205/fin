# Plan: P1 — 真实进程入库推送对接

**Goal:** 用**现有**评测样本，在真实 **iknow-memory** 进程上把入库推送、召回、失效跑通：推送文本即召回文本、失败具名、产品路径不关能力。
**Approach:** Fake port 与 D0 客户端已在 `main`。本计划不再拆 D0 /「阶段 2 文档」。只补真实进程这一截。B6 若仍 1-byte mismatch，先修再联调。默认 `uv run pytest -q` 仍不启检索进程。做完本文件全部子弹再开 P2，不要并行开 corpus-ops。
**Spec link:** `specs/corpus-retrieval.md` §B2–§B4、SC15、SC18、SC21、SC23、SC24、SC25、SC39；配对 `iknow-memory` `specs/retrieval-stack.md` 跨仓对齐基线章节
**Queue:** 三份之一。入口 [`plans/2026-09-02-product-queue.md`](2026-09-02-product-queue.md)
**Tracker:** FALLBACK — 同 product-queue 头。不开 GitHub issue。
**Per-ticket loop (all bullets):** tdd → `uv run pytest -q`（默认 Fake）与本弹声明的 opt-in 真实进程 → verification-before-completion → one commit on `docs/2026-09-02-product-queue`

## ACR（自 `specs/corpus-retrieval.md` 原样粘贴范围，本切片不改模块边界）

bounded-context-guardian: yes — 解析、切分、推送、失效触发留在语料能力切片内；排序仍在 retrieve port 之外；检索服务仍是独立进程。
defensive-contract-validator: yes — 空 / 非法 / 溢出 / 并发 / 异常沿用 SC42–46；真实进程失败具名，不把降级当空结果。
error-handling-enforcer: yes — 推送不可用、协议失败、失效失败具名并留痕；禁止失败即空成功。
complexity-anti-drift: yes — 适配器做字段映射与进程联调，不在 host 再写一套检索。
minimal-change-verifier: yes — 只补真实进程验收与 B6 若缺口；不改引用门规则；不改 §B1 正文。

## Harvest

**已决：** 切分归 fin（ADR 0037）；D0 已映射 `limit`/`filters`/`chunks`、`push`/`invalidation` 工具、`DEFAULT_MCP_COMMAND = ("iknow-memory",)`；Fake 路径 T14–T21 已勾。
**开放（implementer headroom）：** opt-in 标记名、如何拉起 stdio 子进程、临时数据目录。
**不做：** 消融全量、pdf≥20、corpus-ops、合 #172–#185、默认 pytest 启真实进程。

## Tasks (ordered by dependency)

1. **跨仓基线章节逐字比对过门** — tag: `[implementation]`
   - **Inherits:** `specs/corpus-retrieval.md` §B6 / SC41：两份 spec「跨仓对齐基线」章节正文（标题行之后至下一 `## ` 之前，去首尾空行）sha256 相等则通过。iknow 侧权威文件为 `specs/retrieval-stack.md` 同名章节。
   - **Surface:** `corpus` 既有 `python -m fin.corpus.baseline`；若对侧章节差 1 字节，改动停在 **iknow-memory** 该章节或 fin 该章节的同步，两侧同一次改。
   - **Acceptance:** `uv run python -m fin.corpus.baseline <fin_spec> <iknow_spec>` exit 0；不一致时 exit 非 0 且两侧哈希都打印。默认 `uv run pytest -q` 仍不读对侧仓为硬依赖（命令接受路径）。
   - **Completion:** 修哪一侧的空白/换行由 implementer 选，只要 B6 取段规则不变。
   - Status: [x] complete
   - **默认第一刀**

2. **真实进程：幂等推送且召回文本逐字节相同** — tag: `[implementation]`
   - **Inherits:** §B4 不二次切分；SC15；SC18 幂等基准行（连续推送 3 次，块总数增量 = 0）。用 Plan C **之前**已有的真实块，不等 pdf≥20。
   - **Surface:** fin `retrieve` MCP push/retrieve 适配 + **iknow-memory** 已公布的 `push` / `retrieve` 工具。
   - **Acceptance:** opt-in 下启动真实 iknow stdio：推送一批现有 child 块后 retrieve 命中文本与推送文本逐字节相同；同一批再推 2 次块总数增量 = 0；默认 `uv run pytest -q` 不跑该 opt-in。
   - **Completion:** 进程拉起方式与 fixture 块数由 implementer 选。
   - Status: [x] complete
   - [blocks: T1]

3. **真实进程：分批中断后续传** — tag: `[implementation]`
   - **Inherits:** SC23：中断后重推已成功批次不重复写入，未成功批次补齐至预期总数；SC21：不可达 / 协议失败具名，不落盘即算完成。
   - **Surface:** 既有 push batching + 真实 push 工具。
   - **Acceptance:** opt-in 下模拟中途失败后重跑：已成功批次不产生第二份写入，补齐后块数 = 预期总数；进程杀掉后的推送是具名失败不是空成功；默认 pytest 不跑。
   - **Completion:** 如何注入中断由 implementer 选。
   - Status: [x] complete
   - [blocks: T2]

4. **真实进程：内容失效幂等** — tag: `[implementation]`
   - **Inherits:** §B4 失效 = 关区间、不物理删除、幂等、留记录；SC24：失效后任意查询时刻旧块出现次数 = 0。
   - **Surface:** 既有 invalidation 适配 + 对侧 `invalidation` 工具。
   - **Acceptance:** opt-in 下对同一旧块两次失效不报错且 retrieve 在有效时刻不再命中该旧文本；默认 pytest 不跑。
   - **Completion:** valid_at 时钟由 implementer 选。
   - Status: [x] complete
   - [blocks: T2]

5. **真实进程：产品路径全开且降级不当空结果** — tag: `[implementation]`
   - **Inherits:** ADR 0039 / SC25：产品调用不带能力关闭键；§B3 / SC39：`degraded` 为真时不得当完整空命中。
   - **Surface:** host 产品 compile 路径 + MCP retrieve。
   - **Acceptance:** opt-in 产品 retrieve 发出的键不含 K2 关闭形；对侧若返回 `degraded=true`，fin 不把该次当 `{chunks:[]}` 成功空结果；默认 pytest 不启进程。
   - **Completion:** 如何注入 degraded 信封由 implementer 选（真实工具或测试替身进程）。
   - Status: [x] complete
   - [blocks: T2]

## 不在本计划范围

- P2 corpus-ops、P3 pdf 规模与 live 消融
- 合入 parked #172–#185
- 改引用门判定规则

## End of round

P1 五弹落地后对照 §B4 / SC15/18/21/23/24/25/39 做 verification-before-completion，再开 P2。整轮三份计划完才做一次 code-review。
