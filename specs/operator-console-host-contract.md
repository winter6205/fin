# Spec: operator-console-host-contract

> **Status:** assumption gate confirmed in-thread 2026-09-10. Pair spec: iknow-memory worktree `specs/operator-console.md`.
> **Map (memory repo):** [操作员完整控制台：同一权威源上的检视、喂数与图投影](https://github.com/oocang/iknow-memory/issues/201)

## Objective

Change fin’s **injector** and read tools so company-corpus text after ingest lives on **iknow-memory**: push one envelope (**资料** body + fin-cut **chunk**s), **read_file** for ingested locators calls MCP `read_source`, local paths keep the existing local read tool. Prompt map and hard gates follow `docs/guides/prompt-development.md` (tools in system; bodies in tool_result).

**Who:** research-track model + operator ingest.

**Success looks like:** default `uv run pytest -q` stays green with Fake retrieve; ingest endpoint **切分** then MCP push of the new envelope; thread-attach still does not push.

## Glossary

Quoted from this repo `docs/CONTEXT.md` (exact copy, not redefinition):

- **retrieve port**, **read_file**, **切分**, **入库推送**, **iknow-memory**, **locator**, **citation verifier**, **thread attach**

Memory-repo terms this contract uses but does not redefine (see iknow-memory `docs/CONTEXT.md`): **资料**, **chunk**, **read_source**, **ingest**, **retrieve**.

## Confirmed assumptions (gate)

1. Spec path is this worktree `specs/operator-console-host-contract.md`. Implementation commits stay in fin.
2. Default `uv run pytest -q` does not require a live MCP server. Live transport stays opt-in (`mcp` mark or existing equivalent).
3. Fin already converts PDF/office to text on the ingest endpoint. This spec does not rebuild that pipeline; it only changes the MCP push/read shape.
4. **切分** stays fin’s (ADR 0037). iknow-memory must not re-split (memory ADR-0004).
5. Prompt discipline: system is a tool map; **retrieve** / `read_source` / local read bodies stay in tool_result. Hard gates in code, not longer prompts.
6. Out of this spec: fin Agent/XBRL lockstep rewrite; **retrieve** query cache; physical purge on the memory side.

## Boundaries

- **Does:**
  - Ingest endpoint: after **切分**, **入库推送** with one envelope: `source_ref` (locator string) + 资料 body (extracted unit text) + `chunks[]` `{ content, index }`.
  - **read_file**: if locator is ingested on MCP → `read_source`; if only a local path / **thread attach** → existing local read. Same tool name for the model; dispatch in code.
  - **retrieve port** unchanged envelope `{ chunks[] }` + `source_ref`; still not a full-body dump.
  - System prompt map: 查已入库 → MCP **retrieve**; 读入库整篇 → `read_source`; 本地路径 → local read. Research claims about ingested filings require MCP **retrieve** or `read_source` on the trace; local-file claims require the local read with a path.
  - Version close/replace remains MCP **invalidate** / replace on the memory **资料** key (locator / `source_ref`).
- **Confirms with human:** (none)
- **Out of this spec:**
  - iknow-memory dashboard UI; PDF parser rewrite; live-MCP default CI; connectors; multi-tenant sources.

## Success Criteria

- `uv run pytest -q` exits 0 (Fake retrieve / scripted MCP).
- New test: ingest push payload to Fake MCP includes `source_ref`, a non-empty 资料 body, and `chunks` with monotonic `index` starting at 0; slice texts are contiguous substrings of the body (fin freeze unit).
- New test: **read_file** on an ingested locator issues `read_source` (not a local tree read); unknown locator still `{ error: "unknown_locator" }`.
- New test: **read_file** on a **thread attach** / local path does not call `read_source`.
- New test: **retrieve** Fake still maps empty hits to `{ chunks: [] }`; no full 资料 body field on the retrieve envelope.
- New test: research-track fixture that asserts a numeric/filing claim without MCP **retrieve** or `read_source` fails the hard gate (or equivalent existing verifier extension).
- Opt-in `mcp` (or current mark) may cover initialize + `tools/call` `read_source`; not collected by default pytest.

## Open Questions

(none)

## Inherits / Changes

**Inherits**

- ADR 0032 retrieve adapter; ADR 0037 **切分**; ADR 0046/0047 cite-from-this-run-tools; `specs/corpus-serve-ingest.md` two doors (thread attach vs ingest).
- `docs/guides/prompt-development.md` three-layer assembly.
- Test command: `uv run pytest -q`.

**Changes**

- Push shape matches iknow-memory one envelope.
- **read_file** dispatch: MCP `read_source` vs local path.

**待写入:** (none in this repo — memory glossary persist is iknow-memory’s list.)

**Planned files (ACR)**

- ingest push mapper (corpus ingest → MCP)
- `read_file` tool wrapper
- prompt/tool-map + gate tests
- `tests/retrieve/` / `tests/host/` as needed

## architecture-change-reviewer

```
bounded-context-guardian: yes — fin stays injector/host; storage stays iknow-memory; no second SoR tree
defensive-contract-validator: yes — empty hits, unknown_locator, thread-attach vs ingested, overflow left to existing ingest caps, Fake concurrent 409 inherited
error-handling-enforcer: yes — unknown_locator and retrieve_unavailable stay typed; no silent local-tree fallback for ingested locators
complexity-anti-drift: yes — dispatch in read_file + push mapper; prompt map is not a second retrieve implementation
minimal-change-verifier: yes — this repo only the host contract; dashboard/schema commits stay in iknow-memory
```
