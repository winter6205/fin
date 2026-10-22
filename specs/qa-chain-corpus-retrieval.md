# Spec: QA-chain catalog and retrieval

**Status:** Adopted catalog/retrieval contract; public upload identity clarified on 2026-10-21. Product implementation and batch acceptance remain pending.
**Date:** 2026-10-16

## Objective

Make supported corpus documents visible and answerable while preserving the current retrieve port, trusted source authority, financial fact/SQL behavior, and truthful bounded retrieval outcomes.

## Boundaries

- **Does:** Define the catalog metadata-filter contract, publication of documents without company/period metadata, durable publication recovery, retrieval scope/error semantics, and the upstream-to-fin compatibility window.
- **Adoption:** On 2026-10-16 the operator agreed to the four-key metadata filter, publication without company/period metadata, distribution-without-clarification behavior, and the catalog plus ingest-audit persistence design. These choices close this spec's T0 design questions. Fresh architecture review passed; see the final review below.
- **Out of this spec:** New ACL or tenant semantics, a new provider/dependency, changes to finance facts or SQL, changes to the 20-document seed, attachment lifecycle ownership, ranking inside fin, department/time-range filters, or replacing the four canonical question-module contracts. The existing shared question wire contract remains authoritative, and the journey acceptance contract remains their integrated product-acceptance gate.
- **Evidence limit:** This is a frozen design contract. It does not claim that upstream support is deployed, that runtime recovery has passed, or that any implementation test has run.

## Frozen contract

### Public corpus upload identity (clarification 2026-10-21)

Every supported public corpus upload uses one file-only entry; there is no financial or generic upload mode. The file is the only required input. `title`, `company`, `period`, and `doc_type` remain optional metadata subject to their existing validation rules. A new public document ID derives from trusted corpus scope and the SHA-256 of the raw file bytes; the filename and metadata do not participate in identity. Re-submitting the same scope and bytes joins the existing job and preserves its first-accepted metadata.

This upload rule is separate from the catalog's generic metadata-filter contract below. It does not change existing seed or legacy IDs, readers, schema, or recovery of previously accepted pending jobs.

### Retrieve request and metadata filters

Keep the existing retrieve port as fin's sole backend seam. A caller supplies the query, an optional metadata-filter object, the bounded result limit, and any existing typed structured-query/search-mode controls. The adapter owns transport and upstream field mapping. Do not add a second model-facing search tool or restore BM25 to the product path.

The public metadata-filter object has exactly these keys:

| Key | Meaning | Required catalog value |
| --- | --- | --- |
| `title` | Exact document title | Yes; sanitized title or explicit accepted title |
| `doc_type` | Exact document type | Yes; detected type or `unknown` |
| `company` | Exact company label | Optional |
| `period` | Exact period label | Optional |

The object is absent or empty to search the caller's entire currently authorized source scope. An explicitly supplied `filters` value must be a JSON object with no more than four members. Validate it at fin's public boundary before acquiring a lock, starting a process, or making any I/O. The four names above are the complete metadata allowlist; internal controls such as `search_mode`, SQL windows, structured queries, evidence reads, and trusted source scope use their existing typed channels and are never accepted as arbitrary metadata keys.

Normalize each value by Unicode NFC normalization and trimming surrounding Unicode whitespace. A whitespace-only value is omitted; if every value is omitted, the request is equivalent to an empty filter object. Every remaining value must be a string of 1–256 UTF-8 bytes and must contain no Unicode `Cc` control character and no `*` or `?` wildcard character. Values are exact, case-sensitive comparisons after normalization; there is no substring, array, range, or wildcard matching. Missing `company` or `period` does not match a request that supplies that key.

Return the canonical named input failure `{ "error": "invalid_filter" }` without `chunks` and without starting I/O for an unknown/reserved key, a non-object filter, too many keys, a non-string value, an overlong or control-containing value, or a wildcard value. In particular, `cik`, `form`, `ticker`, and `filing_date` are never members of the new metadata allowlist.

### Trusted source scope and retrieval arms

