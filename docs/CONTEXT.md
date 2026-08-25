# fin — 领域词汇表

> 本文件是项目领域词汇的**单一事实源 (SSOT)**.
> 仅 `domain-modeling` 技能可写; 其他技能按 `docs/agents/context-contract.md` 消费.

---

## Language

### Product identity

**fin**:
A normal conversational agent whose durable runtime is a LangGraph host graph: each turn is routed to a **track**; only the **research track** specializes in the frozen 10-Q corpus with tools and the citation verifier. Retrieve ranking stays off-graph behind the retrieve port.
_Avoid_: Q&A API, memory backend, retrieval graph, treating research-track contracts as the whole product personality

**host agent**:
fin as the LangGraph runtime that routes tracks and calls research tools behind the **retrieve port**; it is not the ranking or vector store.
_Avoid_: client, treating fin as only a FAQ bot, treating fin as the long-term memory server

**track**:
The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `research`. Chosen in-graph before execution; tools and the citation verifier bind only to `research`.
_Avoid_: Chat|Research UI mode, HTTP-only router, single global research contract for every utterance

**Soft-Track**:
The v1 turn-orchestration shape: one compiled host graph, multi-track routing, tiered track decision (rules → ambiguous LLM → inherit → grounded clarify → conditional policy), flat graph until upgrade triggers in ADR 0027.
_Avoid_: supervisor handoff as v1, dual product shells, treating flat topology as permanent

**status snapshot**:
One append-only per-turn record of research working context (e.g. company, period, document ids, active track cues) stored on `AgentState` in the checkpointer. History is retained; the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot.
_Avoid_: overwriting away prior snapshots, dual-writing into thread_store as SSOT, replacing full message history with snapshots

**iknow-memory**:
A separate TypeScript MCP memory server (own repo and release train). It may later supply institutional or cross-session memory behind fin's retrieve adapter; fin's filing vertical slice does not wait on it.
_Avoid_: treating fin as the long-term vector backend, lockstep releases with fin, mirroring fin consumer ADRs into that repo

### Seams

**retrieve port**:
The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
_Avoid_: search API, vector store

**retrieve adapter**:
The implementation behind the retrieve port. Default pytest uses a Fake port. When configured, product retrieve is an MCP 2.0 client (protocol-testable against a compliant public or fixture server); hits map to fin locators as `source_ref`. In-process BM25 is archived (ADR 0007; ADR 0030 superseded by 0032). Ranking and chunking for remote memory stay outside fin; the extracted cite tree stays in fin.
_Avoid_: shipping BM25 as the product path, hybrid/RRF implemented in this repo, moving the cite tree into a memory server, compile-time coupling to one memory repo

### Agent

**agent loop**:
On the **research track**, the model repeatedly picks `list_files`, `retrieve`, `read_file`, or `get_xbrl_fact` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Research `done` is only after the citation verifier (and numeric fact gate when a fact is claimed) passes; non-research tracks may `done` without tools or citations. Step order is not hardcoded.
_Avoid_: pipeline, chain, fixed graph of retrieve-then-generate, requiring the citation verifier on dialogue/clarify/off_topic

**list_files**:
Catalog tool over **filings** in the extracted tree, keyed like EDGAR submissions: CIK, form, accession, filing_date always exist on a live row; ticker and fiscal period are optional. v1 callers still pass `company` / `period`; additive optional `cik` / `form` / `ticker` / `filing_date`; hits stay `{document_id, company, period, version, page_count}` until output keys are extended; unknown or conflicting filters yield `filings: []`.
_Avoid_: list_filings, list_documents, glob, requiring ticker, treating company as forever equal to ticker, a generic CMS file browser

**get_xbrl_fact**:
Research-track **lookup** of one tagged financial fact from an ingest-built fact table (concept, period, issuer/document). XBRL is the reporting format, not a website. A numeric citation gate matches the claimed value to that row; the model does not invent the arithmetic. Embeddings may later resolve concept aliases; they do not author the number.
_Avoid_: treating XBRL as a site name, OCR or vision as the number SSOT, a `verify` tool, using retrieve/embeddings as the fact SSOT

