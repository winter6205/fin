# Testing Rules

## Behavior coverage

When adding or changing business behavior, add or update tests before implementation. Cover each applicable behavior class:

1. Normal behavior.
2. Failure behavior.
3. Boundary conditions.
4. Insufficient permissions.
5. Empty or invalid input.
6. Concurrent or duplicate submissions, when applicable.

Derive expected results from the relevant spec, ADR, or plan. Keep expected values independent of the implementation under test; tests must assert the promised behavior, not only that the code ran.

## Representative fin data

When behavior depends on filing content, document structure, or fin's cross-module contracts, use a committed, versioned representative fixture from `data/corpus/` or the existing test fixtures. Keep the fixture small enough for routine tests, but preserve the real shapes that affect the behavior. Assert results against the relevant acceptance criteria in `specs/`, `plans/`, or `docs/adr/`; do not replace a relevant real-data case with invented toy input unless the test is specifically about an isolated edge case.

## Real dependency boundaries

The default `uv run pytest -q` suite must remain hermetic: do not require external network access, a live iknow-memory backend, or live model credentials. Use the default Fake retrieve and Jev clients for deterministic contract tests, as described in `docs/CONTEXT.md` and `tests/conftest.py`.

For a change whose acceptance crosses a real external boundary, use the corresponding opt-in acceptance suite defined by its spec or plan. Keep fixture-server MCP protocol smoke distinct from a round-trip against a live iknow-memory process: `tests/retrieve/test_mcp_smoke.py` verifies the protocol against a local fixture server; it does not prove the live backend works. Relevant live retrieve/ingest and launcher acceptance suites are under `tests/retrieve/` and `tests/host/`.

The Jev live round-trip is required when implementing or changing the Soft-Track or search-mode Jev product path, as required by ADR 0080 and ADR 0081. It is not a requirement for unrelated changes. A missing live prerequisite for a required acceptance is blocked evidence: do not report it as passed, and do not replace it with a Fake pass or a skip.

## Preserve test strength

Do not delete tests or weaken assertions to make a build pass. A removed or replaced test must be covered by an equal or stronger test and include the reason, authorization, and explanation in the commit body. Do not turn a failing test into a skip unless explicitly authorized and the reason is recorded.

## Validation record

If required validation cannot run, record:

```text
Validation:
- Not run: <reason>
- Expected command: <command>
- Blocking issue: <issue>
```
