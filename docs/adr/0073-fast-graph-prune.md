# 0073. Fast extract prunes the graph before Leiden; standard does not

Date: 2026-09-17
Status: accepted

After **Fast 抽图** (NLP co-occurrence), the index job **prunes** rare/hub nodes and weak edges before hierarchical Leiden so modularity is not dominated by noise. Fast edge weight starts as **same-window co-occurrence count**, then a **PMI-style** score so chance pairs of frequent terms rank below rare terms that actually travel together; **weak** means below a **percentile of that graph’s remaining edges** (default 40th), not a global cutoff. **Standard** LLM extract does not run that prune by default (canonical merge already gated the graph). Prune thresholds stay index-job config, not frozen product constants. Prune is not a retrieve 档 and not canonical merge.

Considered Options: prune both paths; prune neither; treat prune as duplicate-entity merge; raw co-occurrence count as the only prune score.
