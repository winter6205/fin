# P2 — the parser job service contract

Date: 2026-10-21
Status: accepted (frozen contract; the foundation implements §1–§9 as written here)
Authority: [`specs/harness-runtime-parser-service.md`](../../specs/harness-runtime-parser-service.md) Success Criteria 1–4 and 7, [`docs/adr/0103-durable-runtime-and-parser-ownership.md`](../adr/0103-durable-runtime-and-parser-ownership.md), [`docs/context-map.md`](../context-map.md) §Current physical contexts, and the local decision records `docs/wayfinder/tickets/hr-document-contract.md` and `docs/wayfinder/tickets/hr-parser-selection.md` (git-ignored local artifacts; read them from a local checkout).
Implements: the "contract section N" citations in the module docstrings under `src/fin/parser/` — §1 and §2 (`__init__.py`), §3 (`schema.py`), §4 (`ports.py`, `notifications.py`, `worker.py`, `results.py`), §3–§5 (`job_store.py`), §6 (`errors.py`), §7 (`api.py`), §8 (`notifications.py`, `redis_client.py`).
Blocks: P2 T8 (a fenced controlled parse result) and the P6 result-adoption work.

This is the source the parser module docstrings cite. Every acceptance point in
the package traces to a section below; a section that is not here is not a
contract.

## §1 bounded context

The parser service is an **independent bounded context**.

- It owns the **parser job**, the **parser attempt** that works on it, cancellation, and the durable **existence** of a **parser result envelope**.
- It owns **no** corpus identity, **no** attachment readiness, **no** indexing, **no** publication and **no** citation verification. fin keeps those authorities and decides whether a result is usable.
- Its durable state lives in its **own** PostgreSQL schema `fin_parser` on the existing PostgreSQL service. It never reads or writes `public.*`.
- The package imports **no** other `fin.*` package: `src/fin/parser/` imports only `fin.parser.*`, and no other context imports the parser. fin reaches the service over its **own HTTP API**; the service never calls fin.
- It has exactly two doors: its HTTP surface and its worker process. Both are separate OS processes.

## §2 module layout

| module | owns |
|---|---|
| `src/fin/parser/__init__.py` | the package's public surface |
| `envelope.py` | the versioned `ParsedDocument` envelope every adapter emits, and its validation |
| `errors.py` | the typed error taxonomy and its stable codes (§6) |
| `schema.py` | the `fin_parser` DDL (§3) |
| `ports.py` | the store port protocol and the value objects the ports speak in (§4) |
| `job_store.py` | the parser-owned PostgreSQL job store: acceptance, attempts, fencing, cancellation, results, outbox/inbox (§3–§5) |
| `results.py` | the result commit/read boundary, the read-only connection and the tombstone read (§4) |
| `staging.py` | the injected staging root and the orphan-collection pass |
| `worker.py` | the worker attempt authority: claim, lease, fence, commit, cancel (§5) |
| `worker_main.py` | the worker process CLI and its composition root |
| `notifications.py` | the outbox/inbox reconciliation, the notification port protocols and the Redis wakeup channel (§8) |
| `redis_client.py` | the Redis delivery transport (§8) |
| `api.py` | the HTTP surface (§7) |
| `app.py` | the FastAPI application that mounts the router |

No `controllers/`, `services/`, `repositories/`, `models/`, `utils/` or `shared/`
directory exists inside the package: the layout is by capability, not by
technical layer.

## §3 SQL DDL

The schema is `fin_parser` (`fin.parser.schema.PARSER_SCHEMA_NAME`). The DDL is
**idempotent** and runs when a store is constructed, so there is no separate
migration step and two stores may construct themselves without coordinating. A
statement runs only when its table is missing, because `CREATE TABLE IF NOT
EXISTS` still takes an exclusive lock on the table it inspects. A restricted role
that owns the tables but not the schema may use the store as it is.

| table | holds | key columns |
|---|---|---|
| `fin_parser.job` | one job | `job_id` PK, `idempotency_key` UNIQUE, `input_sha256`, `source_ref` (JSONB), `declared_format`, `detected_format`, `status`, `created_at`, `updated_at` |
| `fin_parser.attempt` | one worker attempt on a job | (`job_id`, `attempt_no`) PK, `fencing_token`, `worker_id`, `state`, `lease_expires_at`, `claimed_at`, `completed_at` |
| `fin_parser.result` | one committed parse result | `job_id` PK → `job`, `fencing_token`, `envelope` (JSONB), `result_ref`, `created_at` |
| `fin_parser.tombstone` | one deleted job's replay protection | `job_id` PK, `idempotency_key`, `input_sha256`, `created_at` |
| `fin_parser.outbox` | one durable notification | `notification_id` PK, `topic`, `payload` (JSONB), `created_at`, `published_at` |
| `fin_parser.inbox` | one received notification | `notification_id` PK, `received_at` |
| `fin_parser.staging_artifact` | one staging output | `artifact_id` PK, `job_id`, `fencing_token`, `path`, `referenced`, `created_at` |

