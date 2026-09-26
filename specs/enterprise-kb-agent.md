# Spec: enterprise-kb-agent

> **Identity banner (2026-09-06; wording ruled by [ADR 0090](../docs/adr/0090-product-identity-knowledge-qa-entry-point.md)):** this file is still the program contract, and **fin** in it is the enterprise **knowledge-QA** entry point — the 入口 that answers from the corpus behind the **retrieve port**, not a knowledge base. Do not replace it with the archived 2026-08 SEC-research feature spec (SEC 财报研究工作台). Frozen-12 SEC is the first corpus seed, not the product name. Current glossary: [`docs/CONTEXT.md`](../docs/CONTEXT.md).

## Assumption gate (confirmed 2026-08-27)

Operator waived per-item grilling (`/spec-driven-development` + prior thread). These are in force:

1. Product is an enterprise **knowledge-QA** entry point, not a knowledge base; the **workspace** web UI is the only product entry.
2. First knowledge corpus is SEC filings (expand in-repo; do not wait on another store).
3. Same compiled **host graph** as CLI / `POST /ask`; local no-auth `fin-serve` unchanged.
4. **citation verifier** stays a code gate; retrieve ranking stays behind the **retrieve port** (ADR 0032).
5. This file is a **coarse program contract**. `writing-plans` must split it into sequenced waves (not one commit, not one plan file that implements everything).

## Objective

Ship **fin** on the existing **workspace** as the operator's **knowledge-QA** entry point: ask in the thread, get answers grounded in the corpus with citations, grow and operate that corpus, continue work after the answer (watch, export, stop), and see that runs are auditable and the process is healthy.

Success is: each Does focus below has binary checks that later wave plans can take as their Acceptance; the operator never leaves the workspace to use those capabilities.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition):

- **fin**: A **workspace** **knowledge-QA** entry point (the 入口, not the 库): the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **grounded** track. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
- **workspace**: A local web UI server: create, list, open, resume, and delete a research thread; **thinking fold** and **tool fold** stream then collapse (`specs/turn-trace-folds.md`); citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
- **research thread**: The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
- **citation verifier**: A code gate on the **grounded** track path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Other tracks skip this gate.
- **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **Soft-Track**: The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
- **Corpus 首页**: The workspace home route (`/`): frozen-12 corpus cards and the empty-thread entry points. Distinct from cold-start restore of the last `/t/{thread_id}` — the operator may navigate here deliberately in the same session without being bounced back to that thread.
- **get_xbrl_fact**: Research-track **lookup** of one tagged financial fact from an ingest-built fact table (concept, period, issuer/document). XBRL is the reporting format, not a website. A numeric citation gate matches the claimed value to that row; the model does not invent the arithmetic. Embeddings may later resolve concept aliases; they do not author the number.
- **list_files**: Catalog tool over **filings** in the extracted tree, keyed like EDGAR submissions: CIK, form, accession, filing_date always exist on a live row; ticker and fiscal period are optional. v1 callers still pass `company` / `period`; additive optional `cik` / `form` / `ticker` / `filing_date`; hits stay `{document_id, company, period, version, page_count}` until output keys are extended; unknown or conflicting filters yield `filings: []`.
- **run**: One graph `invoke` or `stream` on a thread (`run_id`): `done` / `refuse` / `step_cap` end the run; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Crash resume continues that in-flight `run_id` from the last complete host-graph node; at most one run is active per thread.

## Boundaries

- **Does:**

  **Focus 1 — Grounded answers in the workspace**
  - Cross-period and peer comparison as **host tools** (not free-prose tables). Workspace renders the tool result on the turn (table or redline), then the **citation verifier** still gates any prose answer.
  - Numeric claims go through **get_xbrl_fact** / the numeric gate and/or table extract bound to a **locator**; the model does not invent arithmetic.
  - `refuse` / `step_cap` keep empty citations and ordinary-person copy (existing workspace contract).

  **Focus 2 — Operable corpus on Corpus 首页**
  - Catalog and home cards show more than the frozen-12 10-Q slice: additional issuers and form types (`10-K`, `8-K`) via **list_files** filters; ingest writes the extracted tree without live SEC in default pytest.
  - Home lists form / period so the operator can see what is in the library.

  **Focus 3 — Work after the answer**
  - Watchlist CRUD in the workspace (issuer + form types); reload persists.
  - In-app notice when a watched filing appears in the local catalog (no outbound email/Slack).
  - Export of a comparison or citation bundle as a file download from the thread.
  - Stop cancels the in-flight **run** on that thread (composer re-enabled); 409 concurrent send remains.

  **Focus 4 — Multi-issuer synthesize**
  - One **research thread** can ask across a capped set of issuers; the **host graph** fans out then synthesizes; every surviving claim still passes the **citation verifier**.
  - Over-cap issuer lists are a typed overflow, not a hang.

  **Focus 5 — Trust in the workspace**
  - Each terminal workspace **run** persists the locators this run retrieved or read (`evidence_locators` on `fin_run`), not only verifier-passed citations.
  - `GET /health` reports Postgres reachability when `FIN_CHECKPOINT_URL` is set (distinct from `GET /api/status`, which stays boot-scoped per ADR 0033).
  - `uv run pytest -q -m eval` remains 9 gold cases; a documented command prints pass/refuse counts (scripted, no live LLM-as-judge).

  **Test allocation (all foci, 5 classes):** empty (empty watchlist, empty compare set, `{filings: []}` / `{chunks: []}`); negative (unknown locator, unknown issuer, malformed export); overflow (issuer fan-out cap, step_cap unchanged); concurrent (409 `concurrent_run`, stop vs send); exception (ingest typed EXIT, `/health` degraded when URL set but DB down).

