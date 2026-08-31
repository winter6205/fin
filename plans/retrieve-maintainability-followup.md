# Plan: retrieve 可维护性 follow-up（corpus-retrieval 轮末 Standards 遗留）

**Goal:** 收拢 `plans/corpus-retrieval.md` T14–T24 那一轮留下的 Standards 轴 Medium/Low：拆开越过软阈值的模块、消掉五份重复的 JSONL ledger 实现、给并发路径补上真正的同步保护。**不改任何对外契约与字段名。**

**Approach:** 上一轮是按 ticket 纵向切的，每弹自带「错误家族 + ledger + 驱动」三件套，横向重复因此逐弹累积。本轮反过来横切：先抽出 append-only JSONL 基础设施这一层（T1），再让各模块按边界拆分落到它上面（T2–T5），最后补并发同步（T6）。并发那两条排在最后是因为它们要改的临界区在拆分后位置会变。

**Basis:** `plans/corpus-retrieval.md` T14–T24 轮末 code review，Standards 轴 `OVERALL: 0 High / 8 Medium / 1 Low`（base `f89fd7c` → HEAD `8faf992`）。Spec 轴 3 个 High 与复审的 1 High / 1 Medium / 1 Low 已在该轮内修完，不在本计划范围。
**Spec link:** `specs/corpus-retrieval.md`（契约不变，本轮只动内部结构）
**Per-ticket loop:** test-driven-development → `uv run pytest -q` → verification-before-completion → one commit
**Tracker:** 本文件。

## 前置约束（全轮适用）

- **对外契约一字不改**：`source_ref` / `retrieval_id` / `degraded` / `degraded_reasons` / 8 个能力键 / 全部已具名的稳定 error code，拼写与语义都不得变动。
- §B1 的数字口径继续回源 spec，不得本地另立。
- `src/fin/host/citation_gate.py` 不改。
- 每弹完成后 `uv run pytest -q` 的 passed 数**不应减少**（纯结构调整，不删测试）；参考基线 **1758 passed / 1 failed / 46 skipped**，那 1 failed 是预存的 `tests/host/test_memory_dream.py::test_gc_hard_deletes_transported_rows_past_the_retention_window`（与本计划无关，不要修）。
- `tests/corpus/test_live_fetch.py` 会真实访问 sec.gov，网络不稳时会超时失败，不要为它改代码。

## Tasks (ordered by dependency)

1. **抽出 append-only JSONL ledger 基础设施** — tag: `[refactor]`
   - **Findings:** Standards Low —— `input_validation.py:311-360`、`degradation.py:365-412`、`hit_mapping.py:236-284`、`invalidation_port.py:472-523`、`push_batching.py` 的 journal 各自重复实现了 append / replay / 错误包装。
   - **Acceptance:** 一处内部通用 append-only JSONL 组件，承担写入、重放、以及 I/O 与损坏内容的错误包装；五个领域 ledger 改为在其之上实现，各自**保留**自己的领域错误类型与 code（错误 code 与外部可见行为零变化）；重复实现消失。
   - **Completion:** 组件的位置与命名由实现者定；「领域错误族不被合并成一个通用错误」是硬约束——`push_journal_corrupt` / `invalidation_ledger_unwritable` 等 code 必须逐字保留。
   - Status: [ ] pending

2. **拆分 `src/fin/eval/concurrency/runner.py`（911 行）** — tag: `[refactor]`
   - **Findings:** Standards Medium —— 单文件同时承载 L1 / L2 / 幂等三个场景的 driver、报告模型与校验逻辑，发散式变化。
   - **Acceptance:** 按场景拆出 driver、report、validation 三部分，共享执行原语保留在一处；opt-in CLI 调用形式与落盘格式不变；T23 既有测试全绿。
   - [blocks: T1]
   - Status: [ ] pending

3. **拆分 `src/fin/retrieve/input_validation.py`（675 行）与 `invalidation_port.py`（612 行）** — tag: `[refactor]`
   - **Findings:** Standards Medium ×2 —— 前者同时负责错误注册、ledger、四类 validator 与 outcome 观察；后者同时含协议解析、ledger 持久化与 invalidation 执行。
   - **Acceptance:** 各自按职责拆开（持久化 / 分类规则 / 观察，以及 codec / ledger / port operation）；T18、T21 既有测试全绿；对外导出面不扩大。
   - [blocks: T1]
   - Status: [ ] pending

4. **拆分 `src/fin/retrieve/capabilities.py`（522 行）** — tag: `[refactor]`
   - **Findings:** Standards Medium —— snapshot 校验与 product-path AST 扫描是两类职责。
   - **Acceptance:** snapshot/enumeration 与 static switch scanner 分离；T19 的 8 键逐字断言与「产品路径开关命中数 = 0」两条断言不变且仍绿。
   - Status: [ ] pending

5. **拆分 `src/fin/eval/ablation/matrix.py`（543 行）** — tag: `[refactor]`
   - **Findings:** Standards Medium —— T22 的 ablation matrix 构建与 T23 的 concurrency contract 解析挤在同一文件。
   - **Acceptance:** 两者拆为独立模块；§B1 的动态读取与 drift 校验仍是单一权威、仍在 run 入口生效；T22、T23 既有测试全绿。
   - Status: [ ] pending

6. **并发路径补同步保护** — tag: `[implementation]`
   - **Findings:** Standards Medium ×2 —— `fake_push_port.py:160-176` 的 `_by_key` check-then-write 无锁，导致 `tests/eval/test_concurrency.py:220-230` 的并发幂等场景**测不出真正的竞争**；`graph.py:1388-1395` 把 ledger 共享给 fan-out worker，但 `degradation.py` / `hit_mapping.py` 的记录与文件追加没有同步保护。
   - **Acceptance:** 契约 fake 的完整「检查-写入」临界区加锁，并有一条**确定性**的竞争测试（不是靠 sleep 碰运气）；ledger 的记录与 append 走线程安全路径，并有并发记录测试；SC18 的幂等断言在加锁后仍成立且现在是真的在测并发。
   - **Completion:** 同步原语的选择由实现者定；「并发幂等场景必须能实际捕获竞争」是硬约束——加锁不得把测试变成串行假通过。
   - [blocks: T1]
   - Status: [x] completed

7. **统一 `observe_input_outcome` 的两条记录入口** — tag: `[implementation]`
   - **Findings:** Standards Medium —— `input_validation.py:595-623` 的异常路径调用严格的 `record_input_failure`，error-envelope 路径却直接 `ledger.record`，两条路径的校验强度已不一致。
   - **Acceptance:** 两条路径复用同一个记录入口，envelope 路径同样校验 `degraded` 与 code/class 的一致性；T21 既有测试全绿，并补一条 envelope 路径校验生效的测试。
   - [blocks: T1]
   - Status: [ ] pending

## 不在本计划范围

- 线 B（**iknow-memory** 仓库）的真实联调 —— 该仓库就绪后另开。
- T23 的 B1 达标压测（当前只有缩短参数的 smoke）与 L2 真实 LLM 运行（无凭证）—— 属评测运维，不是可维护性。
- 数据集 known_gaps（pdf 12 < 20、chunk_total 15686 < 20000）—— 补齐入口是 `uv run python -m fin.corpus.dataset expand-primary`，opt-in 且会访问 EDGAR。
- 预存失败 `test_memory_dream.py::test_gc_hard_deletes_...` —— 既有问题，单独处理。
