# Spec: structured corpus evidence

Status: **Review-ready contract, 2026-10-03; implementation pending.** Source
versioning and finite retention were confirmed by the operator. Remaining wire
choices below were delegated to the author and independently reviewed. This
contract does not claim that the current evidence-kind validator accepts it.

## Objective

Keep structured results attributable to immutable source versions while letting
current knowledge change and obsolete storage expire. fin retains bounded answer
snapshots; iknow owns sources, extraction records, supports, and finite evidence
references. A historical citation shows its original evidence or an explicit
unavailable state. It never silently resolves to a replacement source.

The [query contract](structured-corpus-query.md) owns result identity and exact
count semantics. The [maintenance contract](structured-corpus-maintenance.md)
owns activation, retirement, collection, and concurrency. This contract owns
source identity, support identity, snapshots, and retention obligations.

## Assumption gate

1. **Operator-confirmed:** immutable source versions, independent assertion
   supports, and mutable published-source pointers; immutability is not a
   requirement to retain everything forever or make every database table append-only.
2. **Operator-confirmed:** retired unreferenced evidence becomes eligible after
   90 days; finite historical references protect evidence for at most 365 days
   after retirement. Re-reading cannot renew a reference indefinitely.
3. **Delegated choice:** a separate `structured-result` citation kind and typed
   snapshot prevent collisions with the legacy database-row contract.
4. **Inherited:** trusted authorization and source ownership stay behind the
   retrieve seam; current access checks and explicit deletion override retention.
5. **Implementation prerequisite:** a public real-source acceptance pack and
   independently reviewed expected locators must be frozen before implementation
   acceptance. Examples in this document are illustrative.

## Boundaries

- **Does:** source/version/location identity; multi-origin supports; bounded
  evidence references; a new citation kind; retained row/count snapshots;
  type-preserving digest; historical reads; finite retention and deletion overrides.
- **Confirms with human:** source/version separation and retention policy are
  confirmed. The operator delegated the concrete wire and digest choices for
  review; no additional architecture decision is left pending here.
- **Out of this spec:** query planning and assertion equivalence; job execution
  and physical collector scheduling; existing-corpus migration; automatic
  adjudication; full historical query membership; backup/media erasure procedures.

## Inherits / Changes

Exact text from `docs/CONTEXT.md`, **retrieve port**:

> The only retrieve dependency fin is allowed to have: `query` (+ optional filters or a typed structured request) in, authorized corpus results with source attribution out — MCP-shaped, transport-agnostic. Its adapter also carries host-internal evidence reads and invalidation acknowledgements over the same connection, without another model-facing search tool.

Exact sentence from `docs/CONTEXT.md`, **locator**:

> A typed source identity for citation and `read_source`: company sources use `{document_id, version, unit}` and thread attachments use `{kind: "attachment", attachment_id, version, unit}`.

- ADRs 0090 and 0094 preserve corpus ownership and original versioned provenance.
  fin does not become a second source repository.
- ADRs 0021 and 0046 preserve current-turn evidence and document quote checks.
  A structured value can support a structured claim; it cannot fabricate a quote.
- ADR 0091's legacy `database-row` shape remains unchanged. Its returned-field
  verification principle applies through a separate kind here.
- ADR 0092's legacy textified digest remains unchanged; the new typed snapshot
  has a separate, type-preserving canonical encoding specified below.
- ADR 0093 applies to all new envelope and snapshot tokens: stable token names,
  explicit `schema_version`, and decoded finite-integer validation. `1` and
  `1.0` mean the same version; booleans, strings, null, and non-finite numbers
  are malformed. Unsupported integer versions are unsupported, not malformed.
- The new source lifecycle decision is recorded separately from the three
  contracts; terminology and persistence pointers appear at the end.

### Existing gaps

Read-only exploration of iknow found mutable current source metadata and
single-origin triple links. Duplicate triples can update confidence without
preserving additional origins; chunk deletion can detach an assertion link.
Existing records cannot establish immutable multi-origin provenance by
inference. A first delivery must create the required records from retained
original inputs; it must report unavailable historical origins honestly.

## Source and support interface

### Source version

`source_ref` identifies one logical company source. `source_version` is an opaque
immutable version key scoped to that logical source. A version binds original
content digest, retained original object reference, parser revision, frozen-unit
manifest, and the explicit mapping to fin locators. Source bytes alone are not
enough to interpret changed parsing; a changed unit manifest requires a distinct
source version even if raw bytes match. Changing only extraction model or rules
creates an extraction revision, not a source-content revision.

