# The business SQL 档 contract (A1 decision brief)

Status: **historical A1 decision brief — [ADR 0094](../docs/adr/0094-structured-corpus-search-and-knowledge-maintenance.md) supersedes this brief's separate
enterprise-ledger source and contract-expiry-only primary target.** Its former
delivery details remain historical context and are not the current structured-
search contract. ADRs 0091–0093 continue to govern existing database-row evidence
until a replacement SPEC explicitly decides otherwise. Runtime behavior remains
unchanged pending that SPEC and implementation; this status update does not waive
any issue #330 acceptance obligation.

Refs: issue #322 (`plans/post321-real-sql.md` A1), `specs/search-mode-retrieve.md`,
`specs/search-mode-lexical-sql-tokens.md`, `docs/adr/0046`, `docs/adr/0021`,
`docs/adr/0059`, `docs/adr/0087`, `docs/adr/0090`.

## 1. What this brief settles

**Settled, per operator decision:** the source class — an enterprise operations
ledger, contract expiry as the first and only query path; the division of
responsibility; the reference schema and its adaptation rule; the read-only and
authorization model; the database-row evidence shape; and the acceptance.

**Scope of the first delivery — deliberately narrow.** One parameterized contract
query. No free-form SQL. No `invoice` or `payment` queries. This is a reliable
contract query, not the first step toward a general enterprise data platform, and
the boundary is chosen so the next business system can be added without
redesigning the seam.

### 1.1 Division of responsibility

| Concern | Owner | What that means concretely |
|---|---|---|
| user identity, session orchestration, answer and display | **fin** | fin resolves who is asking, decides the 档, renders the answer and its citations |
| source connection, constrained query, database-side enforcement | **iknow** | iknow holds the credential, runs the parameterized template, lets the database enforce the scope |

fin keeps its **single `retrieve` entry point** and its per-call 档 assignment; the
`sql` 档 is one more value for an existing seam, not a new tool and not a new
path around the port. The SQL module in iknow is **independent of the document
index** and reuses the MCP entry, so a ledger query touches no chunk, embedding
or graph machinery.

### 1.2 Development may proceed; production stays blocked

An isolated PostgreSQL and a **versioned synthetic ledger** are enough to build
and verify the whole path (A2 and the §9 leg-2 acceptance). What stays blocked
until a real source and its authorization exist: production wiring, and the §9
leg-1 real-acceptance run. Neither the checkpoint database nor iknow's
corpus/index Postgres may stand in for the business source.

## 2. The business source: an enterprise operations ledger

**The source is an enterprise operations ledger (企业台账), with contract expiry
(合同到期) as the first query class.** That is the operator's decision and it
settles what the tier is for.

fin is a knowledge-QA entry point whose first corpus happens to be filings
(ADR 0090); the corpus domain is not the product identity. So the tier's value is
not "financial data" — it is that a question about the operator's own books can be
answered from a structured system, with citable evidence, without ingesting a
single document. A contract-expiry question is exactly that: no filing contains
your vendor's renewal date, and no document corpus can produce one.

**The real source is not yet determined, so production ingestion stays blocked**
(§1.2). What follows is the **reference structure** a real ledger is mapped onto,
plus the adaptation rule. §2.4 records what was loaded while establishing
feasibility, and why it does not count as the source.

### 2.1 Reference structure, not a schema to impose

**No enterprise has to reshape its database to match this spec.** The tables
below are the *canonical shape* the tier queries; a real ledger reaches it
through one of two adapters, and nothing else changes:

- **an authorized view** the data owner creates in their own database, mapping
  their columns onto the canonical names; or
- **a field map** in the query configuration, naming which of the source's
  columns fills each canonical field.

The constraint that must hold either way is only that the eight fields the
expiry query needs are obtainable and the read is authorized. A ledger that
calls its expiry column `renewal_dt` or stores the counterparty as a free-text
name is a normal case, not a rejection.

```sql
-- Canonical contract shape: the reference, not a demand.
CREATE SCHEMA IF NOT EXISTS ledger;

CREATE TABLE ledger.contract (
    contract_id      text        PRIMARY KEY,
    counterparty_id  text        NOT NULL REFERENCES ledger.counterparty (counterparty_id),
    title            text        NOT NULL,
    contract_type    text        NOT NULL,          -- goods | service | lease | nda | employment
    effective_date   date        NOT NULL,
    expiry_date      date        NOT NULL,
    auto_renew       boolean     NOT NULL DEFAULT false,
    notice_days      integer     NOT NULL,          -- days before expiry that notice must be given
    renewal_term_days integer,                      -- NULL when the contract does not renew
    total_amount     numeric(16,2),
    currency         char(3),
    status           text        NOT NULL,          -- active | terminated | expired | renewed
    owner_dept       text        NOT NULL,
    source_system    text        NOT NULL,          -- provenance: which system of record asserts this
    ingested_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT contract_date_order CHECK (expiry_date >= effective_date),
    CONSTRAINT contract_notice_sane CHECK (notice_days >= 0),
    CONSTRAINT contract_status_closed CHECK (
        status IN ('active', 'terminated', 'expired', 'renewed')
    )
);

CREATE INDEX contract_expiry_idx ON ledger.contract (expiry_date)
    WHERE status = 'active';
```

A supporting party table, so a contract-expiry answer can name the counterparty
without a second lookup:

