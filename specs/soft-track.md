# Spec: soft-track

## Objective

Ship **Soft-Track** turn orchestration on fin's existing **host graph** so the workspace research thread behaves as a normal conversational agent: greetings, clarification, and off-topic soft replies are first-class; only the **research track** binds tools and runs the **citation verifier**. Operators stop seeing greeting → bounce → flicker. Success is one compiled graph, four tracks, durable status-snapshot history, auditable `track_decision` on each run, and five opt-in scripted Soft-Track contracts that pass before merge.

Quoted from `docs/CONTEXT.md`:

- **fin**: A normal conversational agent whose durable runtime is a LangGraph host graph: each turn is routed to a **track**; only the **research track** specializes in the frozen 10-Q corpus with tools and the citation verifier. Retrieve ranking stays off-graph behind the retrieve port.
- **track**: The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `research`. Chosen in-graph before execution; tools and the citation verifier bind only to `research`.
- **Soft-Track**: The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
- **status snapshot**: One append-only per-turn record of research working context (e.g. company, period, document ids, active track cues) stored on `AgentState` in the checkpointer. History is retained; the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot.
- **agent loop**: On the **research track**, the model repeatedly picks `list_files`, `retrieve`, or `read_file` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Research `done` is only after the citation verifier passes; non-research tracks may `done` without tools or citations. Step order is not hardcoded.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears on a page this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
- **workspace**: A local web UI server: create, list, open, resume, and delete a research thread; thinking streams unframed then collapses; tool calls stream then collapse as human-readable traces; citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **run**: One graph `invoke` or `stream` on a thread (`run_id`): `done` / `refuse` / `step_cap` end the run; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Crash resume continues that in-flight `run_id` from the last complete host-graph node; at most one run is active per thread.
- **v1 corpus**: The frozen private fixture source SEC 10-Q slice at `/home/winner/projects/fin`: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3 (12 PDFs). Upstream commit `547f132`. Cross-period questions use 2023 Q1–Q3 only.

## Boundaries

- **Does:**
  - In-graph Soft-Track routing on the same compiled **host graph** used by CLI, `POST /ask`, and workspace (ADR 0013 / ADR 0027). No HTTP-only classifier as the authority.
  - Tiered track decision engine: structural rules → LLM structured classify only when ambiguous (reuse `FIN_MODEL`) → thread inherit from latest **status snapshot** → grounded **clarify** path → conditional policy (corpus/task signal → research; none → dialogue; out-of-domain → off_topic). No single global default track.
  - Four tracks: `dialogue`, `clarify`, `off_topic`, `research`.
  - **research**: bind `list_files` / `retrieve` / `read_file`; existing agent loop; **citation verifier** on path to END; bounce / refuse / step_cap unchanged in kind.
  - **dialogue** / **off_topic**: no tool binding; no citation verifier; call-scoped system prompt; terminal `status == "done"` with `citations` empty allowed.
  - **clarify**: dedicated thin path; ask for missing slots using options grounded in the frozen-12 manifest (and current snapshot gaps); no tools; no citation verifier; `done` with empty citations; next user turn is a new **run**.
  - **off_topic**: model-generated soft refuse under call-scoped capability bounds; not `refuse` status; not a fixed redirect string as the only runtime reply.
  - Per-track call-scoped system prompts (prepend at model call time; not written into checkpoint messages).
  - **status snapshot** history on `AgentState` in the checkpointer (SSOT). Full `messages` / turn rows / citations remain fully retained. Append when research working context is set or changes (including company change → new snapshot with dependent fields cleared); latest is the active routing view. Dialogue/off_topic need not append a snapshot every turn. Do not dual-write snapshot SSOT into `thread_store`.
  - Persist `track_decision` on each workspace `RunRecord` (and mirror on state for in-run routing): at least `track`, `confidence`, `decision_tier`, and structured `signals` (machine-readable, not free-form dumps). GET history / eval may read RunRecord as the presentation SSOT for past turns.
  - Classify-tier failure EXITs (typed, non-empty): invalid/missing track enum, structured-output schema parse failure, or classify model error each EXIT to the next tier in order (rules → LLM → inherit → grounded clarify → conditional policy) with a recorded `decision_tier` / reason; they must not crash the run and must not enter the citation verifier on a non-research outcome.
  - Workspace: optional light research-track hint on the turn/status line; no Chat|Research mode picker. SSE (or equivalent status payload) exposes `track` for that run so the SPA can render the hint.
  - Flat `StateGraph` for Soft-Track v1 (route + track leaves + existing research nodes). Logical boundary: turn orchestration vs research execution. Subgraph extraction is out of this spec but upgrade triggers from ADR 0027 apply after ship.
  - Soft-Track scripted contract tests (ScriptedChatModel / InMemorySaver), marked opt-in (not collected by default `uv run pytest -q`), covering five MUST suites: greeting→dialogue; clarify→no tools; off_topic→soft `done` (not `refuse`); true 10-Q→research+verifier path; inherit follow-up→research via snapshot. Boundary-test agent authors **≥2 boundary cases per suite** before merge, including an explicit 5-class Soft-Track matrix: empty (blank/underspec utterance or empty-snapshot inherit), negative (off-domain / wrong-track assertion), overflow (research `step_cap` / `citation_repair_cap` only on research; snapshot append does not wipe history), concurrent (same-thread Soft-Track send still 409 / `ConcurrentRunError`), exception (invalid structured classify → typed tier EXIT, not crash or silent mis-route).
  - Keep existing nine gold `-m eval` cases opt-in; they are not Soft-Track merge blockers.
