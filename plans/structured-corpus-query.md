# Plan: Structured corpus query

**Goal:** Add bounded entity and relation-assertion search over iknow's ingested corpus through fin's existing retrieve boundary, with stable identity, exact list/count semantics, finite evidence, and no legacy-ledger fallback.
**Approach:** Freeze the shared real-corpus oracle and compatibility decision once for all three plans. Export persistent result identity before evidence support persistence; then consume the Evidence and Maintenance milestones for current list, cursor, count, and fin routing. fin remains the host and adapter, while iknow owns query validation and execution.
**Spec link:** [specs/structured-corpus-query.md](../specs/structured-corpus-query.md)
**Baselines:** fin `cdf1f771409ea90ea600e5eb794b0455ed29a4ba`; iknow-memory `eea1e07124e8baf68893bc36fda692ac1dc9d684`. Reconcile each checkout to its implementation start revision before work.
**Per-ticket loop (implementation tickets):** failing-first test → focused typecheck and tests → `verification-before-completion`. Define each new acceptance driver and its invocation before its implementation; existing commands below are regression checks only.

## Implementation architecture review

**Observed seams (read-only LSP exploration on the baselines above):** fin's `RetrievePort.retrieve` (`src/fin/retrieve/port.py:325`), model-facing retrieve tool (`src/fin/host/graph.py:1092`), and MCP request/response handling (`src/fin/retrieve/mcp_adapter.py:945`) currently have no structured request/result carrier. iknow's `src/tools/retrieve.ts:105` currently accepts string-valued filters, while `src/core/retrieval/search-mode.ts:162` routes `sql` to the separate legacy business ledger. Its `src/core/extraction/dedup.ts:158` uses overlapping-interval deduplication and preserves only one origin; the mutable source upsert at `src/core/storage/pgvector-backend.ts:1198` cannot establish immutable source history. Existing IDs, jobs, and storage are reuse candidates; these observations are not implementation or deployed acceptance evidence.

The planned runtime blast radius is the existing iknow query, identity, and storage surfaces (`core/types.ts`, `storage/backend.ts`, `storage/schema-bootstrap.ts`, `storage/pgvector-backend.ts`, `storage/memory-backend.ts`, `tools/retrieve.ts`, and `index.ts`), plus fin's host/retrieve seam (`src/fin/host/graph.py`, `src/fin/retrieve/port.py`, and `src/fin/retrieve/mcp_adapter.py`). This is a plan-level scope; implementers choose any new internal file names. Evidence snapshot validation and citation ownership remain with the Evidence plan.

```text
bounded-context-guardian: yes — iknow owns corpus identity, validation, and query execution; fin admits the host-bound request and carries the result only through the retrieve port/MCP boundary.
input-contract-tests: yes — QP4/QP6/QP7/QP8 cover empty, malformed, overflow, concurrent revision/access changes, and backend failures; new drivers are defined before implementation.
error-handling-enforcer: yes — the closed structured error codes are preserved through fin's typed retrieve failures; partial counts, empty-success fallbacks, and legacy fallback are refused.
complexity-anti-drift: yes — identity, validation, current-list, cursor, count, and fin transport remain separate slices; shared lifecycle behavior is consumed through the declared Evidence/Maintenance milestones.
minimal-change-verifier: yes — one bounded structured-query feature with only its identity and public retrieve wiring; the compatibility decision is a rollout prerequisite, not permission to implement a migration here.
```

The contract-stage verdict in the SPEC reviewed design, not runtime implementation. This implementation ACR uses the read-only fin and iknow seam reports. Shared TypeScript storage/type edits are serialized across the three plans; fin and iknow continue to communicate over MCP with no compile-time cross-repository import.

## Exported milestones and imports

Sibling plans own the external milestones below. The integrated dependency table resolves their exact ticket IDs.