Two CHECK constraints pin the vocabularies: the job status list on `job` (the
closed set of §7) and the attempt state list (`claimed` / `running` /
`succeeded` / `failed` / `expired`) on `attempt`. The job-status constraint is
aligned with `fin.parser.ports.JOB_STATUSES` on every construction, so a
schema that predates the vocabulary change is migrated rather than left
carrying a stale list. `result.result_ref` is a **reference** to derived
output, never the output bytes and never the source bytes.

## §4 ports protocols

The port protocols live with the code that owns them, so a caller never
depends on a driver.

- **Store protocol — `ParserJobStore`** (`src/fin/parser/ports.py`). The durable authority. It covers acceptance (`submit_job`), the job reads (`get_job`, `list_open_jobs`), the attempt lifecycle (`claim_attempt`, `renew_attempt`), cancellation (`cancel_job`, the two-step request of §5), the fenced result (`commit_result`, `read_result`), deletion (`delete_job`), the notification lane (`enqueue_notification`, `claim_unpublished_notifications`, `mark_notification_published`, `record_notification_received`, `unreconciled_notification_ids`) and the staging ledger (`register_staging_artifact`, `list_staging_artifacts`, `mark_staging_referenced`, `drop_staging_artifact`).
- **Reader protocols.** `NotificationPayloadReader` (`src/fin/parser/notifications.py`) reads one outbox row back by id, the one read the frozen store port does not expose. `fin.parser.results.parser_read_connection` is the context's single read-only connection: a read that cannot reach PostgreSQL is always `ParserPersistenceError`, never a bare driver exception. `fin.parser.results.read_tombstone` is the one read that answers for a deleted job.
- **Notification protocol — `NotificationTransport`** (`src/fin/parser/notifications.py`). `enabled`, `publish`, `consume`, `acknowledge` — the wakeup channel the service talks to (§8).
- **Worker seam — `WorkerAttemptStates`** (`src/fin/parser/worker.py`). `mark_running`, `release_attempt`, `fail_attempt`: the worker's own bookkeeping on its **own** attempt row. They never decide ownership; the fence belongs to the store.

Two rules hold for every implementation:

1. Failure is **raised** as a typed `fin.parser.errors` exception. It is never signalled by returning `None`, `-1` or `""`. `None` means one thing only: "there is no such row".
2. `fencing_token` is a **per-job** counter. A caller that holds a token proves it still owns the job only while that token is the job's current effective token.

## §5 fencing semantics

- The `fencing_token` starts at 1 and only increases. It is scoped to the job, never global.
- A claim runs in **one transaction that locks the job row**, so two workers can never hold the same effective attempt: every claim advances the token, and the earlier token is stale from that moment on.
- A claim on a job whose latest lease has **expired** is allowed and produces a **higher** token. The lease bounds the **renewal**; the token decides who may still commit.
- A renewal succeeds only while the token is still the job's current effective token and its lease has not expired.
- A commit succeeds only when the supplied token equals the job's current effective token, the job is in neither `cancelled` nor `cancel_requested` status, and the job is not tombstoned. A **superseded attempt writes nothing** — the refusal is `ParserStaleFenceError`, not a partial write — and a commit against a `cancelled` or `cancel_requested` job is refused as `ParserJobCancelledError`, writing nothing either.
- A cancellation is **two-step**, and its request is **durable at once**. `cancel_job` runs in **one transaction that locks the job row**: a job with **no live attempt** becomes `cancelled` in that transaction, and a job with a **live attempt** becomes `cancel_requested` in that transaction, with the attempt **fenced in the same transaction** (it leaves the claimable states and its lease ends now), so it can no longer commit. The worker then finalizes the request (`ParserWorker.finalize_cancelled`), and the job reaches `cancelled`. From the moment the request commits, the job is not claimable and no commit of any token is accepted, so no later action can make the job regain worker ownership: `cancel_requested` is a durable cancellation, not a pending suggestion.
- A deletion leaves a **replay-protection tombstone**; a late commit against a tombstoned job is refused, so a deleted job is never resurrected and its result is never published.

