# Plan: search-mode retrieve (fin port)

**Goal:** Grounded retrieve passes `filters.search_mode` and maps 具名失败 including C index failure; ranking stays in iknow.
**Approach:** Wire the existing port + `named_failures`; Fake tests only for contract. Product A/B/C ranking is the iknow sibling plan.
**Spec link:** `specs/search-mode-retrieve.md`
**Sibling plan:** [`../../iknow-memory/plans/search-mode-retrieve.md`](../../iknow-memory/plans/search-mode-retrieve.md)
**Program:** `plans/extract-index-search-mode-program.md` wave 3b
**ACR:** all-yes (from spec)

bounded-context-guardian: yes — one retrieve port; ranking in iknow; no four host retrieve nodes.
input-contract-tests: yes — Boundaries name all five classes; Fake covers contract + named exits.
error-handling-enforcer: yes — codes pinned (`corpus_not_ready` / `mapping_drop` / `tier_not_open` / `real_db_not_wired` / `community_summary_index_failure`); missing mode → B; invalid not silent C.
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
   - Status: [ ] pending

2. **Port forwards `filters.search_mode`** — tag: `[implementation]`
   - **Inherits:** spec — missing → B on iknow; fin must pass the host’s assigned 档.
   - **Surface:** `fin.retrieve` adapter / retrieve tool
   - **Acceptance:** Fake or recorded MCP call includes `filters.search_mode` when host set a 档; invalid 档 from a pin still uses `tier_not_open` / `real_db_not_wired` without starting a silent C retrieve.
   - `[blocks: T1]`
   - Status: [ ] pending

3. **Five-class Fake contract** — tag: `[implementation]`
   - **Inherits:** spec Boundaries test classes; Fake is not a 档.
   - **Surface:** `tests` retrieve/host Fake
   - **Acceptance:** empty / negative / overflow cap / concurrent 409 / exception MCP 409 covered on the port+named-exit path; `uv run pytest -q` stays green.
   - `[blocks: T2]`
   - Status: [ ] pending
