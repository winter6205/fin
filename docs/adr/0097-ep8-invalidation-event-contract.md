# 0097. The EP8 invalidation event is a content-free, seq-ordered, per-consumer stream

Date: 2026-10-07
Status: accepted
Authority: [`specs/structured-corpus-evidence.md`](../../specs/structured-corpus-evidence.md) §"Internal evidence capability" is the SSOT. This record decides only what that section deliberately leaves open. It does not reopen it.
Implements: `plans/structured-corpus-evidence-lifecycle-p2.md` §P2-D1. Accepted 2026-10-07; P2-I3 shipped alongside this record's proposal in `4bd3f27`, so it is no longer blocked on it — what remains open is the IK-side capability listed in §4, which P3 owns.


Citation anchor: every `src/` line reference below is verified against the base commit `49471ad`, not against the live working tree. A concurrent lane is editing `src/fin/host/thread_store.py` in this worktree, so those line numbers shift while the tree is dirty; the code they point at is unchanged.

> **Amended 2026-10-07, against the authority this record already names.** Three clauses are
> corrected because the spec answers them and this record had gone past it. The Authority line
> above is the basis: the spec is the SSOT, and this record decides only what the spec
> deliberately leaves open.
>
> 1. **§2.1's per-event `schema_version` is withdrawn**, and no `schema` key travels on an
>    event. The spec makes `invalidations` an **action of the existing `iknow.evidence_result`
>    envelope** (`specs/structured-corpus-evidence.md:218-219`), and that envelope already
>    carries `schema_version: 1`. The event carries `event_id`, `evidence_ref`, `reason`
>    (`:252`). The spec sets its own convention three lines later: `corpus_collection_outcome`
>    gets **its own token pair** (`:232-234`) because it is a *separate method* — a separate
>    method gets a separate token, an action of an existing envelope does not. The token name
>    `iknow.evidence_invalidation_event` appears nowhere in the spec.
> 2. **§2.4's column table is replaced by the shipped DDL.** It mandated `scrubbed_events`,
>    which no code has, and was silent on `last_seq` and `acked_checkpoint`, both load-bearing.
>    The code is the strict improvement, and it is what proved "acked" and "applied seq" are
>    two separate facts inside one system.
> 3. **"Six scalar columns" stays six — but its membership was wrong.** `:259` requires
>    cross-consumer checkpoints to *refuse*, which is only observable with N ≥ 2, so the spec
>    mandates N-generality; a per-consumer outbox therefore needs a `consumer_id` column this
>    record never counted. `schema_version` is withdrawn by clause 1 above and `consumer_id`
>    added here, so the total is unchanged. The spec is overriding this record's *field list*,
>    not its arithmetic.
>
> Also recorded, from the same reading: `seq` is allocated by a per-consumer counter under a
> blocking row lock, and a Postgres `SEQUENCE` is **disqualified** — `nextval` is
> non-transactional, so one rolled-back purge permanently wedges fin's drain, which refuses a
> first `seq` beyond the watermark by more than one and states it "never resets to the start
> and never silently skips". See `plans/structured-corpus-p3-prerequisites.md` §1b.1a.

## 1. Already decided by the spec, not decided here

- The two actions and their fields: `invalidations` takes nullable `checkpoint` and `limit` in 1..20 (default 20) and returns content-free `events`, `next_checkpoint`, boolean `gap`; `ack` takes `checkpoint` and returns the acknowledged `checkpoint` (`specs/structured-corpus-evidence.md:222-227`).
- Each event names `event_id`, `evidence_ref`, `reason`, where `reason` is `access_revoked` or `purged`, and carries no source text (`:241-243`).
- The backend records the trusted fin consumer against each grant. Fin drains at least once per minute while online, transactionally withholds and scrubs affected stored snapshot payloads, and acknowledges only afterwards (`:243-245`).
- Checkpoints and cursors are server-authenticated and consumer-bound. An acknowledgement advances only through a returned contiguous batch successfully processed by that consumer. A malformed or cross-consumer checkpoint refuses. A reference never stored by fin is acknowledgeable after fin verifies no local payload exists (`:246-249`).
- Crash and reconnect recovery resume from the last acknowledged checkpoint before exposing historical evidence. If a retained checkpoint is unavailable (`gap`), all retained references are revalidated and denied ones scrubbed before exposure (`:250-252`).
- The backend keeps unacknowledged events while their grants remain unexpired; expiry lets both stores collect payloads without depending on acknowledgements (`:253-254`).
- Explicit purge stays pending across owners until fin confirms scrub; backend source deletion can proceed independently; a disconnected owner cannot be reported physically erased merely because backend replay is denied (`:255-258`).

