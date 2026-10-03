# Spec: structured corpus query

Status: **Review-ready contract, 2026-10-03; implementation pending.** This
specification is not an implementation or runtime acceptance result.

## Objective

Define one bounded, typed query contract for structured representations in
iknow's ingested corpus. The first delivery supports `entity` and
`relation_assertion` subjects, exact listing, one-hop directed relationship
predicates, and exact counts. Free-text `claims`, numeric aggregation, and
business-field catalogs are outside this contract.

The structured value is carried in MCP `filters.structured_query`. fin's
existing retrieve port exposes it internally as optional `structured_request`;
the existing routing cascade must have selected `sql` before execution. iknow returns
`structured_result`, which fin exposes alongside the existing retrieve result
fields. This keeps one model-facing retrieve tool and does not import the
iknow implementation into fin. Query validation and execution remain in iknow;
fin does not become a second corpus store.

The query contract owns result identity, current-result visibility, pagination,
and count semantics. The [evidence contract](structured-corpus-evidence.md)
owns source-version/location identity and retained evidence references. The
[maintenance contract](structured-corpus-maintenance.md) owns source and
conflict-check state. This spec consumes their fields without redefining their
identities or state transitions.

## Boundaries

- **Does:** the structured request/result envelopes; the two result subjects;
  exact filter and one-hop predicates; persistent result IDs; current support
  visibility; deterministic pages; exact counts and bounded count receipts;
  maintenance-summary placement; request/response limits; and typed errors.
- **Confirms with human:** independent architecture review of this contract;
  the real acceptance corpus and independent expected-result oracle are frozen
  before implementation acceptance.
- **Out of this spec:** source-version and location identity; evidence snapshot
  and citation digest details; conflict detection rules and state transitions;
  extraction coverage guarantees; semantic truth or automatic adjudication;
  claims, sums, business fields, and any additional model-facing search tool.

## Assumption gate

1. **Operator-confirmed scope:** the first subjects are entities and relation
   assertions; `claims`, sums, and business-field catalogs are excluded. Source
   versions and supports are immutable records behind a mutable published
   pointer.
2. **Operator-confirmed lifecycle:** current results use the published
   `(source_version, extraction_revision)` pair. Only supports activated for
   that pair are current; omissions from a newer accepted extraction do not
   remain current merely because an older extraction contained them.
3. **Delegated and frozen here:** typed request/result schemas, filter and
   relationship bounds, page/cursor semantics, count behavior, response size,
   and typed failure codes are concrete contract choices for review, not
   runtime implementation claims.
4. **Inherited:** the trusted caller supplies authorization; iknow owns the
   corpus, and fin keeps one retrieve tool and the existing retrieve seam.
   Model-supplied scope or identity never widens access.
5. **Acceptance prerequisite:** the real source corpus, immutable source and
   extraction inputs, backend ID mapping, and independent fact/source oracle
   have not been selected. They must be frozen before runtime acceptance; no
   example below claims to be that corpus or oracle.

## Inherits / Changes

### Exact terms from `docs/CONTEXT.md`

**fin**:

> An enterprise **knowledge-QA** agent (知识问答助手) (the 入口, not the 库): the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **grounded** track. First corpus is operator-held **资料** behind the **retrieve port**.

**retrieve port**:

> The only retrieve dependency fin is allowed to have: `query` (+ optional filters or a typed structured request) in, authorized corpus results with source attribution out — MCP-shaped, transport-agnostic. Its adapter also carries host-internal evidence reads and invalidation acknowledgements over the same connection, without another model-facing search tool.

**iknow-memory**:

> A separate TypeScript MCP server (own repo). It is the product 库: after ingest/push, **资料** are queried through fin's **retrieve port** against this backend (ADR 0045); a distinct **memory port** channel may hold explicit pins and must not share one config key.

### Existing decisions and changes

- ADR 0090 keeps fin as the knowledge-QA entry point and iknow as corpus
  owner. ADR 0094 moves the `sql` purpose to typed search over that same
  ingested corpus, through the existing retrieve boundary.
- ADR 0059 remains in force for the other retrieve tiers and their
  index-artifact behavior. ADR 0094 supersedes only its former separate
  business-database source purpose for `sql`.
- ADRs 0091–0093 continue to govern existing `database-row` evidence and its
  digest. Structured results have their own result/evidence kind and do not
  reuse the `database_rows` envelope or legacy row digest. ADR 0093 requires
  the stable schema token and separate integer `schema_version`; tokens below
  do not carry a version suffix.
- `docs/context-map.md` keeps ranking and the iknow corpus outside fin. This
  contract extends the existing retrieve port/MCP result with a typed
  structured envelope and preserves one model-facing `retrieve` tool.
