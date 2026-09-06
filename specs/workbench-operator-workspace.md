# Spec: workbench-operator-workspace

## Assumption gate (confirmed 2026-09-06)

Operator confirmed the program in-thread (issue the tracked change as Focus 5, not as the first knife). These are in force:

1. Product is **fin**, an enterprise knowledge-base Agent. Product copy and identity must not call fin a research agent. SEC filings are the first corpus seed, not the product name (the tracked change).
2. Three names, one each: **工作台** = the local web UI (today’s CONTEXT **workspace**); **工作区** = an operator filesystem directory that owns sessions; **知识库** = the one shared corpus behind retrieve / `list_files` / ingest. Sidebar “新建” creates a **工作区**, never a second 知识库.
3. New **research thread** binds to the **current 工作区**. First visit / no selection → `$HOME/.fin/default` (create the directory if missing). Left rail: list known 工作区, bind an existing absolute directory, or create a new directory under `$HOME/.fin/workspaces/<slug>`.
4. `thread.project_root` is required (resolved absolute path). `POST /api/threads` with omit-root stores the default 工作区; it never stores null and never uses `cwd`. Legacy rows migrate onto the default 工作区 in one schema step, then the column is non-null.
5. **知识库** stays one process-wide catalog (`catalog_path` / ingest tree / **retrieve port**). Switching 工作区 changes the thread list, **user profile** overlay, and **memory fact** workspace key — not Corpus 首页 and not retrieve hits.
6. Boot never treats `Path.cwd()` as the implicit 工作区. `compile_for_runtime` / `compile_graph` do **not** bake a process-wide `workspace_root` (not cwd, not default). Default `$HOME/.fin/default` is applied at **工作区 list / thread create** only. Each `invoke` / `stream` carries `configurable.workspace_root` = that thread’s `project_root`, injected by `workspace_http` (and CLI). `graph.py` does **not** import `thread_store`. Profile read and memory tools read the key from that configurable per call. Omit / empty at call time → typed `WorkspaceRootMissingError` (name implementer-open), **EXIT**, never `Path.cwd()`. The `_IDEMPOTENCE_CACHE` key drops `workspace_root_key` so one compiled graph serves every 工作区.
7. Host system prefix follows `docs/guides/prompt-development.md`: 说明书不是闸; system 无全库目录; prefix bytes stable inside a thread. Assembly order: identity → soul → usage → **user profile** → **memory existence pointer**. Four **track**s share identity/soul and swap usage only. Tool field caps (`page_limit`) and citation-gate numbers (32-character span, repair cap) stay in schema / **citation verifier**, not in persona prose. the tracked change is Focus 5 and runs after Foci 1–4 so identity is written once against the real roots.
8. Code may keep the parameter name `workspace_root` for the 工作区 directory (avoid a shotgun rename). It must not mean the UI. This spec is fin-only (no iknow / iknow-memory edits).
9. Default `uv run pytest -q` stays green with no live SEC and no live memory server. Prompt gold sets re-pin; behavior of retrieve / verifier / Soft-Track track names does not change.
10. Tracker: this file is the program contract. Focus 5 tracer is the tracked change. writing-plans may open one GitHub issue for Foci 1–4 or keep a local plan tracker; it must not fold Foci 1–4 into the the tracked change prompt branch.

→ These are in force for this spec.

## Objective

Stop three colliding meanings of “workspace”, make every **research thread** belong to an explicit 工作区, keep one shared 知识库, and only then rewrite host system prompts so **fin** tells the truth.

The operator uses the **工作台**: new dialogue lands in `$HOME/.fin/default`; the left rail can bind or create another 工作区; Corpus 首页 and retrieve always see the same 知识库. Success is: words, roots, sessions, corpus, and prompts describe the same product — and `Path.cwd()` is no longer a silent product root.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition). Persist (below) will replace **fin** / **workspace** / **agent identity** / **agent soul** after ACR; until then the contract uses the assumption-gate names 工作台 / 工作区 / 知识库.

