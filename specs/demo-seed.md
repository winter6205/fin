# Spec: demo seed into the isolated dev stack

Status: implemented. Live browser acceptance requires the isolated `_test` configuration and credentials.
Paired with iknow-memory `specs/demo-seed-dev-stack.md` (Plan A). ADR: [0100](../docs/adr/0100-demo-seed-isolated-dev-stack.md). Plan: [`plans/2026-10-15-demo-seed.md`](../plans/2026-10-15-demo-seed.md).

## Objective

Run `seed:demo` once to load 20 fictional-company enterprise documents into the isolated dev stack (PG 5434, ES 9201, Neo4j 7688), so that after starting the service, cited Q&A can be demonstrated directly.

## Changes

1. In `src/fin/host/http.py` and `cli.py`, find where `transport_env` is assembled and add `IKNOW_MEMORY_HOME`; also add it to `INHERITED_ENV_ALLOWLIST` in `mcp_session.py:92-101`. For the registry, see `evidence_session_registry.py:43-49` and `:339-341`.
2. `scripts/dev-up.sh` supports pointing at the `iknow-memory-dev` compose (see `docs/guides/iknow-config-window.md:5`).
3. `status.py:77-80`: when the snapshot is None, show "not probed" instead of "missing".
4. Fixture (self-written corpus, fictional company):
   - The corpus is the in-house enterprise material of the fictional company ZEUS, written entirely for this demo and containing no real financial-report text. It has exactly 20 documents across eight categories: 3 employee handbooks, 3 product descriptions, 3 policy or process documents, 3 meeting minutes, 2 financial-report summaries, 2 contract templates, 2 FAQs, and 2 project weekly reports. All financial figures and business details are fictional.
   - Each document is stored as `fixtures/demo-corpus/<source_ref>.json`; the file name is the document id and the fixture's own `source_ref` field declares that same document id — it is not the wire value. The wire `source_ref` is the §B4 canonical serialization of the locator triple `{document_id, version, unit}`: the document id, the sha256 of the fixture file as the version, and the unit (the page). Every chunk starts with the document name and explicit page marker, then an explicit section marker; the page marker and the chunk's ordinal name the same page, and the seed refuses a fixture whose two disagree. Chunk lengths and page counts vary, and some documents cross-reference other corpus documents. The content is the ingest envelope verbatim (`ingest.ts:64-75`). Once committed the content is fixed, so content_identity is fixed.
   - The envelope does not carry separate page or section locator fields, so citations use the page and section markers in the chunk content (for example, `ZEUS Employee Handbook · Page 1, Section 1`).
   - Documents use mixed chunk lengths and one to three pages so short lookup material and longer guidance or reports are both represented.
   - Catalog demo rows are generated from the fixture instead, one row per document, keyed by `(document_id, version)` with the version being the same sha256 the wire `source_ref` carries, and with `page_count` and document type taken from the fixture's actual values; the 3 hard-coded rows in `demo_seed.py:25-29` are no longer kept. The seed owns the rows of the documents the fixture holds and replaces them on every write, so a fixture whose bytes changed cannot leave the previous version's row beside the new one; the rows of documents the seed does not own are never touched. `--reset` clears catalog demo rows by this set of source_refs.
   - `expected.json` contains 15 questions. It preserves the original q1–q6 cases, adds representative questions for the contract, FAQ, and weekly-report categories, and includes lexical, graph, and global document questions. It also includes a cross-document question, a follow-up turn in the same conversation, an SQL boundary question that must not invent rows from prose, and a positive SQL question against the synthetic contract ledger. There is at least one document-answerable question for every document category; the unsupported division-budget question and unseeded invoice lookup expect a refusal. Document questions state their key facts, source_ref, and page/section locator, and their facts are present in the cited fixture text. The positive SQL question records its four expected database rows separately from document citations.
5. Orchestration (`src/fin/corpus/demo_seed.py`, `scripts/seed_demo.py`), executed in this order:
   - Guard: require `IKNOW_MEMORY_HOME` to be set, not equal to `$HOME/.iknow-memory`, ports to be 5434, 9201, 7688, and `GRAPHRAG_MEMORY_STORAGE` to resolve to `pgvector`; if any condition fails, `exit 2`.
   - `--reset`: TRUNCATE by Plan A's allowlist; delete the ES indices; run `MATCH (n) DETACH DELETE n` in Neo4j; clear the demo rows in the fin catalog. No docker container or volume is touched.
   - Schema: call the entry point from Plan A item 9.
   - Embed probe: check the dimension and `embedding_generation`; on mismatch, `exit 2`.
   - Preview: by default only print status; write only with `--yes`.
   - Submit: one envelope, one wire `source_ref` and one index job per unit (the page) — the shipped corpus is 20 documents and 37 units, so 37 jobs. Each unit's content_identity is computed over the pushed body, which is that unit's own body; submit first, then handle the result:
     - `reused` with status succeeded: record as skipped.
     - `reused` with status failed: stop and print the `index:reset-job` command.
     - `reused` with status in_progress: keep polling.
     - Source does not exist (`index-job.ts:148-151`): ingest first, then submit.
   - Worker: start `index:worker --batch-size 1` as a subprocess, passing `IKNOW_MEMORY_HOME` explicitly, running the guard once more before start; in `finally`, send SIGTERM and wait for the process to exit.
   - Poll: poll until a terminal state. The total timeout for the first run is set by the executing session and written into the report; later runs use a multiple of the measured maximum per-source duration from the first `seed:demo --reset --yes`, with the actual duration recorded in the report. On failed, timeout, or `ENTITY_MERGE_EMBED_LIMIT`, exit non-zero and print job_id, failure_code, and the reset command.