- Existing iknow storage has UUID entities and triples with head, relation,
  tail, validity, and source links; current triple deduplication does not
  retain all duplicate origins, and a write path can auto-supersede by
  recency. Those observations do not establish the identity/support rules
  below. The implementation must satisfy them before these rows are emitted as
  grounded results.

## Query contract

### Request

The object below is the value carried in MCP `filters.structured_query` and
passed by fin's retrieve adapter as optional `structured_request`. It cannot
override the host-bound search mode: a request on another tier refuses as
`malformed_structured_query`. The existing outer natural-language `query` can
describe the request but never changes typed predicates. The adapter supplies
the bound search mode and structured value; unsupported outer business/date
filters refuse rather than silently alter or narrow the structured result.
Missing backend capability returns `structured_search_unavailable`, never a
fallback to the legacy ledger. The backend derives
authorization scope from the trusted caller; the model cannot send
`scope_binding` or `queried_at`, select another identity, or widen access.
The request token and version are separate:

```json
{
  "schema": "iknow.structured_query",
  "schema_version": 1,
  "subject": "relation_assertion",
  "operation": "list",
  "filters": [
    {"field": "relation_norm", "op": "eq", "value": "works_for"}
  ],
  "relationship": {
    "anchor_entity_id": "00000000-0000-4000-8000-000000000001",
    "direction": "outgoing"
  },
  "limit": 20
}
```

This is an illustrative structured-query value, not a statement about the real
corpus. Optional fields are `filters` (default empty), `relationship` for a
relation assertion, `limit` for list, `cursor` for list continuation, and
`read_revision` to bind a count to a current list revision. List `limit`
defaults to 20 and must be an integer from 1 through 20. Count rejects `limit`
and `cursor`.

The closed subject set is `entity | relation_assertion`; operations are
`list | count`. Predicates are one conjunction of exact tests. The allowlist
is:

| Subject | Filter fields | Value type |
|---|---|---|
| `entity` | `id`, `canonical_name`, `type` | UUID; exact string; member of the backend's closed `EntityType` catalog |
| `relation_assertion` | `id`, `head_id`, `relation_norm`, `tail_id`, `source_ref` | UUID for IDs; exact string for the other fields |

The entity type set is the closed iknow `EntityType` catalog:
`person | organization | place | event` (`src/core/extraction/entity-type.ts`
and the entity table check in `src/core/storage/schema-bootstrap.ts`).
Acceptance pins the catalog revision.
Only `eq` and `in` operators are accepted. `in` has 1–20 unique values of
the field's declared type. A maximum of 16 distinct field filters is allowed.
Duplicate field entries, implicit coercion, fuzzy matching, alias expansion,
and unknown top-level fields are rejected. String equality uses the stored
canonical value. A relation predicate is optional, allowed only for
`relation_assertion`, and consists of one anchor UUID plus
`direction: outgoing | incoming`. Outgoing binds the anchor to `head_id`;
incoming binds it to `tail_id`. It is one hop only. The relationship
predicate and field filters combine with AND.

There is no filter on entity metadata, confidence, raw relation text,
`valid_window`, source version, or location. `source_ref` is a logical-source
predicate over current authorized support, not an authorization selector.
The interface accepts no SQL, nested boolean tree, arbitrary join, free-text
claim, or multi-hop path.

The complete UTF-8 JSON request is at most 32 KiB. A list has a default page
size of 20 and a hard maximum of 20. UUID filters use canonical UUID strings.
All count, limit, and maintenance counter values are finite safe integers;
booleans, fractions, negative values, and unsafe integers are not accepted.
Schema version is checked on the decoded value per ADR 0093: finite whole
integer 1 is accepted; JSON `1.0` decodes as the same value; booleans and
non-finite or non-integer values are malformed. A recognized token with a
different integer version, or an unknown token, is unsupported.

### Result identity and current visibility

An entity result has its persistent UUID. It retains that ID across extraction
and index rebuilds; query-time similarity or alias matching never merges
entities.

`canonical_name` and `type` are deterministic attributed display values, not
identity or truth decisions. Among the entity's current authorized accepted
supports, choose the smallest distinct `(name, type)` pair by UTF-8 byte order;
within that pair choose the smallest support UUID. Return its ID as
`display_support_id` and set `display_disagreement` iff more than one distinct
pair exists. The selected support must be readable through the row's evidence
reference. Exact name/type filters match this chosen display pair, not every
alias. Relation endpoint objects use the same rule and include both display
fields; the relation evidence grant also binds their selected support dependencies.
Neither arrival order, source recency, nor a hidden support can select the value.

