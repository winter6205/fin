# Spec: Soft-Track judge routing

## Objective

On each turn, when rule-based Soft-Track classification is ambiguous, a judge picks exactly one of `dialogue | clarify | off_topic | grounded`. Low confidence goes to `clarify`, not silent `grounded`. Tools and citation verifier still bind only to `grounded`. Rules that already decide a track do not call the judge.

**Who:** fin host graph classify step.

## Boundaries

- **Does:** closed four-label pick; confidence gate → `clarify`; credentials from existing fin/operator env (no keys in git); keep rule tier first; inherit policy unchanged when the judge is not used.
- **Confirms with human:** none.
- **Out of this spec:** [citation judge check](citation-judge-check.md).

## Success Criteria

- Fixture: rules already `dialogue` → zero judge calls, leaf is dialogue.
- Fixture: ambiguous → fake judge `grounded` + high confidence → grounded binds tools.
- Fixture: ambiguous → fake judge any label + low confidence → `clarify`, no tools.
- Fixture: judge throw → `clarify` (fail-closed), not `grounded`.
- `uv run pytest -q -m soft_track` on suites this change touches exits 0 (or the repo’s documented Soft-Track marker command).

## Open Questions

(none)

## Inherits / Changes

**Inherits:** ADR 0027 / 0061 Soft-Track four leaves; rules-primary classify; grounded-only tools and citation verifier.

**Changes:** ambiguous tier uses a four-label judge; low conf / judge-down → clarify.

**待写入:** fin CONTEXT classify cascade names the four-label judge tier.

## Architecture Change Reviewer

bounded-context-guardian: yes — host classify only; retrieve port unchanged.
input-contract-tests: yes — rule hit, ambiguous, low conf, throw.
error-handling-enforcer: yes — no silent grounded on judge fail.
complexity-anti-drift: yes — one judge call in existing cascade slot.
minimal-change-verifier: yes — ambiguous tier only.