6. Answer presentation (added 2026-10-15, operator requirement):
   - The internal receipt surface — run status, Runtime receipts, source evaluation — is **hidden from the user by default**. It renders only in developer mode (a configuration switch) or in the logs. The default user view carries none of it.
   - Answer prose carries **inline superscript citations** (`[1][2]`) placed immediately after the sentence or paragraph they support, not collected in a block. Hover or click on a superscript reveals the source excerpt it points at.
   - The answer ends with a **sources list**. Each entry carries document name, section or subheading, and page number, and is clickable to jump to that location.
   - When the corpus supports the question, the answer states it directly and carries its citations. When it does not, the answer says explicitly `资料中未找到相关内容` and fabricates nothing.
7. Frontend: the presentation reuses fin's existing components and styles. No new UI framework is introduced.

## Interface contract

- `ingest` (`ingest.ts:750`), `submit_index_job` (`index-job.ts:228`), `poll_index_job` (`:245`): inputs and outputs unchanged.
- The pushed `body_content_id` is the service's own content identity of the pushed body. The envelope's declared rule versions (`verbatim-v1` / `demo-fixture-v1`) are the SC20 filter surface; the identity is service-owned, because the index job is addressed by the pair the service resolves.
- Breaker log fields are `source_ref`, `embed_calls`, `embed_cap`; the matching failure code is `ENTITY_MERGE_EMBED_LIMIT`.
- Pool signals (no acquire/release summary log exists): `index-job-pool-idle-error` and `index-job-client-error` (field `error`; the client event adds `jobId`), `pgvector-pool-idle-client-error` (field `errorName`), and worker crash `index-job-worker-fatal` (field `error`).
- `index:reset-job`, the schema entry point, and the reset allowlist are defined by Plan A.
- Embedding model and dimension use the same fixed value on both sides.

## Acceptance

- Guard: each of the following cases must `exit 2`:
  - `IKNOW_MEMORY_HOME` not set;
  - pointing at the default directory;
  - wrong port;
  - embedding generation mismatch;
  - the storage backend the MCP resolves does not persist — unset or anything but `pgvector`, because `push` would then report rows written that no store holds while the index worker claims none.
- Running `seed:demo --reset --yes` must satisfy all of:
  - every source's job is succeeded, with `embed_calls ≤ ceiling`;
  - no worker process remains after the run;
  - the count and states of the main store's `index_jobs` are unchanged before and after the seed.
- `/api/status` shows `knowledge_mcp` as reachable.
- Question 6: the answer is a refusal or a statement of insufficient evidence, with no fabricated numbers.
- The contract, FAQ, and weekly-report examples (q7–q9) return their fixture facts and page/section citations.
- The lexical, graph, and global document questions (q10–q12) use the expected source material; q11 cites both documents and q12 summarizes both weekly reports.
- Run q13 and its follow-up in one conversation; the follow-up preserves the annual-leave context and cites its source.
- The SQL boundary question q14 does not fabricate supplier invoice rows from the document corpus. SQL business-data acceptance uses the separate live database scenario.
- The positive SQL question q15 uses the demo-owned synthetic `_test` contract ledger for the inclusive 2026-01-01 through 2026-04-30 window. The real browser answer contains the four expected contract rows, each citation opens its structured row snapshot, and developer mode shows the SQL receipt. The browser runner does not receive the ledger URL.
- Ask the first 5 questions in `expected.json` one by one; each must satisfy all of:
  - the answer contains the key facts of the expected answer;
  - the citation points at the expected source_ref and locator field;
  - the source_ref returned by Evidence can be found in the dev stack.
- Run `--yes` again without `--reset`: every unit is skipped (reused), and embed calls in the indexing stage are 0. The demo catalog rows are refreshed in place, so the corpus holds one row per document — never a second row for a previous fixture version.
- Answer presentation (requires a completed seed, so it is gated on the acceptance run above):
  - Ask three questions against the seeded ZEUS corpus — one from `expected.json` q1 (employee handbook), one from q2 (product description), one from q5 (financial-report summary). Screenshots must show all three of: inline superscripts in the prose, a sources list at the end of the answer, and **no receipt panel** on the page.
  - Ask q6. The answer must state `资料中未找到相关内容` and carry no fabricated numbers.
  - A developer-mode run shows the receipt surface; the default run does not.

## Public mirror

The corpus is entirely self-written and may go into the public mirror; no exclusion list is added.

## Adopted QA-chain completion amendment (2026-10-16)

