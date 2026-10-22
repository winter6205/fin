# fin — 领域词汇表

> 本文件是项目领域词汇的**单一事实源 (SSOT)**.
> Maintained by `domain-modeling`; consumers quote definitions verbatim.
>
> Product identity disambiguation: see [ADR 0090](adr/0090-product-identity-knowledge-qa-entry-point.md) (fin is a knowledge-QA entry point, not a knowledge base and not a finance/research product).

---

## Language

### Product identity

**fin**:
An enterprise **knowledge-QA** agent (知识问答助手) (the 入口, not the 库): the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **grounded** track. First corpus is operator-held **资料** behind the **retrieve port**.
_Avoid_: research-agent product, treating fin as a finance/financial-research assistant product (finance is only the current corpus domain, not the product identity), workspace-as-kind, 10-Q as the product name, Q&A API as the product, treating grounded-track contracts as the whole product personality, treating fin as a parallel corpus store or 库门

**host agent**:
fin's LangGraph host graph, which routes tracks and calls grounded-track tools behind the **retrieve port**; it is not the application-level **Fin Runtime**, ranking service, or vector store.
_Avoid_: client, treating fin as only a FAQ bot, treating fin as the long-term memory server, treating the graph as the durable operation queue

**track**:
The per-turn contract on the host graph: `dialogue`, `clarify`, `off_topic`, or `grounded`. Chosen in-graph before execution; tools and the citation verifier bind only to `grounded`.
_Avoid_: Chat|Research UI mode, HTTP-only router, single global grounded contract for every utterance, search mode as a track

**grounded**:
The Soft-Track leaf that binds corpus tools and the **citation verifier** (ADR 0061). Not a product name, not **search mode**, and not the `retrieve` tool. Wire value `research` is an alias for this leaf.
_Avoid_: Research mode picker, retrieve as the track enum, four retrieve nodes, renaming **research thread**

**route intent**:
The product action selected for an input before execution, distinct from the graph **track** and the eventual answer outcome.
_Stage_: Adopted QA-chain contract (2026-10-16); runtime implementation and acceptance remain in the execution plans.
_Avoid_: fifth track, semantic result, search mode

**Soft-Track**:
The v1 turn-orchestration shape: one compiled host graph with rules-first routing across four tracks and optional ambiguous **Jev** hints; missing, failed or unusable hints default to authorized retrieval, while actionable missing user conditions may still clarify. Route intent and semantic outcomes are separate; flat topology retains ADR 0027 upgrade triggers.
_Stage_: Adopted routing amendment under ADR 0080 (2026-10-16); default pytest remains Fake, and runtime acceptance remains pending.
_Avoid_: supervisor handoff as v1, dual product shells, treating flat topology as permanent, MiniMax JSON as the product gray-zone classifier, MiniMax as Jev fallback

**status snapshot**:
One append-only per-turn record of research working context (e.g. company, period, document ids, active track cues) stored on `AgentState` in the checkpointer. History is retained; the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot.
_Avoid_: overwriting away prior snapshots, dual-writing into thread_store as SSOT, replacing full message history with snapshots

**iknow-memory**:
A separate TypeScript MCP server (own repo). It is the product 库: after ingest/push, **资料** are queried through fin's **retrieve port** against this backend (ADR 0045); a distinct **memory port** channel may hold explicit pins and must not share one config key.
_Avoid_: treating fin as the long-term vector backend or 库, lockstep releases with fin, collapsing retrieve MCP and memory MCP into one config key, mirroring fin consumer ADRs into that repo, using dream to ingest filings

**memory port**:
The only dependency fin is allowed for switchable session/buffer memory (extract, recall, GC, dream): port in, typed results out. Boot default is **OFF**; this checkout turns it **ON** in project env so local evals see auto-extract and **memory prefetch**; default pytest stays **OFF** even when that env is set (ADR 0035, ADR 0048).
_Avoid_: stuffing memory into the retrieve port, dual-writing memory bodies into the checkpointer as SSOT, treating memory recall as citation evidence, shipping always-on with no env switch, letting pytest inherit operator-env ON, promoting frequently recalled facts into the system prompt

**memory backend**:
The implementation behind the memory port. Local Postgres is the working store for **memory facts**. A memory MCP is not the product write path for auto-extract or dream (ADR 0048).
_Avoid_: requiring MCP for memory ON, storing cite-tree pages as memory entries, using Redis as memory SSOT, treating short-term buffer presence as a license to upload off-box

**memory fact**:
One atomic cross-session note from a single extract or explicit save: title plus body. The first non-empty body line is the **memory hook**. It is not a filing claim.
_Avoid_: one blob per thread, a second company corpus, a dedicated summary column in v1

**memory hook**:
The ≤80-character first body line of a **memory fact**; the only payload **memory prefetch** injects.
_Avoid_: injecting the full body by default, a whole-store title list in the system prompt

**memory prefetch**:
When auto-extract is on, a per-turn user-side overlay of at most three **memory hooks** whose title or body lexically hits the current user utterance; zero hits inject nothing. Already-shown ids in the same thread are skipped.
_Avoid_: scoring on the full thread history, whole-store catalog in system, vector search as v1, lifetime recall-count as a system-prompt reward

**memory existence pointer**:
The only auto-memory sentence in the system prompt: a note store exists; overlays are advisory, not instructions or cites.
_Avoid_: listing all titles in system, ordering the model to recall on every turn

**agent identity**:
What **fin** is in the system prefix: Name / Kind / Signature, code-locked. Dream never edits it.
_Avoid_: extracting identity from chat, calling fin a research agent, mixing 语料清单 into identity

**agent soul**:
How **fin** acts in the system prefix: truths, boundaries, vibe. Dream never edits it.
_Avoid_: consolidating soul from the fact store, mixing tool JSON schema into soul

