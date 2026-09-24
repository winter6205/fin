# 0047. Ingest endpoint plus two upload doors; versioned bytes live on the retrieve backend

Date: 2026-09-04
Status: accepted

Amendment (2026-09-23): The thread-attach and `read_file` sentences below record the original scope. ADR 0083 establishes binary, thread-scoped attachment storage and logical addressing; ADR 0085 replaces turn-only attachment reading with a thread-scoped temporary index accessed through `retrieve` and `read_file`. The statements below about entering search and storing on the retrieve backend concern the permanent company corpus; its corpus-ingest door remains in force.

Company files enter search only through an **ingest endpoint**: fin parses and **切分**s by a deterministic format parameter table, then push/invalidate via MCP (ADR 0045). Replacing or closing a version is an MCP call, not a second local tree. Workspace **thread attach** (drag into the open thread) gives the model that file for this turn only and does not push. A separate **corpus ingest** control is the only UI that stores into the retrieve backend. **read_file** remains an ordinary read of a known locator (MCP when that is where the bytes live). The research model must not call 切分 on arbitrary strings.

Considered Options: one drag that both reads and stores (operator intent mixed; discarded); 切分 as a research-track tool (chat drafts become corpus; discarded); keep a serve-time extracted tree beside MCP (ADR 0046 already discarded).
