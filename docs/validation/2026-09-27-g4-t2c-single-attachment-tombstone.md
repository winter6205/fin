# G4 A.T2c — single-attachment tombstone, void-before-cleanup, delete door

Date: 2026-09-27 (evidence re-run case by case the same day; re-measured again
after the four review repairs below landed — see "Commands", rows 16–19; then
reconciled once more against the four second-round repair merges — see
"Commands", rows 20–24, which supersede rows 16–19 wherever they differ)
Branch: `feat/g4-attachment-t2c` (base `origin/main` `cc1a9cb`), HEAD `e6333a9`,
whose tree content is the merge tip `0b7775c`.
Commits, in order: `9219609` the inherited void path with its failing test layer
(`wip(g4)`, squash on merge), `4e7f4dd feat(g4-web)` chip delete, `3d0f6ca`
search and log clauses, `4344250` this record, `0587d69` the host↔web path
guard, `7c1e04b` dropping a duplicated statement the previous session left
uncommitted, and the record correction that carries this line — the tip every
**pre-repair** number below was taken at.
Then the review round, four repairs merged on top of it, each named by its head
commit with the siblings that carry part of the same finding: `325a2e7` the
locator guard on every attachment door that takes one; `51d9678` the chip's
delete control driven through the real element tree instead of scraped from
source; `0854988` with `33c4315` (retire in-memory obligations with their thread)
and `c9cf435` (route both Postgres deletion doors through one void); and
`212471c` with `9532a35` (read the order while the byte store is touched) and
`19c0a6b` (name the blocked writer by its wait chain). `e6333a9` is this record
rewrite, landed as `d20986a`.
Then a SECOND review round, four merges on top of `d20986a`: `9d4d12f`
(`9649bc2` never let a store deletion drop an unnamed obligation, `b7a4781`
answer a shadowed void from the record it wrote), `71b8dff` (`5bbf70f` pin the
void's park to the tombstone write, `17d6675` park the void inside its row
lookup so both lock halves bite), `68e0b5a` (`8f0f7e4` state the locator
refusal once and close the verdict set) and `cebfd02` (`0c35e92` state each
attachment door's real answers in the client, `77c6cf8` and `55c90c2` name the
host doors the client has no wrapper for and the shared miss cause, `88752cd`
drop the last source-text scrape from the chip's suite). `cebfd02` is the
content tip every **[r2]** row and every row 20–24 figure below was taken at;
rows 16–19 stand as the first round's `0b7775c` captures, superseded where
they differ.

Inherited contract: `docs/adr/0089-lazy-page-region-attachment-preparation.md`
(upload stores raw bytes only; preparation is requested later) and
`docs/validation/2026-09-26-g4-t2b-raw-upload-send-gate.md` (the upload door,
its typed store-failure clause, and the live-door harness this record reuses).
T2b established that a thread delete refuses with `attachment_cleanup_failed`
rather than acknowledging a thread whose bytes remain. T2c extends that to one
attachment and makes the ordering explicit: the **void lands first**, the bytes
go after, and anything still owed stays recorded.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | Rows 1–15 of "Commands" were executed at `7c1e04b` — every command there, the two long runs included. At that tip the only post-record change under `src/` was one comment-only correction to `AttachmentCleanupError`'s docstring, which asserted "nothing durable is removed" — false from the moment the void was ordered first, since the void does remove the attachment row and its derived layers. It moved no statement: `uv run --no-sync pytest -q` over the three cleanup node ids of clause 4 returned `3 passed in 0.73s` after it, and the `s5-complexity` ratchet over `--base cc1a9cb` still read `13 touched function(s) within baseline`. Rows 16–19 were executed on this branch at `e6333a9` (content `0b7775c`), which is **not** a tests-only state: the repairs changed `src/fin/host/workspace_http.py` (the locator guard on all three doors), `src/fin/host/attachment_store.py` (one `_void` behind both doors in both adapters, obligations retired with their thread), `src/fin/host/attachment_prepare.py` (the order read from inside the byte delete, one authoritative cleanup log line) and the `web/` chip, so the pre-repair totals stay in this file as baselines and are labeled as such wherever they appear. Rows 20–24 were executed at the second-round content tip `cebfd02`, whose second-round `src/` changes are all in the three files already named: `attachment_store.py` (`_retire_gone_locked` is THE lock-entering retirement step for all five cleanup doors — `tombstone`, `delete_for_thread`, `pending_cleanup`, `confirm_cleanup`, `confirm_thread_cleanup`; `create()` refuses an id that still owes a cleanup, on both adapters; the Postgres `_void` opens with a locking row read and no longer returns before its `DELETE`, answering a shadowed or retried void from the record it wrote), `attachment_prepare.py` + `workspace_http.py` (the three attachment doors share one `_locator_refusal`; the byte-delete verdict set closed to the two live verdicts and the dead `_OUTSTANDING` / `_ORPHANED` constants were deleted), and `web/` (per-door answer lists in `api.ts` / `attachFailure.ts`, comment-only in the last two rounds) |
| Python | 3.12.14, `uv run pytest` 9.1.1 (hermetic default suite), `uv run --no-sync pytest -m mcp` for opt-in |
| Durable metadata | Postgres `16.15` (`show server_version`, `fin-postgres-1`, image `postgres:16-alpine`, published `localhost:5433/fin`, 12 `fin_%` tables present), `FIN_CHECKPOINT_URL` set in the shell only to the `.env.example:23` template value, expanded — recomputed `sha256` prefix `3856bda1`, matching the value first recorded here. No DSN, credential, or expanded connection string appears in this file, in a commit message, or in the PR body — the variable NAME `FIN_CHECKPOINT_URL` is the only identifier used. Not automatic: at the re-run the docker daemon was not running and `fin-postgres-1` was `Exited (255)`, so both had to be started before a single Postgres case could execute. Rows 16 and 17 ran at the new tip with `FIN_CHECKPOINT_URL` present in the environment, which is why the Postgres legs of the store file report passes there instead of the skips row 1 lists. Rows 20–22 ran with the variable loaded from the repo's gitignored `.env` rather than re-typed from the template: the two values differ in the credential segment, so this round's expanded value hashes to prefix `8229bf8a` while the `.env.example:23` template string itself still hashes to `3856bda1` (both recomputed over `sha256sum`; only the prefixes appear here, never a DSN). `show server_version` re-read as `16.15` and the `fin_%` table count re-read as 12; `fin-postgres-1` and `fin-g4-t2c-s3` were already up this time (`docker ps`) |
| Byte store (live suite) | SeaweedFS S3 gateway 4.43, image `chrislusf/seaweedfs:4.43`, container image id `sha256:7bea581f48155c069d3c725e60c386c88210c67cde8bce412344ff6ebea264da` (re-checked with `docker inspect`, matches), container `fin-g4-t2c-s3` started as `server -s3 -s3.port=8333 -s3.ip.bind=0.0.0.0 -ip=127.0.0.1 -dir=/data` with no S3 auth config, endpoint `127.0.0.1:8333`, bucket `fin-g4-t2c`, `S3CompatibleObjectStore` through the product's own `create_object_store()`; local throwaway test credentials, not real ones, and never written to a tracked file |
| Gold data | committed `data/corpus/g4_attachment_gold/*`, `data/corpus/pdf/2023_Q1_MSFT.pdf`, `data/corpus/manifest.json` + `extracted/` |
| Web | `web/` `tsc --noEmit` (typescript 5.6), `vitest run` 3.2.7 in the node environment this repo's `vitest.config.ts` declares. The post-repair chip evidence (rows 19 and 23) is the single-file form `cd web && npx vitest run src/attachmentDelete.test.ts`; the repair added no DOM environment, it drives the real element tree inside that same node environment |

The default `uv run pytest -q` run stays hermetic: no network, no live model,
no S3. The real-boundary evidence is the opt-in `-m mcp` module, run
separately below.

## Acceptance clauses, per case

Every row is an executed assertion in the named file, not a code reading.
`durable` = `tests/host/test_thread_attachment_durable.py`, `store` =
`tests/host/test_attachment_store.py`, `door` = `tests/host/test_thread_attach.py`,
`live` = `tests/host/test_live_attachment_upload_door.py`.
Clauses 1–7 were re-executed for the record correction by commands 1, 3, 4, 6
and 12 (all green, `0` skips once `FIN_CHECKPOINT_URL` was really set), and
clause 8 by commands 9 and 10. After the four review repairs they were executed
again by rows 16–18 — the three `durable` / `store` / `door` files with the DSN
present, `166 passed, 0 skipped` — and clause 8 by row 19. After the second
round they were executed once more by rows 20–22 at `cebfd02` — same three
files, `169 passed, 0 skipped` — and clause 8 by rows 23–24. Two rows below are
the exception and say so: the `live` cases were not re-run after either repair
round, and the `web` totals row keeps the pre-repair whole-suite figure because
only the one file was re-measured there (the whole-suite re-measure is row 23).
New clause rows added by the first round are marked **[repair]**, by the second
**[r2]**. No cited case row is absent from its file — re-checked by this pass, after the
second round's renames and the twins-to-`store_pair` collapse, by matching every
`test_*` identifier this record cites against `def` statements in the four
attachment modules plus the two Soft-Track files; a name that survives only
inside a **Correction:** text is there precisely as the retired identifier, and
says so.

### 1. The tombstone lands before any byte or derivative cleanup

| Case | Observed |
| --- | --- |
| `durable::test_deleting_one_attachment_voids_access_then_removes_its_layers` | 200; then status `None`, door read 400, units `()`, postings `{}`, `indexed_versions() == ()`, nothing pending, `UnknownObjectKeyError` on the key, sibling untouched |
| `durable::test_thread_deletion_tombstones_before_cleanup_and_stays_retriable` | with the byte store refusing: 500 `attachment_cleanup_failed`, status already `None`, units and postings already empty, bytes STILL present, obligation `[object_key]` |
| `store::test_tombstone_voids_access_and_returns_the_byte_obligation` | returns `threads/{thread}/{aid}` and the row, units, postings and readiness are gone in the same call |
| `live::test_live_single_attachment_delete_voids_the_real_bytes` | 200 `{"ok": true, ...}`; a SECOND store client in a process that never served the request answers `absent`; attachment row gone; derived counts `(0, 0)`; no tombstone row left |
| `durable::test_the_delete_door_touches_no_byte_before_the_void_is_durable` **[repair]** | the byte store reports what the durable record said at the instant it was asked to remove the key: no live row, no derived layers, that key already owed. Exactly one snapshot, and the touched set is that one key |
| `durable::test_the_byte_delete_finds_the_attachment_already_voided` **[repair]** | the same report taken through a second store view, parameterized `[memory]` / `[postgres]`; on the Postgres leg that view is a second connection, so what it sees is a committed tombstone, never the deleting transaction's own uncommitted row |
| `durable::test_a_thread_delete_voids_every_attachment_before_the_first_byte` **[repair]** | two attachments, on both backends: at each byte touch every row of the thread is already voided and BOTH keys are already owed — not one-voided-then-bytes-then-repeat |
| `durable::test_the_void_waits_for_a_row_another_session_holds` **[repair]** (Postgres, two real connections) | while a second session holds the row the void writes, the delete is parked before its first byte touch: no key touched, no outcome yet, `status()` still readable. The parked session is matched to this test's own holder by its ungranted lock against the lock that holder granted; after `commit` the same delete finishes in the promised order |
| `durable::test_the_parked_void_has_not_reached_its_tombstone_write` **[r2]** (`tests/host/test_thread_attachment_durable.py:993`, Postgres, two real connections) | the byte-waiting claim above holds without the row lock — a lockless void would still park later at its row removal — so this test pins what ONLY `FOR UPDATE` enforces: while another session holds the attachment row the way a derived write holds it, the void has not even BEGUN its tombstone write, read where an in-flight `INSERT` leaves a trace no MVCC read can hide (the writer's granted lock on the tombstone table). Removing `FOR UPDATE` from `_VOID_TARGET_SQL` fails exactly this one test — row 24(a) |
| `durable::test_a_derived_write_racing_a_parked_void_meets_the_lock_not_the_window` **[r2]** (`tests/host/test_thread_attachment_durable.py:1119`, in-memory) | mid-void, what a preparation can read is the lock, never a live row: the void is parked between its liveness sweep and its tombstone write — a fully publishable state if anything could read it — and a racing derived write cannot complete while it sits there; once the void finishes, the write loses with the named typed outcome and publishes nothing |

These rows are the ticket's central ordering claim, and they bite. A
byte store whose `delete` refuses (`GateObjectStore.delete_fails`, and in the
live case `FIN_LIVE_FAIL_OBJECT_DELETE` inside the deployed service's own
client) is asserted to leave access voided, units and postings empty, the
object still readable and the obligation `[object_key]` — a state that can only
exist if the tombstone was written before the byte removal was attempted.
Confirmed by falsification rather than by reading: with the byte pass of
`AttachmentPreparationService.delete_attachment` moved ahead of
`store.tombstone`, and the same swap made in `delete_thread`,
`uv run pytest -q` over the three `durable` cases of this table returned
`2 failed, 1 passed in 0.72s`, each failure landing on exactly its
`assert wired["service"].status(aid) is None` line
(`tests/host/test_thread_attachment_durable.py:582` and `:464` **as they were
numbered at `7c1e04b`** — that mutation run predates the repair round, and the
same two assertions now sit lower in the grown file). Reverting the
production file — byte-identical to `7c1e04b` at that moment, `git status`
clean —
returned all four cases including
`store::test_tombstone_voids_access_and_returns_the_byte_obligation` to green
(`4 passed in 0.70s`).
The same run also named the gap that the repair round closed:
`durable::test_deleting_one_attachment_voids_access_then_removes_its_layers`
stayed GREEN under the inverted order, because with no injected fault the byte
delete succeeds either way. On the success path it pinned the end state, not the
order; only the fault-injected rows pinned the order. That limit is why the
four **[repair]** rows above read the state from INSIDE the byte-delete call, so
the success path pins the order too, and why the Postgres leg runs a second real
connection instead of one process's dictionary. The `84 tests still passed`
figure in the "Review round" table is the measured size of the hole
before those rows existed. The second round closed the lock's own half of
that hole: with `FOR UPDATE` removed from `_VOID_TARGET_SQL` the durable file
fails exactly `1 failed, 43 passed` — alone,
`test_the_parked_void_has_not_reached_its_tombstone_write` — and with it
restored the file measures `44 passed, 0 skipped` with the DSN (row 24(a) and
row 20, both executed at `cebfd02`).

### 2. After the void, read / status / units / postings / readiness all refuse that identity

| Case | Observed |
| --- | --- |
| `store::test_tombstone_voids_access_and_returns_the_byte_obligation` | `get` `None`, `list_for_thread` `[]`, `units` `()`, `index_postings` `{}`, `indexed_versions` `()`, `incomplete` `[]` |
| `store::test_postgres_tombstone_voids_access_and_returns_the_byte_obligation` | same six reads on real SQL rows |
| `durable::test_a_deleted_attachment_is_not_referenceable_by_a_later_turn` | a later send naming the id → 400 `attachment_not_found` |
| `durable::test_a_voided_attachment_matches_no_query_while_its_sibling_still_does` | searching the voided identity's postings returns no units while a sibling prepared at the same version still matches |
| `live::test_live_single_attachment_delete_voids_the_real_bytes` | over real HTTP: status and retry both answer `attachment_not_found`, derived counts stay `(0, 0)` |

### 3. A preparation in flight when the tombstone lands publishes nothing

| Case | Observed |
| --- | --- |
| `store::test_tombstone_blocks_every_late_derived_write` | `record_units`, `put_index`, `mark_ready`, `mark_failed` all raise `AttachmentThreadGoneError` after the void; units and indexed versions stay empty |
| `durable::test_deleting_one_attachment_refuses_a_late_preparation` | a prepare blocked inside its byte read, deleted mid-flight, returns `AttachmentThreadGoneError`; status `None`, units `()`, `indexed_versions() == ()`, row gone, obligation closed, bytes gone — nothing was published |
| `store::test_postgres_tombstone_voids_access_and_returns_the_byte_obligation` | the same three late writes raise on the durable store, where `SELECT … FOR UPDATE` puts the void and the preparation's touch in one order |
| `live::test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart` | a different request in the same process after the void: prepare answers `attachment_not_found`, derived counts `(0, 0)`, tombstone count 1 |

### 4. Cleanup failure keeps the tombstone, is typed, observable, retriable, never complete

| Case | Observed |
| --- | --- |
| `durable::test_deleting_one_attachment_keeps_the_tombstone_when_cleanup_fails` | 500 `{"error": "attachment_cleanup_failed"}`; status `None` (blocked, not readable); bytes still present; `pending_cleanup == [object_key]`; with the store healthy again the same door answers 200, the bytes are gone, the obligation is empty, and a third call is 400 |
| `durable::test_a_refused_cleanup_names_the_owed_bytes_in_the_log` | the `fin.host.attachment_prepare` WARNING carries both the thread id and the owed object key. **[repair]** the assertion is now a COUNT and a verdict: exactly one record names that key (a second line for the same key fails), it says `still owed`, and it carries `exc_info` — so the byte store's own reason survives, not a formatted string that dropped the traceback |
| `live::test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart` | the fault is injected in the deployed service's own byte client inside the first Uvicorn process; that process is stopped and a SECOND process, with intact wiring, deletes the same identity through the same door (200), after the obligation was read back as a real `fin_thread_attachment_tombstone` row |

The only row here that crosses a real process boundary is the `live` one, and
it is opt-in: it needs both `FIN_CHECKPOINT_URL` and a configured S3 endpoint,
and skips by its own gate otherwise. `durable::…keeps_the_tombstone_when_cleanup_fails`
retries inside the test process; `store::test_postgres_tombstone_obligation_survives_a_new_store_instance`
carries the obligation into a second store instance over real SQL rows but not
over the HTTP door. So "retriable across a fresh process" is proven by
`live::test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart`
alone — executed, 14/14 passed, no skip, in command 6, which is a **pre-repair**
run at `7c1e04b`. The live module has not been re-executed since the repairs
landed, so that row is carried forward with its date attached, not re-confirmed
(see "Commands" rows 6–7 versus 16–19).

### 5. Cross-thread / unknown / stale → `unknown_locator`; malformed → typed validation on every door that takes one; never a 500

| Case | Observed |
| --- | --- |
| `door::test_delete_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error` (blank / tab / newline / oversized) | 400 `invalid_locator` on each, and the thread's real attachment is still there |
| `door::test_status_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error` **[repair]** | same four malformed ids, same 400 `invalid_locator`, and a spy wrapped around the store records `[]` — no lookup was handed the garbage. The thread's real row stays readable |
| `door::test_retry_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error` **[repair]** | 400 `invalid_locator`, `handed_to_store == []`, and the real attachment is still `uploaded` — retry neither looked it up nor prepared it |
| `door::test_the_two_lookup_doors_still_name_a_wellformed_identity_miss` **[repair]** | the guard does not swallow the miss's own answer: a plausible id no thread owns is still 400 `attachment_not_found`, an unknown thread still 404 `thread_not_found` |
| `door::test_delete_door_names_an_unknown_thread_and_touches_nothing` | 404 `thread_not_found`, attachment untouched |
| `door::test_delete_door_names_an_unknown_locator` | 400 `unknown_locator` |
| `door::test_delete_door_takes_no_request_body` | a malformed JSON body and no body both answer 200 and both void — the route never parses a DELETE payload |
| `durable::test_repeated_and_foreign_deletion_of_one_attachment_names_the_identity` | never-uploaded, another thread's id, and a stale (already deleted) id all answer the same 400 `unknown_locator`; the owner's copy survives a foreign delete |
| `store::test_tombstone_of_another_threads_attachment_reveals_nothing` **[r2]** (parameterized `[memory]` / `[postgres]` by the `store_pair` fixture) | `None` on both stores, row intact, no obligation recorded for either thread — the same assertions in one case, not two copies. **Correction:** this row previously also cited a hand-copied `store::test_postgres_tombstone_of_another_thread_reveals_nothing`; the second round folded the twins into the fixture |
| `store::test_tombstone_of_an_unknown_attachment_is_a_miss_not_an_error` | `None`, nothing pending |
| `store::test_thread_removal_retires_obligations_instead_of_hiding_them` **[r2]** (also pins the durable miss) | a void that owns nothing leaves nothing: after the thread is gone the tombstone answers `None` and the retained-debt count is zero on BOTH backends (the dict in memory, the cascaded row in Postgres — read through `_retained_obligations`, because `pending_cleanup()` answers `[]` either way). **Correction:** this row first cited `store::test_postgres_tombstone_miss_writes_nothing` and claimed the shared `_void` "finds no row, returns without a statement" — true of the `0b7775c` `_void`, which returned before reaching its `DELETE`; the second round deleted that scripted case, made the retry path read the record it holds (`_OWED_KEY_SQL`) and roll the write back, so the pinned property is now the observable one: a miss writes nothing that survives the call |
| `store::test_tombstone_refuses_a_blank_identity` | `AttachmentStoreError` for a blank or empty identity — the typed store error, before any write |
| `door::test_delete_door_maps_a_store_rejection_to_a_named_exit` | a store that refuses the void → 500 `attachment_store_failed`, with a body, not a bare 500 |

### 6. Repeated deletion is idempotent; delete-vs-delete and delete-vs-prepare are safe

| Case | Observed |
| --- | --- |
| `store::test_tombstone_holds_the_obligation_until_cleanup_is_confirmed` | a repeat while bytes are owed returns the SAME key (retryable), and after `confirm_cleanup` the id is a plain miss |
| `store::test_postgres_tombstone_obligation_survives_a_new_store_instance` **[r2]** (retry half) | a reopened store answers the same `threads/{thread}/{aid}` from the owed record alone — `reopened.tombstone(...) == key` before any confirm, and the debt list is unchanged by the read. **Correction:** this row first cited a scripted `store::test_postgres_cleanup_retry_reads_the_record_without_a_second_write` that counted zero tombstone writes AND zero rollbacks; the second round deleted it, and the retry path now runs its owed-key read inside a transaction the door commits, so the pinned property is the answer coming from the record, with one debt per id kept by `ON CONFLICT DO NOTHING` plus the create refusal below |
| `store::test_create_refuses_an_id_that_still_owes_a_cleanup` **[r2]** (parameterized `[memory]` / `[postgres]`) | PREVENTED, not pinned: an id whose bytes are still owed is refused with `AttachmentStoreError`, in the acting thread's create and another thread's; the refused attempt writes nothing (the debt and its answer are untouched); the refusal lifts at `confirm_cleanup`, after which the identity replays cleanly. **Correction:** this row was the r1 "pinned, not prevented" pair `store::test_create_does_not_consult_a_pending_obligation` + its Postgres twin, with the missing guard documented as an INVARIANT; `9649bc2` turned the guard on, so the pinned-acceptance cases retired with it |
| `store::test_thread_delete_tombstones_every_attachment_before_cleanup` | second `delete_for_thread` returns `[]` while both obligations stay readable |
| `durable::test_concurrent_deletes_of_one_attachment_expose_nothing` | two deletes of one id, one of them blocked inside the byte store: neither raises a cleanup/store failure, status `None`, no obligation left, bytes gone |
| `durable::test_deleting_one_attachment_refuses_a_late_preparation` | delete-vs-prepare: the preparation loses with `AttachmentThreadGoneError`, publishes nothing |
| `durable::test_delete_during_preparation_cannot_resurrect_the_index` (pre-existing, still green) | the thread-level form of the same race |
| `live::test_live_single_attachment_delete_voids_the_real_bytes` | a retry of a finished deletion over real HTTP is 400 `unknown_locator`, and cannot report bytes as owed |

### 7. Thread deletion goes through the same tombstone rule — one void, in both adapters

| Case | Observed |
| --- | --- |
| `store::test_thread_delete_tombstones_every_attachment_before_cleanup` **[r2]** (both backends via `store_pair`) | `delete_for_thread` returns the keys AND leaves one obligation per attachment, readable until `confirm_thread_cleanup`; a retry while owed returns `[]` with both obligations still readable |
| `store::test_thread_voiding_returns_the_obligation_it_recorded` **[r2]** (parameterized `[memory]` / `[postgres]`) | `delete_for_thread` answers `[fresh.object_key]` with an older debt already handed over in the way, and `pending_cleanup()` then holds BOTH keys — the door's answer and the record come out of the same void write, not an enumeration beside it; a retry of each door re-reads the records and re-voids nothing. **Correction:** the hand-copied `store::test_postgres_thread_voiding_returns_the_obligation_it_recorded` twin is gone; the fixture carries the second leg now |
| one rule, both doors, structurally **[r2]** | both Postgres deletion doors issue the tombstone through the single `_void`, per id, and the class docstring states it. **Correction:** this row cited `store::test_both_postgres_deletion_doors_write_one_tombstone_statement`, a scripted SQL-identity counter; `9649bc2` deleted it — a statement count is a proxy, and the behavior behind it is pinned by the store_pair rows above and below, so no command in this file counts statements any more |
| `store::test_thread_removal_retires_obligations_instead_of_hiding_them` **[r2]** (both backends in one case) | after `delete_thread`, `pending_cleanup()` is `[]` AND the retained debt is zero — per backend, through `_retained_obligations`: the dict entry in memory, the cascaded row in Postgres, because `pending_cleanup()` alone answers `[]` even for a filtered-but-retained debt |
| `store::test_the_cleanup_door_names_a_row_whose_thread_row_is_already_gone` **[r2]** | no row leaves the store without a door having named its bytes, and the two backends answer differently BY CONTRACT: `ON DELETE CASCADE` removes the durable row with the thread, so Postgres names nothing (`keys == []`), while the in-memory retirement is lazy and the thread-wide door hands back the orphan's key (`keys == [object_key]`) on its way out; both end with no obligation retained and the door idempotent. **Correction:** this row cited `store::test_thread_removal_retires_orphan_rows_at_the_cleanup_door` and claimed one shared "names no keys" answer; the renamed case is where the deliberate per-backend split is stated |
| `store::test_confirm_cleanup_enters_the_same_retirement_step` **[r2]** | the fifth door retires like the other four: confirming one id of a gone thread sweeps the thread's other shadowed rows and obligations too, so no debt waits for some later door to happen to collect it — the `self._lock`-held single retirement step all five cleanup doors enter |
| `store::test_postgres_thread_delete_cascades_the_cleanup_obligation` **[repair]** | the SQL-side guarantee the in-memory sweep now has to match deliberately, on real rows |
| `store::test_thread_delete_obligations_are_scoped_to_their_thread` | another thread's attachment, units and obligations are untouched |
| **Correction (r2):** the former row `store::test_postgres_thread_delete_tombstones_before_cleanup` ("same on real SQL rows") is gone — that leg is now the `[postgres]` parameter of the store_pair case at the head of this table; no durable claim was dropped with the copy | the shared case runs `delete_for_thread`, `pending_cleanup` and the confirms against real SQL rows |
| `durable::test_thread_deletion_tombstones_before_cleanup_and_stays_retriable` | the whole route: refusal keeps the thread row, voids access, keeps bytes and obligation; the retry completes and the cascade leaves nothing |
| `store::test_postgres_tombstone_obligation_survives_a_new_store_instance` | an obligation opened by one process is read, retried and confirmed by a second store instance |
| `live::test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart` | after the single-attachment retry completes, `DELETE /api/threads/{id}` answers 200 with nothing owed and no tombstone row left for the thread |

### 8. The web chip's `×` calls the single-attachment route and renders typed states distinctly

`web/src/attachmentDelete.test.ts` — **22 cases, all pass** (row 23, at the
second-round tip `cebfd02`; 23 at the first-round tip `0b7775c` (row 19); the
same file had 17 before the repairs). The count moved 17 → 23 with the
driven-control swap — a net, not eight additions: two text-matching cases were
replaced by the driven-control group below (see "Limits and leftovers" for the
replacement's reason and authorization) — and 23 → 22 when `88752cd` deleted
the last source-text case in the second round.

| Group | Observed |
| --- | --- |
| wire contract | `DELETE` on `/api/threads/{id}/attachments/{aid}`, percent-encoded identity, no request body, `{"ok": true, "attachment_id"}` accepted |
| named refusals | `unknown_locator` 400, `invalid_locator` 400, `thread_not_found` 404, `attachment_cleanup_failed` 500, `attachment_store_failed` 500 each reject with that code and status; an unparseable body becomes `http_502`, never a success |
| store behavior | only the confirmed record leaves the slice; a refusal keeps the chip so the retry can be clicked; the retry that lands clears it; another thread's slice is untouched; the upload gate's `pendingUploads` count is untouched by a delete |
| copy | the four delete kinds get four distinct non-empty sentences, and none of them reuses the read door's `attachment_not_found` copy; the cleanup one says retry |
| the chip's delete control, driven **[repair]** | `describe("the chip's delete control")` walks the REAL element tree — `AttachmentChipList` renders one `AttachmentChip` per stored attachment, each `×` calling the exported `deleteChipAttachment`, which delegates to the store's own delete action and routes a refusal through `AttachmentDeleteNotice`. The test locates the control by the row identity it names, then invokes its real `onClick` against the real store action and a stubbed host: `clicking one row's × deletes exactly that attachment, then drops only that row`, `refuses to read a declined delete as a completion`, `keeps the four typed delete refusals distinct on screen` (`attachment_cleanup_failed`, `unknown_locator`, `invalid_locator`, `attachment_too_large`, each rendered, not each named in a string) |
| no source assertion is left **[r2]** | **Correction:** this row cited `keeps the × on its own row identity in the shipped source`, the last surviving text assertion (the `attachment-delete-${id}` template). `88752cd` deleted it: the driven group finds each control through the RENDERED `data-testid` value the shipped template produces (`deleteControl()` matches on it at runtime), so a string match against the source file added nothing the click cases do not already pin. Zero source-text assertions remain in this file; the template's location in `Composer.tsx:160` is a reading re-check in row 10, not a test |
| host↔web path parity | `tests/host/test_thread_attach.py::test_the_shipped_attachment_delete_path_is_the_one_the_chip_calls` (row 12 pre-repair, row 18 at the first-repair tip, row 22 at the second-round tip): the URL `web/src/api.ts` builds with `method: "DELETE"`, the host's own path helper, and the route that answers `thread_not_found` on that literal path are one string — and the client's delete call is not the retry call |

The strength claim, re-measured at the second-round tip (row 24) rather than
carried from the repair session: deleting the control's `onClick`
(`web/src/components/Composer.tsx:163`) fails **6** tests in this file
(`6 failed | 16 passed (22)`) — the first round's figure reproduces even after
the scrape deletion — and mislabeling one refusal's failure kind, done as a
kind remap inside `describeAttachFailure` (`attachment_cleanup_failed` routed
to `attachment_store_failed`), fails **4**. **Correction:** the first round
logged `5` for the kind mutation; its exact edit was not preserved in history,
so the `5` stands as the r1 session's figure on the 23-case file, and row 24's
`4` (plus a `2`-failing copy-duplication variant of the same mutation) is the
measured answer at `cebfd02`. Before the repairs both mutations left the suite
green, because the clause was proven by scraping `Composer.tsx` for the
handler's name.

## Commands, output, exit status

Rows 1–15 were executed at `7c1e04b` and are the **pre-repair** baseline set:
every command there, the two long runs included. Rows 16–19 were executed at the
repaired tip `e6333a9` (content `0b7775c`), with `FIN_CHECKPOINT_URL` present in
the environment, loaded from the shell — no DSN or credential appears in any row
below, only the variable name. Where a number this record previously printed did
not reproduce, the row states the correction instead of quietly restating a new
figure. Rows 20–24 were executed on this reconciliation branch at the
second-round content tip `cebfd02` (the working tree is byte-identical to it
apart from this file), with `FIN_CHECKPOINT_URL` loaded from the repo's
gitignored `.env` — same no-DSN rule, prefixes only.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `uv run pytest -q tests/host/test_attachment_store.py tests/host/test_thread_attachment_durable.py tests/host/test_thread_attach.py tests/host/test_attachment_budgets.py tests/host/test_attachment_index.py` (DSN unset) | 0 | `352 passed, 12 skipped in 6.65s`. All 12 skips are the `FIN_CHECKPOINT_URL` gate inside `tests/host/test_attachment_store.py` — a skip, therefore no evidence; executed for real in row 3, and in rows 16–17 at the repaired tip |
| 2 | the three attachment files of row 1 only, DSN unset | 0 | `127 passed, 12 skipped in 5.12s`. **Correction:** the figure logged here was `124 passed, 12 skipped in 8.06s`, and it does not reproduce at this HEAD. Same 12-skip set, three more passing cases — a count captured before the last cases landed, not a behavior difference |
| 3 | row 1's five files plus `tests/host/test_thread_attachment_durable_postgres.py`, `FIN_CHECKPOINT_URL` set in the shell to the `.env.example` template value | 0 | `371 passed in 9.21s`, with `grep -c SKIPPED` over that run's own output = **0**. 352 + the 12 store twins + the 7 durable-Postgres cases = 371: the Postgres layer EXECUTED, on `16.15`, rather than skipping |
| 4 | the three files of row 2 with the DSN set | 0 | `139 passed in 7.35s`, no skip. **Correction:** this row previously read `138 passed in 7.14s` |
| 5 | the same three files with the DSN only, before the two new cases were added | — | **Not reproducible at this HEAD** — a mid-session capture; superseded by rows 2–4 |
| 6 | `FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=http://127.0.0.1:8333 FIN_OBJECT_STORE_BUCKET=fin-g4-t2c … uv run --no-sync pytest -v -rs -m mcp tests/host/test_live_attachment_upload_door.py` | 0 | `14 passed in 13.34s`, **0 skipped** — per-case list below. Prerequisites did not exist by themselves: `dockerd` was not running, `fin-postgres-1` and `fin-g4-t2c-s3` were both `Exited (255)`, and had to be started. The bucket already existed (`BucketAlreadyOwnedByYou`) with 4 object keys left by an earlier session; this run added none (4 before, 4 after) |
| 7 | same module, `-q -m mcp -k "delete or cleanup_failure"` | 0 | `2 passed, 12 deselected in 7.44s` — the two deletion doors, on the real S3 adapter |
| 8 | `uv run --no-sync pytest -q tests/host/test_live_attachment_upload_door.py` (no `-m`) | 5 | `14 deselected in 0.25s` — the hermetic default run carries none of the live cases |
| 9 | `npm --prefix web run typecheck` | 0 | `tsc --noEmit` clean |
| 10 | `npm --prefix web test` | 0 | `Test Files 26 passed (26) / Tests 378 passed (378)`. Per file: `src/attachmentDelete.test.ts (17 tests)`, `src/uploadDoors.test.ts (35 tests)`, `src/attachFailure.test.ts (23 tests)` — the counts this record cites at that tip. At the repaired tip that first file carries 23, not 17 (row 19), so `378` is a **pre-repair** whole-web total and was not re-measured. Chrome, re-read at the repaired tip: `web/src/components/Composer.tsx:160` carries `data-testid={`attachment-delete-${id}`}` inside `AttachmentChip` (:136), whose `onClick` at `:163` calls the exported `deleteChipAttachment` (:120); `AttachmentChipList` (:177) renders one per attachment and is mounted at `:404`; `:208` is the separate `testId="attachment-delete-failure"`, and `:75` the `data-failure-kind={notice.kind}` it renders |
| 11 | `node ~/.claude/plugins/marketplaces/arthurpower-local/plugins/arthurpower/scripts/s5-complexity/check.mjs --changed --base cc1a9cb` | 0 | `s5-complexity: 13 touched function(s) within baseline.` **Correction:** the command logged here, bare `npm run lint:s5`, no longer produces that — at a clean HEAD it reads an empty working-tree diff and prints `no changed code files; skip.` (also exit 0). The 13-function ratchet is only reachable when the base is named |
| 12 | `uv run --no-sync pytest -q tests/host/test_thread_attach.py` | 0 | `43 passed in 4.14s`, including the host↔web URL parity guard |
| 13 | ordering falsification: byte pass moved ahead of the tombstone in `delete_attachment` and `delete_thread`, then `uv run pytest -q` over the three `durable` ordering cases, then `git checkout -- src/fin/host/attachment_prepare.py` and the same command plus the store case | 1 then 0 | `2 failed, 1 passed in 0.72s` with the inverted order — both failures on their `status(aid) is None` line, `test_thread_attachment_durable.py:582` and `:464`; `4 passed in 0.70s` after the revert, with `git status` showing no modification under `src/`. The success-path case stayed green under inversion, which is why clause 1 now carries the four **[repair]** rows that read the order from inside the byte delete. Both figures here are `7c1e04b` captures, and the line numbers are that tip's numbering; the inversion was NOT re-run at the repaired tip, where the reviewers' own `84 tests still passed` measurement (see "Review round") is the pre-repair size of the hole |
| 14 | `uv run --no-sync pytest -q tests/host` (whole host surface, DSN unset) | **1** | `2 failed, 2021 passed, 66 skipped, 241 deselected, 8 warnings in 102.09s`. The previously logged `1 failed, 2022 passed` in this same 66-skip / 241-deselect shape did not reproduce: the extra failure is `test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory`, an operator-environment case, see "The two failures, in full". Nothing attachment-related moved, and the 66 skips are the `FIN_CHECKPOINT_URL` gate (executed by row 3) plus the `web/dist` artifact |
| 15 | `uv run pytest -q` (whole repo, hermetic, default markers, DSN unset) — run once | **1** | `2 failed, 3410 passed, 66 skipped, 318 deselected, 8 warnings in 896.97s (0:14:56)`. The passed count is exactly the `3410` this record logged before; the failure set is one larger, for the same launcher reason. The two failing node ids were then re-run alone and reproduced: `2 failed in 15.88s` |
| 16 | `uv run pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_attachment_store.py tests/host/test_thread_attach.py`, `FIN_CHECKPOINT_URL` present in the environment | 0 | `166 passed, 0 skipped` — the repaired tip, the three files that carry clauses 1–7, every Postgres leg executed. Row 4's `139 passed` for these same three files is the pre-repair figure; the 27 additions are this round's guard, one-rule and order-from-inside cases |
| 17 | `uv run pytest -q tests/host/test_attachment_store.py`, run twice — with the live server, then with `FIN_CHECKPOINT_URL` absent | 0 | `72 passed` with the server; `57 passed, 15 skipped` without it. The 15 are the file's own `FIN_CHECKPOINT_URL` gate, a skip therefore no evidence, paid for by the first figure on the same file. (Pre-repair the same file gated 12 skips; the repair added three env-gated Postgres twins, and the statement-shape cases that need no server run in both halves — see that file's module docstring) |
| 18 | `uv run pytest -q tests/host/test_thread_attach.py` | 0 | `52 passed` — the door file at the repaired tip, including the two new malformed-locator doors and the host↔web parity guard. Row 12's `43 passed` was that file's pre-repair count |
| 19 | `cd web && npx vitest run src/attachmentDelete.test.ts` | 0 | `23 passed` — clause 8 at the first-repair tip. The same file reported 17 before it (inside row 10's `378`). **Superseded:** row 23 re-measured this file at the second-round tip, `22 passed` — `88752cd` deleted the last source-text case |
| 20 | `uv run pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_attachment_store.py tests/host/test_thread_attach.py`, `FIN_CHECKPOINT_URL` loaded from the repo's gitignored `.env` (values redacted; only the name appears here), at content tip `cebfd02` | 0 | `169 passed`, no skip line — the second-round tip's three clause 1–7 files, every Postgres leg executed; the per-file split is `44 + 73 + 52 = 169` (the durable leg at `44 passed` is row 24(a)'s restored run, the store leg is row 21, the door leg row 22). Row 16's `166` stands as the `0b7775c` figure: the second round added the two lock-proof durable cases and grew store coverage, while deleting the scripted statement cases. DSN absent, the same three files report `145 passed, 24 skipped` (durable `38 + 6 skipped`, store `55 + 18 skipped`, door `52`) |
| 21 | `uv run pytest -q tests/host/test_attachment_store.py`, run twice — with the live server, then with `FIN_CHECKPOINT_URL` absent | 0 | `73 passed` with the server; `55 passed, 18 skipped` without it. **Correction:** row 17's `72` / `57 + 15` were the `0b7775c` figures. The skip story changed with the fixture: the hand-copied `test_postgres_*` twins are gone, replaced by `store_pair`-parameterized cases whose `[postgres]` leg skips without the DSN, plus the file's own env-gated Postgres cases; the scripted statement-counter cases were deleted outright |
| 22 | `uv run pytest -q tests/host/test_thread_attach.py` | 0 | `52 passed` — reproduces row 18 exactly; the second round changed no door case, only the shared `_locator_refusal` behind them |
| 23 | `cd web && npx vitest run src/attachmentDelete.test.ts`, then `npm --prefix web test` | 0 | `22 passed` for the one file — clause 8 at `cebfd02` — and `Test Files 26 passed (26) / Tests 383 passed (383)` whole-web. Row 10's `378` stays the pre-repair total; its per-file citations re-read at `cebfd02` as `attachmentDelete (22)`, `uploadDoors (35)` and `attachFailure (23)` — the latter two unchanged, the first down one from `88752cd` |
| 24 | falsifications at `cebfd02`, each reverted with `git checkout --` and a clean `git status` confirmed after: (a) `FOR UPDATE` removed from `PostgresAttachmentStore._VOID_TARGET_SQL`, durable file re-run with the DSN; (b) the `onClick` line `web/src/components/Composer.tsx:163` deleted, `npx vitest run src/attachmentDelete.test.ts`; (c) `describeAttachFailure`'s kind remapped (`attachment_cleanup_failed` answered as `attachment_store_failed`), same vitest command | 1 then 0 (each) | (a) `1 failed, 43 passed` — exactly `test_the_parked_void_has_not_reached_its_tombstone_write`; restored, `44 passed` (exit 0). (b) `6 failed \| 16 passed (22)` — the first round's `6` reproduces. (c) `4 failed \| 18 passed (22)`; a copy-duplication variant of the same mutation instead failed `2`. See clause 8's strength paragraph for what each number replaces |

Rows 16–19 were the only commands re-executed after the four first-round
repairs; rows 20–24 are the second round's re-measurement at `cebfd02` and
supersede them wherever they differ. Rows 6, 7, 10, 11, 14 and 15 belong to
`7c1e04b`; where this record leans on one of them it says so, and the live
module in particular has NOT been re-executed against the new `src/` (neither
round changed that fact). For the record's own audit trail, the two reviewers' baselines on that
previous tip match the pre-repair rows rather than replacing them: the
attachment row set at `352 passed / 12 skipped` (row 1), the Postgres tombstone
legs at `4 passed / 0 skipped` (row 13's post-revert half), one live door row at
`2 passed / 12 deselected` against real Uvicorn + Postgres + S3 (row 7), and the
web suite at `378 passed` (row 10).

Row 6, per case — all `PASSED`, none skipped:
`test_upload_of_each_gold_payload_lands_as_durable_bytes_with_nothing_derived`,
`test_byte_adapter_is_the_one_the_product_itself_selected`,
`test_refusal_at_the_deployed_cap_leaves_no_object_and_no_row`,
`test_upload_starts_no_work_behind_the_response`,
`test_unsupported_bytes_upload_and_only_preparation_names_them`,
`test_empty_body_and_unknown_thread_stay_named_and_store_nothing`,
`test_concurrent_and_duplicate_uploads_land_as_distinct_rows`,
`test_live_server_uploads_every_gold_payload_as_uploaded`,
`test_live_upload_bytes_persist_after_uvicorn_exit_in_fresh_adapter_process`,
`test_live_server_accepts_the_filing_the_retired_constant_rejected`,
`test_live_server_uploaded_row_is_not_a_recovery_candidate`,
`test_live_single_attachment_delete_voids_the_real_bytes`,
`test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart`,
`test_module_leaves_the_real_tables_as_it_found_them`.
`test_byte_adapter_is_the_one_the_product_itself_selected` passing with
`FIN_OBJECT_STORE_URL`/`FIN_OBJECT_STORE_BUCKET` set is the assertion that the
adapter under test was `S3CompatibleObjectStore` and not the degraded in-memory
one; the same module would have demanded
`ObjectStoreNotConfiguredError` had the endpoint been absent.

## Review round — the four repairs and the finding each closes

Gate outcome on this branch, both axes: **Standards PASS, Spec PASS**, `0` High,
`8` Medium, `8` Low. Four of those findings are repaired below each reviewer's
own words; the rest are the limits this record states as limits.

| Commit | What landed | The finding it closes |
| --- | --- | --- |
| `325a2e7` | the malformed-locator guard covers the GET-status and retry-cleanup doors, not only DELETE, and a spy wrapped around the store proves a malformed segment is never handed to it (`handed_to_store == []`, clause 5) | "only one of three doors on the same resource validates its input" |
| `51d9678` | the chip's delete control is exercised through the real element tree down to the handler actually bound to it; removing the `onClick` now fails `6` tests and mislabeling the failure kind fails `5` | "this clause is proven by scraping the source text, so it stays green with the handler deleted" — raised independently on both review axes |
| `0854988` (with `33c4315`, `c9cf435`) | one tombstone rule for both doors in both adapters; thread-wide voiding routed through the single-attachment helper; owed keys derived from the void write instead of a second enumeration; obligations for a deleted thread retired rather than hidden; `create()`'s missing guard documented as an invariant instead of added | "one rule written at four sites", and "the docstring promises removal that only one adapter does" |
| `212471c` (with `9532a35`, `19c0a6b`) | the headline ordering promise pinned from INSIDE the byte-delete call, parameterized over both stores, Postgres legs on two real connections proving the row lock through its wait chain; one authoritative cleanup log line replacing a doubled one | "the ticket's order guarantee has no test that fails when it is violated" — before this, inverting the order left `84` tests passing |

That last row is why clause 1 no longer says the success path cannot pin an
order. The `84` figure is the reviewers' mutation run on the previous tip, not a
command in this file; the rows that answer it are the four marked **[repair]**
there, executed by row 16.

### Second repair round — the four merges, named by what each delivered

The gate outcome the r1 table above records was followed by a second round on
this branch; the round's own gate numbers are not recorded in this file, and
what each merge delivered is named by its commit message, not by reviewer
words this record never captured.

| Merge | What landed | Where this record shows it |
| --- | --- | --- |
| `9d4d12f` (`9649bc2`, `b7a4781`) | the store never lets a deletion drop an unnamed obligation and answers a shadowed or retried void from the record it wrote: `_retire_gone_locked` is the one lock-entering retirement step all five cleanup doors pass through, `create()` refuses an id that still owes cleanup on both adapters, and the Postgres `_void` opens with the locking row read instead of returning before its `DELETE`. The hand-copied `test_postgres_*` twins became the parameterized `store_pair` fixture (drift between a copy and the assertion it shares was the fixture docstring's stated reason), and the scripted SQL-identity cases were deleted | clause 5's **[r2]** rows, clause 6's refusal row, clause 7's five rows; rows 20, 21 |
| `71b8dff` (`5bbf70f`, `17d6675`) | the void's park is pinned to the tombstone write, not the byte order, and moved inside the row lookup so BOTH lock halves bite: `FOR UPDATE` on Postgres and the in-memory `self._lock` each now have a test that fails when the lock is removed | clause 1's two **[r2]** rows; row 24(a), the `FOR UPDATE` half measured here (`1 failed, 43 passed`, one test) |
| `68e0b5a` (`8f0f7e4`) | the three attachment doors share one `_locator_refusal` (the per-door copies collapse to one), and the byte-delete verdict set closes to the two live verdicts — the dead `_OUTSTANDING` / `_ORPHANED` constants are deleted, the surviving pair typed as `Literal` | clause 5's door rows, unchanged in behavior; "Code under test" |
| `cebfd02` (`0c35e92`, `77c6cf8`, `88752cd`, `55c90c2`) | the client states each attachment door's real answer list (`web/src/api.ts:844-860` the retry door, `:883-902` the delete door — never-`parsing` for retry, the five named refusals for delete, the shared `attachment_not_found` causes named without counting doors), the status door is named where no client wrapper exists, `attachFailure.ts:64-68` gives `unknown_locator` and `invalid_locator` their own sentences ("删除没有可执行的内容", "这次操作没有执行"), and the last source-text scrape leaves the chip's suite | clause 8's count and the no-source-assertion row; rows 23, 24(b)(c) |

## Host-surface containment

Row 14 ran the whole host surface **at `7c1e04b`**, so nothing outside the
attachment modules moved with the pre-repair change: both failures there are the
two of rows 14–15, the launcher shim and the Soft-Track copy wrapper, neither of
which appears in `git diff --name-only cc1a9cb..HEAD`, and every attachment door,
store, budget, index, boot and live-deselect case passed. The Postgres half of
that surface is not hidden by the skips either — row 3 ran the five attachment
files plus `test_thread_attachment_durable_postgres.py` with the DSN really set
and reported `371 passed` with `0` skips.

The repairs after that tip added `src/` changes of their own, all three inside
the attachment surface this ticket owns — `git diff --name-only cc1a9cb..HEAD` is
still thirteen files: four `tests/host/`, three `src/fin/host/`
(`workspace_http.py`, `attachment_store.py`, `attachment_prepare.py`), five
`web/`, and this record — and no whole-host or whole-repo run has been repeated
since (rows 16–19 are the first round's post-repair measurement). The second
round was re-checked the same way at `cebfd02`: the diff is those same thirteen
paths, nothing new entered `src/` outside the three named files, and no
launcher, search-mode or Soft-Track file appears — but again no whole-host or
whole-repo run was repeated (rows 20–24 are scoped, like rows 16–19). So the
containment claim holds by the shape of the diff at both rounds, and the
execution evidence at each is its scoped rows: the whole-suite figure is
**not measured in this record at either repaired tip**.

## The two failures, in full

`test_soft_track_pytest_exits_0` shells out to `pytest -q -m soft_track` and
demands exit 0. Inside it, two Soft-Track search-mode copy cases fail —
`tests/host/test_search_mode_call_scoped.py::test_messages_for_model_injects_mode_copy_once`
and `::test_each_mode_copy_names_only_its_tier` — observed again here as
`2 failed, 202 passed, 3592 deselected`. The earlier record proved those two red
on a clean `cc1a9cb` worktree; this ticket touches no search-mode file, so the
ticket's own contribution is the other 3410. Reported as a main-branch failure,
not as a pass and not as a skip.

`test_shim_drives_a_checkout_from_any_directory` was not in the record's failure
set, and it fails for an operator-environment reason rather than a code reason:
the shim's `fin check` preflight demands an answering docker daemon reachable by
the invoking user, and here `docker info` returns `permission denied while
trying to connect to the docker API at unix:///var/run/docker.sock` (socket
`srw-rw---- root docker`, `id` groups = `winner` only, no `docker` membership)
even after the daemon was started. The cause is now pinned to the group, not to
the daemon: the socket belongs to `docker`, this uid is not a member of it, so
plain `docker` is refused while `sudo -n docker` on the same socket succeeds.
The same test therefore fails whether the daemon is down (as it was when this
audit started) or up-but-unreachable-by-this-user, and it would fail identically
at `cc1a9cb`, whose launcher and preflight files this branch does not modify.
Environmental — recorded as neither a pass nor a skip, not weakened, not
skipped, and not counted as this ticket's pass.

Neither of the two was re-executed after either repair round: rows 14–15 are
`7c1e04b` captures, and no full-suite run exists at the first- or second-round
tip (see "Host-surface containment"). Neither round touched a launcher,
search-mode or Soft-Track file, so
these two remain the standing failure set of the branch, not new evidence.

## What is real here and what is not

Real in rows 6 and 7 (a `7c1e04b` run; the module has not been re-executed since
the repairs, see "Blocked evidence"): a separate Uvicorn process serving
`fin.host.http.app`,
HTTP through `urllib` in the shaped-`fetch` style of the shipped client,
`PostgresThreadStore` + `PostgresAttachmentStore` on the live Postgres, the
product's own `create_object_store()` S3 adapter, the tombstone table read back
over a second SQL connection and a second store client, and the deployed
`FIN_ATTACHMENT_*` bounds taken from `.env.example`.

Not real: `get_graph` is overridden with a null graph in the child process. The
delete door never reaches the graph and a live graph means a live model, which
this change does not involve. The injected fault in
`test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart` is one
function on the deployed attachment service's own byte client — everything
above it is the product.

## Blocked evidence

Nothing in the clause tables above is blocked **at `7c1e04b`**, the tip rows
6–7 executed at; at the repaired tip the `live` rows become carried-forward
evidence instead, which is the second kind of blocked named below. The row this
record first printed said "`fin-postgres-1` container (already running)", and
that is the part that did not survive: at the re-run the docker daemon itself was
not running, and both `fin-postgres-1` and `fin-g4-t2c-s3` were `Exited (255)`.
With that state the twelve Postgres twins inside rows 1–2 stay skips, the whole
live module of rows 6–7 skips on its own gate, and every one of those cases is
therefore **BLOCKED, not green**. Row 1 and row 2 are exactly what the
unconfigured machine reports; starting the daemon and the two existing
containers is what let row 3 report `0` skips and row 6 report
`14 passed, 0 skipped`; the S3 container is not recreated from a
committed recipe, so a machine without it cannot reproduce rows 6 and 7 at all.
Reproduce them with:

```bash
sudo dockerd &                                    # daemon was absent
sudo docker start fin-postgres-1 fin-g4-t2c-s3    # both existed, both exited
# boto3 is NOT in the default env — the live-S3 legs need the non-default extra:
uv sync --extra object-store                      # pyproject.toml:23
# then export FIN_CHECKPOINT_URL (from .env.example:23) and
# FIN_OBJECT_STORE_URL / FIN_OBJECT_STORE_BUCKET, plus the object-store
# credential variables by NAME; throwaway values only, never written here
```

Without `FIN_OBJECT_STORE_URL` / `FIN_OBJECT_STORE_BUCKET` the S3 read-back
cases in that module skip by their own gate; that skip is blocked evidence, not
a pass, so the gateway was started rather than the row reported green. Same
standing for `boto3`: with the extra absent the S3 adapter cannot be built at
all, which is why the recipe names it — a machine on the default env reproduces
row 6 as skips, not as its `14 passed`. And a caveat this record cannot lift:
rows 6–7 ran at `7c1e04b`, so the live round-trip has not been re-executed
against the repaired `src/` — neither round's rows 16–19 nor rows 20–24 touch
real S3, both are hermetic plus Postgres only — and `attachment_store`,
`attachment_prepare` and `workspace_http` all changed afterwards. Real S3
evidence at either repaired tip is BLOCKED until someone runs that recipe.

Left behind, not blocking: 4 orphan object keys were already in the throwaway
`fin-g4-t2c` bucket before this run and are still there after it (`thread-
attachments/<uuid>/<uuid>`, no SQL row references them — the SQL-side guard in
`test_module_leaves_the_real_tables_as_it_found_them` covers tables, not the
bucket). This run added none. Cleaning them means deleting contents in a
disposable bucket; left as-is deliberately, and named here so the next run's
"nothing left behind" claim is read as scoped to the tables.

The live Jev round-trip and the live MCP retrieve suites are not required by
this change (no Soft-Track or search-mode product path was touched, ADRs
0080/0081), and were not run.

## Limits and leftovers

* Clause 3's acceptance names four things a late job must not publish: units,
  observations, index entries, readiness. Three of them are proven blocked —
  `record_units`, `put_index`, `mark_ready` (and `mark_failed`) all raise
  `AttachmentThreadGoneError` after the void, on both stores and over a real
  race. **Observations are not exercisable yet**: no vision-derivative write
  exists in `AttachmentStore` at this HEAD, so there is nothing for the
  tombstone to refuse. This is not a claim that late observations are held
  back; it is a surface that does not exist until the ticket that adds it, and
  that ticket inherits the assertion.
* The HTTP delete door is proven against real SQL rows only by the opt-in live
  module. `tests/host/test_thread_attachment_durable_postgres.py`, which drives
  the doors over Postgres inside one process, has no single-attachment delete
  case; the durable store's own void semantics are covered there only through
  `tests/host/test_attachment_store.py`'s Postgres twins.
* `attachment_prepare.delete_thread` sweeps late-arriving attachment rows
  best-effort after its confirmed pass, so a byte delete that fails in that
  narrow window is logged and left to the thread-row cascade rather than to a
  retryable obligation. It is the pre-existing sweep design, unchanged here.
* The web suite has no DOM environment. The chip is now proven by walking the
  real element tree and invoking the handler actually bound to it (clause 8),
  with no source-text assertion left anywhere in its file — the last one (the
  shipped `attachment-delete-${id}` testid template) was deleted by `88752cd`
  once the driven group proved the identity through the rendered attribute at
  runtime. So this is a render-and-click proof inside the node
  environment, not a browser one: no layout, no CSS, no event delegation through
  a real document, and a handler wired through a mechanism only a browser
  exercises would still pass here.
* No migration is needed: `fin_thread_attachment_tombstone` is created by the
  same `_SCHEMA_SQL` the other attachment tables come from, and it carries an
  `ON DELETE CASCADE` key to `fin_thread` like the rest.
* The web client builds the delete URL as its own literal, which is the file's
  existing convention for every door. Command 12 closes the drift risk from the
  host side: the literal path, the route that answers on it, and the string the
  client assembles are compared in one case.
* `workspace_http` still judges a malformed path identity two ways: the
  feedback door's `invalid_feedback` (strip + `MAX_RUN_ID_LENGTH`) and the
  attachment doors' `invalid_locator` (strip + length + no whitespace or
  control characters), the latter now stated ONCE as the shared
  `_locator_refusal` every `{attachment_id}` door calls (`8f0f7e4`).
  Unifying them would change the feedback door's accepted input, which is
  outside this ticket; noted for whoever owns run-id input next.
* Deliberately NOT changed, each with the ground: `unknown_locator` and
  `invalid_locator` keep their separate names because the ticket's acceptance
  names both verbatim
  (`specs/thread-attachment-source-read.md:67,76,82`), so a reviewer's proposal
  to fold `invalid_locator` into `attachment_not_found` was rejected on that
  basis — renaming a spec-named exit is a spec change, not a cleanup.
  `_attachment_delete_path` stays a private symbol that tests import, because
  three concurrent consumers reference it by that name
  (`tests/host/test_thread_attach.py`, `tests/host/test_thread_attachment_durable.py`,
  `tests/host/test_live_attachment_upload_door.py`) and exporting or renaming it
  moves a name for no behavior change. **Superseded (r2):** the bullet's last
  item — `create()` documenting its invariant instead of enforcing the missing
  guard — was the r1 position, and the second round changed this exact decision:
  `9649bc2` made `create()` REFUSE an id that still owes a cleanup on both
  adapters, after the shadowed-void finding made one-debt-per-id load-bearing
  rather than merely unreachable. What the r1 text kept as a pinned behavior is
  now an enforced guard, pinned by clause 6's **[r2]** refusal row.
* The spec's `waiting runs cannot use it` term
  (`specs/thread-attachment-source-read.md:66`) is proven for the surfaces that
  exist: a read, a retrieval, a visual pass, a later send and an in-flight
  preparation each lose (clauses 2, 3, 5). Its DURABLE-JOB form — a preparation
  queued but not yet started — still has no surface at this HEAD, because
  preparation is request-driven, so this record does not claim it. T2f ("Gate
  preparation through a durable graph job") inherits that term explicitly, the
  same way T2e inherits the observation write.
* Where a replaced test's reason lives: the project rule wants the reason and the
  authorization in the commit body. History on this line squashes on merge, so
  for the already-squashed portion the reason is recorded HERE, in the two
  bullets below (the inherited readable-after-failure assertion, and the
  source-scraping chip cases), and this branch's own commits carry it going
  forward — `51d9678`'s body is the one that names what replaces the two cases it
  removed, and `88752cd`'s body carries the deletion of the survivor that
  followed in the second round.
* The chip's two source-scraping cases (the `toContain("deleteAttachment")` /
  `"function AttachmentChip"` match and the notice-slot `toContain` checks) were
  replaced, not dropped: clause 8's driven-control rows prove the same row
  identity, click path and typed refusal render by invoking and rendering them,
  which is strictly stronger than matching text. The one source assertion that
  initially survived to pin the shipped testid did not survive the second
  round: `88752cd` deleted it after two inversions showed it carried no claim
  of its own (binding the handler to another row's id failed only the
  behavioural test while the scrape still passed; renaming the test id failed
  both). The testid is pinned by the driven group's runtime lookup now.
* Deleted scripted cases, with the reason where the rule wants it: the
  statement-counting `store::test_both_postgres_deletion_doors_write_one_tombstone_statement`
  and the write/rollback-counting
  `store::test_postgres_cleanup_retry_reads_the_record_without_a_second_write`
  were deleted by `9649bc2` — their commit body states the delivery they came
  with — because a statement count is a proxy for a rule the production shape
  now states once (`_void`, `ON CONFLICT DO NOTHING`, the create refusal) and
  the observable answers are covered by equal-or-stronger store_pair cases in
  clauses 5–7; clause 5's miss row likewise lost its "returns without a
  statement" claim with that shape. The hand-copied `test_postgres_*` twins
  were replaced, not weakened: the `store_pair` fixture runs their shared
  assertions on both backends, per the fixture docstring's drift reason.
* One inherited test was rewritten rather than deleted:
  `test_cleanup_failure_is_typed_observable_and_retriable` asserted that a
  failed cleanup left the attachment READABLE, which is exactly the behavior the
  tombstone rule replaces. Its successor
  `test_thread_deletion_tombstones_before_cleanup_and_stays_retriable` covers
  the same door, the same refusal and the same retry, and adds the blocked-read,
  units, postings, obligation and post-retry byte assertions.

## What this re-audit changed in the record

Every clause table survived re-execution; what did not survive were four
summary-level claims, corrected above rather than restated:

* `124 passed, 12 skipped` (three files, DSN unset) → `127 passed, 12 skipped`.
* `138 passed` with the DSN → `139 passed`; the Postgres twins were verified to
  execute, not skip, by a `0` skip count over the run that included the
  durable-Postgres module (`371 passed`, row 3).
* `npm run lint:s5` printed `13 touched function(s) within baseline` only
  because the tree was dirty at the time; the reproducible form names the base
  (row 11).
* "`fin-postgres-1` (already running)" and a whole-repo run with exactly one
  failure. Neither holds on a fresh machine: the daemon and both containers have
  to be started, and the docker-socket permission case joins the failure set
  (rows 14–15). The image digest, the DSN's `sha256` prefix, the Postgres 16
  claim, the per-file web case counts and every cited case name did check out.

## What the repair round changed in the record

The re-audit above corrected summary claims; the review repairs changed the
numbers themselves, so the stale figures were replaced in place and the ones that
only exist at `7c1e04b` were labeled rather than quietly carried forward:

* Clause 1's admitted hole — the success path "pins the end state, not the order"
  — is closed by four new rows read from inside the byte-delete call, and the
  sentence naming the hole now names the finding and its `84` measured size.
* Clause 8 went from `17` cases and a source-scraping chrome guard to `23` cases
  with the control clicked through the real element tree; the row's claim changed
  from "the source contains the handler's name" to "clicking it moves the
  transaction", with the mutation counts (`6` / `5`) stated as the repair
  session's, not as a command here. The second round took the file to `22` and
  re-measured the mutations as commands (row 24); see the section below.
* Clauses 5, 6 and 7 gained the guard rows, the one-void rows and the retired-
  obligation rows; clause 4's log row gained its count, verdict and traceback
  assertions.
* The three-file DSN figure `139 passed` (row 4) is superseded by `166 passed,
  0 skipped` (row 16) at the first-repair tip; the door file's `43 passed` (row 12)
  by `52 passed` (row 18); the store file's skip story by `72` executed /
  `57 + 15 skipped` (row 17). Rows 20–22 carry all three of these into the
  second round again: `169 passed`, `52 passed`, `73 / 55 + 18 skipped`.
* Two claims this record previously made unqualified are now qualified: the live
  S3 evidence is `7c1e04b` evidence and BLOCKED at the repaired tip until the
  recipe is re-run (with `--extra object-store`), and the whole-suite figures of
  rows 14–15 were not re-measured after `src/` changed.

## What the second repair round changed in the record

The r2 reconciliation pass re-verified every file:line citation, count and skip
figure against the merged tree at `cebfd02`, and corrected what had gone stale:

* The re-measured totals: three files with the DSN `166` → `169` (row 20), the
  store file `72 / 57 + 15` → `73 / 55 + 18` (row 21), the door file `52`
  reproduced (row 22), the chip file `23` → `22` and the whole-web `378` era
  finally re-measured at `383` (row 23).
* Clause 1 gained the two **[r2]** lock-falsifiability rows (`:993`, `:1119`)
  and the row-24(a) measurement — removing `FOR UPDATE` fails exactly one test;
  the durable file with the DSN is `44 passed, 0 skipped`.
* Clause 6's "pinned, not prevented" create pair became the refusal row — the
  r1 limits bullet arguing against the guard is superseded in place.
* Clauses 5 and 7 lost their hand-copied `test_postgres_*` twin citations to the
  `store_pair` fixture, lost the scripted statement- and write-counters to
  deletion, and gained the per-backend truth the renamed cleanup-door case
  states (Postgres names `[]` for an orphaned thread, memory names the key).
* Clause 8's driven-control strength numbers were re-run, not carried: the
  `onClick` deletion's `6` reproduces on the 22-case file; the kind-mislabel
  figure `5` did not survive as measured — the same-class mutation now fails
  `4` (row 24), and the exact r1 edit is unrecoverable from history, so the
  `5` stands only as that session's number.
* The DSN provenance note in "Environment" now distinguishes the template's
  hash (`3856bda1`, re-verified) from this round's `.env`-loaded value
  (`8229bf8a`), and the Postgres server version and table count re-read green.
