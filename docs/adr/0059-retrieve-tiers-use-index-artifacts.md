# 0059. Retrieve 档 consume the three index artifacts without renaming 档

Date: 2026-09-17
Status: accepted

Query routing keeps **检索档** A 查找 / B 知识问答 / C 综述 / D 真数据库. After indexing (ADR 0052–0058): A searches **抽图窗** with lexical-first (vector allowed as the parallel arm). B is graph-priority over entities and relations, then fills remaining slots from 抽图窗 vector∥BM25. C reads **community** summaries written at index time, then may drop to citable 抽图窗. D is the business SQL path; figures that live only in filings stay on B. This does not replace the graph with a single window top-k, and does not extract from retrieve child blocks.

Considered Options: one RAG hit list of 抽图窗 for every 档; renaming 档 to match foreign query-mode names.

> 2026-09-22 spelling note: the four 档 are now `lexical | graph | global | sql` (`specs/search-mode-lexical-sql-tokens.md`) — `A`→`lexical`, `B`→`graph`, `C`→`global`, `D`→`sql`, with no letter accepted at any seam. Which index artifact each arm reads, decided here, is unchanged; "lexical-first" above describes the mechanism, not a lasting token.
