# Plan: extract / index / search-mode program (two-repo)

**Goal:** Sequence the six ACR-passed specs so implementers never ship one mega-diff: window tree, then graph index, then 档 retrieve, then host cascade.
**Approach:** **Documents can be written together; code cannot.** Each wave is one spec’s plan. Fin spec 2 is a joint contract with **no fin ranking/extract implementation**. iknow owns extract/Leiden/档 arms. Host cascade last so `filters.search_mode` already exists.
**Spec links:** `specs/extract-window-tree.md`, `specs/graph-index-standard-fast.md`, `specs/search-mode-retrieve.md`, `specs/search-mode-host.md` plus iknow siblings `graph-index-standard-fast.md`, `search-mode-retrieve.md`.
**ACR:** all six specs OVERALL PASS 2026-09-17 (see each child plan). Do not re-merge into one task — ACR `minimal-change-verifier` already split them.
**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion

## Wave order (repos)

| Wave | Plan file                                            | Repo  | Blocks                                     |
| ---- | ---------------------------------------------------- | ----- | ------------------------------------------ |
| 1    | `plans/extract-window-tree.md`                       | fin   | —                                          |
| 2    | `../iknow-memory/plans/graph-index-standard-fast.md` | iknow | Fast children need wave 1 push shape       |
| 3a   | `../iknow-memory/plans/search-mode-retrieve.md`      | iknow | C live needs wave 2 summaries              |
| 3b   | `plans/search-mode-retrieve.md`                      | fin   | `[parallel]` with 3a after codes exist     |
| 4    | `plans/search-mode-host.md`                          | fin   | needs 3b `filters.search_mode` on the port |

**Not a plan:** fin `graph-index-standard-fast` — contract only; no `src/fin` extract/Leiden/ranking tickets.

## 待写入

empty

## Tasks (program-level, not implementation)

1. **Wave 1 window tree** — tag: `[implementation]`
   - **Inherits:** spec 1 ACR PASS; ADR 0053 / 0060 / 0063.
   - **Surface:** `fin.corpus`
   - **Acceptance:** Child plan `extract-window-tree.md` bullets all checked; `uv run pytest -q` still Fake retrieve.
   - Status: [ ] pending

2. **Wave 2 graph index** — tag: `[implementation]`
   - **Inherits:** iknow spec + ADR-0011; fin 抽图窗/children as input.
   - **Surface:** iknow extract / graph / community CLI
   - **Acceptance:** Child plan `graph-index-standard-fast.md` (iknow) complete; `npm test` without live GDS.
   - `[blocks: T1]` for Fast co-occurrence on children
   - Status: [ ] pending

3. **Wave 3 retrieve 档** — tag: `[implementation]`
   - **Inherits:** `filters.search_mode`; named failure codes including `community_summary_index_failure`.
   - **Surface:** iknow retrieve assembler + fin retrieve port / `named_failures`
   - **Acceptance:** Both search-mode-retrieve child plans complete.
   - `[blocks: T2]` for C-ready path; 3a/3b `[parallel]` for B default + wire
   - Status: [ ] pending

4. **Wave 4 host cascade** — tag: `[implementation]`
   - **Inherits:** spec 4 ACR PASS; Soft-Track `decide_track` shape.
   - **Surface:** `fin.host` + 工作台 interrupt
   - **Acceptance:** Child plan `search-mode-host.md` complete; default pytest still deselects opt-in marks.
   - `[blocks: T3]`
   - Status: [ ] pending