- **Confirms with human:** none remaining (assumption gate self-confirmed by operator 2026-08-24: proceed from D1–D18 / ADR 0027).
- **Out of this spec:**
  - Hybrid/dense retrieve, XBRL tools/verifiers, peer-compare `Send`, research subgraph extraction, Chat|Research chrome, supervisor handoff, trained intent classifier service, long-thread compaction, HITL approval UX, auth/multi-tenant.
  - HTTP-only router; gate-predicate-as-router; single global default track fallback.
  - Rewriting Soft Brutalism layout beyond the light research hint.
  - Replacing ADR 0021 citation semantics on the research track.
  - Making Soft-Track suites part of every default pytest invocation.

## Success Criteria

- `uv run pytest -q` still exits 0 without selecting Soft-Track opt-in marks (existing non-Soft-Track suite green; Soft-Track cases deselected by default). Default (non-opt-in) tests that encoded pre-Soft-Track “no tools → always citation bounce” must be narrowed to `track=research` so the default suite stays green after Soft-Track lands.
- Soft-Track opt-in suite (exact mark name fixed in plan) collects ≥5 MUST suites **and ≥2 boundary cases per suite** (including the 5-class matrix in Boundaries) and exits 0 under ScriptedChatModel + InMemorySaver: (1) greeting → `track=dialogue`, no tool calls, no citation bounce, `status=done`, empty citations; (2) underspecified filing ask → `track=clarify`, no tools, `done`, empty citations; (3) off-domain ask → `track=off_topic`, soft reply, `status=done` (not `refuse`), empty citations; (4) true 10-Q fact ask with scripted tools+span → `track=research`, citation verifier path, `done` with ≥1 citation when script supplies a passing span; (5) prior research snapshot present + short follow-up → inherit to `research` without requiring re-stated company/period in the utterance.
- Scripted run that would formerly bounce a greeting (no tools → citation gate) no longer enters the citation verifier on `dialogue` / `clarify` / `off_topic`.
- After a non-research Soft-Track run, checkpointed `messages` do not contain that track’s call-scoped system prompt as a durable `SystemMessage` (prepend-at-call only).
- `AgentState` (or documented channel) retains ≥2 status snapshots after two successive research-context turns; after a company-change turn, the latest snapshot has cleared dependent fields while the prior snapshot remains readable from checkpoint history.
- Workspace `RunRecord` (or GET turn row) for a Soft-Track run includes `track_decision` with `track`, `confidence`, `decision_tier`, and structured `signals`; `track_decision.track` matches the graph decision for that `run_id`.
- Scripted classify failure (invalid track enum, schema parse fail, or classify model error) records a tier EXIT and still terminates the run with a defined track via the remaining cascade — never an uncaught exception on the Soft-Track path.
- Research-track workspace SSE (or terminal status frame) includes `track` equal to `research` when that track was selected; dialogue turns do not require a visible mode picker.
- `compile_graph` remains a single compiled graph object shared by CLI / `/ask` / workspace (identity or compile helper contract from existing tests still holds).
- No new Chat|Research toggle appears in `web/` for this increment.
- ADR 0027 upgrade-trigger language is cited in Inherits; this ship does not add a research subgraph node.

