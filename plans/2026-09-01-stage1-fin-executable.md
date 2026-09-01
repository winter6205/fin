# Plan: Stage 1 fin 可执行子集（Plan A A1–A7）

**Goal:** 在本 worktree 落地 that pull request follow-up Plan A 中 **fin 侧不依赖跨仓 wire** 即可完成的 A1–A7：抽出 JSONL ledger、按职责拆分超软阈值模块、补剥键警告、放宽 serve 兜底入口。

**Approach:** 本文件是 `plans/2026-09-01-pr225-followup-plan-a.md` 的可执行子集，不新增产品范围。默认第一刀 A1（抽出 append-only JSONL ledger）；A2 / A3 消费该基础设施后拆分 runner 与 input_validation / invalidation_port；A4 / A5 与 A1 无硬依赖，可并行，但 A5 只拆 ablation 模块、不改 `specs/corpus-retrieval.md` §B1 正文；A6 与 A7 互不依赖，可与 A4 / A5 并行。阶段 2（D）、跨仓缺口 1–6 真实联调、A8 / A9、iknow-memory 仓工作均不在本轮。

**Spec link:** `specs/corpus-retrieval.md` §B1–§B6（只读契约基准，本轮不修改）

**Tracker:** 本地 markdown fallback。operator forbade GitHub issues for this round; SSOT is this file. 每颗 tracer 的 Status 只在本文件勾选；不建 GitHub issue、不补原生阻塞边。授权 GitHub 主路径后，再按 `writing-plans/references/tracker.md` 另开。

**ACR:** all-yes — 针对本轮实现工作（ledger 抽出 + 文件拆分 + 两处小 adapter 调整），不是针对写本 markdown。五线如下。

bounded-context-guardian: yes — ledger 抽出与 runner / retrieve / ablation 拆分留在既有 `retrieve` 与 `eval` 能力切片；`mcp_adapter` 与 host 的 `DEFAULT_MCP_COMMAND` 不越界到检索服务仓；不新增 `controllers/` / `services/` 分层。
defensive-contract-validator: yes — 各弹复用既有 T18 / T19 / T21 / T22 / T23 与 T16 路径覆盖空 / 非法 / 溢出 / 并发 / 异常；A6 补剥键警告的集成断言；默认套件不引入真实检索进程或 EDGAR。
error-handling-enforcer: yes — 五处领域 ledger 错误类型与 code 逐字保留、不合并成通用错误；A6 剥键走 warning 而非静默；A7 常量兜底不升格为契约。
complexity-anti-drift: yes — 声明结构是「一处 JSONL 原语 + 按职责拆文件」，不是加深嵌套或复制第三套 ledger；行数 / 圈复杂度不写入 Acceptance，由 `complexity-anti-drift` 的 `references/thresholds.md` 裁决。
minimal-change-verifier: yes — 七弹各一逻辑任务、各一 commit；不改跨仓基线章节、不改 citation_gate / corpus 公开 API、不改 iknow-memory。

**Per-ticket loop (all bullets):** tdd → pytest（本 worktree：`uv run pytest -q`）→ verification-before-completion → one commit on the ticket branch（this worktree）

**待写入:** 无。本计划不引入相对 `docs/CONTEXT.md` 的新术语，不重开 ADR。

## 已锁定决策

1. 阶段 2（D）与跨仓缺口 1–6 真实联调不在本轮。
2. 不改跨仓对齐基线章节；SC41 mismatch 另开，不阻塞本 plan。本轮不改 `specs/corpus-retrieval.md` §B1–§B6。
3. A8 评测数据集 known_gaps 仍卡 Plan A CHECKPOINT，本轮不做。
4. 执行顺序：A1 先做（抽出 JSONL ledger），A2/A3 依赖 A1；A4/A5 与 A1 无硬依赖但 A5 不碰 §B1 正文；A6 与 A7 互不依赖、可与 A4/A5 并行。默认第一刀 A1。
5. A6 只做 fin 侧剥键警告（mock 无 `.strict()`）；不改 iknow-memory。
6. A7 只改两处 `DEFAULT_MCP_COMMAND` 为 `("iknow-memory",)`；兜底非契约。

## 前置约束

- 对外契约一字不改：`source_ref` / `retrieval_id` / `degraded` / `degraded_reasons` / 8 能力键 / 已具名稳定 error code。
- 不动 `src/fin/host/citation_gate.py`、不动 `src/fin/corpus/` 公开 API。
- 每弹完成后默认 `uv run pytest -q` 的 passed 数不应减少；基线参考 `1758 passed / 1 failed / 46 skipped`。那 1 failed 是预存 `tests/host/test_memory_dream.py::test_gc_hard_deletes_transported_rows_past_the_retention_window`（A9，不在本计划范围）。
- `tests/corpus/test_live_fetch.py` 真实访问 sec.gov；网络不稳不要为它改代码。

