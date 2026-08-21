# fin — 领域词汇表

> 本文件是项目领域词汇的**单一事实源 (SSOT)**.
> 仅 `domain-modeling` 技能可写; 其他技能按 `docs/agents/context-contract.md` 消费.

---

## Language

### Product identity

**fin**:
A bounded 10-Q research agent: the model chooses tools in a loop; code verifies citations; LangGraph only persists and resumes the run.
_Avoid_: chatbot, Q&A API, memory backend

**host agent**:
fin's role relative to iknow-memory: the caller of `retrieve`, not the search engine.
_Avoid_: client, chatbot

**iknow-memory**:
The existing TypeScript MCP memory server (separate repo). It owns ingest and retrieve algorithms; this repo must not reimplement cosine, hybrid fusion, or RRF.
_Avoid_: vector backend, knowledge base, our retrieval code

### Seams

**retrieve port**:
The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out. Same shape as iknow-memory `retrieve`, regardless of transport.
_Avoid_: search API, vector store

**retrieve adapter**:
The current in-process implementation of the retrieve port (local corpus or small index). Swappable later for an MCP client to iknow-memory without changing the host graph.
_Avoid_: mock retriever, stub, fake memory

### Agent

**agent loop**:
The model repeatedly picks a tool or a final answer until a stop condition (done, refuse, step cap). Step order is not hardcoded.
_Avoid_: pipeline, chain, fixed graph of retrieve-then-generate

**citation verifier**:
A deterministic check that each cited locator actually contains the claimed span. Failed cites send the loop back or force refuse; the model does not self-attest grounding.
_Avoid_: faithfulness prompt, LLM-as-judge as the only gate

### Host graph

**host graph**:
LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt). It is the durable runtime, not a hand-drawn retrieve → generate flowchart.
_Avoid_: retrieval graph

### Corpus and eval

**v1 corpus**:
The frozen private fixture source SEC 10-Q slice at `/home/winner/projects/fin`: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3 (12 PDFs). Upstream commit `547f132`. Cross-period questions use 2023 Q1–Q3 only.
_Avoid_: demo FAQ (pressure pack only later)

**v1 eval slice**:
A later, small gold set (fact, thin cross-period, refuse) run by a harness. Not a 195-question commitment; allowlist cases stay out of v1.
_Avoid_: 40-question matrix, permission leak tests, corpus view
