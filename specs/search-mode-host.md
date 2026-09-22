# Spec: search-mode host (Soft-Track 检索档 + 工作台 choice window)

> **Status:** assumption gate confirmed 2026-09-17 (four-spec split; this is spec 4 of 4).
> **Siblings:** `specs/extract-window-tree.md` (1/4), `specs/graph-index-standard-fast.md` (2/4), `specs/search-mode-retrieve.md` (3/4).
> **This file:** fin **host graph** + **工作台** only.

## Objective

After **Soft-Track** has already chosen **grounded**, the **host agent** assigns **检索档** (search mode A/B/C/D) with the **same cascade** as `decide_track` (ADR 0027 / 0062), injects call-scoped search-mode copy (ADR 0061), and — when cascade **clarify** fires — compiles a **HITL** **choice window** on the **v1 surface** so the operator picks, the run **resumes**, then retrieve proceeds. Operators are not forced through a per-turn 档 picker. Invalid structured classify must not silent-open **C 综述**. The host may **propose** **Fast 抽图** in a **choice window**; it must never silent-apply the extract method. Ranking and the extract job stay out.

**Who:** local operator on the **工作台**, talking on a **research thread**.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **track**: The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `grounded`. Chosen in-graph before execution; tools and the citation verifier bind only to `grounded`.
- **grounded**: The Soft-Track leaf that binds corpus tools and the **citation verifier** (ADR 0061). Not a product name, not **search mode**, and not the `retrieve` tool. Wire value `research` is an alias for this leaf.
- **Soft-Track**: The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
- **检索档** (`search mode`): Which retrieve path a knowledge question uses after it has entered **grounded**: A 查找 (lexical-first), B 知识问答 (iknow graph-priority then vector∥BM25 fill), C 综述 (index-time community summaries; report text ES ∥ report vectors, then optional drop to windows — ADR 0070), D 真数据库 (real SQL/business DB; document tables stay B). Assignment reuses the Soft-Track cascade (ADR 0027 / 0062): rules, then structured classify when ambiguous, then inherit, then clarify or typed fail — not a required picker and not silent C. Cascade **clarify** opens a **choice window** (structured options → operator pick → resume execute). Search-mode copy is call-scoped (ADR 0061): inject once in near context, never rewrite history; after **session compaction**, re-inject the mode this turn needs.
- **HITL**: LangGraph `interrupt` may be compiled into the host graph. Durability is crash resume of the in-flight run. **Choice window** is the v1 interrupt UX for Soft-Track / search-mode clarify (structured options, operator pick, then execute). Plan-approval and tool-approval UI stay out of v1.
- **v1 surface**: The first ship binary is a CLI that compiles the host graph; v1 HTTP exposes that same compiled graph and must call sync `invoke` (not `ainvoke`) so the sync `PostgresSaver` stays valid. Crash resume continues the in-flight run on that `thread_id`. Plan-approval and tool-approval HITL are not v1; **choice window** on cascade clarify is (ADR 0062).
- **Fast 抽图**: The NLP / co-occurrence **extract method** (index job): mixed **Chinese/English noun-phrase regex** (ADR 0076), co-occurrence on **in-window children** then **prune** and Leiden, community reports from **抽图窗** text (ADR 0067 / 0073 / 0077). An **operator switch** selects it (default **standard**, **one method per corpus version** — ADR 0079); the model may propose Fast in a **choice window** and must not silent-apply (ADR 0068).

Choice-window language above is the SSOT phrase this spec implements: **structured options → operator pick → resume execute**.

## Confirmed assumptions (gate)