```sql
CREATE TABLE ledger.counterparty (
    counterparty_id  text PRIMARY KEY,
    legal_name       text NOT NULL,
    entity_type      text,                          -- vendor | customer | partner
    country          char(2),
    risk_rating      text,                          -- low | medium | high | unknown
    is_active        boolean NOT NULL DEFAULT true
);
```

**A ledger is never just contracts, but this delivery is contracts only.**
`invoice`, `payment` and `renewal_notice` will follow, and the read-only grants
(§2.2) and the permitted-query set (§5) are shaped so adding them does not
redesign the seam. None of them is built now: the first delivery is one
contract-expiry path, and building the others now would be the "general
enterprise data platform" this spec explicitly is not.

### 2.2 Read-only permissions

Two orthogonal, database-enforced layers — the same structure as §5, applied to
this source. Both are required: (a) bounds what the reader can *change*, (b)
bounds what it can *see*.

**(a) Reader role — DB-enforced against writes.** Granted `SELECT` on **named
views only**, never `ALL TABLES`, so a base table added later is not silently
reachable. `default_transaction_read_only` is the belt-and-braces layer: it is
the only thing that refuses a `CREATE TEMP TABLE`, which is precisely the probe
A3 must use to prove the layer actually took effect.

```sql
CREATE ROLE fin_sql_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE :business_db TO fin_sql_reader;
GRANT USAGE  ON SCHEMA ledger_ro TO fin_sql_reader;
GRANT SELECT ON ledger_ro.contract_expiry TO fin_sql_reader;
ALTER ROLE fin_sql_reader SET default_transaction_read_only = on;
ALTER ROLE fin_sql_reader SET statement_timeout = '3s';
ALTER ROLE fin_sql_reader SET lock_timeout = '1s';
ALTER ROLE fin_sql_reader SET idle_in_transaction_session_timeout = '10s';
```

**(b) View layer — DB-enforced against over-reading.** The reader is granted on
`ledger_ro`, and the views are the scope. This is the layer that enforces "whose
rows" — a `WHERE` the application wrote is policy, not enforcement.

```sql
CREATE SCHEMA ledger_ro;

-- The permitted read surface: active contracts, with a projection that keeps
-- notice arithmetic out of the query and puts the derived deadline in-band.
CREATE VIEW ledger_ro.contract_expiry AS
    SELECT c.contract_id,
           c.title,
           c.contract_type,
           p.legal_name       AS counterparty,
           p.risk_rating,
           c.effective_date,
           c.expiry_date,
           c.auto_renew,
           c.notice_days,
           c.expiry_date - (c.notice_days || ' days')::interval AS notice_by,
           c.total_amount,
           c.currency,
           c.owner_dept,
           c.source_system
      FROM ledger.contract c
      JOIN ledger.counterparty p USING (counterparty_id)
     WHERE c.status = 'active';
```

A direct `SELECT` against `ledger.contract` must fail 42501 for this role — that
is the assertion, not a comment.

**Identity boundary.** The draft above is a *single shared reader role*, which is
correct only while the ledger is not multi-tenant. If the operator's ledger is
per-department or per-entity, the role must become per-scope (or use database row
level security) before A2 — otherwise every contract is visible to every operator.
This remains open (§5.2).

### 2.3 The first query class: contract expiry

The permitted query set (§5) starts with exactly one template, parameterised,
never assembled from user text:

```sql
-- template: contracts_expiring
SELECT contract_id, title, counterparty, expiry_date, notice_by,
       total_amount, currency, owner_dept
  FROM ledger_ro.contract_expiry
 WHERE expiry_date BETWEEN $1::date AND $2::date      -- window bounds
 ORDER BY expiry_date
 LIMIT $3                                          -- capped per §6
```

The question it answers: *"which contracts expire in the next 90 days, who are
the counterparties, and by when must we give notice?"* Nothing in a document
corpus can answer it — the ledger is the only place these rows exist.

### 2.4 Status of the real source: still blocked

No enterprise operations ledger has been identified. The tables above are a
**design**, not a loaded dataset, and A2 must not be written against them as if
they held real rows.

What *was* loaded, while establishing that a real database could be stood up and
queried, is public market data in a separate instance (`fin_business` on
`localhost:5434`, not the `5433` checkpoint store and not iknow's corpus/index
Postgres, per `plans/post321-real-sql.md:25`):

| Schema.table | Rows | Range | Source and licence |
|---|---|---|---|
| `fiscal.avg_interest_rate` | 408 | 2024-01-31 … 2025-12-31 | api.fiscaldata.treasury.gov — "free, without restriction, and available to copy, adapt, redistribute, or otherwise use for non-commercial or commercial purposes" |
| `rates.treasury_yield` | 998 | 2024-01-02 … 2025-12-31 | home.treasury.gov daily yield curve — US government work, public domain |
| `market.daily_price` | 2 268 | 2024-01-02 … 2024-12-31 | stockanalysis.com — redistribution restricted; see below |

These are **not the business source and must not be treated as one**: they are
public market data, they are financial, and they stand in no relation to an
enterprise's own books. They are useful for exactly one thing — proving the
infrastructure (a real Postgres, a read-only role, a typed failure path) can be
exercised end to end while the ledger source is being sourced. A fixture built
on them would validate the plumbing without validating the product question.

### 2.5 Known constraint on the market table

`stockanalysis.com` is an undocumented internal API whose terms allow internal
use but restrict republication. That is acceptable for a development-period
infrastructure check and **must not ship to end users** without a licensed
replacement. Since the market tables are not the business source in any case
(§2.4), this constraint is informational rather than blocking.

