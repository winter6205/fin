# P3 §1.1 invalidation outbox slice — decisions, boundary, architecture gate

Date: 2026-10-07
Status: implementation in progress
Register: `plans/structured-corpus-p3-prerequisites.md` §1.1, §1b.1, §1b.3, §1b.4, §2.8
Contract authority: `docs/adr/0097-ep8-invalidation-event-contract.md` §2.1, §2.3, §2.5, §2.6, §2.7, §4.1, §4.2
Spec authority: `specs/structured-corpus-evidence.md:213-269`

Bases: Fin `origin/fix/t39-behavioural-guard` @ `f577f8c`; IK
`origin/feat/structured-p1-query-production` @ `e5009a1`, whose second parent is
the root-validated candidate `f5ff50e`.

## Scope

This slice is exactly the handoff's single `[NEXT]` item: the IK-side outbox and
per-consumer cursor DDL, the trusted `consumer_id` carrying that register §1b.3
makes its precondition, and the §2.8 alignment of Fin's closed event parser.

Out of scope, and named here so the boundary is auditable rather than implied:

- register §1.2–§1.6 (purge-transaction emission, the `invalidations`/`ack`
  actions, the drain read, ack re-verification, unacked-event retention);
- register §1b.5a's `unsupported_evidence_contract` member of `EVIDENCE_FAILURE`,
  which §1b.5 assigns to §1.3;
- register §2.1/§2.2/§2.3/§2.6, M6, P2-I4, the P1 inventory dispositions and
  R01–R04;
- the T40 build rebinding. Register §3.3 makes it deliberate, so the built IK
  artifact stays bound to `f5ff50e` and the `a5cea161…` dist receipt stays
  attributed to that candidate. This slice therefore runs no Fin live lane
  against the new build and claims no new T40 receipt.

## Decisions

### D1 — Event granularity: one event per `(consumer_id, evidence_ref, reason)`

Register §1b.1a leaves granularity open and states it "must not be silently
chosen". This is the choice.

ADR 0097 §2.1 defines `evidence_ref` as "the invalidated grant's reference
only", and §3 keeps "Two events per reference, one per reason". The unit is
therefore one invalidated grant, and a reference that changes both reason facts
emits two events. Fin scrubs on the first and records the second as an
idempotent no-op, per ADR 0097 §3.

Consequences: `UNIQUE (consumer_id, seq)` is the stream's addressability
constraint and a duplicate `(consumer_id, evidence_ref, reason)` is not
independently forbidden, because the sequence, not the triple, is what identifies
an event.

### D2 — Table names

`evidence_invalidation_outbox` and `evidence_invalidation_cursors`. IK already
owns an unrelated `projection_outbox`; reusing a bare `outbox` name would make
two different outboxes indistinguishable in `information_schema` and in every
failure query.

### D3 — The outbox carries six scalar columns and no payload

ADR 0097 §2.5 makes "content-free" a property of the schema rather than a parser
convention: no payload column exists, so there is nothing to fill in later. The
content prohibition is therefore asserted as an exact column-set assertion
against `information_schema.columns`, not only as a comment.

### D4 — No foreign key from the outbox to `evidence_grants`

`evidence_ref` is an opaque handle (ADR 0097 §2.5), and retention is governed by
spec `evidence:253-254`, which keeps unacknowledged events while their grants
remain unexpired. A foreign key with `ON DELETE CASCADE` would delete an
unacknowledged event the moment the grant row is collected — the exact opposite
of that clause. The column is bounded in the DDL instead.

### D5 — `seq` is a blocking per-consumer counter row, not a sequence

ADR 0097 §4.2 disqualifies `SEQUENCE` because `nextval` is non-transactional, so
one rolled-back purge permanently burns a value, and disqualifies
`FOR UPDATE SKIP LOCKED` because a skipped allocation leaves an event with no
`seq` and pushes it outside the caller's transaction. The upsert-then-block form
is used so the first use of a consumer creates its counter row and later uses
take the row lock in a single round trip.

The rollback property is the whole reason this allocator exists, so it is the
assertion the real-PostgreSQL suite exists to make.

### D6 — The version check moves from the event to the envelope

ADR 0097 §2.1 withdrew the per-event `schema_version`: `invalidations` is an
action of the existing `iknow.evidence_result` envelope, which already carries
`schema_version: 1` (spec `:218-219`, `:252`). A separate method got its own
token pair in `evidence_request`/`evidence_result` precisely because it is a
separate method; an action of an existing envelope does not.

Fin's closed parser consequently drops to five fields, and the
`unsupported_evidence_contract` refusal relocates to the envelope so that ADR
0097 §2.7's typed exit stays reachable — register §1b.5a decides that unknown
integers must never be silently read as version 1.

