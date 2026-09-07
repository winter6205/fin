# Spec: workbench chrome (对话 / 知识库 / 监控)

> **Identity:** quote [`docs/CONTEXT.md`](../docs/CONTEXT.md). **fin** is an enterprise knowledge-base Agent. The UI is **工作台**. **知识库** is the one shared corpus. Chrome labels 对话 / 知识库 / 监控 are **工作台** surfaces; 对话 here is not Soft-Track `dialogue`.

## Assumption gate (confirmed 2026-09-07 in-thread)

Operator confirmed IA variant A plus a top-left entry row. These are in force:

1. Left chrome is one row of three entries — **对话**, **知识库**, **监控** — not a stack of Corpus / 关注 / status widgets.
2. Thread list and 新建 stay under that row. No second **工作区**, no second **知识库**, no left-rail 指定/新建 (ADR 0049).
3. **监控** is a separate main-area page: watchlist, notices, retrieve reachability, and **重连**. Model identity is not a footer line; model failures stay on the **research thread** (composer / turn copy).
4. Nav copy never says Corpus, MCP, Status, or 关注. Retrieve backend may still be MCP; the operator-facing name is 检索.
5. `GET /api/status` payload and derivation stay ADR 0033 (`probe_retrieve_reachability` is PATH-only; no `tools/call` health RPC). **重连** re-fetches that route after the operator may have put the MCP command on `PATH`; it does not spawn a new MCP client handshake.
6. Routes: `/` = 对话工作面; `/kb` = 知识库 catalog + ingest; `/monitor` = 监控; `/t/{thread_id}` unchanged. Cold-start last-thread restore still runs on `/` unless this session already chose **知识库** or **监控**.
7. Look remains Soft Brutalism. Tracker for this plan is local markdown only (operator: no GitHub issues).
8. Default `uv run pytest -q` stays green with no live SEC and no live memory server.

→ These are in force for this spec.

## Objective

The operator can tell the **工作台** apart from diagnostics. Daily path is **research thread**s. Catalog and ingest live on **知识库**. Watchlist and retrieve-down live on **监控**, with a reconnect control when retrieve is unreachable. Success is: three named entries, no footer dump of model / MCP / filing count, and an unreachable retrieve is still visible without a blank panel.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: An enterprise knowledge-base Agent: the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held filings behind the **retrieve port**.
- **工作台**: The local web UI: chrome is 对话 / **知识库** / **监控**; create, list, open, resume, and delete a **research thread**; **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn.
- **知识库**: The one process-wide corpus (catalog, ingest tree, **retrieve port**) shared by every **research thread**.
- **监控**: The **工作台** route `/monitor`: issuer watchlist, in-app notices, and retrieve-reachability (with reconnect as a re-read of `GET /api/status`). It is not a second **知识库**.
- **Corpus 首页**: Retired chrome name. The **知识库** catalog and ingest live on **工作台** route `/kb`. Distinct from `/` (对话 empty / last-thread restore) and from `/t/{thread_id}`.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.

## Boundaries

- **Does:**
  - Sidebar top: three entries 对话 / 知识库 / 监控. Active entry matches the current route (`/` and `/t/{id}` count as 对话).
  - Move corpus cards + ingest door from `/` to `/kb`. `/` is the 对话 empty / restore surface (thread summaries on `/` are optional; they must not be the only path to threads — the sidebar list remains).
  - Move watchlist CRUD + notices off the sidebar into `/monitor`.
  - Remove the always-on Sidebar StatusStrip (retrieve / adapter mcp / model / corpus count). Unreachable retrieve: mark on the **监控** entry; `/monitor` shows ordinary-person degraded copy plus **重连** (re-GET `/api/status`, then refresh store). Status route 失败 (malformed / network): same **监控** surface, never a blank panel, never an error page.
  - Session gate: choosing **知识库** or **监控** this session blocks last-thread restore bounce, same role as today's Corpus-home gate.
  - Copy helpers stay unit-testable without a DOM (same split as `statusStrip.ts` / `watchlist.ts`).
  - Persist glossary + ADR 0050; amend ADR 0033 presentation only.

- **Confirms with human:** none remaining (2026-09-07).