**usage** (prompt segment):
Tool-choice and citation _behavior_ per **track** in the system prefix. Not tool JSON schema and not **citation verifier** numeric caps.
_Avoid_: restating page_limit or 32-character rules, dumping the catalog into usage

**user profile**:
Operator or tool-writable preferences (who they are, how they like to work): `$HOME/.fin/user.md`. Dream does not rewrite this file; stable likes go here, not into **memory facts**.
_Avoid_: treating the user profile as the episodic store, auto-extract into identity, per-工作区 overlay files, using the **工作台** UI as the overlay path

**dream** (fin):
A dual-gated LLM merge over **memory facts** only: 24h cooldown after last success or skip, then a store/session/turn threshold. It is not company-corpus ingest (ADR 0045), not a memory-MCP upload, and not a graph tool (ADR 0048).
_Avoid_: running dream inside model_node, per-turn SSE dream, auto-writing every turn to MCP, using dream to fill the retrieve backend, dreaming identity/soul/user profile, refreshing the 24h cooldown with an empty skip when fewer than two facts exist

### Seams

**资料**:
Operator-facing name for one corpus body held behind the **retrieve port** after ingest. It is not a 10-Q product name and not a **工作台** catalog page.
_Avoid_: filings as the operator word, freeze-slice cards as the product 库, treating **thread attach** as **资料** ingest

**知识 MCP**:
The retrieve MCP (**iknow-memory**) as the operator-visible dependency of the **工作台**. Boot publishes 缺 / 坏 / 可达 from `initialize` + `tools/list`; non-可达 copy is 知识 MCP 故障 (ADR 0051).
_Avoid_: Fake as healthy search, dashboard HTTP as this signal, PATH-only as 可达, 能聊不能搜

**retrieve port**:
The only retrieve dependency fin is allowed to have: `query` (+ optional filters or a typed structured request) in, authorized corpus results with source attribution out — MCP-shaped, transport-agnostic. Its adapter also carries host-internal evidence reads and invalidation acknowledgements over the same connection, without another model-facing search tool.
_Stage_: the structured request/result and internal evidence capabilities are specified in the [three structured-corpus contracts](../specs/structured-corpus-query.md); runtime implementation and acceptance remain pending.
_Avoid_: search API, vector store, conflating with the memory port

**retrieve adapter**:
The implementation behind the retrieve port. Default pytest uses a Fake port. When configured, product retrieve is an MCP 2.0 client (protocol-testable against a compliant public or fixture server); hits map to fin locators as `source_ref` and their returned text is serve-time citation evidence (ADR 0046). In-process BM25 is archived (ADR 0007; ADR 0030 superseded by 0032). Ranking stays outside fin; **切分** is fin's at ingest (ADR 0037) and the retrieval service stores and returns pushed chunk text byte-for-byte without re-splitting.
_Avoid_: shipping BM25 as the product path, hybrid/RRF implemented in this repo, re-splitting / truncating / rewriting pushed chunk text on the retrieval side, compile-time coupling to one memory repo, reusing the memory-backend URL as the retrieve adapter by implication, treating Fake as a product-healthy **知识 MCP**

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
On the **grounded** track, the model repeatedly picks `list_files`, `retrieve`, `read_source`, or `get_xbrl_fact` until it stops calling tools, or hits refuse (unknown locator or citation-repair cap) or step cap. Grounded `done` is only after the citation verifier (and numeric fact gate when a fact is claimed) passes; other tracks may `done` without tools or citations. Step order is not hardcoded.
_Avoid_: pipeline, chain, fixed graph of retrieve-then-generate, requiring the citation verifier on dialogue/clarify/off_topic

**list_files**:
Catalog tool over ingested/pushed **资料**, exposing display title/type and optional descriptive company/period; new public document IDs derive from trusted corpus scope and raw bytes, never filename or metadata. The catalog owns bounded metadata filters and typed validation; the adopted QA-chain retrieval spec fixes schema and caller compatibility under ADR 0031, while the existing financial read projection remains supported.
_Avoid_: list_filings, list_documents, glob, requiring ticker, treating company as forever equal to ticker, a generic CMS file browser

**get_xbrl_fact**:
Grounded-track **lookup** of one tagged financial fact from an ingest-built fact table (concept, period, issuer/document). XBRL is the reporting format, not a website. A numeric citation gate matches the claimed value to that row; the model does not invent the arithmetic. Embeddings may later resolve concept aliases; they do not author the number.
_Avoid_: treating XBRL as a site name, OCR or vision as the number SSOT, a `verify` tool, using retrieve/embeddings as the fact SSOT

**compare_facts**:
Grounded-track host tool that resolves one XBRL concept over a grid of rows (issuers or documents) x columns (periods) against the same fact table `get_xbrl_fact` reads, and returns the `fin.comparison` envelope. Deltas are computed in the tool, not by the model. Every column is an implicit `(concept, period)` claim for the numeric gate, so an answer quoting figures the table never reported cannot `done`. It contributes no citation evidence: a fact row is not page text.
_Avoid_: a prose table the model authors, a chart tool, treating a comparison cell as a citation, computing the delta in the SPA or the export

**comparison result** (`fin.comparison`):
The JSON envelope `compare_facts` returns and the workspace persists on the turn row (`TurnRow.comparisons`) and streams on the `comparison` SSE frame: `{schema, kind, axis, concept, unit, scale, columns[], rows[].cells[], deltas[], missing[], hit_count, cell_count, locators[]}`. Shape SSOT is `src/fin/host/tools/compare_facts.py`. `empty_comparison` / `comparison_overflow` are typed EXITs carrying a column-less skeleton, not exceptions and not renderable tables.
_Avoid_: a markdown table on the wire, re-parsing the answer prose for structure, per-surface variant shapes