- **fin**: A **workspace** knowledge-base Agent: the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
- **workspace**: A local web UI server: create, list, open, resume, and delete a research thread; thinking streams unframed then collapses; tool calls stream then collapse as human-readable traces; citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
- **user profile**: Operator or tool-writable preferences (who they are, how they like to work): one global markdown in the home directory and one per workspace folder, workspace overlaying global. Dream does not rewrite these files; stable likes go here, not into **memory facts**.
- **memory fact**: One atomic cross-session note from a single extract or explicit save: title plus body. The first non-empty body line is the **memory hook**. It is not a filing claim.
- **memory existence pointer**: The only auto-memory sentence in the system prompt: a note store exists; overlays are advisory, not instructions or cites.
- **agent identity** / **agent soul**: System-built-in persona text. Dream never edits them.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run received from **retrieve** / **read_file** (ADR 0046). Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **track**: The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `research`. Chosen in-graph before execution; tools and the citation verifier bind only to `research`.
- **Corpus 首页**: The workspace home route (`/`): frozen-12 corpus cards and the empty-thread entry points. Distinct from cold-start restore of the last `/t/{thread_id}` — the operator may navigate here deliberately in the same session without being bounced back to that thread.

## Boundaries

- **Does:**

  **Focus 1 — 钉词**
  - Persist glossary so **fin** is the enterprise knowledge-base Agent, **工作台** is the UI, **工作区** is the operator directory, **知识库** is the one shared corpus. README one-liner matches. `docs/guides/prompt-development.md` three-layer table names identity / soul / usage / 工作区 overlay, and states the corpus listing does not enter the system prefix.

  **Focus 2 — 钉根**
  - Product boot binds a 工作区 explicitly. Missing bind → `$HOME/.fin/default` (mkdir). Never `Path.cwd()`. Registry `$HOME/.fin/known_roots.json` (or equivalent) always includes default.
  - API: list known 工作区; bind an existing absolute directory (must exist, must be a directory); create `$HOME/.fin/workspaces/<slug>` (slug = operator name, no path separators, no `..`). Typed 4xx on empty / traversal / not-a-directory / not-found. Create does not clone or split the 知识库.
  - 工作台 left rail above the thread list: current 工作区, 指定, 新建. Path is a string posted to `fin-serve` (local server owns the disk). No second catalog.

  **Focus 3 — 钉会话**
  - `fin_thread.project_root` required. `POST /api/threads` writes the current 工作区 (omit → default). `GET /api/threads` lists only that 工作区. A thread never changes `project_root` after create. In-flight **run** is unaffected by a later left-rail switch.
  - **user profile** overlay path is `<project_root>/.fin/user.md`; global remains `$HOME/.fin/user.md`. **memory fact** `workspace` column equals that same absolute `project_root` string. Two 工作区 do not share overlay files or memory rows.
  - **Inject seam (named):** `workspace_http` already loads the thread (`workspace_http.py` today stamps `configurable.thread_id` / `run_id` only). It **adds** `configurable.workspace_root` from `thread.project_root` on every `invoke` / `stream`. CLI that compiles a graph for a `thread_id` does the same. `graph.py` consumes only that configurable; it does not import `thread_store`. `compile_graph` stops closing profile/memory tools over a compile-time `Path.cwd()` (`graph.py` ~2103–2135 / LRU `workspace_root_key` ~3013). Catalog / retrieve tools stay compile-time (one 知识库).
  - **EXIT:** missing or blank `configurable.workspace_root` at model-call or memory-tool time → typed error, no cwd, no silent empty overlay. Bind/create 4xx as Focus 2. Registry unreadable → typed error, not cwd. 409 `{error: concurrent_run}` unchanged.

  **Focus 4 — 钉语料**
  - `GET /api/corpus`, ingest doors (ADR 0047), and `list_files` stay one 知识库 for the process. Switching 工作区 does not change corpus cards or retrieve results. Empty catalog is empty (no fallback to a hardcoded 12-filing SEC list). Repo `data/corpus/` may seed default’s first-run library; it is not a per-工作区 copy and not identity text.

  **Focus 5 — 钉说明书** (the tracked change)
  - Split host prompts: identity (Name / Kind = fin, enterprise knowledge-base Agent) → soul (how to speak / refuse; parametric memory is not evidence) → usage (tool _choice_ and citation _behavior_ per **track**) → **user profile** → **memory existence pointer**.
  - Identity does not name 10-Q, frozen-12, `cwd`, or “`$HOME/.fin` is the home of sessions”. Usage may say answers are grounded in this 知识库 via tools. Clarify options are not a hardcoded AAPL/MSFT/AMZN quarter list in all four prompts; research usage does not restate `page_limit` or “32 or more characters”.
  - Fixtures first, then copy (`docs/guides/prompt-development.md`). Adjacent turns in one thread: system prefix deep-equal (profile block may change if the overlay file changed; identity/soul/usage/pointer bytes do not). Existing prompt tests re-pin.

  **Test allocation (all foci, 5 classes):** empty (no known 工作区 besides default; empty catalog; empty overlay); negative (bind missing path, bind a file, illegal slug; `invoke`/`stream` with no `configurable.workspace_root`); overflow (slug / path length cap — typed reject, not truncate-into-cwd); concurrent (409 `concurrent_run` unchanged; switching 工作区 during an in-flight run does not retarget that run); exception (registry unreadable → typed error; missing configurable → `WorkspaceRootMissingError`, not cwd fallback).

