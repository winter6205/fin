# 0055. Extract windows are graph provenance, not the four retrieve 档

Date: 2026-09-17
Status: accepted

Indexing that **creates** the company graph uses **抽图窗** (ADR 0053 / 0054): extract entities and relations from those windows, hang provenance on them, then existing canonical merge (ADR 0052). That is not a second RAG parent/child precision index and not a replacement for the graph. The graph lives as entities, relations, and later communities; **检索档** A 查找 / B 知识问答 / C 综述 / D 真数据库 stay separate query paths (CONTEXT). This extract work does not assign BM25, graph-priority, community reports, or SQL to those 档. Parent/child retrieve chunks (ADR 0037) are not the extract input.

Considered Options: making every 档 hit 抽图窗 (collapses graph and A/B/C/D into one RAG top-k); extracting from retrieve child blocks (too fine for relations); treating document parent/child as the graph’s hierarchy.

> 2026-09-22 spelling note: those four 档 are now `lexical 查找 / graph 知识问答 / global 综述 / sql 真数据库` (`specs/search-mode-lexical-sql-tokens.md`). The letters above are the spelling at decision time; the query paths they name did not move.
