# Plan: 提交钩子接入 Pyright + 类型检查范围扩到 `src/fin`（2026-10-07）

**Goal:** 两步闭环。① 让已有类型检查在提交时执行（复用 `.husky/pre-commit`，保留现有复杂度检查，接入锁定版本 Pyright，硬失败）。② 把 `[tool.pyright]` 的检查范围从 `src/fin/host` + `src/fin/retrieve` 扩到整个 `src/fin`，先修完新增的 42 项诊断再扩范围。

**Scope freeze:** 不改 `settings.json`；不新增依赖；不改 `uv.lock`；不改 `pyproject.toml` 的依赖声明；`scripts/` 与 `tests/` 暂不纳入检查范围（因此结论只能称「应用源码全量检查」，不能称「全仓检查」）。

**Baseline（2026-10-07，worktree `fin-wt-pyright`，`uv sync --all-extras --frozen` 后实测）:**

- 当前门禁 `uv run --no-sync pyright`（config `include = host + retrieve`）→ **0 errors**，exit 0。
- 全量 `src/fin` → **42 errors**：corpus 40、eval 2、host 0、retrieve 0。
- 重要环境事实：直接执行 `.venv/bin/pyright` **不会**自动解析 venv，会产生 61 条假 `reportMissingImports`；唯一正确入口是 `uv run --no-sync pyright`（由 `uv run` 注入解释器）。钩子必须走这个入口。
- 规则分布：`reportArgumentType` 27、`reportAttributeAccessIssue` 9、`reportAssignmentType` 2、`reportOptionalMemberAccess` 1、`reportCallIssue` 1、`reportReturnType` 1、`reportFunctionMemberAccess` 1。

**affects (predicted before implementation; the actual diff is the authority — three predicted files ended up untouched):**

- `.husky/pre-commit`
- `pyproject.toml`（仅 `[tool.pyright].include`）
- `README.md`
- `scripts/pyright-staged.mjs` (new)
- `src/fin/corpus/chunking.py`
- `src/fin/corpus/determinism.py` (not predicted — the `canonical_json_bytes` signature was the real defect behind `push.py:208`)
- `src/fin/corpus/ingest.py`
- `src/fin/corpus/ingest_endpoint.py`
- `src/fin/corpus/ingest_state_model.py`
- `src/fin/corpus/ingest_state_postgres.py`
- `src/fin/corpus/live_fetch.py`
- `src/fin/corpus/params.py`
- `src/fin/corpus/parsers/_pages.py`
- `src/fin/corpus/rebuild.py`
- `src/fin/corpus/structure_units.py`
- `src/fin/eval/ablation/live_set.py`
- `src/fin/eval/concurrency/__main__.py`
- predicted but **unchanged**: `ingest_state_memory.py` (cleared by the shared `validate_document_id` annotation in the model), `ingest_worker.py` and `push.py` (cleared by fixing the unsound upstream declarations they hit, in `ingest_state_model.py` and `determinism.py`)

## Architecture gate (pre-implementation, ACR)

- bounded-context-guardian: yes — 钩子改动只碰 repo 级钩子与 `[tool.pyright]`，类型修复限定在 `corpus` / `eval` 两个既有包内，不新增目录、不跨包搬代码、不改 `host` / `retrieve`。
- input-contract-tests: yes — 钩子本身的输入契约（staged 内容 / 工具缺失 / 干净树）由 Step 1 的 boundary-testing 用真实 `git commit` 场景覆盖，不新增业务 API 故无新公开入参面。
- error-handling-enforcer: yes — 钩子失败路径必须硬退出（非零），不吞异常、不 `|| true`、不打印警告后继续；诊断信息原样透传给开发者。
- complexity-anti-drift: yes — 只在既有函数内做局部注解/收窄修复，不拆新抽象层、不引入共享 helper 模块，函数长度与嵌套不因本次改动增加。
- minimal-change-verifier: yes — 一个逻辑任务（类型门禁闭环），42 项诊断按模块并行修复但同属该任务；不顺带做 #336 类型债务清理，不引入新依赖。

**冲突处置:** 无冲突。五项全部 yes，允许进入实施。

## Tasks

1. **Step 1 — 钩子接入（`.husky/pre-commit` + `package.json` 脚本 + README）**
   保留 `lint:s5:staged` 复杂度检查；在其后追加 `uv run --no-sync pyright` 并硬失败。检查范围取暂存内容（staged），避免「暂存代码有错、未暂存修好」被放行。
2. **Step 2 — 修 42 项诊断（按模块派 implementer）**
   corpus 与 eval 分派独立 implementer；逐项修真实类型与边界约束。
3. **Step 3 — 扩大范围**
   `[tool.pyright].include` 改为 `["src/fin"]`，README 口径改为「应用源码全量检查」并说明 `scripts/` / `tests/` 不在范围。
4. **Step 4 — 验证、review、commit、push**

## Review findings that produced real code changes

The Spec-axis review surfaced two defects in the first version of `scripts/pyright-staged.mjs`. Both were fixed and re-verified:

1. **Fail-open on a killed checker.** `spawnSync` reports `status: null` when the child dies from a signal (OOM killer, supervisor SIGTERM), and `process.exit(null)` is `0` — so an OOM-killed pyright let the commit through with a "commit aborted" message printed. Reproduced end-to-end with a stand-in `uv` that `kill -KILL`s itself: exit `0`. Now caught explicitly → exit `1`.
2. **Temp-dir leak on every post-copy failure.** `abort()` called `process.exit()` from *inside* the `try`, which terminates without unwinding, so the `finally` cleanup never ran and each failing commit leaked a full checkout copy (~2900 files) into `/tmp`. Restructured so `abort()` throws a `HookAbort` and the process exits only after the `finally` has run. Verified `leaks=0` on the success, type-error, signal-kill, missing-`uv`, and missing-interpreter paths.

## Known limits (disclosed, not fixed)

- The hook reads the **staged** `pyproject.toml`, so a commit that narrows `[tool.pyright].include` is checked against the narrowed scope. This follows from the spec (check the staged tree) and is documented in `README.md`; it is a review-visibility limit, not an exploit path.
- `git add -N` (intent-to-add) makes `git checkout-index` emit 0-byte stubs for files the commit will not record. No exploitable false pass: an empty stub produces no diagnostic and is not committed anyway.
- `core.hooksPath` is `.husky/_`, which only exists after `npm install` (the `prepare` → `husky` step). Without it git silently runs no hook at all. Documented in `README.md`.
- Adding `list_audit` to the `@runtime_checkable` `IngestStateStore` Protocol narrows runtime `isinstance` semantics. Verified harmless today: both implementations and every test double (all subclass `InMemoryIngestStateStore`) already provide it, and the only two `isinstance(store, IngestStateStore)` assertions are against those concrete stores.

## Acceptance

- `uv run --no-sync pyright` exit 0，覆盖整个 `src/fin`。
- 临时副本注入受控类型错误 → `pyright` 与 `git commit` 均非零退出。
- 三场景通过：部分暂存（staged 有错 / unstaged 已修 → 仍失败）、工具缺失（→ 失败）、正常提交（→ 通过）。
- 类型修复若改变运行行为，必须先确认框架行为（FastAPI / LangChain 注解用途）并在报告中单独说明依据。
- `#336` 作为独立类型债务保留。
- 不 push 远端 PR 门禁：本钩子是本地提交约束，可被 `git commit --no-verify` 绕过。