A relationship assertion's persistent UUID identifies the exact normalized
tuple:

```text
(canonical head entity UUID, relation_norm, canonical tail entity UUID, valid_window)
```

`valid_window` is a typed half-open interval `[from, until)`, with each endpoint
encoded as `"YYYY-MM-DDTHH:mm:ssZ"` or `null`; endpoints may be unbounded.
Accepted extraction normalizes endpoints to strict whole-second UTC. Nonzero
fractional seconds are rejected before acceptance rather than rounded to invent
equivalence. Offset conversion is allowed only when lossless; fin never coerces
returned timestamps. When both
endpoints are present, `from < until`; an empty or reversed interval is not
accepted as a query-visible assertion. Rebuilds preserve the UUID for the same
normalized tuple. Exact tuple identity groups its source supports under one
assertion ID, so additional sources or locations do not create extra result
rows or increase the count. Similarity, co-occurrence, confidence, or source
frequency does not merge identities or establish truth. A versioned identity
correction that changes the tuple creates the corrected identity and advances
`read_revision`; it does not assert that the historical interpretation was
false.

Only rows with at least one active, non-withdrawn support from the currently
published `(source_version, extraction_revision)` pair are in the current
query result. If replacement removes an assertion's only current support, that
row leaves current results; its historical evidence is preserved and its
disappearance is not a claim that it was false. A current support from another
source keeps the assertion visible. The same rule applies to entity name/type
support. An unresolved conflict does not suppress either assertion. Only an
explicit versioned maintenance decision may change that visibility; there is
no newest-wins rule.

`valid_window` describes business validity, not source publication or current
support. V1 applies no implicit wall-clock filter: a currently supported
assertion is returned and counted even when its validity interval is in the
past or future. Rows expose the interval so the caller can state its meaning.

### List, revision, cursor, and count

List returns each matching subject UUID once in ascending UUID byte order.
Relation rows are distinct by persistent assertion UUID, not by support row.
The page may contain fewer rows than requested to fit the response envelope,
but it cannot skip a row. If a mandatory row cannot fit, the server returns
`result_too_large`, not an empty page. `next_cursor` is null only when no
further matching result exists in that read revision.

`read_revision` is an opaque generation for query data and authorized support
visibility; it is not a source version. It changes when result membership,
returned display/value fields, published extraction, or support visibility
changes. Each request reads query data, count, and the
maintenance metadata it returns from one consistent transaction. Maintenance
progress has a separate `maintenance_revision` and does not invalidate list
pagination. A published row remains queryable while its desired index state is
pending, with that state visible in the maintenance summary.

A cursor binds normalized query, subject, access scope, and `read_revision`.
It expires 10 minutes after the first page; continuation does not renew the
expiry. It is accepted only while that read revision, access scope, and query
remain current. A data or authorization-support revision change refuses the
continuation; the caller must restart at the new revision. A server reader
lease lasts at most 60 seconds per request. Permissions are checked on every
call; an old cursor cannot restore revoked access. No cross-request historical
MVCC snapshot or full membership replay is promised.

Count accepts the same subject and predicates, with no `limit` or `cursor`.
It computes the exact number of distinct current authorized stored-subject IDs
in one consistent read revision, including zero. It never counts page rows,
top-k results, source supports, or inline provenance. A count request may carry
the current `read_revision` returned by list so the two operations use the
same data revision; a stale revision returns `read_revision_mismatch`. The
count says how many matching stored subjects were published in scope. It does
not claim extraction completeness or count every statement in source files.

Every successful envelope uses `schema: "iknow.structured_result"`,
`schema_version: 1`, and includes `subject`, `operation`, `query_digest`,
`read_revision`, `scope_binding`, and `queried_at`. The backend computes
`query_digest` from the canonical validated subject and predicates; it excludes
operation, page size, cursor, revision, and scope so list/count share it.
`scope_binding` is backend-produced from the trusted caller. `queried_at` is
produced as strict whole-second UTC; fin does not round it. The request cannot
supply or override these response fields.

The list envelope has `rows`, `next_cursor`, and `maintenance` in addition to
the common response fields. An entity row has `id`, `canonical_name`, `type`,
`display_support_id`, `display_disagreement`, and evidence fields. A
relation-assertion row has `id`, `head`, `relation_norm`,
`tail`, `valid_window`, and evidence fields. Endpoint objects contain `id`,
`canonical_name`, `type`, `display_support_id`, and `display_disagreement`.

