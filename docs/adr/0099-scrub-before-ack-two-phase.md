# 0099. Scrub-before-ack is a two-phase state machine with exactly one writer for completion

Date: 2026-10-07
Status: accepted
Authority: [`specs/structured-corpus-maintenance.md`](../../specs/structured-corpus-maintenance.md) §"Physical collection" and [`specs/structured-corpus-evidence.md`](../../specs/structured-corpus-evidence.md) §"Internal evidence capability" are the SSOT. This record makes the ordering the specs require executable and checkable.
Implements: `plans/structured-corpus-evidence-lifecycle-p2.md` §P2-D3. Accepted 2026-10-07; P2-I3 shipped alongside this record's proposal in `4bd3f27`, so it is no longer blocked on it. What remains open is P3's MP16/MP17 completion recording, which this record's two-phase machine governs.


Citation anchor: every `src/` line reference is verified against base commit `49471ad`. `thread_store.py` is being modified concurrently in this worktree, so `:686` and `:743` are base-state lines; the code they point at is unchanged.

## 1. Already decided by the specs, not decided here

- The purge transaction schedules the durable fin invalidation events; local backend deletion and fin snapshot scrubbing have **separate persisted outcomes**; all-owner purge completion requires the configured snapshot owner's acknowledgement; offline or unreachable owners leave that phase pending and must reconcile before serving evidence after restart; no successful completion is recorded while copied live snapshot payloads remain unacknowledged (`specs/structured-corpus-maintenance.md:326-331`).
- Explicit purge remains pending across owners until fin confirms scrub; backend source deletion can proceed independently; a disconnected owner cannot be reported physically erased merely because backend replay is denied (`specs/structured-corpus-evidence.md:255-258`).
- fin drains at least once per minute while online, transactionally withholds and scrubs affected stored snapshot payloads, and acknowledges only afterwards (`:244-245`).
- A collector failure persists its phase and retries, and never records successful deletion before payload removal (`specs/structured-corpus-maintenance.md:333-336`).
- M16 requires that a crash midway through purge and retry leaves each payload deleted once or safely retried, brief audit surviving, and successful completion not premature. M20 requires a purge to stay pending until fin snapshot scrub is acknowledged (`specs/structured-corpus-maintenance.md:361`, `:365`).

The specs state the invariant. They do not say which code may write which field, which is what allows "premature completion" to be a bug rather than a contradiction.

## 2. Decision

### 2.1 Four states, one writer each

Persisted per `(scope_binding, purge_revision)` in the backend, plus one row per `(scope_binding, purge_revision, consumer_id)` for owner state:

| State | Written by | True only when | Explicitly not |
|---|---|---|---|
| `scheduled` | the purge transaction, in the same commit as the tombstone | the invalidation events are durably queued | not a deletion, not a scrub |
| `scrubbed` | the ack handler, for that owner only | the acknowledged checkpoint covers a contiguous batch whose local scrub committed on the consumer | not backend collection, not all-owner completion |
| `collected` | the local delete transaction, in the same commit as the payload removal | the backend payload rows are gone | not all-owner completion |
| `completed` | the reconciler only | `collected` **and** `scrubbed` for **every** configured owner of that revision | never set by the purge, ack or delete transaction |

`failed` is terminal for an attempt, not for the record: a retry re-enters the phase it failed in, preserving the brief non-content audit (`:314`, `:336`).

### 2.2 A crash between the two phases is safe in both directions

- **After the local scrub commit, before the ack.** The backend still holds the event unacknowledged and redelivers it at least once. fin's scrub is idempotent: a reference with no local payload is recorded as an idempotent no-op, never as "nothing to do", so the event still advances. No state regresses and no reference is dropped.
- **After the ack, before the backend's local delete.** The owner row reads `scrubbed` and the record does not read `collected`, so `completed` is not recorded. The backend then collects on its own schedule (`specs/structured-corpus-evidence.md:256-257`). This is exactly the case the specs' "separate persisted outcomes" sentence exists for, and it is the reason the two phases are separate rows rather than one flag.

fin's own crash window is the mirror image and is covered by P2-D1 §2.4: `applied_checkpoint` advances only with the scrub commit, and the ack is issued only afterwards.

### 2.3 "An offline owner stays pending across restart" operationally

Pending-ness is a row, not a live condition.

