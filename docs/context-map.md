# fin — Context Map

> Vocabulary authority is [`CONTEXT.md`](CONTEXT.md). This map names the bounded
> contexts, their ownership edges, and the legal dependency direction. It defines
> no term: every bold term below is consumed from `CONTEXT.md` verbatim.

## Language

**host-agent context**:
The **host agent** boundary: **fin** as the LangGraph runtime that routes tracks and calls grounded-track tools behind the **retrieve port**; it is not the ranking or vector store. Physically `src/fin/host/`.

**attachment capability**:
The **thread attach** boundary inside the host-agent context: fin owns each thread's durable raw bytes, parsed units, versioned **visual observations**, thread-private searchable index, and lifecycle state (ADR 0088). Physically a module prefix, not a package: `src/fin/host/attachment_*.py`.

**corpus context**:
The ingest-side boundary: fin parses source text into **冻结单元**, deterministically **切分**s it, and performs **入库推送** through the push and invalidation ports (ADR 0037). Physically `src/fin/corpus/`.

**retrieve context**:
The seam boundary: the **retrieve port** fin is allowed to depend on, plus its **retrieve adapter** implementations. Ranking stays outside fin. Physically `src/fin/retrieve/`.

**driver layer**:
**contract eval** and **capability eval** harnesses that compose the three contexts above to emit quantified reports. Not a business context; it owns no product data. Physically `src/fin/eval/`.

## Contexts

| Context | Physical surface | Purpose | Owns | depends_on (legal direction) | Integration pattern |
|---|---|---|---|---|---|
| host-agent | `src/fin/host/` (`graph.py`, `compile.py`, `state.py`, `tools/`, `citation_gate.py`, `numeric_gate.py`, `thread_store.py`, `workspace_http.py`), `web/` | run a **research thread** to a cited answer | tracks, **agent loop**, host tools, **citation verifier**, **step cap** / **citation-repair cap**, **session compaction**, run + thread durability, **工作台** surface | corpus published value types; retrieve ports for every call, plus the two retrieve adapter classes it selects at the composition root | composition root — it selects and injects adapters |
| attachment capability | `src/fin/host/attachment_store.py`, `attachment_prepare.py`, `attachment_index.py` | make one thread's own file answerable, then disposable | **uploaded** → **ready** lifecycle, **attachment preparation job**, parse/OCR/vision derivatives, thread-private index, retry, tombstone deletion | corpus `object_store` port, host `thread_store` + graph | thread-bound interface owned by host; never a package of its own while its data is host-resolved |
| corpus | `src/fin/corpus/` (`extracted.py`, `chunking.py`, `push.py`, `invalidation.py`, `object_store.py`, `catalog.py`, `facts.py`, `ingest_endpoint.py`, `parsers/`) | turn a file into reproducible pushed chunks | **冻结单元**, **切分**, **结构化元数据**, **幂等键**, **入库推送** payloads, **内容失效**, **object store** byte custody, catalog, XBRL fact table | retrieve push + invalidation ports | port consumer; iknow-facing transport stays in retrieve |
| retrieve | `src/fin/retrieve/` (`port.py`, `read_port.py`, `push_port.py`, `invalidation_port/`, `mcp_adapter.py`, `mcp_session.py`, `capabilities/`) | keep iknow-memory swappable behind one seam | **retrieve port** contract, **retrieve adapter** (MCP client), **降级标记** reading, hit→locator mapping, input validation at the seam | corpus published types (`Locator`, push payload, `InvalidationTarget`) | port + adapter; out-of-process MCP 2.0 stdio client |

Outside this repository, **iknow-memory** is a context of its own: the product 库, the ranking and vector/index authority, and the company-source boundary. It is reached only through the **retrieve port** and **知识 MCP**; there is no import edge from fin to it (ADR 0001: "do not compile-time couple the two repos").

## Explicit non-ownership

