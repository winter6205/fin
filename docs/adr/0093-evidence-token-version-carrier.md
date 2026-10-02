# 0093. An evidence token names a shape; the version is an explicit `schema_version` field

Date: 2026-09-30
Status: accepted

## Context

`specs/enterprise-evidence-contract.md` §2.1 froze six evidence tokens that all
ended in a version suffix: `fin.evidence.database_row_snapshot.v1`,
`fin.evidence.retrieve_rows.v1`, `fin.evidence.citation.v1`,
`fin.evidence.visual_claim.v1`, `fin.evidence.business_field.v1`, and the
incumbent `fin.comparison.v1`. The suffix was the only place a version lived, and
that created three problems that are worth naming separately.

**The suffix has no separate version field to fail against.** Because the version
was embedded in the token string, a producer that emitted a newer shape could not
say "same shape, new version" or "new shape" as separate facts — the two were the
same character at the end of a string. Any check on version had to be a string
comparison against a frozen constant, and a token carrying a version nobody had
frozen was indistinguishable from a token naming a different surface entirely.

**The ingress boundary over-required `schema` per row.** The frozen implementation
required every row inside the `database_rows.rows[]` list to carry its own
`schema` key. That was not what the contract meant: at the MCP ingress boundary
the **envelope** is what names the data format, and a per-row `schema` both
duplicates that declaration on every element and permits a list whose elements
disagree about their own format. The implementation was stricter than the prose,
which is a defect in the implementation, not a reason to freeze the duplication.

**`queried_at` had a producer duty nobody owned.** The strict whole-second
timestamp pattern (§2.5) is a fin *consumer* rule, but truncating a database's
sub-second clock reading to whole seconds before it reaches the wire is a
decision only the producer can make. fin accepting the pattern and refusing
everything else is correct, but the spec did not say where the truncation
happens, and a reader could reasonably have concluded fin would repair a
fractional value — which §2.4 forbids outright, since rewriting the value would
make fin the party that decided what instant the ledger was read at.

**Implementing the version check surfaced four questions the decisions below
did not settle.** A token names a shape and a version numbers it, so once they
are separate fields they can fail separately and §6 has to say *which* class
each failure is. The §7 legacy grace is keyed on the absence of the `schema`
token, so a version sibling beside an absent token is a state the decisions
never described. The comparison export and the ingress row each have a shape
of their own that the token split touched without describing. All four are
settled in the decision list below.

**Two further questions surfaced on the operator's review of that first
answer, and both are settled below.** The first is the mirror image of the one
already asked: a *token* beside an *absent version* is the other state the
decisions never described, and the first answer resolved it by leniency. The
second is the boundary of the version rule itself — "is it an integer?" is
unanswerable as written once the value is a decoded JSON number, because a
number written `1.0` and a number written `1` are the same value by the time
any validator sees them. Decision 8 states the value rule and the
representation fact together; decision 12 supersedes decision 9's leniency half
and states the refusal with no repair.

## Decision

1. **The version suffix is dropped from all six frozen evidence tokens.** A token
   is now `fin.<surface>` with no suffix: `fin.evidence.database_row_snapshot`,
   `fin.evidence.retrieve_rows`, `fin.evidence.citation`,
   `fin.evidence.visual_claim`, `fin.evidence.business_field`, `fin.comparison`.
2. **The version moves to an explicit integer sibling field `schema_version`** on
   the same object, and every object that carries `schema` also carries
   `schema_version`. It is specified on exactly three carriers: the ingress
   `database_rows` envelope (with `schema`, `rows`, `result_count`), the egress
   `database_row` snapshot (with `schema` and its five identity/value keys), and
   the response / terminal SSE frame (with `evidence_schema`).
3. **A version number is reserved for genuine shape changes.** It is never a
   label for the step, the stage, or any transitional state of an implementation.
   A rollout status or a cutover marker has no representation in a token or a
   version number.
