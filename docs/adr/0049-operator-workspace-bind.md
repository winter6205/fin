# 0049. Operator 工作区 bind: default directory, never cwd; one shared 知识库

Date: 2026-09-06
Status: accepted

The operator **工作区** is an explicit filesystem directory (`thread.project_root`). Product create/list uses `$HOME/.fin/default` when unset; `Path.cwd()` is never the implicit root. Each `invoke` / `stream` carries `configurable.workspace_root` per-call injected by HTTP/CLI from that thread row; `graph.py` does not import `thread_store`. The **知识库** (catalog / ingest / **retrieve port**) stays one process-wide corpus; creating a 工作区 does not create a second library. Spec: `specs/workbench-operator-workspace.md`.

Considered Options: bake `workspace_root` at `compile_graph` from cwd (rejected — silent product root, LRU keyed per directory); graph looks up `fin_thread` itself (rejected — reverse dep onto `thread_store`); per-工作区 catalog (rejected — 知识库 is shared); sessions as `~/.fin/sessions/` files (rejected — Postgres checkpointer remains ADR 0015).
