# PR 339 review repairs and landing

Basis: operator instruction to have subagents repair the review findings, merge, and then proceed to the next step. CI is explicitly not a waiting condition. B3 is not run by this task.

Pinned starting point: fin `5c59a05796d0edbca31361456e59f0c17a6d98b8`; companion producer `eea1e07124e8baf68893bc36fda692ac1dc9d684` (currently local, remote PR 279 still at `620f135`). Review inventory: Standards 1 High / 2 Medium; Spec 2 High / 1 Medium.

## Repair units and ownership

1. Web contract/errors: `web/src/evidenceContract.ts`, `web/src/comparison.ts`, `web/src/api.ts`, associated `types.ts`/`store.ts` only when the existing refusal path requires it; tests in `web/src/evidenceContract.test.ts`, `comparison.test.ts`, `comparisonStream.test.ts`, and existing API/store tests. Check token/version pairing before token recognition. A malformed or unsupported comparison reaches the caller as the existing typed evidence refusal, never a silently missing table. History follows the same explicit refusal rule. Old persisted comparison tokens remain unsupported under the existing contract; no suffix stripping or database migration is introduced.
2. Backend contract: `src/fin/host/evidence_contract.py` and `tests/host/test_evidence_contract.py`. Share row-value, timestamp and bound checks while preserving distinct ingress/egress key sets and egress version-pair checks. Preserve refusal precedence, finite-integer version semantics, snapshot bytes and digests.
3. Live evidence and corpus test setup: `tests/retrieve/test_live_search_modes_pg_es.py` and, only if necessary, `live_iknow_support.py`; `tests/corpus/test_ingest_state_store_durable.py` and `test_ingest_durable_restart.py`. Keep routing/refusal coverage and add a separate positive real-ledger case with fixed fixture IDs and complete validated envelope assertions. Initialize the actual corpus store before fixture cleanup on a fresh database; do not change product code to accommodate tests.
4. Commit isolation: AGENTS changes are removed from the feature PR and carried as a separate documentation PR with the operator's already-supplied content. No new rule, dependency, lockfile or settings change. This is independent from the three code repair units.
5. Evidence: `docs/validation/2026-10-02-pr339-repairs.md` records commands, producer revision, positive ledger evidence, historical-record impact and remaining prerequisites.

Test engineers own test edits first; implementation agents then own their respective production modules. No worker may revert another worker's changes. The live-test unit shares no production write path with either implementation unit.

## Architecture gate

bounded-context-guardian: yes — repairs stay in existing web and fin host modules; no new cross-context implementation dependency.
input-contract-tests: yes — empty arrays remain legal; missing/wrong-type versions are malformed; negative but integral unsupported versions preserve their class; existing overflow gold covers shared row limits; concurrency is inapplicable to pure decoders and existing stream/history ordering tests remain; transport exceptions retain their existing error path.
error-handling-enforcer: yes — malformed, unsupported and comparison overflow use non-empty closed-set codes; an empty display list accompanies an explicit refusal with an EXIT comment rather than success-shaped silence.
complexity-anti-drift: yes — shared value/bounds checks keep distinct key contracts, pure decoding and a single refusal path; no new nested aggregate or duplicate ladder.
minimal-change-verifier: yes — feature repairs cover the reported contract/error/evidence findings; normative content is isolated in a separate documentation PR.

These five verdicts were independently reviewed before implementation. The initial gate was blocked; its input matrix, named exits and independent normative task were clarified, and the revised gate passed.

## Verification and landing

- Demonstrate failing tests for unknown-token/missing-version pairing and observable comparison refusals before code fixes.
- Run all evidence conformance cases, shared version cases, backend contract/export/workspace tests, complete web tests/build and pyright.
- Run positive SQL/MCP against the independent seeded readonly ledger and pinned/rebuilt producer; routing-only passes cannot stand in for this success case.
- Run the required real visual lane with its existing provider-file seam; record intermittent failures as well as eventual results. Stop owned test servers.
- Run the default hermetic suite and baseline gold after edits stabilize; keep logs under ignored `.fin/pre330/logs/`.
- Verify the repair diff and check review findings against actual results; no full-G4 or B3-readiness claim is inferred from local checks.
- Push only intended commits, preserve the sibling's uncommitted AGENTS file, and land the companion producer before fin using squash merges. Do not wait for CI under the operator's explicit instruction; local validation remains required.

## Final comparison-shape finding: expanded bounded repair

Final Spec review identified one remaining High: the comparison decoder accepts the committed malformed `unknown-field-envelope` fixture. The fix must enforce the comparison's closed structural contract, not special-case one extra field. Files: `web/src/evidenceContract.ts`, optional pure leaf `web/src/comparisonShape.ts`, `web/src/types.ts` to remove the forbidden optional comparison metadata, and existing comparison, SSE and history tests. The leaf has separate field/grid/count/locator/delta/missing guards, imports no runtime evidence decoder, and returns existing closed refusal codes. Pair/token/version classification stays at its existing boundary. Web checks structural evidence; server retains run-specific provenance admission.

The graph currently injects `tool_call_id` into that closed envelope. Repair `src/fin/host/graph.py::comparison_from_tool_message` to preserve the producer comparison object without injecting transport metadata. The separate tool-result frame and tool trace retain their correlation ID. Backend test scope is `tests/host/test_compare_in_thread.py` and `tests/host/test_compare_persistence.py`: prove real compare_facts output remains structurally valid through extraction, SSE and history; preserve non-comparison routing and trace association. Test changes precede this implementation and replace any assertions expecting the forbidden extra key with stronger schema/round-trip checks.

The existing `web/src/pages/ThreadPage.tsx` render call-site reads the forbidden optional field for its React key. Its one-line alignment keeps the existing ordered-list index fallback as the key, removing the invalid metadata dependency. No UI state, ordering, content or interaction behavior is added. The five verdicts below cover this same-module caller alignment.

The existing zero-baseline delta (`percent: null`) conflicts with the frozen numeric-only comparison contract. It is an existing functional producer issue, already refused by server validation, and is reported for a bounded follow-up rather than silently changing the schema, fabricating a numeric percent, or broadening this repair.

The independent Spec report is the evidence for this necessary scope expansion. A new agent dispatch was unavailable because the tool returned `agent thread limit reached`; the primary agent performs this pre-implementation gate:

bounded-context-guardian: yes — the pure web leaf has no runtime decoder dependency; the host extractor preserves its own closed payload and existing trace boundary.
input-contract-tests: yes — independent committed malformed fixtures cover missing/unknown keys, wrong scalar types, empty or invalid grids and overflow; SSE/history must expose named refusals; real tool output provides the positive backend round trip; concurrent mutation is inapplicable to these pure guards.
error-handling-enforcer: yes — malformed, unsupported and overflow return the existing non-empty typed refusal codes; no silent version default or transport-metadata exception is introduced.
complexity-anti-drift: yes — field, grid, count, locator, delta and missing-entry checks stay separate; the leaf has one structural responsibility and no cyclic runtime imports or deep aggregate conditional.
minimal-change-verifier: yes — changes repair the single comparison-contract High and its producer incompatibility; the distinct zero-baseline functional behavior is reported, with no schema, dependency, lockfile or settings change.
