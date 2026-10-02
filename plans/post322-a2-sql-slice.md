# Plan: A2 — the bounded SQL vertical slice

**Goal:** A request explicitly assigned `sql` reaches one named parameterized
query against an enterprise operations ledger, returns rows as their own
structured evidence kind, and every one of those facts is provable against a
live database rather than a fake.

**Approach:** The seam is already routed on both sides — fin refuses `sql` at
three seams, iknow's `SEARCH_MODE_ARMS` already carries a `sql` row. Neither side
is missing a dispatch point, so this is not new plumbing; it is replacing two
refusals with a real path and carrying a new evidence shape across the MCP
boundary. The two repositories move in **two PRs with a hard order** (T7 before
T8), because deleting fin's refusal first turns a typed `real_db_not_wired` into
a false `retrieve_unavailable` on the wire. Three of the four slice shapes are
not yet decided by the spec — how a row crosses the typed seam, where a SQL
failure token lives, and what an empty SQL result says — so they are
`[decision]` bullets that close before code moves.

**Spec link:** `specs/search-mode-business-sql-contract.md`; `plans/post321-real-sql.md` A2.

**ACR:** BLOCKED at plan time (4 of 5). Every failing line is a hole this plan
closes in T1–T3, not a reason to stop:

```text
bounded-context-guardian: unclear — a `database-row` cannot cross the typed
  seam: iknow's RetrieveOutputEnvelope is chunk-only (types.ts:712-720) and
  fin's Chunk.source_ref is hard-typed fin.corpus.Locator (port.py:39,166).
  Spec §8 never says which type carries a row. → closed by T1.
input-contract-tests: no — §6's lock_timeout and §7's three new codes have no
  assertion in either leg; the one empty-window case has an undecided expected
  output. → closed by T2 and T5.
error-handling-enforcer: unclear — the empty-result EXIT is unsettled by the
  spec's own admission (spec:483-488), and §5.2 step 3 demands a provenance
  verification for which no attestation mechanism exists today
  (GraphragFailureToken is a single-member union, errors.ts:23). → closed by
  T2 and T3.
complexity-anti-drift: yes — one template, one module, one evidence kind, no
  free-form SQL, single abstraction level. Watch item: both identity sites are
  binary rather than extensible (export.py:261, workspace_http.py:1757), so the
  spec's "third case" touches two already-large files. → acknowledged in T6.
minimal-change-verifier: no — not one task. Two repositories, and the spec makes
  merge order a hard gate (spec:346-354). → accepted as T7/T8 sequencing.
```

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review →
(GATE BLOCKED → review-report-repair) → verification-before-completion. Landing
grain is the operator's global commit section, not one commit per bullet.
One code-review phase closes the round after T8; WIP bullets skip it.

**Not in scope, named so it is not silently skipped:** the dashboard HTTP
handler `GET /api/query` calls `assembleRetrievalResult` directly and bypasses
the 档 table entirely, so a SQL port is invisible there. Production wiring and
the §9 leg-1 real-ledger run stay blocked. B3 owns the browser journey.

## Tasks (ordered by dependency)