## Open Questions

(none — assumption gate cleared 2026-08-24)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; `src/fin/host/graph.py` model/tools/citation_gate; `AgentState` + `reset_input_state` (run-scoped reset must not wipe snapshot history or accumulated messages); `citation_gate.verify_citations`; `agent_instructions.messages_for_model` call-scoped pattern; `thread_store.RunRecord` / workspace SSE from `specs/workspace.md` + ADR 0026; ScriptedChatModel; PostgresSaver / InMemorySaver (ADR 0015); FIN_MODEL / FIN_BASE_URL (ADR 0022); frozen-12 corpus + manifest; ADR 0013 same graph; ADR 0021 citations-from-verifier; ADR 0027 Soft-Track decisions; CONTEXT terms quoted above.

**Supersedes (research-track scoping):** `specs/v1-host-agent.md` Success Criterion that a scripted run with no retrieve/read_file and a non-refuse answer must bounce / eventually `refuse` with `citation_repair_cap` applies **only on `track=research`** after Soft-Track. Non-research tracks may `done` with empty citations and must not enter the citation verifier. Default (non-opt-in) tests that encoded the pre-Soft-Track universal gate must be narrowed accordingly so `uv run pytest -q` stays green.

**Changes this spec adds:** Soft-Track routing nodes and policies; per-track prompts and dynamic tool bind; status-snapshot channel on host state; `track_decision` on RunRecord (+ SSE `track`); light research UI hint; Soft-Track opt-in scripted contracts with boundary cases + 5-class matrix; narrow citation-gate entry to research only; classify-tier typed EXITs.

**待写入 (persist after ACR):** empty — CONTEXT terms (`track`, `Soft-Track`, `status snapshot`, updated `fin` / `agent loop` / `citation verifier` / `research thread` / `workspace`) and ADR 0027 already landed via domain-modeling before this spec.

## Architecture change reviewer

bounded-context-guardian: yes — Soft-Track stays in `host` capability (+ light `web/` hint); retrieve/corpus ports unchanged; snapshot SSOT on AgentState not dual-written to `thread_store`; no technical-layer top-level packages.
defensive-contract-validator: yes — five MUST suites plus ≥2 boundary cases each; explicit empty/negative/overflow/concurrent/exception Soft-Track matrix; default suite narrowed to research-only gate; concurrent 409 preserved.
error-handling-enforcer: yes — classify-tier failures have named EXITs down the cascade; off_topic uses `done` soft path; research refuse/bounce/step_cap kinds preserved; non-research never enters citation gate.
complexity-anti-drift: yes — flat graph with distinct track leaves and existing research nodes; no god-router mixing classify+verify; subgraph deferred with ADR 0027 upgrade triggers.
minimal-change-verifier: yes — one Soft-Track ship contract; implementation may split into tracer-bullet commits in writing-plans; no hybrid retrieve/XBRL scope creep.

All 5 verdicts yes.
