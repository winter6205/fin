# Spec: workbench sidebar rail (知识库 / 监控 + 会话)

> Increment on merged PR #246 (`specs/workbench-chrome.md`). **对话 as a chrome entry is retired** — that row was the overview the operator does not want. Soft-Track `dialogue` is unchanged.

## Assumption gate (confirmed 2026-09-07 in-thread)

1. Chrome is **two** entries only: **知识库**, **监控**. There is no 对话 nav item and no `/` 对话总览 (no thread-card wall).
2. Brand subtitle is **企业知识库助手** (not 「向知识库提问」, not 「10-Q 研究工作区」).
3. Icons are 16px stroke SVG, `currentColor`, Soft Brutalism (1px black language). **No emoji.** No new icon-pack dependency unless one is already in `web/package.json` (none is).
4. A 1px black rule separates chrome from a **会话** disclosure: collapsible list of **research thread**s + 新建. Folded state is remembered this session.
5. Unreachable retrieve: **no right-side pebble/dot**. 监控 shows the warning in the **icon plus label text** (e.g. 「监控 · 搜索断开」) so color is not the sole indicator.
6. `/` last-thread-restores when no 知识库/监控 session gate; otherwise empty composer — not a second thread list in the main pane.
7. `GET /api/status` and PATH-only probe stay ADR 0033. No GitHub issues for this plan.
8. `uv run pytest -q` stays green with no live SEC / no live memory server.

→ These are in force.

## Objective

The operator can tell **places** (知识库, 监控) apart from **chats** (会话 list). Success is: two icon+label entries, a divider, a foldable 会话 list, brand line 企业知识库助手, and retrieve-down without a sidebar pebble.

Quoted from `docs/CONTEXT.md` after persist of this increment:

- **工作台**: The local web UI: chrome is **知识库** / **监控**; **research thread**s live in a 会话 list, not a 对话 nav; create, list, open, resume, and delete a **research thread**; **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn.
- **知识库** / **监控** / **research thread**: unchanged meaning; **监控** is still `/monitor`.

## Boundaries

- **Does:**
  - Sidebar: `h1` fin; subtitle 企业知识库助手; nav 知识库 `/kb` + 监控 `/monitor` with SVG icons; 1px black divider; `h2` 会话 as `<button aria-expanded>` disclosure; 新建 + thread links indented under it.
  - Remove NAV 对话 and HomePage thread-card overview. `/` and `/t/{id}` light no chrome entry (or light none — 会话 row is active by list item).
  - Keyboard: nav links Tab; disclosure Enter/Space; existing delete dialog focus trap stays.
  - Tokens only: `page` / `card` / `accent` / `tool` / 1px black. No emoji, no new hex, no `rounded-2xl`.
  - Amend ADR 0050 presentation; CONTEXT **工作台** chrome sentence.

- **Confirms with human:** none remaining.

- **Out of this spec:** MCP handshake on 重连; new icon npm package; changing `/kb` or `/monitor` page bodies except as needed for restore gates; host graph; a second 知识库.

## Success Criteria

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0.
2. Sidebar chrome labels are exactly 知识库, 监控 (no 对话, no Corpus, no emoji in those labels or as the icon).
3. Brand subtitle text is 企业知识库助手.
4. A 1px black separator sits between that chrome and the 会话 disclosure; 会话 can collapse and expand; collapsed still exposes a count so threads are not lost.
5. `/` does not render a thread-card grid as a 对话总览. Opening `/` with a restorable last thread still restores unless a 知识库/监控 gate is set.
6. Unreachable retrieve: 监控 row has no `h-2 w-2 rounded-full` pebble; label or icon includes a non-color cue; `aria-label` names 搜索断开.
7. Contrast: chrome text vs `page` meets 4.5:1; selected `accent`+white is the existing token pair.

## Open Questions

(none)

## Inherits / Changes

**Inherits:** SPA `web/` Soft Brutalism; routes `/kb` `/monitor` `/t/{id}` from `specs/workbench-chrome.md` / PR #246; last-thread restore + kb/monitor session gates; ADR 0033 status route; watchlist on `/monitor`.

**Changes:** 对话 chrome retired; 会话 disclosure; SVG icons; 企业知识库助手; retrieve-down without a right-side dot. ADR 0050 amended (not superseded).

## 待写入 (persist after ACR)

Flushed 2026-09-07: CONTEXT **工作台**; ADR 0050 amendment. Empty after that flush.

## architecture-change-reviewer

Round 1 (2026-09-07):

bounded-context-guardian: yes — chrome stays in **host** SPA (`web/`); no new context; no retrieve client in the UI.
defensive-contract-validator: yes — empty 会话 list, negative unreachable-without-dot, overflow N/A (list already scrolls), concurrent double-toggle of disclosure is local UI state, exception status fetch still on `/monitor`.
error-handling-enforcer: yes — retrieve-down still typed on `/monitor`; sidebar warning is copy+icon not a silent pebble; no empty catch.
complexity-anti-drift: yes — rail vs disclosure vs existing pages; no god-sidebar of forms.
minimal-change-verifier: yes — one chrome-rail task, sequenced as tracer bullets; no host-graph rewrite.

affects: `web/` sidebar + nav helpers + HomePage `/`, `docs/CONTEXT.md`, `docs/adr/0050-workbench-chrome.md`, `specs/workbench-sidebar-rail.md`, `plans/workbench-sidebar-rail.md`