4. **At the MCP ingress boundary the envelope describes the data format.** A row
   in `rows[]` does not carry its own `schema`, and a validator must not require
   one. The existing rules are unchanged: a bare-array `database_rows` is still
   refused as `malformed_evidence_contract`, and the list member name `rows`
   stays frozen. The `schema`-bearing snapshot object is the *egress* object,
   which travels alone rather than inside a list.
5. **The SQL producer's executor truncates `queried_at` to whole seconds before
   the value reaches the wire.** This is the tracked change's duty, not fin's. **fin MUST NOT
   normalize, round, or coerce a producer's timestamp**; a fractional or offset
   value is refused as `malformed_evidence_contract` exactly as any other
   malformed value is, and §2.4's ban on silent coercion binds fin here. A row's
   `queried_at` is an *instant*, not a business calendar date, so the producer's
   separate date-column rendering rule (local calendar components rather than a
   UTC midnight instant) does not apply to it.
6. **An unrecognized token name and a recognized name with an unsupported
   `schema_version` are both refused** as `unsupported_evidence_contract`.
   Neither is repaired by a nearest-version match or a silent suffix strip.
7. **A `schema_version` that is not a FINITE INTEGER is
   `malformed_evidence_contract`, not `unsupported_evidence_contract`, and the
   rule is over the DECODED value.** §6 puts a wrong *type* or shape under
   `malformed` and reserves `unsupported` for a recognized token name carrying
   an unsupported version integer, so `1.5`, `NaN`, `Infinity`, a string, a
   boolean, `null`, a list, and an object are each refused as `malformed` while
   a well-formed integer that is not the current version is `unsupported`. The
   two failures are different in kind: a producer that ships a type bug and a
   peer running a different contract version both break the same read, and
   conflating them would make the first indistinguishable from the second. Only
   an integer can be a version, so only an integer can be a skew. A boolean is
   malformed precisely because a JSON `true` is not a number — the int-and-not-
   bool test is the whole rule, and `Number.isInteger` on the web side excludes
   it for the same reason.
8. **`1.0` and `1` are the same value at this boundary, and no validator may
   distinguish them.** A JSON number written `1.0` *decodes to the integer* `1`;
   the two spellings are the same number once decoded, and nothing on the wire
   survives to tell them apart. A requirement to "reject a version spelled
   `1.0`" is therefore **not implementable** on either side of this wire and is
   not part of the contract. This is stated normatively because the earlier
   draft of this decision was ambiguous in exactly the way that produces a web
   requirement no JavaScript decoder can execute. The rule is the value rule —
   a finite integer with no fractional part — and the two ends apply the same
   one predicate over the decoded value, each in its own language's terms:
   `Number.isInteger` in TypeScript, and in Python an `int` that is not a
   `bool`, **or a `float` that is finite and has no fractional part**. The
   Python float arm is load-bearing, not decorative: `json.loads` decodes the
   text `1.0` to the float `1.0`, whose VALUE satisfies this rule, so a bare
   type test (`isinstance(v, int) and not isinstance(v, bool)`) would refuse
   the very value this decision declares equal to `1` and would make the two
   ends answer differently about identical bytes. No test may assert that
   `1.0` is refused.
