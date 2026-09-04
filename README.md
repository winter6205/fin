# fin

**Workspace knowledge-base Agent** (企业知识库 Agent): the operator asks in a **research thread**; each turn is a **track**; tools and the **citation verifier** bind only to the **research track**. The first corpus on disk is operator-held SEC filings — a **source**, not the product. Citation checks run in code; LangGraph is the durable runtime (checkpoint / resume), not a retrieve-then-generate flowchart.

Product identity is SSOT'd in [`docs/CONTEXT.md`](docs/CONTEXT.md); the program contract is [`specs/enterprise-kb-agent.md`](specs/enterprise-kb-agent.md), mapped by wayfinder.

This repository is the SSOT. Do not keep parallel copies under `projects/test`.

## Layout

| Path                                                     | What                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `docs/CONTEXT.md`                                        | Domain terms **and product identity** (SSOT)                                         |
| `docs/adr/`                                              | Accepted decisions (0001–0027)                                                       |
| `specs/enterprise-kb-agent.md`                           | Program contract: the five foci of the knowledge-base Agent                          |
| `docs/research/002-demo-corpus-and-evidence-contract.md` | Inherited corpus/evidence contract (snapshot from iknow wayfinder)                   |
| `docs/research/003-enterprise-product-research-index.md` | Enterprise-product research index (004–009; not CONTEXT/ADR SSOT)                    |
| `data/corpus/pdf/`                                       | Frozen 12 SEC 10-Q PDFs (AAPL, MSFT, AMZN)                                           |
| `data/corpus/manifest.json`                              | document_id, periods, sha256, upstream commit                                        |
| `data/upstream/`                                         | private fixture source LICENSE, sec-10-q README, `qna_data.csv` (upstream QA; not the v1 gold set) |

Upstream freeze: [docugami/private fixture source](https://github.com/docugami/private fixture source) `547f132`. Packaging license is MIT; filings are public 10-Qs.

## Status

v1 host-agent ships: ingest artifacts are in git; CLI (`python -m fin`) and HTTP `POST /ask` share one compiled host graph. Default model is `MiniMax-M3` via `FIN_MODEL` + official MiniMax Chat Completions `FIN_BASE_URL` (ADR 0022). Soft-Track (ADR 0027, that pull request) routes each turn to `dialogue` / `clarify` / `off_topic` / `research`; only the research track binds tools and the citation verifier. Opt-in contracts: `uv run pytest -q -m soft_track`.

**NOW (2026-08-30):** the destination is **fin as an enterprise knowledge-base Agent** — wayfinder, contracted by `specs/enterprise-kb-agent.md` in five foci:

1. **Grounded answers in the workspace** — comparisons and numbers come from host tools, not free-prose tables; the citation verifier still gates any prose answer.
2. **Operable corpus on Corpus 首页** — grow the library past the frozen 12 (`10-K`, `8-K`, more issuers) through `list_files` filters and ingest.
3. **Work after the answer** — watchlist, in-app notices, comparison / citation export, stop.
4. **Multi-issuer synthesize** — fan out in the host graph, then synthesize; every surviving claim still passes the verifier; over-cap is a typed overflow.
5. **Trust in the workspace** — `evidence_locators` persisted on the run row, `GET /health`, and the eval count command.

The workspace web UI is the only product entry; SEC filings are the first corpus, not the product name. the tracked change and the tracked change stay useful as **workstream** notes (vertical slice, quality loop) and are superseded as the product destination. Naming rule from the tracked change: the internal `research` track enum may stay (rename cost), but product copy and `docs/CONTEXT.md` must not call fin a research agent. Opt-in eval: `uv run pytest -q -m eval` (9 gold cases).

## Running the workspace server (T1)

`fin-serve` binds `0.0.0.0:8000` and serves both the API routes and the built SPA same-origin. The SPA is NOT committed — it is a build artifact:

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