**XBRL**:
The tagged-numbers format embedded in official 10-Q/10-K iXBRL filings; it is not a separate website.
_Avoid_: using XBRL as a product or hostname, conflating it with iknow-memory retrieve

**read_source**:
The model-facing name for a known-locator source read: it resolves one authorized source unit from the company corpus or active-thread attachment without requiring a `retrieve` hit in the same run. Each read returns at most 8,000 Unicode code points and a complete serialized response of at most 32 KiB, including on the backend wire; continuation uses a zero-based code-point `next_position`, and only text returned in this run may become quoted-text citation evidence (ADRs 0046 and 0088); visual claims use the separate **visual evidence** path (ADR 0089).
_Avoid_: read_file as the future tool name, read_locator, read_page, read_chunk, treating read as retrieve, reading embeddings

**ingest endpoint**:
The single public file-upload job that parses supported documents, applies **切分** from the format parameter table, and **入库推送** or invalidates via MCP; new document identity derives from trusted corpus scope and raw bytes. Version close/replace is an MCP call. Not a grounded-track tool.
_Avoid_: chunk-any-string tool, model-chosen split points, EDGAR form name as the root document type

**thread attach**:
An operator-submitted file under a logical `attachment_id` for the **research thread** lifetime; fin owns its durable original bytes, access scope, thread-private index, versioned **visual observations**, and caller-visible lifecycle. In the adopted target, the independent parser service owns parsing-job state and result artifacts, while fin consumes results and decides readiness, binds access to the active thread, tombstones before cleanup, and never pushes the attachment into the permanent company corpus (ADRs 0083, 0085, 0086, 0088, and 0089).
_Stage_: Current code also stores parsed units and performs parsing inside the host attachment workflow; the target ownership split is not yet implemented.
_Avoid_: treating drag-into-chat as corpus ingest, treating `uploaded` as `ready`, model-visible filesystem paths, injecting the attachment body into the prompt, page-only attachment browsing

**uploaded**:
The thread-attachment state meaning its raw bytes are durably stored and may be attached to a message; no parsing, vision analysis, or indexing is implied.
_Avoid_: prepared, ready, parsing

**ready** (thread attachment):
The thread-attachment state meaning parsed page text, required visual observations, and the thread-private index are usable for graph retrieval, including after restart. It does not mean a later run has already read visual evidence into its own context.
_Avoid_: uploaded, merely indexed, model-memory-ready

**attachment preparation job**:
The fin-owned attachment workflow that associates a **parser job** with an attachment version, consumes its durable result, builds the thread-private index, and changes readiness before resuming a waiting run.
_Stage_: The current implementation may perform parsing inside this workflow; the adopted target assigns parsing status, attempts, and result-artifact existence to the independent parser service.
_Avoid_: treating a **parser job** as attachment readiness, upload-time implicit success, per-turn duplicate parse, background corpus ingest

**parser job**:
One versioned asynchronous parse operation whose independent parser service owns status, worker attempts, cancellation, and durable result existence; fin owns the calling attachment or corpus association and decides whether the result is usable for readiness or publication.
_Stage_: Adopted target design on 2026-10-20; no parser-service implementation or fidelity result is implied.
_Avoid_: attachment-ready state, indexed content, citation verification, Runtime graph run

**parser service**:
The independent context that owns **parser job** state, the **parser attempt**s that work on those jobs, cancellation, and the durable **parser result envelope**, in a PostgreSQL schema of its own. fin keeps ownership of attachment and corpus visibility, readiness, indexing, and publication, and reaches the service over its own API.
_Stage_: Adopted target design on 2026-10-20; the current implementation is the parser package under `src/fin/parser/`, which owns the `fin_parser` schema and imports no other fin context.
_Avoid_: the corpus ingest endpoint, the host attachment workflow, a second corpus store, Redis as job truth

**parser attempt**:
One fenced claim by a parser worker to work on a **parser job**, identified separately from that job and guarded by the job's current **fencing token** and lease. Only the attempt holding the job's effective token may commit; a superseded attempt's write is refused rather than applied.
_Stage_: Adopted target design on 2026-10-20; the current implementation fences attempts per job with a monotonically increasing token.
_Avoid_: **worker attempt** for a graph run, a retry counter, an unfenced process-local owner

**fencing token**:
The per-**parser job** counter that names the one **parser attempt** allowed to act on that job right now. Every claim produces a higher token and a token is never reused, so a late write from a superseded attempt is refused instead of applied.
_Avoid_: lease expiry time, attempt number, an opaque id that cannot be compared, a UUID

**parser result envelope**:
The durable, versioned parse output a **parser attempt** commits for a **parser job**: the parsed document under its schema version plus a reference to the derived output, never the output bytes. The **parser service** keeps ownership of the artifact; fin validates and adopts it before any readiness or publication decision.
_Stage_: Adopted target design on 2026-10-20; the current implementation stores the envelope and the result reference in the `fin_parser` schema.
_Avoid_: the retrieve envelope, attachment readiness, storing the parsed bytes inline as the result

**visual observation**:
A persisted, versioned description from fin's configured vision model, bound to an original attachment page or crop and carrying structured coverage of inspected scope and visual detail. It is derived search material tied to the attachment version and original image location, never source text or a verbatim quote (ADR 0089).
_Avoid_: OCR text, source quote, unproven model recollection

**observation coverage**:
A versioned structured record that binds each covered visual detail to its inspected whole-page or crop scope and complete target; a numeric value's target includes its chart, series, and category identity, or an equivalent complete identity. After the grounded model names a required complete target and region and the current run reads the observation, the host reuses it only on a deterministic coverage match; an unknown or incomplete target or region, or missing coverage, requires inspection of the stored original.
_Avoid_: whole-page scope as proof every chart is covered, treating only a chart, series, or category as a complete numeric target, treating one value as coverage of an entire chart, inferred coverage, model-declared sufficiency, conversation-memory coverage

