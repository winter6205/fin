# Spec: v1 host agent

## Objective

Ship **fin** v1: a bounded 10-Q research agent. The operator asks a question on the CLI (and the same compiled graph over HTTP). The model selects `list_files`, `retrieve`, or `read_file` in a loop. Code verifies that the answer is grounded in pages this run actually fetched, then fills the citation list from those passed checks. LangGraph is the durable **host graph** (checkpoint / resume / caps), not a retrieve-then-generate flowchart.

Success is a clone-to-ask path: ingest artifacts already in git, Postgres up, `python -m fin` returns `done` with code-filled citations, or `refuse` / `step_cap` as specified.

Quoted from `docs/CONTEXT.md`:

- **fin**: A bounded 10-Q research agent: the model chooses tools in a loop; code verifies citations; LangGraph is the host graph (loop, code gates, checkpoint/resume). Retrieve ranking stays off-graph behind the retrieve port.
- **host graph**: LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
- **v1 corpus**: The frozen private fixture source SEC 10-Q slice at `/home/winner/projects/fin`: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3 (12 PDFs). Upstream commit `547f132`. Cross-period questions use 2023 Q1–Q3 only.

## Boundaries

- **Does:**
  - Python 3.12 package at `src/fin` (uv + pytest).
  - Ingest command that turns the frozen 12 PDFs into page text + page-passage chunks under `data/corpus/extracted/` (committed). Query path never opens a PDF.
  - Pure-function tools `list_files`, `retrieve`, `read_file` per ADR 0009, over extracted pages; BM25 (`rank-bm25` or equivalent in-process lexical) built in RAM at process start.
  - Host graph authored in this repo: model node + ToolNode + citation gate on the path to END + step cap 32 in fin state. LangChain `ChatOpenAI` via `FIN_MODEL` + `FIN_BASE_URL` (ADR 0022, default official MiniMax Chat Completions). Sync compile with `PostgresSaver` (`FIN_CHECKPOINT_URL`); tests use `InMemorySaver`.
  - Citation gate (before the run is `done`): evidence is locators+text from this run's `retrieve` / `read_file` results. A supporting span is a contiguous substring of the answer, length ≥ 32, that appears on an evidenced page. Pass → code fills the user-visible citations from those spans. Recoverable fail → template bounce, hidden from CLI/HTTP user rendering, back to the model. Unknown locator → `refuse` immediately. Repair cap 3 then `refuse`. `step_cap` is a distinct stop.
  - CLI `python -m fin`: question in, stdout of `status`, answer prose, code-filled citations; optional `--thread-id`.
  - Thin FastAPI HTTP on the same compiled graph, sync `invoke`, no auth, localhost.
  - `docker-compose` for Postgres only.
  - Unit tests with a scripted ChatModel (no live LLM). Nine gold cases as pytest mark `eval` (needs keys; not in default `pytest -q`).
- **Confirms with human:** none remaining (assumption gate 1–11 confirmed 2026-08-22).
- **Out of this spec:**
  - MCP client to iknow-memory; cosine / hybrid / RRF; `verify` as a model-facing tool.
  - LLM goal evaluator / LLM-as-judge for citation failures.
  - HITL / plan-approval / tool-approval UI; LangGraph Studio as a required surface.
  - Source allowlist, 40-question matrix, broader retrieval evaluations, wholesale `qna_data.csv` gold.
  - Auth, LangSmith as required, streaming `ainvoke` as the v1 durable path.

## Success Criteria

- `uv run pytest -q` exits 0 (no `eval` mark; no network LLM).
- Ingest is idempotent: running it against the frozen PDFs writes `data/corpus/extracted/` with one page-text object per manifest `document_id` × 1-based page, plus passage chunks each carrying a **locator** `{document_id, version, page}`; a second run does not change checksums of those files.
- Query/runtime code has no `pdfplumber` (or PDF) import on the ask path; missing extracted artifacts fail fast with a typed ingest-missing error, not a silent extract.
- `list_files` with unknown `company` / `period` returns `{filings: []}`; `retrieve` with empty query or unknown `document_id` returns `{chunks: []}`; `read_file` with unknown id, version mismatch, or out-of-range page returns `{error: "unknown_locator"}` and does not throw.
- Scripted **research-track** run that never calls retrieve/read_file and then emits a non-refuse answer is not `done`: the gate bounces (repair count 1) with a hidden template; after three such recoverable failures the run status is `refuse` with reason `citation_repair_cap`. (After Soft-Track / ADR 0027, non-research tracks may `done` without tools or citations and must not enter the citation verifier — see `specs/soft-track.md`.)
- Scripted run whose answer contains a ≥32-char span copied from an evidenced page ends `done`; stdout/HTTP body citations equal exactly those passed spans and their locators (no extra, no model-invented `version`).
- Scripted final payload that cites a locator not in this run's evidence (and not in the frozen corpus) ends `refuse` with reason `unknown_locator` on the first failure (no repair).
- A run that executes 32 agent-loop steps without `done`/`refuse` ends with status `step_cap` (not `refuse`); LangGraph `recursion_limit` is > 32 so this fuse fires first.
- `python -m fin --help` exits 0; `python -m fin "q"` with scripted model and `InMemorySaver` prints `status` then answer then citations.
- `docker compose config` exits 0 and defines a Postgres service whose URL matches the `FIN_CHECKPOINT_URL` example in `.env.example`.
- HTTP POST to the v1 ask route uses the same compiled graph object as the CLI (identity or equivalent compile helper); the handler calls sync `invoke`, not `ainvoke`.
- `uv run pytest -q -m eval` is collected (≥9 cases: 3 fact, 3 cross-period 2023 Q1–Q3, 3 refuse) and skipped or selected only with `-m eval`.

