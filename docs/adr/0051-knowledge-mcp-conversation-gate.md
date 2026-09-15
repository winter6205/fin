# 0051. 知识 MCP: bind without MCP; hard-gate conversation entry

Date: 2026-09-13
Status: accepted

Product retrieve is **知识 MCP** (**iknow-memory**). `fin-serve` may bind when MCP is 缺 or 坏; boot probe-and-publish is `initialize` + `tools/list` into one snapshot shared by `GET /api/status` and the **工作台** left corner (ADR 0033 amendment). Non-可达, the server refuses create thread, open thread, and send (409 `knowledge_mcp_fault`, copy 知识 MCP 故障). Fake / scripted stay test flags and must not publish 可达. Soft 「能聊不能搜」 and PATH-only-as-reachable are rejected. Spec: `specs/workbench-retrieve-product-seam.md`.

Considered Options: refuse to bind `fin-serve` when MCP is missing (hides the 工作台 and blocks local UI work); SPA-only disable of composer (not a server gate); Fake as a product-healthy retrieve mode.