9. **A record with no `schema` field is still accepted as legacy v0 even when it
   carries a `schema_version`.** The version check applies only where a `schema`
   is present, because §7's grace is keyed on the **absence of the token** and
   adding a version does not make a record an E5 emission. The trade-off is
   recorded honestly: such a record is accepted rather than refused. This is
   deliberate leniency inherited from the pre-existing §7 behavior, and a
   producer must not rely on it — the version is dropped from the rebuilt
   object rather than kept as half a contract.
   **Superseded in part by decision 12** (operator ruling). What decision 9
   settled and what still stands is the *v0 half*: a record carrying **no
   `schema` field at all** is left exactly as it is and stays accepted, its
   version (present or not) not being judged. What no longer stands is the
   leniency that decision 9's sibling once gave the *web*: a **recognized token
   with an absent version** is no longer waved through as `known`. See decision
   12.
   **Annotation (implementation re-sync, decisions not renumbered).** The v0
   half above is now the *whole* of what survives, and the export no longer
   leans on the withdrawn leniency. `fin.host.export._schema_version_problem`
   had implemented decision 9's pre-ruling text — "ABSENT key — accepted",
   citing the empty-cell rendering — and so refused to bind only a
   present-but-wrong version. That left the export the single Python boundary
   disagreeing with `evidence_contract._check_schema_version` and with the web
   decoder on the same bytes, for the `version-absent-with-token` case of
   `data/conformance/evidence/schema_version_cases.json` (declared
   `malformed_evidence_contract`). The export now refuses that case as
   `malformed_evidence_contract`. **The v0 half was NOT touched by that
   change and is unchanged:** a record with no `schema` field at all is still
   accepted by the contract as v0. It simply never reaches this module's CSV —
   `export._bound_comparisons` binds only `c.get("schema") ==
   COMPARISON_SCHEMA`, so a schema-less record is dropped as another schema and a
   run carrying only those raises `unsupported_comparison_schema`.
10. **The export CSV records the declared version.** The comparison export's
    meta identity rows carry a `schema_version` row, so an exported table states
    the version it was emitted under; an absent version renders as an empty
    cell, exactly like the other absent identity values. An export that silently
    omitted the version would lose the ability to tell *absent* from *present*
    downstream — the same distinction the token-vs-version split exists to keep.
    **This is about the CSV's rendering, not its acceptance gate, and the two
    are separate decisions.** The export's EXPORT path *refuses* a
    present-but-wrong `schema_version` (an integer that is not the current
    version → `unsupported_evidence_contract`; a non-finite-integer →
    `malformed_evidence_contract`), exactly like every other boundary, and the
    empty cell is reserved for the *absent* case that reaches the CSV in the
    first place. A present version is never rendered as an empty cell, and an
    empty cell is never a place a wrong version is allowed to hide.
    **Annotation (decision 12 governs the gate; this decision is not renumbered
    or withdrawn).** The clause above presumes an absent version *reaches* the
    CSV. Decision 12 removed that case from this boundary: a recognized token
    with no version is `malformed_evidence_contract` and never reaches any
    boundary's acceptance, so the export now refuses it upstream in
    `_bound_comparisons` rather than rendering it. **What still stands here is
    the rendering half, not the gate half:** the `schema_version` identity row
    remains, an exported table still states the version it was emitted under,
    and an absent identity value still renders as an empty cell — which is what
    "exactly like the other absent identity values" covers (`concept`, `axis`,
    `unit`, `scale`). The cost is recorded plainly: **no comparison envelope
    can now produce an empty `schema_version` cell**, so that particular
    rendering has no reachable path and no test pins one.
11. **The ingress row can no longer express a row-level token.** Because the
    envelope describes the data format, an ingress row is held to exactly
    `{source, record_key, queried_at, fields, summary}`, and a row-level
    `schema` key is refused as an unknown key **regardless of its value** — a
    correct token is no more tolerated there than a wrong one. The consequence
    is honest and follows from decision 4: `fin.evidence.database_row_snapshot`
    is now carried only by EGRESS snapshots, and the ingress lane can exercise
    the envelope's token and version but not a row-level one. One existing
    conformance case whose intent was "a bad snapshot version at the ingress
    row" became inexpressible under this rule and was re-pointed at the
    envelope's `schema_version`.
