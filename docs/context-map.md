# fin — Context Map

> Vocabulary authority is [`CONTEXT.md`](CONTEXT.md). This map names the bounded
> contexts, their ownership edges, and the legal dependency direction. It defines
> no term: every bold term below is consumed from `CONTEXT.md` verbatim.
>
> The physical paths, import counts, and known debt below describe the current
> repository snapshot; they are not claims that the adopted target architecture
> has been implemented. The target process and authority boundaries are mapped
> separately at the end of this document.

## Language

**host-agent context**:
fin's LangGraph host graph, which routes tracks and calls grounded-track tools behind the **retrieve port**; it is not the application-level **Fin Runtime**, ranking service, or vector store. Physically `src/fin/host/`.

**attachment capability**:
Current implementation of the **thread attach** boundary inside the host-agent context: fin owns each thread's durable raw bytes, parsed units, versioned **visual observations**, thread-private searchable index, and lifecycle state (ADR 0088). Physically a capability package: `src/fin/host/attachment/`; the adopted target parser ownership is separate below.

**corpus context**:
The ingest-side boundary: fin parses source text into **冻结单元**, deterministically **切分**s it, and performs **入库推送** through the push and invalidation ports (ADR 0037). Physically `src/fin/corpus/`.

**retrieve context**:
The seam boundary: the **retrieve port** fin is allowed to depend on, plus its **retrieve adapter** implementations. Ranking stays outside fin. Physically `src/fin/retrieve/`.

**parser context**:
The independent parse-service boundary: it owns **parser job** state, the **parser attempt**s that work on those jobs, and the durable **parser result envelope**, in a PostgreSQL schema of its own. fin keeps ownership of attachment and corpus visibility, readiness, indexing, and publication. Physically `src/fin/parser/`.

**driver layer**:
**contract eval** and **capability eval** harnesses that compose the corpus, host-agent and retrieve contexts — not the parser context — to emit quantified reports. Not a business context; it owns no product data. Physically `src/fin/eval/`.

**host capability package**:
A business capability of the host-agent context that owns its own modules and, where it owns a graph node, the node's behaviour. Physically `src/fin/host/{application,attachment,evidence,grounded,question,session,task}/`. The rule below governs the six capability packages `attachment`, `evidence`, `grounded`, `question`, `session` and `task`: each of them may not import the transport, the application boundary or the graph composition root, and its capability-to-capability edges are recorded in `scripts/check_capability_boundaries.py`. The application boundary, `src/fin/host/application/`, is the composition owner rather than one of the six: it owns application composition and lifetime, and it is exempt from the "must not import the graph composition root" rule, because it reaches that root on purpose (`src/fin/host/application/composition.py` imports `compile_graph` from `fin.host.graph`, plan T3). The other six capability packages are not exempt. The web client has its own capability packages under `web/src/capabilities/`.

## Current physical contexts

The following table is the as-is code map. The target ownership table below
does not invent source directories or change any measured import count.

| Context | Physical surface | Purpose | Owns | depends_on (legal direction) | Integration pattern |
|---|---|---|---|---|---|
| host-agent | `src/fin/host/` (`graph.py`, `compile.py`, `state.py`, `tools/`, `thread_store.py`, `workspace_http.py`) plus the capability packages `application/`, `attachment/`, `evidence/`, `grounded/`, `question/`, `session/`, `task/`, `web/` | run a **research thread** to a cited answer | tracks, **agent loop**, host tools, **citation verifier**, **step cap** / **citation-repair cap**, **session compaction**, run + thread durability, **工作台** surface | corpus published value types; retrieve ports for every call, plus the two retrieve adapter classes it selects at the composition root | composition root — it selects and injects adapters |
| attachment capability | `src/fin/host/attachment/` (`attachment_store.py`, `attachment_prepare.py`, `attachment_index.py`, `attachment_budgets.py`, `attachment_visual_read.py`, `attachment_vision.py`, `nodes.py`) | make one thread's own file answerable, then disposable | **uploaded** → **ready** lifecycle, **attachment preparation job**, parse/OCR/vision derivatives, thread-private index, retry, tombstone deletion | corpus `object_store` port, host `thread_store` + graph | thread-bound interface owned by host; the capability is a package, and its data stays host-resolved |
| corpus | `src/fin/corpus/` (`extracted.py`, `chunking.py`, `push.py`, `invalidation.py`, `object_store.py`, `catalog.py`, `facts.py`, `ingest_endpoint.py`, `document_identity.py`, `parsers/`) | turn a file into reproducible pushed chunks | **冻结单元**, **切分**, **结构化元数据**, **幂等键**, **入库推送** payloads, **内容失效**, **object store** byte custody, catalog, XBRL fact table, document identity (canonical id and its aliases) | retrieve push + invalidation ports | port consumer; iknow-facing transport stays in retrieve |
| retrieve | `src/fin/retrieve/` (`port.py`, `read_port.py`, `push_port.py`, `invalidation_port/`, `mcp_adapter.py`, `mcp_session.py`, `capabilities/`) | keep iknow-memory swappable behind one seam | **retrieve port** contract, **retrieve adapter** (MCP client), **降级标记** reading, hit→locator mapping, input validation at the seam | corpus published types (`Locator`, push payload, `InvalidationTarget`), plus the corpus-owned document identity store (`fin.corpus.document_identity`), consumed at the read point to resolve an alias to its canonical id so both identifier forms of one document read the same text | port + adapter; out-of-process MCP 2.0 stdio client |
| parser | `src/fin/parser/` (`api.py`, `app.py`, `job_store.py`, `worker.py`, `worker_main.py`, `envelope.py`, `results.py`, `notifications.py`, `staging.py`, `redis_client.py`, `schema.py`, `ports.py`, `errors.py`) | own the durable parse of a **parser job** | the `fin_parser` PostgreSQL schema, **parser job** state, **parser attempt**s with their **fencing token**s, cancellation, the durable **parser result envelope** | none — the package imports only `fin.parser.*` | independent service: its own schema, its own HTTP API, its own worker process; fin reaches it over that API |

