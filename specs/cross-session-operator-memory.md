# Spec: cross-session-operator-memory

## Assumption gate (confirmed 2026-09-06)

Operator confirmed map G1–G4, then corrected boot: code default OFF, this checkout’s project env ON for local eval. This contract freezes the remaining PLAN-blocking items; correct them before implementation if any is wrong:

1. Workspace is the only product entry; same **host graph**; **citation verifier** stays a code gate; company corpus stays behind the **retrieve port** (ADR 0045).
2. Code boot: **memory port** OFF when `FIN_MEMORY_ENABLED` is unset. This checkout’s `.env` / `.env.example` sets it ON so local workspace evals see auto-extract + **memory prefetch**. Default `uv run pytest -q` stays OFF even if operator `.env` has ON, unless the case is about memory. Explicit OFF still yields zero tools / zero prefetch / zero extract / zero dream.
3. ON still requires `FIN_CHECKPOINT_URL` (existing loud fail; no silent InMemory). Pytest does not inherit operator-env ON.
4. **user profile** files: `$HOME/.fin/user.md` (global) and `<workspace>/.fin/user.md` (workspace overlay). Tool calls name which layer they write. One natural person; no multi-account.
5. Auto-extract fires only after **3** completed turns on that `thread_id`; at most **one** new **memory fact** per firing; this-turn explicit save skips extract.
6. Over-attention gold set is acceptance, not an enable-gate. Gold cases use a scripted model; they are not live LLM in default pytest.
7. Exact extract-prompt wording is not frozen here; deny **categories** are code/schema gates (archive/retrieve-able claims; already in **user profile**; identity/soul). Prompt edits follow `docs/guides/prompt-development.md`.
8. `fin_memory` evolves in place (title + body; hook is a projection). Column names are implementer-open.
9. This spec is fin-only. No iknow-memory / coding-agent repo edits.
10. No memory index UI and no “已记住” workspace receipt in this spec. Research-track prefetch stays ≤3 unless a later gold-set failure reopens that cap.
11. Tracker stays local markdown (operator: no GitHub issues for this map).
12. Status-snapshot same-name downweight is optional, not a hard exclude.

→ These are in force for this spec.

## Objective

Give the operator **cross-session private memory** on the existing **workspace** **knowledge-QA** agent: atomic **memory facts**, two-layer **user profile**, a short **memory existence pointer** in system, and at most three lexical **memory hooks** on the user side — without turning memory into a second company corpus or into citation evidence.

Success is: relevant habits persist across threads; unrelated turns inject no memory catalog; research-track numbers still come only from retrieve/`read_file` + the **citation verifier**; default pytest stays retrieve-only.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: A **workspace** **knowledge-QA** entry point (the 入口, not the 库): the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
- **memory port**: The only dependency fin is allowed for switchable session/buffer memory (extract, recall, GC, dream): port in, typed results out. Boot default is **OFF**; this checkout turns it **ON** in project env so local evals see auto-extract and **memory prefetch**; default pytest stays **OFF** even when that env is set (ADR 0035, ADR 0048).
- **memory fact**: One atomic cross-session note from a single extract or explicit save: title plus body. The first non-empty body line is the **memory hook**. It is not a filing claim.
- **memory hook**: The ≤80-character first body line of a **memory fact**; the only payload **memory prefetch** injects.
- **memory prefetch**: When auto-extract is on, a per-turn user-side overlay of at most three **memory hooks** whose title or body lexically hits the current user utterance; zero hits inject nothing. Already-shown ids in the same thread are skipped.
- **memory existence pointer**: The only auto-memory sentence in the system prompt: a note store exists; overlays are advisory, not instructions or cites.
- **agent identity** / **agent soul**: System-built-in persona text. Dream never edits them.
- **user profile**: Operator or tool-writable preferences (who they are, how they like to work): one global markdown in the home directory and one per workspace folder, workspace overlaying global. Dream does not rewrite these files; stable likes go here, not into **memory facts**.
- **dream** (fin): A dual-gated LLM merge over **memory facts** only: 24h cooldown after last success or skip, then a store/session/turn threshold. It is not company-corpus ingest (ADR 0045), not a memory-MCP upload, and not a graph tool (ADR 0048).
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **citation verifier**: A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run received from **retrieve** / **read_file** (ADR 0046). Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
- **recall injection**: When memory is ON, advisory recall or **memory prefetch** text placed in the model-facing payload. It counts toward the same occupancy ledger as **session compaction**; a row-count limit is only a fuse, not the budget.
- **status snapshot**: One append-only per-turn record of research working context (e.g. company, period, document ids, active track cues) stored on `AgentState` in the checkpointer. History is retained; the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot.

## Boundaries