Identical content in two logical sources remains two origins. Reuse of a version
key is allowed only within the same logical source and with identical immutable
content and manifest. A physically purged version key is never reused. Reimport
creates a new version identity. Repeated activation of retained content still
increments the maintenance generation, preventing A-to-B-to-A races.

The mutable published pointer selects `(source_version, extraction_revision)`
as serving evidence. Uploaded/desired input is separate from published input;
only successful publication changes the latter. Re-extraction of unchanged
source content atomically replaces its accepted occurrence set, rather than
leaving every prior extraction active. Lifecycle changes do not rewrite content.

### Support

Every entity and assertion must have at least one current authorized support.
Each retained support binds a result ID to one source occurrence:

```json
{
  "support_id": "00000000-0000-4000-8000-000000000101",
  "source_ref": "example-source",
  "source_version": "opaque-source-version",
  "location": {"document_id": "example-document", "version": "frozen-unit-version", "unit": 1},
  "span": {"start": 0, "end": 42},
  "extraction_ref": "opaque-extraction-revision"
}
```

`location` obeys the existing company-locator validator. The ingest producer
must bind it to the specified source version; the two version strings need not
match. Missing or unresolvable binding fails as `unsupported_source_binding`.
Thread attachments do not enter this permanent company-corpus contract.

`span` is optional. When present it is a validated non-empty half-open Unicode
code-point interval within that frozen unit; it is not a byte offset. An absent
span permits structured evidence, never a quotation. A requested quote must be
read through the existing source-read path and satisfy ADR 0046.

Occurrence identity is `(result subject, result ID, source_ref, source_version,
canonical location, span-or-absent)`. Repeated extraction of that occurrence
attaches another extraction record to the same support; it does not increase
query count. Distinct source versions, locations, or spans remain distinct.
The returned `extraction_ref` selects the published extraction membership for
this support; all prior extraction links remain internally attributable. It is
not a field whose changing display rewrites the support identity.
Published support identity is immutable; activation and retirement are separate
lifecycle records. A support is active only if the published extraction revision
includes that occurrence. A newer extraction can retain its identity or retire
it by omitting it; an old extraction link alone cannot keep it current. Query
rows show only current authorized supports; retained snapshots preserve the
support set received at their query time.

A model must never receive fields or display names derived solely from an
unauthorized support. Projection and display values must be grounded in the
same authorized source scope as their returned row.

## Bounded references and historical reads

Each returned row and each count receipt has an opaque `evidence_ref` and
`evidence_expires_at`. iknow creates a finite retained reference atomically with
the query's authorized read snapshot; it cannot issue a reference after a purge
candidate has stopped accepting new obligations. The reference binds exact
returned value, query context, maintenance summary, and underlying dependencies.
A count reference preserves the receipt, not all matching entity/assertion IDs.

There are two retention grant types. A **row grant** protects its returned value
and exact source/support/extraction payload dependencies, including selected
entity display supports for relation endpoints. A **count grant** protects only
its bounded receipt and a backend-private shared authorization receipt
`(scope_binding, authorization_revision, purge_revision)`. It pins no entity
membership, source-content payload, or full source-version vector. The backend
must maintain these revisions for the trusted admission scope: any permission
change or explicit purge affecting that scope advances the corresponding
revision. Replay requires both revisions still match; conservative refusal
after a broader scope change is permitted. This applies even to zero counts.
Ordinary source publication or old-version GC changes data revision but does
not advance these authorization/purge revisions or invalidate a count receipt.
The query read revision records when that count was produced; it is not a
retained historical query snapshot. The bounded authorization receipt is shared
among references using that scope/revision and expires with its final grant.

For a row, the reference binds the complete support set for that row at that
instant. The inline `provenance` list contains at most 8 supports, ordered by
support UUID; it can be shorter to fit the 32 KiB envelope. Both subjects use
`provenance_complete` and nullable `provenance_next_ref`. A false completeness
flag requires a continuation reference; null is allowed only when complete.
The minimum row must fit; truncation never removes its required identity.

Provenance continuation is an evidence read through the existing retrieve/read
adapter seam, not another model-facing search tool. It returns at most 8
supports and 32 KiB, binds the same immutable reference and scope, rechecks
current access, and expires with the original reference. It neither re-queries
current supports nor issues a new retention grant.

