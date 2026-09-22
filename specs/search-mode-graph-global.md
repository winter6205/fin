# Spec: search-mode graph / global (host wire tokens + copy)

> **Status:** assumption gate locked 2026-09-20 (this file). Token rename is **in implementation scope**.
> **Sibling (iknow-memory, Out-link; may land in parallel):** [`../../iknow-memory/specs/search-mode-graph-global.md`](../../iknow-memory/specs/search-mode-graph-global.md)
> **Do not rewrite in place:** [`specs/search-mode-retrieve.md`](search-mode-retrieve.md) (档 consumption / ranking-behind-port). This spec **Inherits** that contract and **Changes** the MCP `filters.search_mode` closed set plus host usage copy.
> **档 assignment cookbook (out-link, not restated here):** [`specs/soft-track-judge-route.md`](soft-track-judge-route.md) then [`specs/search-mode-host.md`](search-mode-host.md) — Soft-Track `track=grounded`, then cascade names one of `A|graph|global|D`.
> **Plan:** not opened until `architecture-change-reviewer` on this spec.

## Objective

After Soft-Track has already chosen **grounded** and the host cascade has named a **检索档**, fin passes that 档 on the existing **retrieve port** as MCP `filters.search_mode` with lasting tokens **`A` | `graph` | `global` | `D`**. Knowledge-QA is **`graph`** (formerly `B`; missing key defaults to **graph**). 综述 is **`global`** (formerly `C`). **A 查找** and **D 真数据库** wire letters stay. Invalid values are typed. Ranking stays in **iknow**. The host does not re-rank; it writes the operator-visible answer from returned `chunks[]` (optional later batching when many reports arrive). Usage copy strings follow the same tokens.

**Who:** local operator on the **工作台**; product **知识 MCP** (**iknow-memory**) ranks; fin is host.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **检索档** (`search mode`): Which retrieve path a knowledge question uses after it has entered **grounded**: A 查找 (lexical-first), **graph** 知识问答 (iknow graph-priority then vector∥BM25 fill; community summaries may mix in, capped by **community level** — MCP `filters.search_mode=graph`, formerly `B`; missing key defaults to graph), **global** 综述 (index-time community summaries; report text ES ∥ report vectors, then optional drop to windows — ADR 0070; MCP `filters.search_mode=global`, formerly `C`), D 真数据库 (real SQL/business DB; document tables stay graph). Assignment reuses the Soft-Track cascade (ADR 0027 / 0062): rules, then structured classify when ambiguous, then inherit, then clarify or typed fail — not a required picker and not silent global. Cascade **clarify** opens a **choice window** (structured options → operator pick → resume execute). Search-mode copy is call-scoped (ADR 0061): inject once in near context, never rewrite history; after **session compaction**, re-inject the mode this turn needs.
- **grounded**: The Soft-Track leaf that binds corpus tools and the **citation verifier** (ADR 0061). Not a product name, not **search mode**, and not the `retrieve` tool. Wire value `research` is an alias for this leaf.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **具名失败**: A typed product failure that names empty-bound 资料, mapping-drop, citation-verifier refuse, 档未开, 真库未接, or community-summary index failure as distinct exits — not one `unknown_locator` plus empty citations.

## Confirmed assumptions (gate)

1. This increment is the **wire token change + call-scoped copy strings** on the fin host / **retrieve port**. 档 **assignment** (rules / classify / inherit / **choice window**) remains [`search-mode-host.md`](search-mode-host.md); track judge remains [`soft-track-judge-route.md`](soft-track-judge-route.md). Do not fork a second cascade.
2. Closed MCP set is `filters.search_mode` ∈ `A|graph|global|D`. **A** and **D** unchanged. **graph** replaces lasting `B`. **global** replaces lasting `C`. Missing key defaults to **graph** (backward compatible with omit). Invalid (including leftover `B`, `C`, and `local`) is typed `invalid_search_mode`, not silent **global**, not silent **graph**.
3. Host **normalize** (cascade / pin / choice-window ids / usage-copy keys) **is the rename**: operator labels stay 查找 / 知识问答 / 综述 / 真数据库; utterance `B档` / `C档` and former pin letters map to **graph** / **global** before the port. The port seam accepts only `A|graph|global|D`.
4. Ranking stays in **iknow**. fin does not add four retrieve tools or four LangGraph retrieve nodes (ADR 0061). Host does not re-rank `chunks[]`.
5. Host writes the user-visible answer from this-run `chunks[]`. Optional later batching of many **global** reports is allowed as a follow-on; this spec does not invent a second retrieve tool for that batching.
6. **global** not-ready and **D** unwired keep the same **具名失败** _meanings_: community-summary index failure (`community_summary_index_failure`) vs 真库未接 (`real_db_not_wired`). Unopened 综述 as a 档 is still 档未开 (`tier_not_open`) on token **`global`**. Document tables stay **graph**, not **D**.
7. [`search-mode-retrieve.md`](search-mode-retrieve.md) consumption (A child→parent window; graph-priority then fill; report ES∥vectors then optional window drop; D named 未接) is **inherited**, not rewritten in that file this turn.
8. Default `uv run pytest -q` stays **Fake** retrieve; Fake is not a 档. Live MCP is opt-in.
9. **资料类型归档** is out (ops later).

