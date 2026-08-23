# Plan: workspace

**Goal:** An ordinary person can talk in a local **workspace**: open a **research thread**, stream a **run**, see citations, follow up, reload history, and resume after a crash from the last complete host-graph node.
**Approach:** Four conversation-shaped bullets (the tracked change coarse grain — not T7/T9 one-shot surfaces). First a thread you can actually send on; then follow-up as a new **run**; then thinking/tools/citations as specified; then node-complete crash resume. `/ask` and the CLI stay eval adapters. ADR 0026 already records the streaming/resume decision — no extra `[decision]` bullet.
**Spec link:** `specs/workspace.md`
**Parent map:** the tracked change
**Tracker:** GitHub `winter6205/fin` (main path). Spec the tracked change ; T1–T4 are #28–#31 with native blocking edges.
**ACR:** all-yes (copied from spec)

bounded-context-guardian: yes — workspace is a **host** surface (HTTP + `web/` SPA) on the existing host graph; corpus/retrieve stay behind their packages; no `controllers/` / `services/` / `repositories/` / `models/` top-level dirs; `web/` is the UI of workspace, not a second agent.
defensive-contract-validator: yes — empty question no-op; unknown thread 404; concurrent same-thread 409; ingest-missing 503; follow-up must not inherit prior-run evidence; node-level resume after model-complete / tool-incomplete; stream disconnect does not delete the HumanMessage; thinking-in-content must not pass the citation gate as the answer.
error-handling-enforcer: yes — typed `ConcurrentRunError` / `IngestMissingError` / thread-not-found; 409 decided before SSE headers; hidden bounce never on a user surface; no empty catch on stream mapping; refuse/step_cap are terminal statuses with explicit copy, not swallowed blanks.
complexity-anti-drift: yes — declared structure keeps stream mapping, thinking converter, turn persistence, and graph nodes as separate modules; `model_node` streams then returns one complete `AIMessage`; UI splits thinking / tools / answer / citations rather than one god transcript component.
minimal-change-verifier: yes — one logical conversation increment; implementation splits into tracer-bullet commits in writing-plans; this spec is one contract; `/ask` and the host graph’s citation/tool contracts stay unless a bullet must reset run-scoped state (that reset is in-scope, not a drive-by refactor).

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** after T4, one code-review pass on the whole workspace diff (not per-commit during WIP).

**待写入:** (empty — CONTEXT **workspace** / **run** and ADR 0026 already flushed in SPECIFY persist)

> ADR 0013 still requires sync `invoke` on `POST /ask`. This plan does **not** reopen that. Workspace routes stream per ADR 0026.

## Tasks (ordered by dependency)

1. **Talk on a research thread** — tag: `[implementation]` (#28)
   - **Inherits:** spec: one FastAPI process, same compiled graph as CLI/`POST /ask`; `POST /ask` still sync `invoke`; POST `fetch` SSE on `/api/threads/{id}/messages`; turn rows for GET history; `web/` SPA at `/` and `/t/{thread_id}`; Soft Brutalism tokens; chrome create/list/open/resume/delete; `GET /api/corpus` is the frozen 12 `{document_id, company, period, version, page_count}`; empty submit is a no-op; bind `0.0.0.0:8000` no auth; `uv run pytest -q` and `web/` `tsc --noEmit`; do not copy `prototype_workspace`
   - **Surface:** `host` HTTP + `web/`
   - **Acceptance:** operator can create a thread, send a scripted question, watch `answer`/`status` (and terminal `citations`) SSE into the SPA, reload `/t/{id}` and see the same turns without re-invoking; unknown id is 404 + back to list; delete then GET is 404; corpus endpoint returns the real 12; `/ask` still `invoke` on the same graph object
   - Status: [ ] pending

2. **Follow-up is a new run** — tag: `[implementation]` (#29)
   - **Inherits:** CONTEXT **run**: follow-up resets run-scoped evidence and caps; spec: title from first question (50 chars + ellipsis); concurrent send on the same thread is 409 `{error: concurrent_run}` before SSE headers; composer disabled while that thread’s run is in flight (no queue, no Stop); `refuse` / `step_cap` are normal turns with empty citations plus ordinary-person copy
   - **Surface:** `host` (state/graph send path) + `web/` composer/sidebar
   - **Acceptance:** second send on the same `thread_id` starts `step_count`/`repair_count` at 0 and does not treat the previous run’s pages as this run’s evidence; a scripted answer that only quotes last run’s pages is not `done`; overlapping POST returns 409 with no event-stream body; sidebar title is the first question, truncated
   - Status: [ ] pending
   - [blocks: T1]

3. **Thinking and tools stream then collapse** — tag: `[implementation]` (#30)
   - **Inherits:** ADR 0025/0026; spec: `model.stream()` token deltas; MiniMax `extra_body` thinking + `reasoning_split` with converter replay; thinking-stripped prose for the citation gate and `answer` events; thinking UI unframed + line-clamp until expand; tools are `#F5F5F4` details with human-readable summaries (not raw JSON); citations = inline chips at verified spans **and** a sources list; global thinking preference off → `thinking.type=disabled`; hidden bounce never on SSE/GET/SPA
   - **Surface:** `host` (model stream + thinking transport) + `web/` transcript
   - **Acceptance:** scripted thinking deltas never appear inside `answer`; `verify_citations` sees stripped prose and can still `done` on a ≥32-char evidenced span; GET after `done` includes collapsed thinking and tool summaries without bounce marker or retrieve JSON; SPA shows unframed thinking only after the first thinking event, tools collapse after the run, chips and sources list both present
   - Status: [ ] pending
   - [blocks: T2]

4. **Crash resume from the last complete node** — tag: `[implementation]` (#31)
   - **Inherits:** CONTEXT **run** / ADR 0018 / ADR 0026: crash resume continues the same `run_id` from the last complete host-graph node; user HumanMessage is never deleted; process-alive refresh does not reconnect token-by-token (GET shows running, POST → 409); process-died open uses `stream(None)` with no new HumanMessage; ingest-missing workspace send is 503; UI may switch threads but must not paint deltas onto another `thread_id`; this increment does not start a second in-flight run from the UI
   - **Surface:** `host` stream/open path + `web/` running/resume
   - **Acceptance:** a scripted checkpoint after a complete `AIMessage` with `tool_calls` and before tool results: `stream(None)` does not append a HumanMessage, does not regenerate that model message, runs the tool node, and reaches a terminal status; GET while a lock is held reports running; ingest-missing send is 503 `{error: ingest_missing}`; switching threads does not attach the live SSE to the wrong thread
   - Status: [ ] pending
   - [blocks: T3]
