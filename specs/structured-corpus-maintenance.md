# Spec: incremental structured corpus maintenance

Status: **Review-ready contract, 2026-10-03; implementation pending.** The
operator confirmed staged source updates, retained historical evidence, and
finite collection. Routine execution choices were delegated to the author.
This contract does not claim that existing index jobs already satisfy it.

## Objective

Publish structured knowledge from accepted versioned extraction, keep updates
recoverable, expose honest readiness, and physically collect obsolete payloads.
Maintenance is owned by iknow behind the existing retrieve seam; fin consumes
bounded results/status and owns expiry of its answer snapshots.

The [query contract](structured-corpus-query.md) owns subjects and equivalence.
The [evidence contract](structured-corpus-evidence.md) owns immutable source and
support identity and historical retention obligations. This contract owns their
lifecycle, durable jobs, publication guards, readiness, and collection protocol.

## Assumption gate

1. **Operator-confirmed:** v2 replaces published v1 only after successful staged
   extraction/validation/publication; failure keeps v1 serving with explicit
   pending/failed refresh status. Withdrawal is a separate immediate operation.
2. **Operator-confirmed:** supports retire individually; another active origin
   preserves the current assertion. Retirement does not declare an assertion false.
3. **Operator-confirmed:** current entities/views are mutable and rebuildable;
   persisted source versions, extraction records, supports, and decisions retain
   their identity. No default newest-wins resolution is permitted.
4. **Operator-confirmed:** normal garbage collection has finite 7/90/365-day
   thresholds; current dependencies, bounded readers, and unexpired row-evidence
   content references protect source payloads. Deletion is checked twice and can be retried.
5. **Delegated choice:** reuse index jobs/outbox with separate check readiness;
   typed structural candidates, bounded retries, 7-day collector grace, and
   separate query-data versus maintenance-progress revisions.
6. **Inherited:** no new global queue, no semantic truth adjudicator, no direct
   fin-to-iknow imports. New behavior must be implemented and verified separately.

## Boundaries

- **Does:** stage/activate/withdraw source lifecycle; immutable support updates;
  exact current compilation; conflict candidate checks; durable job/retry/status
  contracts; intermediate-write fencing; rebuild versus re-extraction; finite
  GC and explicit purge; safety requirements on consumers of derived artifacts.
- **Confirms with human:** source lifecycle and retention are confirmed; remaining
  technical defaults are delegated. Automatic adjudication and broad identity
  governance require a later decision if pursued.
- **Out of this spec:** semantic entity merge/split workflows; LLM/Jev provider
  selection; maintenance UI; numeric sums/claims; a new global scheduler;
  ES/PG multi-store reconciliation internals; capacity policy and backup erasure.

## Inherits / Changes

Exact text from `docs/CONTEXT.md`, **iknow-memory**:

> A separate TypeScript MCP server (own repo). It is the product 库: after ingest/push, **资料** are queried through fin's **retrieve port** against this backend (ADR 0045); a distinct **memory port** channel may hold explicit pins and must not share one config key.

Exact text from `docs/CONTEXT.md`, **派生索引**:

> 向量索引与词法索引，是权威源的派生物；可删除、可从权威源重灌，重灌不影响权威数据。因此向量库可平滑升级而不动权威数据。

ADRs 0090/0094 preserve corpus ownership, incremental checks, provenance,
explicit resolutions, separate readiness, and stale-worker fencing. ADRs
0091–0093 remain unchanged for legacy row evidence. This contract refines the
accepted direction without claiming that the tracked change is satisfied.

Existing iknow index jobs, maintenance handlers, dedup helpers, and projection
outbox are reuse candidates. Read-only exploration found that index readiness
is not conflict readiness, some duplicate writes preserve only one origin, and
terminal failed jobs are not proven retryable. Existing recency/confidence-based
supersession cannot be applied as this path's default. Compatibility requires
an isolated new capability and explicit opt-in; rewriting legacy historical
origins from guesses is prohibited.

## Source lifecycle and current compilation

Each logical source has `desired_version`, nullable `published_version`, desired
and published extraction revisions, a monotonic `source_generation`, and an
access/withdrawal tombstone. The serving pointer is the pair of source version
and accepted extraction revision. Either desired component can differ from its
published component while an update is in progress.