12. **A record or frame that carries a `schema` token but omits
    `schema_version` is `malformed_evidence_contract`, and nothing back-fills
    it.** This supersedes the earlier decision that recorded the web's leniency
    for exactly that case (a recognized token with an absent version staying
    `known`) as a settled, deliberate trade-off; that leniency is withdrawn and
    **the server and the web are tightened together**, so the two ends of the
    wire never disagree about the same bytes. The reasoning: a token and its
    version are minted together, so a record that names the shape and omits the
    number is a producer type bug on both sides' reading. Three further
    constraints make the tightening exact rather than merely stricter:
    - **No back-fill, anywhere.** No component may infer, default, or supply
      the missing number — not the store, not admission, not the server-side
      encoder, not the web decoder. A component that writes the version it
      thought was meant has invented a contract fact, which §2.4 forbids.
    - **The §7 legacy rule is not amended to permit a back-fill.** The grace is
      keyed on the **absence of the `schema` field**; decision 9's v0 half is
      untouched by this decision and remains the one-directional rule it has
      always been. Stored records with no `schema` field at all are left
      exactly as they are and stay accepted as v0 — unchanged, not tightened,
      because the shape §7 protects predates `schema_version` entirely.
    - **An ACTIVE frame may not borrow the legacy rule to skip version
      validation.** A fresh emission is not a stored legacy record: the token
      is present, so the full §2.1 pairing applies, and no "it looks like an
      old record" argument survives the presence of `schema`.
    - **This ruling binds every boundary, and it binds them together.** "No
      back-fill, anywhere" names the store, admission, the server-side encoder
      and the web decoder; the comparison **export** is a boundary in exactly
      that sense and is included. It was the last boundary still implementing
      the pre-ruling leniency, and it has been re-synced: a recognized
      `fin.comparison` token with an absent `schema_version` is refused as
      `malformed_evidence_contract` by `export._schema_version_problem`, on the
      CSV path and the JSON path alike. All three consumers of
      `data/conformance/evidence/schema_version_cases.json` now return that
      case's declared class, so the export's documented exception in that
      file's `export_scope_note` no longer describes the implementation. The
      validator, the export, and the web are three independent implementations
      and the shared declaration is written from the rule rather than read off
      any of them, so agreement is evidence rather than a shared assumption.

## Considered Options

- **Keep the suffix and add `schema_version` anyway.** Rejected: two places
  holding one fact, free to disagree. The suffix would have to be validated
  against the integer, and a mismatch would be a new refusal class with no
  product meaning.
- **Keep the suffix only, no new field.** Rejected: this is the status quo. The
  version remains unreachable to a validator that wants to compare a number
  rather than a string, and "which version is this" is still answered by parsing
  a token.
- **Put `schema` on every row and keep it there.** Rejected: it is the strictness
  the frozen implementation already over-applies. Per-row duplication cannot
  express anything the envelope does not fix, and it lets a list disagree with
  itself.
- **Let fin truncate a fractional `queried_at` to whole seconds.** Rejected: it
  is silent coercion, and the stored value would no longer be the instant the
  database reported. A mismatch against the database's own record would become
  undetectable at exactly the point where it matters.
- **Class a non-finite-integer `schema_version` as
  `unsupported_evidence_contract`.** Rejected: it is the one class that names a
  *contract* mismatch, and `"1"` is not a version skew — it is a producer's type
  bug. Filing a type bug under the code that operators read as "your peer runs
  a different version" sends the fix to the wrong place.
- **Refuse a record that carries `schema_version` without `schema`.** Rejected,
  and this remains the standing v0 half of decision 9: it is stricter than §7
  was before the version field existed, the grace is keyed on the token, and
  the shape the §7 grace protects predates `schema_version` entirely.
- **Require the web to refuse a version spelled `1.0`.** Rejected as
  unimplementable, not merely redundant (decision 8): a JSON number written
  `1.0` *decodes to the integer* `1`, so the spelling is gone before any
  validator sees it. A contract that names an unimplementable requirement is
  worse than one that names a weaker real one, because the unimplementable one
  gets "implemented" as a `String()` check on the raw bytes at some boundary
  that does not have the raw bytes.
- **Refuse a record that carries a `schema` token with no `schema_version`,
  and let a lenient component supply the version for it.** Rejected on both
  halves (decision 12): the refusal is right, the leniency is not. A
  back-filling component is a component that decides what version a producer
  meant, and the two ends of the wire would then disagree about the same bytes
  in opposite directions. The legacy rule is left exactly as it was — keyed on
  the absence of `schema` — rather than amended to carve out an exception.
