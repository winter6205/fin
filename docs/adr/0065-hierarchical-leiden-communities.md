# 0065. Communities are hierarchical Leiden on the entity graph

Date: 2026-09-17
Status: accepted

After extract and canonical merge (ADR 0056), **community** detection is **hierarchical Leiden** on the undirected weighted entity-relation graph (levels + parent ids; split until a max community size). It is graph augmentation, not a query-time job and not a substitute for extract. **Fast** (NLP co-occurrence extract) is a different extract method, not a step between Leiden and community reports. Community membership and reports stay on the retrieve-backend index; fin **检索档 C** only reads them (ADR 0057 / 0064). This supersedes treating a flat Louvain partition as the product clustering.

Considered Options: flat Louvain as the product algorithm; defer the algorithm; run Fast extract and Leiden as a required sequence.