| Event | Serving state immediately afterward | Historical state |
|---|---|---|
| First ingest starts | No published source; index pending/running | Staged immutable source retained |
| v2 received while v1 is published | Continue v1; desired v2 pending/running | v1 evidence remains active |
| v2 extraction fails | Continue v1; desired v2 failed | Failed stage retained for bounded repair |
| Valid v2 publishes | One transaction switches pointer and active support set to v2 | v1 becomes retired; old support inactive, evidence retained |
| New extraction of unchanged v1 publishes | Atomically replace accepted occurrence set and extraction pointer | Shared occurrences keep support IDs; omitted occurrences and old extraction retire |
| Source withdrawn | Tombstone increments generation; remove its supports from current visibility immediately | Historical evidence subject to current access and retention |
| Access revoked or explicit purge requested | Stop affected current and historical visibility immediately | Purge invalidates references; no grace for visibility |

Publication is a single authoritative transaction: pointer switch, support
activation/retirement, affected entity/assertion compilation, query revision,
status, and outbox scheduling commit together. Re-extraction also switches the
published extraction pointer atomically; prior extraction links alone never keep
omitted occurrences active. A reader never observes a source
whose v1 supports were removed before its v2 supports were ready. Scope access
checks remain authoritative even if an external index has not caught up.

The current view is the union of accepted active supports from published,
non-withdrawn, authorized source versions, subject to explicit recorded
resolution decisions. Updating one source retires only that source's former
supports. An assertion remains current while at least one active authorized
support remains; when the last disappears it leaves current query results.
Historical identity/evidence remains until retention permits collection.
Exact active-set membership must determine this transition; a bounded display
list or saturated support counter cannot determine whether the set is empty.

An unchanged normalized assertion retains its ID across re-extraction and
projection rebuild. A changed endpoint/relation/valid interval receives a
separate assertion ID under the query contract. Ordinary attributes update the
same entity ID; no semantic alias/embedding merge is introduced. Explicit
identity corrections require a recorded redirect/decision and invalidate the
query revision; full merge/split workflows are a separate issue.

Extraction/publication time is distinct from a fact's business validity interval.
Neither a late source nor high confidence automatically supersedes another
active origin. Missing validity bounds mean unknown/unbounded, not proof of
simultaneous real-world truth. Cross-source alternatives remain queryable with
attribution until an explicit versioned decision/rule resolves them.

## Durable work and publication fences

### Job interface

Reuse durable job/outbox storage where it satisfies the contract. Work kinds
are `extract_publish`, `conflict_check`, and `projection_rebuild`; they have
independent state. The persisted job key binds work kind, source reference and
version/generation, input revision, and config/ruleset revision. Repeated
scheduling of the same key joins one job. An explicit retry creates a new attempt
under that job with history preserved.

`input_revision` includes a canonical sorted dependency vector of source,
generation, accepted extraction, and affected relation-bucket revisions, not
just the initiating source's content hash. Different dependency sets cannot join
the same conflict-check/rebuild job. Desired extraction revision is checked at
publication as well as source version; a changed model can supersede work even
when source content is unchanged.

Each attempt has an unguessable fencing token, lease expiry, attempt number,
input/dependency revisions, and a stage namespace. Source stage writes are
isolated from serving tables; every write checks the token and generation.
Expired or superseded attempts cannot append supports, rewrite entities, update
status for a newer attempt, or expose a projection merely because they might
pass a later final check.

Before conditional publication, recheck source generation and desired version,
accepted extraction/input revision, complete affected dependency revisions,
config/ruleset revision, attempt token, and lease. Compile affected current
sets inside the serialized publication transaction. Concurrent changes to an
affected relation bucket/source invalidate the attempt and require recomputation;
unrelated progress updates do not. Unique exact identity keys prevent parallel
workers from minting duplicate assertion IDs. A-to-B-to-A activation still
changes generation; content equality never excuses an obsolete attempt.

Conflict checks bind both sides' accepted input revisions. A check for source
A becomes stale if dependent B changes even when A does not. Missing dependency
metadata cannot produce `ready`. Publication guards must apply to intermediate
check/projection writes as well as final status updates.

### State and recovery

