# Plan: Structured corpus evidence

**Goal:** Give every structured query result a finite, source-bound evidence reference that fin can verify and replay without changing legacy evidence.
**Approach:** Build immutable source and support records in iknow first, then add finite row/count grants and the private evidence actions on the existing MCP connection. In fin, add the separate structured-result snapshot, cross-language digest, current-turn verification, replay, invalidation handling, and local expiry. Query owns shared result identity and the public acceptance pack; Maintenance owns accepted-set publication and backend collection, so this plan consumes those outcomes only at their actual integration points.
**Spec link:** [structured corpus evidence](../specs/structured-corpus-evidence.md)
**Related contracts:** [structured corpus query](../specs/structured-corpus-query.md), [structured corpus maintenance](../specs/structured-corpus-maintenance.md), [ADR 0091](../docs/adr/0091-database-row-citation-verifies-without-a-text-span.md), [ADR 0092](../docs/adr/0092-stored-database-snapshot-identity-is-a-recomputable-consumer-digest.md), [ADR 0093](../docs/adr/0093-evidence-token-version-carrier.md), [ADR 0094](../docs/adr/0094-structured-corpus-search-and-knowledge-maintenance.md), [ADR 0095](../docs/adr/0095-immutable-source-history-and-finite-retention.md)
**Planned surfaces (ACR scope):**
- iknow-memory: existing TypeScript source/extraction storage and ingest path; existing retrieve/MCP capability; the associated schema, PostgreSQL and in-memory storage paths and tests.
- fin: `src/fin/retrieve/` for the existing retrieve port/adapter and `src/fin/host/` for evidence admission, citation verification, replay, and turn storage; their existing tests.
- No direct cross-repository imports and no additional model-facing search tool.

Existing affected-file candidates are iknow's `src/core/types.ts`, `src/core/storage/backend.ts`, `src/core/storage/schema-bootstrap.ts`, `src/core/storage/pgvector-backend.ts`, `src/core/storage/memory-backend.ts`, `src/tools/push.ts`, `src/tools/retrieve.ts`, and `src/index.ts`; fin's `src/fin/retrieve/port.py`, `src/fin/retrieve/mcp_adapter.py`, `src/fin/retrieve/mcp_session.py`, `src/fin/host/evidence_contract.py`, `src/fin/host/evidence_admission.py`, `src/fin/host/citation_gate.py`, `src/fin/host/graph.py`, and host-owned persistence/composition such as `src/fin/host/thread_store.py` and `src/fin/host/workspace_http.py`. The existing regression files below are the test scope. Confirm the per-ticket subset before implementation; new internal files and physical storage design remain implementer choices. This enumerates the concrete cross-module blast radius without prescribing one patch.

