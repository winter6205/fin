# Plan: workbench-operator-workspace (Wave A)

**Goal:** The 工作台 binds every **research thread** to an explicit 工作区 (default `$HOME/.fin/default`); the left rail can specify or create a directory; the **知识库** stays one shared corpus.
**Approach:** One session, one branch, four tracer commits: copy, then 工作区 API, then thread bind + inject seam, then left rail + isolation proofs. Do not rewrite host identity/soul/usage here (Wave B / the tracked change).
**Spec link:** `specs/workbench-operator-workspace.md` (Foci 1–4, SC1–SC9, SC14)
**Worktree:** new branch from current `main` (name open). Do not use `feat/` prompt-assembly / the tracked change.
**Tracker:** the tracked change. Bullets are commits, not extra issues. Do not fold into the tracked change.
**Out of this worktree:** host prompt split; `12 SEC` / `page_limit` / 32-char copy; iknow code; per-工作区 catalog; `graph.py` importing `thread_store`.
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → `uv run pytest -q` (and `npx tsc --noEmit` in `web/` when a bullet touches it) → verification-before-completion → one commit on this branch
**End-of-round:** after all four bullets, one code-review (Standards + Spec) on the wave diff; not per commit.
**待写入:** empty — CONTEXT + ADR 0049 flushed 2026-09-06. README / `prompt-development.md` are T1 of this plan, not a persist list.

**Next session:** only after this wave merges. Then `plans/host-prompt-assembly.md` + the tracked change.

## ACR（自 spec Round 2 原样粘贴）

```
bounded-context-guardian: yes — Focus 3 Inject seam + assumption 6 name HTTP/CLI as injectors (`workspace_http` adds configurable.workspace_root from thread.project_root); graph.py consumes that configurable only (Out of this spec + ADR 0049 forbid graph.py→thread_store); catalog/retrieve stay compile-time (Focus 4)
defensive-contract-validator: yes — Test allocation names empty / negative / overflow / concurrent / exception; SC4–SC8 bind those classes including omit configurable.workspace_root
error-handling-enforcer: yes — omit/empty configurable.workspace_root → typed WorkspaceRootMissingError EXIT never Path.cwd(); bind/create 4xx; registry unreadable typed not cwd; 409 concurrent_run unchanged
complexity-anti-drift: yes — inject (HTTP/CLI) vs consume (graph per-call configurable) vs bake-removal (compile-time cwd closures / workspace_root_key); catalog compile-time; Focus 5 splits identity/soul/usage; no whole-flow-in-one-function
minimal-change-verifier: yes — one logical program ≠ one commit; writing-plans one wave = one plan = one commit family per Focus 1–5; Focus 5 the wayfinder program; Foci 1–4 must not share that branch
```

**OVERALL: PASS.** Operator 2026-09-06 coarsened five foci to **two waves**; MCV still means split commits inside this wave, not five sessions.

## Harvest（已决 vs 开放）

**已决:** ADR 0049; CONTEXT **工作台** / **工作区** / **知识库**; default `$HOME/.fin/default`; never `Path.cwd()`; `configurable.workspace_root` injected by HTTP/CLI; `graph.py` does not import `thread_store`; one process-wide catalog; omit configurable → typed missing-root EXIT; 409 `concurrent_run` unchanged.

**开放:** registry file vs table; slug sanitizer; typed-error class name; left-rail widget split; how SPA remembers the current 工作区 across refresh.

## Tasks (ordered by dependency)

1. **Align remaining product copy with CONTEXT** — tag: `[implementation]`
   - **Inherits:** spec SC2, SC14; **fin** is an enterprise knowledge-base Agent; **工作台** is the UI; **工作区** is the directory; **知识库** is the one corpus.
   - **Surface:** `README.md`, `docs/guides/prompt-development.md`
   - **Acceptance:** README one-liner matches CONTEXT **fin** (no `research agent`, no workspace-as-kind); prompt-development system-prefix row lists identity, soul, usage, **user profile**, **memory existence pointer**, and states the corpus listing does not enter that prefix.
   - Status: [ ] pending

2. **Default 工作区 and list/bind/create API** — tag: `[implementation]`
   - **Inherits:** spec SC3–SC4, Focus 2; default `$HOME/.fin/default` (tests inject home); create under `$HOME/.fin/workspaces/<slug>`; typed 4xx on missing path, file-not-dir, slug with `/` or `..`; create does not add a catalog.
   - **Surface:** `src/fin/host/` HTTP + tests
   - **Acceptance:** list includes default; bind of a missing path or a file is typed 4xx; illegal slug is typed 4xx; `compile_graph` is not passed `Path.cwd()` as a baked 工作区.
   - Status: [ ] pending
   - [blocks: T1]

3. **Thread `project_root` plus per-call inject seam** — tag: `[implementation]`
   - **Inherits:** spec SC5, SC8, SC9, Focus 3 EXIT, ADR 0049; omit-root create stores default; `workspace_http` copies `project_root` into `configurable.workspace_root`; missing configurable → typed error, not cwd, not default; idempotence cache drops `workspace_root_key`; `graph.py` still has zero `thread_store` imports.
   - **Surface:** `thread_store`, `workspace_http`, `compile_graph` / host graph, CLI if it invokes
   - **Acceptance:** `POST /api/threads` with omit-root persists default `project_root`; `GET /api/threads` is scoped; legacy null rows migrate to default; a scripted invoke without `workspace_root` raises the typed missing-root error; HTTP stream configurable includes `workspace_root`.
   - Status: [ ] pending
   - [blocks: T2]

4. **Left rail, overlay/memory isolation, 知识库 unchanged** — tag: `[implementation]`
   - **Inherits:** spec SC6–SC8, Focus 4; left rail 指定/新建/select; in-flight run keeps its `project_root`; `GET /api/corpus` identical across 工作区 switch; profile overlay `<project_root>/.fin/user.md`; memory rows keyed by that path (existing memory SC6).
   - **Surface:** `web/` sidebar + host profile/memory + corpus route
   - **Acceptance:** operator can select/create a 工作区 and see only that 工作区’s threads; A vs B overlays and memory facts do not leak; corpus cards unchanged after switch; 409 concurrent send on the in-flight thread is unchanged; `uv run pytest -q` exits 0 and `web/` `tsc --noEmit` exits 0.
   - Status: [ ] pending
   - [blocks: T3]