| State | Meaning / legal next transition |
|---|---|
| `pending` | Durable work exists; an available worker may claim `running` |
| `running` | One leased attempt owns isolated stage work; may become ready/failed/stale |
| `ready` | Work completed and published for its exact bound inputs; becomes stale if those inputs change |
| `failed` | Current attempt stopped with a typed cause; explicit retry can create a pending new attempt |
| `stale` | Input superseded or guard lost; no publication; fresh work must bind current inputs |

Automatic transient retry permits at most 3 attempts per job, delayed by 5 then
30 seconds; after exhaustion expose `failed`. Each attempt has a 5-minute worker
lease, heartbeat at most every minute, and a 30-minute absolute execution limit.
A crash releases work at lease expiry; reclaimer invalidates the old token before
claiming a new attempt. Input supersession is stale, not a successful completion.
Validation/config failures are not automatically retried. Resource exhaustion
is a visible typed failure; retry never hides it with empty output.

Request reader leases are different: a query/evidence read must finish within
60 seconds or cancel with a typed timeout. Pagination has a 10-minute fixed
cursor deadline and retains no cross-request database transaction. Long-lived
jobs hold explicit input dependencies only while their valid worker lease and
absolute attempt bound remain active; stale tokens cannot indefinitely pin GC.

### Rebuild versus re-extract

`projection_rebuild` consumes persisted accepted extraction, supports, and
recorded decisions. It preserves assertion/source identity and cannot silently
run a new extraction model. A model/prompt/parser change schedules explicit
re-extraction against bound retained inputs and produces a new extraction
revision before publication. Community/report generation may use an LLM, but
its result remains a derived artifact with complete dependency revisions; it is
never authority for replacing original assertions.

Every derived generation carries dependencies and publication revision. A source
withdrawal, purge, access change, or dependency change invalidates affected
reports/indices immediately. Consumers must authorize and check dependencies
against canonical lifecycle state before serving; otherwise suppress the whole
artifact or return a typed unavailable/degraded result. Multi-source reports
cannot be made safe by removing one visible citation. If complete dependencies
are missing, that generation is not eligible to serve. Durable outbox rebuild
work restores readiness later. Detailed ES/PG reconciliation is a follow-up
issue; this safety obligation is part of first delivery and cannot be deferred.

## Conflict candidates and readiness

The initial ruleset is `structural-alternatives`, revision `1`:

- For the same canonical head and normalized relation, different tails with
  overlapping half-open valid intervals form a **potential alternative**.
- Both null validity bounds mean unknown/unbounded for overlap screening. The
  candidate records this uncertainty; it does not claim a semantic contradiction.
- Exact same head/relation/tail/interval is one assertion with multiple supports,
  not a conflict. Distinct intervals remain distinct assertions.
- A versioned relation catalog may label relations multi-valued or exclusive.
  Known multi-valued relations do not create a conflict candidate by tail difference
  alone; unclassified relations yield potential alternatives, never contradiction
  verdicts. Initial catalog is empty; no domain cardinality is invented.

Candidate identity binds sorted assertion IDs, relevant support/input revisions,
rule revision, and uncertainty reason. Checks publish immutable evidence and
status, not automatic retirement or truth selection. `ready` means the declared
structural screen completed, not that all knowledge is correct or semantically
consistent. No Jev routing result counts as corpus adjudication.

The minimal recorded-decision interface is backend-owned: decision ID, actor,
reason, affected assertion/support IDs, expected input revisions, outcome
(`keep_both`, `retire_support`, or `supersede_assertion`), and timestamp. Accept
only currently matching inputs in a transaction; retain the decision's evidence
and increment query revision when current visibility changes. The first delivery
needs this persistence/guard seam, not a UI or automatic semantic decision maker.
A decision cannot erase source history or make a hidden support public.

### Response status

Every query includes this shape (example counts are illustrative):

```json
{
  "maintenance_revision": "opaque-progress-revision",
  "sources_in_scope": 2,
  "published_sources": 1,
  "desired_index": {"pending": 1, "running": 0, "ready": 1, "failed": 0, "stale": 0},
  "conflict_checks": {"pending": 1, "running": 0, "ready": 1, "failed": 0, "stale": 0},
  "ruleset_revision": "structural-alternatives:1",
  "coverage": "structural_candidates_only"
}
```