- **Keep the CSV's identity section to the token alone.** Rejected: the CSV is
  a downstream artifact, and the token alone cannot say which version produced
  the table. An empty cell for an absent version keeps the two states readable.
  The empty cell is a *rendering* of an absent value, never a rendering of a
  wrong one: the export path refuses a present-but-wrong version (decision 10).
- **Let the ingress row keep a `schema` when the value is correct.** Rejected:
  tolerance by value is not a contract rule, and the same key would then be an
  unknown key in one payload and a declaration in the next.

## Consequences

- **Applied:** the token is a stable name and the version is a checkable
  integer. An unrecognized token name and an unsupported version are now two
  distinct facts a validator can state separately, and a registry lookup does not
  have to parse a suffix. The ingress contract matches the implementation's
  intent — one envelope-level `schema`, rows with value keys only.
- **Trade-offs:** this is a wire change, and it is a breaking one. **Pre-rename
  stored records that carry a suffixed token are refused, not silently accepted**
  — they fall under the §7 legacy rule keyed on the absence of `schema`, and a
  record that *does* carry a `schema` with an old spelling is
  `unsupported_evidence_contract`. Likewise **any producer still emitting a
  suffixed token is refused** rather than accommodated: `fin.comparison.v1` and
  friends are now unrecognized token names, exactly like a typo would be. That
  is the intended posture — the contract names a shape, and a shape that is not
  the one in force is not interpreted under a guess. The corresponding schema
  files and conformance fixtures are renamed off `.v1.` to match.
- **Applied (settled during implementation):** the refusal classes now
  distinguish a producer's type bug from a genuine version skew; the export
  carries the version it was emitted under; and the ingress row's key set is
  exactly the five value keys, so the envelope is the one place the ingress
  format is declared.
- **Trade-offs (settled during implementation):** the ingress lane can no
  longer be used to exercise a row-level token, so
  `fin.evidence.database_row_snapshot` is an E5 *egress* token only. A
  re-pointed conformance case is evidence, not a silent narrowing: its intent
  (a bad version at this boundary) is still covered, now at the envelope.
- **Revised (decision 12; the earlier record superseded):** the version
  rule is now stated as a **finite integer over the decoded value**, and the
  `1.0`/`1` fact is normative (decision 8) rather than left to each
  implementation's reading of the JSON. A `schema` token with an **absent**
  version is `malformed_evidence_contract` on the server **and** in the web
  decode, and no component back-fills it. The cost is honest and is paid on
  purpose: a producer that shipped half a contract now fails loudly at both
  ends instead of being repaired at one, and the web can no longer be handed a
  requirement it cannot execute. The leniency that survives is exactly one
  half — a record with **no `schema` field at all** is still accepted as v0
  with its version unjudged — and it survives because §7's protected shape
  predates `schema_version`, not because any validator stays permissive.

## Verification

Recorded honestly, and separated into what was **run** and what was **blocked
or not run**. A skip is not a pass: under `.claude/rules/test.md` a required
acceptance that did not run is *blocked evidence*, and it is recorded as such
rather than absorbed into a green line. An earlier draft of this section
claimed a clean `pyright` + Python + web pass without running them; that
withdrawal has since been resolved by actually running each of those tools
below. A later draft of this section reported the live SQL link as blocked;
that claim was **itself wrong** and is replaced below by the two passing live
runs, stated with the same standard the withdrawal applied: every pass in this
section corresponds to a run whose output was observed.