**XBRL**:
The tagged-numbers format inside official 10-Q/10-K iXBRL. Not a website; SEC.gov is the filing host.
_Avoid_: using XBRL as a product or hostname, conflating it with iknow-memory retrieve

**read_file**:
v1 read of a frozen filing by `document_id` and `version`, with 1-based `page` (default 1) and `page_limit` (default 1, cap 3). Misses return `{ error: "unknown_locator" }`; the citation verifier still checks one page per cite.
_Avoid_: read_locator, read_page, cat

**locator**:
Cite identity `{document_id, version, unit}`: for the frozen 12, `version` is the PDF sha256 and `unit` is the 1-based pdfplumber `page`; for live EDGAR, `version` hashes the official primary iXBRL `.htm` bytes and `unit` is a deterministic frozen partition of that HTML's stripped text, not an SEC or unofficial-PDF page. The claimed span is a quote string, not a locator field; the citation verifier checks it is a contiguous substring of that unit's frozen text; chunks carry the locator as `source_ref` and are not the identity.
_Avoid_: chunk_id, char offset, bbox, page-less citation, unofficial PDF page as live EDGAR identity, verifying an HTML quote against a PDF page

**citation verifier**:
A code gate on the **research track** path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in a frozen locator unit this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse. Non-research tracks skip this gate.
_Avoid_: faithfulness prompt, LLM-as-judge as the only gate, model-authored citation block, `verify` as a tool, running the gate on greetings or soft off-topic replies, web-search text as gate evidence

**citation span**:
One user-visible citation row from the verifier: a ≥32-character contiguous substring of the answer that matched on a retrieved or read page. The workspace sources list may show many spans per page (sliding-window overlap); that granularity is expected, not a send failure.
_Avoid_: treating each span as a full sentence, hiding overlap as a bug, conflating span count with page count

**citation-repair cap**:
v1 limit of 3 recoverable citation-verifier failures (span not on the page, or a non-refuse final answer with no cites) before the run must refuse.
_Avoid_: retry budget, max repairs, using step cap as the cite-fail rule

**step cap**:
v1 outer fuse of 32 agent-loop steps (counted in fin state, not LangGraph `recursion_limit`). Exhaustion stops the run as `step_cap`, not as a citation-verifier failure.
_Avoid_: timeout, token budget, treating this number as the cite-repair limit or as `recursion_limit`

### Host graph

**host graph**:
LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
_Avoid_: retrieval graph

**checkpointer**:
v1 durable runs use LangGraph `PostgresSaver` via `FIN_CHECKPOINT_URL`. Pytest uses `InMemorySaver` only; it does not survive process exit.
_Avoid_: SqliteSaver as the product default, InMemorySaver in CLI/HTTP, LangSmith-hosted persistence

**thread_id**:
A conversation session: follow-up questions reuse this id so checkpoints and messages accumulate. Omitting it creates a new session before invoke; it is not a user_id.
_Avoid_: one question per thread, treating a refuse as killing the session

**run**:
One graph `invoke` or `stream` on a thread (`run_id`): `done` / `refuse` / `step_cap` end the run; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Crash resume continues that in-flight `run_id` from the last complete host-graph node; at most one run is active per thread.
_Avoid_: collapsing run into thread_id, concurrent invokes on one thread, deleting the user turn to “sanitize” a crash

**research thread**:
The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
_Avoid_: Q&A session, demo ask, one-shot invoke as the product UX, forbidding greetings or clarification on the thread

**workspace**:
A local web UI server: create, list, open, resume, and delete a research thread; thinking streams unframed then collapses; tool calls stream then collapse as human-readable traces; citations sit on the assistant turn (inline chips plus a sources list); research turns may show a light track hint without a mode picker. Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
_Avoid_: CLI REPL as the product, hosted accounts this increment, raw tool JSON as the UI, always-expanded traces, Studio, POST /ask as the conversation surface, rolling back a user turn on crash, Chat|Research dual chrome

