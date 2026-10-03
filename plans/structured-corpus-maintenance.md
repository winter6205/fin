# Plan: Incremental structured corpus maintenance

**Goal:** Publish, check, withdraw, and collect versioned structured knowledge without serving stale or unauthorized results or losing retained evidence.
**Approach:** Keep lifecycle authority inside iknow's existing TypeScript package and ship it as bounded, recoverable transitions. The plan exports three backend milestones—accepted current-set publication, a same-scope maintenance summary, and a local purge primitive—for the Query and Evidence plans to consume through their existing shared package / retrieve-port boundaries. It does not create a scheduler, a new package, or a second fin-side corpus store.
**Spec link:** [structured corpus maintenance](../specs/structured-corpus-maintenance.md)
**Baselines:** fin `cdf1f771409ea90ea600e5eb794b0455ed29a4ba`; iknow-memory `eea1e07124e8baf68893bc36fda692ac1dc9d684`.
**Persist:** Empty — no new shared domain term or ADR reopening is introduced; source-version, support, publication, and retention vocabulary stays with `docs/CONTEXT.md` and ADR 0095.
**Implementation surfaces:** iknow's existing TypeScript package, including its shared core types and index-job logic, storage backend/schema plus PostgreSQL and in-memory implementations, and existing retrieve/maintenance composition. The related compile units include `src/core/types.ts`, `src/core/index-job.ts`, `src/core/storage/backend.ts`, `src/core/storage/schema-bootstrap.ts`, `src/core/storage/pgvector-backend.ts`, and `src/core/storage/memory-backend.ts`; select the concrete set per tracer bullet at implementation. fin remains behind `src/fin/retrieve/` and `src/fin/host/`; its Query/Evidence wire changes belong to those plans. Keep these changes serialized within iknow's one TypeScript package because schema, types, backend operations, and tool composition compile together.
**Cross-plan gates:** [QP1](structured-corpus-query.md#qp1) freezes originals/oracle before behavior tests; [QP10](structured-corpus-query.md#qp10) independently freezes actual backend IDs after ingest, and [QP11](structured-corpus-query.md#qp11) measures the executable path before final acceptance. MP6 does not wait for the later ID lock. QP2 independently gates production opt-in/re-ingest/rollback. Q-ID (QP3) and E-SOURCE (EP1) supply immutable identity/input primitives; EP2 supplies grant protection, EP8 supplies durable events, and EP9–EP10 supply fin scrub/recovery acknowledgements. Only MP17 waits for fin acknowledgement; backend GC does not depend on EP11. Final MP18 uses QP8 and EP7/EP11, without waiting for final QP9 or EP12. The [integrated dependency table](structured-corpus-query.md#integrated-execution-dependencies) resolves these prerequisites. the tracked change–the tracked change do not defer immediate visibility guards, finite collection, or stable identity. Legacy ADRs 0091–0093 and the tracked change obligations remain intact; missing origins are never inferred.
**Per-ticket loop (implementation bullets):** TDD (RED against the independent expected result → minimal GREEN) → focused package tests and typecheck/build → `arthurpower:verification-before-completion`. Existing regressions only: iknow `npm test`, `npm run typecheck`, `npm run build`; fin `uv run pytest -q`, `uv run --no-sync pyright` when shared consumer paths change. MP1 must select and record the future M acceptance driver and exact command before behavior code; no new test path or flag is prescribed here. A live or real-corpus result is never inferred from these regressions.

## Architecture change review

bounded-context-guardian: yes — lifecycle, source history, jobs, decisions, and collection stay inside iknow's existing corpus/maintenance ownership; fin communicates only through its retrieve port, and no package or cross-repository import is added.
input-contract-tests: yes — the slices allocate empty/malformed, stale/negative, bounded/overflow, duplicate/concurrent, and exception/outage cases for schedule, retry, publication, decisions, readers, collection, and purge; M1–M20 remain the acceptance matrix.
error-handling-enforcer: yes — typed work failures remain distinct from empty success; expired fences, missing dependencies, unreachable owners, and partial deletion stay visible as failed/pending states, and purge completion is never reported before its phase finishes.
complexity-anti-drift: yes — each ticket owns one lifecycle transition or read boundary; extraction, conflict checks, projection rebuild, normal collection, and explicit purge remain separate work kinds and outcomes, with no generic scheduler or monolithic lifecycle function planned.
minimal-change-verifier: yes — one maintenance capability in the existing package; Query and Evidence plans own their wire consumers; QP2 owns rollout migration; UI, capacity policy, backup erasure, semantic merge/split, and detailed the tracked change reconciliation stay out of this plan.

## Milestone imports and exports

| Milestone | Direction | Owner and contract |
|---|---|---|
| `QP1`, `QP10`, `QP11` shared acceptance pack | import | Query owns original inputs/oracle, post-ingest ID mapping, and measured bounds in separate tickets; early Maintenance work consumes only QP1. |
| `QP2` rollout decision | import | Query plan owns the shared opt-in migration/re-ingest/rollback decision; required before existing-corpus rollout, not before `MP6` publication. |
| `Q-ID` | import | Query plan supplies persistent entity/assertion identity registration and exact tuple identity to publication and support compilation. |
| `E-SOURCE` | import | Evidence plan supplies immutable source-version, extraction, occurrence/support, locator, and evidence-dependency primitives. |
| `EP2` grant inputs | import | Evidence plan supplies row/count grant dependency semantics used by normal collector eligibility; this is a bounded grant primitive, not the full fin expiry lifecycle. |
| `EP8` invalidation event | import | Evidence supplies the event capability before MP16 atomically enqueues events with the backend purge request; it does not wait for completed purge. |
| `EP9–EP10` scrub/recovery acknowledgements | import | Evidence plan owns fin snapshot scrubbing and recovery acknowledgements. `MP17` consumes the acknowledgements without making EP9/EP10 wait on all-owner completion. |
| `EP11` fin-local expiry | integration | Evidence plan consumes `M-COLLECT` deadlines/outcomes for fin-local expiry. MP13–MP16 do not depend on EP11. |
| `QP8` host retrieve wire | final acceptance input | MP18 exercises the integrated host retrieve path; it does not wait for final QP9 or EP12 acceptance. |
| `M-PUBLISH` (`MP6`) | export | One accepted source-version/extraction pair and its exact active supports/current compilation become visible atomically. Query and Evidence consume this backend boundary, not fin wire types. |
| `M-STATUS` (`MP10`) | export | A bounded summary for the caller's authorized source scope, read with query data from one consistent snapshot. Query places this summary in its result. |
| `M-COLLECT` (`MP13`, `MP15`) | export | Finite normal-GC deadlines, backend deletion phase, and local outcome for EP11 fin-local expiry. |
| `M-PURGE` (`MP16`) | export | Backend purge request, immediate read/reference denial, physical-delete phases, and persisted local outcome. It is independent of fin owner acknowledgement. |
| `M-OWNER-COMPLETE` (`MP17`) | export | All-owner completion is true only after the backend outcome and Evidence-owned fin scrub/recovery acknowledgement agree. |

## M1–M20 ticket coverage

| Acceptance case | Ticket | Acceptance outcome |
|---|---|---|
| M1 | MP2 | Restart recovers one durable job; query has no write side effect. |
| M2 | MP5, MP6 | Old publication serves while refresh is pending/failed; accepted replacement switches atomically. |
| M3 | MP7 | Withdrawing one origin preserves another; withdrawing the last removes current visibility and retains allowed history. |
| M4 | MP4 | A-to-B-to-A, ruleset, or dependency changes fence stale attempts and intermediate writes. |
| M5 | MP6 | Duplicate/parallel extraction preserves exact ID and all distinct supports. |
| M6 | MP4 | Lease expiry invalidates the old token before a retry can write, publish, or pin collection. |
| M7 | MP3 | Attempt history is retained; transient retry bounds and invalid-input refusal are exact. |
| M8 | MP8 | Structural candidates follow the versioned screen; no truth adjudication or newest-wins mutation occurs. |
| M9 | MP9 | Stale decisions refuse; current decisions retain evidence and advance query revision when visibility changes. |
| M10 | MP10 | Progress advances maintenance revision without invalidating a cursor; data/access changes advance read revision. |
| M11 | MP10 | No-hit pending sources count in authorized scope; hidden sources do not affect totals. |
| M12 | MP11 | Projection rebuild preserves extraction identity and performs no new extraction. |
| M13 | MP12 | Multi-source derived artifact is suppressed immediately when a required source is withdrawn. |
| M14 | MP13 | Fake-clock retention and grace windows respect jobs, readers, dependencies, and row grants. |
| M15 | MP14 | Reactivation/reference races cancel collection atomically or refuse tombstoned identities. |
| M16 | MP15 | Crash-resumable ordered deletion preserves shared objects and truthful phase outcomes. |
| M17 | MP16 | Explicit purge denies reads immediately and cancels bounded active reads within 60 seconds. |
| M18 | MP6, MP18; EP7 | Failed re-extraction retains the old set; accepted re-extraction retires omissions; old snapshots actually replay their original attribution through the real evidence seam. |
| M19 | MP12 | Preexisting derived hits cannot serve before reconciliation; missing dependency guards fail closed. |
| M20 | MP13, MP17 | Count-only grants do not pin source payloads; explicit purge invalidates scope receipts and waits for owner scrub acknowledgement. |

## Tasks (ordered by dependency)

1. <a id="mp1"></a> **MP1 — Select the M1–M20 acceptance driver and bind the case matrix** — tag: `[decision]`
    - **Inherits:** “Freeze public original inputs and an independent expected timeline before implementing behavior.” (`specs/structured-corpus-maintenance.md`, Success Criteria)
    - **Surface:** iknow's existing package test and real-acceptance workflow; QP1 supplies and owns the source pack and oracle.
    - **Acceptance:** Before behavior implementation, record the existing or newly selected versioned driver, exact command, exit-code contract, real-boundary prerequisites, and independent expected M1–M20 outcomes using QP1's original corpus/oracle. Assign each applicable empty/invalid, negative/overflow, duplicate/concurrent, permission, and exception class from the input-boundary table below to a owning implementation ticket; justify N/A against the actual public input shape. Do not invent test paths, flags, IDs, or claim the driver already ran.
    - **Completion:** A reviewer can map every M case to a planned ticket and to a concrete future invocation without mistaking unit regressions for live acceptance.
    - **Dependencies:** Requires QP1; unblocks MP2–MP18 behavior tests.
    - **Status:** [ ] pending

2. <a id="mp2"></a> **MP2 — Recover one durable job after scheduling or restart** — tag: `[implementation]`
    - **Inherits:** “Repeated scheduling of the same key joins one job.” (`specs/structured-corpus-maintenance.md`, Durable work and publication fences)
    - **Surface:** iknow's existing durable job/outbox and query read path.
    - **Acceptance:** The persisted job key binds work kind, source reference, source version/generation, input revision, and config/ruleset revision. Scheduling the same key twice joins one recoverable job after restart; distinct dependency vectors/config inputs cannot join it. Querying performs no lifecycle write. Verify malformed/empty identities, unsupported work kinds, applicable numeric bounds, concurrent duplicates, and storage failure with the SPEC's typed exits; no invalid input creates a success-shaped job. M1 passes against MP1's driver.
    - **Completion:** Duplicate schedules and a crash between schedule and work cannot create a second logical job or a query-triggered mutation.
    - **Dependencies:** Requires MP1; unblocks MP3, MP4, and MP5.
    - **Status:** [ ] pending

3. <a id="mp3"></a> **MP3 — Bound retries and preserve attempt history** — tag: `[implementation]`
    - **Inherits:** “Automatic transient retry permits at most 3 attempts per job, delayed by 5 then 30 seconds; after exhaustion expose `failed`.” (`specs/structured-corpus-maintenance.md`, State and recovery)
    - **Surface:** iknow's existing job state and worker retry path.
    - **Acceptance:** Transient failures use only the two specified delays and end in visible `failed` after three attempts; explicit retry creates a new attempt without erasing prior attempts; validation/config failures do not auto-loop, and resource exhaustion remains a typed failure. M7 passes with a fake clock.
    - **Completion:** Each retry outcome is bounded, attributable to an attempt, and distinguishable from successful publication.
    - **Dependencies:** Requires MP2; unblocks MP4 and MP5.
    - **Status:** [ ] pending

4. <a id="mp4"></a> **MP4 — Fence staged writes and worker leases by the full dependency revision** — tag: `[implementation]`
    - **Inherits:** “Publication guards must apply to intermediate check/projection writes as well as final status updates.” (`specs/structured-corpus-maintenance.md`, Durable work and publication fences)
    - **Surface:** iknow's existing worker claim, stage writes, and publication guard.
    - **Acceptance:** Each attempt binds its number, unguessable token, five-minute lease, heartbeat interval no greater than one minute, 30-minute absolute limit, source reference/version/generation, desired source and extraction revisions, accepted extraction/input revision, config/ruleset revision, and the complete sorted source/generation/extraction/affected-relation-bucket dependency vector. Every stage/final write rechecks the live inputs, token, and lease. Reclaim invalidates the old token before a new claim; it can no longer heartbeat, write data/status, publish, or pin collection. A-to-B-to-A, dependency-B update, lease-expiry, config/model supersession, and incomplete dependency cases refuse without mutation (M4, M6).
    - **Completion:** Every intermediate and final write proves the same live token and complete current inputs; incomplete dependency metadata never reaches `ready`.
    - **Dependencies:** Requires MP2 and MP3; unblocks MP5, MP8, MP11, and MP13.
    - **Status:** [ ] pending

5. <a id="mp5"></a> **MP5 — Stage desired source state while the published version keeps serving** — tag: `[implementation]`
    - **Inherits:** “v2 replaces published v1 only after successful staged extraction/validation/publication; failure keeps v1 serving with explicit pending/failed refresh status.” (`specs/structured-corpus-maintenance.md`, Assumption gate)
    - **Surface:** iknow's existing source lifecycle, accepted extraction, and query visibility.
    - **Acceptance:** `desired_version` / desired extraction revision can advance without changing the published pair; v1 stays queryable while v2 is pending or failed, and the desired status reports that state. Staged source/extraction writes are isolated from serving records and bound to `E-SOURCE` identity.
    - **Completion:** A failed or in-flight replacement cannot partly alter the current result set or claim publication readiness (M2, first half).
    - **Dependencies:** Requires MP1 and MP4 plus E-SOURCE; unblocks MP6, MP7, and MP10.
    - **Status:** [ ] pending

6. <a id="mp6"></a> **MP6 — Publish one accepted extraction and exact active support set atomically (`M-PUBLISH`)** — tag: `[implementation]`
    - **Inherits:** “pointer switch, support activation/retirement, affected entity/assertion compilation, query revision, status, and outbox scheduling commit together.” (`specs/structured-corpus-maintenance.md`, Source lifecycle and current compilation)
    - **Surface:** iknow's existing publication transaction, identity registration, and current structured view.
    - **Acceptance:** Using `Q-ID` and `E-SOURCE`, a successful publication atomically selects one source-version/extraction pair and compiles its exact active support set; failure leaves the prior pair current. Parallel/duplicate extraction preserves one exact assertion ID and every distinct support; re-extracting the same version retires omitted occurrences, retains shared support IDs, advances the extraction revision, and preserves history (M2, M5, M18). Export the accepted current-set boundary as `M-PUBLISH` without coupling it to Query/Evidence fin wire code.
    - **Completion:** Readers see only the old accepted set or the new accepted set, never a partially switched set; ordinary publication and rebuild preserve Q-ID identities.
    - **Dependencies:** Requires MP4, MP5, Q-ID, and E-SOURCE; unblocks MP7, MP8, MP10, MP11, and MP12.
    - **Status:** [ ] pending

7. <a id="mp7"></a> **MP7 — Withdraw one source without retiring another source's support** — tag: `[implementation]`
    - **Inherits:** “An assertion remains current while at least one active authorized support remains; when the last disappears it leaves current query results.” (`specs/structured-corpus-maintenance.md`, Source lifecycle and current compilation)
    - **Surface:** iknow's existing source lifecycle and exact support-set compiler.
    - **Acceptance:** Withdrawal increments the source generation and removes that source's support from current visibility immediately; a second active origin keeps the assertion current, while withdrawal of the final origin removes it from list/count visibility and preserves retained historical evidence (M3).
    - **Completion:** Current membership follows exact active-set membership, never a truncated display list or saturated counter, and withdrawal does not label the assertion false.
    - **Dependencies:** Requires MP6, Q-ID, and E-SOURCE; unblocks MP10, MP12, MP13, and MP16.
    - **Status:** [ ] pending

8. <a id="mp8"></a> **MP8 — Produce versioned structural alternatives without adjudicating truth** — tag: `[implementation]`
    - **Inherits:** “Checks publish immutable evidence and status, not automatic retirement or truth selection.” (`specs/structured-corpus-maintenance.md`, Conflict candidates and readiness)
    - **Surface:** iknow's existing durable work and relation-maintenance boundary.
    - **Acceptance:** The revision-1 `structural-alternatives` check produces potential candidates for different tails with overlapping half-open intervals, records unknown/unbounded uncertainty, excludes exact duplicates/non-overlap/known multi-valued relations, and leaves the initially empty relation catalog empty. No confidence, recency, or default newest-wins write changes visibility (M8).
    - **Completion:** Candidate identity binds the sorted assertion IDs, accepted dependency/support revisions, rule revision, and uncertainty; `ready` means only that this structural screen completed.
    - **Dependencies:** Requires MP4 and MP6; unblocks MP9 and MP10.
    - **Status:** [ ] pending

9. <a id="mp9"></a> **MP9 — Apply only a current, recorded maintenance decision** — tag: `[implementation]`
    - **Inherits:** “Accept only currently matching inputs in a transaction; retain the decision's evidence and increment query revision when current visibility changes.” (`specs/structured-corpus-maintenance.md`, Conflict candidates and readiness)
    - **Surface:** iknow's existing maintenance decision and source/support lifecycle.
    - **Acceptance:** A decision carries its ID, actor, reason, affected IDs, expected input revisions, outcome, and timestamp. Empty/malformed/unknown decisions and denied actors refuse; stale or concurrent conflicting revisions do not mutate visibility. Current `keep_both`, `retire_support`, or `supersede_assertion` retains original evidence and changes only explicitly selected visibility; a visibility change atomically advances `read_revision` (M9).
    - **Completion:** The result is an auditable versioned decision, never semantic adjudication inferred from a model or candidate score.
    - **Dependencies:** Requires MP6 and MP8; unblocks MP10.
    - **Status:** [ ] pending

10. <a id="mp10"></a> **MP10 — Export a same-scope, consistent maintenance summary (`M-STATUS`)** — tag: `[implementation]`
    - **Inherits:** “Rows, count, and this summary come from one consistent authorized read snapshot.” (`specs/structured-corpus-maintenance.md`, Response status)
    - **Surface:** iknow's existing authorized query/read transaction and maintenance status projection.
    - **Acceptance:** `M-STATUS` returns the complete specified summary for the authorized active-source scope, including sources with no matches; each scoped source contributes one desired-index and one conflict-check bucket; hidden sources do not affect exposed totals, and missing required status refuses with `maintenance_status_unavailable`. Progress changes only `maintenance_revision`; query-visible changes use `read_revision` and invalidate continuation as specified (M10, M11).
    - **Completion:** Query can call the summary within its existing consistent read and serialize the unchanged contract; this milestone contains no fin result-wire implementation.
    - **Dependencies:** Requires MP5–MP9; unblocks the Query consumer and MP18.
    - **Status:** [ ] pending

11. <a id="mp11"></a> **MP11 — Rebuild derived projections without running extraction again** — tag: `[implementation]`
    - **Inherits:** “`projection_rebuild` consumes persisted accepted extraction, supports, and recorded decisions.” (`specs/structured-corpus-maintenance.md`, Rebuild versus re-extract)
    - **Surface:** iknow's existing projection outbox and derived index workers.
    - **Acceptance:** A projection rebuild uses accepted extraction/support/decisions, preserves exact identities, and performs zero new assertion extraction. Every derived generation, including reports and indices, persists complete dependency revisions and its publication revision before serving; a dependency change makes it stale for MP12's guard. An explicit parser/model/rules change schedules separate re-extraction with a new extraction revision (M12).
    - **Completion:** Operators and tests can distinguish projection repair from a new interpretation of source content.
    - **Dependencies:** Requires MP4 and MP6; unblocks MP12 and MP13.
    - **Status:** [ ] pending

12. <a id="mp12"></a> **MP12 — Suppress derived artifacts as soon as a dependency is no longer serveable** — tag: `[implementation]`
    - **Inherits:** “Consumers must authorize and check dependencies against canonical lifecycle state before serving; otherwise suppress the whole artifact or return a typed unavailable/degraded result.” (`specs/structured-corpus-maintenance.md`, Rebuild versus re-extract)
    - **Surface:** iknow's existing ES/vector/graph retrieval and report consumers, through the shared package contract.
    - **Acceptance:** Before outbox reconciliation, a hit or multi-source report depending on a withdrawn/purged sole source is suppressed as a whole; absent/incomplete dependency metadata refuses with typed unavailable/degraded status rather than returning stale content or citeable evidence (M13, M19). External reconciliation remains the tracked change; the immediate visibility guard is implemented here.
    - **Completion:** A stale derived artifact cannot re-enter a response merely because deletion/reindex work is pending.
    - **Dependencies:** Requires MP6, MP7, and MP11; unblocks MP13 and MP18.
    - **Status:** [ ] pending

13. <a id="mp13"></a> **MP13 — Mark only normally collectible payloads at finite deadlines** — tag: `[implementation]`
    - **Inherits:** “Collection is mark, wait 7 days, recheck, delete.” (`specs/structured-corpus-maintenance.md`, Physical collection)
    - **Surface:** A new finite-collection capability in iknow's existing maintenance/storage boundary, using the shared source lifecycle and evidence-grant inputs.
    - **Acceptance:** A fake-clock case proves the 7/90/365-day eligibility bounds and the seven-day grace: current knowledge, bounded jobs/readers, and unexpired row-content grants block source collection; expired grants stop protecting content without renewal; count-only grants protect their receipt only (M14, M20 portion).
    - **Completion:** A collector mark records the expected lifecycle revision/deadline and stops new obligations against its candidate; eligibility and physical deletion remain distinct events.
    - **Dependencies:** Requires MP4, MP7, MP11, MP12, E-SOURCE, and EP2 grant inputs; unblocks MP14 and MP15. It does not require EP11 fin-local expiry.
    - **Status:** [ ] pending

14. <a id="mp14"></a> **MP14 — Cancel collection atomically when a candidate is reactivated or referenced** — tag: `[implementation]`
    - **Inherits:** “A requested reactivation must transactionally cancel the mark before creating dependencies.” (`specs/structured-corpus-maintenance.md`, Physical collection)
    - **Surface:** The new iknow finite-collection capability plus source activation and reference-creation transactions.
    - **Acceptance:** A concurrency test races reactivation/reference creation against mark and post-grace recheck; the transaction either cancels collection before adding the dependency or refuses a tombstoned identity, never deleting a newly protected payload or resurrecting a tombstone (M15).
    - **Completion:** Recheck uses the current lifecycle revision and reference/lease set, not the earlier collector scan.
    - **Dependencies:** Requires MP13; unblocks MP15 and MP16.
    - **Status:** [ ] pending

15. <a id="mp15"></a> **MP15 — Delete a normal-GC candidate through retryable tombstone phases** — tag: `[implementation]`
    - **Inherits:** “Commit a purge tombstone before removing payloads. Delete dependent supports, extraction payloads, frozen units/objects, superseded caches, and snapshot payloads in dependency order. Persist progress for retry.” (`specs/structured-corpus-maintenance.md`, Physical collection)
    - **Surface:** The new iknow finite-collection capability and shared physical-object/reference accounting.
    - **Acceptance:** A crash at every persisted delete phase can safely resume; a completed payload phase is not deleted twice unsafely; shared bytes remain while any logical source references them; brief non-content audit outcome survives; no success is recorded before deletion is complete (M16).
    - **Completion:** A retry reaches one truthful terminal outcome without stale workers recreating collected identities.
    - **Dependencies:** Requires MP13 and MP14; unblocks MP16 and EP11 through M-COLLECT.
    - **Status:** [ ] pending

16. <a id="mp16"></a> **MP16 — Expose backend explicit purge request, immediate denial, and local completion (`M-PURGE`)** — tag: `[implementation]`
    - **Inherits:** “Explicit purge skips normal age/grace delays, immediately denies new reads and references, cancels affected jobs, and waits at most the bounded active-read execution before physical removal.” (`specs/structured-corpus-maintenance.md`, Physical collection)
    - **Surface:** The new iknow backend purge primitive over its source lifecycle, read guards, worker cancellation, and MP15 deletion phases.
    - **Acceptance:** In one authoritative purge transaction, tombstone the source, advance the scope purge revision, deny affected current/historical reads and new grants, cancel jobs, and enqueue EP8's durable content-free per-consumer invalidation events for affected grants. Malformed/empty scopes and denied callers refuse before mutation; duplicate/concurrent requests join retryable persisted phases. Active-read cancellation finishes within 60 seconds; physical phases are idempotent and report a separate backend-local outcome (M17), which never waits for fin acknowledgement.
    - **Completion:** A backend-local purge result never claims all-owner completion while fin snapshot payloads are still outstanding.
    - **Dependencies:** Requires MP14, MP15, and EP8's event capability; unblocks EP9–EP10 purge integration and MP17. EP8 is independently implementable before purge and does not wait for MP16; backend-local deletion does not wait for fin acknowledgement.
    - **Status:** [ ] pending

17. <a id="mp17"></a> **MP17 — Close all-owner purge only after fin scrub and recovery acknowledgement** — tag: `[implementation]`
    - **Inherits:** “No successful completion is recorded while copied live snapshot payloads remain unacknowledged.” (`specs/structured-corpus-maintenance.md`, Physical collection)
    - **Surface:** iknow's persisted purge outcome plus the Evidence plan's owner-invalidation/scrub/recovery boundary.
    - **Acceptance:** Explicit purge advances the shared scope purge revision and stays pending until Evidence-owned `EP8` invalidation and `EP9–EP10` scrub/recovery acknowledgement confirm no copied snapshot payload remains. Backend-local deletion can be complete while all-owner completion remains pending (M20); normal-GC treatment of count-only grants is proven in MP13.
    - **Completion:** Backend deletion and fin snapshot erasure have separate durable outcomes, and an unavailable owner can be reconciled after restart without falsely completing the purge.
    - **Dependencies:** Requires MP16, EP8, and EP9–EP10; unblocks MP18 and the Evidence plan's final acceptance. It does not require EP11 or EP12.
    - **Status:** [ ] pending

18. <a id="mp18"></a> **MP18 — Close M1–M20 only on full measured acceptance** — tag: `[implementation]`
    - **Inherits:** “The real acceptance corpus and resource measurements are implementation prerequisites, with no claimed live acceptance pass.” (`specs/structured-corpus-maintenance.md`, Open Questions)
    - **Surface:** MP1's recorded real-acceptance driver, the integrated iknow maintenance path, and its fin retrieve/evidence boundary.
    - **Acceptance:** On QP1's originals/oracle, QP10's reviewed IDs, and QP11's measured bounds, run every M1–M20 case on the integrated candidate and record command, revisions, expected/observed result, and exit. M18 must actually replay an old snapshot through EP7 after successful same-source re-extraction, proving original attribution and no implicit new query. All 20 must pass; any missing input, unavailable live dependency, fail, skip, or unrun case leaves MP18 pending. A blocker report alone is not completion.
    - **Completion:** MP18 closes only with 20/20 measured passes and current revision-bound evidence; this does not amend the tracked change or authorize rollout before QP2.
    - **Dependencies:** Requires MP1–MP17, QP10, QP11, QP8 host retrieve wire, EP7 replay, and EP11 fin-local expiry. It does not require final QP9 or EP12 acceptance.
    - **Status:** [ ] pending

## Input-boundary verification allocation

MP1 freezes these cases against actual public entry shapes before implementation. Applicable numeric cases use the SPEC's finite counters, fixed deadlines, and declared resource bounds; opaque revisions are not numbers and numeric classes require an explicit N/A rationale where absent. This introduces no adjustable lease or undocumented limit. Invalid requests leave canonical state unchanged and use explicit typed codes, including `invalid_input`, `access_denied`, `input_stale`, `lease_lost`, `resource_exhausted`, `backend_unavailable`, and `execution_timeout` as applicable.

| Entry and owner | Empty / invalid | Negative / overflow | Concurrent / exception / access |
|---|---|---|---|
| Schedule/retry — MP2–MP3 | Missing source/version/key or unsupported kind/config refuses | Invalid attempt/counter values cannot wrap or bypass fixed retry bounds | Duplicate schedules join; storage failure persists failure; denied actor cannot schedule |
| Worker/read leases — MP4 | Missing token/dependency metadata refuses | Invalid lease/deadline and expired/absolute bounds cannot be extended | Lost old token cannot heartbeat or write; reclaim and read cancellation races are bounded |
| Resolution — MP9 | Empty actor/reason/affected IDs or unknown outcome refuses | Opaque revision inputs are not numerically coerced; justify N/A for numeric classes absent from this shape | Stale/concurrent decisions refuse; denied actor cannot retire support |
| Status/read — MP10 with QP5–QP7 | Missing required state returns maintenance_status_unavailable; zero-hit valid scope succeeds | Bucket/count values remain finite safe integers and totals match scope | Snapshot interleavings remain consistent; hidden scope and outage never become empty success |
| Collector — MP13–MP15 | Missing lifecycle/dependency/reference metadata cannot produce eligibility | Invalid clock/deadline/accounting values cannot make deletion early; fake clock proves boundaries | Reference/reactivation/lease races recheck; crash persists phase; collection respects authoritative owner scope |
| Purge — MP16–MP17 | Empty/malformed target or request refuses before mutation | Invalid revision/deadline values cannot bypass bounded active-reader execution | Unauthorized caller refuses; duplicates/retries are idempotent; offline fin leaves final outcome pending |

## Scope and legacy boundary

The existing fin index-job tracker is an ingest-side search-index handoff, not the source-version publication, conflict-check readiness, or finite collector contract in this plan. The existing iknow mutable source rows, single-origin links, and recency supersession path remain legacy behavior; this plan adds an isolated opt-in capability and does not rewrite old origins from assumptions. QP2 is the only owner of the existing-corpus rollout/re-ingest/rollback decision. Keep `database_rows` and its ADR 0091–0093 digest/schema behavior unchanged. Do not include a maintenance UI, new scheduler, model adjudicator, full entity merge/split workflow, storage-capacity policy (the tracked change), backup erasure/restore (the tracked change), semantic identity workflows (the tracked change), or the detailed external-store reconciliation internals (the tracked change); the immediate serve-time lifecycle guard in MP12 remains required.

## Final review gate

After every Q/E/M implementation ticket and MP18 reaches its actual acceptance outcome, run one end-of-round `arthurpower:code-review` across the integrated changes. If its gate is blocked, the next slot is `arthurpower:review-report-repair`; then run `arthurpower:verification-before-completion` and commit locally under the project rules. Do not push without explicit authorization.