### 2.6 What was already on the machine, for contrast

| Asset | Class | State |
|---|---|---|
| `localhost:5433` `postgres:16-alpine` db `fin` | fin checkpoint (ADR 0015 `PostgresSaver`) | up |
| `localhost:5432` `iknow_memory` pgvector | iknow corpus/index ledger | down |
| `127.0.0.1:9200` Elasticsearch 8.19 | iknow precision layer | down |
| `localhost:7687` Neo4j 5.26 | iknow graph projection | down |
| `localhost:5434` `fin_business` | dev instance for infrastructure proof only (§2.4) | up |

No `FIN_BUSINESS_*` / vendor key existed before this change. No
`.sql`, `.dump`, or `.sqlite` fixture exists in either repository. fin's 15 SQL
tables are all product runtime state (`fin_thread`, `fin_run`, `fin_memory`, the
attachment tables, the ingest tables) — none is a system of record.
`plans/post321-real-sql.md:25` forbids using the first two as the business
source, and that ruling is correct: fin's checkpoint credential is a *write*
credential by construction (the saver writes every turn).

## 3. Why the tier is warranted

Adversarial review of "is this a duplicate of what fin already has?"

**Would a business DB duplicate `graph`?** Only for data that lives in
documents. `docs/adr/0059-retrieve-tiers-use-index-artifacts.md:6` fixes it:
*"D is the business SQL path; figures that live only in filings stay on B."* A
database holding company fundamentals is therefore a duplicate and is rejected.

**An operations ledger is the opposite case.** A contract register is not
derivable from any document: the renewal date, the notice window, and the
counterparty's risk rating are ledger facts that exist only because some system
of record maintains them. Ingesting the contracts as PDFs would not make the
question answerable — it would make it *less* answerable, because a document
corpus cannot express "which of my 400 contracts expire in the next 90 days and
by when must I give notice" as a query at all.

This is also the reason the tier is not finance-shaped. A previous draft of this
brief justified the tier with market and valuation data and proposed interest
rates as a "domain-neutral" sample; both were wrong, because US government debt
rates are financial data. The tier's justification is the operator's own books,
not a market.

**What a document corpus structurally cannot contain**, verified against the
current corpus:

| Class | Why documents cannot answer it | Evidence |
|---|---|---|
| contract expiry / notice windows | ledger facts, maintained by a system of record, not text | no contract, counterparty, renewal or notice concept exists anywhere in the corpus |
| intraday / intra-quarter | a filing cannot contain a figure that did not exist at filing time — structural, not a coverage accident | `intraday`, `intra-day`, `real-time`, `bid/ask` → 0 hits; catalog periods are reporting periods (`2022 Q3`, `2023 Q1/Q2/Q3`, `2023 FY`, plus a `live` bucket) |
| market / valuation (price, market cap, P/E) | the data lives in a system, not a document | `closing price` and `price-earnings` → **0 hits** across all 21 documents / 637 page units. `market value` does occur (64 hits) but only as risk-factor boilerplate about *investment* fair value, never as the 10-K cover-page aggregate market value; `market capitalization` occurs only in a stock-decline risk sentence |

**Not warranted, and A1 must reject it if this is the answer:** a business DB
holding *company fundamentals*. That is a duplicate of `graph`.
`docs/adr/0059-retrieve-tiers-use-index-artifacts.md:6` fixes it: *"D is the
business SQL path; figures that live only in filings stay on B."* The host
already says so to the model at
`src/fin/host/search_mode_decision.py:140-143` — `graph` is *"a knowledge
question answered from the filed documents, including any figure that only lives
in a filing (revenue, net sales, gross margin, growth)."*

**Corollary, and it constrains sequencing:** building the tier against filings
data would duplicate `graph`. The `compare_facts` / `get_xbrl_fact` host tools
already implement filing-derived comparison, and they are currently **empty for
an orthogonal reason** — no iXBRL parser exists
(`src/fin/corpus/facts_seed.py:5-6` defers it to a later ADR;
`src/fin/corpus/live_fetch.py:465-469` writes `{"facts": []}`). Measured: 8 of 9
sidecars are empty; the one populated file holds a single fact
(`AAPL-2023Q3`, `us-gaap:Revenues = [private source value removed]`, `scale: millions`). A 4×4
AAPL/MSFT/AMZN/GOOG matrix over `2023-Q1/Q2/Q3-YTD` returns **1 hit of 16 cells
and 0 deltas** (the period spelling matters: the sidecar stores
`2023-Q3-YTD`, so catalog-style `2023 Q3` labels match nothing).

So: **the XBRL-parser decision should be made before the SQL decision.** Closing
the parser gap makes `compare_facts` fully functional, which makes a
filings-sourced SQL 档 *more* redundant, not less. Shipping the parser and then
opening the SQL 档 on the same corpus would be building the redundant thing.
This is a recommendation about sequencing across tickets, not a ruling inside
#322.

## 4. Settled: ownership and module placement

**iknow-memory owns SQL execution, in a business-query module independent of the
document index.** The operator has decided this; the reasons below are recorded
because they were the basis of the decision.

The business database is an external dependency of iknow's retrieve capability,
not a new bounded context of its own.