**visual evidence**:
A current-run reference proving that the run explicitly retrieved or read a matching persisted **visual observation**, with provenance to the original attachment version, source unit, physical page, and crop when available. The host checks **observation coverage** against the requested target and region after that read; sufficient coverage can be reused without uploading the attachment again or repeating vision, while unknown or missing coverage triggers inspection of the stored original and a new observation version (ADR 0089).
_Avoid_: model conversation memory, generated source quote, re-upload as citation, bare vision response without persisted provenance

**corpus ingest**:
The operator action that sends a supported file through the single **ingest endpoint** into the retrieve backend; `title`, `company`, `period`, and `doc_type` are optional descriptive metadata and do not define identity. A **thread attach** remains scoped to the active conversation and is not permanent corpus ingestion.
_Avoid_: a financial upload mode, a generic upload mode, requiring company/period, treating thread attachment as permanent corpus ingestion

**locator**:
A typed source identity for citation and `read_source`: company sources use `{document_id, version, unit}` and thread attachments use `{kind: "attachment", attachment_id, version, unit}`. The host validates the attachment against the trusted active thread and parsed-content version; a read continuation position is separate from the locator. Retrieved chunks carry a locator as `source_ref`, while the claimed quote remains separate (ADRs 0046 and 0088). A **database row is not a locator** — it has a third identity shape, not a third locator spelling; see **database-row** (ADR 0091).
_Avoid_: chunk_id, char offset as citation identity, bbox, page as the generic unit field, unofficial PDF page as live EDGAR identity, verifying an HTML quote against a PDF page

**database-row**:
A quotable evidence kind for one row returned by the `sql` 档, carrying `{kind: "database-row", source, record_key, queried_at, fields, summary}` — its identity is the `record_key` plus the permitted view named in `source`, and its currency is the `queried_at` of the query that produced it. It travels as a **sibling key on the retrieve envelope, never in `chunks[]`**, and its `source_ref` is never a locator. It verifies against the `record_key` and the `fields` **this round** returned rather than by a text span, so a scalar fact such as a notice period is citable and the span rule of ADR 0046 is scoped to document kinds only (ADR 0091). Re-reading a citation shows the retained snapshot and issues no query; a refresh is a new query that never rewrites a prior answer, and a past snapshot is still withheld when current permissions no longer cover that record.
_Avoid_: locator, `field=value` text line, substring-matched row, a row shaped as a chunk, a re-query dressed as the original evidence, implying the data is unchanged because no refresh was requested

**source version**:
An immutable iknow record binding one logical company source's original content and frozen-unit manifest to its source locations. Current publication selects a source version and accepted extraction revision; historical content has finite retention under ADR 0095.
_Avoid_: content hash as logical source identity, mutable source snapshot, permanent archive

**assertion support**:
An attributable occurrence linking an entity or relation assertion to one immutable source version, location, and optional span. Current support belongs to the published accepted extraction set; retirement preserves its historical attribution until retention permits collection.
_Avoid_: occurrence count as truth, single-origin assertion, latest source wins

**structured result evidence**:
A finite retained snapshot of a structured query row or exact stored-subject count receipt, carried as the separate `structured-result` citation kind. Historical reads preserve the received value while rechecking current authorization and fixed expiry under the structured-corpus evidence contract.
_Avoid_: database-row, document quote, historical membership manifest, live re-query as old evidence

**citation verifier**:
A code gate on the **grounded** track path to END: a supporting text span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run received from **retrieve** / **read_source** (ADR 0046); visual claims use a separate typed path requiring current-run **visual evidence** tied to the original attachment page or crop, and a generated observation is never a source quote. Unknown locators refuse immediately; missing or unmatched evidence bounces with a hidden template until the citation-repair cap, then refuses; other tracks skip this gate (ADRs 0082 and 0089). The span rule above governs **document** evidence; a **database-row** verifies by its record key and this round's returned fields instead, which is deliberate and not an exception to clean up (ADR 0091).
_Avoid_: faithfulness prompt, LLM-as-judge as the citation gate, model-authored citation block, `verify` as a tool, running the gate on greetings or soft off-topic replies, web-search text as gate evidence, treating visual observations as quoted source text, a second serve-time extracted tree as citation SSOT

**citation span**:
One user-visible citation row from the verifier: a ≥32-character contiguous substring of the answer that matched on a retrieved or read source unit. The workspace sources list may show many spans per unit (sliding-window overlap); that granularity is expected, not a send failure.
_Avoid_: treating each span as a full sentence, hiding overlap as a bug, conflating span count with unit count

**citation-repair cap**:
v1 limit of 3 recoverable citation-verifier failures (span not in returned source text, or a non-refuse final answer with no cites) before the run must refuse.
_Avoid_: retry budget, max repairs, using step cap as the cite-fail rule

**step cap**:
v1 outer fuse of 32 agent-loop steps (counted in fin state, not LangGraph `recursion_limit`). Exhaustion stops the run as `step_cap`, not as a citation-verifier failure.
_Avoid_: timeout, token budget, treating this number as the cite-repair limit or as `recursion_limit`

### Host graph

**host graph**:
LangGraph compiled around the agent loop plus code gates (verifier, caps, interrupt); it executes one logical **run** and owns graph state through the **checkpointer**. It does not own durable input acceptance, run dispatch, or parser-job lifecycle.
_Avoid_: retrieval graph, durable operation queue, second business-state authority

**Fin Runtime**:
The application-owned authority that accepts durable client operations, orders work per **research thread**, manages run reservations and fenced **worker attempts**, and exposes thread snapshots and subscriptions. It dispatches the host graph but does not own graph checkpoints or **parser job** state.
_Stage_: Adopted target design on 2026-10-20; the current application still has process-local run controls and its single-process guard.
_Avoid_: LangGraph checkpointer, Redis as business-state authority, parser scheduler, second workflow engine

