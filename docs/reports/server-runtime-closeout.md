# Closeout verification

Date: 2026-10-18. Basis: `plans/qa-chain-02-server-runtime.md`,
`specs/qa-chain-server-runtime.md`, ADR 0101, and the operator's request to
finish incorrect or missing implementation, squash merge, and synchronize main.

## Scope and acceptance

The existing PR had already received code review. The operator explicitly waived
an unnecessary repeat review and waiting for CI. This closeout used Serena symbol
navigation, read-only exploration, one responsible host implementer, and an
independent acceptance verifier for the fixes below.

| Outcome | Closeout artifact and direct regression |
| --- | --- |
| App-owned runtime and compatible graph dependency | Separate application runtime compilation; request-scoped graph resolution preserves dependency overrides and legacy direct calls; two-app graph and shutdown isolation tests |
| Evidence-session lifetime | Application composition owns the registry it creates; closing the runtime drains only that application's registry, including compile failure cleanup |
| Partial-start resource cleanup | Acquired checkpointer stack closes on setup, service resolution, and graph construction exceptions; graph-build failure regression verifies closure |
| App-owned worker shutdown | Four application worker starts return their own stop closures, with shared construction helpers for legacy entry points; peer resume-worker shutdown, partial startup, no-transport and boot tests |
| One execution owner through persistence | Send/resume hold thread admission through pause/terminal persistence and release on exception/cancellation; blocked append regressions cover both entry paths |
| Truthful checkpoint recovery | Resume reconciles accepted inputs before and after execution; restart-ahead-of-checkpoint, continued reply, replay idempotency, and bounded-history regressions |

The work preserves the original run identity, receipt schema, store transaction
contract, and public entry points. No dependency, lockfile, settings, schema, or
model prompt/tool-description change is part of this closeout.

## Verification environment

Python tests run with `PYTHONPATH=src` so the linked worktree, rather than the
editable install's original checkout, supplies product code. Required corpus and
private fixtures were copied from existing local development assets. Tests use
an isolated temporary `FIN_HOME` and a dedicated PostgreSQL 16 container; each
comparison run uses its own database whose name ends in `_test`. No daily
database or real user home is used.

Reproduction setup for every command below (use a fresh home for each database):

```bash
export PYTHONPATH=src
export FIN_PRIVATE_FIXTURES=<copied-private-fixture-root>
export FIN_HOME="$(mktemp -d)"
# Supply FIN_TEST_DSN through the environment for an isolated database ending
# in _test; the connection value is intentionally omitted from this record.
```

The unit-only independent verifier does not require a database connection.

## Results

Test commands use `uv run --no-sync`, preserving existing dependencies. The
following targeted measurements are complete; the broad host comparison is
recorded separately below.

- Implementer regression command: `PYTHONPATH=src uv run --no-sync pytest -q
  tests/host/test_compile.py tests/host/test_app_lifespan.py
  tests/host/test_run_execution.py tests/host/test_turn_receipt_paths.py
  tests/host/test_structured_invalidation_drain_worker.py
  tests/host/test_ingest_worker_boot.py tests/host/test_operator_env.py
  tests/host/test_attachment_resume_worker.py
  tests/host/test_attachment_budgets.py tests/host/test_http.py`:
  **423 passed**, one warning, exit 0.
- Independent verifier command: `uv run --no-sync pytest -q
  tests/host/test_app_lifespan.py tests/host/test_run_execution.py
  tests/host/test_turn_receipt_paths.py tests/host/test_turn_receipt_doors.py
  tests/host/test_compile.py::test_compile_runtime_handle_closes_postgres_after_graph_compile_failure
  tests/host/test_structured_invalidation_drain_worker.py::test_app_owned_invalidation_start_without_transport_returns_idempotent_stop`:
  **115 passed**, exit 0 on the final implementation (including the session
  registry and clarification-door regressions). Additional direct probes checked pause-persistence
  admission, failed persistence, GeneratorExit cleanup, legacy direct graph
  access, and peer isolation of ingest/preparation/invalidation worker stops.
- After copying existing ignored G4 result artifacts into both worktrees:
  `PYTHONPATH=src uv run --no-sync pytest -q --tb=short
  tests/host/test_attachment_layout.py tests/host/test_attachment_layout_harness.py
  tests/host/test_g4_pipeline_declaration_crosscheck.py
  tests/host/test_g4_pipeline_pin_contract.py`:
  **206 passed**, exit 0 on both base and final implementation.
- After the incomplete-history correction:
  `PYTHONPATH=src uv run --no-sync pytest -q
  tests/host/test_turn_receipt_doors.py tests/host/test_turn_receipt_paths.py`:
  **33 passed**, exit 0; existing clarification-count assertions are unchanged.
- Identity-bound session cleanup regressions first failed with zero registry
  close calls on both runtime close and compilation failure. After the fix,
  `PYTHONPATH=src uv run --no-sync pytest -q tests/host/test_app_lifespan.py
  tests/host/test_http.py`: **84 passed**, exit 0.
- `npm run typecheck`: zero errors, warnings and information messages, exit 0.
- `npm run lint:s5`: exit 0; no changed TS/JS files, so the complexity checker
  reported no applicable changed code files.
- The initial linked-worktree commit did not run Husky automatically because
  its ignored generated `.husky/_` helper was absent. No bypass flag was used.
  Equivalent full typecheck and applicable S5 commands were run explicitly.
  The generated helper was then copied from the original checkout for the
  evidence commit; automatic hook status is recorded with that commit output.
