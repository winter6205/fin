# Spec: memory-dialogue-platform

## Assumption gate (confirmed 2026-08-29)

Operator confirmed the numbered gate in-thread (`/spec-driven-development`). These are in force:

1. Workspace is the only product entry; same **host graph**; **citation verifier** stays a code gate; filing retrieve stays behind the **retrieve port** (ADR 0032).
2. **Postgres = only authority store**; **Redis = optional** (unset URL → single-process degrade); Redis never holds memory bodies, citations, or checkpoints.
3. This file is a **coarse program contract**, independently shippable from `specs/corpus-ops-depth.md` (no EDGAR bulk / redline required).
4. Default `uv run pytest -q` stays green with no live SEC and no live memory MCP hard dependency.
5. Full **memory subsystem in fin**, **default OFF**; ON assembles extract / recall / GC / dream.
6. **dream** consolidates useful memory then **transports it to MCP for durable persistence**; without MCP or with memory OFF, dream skips or stops at Postgres buffer; swapping MCP = swap **memory** adapter only.
7. Retrieve MCP ≠ memory MCP (separate configs/channels); memory recall is **advisory only** and must not become citation evidence.
8. Session sliding window + incremental summary live in this spec; they compress the current thread and do not default-write to MCP.
9. DST / intent upgrade (confidence + optional Qwen3-1.7B on classify) keeps Soft-Track four tracks and rules tier; does not rewrite the track set.
10. Thumbs up/down land in Postgres; they do not directly change the verifier or retrieve ranking; training-set export is Out / future unless a wave explicitly adds it.
11. Includes Postgres connection pooling, optional Redis queue/rate-limit skeleton, and minimal observability (latency / refuse counts visible — not the third-party 300ms/800ms hard SLA).
12. Agent rewrite / empty-hit retry is a **tool-loop enhancement**, not a fixed retrieve pipeline.
13. `#141` / Soft-Track / workspace stream baseline are **Inherits**.

## Objective

Ship a **switchable** memory-and-dialogue platform on the existing **workspace**: long threads stay coherent under compaction, operators get better track classification, optional durable memory via dream→MCP, thumbs feedback for ops, and production-ready data/queue knobs — without blocking corpus expansion work.

Success is: each Does focus below has binary checks that later wave plans can take as Acceptance; with memory OFF the product behaves as today's filing agent.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: A **workspace** knowledge-base Agent: the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
- **host agent**: fin as the LangGraph runtime that routes tracks and calls research tools behind the **retrieve port**; it is not the ranking or vector store.
- **Soft-Track**: The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
- **status snapshot**: One append-only per-turn record of research working context (e.g. company, period, document ids, active track cues) stored on `AgentState` in the checkpointer. History is retained; the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot.
- **iknow-memory**: A separate TypeScript MCP memory server (own repo and release train). It may later supply institutional or cross-session memory behind fin's retrieve adapter; fin's filing vertical slice does not wait on it.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **run**: One graph `invoke` or `stream` on a thread (`run_id`): `done` / `refuse` / `step_cap` end the run; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Crash resume continues that in-flight `run_id` from the last complete host-graph node; at most one run is active per thread.

## Boundaries

- **Does:**

  **Focus 1 — Data and optional queue plane**
  - Postgres connection pooling for checkpointer / thread_store paths used by `fin-serve`.
  - Optional Redis: rate-limit / fair queue / Pub/Sub skeleton; absent URL → documented single-process behaviour; never SSOT for memory or turns.

  **Focus 2 — Session memory governance**
  - Sliding window + incremental summary on long threads; critical slots (company/period and status-snapshot dependents) survive compaction.
  - Compaction artifacts stay on the thread/checkpoint (+ Postgres metadata as needed); not default MCP writes.

  **Focus 3 — Switchable memory subsystem**
  - `memory port` with local Postgres buffer backend; default OFF = zero memory tools, zero dream, pytest green.
  - ON: extract / recall / GC over local buffer; recall is advisory (dialogue or gated tracks per implementer headroom) and never citation evidence.
  - **dream**: rule-gated consolidate of useful buffer entries → transport to **memory MCP** for durable persistence; failures typed; must not fail the operator's foreground turn.

  **Focus 4 — Intent / DST upgrade**
  - Confidence tiers on Soft-Track decisions; low confidence → clarify (existing leaf).
  - Optional small-model classify (e.g. Qwen3-1.7B-class) only for the ambiguous LLM tier; rules tier remains authoritative when it fires.

  **Focus 5 — Feedback and observability**
  - Thumbs up/down on a turn (workspace UI + Postgres); does not alter verifier/retrieve.
  - Minimal observability: refuse/done counts and/or latency hooks documented; not third-party hard SLAs.
  - Optional empty-hit / rewrite retry inside the research tool loop without replacing the agent loop with a retrieve pipeline.

  **Test allocation (all foci, 5 classes):** empty (memory OFF, empty buffer, empty Redis); negative (bad memory MCP URL, invalid thumb payload, classify schema fail); overflow (summary token/window caps, dream batch cap); concurrent (409 concurrent_run unchanged; Redis lock fair-queue if ON); exception (Postgres pool exhaustion typed, Redis down degrade, dream MCP fail typed).

