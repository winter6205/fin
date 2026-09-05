# 0047. Ingest endpoint plus two upload doors; versioned bytes live on the retrieve backend

Date: 2026-09-04
Status: accepted

Company files enter search only through an **ingest endpoint**: fin parses and **切分**s by a deterministic format parameter table, then push/invalidate via MCP (ADR 0045). Replacing or closing a version is an MCP call, not a second local tree. Workspace **thread attach** (drag into the open thread) gives the model that file for this turn only and does not push. A separate **corpus ingest** control is the only UI that stores into the retrieve backend. **read_file** remains an ordinary read of a known locator (MCP when that is where the bytes live). The research model must not call 切分 on arbitrary strings.

Considered Options: one drag that both reads and stores (operator intent mixed; discarded); 切分 as a research-track tool (chat drafts become corpus; discarded); keep a serve-time extracted tree beside MCP (ADR 0046 already discarded).