- Commit security checks: the filename scan is clean; the content scan matches
  only an unchanged documentation URL with obvious sample credentials in
  `src/fin/host/compile.py`. This is a confirmed placeholder false positive,
  not a newly introduced credential. No secret value is reproduced here.
- `git diff --check`: no output, exit 0.
- Dependency-direction self-check: no `fin.eval` import in host/retrieve; the
  corpus check finds the pre-existing lazy `fin.host.fin_home` import at
  `src/fin/corpus/catalog.py:73`, identical in base `0158f0a3`. No changed file
  introduces a forbidden dependency; this existing finding is not fixed here.

### Complexity scope

Independent behavior acceptance covers the five initial fixes and the additional identity-bound session-lifetime correction. S1/context slicing and
S2/external payload changes are not applicable; S3 error handling, S4 behavioral
evidence and S6 minimal scope pass within this closeout. This is not a second
full-PR code review.

A standard-library AST decision count found two new complexity-11 methods.
Extracting the shared execution release and bounded-history projection reduced
them below ten without changing behavior. Final measured decision counts are
  5 for send, 7 for resume, 5 for shared release, 8 for reconciliation, 4 for
  completeness detection and 5 for lower-bound projection; the implementer
  measured their maximum control nesting at three and parameters at four
  excluding `self`. Existing long functions remain an
explicit length REVIEW (not a claim that all Python code satisfies every S5
threshold). Python clone rate has not been measured; the repository's automated
S5 command only checks changed web TS/JS files. No dependency was installed or
check configuration changed to manufacture a pass.

### Broad host comparison

Command: `PYTHONPATH=src uv run --no-sync pytest -q --tb=short tests/host
-o faulthandler_timeout=120`, with the isolated environment described above.

Base `0158f0a3`: **4920 passed, 44 failed, 28 errors, two skipped, 315
deselected**, exit 1. Failures include 24 missing G4 artifacts (subsequently
restored and verified above), 19 instance-identity fixture conflicts, and the
existing thinking-text assertion. Errors include five corpus-fixture directory
collisions under the fixed test home and 23 unavailable live-child fixtures.
These baseline failures are not represented as passing tests.

The broad pre-final run reported **5316 passed, 34 failed, 28 errors, two skipped,
315 deselected**, exit 1. Comparing exact failed/error node IDs found 12 removed
failures after the G4 assets were restored and two additional receipt-test
failures. Clarification counting exposed a real missing-prefix case: a legacy
checkpoint can contain only a reply for the turn. Recovery now requires an
original-message entry before replacing the known total; otherwise it retains
the known prefix and marks accounting incomplete. The duplicate-terminal
PostgreSQL case passed alone in a fresh isolated database (one passed, exit 0),
showing a broad-suite fixture interaction. The final changed-file suite below
checks both cases after the recovery correction. Its first isolated run reported
1011 passed, 13 failed, one skipped and two deselected: eleven compaction tests
implicitly constructed the default database attachment dependency after earlier
new lifecycle tests initialized database identity, and the duplicate-terminal
fixture reused a global run ID owned by another module. These test-isolation
issues are corrected by explicit in-memory attachment injection and a module
namespace for receipt test run IDs; assertions are preserved. The same-order
base comparison remains 635 passed and the one existing thinking-text failure. The broad run is not claimed
as green or rerun after the last correction.

### Final changed-file selection

Final result: **1023 passed, one failed, one skipped, two deselected**, exit 1
(132.65 seconds). The only failure is the exact thinking-text assertion also
failing on main; neither fixture-isolation failure nor the recovered clarification
regression remains. The skip is the absent gitignored Vite build artifact. This
result is a comparison with an existing failure, not an all-green claim.

The final regression selects every Python test file changed by the PR plus
`test_compile.py`, with a fresh test database and home. Equivalent reproduction
from the completed branch, after the environment setup above:

```bash
mapfile -t changed_tests < <(git diff --name-only 0158f0a3 HEAD -- tests | rg '\.py$')
uv run --no-sync pytest -q --tb=short "${changed_tests[@]}" tests/host/test_compile.py
```

After the final product correction and before the fixture-ID isolation change,
the dedicated receipt suite returned **236 passed**, exit 0, against another
fresh test database:

```bash
uv run --no-sync pytest -q --tb=short \
  tests/host/test_turn_receipt_{store,postgres,schema_postgres,projection,paths,doors,deletion,deletion_door}.py \
  tests/host/test_receipt_view_http.py tests/host/test_thread_store_run_idempotency.py
```

## Limits and downstream work

- The full repository baseline run was interrupted after 418 passing tests and
  526 deselections when an unchanged PDF parser remained busy. That run is not
  a full-suite pass; final comparison is scoped to the host suite.
- A fresh baseline run of the 18 existing tests touched by the PR plus compile
  tests returned 635 passed, one failed, one skipped and two deselected. The
  failure was `test_send_message_persists_thinking_duration_and_echoes_on_status`
  (`thinking hard` absent); the skip requires the gitignored Vite build artifact.
- Full browser acceptance and live-model evaluation were not rerun for this
  lifecycle/receipt closeout. The original PR's browser report remains historical
  evidence, not a new measurement. G1-G3 and G2 product acceptance remain open.
- T11 owns hard interruption of blocked synchronous work and the disconnect
  recovery target. T12 owns the exporter; there is no export queue to discard.
- CI is not a landing gate for this task, by explicit operator instruction.
