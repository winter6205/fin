# Plan: that pull request follow-up — Plan A（fin 侧可独立推进项收口，2026-09-01）

**Goal:** 把 that pull request 合 main 之后 fin 侧**不依赖跨仓 wire** 即可推进的事项收口为单一 plan，避免继续追加到 `plans/retrieve-maintainability-followup.md` 造成单点膨胀。本 plan 是「fin 侧 follow-up」轮次的第一刀。

**Approach:** 本轮会话已知跨仓契约 6 项缺口（见 `docs/handoff/2026-09-01-pr225-merged.md` 与 `docs/cross-repo/alignment-proposal.md`），且当前 iknow-memory 仓侧 spec/`plans/` 下尚无任何「跨仓对齐基线」章节、SC41 校验器事实单边跑过。因此本 plan **冻结**如下边界（任何越界改动都需另开 plan 并经 explicit 授权）：

- 不动跨仓契约字段名：`source_ref` / `retrieval_id` / `degraded` / `degraded_reasons` / 8 能力键（`vector` / `keyword_bm25` / `cjk_segmentation` / `phrase` / `field_boost` / `graph_ppr` / `rrf` / `rerank`）
- 不动 `specs/corpus-retrieval.md` §B1–§B6
- 不动 `src/fin/host/citation_gate.py`
- 不动 `src/fin/corpus/` 公开 API
- 不动 §B1 数字口径、不新建独立评测条目

**Inherits:** `plans/retrieve-maintainability-followup.md`（承接其 T1–T5 pending，T6 / T7 已 done）

**Spec link:** `specs/corpus-retrieval.md` §B1–§B6（**不修改**，仅作为 Acceptance 的契约基准）

**Per-ticket loop:** test-driven-development → `PYTHONPATH=src /home/winner/projects/fin/.venv/bin/python -m pytest -q -p no:cacheprovider <target>` → verification-before-completion → one commit on main

**Tracker:** 本文件。

## 前置约束

- 每条 task 完成后，默认 `uv run pytest -q` 的 passed 数**不应减少**；基线参考 `1758 passed / 1 failed / 46 skipped`，那 1 failed 是预存的 `tests/host/test_memory_dream.py::test_gc_hard_deletes_transported_rows_past_the_retention_window`（不在本 plan 范围）。
- `tests/corpus/test_live_fetch.py` 真实访问 sec.gov，网络不稳会超时，不要为它改代码。
- 任何对外契约字段拼写 / 语义 / 已具名稳定 error code 的修改一律禁止。

## Tasks (ordered by dependency)

### A1. 抽出 append-only JSONL ledger 基础设施 — tag: `[refactor]`

- **Inherits:** `plans/retrieve-maintainability-followup.md` T1（Standards Low）；五处重复实现：`fin/retrieve/input_validation.py:311-360` / `degradation.py:365-412` / `hit_mapping.py:236-284` / `invalidation_port.py:472-523` / `push_batching.py` 的 journal
- **Surface:** `src/fin/retrieve/_ledger/` 或同等单层模块；五处领域 ledger 改为在该基础设施之上实现
- **Acceptance:**
  - 一处内部通用 append-only JSONL 组件，承担写入、重放、以及 I/O 与损坏内容的错误包装
  - 五个领域 ledger（input validation / degradation / hit mapping / invalidation / push batching journal）改为在其之上实现
  - 各自**保留**自己的领域错误类型与 code（`push_journal_corrupt` / `invalidation_ledger_unwritable` / `degradation_ledger_corrupt` / `mapping_failure_ledger_corrupt` / `input_validation_ledger_corrupt` 等逐字保留）
  - 五处的重复实现消失；通过 rg 与 AST 双重自证「append / replay / 错误包装」三件套未在领域 ledger 内重写
  - `uv run pytest -q` passed 数 ≥ 1758（基线）、failed 数 ≤ 1（仅预存 memory dream）
- **Completion:** 组件的位置与命名由实现者定；「领域错误族不被合并成一个通用错误」是硬约束——上述五处 code 必须逐字保留
- [blocks: A2, A3, A4, A5, A6]
- Status: [ ] pending

### A2. 拆分 `src/fin/eval/concurrency/runner.py`（911 行） — tag: `[refactor]`

- **Inherits:** `plans/retrieve-maintainability-followup.md` T2（Standards Medium）
- **Surface:** `src/fin/eval/concurrency/` 内部分模块，按 driver / report / validation 三职责拆分
- **Acceptance:**
  - 按场景拆出 driver（L1 / L2 / 幂等）、report（落盘模型）、validation（断言与校验）三部分
  - 共享执行原语保留在一处（`runner/` 下单一 `primitives.py` 或同级）
  - opt-in CLI 调用形式与落盘格式不变（`tests/eval/test_concurrency.py` 端到端断言）
  - T23 既有测试（`test_concurrency.py`）全绿；`tests/eval/concurrency/` 下新增拆分后各模块的针对性用例
  - 默认 `uv run pytest -q` 不增加 regression