1. **Row carrier and failure-token vocabulary** — tag: `[decision]`
   - **Inherits:** spec §8: *"a database result is not a document passage, and
     this spec no longer forces it to be one"*; the `database-row` shape with
     `source` / `record_key` / `queried_at` / `fields` / `summary` (spec:530-542).
     spec §7: *"a named failure is blocked evidence, not a skip"*; the three new
     codes `sql_query_denied` / `sql_query_timeout` / `sql_authorization_unverified`.
   - **Surface:** the retrieve output contract on both sides of MCP — iknow's
     retrieval output type and its error vocabulary, and fin's retrieve port
     result type. ADR 0021 (a citation's identity comes from this run's retrieve
     result) and ADR 0046 (a source marker without a matching span is not a
     citation) both constrain the outcome; ADR 0021 holds exactly, ADR 0046 in
     spirit.
   - **Acceptance:** One written decision per fact, recorded in the spec's
     consequences section, and each is answerable with a typed yes/no:
     (a) a ledger row is representable on the wire **without** being coerced
     into a `source_ref`/`valid_window` chunk — the row keeps its own identity
     (`record_key`) and its own time (`queried_at`);
     (b) the port's return type widens by a declared, named route rather than by
     loosening `Locator`; `Chunk.source_ref` stays a `Locator` for document
     retrieval;
     (c) the three §7 codes are reachable as **structured** `error` fields on
     the wire, which today they are not — `GraphragFailureToken` is a
     single-member union (`errors.ts:23`) and `failureTokenOf` is its only read
     path, so a plain-text `isError` result cannot carry a code;
     (d) `real_db_not_wired` after A2 means *not provisioned* only, never
     *refused* — the empty-vs-error conflation the port forbids.
     Acceptance is that a second implementer reading the decision cannot produce
     a wire shape that is simultaneously valid for a document chunk and a
     ledger row.
   - **Completion:** the decision exists in `specs/search-mode-business-sql-contract.md`
     and the closed sets that must change are named with file:line.
   - **Decided (spec §8.0, §7.1, §7):** the row is a **sibling key on the
     envelope**, never in `chunks[]` — the chunk shape requires `source_ref` and
     `valid_window`, so fitting a row would fabricate the document identity the
     citation then claims, and `Chunk`/`Locator` stay untouched. A SQL failure
     reaches the wire **only as a widened named token**; an untokened error is a
     defect, not a fallback. `real_db_not_wired` afterwards means *not
     provisioned* only. ADR 0091 (`accepted`) and the `database-row` term in
     `docs/CONTEXT.md` are written.
   - Status: [x] done

2. **Empty SQL result and unverified-authorization exits** — tag: `[decision]`
   - **Inherits:** spec §7: *"A1 must therefore decide the copy, not the code …
     Either the copy branches on 档, or a dedicated code is added. This is a
     genuine open decision, not a settled item."* spec §5.2 step 3: iknow
     *"verifies where that context came from before believing it … an
     unverifiable context is a named failure, not a default."*
   - **Surface:** fin's named-failure resolution for the 档-specific empty exit,
     and iknow's authorization-context admission. `src/fin/host/named_failures.py`
     and the `sql` 档 copy paths are the fin side; the reader role and the
     context attestation are the iknow side.
   - **Acceptance:** (a) A query that ran correctly and matched no row produces a
     run whose operator copy names the database and the window and does **not**
     tell the operator to ingest documents — the existing `corpus_not_ready`
     copy ("add documents (corpus ingest) and ask again") is factually wrong
     advice after a database miss; (b) that exit stays **distinct** from denied,
     timeout, unavailable, and untrusted-context — the five classes are
     separately assertable; (c) an authorization context whose provenance cannot
     be verified produces a **named** failure and **never** a fallback to a
     default scope, because falling back is the silent-widening failure the whole
     authorization design exists to prevent; (d) the choice of *one* authorized
     range for the first version is stated as such and does not claim department
     isolation.
   - **Completion:** both exits have an expected operator-visible result written
     down, derivable without reading the implementation.
   - **Decided (spec §7, §5.2):** a dedicated code **`sql_no_matching_rows`**, by
     the argument the spec already uses for `timeout` — two product facts with
     two different remedies (widen the window vs. ingest documents) may not
     share a code, and branching only the copy would leave them identical to
     anything reading the code. For authorization, **v1 carries no scope on the
     request at all**: the reader role comes from instance configuration and a
     request naming a scope is a named failure, which removes the forgery trap
     instead of guarding against it. `sql_authorization_unverified` is retained
     and named for the per-identity era.
   - Status: [x] done
   - [parallel] with T1 (different files, different repo concerns)

