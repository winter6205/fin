# 0056. Community detection is an indexing step after extract

Date: 2026-09-17
Status: accepted

After **抽图窗** extract and canonical merge (ADR 0052–0055), graph indexing runs **community** clustering on the entity graph and stores community summaries. That is part of building the graph, not a retrieve-time job and not a substitute for extract windows. **检索档** C 综述 consumes those summaries; B 知识问答 stays graph-priority over entities and relations (fill arms unchanged by this ADR). D 真数据库 is out of this path. Skipping community leaves the entity graph usable for B but not a complete topic layer for C.

Considered Options: community only when a C question arrives (query-time clustering); never clustering (C has no topic layer); treating document headings as communities.

> 2026-09-22 spelling note: `C` is now `global` and `B` is `graph` (`specs/search-mode-lexical-sql-tokens.md`); the letters above are the spelling at decision time and the fill arms this ADR froze did not move.
