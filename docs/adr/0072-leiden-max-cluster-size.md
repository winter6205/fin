# 0072. Leiden max community size defaults to 10 and stays tunable

Date: 2026-09-17
Status: accepted

Hierarchical Leiden (ADR 0065) splits communities larger than **max_cluster_size**. Product default is **10** (index-job config). Operators may tune per corpus; this is not a frozen constant and does not conflict with keeping the value configurable.

Considered Options: freeze 10 with no config; pick a larger frozen default now without graph evidence.
