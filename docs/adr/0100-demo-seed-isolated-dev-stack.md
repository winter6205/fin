# 0100. The demo seed targets an isolated dev stack selected by IKNOW_MEMORY_HOME

Date: 2026-10-15
Status: accepted
Authority: [`specs/demo-seed.md`](../../specs/demo-seed.md). Paired with iknow-memory ADR-0015.

## Context

The demo needs retrievable backend data, not only catalog metadata. `seed:demo` must load a demo corpus into the iknow-memory stores without touching the main store.

## Decision

1. `seed:demo` loads 20 fictional-company (ZEUS) enterprise documents into the isolated dev stack (PG 5434, ES 9201, Neo4j 7688): 3 employee handbooks, 3 product descriptions, 3 policy or process documents, 3 meeting minutes, 2 financial-report summaries, 2 contract templates, 2 FAQs, and 2 project weekly reports. Every page chunk carries an explicit page and section marker; chunk lengths vary and selected documents cross-reference each other.
2. The target is selected by `IKNOW_MEMORY_HOME`. It is added to `transport_env` (`src/fin/host/http.py`, `cli.py`) and to `INHERITED_ENV_ALLOWLIST` (`mcp_session.py:92-101`), so the Evidence route reads the same stack as ordinary retrieval.
3. Guard: require `IKNOW_MEMORY_HOME` to be set, not equal to `$HOME/.iknow-memory`, and ports to be 5434, 9201, 7688; if any condition fails, `exit 2`. The guard runs again before the worker starts.
4. Preview by default; write only with `--yes`. `--reset` truncates by Plan A's allowlist, deletes the ES indices, runs `MATCH (n) DETACH DELETE n` in Neo4j, and clears the fin catalog demo rows; it touches no docker container or volume.
5. The corpus and its 15 expected questions are entirely self-written. They cover all eight document categories, lexical / graph / global retrieval, a cross-document answer, and a follow-up turn. The SQL boundary question expects no fabricated invoice result from document prose. A separate positive SQL question reads four expected rows from a demo-owned synthetic `_test` contract ledger; only the `fin-serve` process receives its generated connection URL, and the URL is not persisted or passed to the browser runner. The corpus may go into the public mirror; no exclusion list is added.
6. The user-facing answer carries the evidence; the machinery does not. Run status, Runtime receipts and source evaluation are developer-mode or log-only, hidden by default. Citations are inline superscripts placed after the sentence they support, with a clickable sources list (document, section, page) at the end of the answer, and an explicit `资料中未找到相关内容` when the corpus does not support an answer. The presentation reuses fin's existing frontend components; no new UI framework.

## Consequences

- The main store's `index_jobs` count and states are unchanged before and after the seed.
- Running `--yes` again without `--reset` skips every source with 0 embed calls in the indexing stage.
- Live presentation acceptance is a browser check against the seeded dev stack and requires the isolated `_test` configuration and credentials.
- Positive SQL presentation acceptance also verifies the demo-owned read-only ledger through the real browser question and structured row citations.
