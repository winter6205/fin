# fin

**Enterprise knowledge-QA agent** (企业知识问答助手) — the 入口, not the 库: the operator asks in a **research thread** on the **工作台**; each turn is a **track**; tools and the **citation verifier** bind only to the **grounded** track. The first corpus is operator-held filings behind the **retrieve port** — a **source**, not the product. Citation checks run in code; LangGraph is the durable runtime (checkpoint / resume), not a retrieve-then-generate flowchart.

Product identity is SSOT'd in [`docs/CONTEXT.md`](docs/CONTEXT.md) and ruled in [ADR 0090](docs/adr/0090-product-identity-knowledge-qa-entry-point.md); the program contract is [`specs/enterprise-kb-agent.md`](specs/enterprise-kb-agent.md), mapped by wayfinder.

This repository is the SSOT. Do not keep parallel copies under `projects/test`.

## Layout

| Path                                                     | What                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `docs/CONTEXT.md`                                        | Domain terms **and product identity** (SSOT)                                         |
| `docs/adr/`                                              | Accepted decisions (0001–0090)                                                       |
| `docs/adr/0003-v1-inherits-002-thin-slice.md`            | Inherited corpus/evidence contract — page-level citations, refuse-when-missing       |
| `specs/enterprise-kb-agent.md`                           | Program contract: the five foci of the knowledge-QA agent                            |
| `data/corpus/pdf/`                                       | Frozen 12 SEC 10-Q PDFs (AAPL, MSFT, AMZN) plus a local GOOG `10-K` fixture          |
| `data/corpus/manifest.json`                              | document_id, periods, sha256, upstream commit                                        |
| `data/upstream/`                                         | private fixture source LICENSE, sec-10-q README, `qna_data.csv` (upstream QA; not the v1 gold set) |