**On skipped and deselected figures.** The opt-in marker-owned classes
(`mcp`, `sql_live`, `jev_live`, `memory_on`, `soft_track`, `eval`) total **398
deselected** cases in the default selection (`uv run pytest -q --collect-only`
→ `5299/5697 tests collected (398 deselected)`), and the default selection
reports **115 skipped** cases, all of them the documented hermetic-default
gates (no `FIN_CHECKPOINT_URL` / `FIN_INGEST_STATE_DSN` set; Postgres-backed
durability and live vision round-trip legs). Neither number is a pass, and
neither is a coverage claim for this change. The lanes those markers own are
addressed individually below — the `sql_live` and `mcp` lanes **were run and
pass**, the visual-claim live lane is **blocked-not-passed** — so no
deselected or skipped class is being counted as evidence.

**A skip is not automatically this change's blocked evidence.** The skips
were classified by which surface they touch, and only three of the skipping
files bear on the `schema` / `schema_version` change:
`test_visual_claims_wire.py` (1), `test_workspace_http.py` (1), and
`test_thread_store.py` (9). Those eleven were treated as required-for-this-
round-and-not-run until the development configuration was loaded, at which
point they became measured results: with `.fin/pre330/environment.sh` sourced
the three files give `181 passed, 0 skipped`. The remaining skips belong to
attachment-store, memory-store, memory-buffer and ingest-durability files
whose dependencies (object store, durable Postgres) are unrelated to this
contract, so they are optional default-suite tests, not blocked evidence for
this change. Counting every skip against this change would overstate what is
missing.

### Run and passing

- **The whole default Python suite — run to completion, on the final tree.**
  `uv run pytest -q` → `5188 passed, 115 skipped, 398 deselected, 8 warnings
  in 912.43s (0:15:12)`, exit 0. (Earlier runs of this branch reported
  `5184` and then `5187`; both predate this figure — the `+1` is the
  `test_an_absent_identity_value_renders_an_empty_cell` byte pin added when
  the export refusal propagated.) The 115 skips are all environmental
  gates of the hermetic default selection (`FIN_CHECKPOINT_URL` /
  `FIN_INGEST_STATE_DSN` unset, one `FIN_VISION_LIVE_ROUNDTRIP=1` gate); none
  is a skip of this change's contract surface.
- **The changed surfaces, with the development environment loaded — run,
  zero skips.** `set -a && . ./.fin/pre330/environment.sh && set +a &&
  uv run --no-sync pytest -q tests/host/test_visual_claims_wire.py
  tests/host/test_workspace_http.py tests/host/test_thread_store.py -rs` →
  `181 passed in 20.61s`, exit 0, **no skips**.
  This needed the container stack actually running, and the distinction
  matters: `tests/conftest.py` `_drop_unreachable_checkpoint_url` unsets
  `FIN_CHECKPOINT_URL` whenever the checkpoint Postgres cannot be reached,
  so with the stack down the same command still reported `170 passed,
  11 skipped` and the nine `PostgresThreadStore` cases never ran. Those nine
  are now measured (`test_thread_store.py` alone: `41 passed, 0 skipped`).
  The `FIN_CHECKPOINT_URL` skips in the default selection are therefore
  configuration the suite detects and neutralizes, not a gap in this
  change — but only once the database is reachable, and this section
  claims the zero-skip figure only for the run where it was.
- **The live SQL lane (`sql_live` marker) — run and passing.**
  `set -a && . ./.fin/pre330/environment.sh && set +a && uv run --no-sync
  pytest -q -m sql_live tests/retrieve/test_live_sql_business_db.py` →
  `6 passed in 8.23s`, exit 0 (the same command also observed at
  `6 passed in 7.67s`, exit 0, in a second environment session). This is the
  scratch `fin-sql-business-db` container, not one of the shared
  `fin-pre330-*` services.
- **The live search-mode lane (`mcp` marker) — run and passing.**
  `set -a && . ./.fin/pre330/environment.sh && set +a && FIN_LIVE_IKNOW_ROOT=/home/winner/projects/iknow-memory
  uv run --no-sync pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py`
  → `11 passed in 4.57s`, exit 0 (also observed at `11 passed in 6.94s`,
  exit 0, in a second session). This exercises the six-container live stack
  (`fin-pre330-es`, `fin-pre330-neo4j`, `fin-pre330-iknow-pg`,
  `fin-pre330-ledger`, `fin-pre330-s3`, `fin-pre330-pg`), all observed
  `Up` under `docker ps`.
