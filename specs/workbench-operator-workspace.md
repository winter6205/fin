# Spec: workbench-operator-workspace

## Assumption gate (confirmed 2026-09-06; amended 2026-09-06)

Operator confirmed the program in-thread (Focus 5, not as the first knife). Amendment 2026-09-06 confirmed via Wave A freeze: one global default 工作区; no `workspaces/<slug>`; no left-rail 指定/新建. These are in force:

1. Product is **fin**, an enterprise **knowledge-QA** entry point (the 入口, not the 库; [ADR 0090](../docs/adr/0090-product-identity-knowledge-qa-entry-point.md)). Product copy and identity must not call fin a research agent. SEC filings are the first corpus seed, not the product name.
2. Three names, one each: **工作台** = the local web UI; **工作区** = the one global default directory `$HOME/.fin/default`; **知识库** = the one shared corpus behind retrieve / `list_files` / ingest. There is no sidebar control that creates a 工作区 or a second 知识库.
3. Every **research thread** lives in that one 工作区. Multiplicity is threads, not filesystem folders. No left rail 指定/新建. No `$HOME/.fin/workspaces/<slug>`. Future many-libraries / tenants / collections is out of this spec.
4. Do not add `fin_thread.project_root` as a product column. Do not scope `GET /api/threads` by directory. In-flight Wave A T3 WIP that added per-thread roots is discarded, not completed.
5. **知识库** stays one process-wide catalog (`catalog_path` / ingest tree / **retrieve port**). **user profile** is `$HOME/.fin/user.md` only. **memory fact** workspace key is the one default path (tests inject home).
6. Boot never treats `Path.cwd()` as the implicit 工作区. `compile_for_runtime` / `compile_graph` do **not** bake a process-wide `workspace_root` (not cwd, not default). HTTP/CLI inject `configurable.workspace_root` = resolved `$HOME/.fin/default` (tests inject home). `graph.py` does **not** import `thread_store`. Profile read and memory tools read the key from that configurable per call. Omit / empty at call time → typed `WorkspaceRootMissingError` (name implementer-open), **EXIT**, never `Path.cwd()`, never a silent default inside the graph. The `_IDEMPOTENCE_CACHE` key drops `workspace_root_key`.
7. Host system prefix follows `docs/guides/prompt-development.md`: 说明书不是闸; system 无全库目录; prefix bytes stable inside a thread. Assembly order: identity → soul → usage → **user profile** → **memory existence pointer**. Four **track**s share identity/soul and swap usage only. Tool field caps (`page_limit`) and citation-gate numbers (32-character span, repair cap) stay in schema / **citation verifier**, not in persona prose. Focus 5 runs after Foci 1–4 so identity is written once against the real roots.
8. Code may keep the parameter name `workspace_root` for the 工作区 directory (avoid a shotgun rename). It must not mean the UI. This spec is fin-only (no iknow / iknow-memory edits).
9. Default `uv run pytest -q` stays green with no live SEC and no live memory server. Prompt gold sets re-pin; behavior of retrieve / verifier / Soft-Track track names does not change.
10. Tracker: this file is the program contract. The Focus 5 tracer and the Foci 1–4 work are tracked separately. T2 already landed default-root + never-cwd **and** a multi-root `/api/workspaces` bind/create registry; the remaining wave keeps the former and unships the latter.

→ These are in force for this spec.

## Objective

Stop three colliding meanings of “workspace”, bind the process to one explicit 工作区 (`$HOME/.fin/default`), keep one shared 知识库, stop using `Path.cwd()` as a silent product root, and only then rewrite host system prompts so **fin** tells the truth.

