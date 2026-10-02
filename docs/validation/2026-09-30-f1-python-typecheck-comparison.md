# F1 — Python Static Type Checker Comparison & Baseline

Decision slice for `plans/post321-python-typecheck.md` (Session F, task F1). No repo
dependency, config, source, or plan file was modified. Only this record was written.

## 1. Environment facts

| Fact | Value |
| --- | --- |
| Project requires-python | `>=3.12,<3.13` (`pyproject.toml`) |
| Tool used to run project | `uv 0.12.17` |
| Worktree interpreter for checks | `/home/winner/projects/fin/.venv/bin/python` = CPython 3.12.14 |
| Worktree under test | `/home/winner/projects/fin/.qoder/worktrees/session3-typecheck-q1` |
| Target scope | `src/fin/host` (71 `.py`), `src/fin/retrieve` (41 `.py`) → 112 files analyzed |
| Existing checker config / dep | None. `[dependency-groups] dev = ["pytest>=8"]`. No `[tool.pyright]`. |
| Repo git status after F1 | clean (no changes) |

### Dependency-resolution method used

The worktree has **no** `.venv`, and `uv sync` is out of scope (must not modify the repo
env). Checkers ran as disposable tools via `uv run --no-project --with <checker>==<pin>`,
and resolved fin's third-party dependencies through the already-present main-checkout
venv passed with `--pythonpath /home/winner/projects/fin/.venv/bin/python`. This venv is
used read-only; it is not installed into or modified.

- Third-party imports (`fastapi`, `langchain*`, `psycopg`, …) **resolve**: no
  `reportMissingImports` diagnostics appeared in any run.
- Some third-party libraries ship no inline annotations/stubs, so `--strict`/`all` modes
  report `reportMissingTypeStubs` (≈372–374). This is a resolved-but-untyped condition,
  not an unresolved import. It does not affect the non-strict baseline (count is 0 in
  `basic`/`standard` modes).
- First-party imports are resolved from the worktree itself because both target dirs are
  analyzed together with absolute worktree paths (see §4). The main venv's editable `fin`
  (which points at the main checkout) was never selected: 0 diagnostics were attributed
  to files outside the two target packages.

## 2. Pinned versions

| Tool | Pin (latest stable at run time) | Discovered via |
| --- | --- | --- |
| pyright | `1.1.414` | `uv run --no-project pip index versions pyright` |
| basedpyright | `1.40.1` | `uv run --no-project pip index versions basedpyright` |

`basedpyright 1.40.1` is built on the same pyright 1.1.414 core.

## 3. Reproducible invocation

Config lives only under `/tmp/f1` (settings-only JSON, no `include`). Targets are passed
as absolute worktree paths so first-party scope stays on the worktree. Reviewer rerun:

```bash
WT=/home/winner/projects/fin/.qoder/worktrees/session3-typecheck-q1
VENVPY=/home/winner/projects/fin/.venv/bin/python        # read-only 3.12.14 env for dep resolution
mkdir -p /tmp/f1/std && printf '{"typeCheckingMode":"standard","pythonVersion":"3.12"}\n' \
  > /tmp/f1/std/pyrightconfig.json

# SELECTED: pyright standard
uv run --no-project --with pyright==1.1.414 pyright \
  --project /tmp/f1/std/pyrightconfig.json --pythonpath "$VENVPY" \
  --outputjson "$WT/src/fin/host" "$WT/src/fin/retrieve"

# comparison runs: swap config typeCheckingMode + tool pin
#   pyright:       basic | standard | strict
#   basedpyright:  basic | recommended | all   (binary name: basedpyright, pin 1.40.1)
```

Mode selection requires a config file (no CLI flag for type-checking mode); `--level`
only filters severity. `basic`/`standard`/`strict` and `basic`/`recommended`/`all`
configs were each generated the same way under `/tmp/f1/{pr,bp}_<mode>/`.

## 4. Boundary & first-party findings

Measured across all six runs:

- **Files analyzed:** 112 every run.
- **Out-of-scope diagnostics:** 0 in every run — no diagnostic file lies outside
  `src/fin/host` or `src/fin/retrieve`. Imported first-party modules
  (`fin.host` → `fin.retrieve`, shared `fin.*`) do **not** escape the boundary; they are
  type-checked only where they appear inside the two target packages.
- **Unresolvable first-party imports:** 0 (`reportMissingImports` never fired for a
  `fin` module).
- **Third-party stub gaps:** surface only in strict/all modes as `reportMissingTypeStubs`
  (~372–374); imports themselves resolve.

## 5. Annotation coverage (measured, not copied from prior inventory)

AST measurement over the 112 files (`ast.FunctionDef`/`AsyncFunctionDef`; "fully
annotated" = return annotation present AND every non-`self`/`cls` named parameter
annotated):

| Package | files | functions | return-annotated | params annotated | fully annotated |
| --- | --- | --- | --- | --- | --- |
| `src/fin/host` | 71 | 1197 | 1183 (99%) | 2109/2133 (99%) | 1177 (98%) |
| `src/fin/retrieve` | 41 | 326 | 325 (100%) | 515/516 (100%) | 324 (99%) |

The prior inventory's "annotations on most functions" claim is confirmed. High annotation
density is why the non-strict baseline is small (~100) rather than thousands of
"unannotated" errors — but density does not mean the annotations are correct: the real
type conflicts in §6 are the actionable evidence.

## 6. Baseline diagnostics — totals by checker / mode

`files` = files carrying ≥1 diagnostic; `E`/`W` = error/warning counts from each tool's
own summary.