The host resolves source eligibility through the existing Source Scope authority and intersects it with current permissions at execution time. The model cannot widen this scope. One trusted scope can contain eligible permanent-corpus documents and eligible attachments from the current thread; attachment reads remain in the existing host-owned adapter and do not become permanent-corpus ingest. A saved selection or checkpoint never grants access by itself.

`document_id` remains an explicit trusted locator where existing callers require it; it is not a model-visible metadata dimension. Any explicit document restriction must be a subset of the host-resolved scope. Metadata filters and trusted scope compose by intersection.

Every evidence-producing arm must honor the effective source scope before it returns content: lexical and vector retrieval, graph facts, global/community reports, structured SQL/fact reads, and thread-attachment reads. The upstream adapter maps the four public keys to the corresponding `metadata.title`, `metadata.doc_type`, `metadata.company`, and `metadata.period` fields. Filters must be applied before ranking/limiting. Do not post-filter a truncated top-k and describe it as a complete filtered search. Never return an unscoped report, graph result, fact row, or attachment. The generic metadata map is not translated into SQL predicates; if a request combines it with a SQL/fact arm that cannot preserve the requested predicates, return `unsupported_filter_mode` or exclude that optional arm with truthful degradation. The existing SQL/fact query and validation contracts otherwise remain unchanged. An attachment arm must apply the same metadata predicates when it has equivalent metadata; if it cannot, it must fail or be excluded with truthful degradation rather than return unfiltered attachment evidence.

The selected search mode is preserved. An invalid closed-set mode returns `invalid_search_mode`; fin never silently changes the requested mode, drops filters, or falls back to another mode/provider. If the upstream cannot honor a requested filter-plus-mode combination, return `{ "error": "unsupported_filter_mode" }`. If it cannot honor the trusted source scope, return `{ "error": "unsupported_source_scope" }`. An optional arm may be excluded only when the remaining result is still within the requested scope and the response reports truthful degradation. A required or explicitly requested arm that cannot honor scope/filter semantics fails explicitly.

Confirmed document scope is not ambiguity. Usable hits from several documents remain candidates for synthesis; they do not automatically trigger clarification. Existing question routing and clarification contracts decide whether to ask the user.

### Result, error, readiness, and degradation wire

A successful result is `{ "chunks": [...], "distribution": "zero" | "hit" | "dispersed" }`, with the existing chunk/source-ref shape. `distribution` is computed mechanically from distinct document IDs among usable returned chunks after scope, filter, and locator validation:

- `zero`: no usable returned chunks;
- `hit`: usable chunks identify exactly one document;
- `dispersed`: usable chunks identify more than one document.

This value describes where returned evidence came from; it makes no claim about answerability or ambiguity. Preserve all usable candidates and their source references. Only grounded admission decides whether a clean zero result becomes an evidence refusal. An empty result accompanied by degradation is incomplete evidence, not a clean zero-result decision.

Healthy responses omit degradation fields. When an arm is omitted or partially fails but the remaining scoped evidence is usable, include the existing fields `degraded: true` and `degraded_reasons: [<non-empty reason strings>]`. Preserve reasons verbatim through the retrieve port into access-controlled diagnostics; the list contains one to sixteen strings, each at most 256 UTF-8 bytes without control characters. A malformed or oversized degradation envelope is `retrieve_protocol_error`, never an undegraded result. Default status, SSE, tool folds and history expose the degradation flag and safe copy “部分资料暂不可用，当前结果可能不完整”, without raw reason strings or exception details. Thus operational degradation stays visible without leaking backend names, IDs or technical tokens. If all arms fail or the partial result cannot satisfy the request, return `{ "error": "retrieve_degraded", "degraded": true, "degraded_reasons": [...] }` with no `chunks`; the same safe presentation rule applies.

The named failure envelopes have no `chunks` key:

