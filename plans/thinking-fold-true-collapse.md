# Plan: thinking fold true collapse + thinking pending

**Goal:** Collapsed **thinking fold**s hide the burst body; a slow first SSE frame still shows **thinking pending** on the same chrome so the **research thread** does not sit blank after send.
**Approach:** Flush the glossary and spec reversal first (three-line tail preview is out). Then make collapse header-only. Then seed **thinking pending** on the optimistic turn and hand the first real `thinking` (or drop on `tool_call` / `answer`) into that same region. No new SSE types and no host-graph change.
**Spec link:** `specs/turn-trace-folds.md` (T1 amendment)
**ACR:** all-yes (block below)
**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch
**Tracker:** GitHub. Spec: [spec: thinking fold true collapse + thinking pending](https://github.com/winter6205/fin/issues/254). Tickets: [T1 SSOT](https://github.com/winter6205/fin/issues/255), [T2 header-only collapse](https://github.com/winter6205/fin/issues/256) (blocked by T1), [T3 thinking pending](https://github.com/winter6205/fin/issues/257) (blocked by T2).
**Worktree:** `.worktrees/thinking-fold-true-collapse` on `feat/thinking-fold-true-collapse`

> Contradicts ADR-0025 — the accepted paragraph still says “tail preview on collapsed thinking”. Reopen that sentence only: grain stays burst-scoped folds; collapsed thinking is header-only; **thinking pending** is named. Worth reopening because CONTEXT and this plan’s T1 replace the preview contract.

Two behaviours only — three bullets because the SSOT reversal is a docs ticket before UI. Not padded: T1 has no code; T2/T3 are the two ACR commits.

## ACR

bounded-context-guardian: yes — stays in 工作台 transcript + glossary SSOT; no new top-level package; no retrieve/memory/host HTTP contract change
defensive-contract-validator: yes — empty (header-only / pending no body); negative (first `tool_call` or `answer` drops pending); overflow (full burst hidden when collapsed); concurrent (same-region pending→first thinking; existing no-queue send); exception (pre-frame drop of the optimistic turn unchanged)
error-handling-enforcer: yes — no new SSE/error types; pending rides the optimistic turn and inherits existing 409/503 / drop-if-no-frame paths
complexity-anti-drift: yes — two local transcript edits (collapsed body hidden; pending as empty 深度思考 chrome in the same region); no god-function or host-graph rewrite
minimal-change-verifier: yes — two behaviours, two implementation commits after one SSOT ticket; do not merge collapse and pending into one ticket

affects: `docs/CONTEXT.md`; `docs/adr/0025-ordinary-person-workspace.md`; `specs/turn-trace-folds.md` (+ `specs/workspace.md` pointer if still claiming tail preview); `web/` thread transcript / send store / existing fold tests

## Tasks (ordered by dependency)

1. **SSOT: true collapse + thinking pending** — tag: `[decision]`
   - **Inherits:** LogicSync 1A/2A (2026-09-09): collapsed **thinking fold** is header-only; live burst stays expanded with tail-follow; **thinking pending** uses the same 深度思考 chrome until the first `thinking` frame; first `tool_call` / `answer` drops pending; no new SSE types
   - **Surface:** `docs/CONTEXT.md`, `docs/adr/0025-ordinary-person-workspace.md`, `specs/turn-trace-folds.md` (and the workspace spec pointer if it still quotes the three-line preview)
   - **Acceptance:** CONTEXT **thinking fold** no longer names a three-line tail preview; **thinking pending** is defined; ADR 0025 no longer cites tail preview as the burst grain; the fold spec’s collapsed / pending clauses match those terms; commit touches no `web/` or `src/` behaviour
   - Status: [x] done

2. **Collapsed thinking is header-only** — tag: `[implementation]`
   - **Inherits:** amended spec: live thinking remains expanded full text with tail-follow; manual collapse and auto-collapse at burst end hide the burst body; header is 深度思考 plus duration when measured; answer / citations / **tool fold** unchanged
   - **Surface:** `web/` thread transcript thinking chrome
   - **Acceptance:** a collapsed thinking burst shows the header and does not show burst prose (no three-line preview node); expanding shows the full burst; a live un-collapsed burst still shows the latest lines; `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending
   - [blocks: T1]

3. **thinking pending until the first real frame** — tag: `[implementation]`
   - **Inherits:** amended spec: optimistic send immediately shows **thinking pending** (same 深度思考 chrome, no body); first `thinking` frame becomes that fold; first `tool_call` or `answer` removes pending and attaches the real fold or answer card; stream error / no-frame drop still removes the optimistic turn
   - **Surface:** `web/` send/resume store + thread transcript
   - **Acceptance:** after send and before any SSE event, the assistant side shows 深度思考 chrome with no thinking prose; a first `thinking` delta appears in that same region (not a second card); a thinking-off stream whose first event is `tool_call` or `answer` does not leave a leftover pending fold; existing drop-if-no-frame behaviour still holds
   - Status: [ ] pending
   - [blocks: T2]

## 待写入

- Update **thinking fold** (header-only collapse; drop three-line tail preview)
- Add **thinking pending**
- Amend ADR 0025 burst-grain sentence (drop tail preview; point at amended spec) — not a new ADR

Flushed 2026-09-09 in this worktree (domain-modeling): CONTEXT **thinking fold** / **thinking pending**; ADR 0025 burst-grain sentence. Spec prose remains T1.

## Code review phase

After all bullets: `arthurpower:code-review` then `arthurpower:verification-before-completion` on the branch.