Every row has a mandatory immutable `evidence_ref`, its matching
`evidence_expires_at`, `provenance` with at most 8 support records,
`provenance_complete`, and nullable `provenance_next_ref`. These fields are
mandatory for both subjects. The inline provenance is a stable ordered prefix
of the exact authorized current support set for that row. `false` completeness
requires a continuation reference; null is allowed only when complete.
`evidence_ref` retains the complete set at `queried_at`; continuation reads it
through the existing retrieve/read evidence seam and do not re-query current
supports. The evidence contract owns support identity, reference format,
continuation behavior, and expiry. Each reference expires 365 days after
`queried_at`, is nonrenewable, and is withheld earlier if current authorization
is revoked or its source is purged.

Each successful response carries one complete same-scope `maintenance` object
defined by the [maintenance contract](structured-corpus-maintenance.md). The
object is not redefined here. Its `maintenance_revision` changes for
maintenance progress without changing `read_revision` or invalidating a
cursor. A response reads query data and its maintenance summary from one
consistent authorized snapshot. List continuation may return newer maintenance
progress while preserving the same query `read_revision`.

A count envelope additionally contains top-level `count` and `count_receipt`.
The receipt has `subject`, `count`, `query_digest`, `scope_binding`,
`read_revision`, `queried_at`, `coverage`, `maintenance_revision`, the same
`maintenance` object, `evidence_ref`, and `evidence_expires_at`. Its `subject`
and `count` equal the enclosing response; query context fields equal the
enclosing response; `maintenance_revision` equals
`maintenance.maintenance_revision`. Its evidence expiry is 365 days after
`queried_at`. Receipt `coverage` is `stored_structured_subjects`, matching the
evidence snapshot coverage field. In prose, the count is the exact complete
matching current authorized subject set in the same consistent read snapshot;
coverage makes no extraction-completeness claim. The backend retains the
bounded receipt, not all matching IDs, and promises no historical membership
re-query. fin stores the bounded snapshot through the evidence contract and
does not independently recompute the count.

The complete UTF-8 encoding of each structured-query request and each
`structured_result` or `structured_error` response envelope, including rows,
provenance, maintenance, receipt, and cursor, is capped independently at 32
KiB. Provenance continues through `provenance_next_ref` and is never
silently truncated. The server may reduce a list page at row boundaries to
fit. If required envelope fields or the first row cannot fit, it returns
`result_too_large`.

### Typed errors

An MCP failure uses one structured shape and never becomes prose-only failure
or an empty successful result:

```json
{
  "schema": "iknow.structured_error",
  "schema_version": 1,
  "error": {"code": "invalid_value", "retryable": false}
}
```

The closed query error set is:

| Code | Retryable | Meaning |
|---|---:|---|
| `malformed_structured_query` | no | Missing/wrongly typed value, duplicate field, or illegal field combination |
| `unsupported_structured_schema` | no | Unknown schema token or unsupported integer version |
| `unsupported_subject` | no | Subject outside the closed set |
| `unsupported_operation` | no | Operation outside `list | count` |
| `unsupported_field` | no | Field outside the subject allowlist |
| `unsupported_operator` | no | Operator outside `eq | in` |
| `invalid_value` | no | Value violates the field type or bound |
| `request_too_large` | no | Request exceeds 32 KiB |
| `authorization_denied` | no | Trusted caller is not authorized for the bound scope |
| `invalid_cursor` | no | Cursor is malformed or cannot be authenticated |
| `cursor_expired` | no | Cursor is older than its fixed 10-minute lifetime |
| `cursor_revision_mismatch` | no | Query, scope, or current data revision differs from the cursor |
| `read_revision_mismatch` | no | Count requested a stale `read_revision` |
| `result_too_large` | no | Required response data cannot fit within 32 KiB |
| `count_unavailable` | yes | Complete count could not finish; no partial count is returned |
| `query_timeout` | yes | Read exceeded its 60-second execution lease |
| `structured_search_unavailable` | yes | Structured query backend failed |
| `maintenance_status_unavailable` | yes | Required same-scope status could not be read consistently |

No-match is a successful empty list or a zero count receipt. Every incomplete,
unauthorized, malformed, expired, or failed operation returns a typed error
without partial count or success-shaped empty fallback. fin maps these codes
to its existing typed retrieve failure handling.

## Success Criteria

These checks are future implementation acceptance criteria, not results of
this draft. Each requires an independently authored expected result.

