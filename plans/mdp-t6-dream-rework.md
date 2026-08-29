# Plan: mdp-t6-dream-rework

**Goal:** 让 dream 从"一个没人调用的类"变成 fin 里真正会运行的一步 —— 整理出有用的记忆、由闸门触发、交给可替换的 backend 落库。

**Approach:** 四票串行。先把 working buffer 落到 Postgres 并补上打分与闸门所需的数据基础（这是回归 ADR 0035，不是新功能）；再实现梦境核心的整理与闸门逻辑；然后把它接进运行时，让它在真实请求结束后自动跑起来；最后一步真实 MCP 落库被 iknow-memory 后端阻塞，记为待开票。后两票都靠注入的 fake backend 保持可演示，不依赖真实 MCP。

**Spec link:** `specs/memory-dialogue-platform.md`（父，Focus 3 / SC7）、`specs/memory-dream.md`（本轮合同）

**Tracker:** 本地 `plans/mdp-t6-dream-rework.md`（fallback）—— 父 plan `plans/memory-dialogue-platform.md` 明确 "local plans only — do not embed GitHub issue numbers in this file"，故不建 GitHub issues。

**Per-ticket loop (all bullets):** tdd → typecheck+tests（`uv run pytest -q`）→ verification-before-completion → 一个 commit on the ticket branch。**代码审查按操作员指示不在 per-ticket loop 内**，整轮结束后视情况集中一次。

**ACR（自 `architecture-change-reviewer`，spec 复审时取得）**

```
bounded-context-guardian: yes — 契约反转显式声明且限定已出库行，未出库仍只软禁；MemoryDreamBackend 归属 CONTEXT:37；表与接缝仍在 host 能力内。
defensive-contract-validator: yes — Test allocation 五类逐类给实例并继承父 spec:64；thread_id 数据源与 typed 降级已封。
error-handling-enforcer: yes — 谱系钉为 MemoryDreamError(MemoryPortError)，三子类；四条失败路径全部点名；归纳失败 → DreamInputError 且"不丢弃、不部分落库"。
complexity-anti-drift: yes — 纯函数签名、只钉契约形状不钉文件名、分票、装配位置，均无 god-module 意图。
minimal-change-verifier: yes — T6a 定性为回归；继承一票一 commit；psycopg-pool 补成显式声明。
OVERALL: PASS
```

**待写入:** 空 —— 本轮不新增领域术语（dream / memory port / memory backend 均已在 `docs/CONTEXT.md` 定义），不产生单向门 ADR。若 T6d 最终决定引入官方 MCP SDK 或自建共用传输层，届时该票需补一条 ADR 并记在自己的 待写入 上。

---

## Harvest

**已决（来自 spec 与操作员裁决，实现者不得改）**

- dream 的含义以 `docs/CONTEXT.md:45` 为准：**rule-gated consolidate** → transport，MCP 缺失时**停在 Postgres buffer**，且**不得让前台 turn 失败**。
- 有用性判定：规则打分优先；LLM 归纳可注入、**默认关闭**、memory OFF 时绝不跑。
- 触发：双闸门 —— 距上次 dream ≥ 24h **且** 新增 distinct `thread_id` ≥ 5。计数单位是 `thread_id` 不是 `run_id`。
- 落库后：软禁（`status="disabled"`, `disabled_reason="transported"`）+ `gc()` 对已出库且超 7 天的行硬删。**这是有意反转 T5 的 "GC never deletes authority data"，仅限已出库行**。
- dream **不进 graph 节点**（`CONTEXT.md:46` "*Avoid*: running dream inside model_node"），挂 HTTP 层、SSE 流结束后触发。
- memory ON **不要求 MCP**（`CONTEXT.md:42` "*Avoid*: requiring MCP for memory ON"）。
- 数据不写入 checkpointer（`CONTEXT.md:38`）、游标不放 Redis（`CONTEXT.md:42`）、memory 与 retrieve 不共用 config key（ADR 0035）。

**开放（implementer headroom）**

- 表名、类名、文件划分、helper 拆分 —— 实现者自选（父 spec:116 授权 "implementer chooses name"）。spec 只钉了契约形状：`MemoryDreamScorer.score(rows, now) -> list[ScoredMemory]`、`DreamCursorStore.load()` / `advance()`。
- 打分阈值（+3 / +1 / +2，入选阈值 3）与 7 天保留窗口是**初始定值**，OQ2 校准时可调整。
- 索引、分区、连接池参数调优。

---

## Tasks (ordered by dependency)

1. **T6a — memory working buffer 落 Postgres** — tag: `[implementation]`
   - **Inherits:** ADR 0035 "Local Postgres holds the working buffer (extract / recall / GC)"；`specs/memory-dream.md` SC3–SC11。**注：当前 `LocalMemoryBuffer` 是进程内 dict，属偏离；本票是回归契约，不是新功能。**
   - **Surface:** `host`（memory 能力上下文）+ T1 的 `PostgresPool`（`src/fin/host/db_pool.py`）
   - **Acceptance:**
     - memory ON 且配置了 PG 时，buffer 走 Postgres；`extract` / `recall` / `gc` 读写 Postgres，进程重启后数据仍在。
     - memory ON 但无 PG 时仍 raise `MemoryBackendUnavailableError`（保留 T3 防呆）。
     - 进程内实现与 PG 实现**共用同一套测试套件**（参数化注入），二者行为一致。
     - 幂等 DDL 建表，表独立于 checkpointer。
     - `pyproject.toml` 显式声明 `psycopg-pool` 并同步 lockfile（目前只是传递依赖）。
     - 需要真实 PG 的测试是 **opt-in**（无 `FIN_CHECKPOINT_URL` 时 skip，与 `test_thread_store.py` 现处理方式一致）；`uv run pytest -q` 默认 exits 0。
   - Status: [ ] pending

