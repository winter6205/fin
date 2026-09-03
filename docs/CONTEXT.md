# fin — 领域词汇表

> 本文件是项目领域词汇的**单一事实源 (SSOT)**.
> 仅 `domain-modeling` 技能可写; 其他技能按 `docs/agents/context-contract.md` 消费.

---

## Language

### Product identity

**fin**:
A **workspace** knowledge-base Agent: the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. First corpus is operator-held SEC filings; retrieve ranking stays off-graph behind the **retrieve port**.
_Avoid_: research-agent product, Q&A API as the product, memory backend, retrieval graph, treating research-track contracts as the whole product personality

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
A separate TypeScript MCP memory server (own repo and release train). It may supply institutional or cross-session durable memory behind fin's **memory backend**, and may also back filing **retrieve** when configured — those are separate adapters/channels. fin's filing cite tree does not wait on it.
_Avoid_: treating fin as the long-term vector backend, lockstep releases with fin, collapsing retrieve MCP and memory MCP into one config key, mirroring fin consumer ADRs into that repo

**memory port**:
The only dependency fin is allowed for switchable session/buffer memory operations (extract, recall, GC, dream transport): port in, typed results out — MCP-shaped for the durable backend, transport-agnostic. Default product boot leaves memory **OFF** (no tools, no dream).
_Avoid_: stuffing memory into the retrieve port, dual-writing memory bodies into the checkpointer as SSOT, treating memory recall as citation evidence

**memory backend**:
The implementation behind the memory port. Local Postgres buffer is the in-fin working store; an optional MCP server (often iknow-memory) receives **dream** transports for durable persistence. Swapping MCP changes the adapter only.
_Avoid_: requiring MCP for memory ON, storing cite-tree pages as memory entries, using Redis as memory SSOT

**dream** (fin):
A rule-gated consolidate step that selects useful local buffer memory and transports it to the configured **memory backend** MCP for durable persistence. With memory OFF or MCP absent it skips or stops at the Postgres buffer; it must not fail the foreground turn.
_Avoid_: running dream inside model_node, auto-writing every turn to MCP, using dream to move filing locator units out of fin

### Seams

**retrieve port**:
The only retrieve dependency fin is allowed to have: `query` (+ optional filters) in, ranked chunks with `source_ref` out — MCP-shaped, transport-agnostic.
_Avoid_: search API, vector store, conflating with the memory port

**retrieve adapter**:
The implementation behind the retrieve port. Default pytest uses a Fake port. When configured, product retrieve is an MCP 2.0 client (protocol-testable against a compliant public or fixture server); hits map to fin locators as `source_ref`. In-process BM25 is archived (ADR 0007; ADR 0030 superseded by 0032). Ranking stays outside fin; **切分** is fin's (ADR 0037) and the retrieval service stores and returns pushed chunk text byte-for-byte without re-splitting. The extracted cite tree stays in fin.
_Avoid_: shipping BM25 as the product path, hybrid/RRF implemented in this repo, re-splitting / truncating / rewriting pushed chunk text on the retrieval side, moving the cite tree into a memory server, compile-time coupling to one memory repo, reusing the memory-backend URL as the retrieve adapter by implication

**降级标记** (`degraded`):
检索响应信封上的布尔字段 `degraded`（连同同处的 `degraded_reasons` 有限枚举列表）：置位表示存在失败或无法尝试的能力臂，缺省即未降级。已尝试而未命中、以及调用方显式关闭的能力都不置位；fin 必须读取该标记，为真时按部分失败语义处置，不得把降级当合法空结果。
_Avoid_: 空结果, 静默回退, 确定性伪向量兜底, fail-open 重试, 另立名称（如 `partial` / `fallback` / 中译字段名）

### Retrieval stack

**精度层**:
由 Elasticsearch 承担的词法能力层（中文分词、Okapi BM25、短语查询、字段加权），与向量互补，在「找实体」与回退两处都在场。不是向量的备路，不做主备切换。
_Avoid_: 关键词库, 备用召回, Postgres 全文检索, 第二套词法排序实现

