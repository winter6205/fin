# Spec: corpus-serve-ingest

> **See also (2026-09-16):** live empty iknow + closed-loop ingest is [`first-closed-loop.md`](./first-closed-loop.md) (does not replace this spec’s two-door contract).

## Assumption gate (confirmed 2026-09-04)

Operator confirmed scope A in-thread. These are in force:

1. Serve-time product must stop using a local extracted tree as company-corpus SSOT; **retrieve** evidence is MCP-returned text; ordinary **read_file** reads the stored object (MCP when that is where it lives).
2. Workspace has **two doors**: drag into the current **research thread** = this-turn read only (no push); a separate **corpus ingest** control = ingest endpoint then MCP push.
3. **切分** runs only on the ingest endpoint (deterministic format table, not the research model). Version close/replace is an MCP call.
4. Default `uv run pytest -q` stays green with Fake retrieve; no live MCP required.
5. Connectors (mail, calendar, people) and multi-user source isolation stay out; they are tracked as GitHub issues, not as this spec's tickets.
6. This spec's plan and spec files use **local markdown only** — no GitHub issue tracker for tracer bullets.

## Objective

Ship the host/workspace contract so **company corpus** is stored and queried on the **retrieve** backend (typically **iknow-memory**), fin only produces bytes at ingest and gates citations on this-run tool text, and operators can attach a file to a turn without accidentally ingesting it.

Success is: a scripted serve path no longer needs a second on-disk cite tree; drag-to-thread does not push; the ingest door does push; pytest default stays green.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition) — after persist these names match the glossary:

- **retrieve port**, **citation verifier**, **read_file**, **切分**, **场景适配颗粒度**, **入库推送**, **iknow-memory**, **research thread** as defined there.

## Boundaries

- **Does:**

  **Focus 1 — Serve evidence from the retrieve backend**
  - Product HTTP/CLI research path uses **retrieve port** hit text as citation evidence (ADR 0046 / 0047).
  - **read_file** is an ordinary read of a known locator; when the object is on MCP, the tool fetches that body (not a parallel extracted tree).
  - **list_files** catalogs ingested/pushed filings, not a leftover HTML tree as SSOT.

  **Focus 2 — Ingest endpoint**
  - One ingest entry: parse + **切分** via the format×section×density table (**场景适配颗粒度**), then **入库推送** / invalidate through MCP.
  - Root **文档类型** is file format (PDF, Word, PPT, Excel, HTML, Markdown, plain text), not EDGAR form names.
  - Unknown format: named failure or documented dumb fallback row — not model-chosen split points.
  - Research-track tools must not expose “chunk any string”.

  **Focus 3 — Two upload doors on workspace**
  - Drag/drop (or equivalent) into the open thread: **thread attach** — model may read this turn; default no MCP push.
  - Separate control: **corpus ingest** — same ingest endpoint as Focus 2.

  **Test allocation (5 classes):** empty (no file, empty retrieve hits); negative (unsupported type, ingest reject); overflow (oversize upload, push batch cap); concurrent (409 concurrent_run unchanged); exception (MCP down typed, ingest named failure, serve without local tree still typed).

- **Confirms with human:** none remaining after gate A.

- **Out of this spec:** mail/calendar/people connectors; multi-user OAuth sources; dream/extract as corpus ingest; session compaction wiring; thumbs UI; observability/rewrite retry; hybrid/RRF inside fin; Redis fair-queue SLA; 切分表具体数值（implementer / existing corpus-retrieval headroom）.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 when web surfaces change.
2. A scripted research turn with Fake or live retrieve can **done** with citations whose spans occur only in this-run retrieve/`read_file` payloads — not by reading a serve-time extracted tree SSOT.
3. With the local extracted tree absent or unused on the serve path, retrieve/`read_file` still return typed misses or MCP/Fake bodies (no silent empty success that implies a local tree).
4. Thread attach of a fixture file makes that text available to the model on that turn and does **not** create a new pushed chunk on the retrieve backend (Fake or assert no ingest call).
5. Corpus-ingest of a fixture file of a supported format results in a push (or Fake ingest record) and a subsequent retrieve can hit that text.
6. Calling 切分 as a research tool is impossible (no such tool on the graph) or is rejected by contract test.
7. Unsupported format on corpus ingest is a named failure, not an empty 200 with silent skip.
8. MCP/ingest failure on corpus ingest is typed; the foreground thread attach path still completes.
9. Citation verifier still refuses unknown locators; memory/advisory text still cannot satisfy the gate (ADR 0035).

## Open Questions

(none)

## Inherits / Changes

**Inherits:** retrieve port ADR 0032; 切分归 fin ADR 0037; citations from this-run evidence ADR 0021 as amended 0046; company corpus via retrieve not dream ADR 0045; workspace ADR 0023–0026; Soft-Track ADR 0027; `specs/corpus-retrieval.md` format table and 父子分块 contract (numerical table values remain headroom); pytest Fake retrieve.

**Changes this spec adds:** serve-time extracted tree is not company SSOT (ADR 0047); two upload doors; ingest endpoint is the only 切分 caller from product UI; version mutate via MCP.

## 待写入 (persist after ACR)

empty — flushed 2026-09-04: CONTEXT **ingest endpoint** / **thread attach** / **corpus ingest** + **read_file** / **list_files**; ADR 0047 accepted.

## architecture-change-reviewer

bounded-context-guardian: yes — ingest and 切分 stay a corpus/host capability; ranking stays behind **retrieve port**; web only offers attach vs ingest; no `controllers/` layer.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–8.
error-handling-enforcer: yes — named ingest/MCP/unsupported-type failures; no silent skip-as-success; attach failure must not look like ingest success.
complexity-anti-drift: yes — three foci (serve evidence, ingest endpoint, two doors); writing-plans must not dump connectors or memory-dream into this plan.
minimal-change-verifier: yes — one logical program, not one commit; writing-plans sequences one wave = one tracer bullet.

**OVERALL: PASS — hand to writing-plans.** Spec path: `specs/corpus-serve-ingest.md`.

Affected surfaces (declared): `src/fin/host/` (retrieve/read wiring, ingest HTTP), `src/fin/corpus/` (endpoint, table), `web/src/` (attach vs ingest), `tests/host/` / `tests/corpus/`.
