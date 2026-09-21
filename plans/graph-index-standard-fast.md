# Plan: graph index host contract (standard vs Fast)

**Goal:** Fin records one extract method per 资料集合 version (default **standard**), feeds freeze-text / **抽图窗** (plus in-window children) as the index-job input, and surfaces typed exits — without ranking or extract/Leiden in this repo.
**Approach:** Version switch first, then bind the existing Fast **choice window** to that switch (no silent Fast), then prove 入库推送 is the only extract input. The sibling [`../../iknow-memory/plans/graph-index-standard-fast.md`](../../iknow-memory/plans/graph-index-standard-fast.md) owns extract, merge, prune, Leiden, summaries, and embeddings. Search-mode assignment stays other plans.
**Spec link:** `specs/graph-index-standard-fast.md`
**Sibling plan (job):** [`../../iknow-memory/plans/graph-index-standard-fast.md`](../../iknow-memory/plans/graph-index-standard-fast.md)
**Program:** `plans/extract-index-search-mode-program.md` wave 2 (fin contract; iknow implements the job)
**ACR:** all-yes / N/A-with-reason (from spec, 2026-09-17)

bounded-context-guardian: yes — extract/merge/Leiden/embeddings in iknow; fin freeze-text / 抽图窗 only.
input-contract-tests: N/A — five classes allocated to iknow opt-in plan, not fin default pytest.
error-handling-enforcer: yes — typed exits named (empty Fast, dual method, silent Fast, summary fail → C not-ready); public strings Open Question on iknow job codes.
complexity-anti-drift: N/A — no fin implementation modules in this spec.
minimal-change-verifier: yes — spec 2 of 4; host cascade and retrieve ranking out.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion (landing grain: operator global commit section)

**Live / opt-in index commands (named, not default pytest):** standard extract; Fast extract; Leiden rebuild; global-ready check. These run in the **iknow** sibling plan. Fin `uv run pytest -q` stays **Fake retrieve**.

## 待写入

empty

## Tasks (ordered by dependency)

1. **One extract method per corpus version (default standard)** — tag: `[implementation]`
   - **Inherits:** ADR 0079 / 0068; spec SC3–SC4 — exactly one method per version; index-job default **standard**; dual graphs on one version rejected as typed fail; switching rebuilds that version (re-extract is the sibling job).
   - **Surface:** `fin.corpus` 资料集合 version / 入库推送 metadata (operator switch record)
   - **Acceptance:** A new version is **standard** unless the operator switch records Fast; a second method on the same version is a typed fail, not a merged dual graph; `uv run pytest -q` stays Fake retrieve. Fin does not run extract or Leiden.
   - Status: [x] done (2026-09-21 worktree graph-index-standard-fast)

2. **Choice window never silent-applies Fast** — tag: `[implementation]`
   - **Inherits:** ADR 0068; spec SC4 — AI may propose Fast in a **choice window**; empty/ambiguous/no pick keeps **standard**; Fast is not a 检索档 token. Host propose window already opens (`plans/search-mode-host.md` T4); this bullet writes the operator pick onto T1’s version switch (that increment left `applied` false because spec 2 owns the switch).
   - **Surface:** `fin.host` choice window + T1 version switch
   - **Acceptance:** Propose path still opens options; no pick / bad pick leaves the version **standard**; an operator Fast pick records one method on that version (T1) and does not set `search_mode` to Fast; no silent Fast without a switch pick.
   - `[blocks: T1]`
   - Status: [x] done (2026-09-21 worktree graph-index-standard-fast)

3. **Freeze-text / 抽图窗 (+ children) is the extract input** — tag: `[implementation]`
   - **Inherits:** ADR 0054 / 0060 / 0063 / 0077; spec — fin input is freeze-text / **抽图窗**; Fast co-occurrence uses in-window **父子** children, not a second document cut; standard LLM extract is per **抽图窗** (sibling). Fin must not re-rank.
   - **Surface:** `fin.corpus` 入库推送 / `fin.retrieve` push port
   - **Acceptance:** Push envelopes still carry parent window text plus in-window children; Fast is not implemented by a second 切分 in fin; ranking is absent from this increment; `uv run pytest -q` Fake retrieve green.
   - `[blocks: T1]`
   - `[parallel]` with T2
   - Status: [x] done (2026-09-21 worktree graph-index-standard-fast)

4. **Empty Fast on Chinese 资料 is 具名失败** — tag: `[implementation]`
   - **Inherits:** ADR 0076; spec SC5 — empty Fast graph on Chinese **资料** is a **具名失败**, not a silent empty index and not a healthy graph/global miss. Public JSON/SSE/MCP **code spelling** is Open Question 1 — typed and distinct from 档未开 / 资料未就绪; do not invent the wire string here.
   - **Surface:** `fin.host` named_failures + Fake retrieve/index fail mapping
   - **Acceptance:** Fake (or recorded) job fail for empty Fast on Chinese **资料** surfaces a typed host exit distinct from `tier_not_open` / `corpus_not_ready`; default pytest does not run a live Fast job. Leiden/summary/embed failures stay the sibling plan.
   - `[blocks: T1]`
   - Status: [x] done (2026-09-21 worktree graph-index-standard-fast)

## Code review phase

After all bullets of this task land: `code-review`; `GATE: BLOCKED` → next slot `review-report-repair`.