## §6 error classification

Every parser failure is one class of the `fin.parser.errors` taxonomy, and each
class owns a stable machine-readable `code`. Callers branch on the **type**; the
HTTP surface answers with the **code** alone. A fault in the parser's **staging
area** is a `ParserPersistenceError`, because the staging area is part of the
parser-owned durable area, so the taxonomy keeps one class per failure kind.

| class | code | meaning | HTTP |
|---|---|---|---|
| `ParserError` | `parser_error` | the base class of every parser failure | 500 |
| `ParserInvalidRequestError` | `invalid_request` | the request identity or input is empty, missing or malformed | 400 |
| `ParserEnvelopeError` | `envelope_invalid` | a parse result does not satisfy the envelope contract | 422 |
| `ParserJobNotFoundError` | `job_not_found` | no job carries the requested id | 404 |
| `ParserJobConflictError` | `job_conflict` | an idempotency key was reused with a different input | 409 |
| `ParserJobCancelledError` | `job_cancelled` | the job was cancelled, so it may not proceed or commit | 409 |
| `ParserJobTombstonedError` | `job_tombstoned` | the job was deleted; its tombstone blocks any replay | 409 |
| `ParserQueueWaitTimeoutError` | `parser_queue_wait_timeout` | the job waited past the queue-wait deadline; it is not claimable and becomes `timed_out` | 409 |
| `ParserExecutionTimeoutError` | `parser_execution_timeout` | the attempt ran past the execution deadline; the result is refused and the job becomes `timed_out` | 409 |
| `ParserStaleFenceError` | `stale_fence` | the attempt's fencing token is no longer the effective one | 409 |
| `ParserJobCapacityError` | `job_capacity` | the open-job limit would be exceeded by accepting this work | 429 |
| `ParserQueueFullError` | `parser_queue_full` | the pending-queue cap would be exceeded by accepting this work | 429 |
| `ParserPersistenceError` | `persistence_unavailable` | the parser-owned store could not be read or written | 503 |
| `ParserNotificationError` | `notification_failed` | a notification could not be published, received or reconciled | 503 |

The status column is the surface's answer, not a second taxonomy: the table
lives in one place in the code (`src/fin/parser/api.py`, `_ERROR_TABLE`), so a
class and its status cannot drift apart.

`details` carries structured context for logs and internal handling only. It
never reaches an API body, and neither does a filesystem path, a digest beyond
the caller's own input, or a connection string.

## §7 HTTP contract

The surface is one FastAPI router mounted under `/v1`.

| route | answer |
|---|---|
| `POST /v1/jobs` | `202` for a new acceptance, `200` for a replay of the same key with the same input; body `{job_id, status, replayed}` |
| `GET /v1/jobs/{job_id}` | `200` with the durable status of the job |
| `GET /v1/jobs/{job_id}/result` | `200` with the durable result (`job_id`, `fencing_token`, `envelope`, `result_ref`, `created_at`) |
| `POST /v1/jobs/{job_id}/cancel` | `200` with the job's **current** status: `cancelled` when the job had no live attempt, `cancel_requested` while a live attempt is being finalized, and `cancelled` once that attempt is final |
| `DELETE /v1/jobs/{job_id}` | `204`; the job is deleted and a replay-protection tombstone is left; `204` again when the job was already deleted — nothing is left to do, and the tombstone stays as it is; `404` `job_not_found` when no job and no tombstone ever carried that id |

**The cancel answer reports the current status.** The cancellation request is durable the moment it commits, but the job may still be finishing: a job that had a live attempt is `cancel_requested` until the worker finalizes it, and `cancelled` from then on. A client that reads `cancel_requested` holds a cancellation that cannot be undone — the attempt is already fenced and can no longer commit (see §5).

Request contract for `POST /v1/jobs`: `idempotency_key` (non-empty, ≤ 512
characters, no control character), `input.sha256` (exactly 64 lowercase hex
characters), `input.artifact_ref` (optional, ≤ 1024 characters), `input.declared_format` (optional, ≤ 64 characters). A body that does not match the shape is a malformed request, refused before any write.

A rejection is always typed, and the body of a failure carries the stable token
only: `{"error": "<code>"}`.

The answer is written **only after the job row is committed**, so a client that
holds a `202` holds a durable job. A same-key/same-input retry converges on the
same job; a same-key/different-input retry is `job_conflict`.

