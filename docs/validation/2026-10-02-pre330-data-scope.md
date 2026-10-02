# Pre-330 T6 — development-only data scope: what the read-only fixtures can and cannot prove

Date: 2026-10-02. Task: `plans/pre330-remaining-prerequisites.md` T6. Basis: `plans/post321-real-sql.md`
A3b; `plans/post321-real-browser-acceptance.md` B3; `specs/search-mode-business-sql-contract.md`;
`tests/retrieve/fixtures/ledger/`. This record was a proposal for operator ruling; the operator
ruling, recorded 2026-10-02, is below. It executes no browser leg, changes no product code, and
closes nothing.

## Purpose and authority

T6 is the data-scope decision task. Its acceptance is to use already-authorized development
read-only fixtures to state exactly which browser behaviors a development-only scope can prove and
which enterprise-ledger claims remain unproved, then obtain an operator ruling only on whether to
amend #330's acceptance to allow that bounded scope. Development fixture access was already
authorized, so no renewed permission was requested here.

T6 inherits A3b unchanged (`plans/pre330-remaining-prerequisites.md:80`). A3b requires an
**operator-approved** ledger whose query values a reviewer can independently see in that ledger's own
system of record, and it states plainly that a synthetic ledger cannot close the leg
(`plans/post321-real-sql.md:48`). The synthetic-source prohibition is carried by
`specs/search-mode-business-sql-contract.md` — not by `specs/enterprise-evidence-contract.md`, which
contains no such clause. This record does not reinterpret, weaken, or satisfy any of them.

What this record is not: not a browser acceptance run, not an A3b pass, not a claim that the
development fixtures or the public filing corpus are a business source, and not a CI statement.

## What the development read-only scope CAN prove

Source: B3's eight acceptance steps at `plans/post321-real-browser-acceptance.md:37-44`. The
development read-only fixture ledger is `tests/retrieve/fixtures/ledger/`, whose own header declares
it a DEVELOPMENT fixture, "not a business source", containing no real operator's data
(`001_schema.sql:1-7`). The committed public attachment is `data/corpus/g4_attachment_gold/`
(manifest-pinned PDFs), and `data/corpus/dataset/t1/expense-policy-001.md` is a reviewed
expense-policy document with both a present rule and a clear absent-rule surface.

| Step | Line ref | Provable on dev? | What exactly it proves |
| --- | --- | --- | --- |
| S1 | `:37` | Yes | Thread create/open, composer upload of the committed public attachment, and send all driven from snapshot-derived controls in a real `playwright-cli` session, with no HTTP/API setup. Data-independent. |
| S2 | `:38` | Partly | The document/attachment half: a citation opens the cited source and the displayed quote matches the expected span from committed manifests; SQL result *shape* and visual claim scope are inspectable. The database half is anchored to the A3b contract and is not provable — see below. |
| S3 | `:39` | Yes | First visual turn refreshes insufficient evidence through the real configured provider and persists an observation in real Postgres against original bytes in the real object store; a later turn reuses it with no re-upload, provider call, or version increment. Data-independent of the ledger. |
| S4 | `:40` | Yes | Durable history, attachment state, citation navigation, and visual reuse after a real server restart, plus the named race and empty/invalid/overflow/concurrent/exception exits. Data-independent. |
| S5 | `:41` | Partly — NOT authorized by this amendment | Not amendment-authorized. Its data-independent half — the #276 live MCP / `pg_es` reconciliation and the required Jev round-trip — is discharged by the separate T7 live MCP / Jev evidence record, which is cited here as reconciliation evidence and NOT as a pass of the enterprise half. Its enterprise half is the separately defined S5b below, which stays unclosed; this amendment does not authorize S5. |
| S5b | `:41` | **No** | The clause requires the final business-source SQL turn to execute "against the approved ledger path delivered by A3b", and states that a `real_db_not_wired` refusal or a synthetic-only result does not close that leg. |
| S6 | `:42` | Yes (mechanism) | The Session E evidence-schema contract end to end: document/attachment source location and quoted spans stay verifiable; database-row evidence shows the original bounded snapshot and `queried_at` without implicit re-query, an explicit refresh creates new evidence, and a historic snapshot rechecks current access. This proves the **schema and access mechanism**, over development row values only. |
| S7 | `:43` | Yes | A present rule in `expense-policy-001.md` (`§1`, the 75/110 USD working-meal limits) yields a grounded answer citing that source; an absent rule yields an explicit insufficient-evidence outcome, not `off_topic` or invention. The document's monetary amounts also exercise the clause that an amount alone must not select the financial/XBRL path. |
| S8 | `:44` | Yes | The recording discipline: commands, exit codes, session name, revisions, gold hashes, expected versus observed values, deltas, and every failure. Data-independent. |

