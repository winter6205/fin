# Plan: turn trace folds

**Goal:** The **research thread** transcript uses burst-scoped **thinking fold**s and **tool fold**s, matching live summaries to GET, with last-turn SVG thumbs.
**Approach:** First make SSE one-liners carry the same args as persist (so retrieve query is visible for debug). Then group the stream into sibling folds with live expand / auto-collapse / three-line tail preview. Then swap feedback chrome. Docs for the contract already landed with this plan's spec.
**Spec link:** `specs/turn-trace-folds.md`
**ACR:** all-yes (block below)
**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch
**Tracker:** GitHub. Spec: [spec: turn trace folds](https://github.com/oocang/fin/issues/248). Tickets: [T1 SSE and persist tool one-liners share args](https://github.com/oocang/fin/issues/249), [T2 Thinking fold and tool fold on the live timeline](https://github.com/oocang/fin/issues/250) (blocked by T1), [T3 Last-turn SVG thumbs](https://github.com/oocang/fin/issues/251) (parallel).

## ACR

bounded-context-guardian: yes — transcript chrome stays the **工作台** surface of **host**; retrieve/MCP stay behind the **retrieve port**; no new top-level technical-layer package.
defensive-contract-validator: yes — empty thinking (no region); empty/missing `tool_call_id` still a row; unknown tool name; overflow many tools in one burst (fold, do not dump JSON); concurrent 409 unchanged; stream exception still refuse copy.
error-handling-enforcer: yes — no new empty catch; feedback submit failure stays the existing inline retry; tool summarizer typed fallback is a one-liner not a swallowed blank.
complexity-anti-drift: yes — declared split: burst grouping helper, thinking-fold view, tool-fold view, summarizer dispatch; ThreadPage does not become a single transcript god component.
minimal-change-verifier: yes — one contract; one tracer-bullet commit per task below; no host-graph rewrite.

## Tasks (ordered by dependency)

1. **SSE and persist tool one-liners share args** — tag: `[implementation]`
   - **Inherits:** spec: `format_tool_summary` on the SSE path passes the same args as graph persist; retrieve shows query while running and after; unknown/memory tools are not the bare name when args or result exist; empty `tool_call_id` still a row on GET
   - **Surface:** `src/fin/host/` stream mapping / workspace SSE
   - **Acceptance:** a scripted retrieve `tool_result` frame includes the query in `summary`; GET `tool_summaries` for that id matches; `save_memory` or an unknown name with args is not equal to the bare name; `uv run pytest -q` covers the summarizer cases
   - Status: [ ] pending

2. **Thinking fold and tool fold on the live timeline** — tag: `[implementation]`
   - **Inherits:** spec: arrival-order bursts; live thinking expanded with tail-following full text (not first-line clamp as the live view); burst end auto-collapses thinking to header + three-line tail preview; live tool fold expanded with compact rows; burst end auto-collapses to 工具 + count; comparison/answer/citations stay outside; `<details open={live}>` must not force-close a user-opened completed fold
   - **Surface:** `web/` thread transcript + send/resume store folding
   - **Acceptance:** a scripted sequence thinking → retrieve → thinking → answer renders thinking-fold → tool-fold → thinking-fold → answer; live thinking shows latest lines; after the first tool_call the first thinking fold is collapsed with a tail preview; `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending
   - [blocks: T1]

3. **Last-turn SVG thumbs** — tag: `[implementation]`
   - **Inherits:** spec: stroke SVG up/down; last terminal turn only when the thread is not `in_flight`; persist API unchanged; no emoji; failed submit keeps inline retry copy
   - **Surface:** `web/` thread transcript / existing feedback helpers
   - **Acceptance:** two terminal turns → only the last shows thumbs; `canSubmitFeedback` still gates persist; `aria-pressed` round-trips after a successful submit
   - Status: [ ] pending
   - [parallel]
   - (no blocker; can land beside T2)

## 待写入

empty — flushed with the spec (CONTEXT **thinking fold** / **tool fold** / **工作台**).

## Code review phase

After all bullets: `arthurpower:code-review` then `arthurpower:verification-before-completion` on the branch.
