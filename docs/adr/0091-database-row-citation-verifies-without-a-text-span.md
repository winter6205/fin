# 0091. A database-row citation verifies by record key and returned fields, not by text span

Date: 2026-09-29
Status: accepted

ADR 0046 ends "a source marker without a matching span is not a citation", and that
sentence stays in force for **document** evidence. This ADR records the narrow
divergence: a row returned by the `sql` 档 is evidence of a different shape, and
forcing it through the span rule would be wrong rather than merely strict. A
document citation verifies because a contiguous span of the answer appears in text
this run received; a ledger row has no page, no manifest digest, and no span — a
`notice_days` of `30` is a real fact, and quoting a 32-code-point window around it
proves nothing. So a database-row citation verifies by reference to the row's
`record_key` and the `fields` this round actually returned; a number the model
states that no returned field supports is unsupported, which is a different
failure from a missing locator. ADR 0046 holds in spirit — the row is valid only
against a result set this round produced, never against a model's reconstruction
(ADR 0021) — and ADR 0046's span sentence is **not** weakened, only scoped.

**Why not a documented exception instead:** an exception reads as a known defect to
be cleaned up, and the next person to see a span-less citation would have no way to
tell an intentional contract from a regression.

**Why the 32-code-point minimum is not lowered generally:** the asymmetry is
deliberate. Document passages are prose, where a long verbatim span is the natural
evidence; ledger values are scalars, where the length rule carries no information.
Lowering the floor for both would weaken the document path for no gain.

**Considered Options:** keep one span rule for every kind (a scalar fact could not
be cited at all, so the `sql` 档 could not ground a number); let a database row
render as a `field=value` text line and substring-match it (adopted then reverted —
an ordinary ledger value like a counterparty reference shaped like a filing locator
made the existing locator regex read the row as a forged document and refuse the
whole run, which is evidence the two shapes are different things, not a detail to
work around).

**Consequences:**

- **正面 / Applied:** the `sql` 档 can ground numeric answers that a document corpus
  structurally cannot contain; the verifier for this kind is a check against a
  returned result set rather than a text search.
- **负面 / Trade-offs:** there are now two verification rules behind one evidence
  closed set, so the gate is no longer uniform. A future kind must declare which
  rule it uses, and a citation whose kind is unknown must be refused rather than
  defaulted to either rule.
