# P1-D2 — the Query envelope's `evidence_ref` is the Evidence grant, and the exclusion proof belongs to T21/T13/T14

Date: 2026-10-07
Status: accepted (decision, no code)
Authority: [`specs/structured-corpus-evidence.md`](../../specs/structured-corpus-evidence.md) §"Internal evidence capability" and §"Retrieved corpus", which outrank the plans and the P1 lane's own records.
Implements: the boundary question the P1 review raised at `docs/validation/2026-10-07-p1-review-closure-verdict.md` §5 — wiring P1-I6 makes `evidence_ref` model-visible, and T39 §3.5 explicitly left the semantic exclusion unproven.
Blocks: P1-I6.

## Question

Once P1-I6 issues grants, the `structured_result` envelope carries a real
`evidence_ref` instead of the `""` that makes `readEvidenceRef` refuse today. That
reference is also the input the private `corpus_evidence` route reads. May one value be
both model-visible and the private channel's key, or must the Query lane mint something
distinct?

## Decision

**One reference, not two.** The Query envelope's `evidence_ref` IS the Evidence grant
reference. Do not invent a second reference type for the Query lane.

## Why

Three spec sentences settle it, and they point one way:

1. The evidence spec states that fin's adapter "carries host-internal evidence reads
   and invalidation acknowledgements over the same connection, without another
   model-facing search tool" (`:53`). The architecture *intends* the Query result to
   carry the reference the host then uses for its private reads. A Query-only reference
   type would mean the host holds two identities for one grant, and P2's invalidation
   drain — which scrubs `evidence_ref` — would have to know which one it is scrubbing.
2. The two grant types are already fixed as **row** and **count** (`:160-167`), with the
   count grant's shared authorization receipt explicitly *backend-private*. Adding a
   third reference kind to distinguish planes would reopen that closed pair.
3. The exclusion the reviewers worried about is a **routing** property, not a naming one:
   no model-facing tool accepts an `evidence_ref`, because the Evidence route is
   host-internal. A visible value that no model-reachable surface consumes is not a
   channel key.

## What this decision does NOT settle

**The semantic exclusion is not proved by this record.** T39 §3.5 states that the
Evidence-vs-Query exclusion "is guarded at schema-key level but not yet proven
end-to-end", and assigns that proof to T21/T13/T14 — not to T39 and not to P1-I6. That
assignment stands. What P1-I6 must do is refuse to *create* the condition:

- the reference is issued only for a row already returned under an authorized scope, so
  the spec's rule at `:147` — "a model must never receive fields or display names
  derived solely from an unauthorized support" — holds by construction;
- nothing on the model plane reads it back. The one thing P1-I6 may not do is add a
  model-facing consumer for it, which is the same invariant T39's freeze guards.

## Overturn condition

If T13 or T14 finds any model-reachable surface that consumes an `evidence_ref` — a tool
parameter, a filter key, an output field the model is instructed to pass onward — then
this decision is wrong and the planes must be separated at the reference. That is the
finding to look for, and it is why the exclusion proof stays with T21/T13/T14.
