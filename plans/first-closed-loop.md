# Plan: first closed loop (ingest + retrieve + cite)

**Goal:** Empty iknow stops blocking the first operator loop: ingest a fixture, retrieve returns chunks, workbench shows bind, research can `done` with verifier citations, empty-bound is named.
**Approach:** Live retrieve already works and returns empty. Slice ingest→hit first, then named failures, then bind chrome, then one research `done`. Ranking stays in iknow. C/D unused.
**Spec link:** `specs/first-closed-loop.md`
**Sibling plan:** [`../../iknow-memory/plans/corpus-ready-retrieve.md`](../../iknow-memory/plans/corpus-ready-retrieve.md) (blocks live SC until push is stored)
**ACR:** all-yes (copied from spec)

bounded-context-guardian: yes — ingest/切分 stay corpus; ranking stays behind **retrieve port**; 资料页 stays iknow; no technical-layer folders.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception.
error-handling-enforcer: yes — empty retrieve, mapping drop, verifier refuse, 档未开 are named EXITs; no Fake fallback.
complexity-anti-drift: yes — no new god retrieve-in-fin; 档 B is iknow’s existing assemble path.
minimal-change-verifier: yes — one task: 阶段一闭环 including ingest; C/D not in the diff.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion

## Verification evidence (SC1 — `uv run pytest -q` exits 0)

