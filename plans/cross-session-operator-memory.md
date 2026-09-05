# Plan: cross-session-operator-memory

**Goal:** Product-default cross-session private memory: facts + hook prefetch + two-layer user profile; research cites stay retrieve-only; default pytest stays memory OFF.
**Approach:** First supersede the leftover MDP T5/T6 and dream→MCP contracts so implementers do not follow them. Then evolve the store, flip boot defaults, assemble prefetch/existence-pointer, wire explicit tools, gate auto-extract and dream, prove memory cannot cite, then run the over-attention gold set.
**Spec link:** `specs/cross-session-operator-memory.md`
**Worktree:** `/home/winner/projects/fin-wt-cross-session-memory` · branch `feat/cross-session-operator-memory`
**Tracker:** FALLBACK — local markdown. Operator ruled no GitHub issues for [公司操作者跨会话私人记忆](../docs/wayfinder/cross-session-operator-memory-map.md); do not `gh issue create`.
**Out of this worktree:** retrieve ranking; citation span rules; Soft-Track rename; iknow-memory code; memory index UI; vector search on facts.
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（`uv run pytest -q`；memory cases as the spec’s ON path; web only if a bullet touches `web/`: `npx tsc --noEmit`）→ verification-before-completion → one commit on this worktree branch
**End-of-round:** 全部子弹落地后对整轮 diff 做一次 code-review（Standards + Spec）；单弹 loop 不重复整轮 review。
**待写入:** empty — CONTEXT + ADR 0035/0048 already flushed 2026-09-06.

## ACR（自 `specs/cross-session-operator-memory.md` 原样粘贴）

bounded-context-guardian: yes — private memory stays a host/workspace capability behind the **memory port**; company corpus stays on the **retrieve port**; **user profile** is overlay files, not a second retrieve backend; no `controllers/` / `services/` layer; web is out of this spec.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 3–9, 13–19, 22–23.
error-handling-enforcer: yes — named EXITs: explicit OFF, ON without checkpoint URL (typed), extract deny (no persist), dream fail must not fail the foreground turn, no silent MCP write, invalid `FIN_MEMORY_ENABLED` stays typed.
complexity-anti-drift: yes — store, prefetch assembly, graph tools, auto-extract, dream gates are separate concerns; dream is not nested in `model_node`; prefetch is host assembly not a model tool; writing-plans must not dump all SCs into one commit.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences tracer bullets; conflict with “1 commit” is resolved by split. Docs supersede of MDP/dream contracts is its own bullet before code.

**OVERALL: PASS**

## Harvest（已决 vs 开放）

**已决（quote spec / ADR 0048）：**

- Product boot ON; default pytest OFF unless the case is about memory.
- Facts in `fin_memory`; hook = first body line ≤80; prefetch ≤3 lexical; zero hits → 0.
- System: existence pointer only; no catalog; no promote.
- Tools: save / recall / edit user profile; no dream tool.
- Extract N=3, cap 1, skip if this-turn save; deny retrieve-able and profile-already-said.
- Dream: 24h cooldown; threshold OR; facts&lt;2 no LLM and no cooldown refresh; no per-turn SSE; no MCP write.
- Memory ≠ citation evidence (ADR 0035, ADR 0046).
- User profile: `$HOME/.fin/user.md` overlay `<workspace>/.fin/user.md`.

**开放（implementer headroom）：**

- `fin_memory` column names and migration shape.
- Lexical tokenizer / title-vs-body weights (must be title-heavier; zero token hit = 0).
- Graph tool names and profile-layer argument shape.
- Extract prompt strings (deny **categories** are gates; wording follows `docs/guides/prompt-development.md`).
- Dream LLM injectable; scoring internals as long as SC17–SC22 hold.
- Optional status-snapshot same-name downweight.

## Tasks (ordered by dependency)

1. **Supersede leftover MDP T5/T6 and dream→MCP contracts** — tag: `[decision]`
   - **Inherits:** spec SC30; ADR 0048 2026-09-06; this spec wins over `specs/memory-dialogue-platform.md` assumptions 5–6 and `specs/memory-dream.md` SC17–SC18.
   - **Surface:** `specs/memory-dialogue-platform.md`, `specs/memory-dream.md`, `plans/memory-dialogue-platform.md`, `docs/wayfinder/cross-session-operator-memory-map.md`
   - **Acceptance:** those files point at this spec/plan for remaining memory product wiring; they no longer instruct dream MCP upload or unconditional post-SSE dream as in-force work; commit touches no product code.
   - Status: [ ] pending

2. **Memory facts round-trip in the working store** — tag: `[implementation]`
   - **Inherits:** spec SC5–SC6; ADR 0048: one extract = one fact; workspace isolation; hook is a projection not a second store.
   - **Surface:** host memory store / buffer
   - **Acceptance:** title+body persist and read back; hook equals first non-empty folded body line ≤80; workspace A rows are invisible in workspace B; empty store returns 0; `uv run pytest -q` exits 0.
   - **Completion:** column names and DDL shape open.
   - Status: [ ] pending
   - [parallel with T3]