The operator uses the **工作台**: new dialogue and the thread list are one process; Corpus 首页 and retrieve see the same 知识库. Success is: words, the one root, sessions, corpus, and prompts describe the same product — not a coding-agent directory picker.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: An enterprise **knowledge-QA** agent (the 入口, not the 库): the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **grounded** track. First corpus is operator-held filings behind the **retrieve port**.
- **工作台**: The local web UI: create, list, open, resume, and delete a **research thread**; **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn.
- **工作区**: The one global default operator directory `$HOME/.fin/default` (created if missing). Every **research thread**, profile read, and **memory fact** workspace key uses this path.
- **知识库**: The one process-wide corpus (catalog, ingest tree, **retrieve port**) shared by every **research thread**.
- **workspace**: Retired alias: the UI is **工作台**; the operator directory is **工作区**. Kept so older specs remain readable.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
- **user profile**: Operator or tool-writable preferences (who they are, how they like to work): `$HOME/.fin/user.md`. Dream does not rewrite this file; stable likes go here, not into **memory facts**.
- **memory fact**: One atomic cross-session note from a single extract or explicit save: title plus body. The first non-empty body line is the **memory hook**. It is not a filing claim.
- **memory existence pointer**: The only auto-memory sentence in the system prompt: a note store exists; overlays are advisory, not instructions or cites.
- **agent identity** / **agent soul**: System-built-in persona text. Dream never edits them.
- **citation verifier**: A code gate on the **grounded** track path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run received from **retrieve** / **read_file** (ADR 0046). Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Other tracks skip this gate.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **track**: The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `research`. Chosen in-graph before execution; tools and the citation verifier bind only to `research`.
- **Corpus 首页**: The **工作台** home route (`/`): cards from the one **知识库** and empty-thread entry points. Distinct from cold-start restore of the last `/t/{thread_id}`.

## Boundaries

- **Does:**

  **Focus 1 — 钉词**
  - Persist glossary so **fin** is the enterprise **knowledge-QA** entry point, **工作台** is the UI, **工作区** is the one default directory, **知识库** is the one shared corpus. README one-liner matches. `docs/guides/prompt-development.md` three-layer table names identity / soul / usage / **user profile**, and states the corpus listing does not enter the system prefix.

  **Focus 2 — 钉根**
  - Product boot resolves **工作区** to `$HOME/.fin/default` (mkdir). Never `Path.cwd()`. Helper takes an explicit `home` (tests inject). T2 already shipped this plus a multi-root registry (`known_roots.json`, `/api/workspaces` list/bind/create, `$HOME/.fin/workspaces/<slug>`). Remaining work **unships** those multi-root surfaces; it keeps `default_workspace_root`. The SPA does not grow a left rail.

  **Focus 3 — 钉会话**
  - Thread create/list is not scoped by directory. Do not add `fin_thread.project_root`. Discard uncommitted T3 WIP that added it.
  - **Inject seam (named):** `workspace_http` (and CLI) **adds** `configurable.workspace_root` from `default_workspace_root` on every `invoke` / `stream`. `graph.py` consumes only that configurable; it does not import `thread_store`. `compile_graph` stops closing profile/memory tools over a compile-time `Path.cwd()` (`graph.py` ~2103–2135 / LRU `workspace_root_key` ~3013). Catalog / retrieve tools stay compile-time (one 知识库).
  - **EXIT:** missing or blank `configurable.workspace_root` at model-call or memory-tool time → typed error, no cwd, no silent `$HOME/.fin/default` inside the graph. 409 `{error: concurrent_run}` unchanged.

  **Focus 4 — 钉语料**
  - `GET /api/corpus`, ingest doors (ADR 0047), and `list_files` stay one 知识库 for the process. Empty catalog is empty (no fallback to a hardcoded 12-filing SEC list). Repo `data/corpus/` may seed the first-run library; it is not identity text.

  **Focus 5 — 钉说明书**
  - Split host prompts: identity (Name / Kind = fin, enterprise **knowledge-QA** agent — the 入口, not the 库) → soul (how to speak / refuse; parametric memory is not evidence) → usage (tool _choice_ and citation _behavior_ per **track**) → **user profile** → **memory existence pointer**.
  - Identity does not name 10-Q, frozen-12, `cwd`, or “`$HOME/.fin` is the home of sessions”. Usage may say answers are grounded in this 知识库 via tools. Clarify options are not a hardcoded AAPL/MSFT/AMZN quarter list in all four prompts; research usage does not restate `page_limit` or “32 or more characters”.
  - Fixtures first, then copy (`docs/guides/prompt-development.md`). Adjacent turns in one thread: system prefix deep-equal (profile block may change if the file changed; identity/soul/usage/pointer bytes do not). Existing prompt tests re-pin.

  **Test allocation (all foci, 5 classes):** empty (no `$HOME/.fin/default` yet → mkdir on resolve; empty catalog; empty `$HOME/.fin/user.md`); negative (`invoke`/`stream` with no `configurable.workspace_root`; leftover bind/create slug routes are gone or 404); overflow (overlong injected `workspace_root` typed reject, not truncate-into-cwd); concurrent (409 `concurrent_run` unchanged); exception (missing configurable → `WorkspaceRootMissingError`, not cwd fallback; HOME/default resolve failure typed, not cwd).