- **Confirms with human:** none.

- **Out of this spec:** other-agent MCP/API as a product entry; turn 👍/👎; weakening the **citation verifier**; in-repo hybrid/RRF ranking; rewriting **Soft-Track** tracks.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset; default `uv run pytest -q` stays green with no live SEC and no live memory server.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0.
2. A scripted **grounded** track turn that uses the comparison tool(s) shows a structured comparison on GET `/api/threads/{id}` (not only prose); a turn that quotes only unverified numbers does not `done`.
3. `list_files` with `form=10-K` or `form=8-K` returns `{filings: []}` on the frozen-12-only fixture (negative) and returns rows when the test corpus includes those forms (positive); **Corpus 首页** / `GET /api/corpus` exposes `form` (or equivalent) on each card.
4. Watchlist: create / list / delete via workspace API; after process restart with InMemory stand-in or Postgres test URL, list matches; empty list is `[]` not an error.
5. Stop: a scripted in-flight **run** ends without a new HumanMessage; composer accepts a later send; concurrent POST still 409 `{error: concurrent_run}` before SSE.
6. Export: one authenticated-to-local GET or POST from the thread returns a file body for citations or comparison; missing run → typed 404.
7. Fan-out: a scripted turn over N issuers at the cap synthesizes with ≥1 citation per issuer that contributed; N+1 issuers → typed overflow (not `done` with silent drop).
8. `append_run` / GET turn row includes `evidence_locators` copied from that run’s evidence before reset; a follow-up does not inherit the previous run’s locators as this run’s evidence.
9. `GET /health` with `FIN_CHECKPOINT_URL` unset is a typed degraded/skip shape (not 500 empty); with URL pointing at a down socket, degraded typed body, never a blank 500.
10. `uv run pytest -q -m eval` exits 0 (9 cases); a repo-documented command prints integer done-count and refuse-count for that suite.
11. Citation gate still fills only verified spans; `refuse` / `step_cap` citations `[]` (inherited from `specs/workspace.md`).

## Open Questions

(none)

## Inherits / Changes

**Inherits:** Python 3.12 + uv + pytest; slices `src/fin/corpus/`, `src/fin/retrieve/`, `src/fin/host/`, SPA `web/`; ScriptedChatModel + InMemorySaver default tests; marks `soft_track`, `eval`, `mcp` stay opt-in; typed EXITs `retrieve_unavailable`, `unknown_locator`, `{chunks: []}`, `{filings: []}`, 409 `{error: concurrent_run}`, 503 `{error: ingest_missing}`; ADR 0013 one compiled graph; ADR 0021 citations from verifier; ADR 0023–0026 workspace conversation; ADR 0027 Soft-Track; ADR 0032 retrieve adapter MCP client + Fake pytest; ADR 0033 `/api/status` boot-scoped (no `%` context window on that route). Prior specs: `specs/workspace.md`, `specs/soft-track.md`, `specs/s1-corpus-retrieve.md`, `specs/vertical-slice-hardening.md`. Map: [wayfinder #130](https://github.com/winter6205/fin/issues/130).

**Changes this spec adds:** comparison / watchlist / stop / export / multi-issuer fan-out / corpus form expansion / `evidence_locators` / `GET /health` / eval count command — all on the **workspace** + **host graph** + **corpus** slices. `POST /ask` stays eval adapter (sync `invoke`).

## 待写入 (persist after ACR)

empty — **fin** identity flushed to `docs/CONTEXT.md`; ADR 0034 proposed at `docs/adr/0034-host-graph-issuer-fanout.md`.

## architecture-change-reviewer

Pre-impl 5-verdict (declared structure; not line-count of unwritten code):

bounded-context-guardian: yes — new work stays in `corpus` / `retrieve` (port only) / `host` / `web`; watchlist and health are host/workspace capability, not a `controllers/` layer; retrieve ranking does not move into host.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 3–9.
error-handling-enforcer: yes — inherit existing typed EXITs; new paths named (watchlist empty vs error, export 404, fan-out overflow, `/health` degraded); no silent empty catch.
complexity-anti-drift: yes — five foci, one concern each; writing-plans must not dump all foci into one god-plan; fan-out is a graph join node, not nested in `model_node`.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences **one wave = one plan = tracer bullets**; conflict with “1 commit” is resolved by split, not by merging waves.

**OVERALL: PASS — hand to writing-plans** when the operator asks for the plan (`到时候`). Spec path: `specs/enterprise-kb-agent.md`.

Affected surfaces (declared):

- `src/fin/corpus/` — ingest/catalog/form types
- `src/fin/host/` — tools, graph fan-out, `workspace_http`, `thread_store`, compile/health
- `src/fin/retrieve/` — port/tool wiring only
- `web/src/` — home, thread, watchlist, export, stop
- `tests/corpus/`, `tests/host/`, `tests/eval/`
