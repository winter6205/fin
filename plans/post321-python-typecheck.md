# Plan: Scoped Python type-checking command

**Goal:** Add a reproducible static type-checking command for fin's Python host and retrieve boundaries, selected from measured repository evidence and installed as a development tool.

**Approach:** First compare supported checker candidates against the same bounded code scope using disposable, explicitly versioned environments. Record the initial diagnostic baseline and choose one checker and mode. Then add that checker to the development dependency group, update the lockfile, resolve the actionable in-scope diagnostics, and document one clean-checkout command. Keep the first gate scoped to `src/fin/host` and `src/fin/retrieve`; expansion beyond those packages is a later decision.

**Relationship to other work:** This is an independent quality lane. It can start now, does not block #330, and need not wait for Session E or the tracked change/the tracked change/the tracked change. Runtime evidence-schema validation in [Session E](post321-evidence-schema-route.md) remains required: Python annotations cannot validate untrusted JSON. The web package continues to use its existing strict `tsc --noEmit` command.

**Repository evidence:** Python 3.12 and uv are the project runtime/tooling. The `dev` dependency group currently contains pytest only, with no Python checker configuration or dependency. The web package already exposes `typecheck` as `tsc --noEmit`. A prior read-only inventory found annotations on most functions in the proposed Python scope; annotation coverage does not establish that the annotations are correct or checked.

**Ownership:** fin owns the checker configuration, the host/retrieve scope, and any small source corrections needed to clear real diagnostics in those packages. The person implementing F1 records the comparison; the implementer of F2 adds the selected tool and lock update. Do not broaden this issue into unrelated Python packages or runtime evidence-schema behavior.

**ACR for this plan artifact:** N/A — this file changes no executable code or module boundary. Before implementation, run `arthurpower:architecture-change-reviewer` against the actual diff; implementation is gated on five `yes` or `N/A with reason` verdicts.

```text
bounded-context-guardian: N/A — this plan does not change module boundaries.
input-contract-tests: N/A — this plan adds no application API; F2 verifies the developer command's selected scope and fail-on-error behavior.
error-handling-enforcer: N/A — this plan adds no application error path.
complexity-anti-drift: N/A — this plan adds no executable logic.
minimal-change-verifier: yes — the dependency must be justified by F1 evidence and any dependency change must include its matching lockfile update.
```

**Per-change implementation loop:** For source behavior corrections, write a failing test first, then make the smallest correction and run targeted tests. For tooling-only changes, verify lock consistency, run the exact command from a clean environment, and demonstrate that a controlled in-scope type error makes that command fail. Review each logical code change before landing it.

## Session F — Python type-checker adoption

**Proposed issue title:** `Add a scoped Python type-checking gate`

**Issue goal:** Establish one deterministic, locked, documented Python type-checking command for `src/fin/host` and `src/fin/retrieve`, with an initial baseline that does not hide broad portions of the scope.

**Issue start / dependencies:** Start independently now. F1 does not modify the project dependency files. F2 depends on F1's recorded checker choice, pinned version, mode, command, and diagnostic baseline. This issue is not a native blocker of the tracked change.

### Tasks (ordered by dependency)

1. **F1 — Select and baseline the Python checker** — tag: `[decision]`
   - **Inherits:** Python 3.12 and uv are the project runtime/tooling. `pyproject.toml` has no Python checker configuration or dependency; `web/` already has a separate TypeScript check.
   - **Surface:** `src/fin/host`, `src/fin/retrieve`, and the checker configuration choices that affect those targets.
   - **Acceptance:** Compare Pyright and basedpyright against the same host/retrieve scope in disposable, explicitly versioned environments. Record each tool version, exact reproducible invocation, mode/configuration, total and categorized diagnostics, and whether imported first-party modules escape the intended boundary. Select the checker and mode that provide an actionable gate without broad excludes or blanket ignores. Record any unresolved policy choice as an explicit decision. Do not change `pyproject.toml` or `uv.lock` in this decision slice.
   - **Completion:** A reviewer can reproduce the comparison and understand the selected tool, initial error baseline, checked-file scope, and reasons for the choice.
   - Status: [x] done — measured comparison and selection in [docs/validation/2026-09-30-f1-python-typecheck-comparison.md](../docs/validation/2026-09-30-f1-python-typecheck-comparison.md) (pyright 1.1.414, standard mode)
   - [parallel with the tracked change/the tracked change/the tracked change and Session E]

2. **F2 — Add the locked Python type-checking gate** — tag: `[implementation]`
   - **Inherits:** F1's selected checker, pinned version, mode, command, target scope, and diagnostic baseline. The checker belongs in the development dependency group, not runtime dependencies.
   - **Surface:** `src/fin/host`, `src/fin/retrieve`, the development dependency group and lockfile, tracked checker configuration, and developer command documentation. Source changes are limited to minimal corrections needed to resolve in-scope diagnostics.
   - **Acceptance:** A clean checkout installs from the committed lock and runs one stable documented command that checks the agreed targets and exits 0. Resolve in-scope diagnostics or use individually justified narrow exceptions; do not add blanket `type: ignore` directives, broad module exclusions, or mass suppressions. Demonstrate fail-closed behavior by running the command against a disposable copy with an intentional in-scope type error and observing a nonzero exit. Pair any dependency change with its matching `uv.lock` update and retain F1's evidence. The command must not require a globally installed checker. Defer coverage expansion beyond host/retrieve to a later decision after this gate is stable.
   - **Completion:** A fresh environment can reproduce a clean result from the lockfile, a controlled type error is rejected, and maintainers can see exactly which Python boundary the gate checks.
   - Status: [x] done — gate and evidence in [docs/validation/2026-09-30-f2-python-typecheck-gate.md](../docs/validation/2026-09-30-f2-python-typecheck-gate.md)
   - [blocks: F1]

## Parallelism and tracker handoff

Issue owns Session F with F1 and F2 as ordered internal tracer bullets. Keep it independent from the tracked change and Session E. Do not open one issue per tracer bullet. F1 can run while the existing SQL, document-ingest, and reproducibility sessions proceed; F2 starts once F1 is recorded. Each source correction should remain a separately reviewable logical change within the issue.

## Completion evidence and limits

Record the selected tool and version, the comparable initial diagnostics, the exact clean-environment command and its output, the controlled negative-check result, and the final checked-file scope. The Python type-checking gate does not replace runtime input validation, source-specific evidence verification, tests, or TypeScript checking. This plan does not claim that a Python checker is currently installed or configured.
