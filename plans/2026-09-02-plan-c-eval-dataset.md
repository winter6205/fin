# Plan: C — 评测数据集 known_gaps 补齐（线 C）

**Goal:** 把评测数据集补到 §B1 规模下限（每格式 pdf 样本 ≥20、切分后块 ≥20000 且 ≤120000），清单可一键重建且 verify 一致，默认 pytest 仍不读大语料。
**Approach:** 本文件承接 Plan A 的 A8，但**不**等待联调或 B6。只走既有 `fin.corpus.dataset` 入口与 T10 约定；不推 iknow-memory、不改 MCP、不改 §B1 正文。默认第一刀：`expand-primary` 补一级 PDF。
**Spec link:** `specs/corpus-retrieval.md` §B1（数据集规模与构成、语料来源三级）与 §Boundaries 中评测数据集条款
**Tracker:** FALLBACK — 本地本文件。本轮不开 GitHub issue。
**Per-ticket loop (all bullets):** tdd → `uv run pytest -q`（默认套件不读 `data/corpus`）与既有 `-m eval` 数据集契约 → verification-before-completion → one commit on this worktree branch

## ACR

bounded-context-guardian: yes — 扩充停在语料 / 评测数据集切片；不新增清洗中心；不把检索服务或 MCP 工具树拉进本 plan。
defensive-contract-validator: yes — 沿用 T10：无来源 / 合成不计入；verify 不一致样本数 = 0；默认套件不触网。
error-handling-enforcer: yes — EDGAR 失败走既有具名路径，不为超时去改 `test_live_fetch`；不静默塞合成样本充数。
complexity-anti-drift: yes — 不新写第二套拉取器；节流与上游版本策略沿用 T10。
minimal-change-verifier: yes — 一弹一 commit；不改引用门、不改跨仓基线章节、不改 retrieve port。

## 已锁定决策

1. 本 plan 与 Plan D0（缺口 1–6 联调）**并行**，互不为入口。
2. 覆盖 Plan A 对 A8 的 CHECKPOINT 等待：operator 已选定「语料与联调两张计划同时开」。
3. 交叉联调用现有样本即可；本 plan 只为阶段 3（E1/E2）凑 §B1 规模。
4. 不新增「数据清洗中心」模块。

## Tasks (ordered by dependency)

1. **一级 PDF 补到 §B1 每格式下限** — tag: `[implementation]`
   - **Inherits:** `plans/corpus-retrieval.md` T10 known_gaps（pdf 12 < 20）；§B1 每格式样本下限；语料来源三级（可引用 / 仅供查数 / 派生）；缺来源或合成不计入。`uv run python -m fin.corpus.dataset expand-primary`；节流与上游版本固定沿用 T10。
   - **Surface:** 既有 `fin.corpus.dataset` 与评测数据集清单（`data/corpus` 一类既有树，默认 pytest 仍不读）。
   - **Acceptance:** 计入规模的 pdf 一级样本 ≥20；清单带来源与许可；默认 `uv run pytest -q` 不读新增样本且不因本弹触网失败。
   - Status: [ ] pending
   - **默认第一刀**

2. **派生导出与切分后块数落到 §B1 下限** — tag: `[implementation]`
   - **Inherits:** §B1「切分后块 ≥ 20000 且 ≤ 120000」；T10 派生级 `origin=derived` + `derived_from_sha256`；T11 切分契约（连续子串违规数 = 0）在全量样本上仍成立。当前 `chunk_total=15686` 为 known_gaps。
   - **Surface:** 既有派生导出器与切分入口（不新切开分器）。
   - **Acceptance:** manifest `chunk_total` 落在 §B1 闭区间；派生样本仍标 derived 且回指一级 sha256；既有 `-m eval` 切分契约（子串 / 归一化 fixed point）全绿。
   - Status: [ ] pending
   - [blocks: T1]

3. **重建对账与 known_gaps 关闭** — tag: `[implementation]`
   - **Inherits:** T10：一键重建两遍 manifest sha256 一致；`verify` 不一致样本数 = 0；`report` 可打印规模。规格数字只回源 §B1，不在代码里另抄一套下限。
   - **Surface:** 既有 `dataset build / verify / report`。
   - **Acceptance:** 连续两次重建哈希一致；verify 不一致 = 0；report 不再把 pdf<20 或 chunk_total<20000 记为 known_gaps；默认 `uv run pytest -q` 仍不读 `data/corpus`。
   - Status: [ ] pending
   - [blocks: T2]

## 不在本计划范围

- Plan D0 缺口 1–6、阶段 2（D）整段验收、阶段 3 消融/并发达标跑
- B6 基线措辞、A9 memory dream
- 新 MCP 工具、清洗中心、改 `specs/corpus-retrieval.md` §B1–§B6 正文

## 关联工件

| 类型            | 路径                                                                                 |
| --------------- | ------------------------------------------------------------------------------------ |
| 并行联调（fin） | `plans/2026-09-02-plan-d0-cross-repo-wire.md`（`docs/plan-d0-cross-repo-wire` 分支） |
| Plan A A8 原文  | `plans/2026-09-01-pr225-followup-plan-a.md`                                          |
| T10 源计划      | `plans/corpus-retrieval.md`                                                          |
| 总计划线 C      | `plans/retrieval-upgrade.md`                                                         |
| Spec            | `specs/corpus-retrieval.md` §B1                                                      |

## End of round

三弹落地后对照 §B1 与 T10 做 code-review，再 `verification-before-completion`。不要把 EDGAR 拉取和 MCP 联调揉进同一 commit。
