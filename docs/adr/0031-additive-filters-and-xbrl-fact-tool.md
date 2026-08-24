# 0031. Additive catalog filters and get_xbrl_fact

Date: 2026-08-25
Status: accepted

`list_files` / `retrieve` keep v1 names and `company` / `period` / `query` / `document_id` / `limit`. S1 adds optional `form` (`10-Q` \| `10-K`) and `ticker` (same meaning as today's `company` code; conflicting pair yields empty hits). Do not rename keys, do not ship a second gold schema, do not add auth filters, and do not make `filing_date` range a hard field while ingest rows may lack it. Research track gains `get_xbrl_fact`: tagged financial facts from ingested iXBRL (XBRL is the fact format, not a website); the numeric gate matches claimed values to those facts, not LLM arithmetic and not OCR. Host graph topology stays; retrieve ranking remains the iknow-memory MCP adapter (ADR 0030).

Considered Options: rename `company` to `ticker` (breaks gold and stream summaries); dual tool schemas for eval vs live.
