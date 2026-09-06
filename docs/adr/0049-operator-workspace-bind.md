# 0049. Operator 工作区: one global default, never cwd; one shared 知识库

Date: 2026-09-06
Status: accepted

The operator **工作区** is the single directory `$HOME/.fin/default` (mkdir if missing). `Path.cwd()` is never the implicit root. Each `invoke` / `stream` carries `configurable.workspace_root` injected by HTTP/CLI from that default (tests inject home); `graph.py` does not import `thread_store` and does not bake the root at compile time. The **知识库** (catalog / ingest / **retrieve port**) stays one process-wide corpus. There is no left-rail 指定/新建, no `$HOME/.fin/workspaces/<slug>`, and no per-thread `project_root` switch. Spec: `specs/workbench-operator-workspace.md`.

Amendment 2026-09-06: retracted multi-root bind (`thread.project_root`, `known_roots.json`, `/api/workspaces` bind/create, `workspaces/<slug>`). Multiplicity is **research thread**s. Future many-libraries / tenants / collections is not this ADR.

Considered Options: bake `workspace_root` at `compile_graph` from cwd (rejected — silent product root); graph looks up `fin_thread` itself (rejected — reverse dep onto `thread_store`); per-工作区 catalog (rejected — 知识库 is shared); sessions as `~/.fin/sessions/` files (rejected — Postgres checkpointer remains ADR 0015); coding-agent left-rail many directories (rejected 2026-09-06 — fin is an enterprise knowledge-base Agent, not a repo picker); per-thread `project_root` (rejected 2026-09-06 — YAGNI until a tenant/library split exists).
