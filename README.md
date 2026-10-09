# fin

fin is an enterprise knowledge-QA agent for questions over an operator-provided corpus. Its durable LangGraph host graph routes research threads through retrieval and source-reading tools. A code-level citation verifier accepts cited evidence only when it was returned during the current run. fin is the operator-facing entry point; the configured retrieval backend holds and serves the corpus.

The host uses LangChain `ChatOpenAI` with a configurable model (`FIN_MODEL`, default `MiniMax-M3`) and a LangGraph `PostgresSaver` for durable runs. Retrieval can use the `iknow-memory` MCP server behind fin's retrieve port. The workspace UI is served locally by the same FastAPI process as the host API.

## Repository map

- `src/fin/host/` — graph runtime, API, and workspace routes.
- `src/fin/retrieve/` — retrieval port and adapters.
- `src/fin/corpus/` — corpus ingestion and catalog capabilities.
- `web/` — workspace frontend.
- [`docs/CONTEXT.md`](docs/CONTEXT.md) — domain vocabulary and product identity.
- [`docs/adr/`](docs/adr/) and [`specs/`](specs/) — architecture decisions and historical contracts.

## Run locally

Prerequisites: Python 3.12, `uv`, Node.js, a running Docker daemon, and an `iknow-memory` checkout (set `FIN_IKNOW_ROOT` if it is not beside this checkout). Put provider credentials such as `FIN_API_KEY` in your local environment or ignored `.env` file; do not commit secrets.

```bash
uv sync --all-extras --frozen
(cd web && npm ci && npm run build)
bash scripts/fin check
bash scripts/fin up
```

The API listens on `0.0.0.0:8000`; open the workspace at `http://127.0.0.1:8000` on the same machine. Run `bash scripts/fin down` to stop the local services. Configure model and checkpoint settings with `FIN_MODEL`, `FIN_BASE_URL`, and `FIN_CHECKPOINT_URL` as needed.

## Corpus and validation

The public distribution omits the private corpus, extracted ingestion artifacts, and corpus-based fixtures. Corpus-dependent tests need the maintainer fixture bundle outside the checkout. Its root contains `values/<sha256>.json` files used by `FIN_PRIVATE_FIXTURES` and `data/corpus/` with the historical manifest and extracted artifacts. No data source is bundled or fetched automatically.

Set the fixture root and make its corpus visible at the path expected by the historical eval. Run the symlink command only if `data/corpus` does not already exist; `ln -s` will fail rather than replacing an existing path.

```bash
export FIN_PRIVATE_FIXTURES=/path/to/maintainer-fixtures
mkdir -p data
ln -s /path/to/maintainer-fixtures/data/corpus data/corpus
```

Without the private fixture bundle, corpus-dependent tests report a missing-fixture error instead of substituting data. This repository does not claim those tests pass without the fixtures.

The following focused checks do not require the private corpus fixtures:

```bash
uv run --frozen pytest -q \
  tests/test_repo_hygiene.py \
  tests/host/test_jev_config.py \
  tests/retrieve/test_jsonl_ledger.py \
  tests/corpus/test_private_fixtures.py
```

To run only the historical gold eval cases (rather than all opt-in eval and live-service tests), configure the fixture bundle above, then run:

```bash
uv run --frozen pytest -q -m eval tests/eval/test_gold.py
```

Live retrieval diagnostics and provider-backed checks are opt-in. The [retrieve probe](scripts/retrieve_probe.py) and [live-provider smoke lane](scripts/run-live-smoke.mjs) use configured services; they are not run by the focused pytest command above.

## License

Original fin code is licensed under the MIT License; see [`LICENSE`](LICENSE). Third-party software and materials retain their own applicable license terms.
