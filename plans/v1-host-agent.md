# Plan: v1 host agent

**Goal:** Operator can ingest-frozen corpus, ask on CLI (and HTTP), and get `done` with code-filled citations or `refuse` / `step_cap`.
**Approach:** Stand up the package, freeze extracted pages, then three parallel tool/gate slices, then one host-graph slice that wires them, then CLI / Postgres / HTTP in parallel, then the opt-in eval mark. No MCP, no HITL UI, no live LLM in default pytest.
**Spec link:** `specs/v1-host-agent.md`
**Tracker:** GitHub `the private organisation/fin` (main path). Spec the tracked change ; T1–T10 are #10–#19 with native blocking edges.
**ACR:** all-yes (copied from spec)

bounded-context-guardian: yes — three capability packages (`corpus`, `retrieve`, `host`); retrieve stays behind the port; HTTP/CLI are host surfaces; no technical-layer top-level dirs.
defensive-contract-validator: yes — tools cover empty/unknown/out-of-range; gate covers missing cites, unknown locator, repair overflow (3), concurrent one-run-per-thread, missing extracted artifacts as typed exception.
error-handling-enforcer: yes — typed stop/error kinds (`unknown_locator`, `citation_repair_cap`, `step_cap`, ingest-missing); no empty catch; bounce is an explicit return to the model, not a swallowed fail.
complexity-anti-drift: yes — ingest, retrieve adapter, tool functions, model node, citation gate, and surfaces stay separate; gate is match-and-fill, not a mixed LLM judge.
minimal-change-verifier: yes — one logical v1 ship; implementation splits into tracer-bullet commits in writing-plans; this spec is one contract.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → verification-before-completion → one commit on the ticket branch

**待写入:** (empty — flushed to CONTEXT / ADR 0019–0021 / `CLAUDE.md` in SPECIFY persist)

## Tasks (ordered by dependency)

1. **Package collects under pytest** — tag: `[implementation]` (#10)
   - **Inherits:** spec: Python 3.12, uv, `src/fin`, pytest; default `uv run pytest -q` has no live LLM
   - **Surface:** new `corpus` / `retrieve` / `host` packages as empty importable slices plus project metadata
   - **Acceptance:** `uv run pytest -q` exits 0 on a smoke test that imports the three packages
   - Status: [x] done

2. **Ingest writes committed extracted pages** — tag: `[implementation]` (#11)
   - **Inherits:** ADR 0020; spec: idempotent ingest; ask path does not open PDFs; missing artifacts are a typed ingest-missing error
   - **Surface:** `corpus`
   - **Acceptance:** ingest against the frozen 12 PDFs writes page text and locator-bearing passages under `data/corpus/extracted/`; a second run does not change file checksums; runtime load without that tree fails typed
   - Status: [x] done
   - [blocks: T1]

3. **[parallel] Catalog and page read match ADR 0009** — tag: `[implementation]` (#12)
   - **Inherits:** ADR 0009 `list_files` / `read_file`; extracted tree is the only page source
   - **Surface:** `corpus` + `host` tool wrappers
   - **Acceptance:** unknown filters → `{filings: []}`; unknown id / version mismatch / out-of-range page → `{error: "unknown_locator"}` without throw; happy path returns frozen page text and `page_count`
   - Status: [x] done
   - [blocks: T2]

4. **[parallel] Retrieve port is in-process BM25** — tag: `[implementation]` (#13)
   - **Inherits:** ADR 0001, 0007, 0009 `retrieve`; BM25 in RAM at process start from extracted passages
   - **Surface:** `retrieve`
   - **Acceptance:** empty query and unknown `document_id` → `{chunks: []}`; hits carry `source_ref` locators; ranking is lexical BM25, not cosine
   - Status: [x] done
   - [blocks: T2]

5. **[parallel] Citation gate fills only passed spans** — tag: `[implementation]` (#14)
   - **Inherits:** ADR 0008, 0021; spec: ≥32-char extractive span; hidden template bounce; repair cap 3; unknown locator refuses immediately
   - **Surface:** `host` (citation gate as code, not a model tool)
   - **Acceptance:** no evidence + non-refuse answer bounces then `refuse`/`citation_repair_cap` at 3; evidenced ≥32-char copy → `done` citations equal those spans; locator not in corpus → `unknown_locator` with no repair
   - Status: [x] done
   - [blocks: T2]

6. **Host graph reaches done / refuse / step_cap** — tag: `[implementation]` (#15)
   - **Inherits:** ADR 0015 tests `InMemorySaver`; 0016 thread/run; 0019 authored graph; step cap 32; `recursion_limit` > 32; scripted ChatModel
   - **Surface:** `host`
   - **Acceptance:** scripted tool-then-answer run is `done` with code-filled citations; 32 steps without terminal status is `step_cap`; at most one active run per `thread_id`
   - Status: [x] done
   - [blocks: T3, T4, T5]

7. **[parallel] CLI asks and prints status** — tag: `[implementation]` (#16)
   - **Inherits:** spec: `python -m fin`; stdout is status, prose, code-filled citations; optional `--thread-id`
   - **Surface:** `host` CLI
   - **Acceptance:** `python -m fin --help` exits 0; scripted `python -m fin "q"` prints status then answer then citations
   - Status: [x] done
   - [blocks: T6]

8. **[parallel] Durable compile uses Postgres** — tag: `[implementation]` (#17)
   - **Inherits:** ADR 0015 `PostgresSaver` + `FIN_CHECKPOINT_URL`; compose for local Postgres; CLI/HTTP still sync `invoke`
   - **Surface:** `host` compile + compose
   - **Acceptance:** `docker compose config` exits 0 and the example URL in `.env.example` matches that service; durable compile is not `InMemorySaver`
   - Status: [x] done
   - [blocks: T6]

9. **[parallel] HTTP invokes the same compiled graph** — tag: `[implementation]` (#18)
   - **Inherits:** ADR 0013 same graph, sync `invoke`, no auth
   - **Surface:** `host` HTTP
   - **Acceptance:** ask route uses the same compiled graph object as the CLI helper; handler calls sync `invoke` not `ainvoke`
   - Status: [x] done
   - [blocks: T6]

10. **Nine gold cases are opt-in eval** — tag: `[implementation]` (#19)
    - **Inherits:** ADR 0012; spec: 3 fact, 3 cross-period 2023 Q1–Q3, 3 refuse; default `pytest -q` does not run them
    - **Surface:** `host` eval
    - **Acceptance:** `uv run pytest -q -m eval` collects ≥9 cases in those three classes; `uv run pytest -q` still exits 0 without invoking a live LLM
    - Status: [x] done
    - [blocks: T6]
