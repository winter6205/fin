# Plan P3 — Maintenance collection, purge, and final acceptance

**Goal:** Build the missing lifecycle capabilities — serving guards against withdrawn sources, finite collection with mark/recheck/delete, explicit purge with all-owner completion — and then run the three final acceptance runs (Q1–Q13, E1–E15, M1–M20) on one frozen candidate pair.
**Authority:** [Maintenance spec](../specs/structured-corpus-maintenance.md), [Maintenance plan](structured-corpus-maintenance.md), and the execution plan's T24–T27, T31, T35, T42, T36–T38 remain the acceptance authority.

## The fact that sizes this plan

**T38 is not a test run. It is an arm-building project.** The frozen 20-case driver (`tests/acceptance/t07-m1-m20-coverage.test.ts`) has no skip path by design — a missing arm throws and exits 1. As of this session, **18 of 20 cases are `not-implemented`**; only M1 and M7 have arms. So "MP18 needs 20/20" means 18 arms must be written against modules that mostly do not exist yet. Sizing T38 as "run the suite" would be the same category of error the previous session made elsewhere.

Of the 18:

| Blocked by | Cases |
|---|---|
| P3 modules that do not exist (MP12–MP17) | M13, M14, M15, M16, M17, M19, M20 |
| Modules that exist and are evidenced, but this arm was never written | M2, M3, M4, M5, M6, M8, M9, M10, M11, M12, M18 |
| Cross-phase: also needs P2's EP7 (M18) or EP8–EP10 (M20) | M18, M20 |

M6 and M20 are the two that cross phases: M6's "pin GC" clause needs MP13, and M20 needs MP13, MP17 and Fin's scrub-acknowledgement boundary. **Even a complete P3 does not close M20** — P2's invalidation lane must land first.

## What already exists (do not rebuild)

The infrastructure for collection was pre-laid, which is the one piece of good news here:

| Capability | Location | Note |
|---|---|---|
| Durable work ledger | `maintenance/durable-work.ts`, `service.ts`, `maintenance-work-pg.ts` (904 lines), `schema-maintenance-work.ts` | leases, fencing tokens, attempts, absolute bounds — all of MP4's semantics. `MAINTENANCE_WORK_KINDS` is a closed CHECK; adding `collect_payload` extends it. |
| Lifecycle timestamps | `source_versions.retired_at / payload_collected_at / obligations_stopped_at` (`schema-evidence.ts:54-66`) | all three mutators implemented (`pgvector-backend.ts:2149,2156,2167`); the three meanings are frozen in `types.ts:380-390` |
| Collector clock | `clock.ts:12` — reserved for the collector's day arithmetic | exists |
| Collector index | `evidence_grant_supports_support_id_idx` (`schema-evidence.ts:234`) | pre-embedded for "which unexpired grants hold this support" |
| Withdrawal + revision advance | `publication-pg.ts:295-343` | `read_revision` + `maintenance_revision` +1 each; outbox intents re-armed |
| Purge read-side refusal | `evidence-store.ts:317` `decideGrantRead` refuses on `purge_revision` mismatch | **the refusal mechanism exists; only the transaction that advances it does not** |
| Revision setters | `pgvector-backend.ts:2125-2145`, `memory-backend.ts:1535-1546` | can advance `purge_revision` |
| Scoped reads honor `active` | `structured-scope-reads.ts:116,175,310` | "hidden or withdrawn sources contribute none" — correct already, at the scope-read layer |

The one existing serving guard is at the scope-read layer, not the serving layers. `pgvector-backend.ts:248`'s vector predicate checks only the legacy ledger's `retired = false` and has no join to structured source lifecycle.

## What must be built

| Gap | Category |
|---|---|
| MP12 serving guards on vector / lexical / graph, plus a fail-closed typed status | new |
| MP13 collector: eligibility + 7/90/365-day rules | new (infra ready) |
| `MAINTENANCE_WORK_KINDS` += `collect_payload` | wiring |
| MP14 mark state + atomic mark/reactivation | new |
| MP15 resumable physical deletion phases | new |
| MP16 purge transaction: atomic tombstone, `purge_revision` advance, active-read cancellation, EP8 event emission | new |
| MP17 all-owner completion state machine | new |
| T42 final pair freeze, then T36/T37/T38 | decision then three runs |

## Ordered work

### P3-I1 — MP12 serving guards (implementation)

Before outbox reconciliation, no stale hit from a withdrawn or sole-purged source may be returned or cited, on any of the three serving tiers. A current remaining support stays visible. Missing dependency or lifecycle state refuses serving with a typed unavailable/degraded status rather than serving stale rows. The vector predicate needs a real join to structured source lifecycle; lexical and graph need equivalent treatment. This unblocks M13 and M19 and is a prerequisite for T24's completion clause.

### P3-I2 — MP13 collector eligibility (implementation)