| Condition | Wire result |
| --- | --- |
| Invalid metadata filter | `{ "error": "invalid_filter" }` |
| Unsupported filter and search-mode combination | `{ "error": "unsupported_filter_mode" }` |
| Unsupported trusted scope | `{ "error": "unsupported_source_scope" }` |
| Invalid search-mode token | `{ "error": "invalid_search_mode" }` |
| Process or transport failure, or an otherwise unknown backend failure | `{ "error": "retrieve_unavailable" }` |
| Malformed or incompatible response contract | `{ "error": "retrieve_protocol_error" }` |
| Authorization denial | `{ "error": "authorization_denied" }` |
| All required arms failed with recorded degradation | `retrieve_degraded` envelope above |

`not_ready` has one exact shape and preserves cause and stage:

```json
{"error":"not_ready","cause":"index","stage":"index"}
{"error":"not_ready","cause":"busy","stage":"queue"}
```

`cause` is exactly `index` or `busy`. `cause=index` is permitted only when an explicit backend readiness/job fact proves index absence or indexing work; a generic `lexical_not_ready` label or silent empty is not evidence. Its `stage` is exactly `index`. `cause=busy` means queue saturation or exhaustion of the total retrieval budget; its `stage` is exactly one of `queue`, `startup`, or `response`, identifying where the budget expired. No other cause/stage pair is valid. Transport, protocol, authorization, unknown, and mixed-degradation failures retain their own named meaning and are never recoded as index work or a successful empty result. The existing grounded `corpus_not_ready` outcome remains reserved for a clean empty result after retrieval; it is not a synonym for `not_ready(index)`.

Start one monotonic deadline before waiting for the retrieve I/O lock. Lock wait, subprocess startup/handshake, and response all consume the same remaining budget. A waiter timing out cannot terminate the current lock owner. An owner that times out quarantines its session so a late response cannot satisfy another request. Cleanup cannot extend the user-facing budget.

### Catalog fields and compatibility projection

Keep `catalog.json`, its `(document_id, version)` upsert identity, process-safe lock, and same-directory atomic replacement. New physical rows require non-empty `document_id`, `version`, `title`, and `doc_type`, plus the existing non-negative integer `page_count`; these are catalog projection requirements, not public upload form fields. `company` and `period` are optional: omit a missing value rather than inventing an empty string or using it as identity. Preserve existing `source_sha256`, `extract_method`, and legacy financial fields when present; do not rename or delete them as part of this change. If a new row writes `source_sha256`, it equals `version`.

The generic catalog read model exposes `document_id`, `title`, `doc_type`, `version`, `page_count`, and present `company`/`period` values. The existing financial `list_filings` read projection remains a compatibility projection with its current key set `{document_id, company, period, version, page_count, form}` for existing financial callers and rows. It does not make the four old filter arguments part of the new generic filter map. Preserve physical `cik`, `form`, `ticker`, and `filing_date` values where they already exist so compatibility reads do not lose data.

Grouping candidates remain `doc_type`, `company`, and `period`; `title` is a display/filter value, never a grouping dimension. Show only dimensions with at least two distinct non-empty values, place missing values under “未分类”, and show a flat title list when no dimension qualifies. Department and time-range dimensions remain outside this contract.

### Public file upload and document identity

An upload needs no sidecar metadata file and no financial identity. `title` uses an accepted explicit title when present; otherwise it uses the sanitized upload filename basename. For filename fallback, treat both `/` and `\\` as path separators, keep only the final component, normalize to Unicode NFC, remove Unicode `Cc` controls, replace `*` and `?` with `_`, trim whitespace, and truncate on a UTF-8 boundary to 256 bytes. If nothing remains, use `Untitled document`. Explicit title, `doc_type`, and present `company`/`period` metadata use the same normalized non-empty string, 1–256 UTF-8-byte, and no-control-character bounds as filters; explicit metadata containing `*` or `?` is rejected as `invalid_upload_metadata` before any write. `doc_type` uses an accepted override when present; otherwise use fin's existing deterministic format/document classification, and use the literal `unknown` when no reliable type is detected. Do not add a classifier provider or dependency. Missing `company` and `period` are valid and remain absent.

For every new public permanent-corpus upload, derive identity only from the trusted corpus-scope ID and the SHA-256 digest of the uploaded bytes. The public upload boundary rejects a caller-supplied `document_id` with its typed 4xx and never derives identity from the filename; existing trusted import seams and the 20-document seed retain their current identities under their existing contracts.