**Exports / imports:**
- **E-SOURCE (EP1):** immutable source/version/frozen-unit binding plus support storage and ingest capability. Imports Query **Q-ID** ([QP3](structured-corpus-query.md#qp3)) and **QP1**'s frozen input/oracle. It does not wait for the full Query result path, M-PUBLISH, or [QP10](structured-corpus-query.md#qp10)'s post-ingest ID mapping.
- **E-GRANT (EP2):** finite grant transaction and immutable read capability over an owner-validated, consistent query snapshot. Imports E-SOURCE for row dependencies. It does not wait for the full Query wire delivery or M-PUBLISH.
- **E-FIN (EP5–EP11):** host snapshot admission, current-turn verification, replay, invalidation, and local expiry. Host admission follows [QP8](structured-corpus-query.md#qp8)'s typed transport. It consumes **M-PUBLISH** ([MP6](structured-corpus-maintenance.md#mp6)) and **M-STATUS** ([MP10](structured-corpus-maintenance.md#mp10)) at the query boundary. **M-COLLECT** ([MP15](structured-corpus-maintenance.md#mp15)) supplies collector behavior. **M-PURGE** ([MP16](structured-corpus-maintenance.md#mp16)) completes its backend-local phases independently; fin's scrub acknowledgement gates **M-OWNER-COMPLETE** ([MP17](structured-corpus-maintenance.md#mp17)).

The [integrated dependency table](structured-corpus-query.md#integrated-execution-dependencies) resolves all ticket prerequisites. QP1 freezes originals/oracle, QP10 freezes actual IDs after ingest, and QP11 supplies measured bounds; early Evidence capabilities do not wait for the latter two gates.
**Implementation ACR (for this plan's declared blast radius):**
```text
bounded-context-guardian: yes — iknow retains source, support, grant, and backend invalidation ownership; fin owns its citation snapshots and host verifier; all cross-repo traffic stays behind the retrieve port.
input-contract-tests: yes — EP0 freezes the new-driver coverage before code; new action boundaries cover empty, invalid, overflow, concurrent, and exception inputs, and raw duplicate-key cases run only where raw bytes are owned.
error-handling-enforcer: yes — contract failures remain typed, unavailable references never become successful empty evidence, and expiry, access denial, purge, and backend failure stay distinguishable.
complexity-anti-drift: yes — source/grant/actions, digest/admission, citation replay, invalidation, recovery, and expiry are separate vertical slices using existing ownership seams.
minimal-change-verifier: yes — the scope is structured-result evidence behind the existing retrieve seam; legacy kinds and #330 obligations remain regression gates, while migration/re-ingest is gated by QP2.
```
This is the implementation-stage ACR block, not an acceptance result. Plan approval checks this decomposition and its dependencies; future runtime acceptance requires the frozen real-source inputs, the post-ingest identity mapping, and the E1–E15 driver results.

**Per-ticket implementation loop (EP1–EP12):** failing-first test → minimal implementation/refactor → typecheck and focused plus relevant existing regression tests → `verification-before-completion`. Define every new acceptance driver, its real-adapter route, and exact runnable command before writing that driver; no new driver path or flag is presumed by this plan. After all tickets, run one end-of-round `code-review`; if `GATE: BLOCKED`, repair and repeat that review, then run `verification-before-completion`. This plan phase itself adds no runtime tests and claims no runtime acceptance.

## Milestones

| Milestone | Tickets | Outcome |
|---|---|---|
| Verification contract | EP0 | E1–E15 mapped to existing regressions and explicitly new real-adapter acceptance coverage before implementation |
| Backend evidence foundation | EP1–EP2 | Immutable source/support inputs and finite row/count grants exist independently of full Query delivery |
| Private evidence reads | EP3–EP4 | The same `corpus_evidence` MCP capability reads exact grants and bounded supports |
| fin evidence lifecycle | EP5–EP7 | Typed snapshot digest, current-turn verification, replay/refresh, and named unavailable states |
| Revocation and expiry | EP8–EP11 | Durable invalidations, scrub-before-ack, safe reconnect/gap handling, and local snapshot expiry |
| Evidence acceptance | EP12 | Every E1–E15 case passes on the integrated real adapter and retained records; missing evidence keeps this ticket pending |

## Tasks (ordered by dependency)

1. <a id="ep0"></a> **EP0 — Freeze the E1–E15 verification map and acceptance-driver contract** — tag: `[decision]`
    - **Inherits:** “A public real-source acceptance pack and independently reviewed expected locators must be frozen before implementation acceptance.”
    - **Surface:** Existing fin and iknow regression suites plus the real retrieve-adapter acceptance boundary.
    - **Acceptance:** Before runtime code starts, a reviewed matrix maps every E1–E15 case below to its owning ticket and verification tier, identifies existing regressions separately from new real-adapter/retained-record acceptance, and records the exact driver location and command only after the implementer verifies the current harness; the matrix uses QP1's frozen source inputs and independent oracle, and records that backend-assigned IDs are frozen only after the first real ingest.
    - **Completion:** A fresh implementer can add the acceptance proof against the real adapter and retained records without treating example data or an invented command as evidence.
    - **Dependencies:** QP1's source-input and expected-locator freeze; QP3 (Q-ID identity registry). The full post-ingest ID mapping is a later final-acceptance input, not a prerequisite for deciding this matrix.
    - **Status:** [ ] pending

2. <a id="ep1"></a> **EP1 — E-SOURCE: persist immutable source bindings and occurrence supports through ingest** — tag: `[implementation]`
    - **Inherits:** “A version binds original content digest, retained original object reference, parser revision, frozen-unit manifest, and the explicit mapping to fin locators.”
    - **Surface:** iknow-memory's existing source/extraction storage and ingest producer; its existing persistent and in-memory storage paths.
    - **Acceptance:** Ingest stores immutable source versions separately from extraction revisions; validates each company locator's binding to the specified source version and refuses an unresolved binding as `unsupported_source_binding`; preserves optional half-open Unicode code-point spans; keys a support by subject/result/source/version/location/span so repeated extraction attaches another extraction link to the same support while distinct occurrences and origins remain distinct; and never sends fields grounded solely by an unauthorized support. Do not reuse the legacy newest-wins/overlap merge path for these semantics.
    - **Completion:** An ingested source and its extraction occurrences can be inspected as stable source-to-locator and support-to-extraction bindings, with duplicate occurrence reuse and distinct-origin preservation observable end to end.
    - **Dependencies:** EP0; QP3 (Query identity registry); QP1 frozen originals and independently reviewed locator oracle. This early capability must not wait for full Q result delivery, M-PUBLISH, or backend IDs that only the first real ingest can produce. M-PUBLISH later owns activation/retirement of accepted occurrences.
    - **Status:** [ ] pending

3. <a id="ep2"></a> **EP2 — E-GRANT: issue finite row and count grants from one consistent read snapshot** — tag: `[implementation]`
    - **Inherits:** “iknow creates a finite retained reference atomically with the query's authorized read snapshot”; “Every returned row receives a grant.”
    - **Surface:** iknow-memory's evidence persistence and query-to-evidence storage seam.
    - **Acceptance:** A single supplied, owner-validated snapshot can create a nonrenewable grant valid for 365 days from `queried_at`; a row grant protects the exact row, full support and extraction dependencies, and selected endpoint display supports, while a count grant (including zero) retains only its receipt plus shared `(scope_binding, authorization_revision, purge_revision)` authorization state and no result membership or source payload; permission changes and explicit purge invalidate count replay, ordinary publication and payload GC do not; no new grant is issued after a purge candidate stops accepting obligations; and size/expiry/failure exits remain typed.
    - **Completion:** Row and count grants can be created and read from the supplied transaction snapshot, with expiry, dependency, and authorization/purge behavior distinguishable from ordinary data revision changes.
    - **Dependencies:** EP1 for row dependency identity; EP0 for input/acceptance coverage. This storage capability accepts owner-validated values through the shared interface and may be completed before QP5/QP7 produce list or count results, before the full Query wire path, or before M-PUBLISH exists.
    - **Status:** [ ] pending

4. <a id="ep3"></a> **EP3 — Expose exact retained-grant reads through internal `corpus_evidence`** — tag: `[implementation]`
    - **Inherits:** “`read` returns exactly the stored row/count rather than running a query.”
    - **Surface:** iknow-memory's existing MCP retrieve capability, fin's `src/fin/retrieve/` port/adapter, and the E-GRANT read interface.
    - **Acceptance:** The host-internal `read` action travels as `corpus_evidence` over the same existing MCP connection through fin's typed retrieve adapter, with trusted consumer identity supplied by transport; it returns original context and exact value only after rechecking all required current dependencies, obeys the 32 KiB envelope and 60-second action bound, and returns a typed unavailable/error response rather than an empty success. It exposes no second model-facing search tool and never re-runs a query; it is a separate retained-grant read, not the current `read_source` text-slice behavior.
    - **Completion:** A grant reference can be replayed through the existing fin retrieve connection into the same retained value, or refused with the contract's typed reason.
    - **Dependencies:** EP2.
    - **Status:** [ ] pending

5. <a id="ep4"></a> **EP4 — Add bounded support continuation and endpoint-display resolution** — tag: `[implementation]`
    - **Inherits:** “It returns at most 8 supports and 32 KiB, binds the same immutable reference and scope, rechecks current access, and expires with the original reference.”
    - **Surface:** iknow-memory's support/grant read service and the existing `corpus_evidence` action capability, carried through fin's typed retrieve adapter on the same MCP connection.
    - **Acceptance:** The `supports` action returns supports in stable UUID order, at most 8 and within 32 KiB, accepts either continuation cursor or selected `support_id` but not both, resolves a selected relationship-endpoint display support even beyond the inline prefix, refuses support reads for count grants, withholds a row if a required dependency is denied/purged/expired, and never renews the grant; it is not a model-facing tool.
    - **Completion:** A row with more than 8 supports can retrieve each authorized continuation and a selected endpoint display support from the same expiring grant through fin's existing retrieve connection without re-querying current supports.
    - **Dependencies:** EP1–EP3; M-PUBLISH supplies the active accepted support set only when this action is integrated with live Query results.
    - **Status:** [ ] pending

6. <a id="ep5"></a> **EP5 — Implement the structured-snapshot canonical bytes and cross-language digest** — tag: `[implementation]`
    - **Inherits:** “Python and TypeScript must share byte-level golden vectors before shipping this new profile; the legacy digest vectors remain unchanged.”
    - **Surface:** fin's evidence snapshot profile and iknow-memory's corresponding canonical-value conformance surface.
    - **Acceptance:** Python and TypeScript produce identical canonical UTF-8 bytes and SHA-256 for shared vectors covering JSON type distinctions, integer normalization, key byte-ordering, array order, escaping, control characters, safe-integer overflow, non-finite values, unpaired surrogates, unexpected keys, and `1`/decoded `1.0`; raw duplicate-key rejection is tested only at a decoder that owns raw bytes, and no object-level test pretends it can observe discarded lexical keys; ADR 0092's legacy digest remains unchanged.
    - **Completion:** Either runtime can independently reproduce the other's structured snapshot digest from the same validated value, with no change to legacy row-digest identity.
    - **Dependencies:** EP0; can proceed alongside EP1–EP4 once shared serialized type changes are coordinated.
    - **Status:** [ ] pending

7. <a id="ep6"></a> **EP6 — Admit and verify structured-result evidence from the current turn** — tag: `[implementation]`
    - **Inherits:** “The host verifier binds an answer claim to fields received in the current turn”; “An absent span permits structured evidence, never a quotation.”
    - **Surface:** fin's existing host evidence admission and citation-verifier path, consuming the retrieve port's typed result.
    - **Acceptance:** fin accepts only the frozen typed `structured_result` sibling and its exact typed snapshot context; it adds `kind: "structured-result"` only to the retained fin snapshot, binds row claims to received subject/operation/result-ID fields and count claims (including zero) to the exact receipt/query identity, and refuses invented/unreceived fields, unknown keys, mismatched expiry/reference/context, and unsupported values with typed exits. A structured span or multiple supports never authorizes a quote: every quote still goes through the existing source-read path and ADR 0046. `database_rows`, `chunks[]`, and legacy evidence validation remain unchanged.
    - **Completion:** A same-turn row or count claim can be verified only from its validated structured payload while existing text, visual, and database-row verification retain their prior behavior.
    - **Dependencies:** EP3, EP5, and Query QP8's typed host transport; it does not depend on QP9 final Query acceptance. Full result integration consumes M-PUBLISH's accepted active set at the query boundary and M-STATUS only for the frozen maintenance summary.
    - **Status:** [ ] pending

8. <a id="ep7"></a> **EP7 — Persist, replay, refresh, and mark unavailable structured citations** — tag: `[implementation]`
    - **Inherits:** “Opening an old citation uses the retained snapshot and reference; it issues no search query. Refresh creates a new query and answer evidence without editing the old answer.”
    - **Surface:** fin's host citation/turn persistence and citation presentation path, plus the existing retrieve adapter's internal read.
    - **Acceptance:** A stored citation contains the exact bounded snapshot, its recomputable digest, and selector. Before rendering any replay, call the private `corpus_evidence.read` capability to recheck current authorization, purge state, fixed expiry, and the exact frozen grant; count replay checks its shared authorization/purge revisions. Refusal withholds the entire snapshot, including before the next periodic invalidation drain. Successful replay returns the original retained value without query/reselection; refresh creates distinct evidence without rewriting the old snapshot. Expired/purged history stays visibly unavailable, distinct from denied access or backend failure; no snapshot exceeds 32 KiB or truncates identity.
    - **Completion:** An answer can keep its original cited structured value across source refresh and later show an explicit unavailable state without redirecting to the replacement version.
    - **Dependencies:** EP3, EP5, EP6, and Query QP8's typed host transport; it does not depend on QP9 final Query acceptance. The result's active provenance is supplied by M-PUBLISH, and its original maintenance summary by M-STATUS where included.
    - **Status:** [ ] pending

9. <a id="ep8"></a> **EP8 — Publish durable per-consumer invalidation reads and contiguous acknowledgements** — tag: `[implementation]`
    - **Inherits:** “Invalidations are durable per-consumer outbox events with `event_id`, `evidence_ref`, and reason (`access_revoked` or `purged`); they contain no source text.”
    - **Surface:** iknow-memory's grant invalidation/outbox storage and existing `corpus_evidence` MCP capability, plus fin's typed retrieve adapter transport for both actions.
    - **Acceptance:** `invalidations` and `ack` travel as host-internal actions over the same MCP connection through fin's existing retrieve adapter; they support nullable checkpoint and limit 1..20 (default 20), content-free events, boolean gap, server-authenticated consumer-bound checkpoints, and typed refusal of malformed/cross-consumer or non-contiguous acknowledgements. Unacknowledged events survive while grants remain unexpired and can be collected after grant expiry without acknowledgement; no second model-facing search tool is added.
    - **Completion:** One consumer can drain a bounded durable event sequence through fin's retrieve port and advance only its own checkpoint through the contiguous returned batch.
    - **Dependencies:** EP2–EP3; Maintenance emits purge/access lifecycle changes, while this task owns only the evidence-grant event and acknowledgement capability.
    - **Status:** [ ] pending

10. <a id="ep9"></a> **EP9 — Scrub fin snapshots transactionally before acknowledging invalidations** — tag: `[implementation]`
    - **Inherits:** “Fin drains at least once per minute while online, transactionally withholds and scrubs affected stored snapshot payloads, and acknowledges only afterward.”
    - **Surface:** fin's host snapshot persistence/invalidation worker and existing retrieve adapter.
    - **Acceptance:** Draining a batch atomically makes every affected local citation unavailable and removes its retained payload before sending `ack`; retry after a crash is idempotent; a grant never stored by fin is acknowledged only after confirming no local payload exists; and no denied payload can be exposed between the event read, scrub, and acknowledgement.
    - **Completion:** A successful acknowledgement is evidence that all matching local evidence payloads have already been withheld and scrubbed.
    - **Dependencies:** EP7–EP8. The host emits the scrub acknowledgement consumed by MP17 (M-OWNER-COMPLETE); it does not depend on final all-owner purge completion.
    - **Status:** [ ] pending

11. <a id="ep10"></a> **EP10 — Recover checkpoints and gaps before exposing historical citations** — tag: `[implementation]`
    - **Inherits:** “Reconnect/crash recovery resumes from the last acknowledged checkpoint before exposing historical evidence”; “If a retained checkpoint is unavailable (`gap`), revalidate all retained references and scrub denied ones before exposing them.”
    - **Surface:** fin's host startup/reconnect and historical citation read path; the existing `corpus_evidence` capability.
    - **Acceptance:** After restart the host processes from its last acknowledged checkpoint before enabling historical structured reads; on `gap`, it revalidates every retained grant and scrubs denied payloads before releasing any citation; explicit purge remains pending for this fin consumer until scrub acknowledgement is emitted, and no backend denial alone is reported as physical erasure. The host does not declare global purge completion; MP17 owns that outcome after consuming this acknowledgement.
    - **Completion:** Reconnect, interrupted drain, and missing checkpoint scenarios all resume safely with denied evidence withheld and fin's acknowledgement delivered only after scrub.
    - **Dependencies:** EP7–EP9. MP17 consumes the resulting acknowledgement and is not a prerequisite for EP8–EP10. EP8's event capability can be built before MP16 integrates it with actual purge transactions.
    - **Status:** [ ] pending

12. <a id="ep11"></a> **EP11 — Expire fin's local snapshots in step with finite backend grants** — tag: `[implementation]`
    - **Inherits:** “Bounded answer snapshots/references expire 365 days after query; source deletion/access revocation can withhold sooner.”
    - **Surface:** fin's host citation/turn retention and expiry collection path, coordinated with Maintenance's physical collector contract.
    - **Acceptance:** At fixed expiry, fin immediately withholds replay and displays an expired/unavailable reason, without renewing the deadline on read. Normal physical collection follows mark, wait seven days, recheck, and delete; explicit authorized purge bypasses normal age/grace. Retain only non-content audit data after deletion. Backend row payload protection remains bounded by reference and retirement deadlines; zero-count receipts pin neither source content nor membership and survive ordinary publication/old-source GC until expiry unless authorization/purge revisions change.
    - **Completion:** Fake-time cases prove immediate withholding at expiry and physical deletion only after the normal grace/recheck, including expiry plus seven elapsed UTC days. Local and backend obligations end on their owner deadlines without depending on consumer acknowledgement.
    - **Dependencies:** EP2, EP7, MP15 (M-COLLECT), and M-STATUS only for the snapshot's frozen maintenance summary. QP2 is required before production opt-in or migration/re-ingest rollout; it does not block isolated development runtime acceptance.
    - **Status:** [ ] pending

13. <a id="ep12"></a> **EP12 — Run final E1–E15 acceptance on the integrated retained-evidence path** — tag: `[implementation]`
    - **Inherits:** “The driver must exercise the real adapter seam and retained records, not only fabricated successful payloads.”
    - **Surface:** EP0's reviewed acceptance driver across the real fin retrieve adapter, iknow storage, and host-retained citations.
    - **Acceptance:** Against QP1's frozen real-source inputs/oracle, QP10's reviewed post-ingest identity map, and QP11's measured bounds, all E1–E15 pass on the integrated development candidate. Any missing, blocked, or unmet case keeps EP12 pending. Required M-PUBLISH/M-STATUS outcomes and MP17's post-ack all-owner completion are present. QP2 gates production enablement or migration/re-ingest, not this development acceptance.
    - **Completion:** All E1–E15 have reproducible observed passes on identified integrated revisions; any fail, skip, unavailable prerequisite, or unrun case leaves this ticket pending.
    - **Dependencies:** EP0–EP11; QP1, QP10, QP11; Query QP5–QP8 behavior/wire (not QP9's final review); MP6, MP10, and MP17 after fin acknowledgement. QP2 is a separate production rollout gate.
    - **Status:** [ ] pending

## E1–E15 coverage and verification tiers

These are future runtime acceptance cases from the SPEC, not passes. **Existing regression** means preserve established behavior using the named current suites; **new adapter acceptance** means the real retrieve adapter, retained records, frozen QP1 inputs/oracle, and the post-ingest identity map are exercised through the driver defined in EP0. New driver paths and flags are intentionally left for that pre-code decision.

| SPEC case | Owning ticket(s) | Verification tier |
|---|---|---|
| E1 | EP0, EP1, EP2, EP12 | New adapter acceptance: real frozen corpus, reviewed source/unit mapping and mismatch refusal |
| E2 | EP1, EP12 | New adapter acceptance: duplicate occurrence reuses one support and retains both extraction links |
| E3 | EP1, EP4, EP12; M-PUBLISH | New adapter acceptance: second origin preserves identity after one origin withdraws |
| E4 | EP6, EP7, EP12 | New adapter acceptance: old answer replays v1, explicit refresh sees v2, replay makes no query |
| E5 | EP5–EP6, EP12 | New host/adapter acceptance: row and zero/nonzero count verify only current-turn fields |
| E6 | EP2–EP4, EP7, EP12 | New adapter acceptance: >8 ordered supports, both size bounds, complete/continuation invariant, fixed expiry |
| E7 | EP5, EP12; EP0 | Cross-language golden vectors plus raw-byte decoder tests only at an owned raw boundary |
| E8 | EP3–EP4, EP8–EP10, EP12 | New adapter acceptance: access revocation/purge withholds existing row and continued support |
| E9 | EP2, EP11–EP12; MP15 | New fake-clock adapter acceptance: 90/365-day eligibility, immediate expiry withholding, seven-day mark/recheck grace, and physical collection; reads never renew |
| E10 | EP7, EP11–EP12 | New host/adapter acceptance: unavailable citation never redirects; cleanup leaves non-content audit only |
| E11 | EP2–EP3, EP5–EP6, EP12 | New action/host contract tests: oversized values and backend failures use typed errors, never empty success |
| E12 | EP6–EP7, EP12 | Existing regression: legacy database-row, locator, visual-evidence, ADR 0092 digest, and ADR 0093 cases unchanged |
| E13 | EP1, EP7, EP12; M-PUBLISH | New adapter acceptance: shared support ID persists, omitted occurrence retires on publish, old snapshot keeps its attribution |
| E14 | EP2–EP3, EP6, EP11–EP12; Maintenance collector | New adapter acceptance: zero receipt survives ordinary publication/old payload GC but not auth/purge revision change |
| E15 | EP3–EP4, EP8–EP10, EP12; MP16–MP17 | New adapter acceptance: read/supports, scrub-before-ack, restart/gap recovery; backend-local deletion and all-owner completion stay distinct |

## Existing regression commands

These are existing commands/suites only; they are regression coverage, not E1–E15 acceptance. New acceptance commands are recorded through EP0 before their driver is written.

- fin evidence/retrieve/read-source regressions: `uv run --no-sync pytest -q tests/host/test_evidence_contract.py tests/host/test_citation_gate.py tests/host/test_sql_row_evidence.py tests/host/test_read_source_tool.py tests/retrieve/test_source_read.py`
- fin typecheck: `uv run --no-sync pyright`
- iknow evidence/read-source/invalidation regressions: `npx vitest run tests/tools/read-source.test.ts tests/tools/invalidation.test.ts tests/integration/read-source-pg.test.ts tests/integration/content-invalidation-pg.test.ts` (the PostgreSQL integration cases require their existing database setup)
- iknow package checks: `npm run typecheck`, `npm run build`, and `npm test`

The integrator should run only affected established checks after the changed paths are known, retain exact exits/counts, and not infer live acceptance from fixture-server or Fake-only tests.

## Coupling, decisions, and gates

iknow-memory compiles as one TypeScript package. Shared types/schema changes, PostgreSQL and in-memory storage, and tool/index wiring can touch the same build boundary, so serialize edits to those shared seams even when evidence storage and Query are separately owned. Keep the new evidence API as a bounded capability; do not expand a single mandatory base interface across unrelated backends solely for convenience.

In fin, Query and Evidence share `src/fin/retrieve/` public types/adapter and `src/fin/host/` evidence/citation paths. Sequence edits to shared retrieve types and the host evidence admission/citation gate, then allow independent storage slices to proceed where their interfaces are fixed. Continue to use the retrieve port and composition root; do not import iknow into fin. The implementation may choose physical schema, helper boundaries, and which existing storage seam owns each transaction as long as the acceptance invariants hold.

Migration/re-ingest is not authorized by this evidence plan's acceptance cases: QP2 owns the opt-in and rollback gate. Existing origins must not be invented or backfilled from mutable legacy rows; the first delivery needs retained original inputs, and missing historical origins stay explicitly unavailable. Existing #330 S6 evidence obligations and A3b source boundaries remain in force; these plans do not mark #330 accepted/closed or turn development/public corpus results into enterprise evidence.

**Persist list:** Empty. No new domain terms are needed, and no ADR is reopened. The new citation kind and snapshot digest remain specified here; existing ADRs 0091–0093 are not rewritten.

**Plan approval versus runtime acceptance:** Plan approval checks the task DAG and implementation ACR. Runtime acceptance remains pending until QP1, QP10, QP11, M-PUBLISH/M-STATUS, MP17's post-scrub outcome, and every E1–E15 observed pass exist. QP2 independently gates production opt-in and migration/re-ingest rollout.
