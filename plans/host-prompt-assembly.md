# Plan: host-prompt-assembly (Wave B / the tracked change)

**Status:** ready — Wave A merged (PR, `main` @ f5d57a5+). **Next wave.**

**Goal:** Host system prompts are identity → soul → usage → **user profile** → **memory existence pointer**; persona no longer hard-codes the 12-filing SEC list, tool field caps, or verifier numbers.
**Approach:** Fixtures first, then split the four **track** prompts, then re-pin. Do not touch inject seam / `default_workspace_root` / catalog / thread APIs.
**Spec link:** `specs/workbench-operator-workspace.md` (Focus 5, SC10–SC13)
**Worktree:** **new** from current `main` — e.g. `git worktree add ../fin-wt-host-prompt-assembly -b feat/host-prompt-assembly`. Do not reuse `wave-a-workspace-bind` (that branch is merged).
**Tracker:** the tracked change. Do not open a second prompt issue.
**Out of this worktree:** `/api/workspaces` or left-rail 工作区 picker; `fin_thread.project_root`; retrieve ranking; citation span length / repair cap; Soft-Track track names.
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → `uv run pytest -q` → verification-before-completion → one commit on this branch
**End-of-round:** after all bullets, one code-review (Standards + Spec) on the wave diff.
**待写入:** empty.

**Depends on:** Wave A on `main` (single global `$HOME/.fin/default`, inject seam, one 知识库). Identity names that split once; do not rewrite persona twice.

## ACR（自 spec Round 3 原样粘贴）

```
bounded-context-guardian: yes — Focus 3 inject seam names HTTP/CLI as injectors (`workspace_http` / CLI add configurable.workspace_root from default_workspace_root, not from thread.project_root); graph.py consumes that configurable only (Out of this spec + ADR 0049 forbid graph.py→thread_store); catalog/retrieve stay compile-time (Focus 4); T2 multi-root routes are unshipped not extended
defensive-contract-validator: yes — Test allocation names empty / negative / overflow / concurrent / exception; SC3–SC8 bind those classes including omit configurable.workspace_root and unshipped bind/create
error-handling-enforcer: yes — omit/empty configurable.workspace_root → typed WorkspaceRootMissingError EXIT never Path.cwd() never silent default inside the graph; HOME/default resolve typed not cwd; 409 concurrent_run unchanged
complexity-anti-drift: yes — inject (HTTP/CLI) vs consume (graph per-call configurable) vs bake-removal (compile-time cwd closures / workspace_root_key) vs unship T2 multi-root HTTP; catalog compile-time; Focus 5 splits identity/soul/usage on Wave B
minimal-change-verifier: yes — discard uncommitted T3 project_root WIP; remaining Wave A is two tracers (inject-default, unship multi-root API) not left rail; Focus 5 the wayfinder program; do not fold into the tracked change
```

**OVERALL: PASS.** This wave is Focus 5 only.

## Harvest（已决 vs 开放）

**已决:** `docs/guides/prompt-development.md` (Wave A T1); CONTEXT **agent identity** / **agent soul** / **usage**; assembly order; no 12 SEC / `page_limit is capped at 3` / `32 or more characters` in research prefix; four tracks share identity/soul; citation numbers stay in the **citation verifier**; parametric memory is not evidence.

**开放:** marker strings; how usage text names tools without restating schema; clarify options via `list_files` vs a frozen snapshot (spec forbids hardcoding AAPL/MSFT/AMZN in all four prompts).

## Tasks (ordered by dependency)

1. **Prompt fixtures before copy** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 “fixtures first”; SC10–SC12 as failing assertions; `docs/guides/prompt-development.md`.
   - **Surface:** host prompt tests
   - **Acceptance:** new assertions fail on current `HOST_SYSTEM_PROMPT` / sibling track constants: research prefix still contains `12 SEC` or `page_limit is capped at 3` or `32 or more characters`; four tracks do not yet share identity/soul bytes. No production prompt rewrite in this commit.
   - Status: [ ] pending

2. **Split identity / soul / usage for all four tracks** — tag: `[implementation]`
   - **Inherits:** spec SC10–SC12; identity = **fin** enterprise knowledge-base Agent; soul = how to speak/refuse + parametric memory is not evidence; usage = tool choice + citation _behavior_ per **track**; pointer stays last.
   - **Surface:** host agent instructions
   - **Acceptance:** research prefix has no `12 SEC`, no `page_limit is capped at 3`, no `32 or more characters`; `messages_for_model` research head is identity then soul then usage; dialogue / off_topic / clarify reuse the same identity/soul bytes and differ only in usage. T1 fixtures go green.
   - Status: [ ] pending
   - [blocks: T1]

3. **Re-pin prompt gold and prefix stability** — tag: `[implementation]`
   - **Inherits:** spec SC12–SC13; adjacent turns deep-equal on identity+soul+usage+pointer when `$HOME/.fin/user.md` unchanged; verifier still refuses a research turn with no retrieve/`read_file`.
   - **Surface:** existing host prompt / gold tests
   - **Acceptance:** previously pinned `HOST_SYSTEM_PROMPT` literals are updated and pass; two adjacent scripted calls on one thread keep the stable prefix substring; `uv run pytest -q` exits 0.
   - Status: [ ] pending
   - [blocks: T2]