Everything below fills a gap left open by that text.

## 2. Decision

### 2.1 Event shape: five event fields, six stored columns, no schema key

One element of `events`, carried inside the `iknow.evidence_result` envelope
(`specs/structured-corpus-evidence.md:218-219`), which already carries `schema_version: 1`
— the event adds none of its own. Five event fields:

| Field | Type | Rule |
|---|---|---|
| `event_id` | string, UUID v4 | globally unique, opaque, **never ordered and never compared** |
| `seq` | integer, int64, `>= 1` | strictly increasing by 1 within one consumer's stream, gapless |
| `evidence_ref` | string, non-empty, `<= 128` chars | the invalidated grant's reference only |
| `reason` | enum | exactly `access_revoked` or `purged` |
| `occurred_at` | string, whole seconds `Z` | the producer's own instant, same rule as `src/fin/host/evidence_contract.py:159-171`; informational, never a deadline |

Plus `consumer_id`, the sixth stored column, required by the spec's N-generality: `:252`
makes the stream per-consumer, `:254` requires the backend to record the consumer against
each grant, and `:259` requires cross-consumer checkpoints to refuse. It identifies the
stream, not the event, so it is stored and not sent. The per-consumer counter that
allocates `seq` is a column of the cursor table in §2.4, not of this one.

The count is therefore still six — the same number this record always claimed. What
changed is the membership: `schema_version` out, `consumer_id` in. See the amendment note
above.

**No `schema` key and no per-event `schema_version` travel.** The version is the
envelope's, not the event's; a separate method gets a separate token
(`specs/structured-corpus-evidence.md:232-234`), and an action of an existing envelope does
not. This is not a stylistic preference — fin's parser is a closed-key parser and refuses
an unexpected key, so a `schema` field would be `malformed_evidence_contract`.

> **This clause creates an unrecorded code obligation, and fin's parser does not yet
> honour it.** `src/fin/host/structured_evidence_invalidation.py:86-88` still declares
> `INVALIDATION_EVENT_FIELDS` as a closed set **including** `schema_version`, and `:194`
> refuses anything whose key set differs from it exactly. So today an event built to this
> amended contract is refused `malformed_evidence_contract` — the amendment as written is
> not yet true in the tree it describes. Whoever builds the IK side (register §1.1) must
> change that frozenset in the same change, and the change is listed as register §2.8. Do
> not treat this clause as a description of current behaviour; it is a decision the code
> has not caught up to, and saying otherwise is what produced the trap.

Any additional key, on either side, is `malformed_evidence_contract`. There is no optional
field set and no free text.

### 2.2 Ordering: a per-consumer gapless `seq`, not `event_id`

`event_id` is an idempotency key only. The orderable cursor is `seq`, assigned per `(consumer_id)` stream and gapless, so that "I missed something" is decidable rather than inferred.

This is forced by the spec's gap rule at `:251-252`. Contiguity is undecidable from opaque identifiers alone: if ordering were `event_id`-only, a consumer that missed an event could not distinguish its absence from a filter, and the spec's "must revalidate" branch would be unreachable in practice. Ordering by `event_id` is rejected for exactly that reason.

Consequence: the stream is per consumer, not global. A global sequence is not required and is rejected, because per-consumer gaplessness is what the ack rule (`:247-248`) actually checks.

### 2.3 Checkpoints stay opaque; `seq` is a verification aid only

Checkpoints are minted, authenticated and bound to one consumer by the backend. fin stores them verbatim and never parses, decomposes, compares or constructs one. A `seq` in an event is used for one thing only: fin asserts that a returned batch is contiguous (`seq[n] == seq[n-1] + 1`) before it acknowledges. fin never derives an acknowledgement target from `seq`.

### 2.4 Watermark home: fin's own Postgres, beside `fin_run`

The consumer state lives in a dedicated table owned by the fin host lane, created next to the existing `fin_run.structured_evidence` DDL at `src/fin/host/thread_store.py:686` and `:743`. One row per `(consumer_id)`:

| Column | Meaning |
|---|---|
| `consumer_id` | the trusted consumer identity fin was configured with; the primary key |
| `applied_checkpoint` | the checkpoint covering events whose local scrub has **committed** |
| `applied_at` | when that commit happened |
| `last_seq` | the highest `seq` in a batch whose scrub **committed** — moves with `applied_checkpoint`, never with the acknowledgement |
| `acked_checkpoint` | the cursor the backend **accepted**; the resume point after a crash between the two phases |

