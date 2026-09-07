# 0050. 工作台 chrome: 对话 / 知识库 / 监控

Date: 2026-09-07
Status: accepted

The **工作台** left chrome is three named entries — 对话, **知识库**, **监控** — not a stack of Corpus, watchlist, and infrastructure widgets. `/` is the **research thread** surface (empty / last-thread restore); `/kb` is the **知识库** catalog and ingest; `/monitor` is watchlist plus retrieve-reachability. `GET /api/status` remains ADR 0033; the operator sees it on **监控**, including **重连** as a re-GET, not as a footer dump of model / MCP / filing count. Spec: `specs/workbench-chrome.md`.

Considered Options: keep StatusStrip + watchlist in the sidebar (rejected — chrome mixed research with diagnostics); hide retrieve health with no entry (rejected — unreachable retrieve would vanish); MCP `tools/call` handshake on 重连 (rejected — ADR 0033 forbids a status-route health RPC).
