# Plan: workbench-operator-workspace (Wave A)

**Goal:** The 工作台 uses one global 工作区 (`$HOME/.fin/default`); never `Path.cwd()`; HTTP/CLI inject that path; the **知识库** stays one shared corpus. No left-rail 指定/新建, no `workspaces/<slug>`, no per-thread `project_root`.
**Approach:** Same branch `feat/workbench-operator-workspace`. Discard uncommitted T3 WIP first. Then two remaining tracers: inject the one default + drop compile-time cwd bake; unship T2 multi-root `/api/workspaces` bind/create. Do not rewrite host identity/soul/usage here (Wave B / #242).
**Spec link:** `specs/workbench-operator-workspace.md` (Foci 1–4, SC1–SC8, SC14; Round 3 ACR)
**Worktree:** existing `feat/workbench-operator-workspace` / `wave-a-workspace-bind`. Do not use the #242 prompt branch.
**Tracker:** [#243](https://github.com/winter6205/fin/issues/243). Bullets are commits, not extra issues. Do not fold into #242.
**Out of this worktree:** host prompt split; `12 SEC` / `page_limit` / 32-char copy; iknow code; per-工作区 catalog; `graph.py` importing `thread_store`; left rail; `fin_thread.project_root`.
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → `uv run pytest -q` (and `npx tsc --noEmit` in `web/` when a bullet touches it) → verification-before-completion → one commit on this branch
**End-of-round:** after remaining bullets, one code-review (Standards + Spec) on the wave diff; not per commit.
**待写入:** empty — CONTEXT **工作区** / **user profile** + ADR 0049 amendment flushed 2026-09-06.

**Already on this branch (keep):** T1 copy (`475ab62`); T2 `default_workspace_root` + never-cwd + one 知识库 (`424e76a`). T2 also landed multi-root registry/HTTP — T4 unships that half.

**Next session:** only after this wave merges. Then `plans/host-prompt-assembly.md` + [#242](https://github.com/winter6205/fin/issues/242).

## ACR（自 spec Round 3 原样粘贴）

```
bounded-context-guardian: yes — Focus 3 inject seam names HTTP/CLI as injectors (`workspace_http` / CLI add configurable.workspace_root from default_workspace_root, not from thread.project_root); graph.py consumes that configurable only (Out of this spec + ADR 0049 forbid graph.py→thread_store); catalog/retrieve stay compile-time (Focus 4); T2 multi-root routes are unshipped not extended
defensive-contract-validator: yes — Test allocation names empty / negative / overflow / concurrent / exception; SC3–SC8 bind those classes including omit configurable.workspace_root and unshipped bind/create
error-handling-enforcer: yes — omit/empty configurable.workspace_root → typed WorkspaceRootMissingError EXIT never Path.cwd() never silent default inside the graph; HOME/default resolve typed not cwd; 409 concurrent_run unchanged
complexity-anti-drift: yes — inject (HTTP/CLI) vs consume (graph per-call configurable) vs bake-removal (compile-time cwd closures / workspace_root_key) vs unship T2 multi-root HTTP; catalog compile-time; Focus 5 splits identity/soul/usage on Wave B
minimal-change-verifier: yes — discard uncommitted T3 project_root WIP; remaining Wave A is two tracers (inject-default, unship multi-root API) not left rail; Focus 5 tracer #242; do not fold into #242
```

**OVERALL: PASS.** Operator 2026-09-06 retracted multi-root 工作区; Round 3 replaces Round 2.

## Harvest（已决 vs 开放）

**已决:** ADR 0049 amendment; CONTEXT **工作台** / **工作区** (one default) / **知识库**; never `Path.cwd()`; `configurable.workspace_root` injected by HTTP/CLI from `default_workspace_root`; `graph.py` does not import `thread_store`; one process-wide catalog; omit configurable → typed missing-root EXIT, never silent default inside the graph; 409 `concurrent_run` unchanged; no left rail; no `project_root` column; **user profile** is `$HOME/.fin/user.md` only.

**开放:** typed-error class name; whether `/api/workspaces` GET that only lists default is deleted with bind/create or kept as a no-op — default is **delete the multi-root router**, keep the helper.

## Tasks (ordered by dependency)

0. **Discard uncommitted T3 WIP** — tag: `[implementation]`
   - **Inherits:** operator freeze 2026-09-06; spec assumption 4.
   - **Surface:** worktree dirty files only (`graph.py`, `thread_store.py`, `workspace_http.py`, tests, …). Not a commit.
   - **Acceptance:** `git status` on the Wave A worktree has no per-thread `project_root` diffs; HEAD is still `424e76a` plus later allowed tracers. Then T3.
   - Status: [x] done (`958e83a` carried the amended contract docs; per-thread WIP reverted via `git checkout -- src/ tests/`).

1. **Align remaining product copy with CONTEXT** — tag: `[implementation]`
   - Status: [x] done (`475ab62`). Re-check README vs amended **工作区** one-liner if copy still says left-rail bind.

2. **Default 工作区 helper + never cwd** — tag: `[implementation]`
   - Status: [x] done for the keepable half (`424e76a`: `default_workspace_root`, never `Path.cwd()`, one 知识库). Multi-root HTTP/registry is T4.

3. **Per-call inject of the one default + drop compile-time cwd bake** — tag: `[implementation]`
   - **Inherits:** spec SC3, SC6, SC8, Focus 3 EXIT, ADR 0049; `workspace_http` / CLI copy `default_workspace_root` into `configurable.workspace_root`; missing configurable → typed error, not cwd, not silent default inside the graph; idempotence cache drops `workspace_root_key`; `graph.py` still has zero `thread_store` imports; no `fin_thread.project_root`.
   - **Surface:** `workspace_http`, `compile_graph` / host graph, CLI if it invokes, `user_profile` / memory key; tests in `tests/host/`
   - **Acceptance:** a scripted invoke without `workspace_root` raises the typed missing-root error; HTTP stream configurable includes `workspace_root` equal to the resolved default (tests inject home); `compile_graph` does not close over `Path.cwd()`; `uv run pytest -q` exits 0.
   - Status: [x] done (`b6d01b5`: typed `WorkspaceRootMissingError` at `graph.py`, per-call resolution in profile/memory tools, HTTP send/resume + `/ask` + CLI inject, cache key drops `workspace_root_key`; review cleanup `9e1390c`).
   - [blocks: T0]

4. **Unship T2 multi-root 工作区 API; no left rail** — tag: `[implementation]`
   - **Inherits:** spec SC4, SC5, SC7, Focus 2 remaining; delete `/api/workspaces` bind/create (and the multi-root registry / `workspaces/<slug>` create path); SPA must not add 指定/新建; keep `default_workspace_root`.
   - **Surface:** `src/fin/host/workspace_registry.py`, `workspace_http.py`, tests that targeted bind/create 4xx; `web/` only to confirm no picker
   - **Acceptance:** bind/create routes absent or 404; no slug create; `GET /api/threads` is not directory-scoped; `GET /api/corpus` unchanged; `uv run pytest -q` exits 0; if `web/` touched, `tsc --noEmit` exits 0.
   - Status: [x] done (`e823a93`: bind/create routes + registry + slug path deleted, unshipped-contract guard suite added; `web/` untouched — no picker existed; review cleanup `9e1390c`).
   - [blocks: T3]

**End-of-round record (2026-09-06):** one code-review (Standards + Spec) on ee2e4ba..9e1390c — Standards 0 High / 0 Medium / 4 Low, Spec 0 High / 1 Medium / 0 Low → GATE PASS. Medium (stale overlay copy) + 2 cheap Lows (dead module state, cli `home` type) fixed in `9e1390c`; 2 Lows (`.fin` literal SSOT, configurable data-clumps helper) deferred as PR advisory on #244.