**This table is the shipped DDL** (`src/fin/host/thread_store.py:867-871`), not the one
this record originally proposed; see the amendment note above. The superset matters
because it encodes why the two checkpoints cannot collapse into one column. `last_seq` and
`applied_checkpoint` are written together by the scrub commit and are **not** written by
`advance_invalidation_acknowledgement`, which deliberately omits `last_seq` from its
update: that update runs before a backend acknowledgement exists, so it must keep the
resume cursor the previous acknowledgement left. A single column could not represent both
— it would either lose an event or be unable to say a scrub already happened.

The `scrubbed_events` audit count this record originally required is **dropped**: it was
never built, and neither §2.2's contiguity argument nor §2.7's gap rule needs it. `last_seq`
already answers "how far have I got" in a decidable form, which an opaque checkpoint cannot
express.

Atomicity with the acknowledgement is two-phase, because a network call cannot join a database transaction:

1. One fin transaction applies the batch and advances `applied_checkpoint` together with the payload scrub.
2. Only after that commit returns, fin issues `ack`.
3. fin never advances `applied_checkpoint` from an acknowledgement, and never acknowledges a batch whose scrub did not commit.

Recovery uses the **server's** acknowledged checkpoint as the resume point and fin's `applied_checkpoint` as the upper bound of what it may already skip; re-drain between them is idempotent, so re-applying is always safe.

Rejected homes: `fin_run.structured_evidence` is run-scoped and the drain is deployment-scoped; the file-backed invalidation journal (`src/fin/host/workspace_http.py:2875`) cannot be atomic with a JSONB payload delete in Postgres; the LangGraph checkpoint is run-scoped and is rewritten by the agent loop.

### 2.5 Content prohibition, enforced in DDL

An event names affected references and nothing else. It may never carry `value`, `context`, support lists, extraction payloads, query text or digests of content, filenames, subject or operation, or any pre- or post-image of the snapshot. `occurred_at` is a time, not evidence; `evidence_ref` is an opaque handle, not content.

Enforcement is structural, not documentary: the IK table has no payload column at all — only the six scalar columns of §2.1, five event fields plus `consumer_id` — so "content-free" is a property of the schema. This follows the precedent in `~/projects/iknow-wt-t13-ik/src/core/storage/projection-outbox.ts`, whose header states that an intent names a subject and never a snapshot of its values (`:5-8`) and whose DDL carries no payload column. fin's side is a closed-key parser, so an unexpected key refuses instead of being dropped.

### 2.6 `reason` cannot be confused with an expiry

`reason` is a closed enum of exactly two members, with no expiry member and no free text. Two consequences:

- An authorization change is `access_revoked`, an explicit purge is `purged`, and there is no third value that could be read as either or as a deadline.
- **Expiry is never an invalidation event.** fin decides expiry locally from the deadline it already froze into the snapshot. An event cannot say "this expired", so no consumer may treat a stream event as an age-based collection signal. See P2-D2.

### 2.7 Gap detection and typing

- The backend decides `gap`. It is `true` when the supplied checkpoint is no longer retained, was never issued, or was issued for another consumer. It is never inferred by fin from an empty `events` array.
- fin's own independent check: if a returned batch is not contiguous under §2.3, fin refuses to acknowledge, records the batch as unprocessed, and retries with the last acknowledged checkpoint.
- On `gap = true`: fin withholds all historical structured evidence, revalidates every retained reference against the current authorized binding, scrubs the denied ones, and only then resumes the drain. This is the spec's `:251-252` rule; this record adds no leeway.
- Typing: a refusal of the drain itself is one of `malformed_evidence_contract` (bad action/fields), `unsupported_evidence_contract` (unknown schema version), or `backend_unavailable` (transport). None of these is `evidence_expired` (`specs/structured-corpus-evidence.md:353-354`).

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| Order by `event_id` only | Contiguity is undecidable, so a silent skip is possible; defeats the spec's gap rule |
| Global monotonic sequence | Per-consumer gaplessness is what the ack rule checks; a global sequence adds cross-consumer coupling for nothing |
| Carry the snapshot or a diff in the event | Violates the content prohibition and turns `ack` into a data-delivery channel that bypasses the 32 KiB snapshot bound (`specs/structured-corpus-evidence.md:303-306`) |
| Deduplicate one pending event per `(ref, reason)`, as `projection_outbox` does with a partial unique index (`projection-outbox.ts:45-49`) | That dedupe exists because the projector re-reads current state on drain (`projection-outbox.ts:6-8`). fin's action is idempotent and monotone, so the dedupe index would add a write and a conflict path for no correctness gain |
| Two events per reference, one per reason | Kept. fin scrubs on the first and records the second as an idempotent no-op; collapsing them would require the backend to reason about fin's scrub state, which it cannot see |
| Acknowledge inside the scrub transaction | Not possible; an ack is a network call. Hence §2.4's two phases |
| fin-side watermark in the agent checkpoint | Run-scoped; the drain must survive restart and outlive any single run |