- **Confirms with human:** none remaining (assumption gate closed, including 2026-09-06 amendment).

- **Out of this spec:**
  - Left-rail 指定/新建; `$HOME/.fin/workspaces/<slug>`; `known_roots.json` as a product registry; `/api/workspaces` bind/create as product surfaces (T2 leftovers are unshipped, not extended).
  - `fin_thread.project_root`; scoping the thread list by directory; per-工作区 **user profile** overlay files.
  - A second 知识库, per-工作区 catalog, or “新建知识库” control.
  - Moving checkpoints into `~/.fin/sessions/` files; sessions stay Postgres (`FIN_CHECKPOINT_URL` / test InMemory).
  - OS native folder dialog; SSO / multi-account / tenants / multiple libraries; iknow/iknow-memory code.
  - Changing **citation verifier** span length, repair cap, Soft-Track track names, retrieve ranking, or ADR 0047 upload doors.
  - MCP `tool_search` lazy schemas; memory title-catalog in system (ADR 0048 still forbids).
  - Rewriting **agent identity** / **agent soul** as operator-editable files (Dream still must not edit them).
  - `graph.py` importing `thread_store`.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset. Default `uv run pytest -q` stays green with no live SEC.

1. `uv run pytest -q` exits 0; when `web/` is touched, `web/` `tsc --noEmit` exits 0.
2. After persist, `docs/CONTEXT.md` **fin** line does not contain `research agent` and does not use **workspace** as the product kind; README product one-liner matches that **fin** line.
3. `compile_for_runtime()` / `compile_graph` do not pass `Path.cwd()` (or any baked 工作区) into `UserProfileStore.from_env` or memory-tool closures; the idempotence cache key does not include `workspace_root_key`. Resolving the 工作区 uses `$HOME/.fin/default` (mkdir; tests inject a fake home), never cwd.
4. Product HTTP has no bind/create-工作区 surface: `/api/workspaces` bind/create (and slug `a/b` / `..` create) are absent or 404; the SPA has no 指定/新建 工作区 control. `default_workspace_root` remains.
5. `POST /api/threads` does not store a switchable `project_root`; `GET /api/threads` returns threads for the process (not filtered by a directory query). No migration adds a required `project_root` column.
6. **user profile** reads `$HOME/.fin/user.md` (tests inject home). Product does not require `<dir>/.fin/user.md` overlays. **memory fact** rows use the one default path as the workspace key.
7. `GET /api/corpus` is the one process catalog. There is no “create 工作区” path that adds a catalog file under a new directory.
8. A scripted `invoke`/`stream` whose `configurable` omits `workspace_root` raises the typed missing-root error and does not read `$HOME/.fin/default` or `Path.cwd()` for profile/memory. `workspace_http` stream `configurable` includes `workspace_root` equal to the resolved default. 409 `{error: concurrent_run}` on a second POST to an in-flight thread is unchanged.
9. (removed 2026-09-06 — was legacy `project_root` null-row migration.)
10. Research-track system prefix does not contain `12 SEC`, `page_limit is capped at 3`, or `32 or more characters`; it does contain a **memory existence pointer** when memory ON (existing SC) and does not contain a whole-store memory title list.
11. `messages_for_model(..., track="grounded")` head starts with identity then soul then usage (markers or equivalent stable headers); `track="dialogue"` / `off_topic` / `clarify` reuse the same identity/soul bytes and differ only in usage.
12. Two adjacent scripted model calls on one thread with unchanged `$HOME/.fin/user.md`: identity+soul+usage+pointer substring of the system prefix is deep-equal; prefetch still sits on the user side.
13. Gold / host prompt tests that pinned old `HOST_SYSTEM_PROMPT` literals are updated and pass; a scripted research turn that never retrieve/`read_file`s still does not `done` (verifier unchanged).
14. `docs/guides/prompt-development.md` system-prefix row lists identity, soul, usage, **user profile**, **memory existence pointer**, and states 语料清单 / full catalog does not enter that prefix.

## Open Questions