**accepted operation**:
One durable record of a stable client intent, committed with its input and resulting queue/run identity before the Runtime acknowledges acceptance; replaying the same operation identity and payload returns its recorded acceptance, while a changed payload conflicts.
_Stage_: Adopted target design on 2026-10-20; the current API does not yet provide this durable idempotency contract.
_Avoid_: a single HTTP attempt, graph execution attempt, corpus-ingest **幂等键**

**worker attempt**:
One fenced claim by a graph worker to execute or resume a logical **run**, identified separately from that run and guarded by the current lease, attempt generation, and thread generation.
_Stage_: Adopted target design on 2026-10-20; current process-local ownership is not evidence of cross-process fencing.
_Avoid_: new user turn, run identity, an unfenced process-local owner

**thread event cursor**:
An opaque resume position backed by a monotonically increasing PostgreSQL sequence over client-visible events for one thread, including events across runs and thread lifecycle changes; its snapshot reads the projection, revision, and high-water mark from one consistent database snapshot, and per-run sequence is auxiliary.
_Stage_: Adopted target design on 2026-10-20; current event delivery is not claimed to provide this durable thread-wide sequence.
_Avoid_: Redis stream ID as durable cursor, per-run sequence as thread-wide order

**session compaction**:
A host gate on the **research thread**: occupancy is estimated tokens of the model-facing payload (track prompt, messages, grounded-track tool schemas, and when memory is ON any **memory prefetch** or **recall injection**) against a 256_000 session budget; at ≥90% occupancy, or when the operator sends `/compact`, old checkpoint `messages` are summarized and removed, aiming at ~40% occupancy. The summarizer is injectable (v1 the **v1 model**); occupancy may render on the thread, not on `GET /api/status`.
_Avoid_: message-count windows, treating `/compact` as a grounded-track tool, putting occupancy on the boot status strip, using the vendor 1M window as the compact threshold, treating a memory row-count cap as the occupancy gate

**recall injection**:
When memory is ON, advisory recall or **memory prefetch** text placed in the model-facing payload. It counts toward the same occupancy ledger as **session compaction**; a row-count limit is only a fuse, not the budget.
_Avoid_: last-N memory window as the token budget, using recall as retrieve/`read_source` evidence

**checkpointer**:
Current v1 runs use LangGraph `PostgresSaver` via `FIN_CHECKPOINT_URL`; product HTTP/CLI raise if that URL is unset, while pytest uses `InMemorySaver` only (plus explicit `FIN_SCRIPTED_MODEL` e2e). The adopted target keeps graph checkpoints in the same PostgreSQL database as Runtime state but in a separate schema, with every checkpoint mutation passing through a generation-aware adapter in the same database transaction as ownership validation.
_Avoid_: treating checkpoints as accepted-operation or run-state authority, an unfenced saver fallback, SqliteSaver as the product default, InMemorySaver as a silent CLI/HTTP fallback, LangSmith-hosted persistence

**thread_id**:
A conversation session: follow-up questions reuse this id so checkpoints and messages accumulate. Omitting it creates a new session before invoke; it is not a user_id.
_Avoid_: one question per thread, treating a refuse as killing the session

**run**:
One logical graph turn (`run_id`) accepted for a thread: `done` / `refuse` / `step_cap` end it; a follow-up is a new run on the same `thread_id` that resets run-scoped evidence and caps. Under the adopted target contract, a run may be queued or waiting without occupying a worker, and takeover resumes the same run only when its checkpoint and effects are safe; each worker claim is a separate fenced **worker attempt**.
_Stage_: The current implementation invokes directly and retains process-local ownership; durable queueing and takeover are target behavior.
_Avoid_: collapsing run into thread_id, treating a worker attempt as a new turn, concurrent effective graph owners on one thread, deleting the user turn to “sanitize” a crash

**turn receipt**:
One logical operational summary of an accepted answer turn, correlated across pause and continuation and distinct from task state, task completion, and per-tool-attempt measurement records.
_Stage_: Adopted QA-chain contract (2026-10-16); receipt fields remain distinct from the target **tool outcome ledger**.
_Avoid_: task outcome, one row per HTTP request, **operational trace**, complete tool-attempt denominator, second task store

**research thread**:
The product conversation: one `thread_id` the operator talks in; each question is a run that may be any **track**; follow-ups keep full messages, citations when present, and status-snapshot history. This is how fin is used after v1; it is not a one-shot `/ask` demo.
_Avoid_: Q&A session, demo ask, one-shot invoke as the product UX, forbidding greetings or clarification on the thread

**工作台**:
The local web UI: **research thread**s live in a 会话 list (create, list, open, resume, delete); **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn; **知识 MCP** health is one left-corner line.
_Avoid_: 知识库 or 监控 as nav labels, `/kb` or `/monitor` as product routes, 工作区 directory, CLI REPL as the product, hosted accounts this increment, POST /ask as the conversation surface, Corpus / Status as nav labels, 对话 as a chrome overview, Fake 「演示搜索」 as healthy chrome

**thinking fold**:
A per-burst collapsible thinking region on a **research thread** turn: streams expanded while that burst is live; auto-collapses when the burst ends; collapsed is header-only (UI label 深度思考, plus duration when measured) with the burst text hidden until expand.
_Avoid_: mixing thinking prose into the answer card, clamping the first lines as the live view, concatenating a whole run's thinking above all tools, a three-line tail preview as the collapsed state

**thinking pending**:
The same **thinking fold** chrome on an optimistic **research thread** turn after send, before the first `thinking` SSE frame, with no burst body. The first `thinking` frame becomes that fold; a first `tool_call` or `answer` removes it.
_Avoid_: a second thinking card, a new SSE event type, treating HTTP wait as model tokens, 处理中 as the default label

