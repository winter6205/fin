# Plan: memory-dialogue-platform

**Goal:** 在 workspace 上交付可开关的完整记忆与对话平台：会话压缩、DST 升级、dream→MCP 持久化、👍/👎、Postgres 池与可选 Redis——不依赖语料扩库/redline。
**Approach:** 粗颗粒度九波：数据面（池+Redis）与记忆空壳并行；会话压缩；本地缓冲；dream→MCP；DST；反馈与可观测/重试收尾。默认 `uv run pytest -q` 记忆 OFF。
**Spec link:** `specs/memory-dialogue-platform.md`
**Map:** local program B (`memory-dialogue-platform`); tracker issues live on GitHub separately — not embedded here.
**Tracker:** local `plans/memory-dialogue-platform.md` only — do not embed GitHub issue numbers in this file.
**Out of this worktree:** Task 4（session compaction）执行权在 `plans/session-compaction.md`（worktree `/home/winner/projects/fin-wt-session-compaction`，分支 `feat/session-compaction`；ADR 0044 occupancy，取代本文件 T4 的条数窗）。本树不要改压缩模块、不要把 T4 当下一刀。
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（`uv run pytest -q`；触及 Soft-Track 时加 `-m soft_track`；eval 弹加 `-m eval`；web：`npx tsc --noEmit`）→ verification-before-completion → one commit on the ticket branch
**End-of-round:** 九波都落地后对整轮 diff 做一次 code-review（Standards + Spec）；单弹 loop 不重复整轮 review。
**待写入:** empty — CONTEXT + ADR 0035/0036 already flushed with the spec persist.

## ACR（自 `specs/memory-dialogue-platform.md` 原样粘贴）

bounded-context-guardian: yes — memory stays a host/workspace capability behind a **memory port** (not a `controllers/` layer); filing retrieve stays on **retrieve port**; web only renders thumbs/compaction UX; ranking/chunking for memory MCP stay outside fin.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–11.
error-handling-enforcer: yes — inherit typed EXITs; new paths named (memory OFF, dream MCP fail, Redis down degrade, classify schema fail); no silent empty catch.
complexity-anti-drift: yes — five foci, one concern each; writing-plans must not dump all foci into one god-plan; dream is not nested inside `model_node`; classify small-model stays in the classify tier.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences **one wave = one plan = tracer bullets**; conflict with “1 commit” is resolved by split, not by merging waves.

**OVERALL: PASS** — 本文件即 coarse 拆波；每波仍 1 commit。

## Harvest（已决 vs 开放）

**已决：**

- 记忆默认 OFF；memory port；dream→MCP 持久化；记忆 ≠ cite（ADR 0035）。
- Postgres SSOT；Redis 可选非 SSOT（ADR 0036）。
- Soft-Track 四 track 不改名；rules 仍可权威短路小模型。
- 👍/👎 不改 verifier/retrieve。
- retrieve MCP ≠ memory MCP。
- 2026-09-04：一库三策略（checkpointer 长连接 / thread_store 短连接 / memory ON 才建池）；限流排队 wont-do。长期记忆 MCP 决策在 iknow-memory 仓并行，不镜像 fin ADR。
- 2026-09-04：recall 注入计入 session occupancy（ADR 0044）；条数上限只当 fuse。extract 为显式写入，不每轮自动抽对话入 buffer（ADR 0035）。
- 2026-09-04：公司资料经 ingest/push 进 retrieve 后端（典型为 iknow-memory）再检索；dream 不是入库（ADR 0045）。T5 图上记忆工具与 T6 dream 不再当作「把公司知识灌进 MCP」的必做波。dream 触发轴不作产品主路径决策。
- 2026-09-04：引用门仍在 host；核对正文为本轮 retrieve/`read_file` 载荷（ADR 0046）。运行时不再另养 extracted 树当第二份公司资料。
- 2026-09-04：Task 4 压缩接线划出本执行队列，由 `fin-wt-session-compaction` / `feat/session-compaction` 接手。

**开放（implementer headroom）：**

- 包名 `host/memory/` vs `fin/memory/`。
- recall 绑定哪些 track。
- 可观测暴露在 `/health` 扩展 vs 独立路由 vs 脚本。
- Qwen 是否单独加载（当前 classify 复用主模型）。