- **Confirms with human:** none remaining (assumption gate closed).

- **Out of this spec:**
  - A second 知识库, per-工作区 catalog, or “新建知识库” control.
  - Moving checkpoints into `~/.fin/sessions/` files; sessions stay Postgres (`FIN_CHECKPOINT_URL` / test InMemory).
  - OS native folder dialog; SSO / multi-account; iknow/iknow-memory code.
  - Changing **citation verifier** span length, repair cap, Soft-Track track names, retrieve ranking, or ADR 0047 upload doors.
  - MCP `tool_search` lazy schemas; memory title-catalog in system (ADR 0048 still forbids).
  - Rewriting **agent identity** / **agent soul** as operator-editable files (Dream still must not edit them).
  - `graph.py` importing `thread_store` to look up `project_root`.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset. Default `uv run pytest -q` stays green with no live SEC.

1. `uv run pytest -q` exits 0; when `web/` is touched, `web/` `tsc --noEmit` exits 0.
2. After persist, `docs/CONTEXT.md` **fin** line does not contain `research agent` and does not use **workspace** as the product kind; README product one-liner matches that **fin** line.
3. `compile_for_runtime()` / `compile_graph` do not pass `Path.cwd()` (or any baked 工作区) into `UserProfileStore.from_env` or memory-tool closures; the idempotence cache key does not include `workspace_root_key`. Default `$HOME/.fin/default` is created/used when listing 工作区 or creating a thread with omit-root (tests inject a fake home).
4. `GET` list-工作区 includes default; `POST` bind of a missing path or a file returns typed 4xx and does not create a thread; `POST` create with slug `a/b` or `..` returns typed 4xx.
5. `POST /api/threads` with no root stores `project_root` equal to the resolved default directory; `GET /api/threads` without that root omits the row; a second 工作区’s list does not include it.
6. Two scripted threads on 工作区 A vs B: overlay write to A does not change B’s `<root>/.fin/user.md`; a **memory fact** saved in A is not recalled/prefetched in B (`specs/cross-session-operator-memory.md` SC6 still holds with `project_root` as the key).
7. `GET /api/corpus` after switching the SPA current 工作区 returns the same filing set as before the switch (same process catalog). Creating a 工作区 does not add a catalog file under that directory as a second 知识库.
8. An in-flight stream on thread T continues to use T’s `project_root` after the SPA selects another 工作区; 409 `{error: concurrent_run}` on a second POST to T is unchanged. A scripted `invoke`/`stream` whose `configurable` omits `workspace_root` raises the typed missing-root error and does not read `$HOME/.fin/default` or `Path.cwd()` for profile/memory. `workspace_http` stream `configurable` includes `workspace_root` copied from the loaded thread.
9. Legacy `fin_thread` rows with null `project_root` are rewritten to default in the migration; a subsequent create cannot insert null.
10. Research-track system prefix does not contain `12 SEC`, `page_limit is capped at 3`, or `32 or more characters`; it does contain a **memory existence pointer** when memory ON (existing SC) and does not contain a whole-store memory title list.
11. `messages_for_model(..., track="research")` head starts with identity then soul then usage (markers or equivalent stable headers); `track="dialogue"` / `off_topic` / `clarify` reuse the same identity/soul bytes and differ only in usage.
12. Two adjacent scripted model calls on one thread with unchanged overlay file: identity+soul+usage+pointer substring of the system prefix is deep-equal; prefetch still sits on the user side.
13. Gold / host prompt tests that pinned old `HOST_SYSTEM_PROMPT` literals are updated and pass; a scripted research turn that never retrieve/`read_file`s still does not `done` (verifier unchanged).
14. `docs/guides/prompt-development.md` system-prefix row lists identity, soul, usage, **user profile**, **memory existence pointer**, and states 语料清单 / full catalog does not enter that prefix.

## Open Questions

