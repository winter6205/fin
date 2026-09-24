# 0009. v1 host tools are list_files, retrieve, and read_file

Date: 2026-08-21
Status: accepted; superseded-by ADR-0087 §model-facing read tool name and return unit only

Amendment (2026-09-24): ADR 0087 selects `read_source` as the model-facing name and one locator-addressed source unit per read when G4 lands. The `read_file` contract below records the current v1 implementation; its other decisions remain in force until separately revised.

ADR 0005 named `list_filings` / `retrieve` / `read_locator`. v1 uses industry verb-noun names: `list_files`, `retrieve` (same word as the retrieve port), and `read_file`. The three are pure functions, testable without a model. `list_files` takes optional `company` / `period` and returns `{ filings: [{ document_id, company, period, version, page_count }] }`, or `filings: []` when filters match nothing. `retrieve` takes `query`, optional `document_id`, and `limit` (default 8, cap 20) and returns BM25 `{ chunks: [{ text, source_ref: locator, score }] }`; empty query, unknown `document_id`, and no hits all return `chunks: []`. `read_file` requires `document_id` and `version`; `page` defaults to 1 and `page_limit` defaults to 1 (cap 3), returning frozen page text plus `page_count` so the model can page; unknown id, version mismatch, or out-of-range page returns `{ error: "unknown_locator" }` without throwing. Citation verifier still checks one page per cite. Do not add ingest, chunk-id read, web search, vector/hybrid variants, or `verify` as tools; do not require `page` as a distinct tool type.
