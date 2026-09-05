# 0035. Switchable memory subsystem; dream transports to MCP; memory is not citation evidence

Date: 2026-08-29
Status: accepted

fin owns a **switchable** memory subsystem behind a **memory port**. Local Postgres holds the working store (extract / recall / GC / dream merge). Filing retrieve stays on the separate **retrieve port** (ADR 0032) — retrieve MCP and memory MCP must not share one implied config. Memory recall is advisory only and must never satisfy the **citation verifier**; filing claims still require retrieve/`read_file` evidence and code gates.

Amended 2026-09-04: a knowledge-base research thread stays retrieve + cite; dialogue is not silently promoted into filing evidence. Recall injection occupancy is ADR 0044. Company corpus ingest is ADR 0045 (retrieve/push, not dream). Explicit-only extract is superseded by ADR 0048 (gated auto-extract, not every-turn steal).

Amended 2026-09-05: read path, auto-extract deny list, fact-level store, and dream-as-local-merge (not MCP upload) are ADR 0048. This ADR still owns: memory port vs retrieve port, recall is not citation evidence.

Amended 2026-09-06: boot default is **OFF**; this checkout opts in via project env for local eval. Default pytest stays **OFF** even when that env is set. Dream does not transport to a memory MCP (ADR 0048). Always-on inside default pytest is still rejected.

Considered Options: memory-only-in-MCP (fin cannot run complete memory offline); always-on memory in default pytest (breaks citation fixtures — still rejected); product boot always ON with no env switch (rejected — ADR 0048 2026-09-06); allowing memory spans as cites (hollows out the product gate); every-turn auto-extract of filing answers into biography (rejected — filing answers stay grounded in retrieve/`read_file`).
