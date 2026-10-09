# Spec: enterprise evidence contract (E1 contract freeze)

> **Status:** E1 decision record. This file freezes the
> shared evidence representation, versioning policy, per-profile payload rules,
> bounds, producer/consumer fixture handoff, and legacy policy. E2–E6 implement
> against it; nothing here claims a producer or validator already conforms.
> **Refs:** `plans/post321-evidence-schema-route.md`,
> `specs/search-mode-business-sql-contract.md` (database-row constraints in
> §§6–8), and ADR 0021, 0046, 0088, 0089, 0090, 0091, 0093.
> **Ownership:** fin owns this contract, its validators, and its fixtures.
> `iknow-memory` owns the SQL producer and must conform to this consumer
> contract; fin does not implement that producer or duplicate source mapping.

## 1. Kind vocabulary (decision)

The discriminator is the existing `kind` string. No parallel vocabulary is
invented; renaming `company` / `attachment` would force a rewrite of every row
already stored in `fin_run.citations`, which is a migration nothing in Session
E can do (the existing constraint). Frozen closed sets — a kind
outside the set for its channel is an `unsupported_evidence_contract` refusal,
never a default (ADR 0091 consequence):

| Channel | `kind` values | Notes |
|---|---|---|
| `citations[]` (quote channel) | `company`, `attachment`, `database-row` | exactly `QUOTABLE_EVIDENCE_KINDS` as shipped |
| `visual_claims[]` | `visual_observation` | its own typed lane (ADR 0089); never enters `citations[]` |
| `business_fields[]` | `business_field` | **reserved lane**, §4.5; no producer and no validator exists yet |
| `comparison` frame | (no `kind` lane; envelope `schema` discriminates) | financial-fact profile, §4.3 — not a citation kind |

Hyphen spelling `database-row` and underscore spelling `visual_observation` /
`business_field` are kept per channel because each already exists or follows
its channel's incumbent style; consistency across channels is not worth a
rename of shipped strings.

## 2. Versioning and envelope policy (decision)

1. **Every serialized contract object carries an explicit `schema` field
   naming its shape.** The token is `fin.<surface>` with **no version suffix**;
   the version travels as an explicit integer sibling field `schema_version` on
   the same object, and **every object that carries `schema` also carries
   `schema_version`.** The token is the stable name of the shape; the
   `schema_version` integer is its version. A version number is minted ONLY
   where the serialized shape can actually change — it is never a label for
   the step, the stage, or any transitional state of an implementation. A
   rollout status, a cutover marker, or a "this is the version that added
   field X" note must never be encoded in a token or a version number;
   stage-specific state has no representation in this contract at all
   (ADR 0093). **The pairing is atomic at every boundary: an object that
   names a `schema` and omits `schema_version` is
   `malformed_evidence_contract`, and NO component may infer, default, or
   back-fill the missing number** (ADR 0093 decisions 8 and 12). A shape name
   without its version is half a contract, and half a contract is exactly the
   ambiguity this split exists to remove. The §7 legacy rule keys on the
   **absence of the `schema` field** and grants no exemption here — an ACTIVE
   frame that carries a token may not borrow the legacy rule to skip version
   validation, and the legacy rule is NOT amended to permit a back-fill. The
   field's location is otherwise derived from where `schema`
   already sits; the three carriers where it is pinned here are the ingress
   `database_rows` envelope (§4.4), the egress `database_row` snapshot
   (§4.4), and the response / terminal SSE frame beside `evidence_schema`
   (§2.2). Frozen tokens, each with its `schema_version`:
   - `fin.evidence.database_row_snapshot` — the `database_row` object;
   - `fin.evidence.retrieve_rows` — the retrieve-ingress sibling key
     `database_rows` envelope (§4.4, producer side);
   - `fin.evidence.citation` — declared once per response / terminal SSE frame
     as `evidence_schema` (§2.2);
   - `fin.evidence.visual_claim` — each `visual_claims[]` element;
   - `fin.evidence.business_field` — each `business_fields[]` element;
   - `fin.comparison` — the comparison envelope (§4.3), which shipped with a
     `.v<N>` suffix and loses it here like the rest.
