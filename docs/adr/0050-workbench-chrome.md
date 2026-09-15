# 0050. 工作台 chrome: 对话 / 知识库 / 监控

Date: 2026-09-07
Status: accepted

The **工作台** left chrome is three named entries — 对话, **知识库**, **监控** — not a stack of Corpus, watchlist, and infrastructure widgets. `/` is the **research thread** surface (empty / last-thread restore); `/kb` is the **知识库** catalog and ingest; `/monitor` is watchlist plus retrieve-reachability. `GET /api/status` remains ADR 0033; the operator sees it on **监控**, including **重连** as a re-GET, not as a footer dump of model / MCP / filing count. Spec: `specs/workbench-chrome.md`.

Amendment 2026-09-07 (sidebar rail): chrome is two entries — **知识库** (`/kb`) and **监控** (`/monitor`) — with stroke SVG icons (no emoji) and a 1px black rule above a collapsible 会话 list. The 对话 nav / `/` 对话总览 is retired; chats are the list, not a third chrome sibling. Unreachable retrieve must not use a right-side pebble; the 监控 label/icon carries the warning. Brand subtitle copy is 企业知识库助手. Spec: `specs/workbench-sidebar-rail.md`.

Amendment 2026-09-13 (ADR 0051, `specs/workbench-retrieve-product-seam.md`): product chrome drops **知识库** and **监控** nav and routes `/kb` `/monitor` (deep links 404). **知识 MCP** health is a left-corner line. Catalog/upload are not fin chrome.