**Status vocabulary (closed set):** `queued`, `running`, `cancel_requested`,
`succeeded`, `partial`, `failed`, `cancelled`, `timed_out` — the job states the
local decision record `docs/wayfinder/tickets/hr-document-contract.md` fixes.

**Operating limits:** a 20-job pending queue, a 600-second maximum queue wait,
and a 900-second maximum execution — the starting configuration the local
decision records `hr-document-contract.md` and `hr-parser-selection.md` fix.
Queue saturation is an explicit capacity answer, never an unbounded acceptance:
`parser_queue_full` when the pending queue is full, `job_capacity` when the
open-job ceiling would be passed.

## §8 notification semantics

- The parser-owned PostgreSQL **outbox is the fact source**. Redis is a
  **bounded wakeup channel** and nothing else.
- What is bounded is the **Redis stream**: every publish trims it to the
  configured maximum number of entries, `FIN_PARSER_REDIS_STREAM_MAXLEN`
  (1000 by default; a malformed or non-positive value falls back to that
  default). When the bound is exceeded the **older entries are trimmed**. A
  trimmed wakeup is **not a lost job**: the outbox row is still there, and
  reconciliation republishes what a delivery missed.
- The **durable outbox and inbox are not trimmed** by this slice: the
  durable log's retention belongs to a later slice, and reconciliation depends
  on those rows.
- Redis may be down, may restart, and may deliver the same notification twice.
  None of that may lose or corrupt job state: every notification is durable in
  the outbox before it is published.
- The **inbox** makes a delivery idempotent: `record_notification_received`
  answers `False` for a duplicate id and `True` for the first delivery.
- **Reconciliation** finds what a delivery missed: `unreconciled_notification_ids`
  lists the outbox ids that were never published or never confirmed, and the
  payload is read back through `NotificationPayloadReader`.
- The transport is **optional**: with no `FIN_REDIS_URL` the client is disabled
  and unpublished outbox rows simply accumulate. When Redis is unreachable every
  command raises `ParserNotificationError` instead of fabricating a delivery.
- The transport is Redis **Streams** with a consumer group, created at id `0-0`
  so a wakeup published before the consumer existed is still delivered.
- No second broker and no workflow engine is introduced.

## §9 test conventions

- **Real PostgreSQL** through `FIN_TEST_DSN`. A database whose name does not end
  with `_test` is **refused, not skipped**: connecting to a development database
  would destroy the operator's data. The suite never truncates a table in a
  database the session does not own: in the shared test database every key a
  case writes carries a run prefix and the teardown removes exactly those
  rows, while a whole-table truncate happens only inside a session-private
  test database that the session creates and drops.
- **Real Redis** through `FIN_REDIS_URL`, on a dedicated Redis database and a
  private stream per case. "Redis is down" is a real client pointed at a
  refused port; "Redis restarted" is a flush of the dedicated database.
- **Two OS processes with a parent-controlled barrier** for the ownership
  races, so the interleaving is decided by the test and not by a race.
- **A fresh process** for the restart reads: a status, a cancellation or a
  result that must survive a restart is read by a new interpreter.
- **Injected temporary roots** for staging and generated artifacts. The parser
  never falls back to the operator's home, and a root inside `~/.fin` is refused.
- **No mocks of the object under test.** A stand-in is used only for a
  dependency the case is about (an unreachable store, a refused Redis port), and
  never for the subject.
- **No skip and no xfail** to hide a failure. The only skip is the environment
  guard that says the required external dependency is absent (`FIN_TEST_DSN` or
  `FIN_REDIS_URL` unset); a run that has both reports `0 skipped`.
- A **restricted role** that may touch only the `fin_parser` schema is what
  proves the store needs no database-wide rights.

## Implementation status (2026-10-21)

The foundation implements §1–§9 as written here. Two things a reader should
know about the slice's edge:

1. **The limits are configurable, and the values above are the defaults.**
   `FIN_PARSER_MAX_PENDING_JOBS` (20), `FIN_PARSER_QUEUE_WAIT_SECONDS` (600)
   and `FIN_PARSER_EXECUTION_SECONDS` (900) are the operating limits in their
   default form; a deployment may lower them, and a test lowers them to cross a
   deadline without waiting for it. `FIN_PARSER_MAX_OPEN_JOBS` (1000) is the
   separate ceiling on jobs that are open at once, and `FIN_PARSER_DSN` names
   the parser's own database.
2. **Parser fidelity.** This contract covers the job lifecycle only. No adapter
   fidelity and no fin readiness adoption is claimed here; both are P6.