```text
digest = SHA256(raw_upload_bytes)                         # 32 bytes
version = lowercase_hex(digest)
document_id = "doc_" + lowercase_hex(
    SHA256(UTF8("fin-corpus-document-v1\0")
           || UTF8(trusted_corpus_scope_id)
           || 0x00
           || digest)
)
```

The scope ID is a stable value from trusted host configuration, never from a filename, metadata field, filter, or model argument. The filename is display metadata only. Same bytes in the same trusted corpus scope reuse the same identity on retry; same bytes in different trusted scopes produce different identities; same filename with different bytes produces different identities. A retry joins the accepted job without replacing its first-accepted metadata and does not create a second catalog row. Display metadata is not part of the identity. Thread-attachment identity and lifecycle stay with the existing host attachment contract and never publish into the permanent catalog.

### Durable catalog publication and recovery

Persist upload/publication facts only in the existing `fin_ingest_audit.metadata` JSONB contract. Do not add `fin_ingest_job` columns, another publication table, or thread-local warning transport. Every new event uses the existing audit row columns; its `action` is the exact event name below and `metadata` is a JSON object with integer `schema_version: 1`. Strings are UTF-8 JSON strings, counts/attempts are non-negative JSON integers (booleans are not integers), absent optional metadata fields are omitted, and no raw exception text, local path, credential, or DSN is persisted.

The event sequence and payloads are:

| `action` | Required `metadata` fields | Meaning |
| --- | --- | --- |
| `upload_metadata_accepted` | `schema_version: 1`; `version`: 64-character lowercase SHA-256 hex; `title`: non-empty string; `doc_type`: non-empty string; optional `company` and `period`: non-empty strings | Durable accepted metadata for this document/version, recorded before the worker runs. |
| `push_succeeded` | `schema_version: 1`; `version`; `content_identities`: object mapping every non-empty `source_ref` string to its non-empty upstream identity string | All required push envelopes completed and fin has the upstream identity mapping. |
| `catalog_publication_pending` | `schema_version: 1`; `version`; `publication_id`: `catalog-v1:<document_id>:<version>`; `attempt`: positive 1-based integer; `catalog_row`: the complete physical row to upsert | Durable redo input, written before each catalog upsert attempt. |
| `catalog_publication_failed` | `schema_version: 1`; `version`; `publication_id`; `attempt`; `failure_code`: `catalog_row_invalid`, `catalog_corrupt`, or `catalog_unavailable`; `failure_stage`: `validate`, `read`, `lock`, or `write` | Typed failed attempt. Does not advance the job to `ready`. |
| `catalog_publication_succeeded` | `schema_version: 1`; `version`; `publication_id`; `attempt` | The idempotent catalog upsert completed. The job may now transition from `cataloging` to `ready`. |

Map `CatalogRowError` to `catalog_row_invalid/validate`, `CatalogCorruptError` to `catalog_corrupt/read`, and catalog `OSError` to `catalog_unavailable` with stage `read`, `lock`, or `write` according to the operation that failed. Unexpected programming errors propagate through the existing typed job failure path; they are not swallowed as catalog warnings.

The order is: persist accepted upload metadata; complete the push and durably record `push_succeeded`; append `catalog_publication_pending` with the final row; perform the idempotent `(document_id, version)` catalog upsert; append success; then transition to `ready`. If the accepted-metadata or pending-publication audit write fails, do not start the worker or catalog upsert because recovery input is not durable. If upsert fails, append the typed failure, keep the job in `cataloging`, expose the warning through job polling and the catalog warning count, and report the document as not ready for this workflow. Polling derives warning state from audit records: a failed attempt with no later success yields `{code: "catalog_publication_failed", failure_code, stage, attempts}`, where `attempts` is the latest 1-based attempt number. A pending attempt with no terminal event yields `{code: "catalog_publication_pending", stage: "recovery", attempts}`. This also covers failure to append either terminal audit event: keep `cataloging`, propagate the typed audit failure, and recover from the already durable pending row instead of claiming `ready` or requiring an unwritten failure event. `catalog_warning` is absent after a recorded success. `catalog_warning_count` is the number of documents with an unresolved pending or failed publication, not the number of attempts. Recovery repeats the idempotent upsert and records success before advancing readiness; a later success clears the warning and removes that document from the count.