The finite reference lifetime is **365 days from `queried_at`**, including for
zero-count receipts. It is nonrenewable. Because current queries cannot mint a
reference to retired supports, every protected historical support expires no
later than its source retirement plus 365 days. Fin must not advertise an
expiry later than the backend grant. Repeated new queries of current data can
create new bounded snapshots; limits on total current storage and query volume
are tracked separately as a capacity issue.

Every returned row receives a grant, even if fin later fails to store the answer
or the model does not cite it. This can retain unused evidence until its fixed
expiry; no undisclosed consumer acknowledgement protocol is required. Capacity
accounting must include these grants, not only user-visible citations.

Evidence reads preserve the snapshot but recheck current authorization for all
its required dependencies. If a row dependency is denied, purged, or expired,
withhold the indivisible snapshot. Do not leak its fields by partially rendering
it. Ordinary source retirement permits authorized historical reads until expiry;
access revocation or explicit purge prevents them immediately.

Count replay checks its authorization receipt and fixed reference expiry,
without reading source payloads or reconstructing result membership. Expiry of
a normally collected retired source does not invalidate this receipt. Permission
revocation or explicit purge in its scope does invalidate it.

### Internal evidence capability

Extend the existing retrieve adapter seam with a host-internal evidence capability
carried over the same MCP connection as `corpus_evidence`. It is not exposed as
a second model-facing search tool. Its request token is
`iknow.evidence_request`, `schema_version: 1`; the response token is
`iknow.evidence_result`, version 1. Trusted caller/consumer identity is supplied
by the transport, never by the model or a stored scope token. The action union is:

| Action | Request fields beyond token/version/action | Required success fields beyond token/version/action |
|---|---|---|
| `read` | `evidence_ref` | `evidence_ref`, original `context`, exact retained `value` |
| `supports` | `evidence_ref`, optional `cursor` or `support_id` (mutually exclusive) | `evidence_ref`, ordered `supports`, nullable `next_cursor` |
| `invalidations` | nullable `checkpoint`, `limit` in 1..20 (default 20) | content-free `events`, `next_checkpoint`, boolean `gap` |
| `ack` | `checkpoint` | acknowledged `checkpoint` |

`context` has subject, operation, query digest, scope binding, read revision,
query time, evidence expiry, coverage, and the original maintenance summary.
`read` returns exactly the stored row/count rather than running a query.
`supports` uses the frozen row grant, returns at most 8 support descriptors,
and can resolve a selected endpoint display support even when it is outside the
inline relationship-support prefix. A count grant cannot request supports.
Continuation binds grant/cursor/scope and does not renew expiry. Every action
has the 32 KiB envelope and 60-second execution bounds. Malformed action/fields
use `malformed_evidence_contract`; an unavailable grant never returns an empty
successful value. Evidence failures below are carried in the existing
`iknow.structured_error` envelope, with explicit typed `error.code`.

Invalidations are durable per-consumer outbox events with `event_id`,
`evidence_ref`, and `reason` (`access_revoked` or `purged`); they contain no
source text. The backend records the trusted fin consumer against each grant.
Fin drains at least once per minute while online, transactionally withholds and
scrubs affected stored snapshot payloads, and acknowledges only afterward.
Checkpoints/cursors are server-authenticated and consumer-bound. An acknowledgement
can advance only through a returned contiguous batch successfully processed by
that consumer; malformed or cross-consumer checkpoints refuse. A reference never
stored by fin can be acknowledged after verifying no local payload exists.
Reconnect/crash recovery resumes from the last acknowledged checkpoint before
exposing historical evidence. If a retained checkpoint is unavailable (`gap`),
revalidate all retained references and scrub denied ones before exposing them.
The backend keeps unacknowledged events while their grants remain unexpired;
expiry lets both stores collect payloads without depending on acknowledgements.
Routine GC and query reads do not require a consumer to acknowledge creating
a citation. Explicit purge remains pending across owners until fin confirms
scrub; backend source deletion can proceed independently. A disconnected owner
cannot be reported physically erased merely because backend replay is denied.

## Citation and snapshot interface

### New kind

`kind: "structured-result"` supports either one returned entity/assertion or an
exact count receipt. It travels in the `structured_result` sibling of the
retrieve envelope, never `chunks[]` or `database_rows`. The host verifier binds
an answer claim to fields received in the current turn, selected by subject,
operation, and result ID (or the count query identity). It cannot accept a
supporting value absent from that payload or treat the assertion as established
truth merely because it has several origins.

