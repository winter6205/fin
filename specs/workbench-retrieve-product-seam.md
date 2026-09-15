# Spec: workbench retrieve product seam

> **Status:** assumption gate confirmed in-thread 2026-09-13 (10 items).
> **Map:** [`docs/wayfinder/workbench-retrieve-product-seam-map.md`](../docs/wayfinder/workbench-retrieve-product-seam-map.md) (图 1；grilling G1/G2/G3/R1 closed).
> **Sequence:** this spec → `architecture-change-reviewer` (all-yes 2026-09-13) → persist → `writing-plans`. Plan: `plans/workbench-retrieve-product-seam.md`.

## Objective

Product runtime has one retrieve path: **工作台** session plus **retrieve port** must talk to **iknow-memory** (MCP). Fake / Scripted / frozen 12-filing slices stay in tests. Corpus directory and upload live outside fin chrome. The operator cannot see “fake retrieve is healthy”; missing or unhealthy MCP is not a product chat mode.

**Who:** local operator on the **工作台**.

**Success looks like:** `fin-serve` still binds without MCP; create-thread / open-thread / send are server-refused until MCP is reachable; left corner shows the same MCP state in the same words; `/kb` and `/monitor` are gone from product navigation and deep links 404.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: An enterprise knowledge-base Agent: the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held filings behind the **retrieve port**.
- **工作台**: The local web UI: chrome is **知识库** / **监控**; **research thread**s live in a 会话 list, not a 对话 nav; create, list, open, resume, and delete a **research thread**; **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **retrieve adapter**: The implementation behind the retrieve port. Default pytest uses a Fake port. When configured, product retrieve is an MCP 2.0 client (protocol-testable against a compliant public or fixture server); hits map to fin locators as `source_ref` and their returned text is serve-time citation evidence (ADR 0046).
- **iknow-memory**: A separate TypeScript MCP server (own repo). After ingest/push, product **company corpus** is queried through fin's **retrieve port** against this backend (ADR 0045); a distinct **memory port** channel may hold explicit pins and must not share one config key.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.

Chrome sentences for **工作台** / **知识库** / **监控** / **Corpus 首页** above are the _current_ glossary. This spec’s product chrome **contradicts** those sentences; persist (after ACR) amends them. Do not treat the quoted chrome as the target IA.

## Confirmed assumptions (gate)

1. This spec is 图 1 only: boot probe-and-publish, conversation-entry hard gate, product default no Fake, hard-remove `/kb` and `/monitor`, left-corner MCP three-state with copy 「知识 MCP 故障」. 图 2 (in-thread ingest / **切分** product shape) and iknow query cache are out.
2. Path: `specs/workbench-retrieve-product-seam.md`. Default check: `uv run pytest -q` (and existing `web/` `tsc --noEmit`); no live iknow, no live SEC. pytest / scripted **keep Fake**.
3. Process may bind without MCP. Hard gate is server-side on create thread / open existing thread / send message.
4. Boot probe **supersedes ADR 0033 PATH-only** on the product path: `initialize` + `tools/list`, short retry, one status source shared by `GET /api/status` and the left corner. Map: **缺 / 坏 / 可达**. Fake must not be published as reachable MCP.
5. Non-reachable: those three HTTP mouths return **409** + stable `error` code; operator-visible body copy matches left corner: 「知识 MCP 故障」. SPA-only blocking does not count.
6. Deep links `/kb` and `/monitor` are **404** (no 302 to dashboard, no silent `/`). Sidebar has no 「知识库」 or 「监控」 items. Watchlist / 重连 / Fake 「演示搜索」 copy leave the product surface.
7. Left corner reports MCP three-state only (not dashboard HTTP, not model, not corpus count). Icon / expand copy are **not** pinned; three-state semantics still tested.
8. Fake only via existing explicit flags (`FIN_RETRIEVE_ADAPTER=fake`, `FIN_SCRIPTED_MODEL`). Product default must not point at Fake.
9. No change to retrieve envelope, **切分** ownership, citation verifier, **memory port**, ingest push shape (`specs/operator-console-host-contract.md`). Dashboard/中台 upload stays the tracked change. fin does not add a discovery door to those UIs.
10. After ACR, persist amends CONTEXT **工作台** / **知识库** / **监控** / **Corpus 首页**, ADR 0050, ADR 0033 probe/render; add glossary 资料 / fin=入口 / iknow=库 if still missing.