## Open Questions

(none — assumption gate cleared all)

## Inherits / Changes

**Inherits (workspace today):** frozen 12 PDFs + `data/corpus/manifest.json` (ADR 0004); retrieve port (ADR 0001); LangGraph runtime (ADR 0002); thin slice (ADR 0003); locator = page identity + quote span (ADR 0006); in-process BM25 adapter (ADR 0007); verifier failure policy (ADR 0008); tool contracts (ADR 0009); CLI-first + HTTP same graph (ADR 0013); LangChain model client (ADR 0014); `ChatOpenAI` + `FIN_MODEL` / `FIN_BASE_URL` default `MiniMax-M3` (ADR 0022); `PostgresSaver` / test `InMemorySaver` (ADR 0015); `thread_id` session / `run` = one invoke (ADR 0016); no HITL UX (ADR 0018). Application package is in `src/fin`.

**Confirmed stack (assumption gate):** Python 3.12, uv, `src/fin`, pytest, FastAPI HTTP, `rank-bm25` (or equivalent), extracted tree at `data/corpus/extracted/` committed, extractive span ≥ 32 characters, CLI `python -m fin`, default tests without live LLM, `eval` mark for nine gold cases.

**Capability slice (not technical-layer dirs):** `src/fin/corpus/` (ingest + extracted catalog/pages), `src/fin/retrieve/` (port + BM25 adapter), `src/fin/host/` (graph, tool wrappers, citation gate, caps, CLI, HTTP). No `controllers/` / `services/` / `repositories/` / `models/` top-level packages.

**Changes this spec adds:** application package; ingest artifacts; host graph; citation gate behavior described above; compose file; CLI/HTTP; unit + `eval` harness.

## 待写入 (persist after ACR)

CONTEXT / ADR only. Spec body does not author those files.

1. **citation verifier** (update): gate on the path to END; evidence = this run's tool results; extractive span ≥ 32 on evidenced pages; code fills user-visible citations from passed checks; recoverable fail = hidden template bounce; not an LLM judge; not a model-facing tool.
2. **agent loop** (update): stop = no tool calls; `done` only after the citation gate passes; refuse / `step_cap` unchanged in kind (ADR 0008).
3. **loop skeleton** (update): host graph is authored in fin; do not name an upstream template as the source of the graph.
4. **reference checkout** (update): reference implementation sibling only; drop `react-agent`.
5. **ingest** (new term): command that writes extracted page text + passages from frozen PDFs; query SSOT; not a tool.
6. ADR **0010** → `superseded by 0019`: host graph authored in fin (model / tools / citation gate / caps), not adapted from an external agent tree.
7. ADR **0017** body: reference implementation sibling only; no template-adapt language.
8. New ADR **0019**: write the host graph in-repo.
9. New ADR **0020**: extracted ingest artifacts are the query SSOT; BM25 in RAM at process start; no PDF on the ask path.
10. New ADR **0021**: citations are filled from gate-passed extractive spans against this run's tool evidence; bounce templates are hidden from the user surface.
11. `CLAUDE.md` 按需查阅: reference implementation sibling is read-only; do not copy its tree into `src/fin`.
12. Reword ADR 0005 / 0013 / 0018 so they do not name an upstream template as the loop source.

## Architecture change reviewer

bounded-context-guardian: yes — three capability packages (`corpus`, `retrieve`, `host`); retrieve stays behind the port; HTTP/CLI are host surfaces; no technical-layer top-level dirs.
defensive-contract-validator: yes — tools cover empty/unknown/out-of-range; gate covers missing cites, unknown locator, repair overflow (3), concurrent one-run-per-thread, missing extracted artifacts as typed exception.
error-handling-enforcer: yes — typed stop/error kinds (`unknown_locator`, `citation_repair_cap`, `step_cap`, ingest-missing); no empty catch; bounce is an explicit return to the model, not a swallowed fail.
complexity-anti-drift: yes — ingest, retrieve adapter, tool functions, model node, citation gate, and surfaces stay separate; gate is match-and-fill, not a mixed LLM judge.
minimal-change-verifier: yes — one logical v1 ship; implementation splits into tracer-bullet commits in writing-plans; this spec is one contract.

All 5 verdicts yes.