1. Four-spec split (2026-09-17): this file is spec **4 of 4**. Window tree is spec 1; extract job / Leiden / Fast NLP is spec 2 (`graph-index-standard-fast.md`); A/B/C/D ranking behind the **retrieve port** is spec 3 (`search-mode-retrieve.md`).
2. Search-mode assignment runs **only after** Soft-Track `track=grounded`. Non-grounded tracks do not assign a 档 and do not open a search-mode **choice window**.
3. Cascade shape copies `src/fin/host/track_decision.py` `decide_track`: high-confidence **rules** first; **structured classify** only when ambiguous and inherit/clarify would not already decide; **inherit** on short follow-ups that stay in the same retrieve job; else **clarify or typed fail**. Residual policy must **not** silent-open **C**.
4. Optional pin/override may set a 档 without opening the window. There is **no** required per-turn 档 picker on the **工作台**.
5. Search-mode **usage copy** is host contract (ADR 0061): call-scoped inject in this spec. Ranking is not.
6. **Fast 抽图** vs **standard** is an **operator switch** on the **index job** (ADR 0068) with **one method per corpus version** (ADR 0079). Host may propose Fast via **choice window**; never silent-apply. Ambiguous or no pick keeps **standard**. Actual extract job is spec 2.
7. Default `uv run pytest -q` stays green. Search-mode host suites extend Soft-Track as **opt-in** marks (not collected by default pytest).
8. **Choice window** payload is a host/HTTP contract even if the SPA is a stub this increment.
9. No GraphRAG product names in this spec or its tests.

## Boundaries

- **Does:**
  - After Soft-Track `decide_track` returns **grounded**, run a search-mode decision cascade (new host helper, same tier names as `DecisionTier` / ADR 0027): **rules** → **structured classify** if still ambiguous → **inherit** → **clarify or typed fail**. Then the grounded leaf may call retrieve (one existing `retrieve` tool / **retrieve port**, not four host retrieve nodes).
  - **Rules (high-confidence only):** explicit A/B/C/D or CONTEXT 检索档 labels in the utterance (and any documented pin/override field) assign that 档 without LLM. Do not invent C from residual “knowledge question” shape.
  - **Structured classify:** only when rules miss **and** inherit/clarify would not already decide; reuse `FIN_MODEL` / existing classify client pattern; JSON names exactly one of `A` | `B` | `C` | `D` (product labels 查找 / 知识问答 / 综述 / 真数据库). Typed EXITs (`invalid_search_mode` / `schema_parse` / `model_error` / `low_confidence`) fall through the cascade; they must **not** crash the run and must **not** silent-open C. **Amended by ADR 0081 (landed):** this tier is a closed-set **Jev** Choice over `A | graph | global | D` — no `FIN_MODEL` reuse, no JSON `A|B|C|D`, no MiniMax fallback — and the EXIT family is `signals["mode_jev_exit"]`: `transport` / `schema` / `invalid_label` from the client plus `no_jev_client` / `invalid_search_mode` / `low_confidence` / `unexpected_error` from the cascade, all still falling through. Default pytest injects the shared Fake Choice; the live round-trip is the named `jev_live` suite.
  - **Inherit:** short / underspec follow-up on the same `thread_id` that stays in the same retrieve job (latest **status snapshot** still has research context; no company-change wipe of that job) reuses the last assigned 档. Do not inherit across a new retrieve job or a cleared snapshot.
  - **Clarify:** host emits a **structured option list** (A/B/C/D ids + operator-visible labels; C/D may be present but marked unopened). Compile LangGraph `interrupt` as **choice window** **HITL** (ADR 0062). **工作台** (or CLI equivalent on the same compiled graph) shows the window; operator pick resumes the in-flight **run**; then retrieve. Free-text clarify with no selectable options is not the v1 confirm path. **Amended by `specs/search-mode-graph-global.md` (landed):** the option ids come from `LASTING_RETRIEVE_TIERS` = `A | graph | global | D` (operator labels 查找 / 知识问答 / 综述 / 真数据库 unchanged), and a pick is resolved onto a lasting token before retrieve; only `A` / `graph` are open.
  - **Typed fail:** if the cascade cannot name A/B/C/D, or the named 档 is not open, return CONTEXT **具名失败** (**档未开** / **真库未接** as applicable) and **do not** invent a 档. Residual **conditional_policy** must not default to C.
  - Persist an auditable search-mode decision on the run (mirror Soft-Track `track_decision`): at least `search_mode` (or explicit null on typed fail), `confidence`, `decision_tier`, structured `signals`. `track` remains the Soft-Track leaf; search mode is not a fifth **track**.
  - **Call-scoped injection (this spec):** when a 档 is assigned, inject search-mode usage **once** into near context at model-call time (same pattern as per-track prompts). Never rewrite checkpointed history / old `SystemMessage`s when the mode changes. After **session compaction**, re-inject the **search mode this turn needs**; a later different mode injects once again (ADR 0061).
  - Optional override pin: request/state field may pin a 档 and skip the window; still record `decision_tier` / signals. No chrome **required** per-turn 档 picker.
  - **Fast 抽图 propose (host only):** the model/host may open a **choice window** whose options distinguish Fast (cheaper / noisier B) vs **standard**. No pick or ambiguous pick keeps **standard** (ADR 0068). Host must not treat Fast as a **检索档** and must not flip the corpus version’s extract method without the operator switch / confirmed pick. Applying extract, Leiden, or rebuilding the version is out (spec 2).
  - **工作台:** render **choice window** from the interrupt payload (structured options). SPA stub is allowed if the payload contract test still passes. No Chat|Research picker; no required 档 picker.
  - Test classes (opt-in, ≥1 case each): empty (blank/underspec utterance after grounded with no inherit); negative (judge answer outside the closed 档 set / off-domain already off_topic so no search-mode / pin of unopened C); overflow (search-mode `step_cap` does not apply — reuse grounded caps only; snapshot inherit does not wipe history); concurrent (same-thread send still 409 / `ConcurrentRunError`); exception (unreachable judge, unparseable judge payload, or a judge that abstains → typed EXIT, not crash or silent C). **Amended by ADR 0081 (landed):** the EXIT named in these classes is `signals["mode_jev_exit"]` — `no_jev_client` / `transport` / `schema` / `invalid_label` / `invalid_search_mode` / `low_confidence` / `unexpected_error` — not a JSON classify, so no case here may require one.