- `docs/context-map.md:33` names iknow-memory as "a context of its own: the
  product 库, the ranking and vector/index authority, and the company-source
  boundary"; `:37` puts company-source read *slicing* there; `:40` states
  retrieve does not own ranking. A DB client that plans text→SQL and ranks rows
  *is* ranking, so fin-side ownership would put a ranking engine inside a
  context whose charter forbids it.
- The `sql` 档 rides `SEARCH_MODE_ARMS`
  (`iknow-memory/src/core/retrieval/search-mode.ts:177-186`) as one row of an
  existing table; dispatch is a single indexed lookup
  (`src/tools/retrieve.ts:229`). The frozen 8-tool MCP surface is untouched —
  `tests/index.test.ts:166-175` pins it by exact equality and stays green.
- fin's change is one table row: `TIER_UNAVAILABLE_CODE`
  (`src/fin/retrieve/tiers.py:80-82`).

**Merge order is a hard gate, not a preference.** If fin deletes the `sql` row
before iknow replaces `modeDNotWiredArm`, a `sql` send passes fin's two
pre-transport refusals, reaches the wire, and iknow answers with **prose** —
`real_db_not_wired` lives in the message text, not a JSON `error` field
(`iknow-memory/src/index.ts:633` returns `textResult(message, true)`). fin's
adapter then falls through its token scan and raises `retrieve_unavailable`.
**The operator sees a false error.** Order: iknow PR first, fin PR second,
gated on it.

## 5. Read-only enforcement, and what each layer can prove

Three layers. Only the first two are database-enforced — they survive a bug in
the calling application. The concrete DDL for the operations ledger is in §2.2;
this section states why each layer is shaped the way it is.

**(a) Reader role — DB-enforced against writes.** Granted `SELECT` on **named
views only**, never `ALL TABLES`: `ALL TABLES` would also cover base tables that
later land in the same schema, silently widening the blast radius of a leaked
credential.

`GRANT SELECT` fails `INSERT`/`UPDATE`/`DELETE` at the privilege layer
(42501). `default_transaction_read_only` additionally refuses a `CREATE TEMP
TABLE` (25006) — the one probe that proves this layer specifically, since a
temp table is otherwise permitted on a read-only-ish grant shape.

**(b) Read-only view layer — DB-enforced against over-reading.** The role is
granted `SELECT` on named views in a `ledger_ro` schema, never on base tables.
This is the layer that actually enforces "permitted schemas and data scope"; no
row limit or timeout can substitute for it. A direct read of the underlying
table must fail 42501.

(a) and (b) are orthogonal: (a) bounds *what you can change*, (b) bounds *what
you can see*. Both are required.

**(c) Fixed parameterized query set — policy, not a security boundary.** The
executor may only send named templates; user text reaches the database as bound
parameters, never as SQL. An unrecognized template is refused before a
connection is opened. This proves "the executor sent nothing else" — and no
more than that.

### 5.1 Engine assumption, stated explicitly

`plans/post321-real-sql.md:18` forbids assuming a vendor. The mechanism above
does assume PostgreSQL — `ALTER ROLE … SET default_transaction_read_only`,
`statement_timeout`, SQLSTATE 42501/25006/57014, and `role_table_grants` are
all PostgreSQL. This is a **conditional** assumption, and it is now a real
question rather than a default: an enterprise ledger could be PostgreSQL, SQL
Server, Oracle, or an ERP system's own store, and this brief does not know.

It is worth answering early because the answer is usually favourable — a
read-only role and a view layer exist in every mainstream engine, and the
failure codes map cleanly — but the concrete DDL in §2.2 would be re-derived if
the answer is not PostgreSQL. Fin declares `psycopg`
(`pyproject.toml:16-17`) and iknow declares `pg` (`package.json:61`), so a
PostgreSQL ledger needs no new dependency on either side.

### 5.2 Authorization travels with the call

**The scope is fixed by a trusted application identity and enforced by the
database. The model never chooses it and can never widen it.**

The chain, end to end:

1. fin knows who the operator is — the authenticated session, never model
   output.
2. fin derives an **authorization context** from that identity and puts it on the
   retrieve call as structured, non-model-writable data. The model's utterance
   cannot add to it, name a different scope, or supply a different identity; the
   cascade and the tool binding already ensure the 档 and this context come from
   the run, not from the model.
3. iknow **verifies where that context came from before believing it**, then
   maps it to a **database role**, opens the connection as that role, and runs
   the parameterized template. The scope is what the credential can reach, not a
   `WHERE` clause the application chose.
4. The database enforces it. A row the identity may not see is not returned, and
   a write attempt fails 42501 regardless of what the request said.

**Step 3 is a verification, not a conversion.** A structured context is
well-shaped data; it is not, by itself, proof of who sent it. A caller that can
put arbitrary JSON on the wire can put `{"scope": "finance"}` on the wire. So
iknow must establish the context's *provenance* — the authenticated channel and
whatever attests the caller — and refuse a context it cannot vouch for. The role
or department named in a request is never taken at face value; an unverifiable
context is a named failure, not a default.

**Why this shape.** A `WHERE` the application writes is policy: it holds only as
long as the code is correct. A grant is enforcement: it holds through a bug in
fin, through a mistaken template, and through anything the model emits. The
difference is the whole point of doing this in the database.

**First version: one explicit scope.** The initial release has a single
authorized range, and the documentation says exactly that. **It does not claim
department isolation**, because it does not have it. Anyone reading the docs
should be able to tell the difference between "this is the only scope" and "we
enforce this operator's scope", and only the first is true today.

