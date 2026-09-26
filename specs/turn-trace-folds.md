# Spec: turn trace folds

## Assumption gate (confirmed 2026-09-07 in-thread; collapsed thinking reversed 2026-09-09)

Operator waived a fresh per-item grill (`/spec-driven-development` after the fold discussion). These are in force (item 2 and the old “preview line count is 3” bullet reversed 2026-09-09; LogicSync 1A/2A):

1. Contract is the **工作台** **research thread** transcript only. No new routes, SSE event _types_, or host-graph topology.
2. Live thinking is expanded (full burst text, viewport follows the tail). When that burst ends, the fold auto-collapses. Collapsed = header-only (burst body hidden until expand). No three-line tail preview.
3. Tools stay visible for operator debug: a sibling **tool fold**, not hidden inside thinking prose and not a default-expanded ledger of raw JSON.
4. Retrieve through the **retrieve port** (including an MCP adapter) is still the `retrieve` tool on this surface — not a separate MCP chrome name.
5. Feedback chrome: stroke SVG up/down on the last terminal turn when the thread is idle; no emoji; no 「有帮助」/「没帮助」 word buttons. Persist API stays the existing per-`run_id` thumbs route.
6. Soft Brutalism tokens and `web/` + FastAPI stack stay. Default `uv run pytest -q` and `web/` `tsc --noEmit` stay the gates.
7. **thinking pending** uses the same 深度思考 chrome (no body) from optimistic send until the first `thinking` frame; a first `tool_call` or `answer` drops it. No new SSE types.

→ These are in force.

## Objective

The operator watching a **research thread** can follow the run without a frozen or denormalized transcript: each thinking burst and each tool burst is its own fold on a time-ordered timeline; live bursts stream; finished bursts collapse; collapsed thinking is header-only. After send, **thinking pending** occupies that same chrome until the first real frame. Tool one-liners on the live wire match GET history (including query/args). Success is: the SPA no longer concatenates all thinking above all tools, no longer clamps the _start_ of thinking as the live view, and no longer drops tool args from SSE summaries.

Quoted from `docs/CONTEXT.md`:

- **fin**: An enterprise **knowledge-QA** agent (the 入口, not the 库): the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held filings behind the **retrieve port**.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history.
- **工作台**: The local web UI: chrome is **知识库** / **监控**; **research thread**s live in a 会话 list, not a 对话 nav; create, list, open, resume, and delete a **research thread**; **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **thinking fold**: A per-burst collapsible thinking region on a **research thread** turn: streams expanded while that burst is live; auto-collapses when the burst ends; collapsed is header-only (UI label 深度思考, plus duration when measured) with the burst text hidden until expand.
- **thinking pending**: The same **thinking fold** chrome on an optimistic **research thread** turn after send, before the first `thinking` SSE frame, with no burst body. The first `thinking` frame becomes that fold; a first `tool_call` or `answer` removes it.
- **tool fold**: A per-burst collapsible tool-call list on the same turn, sibling to the **thinking fold**. Live while those calls run (operator debug, including retrieve); auto-collapses when that burst ends. UI label: 工具.

## Boundaries

- **Does:**
  - Timeline on the assistant side of a turn, between the user bubble and the answer card: zero or more **thinking fold**s and **tool fold**s in arrival order (think → tools → think → tools). Consecutive `thinking` frames append to the current thinking burst; a `tool_call` closes that thinking burst (auto-collapse). Consecutive `tool_call` / `tool_result` frames belong to the current tool burst; the next `thinking` or first `answer` closes that tool burst (auto-collapse).
  - **thinking pending:** after send and before the first SSE `thinking` frame, the optimistic turn shows the same 深度思考 chrome with no burst body. The first `thinking` frame becomes that fold (same region, not a second card). A first `tool_call` or `answer` removes pending and attaches the real fold or answer card. Stream error / no-frame drop still removes the optimistic turn. No new SSE event types.
  - Thinking fold live: expanded; full burst text; viewport pinned to the tail. Collapsed (manual or auto at burst end): header only (深度思考, plus duration when the run has measured `thinking_ms` for a completed burst); burst prose is hidden until expand. Click toggles expand/collapse; a burst the operator expanded stays expanded until they collapse it (auto-collapse must not fight a manual open _during_ the same live burst; auto-collapse _does_ run when the burst ends).
  - Tool fold live: expanded; each call is a compact row (`name` + human one-liner). Running calls use the placeholder until `tool_result`. Collapsed: header (工具 plus a count of calls in that burst). Click expands the list. Default does not dump raw `ToolMessage` JSON; a row may expand once more for that call's summary only.
  - SSE `tool_call` carries `name` / `args` / `tool_call_id` (already on the wire). The live row and the `tool_result` summary both use **args** so retrieve shows the query (and other tools their locators/pages) while running and after. `format_tool_summary` on the SSE path passes the same args the graph persist path already passes. Empty `tool_call_id` still renders a row (stable client key); GET history must not drop those calls.
  - Unknown or memory tools (`save_memory`, `recall_memory`, `edit_user_profile`, any name not in the current summarizer table) render `name` plus one or two arg keys plus done/fail — never the bare name alone, never raw JSON in the fold.
  - Retrieve via MCP adapter: still labeled `retrieve` (or 检索 in copy). Adapter health stays on **监控** / `GET /api/status`.
  - `comparison` tables, citation chips, sources list, and the answer card stay outside both folds.
  - Thumbs: stroke SVG up/down, `aria-pressed`, on the last terminal turn of a thread that is not `in_flight`. Older turns do not show thumbs. Submit still `POST .../runs/{run_id}/feedback`; last write wins; failed submit keeps the existing inline retry copy.
  - Reload/GET restores collapsed thinking text and tool summaries into the same fold timeline (one thinking fold and one tool fold per stored blobs is acceptable when the server does not persist burst boundaries; live grouping is required on the stream).
