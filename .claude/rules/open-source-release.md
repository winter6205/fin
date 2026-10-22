# Open-source release boundary

Private source checkouts keep evaluation datasets, campaign drivers, and recorded results. The public open-source checkouts are published to the `winter6205/fin` and `winter6205/iknow-memory` repositories.

When syncing either repository, do not copy the paths below into those checkouts, and do not `git add -f` them there. Do not gitignore these paths in the private source checkout. They stay tracked here. The release checkouts ignore them so a later sync cannot add new copies. An ignore rule does not remove a file that is already tracked.

## Do not publish

Evaluation datasets:

- `data/corpus/`
- `data/eval/`
- `data/grounded_assessment/`
- `data/conformance/journey_acceptance/`
- `eval/`
- `gold-set/`

Recorded results and session handoffs:

- `docs/validation/`
- `docs/handoff/`
- `docs/verification/`
- `docs/qa-chain-t1-disconnect-baseline.md`
- `docs/qa-chain-t2-real-upstream-finding.md`
- `docs/qa-chain-t3-reset-boundary.md`
- `docs/qa-chain-t3-seed-acceptance.md`
- `docs/qa-chain-t3-seed-non-persistent-store.md`

The operator's local plan records:

- `plans/`

Campaign drivers, probes, and the plans that exist to record measured runs:

- `scripts/grounded_live_probe.py`
- `scripts/grounded_live_probe_prompt.py`
- `scripts/measure_structured_corpus_state.py`
- `scripts/mutate_structured_evidence_recovery.py`
- `scripts/mutate_structured_evidence_replay.py`
- `scripts/structured-env-check.sh`
- `scripts/structured-live-env.sh`
- `tests/acceptance/`
- `tests/conformance/test_journey_acceptance_manifest.py`
- `tests/host/grounded_assessment_cases.py`
- `tests/host/grounded_acceptance_candidates.py`
- `tests/host/grounded_fixture_facts.py`
- `tests/host/test_grounded_assessment_acceptance.py`
- `tests/host/test_grounded_assessment_package.py`
- `tests/host/test_grounded_live_probe_selftest.py`
- `plans/structured-corpus-completion-execution.md`
- `plans/structured-corpus-execution-handoff.md`
- `plans/structured-corpus-test-handoff.md`
- `plans/structured-corpus-p3-prerequisites.md`
- `plans/2026-10-07-p3-prerequisites-closeout.md`

The operator's private release procedure:

- `.claude/rules/open-source-publish.md`

Product code, specs, ADRs, and ordinary unit tests still sync. Specs and ADRs that state a design decision stay publishable. The other development rules stay publishable as well: they state how this repository is developed, which is part of the project.

## Already in the release history

- fin already tracks `tests/eval/` and `src/fin/eval/`. The private corpus those tests read is not in the release tree.
- iknow-memory already tracks `eval/`, `gold-set/snapshot.json`, `src/core/eval/`, `src/core/extraction-eval/`, and the eval CLIs. The default `npm test` loads `eval/` and `gold-set/snapshot.json`. Do not delete those fixtures unless the tests that load them change in the same release commit.

Do not rewrite published history to purge these paths unless that rewrite is explicitly requested. Do not change repository visibility to public as part of a sync.