**Settled by the A2 planning pass: in v1 the request carries no scope at all.**
This is the sharper form of "one explicit scope", and it is chosen because it
removes the trap rather than guarding against it. The paragraph above warns that
a structured context is well-shaped data and not proof of who sent it — a caller
that can put JSON on the wire can put `{"scope": "finance"}` on it. The v1 answer
is that **there is no scope field to forge**: the reader role comes from the
instance's own configuration, and the caller's authorization context can only
select among already-provisioned roles, never name one. A request that tries to
name a scope is not trusted, not mapped — it is a named failure.

Two things follow, and both are deliberate:

- `sql_authorization_unverified` is **retained but not reachable in v1.** It is
  the correct exit for the per-identity era, when a context must be checked before
  it is believed. Keeping it named now means the later change is a widening, not
  a rename.
- The v1 authorization story is entirely the database's: one role, one grant, one
  `WHERE` in the view. fin's role on the call reduces to putting the authenticated
  session identity on it as a non-model-writable field, and iknow's reduces to
  refusing a request that names a scope it was not configured with. Neither
  *chooses* a scope, and neither can widen one.

**What changes when a real source arrives.** Per-department isolation means the
role is chosen per identity (or row-level security is enabled), and the same
chain applies with a different role. Nothing in fin's side of the call changes —
that is the reason the context is structured data at the seam rather than
credentials fin holds.

## 6. Settled: limits

| Bound | Value | Anchored to |
|---|---|---|
| rows returned | 20 | `_LIMIT_CAP` (`src/fin/retrieve/mcp_adapter.py:159`) — no 档 ships a 21st row |
| rows fetched | 21 | *new* — fetch one past the cap so truncation is observable rather than silent; no existing precedent, by design |
| result bytes | 32 768 | ADR 0088:8 — "caps text at 8,000 code points and serialized output at 32 KiB"; `src/fin/retrieve/source_read.py:35`, `read_source.ts:54` |
| `statement_timeout` | 3 s | must be < the MCP per-call budget so the *server* answers first with a typed error. That budget is `DEFAULT_MCP_TIMEOUT_SECONDS` = 60 s (`src/fin/retrieve/mcp_session.py`), raised from 5 s: a measured live push takes ~11 s, so the old 5 s ceiling reported every real push as a transport timeout instead of a result. The ordering this row protects is unchanged (3 s < 60 s). |
| connect timeout | 2 s | `CONNECT_TIMEOUT_SECONDS` (`src/fin/host/health.py:63`) |
| `lock_timeout` | 1 s | a read blocked behind a writer must not eat the statement budget |

Net-new: no `statement_timeout` precedent exists in either repo today (grep
confirms zero hits). That is a deliberate addition, not a reuse.

## 7. Settled: failure taxonomy

Two new codes; the rest reuse machinery that already exists.

| Outcome | Code | Status |
|---|---|---|
| empty (document 档) | `corpus_not_ready` | already exists, and is **correct** for a document miss |
| **empty (sql 档)** | `sql_no_matching_rows` | **new** — see the settled decision below |
| invalid | `invalid_search_mode` / `empty_query` | already exist |
| unavailable | `retrieve_unavailable` | reuse verbatim — a DB that is down and an MCP server that is down are the same fact from the operator's seat |
| **denied** | `sql_query_denied` | **new** |
| **timeout** | `sql_query_timeout` | **new** |
| **untrusted context** | `sql_authorization_unverified` | **new** — the authorization context arrived without verifiable provenance (§5.2 step 3). Distinct from `denied`: `denied` means "we checked and the answer is no", this means "we could not check". **Not reachable in v1**, which carries no scope on the request at all (§5.2); retained and named for the per-identity era |

All four new codes are reachable only after the named-token union is widened —
see §7.1.

**Empty is already named, and the existing name is wrong here.** A retrieve that
returns zero chunks with zero mapping failures is resolved to
`corpus_not_ready` (`src/fin/host/named_failures.py:20-21`, produced via
`state.py:408-421`), whose operator copy is *"the bound 资料 collection has
nothing to cite yet — add documents (corpus ingest) and ask again"*. For a SQL
query that ran correctly against a live database and matched no row, that is
factually wrong advice: the corpus is irrelevant, and nothing needs ingesting.

A1 must therefore decide the **copy**, not the code. The reason code can stay
`corpus_not_ready` — a new code would be a parallel scheme for a state that
already has a name — but the run must not tell an operator to ingest documents
after a database miss. Either the copy branches on 档, or a dedicated code is
added. This is a genuine open decision, not a settled item.

**Settled by the A2 planning pass: a dedicated code, `sql_no_matching_rows`.**
The argument that decided it is the same one this section already uses for
`timeout`. A slow query on a healthy database is *"a different product fact with
a different fix (narrow the query)"*, so it may not reuse
`retrieve_unavailable`. An empty SQL window is equally a different product fact
from an empty corpus, with a different fix: widen the window, or check the
ledger — not ingest documents. Reusing one code for two facts with two different
remedies is the conflation this taxonomy exists to prevent, and branching only
the *copy* would leave the two indistinguishable to anything reading the code.

The copy for `sql_no_matching_rows` names the database, the window that was
queried, and that zero rows matched — it must not mention ingest, corpus, or
documents. It stays **distinct** from denied, timeout, unavailable, and
untrusted-context, and A2's acceptance asserts all five separately.