- **Confirms with human:** none remaining (four-spec split + cascade/injection/HITL/Fast-propose confirmed 2026-09-17). Exact pytest mark string is an implementer/plan choice.

- **Out of this spec:**
  - Leiden / Fast NLP / **抽图窗** tree cut (`extract-window-tree.md`, `graph-index-standard-fast.md`).
  - Four LangGraph retrieve nodes; retrieve ranking / A lexical-first / B graph-priority / C report ES∥vectors / D SQL execution (`search-mode-retrieve.md`; ranking stays behind the **retrieve port**).
  - Plan-approval and tool-approval **HITL** (not **v1 surface**).
  - Rewriting checkpoint history or mutating old messages when **检索档** changes.
  - Silent auto Fast; dual extract methods on one corpus version (ADR 0079 — owned by spec 2).
  - Making search-mode suites part of every default pytest invocation.

## Success Criteria

1. `uv run pytest -q` exits 0 (search-mode host cases deselected by default; existing non-opt-in suite stays green).
2. Opt-in suite (mark name fixed in plan; extending Soft-Track opt-in is allowed) exits 0 under ScriptedChatModel + InMemorySaver, includes the five boundary classes named in Boundaries, and includes at least:
   - **grounded then 档:** Soft-Track `track=grounded` + high-confidence rule or pin → assigned `search_mode` in `{A,B,C,D}` **before** retrieve; non-grounded scripted greeting still `track=dialogue` with **no** search-mode assignment. **Amended by `specs/search-mode-graph-global.md` (landed):** the assignment set is `{A, graph, global, D}` — `B` / `C` are retired input spellings the host normalizes to `graph` / `global` before the port, so no audit row can ever hold them.
   - **inherit:** prior grounded run with an assigned 档 + short same-job follow-up → same 档 via `decision_tier=inherit` without a required picker.
   - **clarify window:** ambiguous grounded ask with no inherit → interrupt payload contains a structured option list (ids + labels for selectable 档); resume with a pick → that 档 is assigned, then retrieve may run.
   - **abstain or fail closed, never silent C:** the scripted 档 **Jev** Choice answers outside the closed set, returns an unparseable payload, is unreachable, is never configured (no key), or lands under the confidence floor → recorded `signals["mode_jev_exit"]` typed EXIT; terminal 档 is **not** `C` unless rules, inherit, pin, or operator pick named C; residual path is clarify **choice window** or **具名失败**, never silent C. **Amended by ADR 0081 (landed):** this is the Jev Choice EXIT family, not `schema_parse` / `model_error` from the deleted JSON classify.
   - **call-scoped copy:** after a 档 is assigned, checkpointed `messages` do not contain that turn’s search-mode usage as a durable rewritten history `SystemMessage`; a second turn with a different 档 still leaves the first checkpointed messages unedited.
   - **choice window payload:** host/HTTP (or graph interrupt) contract test asserts the payload exists with `options` (non-empty list of `{id, label}` at minimum) even if `web/` is a stub and does not paint the window.