## Tasks (ordered by dependency)

Each numbered item is one tracer bullet: one vertical-slice outcome, one tag `[implementation]`, one commit, headroom for the implementer.

1. **抽出 append-only JSONL ledger 基础设施** — tag: `[implementation]`
   - **Inherits:** `plans/retrieve-maintainability-followup.md` T1：一处内部通用 append-only JSONL 组件承担写入、重放、以及 I/O 与损坏内容的错误包装；五个领域 ledger 改在其之上实现，各自保留领域错误类型与 code；`push_journal_corrupt` / `invalidation_ledger_unwritable` / `degradation_ledger_corrupt` / `mapping_failure_ledger_corrupt` / `input_validation_ledger_corrupt` 逐字保留。五处重复实现：`input_validation.py` / `degradation.py` / `hit_mapping.py` / `invalidation_port.py` / `push_batching.py` journal。
   - **Surface:** `src/fin/retrieve/` 内既有领域 ledger 调用点；Plan A 已允许单层内部模块（例如 `_ledger/` 或同等），新文件名由实现者定。
   - **Acceptance:** 领域 ledger 不再各自重写 append / replay / 错误包装三件套（rg 与 AST 双重自证）；对外错误 code 与行为不变；`uv run pytest -q` passed 数 ≥ 1758、failed 数 ≤ 1（仅预存 memory dream）。
   - Status: [ ] pending
   - **默认第一刀**

2. **拆分 concurrency runner 为 driver / report / validation** — tag: `[implementation]`
   - **Inherits:** `plans/retrieve-maintainability-followup.md` T2：按场景拆出 driver、report、validation 三部分，共享执行原语保留在一处；opt-in CLI 调用形式与落盘格式不变；同一进程内「串行假通过」必须仍被既有 barrier 驱动的并发幂等测试捕获。
   - **Surface:** `src/fin/eval/concurrency/`（既有 runner 所在包）
   - **Acceptance:** L1 / L2 / 幂等 driver、落盘 report、断言 validation 分责；共享执行原语单一权威；`tests/eval/test_concurrency.py` 全绿；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] 与 T3（均只依赖 T1）

3. **拆分 input_validation 与 invalidation_port** — tag: `[implementation]`
   - **Inherits:** `plans/retrieve-maintainability-followup.md` T3：`input_validation` 拆为错误注册 / 持久化 / 分类规则 / 观察；`invalidation_port` 拆为 codec / ledger / port operation；对外导出面不扩大（既有 import 路径保留为 re-export）；持久化由 T1 抽出的 ledger 基础设施承载。
   - **Surface:** `src/fin/retrieve/` 内既有 `input_validation` 与 `invalidation_port` 模块
   - **Acceptance:** T18（`tests/retrieve/test_invalidation.py` / `tests/retrieve/test_invalidation_port.py`）与 T21（`tests/retrieve/test_input_validation.py` / `tests/host/test_input_validation_exit.py`）全绿；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [blocks: T1]
   - [parallel] 与 T2

4. **拆分 capabilities：snapshot 与 static switch scanner** — tag: `[implementation]`
   - **Inherits:** `plans/retrieve-maintainability-followup.md` T4：snapshot/enumeration 与 static switch scanner 分离；T19 的 8 键逐字断言与「产品路径开关命中数 = 0」两条断言不变；`PRODUCT_CALL_PATH_FILES`（`port.py` / `tool.py` / `mcp_adapter.py` / `graph.py` / `compile.py`）列表的单一权威位置不变。
   - **Surface:** `src/fin/retrieve/` 内既有 capabilities 模块（Plan A 已允许包化，子文件名由实现者定）
   - **Acceptance:** `load_capability_keys` / `published_capability_keys` / `assert_snapshot_matches_published` 与 `scan_source_for_capability_switches` / `capability_switch_hits` 分责；`tests/retrieve/test_capability_static_check.py` 与 `tests/retrieve/test_capabilities.py` 全过；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [parallel] 与 T1 / T5 / T6 / T7（与 A1 无硬依赖）

