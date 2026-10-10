# Plan P1 — Structured query production: reader, first page, cursor, count, wire

**Goal:** Make the structured Query path executable end to end so Q1–Q13 can run on a real candidate: an authorized read UOW that returns a first list page, deterministic cursored continuation, and an exact same-snapshot count with a bounded receipt, wired through Fin's existing retrieve surface to a real IK producer.
**Authority:** [Query spec](../specs/structured-corpus-query.md), [Query plan](structured-corpus-query.md), and the execution plan's T18–T22/T40/T41 (`structured-corpus-completion-execution.md`) remain the acceptance authority. This plan sequences the work; it does not restate the normative criteria or invent acceptance paths.
**Why a separate plan:** the execution plan's Query tickets were written before the transport slice landed. Two of their premises are now false, and re-deriving them wasted the previous session's time. The corrections are recorded below, not silently absorbed.

**Goal of the previous phase, for reference:** T13/T14 (private retained-grant read and support continuation) are evidenced on IK `feat/structured-ep-private-read`; T40's reader contract is evidenced but its resolver question is partly answered. This plan starts from those.

## Corrections to the execution plan's Query premises

Three statements in T40's status are stale at the current refs. Verified this session:

1. **"The resolver does not exist."** False at `5a47db8`. `src/core/storage/evidence-binding.ts:206-219` `resolveEvidenceScopeContext` is a production resolver, and `src/index.ts:626-640` is its only production mint point (`createEvidenceDispatcher({authority, binding: process.env})` → `setRequestHandler`). It is named for Evidence, and whether Query reuses it or gets its own is still **undecided** (P1-D1 below) — but it is not absent.
2. **"The read UOW's only use is the internal routing probe at `:580`."** Stale. `PgMaintenanceStatusStorage.readMaintenanceStatus` (`structured-status-reads.ts:281-287`) is a second `REPEATABLE READ READ ONLY` user, and it is exactly the shape a Query reader needs.
3. **"No Query driver exists."** Still true, and unchanged: there is no `structured-query-pg.test.ts` and nothing under `src/core/structured/` beyond errors/request/identity/identity-store/evidence-subject.

The dead `withAuthorizedScope` free function (`structured-scope-uow.ts:90`) is confirmed dead in `src/` — all `src/` callers use the interface method. The Query reader must use `runTransaction({readOnly:true})` (as `PgMaintenanceStatusStorage` does), **not** `withAuthorizedScope`, which takes write locks and advances revisions.

## What already exists (do not rebuild)

| Capability | Location | Note |
|---|---|---|
| Identity binding + mint | `evidence-binding.ts:106-176, 206-219`; `src/index.ts:626-640` | 7 env vars, closed set, no fallback |
| Scope authority | `structured-scope-types.ts:153` WeakMap; `:227-238` refuses unminted | unforgeable |
| Read-only transaction | `structured-scope-uow.ts:419-421` via `runTransaction({readOnly:true})` | used by MP10 reader |
| Accepted-snapshot reads | `structured-scope-reads.ts:287-310` `buildPgAcceptedSnapshot` | sources / memberships / extractions |
| MP10 status | `structured-status-reads.ts:188` `buildPgMaintenanceStatus`, `:265`/`:324` the storage classes; `maintenance-status.ts:381` `buildMaintenanceStatus` | one assembly behind all three; the **classes** are still not registered outside tests, but the reader now consumes the free functions (see the P1-I2 amendment) |
| Grant issue | `evidence-pg.ts:570-705` `issueGrantRows`; backends `pgvector-backend.ts:2082`, `memory-backend.ts:1361` | **no production caller** |
| Grant read | `evidence-pg.ts:707-799`; `evidence-store.ts:283-338` `decideGrantRead` | production caller = Evidence dispatch only |
| Support continuation | `evidence-continuation.ts:435` `decideSupportContinuation` | wired to dispatcher by #363 (this phase) |
| Fin request/result carrier | `tool.py:41,116`; `mcp_adapter.py:1070`; `graph.py:1124,1217` | QP8 transport complete; no change needed |
| Fin structured validation | `structured_evidence_validation.py`, `structured_snapshot.py`, `structured_evidence.py` | EP5/EP6 assets |
| UUID-keyset SQL shape | `entity-projection.ts:58-79, 100-115` | pagination shape only — no token, no binding |