Scope is the server-authorized set of active logical company sources, including
ones with no indexed entities or no matching rows. It is not the page/top-k or
only sources that produced a hit. Each active scoped source occupies exactly
one desired-index and one conflict-check bucket; bucket totals equal
`sources_in_scope`. A first ingest without scheduled work reports pending;
missing required state records produce `maintenance_status_unavailable`.
`published_sources` counts sources with a usable published version and can be
nonzero when their desired-index bucket is pending/failed. Scope-local state
must not reveal hidden-source counts.

`desired_index` means canonical structured extraction/publication readiness for
the desired pair, not acknowledgement by every external search store.
`conflict_checks` means checks targeting that desired pair and current affected
dependencies: it remains pending while extraction is pending, even if an older
published pair had a completed check. External-store readiness is distinct;
its detailed reconciliation is the tracked change, while serving guards apply immediately.

`read_revision` changes for query-visible data/display values, published
extraction, active support, identity, resolution, or scope authorization changes.
`maintenance_revision` changes for
job progress/status only. Heartbeats and pending-to-running progress do not
invalidate pagination; scope access changes do. Rows, count, and this summary
come from one consistent authorized read snapshot. Count coverage is only the
stored structured subjects; a pending source is not silently claimed extracted.

## Physical collection

Retention deadlines belong to the evidence contract. Collector eligibility
additionally requires no current source/current-knowledge dependency, no valid
job or reader lease, and no unexpired row-evidence content dependency. A count
grant protects only its receipt/shared authorization metadata, so it does not
delay source payload collection. Explicit scope purge/permission changes still
invalidate the count through the evidence contract's revision checks. A source can be retired
while a decision or another current compiled object still depends on it; that
payload is not garbage until the dependency is removed or materialized independently.

| Payload | Earliest candidate time |
|---|---|
| Failed/stale attempt stage | Terminal state + 7 days; no live retry/input dependency |
| Replaced derived generation/cache | Stable replacement + 7 days; no current dependency or reader |
| Retired unreferenced source/extraction/support | Retirement + 90 days |
| Retired evidence protected by row grants | Later of retirement + 90 days and last finite row-reference expiry; no later than retirement + 365 days absent a current dependency |
| Completed job detailed logs | Terminal state + 90 days; retain only brief non-content outcome afterward |
| Expired backend/fin snapshot payloads | Reference expiry, or authorized purge |

Collection is **mark, wait 7 days, recheck, delete**. Candidate time and physical
purge time are separate: an unreferenced source retired on day 0 is eligible on
day 90 and can be physically deleted on day 97. A protected reference expiring
on day 365 can delay normal physical deletion until day 372. Day values are
elapsed UTC 24-hour periods; no sliding deadlines based on reads.

1. Atomically mark a candidate with expected lifecycle revision and deadline;
   stop new evidence obligations/activations against that candidate. A requested
   reactivation must transactionally cancel the mark before creating dependencies.
2. After grace, lock/recheck eligibility, revision, finite references that protect
   this payload and leases. Count-only grants do not protect source payloads.
   If protected or revision changed, cancel/re-evaluate; never delete
   based solely on an earlier scan.
3. Commit a purge tombstone before removing payloads. Delete dependent supports,
   extraction payloads, frozen units/objects, superseded caches, and snapshot
   payloads in dependency order. Persist progress for retry; no stale worker can
   resurrect the deleted identity. Shared physical objects require exact live
   reference accounting, not deletion by one logical source's hash alone.
4. Preserve only minimal non-content tombstone/outcome data. Fin collects its
   expired snapshot payloads on the same deadlines; history can keep citation
   identifiers and an unavailable reason. The backend owns reference grants,
   so collection never guesses whether fin still has an answer.

Explicit purge skips normal age/grace delays, immediately denies new reads and
references, cancels affected jobs, and waits at most the bounded active-read
execution before physical removal. Purge retries are idempotent. Backup copies
require a separate erasure/restore policy. Ordinary GC does not delete current
knowledge merely because it is old; unlimited current ingestion requires a
capacity policy, not a promise that historical GC bounds total database size.

The purge transaction also schedules the evidence contract's durable fin
invalidation events. Local backend deletion and fin snapshot scrubbing have
separate persisted outcomes; all-owner purge completion requires the configured
snapshot owner's acknowledgement. Offline/unreachable owners leave that phase
pending and must reconcile before serving evidence after restart. No successful
completion is recorded while copied live snapshot payloads remain unacknowledged.

