# Plan: search-mode graph / global (host wire + copy)

**Goal:** After grounded, fin sends `filters.search_mode` ∈ `A|graph|global|D`; ranking stays in iknow; host writes the operator-visible answer from `chunks[]`.
**Approach:** Expand host keys and copy beside `B`/`C`; normalize former letters before the port; then the port only forwards the new closed set. Assignment cookbook stays in search-mode-host / Soft-Track plans. Do not land port-only `graph`/`global` before iknow expand accepts those tokens.
**Spec link:** `specs/search-mode-graph-global.md`
**Sibling plan:** [`../../iknow-memory/plans/search-mode-graph-global.md`](../../iknow-memory/plans/search-mode-graph-global.md)
**Jev 档灰区（不在本文件）：** [`../docs/handoff/host-jev-choice.md`](../docs/handoff/host-jev-choice.md) T4–T5。本文件只改 wire token / copy。T2 勾完才允许那份 T4。
**ACR:** all-yes (from spec)

bounded-context-guardian: yes — one retrieve port + host copy/cascade tokens; ranking remains iknow; no four retrieve tools.
input-contract-tests: yes — Boundaries name empty / negative / overflow / concurrent / exception including leftover `B`/`C` on the port.
error-handling-enforcer: yes — missing → graph; invalid typed `invalid_search_mode`; **global** not-ready and **D** 未接 keep existing codes/meanings; no silent global.
complexity-anti-drift: yes — rename closed SSOT + copy map; no host assembler / re-ranker.
minimal-change-verifier: yes — wire tokens + copy (+ host normalize so the rename ships); assignment cookbook and retrieve.md body stay out-of-place rewrites.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion (landing grain: operator global commit section)

## 待写入

empty — CONTEXT **检索档** already names MCP `graph` / `global`.

## Tasks (ordered by dependency)

External blocker: iknow `plans/search-mode-graph-global.md` T1 (library accepts `graph`/`global`).

1. **Expand host SSOT + usage copy keys** — tag: `[implementation]`
   - **Inherits:** spec: operator labels stay 查找 / 知识问答 / 综述 / 真数据库; lasting MCP tokens become `graph` / `global`; D copy says document tables stay **graph**; 具名失败 meanings unchanged.
   - **Surface:** `fin.retrieve` tiers + host usage copy
   - **Acceptance:** `graph` and `global` exist as keys beside former `B`/`C` this bullet; D copy states document tables stay graph; `uv run pytest -q` default Fake still green.
   - `[blocks: iknow search-mode-graph-global T1]`
   - Status: [x] done

2. **Normalize former letters before the port** — tag: `[implementation]`
   - **Inherits:** spec: cascade / pin / choice-window ids map `B`→`graph`, `C`→`global`; the port seam’s lasting set is `A|graph|global|D`.
   - **Surface:** host search-mode decision / pin / choice window
   - **Acceptance:** assigned 知识问答 records audit `search_mode=graph` before retrieve; 综述 records `global`; utterance/pin `B档`/`C档` do not leave the port as `B`/`C`.
   - `[blocks: T1]`
   - Status: [ ] pending

3. **Port closed set + five-class Fake** — tag: `[implementation]`
   - **Inherits:** spec: omit / `None` → iknow default graph (do not send `B`); port `B`/`C`/`local`/garbage → typed `invalid_search_mode`; global not-ready and D 未接 keep codes; no host re-rank; one retrieve tool.
   - **Surface:** retrieve port / Fake adapter + named_failures mapping
   - **Acceptance:** Fake or recorded call sends `graph`/`global` for those 档; leftover `B`/`C` on the port is typed invalid; global not-ready → `community_summary_index_failure`; D unwired → `real_db_not_wired`; unopened 综述 → `tier_not_open` on **`global`**; overflow `limit` and concurrent 409 unchanged; no hybrid/RRF in `src/fin`.
   - `[blocks: T2]`
   - Status: [ ] pending

## End of round

After T1–T3 land: one code-review phase vs `specs/search-mode-graph-global.md`, then verification-before-completion. Unblocks iknow contract (`B`/`C` refuse).