The development read-only boundary itself is already independently pinned and is evidence, not
assertion: 6 contracts seeded with 5 visible through the view
(`tests/retrieve/test_ledger_readonly_boundary.py:84-85`, enforced at `:266` and `:285`), and a live
window query expected to return exactly 4 rows with ids `C-2315`, `C-2302`, `C-2291`, `C-2024Q1`
(`tests/retrieve/test_live_search_modes_pg_es.py:491-493`). The fixture records the same counts for a
by-hand reviewer (`002_seed.sql:58-59`).

## What remains UNPROVED and why

1. **S2's enterprise-anchored comparison** (`:38`). The step requires comparing visible answers,
   citation locators, SQL result shape, and visual claim scope "against independently authored
   expected values from the committed manifests **and the settled A3b enterprise-ledger contract**".
   The second anchor does not exist. `specs/search-mode-business-sql-contract.md:814-820` records
   the two substitutions that remain forbidden and states the public market data loaded for
   infrastructure proof "proves the plumbing, not the product question"; `:231-236` says the same of
   the public market tables — useful "for exactly one thing — proving the infrastructure … can be
   exercised end to end while the ledger source is being sourced." A development fixture validates
   plumbing without validating the product question.
2. **S5b's approved-ledger browser SQL turn** (`:41`). Quoted in the table above; unproved for the
   same reason, and explicitly not closable by a synthetic-only result.
3. **No real tenant scope in development data.** `owner_dept` is a free-text row label
   (`001_schema.sql:45`), not an enforced tenant scope. The enforced scoping is the view predicate
   `status = 'active'` (`001_schema.sql:84`) plus grants on the named views
   (`003_readonly_role.sql:21-22`). So development data can prove read-only role and view
   enforcement, and cannot prove tenant isolation or per-department authorization.
4. **Volume limits.** A 4-row result against a 6-row table is below any pagination or truncation
   threshold. The `rows returned` cap is 20 (`specs/search-mode-business-sql-contract.md:475`; the
   constant is defined as `_LIMIT_CAP = 20` at `src/fin/retrieve/mcp_adapter.py:171`, so the spec's
   own cited line `:159` is stale). Development data never approaches that cap, so truncation
   behavior under the cap is unproved here.
5. **Business correctness of any row value.** Dates, counterparties, and amounts in the fixture are
   invented for path exercise. Nothing about them says anything about the operator's books.

## Amendment to #330's acceptance (in force per the 2026-10-02 operator ruling)

Wording proposed by T6, narrow and bounded, **in force** as of the operator ruling recorded below:

> #330's B3 acceptance may be recorded as **partially evidenced on development data** when the
> database source is the committed read-only fixture ledger under
> `tests/retrieve/fixtures/ledger/`, applied to a disposable local Postgres through the read-only
> role, provided the record: (1) labels the source a development substitute and cites the fixture's
> own declaration that it is not a business source; (2) states the independently checkable counts
> (6 seeded, 5 visible through the view, 4 rows expected in the live window query) and the tested
> revisions; (3) marks every browser SQL turn and every database-row evidence item as
> synthetic-source; and (4) records S2's enterprise-anchored comparison and S5b's approved-ledger
> SQL turn as **UNPROVED — blocked on A3b**, naming the missing approved ledger as the blocker.