- **Completion:** 模块名 / 文件边界由实现者定；同一进程内「串行假通过」必须仍然被既有 barrier 驱动的并发幂等测试捕获
- [blocks: A1]
- Status: [ ] pending

### A3. 拆分 `src/fin/retrieve/input_validation.py`（675 行）与 `invalidation_port.py`（612 行） — tag: `[refactor]`

- **Inherits:** `plans/retrieve-maintainability-followup.md` T3（Standards Medium ×2）
- **Surface:**
  - `input_validation.py` 拆为「错误注册 / 持久化 / 分类规则 / 观察」四块
  - `invalidation_port.py` 拆为「codec / ledger / port operation」三块
- **Acceptance:**
  - 各自按职责拆开；对外导出面**不扩大**（既有 import 路径保留为 re-export）
  - T18（`tests/retrieve/test_invalidation.py` / `tests/retrieve/test_invalidation_port.py`）、T21（`tests/retrieve/test_input_validation.py` / `tests/host/test_input_validation_exit.py`）既有测试全绿
  - 默认 `uv run pytest -q` 不增加 regression
- **Completion:** 拆分边界由实现者定；持久化由 A1 抽出的 ledger 基础设施承载
- [blocks: A1]
- Status: [ ] pending

### A4. 拆分 `src/fin/retrieve/capabilities.py`（522 行） — tag: `[refactor]`

- **Inherits:** `plans/retrieve-maintainability-followup.md` T4（Standards Medium）
- **Surface:** `src/fin/retrieve/capabilities/` 拆为「snapshot/enumeration」与「static switch scanner」两个职责模块
- **Acceptance:**
  - snapshot 校验（`load_capability_keys` / `published_capability_keys` / `assert_snapshot_matches_published`）与 product-path AST 扫描（`scan_source_for_capability_switches` / `capability_switch_hits`）分离
  - T19 的 8 键逐字断言与「产品路径开关命中数 = 0」两条断言不变且仍绿（`tests/retrieve/test_capability_static_check.py` 与 `test_capabilities.py` 全过）
  - 默认 `uv run pytest -q` 不增加 regression
- **Completion:** 模块边界由实现者定；PRODUCT_CALL_PATH_FILES（`port.py` / `tool.py` / `mcp_adapter.py` / `graph.py` / `compile.py`）列表的单一权威位置不变
- Status: [ ] pending

### A5. 拆分 `src/fin/eval/ablation/matrix.py`（543 行） — tag: `[refactor]`

- **Inherits:** `plans/retrieve-maintainability-followup.md` T5（Standards Medium）
- **Surface:** `src/fin/eval/ablation/` 拆为「ablation matrix 构建」与「B1 数字口径解析」两个独立模块
- **Acceptance:**
  - T22 的 ablation matrix 构建（10 配置 / 40 题 / 重复 3 次的矩阵编排）独立成模块
  - T23 的 B1 并发契约解析（动态读取 §B1 各行）独立成模块
  - §B1 数字口径动态读取仍是单一权威、仍在 `uv run python -m fin.eval.ablation run` 入口生效
  - T22、T23 既有测试（`tests/eval/test_ablation.py` / `tests/eval/test_concurrency.py`）全绿
  - 默认 `uv run pytest -q` 不增加 regression
- **Completion:** 模块边界由实现者定；不允许在本地另立 §B1 第二套口径
- Status: [ ] pending

### A6. fin 侧缺口 1 静默失败监控 — tag: `[implementation]`

- **Inherits:** `docs/handoff/2026-09-01-pr225-merged.md` 风险与已知 gap 节（缺口 1 静默失败随 fin 上 main）；`docs/cross-repo/alignment-proposal.md` 缺口 1；T16 (`tests/retrieve/test_push_failures.py`) 既有静默失败检测路径
- **Surface:** `tests/retrieve/test_mcp_adapter.py` 新增集成测试 + `src/fin/retrieve/mcp_adapter.py` 内警告日志（若需）
- **Acceptance:**
  - mock zod schema **无 `.strict()`**（与 iknow-memory 仓当前实情一致），断言 fin 发送 `top_k` / `filter` / `document_id` 后被剥键时 fin 侧记录警告日志
  - 断言 fin 侧 ToolMessage 不静默吞掉被剥字段——若 field 被剥，至少有一条 warning 级别日志与一条 degraded_reasons 项
  - 用例在脚本化 port + 脚本化 `_call_mcp` 下运行；默认 `uv run pytest -q` 不增加 regression
- **Completion:** 警告日志的具体命名（`fin.retrieve.mcp_adapter.adapter_field_stripped` 一类）与字段被剥检测点由实现者定；不允许把警告降级为 debug
- Status: [ ] pending

### A7. fin 侧缺口 3 serve 入口放宽 — tag: `[implementation]`