5. **拆分 ablation matrix 构建与 B1 口径解析** — tag: `[implementation]`
   - **Inherits:** `plans/retrieve-maintainability-followup.md` T5：ablation matrix 构建与 B1 并发契约解析拆为独立模块；§B1 动态读取与 drift 校验仍是单一权威、仍在 `uv run python -m fin.eval.ablation run` 入口生效；不允许在本地另立 §B1 第二套口径。锁定：本弹不碰 `specs/corpus-retrieval.md` §B1 正文。
   - **Surface:** `src/fin/eval/ablation/` 既有 matrix 所在包
   - **Acceptance:** T22 矩阵编排（10 配置 / 40 题 / 重复 3 次）与 T23 对 §B1 各行动态读取分责；`tests/eval/test_ablation.py` 与 `tests/eval/test_concurrency.py` 全绿；spec 文件无 diff；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [parallel] 与 T1 / T4 / T6 / T7（与 A1 无硬依赖）

6. **fin 侧剥键警告（mock 无 `.strict()`）** — tag: `[implementation]`
   - **Inherits:** Plan A A6 / `docs/handoff/2026-09-01-pr225-merged.md` 缺口 1；`docs/cross-repo/alignment-proposal.md` 缺口 1；T16 `tests/retrieve/test_push_failures.py` 既有静默失败检测路径。锁定：只做 fin 侧；不改 iknow-memory；警告不得降为 debug。
   - **Surface:** `src/fin/retrieve/mcp_adapter.py` 与既有 `tests/retrieve/test_mcp_adapter.py`
   - **Acceptance:** mock zod schema 无 `.strict()`；fin 发送 `top_k` / `filter` / `document_id` 后被剥键时，至少一条 warning 级日志与一条 `degraded_reasons` 项；ToolMessage 不静默吞掉被剥字段；脚本化 port + 脚本化 `_call_mcp`；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [parallel] 与 T4 / T5 / T7

7. **两处 DEFAULT_MCP_COMMAND 放宽为 `("iknow-memory",)`** — tag: `[implementation]`
   - **Inherits:** Plan A A7 / 缺口 3：`src/fin/host/compile.py` 与 `src/fin/host/graph.py` 的 `DEFAULT_MCP_COMMAND = ("iknow-memory", "serve")` → `("iknow-memory",)`；先走 stdio MCP（`npm start` 形态可启动）；纯兜底；常量变更不入 ADR、不入 spec；注释说明「兜底非契约」。锁定：只改这两处常量。
   - **Surface:** `src/fin/host/compile.py` 与 `src/fin/host/graph.py` 既有常量
   - **Acceptance:** 两处常量值同步为 `("iknow-memory",)`；不触其他 host 文件；默认 `uv run pytest -q` 不增加 regression。
   - Status: [ ] pending
   - [parallel] 与 T4 / T5 / T6

## 不在本计划范围

- A8 数据集 known_gaps（pdf 12 < 20、chunk_total 15686 < 20000；`uv run python -m fin.corpus.dataset expand-primary` / EDGAR）—— 仍卡 Plan A CHECKPOINT，本轮不做。
- A9 预存失败 `tests/host/test_memory_dream.py::test_gc_hard_deletes_transported_rows_past_the_retention_window` —— 另开 plan。
- B6 / `specs/corpus-retrieval.md` §B1–§B6 正文与跨仓对齐基线措辞 —— 本轮不改。
- 阶段 2（D）入库推送对接与跨仓契约缺口 1–6 真实联调。
- iknow-memory 仓 `plans/retrieve-graph-quality.md` T2–T11。
- SC41 mismatch 修复（另开，不阻塞本 plan）。
- Standards 轴完整复审、HEAD 全量回归 —— operator 另定时点。

## 关联工件

| 类型                              | 路径                                         |
| --------------------------------- | -------------------------------------------- |
| 本轮继承的 Plan A                 | `plans/2026-09-01-pr225-followup-plan-a.md`  |
| 维护性 follow-up（T1–T5）         | `plans/retrieve-maintainability-followup.md` |
| 跨仓总计划（阶段划分 + 并行边界） | `plans/retrieval-upgrade.md`                 |
| fin 侧分计划                      | `plans/corpus-retrieval.md`                  |
| Spec（只读）                      | `specs/corpus-retrieval.md` §B1–§B6          |
| 上版 handoff                      | `docs/handoff/2026-09-01-pr225-merged.md`    |
| 跨仓对齐提案                      | `docs/cross-repo/alignment-proposal.md`      |

## 收尾

每颗 bullet 走 header 中的 Per-ticket loop，一弹一 commit。全部 A1–A7 落地后，对整轮 diff 做一次 code review（不 per-commit 重复），再跑一轮 verification-before-completion。不进入 A8 / A9 / D。