**tool fold**:
A per-burst collapsible tool-call list on the same turn, sibling to the **thinking fold**. Live while those calls run so the operator can debug (including retrieve via the **retrieve port**); auto-collapses when that burst ends. UI label: 工具. MCP is not a transcript tool name.
_Avoid_: default-hidden tools with no fold, dumping ToolMessage JSON, nesting tools inside thinking prose, labeling the row MCP

**logical tool call**:
One model-requested tool action and its bounded argument-correction loop, tracked separately from each model-emitted attempt and each execution attempt.
_Stage_: Adopted tool-evaluation contract on 2026-10-20; no complete per-call ledger is implied by current traces or receipts.
_Avoid_: one provider tool-call ID, one network request, conflating correction with execution retry

**schema correction**:
The single bounded model feedback opportunity for a tool call rejected by its canonical Pydantic argument schema before side effects; execution failures and uncertain effects follow separate policies.
_Stage_: Adopted target contract on 2026-10-20; implementation and measured correction quality remain pending.
_Avoid_: unbounded prompt loop, retrying a side effect whose outcome is unknown, exposing submitted values in correction feedback

**tool outcome ledger**:
The canonical durable record of tool-call validation and execution outcomes from which tool-quality rates and denominators are computed; traces and UI rows may link to it but are not substitutes for it.
_Stage_: Adopted measurement design on 2026-10-20; the durable ledger and metrics have not been implemented or validated.
_Avoid_: **turn receipt** as every-attempt history, **operational trace** as a metric source, raw call volume as quality

**operational trace**:
The optional, best-effort five-span OpenTelemetry view adopted in ADR 0102 for diagnosing selected execution seams; it may be sampled or incomplete and does not count all tool attempts or establish evaluation outcomes.
_Stage_: ADR 0102 adopts the instrumentation design; this definition does not claim all five spans are currently emitted.
_Avoid_: **tool outcome ledger**, complete attempt history, evaluation denominator, billing authority

**工作区**:
The one global default operator directory `$HOME/.fin/default` (created if missing). Every **research thread**, profile read, and **memory fact** workspace key uses this path.
_Avoid_: Path.cwd() as implicit 工作区, `$HOME/.fin/workspaces/<slug>`, left-rail 指定/新建, a second 知识库, calling the **工作台** a 工作区

**知识库**:
The one process-wide corpus of **资料** behind the **retrieve port**, shared by every **research thread**. It is not a **工作台** route.
_Avoid_: sidebar 「知识库」, `/kb` as product chrome, per-工作区 retrieve index, identity text listing filings, fin as a second 库

**资料集合**:
The operator-visible set of **资料** already pushed to **iknow-memory** (ready when successful **chunk** count ≥ 1). The **工作台** binds a knowledge question to this set; local `corpus_filing_count` is not 就绪.
_Avoid_: freeze-slice catalog as 就绪, treating MCP 可达 as nonempty retrieve, **thread attach** as membership in the set

**检索档** (`search mode`):
The host's internal retrieval implementation for a **grounded** question, using the closed set `lexical | graph | global | sql` behind the **retrieve port**; explicit rules/debug pins, eligible same-job inheritance, and **Jev** decide it, with an audited `graph` / `fallback` residual when still unassigned (ADR 0104; implemented on `fix/qa-design-correction-20261020`; the real-model browser acceptance is Not run because the external model provider is unavailable — see the [implementation report](verification/2026-10-20-qa-design-correction-implementation.md)). Current-turn receipts retain provenance, confidence, and original judge EXITs; people clarify answer-changing content such as documents or periods, while rendered UI omits internal mode names and raw tokens remain in machine diagnostics.
_Avoid_: user-facing mode pickers, treating mode uncertainty as missing user content, silent global fallback, presenting fallback as a confident Jev prediction, four host retrieve nodes, Fake as a mode, retired `A`/`B`/`C`/`D` wire tokens, `local` as the graph token

**社区**:
Entity-graph clusters from **hierarchical Leiden** after extract and merge (ADR 0056 / 0065), on the **largest connected component** by default (ADR 0074), split until **max_cluster_size** (default 10, tunable — ADR 0072), with a **fixed default seed** (ADR 0075). Graph changes **rebuild** Leiden and summaries; C is not-ready until that succeeds (ADR 0064 / 0078); B does not wait on communities.
_Avoid_: 文档父子, 查询期现写综述当默认, 社区报告当唯一答案且无出处, 空摘要当 C, 部分社区成功也开 C, 扁平 Louvain 当产品聚类, Fast 抽图当社区前置步骤

**Fast 抽图**:
The NLP / co-occurrence **extract method** (index job): mixed **Chinese/English noun-phrase regex** (ADR 0076), co-occurrence on **in-window children** then **prune** and Leiden, community reports from **抽图窗** text (ADR 0067 / 0073 / 0077). An **operator switch** selects it (default **standard**, **one method per corpus version** — ADR 0079); the model may propose Fast in a **choice window** and must not silent-apply (ADR 0068).
_Avoid_: Fast as search mode, Fast as a stage after Leiden, Fast as the only C path, silent auto Fast, prune as canonical merge, regex_english as product Fast default

**实体描述**:
One index-time summary of an entity’s merged description list on the **standard** extract job, written after **canonical merge** (duplicate gates first — ADR 0052 / 0069) and before Leiden. It is the text ADR 0058 embeds. Fast has none; query and memory dream do not write it.
_Avoid_: biography as a query tool, every-turn filing extract, Fast 路上的描述, 查询期补写

**claim**:
A time-bounded factual assertion extracted from a **抽图窗** on the **standard** extract job (ADR 0071), after merge and before Leiden. Default on for standard; Fast skips. Not an XBRL fact row and not D.
_Avoid_: 指标 as this term, claim as citation-gate span, query-time claim extract, Fast claims

