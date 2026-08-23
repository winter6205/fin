# fin — 领域词汇表

> 本文件是项目领域词汇的**单一事实源 (SSOT)**.
> 仅 `domain-modeling` 技能可写; 其他技能按 `docs/agents/context-contract.md` 消费.

---

## Language

### Product identity

**fin**:
A bounded 10-Q research agent: the model chooses tools in a loop; code verifies citations; LangGraph is the host graph (loop, code gates, checkpoint/resume). Retrieve ranking stays off-graph behind the retrieve port.
_Avoid_: chatbot, Q&A API, memory backend, retrieval graph

**host agent**:
fin's role relative to iknow-memory: the caller of `retrieve`, not the search engine.
_Avoid_: client, chatbot

**iknow-memory**:
The existing TypeScript MCP memory server (separate repo). It owns vector ingest and cosine / hybrid / RRF ranking. fin v1 is an independent system with its own retrieve adapter; a later adapter may call iknow-memory over MCP without changing the host graph.
_Avoid_: treating fin as the long-term vector backend, pausing fin until iknow-memory ships

### Seams

**retrieve port**:
The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out. Same shape as iknow-memory `retrieve`, regardless of transport.
_Avoid_: search API, vector store

**retrieve adapter**:
v1's real in-process retrieve: lexical BM25 over page-passage chunks, each carrying a locator as `source_ref`. Not a stub. Swappable later (for example an MCP client to iknow-memory) without changing the host graph.
_Avoid_: mock retriever, empty adapter, whole-page-only as the only chunk grain, cosine as v1's ranking

### Agent

**agent loop**:
The model repeatedly picks `list_files`, `retrieve`, or `read_file` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. `done` is only after the citation verifier passes; step order is not hardcoded.
_Avoid_: pipeline, chain, fixed graph of retrieve-then-generate

**list_files**:
v1 catalog tool over the frozen 10-Qs. Optional `company` / `period` filters; each hit is `{document_id, company, period, version, page_count}`; unknown filters yield `filings: []`.
_Avoid_: list_filings, list_documents, glob

**read_file**:
v1 read of a frozen filing by `document_id` and `version`, with 1-based `page` (default 1) and `page_limit` (default 1, cap 3). Misses return `{ error: "unknown_locator" }`; the citation verifier still checks one page per cite.
_Avoid_: read_locator, read_page, cat

**locator**:
v1 cite identity: `document_id`, `version` (that PDF's sha256 in the corpus manifest), and 1-based `page` from pdfplumber. The claimed span is a quote string, not a locator field; the citation verifier checks it is a contiguous substring of the frozen page text. Retrieve chunks carry a locator as `source_ref`; a chunk is not the identity.
_Avoid_: chunk_id, char offset, bbox, page-less citation

**citation verifier**:
A code gate on the path to END: a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears on a page this run retrieved or read. Passed spans become the user-visible citations; unknown locators refuse immediately; missing or unmatched spans bounce with a hidden template until the citation-repair cap, then refuse.
_Avoid_: faithfulness prompt, LLM-as-judge as the only gate, model-authored citation block, `verify` as a tool

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
The product conversation: one `thread_id` the operator talks in; each question is a run; follow-ups keep messages and citations. This is how fin is used after v1; it is not a chatbot identity and not a one-shot `/ask` demo.
_Avoid_: chatbot, Q&A session, demo ask, one-shot invoke as the product UX

**workspace**:
A local web UI server: create, list, open, resume, and delete a research thread; thinking streams unframed then collapses; tool calls stream then collapse as human-readable traces; citations sit on the assistant turn (inline chips plus a sources list). Streaming HTTP is that UI's transport on the same compiled graph; GET history is turn rows; thinking on/off is the model `extra_body.thinking` knob; crash resume continues the in-flight run from the last complete host-graph node.
_Avoid_: chatbot, CLI REPL as the product, hosted accounts this increment, raw tool JSON as the UI, always-expanded traces, Studio, POST /ask as the conversation surface, rolling back a user turn on crash

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

**v1 corpus**:
The frozen private fixture source SEC 10-Q slice at `/home/winner/projects/fin`: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3 (12 PDFs). Upstream commit `547f132`. Cross-period questions use 2023 Q1–Q3 only.
_Avoid_: demo FAQ (pressure pack only later)

**ingest**:
A command that writes frozen page text and page-passage chunks from those PDFs into `data/corpus/extracted/`. `read_file`, retrieve, and the citation verifier read only that tree; the ask path does not open a PDF.
_Avoid_: extract-on-query, putting page text in the checkpointer database

**v1 eval slice**:
Nine hand-authored gold cases after tools exist: three fact, three thin cross-period (2023 Q1–Q3 only), three refuse. Not imported wholesale from `qna_data.csv`; allowlist and broader retrieval evaluations stay out of v1.
_Avoid_: 40-question matrix, permission leak tests, 195-question commitment, corpus view