**代码对照（2026-09-04 harvest；GitHub T1–T9 勾选仍可能过期）：**

对照物是各波 Acceptance，不是「文件在不在」。相关单测 `160 passed, 35 skipped`（skip 因本机未设 `FIN_CHECKPOINT_URL`）；未跑全仓 pytest、未跑 web `tsc`、未打 live MCP。

| 波  | 模块/测试                                                                                  | 产品路径                                                        | 相对 Acceptance                                  |
| --- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------- | ------------------------------------------------ |
| T1  | `db_pool.py`；`test_postgres_pool_t1.py`                                                   | 池只给 memory ON；saver 长连接、thread_store 短连接（ADR 0036） | **决策闭合**；不要再接线 saver                   |
| T2  | `redis_client.py`；`test_redis_skeleton.py`                                                | 未进 `http`/`health`；无限流实现                                | **骨架闭合**；排队 wont-do                       |
| T3  | `memory_port.py` `OffMemoryAdapter`；`test_memory_off_port.py`；HTTP `get_memory_port`     | 默认 OFF；图上无记忆 tools                                      | **闭合**                                         |
| T4  | 条数窗库仍在；接线形状以 ADR 0044 为准                                                     | 本树不接 graph                                                  | **划出本队列**；见 `plans/session-compaction.md` |
| T5  | `memory_buffer.py` / `memory_store.py` + tests                                             | 无 host 记忆 tool；ON 时模型调不到 extract/recall               | 库+单测在；图未接                                |
| T6  | `memory_dream.py`；SSE 结束后 `run_dream_after_turn`；`FIN_MEMORY_DREAM_BACKEND` 仅 `null` | 无 fixture/真 MCP transport（注释 T6d）                         | HTTP 调度在；SC7 MCP 写出未做                    |
| T7  | `track_decision.py` 置信度阈值；`test_track_decision_t7.py`                                | compile 把主模型当 `classify_model`；无独立 Qwen                | **基本闭合**（小模型可选）                       |
| T8  | `feedback.py` + PG store；`web/src/feedback.ts` helper                                     | **无** workspace 路由；无 UI 控件                               | 持久化库在；GET 历史/翻转未接到产品              |
| T9  | `observability.py`；`rewrite_retry.py`；`test_observability_retry_contracts.py`            | 未进 `/health`；retrieve tool loop 未调用 rewrite               | 库+单测在；未暴露、未进 loop                     |

**下一刀（本 worktree，仍非 iknow、不含压缩）：** T8 HTTP+UI、T9 接线。T5 图上记忆工具与 T6 dream 不当公司知识入库（ADR 0045）；需要时仅显式 extract。T4 压缩不在本树做。

## Tasks (ordered by dependency)

1. **Postgres connection pooling** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC3；ADR 0036：产品 serve 路径用池；checkpoint 误配仍响亮/typed。
   - **Surface:** host compile / thread_store + tests
   - **Acceptance:** serve 路径使用池（或文档化等价）；误配 URL 非静默 InMemory；`uv run pytest -q` exits 0。
   - **Completion:** 池库选型开放。
   - Status: [x] closed 2026-09-04 — pool for memory ON only; saver/thread_store not pooled (ADR 0036)
   - [parallel]

2. **Optional Redis skeleton** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC4；ADR 0036：无 URL 单进程；down socket typed degrade。
   - **Surface:** host + tests
   - **Acceptance:** 无 Redis 可 scripted turn；Redis URL 指向 down socket 时非空白 500 且策略降级；`uv run pytest -q` exits 0。
   - **Completion:** 限流/排队哪一个先做开放，但骨架与降级必须可观察。
   - Status: [x] closed 2026-09-04 — skeleton + typed degrade; rate-limit/queue wont-do
   - [parallel]

3. **Memory OFF shell + port wiring** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC2；ADR 0035：默认 OFF 零工具零 dream。
   - **Surface:** host + tests
   - **Acceptance:** 默认 OFF 下 scripted research/dialogue 无记忆工具、dream 不跑；`uv run pytest -q` exits 0。
   - **Completion:** env 名与 port 接口形状开放。
   - Status: [x] closed 2026-09-04 — `OffMemoryAdapter`; default `FIN_MEMORY_ENABLED` off
   - [parallel]