### Frozen snapshot

fin stores one bounded snapshot per cited row/count, with the following exact
key set; `value` is the exact row or count receipt returned by iknow:

```json
{
  "schema": "fin.evidence.structured_snapshot",
  "schema_version": 1,
  "kind": "structured-result",
  "subject": "entity",
  "operation": "list",
  "query_digest": "opaque-query-digest",
  "scope_binding": "opaque-scope-binding",
  "read_revision": "opaque-read-revision",
  "queried_at": "2026-10-03T00:00:00Z",
  "expires_at": "2027-10-03T00:00:00Z",
  "evidence_ref": "opaque-evidence-reference",
  "value": {},
  "coverage": "stored_structured_subjects",
  "maintenance": {}
}
```

The empty `value` and `maintenance` above mark shape slots only; a production
snapshot must contain a fully validated query row/receipt and maintenance
summary. `expires_at` equals `evidence_expires_at`; `evidence_ref` equals the
value's reference. Top-level context must match the backend grant. Unknown keys
or an operation/value mismatch are malformed. The existing turn/citation record
stores the snapshot plus its digest and a selector; no legacy record is rewritten.

A snapshot is at most 32 KiB serialized UTF-8. Snapshot overhead can exceed row
size: if a citation snapshot cannot fit, return `evidence_too_large`; do not
truncate it or silently cite an incomplete value. A producer should reserve
this overhead when budgeting list pages.

### Type-preserving digest

Digest = lowercase hexadecimal SHA-256 of the canonical UTF-8 encoding of the
entire validated snapshot above, without a digest field. This encoding is a
new contract scoped to `fin.evidence.structured_snapshot` version 1:

1. Preserve null, booleans, strings, arrays, objects, and numbers as distinct
   JSON types; reject non-finite numbers and unpaired
   Unicode surrogates. Reject numbers outside the safe integer range or with a
   fractional decoded value. Normalize accepted integer-valued numbers to base-10
   integer text, with zero encoded as `0`. Schema version follows ADR 0093.
2. Sort object keys lexicographically by their UTF-8 bytes. Keep array order;
   support arrays therefore use their required stable ordering. Emit no whitespace.
3. Encode strings without Unicode normalization. Escape quotation marks and
   backslashes as `\"` and `\\`; encode U+0000 through U+001F as lowercase
   six-character `\u00xx` escapes; encode all other scalar values directly in
   UTF-8. This defines control-character encoding independently of library defaults.
4. Emit `null`, `true`, and `false` literally. Recursively use the same rules for
   every value, including maintenance and count metadata.

The v1 fields require no fractional numbers: timestamps are strings and counts,
positions, and units are integers. A future confidence/decimal field requires
an explicit shape/version decision. Numeric `1`, string `"1"`, and boolean
`true` must have distinct digests. `schema_version: 1.0` and `1` must have the
same digest. Python and TypeScript must share byte-level golden vectors before
shipping this new profile; the legacy digest vectors remain unchanged.

Canonicalization consumes the validated decoded object. A raw JSON decoder can
reject duplicate keys only where that module actually owns the original bytes;
an already decoded MCP object cannot reveal keys discarded by an earlier parser.
Raw-decoder checks must use raw-byte cases, not pretend that a dictionary/object
fixture can represent duplicate keys. No consumer is required to recover lost
lexical information at the decoded-object seam.

### Replay and failure

Opening an old citation uses the retained snapshot and reference; it issues no
search query. Refresh creates a new query and answer evidence without editing
the old answer. Successful replay returns exactly the stored value and support
set. Expired/purged history displays an unavailable citation with a reason,
not a replacement or an unsupported statement that the old assertion is false.

New evidence failures use typed exits: `malformed_evidence_contract`,
`unsupported_evidence_contract`, `unsupported_source_binding`,
`evidence_too_large`, `evidence_expired`, `evidence_unavailable`, and
`access_denied`. Unknown shapes never become legacy rows. Backend/network
failure is `backend_unavailable`, never historical expiry. These exits may be
mapped to existing host exit types without parsing prose; user-facing copy
must distinguish expired evidence from unavailable service or denied access.

## Retention contract