`timeout` must not reuse `retrieve_unavailable`: a slow query on a healthy
database is a different product fact with a different fix (narrow the query),
and A2's acceptance requires the classes to stay distinct.

`real_db_not_wired` is **kept** until A2 lands, and after A2 it should mean
*"not provisioned"* only — never *"refused."* Collapsing the two is the
empty-vs-error conflation the port forbids. It stays pinned by
`tests/retrieve/test_tiers_module.py:58-60`, which asserts the literal dict, so
any premature edit fails the suite loudly.

### 7.1 Settled: how a SQL failure reaches the wire

**The failure-token vocabulary is a one-member union today, and that is the
reason the merge order in §4 is a correctness requirement rather than a
coordination preference.** The union has a single member
(`GraphragFailureToken = typeof COMMUNITY_SUMMARY_INDEX_FAILURE`,
iknow `src/core/errors.ts:23`), and `failureTokenOf` is its only read path
(`:41`). A `GraphragError` raised without a token therefore produces **plain-text
`isError`**, not a structured `{"error": …}` field
(`src/index.ts:626-633`).

So `sql_query_denied`, `sql_query_timeout`, `sql_authorization_unverified` and
`sql_no_matching_rows` cannot reach fin as codes until that union is widened to
name them. This is not a detail to work around at the fin end: fin's adapter
classifies by reading the structured `error` field and, absent one, falls through
to a token scan of the message text and raises `retrieve_unavailable`
(`mcp_adapter.py:530-552`). A typed SQL denial that arrives as prose is
indistinguishable from a dead server, and the operator is told the wrong thing.

**The rule, therefore: no SQL failure may be raised as an untokened error.** Each
of the four codes joins the named-token union, and an error that cannot name one
is a defect rather than a fallback. Classification reads the token field, never a
substring of a human-readable message — the same rule the union's own comment
already states.

**A wider instance of the same defect, which the SQL work does not create.**
`invalid_search_mode` and `empty_query` are listed in §7 as *reused* codes, but
neither is a member of the token union today: they are produced on the routing
path as message prose and reach the host as plain text. So a malformed 档
request is already indistinguishable on the wire from a dead server, by exactly
the mechanism described above. Widening the union for the SQL codes does not
fix that, and the routing-path refusals were out of this delivery's scope —
but T7 will touch that path, and leaving it untokened there would contradict
this section's own rule. The honest statement: the union is now seven members,
and the routing refusals remain prose.

**What this changes about the merge order.** §4's gate is not only about a false
`retrieve_unavailable`; it is about this too. iknow must land the widened union
*and* the `sql` arm that uses it before fin removes its refusal, or fin sends a
`sql` request into a server that still answers with today's prose.

`sql` is refused at **three** independent seams — HTTP door
(`src/fin/host/workspace_http.py:3818-3823`), port (`port.py:147-151`), cascade
(`search_mode_decision.py:293-303`). All three must change in one commit. The
cascade seam is the subtle one: it returns `search_mode=None`, so if it is
missed the request silently downgrades to a `graph` answer — the exact silent
mapping the tier design forbids.

The archived `tier_not_open` is **not** reused. It means "档 never shipped"
(`named_failures.py:24-30`), which is the opposite of a runtime permission
refusal on a tier that *is* open. It in fact shares a wire channel with
`real_db_not_wired` — both are routing-time 400 `{"error": <code>}`
(`named_failures.py:63-66`) — so the distinction is meaning, not transport.

## 8. Settled: database results carry their own evidence type

**A database result is not a document passage, and this spec no longer forces it
to be one.** An earlier revision of this brief reused the `company` evidence kind
and made a SQL row render as a `field=value` line that the citation verifier
substring-matched. That was wrong on the merits and is removed.

The reason is concrete. The filing citation path is built around
`Locator{document_id, version, unit}` — a page of a frozen file — with a
32-code-point minimum span, a locator-shaped-claim regex, and a `display` of
`"page"`. Each of those encodes an assumption a business row does not satisfy: a
row has no page, no manifest digest, and a counterparty reference like
`ACME-2024Q1` is an ordinary ledger value that the current `_LOCATOR_REGEX`
(`citation_gate.py:95`) reads as a forged filing locator and refuses the whole
run over. That is not a detail to work around; it is evidence that the two shapes
are different things.

So database results get a **structured evidence shape of their own**:

```json
{
  "kind": "database-row",
  "source": "ledger_ro.contract_expiry",
  "record_key": {"contract_id": "C-2291"},
  "queried_at": "2026-03-01T09:14:22Z",
  "fields": {"counterparty": "Northwind Logistics", "expiry_date": "2026-03-14",
             "notice_days": 30, "notice_by": "2026-02-12"},
  "summary": "C-2291 Northwind Logistics, expires 2026-03-14, notice by 2026-02-12"
}
```

| Field | Why it is there |
|---|---|
| `source` | which permitted view answered; makes the read scope auditable from the citation alone |
| `record_key` | the row's own identity, so a claim traces to a record rather than to a position in a text |
| `queried_at` | the value is true *as of this query*; a ledger changes, and a citation without a time is a claim about a moving target |
| `fields` | the values actually returned this round, not what the model says they are |
| `summary` | one line for display; the model may quote it but cannot invent it |

### 8.0 Settled: the row is a sibling key on the envelope, not a chunk