4. **Session sliding window + incremental summary** — tag: `[implementation]`
<<<<<<< HEAD
   - **Inherits:** spec Focus 2 / SC5：超阈值压缩；company/period 等 snapshot 槽位保留。
   - **Surface:** host graph/checkpointer + tests（soft_track as needed）
   - **Acceptance:** 长 scripted thread 压缩后 classify/inherit 仍见槽位；模型可见历史相对基线缩短可测；不写 MCP；`uv run pytest -q` exits 0。
   - **Completion:** 阈值与摘要模型调用方式开放。
   - Status: [ ] **out of this worktree** — 执行 SSOT 是 `plans/session-compaction.md`（`fin-wt-session-compaction` / `feat/session-compaction`）；本树禁止改压缩
   - [unblocked: T1 closed without saver pooling]
=======
   - **Inherits:** superseded by ADR 0044 and `plans/session-compaction.md`（token occupancy, not message count）.
   - **Surface:** see session-compaction plan
   - **Acceptance:** do not implement this bullet; implement `plans/session-compaction.md` T1–T3.
   - Status: [x] superseded 2026-09-04
   - [unblocked: T1 pooling closed on main by ADR 0036]
>>>>>>> origin/main

5. **Local memory buffer: extract / recall / GC** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC6–SC8；ADR 0035：PG 缓冲；recall advisory；不能单靠记忆 cite。
   - **Surface:** memory port local backend + host tools + tests
   - **Acceptance:** ON 时 extract≥1 条；recall 返回；GC 软禁 fixture；仅记忆文本不能 `done` 出记忆 citations；`uv run pytest -q` exits 0。
   - **Completion:** 条目 schema 开放。
   - Status: [ ] remaining — buffer/store exist; **no graph memory tools**
   - [blocks: T3]

6. **dream → memory MCP persistence** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC7；ADR 0035：规则闸→MCP；失败 typed；不挡前台 turn。
   - **Surface:** host dream + memory MCP adapter + tests（fixture MCP）
   - **Acceptance:** fixture MCP 上 dream 写出可观察；无 MCP 时 skip/仅缓冲且前台正常；`uv run pytest -q` exits 0。
   - **Completion:** 规则阈值与 MCP tool 名开放。
   - Status: [ ] remaining — `null` backend + post-SSE dream; **no MCP transport** (parallel with iknow)
   - [blocks: T5]

7. **DST confidence + optional small classify model** — tag: `[implementation]`
   - **Inherits:** spec Focus 4 / SC9；ADR 0027：rules 优先；低置信 clarify。
   - **Surface:** track_decision + tests soft_track
   - **Acceptance:** 低置信→clarify；rules greeting 不调小模型；`uv run pytest -q -m soft_track` 相关用例绿。
   - **Completion:** Qwen 是否同波接入开放（可先置信度+假小模型接口）。
   - Status: [x] closed 2026-09-04 — confidence + rules-first; Qwen optional leftover
   - [parallel]

8. **Thumbs up/down feedback** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC10：落 PG；不改 citations。
   - **Surface:** host workspace HTTP + web + tests
   - **Acceptance:** 👍/👎 出现在 GET 历史；翻转不改变该 run citations；`uv run pytest -q` exits 0；`web/` `tsc --noEmit` exits 0。
   - **Completion:** UI 控件位置开放（尾卡片）。
   - Status: [ ] remaining — store + `feedback.ts`; **no HTTP route / UI**
   - [unblocked: T1 closed]

9. **Observability + empty-hit/rewrite retry** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC11；tool loop 增强非检索流水线。
   - **Surface:** host + tests
   - **Acceptance:** 文档化命令或路由暴露 refuse/done 或 latency 且无 secret；空结果重试可测且不变成固定 pipeline；`uv run pytest -q` exits 0。
   - **Completion:** 暴露面与重试次数开放。
   - Status: [ ] remaining — helpers exist; **not on /health or retrieve loop**
   - [blocks: T3]
