# 0030. Retrieve adapter is iknow-memory MCP; archive in-process BM25

Date: 2026-08-25
Status: superseded by 0032

S1 product retrieve is an MCP client to **iknow-memory** behind the existing retrieve port. In-process BM25 (ADR 0007) is archived, not a fallback product path. Chunking for retrieve (including later parent/child or section splits) is owned by iknow-memory; fin ingest keeps frozen locator units for `read_file` and the citation verifier and maps MCP hits back to those locators. Host graph unchanged. Do not vendor iknow-memory into `src/fin`, do not move the extracted cite tree into iknow-memory, and do not reimplement hybrid/RRF in this repo.

Amended 2026-08-31 (ADR 0037): the chunking clause above is re-ruled at the clause level — chunking for retrieve (including later parent/child or section splits) is owned by **fin**, and the retrieval service stores and returns the pushed text as-is without any re-splitting; ranking, fusion, and rerank still stay outside fin. The rest of this record's decisions (and its superseded status) are unchanged.

Considered Options: keep BM25 as fallback (two ranking stacks); hybrid implemented inside fin (duplicates iknow-memory).
