# Spec: workspace

## Objective

Ship **fin** as a product the operator can talk in: one local **workspace** web UI over **research threads** on the frozen 12 AAPL/MSFT/AMZN 10-Qs. Each send is one **run** on that `thread_id`. Thinking (when the model emits it) and tool calls stream on the turn then collapse. Citations sit on the assistant turn. Follow-ups reuse the thread. Reload restores history. Crash resume continues the in-flight **run** from the last complete host-graph node — not a new question, not a rolled-back user turn.

`POST /ask` and `python -m fin "q"` stay eval/script adapters on the same compiled **host graph**. This increment is one conversation slice, not another one-shot surface.

Quoted from `docs/CONTEXT.md`:

- **fin**: A bounded 10-Q research agent: the model chooses tools in a loop; code verifies citations; LangGraph is the host graph (loop, code gates, checkpoint/resume). Retrieve ranking stays off-graph behind the retrieve port.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run; follow-ups keep messages and citations. This is how fin is used after v1; it is not a chatbot identity and not a one-shot `/ask` demo.
- **workspace**: A local web UI server: list, open, resume, and delete a research thread; thinking (when the model emits it) and tool calls stream on the turn then collapse; citations sit on the assistant message. Streaming HTTP is this UI's transport on the same compiled graph, not a second product.
- **run**: One graph `invoke` on a thread (`run_id`): done / refuse / `step_cap` end the run; a follow-up is a new run on the same `thread_id`; crash resume continues the in-flight run; at most one run is active per thread.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **v1 corpus**: The frozen private fixture source SEC 10-Q slice at `/home/winner/projects/fin`: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3 (12 PDFs). Upstream commit `547f132`. Cross-period questions use 2023 Q1–Q3 only.

## Boundaries