2. **The `citations[]` element itself is versioned by additive sibling keys,
   not by a per-row `schema` field.** Its eight keys are frozen
   (`kind, document_id, attachment_id, version, unit, display, filename,
   quote`) plus the one optional sibling `database_row` (present only for
   `kind: "database-row"`). A future incompatible citation shape lands as a
   *new* sibling key with its own `schema`; existing keys never change meaning.
   The containing response / terminal SSE frame declares the channel once via
   `evidence_schema: "fin.evidence.citation"`, with its `schema_version`
   sibling alongside it (E5 emits it). This is how the byte-equality promise of
   already-stored rows and explicit versioning both hold.
3. **Unknown token or version → named refusal.** Both an unrecognized token
   name and a recognized name carrying an unsupported `schema_version` are
   `unsupported_evidence_contract`. Never the nearest older parser, never a
   default, never a silent suffix strip.
   **The version rule is a FINITE INTEGER VALUE, and it is stated over the
   DECODED value — not over the JSON text.** A present `schema_version` must
   decode to a finite integer. `1.5`, `NaN`, `Infinity`, a string, a boolean,
   `null`, a list, and an object are each `malformed_evidence_contract`: they
   are a producer's type bug, not a version skew, and §6 keeps the two classes
   apart. A well-formed integer that is not the current version is
   `unsupported_evidence_contract`. The two failures differ in kind — a type
   bug and a peer running a different contract version break the same read, and
   conflating them would send the first fix to the wrong place. Only an integer
   can be a version, so only an integer can be a skew.
   **`1.0` and `1` are the SAME value at this boundary and are not
   distinguishable.** In a JSON document a number written `1.0` decodes to the
   integer `1`; nothing survives the decode to tell the two spellings apart, so
   no validator — on either side of the wire — may refuse a version spelled
   `1.0`. A requirement to "reject `1.0`" is therefore not implementable here
   and is not part of this contract. What the rule excludes is any value that
   still carries a fractional part or is not finite (`1.5`, `2.0.5`,
   `NaN`, `Infinity`), plus every non-numeric JSON type (string, boolean,
   `null`, array, object). The reference implementation is a single
   finite-integer predicate over the decoded value, applied identically on both
   sides of the wire, because the two ends decode the same bytes and MUST NOT
   disagree about them. Each language expresses that one rule in its own terms:
   `Number.isInteger` in TypeScript, and in Python an `int` that is not a
   `bool`, **or a `float` that is finite and carries no fractional part** —
   Python's `json.loads` decodes the JSON text `1.0` to the float `1.0`, whose
   value satisfies the integer condition, so a type test alone
   (`isinstance(v, int) and not isinstance(v, bool)`) would refuse the very
   value this rule declares equal to `1`, and the two ends would then disagree
   about identical bytes. Booleans are excluded on both sides because a JSON
   `true` is not a number.
4. **Unknown field → named refusal.** At every boundary, an object of a known
   version carrying a key outside its frozen set is `malformed_evidence_contract`.
   No silent coercion, invented defaults, empty-result substitution, kind
   coercion, or dropped fields anywhere in this contract.
   (Reconcile note: the current fin wire *drops* malformed visual-claim
   rows with a warning. That posture is superseded at E5 cutover for new turns;
   stored legacy rows fall under §7.)