**Corpus 首页**:
The workspace home route (`/`): frozen-12 corpus cards and the empty-thread entry points. Distinct from cold-start restore of the last `/t/{thread_id}` — the operator may navigate here deliberately in the same session without being bounced back to that thread.
_Avoid_: conflating home with "last thread", auto-send from a corpus card, treating fetch of `GET /api/corpus` as ingest

**v1 surface**:
The first ship binary is a CLI that compiles the host graph; v1 HTTP exposes that same compiled graph and must call sync `invoke` (not `ainvoke`) so the sync `PostgresSaver` stays valid. HITL / interrupt UX is not a v1 product surface; crash resume continues the in-flight run on that `thread_id`.
_Avoid_: HTTP-only first ship, separate graphs per transport, approval UI as v1

**HITL**:
LangGraph `interrupt` may be compiled into the host graph. v1 does not ship plan-approval or tool-approval UI; durability is crash resume of the in-flight run.
_Avoid_: declarative plan-mode as v1, Studio as the required v1 interface

**v1 model**:
The agent-loop LLM is `ChatOpenAI` selected at runtime by `FIN_MODEL` (default `fin/MiniMax-M3`) and `FIN_BASE_URL` (OpenAI-compatible Chat Completions; the URL includes `/v1`). Keys stay in environment variables; slash ids do not go through `init_chat_model`.
_Avoid_: compiled-in model name as the only option, `init_chat_model` for slash ids, `minimax-cn/MiniMax-M3` as the v1 default, LLM-as-judge as the citation gate

**model client**:
LangChain `ChatModel` (`ChatOpenAI` in v1) plus the messages and tool-calling protocol (`bind_tools` / tool schema). LangGraph orchestrates the run; it does not replace this layer.
_Avoid_: raw vendor HTTP as the v1 client, treating LangGraph as the chat SDK, `init_chat_model` as the required constructor

**loop skeleton**:
v1's host graph is authored in fin: model node, tool node, citation verifier, caps. It does not copy a research or RAG product repository into `src/fin`.
_Avoid_: gpt-researcher, open_deep_research, blank-file StateGraph as the required path, forking a finance-agent demo

### Corpus and eval

**product corpus**:
The operator-selected local extracted tree of official 10-Q and 10-K primary HTML. `fin-serve` retrieve, `read_file`, and the citation verifier read this tree under the live locator contract.
_Avoid_: private fixture source 12 as product SSOT, unofficial PDF as live identity, S&P 500 backfill as the ingest default, a second PDF-page product path

**eval fixture**:
A git-pinned snapshot of **ingest** output, same locator contract as **product corpus**. Default CI contract eval reads only this tree.
_Avoid_: pdfplumber-only test ingest, live EDGAR in default pytest, treating the private fixture source 12 PDFs as eval SSOT

**v1 corpus**:
Historical private fixture source 12-PDF freeze (ADR 0004, superseded by 0029). Not the product or eval SSOT.
_Avoid_: current catalog, gold SSOT, demo FAQ

**ingest**:
A CLI that writes frozen locator-unit text from official filings (or the existing extracted tree) into an extracted tree. Retrieve sliding windows are not fin's ranking stack (ADR 0030). The ask path does not fetch EDGAR or open source filings.
_Avoid_: extract-on-query, host-graph ingest node, putting filing bytes in the checkpointer database

**contract eval**:
Default CI harness: scripted model, **eval fixture** only. Required classes: HTML-unit citation pass, unknown locator refuse, catalog miss, cross-period on the fixture. Nine v1 gold cases are not the ceiling.
_Avoid_: live LLM in default pytest, FinanceBench in default pytest

**capability eval**:
Opt-in or nightly harness on **product corpus** plus a live model; records coverage, abstain, and citation quality. Not a per-commit gate.
_Avoid_: web-search text as citations, making this the only eval

**v1 eval slice**:
Historical nine hand-authored cases against the private fixture source 12 (ADR 0012, superseded by 0029).
_Avoid_: 40-question matrix as a v1 commitment, treating nine cases as the current ceiling