- **Does:**
  - One FastAPI process: existing `POST /ask` (sync `invoke`) plus workspace routes and the built SPA. Same compiled host graph object as CLI (ADR 0013).
  - Frontend at repo-root `web/`: React 18 + TypeScript + Vite + TailwindCSS + Radix Primitives + Zustand. FastAPI serves `web/dist` same-origin. Vite dev proxies `/api` and `/ask`. Archived under `docs/design-references/prototype-workspace/`, the prototype variants are not product code and are not copied into `web/`.
  - Look: Soft Brutalism (`#FAFAF7`, 1px black border, single `#2563EB` accent). Left sidebar ~280px; active thread highlighted with the blue accent. Title from the first user question, max 50 characters + ellipsis. Cassette Futurism stays archive-only under `docs/design-references/cassette-futurism/`.
  - Chrome only: create, list, open, resume, delete. Path-addressable `/t/{thread_id}`. Catch-all after `/api` and `/ask` returns `index.html`. No rename, folders, search, Stop, or extra navigation chrome. Delete asks for confirmation (dialog, not a new surface).
  - Thread metadata (id, title, created_at) in a Postgres side table on `FIN_CHECKPOINT_URL`. Graph state stays in `PostgresSaver`. Delete removes the side row and `PostgresSaver.delete_thread` for that `thread_id`. Tests use `InMemorySaver` plus an in-process metadata stand-in.
  - Send path: `POST /api/threads/{thread_id}/messages` with JSON `{question}` returns `text/event-stream`. Client uses `fetch` (not native EventSource). Concurrent send on the same thread returns **409** `{error: concurrent_run}` **before** SSE headers. Composer is a textarea: Enter sends, Shift+Enter newline, empty submit is a no-op; disabled while that thread's run is in flight (no queue).
  - Token stream: `model_node` calls `model.stream()` (not blocking `invoke`). FastAPI wraps sync `_LockedGraph.stream()` in `StreamingResponse` of a **sync** iterator (threadpool). No `ainvoke`, no `astream`, no `AsyncPostgresSaver`.
  - MiniMax-M3 thinking (OpenAI-compat Chat Completions via `FIN_BASE_URL`): `ChatOpenAI.extra_body` sends `thinking: {type: adaptive|disabled}` and `reasoning_split: true`. Default is on (omit / `adaptive`). A global UI preference maps off → `disabled`. Converter copies `reasoning_details` onto the checkpointed `AIMessage` and replays it on the next model call (tool-loop continuity). Official GPT Chat Completions expose no thinking text; if `FIN_BASE_URL` is OpenAI, the UI draws no thinking region.
  - Citation verifier and user-visible `answer` events use thinking-stripped prose only. Hidden bounce marker never appears in SSE, GET history, or the SPA.
  - SSE event types on the wire: `thinking`, `tool_call`, `tool_result`, `answer`, `citations`, `status`. Citations are a terminal event on that run. No `idle` on the wire. HTTP 409 / 503 happen before the stream. `status` values are `done` | `refuse` | `step_cap` (plus a `running` signal on GET while in flight). Each workspace send mints and returns `run_id` (SSE `status` and the turn row).
  - Thinking UI: unframed, on the page background, not in the answer bubble and not the same bordered `<details>` as tools. Region appears only after the first `thinking` event. While collapsed during the run, line-clamp a few lines; expand shows the full text. After the run, collapse to a one-line duration label. Reload restores that collapsed thinking text from the turn row. Thinking tokens are stored on the checkpointed assistant message for **model replay** and are not stripped from `messages` sent back to the model.
  - Tools UI: separate light-gray (`#F5F5F4`) `<details>`. Human-readable one-liners (`list_files` → n filings [+ filters]; `retrieve` → query + hit count + locators; `read_file` → `document_id` p.N). Visible while that call runs; collapse after the run. Events carry name + short summary, not raw `ToolMessage` JSON. Correlate by tool call id.
  - Citations: chips inserted at each verified span inside the answer **and** a sources list under the turn (locator + quote; hover/focus/click-tap reveal quote). Empty list on `refuse` / `step_cap` is visible, with ordinary-person copy (could not ground in the frozen 10-Qs / hit the 32-step fuse) — not a blank crash.
  - Follow-up **run** resets run-scoped `evidence`, `step_count`, `repair_count`, `answer`, `citations`, `status`. `messages` keep accumulating via `add_messages`. `evidence` must be clearable (not `operator.add` + `[]`).
  - GET `/api/threads/{id}` returns the turn table (user text, assistant prose, `run_id`, status/reason, citations, collapsed thinking, tool summaries). Never dump checkpoint `SystemMessage` bounce templates or tool JSON as chat. `GET /api/corpus` is the real catalog shape `{document_id, company, period, version, page_count}` from `list_files` / `list_filings` (the function), the frozen 12, not prototype fake 2024 tickers.
  - Empty corpus bound: cards on **home** (no thread selected) and on an **empty research thread**. Click = create thread + **prefill** a question, not auto-send.
  - Crash resume (node-level, same algorithm as a complete-node save point): a node is complete only after it has finished writing. `model_node` complete ⇒ full `AIMessage` including complete `tool_calls` is checkpointed; resume continues at `tool_node` and does **not** regenerate that model message. `tool_node` complete ⇒ full `ToolMessage`s are checkpointed; resume continues at the next model call. Mid-token or mid-tool-output is not a save point; that node re-runs. The user HumanMessage is never deleted.
    - Process still running, operator refreshes: no live token-stream reconnect this increment. GET shows the thread as running; composer disabled; POST → 409. When the run ends, GET shows the completed turn.
    - Process died: open/resume the thread with `stream(None)` (no new HumanMessage) on the same `thread_id` / `run_id` from the last complete node, and SSE the remainder until `done` / `refuse` / `step_cap`.
  - Switching **research threads** while one run streams is allowed. Deltas stay bound to that `thread_id` (never paint onto another thread). This increment's UI is one in-flight stream: starting a second run on another thread while the first still streams is out. Returning to a still-running thread shows running (or the completed turn if it finished).
  - Bind `0.0.0.0:8000`, no auth, no cookies. Document as a local operator server (WSL). 503 `{error: ingest_missing}` if the extracted tree is missing. Unknown `/t/{id}` is an ordinary 404 + back to the list.
  - First visit: no threads, corpus in main, New thread and cards as starts. Returning visit restores last `/t/{id}` when that id still exists.
  - Tests: `uv run pytest -q` covers thread CRUD, SSE (scripted model + InMemorySaver), 409-before-headers, follow-up reset, node-level resume, hidden-bounce absence, corpus shape. Scripted model covers tool/status/citation/refuse; thinking-delta tests use a fake chunk mapper (or an extended scripted stream) that round-trips `reasoning_details` / additional_kwargs. Frontend gate: `tsc --noEmit` in `web/`. Nine gold eval cases stay on CLI / `POST /ask`.