`consumer_id` is stored and never sent (ADR 0097 §2.1): it identifies the
stream, not the event.

### D9 — The envelope head is `{schema, schema_version}` required, `action` optional

D6 as first written modelled the envelope as `{schema_version, action}` plus the
success fields. Code review found that wrong in both directions, and the fix is
the recorded decision:

- **`schema` is required.** Spec `:218-219` names the response token
  `iknow.evidence_result` and `:222` puts success fields "beyond
  token/version/action", so the token is a required response key. Fin's own
  `_read_envelope` already required exactly that token, and no shipped iknow
  renderer omits it. Omitting it refused every spec-conforming envelope.
- **`action` is optional, and must equal `invalidations` when present.** Spec
  `:222` names it, but no shipped iknow renderer echoes it and Fin's
  `_read_envelope` never checks it, so requiring it failed closed against the only
  producer that exists. Register §1.3 is where iknow starts emitting it; the check
  is already in place for that day.

Each of the three head keys then refuses under the class its kind of mistake
implies, mirroring the house `_check_schema_token` / `_check_schema_version`
split rather than inventing a rule: a value with no string or integer form is
`malformed_evidence_contract`, and a well-formed value naming something this build
does not implement — a foreign token, a version other than 1 — is
`unsupported_evidence_contract`.

The batch shape is stated once, in `tests/host/invalidation_batch.py`, written
from the spec rather than from the parser. Deriving the builders from the parser's
own constants would make an agreement assertion `x == x`, and would have hidden
the drift that left four suites asserting a payload the parser no longer accepts.

### D7 — `consumer_id` is an eighth transport variable, carried but not yet required on Fin

Register §1b.3 puts it in both repos' closed allowlists and on `evidence_grants`
and `GrantRequest`. IK makes it `NOT NULL`, which is where the refusal belongs:
a grant that cannot be attributed to a consumer cannot be invalidated per
consumer.

Fin keeps it optional on `EvidenceIdentityBinding` and forwards it only when
non-empty. Requiring it at that dataclass would change the constructor contract
of seven existing call sites and every live lane, which is a wider decision than
this slice owns. An absent consumer is absent on the wire, never present and
blank.

Neither side is connected to the other yet, and that is the state this slice
leaves behind rather than a property it claims: nothing in this repo
constructs an `EvidenceIdentityBinding`, and the drain's transport has no
production binder, so the consumer a binding would forward and the
`INVALIDATION_DRAIN_CONSUMER_ID` the drain reads are still two values. D7
describes what the transport can carry; wiring the binding into
`start_invalidation_drain_worker` is the composition-root decision that would
make the per-consumer separation hold end to end, and it belongs to the slice
that first starts the drain.

### D8 — `_validated_identity` becomes `_validated_event_id`

ADR 0097 §2.1 withdrew the per-event `schema_version`, so the helper that used
to validate identity AND version had only the identity left to check, under a
name that still promised two things. It is `_validated_event_id` here. Recorded
in this plan rather than in the closed P2 review round, which describes the
state of the code during that round and names the function as it stood then.

## Architecture gate

Cross-repo, twenty-plus files. Verdicts:

- bounded-context-guardian: yes — the DDL and the operations are separate modules
  because DDL renders SQL and the allocator owns one blocking statement; naming
  follows the capability, not a technical layer. The `schema-*.ts` family is
  inherited debt flagged by the prior architecture analysis, recorded here rather
  than widened with a fourth technical split.
- input-contract-tests: yes — all five boundary classes have an owner. Empty
  `evidence_ref` and empty `consumerId` refuse; `reason = 'expired'` is refused by
  the CHECK; `evidence_ref` over 128 characters is refused; two concurrent
  allocations for one consumer stay unique; and a rolled-back transaction does not
  burn a `seq`.
- error-handling-enforcer: yes — typed refusals on closed codes only.
  `EVIDENCE_FAILURE` is deliberately not widened here; §1b.5 assigns that member to
  §1.3, and widening it in this slice would ship a token no production path
  reaches.
- complexity-anti-drift: yes — one abstraction level per module, no god-function.
  The `consumer_id` propagation touches six files because it is one field on one
  existing data path, which is propagation and not a context split.
- minimal-change-verifier: yes — one logical task, the invalidation-outbox
  contract landing atomically on both sides that must agree on it, as the handoff
  and §2.8's "in the same change" mandate. Two repositories therefore produce two
  PRs of one task. The exclusions above are what keeps the diff inside that scope.

Ordering note: the three implementation branches were dispatched before this
block was written, which inverts the gate's intended order. All five verdicts are
`yes`, so nothing was accepted against a failing gate; the block is recorded
before any of that output is reviewed, committed or shipped.