## What must be built

| Gap | Category |
|---|---|
| Query reader mint point / resolver ownership (decide reuse vs. separate) | decision |
| `structured-query-reads.ts`: PG + memory readers over `AuthorizedScopeUnitOfWork & ScopeTransactionOwner` | new |
| Register `PgMaintenanceStatusStorage` / `MemoryMaintenanceStatusStorage` on the backend or `ServerDeps` so the reader reuses MP10 rather than reimplementing it — **superseded**: the reader shares the free function `buildPgMaintenanceStatus` / `buildMaintenanceStatus` instead, which meets the item's purpose by a different route. See the P1-I2 amendment | wiring |
| QP5 first page: distinct subjects ascending by UUID bytes, deterministic attributed display, ≤8 inline provenance, same-snapshot status | new |
| QP6 cursor: encode + bind normalized query, subject, scope, `read_revision`, fixed 10-minute expiry; four typed failures | new |
| QP7 count: `COUNT(DISTINCT result_id)` in the same read UOW, zero included, count receipt, `coverage: stored_structured_subjects` | new |
| Grant/receipt issuance inside the Query read transaction; continuation ref for provenance | wiring |
| Replace IK's hard-coded refusal at `src/tools/retrieve.ts:263-269` with the real producer emitting `iknow.structured_result` | new + wiring |
| Fin live Query driver | new |

## Ordered work

Each item is one narrow outcome with its own boundary. Dependencies are strict; do not start an item before its predecessor is evidenced.

### P1-D1 — Decide the Query reader's mint point (decision)

**Question:** does Query reuse the Evidence dispatcher's mint seam (`src/index.ts:626-640`, env `IKNOW_EVIDENCE_*`) or get a separate Query composition seam?
**Sub-question (carried from T40):** is `actorId` per-session (as B1 decided for Evidence) or per-process for Query?
**Output:** a decision record under `docs/validation/`, mirroring the B1 record's shape (decision, why, what would overturn it). No code.
**Why it blocks:** everything else needs the context object this produces.

### P1-I1 — Query read storage, PG and memory (implementation)

New `src/core/storage/structured-query-reads.ts`. Constructor takes `AuthorizedScopeUnitOfWork & ScopeTransactionOwner` — the same pairing `PgMaintenanceStatusStorage` uses. Entry validates `scopeAuthorityOf(context) !== this.scope` (refuse) and narrows to `authorizedSubset`, then does all work inside one `runTransaction(…, {readOnly: true})`: one accepted snapshot + one maintenance status + one page/count result, so list, status and receipt share a revision.
Boundary: strict real-PostgreSQL driver plus a memory twin over the same contract; both lanes assert the same cases, not two similar suites. Marker-absent must refuse by name.

### P1-I2 — Register the MP10 status storage (implementation, small)