3. Fast propose: scripted propose path emits a **choice window**; with no pick, extract method remains **standard**; host state does not set **检索档** to Fast.
4. Optional pin: request that pins `B` (or A) assigns that 档 without opening the window.
5. `compile_graph` remains one compiled graph shared by CLI / HTTP / **工作台**; v1 HTTP still uses sync `invoke` (existing **v1 surface** contract). No new four retrieve nodes appear on that graph.
6. No GraphRAG identifiers in this spec’s production code paths or test names added for this increment.

## Open Questions

(none — assumption gate confirmed 2026-09-17)

## Inherits / Changes

**Inherits:** `specs/soft-track.md` four tracks + `decide_track` cascade in `src/fin/host/track_decision.py`; `TrackDecision` / `RunRecord.track_decision`; call-scoped prompts (`agent_instructions.messages_for_model`); **status snapshot** append-only history; ScriptedChatModel + InMemorySaver; ADR 0027, ADR 0061, ADR 0062, ADR 0068, ADR 0079; CONTEXT terms quoted above; `specs/first-closed-loop.md` 具名失败 / 档未开 as product exits (codes may still be plan-named); **工作台** research-thread HTTP on the same compiled **host graph**.

**Changes this spec adds:** search-mode cascade after **grounded**; auditable search-mode decision on the run; call-scoped 档 injection + post-compaction re-inject; **choice window** interrupt payload + **工作台**/CLI resume; optional pin; Fast **propose** window that cannot silent-apply; opt-in tests extending Soft-Track.

**Does not change:** retrieve ranking; extract/Leiden jobs; Soft-Track track enum; citation verifier; checkpoint message history rewrite policy (still forbidden).

**Landed contract notes (implementation decisions, recorded 2026-09-20):**

1. **Rules-tier marker adjacency:** an explicit 档 letter or CONTEXT label triggers the rules tier only adjacent to a `档` marker (e.g. `B档` / `档B` / `知识问答档`); a bare label with no marker falls through to classify / inherit / clarify — legal cascade paths, never silent-C.
2. **`/ask` on a paused run:** the unattended v1 surface answers typed `409 {"error":"choice_pending"}`; it never picks a 档 on the operator's behalf.
3. **Pin field:** send body `tiers` with exactly one OPEN 档 is the documented pin (assumption 4's "request/state field"): assigns that 档 without opening the window; `["C"]` / `["D"]` keep the existing 具名 400.
4. **Fast propose seed:** send body optional `propose_extract_method:"fast"` seeds the propose window on grounded turns; absent field keeps the run byte-identical to before, and the seed is run-scoped (never leaks to the next send).
5. **CLI on a paused run:** the CLI renders the paused window (title + options + where to pick) instead of the terminal status/answer/citations triple; interactive CLI pick is not this increment (pick happens on the 工作台 / HTTP resume).

## 待写入 (persist after ACR)

empty — CONTEXT **检索档** / **HITL** / **v1 surface** / **Fast 抽图** / **grounded** and ADRs 0061 / 0062 / 0068 / 0079 already landed.

## architecture-change-reviewer

Affected surfaces: `src/fin/host/track_decision.py` (pattern), `graph.py`, workspace HTTP interrupt, `web/` choice-window stub.

bounded-context-guardian: yes — host graph + 工作台; ranking and extract jobs in sibling specs.
input-contract-tests: yes — Boundaries + SC2 name empty / negative / overflow / concurrent / exception.
error-handling-enforcer: yes — search-mode classify EXITs; 档未开 / 真库未接; no silent C.
complexity-anti-drift: yes — cascade helper mirrors `decide_track`; not four retrieve nodes.
minimal-change-verifier: yes — one increment (档 + choice window + Fast propose).

OVERALL: PASS — 2026-09-17 ACR (second pass after 5-class matrix). Next: writing-plans.