## Boundaries

- **Does:**
  - Product `fin-serve` boot: probe MCP (`initialize` + `tools/list`, short retry), publish **缺 / 坏 / 可达** into one in-process status snapshot; **do not** exit the process solely because MCP is missing or unhealthy.
  - `GET /api/status` remains HTTP 200 with a typed JSON body. Product MCP state is a three-value field consumed by the gate and the left corner. `retrieve_adapter_kind: fake` must not map to MCP **可达**. Existing Fake 「reachable / 演示搜索」 copy is removed from product UI.
  - Server gate when published state is not **可达**: `POST /api/threads`, `GET /api/threads/{id}`, `POST /api/threads/{id}/messages` return 409 `application/json` `{"error": "knowledge_mcp_fault", "message": "知识 MCP 故障"}`. `GET /api/threads` (list) stays 200. Unknown `thread_id` on GET remains 404 `thread_not_found` (do not replace 404 with this 409).
  - **坏**: short retry then re-judge; still unhealthy → same 409. **缺**: gate until state becomes **可达**.
  - SPA: left corner reads the same status snapshot; non-可达 copy is exactly 「知识 MCP 故障」. Product chrome: remove nav 「知识库」「监控」, routes `/kb` `/monitor`, `KnowledgeBasePage` / `MonitorPage` / freeze corpus cards / fin upload shell from the product path, `fin.kb` session gate. Deep `/kb` and `/monitor` → 404.
  - Explicit Fake / scripted: conversation mouths **do not** 409 for `knowledge_mcp_fault`; default `uv run pytest -q` stays hermetic.

- **Confirms with human:** (none — gate cleared 2026-09-13)

- **Out of this spec:**
  - 图 2: in-thread attach / path ingest / **切分** product shape.
  - iknow-memory query cache (sequence ④, other repo).
  - Dashboard / 企业数据中台 page UX; fin discovery links to those UIs.
  - Left-corner icon, tooltip, or expand copy beyond the three-state + 「知识 MCP 故障」.
  - Retrieve ranking, citation verifier, Soft-Track, **memory port**, dream, live-MCP default CI, live SEC.
  - Deleting unused watchlist HTTP if no product route remains (may stay unused this spec).

## Success Criteria

Each line is binary. Default pytest stays Fake / no live MCP server.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0.
2. Product-config test (adapter `mcp`, probe result **缺** or **坏**): `POST /api/threads`, `GET /api/threads/{id}` for an existing id, and `POST /api/threads/{id}/messages` each return 409 with `error=knowledge_mcp_fault` and `message=知识 MCP 故障`. `GET /api/threads` returns 200. `GET /api/threads/does-not-exist` still 404 `thread_not_found`.
3. Same product-config **缺/坏**: `fin-serve` (or the FastAPI app used in tests) still serves `GET /api/status` as 200 (process not required to exit on missing MCP).
4. Product-config test with a fixture MCP that completes `initialize` + `tools/list`: published state is **可达**; the three mouths in (2) succeed with their current non-409 contracts (create 200, get 200, send stream/200 as today).
5. `FIN_RETRIEVE_ADAPTER=fake`: `GET /api/status` must not report MCP **可达**; product copy must not include 「可用」/「演示搜索」 for fake retrieve; the three mouths in (2) must **not** return `knowledge_mcp_fault`.
6. `GET /api/status` 200 body is the same snapshot the left-corner helper reads; for **缺** and **坏** the helper’s operator string is exactly `知识 MCP 故障`; for **可达** that string is absent. Helper is unit-testable without DOM.
7. SPA product routes: no Route `/kb` or `/monitor`; requesting those paths yields 404 (React Router no-match or equivalent), not `<Navigate to="/">`, not an iknow URL. Sidebar labels do not include `知识库` or `监控`.
8. `rg` on `web/src` product chrome: no `reachable-fake` / 「演示搜索」 health copy; no `KnowledgeBasePage` / `MonitorPage` imported from `App.tsx`.
9. ADR 0033 PATH-only probe is **not** the product derivation after this spec: a binary on `PATH` that does not pass `initialize` + `tools/list` must publish **坏** (or **缺** if the command is absent), never **可达**.
10. Persist after ACR (not this file’s job): CONTEXT chrome sentences and ADR 0050 / 0033 match the IA and probe in this spec.