- **`pyright` — run and passing.** `uv run --no-sync pyright` →
  `0 errors, 0 warnings, 0 informations`, exit 0.
- **The web suite — run and passing.** `npx vitest run` in `web/` →
  `Test Files  32 passed (32)`, `Tests  639 passed (639)`, exit 0, and
  `npx tsc --noEmit` → exit 0. This is the whole web suite, not a subset;
  it includes `web/src/evidenceContract.test.ts` covering
  `schemaVersionRefusal` against decisions 7, 8 and 12. (The count moved
  from 638 because the two-door split below added its own cases.)
- **The two ends answer alike on the token-less half-contract — run by
  probe, not by a test.** A snapshot carrying `schema_version` and NO
  `schema` is the state decision 8 was written about, and the 18-case shared
  set cannot reach it because every case rides a recognized token. Probed
  directly on both ends:

  | | result |
  | --- | --- |
  | server `_validate_snapshot` | `malformed_evidence_contract :: database_row snapshot: missing ['schema']` |
  | web `decodeDatabaseRowSnapshot` (emission) | `{"status":"refusal","error":"malformed_evidence_contract"}` |

  Both refuse the identical bytes. The web reached that by splitting the
  decoder's two contracts explicitly: `SnapshotReadMode` distinguishes
  `"emission"` (an active frame; the §2.1 pairing is enforced in full) from
  `"stored-replay"` (a stored v0 record, which §7 leaves exactly as it is).
  Spec §7.5 draws the cut — the grace applies to stored records, not to
  fresh producers — and ADR decision 12's third constraint makes it
  explicit for an active frame. Two tests in
  `web/src/evidenceContract.test.ts` drive the SAME literal through both
  doors so the split is visible rather than looking like a bug, and the
  §7 replay grace is still asserted rather than deleted.
- **The shared 18-case `schema_version` set agrees across the three
  readers — run.** `uv run --no-sync pytest -q -k "schema_version or
  evidence_contract_shared or shared_schema"
  tests/host/test_evidence_contract.py tests/host/test_export.py` →
  `18 passed, 229 deselected`, exit 0.

### Correction of a wrong blocked claim

- **The earlier "live SQL link was attempted and BLOCKED" claim was
  incorrect and is withdrawn, replaced by the passing run above.** The
  failure that draft saw (`Failed: admin psql failed: Error response from
  daemon: container 31b8b825... is not running`) named
  `fin-sql-business-db` — verified by `docker inspect` that the ID resolves
  to that container — a **purpose-built scratch PostgreSQL this project
  created for the `sql_live` lane, not the shared PostgreSQL / ES / Neo4j
  stack**. That scratch container had exited cleanly (`Exited (0)`,
  `RestartPolicy=no`, finished 2026-09-30) and restarting it is normal
  operation of the lane, not out of scope. The cause of the misdiagnosis was
  running the lane without sourcing `.fin/pre330/environment.sh`, which
  supplies `FIN_SQL_DB_CONTAINER` and `FIN_SQL_READER_URL`; the six
  `fin-pre330-*` containers backing the other lanes were up throughout and
  are up now. The earlier draft reported honestly what it saw; the error was
  generalizing one stopped scratch container into a claim about the whole
  stack.

### Required for this change and NOT run — blocked, not passed