- **Confirms with human:** none remaining after gate.

- **Out of this spec:** object store / multi-format ingest / redline / three-year statements / real EDGAR monitor (owned by `corpus-ops-depth`); SSO/RBAC/multi-tenant; Public API + API keys; fin-as-MCP-server; email/Slack; attach_image; queued follow-up; YAML playbooks; in-repo hybrid/RRF; training export from thumbs; weakening citation verifier; rewriting Soft-Track track names.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset; default `uv run pytest -q` stays green with no live SEC and no live memory MCP.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 when web surfaces change.
2. With memory OFF (default), no memory tools appear on research/dialogue paths and dream does not run; behaviour matches pre-spec Soft-Track + tools for a scripted turn.
3. Postgres pool is used on the product serve path (or documented equivalent); misconfigured checkpoint URL remains loud/typed (no silent InMemory in product).
4. With Redis URL unset, serve starts and handles a scripted turn; with Redis URL pointing at a down socket, typed degrade (no blank 500) and requests still complete under single-process policy.
5. A long scripted thread exceeding the compaction threshold keeps latest status-snapshot slots (company/period) available to classify/inherit; model-facing history is shortened vs the uncompacted baseline in a testable way.
6. Memory ON + local buffer: extract produces ≥1 buffer entry from a scripted dialogue; recall returns advisory content; GC soft-disables an expired/superseded fixture entry without deleting authority rows incorrectly.
7. Dream with memory MCP fixture: rule gate fires → durable write observed on the memory adapter; with MCP absent, dream skips or buffers only — foreground turn still `done`/`refuse` normally.
8. A memory recall hit cannot satisfy the citation verifier alone: a research turn that quotes only memory text without retrieve/read evidence does not `done` with citations from memory.
9. Classify confidence below threshold routes to clarify; rules-tier greeting still `dialogue` without calling the small model when rules fire.
10. Thumbs up/down persist on GET history for that turn; flipping does not change citations for that run.
11. A documented observability command or `/health`/`/api/status`-adjacent field set exposes at least refuse/done or latency signal without echoing secrets.
12. Citation gate still fills only verified spans; `refuse` / `step_cap` citations `[]`.

## Open Questions

(none)

## Inherits / Changes

**Inherits:** Soft-Track ADR 0027; status snapshots; track_decision persistence; retrieve port ADR 0032 (filing retrieve); Postgres checkpointer ADR 0015; workspace ADR 0023–0026; enterprise-kb watchlist/health/compare as unrelated surfaces. Map: future wayfinder issue for memory-dialogue-platform.

**Changes this spec adds:** memory port + OFF default; local PG buffer; dream→memory MCP persistence; session compaction; DST confidence + optional small classify model; thumbs feedback; Postgres pooling; optional Redis; minimal observability; tool-loop empty-hit/rewrite retry.

## 待写入 (persist after ACR)

empty — flushed 2026-08-29: CONTEXT **memory port** / **memory backend** / **dream** / **object store** + iknow-memory boundary; ADR 0035 + 0036 accepted.

## architecture-change-reviewer

Pre-impl 5-verdict (declared structure; not line-count of unwritten code):

bounded-context-guardian: yes — memory stays a host/workspace capability behind a **memory port** (not a `controllers/` layer); filing retrieve stays on **retrieve port**; web only renders thumbs/compaction UX; ranking/chunking for memory MCP stay outside fin.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–11.
error-handling-enforcer: yes — inherit typed EXITs; new paths named (memory OFF, dream MCP fail, Redis down degrade, classify schema fail); no silent empty catch.
complexity-anti-drift: yes — five foci, one concern each; writing-plans must not dump all foci into one god-plan; dream is not nested inside `model_node`; classify small-model stays in the classify tier.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences **one wave = one plan = tracer bullets**; conflict with “1 commit” is resolved by split, not by merging waves.

**OVERALL: PASS — hand to writing-plans** when the operator asks for the plan. Spec path: `specs/memory-dialogue-platform.md`.

Affected surfaces (declared):

- `src/fin/host/` — memory port wiring, classify/DST, compaction, dream trigger, thumbs API, pool/redis hooks, observability
- `src/fin/` new memory package or `host/memory/` — buffer/GC/extract (implementer chooses name under capability slicing)
- `web/src/` — thumbs, optional memory/status copy
- `tests/host/`, opt-in memory/mcp marks