Outside this repository, **iknow-memory** is a context of its own: the product 库, the ranking and vector/index authority, and the company-source boundary. It is reached only through the **retrieve port** and **知识 MCP**; there is no import edge from fin to it (ADR 0001: "do not compile-time couple the two repos").

## Explicit non-ownership

- host-agent does **not** own ranking, embeddings, or the vector store; it does not own company source-document storage or the code-point slicing of a company source read (that is iknow-memory's).
- attachment capability does **not** own company-source access, and an attachment is **never** pushed into the permanent company corpus (ADR 0083, ADR 0085). It does not own byte storage media — it consumes corpus's **object store** port.
- corpus does **not** own serve-time citation, thread-attachment data, or any conversation/track behavior. It holds no budget or lifecycle authority over attachments.
- retrieve does **not** own **切分**, ranking, or the product lifecycle of a **research thread**. It answers to the port contract only.
- parser does **not** own attachment or corpus visibility, readiness, indexing, publication, or citation verification. It owns the parse and the durable result; fin decides what may be used.
- driver layer owns no product contract; nothing may depend on it to work.

## Dependency direction

Legal edges, measured on `src/fin/`. Counts are `from fin.<pkg>` import statements,
counted wherever they appear in a module including inside a function, and the
quoted `3` on the debt edge is top-level only; the tree contains zero bare
`import fin.<pkg>` statements, so the counts are complete.

```
host  ──56──▶ corpus      corpus ──1──▶ host          ← one deferred import, see below
host  ──49──▶ retrieve    corpus ──14──▶ retrieve
retrieve ──24──▶ corpus   retrieve ──3──▶ host        ← debt, see below
eval  ──9/4/15──▶ corpus / host / retrieve            eval ──0──▶ by all four
parser ──0──▶ corpus / host / retrieve / eval         parser ──0──▶ by all four
```

Verification commands (re-measured on 2026-10-21):

```bash
grep -rh "from fin\.corpus import\|from fin\.corpus\." src/fin/host --include=*.py | wc -l   # → 56, the host ──▶ corpus row
grep -rn "from fin\.host\|import fin\.host" src/fin/corpus --include=*.py   # → 1 line, a function-level import
grep -rn "from fin\.eval\|import fin\.eval" src/fin/corpus src/fin/retrieve src/fin/host --include=*.py   # → 0
grep -rn "^from fin\.host" src/fin/retrieve --include=*.py                  # → 3 lines
grep -rh "from fin\.parser import\|from fin\.parser\." src/fin/parser --include=*.py | wc -l   # → 32, all of them fin.parser.* to itself
grep -rn "from fin\.corpus import\|from fin\.corpus\.\|from fin\.host import\|from fin\.host\.\|from fin\.retrieve import\|from fin\.retrieve\.\|from fin\.eval import\|from fin\.eval\." src/fin/parser --include=*.py | wc -l   # → 0, the parser context imports no other context
grep -rn "from fin\.parser\|import fin\.parser" src --include=*.py | grep -v "^src/fin/parser/" | wc -l   # → 0, no other context imports the parser
find src -type d \( -name utils -o -name helpers -o -name common -o -name shared \
     -o -name controllers -o -name services -o -name repositories -o -name models \) # → 0
```

- The **corpus → host** count is 0 at module level: the corpus still holds no thread state and reaches no host module at import time. There is exactly one function-level import, at `src/fin/corpus/catalog.py:77` (`from fin.host.fin_home import resolve_fin_home`); it predates this change — it arrived with commit `ff1e83ad` — and it is recorded as debt rather than blessed.
- The **parser context** has no edge in either direction: it imports only `fin.parser.*`, and no other context imports it. It is reached over its own HTTP API and its own `fin_parser` schema, which is what makes it an independent service rather than a fourth library inside fin.
- The **retrieve → corpus** and **corpus → retrieve** edges are package-level bidirectional, but there is no module-level import cycle: the two sides use disjoint modules (`retrieve/push_port.py:75` → `corpus/push.py`, which imports only `fin.corpus.*`). It is coupling to shrink, not a cycle to break.
- The boundary to **iknow-memory** is a port, not a dependency: `src/fin/host/compile.py:132` holds `DEFAULT_MCP_COMMAND = ("iknow-memory",)` — a subprocess command name resolved at boot and spoken to over stdio MCP by `src/fin/retrieve/mcp_session.py`. No Python import of that codebase exists in this repo.
- host's 49 edges into retrieve are legal in one direction only because host is the composition root: `src/fin/host/graph.py:185` names the adapter classes solely to choose which one to build (`graph.py:2296`), and every call site is typed against the retrieve port types (`host/graph.py:192-193`; the `read_source` tool narrows its port through the typed helpers in `fin/retrieve/source_read.py`). Callers outside `compile.py` / `graph.py` must keep using the port types; importing an adapter class anywhere else turns this into a real implementation dependency.
- The pre-existing rows were re-measured on 2026-10-21; the figures they carried earlier were snapshots of an older tree, so the difference between the two sets of numbers is drift in the tree, not an effect of the parser context. This branch moves two of them by one — host → corpus and retrieve → corpus each gain the corpus-owned identity import — and the debt edge moved the same way: the third importer (`src/fin/retrieve/mcp_index_job_port.py:34`) predates this branch, so that debt item's count is a re-measurement of pre-existing debt, not an edge added here.

## Current placement rule for attachment work

In the current code shape, a new module for attachment resource bounds (page counts, vision calls, rendered-page size, preparation concurrency) belongs to the **attachment capability in the host-agent context** — that is, a new `src/fin/host/attachment/<name>.py` beside `attachment_prepare.py`. This is the as-is placement rule, not the target location for parser implementations.

It must not move budget ownership into `fin.corpus`, because:

1. budget is a **thread attach** lifecycle property (ADR 0089: `ensure_attachment_ready` starts or joins durable preparation, and deletion tombstones block late results), and fin's corpus context holds no thread state;
2. placing it in corpus would force corpus to import host attachment types, adding a module-level `corpus → host` edge and converting the verified absence of one into a host↔corpus cycle;
3. the only corpus surface the attachment capability may consume is the published **object store** port, as `src/fin/host/attachment/attachment_prepare.py:25` already does.

For the current implementation, parser / OCR / vision adapters and the thread-private index stay inside the attachment capability; the graph node owns sequencing and waits only; **workspace HTTP** owns upload and send gating and request/response only; and the retrieve capability composes company and attachment candidates through source-specific adapters without duplicating company ranking or moving attachment text to iknow-memory. The adopted target below moves parser-job ownership to the independent parser service while retaining fin's attachment visibility, readiness, indexing, and citation responsibilities.

## Known boundary debt

These are pre-existing and un-fixed; the map records them rather than blessing them.

1. **Three reverse edges into host config.** `src/fin/retrieve/mcp_push_port.py:60`, `src/fin/retrieve/mcp_invalidation_port.py:51` and `src/fin/retrieve/mcp_index_job_port.py:34` all do `from fin.host.compile import DEFAULT_MCP_COMMAND`. This contradicts the rule the retrieve context states for itself at `src/fin/retrieve/tiers.py:13-15`: "the port may not import `fin.host` at module level"; `tiers.py` was moved to a neutral leaf precisely to break that edge. Fix direction: push the MCP command default into a leaf both sides depend on, or pass it in at the composition root.
2. **The attachment capability now has a package of its own.** Its ownership statements in ADR 0088 and ADR 0089 are context-shaped — its own data, rules, and lifecycle — and the P0 reorganisation gave that context its own physical form: `src/fin/host/attachment/`. Its access stays host-resolved (`retrieve` and `read_source` bind to the trusted active thread), so the package is a capability boundary inside the host-agent context, not a new context.
3. **`fin/eval` is a driver over the corpus, host-agent and retrieve contexts, not a context of its own** — it imports corpus (9), host (4), and retrieve (15), imports the parser zero times, and is imported by none. `src/fin/eval/__init__.py` states the one-way property. Its placement in the same namespace as the business contexts invites the five-packages-are-five-contexts misreading; it is a technical layer that has not been moved out.
4. **The attachment and evidence capabilities form a package cycle.** `src/fin/host/attachment/attachment_visual_read.py` imports the evidence capability's visual observation type, and `src/fin/host/evidence/structured_evidence_drain_worker.py` imports the attachment capability's periodic drain worker base. The P0 reorganisation made this cycle visible as a package-level edge; it was already there at module level. Fix direction: move the shared observation type and the drain worker base to a leaf both sides depend on, or pass the drain cadence in at the composition root. `scripts/check_capability_boundaries.py` records the edge so a new one still fails.

## Adopted QA-chain ownership refinement (2026-10-16)

This subsection records the operator-adopted design after fresh architecture review. Implementation and measured import changes remain pending; it adds no bounded context. Existing dependency counts and known debt above remain historical observations.

- Within host-agent, the server composition/lifespan role owns application resources and worker supervision; a single per-run lifecycle hides execution, cancellation, late-result fencing and the **turn receipt** projection. Existing composition roots remain the only selectors of concrete retrieve adapters.
- Within the QA-chain design, Task Runtime remains the product task/checkpoint writer; Source Scope proposes restrictions and rechecks current authority; that task workflow records explicit scope changes in existing thread snapshots. This is distinct from the process-level **Fin Runtime** adopted below, which owns accepted client operations and run dispatch, not Grounded Answer's semantic/admission behavior. **route intent** does not add a fifth **track**.
- Corpus owns catalog identity, metadata and publication; retrieve owns the neutral request/result seam and MCP translation. The server does not move catalog, ranking, attachment or retrieval state into a generic service layer. Attachments retain their host-owned prefix and thread-private lifecycle.

See [the accepted server ADR](adr/0101-server-owns-runtime-lifetime.md), [server runtime contract](../specs/qa-chain-server-runtime.md), and [vocabulary](CONTEXT.md). No `controllers/`, `services/`, `repositories/` or parallel context-map file is introduced.

## Adopted durable Runtime and parser ownership (2026-10-20)

This is the accepted target architecture, not a description of current code. It refines the process and persistence ownership above without inventing implementation paths or claiming that the existing single-process guard, sync saver, or frontend state has already changed. The durable decision is [ADR 0103](adr/0103-durable-runtime-and-parser-ownership.md); [ADR 0101](adr/0101-server-owns-runtime-lifetime.md)'s current guard remains until the rollout evidence passes.

| Target boundary | Owns | Does not own | Authority and boundary |
|---|---|---|---|
| **Fin Runtime** API | Durable accepted operations, per-thread FIFO order, run reservation, execution ownership, cancellation/deletion intent, durable terminal messages, snapshots, and the browser event seam | Graph checkpoint contents, parsing-job lifecycle, retrieval ranking, or attachment readiness | Runtime tables in PostgreSQL; graph work is claimed and fenced by supervised workers |
| Graph worker / host graph | LangGraph execution and tool/business behavior for its claimed logical **run** | Acceptance queue, durable operation identity, or ownership beyond its fenced **worker attempt** | Runtime reservation and attempt lease authorize work; graph state is written only through the checkpointer boundary below |
| LangGraph **checkpointer** | Graph checkpoint state | Accepted inputs, run queue/order, parser status, terminal business answer/evidence, or worker ownership | Same PostgreSQL database as Runtime, separate checkpoint schema; existing synchronous saver interface is preserved behind a mandatory generation-aware adapter |
| Independent parser service | **parser job** state, parser attempts, cancellation, and durable parsed-result artifacts | Thread/corpus visibility, publication/readiness, attachment indexing, citation verification, or browser projection | Parser-owned schema and restricted role in the same PostgreSQL database; independent API and resource-limited worker processes |
| fin attachment and corpus capabilities | Attachment/corpus identity and access scope, association to parser jobs/results, attachment visibility, thread-private indexing/readiness, corpus publication, and citation rules | Accepted-operation identity, parsing-job execution authority, or parser-worker ownership | Existing product-domain authorities remain with fin; parser completion is first recorded in fin's durable inbox, with status reconciliation as recovery |
| PostgreSQL | Durable state for each owner | A shared logical authority merely because schemas share one server | One physical database initially; Runtime, graph checkpoints, and parser have separate schemas and restricted logical roles |
| Redis Streams | Bounded notification, wakeup, and live event delivery/replay | Accepted-operation truth, queue recovery, parser result truth, or terminal-answer durability | PG outbox publishes; consumers deduplicate and reconcile from the owner's PostgreSQL state |
| Browser / Agent UI | Independent per-thread projections, reconnect cursors, and user-visible recovery | Runtime, worker, parser, or Redis authority | Reads snapshots and subscribes only through the Runtime API; each browser/tab reads independently |
| Evaluation driver | Mechanism reports, browser-journal evidence, and versioned **Fin acceptance case set** runs | Product answer logic or the real authorized **评测数据集** | Evaluation evidence is separate from product authorities and reported by evidence layer |

### Required seams and invariants

- **Checkpoint fencing:** Runtime authority and LangGraph checkpoints use different schemas in the same PostgreSQL database. Every mutating sync saver path (`put`, `put_writes`, batch writes, and any other checkpoint mutation) must use one generation-aware adapter that locks and validates the thread reservation, run/attempt lease, attempt and thread generations, cancellation state, and non-tombstoned state in the same database connection and transaction as the checkpoint write. The shared lock order is thread reservation → run/attempt authority → checkpoint record for claims, renewals, cancellation, deletion, and writes. Transactions stay short and never span model or network work. A Python pre-check followed by an independent saver commit is insufficient; graph workers cannot bypass the adapter or fall back to the stock unfenced saver. The synchronous graph/checkpointer contract remains; this decision does not require an async rewrite.
- **Thread event continuity:** PostgreSQL assigns a monotonic `thread_event_seq` to every client-visible run and thread-lifecycle event across all runs for that thread. Each state projection/revision change, sequence allocation, and corresponding outbox row commits in one transaction; a snapshot reads the projection, revision, and thread high-water mark from one consistent PostgreSQL snapshot. This prevents returning a new cursor with stale state. A per-run sequence is auxiliary. The generation-bound opaque cursor advances only over a provable contiguous thread sequence; any unresolved ordering inversion, missing sequence, trimmed range, or other unprovable continuity returns `reset_required` and a fresh snapshot. Redis stream IDs and per-run counters cannot substitute for the thread cursor.
- **Parser completion:** Parser notifications are hints delivered through Redis Streams. fin first commits completion to its durable inbox as pending consumption, then fetches the parser-owned result and validates schema, job, source content version/hash, access scope, and expected generation outside any PG lock transaction. External validation does not grant caller authorization: both the short result-adoption transaction and the final ready transaction lock the current caller attachment/corpus row and compare-and-swap the expected generation, content version/hash, job association, and non-tombstone/non-cancel state; mismatches reject attachment or readiness. The first transaction adopts the validated result reference and consumption state; the parser retains artifact ownership. Required indexing and visual/readiness work also runs outside ownership-lock transactions. Only after all required conditions succeed does fin atomically commit the ready projection, thread event sequence, and outbox/resume intent. Fetch, validation, indexing, or commit failures remain non-ready and reconcile; duplicate or late results are filtered by job, version, and generation. Parsing work and user-facing graph events have separate owners and event paths.
- **Tool validation evidence:** One canonical Pydantic schema governs model-facing arguments and runtime validation at the shared invocation boundary; invalid input is rejected before side effects and may receive one bounded **schema correction**. An uncertain side effect is reconciled before retry. The **tool outcome ledger** supplies attempt/outcome counts and denominators; five-span OTel **operational trace** data remains diagnostic and best-effort under ADR 0102.
- **Acceptance evidence:** The evolving **Fin acceptance case set** has a minimum of 50 independent business cases and is reported separately from mechanism tests, browser journeys, and the real authorized **评测数据集**. Browser navigation, continuity, multi-thread ordering, duplicate intent, reconnect, cancellation, and attachment progress remain release evidence, not optional backend-only substitutes. Bad cases add versioned cases without overwriting prior results.

The four-stage work sequence is tracked in the [Runtime reliability map](wayfinder/harness-runtime-reliability-map.md); this map records target ownership, not implementation progress. No RabbitMQ, PGMQ, second workflow engine, or second scheduler is added. Existing single-process protection remains until process-level fencing, restart, browser, and rollout evidence passes.
