# Pre-330 integration repairs

Basis: operator request to integrate the three completed sessions and prepare the environment before issue #330. Non-functional integration defects may be repaired; business scope and real-ledger access must be reported to the operator. B3 is not executed here.

## Scope

- `src/fin/host/workspace_http.py`: preserve the existing response objects while reconciling E's new 503 branches with F's scoped type gate.
- `scripts/g4_pipeline/build.sh`: repair snapshot command argument handling, demonstrated by a real Bash reproduction.
- `scripts/g4_pipeline/Dockerfile`: consume the already-declared optional pip wheel input; no package or hash changes.
- `tests/corpus/test_chunk_and_push.py`: make the no-opt-in case independent of the operator adapter environment, preserving the refusal assertion.
- `tests/host/test_g4_pipeline_build_execution.py`: command success, failure preservation, and temporary-file cleanup.
- `tests/host/test_live_graph_visual_lane.py`: reconcile the acceptance test with the versioned wire projection's tuple return while preserving persistence, source, and quote assertions.
- This plan and `docs/validation/2026-09-30-pre330-integration.md`: integration evidence and explicit outstanding prerequisites.
- `plans/post321-pipeline-reproducibility.md`, `plans/post321-real-browser-acceptance.md`, and `plans/post321-parallel-program.md`: reflect the operator's phase clarification that dedicated-device measurement belongs to later deployment validation, while retaining C1 and VM pipeline gold before B3.

## Architecture gate

bounded-context-guardian: yes — repairs remain in workspace transport and pipeline orchestration; no new cross-context dependency.
input-contract-tests: yes — existing HTTP outage contract tests and Bash command success/failure cases cover the applicable boundaries; no new public API.
error-handling-enforcer: yes — HTTP keeps its typed persistence error; snapshot preserves the prior artifact on command failure and returns a failure status.
complexity-anti-drift: yes — add one argument shift to an existing helper and two runtime-inert casts; no new abstraction or nested control flow.
minimal-change-verifier: yes — one integration task, no dependency upgrades, settings changes, or functional SQL window implementation.

## Verification

- Scoped pyright; existing HTTP persistence and history-citation cases.
- Failing-first Bash snapshot tests, then the pipeline tests and full hermetic suite.
- Versioned baseline gold and web tests/build.
- Disposable Postgres/object-store live graph gate, current iknow main build MCP checks, Jev live checks.
- Record #324's still-open reproducibility/device evidence and SQL/A3b functional blockers explicitly. Missing real-boundary evidence never becomes a passing skip.