Recovery reads the accepted metadata and the latest pending publication row from audit JSON; it never reconstructs `company` or `period` from `document_id`. If `push_succeeded` is durable, recovery retries only the catalog upsert from the recorded `catalog_row`, without pushing again. If the process crashes after the remote push succeeds but before fin records `push_succeeded`, the two systems do not share an atomic transaction: recovery reconciles the existing push journal and the upstream `(source_ref, body_content_id)` idempotency contract. It may resend the same envelope to recover the authoritative identity, but that retry must converge on the existing upstream content. Do not claim exactly-once delivery or a distributed atomic commit. If reconciliation cannot prove the push result, keep the job unready and return a typed failure.

The push-success event, pending publication row, and terminal publication event are all in the existing audit table. A crash after the catalog upsert but before its success event is safe: recovery repeats the idempotent upsert and records success. No warning is read from or passed through thread-local state.

### Compatibility and release gates

Do not invent deployed release numbers. Release records must identify the actual upstream and fin builds observed at each gate. The compatibility order is:

1. **Upstream expand:** release iknow-memory TUF (filter application across evidence arms) and TUR (typed readiness/error cause and stage) first. Keep the old financial filter fields supported while adding the four generic metadata fields. The release is eligible only after tests prove pre-ranking filtering and trusted scope across vector, lexical, graph, report, and structured arms; unsupported combinations fail explicitly; and readiness cause/stage survives MCP unchanged.
2. **fin migrate/expand:** only after the upstream expansion is verified on every target instance, release fin with the four-key generic map and the temporary legacy `cik`, `form`, `ticker`, and `filing_date` arguments. Validate and translate each legacy argument to its existing upstream financial field without dropping it or conflating it with `company`, `period`, or `doc_type`; preserve the current empty-value and conflicting-filter behavior during this window. If old arguments and generic filters are both supplied, apply both as conjunctive predicates. The generic metadata map itself rejects all four old names with `invalid_filter`.
3. **fin contract:** remove the four legacy arguments and translation only after the inventory of supported callers is migrated and legacy financial filter use is retired. Calls using those names in the generic map continue to receive `invalid_filter`; no compatibility branch silently ignores them. Upstream retains the old fields until every supported fin caller has contracted and no supported remote caller uses them; only then may a separate upstream contraction remove old filter support.

Each step is a release/capability gate, not a guessed version string. Upstream expand must precede fin migrate; fin migrate must be observed on supported callers before fin contract. Preserve the financial catalog read projection and existing financial row fields as required by remaining readers. Specialized financial facts and SQL contracts, and the isolated 20-document seed, remain unchanged throughout.

## Success Criteria

These are required observations, not test results. Each must pass at the named interface; failure keeps its implementation ticket open.

- **CR-1:** Interface tests prove omitted/empty filters search the authorized whole scope, whitespace-only values disappear, and unknown/reserved/wrong-type/control-containing/oversized/wildcard entries return `invalid_filter` without I/O. One catalog-owned allowlist supplies every consumer. The old four arguments are removed only at the contraction gate. Maps to A04/A16.
- **CR-2:** Transport/backend tests prove filters and trusted scope constrain vector, lexical, graph, report, structured, and attachment evidence. Unsupported scoped modes fail explicitly; failed optional arms retain truthful degradation. No arm returns unscoped content or calls truncated post-filtering complete. Maps to A04/A16/A20.
- **CR-3:** Controlled one-document/multi-document/zero results retain `hit`/`dispersed`/`zero` independently of semantic sufficiency. Positive indexing facts map to `not_ready(index,index)`; queue/deadline exhaustion maps to `not_ready(busy,<stage>)`; malformed/transport/permission/degraded failures retain their own causal meaning. Maps to A08.
- **CR-4:** With a blocked owner and queued callers, every retrieval ends within the same single user-facing budget including startup/handshake; waiter timeout cannot kill the owner; owner timeout fences late results without blocking the response on cleanup. Maps to A08/A12.
- **CR-5:** A real metadata-free upload is visible and citable; same-content retry reuses identity and same-name/different-content stays distinct. Three process-concurrent uploads preserve three catalog rows. Durable polling exposes publication warnings; recovery does not repeat a recorded successful push. Maps to A09/A10/A14.
- **CR-6:** Catalog projection shows only eligible grouping dimensions, missing values as unclassified, and a title list if none qualify; legacy financial rows, existing graph/global behavior, positive SQL acceptance, and the 20-document seed remain valid. Maps to A09/A16.

