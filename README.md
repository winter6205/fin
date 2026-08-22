# fin

Bounded 10-Q research agent. The model chooses tools in a loop; citation checks run in code; LangGraph is the durable runtime (checkpoint / resume), not a retrieve-then-generate flowchart.

This repository is the SSOT. Do not keep parallel copies under `projects/test`.

## Layout

| Path                                                     | What                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `docs/CONTEXT.md`                                        | Domain terms                                                                         |
| `docs/adr/`                                              | Accepted decisions (0001–0022)                                                       |
| `docs/research/002-demo-corpus-and-evidence-contract.md` | Inherited corpus/evidence contract (snapshot from iknow wayfinder)                   |
| `data/corpus/pdf/`                                       | Frozen 12 SEC 10-Q PDFs (AAPL, MSFT, AMZN)                                           |
| `data/corpus/manifest.json`                              | document_id, periods, sha256, upstream commit                                        |
| `data/upstream/`                                         | private fixture source LICENSE, sec-10-q README, `qna_data.csv` (upstream QA; not the v1 gold set) |

Upstream freeze: [docugami/private fixture source](https://github.com/docugami/private fixture source) `547f132`. Packaging license is MIT; filings are public 10-Qs.

## Status

v1 host-agent ships: ingest artifacts are in git; CLI (`python -m fin`) and HTTP `POST /ask` share one compiled host graph. Default model is `fin/MiniMax-M3` via `FIN_MODEL` + `FIN_BASE_URL` (ADR 0022).