**权威源**:
块文本与其元数据的唯一权威副本，由 Postgres 承担；只保持「权威数据 + 一个向量扩展」的最小职责，不承担词法排序。
_Avoid_: 缓存, 索引, 把派生索引当权威, 让权威源随索引格式搬迁

**派生索引**:
向量索引与词法索引，是权威源的派生物；可删除、可从权威源重灌，重灌不影响权威数据。因此向量库可平滑升级而不动权威数据。
_Avoid_: 第二份数据, 备份, 与权威源互为校验的对等副本

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

**compare_facts**:
Research-track host tool that resolves one XBRL concept over a grid of rows (issuers or documents) x columns (periods) against the same fact table `get_xbrl_fact` reads, and returns the `fin.comparison.v1` envelope. Deltas are computed in the tool, not by the model. Every column is an implicit `(concept, period)` claim for the numeric gate, so an answer quoting figures the table never reported cannot `done`. It contributes no citation evidence: a fact row is not page text.
_Avoid_: a prose table the model authors, a chart tool, treating a comparison cell as a citation, computing the delta in the SPA or the export

**comparison result** (`fin.comparison.v1`):
The JSON envelope `compare_facts` returns and the workspace persists on the turn row (`TurnRow.comparisons`) and streams on the `comparison` SSE frame: `{schema, kind, axis, concept, unit, scale, columns[], rows[].cells[], deltas[], missing[], hit_count, cell_count, locators[]}`. Shape SSOT is `src/fin/host/tools/compare_facts.py`. `empty_comparison` / `comparison_overflow` are typed EXITs carrying a column-less skeleton, not exceptions and not renderable tables.
_Avoid_: a markdown table on the wire, re-parsing the answer prose for structure, per-surface variant shapes

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

**reference checkout**:
Read-only sibling clone of the reference implementation (workspace folder, not fin git). Consulted, never edited as fin source.
_Avoid_: vendoring under fin/, copying that tree into src/fin

**v1 model**:
The agent-loop LLM is `ChatOpenAI` selected at runtime by `FIN_MODEL` (default `MiniMax-M3`) and `FIN_BASE_URL` (default official MiniMax CN Chat Completions `https://api.minimaxi.com/v1`; the URL includes `/v1`). Keys stay in environment variables (`FIN_API_KEY`); slash ids do not go through `init_chat_model`.
_Avoid_: compiled-in model name as the only option, `init_chat_model` for slash ids, `minimax-cn/MiniMax-M3` as the v1 default, LLM-as-judge as the citation gate

**FIN_API_KEY**:
项目自有的 LLM API key 环境变量名（2026-08-27 由 `ANTHROPIC_AUTH_TOKEN` 硬切而来，无 fallback）
_Avoid_: vendor-prefixed legacy names (`ANTHROPIC_AUTH_TOKEN`), a fallback to the legacy name, hard-coding the key, shipping a baked-in default key, treating an unset key as a working credential

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
A CLI that writes frozen locator-unit text from official filings (or the existing extracted tree) into an extracted tree. 滑动窗口式**切分**是 fin 的职责而非排序栈（ADR 0037），排序仍留在 fin 之外；the ask path does not fetch EDGAR or open source filings.
_Avoid_: extract-on-query, host-graph ingest node, putting filing bytes in the checkpointer database

**object store**:
MinIO/S3-compatible storage for raw filing (and later multi-format) bytes. Postgres holds metadata and pointers; the extracted cite tree stays in fin. Git is not the byte warehouse once configured.
_Avoid_: Postgres bytea as the primary raw store, putting raw PDFs in the checkpointer, treating object keys as citation locators

**contract eval**:
Default CI harness: scripted model, **eval fixture** only. Required classes: HTML-unit citation pass, unknown locator refuse, catalog miss, cross-period on the fixture. Nine v1 gold cases are not the ceiling.
_Avoid_: live LLM in default pytest, FinanceBench in default pytest

**capability eval**:
Opt-in or nightly harness on **product corpus** plus a live model; records coverage, abstain, and citation quality. Not a per-commit gate.
_Avoid_: web-search text as citations, making this the only eval

