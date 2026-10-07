#!/usr/bin/env node
// Pre-commit type check: runs the locked pyright against the STAGED tree.
//
// Why the staged tree and not the working tree
// ---------------------------------------------
// `git commit` records the index. If we type-check the working tree, the commit
// is gated on bytes that will never be committed, and a real defect slips
// through whenever the developer fixes the error in the working tree but stages
// the broken version (or stages nothing). Materializing the index into a temp
// dir and pointing pyright at that copy checks exactly what this commit
// records.
//
// Fail-closed
// -----------
// Every path that does not positively observe "the staged tree is clean" ends in
// a non-zero exit. In particular a checker that dies from a signal reports
// `status: null`, and `process.exit(null)` is 0 -- so that case is caught
// explicitly instead of being treated as a pass.
//
// What it does NOT claim
// ----------------------
// This is a LOCAL commit constraint. `git commit --no-verify` skips it, and it
// is not a server-side / remote PR gate. It guards commits made through hooks
// in this checkout; it does not protect force-pushes, CI, or other clones.

import { execFileSync, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve, sep } from 'node:path'

const HOOK = 'typecheck:staged'

// Abort by throwing, never by calling process.exit(). process.exit() terminates
// immediately without unwinding, which would skip the temp-dir cleanup below
// and leak a full checkout copy on every failure path that happens after the
// copy is made.
class HookAbort extends Error {
  constructor(message, code = 1) {
    super(message)
    this.code = code
  }
}

function abort(message, code = 1) {
  throw new HookAbort(message, code)
}

function git(args, options = {}) {
  return execFileSync('git', args, { encoding: 'utf8', ...options })
}

let stageDir = null
let exitCode = 0
let failure = null
let successNote = null

try {
  const repoRoot = resolve(git(['rev-parse', '--show-toplevel']).trim())
  const worktreeRoot = process.cwd()

  // Nothing staged: there is no commit content to check, so the gate has no
  // work. `git diff --cached --quiet` exits 0 when nothing is staged, 1 when
  // there are staged changes, >1 on a real error.
  const stagedProbe = spawnSync('git', ['diff', '--cached', '--quiet'], { cwd: worktreeRoot })
  if (stagedProbe.error) {
    abort(`failed to inspect the git index: ${stagedProbe.error.message}`)
  }
  if (stagedProbe.status === 0) {
    successNote = 'nothing staged; no staged tree to type-check.'
  } else if (stagedProbe.status !== 1) {
    abort(`git diff --cached --quiet failed with status ${stagedProbe.status}; run this from a checkout.`)
  } else {
    // The locked interpreter: exactly the pyright from the dev dependency group,
    // no global install and no drift from `uv run --no-sync pyright`.
    const venvPython = join(repoRoot, '.venv', 'bin', 'python')
    if (!existsSync(venvPython)) {
      abort(
        `no interpreter at ${venvPython}. Install the locked environment first:\n` +
        `[${HOOK}]   uv sync --all-extras --frozen`,
      )
    }

    stageDir = mkdtempSync(join(tmpdir(), 'fin-typecheck-staged-'))

    // Materialize the index: exactly the bytes this commit will record.
    git(['checkout-index', '--all', `--prefix=${stageDir}${sep}`], { cwd: worktreeRoot })

    const stagedConfig = join(stageDir, 'pyproject.toml')
    if (!existsSync(stagedConfig)) {
      abort('the staged tree has no pyproject.toml, so there is no [tool.pyright] config to apply.')
    }

    // `--project` resolves `include` relative to the staged copy; `--pythonpath`
    // pins resolution to this checkout's venv, which lives outside the temp dir.
    const checked = spawnSync(
      'uv',
      ['run', '--no-sync', 'pyright', '--project', stagedConfig, '--pythonpath', venvPython],
      { cwd: worktreeRoot, stdio: 'inherit' },
    )

    if (checked.error) {
      if (checked.error.code === 'ENOENT') {
        abort('`uv` is not on PATH, so the locked type check cannot run. Install uv and retry.')
      }
      abort(`failed to launch the type check: ${checked.error.message}`)
    }

    if (checked.status === null) {
      abort(
        `the type check was killed by signal ${checked.signal ?? 'unknown'} before it ` +
        'produced a verdict, so the staged code was never proven clean',
      )
    }

    if (checked.status !== 0) {
      exitCode = checked.status
      failure = 'type errors in the STAGED code -- commit aborted.'
    } else {
      successNote = 'staged tree type-checks clean.'
    }
  }
} catch (err) {
  if (err instanceof HookAbort) {
    failure = err.message
    exitCode = err.code
  } else {
    failure = `unexpected failure: ${err?.message ?? String(err)}`
    exitCode = 1
  }
} finally {
  // Runs on every path, including every abort above.
  if (stageDir !== null) {
    rmSync(stageDir, { recursive: true, force: true })
  }
}

if (successNote !== null) {
  console.error(`[${HOOK}] ${successNote}`)
  process.exit(0)
}

console.error(`[${HOOK}] ${failure}`)
if (exitCode === 1) {
  console.error(`[${HOOK}] fix the problem, or commit with --no-verify to override.`)
}
process.exit(exitCode === 0 ? 1 : exitCode)