This amendment does **not** waive any of the following, and none of it may be read as doing so:
A3b stays blocked; a `real_db_not_wired` refusal or a synthetic-only result does not close the
business-source leg; the B3 header bans on scripted models, Fake ports, fixture MCP servers, mocked
routes, API-seeded conversation turns, and skipped live legs stand unchanged
(`plans/post321-real-browser-acceptance.md:33`); no synthetic or public row becomes a business
source; #330 is not marked accepted or closed by a development run; and A3a's earlier mechanism
demonstration still "cannot close the original company-source leg" (`:35`).

## Operator ruling (recorded 2026-10-02)

> **RULING: #330's browser acceptance IS AMENDED, narrowly, to allow a bounded development-only
> evidence scope.**
>
> - B3's browser MECHANICS may be exercised against development read-only data. The six B3 steps
>   the table above shows as provable on dev data (S1, S3, S4, S6-mechanism, S7, S8) may proceed.
> - The two enterprise-ledger-dependent clauses REMAIN UNCLOSED and are NOT waived: S2's
>   enterprise-anchored comparison (`plans/post321-real-browser-acceptance.md:38`) and S5b's
>   approved-ledger browser SQL turn (`:41`). Neither may be reported as satisfied by development
>   or synthetic rows.
> - A3b REMAINS BLOCKED AND UNPASSED. This amendment does not approve a ledger, does not create a
>   business source, and does not change `plans/post321-real-sql.md:45-51`. Original enterprise A3b
>   stays separate, deferred, and not passed.
> - The non-waiver list in the proposal stands unchanged: no real tenant isolation is proven, the
>   4-of-6 row volume never approaches the 20-row cap
>   (`specs/search-mode-business-sql-contract.md:475`; the constant actually at
>   `src/fin/retrieve/mcp_adapter.py:171`), and no independent system-of-record cross-check exists.
> - The amendment is bounded to BROWSER ACCEPTANCE MECHANICS ONLY. It grants no permission to
>   relabel, restate, or report any development result as enterprise evidence.

Ruling date: 2026-10-02. Attributed as an operator decision on this record. The amendment is **in
force** from this date: the development-only scope above is no longer an open question, and no
future step may read the earlier "not in force" wording as current.

T6's completion condition is now satisfied — the proposal and the scope ruling are both recorded
(`plans/pre330-remaining-prerequisites.md:80-83`). A3b itself remains blocked and unpassed, and
whether B3 starts is ROOT's T10 decision, not this record's.

## A3b status restated

A3b remains **BLOCKED — not passed, not skipped, not deferred-passed**. The line that keeps it there
is `plans/post321-real-sql.md:49`: "If no approved ledger exists, leave A3b explicitly blocked."
Its task status is unchanged at `plans/post321-real-sql.md:50`, `Status: [ ] blocked on real source
and access`. `specs/search-mode-business-sql-contract.md:741-742` holds the same position: the leg
"cannot run until a real ledger is identified (§2.4); until then it is blocked evidence, not a skip
and not a pass." This record changes no A3b status and creates no path that marks it otherwise.

## Zero-baseline precondition (T8 disposition ruled 2026-10-02)

> **RULING (T8/T9): the B3 gold case is NOT selected to include a zero baseline. T8 is therefore
> OUT OF SCOPE and creates NO BLOCKER for #330. T9 is NOT started.**

The conflict between the producer's `"percent": null` at a zero baseline
(`src/fin/host/tools/compare_facts.py:275-277`, locked by
`tests/host/test_compare_facts.py:261-294`) and the frozen numeric-only §4.3 validator
(`src/fin/host/evidence_contract.py:1272-1273` and `specs/schemas/evidence/comparison.schema.json:76`,
whose `percent` type is `["number","integer"]`) is NOT resolved by this ruling. It is recorded as an
open BACKLOG ITEM. Do NOT weaken the validator or the schema, and do NOT fabricate a zero or infinity
percentage. B3 does not require a zero-baseline gold case, so the conflict does not block #330.

Supporting corpus fact, unchanged by the ruling: `data/corpus/facts/` holds 9 sidecars, of which 8
contain `"facts": []`; the single populated one, `data/corpus/facts/AAPL-2023Q3.json:4,13`, holds
`us-gaap:Revenues` = [private source value removed] for period `2023-Q3-YTD`. That is one non-zero value for one concept, not
a zero baseline.

