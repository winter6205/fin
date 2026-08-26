# Plan: workspace stream fidelity

**Goal:** Every track's reply streams token-by-token onto the live turn with the model's reasoning captured from the channel the configured provider actually emits; a bounce retry never blanks the bubble; reopening a watched thread never repaints prose the operator already saw; and the stream contract is proven end-to-end by a repaired Playwright runner, a credential-free scripted lane, and a live-provider smoke lane that aborts loudly when `ANTHROPIC_AUTH_TOKEN` is missing — plus a small `/api/status` strip that tells the operator whether retrieve is reachable.

**Approach:** Four independent fidelity fixes land first on the existing host SSE surface and the SPA fold helpers: the send generator's node predicate stops dropping non-research deltas, `stream_map.py` learns the reasoning shape the live provider emits (falling back to the existing inline prose-strip path, never swallowing), the bounce pending-reset keeps its no-standalone-blank contract on both ends of the wire, and the already-tested `mergeReconciledTurn` helper is finally wired into the open-thread path. Two decision gates then unlock the rest: a spec amendment brings Playwright e2e lanes into scope (they are out of `specs/workspace.md` today) before any runner work, and an agent-context-surface decision fixes what `/api/status` may expose (in-process retrieve port only — no MCP client, no % context window) before the Sidebar strip renders it. No bullet reopens `plans/workspace-stream.md` (shipped, that pull request) and none enters the iknow-memory retrieve envelope mismatch — that stays flagged in its own repo.