**具名失败**:
A typed product failure that names empty-bound 资料, mapping-drop, citation-verifier refuse, 档未开, 真库未接, or community-summary index failure as distinct exits — not one `unknown_locator` plus empty citations.
_Avoid_: collapsing those cases, Fake health, `degraded` as a synonym for empty hits

**workspace**:
Retired alias: the UI is **工作台**; the operator directory is **工作区**. Kept so older specs remain readable.
_Avoid_: using workspace as the product kind, as the 知识库, or as Path.cwd()

**监控**:
Retired **工作台** chrome (`/monitor` watchlist + retrieve-reachability). Product MCP state is **知识 MCP**, not this page.
_Avoid_: `/monitor` as a product route, MCP as a nav label, watchlist as fin chrome, treating 监控 as a 工作区 picker

**Corpus 首页**:
Retired chrome name. `/kb` is not a product route; catalog and upload live outside fin (iknow 看板 / 中台). `/` is the homepage, with last-thread restoration limited to one non-demo cold application start there; deliberate later Home/Back visits stay home (design adopted 2026-10-20; implemented on `fix/qa-design-correction-20261020`; the real-model browser acceptance is Not run because the external model provider is unavailable — see the [implementation report](verification/2026-10-20-qa-design-correction-implementation.md)).
_Avoid_: `/` as corpus cards, `/` as a thread-card overview, Corpus as a nav label, 对话 as a chrome entry, auto-send from a corpus card, treating fetch of `GET /api/corpus` as ingest, deep-link `/kb` as a dashboard handoff

**v1 surface**:
The current first-ship CLI and HTTP surface compile the same host graph and HTTP calls sync `invoke` with the sync `PostgresSaver`; that compatibility remains while the target moves graph execution into supervised worker processes. Process separation does not itself require a sync-to-async graph or saver rewrite. Plan-approval and tool-approval HITL remain outside v1 (ADR 0062); the Fast extraction proposal keeps its confirmed choice window and the document-choice question keeps its resumable interrupt, while genuine content clarification is a terminal `task_clarification` question answered by a new-message turn (no graph interrupt, no paused run) and unresolved internal mode selection falls back with a receipt (ADR 0104; implemented on `fix/qa-design-correction-20261020`; the real-model browser acceptance is Not run because the external model provider is unavailable — see the [implementation report](verification/2026-10-20-qa-design-correction-implementation.md)).
_Avoid_: HTTP-only first ship, separate graphs per transport, an implicit async-persistence rewrite, treating plan-approval as the clarify surface

**HITL**:
LangGraph `interrupt` provides durable in-flight resume for the confirmed Fast extraction proposal and for the document-choice question, the one resumable clarification; content clarification is a terminal `task_clarification` question answered by a new turn, with no interrupt and no paused run. Internal mode-selection uncertainty uses the audited fallback instead of a choice window (ADR 0104; implemented on `fix/qa-design-correction-20261020`; the real-model browser acceptance is Not run because the external model provider is unavailable — see the [implementation report](verification/2026-10-20-qa-design-correction-implementation.md)); plan/tool approval UI remains outside v1.
_Avoid_: internal retrieval modes as user choices, Studio as the required interface, a judge outage as missing user content, free-text ambiguity without understandable options as the only confirmation path

**Jev**:
TypeSafe System One for host closed-set **Choice**: Soft-Track's four **track** leaves and, after **grounded**, internal `lexical | graph | global | sql` selection. Default pytest uses Fake Choice, changed configured paths require real evidence, missing optional Soft-Track hints use the adopted ADR 0080 retrieval fallback, and an unresolved search-mode choice retains its EXIT/confidence signals while the host applies graph fallback (ADR 0104; implemented on `fix/qa-design-correction-20261020`; the real-model browser acceptance is Not run because the external model provider is unavailable — see the [implementation report](verification/2026-10-20-qa-design-correction-implementation.md)).
_Avoid_: answering model, retrieve reranker, MiniMax JSON classify fallback, Fake as the product classifier, counting a Fake pass as live, four retrieve nodes

**v1 model**:
The agent-loop LLM is `ChatOpenAI`; its model id is required from `FIN_MODEL` or `llm.model` in operator settings, with no compiled-in model fallback. `FIN_BASE_URL` defaults to the official MiniMax CN Chat Completions endpoint `https://api.minimaxi.com/v1` (the URL includes `/v1`). Keys stay in environment variables (`FIN_API_KEY`); slash ids do not go through `init_chat_model`.
_Avoid_: `init_chat_model` for slash ids, LLM-as-judge as the citation gate, using this client as the product Soft-Track gray-zone classifier

**FIN_API_KEY**:
项目自有的 LLM API key 环境变量名（2026-08-27 由 `ANTHROPIC_AUTH_TOKEN` 硬切而来，无 fallback）
_Avoid_: vendor-prefixed legacy names (`ANTHROPIC_AUTH_TOKEN`), a fallback to the legacy name, hard-coding the key, shipping a baked-in default key, treating an unset key as a working credential

**model client**:
LangChain `ChatModel` (`ChatOpenAI` in v1) plus the messages and tool-calling protocol (`bind_tools` / tool schema). LangGraph orchestrates the run; it does not replace this layer.
_Avoid_: raw vendor HTTP as the v1 client, treating LangGraph as the chat SDK, `init_chat_model` as the required constructor

**loop skeleton**:
v1's host graph is authored in fin: model node, tool node, citation verifier, caps. It does not copy a research or RAG product repository into `src/fin`.
_Avoid_: copying an external research-agent product or code tree into `src/fin`, requiring a particular graph-construction starting point

### Corpus and eval

