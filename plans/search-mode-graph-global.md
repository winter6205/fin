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
   - Status: [x] done
   - **Landed at the time (code-review correction):** only the AUDIT layer was normalized — every entry point (utterance / pin / choice-window pick / cascade) resolved onto a lasting token on `search_mode_decision`, and the port's closed set refused a letter. The assigned 档 was never HANDed to the port: `fin.retrieve.tool.retrieve` passed `search_mode=None` through, so `resolve_search_mode(None)` wrote no `filters.search_mode` key and the wire was 档-free. That hole is T4, not this bullet; the "do not leave the port as `B`/`C`" assertions were tautologies while the hop was missing and are now positive (they assert the exact token the port recorded, and still refuse a letter).

3. **Port closed set + five-class Fake** — tag: `[implementation]`
   - **Inherits:** spec: omit / `None` → iknow default graph (do not send `B`); port `B`/`C`/`local`/garbage → typed `invalid_search_mode`; global not-ready and D 未接 keep codes; no host re-rank; one retrieve tool.
   - **Surface:** retrieve port / Fake adapter + named_failures mapping
   - **Acceptance:** Fake or recorded call sends `graph`/`global` for those 档; leftover `B`/`C` on the port is typed invalid; global not-ready → `community_summary_index_failure`; D unwired → `real_db_not_wired`; unopened 综述 → `tier_not_open` on **`global`**; overflow `limit` and concurrent 409 unchanged; no hybrid/RRF in `src/fin`.
   - `[blocks: T2]`
   - Status: [x] done
   - **Landed at the time (code-review correction):** the port/adapter/Fake closed set and its typed refusals (including the Fake recording the token it accepted) plus the mcp-protocol live driver. The "recorded call sends `graph` for that 档" half only held for a caller that NAMED the 档 directly (the live driver and the port unit tests); a host run recorded no 档 because the hop was absent. See T4. The `global` half of that bullet never held for ANY fin caller, direct or otherwise: `TIER_UNAVAILABLE_CODE` maps `global` → `tier_not_open` (`fin/retrieve/tiers.py`), so `resolve_search_mode` refuses before any wire write (`fin/retrieve/port.py`). `global` on the wire is therefore proven server-side only, via the raw JSON-RPC round trip in the live driver, until that 档 opens.

4. **宿主把 assigned 档 递到端口 (code-review repair)** — tag: `[implementation]`
   - **Inherits:** spec SC2 / SC3 / SC6 / SC7 / SC8: after **grounded** + an assigned 档, the EXISTING `retrieve` tool / port sends `filters.search_mode`; omit / `None` sends no key (iknow's own default); `global` and `D` keep refusing earlier as 具名失败, so the hop never becomes a silent default. ADR 0061 keeps exactly ONE retrieve tool and its LLM-visible signature gains no 档 argument — the cascade stays the single source of truth.
   - **Surface:** `fin.host.search_mode_decision` (`assigned_retrieve_mode`, the one reader of「本次跑的是哪个档」 — it owns the `search_mode_decision` channel) → `fin.host.graph` (`retrieve` tool, `IssuerTask`, the fan-out router and the `issuer_research` branch) → `fin.retrieve.tool` → port / `mcp_adapter` filters
   - **Acceptance:** a grounded 知识问答 run's port calls record `graph`, a 查找 run's record `A`; a refused 综述 run records NO 档 (never a substitute token); a retired letter left in the audit is forwarded VERBATIM and the port names it `invalid_search_mode`; three runs on ONE cached graph object carry their own (or no) 档, so a compile-time capture is impossible; `retrieve` stays the only retrieve tool bound and its args schema carries no `search_mode`.
   - `[blocks: T3]`
   - Status: [x] done

## End of round

After T1–T3 land: one code-review phase vs `specs/search-mode-graph-global.md`, then verification-before-completion. Unblocks iknow contract (`B`/`C` refuse).

Superseded by `plans/search-mode-lexical-sql-tokens.md` (2026-09-22): `TIER_INPUT_TOKENS` is deleted and no seam reads a bare letter, so the wider entry-input tuple described below is the record of this increment only.

Landed state: `RETRIEVE_TIERS` is the closed `A|graph|global|D` and is NOT a superset of `B`/`C`; the retired letters live only on the wider entry-input tuple (`TIER_INPUT_TOKENS`) so the send door can still type and normalize a live request that names them. Live proof: `-m mcp tests/retrieve/test_live_search_modes_pg_es.py` (10 passed) over real PG + ES + Neo4j — the direct-adapter half, plus `-m mcp tests/retrieve/test_live_host_graph_hop.py` (1 passed), which drives the compiled host graph against the same real server and asserts the 档 the cascade assigned is the 档 that reaches the wire and that the server answers. Host proof (T4): the assigned 档 now rides the run's wire call — `tests/host/test_assigned_mode_reaches_port.py` asserts the port recorded `graph` / `A` / no-key per run on ONE cached graph, and the fan-out branches inherit the same assignment. The repair round also moved the reader into `fin.host.search_mode_decision` (one owner of the `search_mode_decision` read) and removed its non-mapping→「不送档」branch, so an out-of-contract audit value is named by the port instead of being handed to iknow's default.
