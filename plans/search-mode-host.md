# Plan: search-mode host

**Goal:** After Soft-Track `grounded`, assign **检索档** with the same cascade, inject call-scoped copy, and clarify via choice-window interrupt — no four retrieve nodes, no silent C.
**Approach:** Copy `decide_track` shape into a search-mode helper; HITL interrupt payload; SPA may stub-paint. Ranking and extract jobs stay other plans.
**Spec link:** `specs/search-mode-host.md`
**Program:** `plans/extract-index-search-mode-program.md` wave 4
**ACR:** all-yes (from spec)

bounded-context-guardian: yes — host graph + 工作台; ranking and extract jobs in sibling specs.
input-contract-tests: yes — Boundaries + SC2 name empty / negative / overflow / concurrent / exception.
error-handling-enforcer: yes — search-mode classify EXITs; 档未开 / 真库未接; no silent C.
complexity-anti-drift: yes — cascade helper mirrors `decide_track`; not four retrieve nodes.
minimal-change-verifier: yes — one increment (档 + choice window + Fast propose).

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion

## 待写入

empty

## Tasks (ordered by dependency)

1. **Search-mode cascade after grounded** — tag: `[implementation]`
   - **Inherits:** ADR 0062; spec Does rules → classify → inherit → clarify/typed fail; residual must not open C.
   - **Surface:** `fin.host` (beside `track_decision`)
   - **Acceptance:** Opt-in suite: grounded + rule/pin assigns `search_mode` before retrieve; greeting has no 档; invalid classify never silent-C; five boundary classes named in spec.
   - Status: [ ] pending
   - `[blocks: plans/search-mode-retrieve.md T2]` for live retrieve flag, not for cascade unit tests

2. **Choice window interrupt** — tag: `[implementation]`
   - **Inherits:** ADR 0062 HITL; spec clarify payload `{id, label}`.
   - **Surface:** `fin.host` graph interrupt + workspace HTTP; `web` stub allowed
   - **Acceptance:** Ambiguous grounded ask → interrupt options; resume pick assigns that 档; payload contract test without requiring painted SPA.
   - `[blocks: T1]`
   - Status: [ ] pending

3. **Call-scoped 档 copy + compaction re-inject** — tag: `[implementation]`
   - **Inherits:** ADR 0061.
   - **Surface:** `fin.host` agent_instructions / compile call
   - **Acceptance:** Checkpointed messages do not store that turn’s search-mode usage as rewritten history; after compact, the 档 this turn needs is injected once again.
   - `[blocks: T1]`
   - `[parallel]` with T2
   - Status: [ ] pending

4. **Fast propose window (not a 档)** — tag: `[implementation]`
   - **Inherits:** ADR 0068 / 0079; no silent Fast.
   - **Surface:** same choice-window path
   - **Acceptance:** Propose path opens options; no pick keeps extract method **standard**; `search_mode` is never the string Fast.
   - `[blocks: T2]`
   - Status: [ ] pending