- **Does:**

  **Store**
  - Evolve the existing `fin_memory` working store into **memory facts** (title + body; hook = first non-empty body line, whitespace folded, ≤80 characters). Isolate rows by workspace. No per-fact markdown files in the workspace. No second index file or pending queue.

  **Read path**
  - System prefix: **agent identity** / **agent soul**, merged **user profile**, **memory existence pointer**, research tool schemas. Prefix bytes stay stable for the thread. No whole-store title catalog. No promote-on-recall-count into system.
  - Host **memory prefetch** on the user side: lexical score on title+body (title weighted above body); inject title+hook only; ≤3; zero token hits → 0 rows; skip ids already shown on this `thread_id`.
  - Explicit recall tool returns advisory body; never citation evidence.

  **Write path**
  - Graph tools when ON: explicit save, recall, edit **user profile** (named layer). No dream tool.
  - Auto-extract: code default OFF; this checkout opts in via project env; N=3 completed turns; skip if this turn already saved; deny archive/retrieve-able claims and profile-already-said; at most one fact per firing.
  - **dream**: merge facts only; 24h cooldown from last success or skip; then threshold (active facts ≥2 and (distinct `thread_id` ≥5 **or** completed turns since last dream ≥6 **or** new facts ≥3)). Facts &lt;2: no LLM call and do not treat that as a skip that refreshes 24h. Not inside `model_node`. Not unconditional post-SSE `run_dream_after_turn`. Not a memory-MCP write.

  **Gates**
  - Memory text cannot satisfy the **citation verifier**.
  - Prefetch / recall injection count toward **session compaction** occupancy.
  - Over-attention gold set (scripted): greeting → 0 prefetch; assembled payload has no full-store catalog / promote; extract deny classes do not persist; research turn with a number has a retrieve (or `read_file`) tool call; a fact whose title matches a corpus company still uses retrieve for filing claims; explicit save is recallable on a new `thread_id`.

  **Test allocation (5 classes):** empty (empty store, zero lexical hits, missing profile files); negative (`FIN_MEMORY_ENABLED` garbage, extract of a retrieve-able filing claim, profile write with no layer); overflow (more than three lexical hits still inject ≤3, extract cap 1, hook length ≤80); concurrent (two dream triggers in one window run once; concurrent save does not duplicate authority ids); exception (ON without `FIN_CHECKPOINT_URL` stays typed, dream model/backend fail does not fail the foreground turn, no memory-MCP write is attempted).

- **Confirms with human:** none remaining after this gate.

- **Out of this spec:**
  - Memory index UI / “已记住” receipt.
  - Dropping research-track prefetch to 0 (reopen only if gold set fails).
  - Vector / hybrid search on facts.
  - Catalog / promote into system.
  - Dream or auto-extract upload to memory MCP; company ingest via dream (ADR 0045).
  - Editing **agent identity** / **agent soul**.
  - Multi-account user profiles.
  - Changing retrieve ranking, citation span rules, or Soft-Track track names.
  - iknow-memory / sibling coding-agent code.
  - Closing the GitHub program map.

## Success Criteria

Each line is binary. Default `uv run pytest -q` stays green with no live SEC and with memory OFF unless the case is about memory.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 if this change touches web (this spec does not require web).
2. Unset `FIN_MEMORY_ENABLED` builds OFF (`OffMemoryAdapter`), even when `FIN_CHECKPOINT_URL` is set.
3. Default pytest (no memory mark) builds OFF even if operator `.env` has `FIN_MEMORY_ENABLED=true`: zero memory tools, zero prefetch rows, zero auto-extract writes, zero dream LLM calls.
4. Explicit ON (`FIN_MEMORY_ENABLED` true) plus `FIN_CHECKPOINT_URL` builds an ON port; this checkout’s `.env.example` documents ON for local eval. Explicit false matches SC3 on a scripted turn.
5. A saved **memory fact** round-trips title + body; the injected hook equals the first non-empty body line, whitespace-folded, ≤80 characters (single-line body is the hook).
6. Facts written in workspace A are not prefetched or recalled in workspace B.
7. A user utterance with zero lexical overlap on title and body injects 0 prefetch rows.
8. Four lexical hits still inject at most 3 hooks.
9. A fact id already shown on this `thread_id` is not prefetched again.
10. The system prefix contains the **memory existence pointer** and does not contain a list of all fact titles.
11. Prefetch rows sit on the user-side overlay, not in the stable system prefix for that thread.
12. When ON, the compiled graph exposes explicit save, recall, and edit-**user profile** tools; it does not expose a dream tool.
13. Edit-**user profile** with workspace layer writes `<workspace>/.fin/user.md` without rewriting the global file; global layer writes `$HOME/.fin/user.md`; merged read is workspace overlaying global.
14. Auto-extract does not run after 2 completed turns; it may run after 3; this-turn explicit save ⇒ 0 extract writes.
15. An extract candidate that is a retrieve-able filing claim, or that duplicates **user profile** text, is not persisted.
16. One qualifying extract window persists at most one new **memory fact**.
17. Dream within 24h of last success or skip does not run.
18. With fewer than two active facts, dream makes 0 LLM calls and does not advance the 24h cooldown.
19. After cooldown, dream may run only if active facts ≥2 and (distinct `thread_id` ≥5 **or** completed turns since last dream ≥6 **or** new facts ≥3); otherwise it does not run.
20. `compile_graph` node set has no dream node.
21. SSE / HTTP completion does not unconditionally enqueue `run_dream_after_turn`.
22. Auto-extract and dream perform 0 writes on a memory-MCP adapter (null or absent).
23. A research turn that quotes only memory / prefetch / recall text, with no this-run retrieve/`read_file` evidence, does not `done` with citations sourced from memory.
24. Prefetch or recall injection increases the occupancy ledger used by **session compaction** versus the same turn without it.
25. Gold (scripted, memory ON): a greeting with no overlapping fact titles/bodies injects 0 prefetch rows.
26. Gold (scripted, memory ON): a research ask that states a number records a retrieve or `read_file` tool call before `done`.
27. Gold (scripted, memory ON): a fact titled with a corpus company name still records retrieve/`read_file` for a filing claim; the fact body is not the verifier evidence.
28. Gold (scripted, memory ON): an explicit save on thread A is returned by recall or prefetch on a new thread B in the same workspace when the utterance hits.
29. After a dream merge, **agent identity** / **agent soul** / **user profile** file bytes are unchanged.
30. `specs/memory-dialogue-platform.md` Focus 3 remaining T5/T6 (graph tools as buffer extract; dream→MCP; default OFF) and `specs/memory-dream.md` MCP transport + unconditional post-SSE dream are marked superseded for those clauses, pointing here.