3. **Product boot ON; default pytest OFF** — tag: `[implementation]`
   - **Inherits:** spec SC2–SC4; CONTEXT **memory port**; ADR 0035 2026-09-06: pytest still OFF; ON without `FIN_CHECKPOINT_URL` stays typed.
   - **Surface:** host memory port / compile / test harness
   - **Acceptance:** unset `FIN_MEMORY_ENABLED` + set checkpoint URL → ON port; default pytest → OFF (zero tools, zero prefetch, zero extract, zero dream LLM); garbage enabled value stays typed; `uv run pytest -q` exits 0.
   - **Completion:** how pytest injects OFF is open.
   - Status: [ ] pending
   - [parallel with T2]

4. **Host prefetch overlay** — tag: `[implementation]`
   - **Inherits:** spec SC7–SC9, SC24; CONTEXT **memory prefetch** / **recall injection**.
   - **Surface:** host assembly (not a model tool)
   - **Acceptance:** zero lexical hits → 0 rows; four hits → ≤3; already-shown id on this `thread_id` skipped; occupancy ledger is higher with prefetch than without; `uv run pytest -q` exits 0.
   - **Completion:** tokenizer and title/body weights open (title heavier; miss = 0).
   - Status: [ ] pending
   - [blocks: T2]

5. **Stable system prefix + existence pointer** — tag: `[implementation]`
   - **Inherits:** spec SC10–SC11; CONTEXT **memory existence pointer**; prompt-development: prefix stable, prefetch not in prefix.
   - **Surface:** host prompt assembly
   - **Acceptance:** system prefix contains the existence pointer and no full-store title list; prefetch rows appear on the user side; two turns with different hits keep the same prefix bytes; `uv run pytest -q` exits 0.
   - **Completion:** pointer wording open (must stay advisory, not an order to recall).
   - Status: [ ] pending
   - [blocks: T4]

6. **Explicit save / recall / user-profile tools** — tag: `[implementation]`
   - **Inherits:** spec SC12–SC13; map G4: no dream tool.
   - **Surface:** host graph tools + user profile files
   - **Acceptance:** ON graph exposes save, recall, and layered profile edit; no dream tool; workspace write does not rewrite global; merged read is workspace over global; OFF graph exposes none of these; `uv run pytest -q` exits 0.
   - **Completion:** tool names and layer argument shape open.
   - Status: [ ] pending
   - [blocks: T2, T3]

7. **Gated auto-extract** — tag: `[implementation]`
   - **Inherits:** spec SC14–SC16; deny categories in Boundaries.
   - **Surface:** host extract path
   - **Acceptance:** 2 completed turns → 0 auto facts; 3 turns may write at most one; this-turn save → 0 extract; retrieve-able filing claim and profile-duplicate are not persisted; `uv run pytest -q` exits 0.
   - **Completion:** prompt strings open; deny classes are code/schema.
   - Status: [ ] pending
   - [blocks: T2, T6]

8. **Dream merge gates; no SSE; no MCP write** — tag: `[implementation]`
   - **Inherits:** spec SC17–SC22, SC29; CONTEXT **dream**; ADR 0048: local merge only.
   - **Surface:** host dream + HTTP/SSE wiring
   - **Acceptance:** 24h blocks; facts&lt;2 → 0 LLM and cooldown unchanged; threshold OR must hold after cooldown; no dream node in `compile_graph`; completion path does not unconditionally enqueue post-SSE dream; 0 memory-MCP writes; identity/soul/user-profile bytes unchanged after merge; foreground turn still `done`/`refuse` if dream fails; `uv run pytest -q` exits 0.
   - **Completion:** injectable dream model and merge internals open.
   - Status: [ ] pending
   - [blocks: T2]
   - [parallel with T6 / T7 after T2]

9. **Memory is not citation evidence** — tag: `[implementation]`
   - **Inherits:** spec SC23; ADR 0035; ADR 0046.
   - **Surface:** citation verifier + memory overlay
   - **Acceptance:** a research turn that quotes only prefetch/recall/fact text, with no this-run retrieve/`read_file` evidence, does not `done` with citations sourced from memory; `uv run pytest -q` exits 0.
   - **Completion:** how the verifier ignores memory payloads is open as long as the property holds.
   - Status: [ ] pending
   - [blocks: T4, T6]

10. **Over-attention gold set** — tag: `[implementation]`
    - **Inherits:** spec SC25–SC28; G3: gold is acceptance, not an enable-gate; scripted model.
    - **Surface:** host tests (memory ON)
    - **Acceptance:** greeting → 0 prefetch; research number records retrieve or `read_file`; company-named fact still uses retrieve for a filing claim; explicit save on thread A is visible via recall or prefetch on thread B when the utterance hits; default `uv run pytest -q` still OFF and exits 0.
    - **Completion:** fixture layout and mark name open.
    - Status: [ ] pending
    - [blocks: T3, T5, T6, T7, T8, T9]
