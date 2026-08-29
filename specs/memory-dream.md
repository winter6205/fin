# Spec: memory-dream

## Objective

让 **dream** 成为 fin 里真正会运行的一步，而不是一个没人调用的类。

`docs/CONTEXT.md:45` 对 dream 的定义（原文引用，本 spec 不重新定义）：

> **dream** (fin): A rule-gated consolidate step that selects useful local buffer memory and transports it to the configured **memory backend** MCP for durable persistence. With memory OFF or MCP absent it skips or stops at the Postgres buffer; it must not fail the foreground turn.

当前 that pull request 不满足这个定义，六处偏离（证据指向未合并的 `cursor/mdp-t6-dream-memory-mcp-c371`）：

1. 落库目标 `memory_store` 不存在 —— iknow-memory 只暴露 `echo` / `ingest` / `retrieve`（`src/tools/registry.ts`），且参数形状（T6 发批量 `memories` 数组 vs `ingest` 要单条 `content`+`source_ref`）与返回字段（T6 认 `stored`/`written`/`count` vs `ingest` 回 `chunk_ids`）都不匹配。
2. 只转发不整理 —— `_is_dreamable()` 是准入门槛而非有用性判定，随后 `copy.deepcopy` 原行整发，无合并、去重、压缩或打分。
3. 未接管 T5 预留的接线点 —— `LocalMemoryBuffer.dream()` 仍返回 `{"status": "deferred"}`，T6 另建了平行的 `MemoryDream`。
4. 无触发条件 —— 只能手工调用，无闸门无游标，graph 与 HTTP 层均未接线。
5. 落库后无状态回写 —— `_transported_ids` 是实例级内存集合，进程重启即忘，buffer 行始终 active。
6. MCP 传输无 `initialize` 握手 —— 沿用主线 `retrieve/mcp_adapter.py` 的既有写法，是既有技术债，归 T6c 处理，不阻塞本 spec。

用户是 fin 的操作员：他要的是"dream 时整理记忆，把有用的（时间久、反复用到、或其他原因）筛出来存进持久记忆"。

## Boundaries

- **Does:**
  - **T6a** — 把 working buffer 落 Postgres（**回归** ADR 0035 "Local Postgres holds the working buffer"：当前进程内存实现是偏离，不是新功能），补上打分所需的 `hit_count`、闸门所需的 `thread_id` 来源、以及 dream 游标。
  - **T6b** — 梦境本体：规则打分整理、**可选且默认关闭的 LLM 归纳**、双闸门触发、HTTP 层接线、落库后软禁与 GC 延迟硬删、可注入的落库 backend 接缝。
- **Confirms with human:**
  - 打分阈值与 GC 保留窗口的定稿数值（本 spec 给出初始定值；校准 OQ2 时可调，但 spec 内不留"建议"字样）。
  - T6c 落库目标：走现有 `ingest` 还是推动 iknow-memory 新增 `memory_store`（等后端定稿）。
- **Out of this spec:**
  - 真实 MCP 落库（T6c，阻塞于 iknow-memory 的 memory tool）。
  - retrieve 通道的 `initialize` 握手缺陷修复（主线既有债，T6c 顺带处理）。
  - 任何前端改动、Redis、retrieve port、citation verifier、iknow-memory 仓库。

**分票与提交**：继承 `specs/memory-dialogue-platform.md:109` 的"一票一 commit"约定 —— **T6a 与 T6b 各一个 commit、各一条 ticket 分支**，不打包。

**T6c 未落地期的 dream 形态**：默认 backend 为 `NullDreamBackend`，此时 dream 恒定 **停在 Postgres buffer**（`CONTEXT.md:45` 明确允许的 "stops at the Postgres buffer" 形态），不做真落库。落库后的状态流转（SC17/SC18）通过**测试注入的 fake backend** 验证，不依赖真实 MCP。

**Test allocation（边界五类，继承父 spec:64 的约定）**

| 类别 | 本 spec 的具体实例 |
|---|---|
| empty | 空表 / 空 metadata / 游标表无行（= 从未 dream）/ 无入选候选 |
| negative | 非法 limit、非 int `hit_count`、游标时间在未来、畸形行、打分输入非法 |
| overflow | 候选数超 `MEMORY_LIMIT_CAP`（截断而非全发）、超长 text、`hit_count` 溢出 |
| concurrent | 同一窗口并发触发 dream（行锁串行化，只跑一次）；并发 `recall()` 累加 `hit_count`（原子 `UPDATE`） |
| exception | PG 不可用 / 池未 open / 连接获取超时、游标损坏、backend 抛错、打分抛错 |

