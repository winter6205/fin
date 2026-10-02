# F2 — Locked Python type-checking gate (Session F)

**Date:** 2026-09-30. **Basis:** F1 selection recorded in
[2026-09-30-f1-python-typecheck-comparison.md](2026-09-30-f1-python-typecheck-comparison.md).

## What landed

- `pyright==1.1.414` in the `dev` dependency group (`pyproject.toml`), locked in `uv.lock`
  (plus transitive `nodeenv`). Justification: F1's measured six-run comparison.
- Tracked configuration, single source: `[tool.pyright]` in `pyproject.toml` —
  `typeCheckingMode = "standard"`, `pythonVersion = "3.12"`,
  `include = ["src/fin/host", "src/fin/retrieve"]`. No excludes, no disabled rules.
- Developer command (documented in `README.md`):

  ```bash
  uv run --no-sync pyright
  ```

  Resolves the pinned checker from the project environment; no global install required
  (`command -v pyright` is absent on the verification host).

## Baseline → final

| Stage | Errors (host+retrieve) | Files w/ diags |
| --- | --- | --- |
| F1 baseline (1.1.414 standard) | 112 | 18 |
| Final, gate command | **0** | 0 |

All 112 diagnostics were resolved by type-level corrections in `src/fin/host` and
`src/fin/retrieve`; no blanket `# type: ignore` and no mass suppression was added.

## Exception ledger

- `src/fin/host/compile.py:470` — one rule-specific `# pyright: ignore[reportArgumentType]`.
  Root cause: the preparation-view Protocols in `graph.py` declare mutable members that a
  frozen dataclass cannot satisfy; a `cast` would fake conformance. Removing the ignore
  requires making those Protocol members read-only properties — deferred as an independent
  change to keep this diff runtime-inert.
- `src/fin/retrieve/tiers.py` — `RetrieveTier` restated as an explicit `Literal[...]` with
  an import-time drift pin that raises `RuntimeError` when the literal and
  `RETRIEVE_TIERS` disagree (runtime-raise form: `python -O` strips bare asserts;
  illegal variable unpacking was the upstream root cause of five consumer
  diagnostics; drift now fails loudly at import).
- `src/fin/host/feedback.py` — `_validate_value` returns the canonical literal
  (`"up"` / `"down"`) instead of the passed `value`: an input equal to `"up"` but of a
  `str` SUBCLASS is now stored as the plain literal. Behavior-equivalent for
  contract-conforming (plain-str) inputs; pinned by
  `tests/host/test_thumbs_feedback.py::test_feedback_2026_09_30_typing_pass_canonicalizes_str_subclass_value`.
- `src/fin/host/memory_store.py` — the `MemoryRow.hook` property gained a guard
  (`body = self.text if self.body is None else self.body`) mirroring the
  `__post_init__` invariant (body falls back to text) so the read is total even
  before normalisation; dead for every reachable input.

## Runtime-inertness policy and one real trap

Corrections were annotations, `cast`, asserts, plus a small set of expression
rewrites (a `_json_content` helper, canonicalized return literals, predicate
inlining), each individually behavior-equivalent for contract-conforming
inputs and test-pinned where observable. One candidate fix — widening tool parameters to
`Annotated[RunnableConfig | None, InjectedToolArg()]` — was reverted after measurement:
`langchain_core` matches the injected-config parameter by identity on `RunnableConfig`
(`langchain_core/tools/base.py:1721`), so the union silently disabled config injection
and broke eight committed visual-lane tests (`visual_claims` empty,
`visual_lane_unwired`).

## Verification (actually run)

| Check | Result |
| --- | --- |
| `uv run --no-sync pyright` (worktree, final source) | 0 errors, exit 0 |
| Fail-closed demo: disposable copy + one intentional `return "not an int"` in `src/fin/host` | the injected error reported, `1 error`, exit 1 |
| `uv run --no-sync pyright --version` | `pyright 1.1.414` from project env, exit 0 |
| Gold eval baseline `pytest -q -m eval tests/eval/test_gold.py` | 9 passed, exit 0 |
| `tests/retrieve` (hermetic) | 842 passed, 60 deselected, exit 0 |
| `tests/host/test_graph.py` + `test_graph_visual_lane.py` | 47 passed, exit 0 |
| `tests/host` attachment/search-mode/workspace_http targeted modules | all green (incl. ocr case under the extra-complete env) |
| Full `tests/host` | 2714 passed, 1 failed — the failure is `pytesseract is not installed` in the disposable venv (environment gap; passes under the dependency-complete env) |
| Pydantic contract probe on the tier field | valid set accepted; bogus and retired-letter tokens still `ValidationError` |

## Scope limits

The gate checks exactly `src/fin/host` and `src/fin/retrieve`. Strict-mode graduation,
third-party stub posture, and expansion to other packages are F1-recorded deferred
decisions. Static annotations do not replace runtime evidence-schema validation (Session E).
