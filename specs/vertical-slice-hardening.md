# Spec: vertical-slice-hardening

## Objective

Harden the already-merged vertical slice — ask filings → evidence → cited answer → reliable **workspace** UX — on execution map [#91](https://github.com/winter6205/fin/issues/91), standing on merged PR #57 (Soft-Track) and PR #79 (S1 corpus-retrieve). This is a harden/close-leftover increment, not a new epic: the **host graph** does not change; new reliability lands as tool/adapter contracts, typed error surfacing, and workspace copy/UX fixes. The user is the operator running the local workspace against the frozen corpus with the Fake retrieve port by default, and optionally a real MCP adapter. Success is: every known contract gap in the retrieve seam has a green test, every known error state renders as ordinary-person copy (never a blank crash card or a silent "0 hits"), and an opt-in MCP 2.0 protocol smoke exists against a fixture server — with default `uv run pytest -q` staying green with no live SEC and no live memory server.

The Does foci mirror the NOW four-focus table in `docs/research/009-optimized-agent-first-path.md`: Soft-Track × research tools; Host tools + retrieve port; Citation UX; optional MCP protocol smoke.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **retrieve adapter**: The implementation behind the retrieve port. Default pytest uses a Fake port. When configured, product retrieve is an MCP 2.0 client (protocol-testable against a compliant public or fixture server); hits map to fin locators as `source_ref`. In-process BM25 is archived (ADR 0007; ADR 0030 superseded by 0032). Ranking and chunking for remote memory stay outside fin; the extracted cite tree stays in fin.
- **Soft-Track**: The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
- **workspace**: A local web UI server: create, list, open, resume, and delete a research thread; thinking streams unframed then collapses; tool calls stream then collapse as human-readable traces; citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **agent loop**: On the **research track**, the model repeatedly picks `list_files`, `retrieve`, `read_file`, or `get_xbrl_fact` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Research `done` is only after the citation verifier (and numeric fact gate when a fact is claimed) passes; non-research tracks may `done` without tools or citations. Step order is not hardcoded.
- **iknow-memory**: A separate TypeScript MCP memory server (own repo and release train). It may later supply institutional or cross-session memory behind fin's retrieve adapter; fin's filing vertical slice does not wait on it.

## Boundaries

- **Does:**

  **Focus 1 — Soft-Track × research tools (route/gate tightening):**
  - Fix the Soft-Track overflow-matrix assertion leak: the boundary case that currently also accepts terminal status `done` must assert the actual overflow outcome (`step_cap` / `refuse` on research overflow), not a disjunction that lets `done` pass.
  - Clarify-track prompt injects the current missing slots (from the latest status snapshot / manifest gaps) into the call-scoped prompt, instead of a static ask.
  - `HOST_SYSTEM_PROMPT` (`src/fin/host/agent_instructions.py`) names `get_xbrl_fact` alongside the other research tools and drops the stale BM25 mention; the prompt text keeps describing the retrieve seam as the **retrieve port** (per ADR 0032). This is prompt-text correction, not a glossary change.

  **Focus 2 — Host tools + retrieve port (contract gaps a–f from exploration):**
  - (a) New test: MCP server empty-hit envelope `{result: {hits: []}}` maps to `{chunks: []}` — an empty result is a miss, never an error dict and never a raise.
  - (b) New test: the default `_call_mcp` real-transport branch with no configured command (command none/false) returns `{error: "retrieve_unavailable"}` — the typed EXIT from `specs/s1-corpus-retrieve.md`, not a crash and not `{chunks: []}`.
  - (c) MCP 2.0 protocol smoke — new implementation and tests: initialize → tools/call handshake against a compliant fixture server (public server optional), under a new opt-in pytest mark (e.g. `mcp`), never collected by default `uv run pytest -q`. ADR 0032 is accepted, so this is in Does as opt-in; it does not lockstep with the iknow-memory repo.
  - (d) Complete response-envelope variant coverage in `tests/retrieve/test_mcp_adapter.py`: `result.hits`, top-level `hits`, and content-block JSON each map to chunks or a typed error — no silent partial parse.
  - (e) Positive default-wiring assertion: the test that today only checks `graph is not None` asserts that test-path `compile_graph` binds `FakeRetrievePort` by default (and product default remains `DEFAULT_RETRIEVE_ADAPTER = "mcp"`).
  - (f) Align signature drift: `src/fin/retrieve/tool.py` stops typing the archived `BM25Adapter` as the retrieve dependency (types against the port); `MCPRetrieveAdapter.retrieve` matches the port's keyword-only `document_id` parameter shape.
  - Host tool keeps forwarding the `retrieve_unavailable` error dict without recording evidence (existing behavior, kept green).

  **Focus 3 — Citation / workspace UX leftovers:**
  - `numeric_gate_fail` gets ordinary-person copy — a numeric-gate refusal must never render a blank crash card (blank terminal copy is already forbidden by `specs/workspace.md` Success Criteria).
  - `format_tool_summary` renders `retrieve_unavailable` distinguishably from 0 hits (e.g. "retrieve backend unavailable" vs "0 hits"), and gives `get_xbrl_fact` a human-readable one-liner instead of falling through to the bare tool name.
  - `fin-serve` boot with `FIN_RETRIEVE_ADAPTER` defaulting to `mcp` but no reachable MCP command logs a boot WARNING (same class as the existing missing-`web/dist` warning); README and `.env.example` document `FIN_RETRIEVE_ADAPTER`.
  - Composer surfaces 409 / 503 send failures to the operator instead of swallowing them after clearing the draft.
  - SPA catch-all tests no longer hard-depend on a committed `web/dist`: default pytest stays green without `dist` (skipif or `tmp_path`-mounted dist).
  - New web tests (vitest, alongside `web/src/turnStream.test.ts`): sources-list collapse behavior for >10 citation spans; last-thread restore on returning visit. Exporting a pure function to make these testable is implementer headroom.

  **Focus 4 — MCP protocol smoke (optional):** covered by item (c); opt-in only, decoupled from any memory-server schedule.

- **Confirms with human:**
  - FinanceBench / gold-baseline coverage-abstain-citation digits: **deferred to a later spec** (operator confirmed 2026-08-25 — not this increment, parked, not an open blocker).
  - Postgres pool / health / unset-URL hardening: **deferred to a later spec** (same confirmation; parked, not an open blocker).
  - No other items — the assumption gate (22 items) closed everything else; Does is exactly the 009 four-focus table with no extra #91-only additions.

- **Out of this spec:**
  - Reopening the S1 / EDGAR epic; rewriting the **host graph**; any new track or subgraph.
  - Reviving BM25 as product or fallback (stays archived per ADR 0032); hybrid/RRF inside fin; ranking or chunking work in this repo.
  - Institutional memory / live **iknow-memory** integration ([#92](https://github.com/winter6205/fin/issues/92) is a non-blocker); requiring a live memory server or live SEC in default pytest; lockstep releases with the memory repo.
  - S2+ fog: cross-period redline, peer matrix, watchlist, charts, reports, multi-agent, SSO/RBAC, conversation image attach.
  - Weakening the **citation verifier** in any way; extending it beyond the research track (it binds only to research; this spec only strengthens surrounding UX and error surfacing).
  - Live EventSource / token-stream reconnect; Stop / cancel button; queued follow-up (still out per `specs/workspace.md`).
  - FinanceBench / gold eval digits and Postgres pool hardening (parked in Confirms above).
  - Authoring plans/ files, `docs/CONTEXT.md`, or new ADRs from this spec body (tracker note lives in Inherits).

## Success Criteria

Each criterion is binary and maps to a command or a named test. "NEW" marks tests the plan must add; unmarked tests exist today and must stay green.

1. `uv run pytest -q` exits 0; the default suite makes no request to `data.sec.gov` / `www.sec.gov` and requires no live memory server (inherited from `specs/s1-corpus-retrieve.md`).
2. `uv run pytest -q -m soft_track` exits 0, and the overflow-matrix boundary case no longer accepts terminal status `done` (assertion tightened in the Soft-Track overflow test under `tests/host/`; verified by reading the assertion — no `"done"` in the accepted-status set for research overflow).
3. NEW test in `tests/retrieve/test_mcp_adapter.py`: scripted server reply `{result: {hits: []}}` → adapter returns `{chunks: []}`, not an error dict, not a raise.
4. NEW test in `tests/retrieve/test_mcp_adapter.py`: default `_call_mcp` real-transport branch with command unset/false → `{error: "retrieve_unavailable"}`.
5. NEW tests in `tests/retrieve/test_mcp_adapter.py`: each envelope variant (`result.hits`, top-level `hits`, content-block JSON) maps to chunks or a typed error dict; malformed envelope → `retrieve_unavailable`.
6. NEW opt-in MCP protocol smoke: `uv run pytest -q -m mcp` (exact mark name fixed in plan) exits 0 against a fixture server exercising MCP 2.0 initialize → tools/call; the mark is excluded from default collection (default `uv run pytest -q` deselects it, verified by the deselected count / marker config).
7. Strengthened test in `tests/retrieve/test_compile_graph_adapter_swap.py` (or `tests/host/test_compile.py`): asserts the test-path `compile_graph` default retrieve binding is `FakeRetrievePort` (positive isinstance/identity assertion, not just `graph is not None`); a companion assertion keeps product `DEFAULT_RETRIEVE_ADAPTER == "mcp"` (`tests/retrieve/test_product_retrieve_wiring.py`).
8. `src/fin/retrieve/tool.py` contains no `BM25Adapter` type annotation on the retrieve dependency (checked by `rg -c "BM25Adapter" src/fin/retrieve/tool.py` returning no product-signature match); `MCPRetrieveAdapter.retrieve` accepts `document_id` keyword-only, matching `RetrievePort.retrieve` (NEW signature-conformance test in `tests/retrieve/`).
9. `tests/host/test_retrieve_typed_exit.py` still passes: host tool forwards `{error: "retrieve_unavailable"}` and records no evidence for that call.
10. NEW test in `tests/host/`: a run ending with `numeric_gate_fail` produces non-empty ordinary-person copy in the turn row / SSE terminal frame (no blank assistant card).
11. NEW tests in `tests/host/test_stream_map.py` (or the module owning `format_tool_summary`): `retrieve_unavailable` summary text differs from the 0-hit summary text; `get_xbrl_fact` produces a human-readable one-liner (not the bare tool name).
12. `HOST_SYSTEM_PROMPT` contains `get_xbrl_fact` and does not contain the string `BM25` (NEW assertion in `tests/host/test_agent_instructions.py`).
13. NEW test in `tests/host/`: the clarify-track call-scoped prompt includes the currently missing slots for that turn (scripted snapshot with a known gap → prompt text names it).
14. NEW test in `tests/host/test_workspace_http.py` (or `test_operator_env.py`): `fin-serve` startup with adapter `mcp` and no reachable MCP command emits a WARNING log record; README and `.env.example` mention `FIN_RETRIEVE_ADAPTER` (checked by `rg -l "FIN_RETRIEVE_ADAPTER" README.md .env.example`).
15. Default `uv run pytest -q` exits 0 in a checkout without `web/dist` (SPA catch-all tests skipif or mount a `tmp_path` dist) — verified by temporarily moving `web/dist` aside and re-running.
16. NEW vitest tests in `web/src/`: sources list collapses when a turn has >10 citation spans; returning visit restores the last `/t/{thread_id}` when it still exists (pure-function extraction allowed). `npx vitest run` (and existing `tsc --noEmit`) exit 0 in `web/`.
17. NEW vitest test in `web/src/`: composer send receiving 409 or 503 renders a visible error state (the draft-cleared-and-silent path is gone).
18. `uv run pytest -q -m eval` remains opt-in and is not made a default gate by this spec (no change to marker config for `eval`).

## Open Questions

(none — assumption gate of 22 items closed 2026-08-25: items 1–20 closed by evidence, items 21–22 confirmed by operator)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; capability slices `src/fin/corpus/`, `src/fin/retrieve/`, `src/fin/host/`, workspace SPA at `web/`; default test doubles ScriptedChatModel + InMemorySaver with 5-class contract tests (empty / negative / overflow / concurrent / exception); opt-in marks `soft_track` and `eval` stay opt-in; typed EXITs from `specs/s1-corpus-retrieve.md` and `specs/workspace.md` — `retrieve_unavailable`, `unknown_locator`, empty hits as empty dicts (`{chunks: []}`, `{filings: []}`), `sec_http_fail`, `ocr_required_unavailable`, workspace 409 `{error: concurrent_run}` / 503 `{error: ingest_missing}`; **ADR 0032** (accepted) as the current retrieve seam — retrieve adapter is an MCP 2.0 client behind the **retrieve port**, Fake default in pytest, BM25 archived, no lockstep memory repo (ADR 0030 is superseded and is not cited as the current seam); ADR 0013 one compiled graph; ADR 0021/0027 citation and Soft-Track contracts; `specs/soft-track.md`, `specs/s1-corpus-retrieve.md`, `specs/workspace.md` as the prior contracts this increment closes leftovers against; `docs/research/009-optimized-agent-first-path.md` NOW table and 贯穿约束 as the scope mirror. Tracker note: `gh` issue writes return 403 from this environment, so the downstream plan artifact is a local `plans/vertical-slice-hardening.md` (written by `writing-plans`, not by this spec).

**Changes this spec adds:** contract tests closing MCP-adapter gaps (empty-hit envelope, no-transport EXIT, envelope variants, positive Fake-default assertion, signature alignment); opt-in MCP 2.0 protocol smoke (new mark + fixture server); Soft-Track overflow assertion tightening; clarify missing-slot prompt injection; `HOST_SYSTEM_PROMPT` tool-list correction; `numeric_gate_fail` ordinary-person copy; `format_tool_summary` unavailable-vs-0-hits and `get_xbrl_fact` one-liners; `fin-serve` retrieve-adapter boot WARNING + `FIN_RETRIEVE_ADAPTER` docs; composer 409/503 surfacing; dist-independent SPA tests; sources-collapse and last-thread-restore web tests.

## 待写入 (persist after ACR)

empty — no new CONTEXT term is introduced (all terms quoted verbatim above), and ADR 0032 already covers the MCP 2.0 initialize → tools/call handshake, so no new one-way-door ADR is proposed. The stale BM25 mention in `HOST_SYSTEM_PROMPT` is a prompt-text fix, not a glossary change.
