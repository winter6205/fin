# Plan P2 — Evidence lifecycle: admission, replay, invalidation, expiry

**Goal:** Complete EP6–EP11 and MP16/MP17's Fin-facing half so retained evidence is admitted only from a verified current turn, replays against its original frozen snapshot, is scrubbed before invalidation is acknowledged, recovers from gaps without exposing denied snapshots, and expires on fixed deadlines that no read can slide.
**Authority:** [Evidence spec](../specs/structured-corpus-evidence.md), [Evidence plan](structured-corpus-evidence.md), and the execution plan's T28–T35 remain the acceptance authority. This plan sequences work and records what already exists; it does not restate normative criteria.
**Boundary with P3:** P2 owns what Fin does with evidence and what IK emits about invalidation. P3 owns collection and purge execution in IK. **They are not independently deliverable** — M20 needs both, and EP11's authoritative input comes from P3's MP15. Where this plan needs a P3 artifact, it says so and names the decision instead of inventing the artifact.

## Corrections to the execution plan's Evidence premises

Two traps in the existing records, verified this session:

1. **`src/fin/retrieve/invalidation_port/` is not EP9.** It is fin→IK content invalidation by `source_ref` (the `invalidate` tool), not an IK→fin evidence-invalidation consumer. The names collide; do not wire them together.
2. **`EVIDENCE_EXPIRED` and `EVIDENCE_UNAVAILABLE` exist in `evidence_contract.py:76-77` as constants with zero production uses.** Their presence is not an expiry implementation. Nothing in Fin currently withholds an expired snapshot.

## What already exists (do not rebuild)

| Capability | Location | Gap |
|---|---|---|
| Snapshot validation, digest, stored record | `structured_evidence_validation.py:623,658,687,823`; `structured_snapshot.py:160,198,210`; `structured_evidence.py:274` | complete; **no production caller** |
| Stored-record read-back | `structured_evidence.py:317` `project_stored_structured_evidence` | the only one with a production caller |
| Storage | `thread_store.py:686,743` — `fin_run.structured_evidence JSONB` | column exists; **nothing writes it** |
| Write-time guard | `thread_store.py:876` `_outgoing_structured_error` | active |
| Private read client | `evidence_route.py:210` `EvidenceRouteClient` (`read`/`supports`) | **zero production callers** |
| Incoming expiry check | `structured_evidence_validation.py:218` `_expected_evidence_expiry` (queried_at + 365d) | validates the producer's claim; **not** an outbound expiry decision |
| Cross-language vectors | fin `tests/host/test_structured_snapshot.py` (E7) | evidenced; retained for E12 |
| Legacy regressions | `test_evidence_contract.py`, `test_evidence_admission.py`, `test_sql_row_evidence.py`, `test_evidence_replay_v0.py` | must stay green (E12) |

Content-free outbox precedent exists in IK (`projection-outbox.ts`, ADR-0001): an intent names a subject, never its values, and has no payload column. That is the design EP8 should follow — but it is a graph-projection log with `subject_kind CHECK IN ('entity','triple')` and no per-consumer watermark, so it cannot be reused as-is.

## What must be built

| Gap | Category |
|---|---|
| IK→Fin EP8 event contract: fields, sequence, per-consumer watermark, content prohibition | decision — **blocks everything else** |
| How MP15's authoritative outcome reaches Fin (channel, fields, and how "expired or unavailable" is expressed so it can never become a successful collection claim) | decision — hard prerequisite for EP11 |
| EP6 production admission chain | wiring |
| `RunResponse.structured_evidence` and its serializer | wiring |
| EP7 replay against the frozen snapshot | new |
| EP9 scrub-before-ack consumer; EP10 checkpoint recovery | new |
| EP11 outbound expiry execution | new |
| MP16/MP17 ack ordering as an explicit two-phase boundary | decision |

## Execution scope for this session (2026-10-07)

Executed on base `49471ad` (T39 guard accepted, as the index requires P2 to start
there), worktree `fin-wt-p2`, branch `feat/structured-p2-evidence-lifecycle`.

Delivered here: **P2-D1, P2-D2, P2-D3, P2-I1, P2-I3**.

Deferred, with the blocker named:

- **P2-I2** — production `EvidenceRouteClient` has no identity-bound session. `graph.py:3126`
  passes no `session`, and `mcp_adapter.py:1352-1357` falls back to a per-call subprocess, so
  every `supports`/`read` would re-authenticate with a fresh `owner_user_id` and could never
  answer an authorization question about a stored snapshot's owner. Fixing it is a session
  lifecycle change in the P1 driver lane, not a P2 change; doing it here would build EP7 on a
  transport that cannot carry it.

  **Update (2026-10-07, branch `fix/p2-evidence-session`): the session-lifecycle prerequisite
  has landed; P2-I2 itself has NOT started.** `EvidenceSessionRegistry` now owns one persistent
  child per frozen `EvidenceIdentityBinding` and replaces the per-call relaunch, so the
  transport can finally carry an identity. What is still absent is every part of the ticket
  body above: there is no history entry point, no read of a stored `snapshot.evidence_ref`, no
  `EvidenceRouteClient.read()` on a production path (the registry constructs the client and
  the graph discards it), no digest comparison against the local frozen snapshot, and no
  refusal on authorization, expiry or purge. Until that lands, "replay never issues a query
  and never redirects to a newer source" is vacuously satisfied, not implemented, and the
  `p2:162` real-storage rule is still unmet for this lane. Supplying the binding is also
  still the caller's duty — the host has no trusted source for the authority facts, so
  production wires no registry at all.
