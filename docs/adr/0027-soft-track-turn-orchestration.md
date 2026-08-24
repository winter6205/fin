# 0027. Soft-Track turn orchestration (multi-track host graph)

Date: 2026-08-24
Status: accepted

fin is a normal conversational agent on one host graph; **research track** is the specialization that binds tools and runs the citation verifier. Each turn is classified in-graph (rules primary, LLM only when ambiguous) into `dialogue` / `clarify` / `off_topic` / `research`. Non-research tracks do not bind tools, do not enter the citation gate, and may `done` with empty citations. Off-topic is a soft model reply under system bounds, not `refuse`. Classification uses a tiered decision engine (rules → LLM → thread inherit → grounded clarify → conditional policy), not a single global default track.

**Phase topology (not final):** Soft-Track v1 ships a **flat** `StateGraph` with conditional edges so routing lands first. This is **not** a permanent ban on subgraphs. Treat orchestration (route / clarify / dialogue / off_topic) and research execution (model–tools–gate) as separate logical boundaries even while flat. Upgrade triggers that **require** reopening topology (do not keep piling into flat): research path ≈8+ nodes or a multi-stage verifier pipeline; parallel fan-out (`Send` / map-reduce); research needing separate compile/test; Soft-Track stable and the next capability is XBRL / numeric / peer compare. Later agents must not cite “D14 chose flat” to refuse a subgraph.

**Memory:** Full message / turn / citation history stays durable. Minimal research context is an **append-only status-snapshot history** in `AgentState` (checkpoint SSOT; not `thread_store`); the latest snapshot is the active view for routing; older snapshots remain for audit/backtrack. Company change appends a new snapshot with dependent fields cleared.

**Prompts / UI / ops:** Per-track call-scoped system prompts (not checkpointed). Optional light UI hint on research only—no Chat|Research mode picker. Ambiguous classify reuses `FIN_MODEL` until cost/latency evidence says otherwise. Soft-Track ship gate: five scripted contract suites (greeting / clarify / off_topic / true 10-Q / inherit follow-up), boundary-tested, opt-in mark (not every default pytest). Persist `track_decision` on each `RunRecord`.

**Rejected for this increment:** HTTP-only router; gate-predicate-as-router; single default fallback track; Chat|Research chrome; supervisor handoff; training a separate intent model; dual-writing slots into `thread_store`.
