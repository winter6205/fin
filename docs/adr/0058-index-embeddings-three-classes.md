# 0058. Index embeddings for extract windows, entity descriptions, and community summaries

Date: 2026-09-17
Status: accepted

Graph indexing, after community summaries (ADR 0057), writes **vector embeddings** for (1) **抽图窗** body text, (2) **entity** descriptions after merge, (3) **community** summary text. Query must not be the first place those three are embedded. Omitting a class disables the matching retrieve arm (passage vectors, entity-seed vectors, topic-summary vectors). This ADR does not change **检索档** routing (ADR 0055).

Considered Options: embed only 抽图窗; embed at query time only.