| ID | Binary check | Required evidence |
|---|---|---|
| Q1 | Only schema v1, the two subjects, the two operations, exact allowlisted fields, and `eq | in` validate; malformed and unsupported values return their typed codes | Protocol conformance cases, including version `1`, decoded `1.0`, booleans, and unknown fields |
| Q2 | Entity rows have one persistent UUID each and relation rows have one persistent UUID per exact normalized assertion tuple | Frozen fixture with a rebuild and repeated identical support from multiple sources |
| Q3 | Source multiplicity does not increase relation result rows/count; a second current source keeps its assertion visible when the first is replaced | Before/after source-version and support records plus independent expected IDs |
| Q4 | A replaced sole support removes its subject from current results without deleting historical evidence or implying falsity | Original and replacement source versions with retained evidence reads |
| Q5 | Past/future `valid_window` values remain visible and countable without an implicit wall-clock filter | Assertions outside current wall-clock validity and exact result/count oracle |
| Q6 | Directed one-hop filters return the exact incoming/outgoing assertion IDs and do not traverse two hops | Both directions plus a two-hop-only graph path |
| Q7 | Pages are ascending by ID, have no duplicates/omissions, and stale or expired cursors refuse without mixing revisions | More than 20 results; update and permission-revocation interleavings |
| Q8 | Count equals the independently computed complete current authorized stored-subject set, including zero and more than one page | Independent ID oracle, same revision/scope, safe-integer receipt with `stored_structured_subjects` coverage |
| Q9 | Maintenance progress changes its own revision but does not invalidate a cursor; its counts remain same-scope and complete | Pending/running/ready/failed/stale fixture and source-scope oracle |
| Q10 | UTF-8 envelopes obey 32 KiB; incomplete provenance continues; irreducibly oversized result and unavailable count return typed errors | Boundary payloads, continuation reads, and injected query/count failures |
| Q11 | Claims, sums, free-text SQL, fuzzy matching, arbitrary joins, and multi-hop paths are unavailable | Negative conformance cases with exact error outcomes |
| Q12 | Permuting source arrival order preserves the attributed display; a different authorized support set produces only its own deterministic values and exact-filter matches | Conflicting name/type pairs, two access scopes, selected support lookup, and relation endpoints |
| Q13 | A structured request cannot override host routing or execute a legacy SQL fallback; natural-language text does not alter its exact predicates | Wrong bound tier, unsupported outer filter, backend without capability, and changed descriptive query text |

The real acceptance corpus is not selected. Before implementation acceptance,
freeze actual source hashes, source versions and extraction revisions, multiple-
source support, one-source replacement, conflict examples, a zero-result
query, and a result set above the page cap. Freeze the mapping from original
inputs to backend-assigned persistent IDs in a separate fixture manifest;
these UUIDs cannot be computed from source text. An independent oracle must
derive fact/source correspondence and expected support locators from original
source material, without using the backend query as its expected-result
generator. Synthetic ledger fixtures and local XBRL sidecars are not evidence
of iknow structured-corpus coverage.

## Open Questions

1. Select and freeze the real corpus, backend ID mapping, and independent
   oracle before implementation acceptance. This is an acceptance prerequisite;
   no query-shape architecture fork remains in this draft.

## Pending persistence and handoff

Persistence is flushed through `domain-modeling`: the shared source/support and
structured-result evidence terms are in [CONTEXT](../docs/CONTEXT.md), and the
source lifecycle decision is [ADR 0095](../docs/adr/0095-immutable-source-history-and-finite-retention.md).
Query field definitions, exact equivalence, revisions, and count semantics stay
owned by this contract; no duplicate operational glossary or query ADR is needed.

Hand these three paths to a later `writing-plans` session. Runtime implementation
and acceptance remain pending; the real corpus/ID mapping/oracle must be frozen,
and existing the tracked change obligations remain separate. This session writes no
implementation plan or runtime code.

## Architecture review

Two read-only subagent reviews and a second author scenario pass covered source
replacement, same-source re-extraction, multiple origins, failed publication,
withdrawal, access changes, pagination, receipt-only count retention, and purge
with an offline snapshot owner. Findings were incorporated into the contracts.
This shared verdict applies to all three specifications at the contract stage:

```text
bounded-context-guardian: yes — iknow owns corpus/query/maintenance; fin owns admission and finite answer evidence behind the retrieve seam.
input-contract-tests: yes — binary future cases cover empty, invalid, overflow, concurrent, and exception inputs; real-data acceptance remains a prerequisite.
error-handling-enforcer: yes — failures are typed and never become success-shaped empty fallback.
complexity-anti-drift: yes — query, evidence, and maintenance have separate ownership and reuse existing job/outbox seams.
minimal-change-verifier: yes — the deliverable is three contracts plus shared terminology/lifecycle persistence; no runtime or unrelated feature change.
```

The verdict is a design review, not evidence that implementation tests ran or
that a production corpus passed acceptance. The evidence and maintenance specs
reference this block rather than maintaining independent copies of it.
