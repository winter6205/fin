# Plan: workbench sidebar rail

**Goal:** Sidebar chrome is 知识库 + 监控 (SVG icons, no emoji, no 对话总览); chats live in a foldable 会话 list under a 1px rule.
**Approach:** Docs for the two-entry rail are already flushed on this branch (CONTEXT **工作台**, ADR 0050 amendment). Implementation: rail first (icons + brand + divider), then kill the 对话 overview and fold 会话, then replace the right-side pebble with label/icon warning. Base: merged PR #246. Uncommitted copy on the operator's `main` working tree (知识库搜索 wording) is optional to cherry-pick; this plan does not depend on it.
**Spec link:** `specs/workbench-sidebar-rail.md`
**ACR:** all-yes (block below)
**Tracker:** local markdown fallback — operator: no GitHub issues.
**Per-ticket loop (all bullets):** tdd → typecheck+tests → verification-before-completion → one commit on `feat/workbench-sidebar-rail`. End-of-round code-review after all bullets.

> Contradicts ADR 0050 three-entry chrome — amended 2026-09-07 on this branch (对话 nav retired). Worth reopening because the 对话 row _is_ the overview the operator rejected.

## ACR

bounded-context-guardian: yes — chrome stays in **host** SPA (`web/`); no new context; no retrieve client in the UI.
defensive-contract-validator: yes — empty 会话 list, negative unreachable-without-dot, overflow N/A (list already scrolls), concurrent double-toggle of disclosure is local UI state, exception status fetch still on `/monitor`.
error-handling-enforcer: yes — retrieve-down still typed on `/monitor`; sidebar warning is copy+icon not a silent pebble; no empty catch.
complexity-anti-drift: yes — rail vs disclosure vs existing pages; no god-sidebar of forms.
minimal-change-verifier: yes — one chrome-rail task, sequenced as tracer bullets; no host-graph rewrite.

affects: `web/` sidebar, nav helpers, HomePage `/`

## 待写入

Flushed 2026-09-07 with the spec (CONTEXT **工作台** / **Corpus 首页**; ADR 0050 amendment). Empty.

## Tasks (ordered by dependency)

1. **Two-entry rail: SVG icons, brand, divider** — tag: `[implementation]`
   - **Inherits:** spec: chrome labels exactly 知识库 / 监控; 16px stroke SVG `currentColor`; no emoji; no new icon npm package; subtitle 企业知识库助手; 1px black rule under chrome; Soft Brutalism tokens; `navChrome` tests pin labels
   - **Surface:** **host** SPA (`web/` sidebar + nav helpers)
   - **Acceptance:** Sidebar shows those two labels with non-emoji icons; subtitle is 企业知识库助手; a 1px black separator sits below that chrome; no 对话 chrome label; `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending

2. **会话 disclosure; `/` is not a 对话总览** — tag: `[implementation]`
   - **Inherits:** spec: 会话 is a collapsible list (button + `aria-expanded`); 新建 lives in that group; `/` has no thread-card wall; last-thread restore on `/` unless kb/monitor gate; collapsed 会话 still shows a count
   - **Surface:** **host** SPA (sidebar list + HomePage)
   - **Acceptance:** 会话 folds and unfolds from the keyboard; `/` does not render the thread-card overview; a restorable last thread still restores when no gate; empty 会话 list is a visible empty state under the disclosure, not a blank rail
   - Status: [ ] pending
   - [blocks: T1]

3. **监控警告 without a right-side pebble** — tag: `[implementation]`
   - **Inherits:** spec: unreachable retrieve must not use `h-2 w-2 rounded-full` (or equivalent pebble) on the right; icon plus label text carry 「搜索断开」; `aria-label` names the failure; color is not the sole cue; `/monitor` 重连 unchanged
   - **Surface:** **host** SPA (nav mark helper + sidebar + existing status panel)
   - **Acceptance:** unreachable paints 监控 · 搜索断开 (or equivalent) with a changed icon, no right-side dot; `uv run pytest -q` exits 0 and `web/` `tsc --noEmit` exits 0
   - Status: [ ] pending
   - [blocks: T1]