## Open Questions

(none — assumption gate cleared all)

## Inherits / Changes

**Inherits**

- Python 3.12 + uv + pytest; SPA `web/` (React + Vite + Zustand + React Router); FastAPI workspace HTTP (`src/fin/host/workspace_http.py`).
- Test command: `uv run pytest -q` (Fake **retrieve adapter**, `FIN_SCRIPTED_MODEL` e2e). Opt-in `mcp` mark stays opt-in.
- **retrieve port** / MCP client (ADR 0032); ingest push envelope (`specs/operator-console-host-contract.md`); two ingest doors (ADR 0047) unchanged as protocol.
- Existing 409 shape for `concurrent_run` (`{"error": "concurrent_run"}`) — this spec adds a second 409 `error` token, does not reuse `concurrent_run`.
- Thread API names: `POST /api/threads`, `GET /api/threads`, `GET /api/threads/{id}`, `POST /api/threads/{id}/messages`.
- Map decisions G1/G2/G3/R1 (2026-09-13). Prior chrome specs `specs/workbench-chrome.md` and `specs/workbench-sidebar-rail.md` are **superseded for IA and status presentation** by this spec; they remain historical.

**Changes**

- Boot: probe-and-publish (`initialize` + `tools/list`) instead of ADR 0033 PATH-only `probe_retrieve_reachability` as product truth.
- Conversation entry hard-fail when MCP is not **可达**.
- Remove `/kb` and `/monitor` from product IA; MCP health on left corner only.
- Fake is not a product-healthy retrieve story.

**待写入**

Flushed 2026-09-13 with the plan (`docs/CONTEXT.md`; ADR 0033/0050 amendments; ADR 0051). Empty.

**Planned files (for ACR, not a plan)**

- `src/fin/host/` boot + `GET /api/status` + thread create/get/send gate
- `web/src/` App routes, Sidebar, left-corner status helper, remove kb/monitor pages from product graph
- `tests/host/test_workspace_http.py` (and focused new tests) + `web/src` unit tests
- CONTEXT + ADR 0033/0050 on persist, not in the implementation PR mixed with “drive-by” glossary if persist runs first

## architecture-change-reviewer

bounded-context-guardian: yes — seam stays in **host** (`src/fin/host` + SPA `web/`); retrieve ranking and iknow dashboard stay out of this repo; no new bounded context.
defensive-contract-validator: yes — missing MCP (缺), handshake fail (坏), Fake-not-可达, unknown thread still 404, concurrent in-flight 409 `concurrent_run` unchanged beside `knowledge_mcp_fault`, probe exception publishes 坏 not empty 200-as-OK.
error-handling-enforcer: yes — typed 409 `knowledge_mcp_fault`; `GET /api/status` stays 200 with payload; no empty catch; non-可达 is hard refuse not soft empty retrieve.
complexity-anti-drift: yes — probe / publish / conversation gate / left-corner render stay separate layers; no god-boot that also deletes chrome.
minimal-change-verifier: yes — one task (图 1 product seam); 图 2 ingest shape and iknow cache stay out.

affects: `src/fin/host/` status+thread mouths, SPA chrome/routes, `tests/host/` + `web/` status helpers, CONTEXT + ADR 0033/0050/0051