Upstream freeze: [docugami/private fixture source](https://github.com/docugami/private fixture source) `547f132`. Packaging license is MIT; filings are public 10-Qs.

The archived 2026-08 「SEC 财报研究工作台」 research is session-only — git-ignored by design, so it is absent from a clone — and is **not** fin's current identity; [ADR 0090](docs/adr/0090-product-identity-knowledge-qa-entry-point.md) is the tracked ruling.

## Status

v1 host-agent ships: ingest artifacts are in git; CLI (`python -m fin`) and HTTP `POST /ask` share one compiled host graph. Default model is `MiniMax-M3` via `FIN_MODEL` + official MiniMax Chat Completions `FIN_BASE_URL` (ADR 0022). Soft-Track (ADR 0027, that pull request) routes each turn to `dialogue` / `clarify` / `off_topic` / `grounded`; only the grounded track binds tools and the citation verifier. Opt-in contracts: `uv run pytest -q -m soft_track`.

**NOW (2026-08-30):** the destination is **fin as an enterprise knowledge-QA agent** — wayfinder, contracted by `specs/enterprise-kb-agent.md` in five foci:

1. **Grounded answers in the workspace** — comparisons and numbers come from host tools, not free-prose tables; the citation verifier still gates any prose answer.
2. **Operable corpus on Corpus 首页** — grow the library past the frozen 12 — a local GOOG `10-K` fixture is the first such addition, then `8-K`, more issuers — through `list_files` filters and ingest.
3. **Work after the answer** — watchlist, in-app notices, comparison / citation export, stop.
4. **Multi-issuer synthesize** — fan out in the host graph, then synthesize; every surviving claim still passes the verifier; over-cap is a typed overflow.
5. **Trust in the workspace** — `evidence_locators` persisted on the run row, `GET /health`, and the eval count command.

The **工作台** web UI is the only product entry; SEC filings are the first corpus, not the product name. the tracked change and the tracked change stay useful as **workstream** notes (vertical slice, quality loop) and are superseded as the product destination. Naming rule from [ADR 0090](docs/adr/0090-product-identity-knowledge-qa-entry-point.md): the internal track enum is `grounded` (ADR 0061); product copy and `docs/CONTEXT.md` must not call fin a research agent. Opt-in eval: `uv run pytest -q -m eval` (9 gold cases).

## Running the workspace server (T1)

Install the `fin` entrypoint once, then a bare `fin` starts everything:

```bash
bash scripts/install-fin-shim.sh    # writes ~/.local/bin/fin (idempotent)
fin                                 # up + readiness verdict
```

Open **http://127.0.0.1:8000**. `fin up` brings up fin Postgres, the iknow-memory infra, `iknow-memory` on `PATH`, the SPA build, and `fin-serve` — then **reports whether the knowledge MCP actually became reachable**, so starting fin cannot silently leave the memory service behind.

The installed `fin` is a shim, not a copy: it hands `scripts/fin` the checkout it was installed from and lets the launcher do the walk-up from the current directory first, so a bare `fin` inside a worktree drives that worktree. From a directory with no checkout above it, the launcher falls back to the install-time checkout and says so on stderr: `fin: not inside a fin checkout; using <path>`.

| Command          | What it does                                                                 |
| ---------------- | ---------------------------------------------------------------------------- |
| `fin` / `fin up` | Start the whole stack, then print the readiness verdict                      |
| `fin check`      | Preflight only (docker daemon, checkout, shim) — starts nothing              |
| `fin status`     | Is it up, and is the knowledge MCP reachable?                                |
| `fin down`       | Stop serve + both compose projects (`--keep-infra` to spare the iknow infra) |
| `fin logs`       | Tail this checkout's serve log (`<checkout>/.fin/serve.log`)                 |

`up` / `check` / `status` / `logs` accept an optional checkout path, so a worktree can be driven from anywhere: `fin status ../fin-wt-search-mode-retrieve`. The path selects **which checkout's scripts run**; `status` still reads its verdict from the local port, of which there is at most one server. Without a path, the checkout is discovered from the current directory. `down` takes only its own flags and acts on the checkout under `$PWD`.

Needs Docker (daemon **running**, not just installed), Node, `uv`, `curl`, `jq`, a sibling `../iknow-memory` checkout (or `FIN_IKNOW_ROOT`), and `FIN_API_KEY` in `.env` for model calls. Ingest/retrieve embeddings need `GRAPHRAG_MEMORY_EMBED_API_KEY` in that iknow checkout's `.env.local`.

**Exit codes** (`check` / `status` report their own):

| Code | Meaning                                                             |
| ---- | ------------------------------------------------------------------- |
| `0`  | Ready — serve answers **and** `knowledge_mcp=reachable`             |
| `1`  | Usage error / not a fin checkout                                    |
| `2`  | Not ready — serve is not answering (down, or boot failed)           |
| `3`  | Gated — serve answers but `knowledge_mcp` is 缺 / 坏, so entry 409s |

`up` does **not** return 0/2/3. It ends in `exec uv run fin-serve`, so it inherits serve's exit status and runs in the foreground until you stop it; its readiness verdict is written to `<checkout>/.fin/ready` as `<code> <word>` (for example `0 ready`). `fin status` is the scriptable gate — use it in scripts, not `up`.

`WORKBENCH_SERVE_PORT` (default `8000`) is a **probe port, not a knob**. `fin-serve` hardcodes 8000 (`uvicorn.run(..., port=8000)`); the variable moves the port `fin status` and `fin down` probe, and `fin up` refuses to run when it is set to anything else. The test suite sets it to point the classifier at a stub server.

`fin down` resolves the iknow-memory checkout exactly as `fin up` does — both call the shared resolver in `scripts/lib/fin-root.sh`, which steps out to the main checkout when the current one is a linked worktree. It exits non-zero when a `compose down` fails or when the iknow checkout cannot be found, so a `down` that reports success has actually stopped both stacks.

The verdict is read from `GET /api/status`, and its classification lives in one place (`scripts/lib/readiness.sh`) shared by `fin status` and the boot reporter. An answer that is not the status payload — for example an HTML error page from whatever else holds the port — is classified **not ready**, never ready.

### Why the knowledge MCP is checked at all

`fin-serve` probes the iknow-memory MCP **once at boot** and publishes 缺 / 坏 / 可达 into an in-process snapshot (`fin.host.knowledge_mcp`). That snapshot gates conversation entry: with the default `mcp` adapter, 缺 / 坏 makes create / open / send return `409 knowledge_mcp_fault` for the whole process lifetime — the probe never re-runs.

A missing or unhealthy MCP is therefore a **WARNING, never a boot failure**: serve keeps running and serves the UI. `fin` surfaces the distinction up front rather than letting the first question fail:

- `missing` — the `iknow-memory` launch command is not on `PATH` (check the checkout and `node`).
- `fault` — the binary exists but `initialize` + `tools/list` failed; usually infra was cold at boot. Because the probe runs once, **restart after infra is warm**: `fin down && fin up`.

Boot order is load-bearing for the same reason: both compose stacks reaching HEALTHY _before_ `fin-serve` starts is what keeps a cold infra from being published as 坏.

Only `adapter=mcp` is gated. A `fake` / scripted boot publishes 缺 with the gate **open**, and `fin status` reports that as ready — classifying by state alone would report a false failure on the scripted lane.

Manual split (what `fin up` does, minus the verdict):

```bash
cd web && npm install && npm run build   # produces web/dist/
uv run fin-serve                          # serves API + SPA on :8000
```

Without a built `web/dist/`, only the API routes serve and boot logs a WARNING (`SPA dist directory ... not found`) — that warning is the signal to run the build step.

### Retrieve adapter (`FIN_RETRIEVE_ADAPTER`)

Retrieve sits behind the retrieve port (ADR 0032) and the adapter is chosen by `FIN_RETRIEVE_ADAPTER`, never inferred from the checkpointer:

| Value           | What it binds                                                                              |
| --------------- | ------------------------------------------------------------------------------------------ |
| `mcp` (default) | MCP client to the **iknow-memory** server, launched as `iknow-memory serve`                |
| `fake`          | In-memory `FakeRetrievePort` — boots without a corpus, answers every query with empty hits |

`fin-serve` preflights the default: with `iknow-memory` absent from `PATH`, boot logs a WARNING (`FIN_RETRIEVE_ADAPTER=mcp but the MCP server command ... is not on PATH`) and keeps serving — research turns then return the typed `retrieve_unavailable` EXIT instead of evidence. Install the server, or set `FIN_RETRIEVE_ADAPTER=fake` in `.env` to acknowledge a corpus-free boot.

### Browser e2e: scripted-provider lane (opt-in, credential-free)

The scripted-provider lane from the `specs/workspace.md` Amendment 2026-08-26. One command boots the workspace on a scripted transcript, drives a browser through send → stream → collapse → reload, tears the server down and exits on the verdict:

```bash
node scripts/run-scripted-e2e.mjs
```

Deterministic and offline: the server child is started with `FIN_SCRIPTED_MODEL=1` (the explicit opt-in read by `fin.host.scripted_boot`) and with `FIN_API_KEY` / `FIN_MODEL` / `FIN_BASE_URL` / `FIN_CHECKPOINT_URL` / `FIN_RETRIEVE_ADAPTER` removed from its environment, so no network LLM is reachable and no credential is read. The transcript quotes the frozen `AAPL-2022Q3` page it reads, so the run ends `done` with real citations; `FIN_SCRIPTED_DELTA_MS` (default 200) paces the deltas so the browser can watch the answer grow.

Exit codes: `0` every check passed, `1` a fidelity observable regressed (for example the answer arriving as one post-hoc dump instead of deltas), `2` the lane could not run (no SPA build, the server never came up, the harness is missing). Flags: `--port`, `--delta-ms`, `--boot-timeout`, `--keep-server`, plus `--poll-count` / `--poll-interval-ms` / `--browser` / `--json` / `--keep-open` forwarded to the runner. `web/dist` is built automatically when it is missing.

`FIN_SCRIPTED_MODEL` is never a fallback: `fin-serve` without it boots the configured provider, and a value that is neither truthy nor falsy stops the boot with a typed error rather than silently restoring the real model. Default `uv run pytest -q` invokes no browser — the lane lives in `scripts/`, not in `tests/`.

### Live-provider smoke lane (opt-in, requires a real credential)

The live-provider lane from the `specs/workspace.md` Amendment 2026-08-26. One command boots the workspace on the **configured** provider, sends one real question, reads the SSE wire the SPA reads, and reports the fidelity observables:

```bash
node scripts/run-live-smoke.mjs
```

`FIN_API_KEY` is required. Without `FIN_API_KEY` the lane prints which variable is missing and exits `3` having started nothing — no server, no browser, no provider call. That check lives in the lane rather than in the server on purpose: `fin.host.compile` resolves the key as `FIN_API_KEY or "sk-no-key-required"`, so an unset token would otherwise reach the provider as a placeholder and come back as a 401 indistinguishable from a revoked key or a wrong base URL. The token is read from the environment or from `./.env` (the file the server itself loads at boot) and its value is never printed. `FIN_SCRIPTED_MODEL` is removed from the server's environment, so a stray scripted opt-in cannot turn this lane into a fake pass.

Observables, each reported PASS / FAIL on its own line: answer deltas reach the wire before the terminal status (`1-answer-streamed`); exactly one typed terminal `status` frame, closed by `citations` (`2-terminal-status`); no `<think>` markup inside the answer (`3-answer-free-of-reasoning-markup`); citations that match the outcome — a finished research turn owes code-filled evidence, a `refuse` / `step_cap` / Soft-Track turn owes an empty list (`4-citations-match-outcome`); and the reasoning channel (`5-reasoning-channel`). A provider that emits no reasoning at all reports `NONE` rather than `PASS`, and `--require-thinking` turns that into a failure.

Exit codes: `0` every observable held, `1` a fidelity observable regressed, `2` the lane could not run, `3` `FIN_API_KEY` is missing. Flags: `--question`, `--model`, `--base-url`, `--port`, `--boot-timeout`, `--run-timeout`, `--require-thinking`, `--require-done`, `--browser-checks`, `--keep-server`, `--json` (`--help` lists them). `--browser-checks` additionally runs the browser lane below against the same live workspace; it expects a provider that actually calls tools, since the trace-collapse check reads tool rows.

Default `uv run pytest -q` never invokes this lane: it lives in `scripts/`, is reached only by the command above, and boots its own server on a free port.

### Browser e2e: workspace stream checks against a running workspace (opt-in)

The same checks against a workspace you started yourself (any provider). One command drives a browser over the served workspace and prints one PASS/FAIL line per check:

```bash
node scripts/run-stream-checks.mjs
```

Prerequisites:

- a workspace served at the target URL — `cd web && npm run build`, then `uv run fin-serve` (default target `http://127.0.0.1:8000`; `--base-url=URL` or `FIN_E2E_BASE_URL` points elsewhere).
- a global `playwright-cli` on `PATH` (`npm install -g @playwright/cli@latest`), or `PLAYWRIGHT_CLI=/path/to/playwright-cli`. `npx playwright-cli` is not used.
- no provider credentials are needed for the runner to start. With the workspace down or the model unreachable the checks report FAIL with the reason — they never skip and never read as a pass.

Exit codes: `0` every check passed, `1` a check failed or produced no verdict, `2` the harness itself could not run (binary missing, browser refused to open, snippet crashed, wall-clock timeout). `node scripts/run-stream-checks.mjs --help` lists the flags (`--poll-count`, `--poll-interval-ms`, `--require-stream-growth`, `--timeout-ms`, `--session`, `--browser`, `--keep-open`, `--json`).

Assertions live in `scripts/playwright-stream-checks.mjs`; the driver imports that ES module and evaluates the check function through `playwright-cli run-code`, so the function stays self-contained (no imports, no module-scope references).

### Eval lane: nine gold cases, and counting their outcomes

The nine gold cases (ADR 0012) stay opt-in — default `uv run pytest -q` deselects them:

```bash
uv run pytest -q -m eval               # the nine cases, pass/fail
uv run python scripts/eval_counts.py   # the same lane, plus done / refuse integers
```

### Python type checking (application source)

```bash
uv sync --all-extras --frozen   # one-time precondition: installs pyright (locked dev group) + the optional extras corpus imports
uv run --no-sync pyright        # pyright 1.1.414 from the dev group, config in [tool.pyright]
```

The gate checks **all application source**, `src/fin` — `host`, `retrieve`, `corpus`, and `eval` — in `standard` type-checking mode against Python 3.12. Scope and mode live only in `[tool.pyright]` in `pyproject.toml`.

`scripts/` and `tests/` are **not** in scope, so this is an application-source check, not a whole-repository check. It is a static check on the locked dev toolchain — no global install required, and it never replaces `uv run pytest` runtime validation. The web package keeps its own check (`cd web && npm run typecheck`, i.e. `tsc --noEmit`).

Use `uv run --no-sync pyright`, never a bare `.venv/bin/pyright`: only `uv run` puts the venv where pyright resolves imports, and running the binary directly reports a wall of bogus `reportMissingImports`.

#### The check also runs on commit

`.husky/pre-commit` runs the complexity check (`lint:s5:staged`) and then the type check (`typecheck:staged` → `scripts/pyright-staged.mjs`). The type check materializes the git **index** into a temp directory and points pyright at that copy, so a commit is gated on the bytes it actually records — fixing an error in the working tree without staging the fix does not get the commit through. Type errors, a missing interpreter, a missing `uv`, or a checker killed before it produced a verdict all fail the commit; the hook never warns and continues.

Scope of that guarantee — read before relying on it:

- It is a **local commit constraint**, not a remote gate. `git commit --no-verify` skips it, and it protects nothing in CI, on force-push, or in another clone.
- It needs `npm install` once per checkout. That is what runs `husky` and creates `.husky/_/`, which is where git actually looks. Without it `core.hooksPath` points at a directory that does not exist and **no hook runs at all, silently**.
- It reads the **staged** `pyproject.toml`, so a commit that narrows `[tool.pyright].include` is checked against the narrowed scope. Treat a scope change there as a reviewed change, never as a way to quiet a finding.

### Live retrieve 评测（opt-in）

The `live-retrieve-eval-wave` ([spec](../specs/live-retrieve-eval-wave.md), [plan](../plans/live-retrieve-eval-wave.md)) opt-in chain runs the existing ablation / concurrency harness against a real `iknow-memory` MCP retrieve port and a real LLM. Default `uv run pytest -q`, default ablation, and default scripted eval all stay on `contract_fake` / Fake adapters — none of these commands are reached by `pytest`, none auto-start iknow, and none inherit live credentials from operator `.env`. Every step below is a separate explicit invocation:

| Step                                    | Command                                                    | Pre-req                                                  | Behavior on failure                                                                                                                                                                           |
| --------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Verify T1 corpus provenance          | `uv run python -m fin.corpus.dataset verify-t1`            | `data/corpus/dataset/t1/` + `t1_manifest.json` committed | exit 1 (drift / missing text / query contract); exit 2 (manifest missing)                                                                                                                     |
| 2. Push T1 corpus into retrieve backend | `uv run python -m fin.corpus.chunk_and_push --adapter mcp` | reachable `iknow-memory` MCP server                      | typed per-sample failures recorded in `report.json`; the run never silently writes Fake results                                                                                               |
| 3. Live ablation                        | `uv run python -m fin.eval.ablation run --eval-set live`   | reachable iknow + T1 pushed (steps 1–2)                  | iknow unreachable → exit code `retrieve_unavailable` (named EXIT, never `{chunks: []}`)                                                                                                       |
| 4. L2 real concurrency                  | `uv run python -m fin.eval.concurrency l2 --llm real`      | `FIN_API_KEY` set + reachable iknow                      | missing `FIN_API_KEY` → exit 3 with skip JSON `{ "status": "skipped", "skip": { "code": "l2_credentials_missing" } }`; the run never falls back to Fake LLM and never reports as a green pass |

`--adapter` / `--llm` / `--eval-set` are the explicit opt-ins. `FIN_RETRIEVE_ADAPTER` is honored by the chunk-and-push step but not by ablation / concurrency, which gate on their own flags so the live path can never be reached through environment drift alone.

The verify step records an honest `known_gaps` list: every scene in the T1 scope (1–6) that has no committed frozen text is recorded under `t1_scene_missing_frozen_text` so the report never silently claims "all six scenes covered". A scene that DOES have text but is missing its human-checked query is a problem, not a gap — verify exits non-zero. The committed T1 manifest covers all six scenes, so the verify step currently reports zero gaps; the gap channel exists for the future addition of more scenes or the loss of one.

Product compile path (ADR 0039) is unaffected: the capability-off switches remain absent from production code, and live-mode opt-ins sit behind the same `--eval-set live` / `--llm real` gate that the existing eval lane already owns.

### Retrieve 探针（opt-in；空库 vs 映射丢）

`scripts/retrieve_probe.py` answers the two numbers the first closed loop turns on, through the **same adapter the product uses** (`MCPRetrieveAdapter` over the real stdio server), so the probe cannot drift from the product seam:

```bash
uv run python scripts/retrieve_probe.py "operating revenue"
uv run python scripts/retrieve_probe.py "operating revenue" --limit 5
uv run python scripts/retrieve_probe.py "revenue" \
    --command node --arg ../iknow-memory/dist/index.js
```

It prints exactly two lines on stdout — `n_chunks=<n>` and `mapping_failures=<n>` — so a shell can read the integers without parsing the envelope; the full envelope goes to stderr. `mapping_failures` is **this call's** drop count (the ledger's per-thread delta around the retrieve), not the process-wide total: with `FIN_MAPPING_FAILURE_LOG` replaying an earlier process's drops, or a concurrent run dropping its own hits on the same shared ledger, an absolute count would print a non-zero number beside `n_chunks=0` for a healthy empty collection. Reading the pair is the point: an empty **资料集合** is `n_chunks=0` **and** `mapping_failures=0` (the 2026-09-16 probe shape, `{"chunks": []}`, no `degraded`), whereas hits that the mapper dropped are `n_chunks=0` with a **non-zero** per-call delta. Collapsing those two into one number is exactly what the probe exists to prevent — and a process-wide total would reintroduce the collapse for any replayed log or overlapping run.

