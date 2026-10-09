# Plan: soft-track

**Goal:** Soft-Track lands on the shared host graph so greetings/clarify/off-topic no longer bounce through the citation verifier, while research keeps tools + cites, with auditable track decisions and opt-in Soft-Track contracts.
**Approach:** Five conversation-shaped bullets. First cut non-research off the gate (the #49 flicker root). Then dedicated clarify, snapshot inherit, ambiguous classify EXITs, and finally wire `track_decision` into RunRecord/SSE/UI plus the opt-in Soft-Track ship suite. Flat graph only; no subgraph, no Chat|Research chrome. ADR 0027 already records the architecture — no extra `[decision]` bullet.
**Spec link:** `specs/soft-track.md`
**Parent map:** the tracked change
**Tracker:** GitHub `the private organisation/fin` (main path). Spec the tracked change ; plan the tracked change ; T1–T5 are #52–#56 with native blocking edges (T2/T3/T4 ← T1; T5 ← T2,T3,T4).
**ACR:** all-yes (copied from spec)

bounded-context-guardian: yes — Soft-Track stays in `host` capability (+ light `web/` hint); retrieve/corpus ports unchanged; snapshot SSOT on AgentState not dual-written to `thread_store`; no technical-layer top-level packages.
defensive-contract-validator: yes — five MUST suites plus ≥2 boundary cases each; explicit empty/negative/overflow/concurrent/exception Soft-Track matrix; default suite narrowed to research-only gate; concurrent 409 preserved.
error-handling-enforcer: yes — classify-tier failures have named EXITs down the cascade; off_topic uses `done` soft path; research refuse/bounce/step_cap kinds preserved; non-research never enters citation gate.
complexity-anti-drift: yes — flat graph with distinct track leaves and existing research nodes; no god-router mixing classify+verify; subgraph deferred with ADR 0027 upgrade triggers.
minimal-change-verifier: yes — one Soft-Track ship contract; implementation may split into tracer-bullet commits in writing-plans; no hybrid retrieve/XBRL scope creep.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** after T5, one code-review pass on the whole Soft-Track diff (not per-commit during WIP). Soft-Track opt-in suites are authored with the boundary-testing agent; they stay on an opt-in pytest mark (not every default `pytest -q`).

**待写入:** (empty — CONTEXT Soft-Track terms and ADR 0027 already flushed)

> Soft-Track pytest mark is **`soft_track`**: deselected by default `uv run pytest -q`; selected via `uv run pytest -q -m soft_track`.

## Tasks (ordered by dependency)

1. **Greeting and off-topic leave the research contract** — tag: `[implementation]` (#52)
   - **Inherits:** spec Soft-Track / ADR 0027: in-graph route; `dialogue` and `off_topic` bind no tools, skip citation verifier, may `done` with empty citations; research still binds tools and uses citation verifier; per-track call-scoped prompts not written into checkpoint messages; same compiled graph (ADR 0013); ADR 0027 upgrade triggers (flat until research≈8+ nodes / `Send` fan-out / separate research compile-test / next capability XBRL-class) — this ship does **not** add a research subgraph node; supersede v1 universal no-tools→bounce to **research only**; default `uv run pytest -q` stays green
   - **Surface:** `host` (graph / state / prompts) + existing host tests
   - **Acceptance:** scripted greeting → `track=dialogue`, no tool calls, never enters citation gate, `status=done`, empty citations; scripted off-domain → `track=off_topic`, soft reply, `status=done` (not `refuse`), empty citations; after either non-research run, checkpointed `messages` have no durable per-track `SystemMessage` (prepend-at-call only); scripted research fact with tools+span still `done` with ≥1 citation; existing `compile_graph` identity / shared-helper contract tests still hold (CLI / `/ask` / workspace one compiled graph); default pytest no longer asserts universal no-tools bounce outside research
   - Status: [x] done — merged PR (`a76d2f8`, 2026-08-24)

2. **Clarify asks before tools** — tag: `[implementation]` (#53)
   - **Inherits:** spec: dedicated thin `clarify` path; options grounded in frozen-12 manifest / snapshot gaps; no tools; no citation verifier; `done` with empty citations; next user message is a new run
   - **Surface:** `host`
   - **Acceptance:** underspecified filing ask (e.g. metric without company/period) → `track=clarify`, no tools, never enters citation gate, `done`, empty citations; reply references real corpus options (not invented tickers outside the frozen 12); checkpointed `messages` have no durable clarify call-scoped `SystemMessage`; follow-up run can then enter research when slots are filled
   - Status: [x] done — merged PR
   - [blocks: T1]
   - [parallel] with T3 after T1

3. **Status snapshots inherit research follow-ups** — tag: `[implementation]` (#54)
   - **Inherits:** CONTEXT **status snapshot** / spec: append on research-context set or change; latest is active view; company change appends new snapshot with dependent fields cleared; prior snapshots retained; `reset_input_state` must not wipe snapshot history; dialogue/off_topic need not append every turn
   - **Surface:** `host` (state / graph)
   - **Acceptance:** two successive research-context turns leave ≥2 readable snapshots; short follow-up with prior research snapshot inherits to `track=research` without restating company/period; company-change turn yields latest snapshot with cleared dependents while prior snapshot remains
   - Status: [x] done — merged PR
   - [blocks: T1]
   - [parallel] with T2 after T1

4. **Ambiguous classify EXITs down the cascade** — tag: `[implementation]` (#55)
   - **Inherits:** spec: rules → LLM (`FIN_MODEL`) when ambiguous → inherit → clarify → conditional policy; typed EXITs for invalid/missing track enum, schema parse fail, classify model error; no single global default track; must not crash; must not send non-research into citation gate
   - **Surface:** `host`
   - **Acceptance:** scripted invalid classify / schema fail / classify model error each records a tier EXIT and still finishes with a defined track via the remaining cascade (no uncaught exception); ambiguous path may invoke LLM classify without replacing rules-primary behaviour on high-confidence rules hits
   - Status: [x] done — merged PR
   - [blocks: T1]
   - [parallel] with T2 / T3 after T1

5. **Track reaches the wire and Soft-Track ship gate** — tag: `[implementation]` (#56)
   - **Inherits:** spec: `RunRecord.track_decision` has `track`, `confidence`, `decision_tier`, structured `signals`; SSE/status exposes `track`; light research UI hint only; no mode picker; Soft-Track opt-in mark `soft_track` with ≥5 MUST suites and ≥2 boundary cases each including empty/negative/overflow/concurrent/exception matrix under ScriptedChatModel + InMemorySaver; nine gold `-m eval` unchanged as non-blockers
   - **Surface:** `host` (thread_store / workspace HTTP) + `web/`
   - **Acceptance:** workspace run persists `track_decision` four fields matching the graph for that `run_id`; research SSE/status includes `track=research`; SPA can show a light research hint without Chat|Research chrome; `uv run pytest -q -m soft_track` exits 0 covering MUST (1)–(5) from the spec plus ≥2 boundaries per suite and the 5-class matrix (ScriptedChatModel + InMemorySaver); default `uv run pytest -q` still exits 0 with Soft-Track deselected; concurrent Soft-Track send still 409 / `ConcurrentRunError`
   - Status: [x] done — merged PR
   - [blocks: T2, T3, T4]

**Shipped:** 2026-08-24 on `main` via that pull request. Opt-in gate: `uv run pytest -q -m soft_track`. Production `classify_model` must not steal underspec filing asks or snapshot inherit (`66086b4`). Worktree `.claude/worktrees/soft-track` removed after merge.
