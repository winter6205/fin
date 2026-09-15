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
   - Status: [ ] pending
   - `[blocks: T1]` if probe is the only empty baseline; else `[parallel]` with T1
   - `[blocks: iknow persist]` — needs sibling push store

3. **Mapping drop vs empty miss vs 未就绪** — tag: `[implementation]`
   - **Inherits:** spec SC4–SC5; mapping ledger already exists.
   - **Surface:** `fin.host` graph / workspace send path
   - **Acceptance:** Bound collection with 0 chunks → named 未就绪 on send, not only `unknown_locator`+`[]`. Mapping-only empty → mapping failure visible. Exact code strings: spec Open Question 1 — implementer picks stable codes and tests them.
   - Status: [ ] pending
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
   - Status: [ ] pending
   - `[parallel]` with T5
