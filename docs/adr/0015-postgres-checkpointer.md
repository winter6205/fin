# 0015. PostgresSaver for durable v1 runs; InMemorySaver only in tests

Date: 2026-08-21
Status: accepted

ADR 0011 chose SqliteSaver to avoid extra infrastructure. Checkpoint bytes do not migrate from SQLite to Postgres, so that default would make a later upgrade a new empty store. v1 therefore uses LangGraph `PostgresSaver` and `FIN_CHECKPOINT_URL`. Durable CLI/HTTP require a running Postgres; local delivery is docker-compose (a native server with the same URL is fine). Pytest uses `InMemorySaver` only — RAM, process-scoped, never the product checkpointer. CLI and HTTP still sync `invoke` with the sync saver (not `AsyncPostgresSaver`). Do not ship SqliteSaver as the product default, and do not use in-memory for CLI or HTTP.