- **Confirms with human:** none remaining (assumption gate + architecture/interaction review + node-level resume confirmed 2026-08-22).
- **Out of this spec:**
  - Interactive CLI REPL; hosted accounts / multi-user SaaS; auth.
  - Live EventSource / Last-Event-ID reconnect of an in-process token stream (refresh does not resume word-by-word; the run itself still continues or resumes from the node checkpoint).
  - Starting a second in-flight run on another thread in this UI increment.
  - Long-thread compaction / token budget; operator-owned filings / ingest-as-product.
  - HITL plan-approval or tool-approval UX; host-graph rewrite; MCP client to iknow-memory.
  - Rename, folders, thread search, Stop button, queued follow-up.
  - Playwright — except the two browser e2e lanes named in the Amendment 2026-08-26 below; everything else the original Playwright exclusion covered stays out. Merging `prototype_workspace` into `main` as the product stays out.
  - `AsyncPostgresSaver` / `ainvoke` as the durable path.

## Success Criteria

- `uv run pytest -q` exits 0 (no `eval` mark; no network LLM). `web/` `tsc --noEmit` exits 0.
- Workspace FastAPI app and CLI `compile_for_runtime` / `compile_for_http` share one compiled graph object (identity or the same compile helper as v1). `POST /ask` still calls sync `invoke`, not `ainvoke`.
- `POST /api/threads/{id}/messages` with a scripted model that emits content in chunks yields `Content-Type: text/event-stream` frames whose `type` is one of `thinking` | `tool_call` | `tool_result` | `answer` | `citations` | `status`. A second concurrent POST on the same `thread_id` returns 409 `{error: concurrent_run}` with no `text/event-stream` body.
- Scripted model.stream path: `thinking` deltas never appear inside `answer` events; `verify_citations` is called with thinking-stripped prose; a ≥32-char evidenced span still ends `done` with code-filled citations.
- `GET /api/threads/{id}` after `done` returns the user turn, assistant prose, `run_id`, citations, collapsed thinking text, and tool summaries; the body contains neither `HIDDEN_BOUNCE_MARKER` nor raw retrieve/read_file JSON.
- Follow-up POST on the same `thread_id`: `step_count` and `repair_count` start at 0 and `evidence` does not contain the previous run's pages; a scripted answer that only quotes the previous run's pages (not re-fetched this run) is not `done`.
- Node-level resume: a scripted run checkpointed after `model_node` has written an `AIMessage` with `tool_calls` and before `tool_node` returns; `stream(None)` on that `thread_id` does not append a HumanMessage, does not regenerate that `AIMessage`, executes the tool node, and reaches a terminal `done` | `refuse` | `step_cap`.
- `GET /api/corpus` returns exactly 12 filings with keys `document_id`, `company`, `period`, `version`, `page_count`; ids match the frozen manifest (e.g. `AAPL-2022Q3` family), not prototype `AAPL-10Q-Q3-2024`.
- `DELETE /api/threads/{id}` then `GET` that id is 404; a subsequent compile on that `thread_id` does not restore the deleted title or turns.
- SPA: `/` renders the home/corpus state; `/t/{existing-id}` restores that thread's turns without re-invoking; `/t/{unknown}` is not a blank chat (404 + back to list). Soft Brutalism tokens (`#FAFAF7`, 1px black, `#2563EB`) are the theme; thinking is unframed relative to the answer bubble; tools use `#F5F5F4` details.
- Ingest-missing workspace send returns 503 `{error: ingest_missing}`. `refuse` / `step_cap` SSE `citations` is `[]` and GET history shows empty sources plus non-empty assistant copy.

## Open Questions

(none — assumption gate and resume-grain confirmation cleared all)

## Inherits / Changes

**Inherits (workspace today):** v1 host agent (`specs/v1-host-agent.md`); frozen 12 PDFs + extracted tree (ADR 0004, 0020); retrieve port + BM25 (ADR 0001, 0007); host graph authored in fin (ADR 0019); citation gate + hidden bounce (ADR 0008, 0021); tool contracts (ADR 0009); CLI-first + HTTP same graph, sync `invoke` for `/ask` (ADR 0013); `ChatOpenAI` + `FIN_MODEL` / `FIN_BASE_URL` default `MiniMax-M3` (ADR 0022); `PostgresSaver` / test `InMemorySaver` (ADR 0015); `thread_id` session / **run** = one invoke / crash resume of the in-flight run (ADR 0016, 0018); conversation slice is a workspace web UI, no CLI REPL (ADR 0023, 0024); ordinary-person chrome and stream-then-collapse (ADR 0025); workspace SSE + node-complete resume (ADR 0026). Capability packages remain `src/fin/corpus/`, `src/fin/retrieve/`, `src/fin/host/`. Frontend lives at `web/` as the workspace surface of **host**, not a new technical-layer package (`controllers/` / `services/` / …).