## Boundaries

- **Does:**
  - After **grounded** + assigned 档, the existing `retrieve` tool / **retrieve port** sends `filters.search_mode` = `A` | `graph` | `global` | `D`. Omit / `None` on the port means iknow default **graph**.
  - Closed SSOT (`fin.retrieve.tiers` / host re-export): replace `B`/`C` members with `graph`/`global`; `TIER_UNAVAILABLE_CODE` keys follow the new tokens (`global` → `tier_not_open` until that 档 is an open product surface; `D` → `real_db_not_wired`). Runtime **global** not-ready (summaries incomplete / 社区 job failed) still surfaces `community_summary_index_failure`.
  - Host cascade classify JSON, choice-window option `id`s, audit `search_mode`, HTTP pin after normalize, and `SEARCH_MODE_USAGE_COPY` keys use `A|graph|global|D`. Copy strings name **graph** 知识问答 and **global** 综述; D copy says document tables stay **graph**.
  - Host normalize maps former letters `B`→`graph`, `C`→`global` (and CONTEXT labels) so the rename lands in implementation; the MCP/port exact match does **not** treat `B`/`C` as legal wire values.
  - Answer path: model/host composes the operator-visible reply from ranked `chunks[]` (and existing `read_file` / citation verifier). No host hybrid/RRF. No fourth MCP retrieve tool.
  - Test classes (extend existing search-mode / port suites; default pytest Fake): empty (omit `search_mode` → graph, not empty success-as-miss); negative (`B`/`C`/`local`/garbage on the **port** → `invalid_search_mode`; pin/name of unopened **global** → 档未开); overflow (existing retrieve `limit` cap unchanged); concurrent (existing 409 `concurrent_run`); exception (MCP down already named; **global** not-ready and **D** 未接 keep their codes).

- **Confirms with human:** none (tokens, defaults, named-failure meanings, no re-rank, copy+rename in scope locked 2026-09-20).

- **Out of this spec:**
  - Rewriting [`search-mode-retrieve.md`](search-mode-retrieve.md) in place; iknow assembler internals (sibling iknow spec).
  - Restating the Soft-Track / search-mode **assignment** cookbook ([`soft-track-judge-route.md`](soft-track-judge-route.md), [`search-mode-host.md`](search-mode-host.md)): cascade tiers, **choice window** UX, Fast propose, required-picker ban.
  - Four retrieve tools; host re-rank; inventing SQL for **D**.
  - **资料类型归档** (ops later).
  - Passing `filters.community_level` from fin (iknow default when omitted).
  - Implementing report batching this increment (allowed later; still one retrieve port).
  - Citation verifier rewrite; checkpoint history rewrite.

## Success Criteria