## 4. IK-side obligation, recorded as a follow-up in a separate repository

`~/projects/iknow-wt-t13-ik` is a different repository and is **not** part of this change. The following is owed there, under its own ticket:

1. DDL for the invalidation outbox with the **six** scalar columns of §2.1 — the five event fields plus `consumer_id` — and no payload column, plus a per-consumer cursor table carrying `(consumer_id PK, acked_checkpoint, acked_seq, acked_at, last_allocated_seq)`. The count of six is unchanged from this record's original proposal; the **membership** changed, `schema_version` out and `consumer_id` in, because the spec's N-generality requires the stream to be identified. The cursor's `acked_seq` is deliberately **not** named `last_seq`: fin's column of that name means *applied*, and one name for two different quantities across two systems is a silent-error generator. `last_allocated_seq` and `acked_seq` are two columns for the same reason fin keeps `last_seq` and `acked_checkpoint` apart — what has been handed out is not what has been acknowledged.
2. `seq` is allocated by a **per-consumer counter row under a blocking `SELECT … FOR UPDATE`**, with `seq = last_allocated_seq + 1 RETURNING`, inside the purge transaction. The shape already exists: `PgStructuredPublication.withdraw` takes the lock (`:301`, `:867-882`) and bumps a `bigint` counter in the same transaction (`:333-342`). A Postgres `SEQUENCE` is **disqualified** — `nextval` is non-transactional, so one rolled-back purge permanently burns a value, after which fin refuses a first `seq` beyond its watermark by more than one and states it "never resets to the start and never silently skips" (`src/fin/host/structured_evidence_invalidation.py:326-334`, `:32-33`). `FOR UPDATE SKIP LOCKED` is disqualified the other way: skipping leaves the event with no `seq` to carry, forcing it out of the purge transaction and violating item 3.
2. Emission inside the purge transaction, as `specs/structured-corpus-maintenance.md:326-327` requires ("the purge transaction also schedules the evidence contract's durable fin invalidation events").
3. The `invalidations` and `ack` actions. Today IK explicitly excludes them: "`invalidations` and `ack` are out of scope here" (`~/projects/iknow-wt-t13-ik/src/core/storage/evidence-read.ts:74`).
4. The drain read runs in one authorized unit of work; the read-only form of the rule is `runTransaction({readOnly: true })` (`~/projects/iknow-wt-t13-ik/src/core/storage/structured-status-reads.ts:286`). `PgMaintenanceStatusStorage` is the shape precedent: one transaction, identity-minted context re-checked inside it (`:265-288`, identity check at `:274-279`).
5. `ack` re-verifies, inside its own write transaction, that the checkpoint was server-minted for this consumer, that every event it covers was in fact returned to this consumer, and that the advance is contiguous. A partial or cross-consumer ack is a refusal, not a partial write.
6. Retention of unacknowledged events while grants remain unexpired, per `specs/structured-corpus-evidence.md:253-254`.

## 5. What fin can implement and prove today, against a stub

Implementable and provable now, with no IK code:

- the consumer state machine of §2.4, including that `applied_checkpoint` never advances before the scrub commit;
- the closed-key parser and every typed refusal in §2.7;
- the contiguity assertion and its refusal path;
- idempotent re-drain after a crash between the two phases;
- the `gap = true` branch, including that evidence stays withheld until revalidation finishes;
- the "reference never stored" absence check before acknowledgement (`specs/structured-corpus-evidence.md:249`);
- durability of the watermark across restart, against real Postgres.

Not provable without IK, and therefore not claimed here: that the backend's `gap` verdict is truthful, that a real authorization change or purge produces an event, that unacknowledged events are retained for the required window, and that a cross-consumer checkpoint actually refuses.

## 6. Consequences

- P2-I3 is unblocked once this record is accepted; its event-handling half depends on §2.1 and §2.6, its state half on §2.4.
- P2-I1, P2-I2 and P2-D2 are unaffected.
- The IK follow-up in §4 is on the critical path for E8, E15 (`:398`, `:405`) and M16, M20 (`specs/structured-corpus-maintenance.md:361`, `:365`). fin-side completion does not make those cases pass.
- Adopting a per-consumer `seq` means a checkpoint minted under this contract is not readable by a consumer that expects another contract. That is intended: version skew is a refusal, not a fallback.