- host-agent does **not** own ranking, embeddings, or the vector store; it does not own company source-document storage or the code-point slicing of a company source read (that is iknow-memory's).
- attachment capability does **not** own company-source access, and an attachment is **never** pushed into the permanent company corpus (ADR 0083, ADR 0085). It does not own byte storage media — it consumes corpus's **object store** port.
- corpus does **not** own serve-time citation, thread-attachment data, or any conversation/track behavior. It holds no budget or lifecycle authority over attachments.
- retrieve does **not** own **切分**, ranking, or the product lifecycle of a **research thread**. It answers to the port contract only.
- driver layer owns no product contract; nothing may depend on it to work.

## Dependency direction

Legal edges, measured on `src/fin/`. Counts are `from fin.<pkg>` import statements; the tree contains zero bare
`import fin.<pkg>` statements, so the counts are complete.

```
host  ──31──▶ corpus      corpus ──0──▶ host          ← hard isolation, verified
host  ──21──▶ retrieve    corpus ──8──▶ retrieve
retrieve ─21─▶ corpus     retrieve ──2──▶ host        ← debt, see below
eval  ──9/4/15──▶ corpus / host / retrieve            eval ──0──▶ by all three
```

Verification commands:

```bash
grep -rn "from fin\.host\|import fin\.host" src/fin/corpus --include=*.py   # → no matches (0)
grep -rn "from fin\.eval\|import fin\.eval" src/fin/corpus src/fin/retrieve src/fin/host --include=*.py   # → 0
grep -rn "^from fin\.host" src/fin/retrieve --include=*.py                  # → 2 lines
find src -type d \( -name utils -o -name helpers -o -name common -o -name shared \
     -o -name controllers -o -name services -o -name repositories -o -name models \) # → 0
```

- The zero **corpus → host** count is the mechanical form of the corpus-isolation invariant: attachment and thread state must never reach ingest, and ingest must never reach a thread. It has no exceptions today, and point 2 of *Placement rule* exists to keep it that way.
- The **retrieve → corpus** and **corpus → retrieve** edges are package-level bidirectional, but there is no module-level import cycle: the two sides use disjoint modules (`retrieve/push_port.py:75` → `corpus/push.py`, which imports only `fin.corpus.*`). It is coupling to shrink, not a cycle to break.
- The boundary to **iknow-memory** is a port, not a dependency: `src/fin/host/compile.py:132` holds `DEFAULT_MCP_COMMAND = ("iknow-memory",)` — a subprocess command name resolved at boot and spoken to over stdio MCP by `src/fin/retrieve/mcp_session.py`. No Python import of that codebase exists in this repo.
- host's 21 edges into retrieve are legal in one direction only because host is the composition root: `src/fin/host/graph.py:185` names the adapter classes solely to choose which one to build (`graph.py:2296`), and every call site is typed against `RetrievePort` / `ReadPort` (`host/graph.py:192-193`, `host/tools/read_file.py:37`). Callers outside `compile.py` / `graph.py` must keep using the port types; importing an adapter class anywhere else turns this into a real implementation dependency.

## Placement rule for attachment work

A new module for attachment resource bounds (page counts, vision calls, rendered-page size, preparation concurrency) belongs to the **attachment capability in the host-agent context** — that is, a new `src/fin/host/attachment_<name>.py` beside `attachment_prepare.py`.

It must not move budget ownership into `fin.corpus`, because:

1. budget is a **thread attach** lifecycle property (ADR 0089: `ensure_attachment_ready` starts or joins durable preparation, and deletion tombstones block late results), and fin's corpus context holds no thread state;
2. placing it in corpus would force corpus to import host attachment types, creating the **first** `corpus → host` edge and converting the verified zero-count isolation into a host↔corpus cycle;
3. the only corpus surface the attachment capability may consume is the published **object store** port, as `src/fin/host/attachment_prepare.py:25` already does.

The same rule bounds the sibling seams: parser / OCR / vision adapters and the thread-private index stay inside the attachment capability; the graph node owns sequencing and waits only; **workspace HTTP** owns upload and send gating and request/response only; and the retrieve capability composes company and attachment candidates through source-specific adapters without duplicating company ranking or moving attachment text to iknow-memory.

## Known boundary debt

These are pre-existing and un-fixed; the map records them rather than blessing them.

1. **Two reverse edges into host config.** `src/fin/retrieve/mcp_push_port.py:59` and `src/fin/retrieve/mcp_invalidation_port.py:52` both do `from fin.host.compile import DEFAULT_MCP_COMMAND`. This contradicts the rule the retrieve context states for itself at `src/fin/retrieve/tiers.py:13-15`: "the port may not import `fin.host` at module level"; `tiers.py` was moved to a neutral leaf precisely to break that edge. Fix direction: push the MCP command default into a leaf both sides depend on, or pass it in at the composition root.
2. **The attachment capability has no package of its own.** Its ownership statements in ADR 0088 and ADR 0089 are context-shaped — its own data, rules, and lifecycle — while its physical form is a filename prefix inside `src/fin/host/`. It is mapped as a capability of host-agent because its access is host-resolved (`retrieve` and `read_source` bind to the trusted active thread). Treat that as the current shape, not the target design.
3. **`fin/eval` is a driver over all three contexts, not a fourth one** — it imports corpus (9), host (4), and retrieve (15) and is imported by none. `src/fin/eval/__init__.py` states the one-way property. Its placement in the same namespace as the business contexts invites the four-packages-are-four-contexts misreading; it is a technical layer that has not been moved out.