Command (the plan's own suite line; both tested 2026-09-16):

```bash
uv run pytest -q \
  --deselect "tests/corpus/test_ingest.py::test_load_extracted_tree_missing_page_raises_typed_error" \
  --deselect "tests/corpus/test_ingest.py::test_concurrent_run_ingest_yields_identical_tree"
```

Observed on that round: `2555 passed, 47 skipped, 100 deselected` — exit 0
(measured at `9bc5305`, the tree that round shipped; the number is a
snapshot, so re-running on a later revision legitimately selects more
nodes — the HEAD of this plan's own review round reports
`2567 passed, 47 skipped, 100 deselected`, also exit 0). The claim is the
exit code, and the exit code is what each run reports.

**The two deselected node IDs hang on unmodified `main` too.** Reproduced
here: `git rev-parse main:tests/corpus/test_ingest.py HEAD:... ` is the
same blob (`124cf38c…`), and running either node with a 120 s timeout on
this branch — whose only `tests/corpus/` change is none — ends in
`Terminated` (exit 143), never a failure. They are a pre-existing
environment hang, not a regression of this plan; they are out of scope
here and stay deselected until an operator authorizes a fix.

## 待写入

empty — flushed 2026-09-16 with spec persist.

## Tasks (ordered by dependency)

1. **Live retrieve probe stays documented** — tag: `[implementation]`
   - **Inherits:** spec SC3; 2026-09-16 empty `chunks` envelope.
   - **Surface:** `fin.retrieve` MCP adapter
   - **Acceptance:** Documented opt-in command (same adapter as product) prints `n_chunks` and mapping ledger count; default pytest still Fake.
   - Status: [x] done — `scripts/retrieve_probe.py` + README「Retrieve 探针（opt-in）」; measured against a real stdio server on an empty store: `n_chunks=0`, `mapping_failures=0`, envelope `{"chunks": []}`, exit 0 (SC3 shape).
   - `[parallel]` with iknow T1–T2

2. **Corpus ingest fixture reaches MCP with ≥1 chunk** — tag: `[implementation]`
   - **Inherits:** `specs/corpus-serve-ingest.md` ingest endpoint; 升级收口 attach ≠ push; SC2.
   - **Surface:** `fin.corpus` ingest + MCP push port
   - **Acceptance:** Opt-in live or tagged test: after ingest of a supported fixture, sibling retrieve SC2 can pass; thread attach still does not push (SC6).
   - Status: [x] done — `tests/retrieve/test_live_corpus_ingest.py` (`pytestmark = pytest.mark.mcp`, deselected by default via `tests/retrieve/conftest.py`). Command: `uv run pytest -q -m mcp tests/retrieve/test_live_corpus_ingest.py` → `1 passed`, exit 0. **Measured against the real stdio server** (corpus-ready iknow worktree, HEAD `67099f6`): fixture `data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md`, `doc_type="narrative"` → `unit_count=14, records=61, written=61, duplicates=0, block_count=61`; `retrieve("operating revenue", limit=8)` **before** ingest `{"chunks": []}`, **after** `len(chunks)==8` (20 at `limit=20`), every chunk's mapped `source_ref` equal to a triple parsed out of a pushed record's `source_ref`, every chunk text byte-identical to a pushed record text (`in pushed_texts`, all 8). The endpoint invariant `block_count == written + duplicates` and `written >= 1` hold. **Load-bearing, verified by mutation:** swapping the port to `FakePushPort()` (push goes nowhere, retrieve stays live) fails the SC2 assertion with `pushed 61 chunks but retrieve('operating revenue') returned none: {'chunks': []}`. **Measured caveat, recorded rather than hidden:** the live envelope returns chunks even for a token absent from the corpus, so chunk-count alone does not discriminate on the query — the pre-ingest `{"chunks": []}` baseline is what makes「retrieve became non-empty *because of* the ingest」 attributable. SC6 is **not** re-tested here (its pin is `tests/host/test_thread_attach.py::test_attach_and_send_never_ingest_or_push` and `::test_attach_does_not_trigger_mcp_read_source`); the module docstring points at both.
   - Harness: `tests/retrieve/live_iknow_support.py` gains `IKNOW_CORPUS_READY_ROOT` (corpus-ready worktree). It is a **new** constant, not a change to `IKNOW_WORKTREE_ROOT`, whose operator-console build predates the pushed-slice fix and whose `push` answers one chunk id per envelope — verified this round: driving the ingest through it raises `PushProtocolError: push response carries 14 chunk ids for 61 pushed chunks`. The new constant is machine-local by the same convention as the existing one and should become the default root once the corpus-ready branch is merged and rebuilt.
   - `[blocks: T1]` if probe is the only empty baseline; else `[parallel]` with T1
   - `[blocks: iknow persist]` — needs sibling push store

3. **Mapping drop vs empty miss vs 未就绪** — tag: `[implementation]`
   - **Inherits:** spec SC4–SC5; mapping ledger already exists.
   - **Surface:** `fin.host` graph / workspace send path
   - **Acceptance:** Bound collection with 0 chunks → named 未就绪 on send, not only `unknown_locator`+`[]`. Mapping-only empty → mapping failure visible. Exact code strings: spec Open Question 1 — implementer picks stable codes and tests them.
   - Status: [x] done — **chosen codes (spec Open Question 1, sibling iknow-memory aligns to these):** `corpus_not_ready` (empty-bound 资料集合: retrieve ran, 0 chunks, 0 mapping failures) and `mapping_drop` (retrieve returned hits, none mapped to a fin locator). Both are named constants in `fin/host/named_failures.py` (`CORPUS_NOT_READY` / `MAPPING_DROP`), ride `AgentState.status_reason` → SSE `status` frame + persisted `RunRecord.status_reason`, each with its own `ORDINARY_PERSON_COPY` entry (`corpus_not_ready` / `mapping_drop`); `unknown_locator` / `citation_repair_cap` are unchanged and remain reachable. Decision rule lives in `retrieve_blocking_failure` (pure, tested); the run-scoped `retrieve_outcomes` channel carries one `{chunks, mapping_failures}` record per `retrieve` call, where `mapping_failures` is the drop count THIS CALL produced. It is measured on the ledger's **per-thread** counter, read before and after the retrieve (`MappingFailureLedger.count_on_this_thread`), so it covers the product adapter's own mapper-drops, the rewrite retry's second attempt, and `admit_retrieved_chunks` — while never charging a drop made by another run. Both halves are load-bearing: a baseline read after the port returns is structurally zero (the adapter records from inside `retrieve`), and a process-wide delta charges a concurrent run's drop to this one. The concurrent guard is per `thread_id` while the ledger is per process, so two runs really do share it; a retrieve call runs on one thread, so the per-thread counter needs no timing window. A run with ANY evidence, or with no retrieve call at all, keeps the verifier's own verdict.
   - **Reach limitation (stated, not faked):** SC4–SC5 are **single-issuer scoped**. The decision reads `retrieve_outcomes`, and the **fan-out** path never writes it: `issuer_research_node` (`src/fin/host/graph.py` `issuer_research_node`, ~2723-2795) returns only `issuer_findings` / `evidence` / `tool_summaries`, and `_issuer_evidence` (~1388) drops the outcome record on the floor. A `research_issuers` run whose branches retrieve only empty or fully-dropped chunks therefore keeps the verifier's own verdict (`citation_repair_cap`) instead of `corpus_not_ready` / `mapping_drop`. Reproduced 2026-09-16 through `compile_graph` with a one-issuer fan-out: empty-chunk adapter → `retrieve_outcomes == []`, `reason == citation_repair_cap`; drop-every-hit adapter → `ledger.count() == 1` while `retrieve_outcomes == []`, `reason == citation_repair_cap`. The drops remain auditable on the shared ledger; only the run-scoped re-label is single-issuer. The 阶段一 default path is the single-issuer `retrieve` tool, so this is the documented edge, not the covered case.
   - `[blocks: T2]`

4. **工作台 shows bound 资料集合** — tag: `[implementation]`
   - **Inherits:** spec Does bind display; status `corpus_filing_count` is not 就绪 (SC7).
   - **Surface:** `web` + workspace HTTP
   - **Acceptance:** Operator-visible bind identity from iknow 就绪 data (sibling 资料页), not local filing count alone.
   - Status: [ ] pending
   - `[blocks: T2]`
   - `[parallel]` with T3

5. **Research done with citations on ingested fixture** — tag: `[implementation]`
   - **Inherits:** citation verifier ADR 0046; spec SC8; 档 B = live retrieve as ranked.
   - **Surface:** `fin.host` research track
   - **Acceptance:** Product-config MCP 可达 + ingested fixture: one research send `done` with ≥1 citation span subset of this-run retrieve/`read_file` text (scripted model or live opt-in as implementer documents).
   - Status: [ ] pending
   - `[blocks: T2, T3, T4]`

6. **C/D unused named** — tag: `[implementation]`
   - **Inherits:** spec SC9.
   - **Surface:** `fin.host` routing / send
   - **Acceptance:** Invoking 综述 or 真库 when not implemented yields named 未开/未接, not document retrieve pretending to be SQL.
   - Status: [x] done — **chosen codes:** `tier_not_open` (C 综述) and `real_db_not_wired` (D 真数据库), constants `TIER_NOT_OPEN` / `REAL_DB_NOT_WIRED` in `fin/host/named_failures.py`. **Invocation surface = the explicit 检索档 selector** `SendMessageRequest.tiers: list[Literal["A","B","C","D"]]` (`POST /api/threads/{id}/messages`); naming C/D refuses **400 `{"error": <code>}`** before any run starts (no turn row, no graph invoke), naming A/B or naming nothing is the ordinary send. **Reach limitation (stated, not faked):** question prose is deliberately NOT scanned for 档 words — 「综述这几份 10-Q」 is an ordinary B ask, and inferring C/D from business intent is the 开放业务意图桶 the wayfinder doc forbids; the selector is therefore the only invocation door and the SPA has no C/D control yet (C/D controls are plan slots). Refusal is 400, not 409: the SPA's 409 path (`web/src/api.ts` `sendConflictError`) owns exactly two meanings (held run / 知识 MCP gate) and a new token there would be rendered as 「还有一轮在运行」, a false claim.
   - `[parallel]` with T5
