# fin

Bounded 10-Q research agent. The model chooses tools in a loop; citation checks run in code; LangGraph is the durable runtime (checkpoint / resume), not a retrieve-then-generate flowchart.

This repository is the SSOT. Do not keep parallel copies under `projects/test`.

## Layout

| Path                                                     | What                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `docs/CONTEXT.md`                                        | Domain terms                                                                         |
| `docs/adr/`                                              | Accepted decisions (0001–0027)                                                       |
| `docs/research/002-demo-corpus-and-evidence-contract.md` | Inherited corpus/evidence contract (snapshot from iknow wayfinder)                   |
| `docs/research/003-enterprise-product-research-index.md` | Enterprise-product research index (004–009; not CONTEXT/ADR SSOT)                    |
| `data/corpus/pdf/`                                       | Frozen 12 SEC 10-Q PDFs (AAPL, MSFT, AMZN)                                           |
| `data/corpus/manifest.json`                              | document_id, periods, sha256, upstream commit                                        |
| `data/upstream/`                                         | private fixture source LICENSE, sec-10-q README, `qna_data.csv` (upstream QA; not the v1 gold set) |

Upstream freeze: [docugami/private fixture source](https://github.com/docugami/private fixture source) `547f132`. Packaging license is MIT; filings are public 10-Qs.

## Status

v1 host-agent ships: ingest artifacts are in git; CLI (`python -m fin`) and HTTP `POST /ask` share one compiled host graph. Default model is `MiniMax-M3` via `FIN_MODEL` + official MiniMax Chat Completions `FIN_BASE_URL` (ADR 0022). Soft-Track (ADR 0027, PR #57) routes each turn to `dialogue` / `clarify` / `off_topic` / `research`; only research binds tools and the citation verifier. Opt-in contracts: `uv run pytest -q -m soft_track`.

**NOW (2026-08-25):** harden the research vertical slice — ask filings → evidence → cited answer → reliable workspace UX. S1 corpus-retrieve landed in PR #79. Execution map: [fin agent wayfinder #91](https://github.com/winter6205/fin/issues/91). Recommended path memo: [`docs/research/009-optimized-agent-first-path.md`](docs/research/009-optimized-agent-first-path.md). Institutional memory / enterprise KB is later optional ([#92](https://github.com/winter6205/fin/issues/92)); filing research does not wait on it.

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