| Checker | Mode | errors | warnings | files w/ diags |
| --- | --- | --- | --- | --- |
| pyright | basic | 111 | 0 | 18 |
| pyright | **standard** (default) | **112** | **0** | **18** |
| pyright | strict | 1583 | 0 | 98 |
| basedpyright | basic | 111 | 0 | 18 |
| basedpyright | recommended (default) | 186 | 4344 | 107 |
| basedpyright | all | 4530 | 0 | 107 |

### Category breakdown — actionable candidates

`pyright standard` (112 errors, all severity=error, 0 noise):

| rule | count |
| --- | --- |
| reportArgumentType | 37 |
| reportReturnType | 37 |
| reportAttributeAccessIssue | 17 |
| reportInvalidTypeForm | 6 |
| reportOptionalMemberAccess | 5 |
| reportTypedDictNotRequiredAccess | 4 |
| reportCallIssue | 2 |
| reportOptionalOperand | 2 |
| reportIncompatibleMethodOverride | 1 |
| reportRedeclaration | 1 |

`basedpyright recommended` is dominated by inference/lint noise not present in pyright
standard: reportAny 1268, reportExplicitAny 796, reportMissingTypeStubs 374,
reportUnannotatedClassAttribute 286, reportUnknownMemberType 275,
reportImplicitStringConcatenation 270, reportUnknownArgumentType 232,
reportUnknownVariableType 201, reportUnusedCallResult 191 (4344 warnings). The underlying
"real" conflicts match pyright (reportArgumentType 56, reportReturnType 39,
reportAttributeAccessIssue 19, …) but are buried under ~4.4k non-blocking warnings.

`pyright strict` (1583) is mostly third-party stub gaps + inference completeness
(reportMissingTypeStubs 372, reportUnknownVariableType 281, reportUnknownMemberType 275,
reportUnknownArgumentType 260, reportUnnecessaryIsInstance 101, …) — the real conflicts
(argument/return/attr) are the same ~90 items visible in standard.

### `pyright standard` baseline by file (F2 work list)

| errors | file (relative to `src/fin/`) |
| --- | --- |
| 39 | host/graph.py |
| 27 | host/workspace_http.py |
| 8 | host/attachment_layout.py |
| 8 | host/visual_evidence.py |
| 4 | host/attachment_prepare.py |
| 4 | host/search_mode_decision.py |
| 3 | host/attachment_vision.py |
| 3 | host/attachment_visual_read.py |
| 3 | host/scripted_model.py |
| 2 | host/compile.py |
| 2 | host/memory_store.py |
| 2 | host/session_compaction.py |
| 2 | retrieve/fake_port.py |
| 1 | host/attachment_observations.py |
| 1 | host/feedback.py |
| 1 | host/filing_monitor.py |
| 1 | host/observability.py |
| 1 | host/thread_store.py |

All 112 errors concentrate in 18 files; `host/graph.py` and `host/workspace_http.py` hold
66 of them.

## 7. Selection decision

**Selected: pyright 1.1.414, `typeCheckingMode = "standard"`**, scope
`src/fin/host` + `src/fin/retrieve`, baseline **112 errors / 18 files, 0 warnings**.

Reasons:

1. Actionable exit-0 gate without broad excludes or blanket ignores (F1 AC). Standard's
   112 items are genuine type conflicts in 18 files — a bounded, real F2 work list.
2. No warning noise and no stub-noise: `reportMissingTypeStubs`/`reportUnknown*`/`reportAny`
   do not fire in standard, so a clean checkout can reach exit 0 by fixing real bugs only.
3. First-party boundary is intact and scope is exact (0 out-of-scope diagnostics), with the
   two target dirs as the only exclusion surface — no per-module excludes needed.
4. basedpyright is a superset fork on the same core; its strict-relevant defaults
   (`recommended`: 4530 diags incl. 4344 warnings; `all`: 4530 errors) cannot become a
   gate without disabling `reportAny`/`reportExplicitAny`/`reportUnknown*`/etc. — blanket
   rule-off, which the AC disallows. Its `basic` mode equals pyright `basic` (111), so it
   adds no value at the non-strict level we want.
5. pyright `strict` (1583) is mostly third-party stub/inference completeness, not real
   bugs; adopting it as the first gate would force mass `# type: ignore` or dependency
   stub pinning — contrary to the "no mass suppression" constraint.

`pyright standard` (the tool default since 1.1.x) is chosen over `basic` (111) because the
single extra rule (`reportIncompatibleMethodOverride`) is a legitimate correctness check
and the tool's default mode is the least-surprising config to lock in F2.

## 8. Open policy decisions (for F2 / maintainer)

- **Strictness level.** Locked at `standard` now. Whether to graduate toward `strict`
  later (after clearing 112) — and how to handle `reportMissingTypeStubs`/`reportUnknown*`
  noise (dependency stubs vs. targeted rules) — is a deferred decision, per plan.
- **Third-party stub posture.** langchain/fastapi/etc. ship partial typing; strict modes
  flag missing stubs (~372). Decide in F2 whether to pin stub packages, keep standard, or
  set specific `reportMissingTypeStubs=none`.
- **basedpyright `reportAny`.** Valuable for the Any-heavy host boundary but too noisy for
  a first gate; if wanted, adopt as individually-ruled opt-in, not blanket.
- **`py.typed`.** fin has no `py.typed` marker; out of scope for F1, note for whether the
  package should advertise typed-ness to downstream consumers.
- **Scope of excludes.** None required for the baseline; any future exclude must be a named,
  justified single file, never a broad package.