**product corpus**:
The operator-selected supported documents after ingest: fin parses source text into **冻结单元**, then deterministically **切分**s and pushes its chunks to the retrieve backend. Serve-time `retrieve` / `read_source` / the citation verifier use this-run tool payloads (ADR 0046), not a second on-disk tree as permanent-corpus SSOT.
_Avoid_: the historical private fixture set as product SSOT, unofficial PDF as live identity, broad index backfill as the ingest default, a second PDF-page product path

**eval fixture**:
A maintainer-held snapshot of **ingest** output, with the same locator contract as **product corpus**. Fixture-dependent tests receive it separately from the public checkout.
_Avoid_: pdfplumber-only test ingest, live source downloads in default pytest, treating a historical private fixture set as the current eval SSOT

**v1 corpus**:
Historical private 12-PDF fixture freeze (ADR 0004, superseded by 0029), covering 2022 Q3 and 2023 Q1–Q3. It is not distributed with the public checkout and is not the product or current eval SSOT.
_Avoid_: current catalog, gold SSOT, demo FAQ

**ingest**:
The CLI/job and public **ingest endpoint** that parse supported corpus documents, apply **切分**, and **入库推送** to the retrieve backend through one file-based upload path. Public document identity derives from trusted corpus scope and raw bytes; filename and optional metadata do not define it, and the ask path does not ingest files.
_Avoid_: extract-on-query, host-graph ingest node, putting filing bytes in the checkpointer database, 切分 as a research-track tool

**资料主体**:
The issuer already stored on a **资料** (company name and optional ticker/CIK): the document an extract window came from. Extract prompts and deictic rewrite use this, not a reread of the whole body.
_Avoid_: 全文回读当主体, ticker as a required equal of company, a global 本公司 entity

**指示语**:
A company mention in window text that is not a proper name (本公司, the Company). It binds to that window’s **资料主体** (ADR 0052), not to same-string merge across 资料.
_Avoid_: treating 指示语 as a canonical name, alias as the only word, LLM-as-judge on the full 资料 as the default

**object store**:
MinIO/S3-compatible storage for raw filing (and later multi-format) bytes. Postgres holds metadata and pointers. Git is not the byte warehouse once configured. Raw object keys are not citation locators; citable text after ingest lives on the retrieve backend (ADR 0046 / 0047).
_Avoid_: Postgres bytea as the primary raw store, putting raw PDFs in the checkpointer, treating object keys as citation locators

**contract eval**:
Scripted-model harness against the separately supplied **eval fixture**. Required classes: HTML-unit citation pass, unknown locator refuse, catalog miss, and a cross-period case. Nine v1 gold cases are not the ceiling.
_Avoid_: live LLM in default pytest, FinanceBench in default pytest

**Fin acceptance case set**:
The evolving, versioned set of at least 50 independent Fin business cases with frozen rubrics, reported separately from mechanism tests, browser journeys, and the real authorized **评测数据集**.
_Stage_: The target acceptance requirement is adopted; the current case count and campaign evidence have not been established by this design decision.
_Avoid_: counting duplicate paraphrases as independent, counting synthetic cases as real corpus, treating a green mechanism suite as business acceptance

**capability eval**:
Opt-in or nightly harness on **product corpus** plus a live model; records coverage, abstain, and citation quality. Not a per-commit gate.
_Avoid_: web-search text as citations, making this the only eval

**v1 eval slice**:
Historical nine hand-authored cases against the private 12-document fixture set (ADR 0012, superseded by 0029). Running them requires maintainer-supplied fixtures.
_Avoid_: 40-question matrix as a v1 commitment, treating nine cases as the current ceiling

**冻结单元**:
A versioned, locator-addressed unit of parsed source text that fin determines before retrieval chunking; "frozen" means its text is fixed for that parsed version, not that fin creates a cache or separate persistence tier. Search chunks are contiguous substrings of the unit, while quoted-text citation evidence comes only from text returned by `retrieve` or `read_source` in the current run; visual evidence remains a separate page/region path (ADRs 0046 and 0089).
_Avoid_: chunk, raw file, arbitrary paragraph, session cache, second citation SSOT, query-time result

**切分**:
fin 把冻结单元文本切成块的过程与规则，是 fin 的职责；切分必须确定性可复现，同一（字节输入 + 规则版本 + 参数表版本）两次切分产物逐字节相同。检索服务对推送文本不做二次切分、截断、重组或归一化改写。
_Avoid_: chunking, 检索侧二次切分, 按字符硬切, 模型判断切点

**抽图窗**:
Title-scoped extract-and-cite unit: current heading plus a body span (≤1200 tokens one window; overflow splits body only, ~8% overlap between those windows). It is the parent of in-window **父子** keys (ADR 0060 / 0063), derived at extract from frozen text (ADR 0054), not a second document cut.
_Avoid_: 两种切法, 整篇定长滑, 用子块抽图, 大节=整份财报, 抽图再OCR, 视觉模型当标题切点, 把窗 8% 抄到 ES

**父子分块**:
Search keys inside one **抽图窗**: children are the lexical/ES hit set and the **Fast** co-occurrence scope (ADR 0077), not standard LLM extract input; each is a contiguous substring of its parent window, returned for extract, citation, and A after a hit (ADR 0060). Default child overlap is 0; narrative may overlap at most one sentence; not the window 8% (ADR 0063).
_Avoid_: 对全文另切一套父块, 子块抽图, 父块参与词法命中, 双层切分当两套原文, ES 强制百分比重叠

**场景适配颗粒度**:
Deterministic (document type × section × density) lookup that sizes in-window child packing, child overlap (0 or one sentence), and overflow packing under the 1200-token cap, not a second extract splitter (ADR 0060 / 0063). Same triad, same row; table version in metadata, bump rebuilds in place.
_Avoid_: 用密度缩小抽图窗, 自适应切分, 用模型判断颗粒度, 双版本参数并存

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
