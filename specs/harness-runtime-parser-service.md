# Spec: Harness Runtime parser service and document contract

**Status:** Draft specification; architecture review and the stated freeze prerequisites are separate from implementation acceptance.
**Date:** 2026-10-21

## Objective

Define the S3 contract for P2 and P6: an independently operated parser API and worker own durable
parse jobs and versioned results; fin owns corpus identity, caller association, authorization,
indexing, readiness, and retention. The result is traceable preparation work that can be adopted
after restart without making a parser notification equivalent to publication or readiness.

## Assumptions (grounded in closed decisions)

1. ADR 0103 and the closed document-contract and parser-selection tickets are the accepted ownership
   and adapter decisions; this spec does not reopen them.
2. S3 owns P2 and P6. P2 may establish the parser service and new corpus identity independently; fin
   adoption in P6 depends on P0, P1, and P2 merging, as allocated in the [reliability
   map](../docs/wayfinder/harness-runtime-reliability-map.md).
3. The initial supported formats and adapters are the baseline stated in Success Criteria 5–7
   below and the [reliability map](../docs/wayfinder/harness-runtime-reliability-map.md#review-of-the-five-proposed-adjustments).
   The local `hr-document-contract.md` and `hr-parser-selection.md` tickets record decision provenance.
4. Every new public corpus upload gets a corpus-owned ID derived from trusted corpus scope and the
   SHA-256 of its raw bytes. Filename and optional `title`, `company`, `period`, or `doc_type`
   metadata do not contribute to identity. Existing seed and legacy IDs, aliases, locators, readers,
   optional EDGAR metadata, and the existing schema remain compatible without migration.
5. CSV and legacy DOC/XLS/PPT remain unsupported unless their scope is explicitly amended before S3
   freezes.

## Boundaries

- **Does:** Specify the independent parser API/worker, durable job/result authority, versioned
  result envelope, supported adapter behavior, public corpus identity compatibility, and fin's
  result-adoption/readiness boundary for P2/P6.
- **Confirms with human:** Any P6 expansion to CSV or legacy Office formats requires a pre-freeze
  amendment to the document-contract and parser-selection decisions covering provider or conversion
  choice, license, input security, anchors, OCR, and resource-backed fixtures.
- **Out of this spec:** Runtime API/run ownership and client event delivery (S1/S2); deployment and
  rollout (S6); changing existing readiness, citation, or retrieval rules; dependencies, lockfiles,
  infrastructure provisioning, or production deployment.

## Success Criteria

1. **Parser API integration evidence:** `POST /v1/jobs` acknowledges only after durable job
   acceptance; same-key/same-input replay returns the same job, changed-input key reuse conflicts,
   and queue/deadline limits return typed outcomes rather than accepting unbounded work.
2. **Restart/reconciliation evidence:** PostgreSQL, in a parser-owned schema and restricted role on
   the existing PostgreSQL service, remains authoritative for job status, attempts, cancellation,
   and results. Status/result reads work without Redis; Redis loss, restart, duplicate notification,
   and missed notification are recoverable by durable outbox/inbox and status reconciliation.
3. **Worker-fencing evidence:** Worker attempts use leases and monotonically increasing fencing
   tokens; cancellation is durable, stale attempts cannot commit or publish results, orphaned
   staging output is collectible, and deletion leaves a replay-protection tombstone.
4. **Envelope contract evidence:** Every adapter emits the same versioned Pydantic-validated
   `ParsedDocument` envelope with source SHA-256, declared/detected format, parser/provider build,
   model/config fingerprints, parse-rule version, ordered units, extracted-asset references,
   coverage, and typed diagnostics. The envelope references derived output and never replaces
   original bytes.
5. **Adapter fixture evidence:** Baseline adapters cover PDF (born-digital, scanned, mixed), DOCX,
   PPTX, PNG/JPEG through the selected Docling library; XLSX uses native exact cell/range anchors
   and distinguishes formulas from cached values without evaluating formulas; HTML, Markdown, and
   TXT use native adapters. Synthetic or licensed fixtures prove promised structure and anchors.
6. **Anchor and partial-result evidence:** PDF pages, DOCX structure, XLSX cells, PPTX
   slides/elements, HTML structure, Markdown/TXT lines, and source images are reported only when
   grounded in provider/source data. Missing anchors carry an explicit unavailable reason.
   Embedded-image OCR preserves assets and true source anchors; missing required coverage is
   `partial` or `failed`, never a false complete success. Visual semantics remain a separate
   analysis.
7. **Unsupported-input evidence:** Malformed, encrypted, empty, over-limit,
   MIME/extension-mismatched, and unsupported inputs produce typed outcomes. CSV and legacy
   DOC/XLS/PPT remain rejected until the amendment and evidence gate in Boundaries is satisfied;
   provider format claims alone do not enable them.
8. **Adoption fault-test evidence:** fin first commits a durable completion inbox entry, then
   fetches and validates schema, expected parser job ID/version, source SHA-256/content version, access scope, and expected
   generation outside ownership locks. Both the short result-adoption transaction and final ready
   transaction lock the current authoritative caller row and compare-and-swap generation, source
   hash/version, job association, and non-tombstoned/non-cancelled state. External validation alone
   never authorizes adoption. Indexing runs outside those transactions; ready projection, thread
   sequence, and outbox/resume intent commit together. Mismatches and incomplete readiness work
   cannot publish ready state and must reconcile. Reprocessing the same adopted result is idempotent
   and never downgrades existing ready content; a stale or late result cannot replace newer state.
9. **Identity compatibility evidence:** New public corpus IDs coexist with stable seed and legacy
   IDs, aliases, locators, readers, source hashes, access/retention scopes, and tombstones. The
   public upload requires only a file; title, company, period, and doc_type are optional metadata
   under existing validation rules. Re-submitting the same trusted scope and raw bytes joins the
   existing job without replacing first-accepted metadata. Existing accepted-metadata events remain
   authoritative for new jobs; older pending jobs without that event retain legacy ID decoding only
   for recovery. A legacy multipart `generic` field is ignored and cannot select an identity path.
   An explicit public `document_id` is rejected with the existing typed 4xx; the lower-level
   synchronous ingest may retain its trusted explicit-ID seam. No schema migration is introduced.

## Open Questions

- The [corpus ingest spec](./corpus-serve-ingest.md) now records the 2026-10-21 supersession of its
  older company/period-required upload identity rule. The workspace spec's filing examples describe
  its historical fixture contract; existing readers and identifiers remain compatible.
- The requested CSV and legacy Office extension remains conditional. The human must accept
  the amended format scope and its provider/conversion, licensing, security, anchor, OCR, and
  fixture evidence before the S3 contract freezes.
- Parser fidelity and Lighthouse resource fit remain unmeasured; fixture and capacity evidence are
  gates, not claims made by this contract.

## Inherits / Changes

Quoted definitions from [docs/CONTEXT.md](../docs/CONTEXT.md):

> One versioned asynchronous parse operation whose independent parser service owns status, worker attempts, cancellation, and durable result existence; fin owns the calling attachment or corpus association and decides whether the result is usable for readiness or publication.

> The application-owned authority that accepts durable client operations, orders work per **research thread**, manages run reservations and fenced **worker attempts**, and exposes thread snapshots and subscriptions. It dispatches the host graph but does not own graph checkpoints or **parser job** state.

Inherits [ADR 0103](../docs/adr/0103-durable-runtime-and-parser-ownership.md), [ADR
0104](../docs/adr/0104-internal-search-mode-automatic-fallback.md)'s document-option compatibility,
the accepted parser/document decisions summarized in the [reliability
map](../docs/wayfinder/harness-runtime-reliability-map.md#closed-architecture-and-operating-decisions),
and the current [context map](../docs/context-map.md). The local document-contract and
parser-selection tickets record provenance. The existing workspace and corpus contracts remain compatibility
surfaces pending the amendments above.

Changes: allocates these accepted decisions to S3/P2/P6 and makes format expansion explicitly
conditional. It adds no new domain term or architecture decision. **Persist list:** empty.

## Architecture Change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review.
