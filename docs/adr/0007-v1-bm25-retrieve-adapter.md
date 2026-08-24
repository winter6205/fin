# 0007. v1 retrieve adapter is in-process BM25, not a stub

Date: 2026-08-21
Status: superseded by 0030

fin is an independent system A: v1 implements retrieve behind the retrieve port as in-process BM25 (or equivalent lexical ranking) over page-passage chunks, each with a locator as `source_ref`. This is the product's retrieval, not a placeholder to avoid iknow-memory. Do not skip a working adapter, and do not put cosine / hybrid / RRF in v1. Later upgrades replace the adapter only (for example an MCP client to iknow-memory); the host graph stays.
