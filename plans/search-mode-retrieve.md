# Plan: search-mode retrieve (fin port)

**Goal:** Grounded retrieve passes `filters.search_mode` and maps 具名失败 including C index failure; ranking stays in iknow.
**Approach:** Wire the existing port + `named_failures`; Fake tests only for contract. Product A/B/C ranking is the iknow sibling plan.
**Vocabulary:** the lasting token set is `lexical|graph|global|sql` (#293 landed 2026-09-22; A=lexical, B=graph, C=global, D=sql, letters fully retired), so "missing → B" reads "missing → graph (iknow default)".
**Spec link:** `specs/search-mode-retrieve.md`
**Sibling plan:** [`../../iknow-memory/plans/search-mode-retrieve.md`](../../iknow-memory/plans/search-mode-retrieve.md)
**Program:** `plans/extract-index-search-mode-program.md` wave 3b
**ACR:** all-yes (from spec)

bounded-context-guardian: yes — one retrieve port; ranking in iknow; no four host retrieve nodes.
input-contract-tests: yes — Boundaries name all five classes; Fake covers contract + named exits.
error-handling-enforcer: yes — codes pinned (`corpus_not_ready` / `mapping_drop` / `tier_not_open` / `real_db_not_wired` / `community_summary_index_failure`); missing mode → graph (iknow default); invalid not silent global.
complexity-anti-drift: yes — consume existing port; no fin hybrid/RRF assembler.
minimal-change-verifier: yes — spec 3 of 4 only.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion

## 待写入

empty — `community_summary_index_failure` is a wire token in `named_failures`, not a new glossary term.

## Tasks (ordered by dependency)

1. **Public C-not-ready code** — tag: `[implementation]`
   - **Inherits:** spec codes; CONTEXT **具名失败** already names community-summary index failure.
   - **Surface:** `fin.host` named_failures + run/SSE mapping
   - **Acceptance:** Token `community_summary_index_failure` is exported and distinct from `tier_not_open`; default pytest asserts the constant exists and is not collapsed into `unknown_locator`.
   - Status: [x] done — COMMUNITY_SUMMARY_INDEX_FAILURE exported (src/fin/host/named_failures.py:114/:280), distinct from tier_not_open and never collapsed into unknown_locator (tests/host/test_named_failure_tier.py:177/:190/:207); five named codes mutually distinct (tests/host/test_search_mode_named_exits.py:280)

2. **Port forwards `filters.search_mode`** — tag: `[implementation]`
   - **Inherits:** spec — missing → graph (iknow default); fin must pass the host’s assigned 档.
   - **Surface:** `fin.retrieve` adapter / retrieve tool
   - **Acceptance:** Fake or recorded MCP call includes `filters.search_mode` when host set a 档; invalid 档 from a pin still uses `tier_not_open` / `real_db_not_wired` without starting a silent C retrieve.
   - `[blocks: T1]`
   - Cross-plan blocker lifted 2026-09-20: `plans/search-mode-host.md` T1 (live retrieve flag — host assigns 档 before retrieve) merged [PR #280](https://github.com/winter6205/fin/pull/280) (`d09db62`). This lift alone does not close the bullet; the file's own `[blocks:]` markers and `Status` lines stay as they are.
   - Status: [x] done — #286/#287 + graph-global T4: port closed set resolve_search_mode (src/fin/retrieve/port.py:105/:129-141), wire write only when a mode is effective (src/fin/retrieve/mcp_adapter.py:836-843), host hop search_mode=assigned_retrieve_mode (src/fin/host/graph.py:645) forwarding the audit verbatim (search_mode_decision.py:561-583); assigned 档 reaches the port per run (tests/host/test_assigned_mode_reaches_port.py:269/:281); pinned retired letter = named exit, routes nothing (tests/host/test_search_mode_before_port.py:271)

3. **Five-class Fake contract** — tag: `[implementation]`
   - **Inherits:** spec Boundaries test classes; Fake is not a 档.
   - **Surface:** `tests` retrieve/host Fake
   - **Acceptance:** empty / negative / overflow cap / concurrent 409 / exception MCP 409 covered on the port+named-exit path; `uv run pytest -q` stays green.
   - `[blocks: T2]`
   - Status: [x] done — five classes across Fake port matrix (tests/retrieve/test_fake_port.py:450 ff: empty/ordering/omit-no-token/overflow-cap/concurrent/negative-invalid/exception), product adapter matrix (tests/retrieve/test_mcp_adapter.py:988 ff, exception as port-level retrieve_unavailable), named-exit half (tests/host/test_search_mode_named_exits.py:144/:163/:186/:209)

4. **Open the global 档 + runtime emit of `community_summary_index_failure`** — tag: `[implementation]`, operator-gated
   - **Inherits:** today `global` is deliberately gated to `tier_not_open` (TIER_UNAVAILABLE_CODE, src/fin/retrieve/tiers.py) so the reserved token has no runtime emit path — no silent global. Opening the 档 is a product decision, not an automatic consequence of the iknow seam landing.
   - **Surface:** tiers.py TIER_UNAVAILABLE_CODE (remove the global→tier_not_open row when opened), host copy, named-failure mapping (community_summary_index_failure becomes reachable at runtime from the iknow typed failure)
   - **Acceptance:** with the 档 open, a global retrieve against a not-ready summary index surfaces `community_summary_index_failure` end-to-end (run/SSE copy), distinct from tier_not_open; live proof `-m mcp tests/retrieve/test_live_search_modes_pg_es.py` re-run against the real server post-iknow-T5b.
   - `[blocks: iknow search-mode-retrieve T5b, operator ruling]`
   - Status: [ ] pending (awaiting operator decision to open the 档)

## End of round

`community_summary_index_failure` runtime emit stays deliberately reserved while the global 档 maps to `tier_not_open` (no silent global). Opening the 档 and the cross-repo live proof are owned by task 4 above, blocked on iknow search-mode-retrieve T5b (the production ES write side T5a plus the read seam) and an operator ruling — the flip is not an automatic consequence of the iknow seam landing. Vocabulary letters A/B/C/D are fully retired (#293); zero letter tier values remain in src/fin.
