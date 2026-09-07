# Plan: workbench chrome (对话 / 知识库 / 监控)

**Goal:** The **工作台** sidebar is a three-entry chrome; catalog, watchlist, and retrieve-down live on named pages, not a footer dump.
**Approach:** Persist already landed on this branch (CONTEXT + ADR 0050 + ADR 0033 amendment). Implementation moves surfaces one at a time: chrome+知识库 first so `/` is 对话 again, then **监控** for watchlist, then retrieve copy + **重连** and the StatusStrip comes off. `GET /api/status` is unchanged.
**Spec link:** `specs/workbench-chrome.md`
**ACR:** all-yes (block copied below)
**Tracker:** local markdown fallback — operator asked not to open GitHub issues (writing-plans `references/tracker.md` fallback rationale).
**Per-ticket loop (all bullets):** tdd → typecheck+tests → verification-before-completion → one commit on `feat/workbench-chrome`. End-of-round code-review after all bullets, not per WIP commit.

## ACR

bounded-context-guardian: yes — chrome stays in **host** SPA (`web/`); no new bounded context; no retrieve/MCP client in the UI; watchlist stays on existing workspace HTTP.
defensive-contract-validator: yes — SC7 allocates empty watchlist, negative unreachable, overflow N/A for this chrome (status body already rejected unknown enums in `parseWorkspaceStatus`), concurrent double-重连, exception on status fetch.
error-handling-enforcer: yes — status fetch failure is typed unavailable copy; retrieve unreachable is payload not HTTP 500; **重连** does not invent a handshake; no silent OK on probe miss.
complexity-anti-drift: yes — declared split is chrome/nav vs 知识库 page vs 监控 page vs existing copy helpers; no god-sidebar of forms + diagnostics.
minimal-change-verifier: yes — one logical product task (chrome IA), sequenced as tracer bullets with one commit each; no host-graph / retrieve-adapter rewrite.

affects: `web/` workspace SPA (sidebar, routes, home/corpus, watchlist, status copy), tests beside those modules

## Tasks (ordered by dependency)

1. **Chrome row + 知识库 on `/kb`** — tag: `[implementation]`
   - **Inherits:** spec: labels 对话 / 知识库 / 监控; `/` is 对话 (empty / last-thread restore); `/kb` is ingest + cards; `/t/{id}` counts as 对话; session gate on 知识库 blocks restore bounce; no Corpus nav label; Soft Brutalism
   - **Surface:** **host** SPA (`web/` router + sidebar + current home/corpus)
   - **Acceptance:** Sidebar shows those three labels; `/kb` shows the ingest door and **知识库** cards; `/` does not use that card grid as its primary catalog; choosing 知识库 this session does not last-thread-restore away; `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending

2. **Watchlist lives on 监控** — tag: `[implementation]`
   - **Inherits:** spec: `/monitor` is the only product surface for watchlist add/remove and unread notices; sidebar has no watchlist form; empty list is valid
   - **Surface:** **host** SPA (existing watchlist helpers + workspace watchlist API)
   - **Acceptance:** `/monitor` can add and dismiss the same watchlist/notices the sidebar used to; Sidebar has no issuer/form inputs; 监控 is the active chrome on that route
   - Status: [ ] pending
   - [blocks: T1]

3. **Retrieve-down on 监控 + 重连; StatusStrip gone** — tag: `[implementation]`
   - **Inherits:** spec: no footer four-line strip; unreachable → mark on 监控 + **重连** re-GETs `/api/status`; never-blank copy for loading / unavailable / unreachable / fake / mcp-reachable; PATH-only probe; model is not a chrome line; SC7 empty/negative/concurrent/exception
   - **Surface:** **host** SPA (existing `GET /api/status` client + status copy helpers)
   - **Acceptance:** StatusStrip footer absent; unreachable paints a degraded mark on 监控 and a **重连** control on `/monitor` that issues another status GET without requiring a full document reload as the only retry; a thrown status fetch still shows unavailable copy; `uv run pytest -q` exits 0 and `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending
   - [blocks: T2]