This adopted delivery requirement is not an implemented-status claim for the amendment. [Plan 01](../plans/qa-chain-01-foundation-seed.md) owns the work sequence and [the program map](../plans/qa-chain-generalization.md) owns the delivery evidence ledger. Retain the isolated-stack guard and all twenty-document acceptance above, with these deltas:

- The unchanged embedding-cap formula is entities + triples + chunks, calculated after source extraction and before merge; only merge calls consume that cap. `ENTITY_MERGE_EMBED_LIMIT` carries `source_ref`, `embed_calls`, `embed_cap`. Test zero/multi-chunk extraction, merge failure, restart/reclaim, concurrent jobs and exhaustion without multiplying the formula. Remove the actual deployed ×50 workaround only after the upstream fix lands, then prove 20/20 succeeded and zero indexing embeddings on repeat seed.
- Compare reset guard patches with main and broaden only a proven necessary demo `_test` target, still port 5434. Do not implement upstream Neo4jGraphReset. This deferral is distinct from ADR 0100's existing fin isolated-seed Neo4j cleanup; it does not remove that requirement. If capability is absent, diagnose and state the exact blocked path instead of claiming a full reset passed.
- A01/A02/A16/A19 in the program map supplement seed delivery evidence. The existing question-module acceptance gate remains independent; successful ZEUS demonstration alone is not proof of that gate.

New seed-generated catalog rows, ingest audit metadata, pending-question/source snapshots and receipt fixtures follow the adopted [catalog](qa-chain-corpus-retrieval.md), [shared wire](question-task-runtime-contract.md) and [receipt](qa-chain-server-runtime.md) representations when the respective implementation tickets land. This is synthetic development/test data generation, not an old-data migration or broader reset permission. No database, catalog or seed command is changed by this documentation task.

**Assumptions:** The isolated targets, fictional fixtures and guarded orchestration are inherited from ADR 0100 and the current task. Only the explicitly scoped QA-chain storage design is adopted; no new reset permission, provider or dependency is assumed. **Open questions:** Runtime provenance of ×50 and actual reset capability remain evidence-gathering tasks in T2/T3. **Persist list:** empty; retain ADR 0100.

## Adopted QA design correction (2026-10-20)

**Status:** adopted design implemented on `fix/qa-design-correction-20261020`; that pull request is open, not merged. The real-model browser acceptance (the 26-check row) and the `eval` / `memory_on` lane are Not run because the external model provider is unavailable — every official MiniMax endpoint answers HTTP 401 and there is no local gateway (see the [implementation report](../docs/verification/2026-10-20-qa-design-correction-implementation.md)). See the [correction plan](../plans/2026-10-20-qa-design-correction.md) and [ADR 0104](../docs/adr/0104-internal-search-mode-automatic-fallback.md). Historical seed success is not evidence for this amendment.

- **Automatic question path:** homepage examples send ordinary questions without `tiers` or mode-labelled text. Internal retrieval choices follow the [host contract](search-mode-host.md). The interface shows no internal retrieval names or picker. Only content ambiguity that changes the answer warrants understandable document/period options under the [clarification contract](search-mode-host.md#clarification-contract). Technical routing source/confidence/EXIT remains auditable through current-turn receipts; raw mode tokens remain in machine diagnostics rather than rendered UI.
- **Streaming entry:** entering the example's thread must preserve the in-flight send and render its live answer immediately. Coordinate `askExample`, `ThreadRoute`, and `ThreadPage` re-sync so a duplicate or late `openThread` read neither covers streaming with an entry loader nor replaces newer streamed state. Preserve cold-link loading and current failure distinctions, and send the question exactly once.
- **Restore ownership:** restore the last thread only on a non-demo cold application start at `/`, after demo status and thread-list readiness resolve. An application-lifetime owner consumes the opportunity once; a HomePage mount-local ref is insufficient and the consumed bit must not persist across browser reload. Demo mode, initial deep links, explicit navigation while startup is pending, and an unresolved/failed demo-status decision cannot trigger later restoration. Explicit later visits to `/`, including browser Back, stay home.
- **Navigation:** the sidebar includes 首页 (`NavLink to="/"`); a demo conversation includes 返回演示首页. Opening an example preserves `/` in browser history. Going home does not reset the corpus or delete the conversation, and a pending send is not duplicated or cancelled merely by returning home.
- **Regression acceptance:** after opening a session, visiting `/` must render and remain on the homepage. Cover demo startup with a saved last-thread marker, delayed status, non-demo startup, initial thread deep link, route remount, reload, sidebar Home, demo return, and browser Back. Exercise an active stream and controlled late reads. Pure state/store tests complement an actual browser route test; source-string checks alone do not establish this behavior.
- **Real-model acceptance:** ask the annual-leave homepage example and representative product/policy questions without selecting or pinning a mode. Verify a direct supported answer, inline citations, and a working versioned source link, with no mode picker or internal mode text throughout the run. Return through both links and browser Back and remain home. A manual mode continuation or pinned request does not pass automatic routing. Keep the original QA closeout as historical evidence and write a new report for the implementation.

No seed reset, data migration, new dependency, model confidence threshold change, or upstream ranking change is required for this amendment.
