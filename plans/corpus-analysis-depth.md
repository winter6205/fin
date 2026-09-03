# Plan: corpus-analysis-depth

**Scope:** T6 only — structured paragraph redline, locator-bound table extraction,
and three-year statements rendered from fixture XBRL facts. No filing monitor,
notices, or compare-facts grid rewrite.

**Affected files:**

- `plans/corpus-analysis-depth.md`
- `src/fin/host/tools/redline.py`
- `src/fin/host/tools/table_extract.py`
- `src/fin/host/tools/statements.py`
- `src/fin/host/graph.py`
- `src/fin/host/state.py`
- `src/fin/host/run_payload.py`
- `src/fin/host/thread_store.py`
- `src/fin/host/workspace_http.py`
- `src/fin/host/stream_map.py`
- `tests/host/test_analysis_tools.py`
- `tests/host/test_analysis_in_thread.py`

bounded-context-guardian: yes — analysis contracts stay in host tools and use
existing corpus extracted/facts seams; no ranking or filing-monitor logic enters
the host.
defensive-contract-validator: yes — tests cover empty, negative, overflow,
concurrent, and exception inputs for the new pure/read-only tool boundaries.
error-handling-enforcer: yes — malformed locators, missing facts, invalid
three-year requests, and malformed page data return explicit typed error envelopes.
complexity-anti-drift: yes — redline, table parsing, and statement rendering are
separate modules with bounded helper phases; graph/workspace changes only carry
validated structured payloads.
minimal-change-verifier: yes — this is one T6 logical change and will land in one
commit; no web or T7 files are planned.

**OVERALL: PASS**
