# Plan: memory-dialogue-platform

**Goal:** 在 workspace 上交付可开关的完整记忆与对话平台：会话压缩、DST 升级、dream→MCP 持久化、👍/👎、Postgres 池与可选 Redis——不依赖语料扩库/redline。
**Approach:** 粗颗粒度九波：数据面（池+Redis）与记忆空壳并行；会话压缩；本地缓冲；dream→MCP；DST；反馈与可观测/重试收尾。默认 `uv run pytest -q` 记忆 OFF。
**Spec link:** `specs/memory-dialogue-platform.md`
**Map:** local program B (`memory-dialogue-platform`); tracker issues live on GitHub separately — not embedded here.
**Tracker:** local `plans/memory-dialogue-platform.md` only — do not embed GitHub issue numbers in this file.
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

**开放（implementer headroom）：**

- 包名 `host/memory/` vs `fin/memory/`。
- 记忆开关 env 名；Redis URL env 名。
- 小模型如何加载（本地 vs `FIN_BASE_URL` 旁路）。
- recall 绑定哪些 track。
- 可观测暴露在 `/health` 扩展 vs 独立路由 vs 脚本。

## Tasks (ordered by dependency)

1. **Postgres connection pooling** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC3；ADR 0036：产品 serve 路径用池；checkpoint 误配仍响亮/typed。
   - **Surface:** host compile / thread_store + tests
   - **Acceptance:** serve 路径使用池（或文档化等价）；误配 URL 非静默 InMemory；`uv run pytest -q` exits 0。
   - **Completion:** 池库选型开放。
   - Status: [ ] pending
   - [parallel]

2. **Optional Redis skeleton** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC4；ADR 0036：无 URL 单进程；down socket typed degrade。
   - **Surface:** host + tests
   - **Acceptance:** 无 Redis 可 scripted turn；Redis URL 指向 down socket 时非空白 500 且策略降级；`uv run pytest -q` exits 0。
   - **Completion:** 限流/排队哪一个先做开放，但骨架与降级必须可观察。
   - Status: [ ] pending
   - [parallel]

3. **Memory OFF shell + port wiring** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC2；ADR 0035：默认 OFF 零工具零 dream。
   - **Surface:** host + tests
   - **Acceptance:** 默认 OFF 下 scripted research/dialogue 无记忆工具、dream 不跑；`uv run pytest -q` exits 0。
   - **Completion:** env 名与 port 接口形状开放。
   - Status: [ ] pending
   - [parallel]

4. **Session sliding window + incremental summary** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 / SC5：超阈值压缩；company/period 等 snapshot 槽位保留。
   - **Surface:** host graph/checkpointer + tests（soft_track as needed）
   - **Acceptance:** 长 scripted thread 压缩后 classify/inherit 仍见槽位；模型可见历史相对基线缩短可测；不写 MCP；`uv run pytest -q` exits 0。
   - **Completion:** 阈值与摘要模型调用方式开放。
   - Status: [ ] pending
   - [blocks: T1]

5. **Local memory buffer: extract / recall / GC** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC6–SC8；ADR 0035：PG 缓冲；recall advisory；不能单靠记忆 cite。
   - **Surface:** memory port local backend + host tools + tests
   - **Acceptance:** ON 时 extract≥1 条；recall 返回；GC 软禁 fixture；仅记忆文本不能 `done` 出记忆 citations；`uv run pytest -q` exits 0。
   - **Completion:** 条目 schema 开放。
   - Status: [ ] pending
   - [blocks: T3]

6. **dream → memory MCP persistence** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC7；ADR 0035：规则闸→MCP；失败 typed；不挡前台 turn。
   - **Surface:** host dream + memory MCP adapter + tests（fixture MCP）
   - **Acceptance:** fixture MCP 上 dream 写出可观察；无 MCP 时 skip/仅缓冲且前台正常；`uv run pytest -q` exits 0。
   - **Completion:** 规则阈值与 MCP tool 名开放。
   - Status: [ ] pending
   - [blocks: T5]

7. **DST confidence + optional small classify model** — tag: `[implementation]`
   - **Inherits:** spec Focus 4 / SC9；ADR 0027：rules 优先；低置信 clarify。
   - **Surface:** track_decision + tests soft_track
   - **Acceptance:** 低置信→clarify；rules greeting 不调小模型；`uv run pytest -q -m soft_track` 相关用例绿。
   - **Completion:** Qwen 是否同波接入开放（可先置信度+假小模型接口）。
   - Status: [ ] pending
   - [parallel]

8. **Thumbs up/down feedback** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC10：落 PG；不改 citations。
   - **Surface:** host workspace HTTP + web + tests
   - **Acceptance:** 👍/👎 出现在 GET 历史；翻转不改变该 run citations；`uv run pytest -q` exits 0；`web/` `tsc --noEmit` exits 0。
   - **Completion:** UI 控件位置开放（尾卡片）。
   - Status: [ ] pending
   - [blocks: T1]

9. **Observability + empty-hit/rewrite retry** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC11；tool loop 增强非检索流水线。
   - **Surface:** host + tests
   - **Acceptance:** 文档化命令或路由暴露 refuse/done 或 latency 且无 secret；空结果重试可测且不变成固定 pipeline；`uv run pytest -q` exits 0。
   - **Completion:** 暴露面与重试次数开放。
   - Status: [ ] pending
   - [blocks: T3]