**Confirmed stack (assumption gate):** Python 3.12, uv, pytest; FastAPI one process; React 18 + TypeScript + Vite + TailwindCSS + Radix + Zustand; POST `fetch` SSE; `0.0.0.0:8000` no auth; MiniMax `extra_body` thinking + `reasoning_split`; node-level checkpoint resume; turn table for GET history.

**Changes this spec adds:** workspace routes + SPA; `model_node` token stream; thinking transport converter; run-scoped state reset; thread metadata + turn rows; node-level `stream(None)` resume on the workspace send/open path; Soft Brutalism UI contract above. `/ask` JSON contract stays. v1 spec sentence that HTTP has “no streaming” is superseded for **workspace routes only**.

## Amendment 2026-08-26: browser e2e lanes (plans/workspace-stream-fidelity.md T5)

This amendment is the outcome of `plans/workspace-stream-fidelity.md` T5 (`[decision]`, gates T6–T8). It supersedes the "Playwright" line under **Out of this spec** for exactly two named lanes; it does not touch checkpoint grain, SSE event types, or any other shipped contract, and the default test contract above (`uv run pytest -q` = scripted model + `InMemorySaver`, no network LLM) is unchanged.

**In scope (new):**

- **Scripted-provider browser e2e lane** — a browser is driven against a locally served workspace booted on the scripted model + `InMemorySaver`: no network LLM, no credentials, deterministic. It runs only via its explicit opt-in command (and any CI lane wired to that same command); default `uv run pytest -q` never invokes a browser.
- **Live-provider smoke lane** — a browser (or the same runner) sends at least one real question through the configured provider. It runs only via its explicit opt-in command and is **never** part of default `uv run pytest -q`. When `FIN_API_KEY` is unset, the lane aborts non-zero with an explicit message naming the missing variable and runs nothing — never a silent skip that reads as a pass.

**Still out (unchanged from the original exclusion):** Playwright (or any browser harness) as a dependency or collection target of default `uv run pytest -q`; browser tests replacing the scripted-model pytest contract above; any browser lane beyond the two named here. Runner filenames, harness library choice, and per-check assertion lists are not fixed by this amendment — the implementation bullets (`plans/workspace-stream-fidelity.md` T6–T8) own those.

## Planned files (ACR input, not a filename freeze)

Host surface: `src/fin/host/http.py`, `src/fin/host/graph.py`, `src/fin/host/state.py`, `src/fin/host/compile.py`, `src/fin/host/scripted_model.py`, plus new host-owned modules for thread metadata / turn rows / SSE mapping / thinking converter as the implementer splits them. Tests under `tests/host/`. SPA under `web/`. No copy of `docs/design-references/prototype-workspace/` into product paths.

## 待写入 (persist after ACR)

Flushed 2026-08-22: **workspace** and **run** updated in `docs/CONTEXT.md`; ADR **0026** accepted.

CONTEXT / ADR only. Spec body does not author those files.

## Architecture change reviewer

bounded-context-guardian: yes — workspace is a **host** surface (HTTP + `web/` SPA) on the existing host graph; corpus/retrieve stay behind their packages; no `controllers/` / `services/` / `repositories/` / `models/` top-level dirs; `web/` is the UI of workspace, not a second agent.
defensive-contract-validator: yes — empty question no-op; unknown thread 404; concurrent same-thread 409; ingest-missing 503; follow-up must not inherit prior-run evidence; node-level resume after model-complete / tool-incomplete; stream disconnect does not delete the HumanMessage; thinking-in-content must not pass the citation gate as the answer.
error-handling-enforcer: yes — typed `ConcurrentRunError` / `IngestMissingError` / thread-not-found; 409 decided before SSE headers; hidden bounce never on a user surface; no empty catch on stream mapping; refuse/step_cap are terminal statuses with explicit copy, not swallowed blanks.
complexity-anti-drift: yes — declared structure keeps stream mapping, thinking converter, turn persistence, and graph nodes as separate modules; `model_node` streams then returns one complete `AIMessage`; UI splits thinking / tools / answer / citations rather than one god transcript component.
minimal-change-verifier: yes — one logical conversation increment; implementation splits into tracer-bullet commits in writing-plans; this spec is one contract; `/ask` and the host graph’s citation/tool contracts stay unless a bullet must reset run-scoped state (that reset is in-scope, not a drive-by refactor).

All 5 verdicts yes.