- **P2-I4** — hard-blocked by the decision the plan itself names: MP15 does not exist in IK
  today (no `collect`/`mark`/`grace`/`locked recheck`), so there is no outcome to execute EP11
  against. P2-D2 records the channel that will carry it; the execution waits for P3.

  The P2-D2 record also narrows EP11 further than this plan assumed, and the narrowing was
  verified against the specs rather than accepted on trust: fin's own `now >= expires_at`
  decision is computable from the frozen snapshot and needs no MP15 input. The half of EP11
  that this change therefore DOES land is the local withholding at the turn-table read
  surface: an expired snapshot is served as an unavailable citation with `evidence_expired`
  and an empty payload, while the answer survives in history. What is still deferred is the
  MP15 half — learning what the backend did to its own copy — plus the drain cadence that
  would ever ask it. See `docs/adr/0098-mp15-to-fin-outcome-channel.md`.

P2-I3 is split from the plan's own single bullet because D1 gates its event shape.

### What P2-I3 does NOT deliver, and why

Stated here rather than left to be discovered, because the code review is right that a
consumer nothing invokes is not a consumer:

- **The drain has no production caller and no scheduler.** The spec's "drains at least once
  per minute while online" cadence has no home: the host has no online loop that owns it, and
  the IK side has no `invalidations`/`ack` producer yet (the IK evidence capability is
  explicitly `read`/`supports` only). Building a scheduler now would poll a channel that
  answers nothing. P2-I3 lands the consumer half of a contract D1 froze; P3 supplies the
  producer and the host loop that calls it.
- **T33's "keep snapshots unavailable until recovery completes" half is not implemented.** On
  `gap`, the drain refuses and leaves the watermark put, which is the safe direction; the
  spec additionally requires withholding every retained reference, revalidating them against
  the current authorized binding, and scrubbing the denied ones before exposing them. That
  sweep needs a reader that honours a withhold flag, which is P2-I2's replay path. Without it
  the revalidation half would be code that decides to withhold with nothing obeying.

### Architecture change review — 5-skill verdict

```
planned files: src/fin/host/state.py, src/fin/host/graph.py, src/fin/host/structured_evidence_admission.py,
               src/fin/host/structured_evidence_invalidation.py, src/fin/host/thread_store.py,
               tests/host/test_structured_evidence_admission.py,
               tests/host/test_structured_evidence_invalidation.py,
               tests/host/test_structured_evidence_live_pg.py, docs/decisions/, plans/, docs/validation/
bounded-context-guardian: yes — I1 lands inside the existing fin.host structured-evidence lane
               (no new package); I3's consumer is a sibling module in that same lane and imports no
               fin.retrieve symbol at module scope, so the fin.retrieve <-> fin.host cycle documented at
               mcp_adapter.py:660-664 stays closed.
input-contract-tests:   yes — the two public entry points (admission of an untrusted producer sibling;
               the invalidation drain with a cursor) are covered for all five classes: empty, invalid,
               overflow (32 KiB snapshot bound + event-count bound), concurrent (idempotent re-drain),
               and exception (write-guard rejection leaves zero rows).
error-handling-enforcer: yes — every failure is typed through CONTRACT_REFUSAL_CODES
               (evidence_contract.py:87-115); no silent fallback, no empty catch. A refused sibling never
               degrades into a success-shaped empty snapshot (structured_snapshots_of's [] is the specific
               trap the admission entry point must not inherit).
complexity-anti-drift: yes — each new module is one abstraction level: admit, drain. Admission keeps the
               reduce/extract shape already proven by verify_structured_claims; the drain keeps the
               consume/advance shape of read-then-checkpoint. Neither duplicates a digest or refusal table.
minimal-change-verifier: yes — one task (the P2 Evidence lane), no drive-by, no new dependency, no
               lockfile change. Anything belonging to P2-I2/I4 or to P1's driver lane is left untouched
               and named in the deferral list above.
```

## Ordered work

### P2-D1 — Freeze the IK→Fin invalidation event contract (decision)

Fields, monotonic sequence, per-consumer watermark, idempotency key, and the content prohibition (an event may name affected grants/revisions; it may not carry any source, snapshot or payload body). Both repositories have zero types for this today, so this is genuinely a contract-first step. Include the gap-detection rule: the consumer must be able to tell "nothing for me" from "I missed something".
Output: a decision record plus the type on the IK side. No consumer yet.

### P2-D2 — Decide the MP15 → Fin outcome channel (decision)

EP11 must consume MP15's authoritative outcome and must never turn an expired or unavailable outcome into a successful collection claim. Neither repository has a carrier for that outcome today. Decide: which IK surface Fin reads, what the fields are, and how absence is represented. If P3 is not yet built, the decision still lands now and P3 conforms to it — the reverse order would leave two incompatible shapes.