- **Out of this spec:**
  - Changing `GET /api/status` fields, adding a MCP `tools/call` health check, spawning retrieve on **重连**, or reconnecting model HTTP.
  - Surfacing `FIN_BASE_URL`, keys, skill lists, or `%` context window.
  - New 工作区 / 知识库, folders, thread search, Stop, HITL approval.
  - Rewriting Soft-Track, citation verifier, ingest doors (ADR 0047), or retrieve ranking.
  - Playwright beyond existing workspace e2e lanes; cassette-futurism.
  - GitHub issue tracker for this plan.

## Success Criteria

Each line is binary. Default `uv run pytest -q` stays green with no live SEC.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0.
2. Product Sidebar chrome labels are exactly 对话, 知识库, 监控 (no `Corpus`, no `MCP`, no `关注` as a nav label). Footer StatusStrip (four-line retrieve / adapter / model / corpus) is absent.
3. `/kb` renders the ingest door and **知识库** cards (empty-corpus ordinary-person copy still applies). `/` does not render that card grid as its primary catalog.
4. `/monitor` is the only product surface for watchlist add/remove and unread notices. Sidebar has no watchlist form.
5. With store `retrieve_reachability: unreachable`, the **监控** entry shows a non-text-only degraded mark and `/monitor` includes a **重连** control. After **重连**, the SPA issues another `GET /api/status` (not a full document reload as the only path).
6. `GET /api/status` still returns the four ADR 0033 fields; unreachable retrieve is still `200` + payload, never HTTP 500 / empty body. `probe_retrieve_reachability` stays PATH-only (no new health RPC).
7. Loading / unavailable / unreachable / reachable-fake / reachable-mcp each produce non-empty 监控 copy (never-blank). Empty watchlist is a valid empty list, not a crash. Double **重连** while in flight does not duplicate error toasts as success. Status fetch throw maps to unavailable copy, not a swallowed empty catch with silent OK.
8. Visiting `/kb` or `/monitor` this session does not last-thread-restore away from that choice; a later `/` visit may still restore when the gate is not set.
9. `docs/CONTEXT.md` defines **监控** and treats **Corpus 首页** as retired chrome; ADR 0050 is accepted; ADR 0033 still owns the status route and records the render move.

## Open Questions

(none — assumption gate cleared 2026-09-07)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; SPA `web/` (React 18 + Vite + Zustand + React Router); FastAPI workspace HTTP; `GET /api/status` four fields (ADR 0033); retrieve adapter MCP / Fake pytest (ADR 0032); watchlist + notices already on the workspace API; last-thread restore + corpus-home session gate; ingest doors (ADR 0047); one **工作区** / one **知识库** (ADR 0049); Soft Brutalism (`specs/workspace.md` look). Prior specs: `specs/workspace.md` (v1 chrome was list/create/delete only — this spec adds named surfaces), `specs/workbench-operator-workspace.md` (Corpus 首页 = `/` is superseded here for routing only).

**Changes this spec adds:** three-entry **工作台** chrome; `/kb` and `/monitor`; StatusStrip leave the sidebar; **重连** = re-GET status; glossary **监控** + retired **Corpus 首页**; ADR 0050; ADR 0033 presentation amendment. Does not change retrieve protocol or model config.

## 待写入 (persist after ACR)

Flushed 2026-09-07: CONTEXT **工作台** / **监控** / **Corpus 首页**; ADR 0050; ADR 0033 presentation amendment. Empty after that flush.

## architecture-change-reviewer

Round 1 (2026-09-07):

bounded-context-guardian: yes — chrome stays in **host** SPA (`web/`); no new bounded context; no retrieve/MCP client in the UI; watchlist stays on existing workspace HTTP.
defensive-contract-validator: yes — SC7 allocates empty watchlist, negative unreachable, overflow N/A for this chrome (status body already rejected unknown enums in `parseWorkspaceStatus`), concurrent double-重连, exception on status fetch.
error-handling-enforcer: yes — status fetch failure is typed unavailable copy; retrieve unreachable is payload not HTTP 500; **重连** does not invent a handshake; no silent OK on probe miss.
complexity-anti-drift: yes — declared split is chrome/nav vs 知识库 page vs 监控 page vs existing copy helpers; no god-sidebar of forms + diagnostics.
minimal-change-verifier: yes — one logical product task (chrome IA), sequenced as tracer bullets with one commit each; no host-graph / retrieve-adapter rewrite.

affects: `web/` workspace SPA, `docs/CONTEXT.md`, `docs/adr/0033-workspace-status-surface.md`, `docs/adr/0050-workbench-chrome.md`, `specs/workbench-chrome.md` (this file), `plans/workbench-chrome.md`