**Out of this plan (explicit):** inventing a skill concept; any "% of context window" telemetry; reopening `plans/workspace-stream.md` or its shipped bullets; fixing the iknow-memory retrieve envelope mismatch (flagged, not entered — it belongs to that repo's release train).

**Spec link:** `specs/workspace.md` (T5 amends it; until T5 lands, Playwright lanes are out-of-spec and T6–T8 must not start)
**Parent map:** the tracked change
**Sibling plan:** `plans/workspace-stream.md` — shipped 2026-08-23 via that pull request; this plan is the fidelity follow-up on the same contract, not a reopen.
**Tracker:** local markdown fallback — `gh` in this agent environment is authenticated read-only (issue creation is blocked), so no tracker issues or native blocking edges exist yet. This file is the tracker SSOT; an operator with write `gh` can publish T1–T10 per `writing-plans` `references/tracker.md` (label `ready-for-agent`, blockers first, GraphQL `addBlockedBy` edges) without changing the bullets.
**ACR:** all-yes — peer review bc-678c6de2 (claude-fable-5-thinking-high, 2026-08-25); verdict block copied verbatim below.

bounded-context-guardian: yes — every touch stays inside the existing host surface (`src/fin/host/workspace_http.py`, `stream_map.py`) and its `web/` SPA + `tests/host/` + `scripts/`; no technical-layer dirs, no reverse dep, `/api/status` reads the in-process retrieve port rather than adding the MCP client that `specs/workspace.md` keeps out of scope; the iknow-memory envelope mismatch is flagged, not entered.
defensive-contract-validator: yes — each finding maps to a boundary class with an existing test surface: empty (G3 zero-delta pending state; empty `reset` frame in `turnStream.test.ts`), negative (non-`model`-node deltas against the `workspace_http.py` filter in `test_workspace_http.py`), overflow (step_cap/long-stream fold already fixtured, extended for soft-track turns), concurrent (mid-stream GET reconcile + existing 409-before-headers tests, extended for R2 merge), exception (provider dropping `reasoning_details` via the fake chunk mapper in `test_stream_map.py`; missing `ANTHROPIC_AUTH_TOKEN` typed abort in the smoke lane).
error-handling-enforcer: yes — failure paths are named and typed in the outline: smoke lane exits non-zero with an explicit missing-token message (never a silent skip-pass), `/api/status` renders retrieve-unreachable as a typed field rather than a blank strip, reasoning capture falls back to the existing prose-strip path in `graph.py` instead of swallowing, and no bullet introduces a fallback without an exit criterion.
complexity-anti-drift: yes — declared structure keeps one abstraction level per module: the node-filter fix stays a predicate change in the SSE generator, reasoning capture stays in `stream_map.py`, fold semantics stay pure functions in `turnStream.ts`, `/api/status` is a separate small route + a Sidebar strip; no god-function/god-file intent (the pre-existing 1164-line `workspace_http.py` is a soft smell noted for the implementer, not a gate).
minimal-change-verifier: yes — one plan of ten sequenced tracer bullets, one commit each; conflicts are sequenced not merged: the spec-amendment [decision] must land before any Playwright bullet (Playwright is out-of-spec today), the agent-context-surface [decision] gates context-telemetry-render, and `plans/workspace-stream.md` is not reopened.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** after T10, one code-review pass on the whole fidelity diff (not per-commit during WIP). Live-provider smoke (T8) is never part of default `uv run pytest -q`.

**Round closed 2026-08-26** on `winter/fidelity-integrate-8400` — the first tree that carries all ten bullets plus the F-series live-verification fixes (F1 reasoning outbound replay, #116/#117, F3 streamed/persisted answer parity) at once. That branch is where each bullet's acceptance was actually exercised end to end: the per-bullet branches were only ever green in isolation. Evidence matrix, the end-of-round `code-review` verdict, and LIVE_GAPS v2 live in `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.

**待写入:** (empty — no new CONTEXT term is settled by this plan itself; T5 and T9 are `[decision]` bullets whose outcomes flush their own spec-amendment / ADR / CONTEXT deltas via `domain-modeling` when they land)

> `plans/workspace-stream.md` is shipped and closed ("Do not reopen these bullets"). This plan fixes fidelity gaps on that wire, it does not re-litigate its contract. ADR 0026 stays: node-complete checkpoint grain; token deltas are not a save point.

## Tasks (ordered by dependency)

1. **Non-research-node deltas reach the wire** — tag: `[implementation]`
   - **Inherits:** spec: thinking and answer token-stream onto the live turn (`model.stream()`, SSE types `thinking` | `tool_call` | `tool_result` | `answer` | `citations` | `status`); Soft-Track (`specs/soft-track.md` / ADR 0027): `dialogue` / `clarify` / `off_topic` are model-generated turns on the same compiled graph that may `done` without tools or citations; hidden bounce marker never appears in SSE. Open for the implementer: the predicate shape (which graph nodes count as answer-authoring) — today the send/resume generator hard-drops every `AIMessageChunk` whose `langgraph_node` is not `"model"`, so soft-track replies arrive as one post-hoc dump instead of a stream.
   - **Surface:** `host` SSE generator (`src/fin/host/workspace_http.py`) + `tests/host/test_workspace_http.py`
   - **Acceptance:** a scripted non-research run (dialogue or clarify) emits multiple `answer` delta frames on `POST /api/threads/{id}/messages` before its terminal `status` frame — not zero deltas followed by a dump; a scripted research run keeps its current frame sequence unchanged (thinking/tool/answer/citations); deltas from nodes that must stay silent (e.g. the classify tier) still never reach the wire; `uv run pytest -q` stays green.
   - **Completion:** headroom holds — an allowlist set, a metadata tag, or an inverted denylist all pass; the bullet fixes the observable (soft-track turns stream), not the predicate's spelling.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [parallel]

2. **Reasoning channel captured in the shape the provider emits** — tag: `[implementation]`
   - **Inherits:** spec: reasoning rides `additional_kwargs.reasoning_details`, is copied onto the checkpointed `AIMessage` for model replay, and never appears inside `answer` events or the citation gate's prose (SC60); `stream_map.reasoning_delta_from_chunk` currently reads only the `reasoning_details` list shape; `graph.py` already owns the inline `<think>`/`<thinking>` prose-strip fallback. Error contract (ACR): when the configured provider drops or renames the split channel, capture falls back to that existing prose-strip path — reasoning is surfaced or stripped, never silently swallowed into the answer.
   - **Surface:** `host` stream mapping (`src/fin/host/stream_map.py`, `graph.py` touch only if the fallback seam moves) + `tests/host/test_stream_map.py` fake chunk mapper
   - **Acceptance:** a fake chunk stream carrying reasoning in the shape(s) the live provider actually emits yields `thinking` SSE frames during the model step and an assembled checkpointed `AIMessage` whose replayable reasoning survives `assemble_final_ai_message`; a fake stream with the split channel absent but inline blocks present still populates thinking via the strip path; in every shape, no reasoning prose appears in any `answer` frame and `verify_citations` receives thinking-stripped prose only; `uv run pytest -q` stays green.
   - **Completion:** headroom holds — the implementer chooses whether the new shape is normalized at chunk read, at assembly, or via a second extractor; both designs pass the same fake-mapper observables.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [parallel]

3. **Bounce reset never retracts the live bubble** — tag: `[implementation]`
   - **Inherits:** send-generator contract already in `workspace_http.py`: a hidden-bounce `SystemMessage` arms a pending reset; the NEXT non-empty answer delta carries `reset: true` in the same frame as its text; no standalone empty reset blanks the SPA before retry prose; if the run ends with pending still armed (zero further deltas — the G3 boundary), the terminal empty answer flushes it and ordinary-person copy lives in `assistant_message` (SC67). SPA side: `mergeAnswerDelta` / `foldSendResultAnswer` in `web/src/turnStream.ts` define the fold for `reset` and post-status frames.
   - **Surface:** `host` SSE generator pending-reset state + `web/` fold helpers (`turnStream.ts`) + `tests/host/test_workspace_http.py` + `web/src/turnStream.test.ts`
   - **Acceptance:** a scripted bounce-then-retry run shows the retracted draft replaced only when retry prose arrives — at no frame between the bounce and the first retry delta is the visible answer empty; a scripted bounce that refuses with zero further answer deltas ends with the stale draft cleared and the refuse copy visible (not a blank bubble, not the pre-bounce draft); the empty-`reset`-frame case is asserted on both the wire fold and the live-bubble fold; `uv run pytest -q` and the `web/` unit tests stay green.
   - **Completion:** headroom holds — whether the fix lands generator-side, fold-side, or both is the implementer's call; the observable is the no-blank-frame invariant across the bounce boundary.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [parallel]

4. **GET reconcile merges without repainting watched prose** — tag: `[implementation]`
   - **Inherits:** spec: refresh does not reconnect token-by-token — GET shows running, POST → 409, and when the run ends GET shows the completed turn; `mergeReconciledTurn` in `web/src/turnStream.ts` is already unit-tested (`turnStream.test.ts`: streamed visible prose preferred when persisted status is `done`; persisted row authoritative for ids and citations) but its own docstring says "not wired yet". The 409-before-headers contract in `test_workspace_http.py` stays untouched.
   - **Surface:** `web/` open-thread path (store / thread page) consuming the existing helper; `host` GET handler unchanged
   - **Acceptance:** after the operator watches a run stream to `done`, the open-thread GET reconcile does not visibly retract-and-repaint the answer prose already on screen, while citations, `run_id`, and thinking duration come from the server row; opening a thread mid-run still shows running with the composer disabled; the helper is consumed by the live open path (no longer dead code) and its existing unit tests still describe the wired behaviour; `web/` `tsc --noEmit` and unit tests stay green.
   - **Completion:** headroom holds — where the merge call sites live (store action, page effect, or api layer) is open; the observable is the no-repaint merge plus server-authoritative persisted fields.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [parallel]

5. **E2E scope amendment: Playwright lanes enter the spec** — tag: `[decision]`
   - **Inherits:** spec "Out of this spec" currently lists Playwright; `scripts/playwright-stream-checks.mjs` already exists outside the spec; spec test contract: default `uv run pytest -q` uses scripted model + `InMemorySaver`, no network LLM. This bullet supersedes the Playwright exclusion for named lanes only — it does not touch checkpoint grain, SSE types, or any shipped contract.
   - **Surface:** `specs/workspace.md` (amendment section) plus an ADR if the lane gating is a one-way door — the amended spec text is this bullet's outcome; no product code in the commit
   - **Acceptance:** the spec names the browser e2e lanes now in scope (a scripted-provider lane and a live-provider smoke lane), states where each runs (opt-in command / CI lane vs never-in-default-pytest), and keeps everything else in the Playwright exclusion out; the amendment cites this plan; the commit touches spec/ADR files only.
   - **Completion:** headroom holds — the decision fixes which lanes exist and their gating, not runner filenames, harness libraries, or assertion lists.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - gates T6–T8

6. **Playwright runner runs to a verdict** — tag: `[implementation]`
   - **Inherits:** T5's amendment (lane now in-spec); the four existing checks in `scripts/playwright-stream-checks.mjs` (home corpus, stream-no-retract, …) are the starting assertion set. Open for the implementer: whether the current playwright-cli harness is repaired in place or replaced — the runner today does not reliably reach a verdict.
   - **Surface:** `scripts/` e2e runner + whatever local serve wiring it needs; `.claude/skills/playwright-cli` conventions as read-side context
   - **Acceptance:** one documented command runs the stream checks against a locally served workspace and terminates with per-check pass/fail plus a non-zero exit on any failure — no hang, no silent skip; a deliberately broken check demonstrably fails (the lane can say no); the command and its prerequisites are written where an operator will find them.
   - **Completion:** headroom holds — harness choice, config layout, and serve strategy are open; the observable is a terminating, falsifiable runner.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [blocks: T5]

7. **Scripted-provider e2e lane: browser fidelity without credentials** — tag: `[implementation]`
   - **Inherits:** T5's gating decision; spec test contract: scripted model + `InMemorySaver`, no network LLM, no credentials; T6's working runner. The fidelity observables this lane must cover are the ones T1/T3/T4 fixed: deltas before terminal status, no retract across a bounce, no bounce marker in the DOM, reload/reconcile without repaint.
   - **Surface:** `scripts/` lane entry + `host` scripted-model boot path + `web/` build; assertions ride the T6 runner
   - **Acceptance:** one command boots the workspace on the scripted provider and drives a browser through send → stream → collapse → reload, passing deterministically with no network and no `ANTHROPIC_AUTH_TOKEN`; the lane fails (non-zero) when a fidelity observable regresses; it runs where T5 said it runs and default `uv run pytest -q` does not invoke a browser.
   - **Completion:** headroom holds — script topology, fixture questions, and how the scripted provider is injected are open; the observable is the deterministic credential-free browser pass.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [blocks: T6]

8. **Live-provider smoke lane with a typed credential abort** — tag: `[implementation]`
   - **Inherits:** T5's gating decision (never in default pytest); T7's lane shape; error contract (ACR): a missing `ANTHROPIC_AUTH_TOKEN` aborts non-zero with an explicit message naming the variable — never a silent skip that reads as a pass; `compile.py` today falls back to a placeholder key, so the abort must live in the lane, not be inferred from a provider 401. Sequenced after T2 so the lane can assert real reasoning capture, not just answer text.
   - **Surface:** `scripts/` smoke lane + operator env reads; provider config via `FIN_MODEL` / `FIN_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` env names only (values never printed)
   - **Acceptance:** with credentials present, one command sends at least one real question and asserts the fidelity observables end-to-end (streamed answer deltas, thinking frames when the provider emits reasoning, terminal status, citations on a research turn); with `ANTHROPIC_AUTH_TOKEN` unset, the lane exits non-zero printing which variable is missing and runs nothing; the lane is reachable only by its explicit opt-in command.
   - **Completion:** headroom holds — question choice, pass thresholds, and output format are open; the observables are the live pass and the loud typed abort.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - [blocks: T7, T2]

9. **Agent-context surface decision: what `/api/status` may say** — tag: `[decision]`
   - **Inherits:** spec keeps "MCP client to iknow-memory" out of scope — the status surface reads the in-process retrieve port, nothing crosses the repo boundary; the retrieve port already exposes the typed `retrieve_unavailable` EXIT; out of this plan: any "% of context window" figure and the iknow-memory envelope mismatch (flagged upstream, not entered here).
   - **Surface:** `docs/adr/` (and a spec touch if the surface becomes contract) — the decision record is this bullet's outcome; no product code in the commit
   - **Acceptance:** the record names the field set the workspace status surface exposes (retrieve reachability is in; candidates like corpus/ingest presence and model identity are decided in or out explicitly), states that the read path is the in-process retrieve port (not an MCP client, not a cross-repo call), and lists the exclusions above; if a CONTEXT term is introduced, this bullet flushes it via `domain-modeling`.
   - **Completion:** headroom holds — the decision fixes the exposed field set and the read seam, not the route's response schema spelling or the UI copy.
   - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
   - gates T10

10. **Context telemetry renders as a Sidebar strip** — tag: `[implementation]`
    - **Inherits:** T9's decided field set and read seam; error contract (ACR): retrieve-unreachable renders as a typed field with readable degraded copy, never a blank strip or a 500; structure contract (ACR): `/api/status` is a separate small route, not another branch in the send / GET-thread handlers, and the strip is a Sidebar concern, not a transcript one.
    - **Surface:** `host` (new status route beside the existing workspace routes) + `web/` Sidebar + `tests/host/` + `web/` unit tests
    - **Acceptance:** `GET /api/status` returns the T9-decided shape in both the reachable and the unreachable retrieve state — the unreachable case is a typed value in the payload, not an error status or an empty body; the Sidebar strip renders both states with the degraded state readable to an ordinary operator; the route handler stays separate from send/GET-thread; `uv run pytest -q` and `web/` `tsc --noEmit` + unit tests stay green.
    - **Completion:** headroom holds — payload key names, polling vs on-open fetch, and strip layout are open; the observables are the typed two-state route and the never-blank strip.
    - Status: [x] done — integrated and verified on `winter/fidelity-integrate-8400`; see `docs/verify/2026-08-26-workspace-stream-fidelity-integration.md`.
    - [blocks: T9]