**A ledger row does not travel in `chunks[]`.** It travels as a sibling key on
the same envelope, beside the keys the runtime already reserves for facts that
are not retrieved passages.

The alternative — widening `chunks[]` so a row can occupy a slot — is rejected on
the evidence. The envelope's chunk shape *requires* `source_ref` and
`valid_window` (iknow `types.ts:712-720`), and a row has neither: it is not a
slice of a frozen unit and has no validity interval. Making it fit would mean
fabricating a `source_ref` for a row that has no document — inventing the
identity the citation then claims. The same applies on the fin side, where a
chunk's `source_ref` is hard-typed to a document `Locator` (`port.py:39,166`)
and the evidence item's `locator` field is required with no default
(`citation_gate.py:139-140`).

So the wire gains one optional sibling key, and **the retrieve port's return type
widens by a named key rather than by loosening `Locator`.** A document chunk's
`source_ref` stays a `Locator`, which is the point: the type keeps its meaning
because a row never enters it.

This is not a new shape of idea. The retrieve contract already reserves sibling
keys for outcomes that are not passages — `community_summary_unready` is exactly
this, a typed outcome riding the same dict beside `chunks`
(`port.py:75`, produced at `mcp_adapter.py:968-972`). A database row joins that
family.

**Consequence for A2's blast radius:** `Chunk` and `Locator` are untouched, so
the two identity switches that are binary today stay binary until they gain a
third case, and the document retrieval path is unaffected by the row's arrival.

**The binding rule** is that an answer must be traceable to the result set *this
round produced*. A claim about a database value is verified by reference to the
`record_key` and the returned `fields` — not by substring matching. A number the
model states that no returned `fields` supports is unsupported, and that is a
different failure from a missing locator.

**Consequences to accept explicitly:**

- `QUOTABLE_EVIDENCE_KINDS` (`citation_gate.py:85`) becomes a three-member set
  with `database-row` added, and `verify_citations` gets a branch for it. The
  closed-set assertion at `tests/host/test_citation_gate.py:1020` is updated, not
  deleted, and existing `company` and `attachment` behaviour is untouched.
- The web client's `kind` union (`web/src/types.ts:16`) and the id-slot branches
  in `export.py:261` and `workspace_http.py:1757` need a third case.
- `display` is the source and record key, never `"page"`.
- **The 32-code-point span rule does not apply to this kind.** A `notice_days` of
  `30` is a legitimate citation, and forcing it to quote a 32-character window
  would be the "stronger than the product needs" failure. Document evidence and
  database evidence verify by different rules, and the asymmetry is deliberate.

**What this preserves.** ADR 0046 — *"a source marker without a matching span is
not a citation"* — holds in spirit: a `database-row` citation is valid only
against a `record_key` and `fields` this round actually returned. ADR 0021 (a
citation's identity comes from this run's retrieve result) holds exactly: the
`record_key` is that result, not a model's reconstruction.

### 8.1 Citation re-read is a snapshot, not a re-query

A `database-row` citation is opened by showing **the evidence this answer was
actually built from**. It never re-runs SQL, because re-running would show the
present and dress it up as the past.

- **The snapshot.** The bounded structured result returned this round, with its
  `queried_at`, is retained as the evidence for that answer.
- **Opening a citation** displays that snapshot. No query is issued.
- **A user-initiated refresh** is a *new* query: new rows, a new `queried_at`,
  a new evidence set. It produces a new answer and leaves the old one intact —
  refreshing never rewrites a prior answer.
- **No automatic diff, in this version.** The product does not claim what changed
  between two snapshots. Absent a fresh query, the answer may not state or imply
  that the data is unchanged: silence about drift is not a guarantee of
  stability.
- **Historical snapshots still require a current access check.** Showing a past
  snapshot is still disclosure. If the viewer's authorization no longer covers
  that record, the snapshot is not shown — the check runs against current
  permissions, not against the permissions held when the snapshot was taken.

This is what makes the citation answer "why did it say that then" without letting
today's ledger impersonate last month's evidence.

## 9. A3 verification shape

A new opt-in driver, `tests/retrieve/test_live_sql_business_db.py`, marked
`mcp`, following the existing `live_iknow_support.py` pattern: per-run unique
schema, seeded rows whose expected values are written in the fixture DDL rather
than read back from the database.

**Two legs, and they prove different things.**

*Leg 1 — the real ledger.* Against the operator's actual system of record, with
a dedicated read-only role: the seeded contract rows return through
`search_mode=sql` and match values a reviewer can read in the ledger's own UI.
Write refusal is **target-dependent, and the SQLSTATE names which layer
refused** — the flat "any write fails 42501" wording was corrected against the
A3a LIVE observation (fin `7cfcc10`, `tests/retrieve/test_ledger_readonly_boundary.py`
on postgres:16-alpine): an `INSERT`/`UPDATE` against the BASE table fails
42501 (the privilege layer, which is the read-only-role proof); an `UPDATE`
against the read VIEW fails 55000 — a two-table-join view is not
auto-updatable, so PostgreSQL refuses the write shape BEFORE any permission
check runs, and 55000 there must not be misread as a grant leak (the
distinction is recorded per target, never collapsed); a `CREATE TEMP TABLE`
fails 25006 (`default_transaction_read_only`). `SHOW
transaction_read_only` returns `on` read from inside the application connection
(so the `ALTER ROLE` is observed, not assumed); the role's `role_table_grants`
equals the expected list exactly; and — unchanged, and the probe all three
SQLSTATEs share — the row count is unchanged after every write attempt.
**This leg cannot run until a real ledger is identified** (§2.4);
until then it is blocked evidence, not a skip and not a pass.

