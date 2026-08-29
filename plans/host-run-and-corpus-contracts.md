# Plan: host-run-and-corpus-contracts

**Goal:** Workspace crash-resume 与 stop 沿用规范 `run_id`；retrieve MCP 失败不可装成 miss；语料发布要么完整可见要么完全不可见；watchlist 与 stream harness 的 typed / exit 出口不再假绿。
**Approach:** 兑现已落地的 workspace / retrieve / ingest 契约，不改 host graph 拓扑、不加 multi-agent。先收口运行身份（resume + stop），retrieve handshake 与语料 publish 可并行；watchlist 与 harness 是侧车，不挡主路径。不引入新 bounded context，不重开 Soft-Track / citation-as-prompt。
**Spec link:** `specs/workspace.md`；retrieve/ingest 契约另引 `specs/s1-corpus-retrieve.md`；MCP 会话形状引 ADR 0032。
**Tracker:** 本地 plan 文件。操作者明确要求本计划不建 GitHub issue、不写 tracker 边；依赖只存在于本文件的 `[blocks:]` / `[parallel]`。
**Per-ticket loop (all bullets):** test-driven-development → typecheck+tests → verification-before-completion → one commit on the ticket branch。整轮子弹落地后再走 code-review（不是每弹重复）。
**待写入:** empty — 未引入新 CONTEXT 术语；resume 沿用既有 **run** / **thread_id**；不 reopen ADR。

## ACR

操作者本槽直接 invoke `writing-plans`（未另跑 ACR agent）。以下为 planner 对照既有 spec/ADR 的 5-verdict 交叉检查；任一行若在实施中变为 no/unclear 则停写代码、先改计划。

bounded-context-guardian: yes — 改动留在既有能力切片 host（workspace HTTP / run 控制）、retrieve、corpus、host watchlist、scripts harness；不新增 `controllers/` / `services/` / `utils/` 技术分层，不新增 bounded context。
defensive-contract-validator: yes — 各弹 Acceptance 覆盖 empty（缺 index / 空 watchlist 文件语义）、negative（非法 shape）、overflow（非本增量主路径）、concurrent（跨实例 watchlist；一 thread 一 active run）、exception（malformed JSON、MCP handshake fail、child non-zero）。
error-handling-enforcer: yes — 失败走既有 typed EXIT（`retrieve_unavailable`、ingest 非零 + typed error、`WatchlistError`）；禁止 malformed 当 missing、禁止空 chunks 装 miss、禁止 `AttributeError`、禁止 PASS JSON 吞掉 child exit。
complexity-anti-drift: yes — 声明结构为契约收口（身份映射、session lifecycle、publish 边界、schema+锁），不改图拓扑、无新 supervisor、无把多能力塞进单函数的意图；阈值 gate 归 `complexity-anti-drift`，不写入 Acceptance。
minimal-change-verifier: yes — 与「一次提交修完全部可靠性」冲突时按子弹拆 commit，不合并；每弹一个逻辑任务；不混入 multi-agent / citation prompt / S2+ 分析能力。

**OVERALL: PASS — hand to writing-plans**

## Harvest（已决 vs 开放）

**已决（各弹 Inherits 引用原文）：**

- `specs/workspace.md`：crash resume 在同一 `thread_id` / `run_id` 上 `stream(None)`，从最后一个完整 host-graph node 继续；不是新问题，不回滚 user turn。send 才 mint `run_id`。
- CONTEXT **run**：follow-up 是同一 `thread_id` 上的 **新 run**（重置 run-scoped evidence）；crash resume 继续 **进行中的** `run_id`。二者不得混用。
- ADR 0016 / ADR 0026：一 thread 至多一个 active run；resume 是 node-complete，不是 mid-token checkpoint。
- `specs/s1-corpus-retrieve.md`：`retrieve_unavailable` = 传输/协议失败，不得用 `{chunks:[]}` 冒充 miss；`sec_http_fail` 时该 accession 不得标 complete。
- ADR 0032：产品 retrieve adapter 是 MCP 2.0 client（initialize → tools/call）；pytest 默认 Fake；BM25 非 fallback。
- ADR 0005 / 0008 / CONTEXT **citation verifier**：引用是代码门；本 plan 不改为 prompt 自报。
- ADR 0027 / 0019：host graph 仍由 fin 编写；本增量不升级 subgraph / supervisor。

**开放（implementer headroom）：**

- resume 身份在 HTTP 层如何接到 checkpointer / turn store（单一 mapping 函数 vs 路由内联），只要可观察键一致。
- MCP session 是 adapter 内顺序调用还是抽出 lifecycle helper。
- live publish 的 staging 形态（临时目录 + 原子替换 vs commit marker），只要读取端看不到半成品。
- watchlist 跨实例互斥（OS 文件锁 vs SQLite/Postgres 事务）。
- harness 失败行如何同时满足人类可读与 machine `[FAIL]` 格式（单一 formatter 即可）。

**本增量明确不做：**

- 拆 multi-agent / 加 `task` 工具 / 改 citation 为 prompt 合同。
- 重开 Soft-Track 拓扑或 issuer-fanout 设计。
- 把 #195 的 21 条 detector eval 再写成第二份 spec；修复后用同一真实 FS/JSON driver 回归即可。

## Tasks (ordered by dependency)