## Success Criteria

每条都可断言（一条命令 exit 0，或一个可断言的性质）。

**默认 OFF（最高优先级）**

1. `uv run pytest -q` exits 0。
2. memory OFF 时**不建立任何 Postgres 连接、不建表、不启动 dream**：在 `FIN_CHECKPOINT_URL` 未设置的环境下全套测试全过。
3. `pyproject.toml` 显式声明 `psycopg-pool` 并同步 lockfile —— T1 的 `db_pool.py:17` 导入了 `psycopg_pool`，但它目前只是 `langgraph-checkpoint-postgres` 的传递依赖，未声明即隐式依赖。本 spec 把它补成显式。**除此之外不引入任何新依赖**。

**T6a — 存储**

4. memory ON 且配置了 PG 时，`build_memory_port` 返回的 buffer 走 Postgres；`extract` / `recall` / `gc` 读写 `fin_memory` 表。需要真实 PG 的测试是 **opt-in**（默认 `pytest -q` 下跳过，与 `test_thread_store.py` 对 `FIN_CHECKPOINT_URL` 的现处理方式一致），逻辑契约用注入的 fake 存储验证。
5. memory ON 但无 PG 时仍 raise `MemoryBackendUnavailableError`（保留 T3 防呆，不让显式 ON 静默降级成 OFF）。
6. **memory ON 不要求 MCP**（`CONTEXT.md:42` "*Avoid*: requiring MCP for memory ON"）：仅有 PG 时 memory 完整可用，MCP 只是 dream 的落库目标。
7. `recall()` 命中一次 `hit_count` 加一：连续两次相同 query recall 后该行 `hit_count == 2`。并发下用 `UPDATE ... SET hit_count = hit_count + 1`，不做读改写。
8. **`thread_id` 是闸门的唯一数据源**：`extract()` 的调用方（host 层）必须把 `thread_id` 写入该行的 metadata；缺失 `thread_id` 的行**不计入**会话闸门，并 typed 记录一次降级，不静默丢弃。
9. `fin_memory` 是独立表、幂等 DDL 建立，**不写入 checkpointer**（`CONTEXT.md:38` "*Avoid*: dual-writing memory bodies into the checkpointer as SSOT"）。
10. PG 实现与进程内实现**共用同一套测试套件**（参数化注入两种存储），二者实现同一 `MemoryPort` 且行为一致。
11. 游标表 `fin_memory_dream_cursor` 落 PG，不放 Redis（`CONTEXT.md:42` "*Avoid*: using Redis as memory SSOT"）。

**T6b — 整理与触发**

12. 打分器是**纯函数**：给定（行集合, now）返回确定的分数与入选列表，无 IO、无随机、可单测。初始定值：`hit_count >= 2` → +3；`hit_count == 1` → +1；存活 ≥ 7 天 → +2；被 supersede → 排除；总分 ≥ 3 入选。
13. LLM 归纳是**可注入的可选项，默认关闭**；memory OFF 时**绝不**跑 —— 可断言：OFF 下 dream 调用产生 0 次模型调用。开启后归纳失败（模型抛错或返回不可解析结果）→ `DreamInputError`，该批记忆保持原样留在 buffer，不丢弃、不部分落库。
14. 时间闸未满足（< 24h）不触发；时间满足但新增 distinct `thread_id` < 5 不触发；两者都满足才触发 —— 三条各有独立断言。
15. 计数单位是 `thread_id` 而非 `run_id` —— 可断言：同一 `thread_id` 的 5 次 run 之后闸门仍不满足。
16. 触发后推进游标（成功或跳过都推进），同一窗口内并发调用 **只执行一次**。
17. dream **不在 graph 节点里**（`CONTEXT.md:46` "*Avoid*: running dream inside model_node"）：挂在 HTTP 层、SSE 流结束后触发 —— 可断言：`compile_graph` 产出的节点集合里没有 dream 节点。

**T6b — 落库后状态与失败路径**

