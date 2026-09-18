# 0074. Leiden clusters the largest connected component by default

Date: 2026-09-17
Status: accepted

Hierarchical Leiden (ADR 0065) runs on the **largest connected component** of the entity-relation graph by default (`use_lcc` on). Isolated specks do not become community-report rows; they may remain as ledger entities for B. The index job may turn lcc off. This is not prune (ADR 0073) and not canonical merge.

Considered Options: always-on lcc with no config; default off so every island gets a community.