2. **T6b — 打分整理与双闸门** — tag: `[implementation]`
   - **Inherits:** `specs/memory-dream.md` SC7–SC8、SC12–SC16、SC21；Test allocation 五类
   - **Surface:** `host`（memory 能力上下文）
   - **Acceptance:**
     - 打分器是纯函数：给定（行集合, now）返回确定的分数与入选列表，无 IO 无随机。初始定值：`hit_count>=2` → +3；`hit_count==1` → +1；存活 ≥7 天 → +2；被 supersede → 排除；总分 ≥3 入选。
     - 三条闸门断言各自独立：时间闸未满足不触发；时间满足但新增 distinct `thread_id` <5 不触发；两者都满足才触发。同一 `thread_id` 的 5 次 run 之后闸门仍不满足（证明计数单位是 thread 而非 run）。
     - `recall()` 命中一次 `hit_count` 加一，并发下用原子 `UPDATE ... SET hit_count = hit_count + 1`，不做读改写。
     - 缺 `thread_id` 的行不计入闸门，并 typed 降级记录一次，不静默丢弃。
     - 触发后推进游标（成功或跳过都推进）；同一窗口并发调用只执行一次。
     - 游标损坏 → `DreamCursorError`，不静默重置。
     - LLM 归纳默认关闭：OFF 下 dream 产生 0 次模型调用；开启后归纳失败 → `DreamInputError`，该批记忆保持原样留在 buffer，不丢弃、不部分落库。
   - Status: [ ] pending
   - [blocks: T6a]

3. **T6c — 接线装配与落库后状态流转** — tag: `[implementation]`
   - **Inherits:** `specs/memory-dream.md` SC17–SC20；`CONTEXT.md:46` 不得在 model_node 里做梦；ADR 0035 不得与 retrieve 共用 config key
   - **Surface:** `host`（`compile.py` 装配链路 + HTTP 层）
   - **Acceptance:**
     - `compile_graph` 产出的节点集合里**没有 dream 节点**；dream 挂在 HTTP 层，SSE 流结束后由 `BackgroundTasks` 触发。
     - 装配走独立 config key（仿 retrieve adapter 的 env → `compile_graph` → HTTP 链路），不与 retrieve MCP 共用。
     - `MemoryDreamBackend` 接缝可用，默认 `NullDreamBackend`：未配置时 dream 返回 `buffered` 且行保持 active（即 `CONTEXT.md:45` 允许的 "stops at the Postgres buffer" 形态）。
     - 用注入的 fake backend 验证：backend 确认写入（`persist()` 正常返回且返回数 ≥1）后，对应行软禁为 `status="disabled"` / `disabled_reason="transported"`，此后 `recall()` 不再返回该行。
     - `gc()` 对已出库且超 7 天的行硬删；**未出库的行仍只软禁、不删**。
     - 任何失败都不影响前台 turn：失败时该 turn 仍正常 `done` / `refuse`，错误以 `MemoryDreamError` 谱系记录，不冒泡。
     - **Headroom 检验**：实现者可用不同的文件名、helper 拆分、测试布局达成以上验收。
   - Status: [ ] pending
   - [blocks: T6b]

4. **T6d — MCP 传输与落库契约** — tag: `[implementation]`
   - **Inherits:** `specs/memory-dream.md` OQ1（未决）；`CONTEXT.md:33` iknow-memory 是独立 repo
   - **Surface:** `host`（memory backend 适配）+ `retrieve`（共用 stdio 传输层时涉及）
   - **Status: 阻塞 —— iknow-memory 的 memory tool 尚未就绪，本轮不排期。**
   - **开票时需包含：**
     - 完整 `initialize` → `notifications/initialized` → `tools/call` 握手，**递增 request id**（不再硬编码 1）。
     - retrieve 与 memory 两条通道共用传输层，顺带修掉主线 `retrieve/mcp_adapter.py` 的 `-32002` 隐患（`DEFAULT_RETRIEVE_ADAPTER = "mcp"`，`compile.py:116`）。
     - 落库目标二选一：走现有 `ingest`（不阻塞，但记忆混入 filing store 会占用 retrieve 的 `top_k`，且现有 `filters` 无法按 metadata 排除），或推动 iknow-memory 新增 `memory_store`（**推荐**，语义干净、支持批量、不污染 filing 检索）。
     - 响应解析不依赖自造的 `stored`/`written`/`count` 字段。
     - 若决定引入官方 MCP SDK 或自建共用传输层，需补一条 ADR 并更新 lockfile。
   - Status: [ ] pending (blocked)
   - [blocks: T6c]

---

## 依赖图

```
T6a (buffer 落 PG + 数据基础)
  └─ T6b (打分整理 + 双闸门)
       └─ T6c (接线装配 + 落库后状态流转)
            └─ T6d (MCP 传输) ── blocked by iknow-memory tool
```

T6a 必须最先：`hit_count`、`thread_id`、游标都是 T6b 的输入。T6c 与 T6b 之间无 true parallelism（T6c 消费 T6b 的打分与闸门结果）。

---

## 复审提醒（来自 architecture-change-reviewer，实现时注意）

- T6a 先于 T6b —— 游标与 `hit_count` 是 T6b 的输入，顺序颠倒会做两遍。
- T6a 的真实 PG 测试**必须走 opt-in skip**（`test_thread_store.py:518` 是现成范例），否则 spec 的 SC1/SC2（默认 OFF 零副作用、`pytest -q` exits 0）会被破坏。