(none — assumption gate cleared all, including the 2026-09-06 single-default amendment)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; slices `src/fin/corpus/`, `src/fin/retrieve/`, `src/fin/host/`, SPA `web/`; ScriptedChatModel + InMemorySaver default tests; `FIN_CHECKPOINT_URL` product Postgres (ADR 0015); one compiled **host graph** (ADR 0013); **citation verifier** (ADR 0021 / 0046); workspace HTTP + stream (ADR 0023–0026); Soft-Track (ADR 0027); retrieve MCP / Fake pytest (ADR 0032); two ingest doors (ADR 0047); **memory fact** workspace isolation at the store (`specs/cross-session-operator-memory.md` SC6 / SC13, ADR 0048) with one product key; prompt rules in `docs/guides/prompt-development.md`. Prior specs: `specs/workspace.md`, `specs/enterprise-kb-agent.md`, `specs/cross-session-operator-memory.md`. Sibling iknow is read-only reference for identity/soul/usage split and “no implicit cwd”, not a code source. Wave A T1 copy and T2 `default_workspace_root` + never-cwd are already on `feat/workbench-operator-workspace`.

**Changes this spec adds:** one global default 工作区; never `Path.cwd()`; HTTP/CLI inject of that default into `configurable.workspace_root`; unship T2 multi-root `/api/workspaces` bind/create and `known_roots.json`; glossary split 工作台 / 工作区 / 知识库; host prompt assembly identity/soul/usage after those roots exist. Does not add `thread.project_root`, left rail, or a second retrieve catalog.

## 待写入 (persist after ACR)

Flushed 2026-09-06 (initial): CONTEXT **fin** / **工作台** / **工作区** / **知识库** / **workspace** / **agent identity** / **agent soul** / **usage** / **user profile** / **Corpus 首页**; ADR 0049.

Flushed 2026-09-06 (amendment): CONTEXT **工作区** / **知识库** / **user profile**; ADR 0049 accepted amendment (single default; no multi-root). Empty after that flush.

## architecture-change-reviewer

Round 1 (2026-09-06) — BLOCKED (BCG unclear: inject seam unnamed; EHE unclear: omit-cwd EXIT unplanned; MCV no: five foci ≠ 1 commit). Spec amended: HTTP/CLI inject `configurable.workspace_root`; `graph.py` does not import `thread_store`; typed `WorkspaceRootMissingError`; writing-plans splits one wave per Focus.

Round 2 (2026-09-06, amended) — PASS (5/5 yes) against the _multi-root_ contract. **Superseded by Round 3** after operator retracted left-rail / `project_root` switching.

Round 3 (2026-09-06, single-default amendment) — PASS (5/5 yes):

```
bounded-context-guardian: yes — Focus 3 inject seam names HTTP/CLI as injectors (`workspace_http` / CLI add configurable.workspace_root from default_workspace_root, not from thread.project_root); graph.py consumes that configurable only (Out of this spec + ADR 0049 forbid graph.py→thread_store); catalog/retrieve stay compile-time (Focus 4); T2 multi-root routes are unshipped not extended
defensive-contract-validator: yes — Test allocation names empty / negative / overflow / concurrent / exception; SC3–SC8 bind those classes including omit configurable.workspace_root and unshipped bind/create
error-handling-enforcer: yes — omit/empty configurable.workspace_root → typed WorkspaceRootMissingError EXIT never Path.cwd() never silent default inside the graph; HOME/default resolve typed not cwd; 409 concurrent_run unchanged
complexity-anti-drift: yes — inject (HTTP/CLI) vs consume (graph per-call configurable) vs bake-removal (compile-time cwd closures / workspace_root_key) vs unship T2 multi-root HTTP; catalog compile-time; Focus 5 splits identity/soul/usage on Wave B
minimal-change-verifier: yes — discard uncommitted T3 project_root WIP; remaining Wave A is two tracers (inject-default, unship multi-root API) not left rail; Focus 5 tracer; do not fold into Focus 5
```

**OVERALL: PASS — writing-plans may amend Wave A.** Spec path: `specs/workbench-operator-workspace.md`.

Sequencing: two session-waves: Wave A = Foci 1–4 (`plans/workbench-operator-workspace.md`); Wave B = Focus 5 (`plans/host-prompt-assembly.md`). Do not fold Wave A into the Focus 5 prompt branch.

Affected surfaces (declared):

- `docs/CONTEXT.md`, `docs/adr/0049-*.md`, `docs/guides/prompt-development.md`, `README.md`
- `src/fin/host/compile.py`, `graph.py`, `workspace_http.py`, `user_profile.py`, `workspace_registry.py`, `agent_instructions.py`, memory store/prefetch key
- `web/` only if T2 left a 工作区 picker (must not add one)
- `tests/host/`

This spec is **one logical program**, not one commit. Wave A remaining = discard T3 WIP + inject-default tracer + unship-multi-root tracer. Wave B stays in Focus 5 after A merges.