*Leg 2 — the mechanism, against synthetic rows.* The same assertions against
fixture contracts seeded into an isolated per-run schema. This proves the read
path, the rendering, the citation mapping and the failure taxonomy without
touching the operator's data. It is what can be built and run today.

**One correction the A2 implementation pass forces on this section.** The
read-only layer is set in **two** places, and the difference decides what the
assertion proves. The `ALTER ROLE … default_transaction_read_only` in §2.2
applies to any session opened as that role, including one an operator opens by
hand — but it is a *provisioning* fact, and a run that never applied it shows
`off` for reasons that have nothing to do with the application. The application
additionally sets it as a **session** option on its own pool, so the guarantee
holds even on an instance whose role was provisioned incompletely.

So `SHOW transaction_read_only` read from inside the application connection
proves the **session** setting and does **not** by itself prove the `ALTER
ROLE` ran. Leg 2 must assert both: the session reading `on` (the application's
own guarantee) and the role's catalog setting (the provisioning guarantee). A
green session assertion alone would let a missing `ALTER ROLE` pass unnoticed,
which defeats the "observed, not assumed" requirement this section states.

**Also unstated until now: §6 bounds the response, not a single row.** `title`
and `total_amount` are free text with no length limit in the reference schema,
so a page of 20 rows can exceed the 32 KiB budget. A2 truncates from the end of
the page and, when not even one row fits, **refuses** with an invalid-query
class rather than returning zero rows — because zero rows would read as
`sql_no_matching_rows`, a different product fact that tells the operator to
widen the window when the real problem is an oversized row.

The distinction matters: leg 2 alone would let a green suite stand in for the
real boundary, which is exactly what `.claude/rules/test.md` forbids.

```bash
# Leg 2 — runnable once A2 lands
uv run pytest -q -m sql_live tests/retrieve/test_live_sql_business_db.py -v
# expects: exit 0. A named failure is blocked evidence, never a skip.

# Leg 1 — requires the real ledger and its read-only credential
FIN_BUSINESS_LEDGER_URL='postgresql://fin_sql_reader:…@…' \
  uv run pytest -q -m sql_live tests/retrieve/test_live_sql_ledger.py -v

# raw SQLSTATEs, without reading test output
psql "$FIN_BUSINESS_LEDGER_URL" -v ON_ERROR_STOP=0 \
  -c 'SHOW transaction_read_only'                       # on
  -c 'SELECT * FROM ledger_ro.contract_expiry LIMIT 1'   # rows
  -c 'SELECT * FROM ledger.contract LIMIT 1'            # expect 42501
  -c 'CREATE TEMP TABLE fin_probe (i int)'              # expect 25006
```

Evidence is recorded in `docs/validation/` following the existing template, and
**must** include a "what this record does NOT prove" section — at minimum, that
the query-template allowlist is complete, that a view's predicate is
semantically the intended scope, that leg 2 says nothing about the real ledger's
own access controls, and that the browser journey works (B3 owns
it, `plans/post321-real-sql.md:39`). A named failure is blocked evidence, never
a skip (`.claude/rules/test.md`).

## 10. Where this stands

**The contract is settled and A2 may start.** Ownership is iknow's independent
business-query module, with fin keeping the single `retrieve` entry point (§1.1,
§4). The scope is one parameterized contract-expiry query against PostgreSQL,
with no free-form SQL and no invoice/payment work (§1, §2.3). The reference
schema and its adaptation rule are in §2.1; the read-only and authorization
model in §2.2 and §5.2; the database-row evidence shape in §8; the limits in §6;
the failure taxonomy in §7.

**Buildable now:** the whole path against an isolated PostgreSQL and a versioned
synthetic ledger, plus the §9 leg-2 acceptance.

**Still blocked, deliberately:** production wiring and the §9 leg-1 run against
a real ledger. Two substitutions remain forbidden, and this brief came close to
both earlier: using fin's checkpoint database or iknow's corpus/index Postgres as
the business source, and treating the public market data loaded for
infrastructure proof as though it were the source. It is financial, public, and
unrelated to the operator's books — it proves the plumbing, not the product
question.

**Acceptance for the first delivery** — the §9 leg-2 run must show, against the
live database rather than a fake:

- the expiry query returns the seeded contracts, matching values a reviewer can
  read independently;
- a query outside the template set, and any write, are both refused — writes with
  42501, a temp table with 25006, and `SHOW transaction_read_only` read `on`
  from inside the application connection;
- a window with no matching contracts explains itself as an empty result rather
  than as a corpus that needs ingesting;
- every database claim in the answer traces to a `record_key` and `fields` from
  this round's result set.

**What a real source still needs to supply**, and cannot be decided here: which
ledger, who owns it, who grants read access, and whether it is scoped per
department — the last of which decides whether the single authorized range in
§5.2 becomes per-identity roles. The first delivery ships with one explicit
range and **does not claim department isolation**.

**What survives even if a real source is never found.** The failure taxonomy
(§7), the database-row evidence contract (§8), the limits (§6), the
authorization chain (§5.2) and the acceptance shape (§9) are all
source-independent. A tier that stays refused, but refuses with a correct
contract behind it, is a better foundation than one built on a schema nobody
agreed to.