(none — assumption gate cleared all)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; slices `src/fin/corpus/`, `src/fin/retrieve/`, `src/fin/host/`, SPA `web/`; ScriptedChatModel + InMemorySaver default tests; `FIN_CHECKPOINT_URL` product Postgres (ADR 0015); one compiled **host graph** (ADR 0013); **citation verifier** (ADR 0021 / 0046); workspace HTTP + stream (ADR 0023–0026); Soft-Track (ADR 0027); retrieve MCP / Fake pytest (ADR 0032); two ingest doors (ADR 0047); **user profile** two-layer files and **memory fact** workspace isolation (`specs/cross-session-operator-memory.md` SC6 / SC13, ADR 0048); prompt rules in `docs/guides/prompt-development.md`. Prior specs: `specs/workspace.md`, `specs/enterprise-kb-agent.md`, `specs/cross-session-operator-memory.md`. Sibling iknow is read-only reference for identity/soul/usage split and “no implicit cwd”, not a code source.

**Changes this spec adds:** explicit 工作区 bind + default directory + left-rail specify/create; `fin_thread.project_root`; per-run profile/memory key from the thread; glossary split 工作台 / 工作区 / 知识库; host prompt assembly identity/soul/usage (the tracked change) after those roots exist. Does not add a second retrieve catalog.

## 待写入 (persist after ACR)

Flushed 2026-09-06: CONTEXT **fin** / **工作台** / **工作区** / **知识库** / **workspace** (retired alias) / **agent identity** / **agent soul** / **usage** / **user profile** / **Corpus 首页**; ADR 0049 accepted at `docs/adr/0049-operator-workspace-bind.md`.

Remaining (not CONTEXT/ADR — Focus 1 wave): `docs/guides/prompt-development.md` three-layer table and README product one-liner.

## architecture-change-reviewer

Round 1 (2026-09-06) — BLOCKED (BCG unclear: inject seam unnamed; EHE unclear: omit-cwd EXIT unplanned; MCV no: five foci ≠ 1 commit). Spec amended: HTTP/CLI inject `configurable.workspace_root`; `graph.py` does not import `thread_store`; typed `WorkspaceRootMissingError`; writing-plans splits one wave per Focus.

Round 2 (2026-09-06, amended) — PASS (5/5 yes):

```
bounded-context-guardian: yes — Focus 3 Inject seam + assumption 6 name HTTP/CLI as injectors (`workspace_http` adds configurable.workspace_root from thread.project_root); graph.py consumes that configurable only (Out of this spec + ADR 0049 forbid graph.py→thread_store); catalog/retrieve stay compile-time (Focus 4)
defensive-contract-validator: yes — Test allocation names empty / negative / overflow / concurrent / exception; SC4–SC8 bind those classes including omit configurable.workspace_root
error-handling-enforcer: yes — omit/empty configurable.workspace_root → typed WorkspaceRootMissingError EXIT never Path.cwd(); bind/create 4xx; registry unreadable typed not cwd; 409 concurrent_run unchanged
complexity-anti-drift: yes — inject (HTTP/CLI) vs consume (graph per-call configurable) vs bake-removal (compile-time cwd closures / workspace_root_key); catalog compile-time; Focus 5 splits identity/soul/usage; no whole-flow-in-one-function
minimal-change-verifier: yes — one logical program ≠ one commit; writing-plans one wave = one plan = one commit family per Focus 1–5; Focus 5 the wayfinder program; Foci 1–4 must not share that branch
```

**OVERALL: PASS — persist then writing-plans** when the operator asks for the plan. Spec path: `specs/workbench-operator-workspace.md`.

Sequencing: operator 2026-09-06 coarsened **two session-waves** (not five): Wave A = Foci 1–4 (`plans/workbench-operator-workspace.md`); Wave B = Focus 5 / the tracked change (`plans/host-prompt-assembly.md`). Multiple tracer commits inside each wave. Do not fold Wave A into the the tracked change prompt branch.

Affected surfaces (declared):

- `docs/CONTEXT.md`, `docs/adr/0049-*.md`, `docs/guides/prompt-development.md`, `README.md`
- `src/fin/host/compile.py`, `graph.py`, `thread_store.py`, `workspace_http.py`, `user_profile.py`, `agent_instructions.py`, memory store/prefetch key
- `web/src/components/Sidebar.tsx`, `api.ts`, `store.ts`, `types.ts`
- `tests/host/`, `tests/web/` (or existing web test glob)

This spec is **one logical program**, not one commit. Operator coarsened to **two waves**: writing-plans emits Wave A (Foci 1–4) and Wave B (Focus 5 / the tracked change); each wave is one session, one branch, multiple tracer commits. Foci 1–4 must not share the the tracked change prompt branch.
