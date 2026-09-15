# Spec: first closed loop (ingest + retrieve + cite)

> **Status:** assumption gate = 图 2 G-arrive closed 2026-09-16 + live MCP retrieve probe the same day.
> **Map:** [`docs/wayfinder/first-operator-business-loop-map.md`](../docs/wayfinder/first-operator-business-loop-map.md)
> **Landing list:** [`docs/wayfinder/闭环与分档-落计划-2026-09-16.md`](../docs/wayfinder/闭环与分档-落计划-2026-09-16.md)
> **Sibling spec (iknow-memory):** [`../../iknow-memory/specs/corpus-ready-retrieve.md`](../../iknow-memory/specs/corpus-ready-retrieve.md)
> **Plan:** `plans/first-closed-loop.md`

## Objective

Make the first operator knowledge loop real on an **empty** iknow store: **corpus ingest** (切分 + **入库推送**) so **retrieve port** returns non-empty **chunks** with canonical `source_ref`; the **工作台** shows which **资料集合** is bound; a research turn on that collection can **done** with **citation verifier** citations. Today MCP retrieve is reachable but returns `chunks: []` with no `degraded` and no mapping drops — cite/refuse work cannot proceed until ingest lands in iknow.

**Who:** local operator on the **工作台**, talking to product **知识 MCP**.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **资料**: Operator-facing name for one corpus body held behind the **retrieve port** after ingest.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **ingest endpoint**: The product job that parses, applies **切分** from the format parameter table, and **入库推送** or invalidates via MCP.
- **thread attach**: The operator places a file on the open **research thread** so the model can read it this turn. Default is no **入库推送**.
- **corpus ingest**: The separate workspace control that sends a file through the **ingest endpoint** into the retrieve backend.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run received from **retrieve** / **read_file** (ADR 0046).
- **知识 MCP**: The retrieve MCP (**iknow-memory**) as the operator-visible dependency of the **工作台**.

## Confirmed assumptions (gate)

1. This spec is **阶段一第一闭环** only. C 综述 and D 真数据库 are **plan slots**, not this spec’s implementation. A 查找 may stay unopened if named.
2. Default retrieve **档 B** means: use the live iknow **retrieve** path as it ranks today (graph-priority + fill); fin does not reimplement ranking. Prompt/档分类 beyond default B is out except named “档未开”.
3. **知识存入** is in scope: without push, retrieve stays empty (measured 2026-09-16: three queries, `chunks: []`, mapping_failures=0).
4. **thread attach** default remains no push (`docs/wayfinder/升级收口-2026-09-13.md` §1). Explicit language/path **corpus ingest** is the write door.
5. `GET /api/status` `corpus_filing_count` is the local catalog, **not** iknow chunk count. Product must not treat that number as “库就绪”.
6. Default `uv run pytest -q` stays Fake retrieve; live MCP/ingest is opt-in.
7. Citation hard gate stays the **citation verifier**; prompts do not replace it.
8. 图 1 seam (`knowledge_mcp_fault`, no `/kb`) stays; this spec does not reopen it.
9. Binding display is on the **工作台**; 资料四字段 live on iknow’s **existing** 资料 page (sibling spec).
10. Local markdown tracker only.

## Boundaries

- **Does:**
  - Product **corpus ingest** of a supported fixture through **ingest endpoint** → MCP **入库推送** with ≥1 chunk; subsequent **retrieve port** `retrieve(query)` returns `chunks` length ≥ 1 with parseable `source_ref` (live opt-in check).
  - Distinguish: legal empty retrieve vs mapping-drop vs verifier `unknown_locator` vs 档/库未就绪 **具名失败** (operator-visible, not one refuse).
  - **工作台** shows the bound **资料集合** identity (read-only this spec; switching collections is implementer headroom if the sibling page exposes more than one).
  - Research send against a bound collection that has chunks: can **done** with non-empty citations whose spans occur in this-run retrieve/`read_file` text.
  - Test classes: empty (no ingest / empty chunks); negative (unsupported format, unnamed ingest); overflow (existing push batch cap); concurrent (existing 409 concurrent_run); exception (MCP down already 409 图 1; live retrieve empty vs degraded).