**v1 eval slice**:
Historical nine hand-authored cases against the private fixture source 12 (ADR 0012, superseded by 0029).
_Avoid_: 40-question matrix as a v1 commitment, treating nine cases as the current ceiling

**冻结单元**:
fin 解析产出的最小可引用文本单位；文本一经产出即冻结，是 **citation verifier** 逐字符比对的基准。解析规则版本或参数表版本变更即产生新版本单元。
_Avoid_: chunk, 原始文件, 任意段落, 未冻结的中间解析产物

**切分**:
fin 把冻结单元文本切成块的过程与规则，是 fin 的职责；切分必须确定性可复现，同一（字节输入 + 规则版本 + 参数表版本）两次切分产物逐字节相同。检索服务对推送文本不做二次切分、截断、重组或归一化改写。
_Avoid_: chunking, sliding window, 检索侧切分, 按字符硬切, 模型判断切点

**父子分块**:
fin 切分的产物形态：子块用于精准匹配与引用定位，父块用于给模型完整上下文，命中集是子块且每个子块携带其父块文本。子块文本是父块文本的连续子串，父块文本是冻结单元文本的连续子串；父块不单独作为可命中单元。
_Avoid_: 双层切分, 段落切分, 父块参与命中, 父子共用一份可召回文本

**场景适配颗粒度**:
按（文档类型 × 板块 × 内容密度）查一张确定性参数表决定分块参数的做法；同一三元组两次查表返回同一参数组，参数表版本进元数据、版本变更就地全量重建。
_Avoid_: 自适应切分, 固定统一块长, 用模型判断颗粒度, 双版本参数并存

**结构化元数据**:
随块一同推送、可被检索侧作为过滤条件消费的字段集，至少包含格式、来源字节哈希、locator 三元组、单元序号、字符数、语言、解析规则版本、参数表版本。缺字段的推送被拒并返回具名失败。
_Avoid_: 任意 JSON 载荷, 标签云, 与块文本分离存放的第二份属性

**入库推送**:
fin 把切分后的块及其结构化元数据以幂等方式推送给检索服务的动作；推送文本即最终文本，批量、可断点续传，失败具名返回不静默。
_Avoid_: 检索侧自拉, 实时单条写入, 本地落盘即算完成, 静默重试

**幂等键**:
推送去重的身份，是（`source_ref`，内容标识）二元组；内容标识为（块文本 + 归一化规则版本 + 解析规则版本 + 参数表版本）的确定性哈希。同一幂等键重复推送不产生第二份写入，返回同一批块标识。
_Avoid_: 块标识, request id, 时间戳去重, 内容无关的批次号

**内容失效**:
冻结单元文本变更时由 fin 对该单元旧块发起的失效动作；检索侧关闭其时间有效区间，使其不再被任何查询时刻命中，不物理删除、可重复执行并留下可查询记录。
_Avoid_: 物理删除, 覆盖写, 多版本并存, 重建后才算失效

**评测数据集**:
按跨仓统一口径构建的多格式、多类型真实文档集合及其标注与查询集；样本须真实、带 provenance，且可一键重建。与 **eval fixture** 不同：后者是 git 冻结的快照，前者是规模与构成受契约约束的语料资产。
_Avoid_: eval fixture, 合成语料, 公开 benchmark 榜单, 占位文本

**真实语料注入**:
把真实（非合成、非占位）语料按来源与许可记录引入**评测数据集**的过程；缺来源或标记为合成的样本不计入规模。
_Avoid_: 合成生成, 占位文本, 无许可抓取, 以数量凑规模

**并发评测**:
在统一的并发数、持续时长与读写比下跑检索与推送，并把 p50 / p95 / p99 与错误数落盘的评测场景；含读为主的主场景与全写的幂等场景，错误数须为 0。
_Avoid_: 压测, 负载测试, 单机串行基线, 只报均值

**消融**:
逐个关闭单个能力项、与全开 / 全关基线对照的评测方法；每个配置重复多次并落原始值与中位数，结论须同时给出百分比与绝对值。
_Avoid_: A/B 测试, 参数扫描, 定性对比, 只给百分比的结论
