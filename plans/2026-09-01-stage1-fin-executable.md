# Plan: 阶段 1 fin 可执行子集（A1–A7）

**Goal:** 在不碰跨仓基线、不联调 iknow-memory 的前提下，把 that pull request 之后 fin 侧可独立推进的 A1–A7 做成可逐弹提交的执行计划。
**Approach:** 本文件是 `plans/2026-09-01-pr225-followup-plan-a.md` 的**本轮执行切片**，不另立产品范围。A1 抽出共用 JSONL ledger 后，A2/A3 落到它上面；A4/A5 拆模块且不改 §B1 正文；A6/A7 是两条互不依赖的小实现，可与 A4/A5 并行。默认第一刀 A1。
**Spec link:** `specs/corpus-retrieval.md` §B1–§B6（只读，本轮不修改）
**Inherits:** `plans/2026-09-01-pr225-followup-plan-a.md`（A1–A7）；`plans/retrieve-maintainability-followup.md`（T1–T5 pending，由 A1–A5 承接）
**Tracker:** FALLBACK — 本地本文件。操作者本轮禁止 GitHub issue；阻塞关系只写在子弹的 `[blocks:]` / `[parallel]` 上。
**Per-ticket loop (all bullets):** test-driven-development → `uv run pytest -q`（passed 数不降，failed ≤ 1 且仅为预存 `test_gc_hard_deletes_transported_rows_past_the_retention_window`）→ verification-before-completion → one commit on `docs/stage1-fin-executable-plan`（或从其再切出的 ticket 分支）

## ACR

bounded-context-guardian: yes — ledger / eval / retrieve 拆分留在既有语料与检索切片内；不新增 `controllers/` / `services/`；不把检索服务 vendoring 进 fin。
defensive-contract-validator: yes — 各弹沿用 Plan A 已有测试面（ledger 损坏、并发幂等、capability 静态扫描、剥键警告）；本轮不新开对外契约。
error-handling-enforcer: yes — 五处领域 ledger 的稳定 error code 逐字保留；禁止空 `except`；A6 剥键必须 warning + `degraded_reasons`，不得降为 debug。
complexity-anti-drift: yes — A1 先抽共用原语再拆文件；A2–A5 各拆一个过软阈值模块，不在一弹里横扫全仓。
minimal-change-verifier: yes — 一弹一逻辑任务、一 commit；不改 citation_gate、不改 `fin.corpus` 公开 API、不改 §B1–§B6。

## 已锁定决策

1. 本轮**不改** `specs/corpus-retrieval.md` 跨仓对齐基线章节；SC41 mismatch 另开，不阻塞本 plan。
2. 阶段 2（D）与跨仓缺口 1–6 的真实联调不在本轮。
3. A8（EDGAR 评测数据集 known_gaps）仍卡 Plan A 的 CHECKPOINT，本轮不做。
4. A9（预存 memory dream 失败）另开 plan。
5. 执行顺序：A1 先做；A2/A3 `[blocks: A1]`；A4 与 A1 无硬依赖；A5 不碰 §B1 正文；A6 与 A7 互不依赖，可与 A4/A5 并行。
6. A6 只在 fin 侧对 mock 无 `.strict()` 的剥键打警告；不改 iknow-memory。
7. A7 只改 `DEFAULT_MCP_COMMAND` 两处常量为 `("iknow-memory",)`，兜底非契约。

## Tasks (ordered by dependency)

1. **抽出 append-only JSONL ledger 基础设施** — tag: `[implementation]`
   - **Inherits:** Plan A A1；`plans/retrieve-maintainability-followup.md` T1。五处领域 ledger 保留各自错误类型与 code（`push_journal_corrupt` / `invalidation_ledger_unwritable` / `degradation_ledger_corrupt` / `mapping_failure_ledger_corrupt` / `input_validation_ledger_corrupt` 等逐字）。
   - **Surface:** retrieve 能力切片内既有 ledger 调用点（input validation / degradation / hit mapping / invalidation / push batching journal）。
   - **Acceptance:** 一处内部共用 append / replay / I/O 错误包装；五处领域实现改走该原语且领域错误族不合并；rg 与 AST 自证三件套未在领域文件内重写；默认 pytest passed 数不降。
   - Status: [ ] pending
   - **默认第一刀**
   - [blocks: none]

