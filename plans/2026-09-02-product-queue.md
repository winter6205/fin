# Plan: product-queue（串行总入口，不是第四份实施计划）

**Goal:** 在同一 worktree 上按顺序做完：真实检索进程对接 → corpus-ops 接到当前 main → 评测规模与 live 消融。
**Approach:** 操作者要求不超过三份可执行计划、一个 worktree、不再换对话切会话。本文件只指向下面三份；实施从 P1 T1 起，P1 全部 `[x]` 再开 P2，P2 全部 `[x]` 再开 P3。开着的 fin the tracked change through the tracked change / #195 只当对照 diff，不是合并入口。iknow-memory 仓的改动由 P1 子弹点名 Surface，在实施该弹时改对方仓（不必再开第二棵计划 worktree）。
**Spec link:** P1 → `specs/corpus-retrieval.md`；P2 → `specs/corpus-ops-depth.md`；P3 → `specs/corpus-retrieval.md` §B1 + 阶段 3 SC。
**Tracker:** FALLBACK — 本地 markdown。操作者要求产物落在本 worktree，且此前检索升级明确不开 GitHub issue；不 `gh issue create`。
**Worktree:** `/home/winner/projects/fin-wt-product-queue` · 分支 `docs/2026-09-02-product-queue`
**顺序:**

| 序  | 文件                                                                               | 做成什么样算这份完                                                                           |
| --- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| P1  | [`plans/2026-09-02-p1-live-retrieve-wire.md`](2026-09-02-p1-live-retrieve-wire.md) | 真实 `iknow-memory` 进程上 push / retrieve / 失效 / 产品路径可演示；默认 pytest 仍不启该进程 |
| P2  | [`plans/2026-09-02-p2-corpus-ops-on-main.md`](2026-09-02-p2-corpus-ops-on-main.md) | `specs/corpus-ops-depth.md` SC1–SC10 在当前 main 上成立                                      |
| P3  | [`plans/2026-09-02-p3-eval-scale.md`](2026-09-02-p3-eval-scale.md)                 | §B1 每格式 pdf≥20 且 verify 诚实；live 消融/并发按 B1 时长而不是 smoke                       |

**Per-ticket loop (all bullets in P1–P3):** tdd → typecheck+tests → verification-before-completion → one commit on this worktree branch. 整轮（三份都完）才做一次 code-review，不 per-commit、不另开对话。