Typed work failures are `invalid_input`, `access_denied`, `unsupported_source_binding`,
`input_stale`, `lease_lost`, `ruleset_unavailable`, `resource_exhausted`,
`backend_unavailable`, and `execution_timeout`. A collector failure persists its
phase and retries, never records successful deletion before payload removal.
External error mapping uses explicit code fields, not message matching.

## Success Criteria

These are **future implementation acceptance cases**. Freeze public original
inputs and an independent expected timeline before implementing behavior.

| ID | Required scenario and binary expected result |
|---|---|
| M1 | Crash after scheduling: durable outbox recovers exactly one logical job, with no query-triggered writes |
| M2 | v1 published, v2 pending/failed: v1 remains queryable with honest desired status; successful v2 switches pointer/supports/current view atomically |
| M3 | Two active origins: withdrawing one preserves the assertion; withdrawing the last removes it from current list/count and preserves old authorized evidence |
| M4 | A-to-B-to-A, model/rule change, or dependency B update: obsolete attempt cannot publish or alter serving intermediate records/status |
| M5 | Duplicate/parallel extraction retains one exact assertion ID and all distinct supports; ordinary update/rebuild preserves entity IDs |
| M6 | Worker lease expires and retry claims: old token cannot heartbeat/publish/pin GC; crashes recover within the bounded lease |
| M7 | Failed job retries preserve attempts; three transient attempts exhaust to failed; invalid inputs do not silently loop |
| M8 | Structural alternatives with overlap become potential candidates; non-overlap, exact duplicates, and known multi-valued relations do not; no newest-wins mutation |
| M9 | Stale resolution refuses; current guarded resolution changes visibility and retains its original evidence; readiness never asserts semantic truth |
| M10 | Progress-only update changes maintenance revision but cursor stays valid; source publication/withdrawal/access change expires the cursor |
| M11 | No matching rows and one pending source: scope status still counts that source; access-hidden sources do not appear in totals |
| M12 | Projection rebuild performs no new assertion extraction; explicit re-extraction records a new revision without rewriting source history |
| M13 | Withdraw a source contributing to an old multi-source report: stale artifact is suppressed before external-index reconciliation; missing dependencies refuse serving |
| M14 | Fake-clock days 7/90/365 plus grace: active dependencies/leases/row-content references prevent source payload purge; nonrenewing expired row references release payloads at the defined boundary |
| M15 | Race reactivation/reference creation with mark/purge: atomic guards prevent deletion of new protected input and reject resurrection of tombstoned identity |
| M16 | Crash midway through purge and retry: each payload is deleted once or safely retried, brief audit survives, and successful completion is not premature |
| M17 | Explicit purge bypasses retention, denies all affected current/historical reads immediately, and finishes active-read cancellation within 60 seconds |
| M18 | Re-extract the same source version with a new model that omits a prior relation: failure keeps the old set, successful publication retires only omitted occurrences, preserves shared IDs, and leaves historical snapshots replayable |
| M19 | Before outbox reconciliation, preexisting ES/vector/graph hits from a withdrawn/purged sole source cannot return or be cited; missing lifecycle guards cause typed unavailable/degraded status |
| M20 | Normal source payload GC ignores count-only grants while preserving their receipt; explicit purge advances scope purge revision and stays pending until fin snapshot scrub is acknowledged |

## Open Questions

No blocking architecture fork remains. The real acceptance corpus and resource
measurements are implementation prerequisites, with no claimed live acceptance
pass. A rollout against an existing corpus requires explicit re-ingest/migration
planning; old lost origins cannot be reconstructed by assumption.

### Follow-up issues

- derived-store revision reconciliation
- current-data/storage capacity and admission policy
- backup erasure and safe restore
- explicit semantic entity merge/split workflows

These issues do not defer the immediate visibility guard, finite GC contract,
or stable identity requirements defined here.

## Pending persistence and handoff

The confirmed source lifecycle is persisted through `domain-modeling` in
[ADR 0095](../docs/adr/0095-immutable-source-history-and-finite-retention.md),
with shared terminology in [CONTEXT](../docs/CONTEXT.md). Operational states,
retention thresholds, and collector guards stay owned by these contracts.
The shared [architecture review](structured-corpus-query.md#architecture-review)
covers all three specs. Hand them together to a later `writing-plans` phase.
No implementation plan, runtime migration, or release is performed in this session.
