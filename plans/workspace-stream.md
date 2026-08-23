# Plan: workspace stream-then-collapse (remainder)

**Goal:** On a **research thread**, thinking and the answer **token-stream** into the live turn; thinking is a global on/off preference; after the **run**, thinking and tools collapse; a returning visit opens the last thread; crash resume uses the same delta wire.
**Approach:** Five conversation-shaped bullets on the existing workspace contract (`specs/workspace.md`). First the send path actually forwards `model.stream()` deltas as `thinking` and `answer` events; then resume shares that wire; then (in parallel) the thinking preference and last-thread restore; then post-run collapse now that a live stream exists to collapse. This round does not rebuild thread CRUD, follow-up evidence reset, `/ask` `invoke`, or chrome. ADR 0026 is not reopened: checkpoint grain stays node-complete; token deltas are not a save point.
**Spec link:** `specs/workspace.md`
**Parent map:** https://github.com/winter6205/fin/issues/22
**Parent plan:** `plans/workspace.md` (T1–T4 #28–#31). This file is the remainder of that spec’s stream-then-collapse contract, not a second product.
**Tracker:** GitHub `winter6205/fin` (main path). Spec https://github.com/winter6205/fin/issues/27 ; remainder T1–T5 are #33–#37 with native blocking edges (#36 and #37 blocked by #33).
**ACR:** all-yes (this remainder slice; copied shape from spec)

bounded-context-guardian: yes — workspace stays a **host** surface (HTTP + `web/` SPA); corpus/retrieve stay behind their packages; no `controllers/` / `services/` / `repositories/` / `models/` top-level dirs; `web/` remains the UI of workspace, not a second agent.
defensive-contract-validator: yes — empty question no-op; unknown thread 404; concurrent same-thread 409 before SSE headers; thinking-in-content must not pass the citation gate or appear in `answer` events; node-complete checkpoint still holds after a streamed model step; stream disconnect does not delete the HumanMessage.
error-handling-enforcer: yes — typed `ConcurrentRunError` / `IngestMissingError` / thread-not-found; 409 decided before SSE headers; hidden bounce never on SSE/GET/SPA; no empty catch on stream mapping; refuse/step_cap stay terminal statuses with explicit copy.
complexity-anti-drift: yes — stream mapping, thinking transport, turn persistence, and graph nodes stay separate modules; `model_node` streams then returns one complete `AIMessage`; UI still splits thinking / tools / answer / citations.
minimal-change-verifier: yes — one logical remainder of the workspace conversation increment; implementation splits into tracer-bullet commits below; `/ask` and the host graph’s citation/tool contracts stay; this round does not copy `prototype_workspace` into `web/`.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** after T5, one code-review pass on this remainder diff (not per-commit during WIP).

**待写入:** (empty — CONTEXT **workspace** / **run** and ADR 0026 already flushed)

> ADR 0013 still requires sync `invoke` on `POST /ask`. This plan does **not** reopen that. Workspace routes stream per ADR 0026. Mid-token buffers are not checkpoints.

## Tasks (ordered by dependency)

1. **Thinking and answer tokens reach the live turn** — tag: `[implementation]`
   - **Inherits:** spec: `model_node` calls `model.stream()` (not blocking `invoke`); FastAPI wraps sync graph `stream()` in `StreamingResponse`; SSE types on the wire are `thinking` | `tool_call` | `tool_result` | `answer` | `citations` | `status`; citation verifier and user-visible `answer` events use thinking-stripped prose only; hidden bounce never appears in SSE; a node is complete only after it has finished writing — `model_node` complete ⇒ one full `AIMessage` including complete `tool_calls` is checkpointed; 409 `{error: concurrent_run}` is decided **before** SSE headers; thinking UI is unframed, not in the answer bubble, and appears only after the first `thinking` event; MiniMax `extra_body` uses `reasoning_split: true` and `thinking.type` adaptive|disabled
   - **Surface:** `host` (send path + stream mapping) + `web/` transcript
   - **Acceptance:** a scripted model that emits thinking then answer in multiple chunks yields multiple `thinking` frames and multiple `answer` frames on `POST /api/threads/{id}/messages` **before** that model step finishes writing the checkpointed `AIMessage`; no thinking prose appears inside any `answer` event; `verify_citations` is called with thinking-stripped prose and can still `done` on a ≥32-char evidenced span; the SPA grows unframed thinking after the first thinking event and grows the answer as answer events arrive (citations remain terminal on that run); `POST /ask` still `invoke`; `uv run pytest -q` and `web/` `tsc --noEmit` stay green
   - Status: [ ] pending

2. **Crash resume streams the remainder on the same delta wire** — tag: `[implementation]`
   - **Inherits:** spec / ADR 0026: process died → open/resume with `stream(None)` (no new HumanMessage) on the same `thread_id` / `run_id` from the last complete node; process still running + refresh does **not** reconnect token-by-token (GET shows running, POST → 409); switching research threads is allowed and deltas stay bound to that `thread_id`; this increment’s UI does not start a second in-flight run
   - **Surface:** `host` stream/open path + `web/` running/resume
   - **Acceptance:** a scripted checkpoint after a complete `AIMessage` with `tool_calls` and before tool results: `stream(None)` does not append a HumanMessage, does not regenerate that model message, and the resume SSE carries later `thinking` / `answer` deltas with the same live-turn behaviour as T1 (not one dump after the remainder finishes); switching threads does not paint those deltas onto another `thread_id`
   - Status: [ ] pending
   - [blocks: T1]

3. **[parallel] Global thinking preference defaults on** — tag: `[implementation]`
   - **Inherits:** spec: a global UI preference maps off → `thinking.type=disabled`; default is on (omit / `adaptive`); official GPT Chat Completions expose no thinking text — if `FIN_BASE_URL` is OpenAI, the UI draws no thinking region; converter replay of `reasoning_details` on the next model call stays on (`reasoning_split` does not turn off with the preference)
   - **Surface:** `web/` chrome/composer + `host` send `extra_body`
   - **Acceptance:** a first-time visitor has thinking on; turning the preference off makes the next send use `disabled` and that run draws no thinking region; the control is a thinking on/off preference, not a step-by-step instruction to the model; reload keeps the preference
   - Status: [ ] pending
   - [parallel]

4. **After the run, thinking and tools collapse** — tag: `[implementation]`
   - **Inherits:** spec: while collapsed during the run, line-clamp a few lines; expand shows the full thinking text; after the run, collapse to a one-line duration label; reload restores that collapsed thinking text from the turn row; tools are separate light-gray (`#F5F5F4`) details, visible while that call runs, collapse after the run; GET after `done` includes collapsed thinking and tool summaries without bounce marker or retrieve JSON
   - **Surface:** `host` turn table + `web/` transcript
   - **Acceptance:** after a terminal `status`, thinking is a one-line duration that expands to the full thinking text; tool rows are collapsed; GET `/api/threads/{id}` still returns the thinking text and human-readable tool summaries; the body contains neither `HIDDEN_BOUNCE_MARKER` nor raw retrieve/read_file JSON
   - Status: [ ] pending
   - [blocks: T1]

5. **[parallel] Returning visit restores the last research thread** — tag: `[implementation]`
   - **Inherits:** spec: returning visit restores last `/t/{id}` when that id still exists; first visit: no threads, corpus in main, New thread and cards as starts; unknown `/t/{id}` is 404 + back to the list
   - **Surface:** `web/` shell / router
   - **Acceptance:** after opening a thread, a later visit to the workspace lands on that `/t/{id}` if the thread still exists; after delete (or unknown id), the visit is home/corpus, not a blank chat
   - Status: [ ] pending
   - [parallel]