- **Confirms with human:** exact public error **code strings** for 未就绪 / 档未开 (Open Questions). Collection-switch UI if more than one collection exists.

- **Out of this spec:** C/D implementation; independent 数据中台 service; Soft-Track 业务意图桶; Fake as product; web search as cite evidence; query cache; 图 1 chrome; iknow 资料页 layout (sibling spec); default pytest live MCP.

## Success Criteria

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 when web changes.
2. Opt-in live: after **corpus ingest** of a repo fixture, MCP **retrieve port** for a query that should hit that text returns `chunks` with length ≥ 1 and each mapped chunk has canonical `source_ref`. Command is documented in the plan (not required on default pytest).
3. Same live setup **before** ingest: retrieve for the same query is `chunks: []` and **not** `retrieve_unavailable`, matching the 2026-09-16 probe shape unless `degraded` is set.
4. When bound collection reports 0 successful chunks, a research send that would cite must **not** only emit verifier `unknown_locator` with empty citations — a named 未就绪 (or equivalent documented code) is present on the run/error surface. **Scope: the single-issuer `retrieve` path** (阶段一 default). A `research_issuers` fan-out branch that retrieves nothing does not record a run-scoped outcome, so such a run keeps the verifier's verdict; the limitation and its reproduction are recorded in `plans/first-closed-loop.md` T3.
5. When retrieve returns hits but mapping ledger count > 0 and mapped `chunks` is empty, the run surfaces mapping failure, not silent miss. **Same single-issuer scope as SC4**; fan-out drops stay auditable on the shared mapping ledger but are not re-labelled on the run. **Concurrency requirement:** the count is attributed per retrieve call, never read as a process-wide delta — two runs in one process share the ledger (the concurrent-run guard is per `thread_id`), so a concurrent run's drop must not be charged to a run whose own collection was simply empty.
6. Thread attach of a fixture does **not** by itself make MCP retrieve hit that file (no stealth push).
7. `GET /api/status` may still report `corpus_filing_count`; product copy/tests must not treat that field as iknow 就绪.
8. Product-config MCP **可达**: research **done** on the ingested fixture includes at least one citation span that is a substring of this-run retrieve or `read_file` text (scripted or live opt-in as plan says).
9. C and D remain unused: if invoked, named 未开/未接, no fake SQL retrieve.

## Open Questions

1. Public JSON/SSE **error code** strings for 资料未就绪 vs locator 映射失败 vs 综述未开 vs 真库未接.
2. If iknow exposes multiple 资料集合, is v1 bind **the process-wide default collection** only?

## Inherits / Changes

**Inherits:** ADR 0032 retrieve MCP; ADR 0037 切分 in fin; ADR 0046/0047 cite from this-run MCP text; ADR 0045 corpus not dream; `specs/corpus-serve-ingest.md` two doors; `specs/workbench-retrieve-product-seam.md` 进对话硬拦; 升级收口 §1 attach default; 落计划 2026-09-16 四条到达.

**Changes:** live empty iknow is a **blocking** product fact; status catalog count ≠ 库就绪; 具名失败 for empty-bound vs mapping vs verifier; 工作台绑定展示; ingest is on the closed-loop critical path.

## 待写入 (persist after ACR)

empty — flushed 2026-09-16: CONTEXT **资料集合** / **检索档** / **具名失败**.

## architecture-change-reviewer

Affected surfaces (not a file recipe): `fin.host` workspace HTTP/graph retrieve, `fin.retrieve` MCP adapter/mapping, `fin.corpus` ingest/push, `web` thread chrome bind line.

bounded-context-guardian: yes — ingest/切分 stay corpus; ranking stays behind **retrieve port**; 资料页 stays iknow; no technical-layer folders.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception.
error-handling-enforcer: yes — empty retrieve, mapping drop, verifier refuse, 档未开 are named EXITs; no Fake fallback.
complexity-anti-drift: yes — no new god retrieve-in-fin; 档 B is iknow’s existing assemble path.
minimal-change-verifier: yes — one task: 阶段一闭环 including ingest; C/D not in the diff.