## Evidence index

| Claim | Evidence |
| --- | --- |
| A3b requires an operator-approved ledger; synthetic cannot close it | `plans/post321-real-sql.md:48` |
| A3b stays explicitly blocked absent an approved ledger | `plans/post321-real-sql.md:49` |
| A3b current status line | `plans/post321-real-sql.md:50` |
| B3 bans scripted models, Fake ports, fixture MCP servers, mocked routes, API-seeded turns, skipped live legs | `plans/post321-real-browser-acceptance.md:33` |
| A3a on synthetic rows cannot close the company-source leg | `plans/post321-real-browser-acceptance.md:35` |
| S1 browser/thread/upload | `plans/post321-real-browser-acceptance.md:37` |
| S2 enterprise-anchored comparison | `plans/post321-real-browser-acceptance.md:38` |
| S3 vision refresh and reuse | `plans/post321-real-browser-acceptance.md:39` |
| S4 restart durability and race cases | `plans/post321-real-browser-acceptance.md:40` |
| S5 / S5b #276, Jev round-trip, approved-ledger SQL turn | `plans/post321-real-browser-acceptance.md:41` |
| S6 evidence-schema contract | `plans/post321-real-browser-acceptance.md:42` |
| S7 expense-policy present and absent rules | `plans/post321-real-browser-acceptance.md:43` |
| S8 recording discipline | `plans/post321-real-browser-acceptance.md:44` |
| Two substitutions remain forbidden; public data proves plumbing, not the product question | `specs/search-mode-business-sql-contract.md:814-820` |
| Public market tables are not the business source | `specs/search-mode-business-sql-contract.md:231-236` |
| Leg is blocked evidence, not a skip and not a pass | `specs/search-mode-business-sql-contract.md:741-742` |
| `rows returned` cap is 20 | `specs/search-mode-business-sql-contract.md:475`; `src/fin/retrieve/mcp_adapter.py:171` |
| Fixture declares itself a development fixture, not a business source | `tests/retrieve/fixtures/ledger/001_schema.sql:1-7` |
| `expiry_date` NOT NULL; date-order CHECK | `tests/retrieve/fixtures/ledger/001_schema.sql:38`, `:48` |
| `owner_dept` is a free-text label, not enforced scope | `tests/retrieve/fixtures/ledger/001_schema.sql:45` |
| Enforced view predicate `status = 'active'` | `tests/retrieve/fixtures/ledger/001_schema.sql:84` |
| Grants on the named read-only view | `tests/retrieve/fixtures/ledger/003_readonly_role.sql:21-22` |
| Seed counts for a by-hand reviewer: 4 / 6 / 5 | `tests/retrieve/fixtures/ledger/002_seed.sql:58-59` |
| `TOTAL_SEEDED = 6`, `VISIBLE_THROUGH_VIEW = 5` and their enforcement | `tests/retrieve/test_ledger_readonly_boundary.py:84-85`, `:266`, `:285` |
| Live window query expects exactly 4 rows, named ids | `tests/retrieve/test_live_search_modes_pg_es.py:491-493` |
| Committed public attachment gold | `data/corpus/g4_attachment_gold/manifest.json` |
| Reviewed expense-policy document used by S7 (present rule / excluded surface) | `data/corpus/dataset/t1/expense-policy-001.md:8`, `:24-26` |
| Corpus is 21 documents, twelve original 10-Qs frozen | `data/corpus/manifest.json` (`documents`, `note`) |
| Facts sidecars: 9 total, 8 empty, one populated with `us-gaap:Revenues` = [private source value removed] | `data/corpus/facts/AAPL-2023Q3.json:4,13` and sibling sidecars |
| Frozen-gold ADR lineage (superseded by 0029) | `docs/adr/0012-v1-eval-nine-gold-cases.md:1-5` |
| T6 authorization and completion condition | `plans/pre330-remaining-prerequisites.md:80-83` |