> **Amended 2026-10-07 — the delivered route, and the deviation.** As written below, this
> item promised to promote `PgMaintenanceStatusStorage` / `MemoryMaintenanceStatusStorage`
> to an injectable dependency so P1-I1 would *call* them. **Neither happened.** Verified at
> iknow-memory `09c6557` (the P1 lane tip, after #296 and #297): `grep -rn "new
> PgMaintenanceStatusStorage\|new MemoryMaintenanceStatusStorage" src/` returns **zero
> hits**. The classes are declared at
> `src/core/storage/structured-status-reads.ts:265` and `:324`, and their only importers in
> the whole tree are that same module plus two test files —
> `tests/acceptance/structured-maintenance-status-pg.test.ts:837` and
> `tests/storage/maintenance-status-memory.test.ts:89`. They remain test-only.
>
> **The shared surface actually used is the free function beneath them, not the classes.**
> The reader `src/core/storage/structured-query-reads.ts` imports and calls
> `buildPgMaintenanceStatus` at `:1495` (PG lane) and `buildMaintenanceStatus` at `:1605`
> (memory lane), both defined in `structured-status-reads.ts:188` and
> `src/core/maintenance/maintenance-status.ts:381`. `buildPgMaintenanceStatus` delegates to
> `buildMaintenanceStatus` at `structured-status-reads.ts:206`, so there is one bucket
> assembly behind all three entry points.
>
> **Deviation recorded, not hidden:** the item's *purpose* — do not implement MP10 twice —
> is met, and met better than the promotion would have been, because the classes and the
> reader now share one function rather than sharing an injected instance. The *mechanism*
> was not delivered, so the sequencing rule this item established ("P1-I2 must precede
> P1-I4") was never load-bearing; nothing downstream depended on the injection. The
> promise is retired rather than carried as an open item, because the work it asked for is
> superseded, not missing. This is the same finding the closure verdict logs as M7.

The original text is retained as written, so the plan still records what was promised:

Promote `PgMaintenanceStatusStorage` / `MemoryMaintenanceStatusStorage` from test-only imports to an injectable dependency on the backend or `ServerDeps`, so P1-I1 calls it rather than copying the bucket assembly. **Do this before P1-I4**, or MP10 ends up implemented twice.

### P1-I3 — First page (implementation, QP5)

Per Q1–Q6/Q12: one row per authorized subject, ascending by UUID bytes; deterministic attributed display (smallest support UUID by UTF-8 byte order of `(name, type)`); exact `eq`/`in` filters and allowed one-hop direction with no two-hop traversal; bounded provenance with a continuation ref; finite row grant; same-scope status; no wall-clock validity filtering. Missing/inconsistent status, unavailable backend, unreadable grant, or oversized mandatory row returns the typed code with no partial success.

### P1-I4 — Cursor (implementation, QP6)

Per Q7: encode and bind normalized query, subject, owner/reader scope, `read_revision`, and a fixed 10-minute expiry from the first page. Continue-page rechecks authorization and revision. Typed failures: malformed/unauthenticated, expired, changed query/scope/revision, unauthorized, timeout, irreducibly oversized. Progress-only status changes must not invalidate a cursor; data or access changes must. The evidence-continuation cursor is **not** a reusable precedent — it binds a frozen grant's membership, not a query and a revision.

### P1-I5 — Count and receipt (implementation, QP7)

Per Q8: count the complete distinct authorized current stored-subject set — not pages, not supports, not top-k — in the same read UOW as list. Zero and over-page-cap both return an exact safe integer, a digest, scope/revision, same-scope status, and a matching bounded count receipt with `coverage: stored_structured_subjects`. Stale revision, incomplete count, expired reader lease, missing status, or oversized response returns the exact typed refusal and no partial count.

### P1-I6 — Grant and receipt issuance inside the read transaction (implementation, wiring)

Call `issueGrantRows` (PG) and the memory equivalent within P1-I1's transaction, so the grant is atomically consistent with the snapshot it describes. Row grants carry the provenance continuation ref. This closes the "grant issuance is EP2's and is not re-proved" limitation T13 recorded, for the Query path only.

### P1-I7 — Real producer at the IK retrieve seam (implementation, wiring)

Remove the unconditional `structured_search_unavailable` throw at `src/tools/retrieve.ts:263-269`; call the reader and emit the `iknow.structured_result` sibling on success, the closed typed error set on refusal. The upstream validation (`wire-filters.ts:256`, `structured/request.ts:207`) already runs and must keep running. **Do not add a model-visible tool** and **do not change the `tools/list` name list** — the B2 review's H1 finding is that the frozen contract cannot see a mode switch inside `filters.structured_query`, so the runtime closed member set (`request.ts:304`) is what holds that line, and a behavioural guard is landing separately under the T39 work.

### P1-I8 — Fin live Query driver (implementation, test)

New `tests/retrieve/test_live_t40_structured_query.py`, following the pin/guard conventions of `test_live_t39_private_route_tools_list.py` (named `BOUND_IKNOW_COMMIT` failure, unique schema, cleanup). Covers success and typed refusals through actual MCP against the real producer. Fin's host/port/adapter are unchanged — this driver proves it.

### P1-D2 — T41 query-checkpoint preflight, then T22 measurement (decision, then measurement)

Only after P1-I8 is green: freeze one exact Fin/IK pair with QP1 oracle and QP10 manifest hashes, run applicable typecheck/build/lint and the changed-surface regressions, and confirm the original implementation review closure for each changed slice (missing closure is `missing`, and blocks). Then T22 measures encoded sizes, page/provenance continuation, exact-count work and execution limits against frozen bounds — the first real measurement of the executable path, and the reference T42 later revalidates.

## Case coverage this plan must satisfy

Q1–Q13 (per the Query spec's success criteria): closed schema validation; identity persistence; duplicate supports do not multiply; sole-support replacement removes the row but keeps history; no implicit wall-clock filtering; exact one-hop filters; gap-free cursor pages; exact counts; maintenance progress does not invalidate a cursor; bounded envelopes with continuation instead of silent truncation; negative capability boundary with no legacy fallback; deterministic attributed display and support-scope isolation; host routing cannot be overridden and descriptive text cannot change predicates. Q2–Q13 all require P1-I3/I4/I5 plus I7; none is satisfiable before the producer exists.

## Sequencing and shared-resource rules

- One implementer owns the Query storage slice at a time; `src/core/storage/` edits, schema and `src/index.ts` composition are serialized.
- P1-D1 must precede all implementation. P1-I2 must precede P1-I4. P1-I3/I4/I5 may proceed in parallel only once I1 and I2 are evidenced, because they share the read transaction and the status dependency.
- Test engineers own disjoint driver files; each run uses a unique schema and removes only what it created.
- Strict drivers must fail by name when the marker or the database is absent, never green-skip.
- This plan adds no model-visible tool, no new package, and no dependency. It does not reopen EP1–EP4, MP4–MP10 or the screening lane.

## Carried-in open items (not decided here)

- Whether an unscreened scope reports normative values or refuses — answered by spec at `specs/structured-corpus-maintenance.md:255`; T17's implementation already matches.
- Per-result direct read: refusal vs. miss — a spec clause, owned by T38; no per-result read surface exists at this layer.
- Multiple `keep_both` on one conflict, and whether a second decision must bind the revision advance caused by the first — owned by T16.
- B2's behavioural `tools/call` guard (H1/H2) lands under the T39 work and must be green before T39 reaches `accepted`; P1-I7 consumes its conclusion.

## Current closeout note (2026-10-07)

This dated status pointer does not change the Query spec, acceptance criteria, or sequencing above.
The integrated P3 prerequisite register and its command-level closeout receipt are
[`structured-corpus-p3-prerequisites.md`](structured-corpus-p3-prerequisites.md) and
[`2026-10-07-p3-prerequisites-closeout.md`](../docs/validation/2026-10-07-p3-prerequisites-closeout.md).

IK's pre-repair candidate contains the Query issuer, but its independent review is blocked on
the authorization/purge-race High. The 33 Query PostgreSQL cases were run before that race was
reproduced; they do not establish acceptance on a repaired final candidate. T40's binding and
the T41/T22/T42 acceptance sequence remain open. The historical P1 review's 9 Medium and 5 Low
findings still need recorded dispositions. Preserve those statuses until the linked records
show their closure.
