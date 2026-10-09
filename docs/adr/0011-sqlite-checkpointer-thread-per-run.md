# 0011. SqliteSaver locally; InMemorySaver in tests; thread_id is one run; no HITL UX

Date: 2026-08-21
Status: superseded by 0015

v1 compiles the host graph with LangGraph `SqliteSaver` at `data/runtime/checkpoints.sqlite` so a single-developer CLI/HTTP process can resume after a crash without Postgres or Agent Server. Depend on `langgraph-checkpoint-sqlite`. Pytest uses `InMemorySaver`. `thread_id` identifies one question/run; the CLI/HTTP wrapper mints a UUID before invoke if the caller omits it — the checkpointer still requires `configurable.thread_id`. Resume requires the same id and applies only to runs that have not reached done, refuse, or step_cap. CLI and HTTP both use sync `invoke`; do not `ainvoke` this saver (use `AsyncSqliteSaver` only if v1 later goes async). The graph may retain LangGraph interrupt primitives; v1 does not ship HITL/approval UX. Do not use in-memory as the product default, and do not treat thread_id as a multi-turn chat session or cross-thread Store.