- `ack_state` per `(scope_binding, purge_revision, consumer_id)` moves forward only, and only on that owner's acknowledged checkpoint. It is never written by a timeout, a retry count, a heartbeat, or the absence of a complaint.
- On restart, fin reconciles before any evidence-serving path opens: it re-reads unacknowledged events for every configured owner, withholds historical structured evidence until reconciliation completes, and resumes from the last acknowledged checkpoint (`specs/structured-corpus-evidence.md:250-252`). A backend restart reconciles the same way, for the same reason (`:326-331`).
- An owner that is offline for the whole retention window is still `NULL`, not "assumed scrubbed". When the grant expires, both stores may collect payloads without depending on the acknowledgement (`:253-254`), and the record moves to `collected` only; if the owner later returns with an event for a reference fin no longer holds, fin's absence check (§2.4d) acks it without claiming anything.

### 2.4 How the code is prevented from recording successful completion early

Four independent mechanisms, each covering a different way the code can be wrong. Any one of them alone would be enough to fail M16; all four are required because they fail differently.

**a. Writer separation.** `completed_at` for a scope purge revision is set by exactly one statement, in the reconciler. The purge transaction, the ack handler and the local delete transaction contain no statement that sets it. This is a schema-and-code-shape property: if a new write path appears, there is no field for it to fill.

**b. Predicate, not a flag.** The reconciler's update is guarded by all of:

- `NOT EXISTS` an owner row for that revision with `ack_state IS NULL` or `'scheduled'`;
- `EXISTS` an owner row with `ack_state = 'scrubbed'` for every configured owner of the revision;
- `collection_state = 'collected'` for that same revision.

A 0-row update is not a completion. A guard that finds a missing owner row leaves the record in `scrubbed`, and re-running the reconciler is the only way forward. A trigger or a job-completion hook that sets `completed_at` directly is a contract violation of this record, not an optimization.

**c. Fin-side atomicity.** fin's local outcome/tombstone row is written in the **same transaction** as the payload deletion from `fin_run.structured_evidence` (`src/fin/host/thread_store.py:686`, `:743`), so "fin recorded the snapshot collected" and "fin's payload is gone" cannot diverge. The acknowledgement is a separate call issued only after that transaction commits. fin never writes an outcome record derived from the backend's word about fin's copies; the backend has no such authority (`:314-317`).

**d. Absence check before acknowledgement.** An event naming a reference fin never stored is acknowledged only after fin reads its own store and confirms no local payload exists (`specs/structured-corpus-evidence.md:249`). Without this, an acknowledgement could be used to declare a payload gone that fin still holds, or to claim a scrub that never ran. The check is a read against fin's own storage in the same drain cycle, not a remembered flag.

### 2.5 Reporting rules that follow

- A purge is `completed` only in the sense of §2.1. Human-facing copy distinguishes "the backend collected its copy", "this owner confirmed scrub", and "all owners confirmed", because `specs/structured-corpus-evidence.md:255-258` forbids reporting a disconnected owner as erased.
- All three states are non-content: reference, revision, time, reason. No payload, no support list, no source text (`:366`).

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| One `completed` boolean on the purge row, set by whichever side finishes first | This is the M16 failure mode exactly: the first finisher reports a completion the other side has not reached |
| Treat acknowledgement as implied by fin holding no evidence any more | A disconnected owner and an emptied owner are indistinguishable, which the spec forbids outright (`:258`) |
| Acknowledge first, scrub after | Inverts the required order; it is the M20 and E15 violation (`:405`; `specs/structured-corpus-maintenance.md:365`) |
| A job state machine that treats "purge finished" as completion | Job completion describes the backend's own work; the all-owner condition is a cross-owner predicate and does not belong in a job state |
| Let the ack handler write `completed` when it sees no unacked events for the owner | That is a per-owner statement being promoted to an all-owner claim, and it needs the reconciler's join to stay honest |
| Expire the whole record after the retention window | Would erase the audit that "no successful completion while payloads remain unacknowledged" depends on |

## 4. Consequences

- P2-I3's consumer half implements §2.4c and §2.4d; the backend half of §2.4a and §2.4b lands with P3's MP16/MP17 collector.
- M16 and M20 are joint cases. fin cannot pass either alone, and no fin-side record may be read as partial acceptance.
- The state machine is testable without MP15: the local half of each transition is driven by fin's own storage commits, so a stub backend can exercise the crash windows of §2.2 and the guard of §2.4b.
- E15 (`specs/structured-corpus-evidence.md:405`) needs both sides; this record fixes the ordering, not the pass.