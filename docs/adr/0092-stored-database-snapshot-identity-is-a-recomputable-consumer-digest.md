# 0092. Stored database-snapshot identity is a recomputable consumer-minted digest over canonical textified JSON

Date: 2026-09-30
Status: accepted

## Context

The database-row evidence contract (spec `search-mode-business-sql-contract`
§8/§8.1, issue #334 E1) retains a bounded result snapshot with each citation:
`source`, `record_key`, `queried_at`, `fields`, `summary`. Opening a citation
displays that snapshot and issues no query; an explicit refresh mints a **new**
evidence item. Once such snapshots are persisted and displayed, the identity
used to name "this exact snapshot" cannot later change without invalidating
every historical citation that showed it. That makes the identity rule a
one-way door, and E1 must decide it rather than leave it to an implementer.

The wire values are JSON scalars: a producer may send `30` or `"30"` for the
same ledger fact, and `d2c3aa0`'s consumer already renders both to one text
form for claim checking (`citation_gate._plain_text`). Any identity rule that
hashed raw JSON bytes would depend on serialization accidents — key order,
number formatting, `30` vs `"30"` — which differ across the TypeScript
producer (#322) and the Python consumer (fin), and are exactly the kind of
instability that turns an identity into a source of false mismatches.

## Decision

The stored-snapshot identity is a **digest minted by the fin consumer at
admission**, fully recomputable from the stored snapshot, defined in
`specs/enterprise-evidence-contract.md` §4.4:

```
snapshot_digest = "sha256:" + hex(sha256(canonical_json(m)))
```

`m` is the object `{source, record_key, queried_at, fields, summary}` with
every value first rendered to its canonical **text** form (strings as-is,
numbers as shortest round-trip decimal, booleans `true`/`false`, `null` as
`""`), and `canonical_json` is UTF-8 JSON with object keys sorted
lexicographically at every level, no insignificant whitespace, and
non-ASCII preserved. Because digest inputs are text-only, no cross-language
number serialization can ever reach the hash. Any carrier of a digest must
equal a recomputation from the snapshot; a mismatch is
`malformed_evidence_contract`. The producer never sends a digest.

## Considered Options

- **Producer-minted id.** Rejected: iknow already owns row identity
  (`record_key`); a second producer-minted id would make snapshot identity
  depend on the producer being reachable and correct, while fin's promise is
  about *this stored result*, which fin holds.
- **Store the digest only, never recompute.** Rejected: a stored digest that
  nothing recomputes cannot detect a corrupted or rewritten snapshot on
  replay, which is the point of an identity for evidence.
- **Hash the raw serialized bytes.** Rejected: serialization is not stable
  across the two languages on either side of the boundary (`30` vs `"30"`,
  key order, float formatting), so the "identity" would split one snapshot
  into many identities.

## Consequences

- **Applied:** replay, display, and refresh comparison all share one
  deterministic identity; corruption of a stored snapshot is detectable
  without any query; #322 owes no digest logic.
- **Trade-offs:** the formula is frozen the moment the first digest is
  persisted — changing it later is a versioned contract bump (`v2`), not a
  refactor, and old and new digests will never match each other. The digest
  covers `summary`, so a display-text change is also an identity change; that
  is intentional (tamper-evidence includes the text the operator saw) and is
  why the summary bound and minting rule are frozen in the same contract.
