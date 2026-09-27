# Validation: corpus T3 — durable ingest worker on the shared parser core

**Ticket:** `plans/permanent-corpus-upload.md` T3 (resume parse / 切分 / push from the durable job), branch `feat/corpus-t3`.
**Code under test:** `edc44bc` (T3 delivery) + the review-repair commit that follows; base `67397e4` (main).
**Spec basis:** `specs/corpus-serve-ingest.md` (amendments 2026-09-25 / 09-26 / 09-27), T3 acceptance bullets.

## What was delivered (one line per acceptance clause)

- Restart resume: `IngestWorker` + `IngestEndpoint.run_durable` claim pending / failed / interrupted jobs by CAS (loser joins or gets typed busy), re-read raw bytes through the durable pointer (sha256 verified), and drive the seven states to `ready` with no re-upload — boot wires `start_ingest_worker()` in `serve()` (startup drain + bounded periodic re-check).
- Shared parser: corpus PDF dispatch consumes the T2d `PageRegionParser` core (`parse_pdf` retired from dispatch); text-layer unit output pinned byte-identical to the retired path.
- Chunking/push: fin-only deterministic table, canonical `{document_id, version, unit}` `source_ref`; authoritative `content_identities` (read back from the push response) persisted on the `cataloging→ready` edge; `ready` only after every batch is pushed or journal-complete.
- Replacement closure: `PushedRecordsStore` (path-backed) supplies the `previous_records` provider, so a superseding upload closes the prior version's stale blocks at the accept door; production also wires path-backed `PushJournal` and `InvalidationLedger`.
- Bounded read: `read()` page_limit clamp fixed to reach the fetch window (single `clamp_page_limit` SSOT); hit→`read_source` same-unit contract pinned hermetically and live.

## Environment recipe (values redacted)

```text
set -a; . /home/winner/projects/fin/.env; set +a     # FIN_CHECKPOINT_URL only (Postgres on localhost:5433)
FIN_OBJECT_STORE_URL=http://127.0.0.1:8333           # SeaweedFS (local dev infra)
FIN_OBJECT_STORE_BUCKET=fin-g4-t2c                   # pre-existing bucket
FIN_OBJECT_STORE_ACCESS_KEY / _SECRET_KEY            # local throwaway test strings, not operator secrets
IKNOW_NODE=<nvm node v24.21.0 path>                  # live MCP legs spawn the real stdio server from ../iknow-memory/dist/index.js
```

Worktree venv needed the declared `object-store` extra (`uv sync --frozen --extra object-store`; `uv.lock` unchanged). iknow infrastructure containers (postgres/es/neo4j) started before the live legs.

## Runs

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| H1 | `uv run pytest -q tests/corpus/test_ingest_worker.py test_ingest_endpoint.py test_ingest_state.py test_pushed_records.py test_pdf_core_dispatch.py tests/host/test_ingest_worker_boot.py test_ingest_service_wiring.py test_workspace_http_ingest.py tests/retrieve/test_source_read_alignment.py test_mcp_adapter.py test_read_port.py` (single command, post-repair tree) | 0 | `304 passed, 1 deselected in 6.65s` — the deselected case is the opt-in live MCP leg |
| H2 | pre-repair per-cluster suites (each repair agent): C1 `100 passed, 2 skipped`; C2 `187 passed`; C3 retrieve-adjacent green; C4 `58 passed` | 0 | adoption-race RED evidence recorded pre-fix (`assert outcome.ran is False` failing on old code); double-adopter test re-run 10x + concurrent-worker test 10x, 0 failures |
| L1 | `uv run pytest -q -rs tests/corpus/test_ingest_worker_durable.py tests/corpus/test_ingest_worker.py tests/corpus/test_ingest_durable_restart.py tests/corpus/test_ingest_state_store_durable.py tests/corpus/test_ingest_storage_selection.py` (DSN + object-store set, post-repair) | 0 | `60 passed, 0 skipped` — real Postgres rows: cross-process dead-worker resume (`test_a_dead_workers_job_is_resumed_and_readied_by_a_fresh_process`), both raw-byte legs (`binary-pdf-35324-bytes`, `real-filing-markdown-57547-bytes` via real SeaweedFS), `list_jobs` state filter on Postgres |
| L2 | `uv run pytest -q -rs -m mcp tests/retrieve/test_live_durable_worker.py tests/retrieve/test_live_content_identity.py tests/retrieve/test_source_read_alignment.py` (live env, post-repair) | 0 | `7 passed, 15 deselected in 14.70s` — durable worker pushed through the real spawned iknow stdio server: bytes durable in S3+PG (no re-upload), `run_job` → ready with `content_identities` byte-equal to `source_documents.content_identity` rows and to the hash oracle, bare `read_source` returned the frozen unit verbatim, second `run_job` joined ready (`ran=False`) |
| L3 | `uv run pytest -q tests/host/test_ingest_worker_boot.py tests/host/test_ingest_service_wiring.py tests/host/test_workspace_http_ingest.py` in the same live (DSN-set) environment | 0 | `42 passed` — boot/wiring legs against the real Postgres store path |
| L4 | full hermetic `tests/retrieve/` (C3 clamp refactor coverage) | 0 | `704 passed, 51 deselected in 563.20s` |

Cleanup after live legs: fin tables (`fin_ingest_job/audit`, `fin_corpus_object_pointer`) at 0 rows for test documents; bucket keys `filings/T3-DUR-*` removed (verified by typed `UnknownObjectKeyError` re-read); per-run iknow schemas `fin_t3dur_*` / `fin_cid_*` and ES `fin_*` indices dropped. iknow dev containers left running (machine-normal).

## Review gate and repair

`code-review` on `67397e4..edc44bc`: Standards `2 High / 3 Medium / 6 Low`, Spec `0 High / 2 Medium / 5 Low` → `GATE: BLOCKED`. Repaired by four root-cause clusters (C1 `run_durable` split + mid-run adoption busy join, C2 PDF-core decomposition + `OcrParsedUnit.from_unit`, C3 clamp single source, C4 worker lifecycle typed-stop tombstone + provider/exclusion hygiene); every behavior fix has RED-before-fix evidence above.

## Deferred findings (Low, with reason)

- Journal-skipped-batch identity recovery re-pushes the skipped envelopes to read the stored identities back; spec Decision 2 words this as "reuse the completing run's map". The authority rule (service's value, never recomputed) is honored and re-push is idempotent; the wording reconciliation belongs to the pending T1-ADR persistence item in `specs/corpus-serve-ingest.md` 待写入.
- Sparse-text-page collapse (`<100` chars + images → typed `ocr_required_unavailable`) and empty-OCR-page drop are T2d-core semantics the spec explicitly delegates; unreachable in the T3 product lane (no OCR port wired yet). Pin the contract when the OCR lane is wired (T4 scope note).
- Unsigned bytes declared `pdf` now end the job as `UnsupportedFormatParseError` (was `MalformedSourceParseError`); both are `ParseError`-family typed `failed` outcomes, staging unchanged. Status-consumer vocabulary mapping is a T4 note.
- `_ingest_components` returns a keyed dict (typo fails at runtime): pre-existing boot style; converting it is drive-by.
- `workspace_http.py` (4171 lines) and `ingest_endpoint.py` (~1500) exceed the 1000-line soft cap: pre-existing, worsened by this diff — recorded as maintainability debt, not repaired here.