18. backend **确认写入**（backend 的 `persist()` 正常返回且返回数 ≥ 1）后，对应行软禁为 `status="disabled"` / `disabled_reason="transported"`，此后 `recall()` 不再返回该行。用注入的 fake backend 验证。
19. `gc()` 对已出库且超过保留窗口（7 天）的行执行**硬删**；未出库的行仍然只软禁、不删。用注入的 fake backend 验证。
20. backend 未配置时 dream 返回 `buffered` 状态而非抛错，且行保持 active。
21. **dream 的任何失败都不影响前台 turn**：失败时该 turn 仍正常 `done` / `refuse`，错误以本 spec 命名的异常类型记录，不冒泡。

## Open Questions

1. T6c 落库目标：`ingest`（现有，但记忆会混进 filing store 并占用 retrieve 的 `top_k`）还是推动 iknow-memory 新增 `memory_store`（语义干净，推荐）—— 阻塞于后端，本 spec 不定。
2. SC12 的阈值（+3 / +1 / +2、入选阈值 3）与 SC19 的 7 天保留窗口，是否需要在真实对话数据上校准后再定稿。

## Inherits / Changes

**Inherits（本工作区已提供，本 spec 依赖它）**

- `docs/CONTEXT.md` 的领域术语，本 spec 原样引用、不重新定义：**memory port**（:37）、**memory backend**（:41）、**dream**（:45）、**iknow-memory**（:33）。
- **ADR 0035**（accepted）："Local Postgres holds the working buffer (extract / recall / GC)"；"retrieve MCP and memory MCP must not share one implied config"；"Memory recall is advisory only and must never satisfy the citation verifier"。
- `specs/memory-dialogue-platform.md`：Focus 3 与 SC7（本 spec 的父合同）、:64 的 Test allocation 约定、:109 的一票一 commit 约定。
- 已合入 main 的实现：T1 `PostgresPool`（显式 `open()`/`close()` + `@contextmanager connection()`）、T3 `MemoryPort` 与默认 OFF 装配、T5 `LocalMemoryBuffer`。
- 既有约定：幂等 DDL（`thread_store.py`）、retrieve adapter 的装配链路（`compile.py` env → `compile_graph` → HTTP）、测试命令 `uv run pytest -q`。

**Changes（本 spec 新增或有意反转）**

- 表：`fin_memory`（新增 `hit_count`、`transported_at`）、`fin_memory_dream_cursor`。
- 新异常类型，谱系明确：**`MemoryDreamError(MemoryPortError)`** 为基类，子类 `DreamInputError`、`DreamTransportError`、`DreamCursorError`。**注**：不取名 `DreamBackendUnavailableError`，以避讳 T3 已有的 `MemoryBackendUnavailableError`（`memory_port.py:34`）。**失败路径点名**：游标损坏 → `DreamCursorError`（不静默重置）；池未 open 或连接获取超时 → 复用 T1 的 `PostgresPoolClosedError` / `PostgresPoolAcquireError`；打分或归纳输入非法 → `DreamInputError`；backend 抛错 → `DreamTransportError`。
- 新契约（只钉形状，具体命名归 writing-plans，父 spec:116 授权 "implementer chooses name"）：打分器 `MemoryDreamScorer`（`score(rows, now) -> list[ScoredMemory]`）、游标 `DreamCursorStore`（`load()` / `advance()`）。
- 新接缝：`MemoryDreamBackend`（落库 backend），归属 `CONTEXT.md:37` 的 "dream transport"，默认实现 `NullDreamBackend`。
- 新状态实体：**游标**（`last_dream_at` / `seen_thread_ids` / `last_status`），持久化在 PG，不放 Redis。
- 依赖：显式声明 `psycopg-pool`（SC3）。
- **契约反转（有意，需知会）**：T5 `memory_buffer.py` 的 `entries()` docstring 承诺 "GC never deletes authority data"，父 spec:79 亦有 "without deleting authority rows"。本 spec **反转**这一不变式，但**仅限已出库行**：已 `transported` 且超保留窗口的行由 `gc()` 硬删；未出库的行仍只软禁、不删。理由：已落库的记忆留在本地缓冲区会持续膨胀且不再有读价值。
- 术语：无新增领域术语（dream / memory port / memory backend 均已在 CONTEXT.md 中定义）。

**待写入：** 无（本 spec 不新增领域术语，也不产生单向门 ADR —— T6c 的 MCP 传输选型若最终引入官方 SDK 或决定自建共用传输层，届时需补一条 ADR）。