## Open Questions

There are no unresolved T0 design choices. Fresh independent architecture review passed for this frozen contract. Runtime evidence remains with the existing TUF/TUR and fin implementation tickets: deployed upstream capability, restart recovery across the push-success/local-audit gap, catalog warning polling, and multi-process publication must be demonstrated there. The planning map retains the owners and evidence rows; this spec does not claim those observations have passed.

## Inherits / Changes

This spec consumes the following definition verbatim from [CONTEXT](../docs/CONTEXT.md):

> **retrieve port**: The only retrieve dependency fin is allowed to have: `query` (+ optional filters or a typed structured request) in, authorized corpus results with source attribution out — MCP-shaped, transport-agnostic. Its adapter also carries host-internal evidence reads and invalidation acknowledgements over the same connection, without another model-facing search tool.

It inherits the [context map](../docs/context-map.md), [Task Runtime](question-task-runtime.md), [Source Scope](question-source-scope.md), [Grounded Answer](question-grounded-answer.md), [Workspace](question-workspace.md), [shared wire contract](question-task-runtime-contract.md), and the four canonical question-module specs. Existing task identity, input history, current permission, and source admission remain authoritative. No receipt, snapshot, route label, or server object becomes a second task or permission authority.

The amended accepted [ADR 0031](../docs/adr/0031-additive-filters-and-xbrl-fact-tool.md) records generic four-key metadata filters and optional financial metadata, superseding the old mandatory identity and unknown-filter behavior. Specialized financial facts, SQL and positive finance acceptance remain.

**Selected persist list:** Amend ADR 0031 with generic filters and optional company/period; reconcile the existing list_files glossary entry without adding a new term.

**Persist result:** Following fresh ACR PASS, domain-modeling accepted the ADR 0031 amendment and reconciled list_files in CONTEXT. Existing retrieve port, status snapshot and source-scope vocabulary are retained.

## Architecture Change Review

Fresh independent review on 2026-10-16 assessed the concrete adopted contracts, including reply admission/ownership, versioned scope, bounded inputs, receipt recovery, publication partial effects and release gates.

- Responsibilities, interfaces, and dependencies: **yes** — existing task/source/grounding authorities remain; app lifespan and execution ownership are explicit.
- Input contracts and test scenarios: **yes** — bounds and invalid, duplicate, stale, concurrent and permission cases are fixed at public seams.
- Failure propagation and partial effects: **yes** — checkpoint ordering, durable redo, causal failures, idempotent recovery and late-result fencing are explicit.
- Structural simplicity and caller burden: **yes** — existing checkpoint, pending question, snapshot, retrieve port and audit table are reused; no new provider or dependency.
- Task scope and migration: **yes** — financial compatibility, upstream-first expansion, supported-caller migration and existing acceptance / G1–G3 gates remain.

**OVERALL: PASS — 0 unresolved applicable concerns.** This is a design verdict, not product implementation or runtime acceptance.

## Plan handoff

[Plan 03](../plans/qa-chain-03-corpus-retrieval.md), after [Plan 01](../plans/qa-chain-01-foundation-seed.md). The [program map](../plans/qa-chain-generalization.md) owns delivery dependencies and the operator's A01–A20 evidence ledger, not a competing module contract.