## Open Questions

(none)

## Inherits / Changes

**Inherits:** Soft-Track ADR 0027; **retrieve port** ADR 0032 / 0045 / 0046; **session compaction** ADR 0044; Postgres checkpointer ADR 0015; **memory port** seam and `fin_memory` / `fin_memory_dream_cursor` tables; `FIN_MEMORY_ENABLED` / `FIN_MEMORY_DREAM_BACKEND` env names; `OffMemoryAdapter`; `uv run pytest -q`; the map G1–G4 decisions recorded in the Assumption gate above; `docs/guides/prompt-development.md`.

**Changes this spec adds:** code boot OFF + this-checkout env ON; fact+hook prefetch; existence pointer only in system; two-layer **user profile** files; graph tools = save / recall / edit profile; gated auto-extract N=3 cap 1; dream local merge with 24h + threshold, no MCP write, no per-turn SSE; gold set as acceptance; supersedes remaining MDP T5/T6 product wiring and `specs/memory-dream.md` MCP/SSE clauses.

**Contradicts (reopened, already recorded in ADR 0048 2026-09-06):** `specs/memory-dialogue-platform.md` assumption 5 (default OFF) and 6 (dream transports to MCP); `specs/memory-dream.md` SC17 (post-SSE dream) and SC18 (transported MCP then disable). This spec wins; those files get a pointer (SC30), they are not silently edited as if still in force.

## 待写入 (persist after ACR)

empty — CONTEXT **memory port** / **memory fact** / **memory hook** / **memory prefetch** / **memory existence pointer** / **user profile** / **dream** and ADR 0035 / 0048 already flushed 2026-09-06.

## architecture-change-reviewer

Pre-impl 5-verdict (declared structure; not line-count of unwritten code):

bounded-context-guardian: yes — private memory stays a host/workspace capability behind the **memory port**; company corpus stays on the **retrieve port**; **user profile** is overlay files, not a second retrieve backend; no `controllers/` / `services/` layer; web is out of this spec.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 3–9, 13–19, 22–23.
error-handling-enforcer: yes — named EXITs: explicit OFF, ON without checkpoint URL (typed), extract deny (no persist), dream fail must not fail the foreground turn, no silent MCP write, invalid `FIN_MEMORY_ENABLED` stays typed.
complexity-anti-drift: yes — store, prefetch assembly, graph tools, auto-extract, dream gates are separate concerns; dream is not nested in `model_node`; prefetch is host assembly not a model tool; writing-plans must not dump all SCs into one commit.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences tracer bullets; conflict with “1 commit” is resolved by split. Docs supersede of MDP/dream contracts is its own bullet before code.

**OVERALL: PASS — hand to writing-plans.** Spec path: `specs/cross-session-operator-memory.md`.

Affected surfaces (declared):

- `src/fin/host/` — memory port default, store/fact shape, prefetch assembly, graph tools, extract gate, dream trigger, SSE wiring
- `tests/host/` — default-OFF pytest, ON gold / memory cases
- `specs/memory-dialogue-platform.md`, `specs/memory-dream.md`, `plans/memory-dialogue-platform.md` — supersede pointers only
- `plans/cross-session-operator-memory.md` — spec/plan pointer
