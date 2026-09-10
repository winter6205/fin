# Plan: operator-console-host-contract

**Goal:** Fin ingest pushes one envelope to **iknow-memory**; **read_file** on ingested locators calls MCP `read_source`; local paths / **thread attach** stay local; prompt map and hard gates stay in code.
**Approach:** Change the Fake-MCP push shape first (no live server). Then dispatch inside **read_file**. Then prompt map + citation gate. Opt-in live MCP last. Do not rebuild PDF/office ingest. Dashboard/schema stay in the memory repo.
**Spec link:** `specs/operator-console-host-contract.md`
**Pair plan:** iknow-memory `plans/operator-console.md` (T3 envelope / T4 `read_source` must exist before a live MCP run; Fake tests do not wait)
**Tracker:** **local markdown fallback** — operator: 本程序不开 GitHub issue tracker；票以本文件 Tasks 编号为准。
**Map (memory repo):** 操作员完整控制台：同一权威源上的检视、喂数与图投影
**ACR:** all-yes (copied from spec · 2026-09-10)
**Per-ticket loop (all bullets):** tdd → typecheck+tests (`uv run pytest -q`) → verification-before-completion → one commit on the ticket branch
**End-of-round:** 全部子弹落地后对整轮 diff 做一次 code-review（Standards + Spec）
**待写入:** empty — this repo’s glossary persist list was empty at spec time.

## ACR

```
bounded-context-guardian: yes — fin stays injector/host; storage stays iknow-memory; no second SoR tree
defensive-contract-validator: yes — empty hits, unknown_locator, thread-attach vs ingested, overflow left to existing ingest caps, Fake concurrent 409 inherited
error-handling-enforcer: yes — unknown_locator and retrieve_unavailable stay typed; no silent local-tree fallback for ingested locators
complexity-anti-drift: yes — dispatch in read_file + push mapper; prompt map is not a second retrieve implementation
minimal-change-verifier: yes — this repo only the host contract; dashboard/schema commits stay in iknow-memory
```

## Harvest（已决 vs 开放）

**已决 / inherits：**

- ADR 0032 retrieve adapter; ADR 0037 **切分**; ADR 0046/0047 cite-from-this-run-tools; `specs/corpus-serve-ingest.md` two doors.
- One envelope after **切分**: `source_ref` + 资料 body + `chunks[]` `{ content, index }`.
- **read_file** same tool name; dispatch in code: ingested locator → `read_source`; local / **thread attach** → local read.
- **retrieve port** stays `{ chunks[] }` + `source_ref`; not a full-body dump.
- Default `uv run pytest -q` does not require a live MCP server.
- Version close/replace remains MCP **invalidate** / replace on the memory **资料** key.

**开放（implementer headroom）：**

- How “ingested locator” is recognized (existing corpus index vs locator table).
- Exact Fake MCP script shape as long as SC payload fields are asserted.
- Which existing verifier extension fails a research-track claim with no MCP **retrieve** / `read_source`.

## Tasks (ordered by dependency)

1. **入库推送 uses the one envelope** — tag: `[implementation]`
   - **Inherits:** spec: after **切分**, **入库推送** with `source_ref` (locator string) + 资料 body (extracted unit text) + `chunks[]` `{ content, index }`; **thread attach** still does not push.
   - **Surface:** existing ingest push mapper (corpus ingest → MCP)
   - **Acceptance:** Fake MCP ingest payload includes `source_ref`, a non-empty 资料 body, and `chunks` with monotonic `index` starting at 0; slice texts are contiguous substrings of the body. `uv run pytest -q` exits 0.
   - Status: [x] done — commit e390fdf (PushEnvelope wire per unit; substring/monotonic-index validation; FakePushPort envelope storage)

2. **`read_file` dispatches ingested vs local** — tag: `[implementation]`
   - **Inherits:** spec: ingested locator → MCP `read_source`; local path / **thread attach** → existing local read; unknown locator still `{ error: "unknown_locator" }`; no silent local-tree fallback for ingested locators.
   - **Surface:** existing **read_file** tool wrapper
   - **Acceptance:** **read_file** on an ingested locator issues `read_source` (not a local tree read); **thread attach** / local path does not call `read_source`; unknown locator is `unknown_locator`. `uv run pytest -q` exits 0.
   - Status: [x] done — commit bd2e650 (MCPRetrieveAdapter.read -> per-unit read_source; unknown_locator misses; no local fallback; attach pin)
   - [blocks: T1]

3. **[parallel] Prompt map + hard gate for ingested claims** — tag: `[implementation]`
   - **Inherits:** spec: system is a tool map; 查已入库 → MCP **retrieve**; 读入库整篇 → `read_source`; 本地路径 → local read; research claims about ingested filings require MCP **retrieve** or `read_source` on the trace (`docs/guides/prompt-development.md`).
   - **Surface:** existing prompt / citation-verifier assembly
   - **Acceptance:** research-track fixture that asserts a numeric/filing claim without MCP **retrieve** or `read_source` fails the hard gate (or the existing verifier extension). `uv run pytest -q` exits 0.
   - Status: [x] done — commit eaa9142 (USAGE_RESEARCH tool map wording; fixture proves ungrounded numeric/filing claim refuses at citation_repair_cap; no gate-code change needed)
   - [parallel]
   - [blocks: T2]

4. **[parallel] Opt-in live MCP `read_source`** — tag: `[implementation]`
   - **Inherits:** spec: live transport stays opt-in (`mcp` mark or existing equivalent); not collected by default pytest.
   - **Surface:** existing opt-in MCP test surface
   - **Acceptance:** marked test may cover initialize + `tools/call` `read_source`; default `uv run pytest -q` still exits 0 without a live server. Live run expects memory-repo T4 (`read_source`) available on that server.
   - Status: [x] done — commit f687f84 (mcp-marked live read_source: tools/list presence, by-ref/by-id wire shape byte-identical body, named not-found; live_iknow root= worktree spawn)
   - [parallel]
   - [blocks: T2]