- **Confirms with human:** none remaining.
- **Out of this spec:**
  - Hiding tools with no fold (debug needs the **tool fold**).
  - New SSE event types; changing citation verifier; host-graph rewrite; Stop / queued follow-up.
  - Training export from thumbs; changing the feedback HTTP contract.
  - Playwright unless an existing opt-in lane already covers the thread page (do not add a lane here).
  - Pixel-perfect restyle of Soft Brutalism; English vs Chinese on StatusLine 「完成」 except fold labels named above.

## Success Criteria

- `uv run pytest -q` exits 0 (no `eval` mark; no network LLM). `web/` `tsc --noEmit` exits 0.
- A scripted stream of thinking, then `tool_call` retrieve, then more thinking, then `answer`: the SPA timeline is thinking-fold → tool-fold → thinking-fold → answer (not all thinking text in one block above all tools).
- While a thinking burst is live and not manually collapsed, the visible thinking text includes the latest lines (tail), not a CSS clamp of the first three lines as the only live view.
- When that thinking burst ends (next `tool_call` or terminal `answer`/`status` with no further thinking), the fold is collapsed to the header only: no burst prose and no three-line tail preview. Expanding shows the full burst.
- After send and before any SSE event, the assistant side shows **thinking pending** (深度思考 chrome, no thinking prose). A first `thinking` delta appears in that same region. A thinking-off stream whose first event is `tool_call` or `answer` does not leave a leftover pending fold.
- While a tool burst is live, the tool fold is expanded and shows `retrieve` with the query from `args` (not `运行中…` without the query, and not `3 hits` with the query missing because SSE omitted args). After `tool_result`, the one-liner matches GET `tool_summaries` for the same `tool_call_id`.
- `format_tool_summary` for `save_memory` / an unknown name is not equal to the bare name when args or a result dict are present.
- Collapsed tool fold header is visible (not `display:none` with no entry). Expanding it shows the call rows. Native `<details open={live}>` must not force-closed a user-opened completed fold on the next render.
- Last terminal turn shows SVG thumbs; a prior terminal turn in the same thread does not. No emoji in that control. `canSubmitFeedback` remains the persist gate.
- GET history after `done` still returns thinking text and tool summaries without `HIDDEN_BOUNCE_MARKER` or raw retrieve JSON (workspace SC unchanged).

## Open Questions

(none — assumption gate cleared all)

## Inherits / Changes

**Inherits:** `specs/workspace.md` (SSE types, run_id, crash resume, Soft Brutalism, fetch SSE); ADR 0025 (stream then collapse — this spec names the burst grain, header-only collapsed thinking, and **thinking pending**); ADR 0009 / 0031 / 0034 tool names; **retrieve port** (ADR 0032 / 0045 MCP adapter behind the port); thumbs persist from `specs/memory-dialogue-platform.md` Focus 5 (per-turn `run_id`, GET round-trip). Capability packages stay `src/fin/host/` + `web/`.

**Changes this spec adds:** burst-scoped **thinking fold** / **tool fold** timeline; header-only collapse on thinking (no three-line tail preview); **thinking pending** on the optimistic turn until the first `thinking` frame (dropped by first `tool_call` / `answer`); SSE summaries pass args; memory/unknown tool one-liners; thumbs chrome (SVG, last idle turn). Supersedes `specs/workspace.md` Does bullets that said thinking is one unframed region with start-of-text line-clamp for the whole run, and that tools are a stack of always-`open={live}` `<details>` under that region.

**待写入:** empty — flushed 2026-09-07: CONTEXT **thinking fold** / **tool fold** / **工作台** sentence. Amended 2026-09-09: header-only collapse + **thinking pending** (CONTEXT / ADR 0025 already flushed). No new ADR (grain refinement of 0025).

## architecture-change-reviewer

Planned implementation surfaces (not this docs commit): `web/` thread transcript, `web/` store SSE folding, `src/fin/host/workspace_http.py` SSE tool_result args, `src/fin/host/stream_map.py` summarizer, host + web tests, `specs/workspace.md` amendment pointer.

bounded-context-guardian: yes — transcript chrome stays the **工作台** surface of **host**; retrieve/MCP stay behind the **retrieve port**; no new top-level technical-layer package.
defensive-contract-validator: yes — empty thinking (no region); empty/missing `tool_call_id` still a row; unknown tool name; overflow many tools in one burst (fold, do not dump JSON); concurrent 409 unchanged; stream exception still refuse copy.
error-handling-enforcer: yes — no new empty catch; feedback submit failure stays the existing inline retry; tool summarizer typed fallback is a one-liner not a swallowed blank.
complexity-anti-drift: yes — declared split: burst grouping helper, thinking-fold view, tool-fold view, summarizer dispatch; ThreadPage does not become a single transcript god component.
minimal-change-verifier: yes — one contract (trace folds + matching summaries + thumbs chrome); tracer-bullet commits in `plans/turn-trace-folds.md`; no host-graph rewrite.

All 5 verdicts yes.