2. **拆分 concurrency runner** — tag: `[implementation]`
   - **Inherits:** Plan A A2。opt-in CLI 与落盘格式不变；同一进程内「串行假通过」仍须被既有 barrier 驱动的并发幂等测试捕获。
   - **Surface:** `src/fin/eval/concurrency/`（既有 runner 职责：driver / report / validation）。
   - **Acceptance:** 三职责分开且共享执行原语只留一处；`tests/eval/test_concurrency.py` 端到端仍绿；默认套件无新增 regression。
   - Status: [ ] pending
   - [blocks: T1]

3. **拆分 input_validation 与 invalidation_port** — tag: `[implementation]`
   - **Inherits:** Plan A A3。对外导出面不扩大（既有 import 路径可 re-export）；持久化走 T1 抽出的 ledger。
   - **Surface:** retrieve 切片内 `input_validation` 与 `invalidation_port` 既有模块。
   - **Acceptance:** T18 / T21 既有测试全绿；默认套件无新增 regression。
   - Status: [ ] pending
   - [blocks: T1]

4. **拆分 capabilities** — tag: `[implementation]`
   - **Inherits:** Plan A A4。8 键逐字断言与「产品路径开关命中数 = 0」不变；`PRODUCT_CALL_PATH_FILES` 单一权威位置不变。
   - **Surface:** retrieve 切片内 capabilities（snapshot/enumeration 与 static switch scanner）。
   - **Acceptance:** `tests/retrieve/test_capability_static_check.py` 与 `test_capabilities.py` 全过；默认套件无新增 regression。
   - Status: [ ] pending
   - [parallel]
   - [blocks: none]

5. **拆分 ablation matrix 与 B1 口径解析** — tag: `[implementation]`
   - **Inherits:** Plan A A5。§B1 动态读取仍是单一权威，仍在 `uv run python -m fin.eval.ablation run` 生效；禁止另立第二套口径。
   - **Surface:** `src/fin/eval/ablation/` 既有 matrix 模块。
   - **Acceptance:** T22 / T23 既有测试（`tests/eval/test_ablation.py` / `tests/eval/test_concurrency.py`）全绿。
   - Status: [ ] pending
   - [parallel]
   - [blocks: none]

6. **缺口 1 静默失败监控（fin 侧）** — tag: `[implementation]`
   - **Inherits:** Plan A A6。mock zod **无 `.strict()`**；被剥 `top_k` / `filter` / `document_id` 时至少一条 warning 与一条 `degraded_reasons`；警告不得降为 debug。
   - **Surface:** `src/fin/retrieve/mcp_adapter.py` 与既有 `tests/retrieve/test_mcp_adapter.py`。
   - **Acceptance:** 脚本化 port + 脚本化 `_call_mcp` 下断言成立；默认套件无新增 regression。
   - Status: [ ] pending
   - [parallel]
   - [blocks: none]

7. **缺口 3 serve 入口放宽** — tag: `[implementation]`
   - **Inherits:** Plan A A7。仅两处常量：`src/fin/host/compile.py` 与 `src/fin/host/graph.py` 的 `DEFAULT_MCP_COMMAND` → `("iknow-memory",)`；不入 spec / ADR。
   - **Surface:** host 编译与图装配里的 MCP 默认命令常量。
   - **Acceptance:** 两处同步；注释标明兜底非契约；默认套件无新增 regression。
   - Status: [ ] pending
   - [parallel]
   - [blocks: none]

## 不在本计划范围

- A8 评测数据集 known_gaps（pdf / chunk_total）—— CHECKPOINT 之后
- A9 预存 `test_gc_hard_deletes_transported_rows_past_the_retention_window`
- B6 / SC41 基线逐字对齐
- 跨仓缺口 1–6 真实联调、阶段 2（D）
- iknow-memory 质量栈与 p0-eval（对方仓 worktree）

## 关联工件

| 类型           | 路径                                         |
| -------------- | -------------------------------------------- |
| 本轮执行 SSOT  | 本文件                                       |
| Plan A（全文） | `plans/2026-09-01-pr225-followup-plan-a.md`  |
| 维护性源计划   | `plans/retrieve-maintainability-followup.md` |
| 跨仓总计划     | `plans/retrieval-upgrade.md`                 |
| Spec           | `specs/corpus-retrieval.md`                  |

## End of round

全部 A1–A7 落地后：对照 Plan A 做一轮 Standards + Spec code-review，再 `verification-before-completion`。不要把 A8 网络拉取混进这些 commit。