Extend `MAINTENANCE_WORK_KINDS` with `collect_payload`, then implement eligibility on top of the three existing lifecycle timestamps: fixed 7/90/365-day rules, rechecks of current dependencies, valid leases, and unexpired row-content references. Normal GC protects live dependencies and row-referenced content; a count-only grant protects its receipt, not source payload. A mark stops new obligations. Invalid or missing inputs cannot create early eligibility, and no runtime-configurable deadline is introduced. Fake-clock cases prove each boundary; persisted eligibility matches the spec.

### P3-I3 — MP14 atomic mark and reactivation (implementation)

Race reactivation or reference creation against a mark: one authoritative state wins, and purge cannot delete newly protected input. A stale mark or revision is cancelled or reevaluated. Reuse the scope UOW's lock order. PostgreSQL and memory lanes must both prove that no mark or purge path deletes newly referenced active input. This is where the A3 finding lands: the previous session's single-identity-guard design needs a test that actually goes red when **all** serialization is removed — that test is part of this item's acceptance, not optional.

### P3-I4 — MP15 resumable physical deletion (implementation)

Recheck lifecycle revision, deadlines, leases and finite row references after the grace window; persist retryable phases and a minimal non-content outcome; **never record success before deletion completes**. Shared objects use exact live-reference accounting. Count-only receipts survive source-payload collection. Crash and retry must remove eligible payloads and leave no protected or current payload deleted. Candidate time and physical purge time stay distinct events.

### P3-I5 — MP16 explicit purge (implementation)

Atomically tombstone the source, advance `purge_revision` (which makes `decideGrantRead` refuse immediately — the mechanism is already there), cancel affected active reads within the spec's bound, cancel affected work, and enqueue the P2-D1 invalidation events. Duplicate and retried requests are idempotent. Report backend-local deletion separately from all-owner completion.

### P3-I6 — MP17 all-owner completion (implementation)

Keep all-owner completion pending until the backend-local outcome and every required Fin scrub/recovery acknowledgement agree. An offline owner stays pending across restart while backend deletion is already complete. MP16 must not wait for this ticket. Needs P2-I3's acknowledgements to be observable.

### P3-D1 — T42 final pair, then T36/T37/T38

Compose only exact returned commits whose scoped implementation acceptance and review/repair/verification closure is recorded; assemble in a fresh candidate worktree; run the applicable typecheck, build, lint and changed-surface regressions in both repositories. Freeze the final Fin/IK SHAs, corpus/oracle/ID-map hashes, and T22's measurement SHAs. If either candidate SHA or a measured path changed, re-run only the affected QP11 dimensions — do not present the old measurement as final-pair evidence.
Then, and only then, the three final runs. Each reports every case individually with command, exit, expected versus observed, and an explicit list of anything blocked, skipped or unrun. **Any missing, skipped, failed or unrun case keeps its ticket pending** — including in these final runs. T36/T37/T38 may run in parallel only after T42, each on the same frozen pair and manifest, with isolated mutable resources between them.

## The arms T38 must gain

Written against the modules above, not against fixtures:

- M2 (accepted publication of v2 while v1 stays queryable), M3 (two origins, withdraw one), M4 (stale attempt publishes nothing), M5 (repeated extraction keeps one assertion id), M6 (lease expiry plus the GC pin clause), M8 (structural candidates), M9 (guarded decisions), M10 (progress vs. visibility revisions), M11 (pending source counted, unauthorized hidden), M12 (rebuild without new extraction) — all against modules that are already evidenced. **These eleven are the cheapest wins in the entire program** and should be written first, because they prove the driver itself works end to end.
- M13, M14, M15, M16, M17, M19 — against P3's new modules.
- M18 — additionally needs P2's EP7 replay.
- M20 — needs P3's MP13/MP17 **and** P2's scrub-acknowledgement boundary.

## Sequencing and shared-resource rules

- P3-I1 is independent of the collection lane and unblocks two cases immediately. Start it first.
- P3-I2 → I3 → I4 are strictly ordered; each builds on the previous phase.
- P3-I5 needs P2-D1's event contract. P3-I6 needs P2-I3's acknowledgements.
- P2-D2 (how MP15's outcome reaches Fin) constrains P3-I4's output shape; decide it before I4, not after.
- Schema and `src/core/storage/` edits are serialized within the one TypeScript package. Unique schema per test run; remove only what the run created.
- Strict drivers refuse by name when the marker or database is absent — never green-skip.
- The M-case driver has no skip path by design. Do not add one; add arms.

## Carried-in open items

- Whether multiple `keep_both` decisions on one conflict are allowed, and whether a second must bind the revision advance the first caused — owned by T16, consumed by M9.
- Whether a per-result direct read refuses or misses — a spec clause owned by T38, needed before T38 can call M3/M11 complete in every reading.
- T12's repair round (2 Medium / 4 Low) is still open at the plan level and its frozen-ID manifest is an input to T42's hash freeze.
- QP2 remains a production rollout gate only. It does not block any of this plan, and nothing here authorizes rollout.
