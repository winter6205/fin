# 0029. Product corpus is live official HTML; eval fixture is a pipeline pin

Date: 2026-08-25
Status: accepted

The private fixture source 12-PDF freeze is no longer product or eval SSOT (ADR 0004 superseded). One locator contract (ADR 0028): official 10-Q and 10-K primary HTML. `fin-serve` reads the operator-selected **product corpus** on local disk. Default CI reads a git-pinned **eval fixture** produced by the same ingest CLI (not a pdfplumber bypass, not live EDGAR in pytest). Ingest is CLI-only; the ask path does not fetch. Contract eval (scripted, fixture) must cover HTML-unit cite pass, unknown locator refuse, catalog miss, and cross-period; nine v1 gold cases are not the ceiling (ADR 0012 superseded). Capability eval is opt-in/nightly on the product tree plus a live model.

Considered Options: keep PDF-12 as eternal eval SSOT (demo grain forever); gold against the network (non-deterministic, fair-access); S&P 500 backfill as the ingest default (too wide before the CLI exists). First eval fixture pin: AAPL and TSLA, official 10-Q and 10-K, two periods each, produced by the ingest CLI.
