# 0066. Fast extract is not on the required index path

Date: 2026-09-17
Status: superseded by ADR-0067

The required company-corpus index is **LLM extract** of entities and relations from **抽图窗**, merge, hierarchical Leiden (ADR 0065), then community summaries (ADR 0057). **Fast** (NLP noun-phrases and co-occurrence edges, cheaper graph) is an optional later extract switch, not a pipeline stage and not the default. B 知识问答 needs the standard graph; C must not force Fast.

Considered Options: Fast as a required step after “graph overview”; Fast as the default extract because community reports exist.