### P2-I1 — EP6 production admission (implementation, wiring)

The chain is written and disconnected. At the retrieve tool node (`graph.py:1485`) the validated `structured_result` sibling must flow: `structured_snapshots_of` → `build_stored_record` → a new `AgentState` channel → the five `RunRecord(...)` construction sites in `workspace_http.py` that already carry `visual_claims`. Add the current-turn claim verification (`verify_structured_claims`) on the host side. Refusals must be typed and must not produce success-shaped empty evidence.
Also add `RunResponse.structured_evidence` and project it in the serializer — without it, stored evidence is unreadable after the run.
This closes T29's Partial.

### P2-I2 — EP7 replay against the original snapshot (implementation, new)

History entry point → the stored `snapshot.evidence_ref` → `EvidenceRouteClient.read()` → digest comparison against the locally frozen snapshot → refuse on authorization, expiry or purge. Replay never issues a query and never redirects to a newer source. The private client already exists; the path to it does not. Refresh (a genuinely new current-turn result) is a separate path and must not reuse the replay digest comparison.

### P2-I3 — EP9 scrub-before-ack and EP10 checkpoint recovery (implementation, new)

Design these together: both need the event sequence and a local watermark. EP9 applies each event idempotently, deletes the affected payloads, retains only allowed non-content audit state, and acknowledges **only after** storage confirms the scrub. EP10 restores the persisted checkpoint on restart, reconciles missed invalidations, and keeps snapshots unavailable until recovery completes. A stale checkpoint or an unavailable backend must never advance acknowledgement.

### P2-I4 — EP11 outbound expiry (implementation, new)

The only expiry logic today validates an inbound claim. Add the outbound decision: on read-back, `now >= expires_at` withholds the citation/snapshot while retaining allowed non-content identity. Ordinary local deletion follows MP15's mark + 7-day grace + locked recheck; deletion is never reported before physical removal. A count-only grant preserves its receipt, not source payload, and survives ordinary publication/payload GC until its own fixed expiry. Explicit owner-authorized purge withholds immediately and bypasses age and grace. Neither evidence reads nor citation replay renews a grant or a deadline. Consume P2-D2's channel.

### P2-D3 — Make the MP16/MP17 acknowledgement boundary explicit (decision)

Backend-local deletion completes independently of Fin acknowledgement; all-owner completion stays pending until every required scrub/recovery acknowledgement agrees. An offline owner stays pending across restart. This ordering must be expressed in code and in the records, not left to ordering accidents.

## Case coverage this plan must satisfy

E4 and E13 need EP7. E5 and E11 need EP6 admission plus the real producer (P1-I7). E8 and E15 need EP8–EP10 with MP16/MP17. E9, E10 and E14 need EP11 with MP15 and MP16. E1–E3, E7 and E12 are already evidenced upstream and are retained, not re-derived. E6 needs the continuation path (T14) plus measured bounds (P1-D2).

## Sequencing and shared-resource rules

- P2-D1 and P2-D2 are independent of each other and of all code; do them first, in parallel.
- P2-I1 is independent of the rest and unblocks T29 alone. Start it immediately.
- P2-I2 needs the private read path (T13/T14, evidenced) and nothing from P2-I3. It is the most expensive single item and gates E4, E13 and M18.
- P2-I3 needs P2-D1. P2-I4 needs P2-D2 and MP15 outcomes from P3.
- Fin host/retrieve changes share adapter and host seams; serialize them against each other and against the P1 Fin driver work.
- Each Fin lane must prove its boundary against real storage, not a fixture: the invalidation consumer and the replay path both cross the live-JSONB boundary.

## Carried-in open items

- B2's behavioural guard is a T39 prerequisite; P2-I2's refusals must use the same closed vocabulary the T13/T14 lanes established.
- T16's `readSnapshot` returning an empty snapshot for an unregistered scope (instead of `access_denied`) is the same family as the per-result-read question and is owned with it, not here.
- Whether a stale authorization change withholds a count receipt sooner than its fixed expiry is specified (T34's acceptance text); no open question remains.

## Current closeout note (2026-10-07)

This dated status pointer does not change the Evidence spec, ticket requirements, or acceptance
criteria above. The current register and command-level integration receipt are
[`structured-corpus-p3-prerequisites.md`](structured-corpus-p3-prerequisites.md) and
[`2026-10-07-p3-prerequisites-closeout.md`](../docs/validation/2026-10-07-p3-prerequisites-closeout.md).

Fin now has replay/session, recovery, and drain helper implementations, with bounded tests
recorded on candidate `01a6917`. Their presence does not mean the production lifecycle is
accepted: register §2.1 still requires a trusted identity bundle and production history/replay
caller; §2.2 still requires production transport binding and a host-lifecycle caller; and §2.6
still requires startup/history recovery acceptance. Cross-process checkpoint visibility (§2.3)
also remains open. P2-I4's MP15-backed outbound expiry remains deferred until IK supplies the
authoritative outcome. The dated test results are evidence for their named slices only; they do
not certify full E or M acceptance.