Exit codes: `0` the probe ran, `1` the port answered with a typed failure envelope (`{"error": "retrieve_unavailable"}` — still printed with both integers), `2` the probe could not run (no launch command resolved: the binary is not on `PATH`). This command is never reached by `uv run pytest`; the default suite keeps the Fake port.

The count command prints exactly two lines on stdout (`done=6`, `refuse=3`) and forwards pytest's own report to stderr, so a CI step can read the integers without parsing test output. The numbers are **observed**: `tests/eval/conftest.py` wraps the graph each case invokes and records that run's terminal status, so the tally reflects what the runs did, not what the assertions expect. No live LLM and no LLM-as-judge are involved — the lane runs `ScriptedChatModel` against the frozen corpus. Exit code is pytest's, so a red eval suite fails the command; a lane that reports no count line exits `2` rather than printing zeros.

### Process health (`GET /health`)

`GET /health` reports whether this process can reach the Postgres behind `FIN_CHECKPOINT_URL` (LangGraph's `PostgresSaver`, ADR 0015, and the workspace thread store):

```json
{
  "status": "degraded",
  "checkpoint": {
    "state": "unconfigured",
    "reason": "checkpoint_url_unset",
    "detail": "..."
  }
}
```

`state` is `reachable` / `unconfigured` (URL unset) / `unreachable` (URL set, Postgres did not answer). Product `fin-serve` does not start when the URL is unset (`MissingCheckpointURLError`); `unconfigured` remains for tests and for a process that imported the app without going through `serve()`. Both degraded states are a 200 with operator copy: a monitoring probe can tell "fin is down" from "fin is up without a database", which a blank 500 cannot. The body never echoes the URL — that string carries the operator's password.

This is a separate route from `GET /api/status`, which stays boot-scoped per ADR 0033 (retrieve reachability, adapter kind, model slug, corpus filing count) and grows no per-request database probe.

### Run counters (`GET /api/observability`)

`GET /api/observability` exposes the host process's observability counters (same process as `GET /health`):

```json
{
  "counts": { "done": 12, "refuse": 2 },
  "latency_ms": { "count": 14, "total": 91234.5, "average": 6516.75 },
  "rewrite_retry": { "count": 3, "exhausted": 1 }
}
```

- `counts` — terminal statuses of completed turns (`done` / `refuse` / `step_cap`), recorded when the turn's SSE stream ends.
- `latency_ms` — aggregate count / total / average of those turns' wall-clock duration.
- `rewrite_retry` — empty-hit rewrite retries the `retrieve` tool performed (`count`) and how many of those still ended on an empty hit after the one bounded rewrite (`exhausted`). The retry fires only on the exact miss shape `{"chunks": []}` — never on hits, typed errors, or invalid input — so a rising `count` means retrieval genuinely missed before the rewrite.

This is a counters-only surface by construction: no per-run data, no question / answer / URL text, no exception strings, no credentials. It is a third route, deliberately separate from `GET /api/status` (boot-scoped, ADR 0033) and `GET /health` (process-scoped); neither grows run-scoped counters.
