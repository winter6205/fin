# 0031. Additive catalog filters and get_xbrl_fact

Date: 2026-08-25
Status: accepted

Filing catalog fields follow EDGAR submissions, not the frozen-12 demo: a submission always has **CIK**, **form** (string, not only 10-Q/10-K), **accession**, and **filing_date**. **Ticker** is optional (none, one, or many per CIK). **Period** / period-end exist on periodic reports (10-Q/10-K) and are absent or unlike a fiscal quarter on event forms (e.g. 8-K later). `list_files` / `retrieve` keep v1 `company` / `period` / `query` / `document_id` / `limit` so gold still works; `company` is not permanently equal to ticker. Additive optional filters: `cik`, `form`, `ticker`, exact `filing_date` when the row has one. Unknown or conflicting filters yield empty hits. Do not rename v1 keys, do not require ticker, do not ship a second gold schema, do not add auth filters, do not make date **range** a hard contract. Research track gains `get_xbrl_fact`: pure lookup of one ingest-built tagged fact (not a second retriever). Numeric gate matches claimed values to that row. Later, iknow-memory embeddings may map user wording to a concept alias; they must not supply the number. Retrieve ranking remains iknow-memory MCP (ADR 0030).

Considered Options: treat `ticker` as required and identical to `company` (fits only the demo corpus); rename `company` → `ticker`.