1. **Crash resume 复用规范 `run_id`** — tag: `[implementation]`
   - **Inherits:** `specs/workspace.md`："Process died: open/resume the thread with `stream(None)` (no new HumanMessage) on the same `thread_id` / `run_id` from the last complete node"；CONTEXT **run**："Crash resume continues that in-flight `run_id`"。
   - **Surface:** host / workspace HTTP 与 run 持久化（既有 thread/run/SSE 能力）。
   - **Acceptance:** 创建可恢复 checkpoint 后调用 resume：SSE `status`、optimistic turn、最终存储记录使用同一个规范 `run_id`；不得因 resume 再 mint 一个无法对账的新 UUID。既有 pytest workspace/resume 相关套件保持绿。
   - Status: [x] done

2. **Stop 之后同一 thread 可再 send** — tag: `[implementation]`
   - **Inherits:** CONTEXT **run**："at most one run is active per thread"；`done` / `refuse` / `step_cap` 结束 run，follow-up 是新 run。cooperative stop 结束的是 **active run**，不得留下第二个 user message 属于被停的那一跑，也不得让后续 send 得到 409。
   - **Surface:** host / workspace HTTP 与 run 控制。
   - **Acceptance:** 在阻塞的 scripted run 上 stop → HTTP 200 且带被停 `run_id`；随后 send 非 409；被停那一跑的 HumanMessage 条数不因 stop 再增一条；终端事件顺序仍可结束为 status + answer + citations（或该 track 的既有终端形状）。
   - [blocks: T1]
   - Status: [x] done

3. **[parallel] MCP 业务请求前完成 initialize 握手** — tag: `[implementation]`
   - **Inherits:** ADR 0032："the product adapter is an MCP 2.0 client (initialize → tools/call)"；`specs/s1-corpus-retrieve.md`：`retrieve_unavailable` "does not return `{chunks:[]}` as if a miss"。
   - **Surface:** retrieve 能力切片（MCP adapter）。
   - **Acceptance:** 记录请求顺序的 fixture：业务 retrieve 之前依次出现 initialize 与 initialized；握手失败 → `{error: "retrieve_unavailable"}`，无成功 chunks。默认 `uv run pytest -q` 仍不依赖 live MCP；协议级断言走既有 `mcp` mark 或与之等价的 opt-in。
   - [parallel]
   - Status: [x] done

4. **[parallel] 损坏的 extracted index fail-closed** — tag: `[implementation]`
   - **Inherits:** `specs/s1-corpus-retrieve.md` 对 ingest 失败的 fail-closed 精神（`sec_http_fail` / `ocr_required_unavailable`：失败不得把该单位标 complete）；malformed 已存在的 `_index.json` 视为损坏，不是 missing。
   - **Surface:** corpus ingest。
   - **Acceptance:** 写入 malformed index 后跑 ingest：进程非零、typed ingest error、原始文件字节不变；不得走「当缺失然后重建覆盖」路径。缺文件（真正 missing）仍允许初始化。
   - [parallel]
   - Status: [x] done

5. **Live fetch 原子发布且登记 catalog** — tag: `[implementation]`
   - **Inherits:** `specs/s1-corpus-retrieve.md`：`sec_http_fail` 时 "no catalog row marked complete for that accession"；成功路径必须让 catalog / `list_files` 能发现该 accession。
   - **Surface:** corpus live fetch 与 catalog/manifest。
   - **Acceptance:** 成功 live ingest 后 extracted index **与** manifest 都含该 accession，`list_files` 能列出。在 page 写入后注入异常：可见树不出现未提交 accession，既有 index 与 manifest 不变。公开 API 不得再忽略调用方传入的 manifest 路径。
   - [parallel] 相对 T1–T3；与 T4 同属 corpus，若共享 index 读写则实施时接在 T4 之后，否则可并行。
   - Status: [x] done

6. **[parallel] Watchlist 非法形状与跨实例写入** — tag: `[implementation]`
   - **Inherits:** 失败须 typed，不得依赖 Python 属性错误；持久化错误不得静默改写输入文件。并发一致性是 store 边界契约，不是实例内 `threading.Lock` 的实现细节。
   - **Surface:** host watchlist 持久化。
   - **Acceptance:** 根值为 `[]`、标量、缺字段、字段类型错误 → 均为 `WatchlistError`，文件字节不变；空/缺文件仍保持既有默认空表语义。两个独立 store（最好跨进程）并发 add 两个 issuer：最终集合恰好两项、JSON 可解析、重复 add 幂等。
   - [parallel]
   - Status: [x] done

7. **[parallel] Stream harness 尊重 child exit 与 FAIL 行格式** — tag: `[implementation]`
   - **Inherits:** 检测器/harness 不得把「stdout 有 PASS JSON」当成成功；非零 child 必须是 machine-visible failure。
   - **Surface:** 既有 workspace stream check scripts。
   - **Acceptance:** fake child stdout 为全 PASS JSON 但 exit 非零 → runner 不成功退出。注入一个失败 check：文本含 protocol 的 `[FAIL]` 行（或与之等价、可机器解析的单一 formatter 输出），且最终 exit 非成功。`--help` 成功不替代本弹。
   - [parallel]
   - Status: [x] done

## Code review phase（整轮结束后）

全部子弹落地、默认测试绿之后：`code-review`（Standards + Spec）再 `verification-before-completion`。单弹 WIP commit 不跑 code-review。
