# 0067. Fast is a second extract path, not a retrieve 档

Date: 2026-09-17
Status: accepted

Company-corpus indexing has **two extract methods** on the index job (joint map with the retrieve backend): **standard** (LLM entities/relations and descriptions) and **Fast** (NLP noun-phrases, co-occurrence edges, no descriptions). The operator (or job config) chooses the method when building the graph — not per question, not as **检索档**. Both paths still freeze **抽图窗**, then Leiden, then community reports; Fast skips entity/relation summarization and claim extract, prunes the noisier graph, and writes reports from window text instead of descriptions. Query **A/B/C/D** stay the same 档; B quality follows whichever graph was built. Default method remains standard. This supersedes ADR 0066 (“Fast later / not on the map”).

Considered Options: Fast as a search mode; Fast only after v1; two retrieve pipelines.