1. `uv run pytest -q` exits 0 with default **Fake** retrieve. `web/` `tsc --noEmit` exits 0 when this task diffs `web/` (skip if no web diff).
2. Port / MCP adapter: assigned 知识问答 sends `filters.search_mode=graph`; assigned 综述 sends `global` **only once 综述 opens** — while `TIER_UNAVAILABLE_CODE` maps `global → tier_not_open` (`fin.retrieve.tiers`), `resolve_search_mode` refuses before any filter write, so no fin caller can put `global` on the wire and that half is proven server-side only, by the raw JSON-RPC probe in the live driver. **A** and **D** unchanged. Missing key / `search_mode=None` does not send `B` and is treated as **graph**.
3. Port negative: `search_mode` in `{B, C, local, ""}` or any non-set token raises / returns typed `invalid_search_mode` (not mapped at the port, not silent **global**).
4. Host normalize + copy: cascade / pin / choice-window `id` / `SEARCH_MODE_USAGE_COPY` use `graph` and `global` as keys; usage copy for 知识问答 and 综述 does not instruct the model that lasting MCP tokens are `B` or `C`; D copy states document tables stay **graph**.
5. Host does not contain a hybrid/RRF (or other) re-ranker for this task; still one `retrieve` tool on the compiled graph.
6. **global** not-ready → `community_summary_index_failure` (same meaning as former C not-ready) — **deferred to the 综述-opening ticket**: fin has no producer for this code yet, only the constant (`fin.host.named_failures`) and its operator copy (`run_payload`); mapping the server's `degraded_reasons` onto it lands when the 档 opens. Until then it is observed server-side only, in the raw probe. **D** unwired → `real_db_not_wired`. Unopened 综述 档 → `tier_not_open` on **`global`**. None of these become empty `chunks[]` as success.
7. Opt-in (or existing) host suite: after **grounded**, a high-confidence 知识问答 assign records audit `search_mode=graph` **before** retrieve; a 综述 assign records `global`. Residual cascade still must not silent-open **global**.
8. Fake tests cover port contract + named-exit mapping only; they do not assert iknow ranking.
9. Citation still uses this-run retrieve/`read_file` text; this spec does not replace the **citation verifier**.

## Open Questions

(none — wire set, default **graph**, invalid typed, named-failure meanings, no host re-rank, rename in scope locked 2026-09-20.)

## Inherits / Changes

**Inherits:** CONTEXT terms above; [`specs/search-mode-retrieve.md`](search-mode-retrieve.md) 档 consumption (A / knowledge-QA / 综述 / D arms, Fake default, one port, named exits) **except** the closed token list `A|B|C|D` and missing→**B**; [`specs/search-mode-host.md`](search-mode-host.md) cascade / injection / **choice window** / pin / Fast propose; [`specs/soft-track-judge-route.md`](soft-track-judge-route.md) four-leaf track judge; ADR 0061 (ranking behind port; call-scoped copy; no four host retrieve nodes); `fin.retrieve.port` + `fin.retrieve.tiers` + `SEARCH_MODE_USAGE_COPY`; default `uv run pytest -q`.

**Changes:** lasting `filters.search_mode` ∈ `A|graph|global|D`; missing → **graph**; host copy + closed SSOT + classify/window/pin/audit keys renamed in implementation; port rejects former `B`/`C` as invalid; document tables stay **graph**; host answer from `chunks[]` without re-rank.

**Does not change:** Soft-Track track enum; retrieve ranking algorithm (iknow); **具名失败** _meanings_ for 档未开 / 真库未接 / community-summary index failure; four-tool prohibition.

## 待写入 (persist after ACR)

empty — CONTEXT **检索档** already names MCP `graph` / `global`, missing→graph, document tables stay graph, and avoids lasting `B`/`C` / `local`.

## architecture-change-reviewer

Affected surfaces (implementation after this spec; not this file alone): `src/fin/retrieve/tiers.py`, `src/fin/retrieve/port.py`, `src/fin/retrieve/mcp_adapter.py` / Fake ports, `src/fin/host/search_mode_decision.py`, `src/fin/host/agent_instructions.py` (`SEARCH_MODE_USAGE_COPY`), `src/fin/host/named_failures.py` (docs/re-export), matching host/retrieve tests. Ranking code stays out of `src/fin`.

bounded-context-guardian: yes — one retrieve port + host copy/cascade tokens; ranking remains iknow; no four retrieve tools.
input-contract-tests: yes — Boundaries name empty / negative / overflow / concurrent / exception including leftover `B`/`C` on the port.
error-handling-enforcer: yes — missing → graph; invalid typed `invalid_search_mode`; **global** not-ready and **D** 未接 keep existing codes/meanings; no silent global.
complexity-anti-drift: yes — rename closed SSOT + copy map; no host assembler / re-ranker.
minimal-change-verifier: yes — wire tokens + copy (+ host normalize so the rename ships); assignment cookbook and retrieve.md body stay out-of-place rewrites.

OVERALL: PASS — 2026-09-20 ACR. Next: writing-plans (not this turn; operator scoped the spec file only).