3. **Versioned synthetic ledger with a DB-enforced read-only boundary** — tag: `[implementation]`
   - **Inherits:** spec §2.1 canonical contract/counterparty structure and the
     adaptation rule (*"an authorized view … or a field map"*); §2.2 the
     two-layer read-only shape (named views only, never `ALL TABLES`; the
     reader role's `default_transaction_read_only`, `statement_timeout`,
     `lock_timeout`, `idle_in_transaction_session_timeout`); §1.2 that
     development may proceed against a synthetic ledger while production stays
     blocked. §2.4's two substitutions stay forbidden: fin's checkpoint
     database and iknow's corpus/index Postgres are not the business source, and
     the public market data loaded for infrastructure proof is not it either.
   - **Surface:** the isolated PostgreSQL instance used for development
     acceptance, and the versioned fixture that seeds it. A real ledger reaches
     the canonical shape through an authorized view or a field map, so the
     fixture must exercise one of those adapters rather than assuming the
     canonical table names.
   - **Acceptance:** the fixture is committed and versioned, and rebuilding from
     it produces the same rows; the expected values a test asserts are written
     **in the fixture DDL**, not read back from the database, so a broken seed
     cannot make a test vacuously true. Against a role holding only the grants
     the spec names, and observed from inside the application connection: a
     `SELECT` on the permitted view returns rows; a direct read of the
     underlying base table fails 42501; a `CREATE TEMP TABLE` fails 25006; any
     write fails 42501; `SHOW transaction_read_only` reads `on`; and the row
     count is unchanged after every write attempt. The permitted-query set is a
     named template list, so a query outside it is refused before a connection is
     opened.
   - **Completion:** a reviewer can rebuild the database from the committed
     fixture and read the expected rows out of the DDL without running anything.
   - **Progress (complete; boundary verified live at `7cfcc10`):**
     `001_schema.sql`, `002_seed.sql`, `003_readonly_role.sql` and
     `tests/retrieve/test_ledger_readonly_boundary.py` are committed; the schema
     and seed are loaded into the isolated dev instance and read back —
     6 contracts seeded, 5 active and visible, the 2026-Q1 window returns the
     four expected rows with `C-2291`'s `notice_by` = 2026-02-12, and the
     terminated row is absent. **The read-only boundary IS now verified:** the
     `fin_sql_reader` role exists on the isolated instance and the §9 leg-2
     probes were OBSERVED (fin `7cfcc10`, postgres:16-alpine) — a base-table
     read/write fails 42501 (the privilege layer), a `CREATE TEMP TABLE`
     fails 25006, a VIEW `UPDATE` fails 55000 (a two-table-join view is not
     auto-updatable — refused before the privilege check, recorded per target
     and never collapsed into the 42501 proof), `SHOW transaction_read_only`
     reads `on` from inside the application connection AND the `ALTER ROLE`
     defaults are read from `pg_db_role_setting` (the §9 correction),
     `role_table_grants` equals the expected list, and row counts are
     unchanged after every attempt. 11 boundary tests + 6 live driver tests
     passed, exit 0 — the run is recorded in
     `docs/validation/2026-09-30-post322-a2-leg2-live-ledger.md`.
   - Status: [x] done — boundary observed live at `7cfcc10`