- **The visual-claim live lane
  (`tests/host/test_live_graph_visual_lane.py`, `-m mcp`) is blocked, not
  passed.** Run as `set -a && . ./.fin/pre330/environment.sh && set +a &&
  uv run --no-sync pytest -q -m mcp tests/host/test_live_graph_visual_lane.py -rs`
  → `9 skipped in 0.08s`, exit 0, every skip with the reason: real
  prerequisites are not configured: "the provider credential (see
  FIN_LIVE_VISION_ENV_FILE); this module is the real-boundary evidence and
  cannot run against an in-memory stand-in". The module's own prerequisite
  check (`tests/host/test_live_graph_visual_lane.py` `_missing_prerequisites`,
  lines 133-155) accepts `FIN_OBJECT_STORE_ENDPOINT` as an alternative to
  `FIN_OBJECT_STORE_URL`; the environment supplies the endpoint and
  `FIN_OBJECT_STORE_BUCKET`, so the **only** missing prerequisite is the
  vision provider credential `FIN_LIVE_VISION_ENV_FILE`. Under
  `.claude/rules/test.md` a missing live prerequisite for a required
  acceptance is blocked evidence, so the visual lane's live legs are **not
  claimed here in any form** — not as a pass, not as a skip, and not as
  "covered by the default suite". The `visual_claim` element's `schema` /
  `schema_version` pair is therefore proven **only** by the hermetic contract
  tests and the conformance fixtures in this change; a live visual emission
  remains unproven against a real provider and a real object store.

### Not run at all, and reported as such

- **`ruff` could not be executed** in the implementation environment
  (re-confirmed: `uv run --no-sync ruff --version` →
  `error: Failed to spawn: ruff`, cause `Permission denied (os error 13)`),
  so the project's lint gate is **UNVERIFIED** for this change. This is a
  tooling gap, stated rather than worked around; it is not a contract claim
  and no conclusion in this ADR rests on it.
- **The whole-suite figure above is the HERMETIC selection.** With
  `.fin/pre330/environment.sh` sourced, the same command reports
  `5286 passed, 1 skipped, 16 errors`, exit 1. All 16 are in
  `tests/corpus/test_ingest_state_store_durable.py` and
  `tests/corpus/test_ingest_worker_durable.py`, and all fail on
  `psycopg.errors.UndefinedTable: relation "fin_ingest_audit" does not
  exist` from the `_wipe` fixture's bare `DELETE FROM fin_ingest_audit`
  (`tests/corpus/test_ingest_state_store_durable.py:93`). The table is
  created by `PostgresIngestStateStore`'s own
  `CREATE TABLE IF NOT EXISTS` on construction
  (`src/fin/corpus/ingest_state_postgres.py:63,122`), so a database that
  has never had that store constructed lacks it, and `_wipe` assumes it is
  there. **This is pre-existing and outside this change**: the file is
  byte-identical at base commit `8321145` and appears nowhere in this
  round's 138-path diff; the two test files are likewise untouched. Under
  the hermetic default selection the same files give `8 passed, 10 skipped`,
  exit 0. No fix is made here — repairing another subsystem's fixture
  assumption is a separate change with its own evidence, and the durable
  ingest contract is not what this ADR governs.

### Correction of a wrong known-red claim

- **The earlier "two known-red failures" line is withdrawn — both tests now
  pass on this tree, observed directly.**
  `uv run --no-sync pytest -q tests/host/test_evidence_contract.py
  tests/host/test_export.py tests/host/test_compare_facts.py
  tests/host/test_workspace_http.py tests/retrieve/test_sql_wire.py` →
  `462 passed, 1 skipped in 20.95s`, exit 0 (the skip is
  `test_workspace_http.py:2054`: `FIN_CHECKPOINT_URL not configured;
  PostgresThreadStore is not wired in this environment`). In particular:
  - `test_case_inventory_matches_manifest_totals` asserts
    `(17, 71, 88)` (`tests/host/test_evidence_contract.py:196`), which
    matches the manifests counted from
    `data/conformance/evidence/*/manifest.json` (accepts 3+3+4+5+2 = 17,
    refuses 15+14+21+12+9 = 71, total 88; the separate `http_sse` manifest
    holds 1 wire example, not a payload case). The earlier draft's
    `(17, 72, 89)` vs 88 framing no longer describes the tree.
  - `test_citations_wire_projection_refuses_a_frame_with_an_unknown_schema_token`
    passes.