5. **Strict timestamps.** Every contract timestamp (`queried_at` and any
   successor) matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` on a proleptic
   Gregorian calendar with `01-12` / `00-59` ranges and no leap second (`:60`
   rejected). Anything else — offsets, fractional seconds, spaces, wrong
   digits — is `malformed_evidence_contract`. (The looser date token in
   `citation_gate`'s claim matcher is answer-text matching, not contract
   parsing, and stays scoped to that.)
   **Producer obligation (whole seconds, at the producer).** The SQL producer's
   executor MUST truncate `queried_at` to whole seconds *before the value
   reaches the wire*; the sub-second remainder is the producer's to drop, not
   fin's. This is the SQL producer's responsibility, not fin's behavior. **fin MUST NOT normalize,
   round, or coerce a producer's timestamp** — fin accepts exactly the
   §2.5 pattern or refuses with `malformed_evidence_contract`, and a fractional
   or offset value is refused exactly like any other malformed one. §2.4's ban
   on silent coercion binds fin here: rewriting a producer's value to fit the
   pattern would make fin the party that decided what instant the ledger was
   read at, and a mismatch between the stored `queried_at` and the database's
   own record would become undetectable. **A row's `queried_at` is an instant,
   not a business calendar date**, so the producer's separate date-column
   rendering rule — a `date` column cited as its local calendar components
   rather than as a UTC midnight instant (the `toISOString()` east-of-UTC
   regression recorded in `plans/post322-a2-sql-slice.md`) — does not apply
   to it.
6. **Strict hashes.** New digest-valued fields use `sha256:<64 lowercase hex>`.
   The incumbent bare-hex `page_image_sha256` keeps its un-prefixed lowercase
   64-hex form — renaming a shipped field is churn with no gain. Any other
   shape is `malformed_evidence_contract`.
7. **Canonical schema representation.** JSON Schema (draft 2020-12) files, one
   per frozen token, live in `specs/schemas/evidence/` and are named after the
   token without a version suffix (`<token>.schema.json`, e.g.
   `database_row_snapshot.schema.json`; the `schema_version` integer is inside
   the file, not in its name) — fin-owned SSOT; created by E3, this section
   names the home. Numeric values in these contracts are text-normalized before
   any comparison or digest (§4.4), so no schema relies on cross-language float
   serialization.

## 3. Bounds (decision)

All bounds are E1-chosen; exceeding one at any boundary is a named overflow
refusal, never truncation by the consumer (the producer-side page truncation
rule of SQL contract §6/§9 stays with the SQL producer).

| Contract | Bound |
|---|---|
| citations per run | 32 |
| quote (document kinds) | min 32 code points (ADR 0046 floor), max 2,000 code points |
| `database_row` snapshot serialized | 32 KiB (SQL contract §6, unchanged) |
| rows per query result | 20 (SQL contract §6, unchanged) |
| `record_key` pairs / key / value-text | ≤ 8 / ≤ 64 chars / ≤ 128 chars |
| `fields` entries / key / value-text | ≤ 16 / ≤ 64 chars / ≤ 512 chars |
| `summary` | ≤ 1,000 code points |
| `source` | ≤ 128 chars, names the permitted view only |
| visual claims per run | 32; `detail` ≤ 4,000 code points |
| visual `scope` | `null` (whole page) or exactly 4 normalized floats in `[0,1]` with x0<x1, y0<y1 |
| business fields per run / per subject | 32 / 24; value + normalized-text ≤ 512 chars each |
| timestamps | §2.5 pattern, strict |
| digests | §2.6 pattern, strict |

Overflow is reported by its lane's code (§6): `citation_overflow`,
`visual_claim_overflow`, `database_row_overflow`, `business_field_overflow`;
the comparison frame keeps the shipped tool exit `comparison_overflow` (§4.3
rules unchanged, admission recorded in §6 — the SQL producer's grid bound is the tool's
own). Precedence inside `database_row`: the 32 KiB serialized-snapshot bound is
evaluated on the snapshot as a whole, before the per-entry bounds; a snapshot
breaking both reports the byte bound. The per-entry bounds cap a field-legal
snapshot near 12 KiB, so the byte bound has no independently reachable input
today — it stays a deliberately redundant transport guard, enforced as a
separate check and reachable on its own if a per-entry bound is later raised.

## 4. Per-profile payload rules

### 4.1 Document citation (`company`, `attachment`)

- Locator identity stays exactly the incumbent one (`docs/CONTEXT.md`:
  `{document_id, version, unit}` / `{kind:"attachment", attachment_id,
  version, unit}`). `display` ∈ `page | unit`.
- Verifier: current text-span rule (contiguous span, ≥32 code points, present
  in text this run received) — scoped to these two kinds and unchanged
  (ADR 0091).
- Semantic verification, shape validation, and identity/trust stay separate:
  an attachment additionally re-passes the active-thread trust check; a well-
  shaped untrusted identity is still a named denial — `citation_trust_denied`
  (§6), never a silent eligibility drop (§2.4).

### 4.2 Attachment / visual observation (`visual_observation`)

- Lane frozen: sibling channel `visual_claims`, never `citations[]`; the key
  set stays `VISUAL_CLAIM_FIELDS` (the 14 keys shipped in
  `src/fin/host/visual_evidence.py`), each required, projected in that fixed
  order; the lane gains the one `schema` field and its `schema_version` sibling
  at versioning cutover (E5).
- `page_image_sha256` follows §2.6; coordinates follow §3. The observation
  must belong to the attachment version and run that the row names — coverage
  and reuse semantics stay ADR 0089's, unmodified. The active-thread trust
  check applies to this lane as to §4.1: a well-shaped untrusted attachment
  identity is `visual_claim_trust_denied` (§6), never a silent drop.

### 4.3 Financial-fact validation profile (`comparison` envelope)

- Not a citation kind and not merged into any evidence lane. The existing
  `fin.comparison` envelope is its versioned contract of record; no new
  envelope is minted. Binding rules are unchanged: concept,
  context/dimensions, period, numeric value, unit, tied to the filing/document
  and exact source version; validated by the existing XBRL numeric gate; a
  page-based presentation validates against that page, an HTML filing uses its
  deterministic source unit and never an invented page number. Restricted to
  financially relevant tasks with a matching source; an amount alone does not
  select it (operator ruling). A frame naming a source version this run did
  not receive is the named refusal `comparison_source_unverified` (§6) —
  shape-valid ≠ authorized here too.

### 4.4 Database row snapshot (`database-row`)

Conforms to the already-frozen facts; none of them is re-decided:

- **Ingress:** the retrieve response gains one optional sibling key,
  `database_rows` — a `fin.evidence.retrieve_rows` object carrying `schema`,
  `schema_version`, `result_count`, and the snapshot list under the frozen
  member name `rows` (full path `database_rows.rows`). A producer shape where
  `database_rows` names the bare array itself (iknow `65f2f9f`, pre-envelope)
  is non-conforming input until wrapped in the envelope object — the envelope
  is frozen (§2.1, §4.4) and a bare array fails it as
  `malformed_evidence_contract` (`database-rows-envelope-wrong-type`); E1
  admits no second list-member name. A row never travels in
  `chunks[]` (SQL contract §8.0): a row has no `source_ref` and no `valid_window`, and
  fabricating either would invent the identity the citation then claims.
- **`schema` placement at ingress (envelope describes the data format, the row
  does not re-declare it).** At the MCP ingress boundary the `schema` /
  `schema_version` pair lives on the **envelope**: the envelope is what names
  the format of the list it carries. **A row inside `rows[]` does NOT carry
  its own `schema` key, and a validator MUST NOT require one.** The row's
  format is fixed by the envelope that carries it; a per-row `schema` would
  duplicate that declaration on every element, permit a list whose rows
  disagree about their own format, and add no information the envelope does
  not already fix. The row keys are exactly §4.4's five value keys
  (`source, record_key, queried_at, fields, summary`) at ingress; a row
  carrying `schema` or any other key outside that set is
  `malformed_evidence_contract` under §2.4 like any other unknown key. The
  `schema`-bearing snapshot object is the **egress** object (§4.4 egress,
  §2.1 rule 1), where the object travels alone rather than inside a list.
- **Egress (shipped fin wire shape):** each `database-row` citation carries
  the one snapshot under sibling key `database_row` with
  `{source, record_key, queried_at, fields, summary}`. The discriminator is
  the citation `kind`; the snapshot object itself carries no `kind` (see §8,
  reconcile note R1). Flat slots mirror snapshot data for incumbent consumers
  (`version == queried_at`, `quote == summary`); the nested snapshot is SSOT
  and a mirror mismatch is `malformed_evidence_contract`.
- **`schema` placement:** the egress snapshot object gains
  `schema: "fin.evidence.database_row_snapshot"` alongside its
  `schema_version` at E5 cutover, beside the five identity/value keys of §4.4
  egress; snapshots minted before the E5 cutover are legacy v0 under §7.
- **Value text rule:** `record_key` and `fields` values are JSON scalars
  (string / number / boolean / null) at ingress; the consumer renders each to
  text: strings as-is, numbers as shortest round-trip decimal, booleans as
  lowercase `true`/`false`, `null` as `""`. (fin's current
  `citation_gate._plain_text` implementation uses Python `str()` for
  numbers — correct — but renders booleans `True`/`False`; E3 aligns that to
  the canonical lowercase. See §8, R5.) This is a declared normalization rule,
  not silent coercion: the text form is the contract's canonical value
  representation for comparison, ordering, and the digest. Keys are sorted by
  name; a mapping is never nested.
- **Stored-snapshot identity (decision; ADR 0092):**
  `snapshot_digest = "sha256:" + hex(sha256(canonical_json(m)))` where `m` is
  the object `{source, record_key, queried_at, fields, summary}` with every
  value first rendered to its text form above, and `canonical_json` is UTF-8
  JSON with object keys sorted lexicographically at every level, no
  insignificant whitespace (`","` / `":"` separators), `ensure_ascii=False`.
  The digest is minted by the fin consumer at admission and is fully
  recomputable from the stored snapshot: any carrier (future display, replay
  check) must equal a recomputation, and a mismatch is
  `malformed_evidence_contract`. The producer never sends a digest.
- **Placeholders, frozen and named:** a `database-row` citation carries
  `document_id == ""`, `attachment_id == ""`, `unit == 1`, `display ==
  "record"`. `unit = 1` is a documented placeholder because the incumbent row
  type requires an integer; no consumer may derive meaning from it. This is
  recorded here precisely so it is never mistaken for an invented default.
- **Zero rows:** `sql_no_matching_rows`, specified by the SQL producer (SQL contract §7). It is a
  run-level named refusal with its own copy (names the database and window,
  never ingest/corpus); it produces no evidence item; it is distinct from
  malformed, denied, timeout, untrusted-context, and unavailable.
- **Refresh and history:** opening a citation displays the stored snapshot and
  issues no query; an explicit refresh is a new query minting a **new**
  evidence item (new `queried_at`, new digest) and never rewrites the prior
  answer; reading historical evidence re-checks **current** authorization
  (SQL contract §8.1) — a denial there is the permission-denial class, code owned by
  SQL producer's source-side check. A well-formed identity/context proves neither
  trust nor authorization.
- **Verifiers stay separate:** shape (this spec), claim-vs-`fields`
  verification (ADR 0091, `unsupported_database_claim`), and
  identity/authorization (SQL producer's database enforcement) are three checks; schema
  validity never grants access.

### 4.5 Business field observation (`business_field`) — bounded, reserved

- New lane `business_fields[]` beside `visual_claims`; never inside
  `citations[]`. Element (frozen at `fin.evidence.business_field`):
  `schema, schema_version, kind, subject_ref, source_locator, original_value,
  normalized_text, status`.
- `subject_ref` names an existing iknow entity/relationship record — no
  per-field graph node, no second graph identity (operator ruling; graph
  identity stays iknow's). A `subject_ref` well-formed but not readable by
  this caller is `business_field_trust_denied` (§6) — shape-valid ≠
  authorized applies to this lane too. `source_locator` is an *already-frozen*
  locator of this spec: a document locator, an attachment locator, or
  `(source, record_key, snapshot_digest)` for a database row. No new locator
  shape is invented.
- `status` closed set: `extracted | missing | ambiguous | conflicting |
  unverified`; conflicting sources stay visible — a `conflicting` element
  carries each disagreeing observation as its own row against the same
  `subject_ref`. `original_value` is preserved verbatim as text;
  `normalized_text` follows §4.4's text rule.
- Deferred with owner: task-specific field sets (operator — only when a real
  use case is selected); credit-field extraction, scoring, approval (out of
  Session E entirely). E1 freezes only the lane and element, because a lane
  with no producer must still have a name nobody later renames.

## 5. Producer / consumer conformance-fixture handoff (decision)

- **Canonical schemas:** `specs/schemas/evidence/*.schema.json`, one file per
  §2.1 token, fin-owned. SQL producer consumes the files directly; it must not
  maintain an independently defined copy.
- **Fixtures:** `data/conformance/evidence/<profile>/<case>.json` with
  profiles `citation`, `visual_claim`, `database_row`, `business_field`,
  `comparison`, and a directory manifest
  `data/conformance/evidence/<profile>/manifest.json` listing cases. The
  directory is not named for a version; a version is the fixture payload's
  `schema_version`, per §2.1.
- Each fixture file is one JSON object:
  `{"case": "<id>", "direction": "producer|consumer", "expect":
  "accept|refuse", "refusal_code": "<code or null>", "payload": {...}}`.
  `direction: "producer"` fixtures describe what a conforming SQL producer emission
  looks like (its producer tests assert it can emit exactly these payloads);
  `direction: "consumer"` fixtures drive fin's E3 decoder tests.
- Fixtures are generated against the schema files; a fixture and its schema
  disagreeing is a fin defect, fixed in fin before the SQL producer consumes.
- These fixtures prove fin's consumer contract only. Nothing here or in the
  fixtures claims the SQL producer exists or conforms.

## 6. Typed outcomes and boundary map (decision)

One vocabulary; a class never collapses into another and none becomes an
empty success:

| Class | Code(s) | Notes |
|---|---|---|
| malformed input | `malformed_evidence_contract` | wrong type/shape/timestamp/hash, unknown field, mirror mismatch |
| unsupported contract | `unsupported_evidence_contract` | unrecognized `schema` token name, unsupported `schema_version` for a recognized name, unknown `kind` for its channel |
| bound overflow | `citation_overflow`, `visual_claim_overflow`, `database_row_overflow`, `business_field_overflow`; comparison keeps the shipped tool exit `comparison_overflow` | one code per lane for every §3 bound that lane carries; refused, never truncated; `database_row` byte-bound precedence per §3; `comparison_overflow` keeps its shipped name (§4.3 rules unchanged — admitted, not re-minted) |
| missing data | `corpus_not_ready` (document), `sql_no_matching_rows` (database, SQL producer), visual exits (`attachment_unavailable`, `observation_stale`, …), `insufficient_evidence` (E2 route-level, business_field status `missing`) | distinct per source; empty ≠ malformed ≠ absent source |
| permission denial | `sql_query_denied`; `citation_trust_denied`, `visual_claim_trust_denied`, `business_field_trust_denied` for the document / visual-claim / business-field lanes; historical-snapshot open denial (code owned by SQL producer's source-side check) | shape-valid ≠ authorized at every lane (§4.4 principle): a well-formed but untrusted identity/subject is a named per-item refusal, never a silent eligibility drop (§2.4); current-authorization check on replay |
| unverified context | `comparison_source_unverified` | a comparison frame names the exact source version (§4.3) this run did not receive, so the binding cannot be established — "could not verify", kept distinct from denial's "checked and the answer is no" (the split recorded in SQL contract §7) |
| capability unavailable | `retrieve_unavailable`, `tier_not_open`, `visual_lane_unwired`, `real_db_not_wired` (= "not provisioned" only, never "refused") | |
| infrastructure failure | transport: `retrieve_unavailable`; persistence: `persistence_unavailable` (named at E4 — the store itself cannot be read or written; distinct code, same posture as the transport exit; the per-record §7 compatibility refusal is NOT this class, it rides `unsupported_evidence_contract`); serialization failure: named contract error, never an empty `done` | |

**Delegated verifier exits (no parallel code).** The document text-span
verifier lane (§4.1, rule unchanged per ADR 0091) is answered by the citation
gate's own typed refuse exits: a locator version that resolves to no known
document version is `unknown_locator`; a quote that never span-matches the
text this run received within the repair budget ends at `citation_repair_cap`
(ADR 0008 — the gate produces exactly these two refuses). These names are
gate-owned, not §6 contract codes; conformance fixtures for this lane carry
the gate exit as the expected named refusal, so the outcome is answerable
without a second vocabulary.

Mapped to the four fin boundaries (E3–E6 implement; codes above are the
contract):

| Boundary | Applies |
|---|---|
| MCP/retrieve ingress | all six classes; trust denial is ingress-time and source-side (SQL producer), shape/semantic are fin's separate checks |
| persistence write + replay | all six; plus §7 legacy compatibility failure as its own named exit under "unsupported contract" |
| HTTP + SSE | all six; serialization exception → named error frame policy, never empty `done`; oversize frame → overflow refusal |
| web runtime decode | malformed, unsupported contract, overflow, missing (displayed per class); TypeScript types are compile-time only and never satisfy decode |

### 6.1 Input-class applicability matrix (decision)

The plan's five test classes, per public fin boundary. Every applicable cell
gets a contract test at that boundary in the owning E-step; `N/A` means the
class is not artificially applicable there, not that it is untested.

| Class | MCP/retrieve ingress (E3) | persistence write + replay (E4) | HTTP + SSE (E5) | web decode (E6) |
|---|---|---|---|---|
| empty | applies: zero chunks → `corpus_not_ready`; zero rows → `sql_no_matching_rows`; empty `database_rows` list; distinct from malformed (§6) | applies: empty history, empty evidence arrays keep their meaning | applies: empty history / empty `citations` array ≠ failure | applies: empty arrays render as defined-empty, not failure |
| negative | applies: absent/wrong-typed fields, unknown `schema`/`kind`, malformed timestamps/hashes, wrong-version locators, mirror mismatch, well-formed but untrusted identity | applies: corrupt/unsupported stored record → §7 named compatibility refusal, never silent drop | applies: malformed request/row → named contract failure | applies: same set as ingress — decode refuses, never coerces |
| overflow | applies: >20 rows, >32 KiB snapshot, >32 citations/claims, per-field bounds (§3) → named overflow refusal | applies: oversize evidence rejected at write; oversize stored row at replay refused by name | applies: oversize frame refused, not truncated | applies: over-limit payload shown as named failure, not partial render |
| concurrent | N/A to pure decoding; admission state (this-round result sets) is run-scoped and inherits existing run semantics | applies: competing refresh/read and write/replay on one thread/run follow existing single-active-run rule (`409` / `ConcurrentRunError`) | applies: concurrent send/stream ordering follows existing run semantics | applies: stream/history reconciliation ordering — the one genuinely stateful decoder path |
| exception | applies: producer/transport error → `retrieve_unavailable`, kept distinct from every contract failure | applies: persistence exception → named persistence failure, not empty history | applies: transport/serialization exception never becomes an empty `done` | applies: fetch/decode exception surfaces its class; no empty-success fallback |

The SQL request/execute classes (parameter validation, database errors,
timeouts at the source) are producer-side matrix owned by the SQL producer and are not fin's to
test here.

## 7. Saved-turn legacy compatibility (decision)

Records stored before the §2 cutover (every citation row in `fin_run` today;
snapshots minted before E5 cutover; visual rows before E5) are
**legacy v0**.

**What makes a record v0 (not the token string).** Legacy status is decided by
the **absence of a `schema` field** and by the record's **key shape** — never
by a version number or a token spelling. Since §2.1 tokens no longer carry a
suffix, there is no token string left to key v0 on, and none is needed: a
record either declares `schema` (current) or it does not (v0). A stored record
whose keys match the frozen v0 shapes below and which carries no `schema` is
v0; a record that carries `schema` is subject to §2.1/§2.3, whatever its
version integer reads.

**What makes a record NOT v0 — the converse is now strict (ADR 0093
decisions 8, 12).** The grace is one-directional. A record that **does** carry
a `schema` field and **omits** `schema_version` is **not** v0 and is not
accepted under this section: it is `malformed_evidence_contract` (§6), at every
boundary that reads it — stored replay, ingress, egress, response / terminal
SSE frame, and the web decode. **No component may infer, default, or
back-fill the missing version** — not the store, not the admission path, not
the server-side encoder, not the web decoder, and not a validator that is
"being lenient". There is no back-fill permission anywhere in this contract,
and §2.4's ban on silent coercion and invented defaults binds here: a
component that writes the number it thought was meant is a component that
invented a contract fact. Stored records that carry no `schema` field at all
are **left exactly as they are** and stay accepted as v0 even when they carry
a `schema_version` sibling; that half is unchanged and is deliberately not
tightened, because the shape §7 protects predates `schema_version` entirely.
An **ACTIVE** frame — a fresh emission, not a stored legacy record — may not
borrow the legacy rule to skip version validation: the token is present, so the
§2.1 pairing is enforced in full.

1. A legacy record is accepted on replay only if it matches its frozen legacy
   shape (v0 snapshot: the five keys of §4.4; visual claim: the fourteen keys
   of `VISUAL_CLAIM_FIELDS`). The citation's v0 is a CLOSED GENERATIVE shape,
   not one key set: stored rows span three real writer generations (`page`
   since the T3 era, `attachment_id`/`filename` since the thread-attach era,
   the `page`→`unit` rename plus `display` at #321 T5 — no migration shipped,
   so all three live in the JSONB today). A citation row is v0-valid iff its
   keys are a subset of {`kind, document_id, attachment_id, version, unit,
   display, filename, quote, page`}, it carries `document_id`, `version`,
   `quote`, and EXACTLY ONE of `unit` or `page` (both is a contradiction,
   neither is corruption — either is a refusal), and it carries NO v1 lane
   marker (`database_row` or any sibling key). The v0 defaults are then frozen
   facts of that shape, not open tolerance: absent `kind` reads as the
   document kind because every v0 row predates the kind vocabulary — this is
   legal ONLY while no lane marker is present, which is what keeps it from
   being the row→citation coercion §4.4 forbids; absent
   `attachment_id`/`filename` read as `""`; a `page` reads as `unit` (the
   rename is an identity map, not an inference); absent `display` reads as
   `page`. Any key outside that nine-name set is a refusal — v0 is a closed
   shape, not "anything without `schema`".
2. Legacy rows are never rewritten, back-filled, or migrated in place. No
   adapter promotes v0 into v1 fields by inference.
3. A refused legacy record yields `unsupported_evidence_contract` (§6's
   unsupported-contract class at the persistence boundary) — visible,
   per-record, typed; the rest of the history still serves. It is
   never silently dropped or replaced with empty evidence.
4. Historical database snapshots keep their original values on replay;
   display still requires the current-authorization check (§4.4).
5. After cutover, any new emission without its required `schema` — or without
   the `schema_version` that §2.1 requires wherever `schema` appears — is
   `malformed_evidence_contract` — the grace applies to stored records, not
   to fresh producers.

## 8. Reconcile notes — current wire shape versus SQL contract (flagged, not silently picked)

- **R1 — `kind` placement.** SQL contract §8's example shows a standalone object with
  `"kind": "database-row"` inside the row payload; fin's current wire puts the
  discriminator on the citation envelope and ships a snapshot with no `kind`.
  Ruling here: envelope discriminates, snapshot carries no `kind` (matches
  shipped egress). The §8 example should be corrected when the SQL producer next edits
  that file — this spec does not edit it.
- **R2 — `fields` value types.** SQL contract §8's example carries a JSON number
  (`"notice_days": 30`); the current frontend types
  `fields`/`record_key` as `Record<string, string>`. Under §4.4's text rule
  both render identically at the gate, but the web decoder must accept JSON
  scalars or the producer must pre-stringify — one of the two will violate the
  other otherwise. Ruling here: ingress scalars are legal (§4.4); E6 must
  widen the web decode typing to scalars-plus-text or producer-side
  stringification must be agreed. Flagged for the SQL producer; not resolved by
  fiat from fin.
  **Resolved at E6 planning on the fin side only:** E6 widens the web
  decoder to scalars-plus-text (superset acceptance, §4.4 render rule);
  this requires no producer change and does not pre-stringify anything the wire
  does not already require.
- **R3 — sibling-key level.** SQL contract §8.0 places the optional sibling key on
  the retrieve envelope; fin's current wire carries a sibling key per citation
  (`database_row`). Both are "sibling, not `chunks[]`" as required; §4.4
  freezes ingress (`database_rows`) and egress (`database_row`) names
  separately so the two are one contract, not two accidents.
- **R4 — warning-drop posture.** the current fin wire drops malformed visual claims
  with a log warning; §2.4 requires named refusal. §7 scopes
  the grace to stored legacy records; live decoding after E5 must not drop.
- **R5 — bool text form.** fin's current `_plain_text` implementation renders Python booleans
  via `str()` as `True`/`False`; the canonical text rule (§4.4) is lowercase
  `true`/`false`, matching JSON and the TypeScript side. Ruling here: the
  contract is lowercase; E3 aligns the gate. Flagged because the digest and
  claim-matching both read the text form, so the two spellings must not ship
  on both sides of a persisted snapshot.
- **R6 — no zero-row or limits contradiction.** §6 limits (20 rows / 32 KiB /
  3 s) and `sql_no_matching_rows` are adopted verbatim from SQL producer; E1 does not
  re-decide them.

## 9. Deferred with named owners

| Item | Owner |
|---|---|
| SQL producer conformance to §4.4/§5 (ingress `database_rows` envelope — wrap the bare array under the frozen `rows` member, carry `result_count`, snapshot emission) | SQL producer |
| SQL request/execute input-class tests (producer side) | SQL producer |
| historical-snapshot open-denial code name | SQL producer source-side check |
| web decode widening for R2 | E6 — decided: fin-side decoder widening, no SQL producer change required |
| ~~`evidence_schema` + its `schema_version` on responses/frames, `schema` + `schema_version` on live snapshot/visual emissions~~ — **DELIVERED (E5)**: shipped on the response / terminal SSE frame, the egress `database_row` snapshot, the `visual_claims[]` element, the `business_fields[]` element, and the `comparison` envelope, with the strict no-back-fill rule in force on both the server and the web decode (ADR 0093 decisions 8, 12) | delivered — no owner outstanding |
| validator implementations + fixture generation | E3 |
| expense-policy routing slice | E2 |
| web snapshot-digest posture (E6 review M-11): the web NEVER recomputes the hex digest — the wire carries none (ADR 0092 mints it server-side at admission), and a cross-language recompute could diverge on number text (`30.0` vs `30`); the web's 32 KiB byte bound uses the same canonical `m` form and is pinned byte-for-byte against `digest_worksheet.json` `canonical_json` by `evidenceContract.test.ts` | decided: no browser digest unless a real display/verify need appears AND a cross-language number-text rule lands first — none exists today |
| `thread_store.py` file length (1,200+ lines: two backends + §7 replay projection + durable-write guard) | follow-up standalone refactor, named in the module header; Session E keeps contract diffs localized by design |
| task-specific credit fields, scoring, approval, production ledger binding | out of Session E; operator decision when a real source/use case exists |