4. **Bounded business-query module in iknow** — tag: `[implementation]`
   - **Inherits:** spec §1.1: *"fin keeps its single `retrieve` entry point …
     the `sql` 档 is one more value for an existing seam"*; §5(c): *"The
     executor may only send named templates; user text reaches the database as
     bound parameters, never as SQL. An unrecognized template is refused before
     a connection is opened."* §6 limits (20 rows returned, 21 fetched, 32 KiB
     result bytes, 3 s statement timeout, 2 s connect timeout, 1 s lock
     timeout). §5.2: the role is chosen from the verified authorization context,
     and *"the scope is what the credential can reach, not a `WHERE` clause the
     application chose."*
   - **Surface:** a business-query module **independent of the document index**,
     reusing the existing MCP entry. It must not touch chunk, embedding, or
     graph machinery. Existing precedent: every statement in
     `pgvector-backend.ts` is a static template literal with positional `$n`
     parameters over a fixed table set, and `pg` is already a hard dependency
     (`package.json:61`) with a documented lazy-import discipline
     (`pgvector-backend.ts:130-158, 586-608`) so memory-mode users skip it —
     the new module keeps that discipline.
   - **Acceptance:** the module exposes exactly one named template with bound
     parameters, and the template is selected by name rather than assembled from
     text; an unrecognized template name is refused without opening a
     connection; the connection is opened as the role the verified
     authorization context maps to; a row beyond the returned cap is not
     returned and truncation is observable rather than silent (fetch one past the
     cap); a statement that exceeds the statement timeout raises a code distinct
     from both a denial and an unavailability; a read blocked behind a writer
     ends at the lock timeout rather than consuming the statement budget; and no
     credential or sensitive query value appears in any error or log. All six
     boundary classes are asserted, not just the normal path.
   - **Completion:** the module can be driven with a fake pool and every class
     asserted hermetically, and then driven against the real database with the
     same assertions holding.
   - **Progress (hermetic half done; live half NOT met):** landed on iknow branch
     `sql-business-query-module` as `805cff2` + `214c732`, not pushed. Typecheck
     exit 0; the new suites are 81 passing; the 18 pre-existing
     `tests/config.test.ts` failures are confirmed unrelated — reproduced
     identically (18 failed / 170 passed) on a clean unrelated worktree at
     `469f84f`. The module is independent of the index, uses a single named
     template with bound parameters only, and lazily imports `pg` so a
     memory-mode install never loads it. **The live half is NOT met** and the
     row carrier has **no wire home yet**: `DatabaseRow` is declared in the
     module but is not on `RetrieveOutputEnvelope` and no arm references it, so
     the module is currently an island. Placing it is T7.
   - **Found and fixed:** `pg` parses `date` and the view's derived timestamp at
     local midnight, so `toISOString()` cited the ledger's `2026-03-14` as
     `2026-03-13` east of UTC — a citation that is internally consistent and
     false. Fixed to read local components, with a regression test that builds
     the row inside the target zone and asserts across three zones.
   - **Contradictions it surfaced, now recorded in the spec:** §9's
     `SHOW transaction_read_only` proves the *session* setting and not the
     `ALTER ROLE`, so leg 2 must assert both (§9); §6 bounds the response but
     not a single row, and an oversized row now refuses rather than reporting
     zero matches (§9); and `invalid_search_mode` / `empty_query` are listed as
     reused codes but are not union members — they are routing prose today, the
     same defect §7.1 warns about, which T7 inherits (§7.1).
   - Status: [~] hermetic half done — wire placement and live run belong to T7/T8
   - [blocks: T3]
   - [parallel] with T6

