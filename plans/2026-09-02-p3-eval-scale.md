# Plan: P3 — 评测规模与 live 收口

**Goal:** 评测数据集达到 §B1 每格式下限（pdf ≥20）且 verify 诚实；在 P1 真实推送链 + 达标数据集上，消融与并发按 B1 时长/配置跑完并落盘，而不是只留 smoke。
**Approach:** 入口是 P1 与 P2 都完成。`plans/corpus-retrieval.md` T22/T23 在 Fake/smoke 上已勾，**不**算本计划完成。当前 manifest `counts.by_format.pdf = 12` 而 `known_gaps = []` 是不诚实状态，必须先修规模再跑 live 消融。
**Spec link:** `specs/corpus-retrieval.md` §B1、SC11–SC12、SC31–SC38、T10 known_gaps 约定
**Queue:** 三份之三。入口 [`plans/2026-09-02-product-queue.md`](2026-09-02-product-queue.md)
**Tracker:** FALLBACK — 同 product-queue 头。
**Per-ticket loop (all bullets):** tdd → 默认 `uv run pytest -q` 不读 `data/corpus` 大语料；规模/消融走既有 `-m eval` 或 opt-in 命令 → verification-before-completion → one commit on `docs/2026-09-02-product-queue`

## ACR（自 `specs/corpus-retrieval.md`）

bounded-context-guardian: yes — 扩充停在语料 / 评测数据集与既有 eval 驱动；不新增清洗中心；不把排序拉进 fin。
defensive-contract-validator: yes — 无来源 / 合成不计入；verify 不一致样本数 = 0；默认套件不触网。
error-handling-enforcer: yes — EDGAR 失败走既有具名路径；不静默塞合成样本充数。
complexity-anti-drift: yes — 不新写第二套拉取器或第二套消融矩阵。
minimal-change-verifier: yes — 不改 §B1–§B6 数字正文；不改引用门。

## Harvest

**已决：** §B1 规模与消融矩阵数字；语料来源三级；T10 一键重建对账。
**已知缺口：** pdf 计入规模 = 12（<20）；`chunk_total` 已在 20000–120000；T23 自述 smoke 未替代 120 秒达标。
**开放：** expand-primary 具体 accession 列表（须真实、有许可）。
**不做：** 再拆 Plan C 独立对话；合 corpus-ops 旧 PR。

## Tasks (ordered by dependency)

1. **一级 PDF 补到每格式 ≥20，known_gaps 诚实** — tag: `[implementation]`
   - **Inherits:** §B1 每格式样本下限 20；缺来源或合成不计入；T10 `expand-primary`。当前 pdf=12 不得继续用空 `known_gaps` 冒充达标。
   - **Surface:** 既有 `fin.corpus.dataset`
   - **Acceptance:** 计入规模的 pdf 一级样本 ≥20；清单带来源与许可；report 不再把 pdf<20 记为已关闭；若仍不达标则 `known_gaps` 非空；默认 `uv run pytest -q` 不读新增大样本且不因本弹触网失败。
   - **Completion:** 拉哪些 EDGAR 文档由 implementer 选。
   - Status: [x] complete
   - **默认第一刀**

2. **重建对账：verify 不一致 = 0 且切分契约仍立** — tag: `[implementation]`
   - **Inherits:** T10 连续两次重建 manifest sha256 一致；SC11 全量子串违规数 = 0；`chunk_total` 仍在 §B1 闭区间。
   - **Surface:** 既有 `dataset build / verify / report` 与切分入口
   - **Acceptance:** 连续两次重建哈希一致；verify 不一致样本数 = 0；`-m eval` 切分契约全绿；默认 pytest 仍不读 `data/corpus`。
   - **Completion:** 重建命令沿用既有入口。
   - Status: [x] complete
   - [blocks: T1]

3. **Live 消融矩阵按 B1 配置落盘** — tag: `[implementation]`
   - **Inherits:** SC31–SC35；10 配置；指标集完整；提升必须带百分比 + 样本数 + 基线绝对值；能力开关只出现在评测路径（ADR 0039）。在 **P1 真实推送链路** 与 T2 达标数据集上跑，不是只对 Fake port。
   - **Surface:** 既有 `fin.eval.ablation`（或同等 opt-in 命令）
   - **Acceptance:** 一条 opt-in 命令跑完全部 10 配置并落盘，缺失配置数 = 0；每个配置含完整指标集与引用门通过率；百分比-only 条数 = 0；默认 `uv run pytest -q` 不收集该命令。
   - **Completion:** 是否夜间窗口、题集取 20–40 脚本化子集，由 implementer 按 B1 资源预算选。
   - Status: [ ] pending
   - [blocks: T2]

4. **Live 并发 L1 / 全写按 B1 时长** — tag: `[implementation]`
   - **Inherits:** SC36：L1 并发 16、持续 120 秒、读写比 9:1、总请求 ≥1200，错误数 = 0，p95/p99 落盘且不超 B1 延迟阈值；全写幂等 16×120s，块总数增量 = 0。T23 smoke **不算**本弹完成。
   - **Surface:** 既有 `fin.eval.concurrency`
   - **Acceptance:** L1 与全写按 B1 时长跑完并落盘；错误数 = 0；压测前后块总数增量 = 0（全写）；默认 pytest 不跑 120 秒场景。
   - **Completion:** L2 含 LLM 若本环境无凭证，具名 `l2_credentials_missing` 并在报告中声明，不静默 skip 当通过。
   - Status: [ ] pending
   - [blocks: T3]

## 不在本计划范围

- 再改跨仓基线数字
- memory-dream A9
- 质量栈 T6+（iknow retrieve-graph-quality 剩余）

## End of round

P3 四弹落地后对照 §B1 与 SC31–SC38 做 verification-before-completion。然后对 P1+P2+P3 整轮 diff **一次** code-review。
