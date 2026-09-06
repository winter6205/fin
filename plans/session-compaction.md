# Plan: session-compaction

**Status（2026-09-05）：** PR the tracked pull request 已合入 `main`。下方 T1–T3 已闭合。

**Goal:** 研究线程按会话额度占用自动压缩；`/compact` 走同一闸；占用可映射到当前 thread。
**Approach:** 先把占用计量和自动压接到 host（checkpoint `messages`），再并行做发送前拦截 `/compact` 与 thread 占用条。不改 `thread_store` 回合表，不上 `GET /api/status`。本文件是 MDP Task 4 的执行计划；`plans/memory-dialogue-platform.md` 已把 T4 划出其 worktree。以 ADR 0044 为准，取代条数窗。
**Spec link:** `specs/memory-dialogue-platform.md` Focus 2 / SC5；合同 SSOT `docs/adr/0044-session-compaction-token-occupancy.md`
**Map:** fin wayfinder: memory-dialogue-platform（本计划不挂新 GitHub tracer）
**Tracker:** FALLBACK — 本地 `plans/session-compaction.md`。操作者要求不 `gh issue create`。
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（`uv run pytest -q`；触及 Soft-Track 时加 `-m soft_track`；web：`npx tsc --noEmit`）→ verification-before-completion → one commit on this worktree branch
**End-of-round:** 三弹都落地后对整轮 diff 做一次 code-review（Standards + Spec）
**待写入:** empty — CONTEXT **session compaction** + ADR 0044 已在本 worktree 落盘。

> 本计划覆盖旧 MDP T4「消息条数窗口」的实施形状；不 reopen ADR，以 0044 为准。

## Harvest（已决 vs 开放）

**已决：**

- 占用 = 模型即将看到的包的估算 token / 256_000 额度（一处常量，操作者可改）。
- ≥90% 自动压；压到约 40%；tool_call 与 ToolMessage 成对处理。
- 摘要 + `RemoveMessage` 写回 checkpointer；禁止只 trim 不删 state。
- 每个 LLM 调用前检查（含同一 run 内 tool 回读撑满占用）。
- 摘要带最新 **status snapshot**；摘要模型可替换，v1 用 **v1 model**。
- `/compact` 在 Soft-Track classify 之前拦截，走同一闸，不等 90%。
- 占用可映射到当前 thread；不上 `GET /api/status`（ADR 0033）。
- `thread_store` 不是压缩对象；压缩失败 typed、不 `refuse` 本 turn。

**开放（implementer headroom）：** 占用条具体控件；`/compact` 成功/空操作对操作者的可见回执文案；常量放哪一处模块。

## ACR

bounded-context-guardian: yes — compaction stays a **host graph** + **workspace** capability; filing **retrieve port** and **memory port** unchanged; web only maps occupancy / `/compact`.
defensive-contract-validator: yes — empty (already under 90%; `/compact` still runs the same gate); negative (malformed `/compact`); overflow (over budget); concurrent (`409 concurrent_run` unchanged); exception (summary failure typed, turn continues).
error-handling-enforcer: yes — summary failure is typed and must not refuse the operator turn; no silent empty catch.
complexity-anti-drift: yes — compact gate is not nested inside `model_node`; `/compact` intercept is not a research tool; S5 gates stay with `complexity-anti-drift`, not copied here.
minimal-change-verifier: yes — this file is one logical program, three commits; do not retie T1 pooling or memory MCP.

**OVERALL: PASS**

## Tasks (ordered by dependency)

1. **Auto-compact on occupancy** — tag: `[implementation]`
   - **Inherits:** ADR 0044: occupancy via `count_tokens_approximately` on the model-facing payload vs 256_000; ≥90% summarizes oldest complete tool pairs, writes summary + latest status-snapshot slots, `RemoveMessage`s evicted checkpoint messages, stops near 40%; injectable summarizer defaults to the v1 model; typed summary failure does not refuse the turn; trim-without-delete is not compaction. Spec SC5: classify/inherit still see snapshot slots; model-facing history shortens; no default MCP write.
   - **Surface:** host graph / host session compaction
   - **Acceptance:** a scripted thread whose model-facing occupancy is at or above 90% of the session budget is compacted before the next LLM call; checkpoint `messages` are shorter and still a valid tool-pair history; latest company/period snapshot remains available to classify/inherit; occupancy below 90% does not compact; `uv run pytest -q` exits 0 (add `-m soft_track` if classify is touched).
   - **Completion:** helper splits and where the 256_000 constant lives are open.
   - Status: [x] closed 2026-09-04 — that pull request

2. **`/compact` intercept** — tag: `[implementation]`
   - **Inherits:** ADR 0044: `/compact` is intercepted before Soft-Track classify and runs the same gate as the 90% path without waiting for 90%. Not a research tool.
   - **Surface:** workspace send path (same compiled host graph)
   - **Acceptance:** sending `/compact` on a research thread runs that gate and does not classify the string as a user question; a thread already under 90% may still compact; `uv run pytest -q` exits 0.
   - **Completion:** operator-visible receipt copy is open.
   - Status: [x] closed 2026-09-04 — that pull request
   - [blocks: T1]
   - [parallel]

3. **Occupancy on the open thread** — tag: `[implementation]`
   - **Inherits:** ADR 0044: occupancy may map onto the open thread UI; it must not appear on `GET /api/status`.
   - **Surface:** workspace HTTP + web
   - **Acceptance:** the open thread shows occupancy derived from the same host figure T1 computes; `GET /api/status` JSON has no occupancy / context-window percent field; `uv run pytest -q` exits 0; `web/` `npx tsc --noEmit` exits 0.
   - **Completion:** control placement on the thread chrome is open.
   - Status: [x] closed 2026-09-04 — that pull request
   - [blocks: T1]
   - [parallel]