5. **Failures and limits made assertable at the boundary** — tag: `[implementation]`
   - **Inherits:** spec §7's taxonomy, including the two distinctions the spec
     insists on: `timeout` must not reuse `retrieve_unavailable` (*"a slow query
     on a healthy database is a different product fact with a different fix
     (narrow the query)"*), and `sql_authorization_unverified` must not collapse
     into `denied` (*"we checked and the answer is no" ≠ "we could not check"*).
     §6's limits.
   - **Surface:** the same business-query module's failure surface and the
     boundary tests for it, in whichever repository T4 placed the module.
   - **Acceptance:** each of empty, invalid, denied, timeout, unavailable, and
     untrusted-context is separately assertable, and a single test run shows all
     six distinguishable — no two collapse into one outcome. The lock-timeout
     class (§6, named but never asserted today) has a case. A named failure is
     never reported as a skip, and an unavailable required dependency is
     recorded as blocked evidence, per `.claude/rules/test.md`.
   - **Completion:** one test run produces six distinguishable outcomes, and a
     reader can tell them apart from the recorded output alone.
   - Status: [ ] pending
   - [blocks: T4] (T5 asserts the failure surface T4 builds; it does not precede it)

6. **`database-row` evidence kind on the fin side** — tag: `[implementation]`
   - **Inherits:** spec §8: the closed quotable set gains a third member and
     *"the closed-set assertion at `tests/host/test_citation_gate.py:1020` is
     updated, not deleted, and existing `company` and `attachment` behaviour is
     untouched"*; §8: *"the 32-code-point span rule does not apply to this kind"*
     and *"Document evidence and database evidence verify by different rules, and
     the asymmetry is deliberate"*; §8.1: opening a citation shows the retained
     snapshot and issues **no** query, a user-initiated refresh is a **new**
     query with new rows, new time, and new evidence, no automatic diff in this
     version, and a past snapshot still requires a current access check. §7: an
     empty SQL miss does not produce ingest advice.
   - **Surface:** fin's citation/evidence contract, its wire projection, its
     export, and the web client's citation type. The named closed sets: the
     quotable kind set (`citation_gate.py:86-88`), the citation display values
     (`workspace_http.py:1723`, currently `{page, unit}` — a database row is
     neither), the id-slot switch in export and in the workspace projection
     (both currently binary, defaulting every unknown kind to a document id),
     and the web kind union (`web/src/types.ts:16`). The evidence item carries a
     required `locator` today, so a row needs its own declared carrier from T1.
   - **Acceptance:** (a) A database claim is verifiable against a `record_key`
     and the `fields` this round returned, and a number the model states that no
     returned `fields` supports is refused — a different failure from a missing
     locator; (b) the span rule applies to document kinds and not to this one, and
     a `notice_days` of `30` is a legitimate citation; (c) opening a citation
     issues no query and shows the retained snapshot with its `queried_at`; a
     refresh is a new query that leaves the prior answer intact; nothing in the
     product states or implies that data is unchanged absent a fresh query; and
     a past snapshot is withheld when current permissions no longer cover that
     record; (d) `company` and `attachment` behaviour is byte-identical to before
     the change, and every closed set that gained a member still rejects the
     kinds it rejected before — the non-quotable kinds stay non-quotable.
   - **Completion:** a reviewer can show a database-row citation opening to a
     retained snapshot with no query issued, and can show the document citation
     path behaving exactly as it did before.
   - **Progress (committed, review not yet run):** `d2c3aa0` on this branch,
     18 files. The kind is threaded through the citation gate, the wire
     projection, the CSV export, the run payload, the CLI and the web client,
     with the closed sets widened rather than replaced. Verified by the
     implementer and re-verified here: the six directly affected test files run
     **270 passed, 2 skipped**, and both skips are environmental
     (`FIN_CHECKPOINT_URL` unset, `web/dist` not built) rather than a missing
     coverage made green.
   - **Not yet done:** the end-to-round behaviour the bullet's acceptance names
     — a citation opening to a retained snapshot with no query issued — cannot
     be demonstrated until the row has a wire home, which is T7. The kind is
     implemented; it is not yet reachable from a real `sql` retrieve.
   - Status: [~] implemented, not yet reachable end to end
   - [blocks: T1]
   - [parallel] with T4 (different repository, disjoint files)

7. **`sql` 档 wired end to end, and fin's three refusal seams retired** — tag: `[implementation]`
   - **Inherits:** spec §4: *"iknow-memory owns SQL execution, in a
     business-query module independent of the document index"* and *"Merge order
     is a hard gate, not a preference … Order: iknow PR first, fin PR second,
     gated on it"*; §1.1 that fin keeps one `retrieve` entry and its per-call 档
     assignment. §7: after A2, `real_db_not_wired` means *not provisioned* only.
   - **Surface:** iknow's search-mode dispatch table row and its failure
     vocabulary; fin's unavailable-tier table and its three independent refusal
     seams — the port, the host decision path, and the HTTP send door. The three
     fin seams must change together in one commit, because the decision-path seam
     returns a null mode and, if it is missed, the request **silently downgrades**
     to a `graph` answer rather than failing.
   - **Acceptance:** a request explicitly assigned `sql` reaches only the
     permitted view and returns bounded rows with evidence the existing answer
     path can verify; `graph`, `global`, and `lexical` answers are unchanged for
     the same request; the 档 is still assigned by the host and never by the
     model; a request that names a tier outside the closed set still fails
     `invalid_search_mode`; and the whole `sql` 档 can be re-closed — the
     unavailable-tier table still works — without touching either of the other
     tiers. Every existing test that pins today's `sql` **refusal** is updated
     to the new expectation with equal-or-stronger assertions, never deleted
     (`.claude/rules/test.md`); the frozen MCP tool surface is unchanged.
   - **Completion:** the same request is answered from a `graph` corpus, a
     `global` corpus, and the `sql` ledger through one entry point, and the
     refusal tests that were updated can be read to show what replaced them.
   - **Progress (landed; completion NOT met — named gap below):**
     `6e8ed97` opened the 档 end to end and consumed row evidence,
     `9888e9b` rewrote fin's retired `sql`-refusal pins to opened-档
     acceptance (equal-or-stronger, none deleted), `d20a7c5` retired the
     stale refusal wording in spec/CONTEXT, `1c6d815` rewrote the
     retrieve-side refusal pins as opened-档 acceptance, and `e077d85`
     completed the E1 row-OBJECT consumption (`database_rows` carries
     `{schema, rows, result_count}`; the bare-array shape is obsolete).
     A dual-axis review of `28fa4da5..e077d85` then landed in this
     branch's follow-up commits: the numeric gate became a per-token
     SUBSET check (a supported figure beside a fabricated one refuses —
     the laundering both axes found), the snapshot key/assembly and the
     omitted-null serializer each gained one owner, source ids escape
     `=`/`,`, and the live ledger suites moved to their own `sql_live`
     mark.
   - **Known gap (why completion is not met):** the graph and the
     workspace send door supply NO `sql_window_start` /
     `sql_window_end` filter pair, so a real user `sql` request
     fail-closes `invalid_business_query` at
     `fin.retrieve.port.resolve_sql_window` before transport — the port
     refuses to default a window, per contract. The 档 is wired and
     hermetically proven end to end through the tool hop (fake port),
     but the acceptance's "one entry point answers sql" for a REAL
     request needs the window plumbed from the ask (host decision or
     pinned 档 filters). That wiring is follow-up work on this lane and
     is named here so grep finds it: until it lands, opening the 档 on
     the wire (T8's combined run) must not be reported as
     user-reachable.
   - Status: [~] wired and refusal seams retired; one real-entry-point
     answer blocked on the `sql_window_*` gap above
   - [blocks: T5, T6]

8. **Acceptance against the live database, and its record** — tag: `[implementation]`
   - **Inherits:** spec §9 leg 2, and its warning that *"leg 2 alone would let a
     green suite stand in for the real boundary, which is exactly what
     `.claude/rules/test.md` forbids"*; §9's required record contents and the
     mandatory *"what this record does NOT prove"* section; §1.2 that the real
     ledger stays blocked.
   - **Surface:** an opt-in live driver for the SQL 档, marked so it is
     distinguishable from the hermetic suite and from a fixture-MCP smoke.
   - **Acceptance:** a non-mocked run against the live database shows the expiry
     query returning the seeded contracts with values matching what the fixture
     DDL independently states; a query outside the template set and any write are
     both refused, with the SQLSTATEs read from the live service rather than
     assumed; a window with no matching contracts explains itself as an empty
     result and not as a corpus needing ingest; every database claim in the
     answer traces to a `record_key` and `fields` from this round's result set;
     and a real user can reach the path through the existing server/retrieve
     route, not only through a test harness. The record names the command, the
     fixture version, the observed results, the exit status, and what it does not
     prove. The real-ledger leg is recorded as **blocked, not skipped and not
     passed** — until a source is named it cannot run, and a green synthetic run
     does not stand in for it.
   - **Completion:** a reviewer can match the sanitized live result to the
     independently expected rows and confirm no write occurred; and the record
     says in its own words what remains unproven.
   - **Progress (leg 2 run and recorded; user-reachable leg NOT met):**
     `7cfcc10` landed the opt-in driver (`test_live_sql_business_db.py`,
     since re-marked `sql_live` for the §9 distinction) and ran leg 2 on
     the live dev instance: 11 boundary + 6 driver tests passed, exit 0
     (postgres:16-alpine, iknow `d07ef4f`, fin `0ac256b`). The sanitized
     record is `docs/validation/2026-09-30-post322-a2-leg2-live-ledger.md`.
     Leg 1 (the operator's real ledger) stays BLOCKED — no source named;
     blocked evidence, not a skip, not a pass. "A real user can reach
     the path through the existing server/retrieve route" is NOT met:
     the T7 known gap (`sql_window_*` supplied by no entry surface)
     fail-closes a real `sql` ask as `invalid_business_query`, so this
     bullet's user-reachability clause carries over with that wiring.
   - Status: [~] leg 2 done and recorded — leg 1 blocked; the
     user-reachability clause waits on the T7 `sql_window_*` gap
   - [blocks: T7]

## Dependency graph

```text
T1 (decision: row carrier + token vocabulary) ─> T6 (fin evidence kind) ─┐
T2 (decision: empty + unverified exits) ─────────────────────────────────┤
T3 (synthetic ledger fixture) ─> T4 (query module) ─> T5 (failures assertable) ─┤
                                                                          ├─> T7 (wire + retire 3 seams) ─> T8 (live acceptance)
```

T2 ∥ T1; T4 ∥ T6. Everything else is a chain. T7 is the merge gate: the iknow
change lands first, the fin change second, gated on it.

## 待写入 (persist)

- **Domain term:** `database-row` is a new quotable evidence kind alongside
  `company` and `attachment`, with its own identity (`record_key` + `source`)
  and its own currency (`queried_at`). It is **not** a document locator and
  carries no span rule. `docs/CONTEXT.md` and the citation-gate ADR set do not
  yet describe it. T1 fixes the exact term; T6 adds the kind.
- **ADR consideration:** ADR 0046 says *"a source marker without a matching
  span is not a citation."* The database-row kind verifies by `record_key` +
  `fields` rather than by span. **Written as `docs/adr/0091-database-row-citation-verifies-without-a-text-span.md`
  (Status: proposed).** It records the judgement — a span-less citation is
  intentional, not a defect — and the two rejected alternatives, so a later
  reader does not "fix" it. It does **not** fix the wire shape, which T1 owns;
  the ADR becomes `accepted` when T1 lands and the shape is decided.
- **CONTEXT.md — blocked on T1:** the source-locator term
  (`docs/CONTEXT.md:189`) currently reads company as `{document_id, version, unit}`
  and attachment as `{kind: "attachment", attachment_id, version, unit}`. A
  database row is a third **identity shape**, not a third locator spelling, and
  giving it its own term keeps ADR 0046's span rule from appearing to apply to it.
  The wording is gated on T1 because writing a term for an undecided shape would
  fix a contract this plan explicitly defers.
- **Cross-reference:** `specs/search-mode-lexical-sql-tokens.md` assumption 8
  locks `sql` as *"a 档 that still refuses"*, and
  `plans/search-mode-retrieve.md:45-51` marks the unwired refusal as done. Both
  become stale the moment T7 lands and should be updated in the same change.
- **Out of scope, recorded not forgotten:** `GET /api/query`
  (`iknow-memory/src/dashboard-api/handlers/query.ts:56`) bypasses the 档 table
  by calling `assembleRetrievalResult` directly, so a SQL port is invisible
  there. Not a defect introduced here; named so it is not mistaken for coverage.
