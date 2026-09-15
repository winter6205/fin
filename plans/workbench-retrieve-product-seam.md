# Plan: workbench retrieve product seam

**Goal:** Product 工作台 chat requires a reachable knowledge MCP; Fake is test-only; `/kb` and `/monitor` leave the product IA.
**Approach:** First make boot publish 缺/坏/可达 from a real handshake (not PATH-only). Then refuse create/open/send on the server when not 可达. Then point the left corner at that snapshot and delete kb/monitor chrome. Glossary/ADR flush is persist after this file, not an implementer guess.
**Spec link:** `specs/workbench-retrieve-product-seam.md`
**ACR:** all-yes (block below)
**Tracker:** local markdown — operator: no GitHub issues.
**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion (landing grain: operator global commit section)

> Contradicts ADR 0033 PATH-only `probe_retrieve_reachability` and Fake-as-reachable — worth reopening because 图 1 G1 requires `initialize` + `tools/list` and forbids fake→可达.
> Contradicts ADR 0050 / sidebar-rail two-entry **知识库** / **监控** chrome — worth reopening because 图 1 G2/G3 hard-remove those routes.

## ACR

bounded-context-guardian: yes — seam stays in **host** (`src/fin/host` + SPA `web/`); retrieve ranking and iknow dashboard stay out of this repo; no new bounded context.
defensive-contract-validator: yes — missing MCP (缺), handshake fail (坏), Fake-not-可达, unknown thread still 404, concurrent in-flight 409 `concurrent_run` unchanged beside `knowledge_mcp_fault`, probe exception publishes 坏 not empty 200-as-OK.
error-handling-enforcer: yes — typed 409 `knowledge_mcp_fault`; `GET /api/status` stays 200 with payload; no empty catch; non-可达 is hard refuse not soft empty retrieve.
complexity-anti-drift: yes — probe / publish / conversation gate / left-corner render stay separate layers; no god-boot that also deletes chrome.
minimal-change-verifier: yes — one task (图 1 product seam); 图 2 ingest shape and iknow cache stay out.

affects: `src/fin/host/` status+thread mouths, SPA chrome/routes, `tests/host/` + `web/` status helpers, CONTEXT + ADR 0033/0050/0051

## 待写入

Flushed 2026-09-13 (`docs/CONTEXT.md` **工作台** / **知识库** / **监控** / **Corpus 首页** / **资料** / **知识 MCP** / **fin** / **iknow-memory** / **retrieve adapter**; ADR 0033 + 0050 amendments; ADR 0051). Empty.

## Tasks (ordered by dependency)

1. **Boot publishes 缺 / 坏 / 可达 from handshake** — tag: `[implementation]`
   - **Inherits:** spec SC3/SC4/SC5/SC9: `fin-serve` still serves `GET /api/status` 200 when MCP is 缺 or 坏; product probe is `initialize` + `tools/list` (short retry); PATH-only presence must not publish 可达; `FIN_RETRIEVE_ADAPTER=fake` must not publish MCP 可达; Fake 「演示搜索」/「可用」 is not product copy
   - **Surface:** **host** workspace HTTP / retrieve adapter boot (`src/fin/host`, existing `GET /api/status`)
   - **Acceptance:** Product-config tests: no MCP command → 缺; command on PATH that fails handshake → 坏; fixture that completes `initialize` + `tools/list` → 可达; Fake flag → not 可达; status route stays 200 JSON. `uv run pytest -q` exits 0
   - Status: [x] done

2. **Create / open / send 409 when not 可达** — tag: `[implementation]`
   - **Inherits:** spec SC2/SC4/SC5: `POST /api/threads`, `GET /api/threads/{id}`, `POST /api/threads/{id}/messages` return 409 `{"error": "knowledge_mcp_fault", "message": "知识 MCP 故障"}` when published state is 缺 or 坏; `GET /api/threads` list stays 200; missing id stays 404 `thread_not_found`; Fake / scripted must not return `knowledge_mcp_fault`; existing 409 `concurrent_run` unchanged
   - **Surface:** **host** thread mouths (`src/fin/host` workspace HTTP)
   - **Acceptance:** The three mouths refuse only on non-可达 product MCP; list still works; Fake boots still create/open/send without this error. `uv run pytest -q` exits 0
   - Status: [x] done
   - [blocks: T1]

3. **Left-corner MCP line; `/kb` and `/monitor` gone** — tag: `[implementation]`
   - **Inherits:** spec SC6/SC7/SC8: left-corner helper reads the same snapshot as `GET /api/status`; 缺 and 坏 copy is exactly `知识 MCP 故障`; 可达 omits that string; no product Route `/kb` or `/monitor` (those paths 404, not Navigate to `/`); sidebar has no 知识库 / 监控 labels; `App.tsx` does not import KnowledgeBasePage / MonitorPage; icon/expand copy not pinned
   - **Surface:** **host** SPA (`web/` chrome, routes, status helper)
   - **Acceptance:** Helper is unit-testable without DOM; deep `/kb` and `/monitor` are 404; product chrome has no those nav words; `web/` `tsc --noEmit` exits 0 and `uv run pytest -q` exits 0
   - Status: [x] done
   - [blocks: T1]
   - [parallel] with T2 after T1

## Code review phase

After all bullets land: `code-review`. `GATE: BLOCKED` → next slot `review-report-repair`.
