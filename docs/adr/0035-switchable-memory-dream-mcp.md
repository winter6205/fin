# 0035. Switchable memory subsystem; dream transports to MCP; memory is not citation evidence

Date: 2026-08-29
Status: accepted

fin owns a **switchable** memory subsystem behind a **memory port** (default OFF). Local Postgres holds the working buffer (extract / recall / GC). **dream** consolidates useful entries and transports them to a configurable **memory backend** MCP for durable persistence; swapping that MCP is an adapter change only. Filing retrieve stays on the separate **retrieve port** (ADR 0032) — retrieve MCP and memory MCP must not share one implied config. Memory recall is advisory only and must never satisfy the **citation verifier**; filing claims still require retrieve/`read_file` evidence and code gates.

Considered Options: memory-only-in-MCP (fin cannot run complete memory offline); always-on memory (breaks default pytest and filing-first demos); allowing memory spans as cites (hollows out the product gate).