| Milestone | Owner | Query plan exports or consumes | Boundary and ordering |
|---|---|---|---|
| Shared acceptance pack | Query (QP1, QP10, QP11) | Exports frozen original inputs/oracle, separately reviewed backend ID mapping, and measured resource record | QP1 freezes inputs; QP10 follows first accepted ingest; QP11 measures the executable path. All three gate final acceptance; early foundations consume only QP1. |
| QP2 — shared compatibility decision | Query | Exports the opt-in compatibility, migration/re-ingest, and rollback decision to Evidence and Maintenance rollout planning | Decision only. Any migration implementation is separate work; absent historical origins stay unavailable. |
| Q-ID — persistent result identity | Query (QP3) | Exports stable entity and exact assertion-tuple IDs for Evidence support persistence | Implemented independently of Evidence support persistence, so Evidence does not block the identity milestone and no dependency cycle is formed. |
| E-SOURCE — immutable source/support persistence | Evidence ([EP1](structured-corpus-evidence.md#ep1)) | Query consumes retained source versions, extraction revisions, and supports | Requires Q-ID before supports reference result IDs. |
| M-PUBLISH — atomic current accepted-support publication | Maintenance ([MP6](structured-corpus-maintenance.md#mp6)) | Query consumes the published `(source_version, extraction_revision)` current view | Query-visible membership requires this publication boundary. |
| M-STATUS — authorized consistent maintenance summary | Maintenance ([MP10](structured-corpus-maintenance.md#mp10)) | Query includes the summary and its separate `maintenance_revision` | Every successful list/count read requires the same-scope summary. Progress alone does not change `read_revision`. |
| E-GRANT — finite row/count evidence grant | Evidence ([EP2](structured-corpus-evidence.md#ep2)) | Query consumes row grants and count receipts | Required before Query returns evidence references or successful count receipts. Private reads/supports are [EP3](structured-corpus-evidence.md#ep3)/[EP4](structured-corpus-evidence.md#ep4). |
| E-FIN — fin current-turn evidence admission, citation, replay, and invalidation | Evidence (EP5–EP11) | Query consumes this only for final integrated acceptance and rollout readiness | It follows wire routing; it is not a prerequisite for backend count receipts or typed host/port/MCP transport. It uses the existing retrieve connection and adds no model-facing search tool. |

**Compilation coupling:** iknow is one TypeScript package with a whole-package build/typecheck. Sequence shared `core/types.ts`, storage interface/schema, PostgreSQL/memory backend, retrieve-tool, and index wiring changes; do not run the three plans as parallel writers to those surfaces. Q-ID precedes E-SOURCE; E-SOURCE precedes publication; Query reads only after M-PUBLISH and M-STATUS are available. fin's host is the composition root and retrieve is the dependency boundary, so host/port/adapter wiring lands as one fin slice after the MCP envelope is fixed.

## Tasks (ordered by dependency)

1. <a id="qp1"></a> **QP1 — Freeze shared original inputs and the independent oracle** — [decision]
    - **ID:** QP1
    - **Inherits:** `specs/structured-corpus-query.md`: “The real acceptance corpus is not selected. Before implementation acceptance, freeze actual source hashes, source versions and extraction revisions, multiple-source support, one-source replacement, conflict examples, a zero-result query, and a result set above the page cap.”
    - **Surface:** Shared real-corpus inputs, independently authored oracle, and acceptance record used by Query, Evidence, and Maintenance.
    - **Acceptance:** Freeze actual public originals and hashes, source units, parser/extraction inputs and revisions, and independently checked expected facts, locators, and lifecycle outcomes, including multiple origins, replacement, conflicts, zero results, and more than one page. Record the operator/oracle approval. Expected facts and locators are authored from originals, without using backend query output. Backend-assigned IDs and measured resources are separately owned by QP10 and QP11 after those capabilities exist.
    - **Completion:** A reviewer can reproduce every expected fact, locator, and lifecycle outcome from the approved original-input record without needing newly generated backend IDs.
    - **Dependencies:** None. This is the input/oracle gate before behavior tests; it does not require implementation, ingest, or the later ID/resource records.
    - **Status:** [ ] pending — corpus and independent oracle are not frozen or approved.

2. <a id="qp2"></a> **QP2 — Decide opt-in compatibility, migration, re-ingest, and rollback** — [decision]
    - **ID:** QP2
    - **Inherits:** `docs/adr/0095-immutable-source-history-and-finite-retention.md`: “This decision refines ADR 0094 and leaves ADRs 0091–0093 and existing runtime behavior unchanged until a separately verified implementation.”
    - **Surface:** Shared Query/Evidence/Maintenance production compatibility and rollout boundary, including existing fin database-row evidence.
    - **Acceptance:** Record an explicit opt-in path for compatibility or re-ingest, the preserved legacy reads/writes, migration prerequisites, and rollback behavior. Keep ADRs 0091–0093 unchanged; do not guess missing historical origins or reconstruct them from replacement content. Keep the existing #330 obligations independent: structured-corpus acceptance does not close or waive them. This ticket decides the path and does not implement a migration.
    - **Completion:** Operators can tell which data is compatible, which needs explicit re-ingest, how rollback behaves, and which historical references must remain unavailable.
    - **Dependencies:** None. The decision gates production rollout; it does not gate isolated implementation work.
    - **Status:** [ ] pending — no migration or compatibility decision is claimed.

3. <a id="qp3"></a> **QP3 — Register persistent structured-result identities** — [implementation]
    - **ID:** QP3
    - **Inherits:** `specs/structured-corpus-query.md`: “An entity result has its persistent UUID.” A relation assertion's persistent UUID identifies the exact normalized tuple `(canonical head entity UUID, relation_norm, canonical tail entity UUID, valid_window)`.
    - **Surface:** iknow entity/relation identity storage and its existing storage capability. Keep the entity type catalog closed and use the existing UUID identity boundary.
    - **Acceptance:** Entity IDs survive extraction/index rebuilds; one exact normalized relation tuple retains one ID across repeated support and rebuild; changed tuple identity is recorded as a corrected identity, not silently merged by similarity or recency. Backend-assigned IDs are persisted and separately capturable; identity registration has no prerequisite on E-SOURCE support persistence. Normalize validity to strict whole-second UTC, allowing only lossless offset conversion; reject nonzero fractional seconds rather than rounding. With both endpoints present require `from < until`, refusing empty/reversed intervals before acceptance; preserve null unbounded endpoints.
    - **Completion:** Q-ID can be independently consumed by E-SOURCE, and identity/rebuild behavior is demonstrable before any support-persistence implementation is present.
    - **Dependencies:** QP1 before behavior tests; no Evidence persistence dependency. QP10 freezes the resulting ID mapping later.
    - **Status:** [ ] pending.

4. <a id="qp4"></a> **QP4 — Validate the bounded structured request and typed refusals** — [implementation]
    - **ID:** QP4
    - **Inherits:** `specs/structured-corpus-query.md`: “The closed subject set is `entity | relation_assertion`; operations are `list | count`.” Only the listed exact fields and `eq | in` are accepted, with schema token and `schema_version` checked separately.
    - **Surface:** iknow's existing retrieve MCP input and query validation boundary; fin's existing bound search-mode admission remains authoritative.
    - **Acceptance:** Validate the closed subjects, operations, fields, operators, value types, limits, duplicate rules, schema version, and 32 KiB request bound, including missing/empty requests and negative, fractional, boolean, and unsafe integer boundaries. Count rejects list-only fields. Use the typed-error ownership map below for exact error/trigger cases; preserve safe-integer behavior and reject unsupported SQL, claims, sums, fuzzy matching, arbitrary joins, and multi-hop requests. No input may select its own identity or authorization scope.
    - **Completion:** A protocol conformance driver can distinguish each refusal from no-match and cannot obtain an empty-success fallback. Define that new driver before implementation; do not invent its path or command in this plan.
    - **Dependencies:** QP3 identity types are stable first; new driver cases use QP1 when they assert real-corpus behavior.
    - **Status:** [ ] pending.

5. <a id="qp5"></a> **QP5 — Return the first current list page with attributed values** — [implementation]
    - **ID:** QP5
    - **Inherits:** `specs/structured-corpus-query.md`: “Only rows with at least one active, non-withdrawn support from the currently published `(source_version, extraction_revision)` pair are in the current query result.”
    - **Surface:** iknow structured query read path consuming E-SOURCE, M-PUBLISH, M-STATUS, and E-GRANT.
    - **Acceptance:** A first list page returns each authorized current entity or exact assertion ID once, applies exact AND filters and the permitted one-hop direction, and has no implicit wall-clock validity filter. Entity and relation-endpoint displays use the specified UTF-8-byte deterministic current-support selection; exact name/type filters use that selected display. Each row carries its immutable evidence reference, fixed nonrenewable expiry, and stable provenance prefix of at most eight supports with truthful continuation state; the selected display support is readable through the grant. Include a same-scope maintenance summary without allowing pending indexing to hide a published row; unavailable/inconsistent required status returns `maintenance_status_unavailable`. Membership, returned display/value, published extraction, or support-visibility changes advance `read_revision` through Maintenance's canonical transaction. Replacing one of multiple current supports preserves visibility; replacing the only support removes it while historical evidence remains governed by Evidence.
    - **Completion:** An authorized caller can demonstrate a complete first page from the atomically published current view with deterministic display and readable row evidence.
    - **Dependencies:** QP3, QP4, EP1 (E-SOURCE), MP6 (M-PUBLISH), MP10 (M-STATUS), EP2 (E-GRANT), and EP4's private support continuation. Wait for shared TypeScript storage/type edits to serialize across plans.
    - **Status:** [ ] pending.

6. <a id="qp6"></a> **QP6 — Page deterministically across one read revision** — [implementation]
    - **ID:** QP6
    - **Inherits:** `specs/structured-corpus-query.md`: “List returns each matching subject UUID once in ascending UUID byte order.” A cursor binds normalized query, subject, access scope, and `read_revision`.
    - **Surface:** iknow list continuation and consistent query-read boundary.
    - **Acceptance:** Pages contain no duplicates or omissions, in ascending ID order, including result sets above 20. Malformed/unauthenticated cursors return `invalid_cursor`, the fixed ten-minute deadline returns `cursor_expired`, and query/scope/data-revision changes return `cursor_revision_mismatch`; denied callers receive `authorization_denied`. Test each revision advance from QP5, including display-only and published-extraction changes. Exceeding the 60-second reader execution lease cancels with `query_timeout`. Maintenance-only progress changes its own revision and may update the summary without invalidating the cursor. Fit pages at row boundaries under 32 KiB; return `result_too_large` if a required row cannot fit.
    - **Completion:** A caller can consume a multi-page result at one data revision or receive the specified typed restart/refusal outcome under each change interleaving.
    - **Dependencies:** QP5, M-PUBLISH, and M-STATUS.
    - **Status:** [ ] pending.

7. <a id="qp7"></a> **QP7 — Return exact same-snapshot counts and bounded receipts** — [implementation]
    - **ID:** QP7
    - **Inherits:** `specs/structured-corpus-query.md`: “It computes the exact number of distinct current authorized stored-subject IDs in one consistent read revision, including zero.”
    - **Surface:** iknow count execution and E-GRANT receipt integration.
    - **Acceptance:** Count applies the list predicates to the full distinct subject set, never page rows/support rows/top-k results. Zero and more-than-one-page results return an exact safe integer, query digest, scope and read revision, same-scope maintenance summary, and bounded count receipt with `stored_structured_subjects` coverage. Receipt fields agree with the enclosing response and E-GRANT. A stale requested revision returns `read_revision_mismatch`; an incomplete count returns `count_unavailable` without partial count, an expired execution lease returns `query_timeout`, and unreadable same-scope status returns `maintenance_status_unavailable`. This ticket proves the backend receipt only; fin snapshot admission/replay is integrated at QP9.
    - **Completion:** An independent ID oracle matches the backend receipt and the list/count pair is demonstrably from one read revision and access scope.
    - **Dependencies:** QP4, QP5, M-STATUS, and E-GRANT.
    - **Status:** [ ] pending.

8. <a id="qp8"></a> **QP8 — Route the typed request through fin's existing retrieve seam** — [implementation]
    - **ID:** QP8
    - **Inherits:** `specs/structured-corpus-query.md`: “The existing routing cascade must have selected `sql` before execution.” A missing backend capability returns `structured_search_unavailable`, never a fallback to the legacy ledger.
    - **Surface:** fin host composition root, retrieve port, MCP adapter, and iknow retrieve MCP input/output.
    - **Acceptance:** Carry the optional typed request from the existing model-facing `retrieve` tool through host-bound mode admission, `RetrievePort`, and MCP `filters.structured_query`; forward `structured_result` alongside existing retrieve fields and preserve typed errors. The host cascade alone binds `sql`; a wrong tier, unsupported outer filters, absent capability, changed descriptive natural-language query, or backend failure cannot widen/narrow predicates or execute the old business-ledger path. Keep one model-facing retrieve tool and no fin-to-iknow code import. This ticket proves typed wire transport and capability refusal, not fin snapshot admission or citation replay.
    - **Completion:** A host-level test demonstrates the complete typed-request route and all refusal paths without invoking the legacy ledger; existing typed retrieve failures remain intact.
    - **Dependencies:** QP4–QP7. Changes to host, port, and adapter are one fin compilation/test slice; E-FIN is consumed at QP9.
    - **Status:** [ ] pending.

9. <a id="qp10"></a> **QP10 — Independently freeze the persistent ID mapping after real ingest** — [decision]
    - **ID:** QP10
    - **Inherits:** `specs/structured-corpus-query.md`: “Freeze the mapping from original inputs to backend-assigned persistent IDs in a separate fixture manifest; these UUIDs cannot be computed from source text.”
    - **Surface:** Shared acceptance-pack identity mapping over Q-ID, E-SOURCE, and the first accepted real ingest.
    - **Acceptance:** Capture backend-assigned entity/assertion/support IDs separately from the already frozen fact/locator oracle. Independently review correspondence to the original inputs, source/extraction revisions, and canonical identity tuples, then freeze the mapping without deriving UUIDs from text or deriving facts from query results. A mismatch blocks final acceptance instead of rewriting the oracle to fit output.
    - **Completion:** A versioned, independently reviewed mapping binds the unchanged original oracle to persistent IDs for all Q/E/M final runs.
    - **Dependencies:** QP1, QP3, EP1, and MP6. This gate follows real ingest/publication and never blocks those capabilities from being implemented.
    - **Status:** [ ] pending.

10. <a id="qp11"></a> **QP11 — Measure the executable path against the frozen resource bounds** — [implementation]
    - **ID:** QP11
    - **Inherits:** `specs/structured-corpus-query.md`: “The complete UTF-8 encoding of each structured-query request and each `structured_result` or `structured_error` response envelope ... is capped independently at 32 KiB.” Request readers have a 60-second maximum and list/provenance have their specified bounds.
    - **Surface:** The real iknow/fin retrieve path and the shared acceptance driver selected before implementation.
    - **Acceptance:** Run the frozen original cases and independently authored boundary cases through the executable path. Record revisions, exact invocation, encoded bytes, page/provenance/count outcomes, and observed resource use; demonstrate typed refusal at irreducible size and execution bounds. Keep deterministic fake-clock/failure controls distinguishable from live measurements; do not invent additional performance thresholds or treat fixture-server smoke as real-service evidence.
    - **Completion:** Reproducible measured evidence satisfies the SPEC's limits; unavailable dependencies, a violated bound, or an unrun measurement leave this ticket pending.
    - **Dependencies:** QP8, QP10, and EP4. It measures transport and query behavior; complete citation replay is integrated at QP9.
    - **Status:** [ ] pending.

11. <a id="qp9"></a> **QP9 — Run the Q1–Q13 real-corpus acceptance boundary** — [implementation]
    - **ID:** QP9
    - **Inherits:** `specs/structured-corpus-query.md`: “Each requires an independently authored expected result.” Existing #330 acceptance obligations remain in force under ADR 0094 and are reported separately.
    - **Surface:** Frozen QP1 corpus/oracle, iknow structured query, fin retrieve path, and the existing acceptance runner selected at implementation start.
    - **Acceptance:** Define and run the new real-corpus acceptance driver against QP1's approved original inputs and independent fact/locator/lifecycle oracle, QP10's reviewed ID mapping, and QP11's measured bounds. Report Q1–Q13 individually with command, exit status, expected-versus-observed IDs/locators/counts, and resource results. Passing structured-query cases do not close #330 or claim enterprise A3b acceptance. Do not substitute old SQL gold rows, synthetic fixtures, MCP fixture-server smoke, or backend query results as the independent oracle.
    - **Completion:** All Q1–Q13 pass on the same identified fin/iknow candidate with QP1, QP10, and QP11 complete. Any blocked, unmet, or unrun case keeps QP9 pending; recording its status is not completion.
    - **Dependencies:** QP8, QP10, QP11, and EP5–EP11 (E-FIN). QP2 remains a separate production-rollout gate.
    - **Status:** [ ] pending — no structured-corpus runtime acceptance has run.

## Integrated execution dependencies

This table resolves direct prerequisites across all three plans. Ticket links and acceptance stay with their owners; transitive prerequisites are implicit. QP1 freezes originals/oracle, QP10 freezes actual IDs after ingest, and QP11 measures the executable path. Final Q/E/M acceptance tickets consume these gates, never each other's final verdict. QP2 independently gates production migration or enablement. Every ticket remains pending.

| Ticket | Direct prerequisites | Delivery boundary |
|---|---|---|
| `QP1` | none | Approved public originals and independent oracle |
| `QP2` | none | Opt-in compatibility and rollout decision |
| `QP3` | `QP1` | Persistent identity registry |
| `QP4` | `QP3` | Closed request validation |
| `EP0` | `QP1`, `QP3` | Evidence driver decision |
| `EP1` | `EP0`, `QP3` | Immutable source/support ingest |
| `EP2` | `EP1` | Finite grants over supplied validated snapshots |
| `EP3` | `EP2` | Private same-MCP retained reads |
| `EP4` | `EP3` | Frozen support continuation |
| `EP5` | `EP0` | Cross-language snapshot digest |
| `EP8` | `EP3` | Durable invalidation/checkpoint capability |
| `MP1` | `QP1` | Maintenance driver decision |
| `MP2` | `MP1` | Durable logical job recovery |
| `MP3` | `MP2` | Bounded attempt retries |
| `MP4` | `MP3` | Intermediate-write and lease fences |
| `MP5` | `MP4`, `EP1` | Desired source stage separated from serving |
| `MP6` | `MP5`, `QP3`, `EP1` | Atomic published accepted set |
| `MP7` | `MP6` | Exact multi-origin withdrawal |
| `MP8` | `MP4`, `MP6` | Structural alternatives |
| `MP9` | `MP8`, `MP6` | Revision-guarded recorded decision |
| `MP10` | `MP5`, `MP6`, `MP7`, `MP8`, `MP9` | Same-snapshot scope summary |
| `MP11` | `MP4`, `MP6` | Projection rebuild without extraction |
| `MP12` | `MP7`, `MP11` | Immediate all-tier serving guards |
| `QP5` | `QP4`, `EP4`, `MP6`, `MP10` | First current list page with readable evidence |
| `QP6` | `QP5` | Bounded revision-bound pages |
| `QP7` | `QP5`, `EP2`, `MP10` | Exact count receipt |
| `QP8` | `QP6`, `QP7` | Host/port/MCP typed query transport |
| `QP10` | `QP1`, `QP3`, `EP1`, `MP6` | Independently reviewed backend ID mapping |
| `QP11` | `QP8`, `QP10`, `EP4` | Measured contract resource bounds |
| `EP6` | `EP3`, `EP5`, `QP8` | Current-turn structured evidence admission |
| `EP7` | `EP6`, `EP3` | Stored citation replay and refresh |
| `EP9` | `EP7`, `EP8` | Fin scrub before acknowledgement |
| `EP10` | `EP9` | Checkpoint/gap recovery before replay |
| `MP13` | `MP4`, `MP7`, `MP11`, `MP12`, `EP1`, `EP2` | Finite collector mark eligibility |
| `MP14` | `MP13`, `EP2` | Atomic mark/reactivation race guard |
| `MP15` | `MP14` | Normal-GC physical phases |
| `MP16` | `MP7`, `MP15`, `EP8` | Immediate purge denial and local deletion |
| `MP17` | `MP16`, `EP9`, `EP10` | All-owner purge completion after fin scrub |
| `EP11` | `EP7`, `EP2`, `MP15` | Fin local expiry and guarded collection |
| `QP9` | `QP8`, `QP10`, `QP11`, `EP4`, `EP5`, `EP6`, `EP7`, `EP8`, `EP9`, `EP10`, `EP11` | All Q1–Q13 pass |
| `EP12` | `EP4`, `EP11`, `EP10`, `MP17`, `QP10`, `QP11` | All E1–E15 pass |
| `MP18` | `MP1`, `MP2`, `MP3`, `MP4`, `MP5`, `MP6`, `MP7`, `MP8`, `MP9`, `MP10`, `MP11`, `MP12`, `MP13`, `MP14`, `MP15`, `MP16`, `MP17`, `QP8`, `EP11`, `QP10`, `QP11` | All M1–M20 pass |

**Implementation assignment:** Dispatch one implementer for each coupled iknow slice, serializing shared types/schema/storage and composition edits. Dispatch a separate fin implementer for coupled host/retrieve changes once the required MCP capabilities exist. Product-test authorship goes to `test-engineer` with explicit disjoint test ownership; the implementer consumes failing-first evidence. Parallel work requires disjoint writes and fixed interfaces; simultaneous acceptance runs also require isolated mutable corpus/storage resources. Three plan authors do not imply three concurrent writers to a shared backend package.

## Q1–Q13 acceptance map

Every row below is a future implementation acceptance case, not evidence already run. “New driver” means the path and command must be selected and reviewed before implementation; this plan deliberately invents neither.

| SPEC criterion | Plan ticket(s) | Verification tier |
|---|---|---|
| Q1 — closed schema, subjects, operations, filters, and typed validation | QP4, QP9 | New request-conformance driver; unit/protocol tier, including decoded `1.0`, booleans, and unsupported fields. |
| Q2 — persistent entity and exact tuple identities | QP3, QP5, QP9 | Identity/rebuild tier against the independently reviewed QP10 backend ID mapping. |
| Q3 — repeated supports do not multiply rows/count; remaining source preserves visibility | QP5, QP7, QP9 | Real-source lifecycle tier with before/after support oracle. |
| Q4 — replacing the sole support hides only the current row and retains historical evidence | QP5, QP9 | Real-source replacement plus authorized historical-read tier. |
| Q5 — past/future validity stays visible without implicit wall-clock filtering | QP5, QP7, QP9 | Query/list/count tier with independently expected intervals. |
| Q6 — exact incoming/outgoing one-hop IDs; no two-hop traversal | QP4, QP5, QP9 | Filter integration tier with both directions and a two-hop-only path. |
| Q7 — ascending complete pages and stale/expired cursor refusal | QP6, QP9 | Multi-page transaction/interleaving tier, including permission revocation. |
| Q8 — exact complete count, including zero and more than one page | QP7, QP9 | Independent ID-oracle/count-receipt tier on the same revision and scope. |
| Q9 — maintenance revision changes without invalidating the query cursor | QP5, QP6, QP7, QP9 | M-STATUS integration tier across pending/running/ready/failed/stale states. |
| Q10 — 32 KiB envelopes, provenance continuation, typed size/count failures | QP4, QP5, QP6, QP7, QP9 | New byte-boundary and injected-failure driver; define before implementation. |
| Q11 — claims, sums, free-text SQL, fuzzy search, joins, and multi-hop remain unavailable | QP4, QP8, QP9 | Negative conformance tier with exact error outcomes and no legacy fallback. |
| Q12 — deterministic attributed display and support-scope isolation | QP5, QP9 | Conflicting display/source-order and two-authorization-scope tier; selected support read is verified. |
| Q13 — host routing, capability guard, and descriptive-query invariance | QP4, QP8, QP9 | Host → port → MCP tier for wrong tier, unsupported outer filter, unavailable capability, and changed natural-language text. |

## Typed-error ownership

The Query SPEC owns each code and its retryable flag. These trigger cases must be observable at the named ticket boundary and preserved by QP8 through fin; they never become prose-only or empty-success fallback.

| Code | Trigger to verify | Owner |
|---|---|---|
| `malformed_structured_query` | Missing/wrong type, duplicate field, illegal combination, or wrong host-bound tier | QP4, QP8 |
| `unsupported_structured_schema` | Unknown token or unsupported integer version | QP4 |
| `unsupported_subject` | Subject outside the closed set | QP4 |
| `unsupported_operation` | Operation outside list/count | QP4 |
| `unsupported_field` | Subject filter field outside its allowlist | QP4 |
| `unsupported_operator` | Operator outside eq/in | QP4 |
| `invalid_value` | Invalid declared type or bound | QP4 |
| `request_too_large` | Encoded request exceeds 32 KiB | QP4 |
| `authorization_denied` | Trusted caller lacks the bound scope | QP4, QP5, QP6, QP7 |
| `invalid_cursor` | Cursor malformed or authentication fails | QP6 |
| `cursor_expired` | Cursor reaches its fixed lifetime | QP6 |
| `cursor_revision_mismatch` | Query, scope, or data revision changed | QP6 |
| `read_revision_mismatch` | Count requests an old data revision | QP7 |
| `result_too_large` | Mandatory response cannot fit | QP5, QP6, QP7 |
| `count_unavailable` | Complete count cannot finish; no partial result | QP7 |
| `query_timeout` | Reader exceeds the 60-second execution lease | QP5, QP6, QP7 |
| `structured_search_unavailable` | Backend capability unavailable or structured backend fails | QP5, QP7, QP8 |
| `maintenance_status_unavailable` | Required scope summary is missing or inconsistent | QP5, QP7 |

## Existing regression checks and unrun acceptance

Run these existing commands/suites only as regression coverage; none substitutes for Q1–Q13:

- fin default regression: `uv run pytest -q`; static regression: `uv run --no-sync pyright`.
- fin retrieve wiring regressions: `tests/retrieve/test_port_signature.py`, `tests/retrieve/test_mcp_adapter.py`, `tests/retrieve/test_product_retrieve_wiring.py`, and `tests/host/test_graph.py`.
- iknow structured/business retrieval regressions: `npm run typecheck`, `npm run build`, `npm test`, and `npx vitest run tests/business-query/sql-query.test.ts tests/retrieval/mode-d-arm.test.ts tests/retrieve.test.ts`.
- Preserve the separate #330 legacy SQL regression commands from `plans/pre330-remaining-prerequisites.md`: `uv run --no-sync pytest -q tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` and `uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py`. Live backend evidence remains distinct from a passing default suite and from Q1–Q13.

No new query acceptance path, command flag, or fixture name is preselected here. Query acceptance drivers are defined before implementation using the actual package scripts and the frozen QP1 input/oracle; fixture-server smoke validates protocol shape only. No query implementation or Q acceptance test ran while writing this plan.

## End-of-round code review gate

After all implementation tickets land, run `arthurpower:code-review` once for the integrated change. If it returns `GATE: BLOCKED`, the next slot is `arthurpower:review-report-repair`; then run `verification-before-completion`. Keep this review separate from the future Q1–Q13 test evidence and from the existing #330 decision.

**Persist list:** Empty. The Query SPEC says no duplicate glossary or query ADR is needed; QP1/QP2 capture acceptance and rollout decisions without changing shared vocabulary or reopening ADRs 0091–0095.

## Planning verification — 2026-10-03

**Basis:** The operator requested three separate implementation plans and review. The three structured-corpus SPECs, ADRs 0091–0095, and the existing #330 acceptance obligations are the normative inputs. This delivery changes only the three plan files; runtime implementation, corpus selection, migration, and Q/E/M acceptance remain pending.

Three independent plan-author assignments produced Query, Evidence, and Maintenance. Read-only cross-review used a different author for each document. Findings were corrected and the same reviewers verified the corrections; main integration separately checked ownership and the cross-plan graph.

| Plan | Independent reviewer | Final review | Reviewed content SHA-256, before final formatting and this record |
|---|---|---|---|
| Query | Maintenance author | PASS; five ACR verdicts yes | `6fff681051088b1c6437d931ba7c8d7b6fabad81eb3776a51a212d43aa8423ba` |
| Evidence | Query author | PASS; five ACR verdicts yes | `bd082e389350b95825aace1049bef510436e62e773f65f7c7445f44bcd97c25d` |
| Maintenance | Evidence author | PASS; five ACR verdicts yes | `d4e8e7f1da578cae196c29e2a8304a9ebfc6c23f918ca7e3ecdf9facff323e3f` |

The corrected findings cover strict validity intervals, query revision changes and typed errors, replay authorization before rendering, expiry versus physical collection, concrete affected-file scope, full job/fence inputs, complete derived dependencies, atomic purge invalidations, old-snapshot replay after re-extraction, and applicable maintenance input classes. Final formatting only normalizes nested ticket-list indentation; it changes none of the reviewed contracts.

**Delivery checks:**

- The temporary document checker `python3 /tmp/fin-structured-plan-check.py` verifies three files, 42 anchored tickets with required fields and one tag each, 48 complete Q/E/M case mappings, 15 passing ACR entries, valid local links, and 42 acyclic execution nodes. Its exit status is 0. It is a one-run document check, not a repository-owned runtime acceptance driver.
- `uv run --frozen --no-sync pytest -q -m eval tests/eval/test_gold.py` returned exit 0: **9 passed**, terminal outcomes **done=6, refuse=3**. The baseline uses the committed ADR 0012 gold cases and corpus manifest SHA-256 `148bf521f7ec2544ba960d1b5e4dfb4e3cdfa5a2584c73cb6d776ec81c2de81c` at fin `cdf1f771409ea90ea600e5eb794b0455ed29a4ba`; it exercises scripted/Fake adapters and is not structured-corpus or live-backend acceptance.
- The broader `uv run --frozen --no-sync pytest -q -m eval` also selected full-PDF evaluation beyond the documentation baseline. It was deliberately interrupted after **15 passed, 5711 deselected**, exit **2**; that run is not counted as passing.
- `git diff --cached --check` must return exit 0 before the local planning commit. No runtime code, lockfile, settings, or canonical domain document is in this delivery.

**Unexecuted scope:** Q1–Q13, E1–E15, and M1–M20 are future runtime cases. QP1's original corpus/oracle, QP10's post-ingest identity mapping, QP11's real-path measurements, and the QP2 production-rollout decision remain unresolved implementation gates. Existing #330 blockers stay unchanged. No new durable vocabulary or rule change is proposed.
