# Issue 318: host cache identity and attachment test wiring

Basis: https://github.com/oocang/fin/issues/318.
Base: PR #316 tip `0f65d6f6d496a66c3c0c1c8240f68af8d058d18b`.

## Scope and root cause

The graph cache stores integer object IDs without retaining every object
that contributes to its key. A disabled memory port, for example, is not
captured by the compiled graph. Collection can leave an obsolete key whose
ID is reused by a later object, allowing that caller to receive another
configuration's graph proxy.

Use a private holder that retains each existing identity-bearing input,
hashes by object ID, and compares by `is`. Pass actual dependency objects
through the private graph factory instead of precomputed integer IDs.
Keep the existing scalar dimensions and process-lifetime dictionary cache.
Do not add omitted configuration dimensions or change the public tool,
retrieval, attachment ownership, or citation contracts.

Share the preparation outcome, resolved attachment, and scripted service
across the gate and retrieval suites. Share typed store forwarding while
keeping each corruption scenario in its own override. Preserve all existing
assertions. The long resume-seam test and Docker launcher environment are
explicitly outside issue #318's repair scope.

## Expected files

- `src/fin/host/graph.py`
- `tests/host/test_graph_cache.py`
- `tests/host/attachment_fakes.py`
- `tests/host/test_graph_attachment_gate.py`
- `tests/host/test_graph_attachment_retrieve.py`
- `tests/host/test_attachment_retrieval.py`
- `plans/318-host-test-wiring.md`
- `docs/validation/2026-09-28-318-host-test-wiring.md`

## Ordered execution

1. Record the independent architecture gate before implementation.
2. Write and run a deterministic cache regression against the unmodified
   graph. Distinct dependency instances must remain distinct even if IDs
   collide; repeated compilation with the same inputs must reuse its proxy.
   Cover unhashable, value-equal dependencies and dependency lifetime.
3. Replace integer identity components with holders; run the RED tests GREEN.
4. Extract shared preparation doubles and typed delegating-store forwarding.
5. Run the issue's host sweep, the existing graph/memory suites, the committed
   nine-case gold baseline, and the full default suite. Compare any failures
   with the unmodified base; do not weaken assertions or add skips.
6. Pin the complete diff for independent Standards and Spec reviews. Repair
   any blocking findings, verify every issue clause, and commit locally.

## Validation

Use the existing T3 environment without synchronizing dependencies:
`UV_PROJECT_ENVIRONMENT=/home/winner/projects/fin-wt-t3/.venv PYTHONPATH=src`.
Run commands from `/home/winner/projects/fin-wt-318` with
`uv run --no-sync`. Confirm imported production code resolves to this worktree.

- `pytest -q tests/host/test_graph_cache.py`: record RED and GREEN.
- `pytest -q tests/host -k "attachment or graph"`: base is 973 passed,
  47 skipped, 1906 deselected, exit 0. Existing skips are live prerequisites.
- `pytest -q -m eval tests/eval/test_gold.py`: ADR 0012 committed nine cases;
  base is 9 passed, with 6 done and 3 refuse, exit 0.
- `pytest -q`: record all failures and distinguish unchanged baseline faults.
- Targeted type checking: compare introduced diagnostics with the base.
- `git diff --check`: no whitespace errors.

No external service or backend wire contract changes in this repair. The
cache tests exercise the actual compiled LangGraph and its in-memory
checkpoint boundary. They do not certify live Postgres, object storage,
model providers, or the separate issue #319 deployment prerequisites.

## Architecture gate

Independent pre-implementation reviewer: `/root/gate_318`.

bounded-context-guardian: yes — production stays in the host graph; shared
fakes stay in host tests, with no new cross-context dependency.
input-contract-tests: yes — no public input or failure contract changes;
cache collisions, unhashable/value-equal objects, reuse, separation, and
dependency lifetime have explicit regression coverage.
error-handling-enforcer: yes — no new failure or fallback paths.
complexity-anti-drift: yes — a small private identity holder and shared
test forwarding preserve the existing orchestration structure.
minimal-change-verifier: yes — the scope is issue #318's three follow-ups,
with issue #319, public APIs, schemas, and dependencies excluded.

OVERALL: PASS.