| Data | Retention obligation |
|---|---|
| Current source, accepted extraction, and current supporting evidence | Retain while current knowledge depends on it; age alone does not expire it |
| Retired source versions/extractions/supports with no unexpired row-content reference | Eligible at retirement + 90 days |
| Retired evidence with finite row references | Eligible at the later of retirement + 90 days and the last row-reference expiry, which cannot exceed retirement + 365 days |
| Bounded answer snapshots/references | Expire 365 days after query; source deletion/access revocation can withhold sooner |
| Small audit tombstones/outcome summaries | Keep non-content identity, retirement/deletion time, and reason; no source text, extracted payload, or historical support list |

An answer can remain in thread history after evidence expiry, but its citation
is marked unavailable. Cached backend payloads and retained snapshot payloads
must be physically collected by their owning stores after expiry and the
maintenance grace. The brief audit record is distinct from retained evidence.
Explicit owner-authorized purge tombstones the source first, invalidates all
references, and schedules payload deletion without waiting for normal retention.
Reader leases must terminate within their maximum execution bound. Purge does
not promise immediate erasure from backups; that policy is a separate issue.

Retirement clocks apply to each source/extraction/support payload when it leaves
the published accepted set, including re-extraction of an unchanged source.
Only a dependency of current knowledge can override the age limit; historical
decision/answer links do not create indefinite obligations. Preserve the brief
decision outcome without source content after its historical evidence expires.

## Success Criteria

These are binary **future implementation acceptance cases**, not recorded passes.
The driver must exercise the real adapter seam and retained records, not only
fabricated successful payloads.

| ID | Required check and expected result |
|---|---|
| E1 | Ingest the frozen public corpus; both subjects resolve to independently reviewed original version/unit bindings; mismatched binding refuses |
| E2 | Same occurrence extracted twice has one support with two extraction links; different versions/locations remain distinct |
| E3 | Same relation supported by two sources has one result identity; withdrawing one preserves the other and old evidence |
| E4 | Update and publish v2; a v1 answer replays v1, while refresh receives v2; no implicit query during replay |
| E5 | Both row and zero/nonzero count snapshot verify only fields received this turn; invented fields and unsupported shapes refuse |
| E6 | More than 8 supports continues in stable order within both limits; no incomplete flag without continuation; expiry never renews |
| E7 | Python/TypeScript golden vectors agree on canonical bytes/digest; numbers versus strings/bools differ; `1`/`1.0` versions agree; unexpected keys, surrogates, overflow refuse; any raw-decoder duplicate-key case is tested with raw bytes |
| E8 | Current access revocation or purge withholds an otherwise unexpired snapshot and inline/continued supports |
| E9 | Fake clock crosses 90/365-day boundaries: row references protect retired source evidence only until their fixed deadline; reading again does not extend it |
| E10 | Expired citation stays visibly unavailable; no redirection to current version; payload cleanup leaves only non-content audit data |
| E11 | Oversized row/snapshot and source-service failure yield their typed errors, without truncation or success-shaped empty evidence |
| E12 | Existing database-row, locator, visual-evidence, ADR 0092 digest, and ADR 0093 conformance cases remain unchanged and pass |
| E13 | Re-extract unchanged source with a new model: omitted occurrences retire at successful publication, shared occurrences preserve support IDs, and prior snapshots retain their original extraction attribution |
| E14 | Zero-count receipt survives ordinary source replacement and unreferenced source payload GC, but scope revocation/purge withholds it immediately; no raw source or full membership pin exists |
| E15 | Internal read/supports return the frozen authorized grant; invalidation drain scrubs fin payload before ack; offline reconnect/gap recovery cannot expose denied snapshots or report purge complete prematurely |

## Open Questions

No blocking architecture fork remains in this contract. The acceptance corpus
must still be selected and independently frozen as required by the query spec;
its missing gold is an implementation prerequisite, not a passing SPEC example.
Backup erasure and total storage limits are tracked as separate issues linked
from the maintenance contract.

## Pending persistence and handoff

Persistence is flushed through `domain-modeling`: **source version**,
**assertion support**, and **structured result evidence** are in
[CONTEXT](../docs/CONTEXT.md), and the lifecycle trade-off is in
[ADR 0095](../docs/adr/0095-immutable-source-history-and-finite-retention.md).
The new wire kind and digest stay owned by this spec. The shared
[architecture review](structured-corpus-query.md#architecture-review) covers
all three contracts. Hand them together to a later `writing-plans` phase;
this session delivers contracts and does not start runtime implementation.