- **Inherits:** `docs/handoff/2026-09-01-pr225-merged.md` 风险节（缺口 3 入口不存在）；`docs/cross-repo/alignment-proposal.md` 缺口 3
- **Surface:** `src/fin/host/compile.py:130` 与 `src/fin/host/graph.py:1442` 的 `DEFAULT_MCP_COMMAND = ("iknow-memory", "serve")` → `("iknow-memory",)`
- **Acceptance:**
  - 两处常量值同步更新；先走 stdio MCP（让 `npm start` 形态可启动）；纯兜底
  - 不影响 spec：常量变更不入 ADR，不入 spec；注释中说明「兜底非契约」
  - 默认 `uv run pytest -q` 不增加 regression
- **Completion:** 修改范围严格限定为上述两行常量；不触其他 host 文件
- Status: [ ] pending

### A8. 数据集 known_gaps 补齐 — tag: `[implementation]`

- **Inherits:** `plans/corpus-retrieval.md` T10 known_gaps（pdf 12 < 20、chunk_total 15686 < 20000）；`plans/retrieve-maintainability-followup.md` 「不在本计划范围」节
- **Surface:** `uv run python -m fin.corpus.dataset expand-primary` 走 EDGAR 真实访问
- **Acceptance:**
  - 用该命令访问 EDGAR 把 pdf 样本从 12 补到 ≥20
  - chunk_total 从 15686 补到 ≥20000（落在 §B1 数据集规模「切分后块 ≥ 20000 且 ≤ 120000」下限）
  - opt-in，需网络；默认 `uv run pytest -q` 不读新增样本（仍退出 0）
  - 重建两遍 manifest sha256 一致、`verify` 不一致样本数 = 0（与 T10 既有断言对齐）
- **Completion:** EDGAR 拉取脚本的请求节流 / 上游版本固定策略沿用 T10 既有约定；不引入新依赖
- Status: [ ] pending

### A9. 预存失败 `test_gc_hard_deletes_transported_rows_past_the_retention_window` 单独处理 — tag: `[implementation]`

- **Inherits:** `plans/corpus-retrieval.md` T22 known failure；`plans/retrieve-maintainability-followup.md` 「不在本计划范围」节
- **Surface:** `tests/host/test_memory_dream.py`（不在本轮 scope）
- **Acceptance:**
  - 本条 task **不在本 plan 推进**：需另开 plan 处理
  - 本 plan 的 Status 字段仅记录「需另开 plan」的事实，不做 Acceptance 描述
- **Completion:** 维持预存失败状态；本 plan 不尝试修复
- Status: [ ] pending（待另开 plan）

### 🛑 CHECKPOINT

**A1–A7 完成后停下**，等待如下两项同时闭合再继续 A8–A9：

1. **跨仓基线对齐完成** —— `docs/cross-repo/alignment-proposal.md` 的反向提案（A / B / C 任一）由对方仓 operator 采纳并落盘，对方仓 spec/`plans/` 下出现「跨仓对齐基线」同名章节，fin 侧 SC41 校验器（`uv run python -m fin.corpus.baseline`）双侧 sha256 比对 exit 0
2. **跨仓契约缺口 1–6 真实联调那一刀**（第三个 plan）落地 —— 至少缺口 1（请求形状）/ 2（响应形状）/ 3（启动入口）三项端到端跑通

CHECKPOINT 闭合之前，A8（数据集会涨到 §B1 上限，且 A8 触及真实网络）不建议在生产路径上跑；A9 不在本 plan 推进。

## 不在本 plan 范围

- 跨仓契约缺口 1–6 的真实联调 —— 留第三个 plan
- iknow-memory 仓的 `plans/retrieve-graph-quality.md` T2–T11 推进 —— 对方仓
- Standards 轴完整复审（CLAUDE.md 已知缺口）—— 留 ARTHURPOWER 全轮收口
- HEAD 全量回归（CLAUDE.md 已知缺口）—— 与本 plan 并行，operator 决定时点
- 预存失败 `test_gc_hard_deletes_...`（A9 单独处理）—— 另开 plan

## 关联工件

| 类型             | 路径                                                                         |
| ---------------- | ---------------------------------------------------------------------------- |
| 上版 handoff     | `docs/handoff/2026-09-01-pr225-merged.md`                                    |
| 跨仓对齐提案     | `docs/cross-repo/alignment-proposal.md`                                      |
| 维护性 follow-up | `plans/retrieve-maintainability-followup.md`（T1–T5 pending，由 A1–A5 承接） |
| 总计划           | `plans/retrieval-upgrade.md`                                                 |
| fin 侧分计划     | `plans/corpus-retrieval.md`                                                  |
| Spec             | `specs/corpus-retrieval.md` §B1–§B6                                          |

## 收尾

每条 task 完成后各自 commit；CHECKPOINT 处停下后由 operator 决定是否继续 A8–A9。完整体 code review 与 verification-before-completion 在跨仓基线对齐闭合 + 缺口 1–6 真实联调完成后统一收尾。
