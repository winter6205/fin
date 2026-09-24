# G4 T1 durable text attachment — fin (2026-09-25)

Validation record for plan [`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T1 (Durable text attachment through a thread)** on branch
`feat/g4-t1-durable-text-attachment`.

Identity of what was measured:

- Base (spec/ADR/docs only): `7a30e84`.
- Implementation commits under review: `b1a97bf` (host persistence), `19f9b27` (web status surface).
- This record is written after the dual-axis code review and its repair round;
  the repair is the commit that adds this file.
- Worktree: `/home/winner/projects/fin-wt-g4-t1`.

Scope honesty: T1 owns the durable text-attachment slice only. Classified
multi-format preparation (T2), thread-bound retrieval selection (T3), typed
bounded `read_source` (T4), and `unit` citations plus legacy `page` removal
(T5) are **not** delivered here, so spec Success Criteria 5–12 and the SC12
committed G4 gold set are not closed by this record.

## Commands and results

Default-suite runs are hermetic. Postgres-backed runs use a throwaway
container the operator started for this ticket
(`fin-g4-t1-pg`, `postgres:16`, host port 5433 → container 5432, DEV-only
credentials from the committed dockerfile, dropped after the run); it is not
the project's `FIN_CHECKPOINT_URL` service.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `FIN_CHECKPOINT_URL=<throwaway pg> uv run pytest -q tests/host/test_thread_attachment_durable.py tests/host/test_thread_attachment_durable_postgres.py tests/host/test_attachment_store.py` | 0 | 77 passed, 0 failed, 0 skipped |
| 2 | `uv run pytest -q tests/host/test_thread_attach.py tests/host/test_agent_instructions.py tests/host/test_attachment_index.py tests/corpus` | 0 | 613 passed, 0 failed, 0 skipped |
| 3 | `uv run pytest -q tests/host/test_workspace_http.py` | 0 | 91 passed, 0 failed, 2 skipped |
| 4 | `cd web && npx tsc --noEmit` | 0 | no type errors |
| 5 | `cd web && npx vitest run` | 0 | 24 files, 326 tests passed |
| 6 | `uv run pytest -m mcp -v tests/retrieve/test_live_research_cite_on_ingest.py tests/retrieve/test_live_mcp_read_source.py tests/retrieve/test_live_corpus_ingest.py` | 0 | 6 passed (real iknow-memory process) |
| 7 | `uv run pytest -q -m mcp tests/retrieve --ignore=tests/retrieve/test_live_search_modes_pg_es.py` | 1 | 31 passed, 1 error (blocked prerequisite, item B1 below) |

The two skips in run 3 are environment gates, not passes:
`test_workspace_http.py:1110` requires `FIN_CHECKPOINT_URL` from the project
service (deliberately unset for that run so it would not collide with the
throwaway container), and `test_workspace_http.py:1321` needs the gitignored
`web/dist` build.

## T1 acceptance, per case

Every row is an executed test id from runs 1–3, with what it observed.

| Plan T1 acceptance clause | Case | Observed |
| --- | --- | --- |
| Upload text bytes → logical id + `parsing`; `ready` only once index data exists | `test_upload_returns_parsing_and_becomes_ready_with_searchable_index` | POST answers `parsing`; status read is `ready`, `searchable: true`, `unit_count >= 1` |
| `ready` never reported stale | `test_an_unprepared_attachment_never_reads_as_ready`, `test_the_list_door_reports_a_row_that_lost_its_index_as_parsing` | A published row whose index was cleared reads `parsing` / `searchable: false` on both doors |
| Same thread inspects status on another turn | `test_thread_lists_its_own_attachments`, `test_a_later_turn_can_reference_the_same_attachment_again` | Second send referencing the same id succeeds; id is not consumed |
| Cross-thread id reveals nothing | `test_another_thread_cannot_read_or_reference_the_attachment` | Another thread's id yields the named `attachment_not_found` on read and send, with no text or hit |
| Restart retains bytes and parsed version, index usable before `ready` | `test_restart_keeps_bytes_and_version_and_restores_index_before_ready`, `test_ready_attachment_is_read_back_by_a_new_process`, `test_a_parsing_row_is_resumed_by_the_next_process`, `test_a_lost_index_is_rebuilt_at_the_same_version` | Fresh store instances over real SQL rows rebuild at the **same** version; `search(postings, "revenue")` hits unit 1 again |
| Failed/incomplete rebuild is a named non-ready state, never an empty hit set | `test_object_store_outage_during_preparation_is_typed`, `test_rebuilding_without_its_bytes_names_the_exit`, `test_whitespace_attachment_fails_with_a_named_reason` | `failed` + `source_unavailable` / `attachment_empty_text`; rebuilt row keeps no index postings |
| Thread delete removes metadata, bytes, units, index | `test_deleting_the_thread_removes_metadata_bytes_units_and_index`, `test_deleting_the_thread_removes_every_attachment_row` | Raw SQL counts `(fin_thread_attachment, _unit, _index) = (0, 0, 0)` via FK cascade |
| Delete racing preparation cannot resurrect an index | `test_delete_during_preparation_cannot_resurrect_the_index` | Late derived write raises `AttachmentThreadGoneError`; no index row survives |
| Cleanup failure typed, observable, retriable | `test_cleanup_failure_is_typed_observable_and_retriable` | 500 `attachment_cleanup_failed`, thread intact, retry succeeds |
| Old `attachment_id` after delete | `test_deleted_attachment_id_yields_unknown_locator_on_a_new_thread` | Reuse yields the unknown-locator surface, never another thread's text |
| Concurrent retry creates at most one active index version (SC11 concurrency class) | `test_concurrent_repreparation_publishes_one_index_version` | Four barrier-released preparations converge on one indexed version whose postings serve the expected hit |
| The one-use attachment assertion is replaced by the thread-lifetime contract | `test_attachment_remains_referenceable_for_the_thread_lifetime` | Replaces the retired single-use case; the id stays referenceable for the thread |
| Public-entry input classes (empty, invalid, overflow, unknown, concurrent) | `test_empty_body_is_rejected`, `test_non_utf8_body_is_rejected`, `test_unknown_thread_upload_is_404`, `test_unknown_attachment_status_is_typed`, `test_every_attachment_door_answers_with_one_shape` | Named 400/404 codes; all four attachment doors return one field set with an integer `version` |
| Model-facing copy matches the thread-lifetime contract | `test_attachment_context_message_states_the_thread_lifetime` | Block states the file belongs to the thread and no longer claims the text vanishes next turn |
| Web send gate has one reference authority | `uploadDoors.test.ts` → `send gate: what a send may reference` (3 cases) | Only `ready` ids reach the wire, so an unrelated question is never refused; every withheld record is returned for display instead of being dropped |

Static sweep: no bare `except` without a named outcome in
`attachment_prepare.py` / `attachment_store.py` (each swallow carries an
`EXIT:` comment naming the condition), no `attachments.store` reach-through
left in the HTTP routes, and no test-only helper added to a public barrel.

## Real-boundary run 6 — iknow-memory MCP

Dependency configuration: real `node` process over
`/home/winner/projects/iknow-memory/dist/index.js`, `storage: "memory"`,
MCP stdio transport, scripted model. Dataset: committed
`data/corpus/manifest.json` fixtures.

| Case | Observed |
| --- | --- |
| `research_done_cites_this_runs_retrieved_text` | PASS |
| `ungrounded_answer_over_the_same_live_evidence_is_not_done` | PASS |
| `test_live_mcp_read_source` (3 cases) | PASS |
| `test_live_corpus_ingest` (1 case) | PASS |

What this proves for T1: the live MCP push/retrieve/`read_source` path still
behaves after the attachment slice landed, i.e. the new thread-attachment
layer did not disturb the company-source boundary. What it does not prove:
attachment semantics. T1's thread-attach door resolves no body through MCP by
contract, and that is pinned hermetically by
`test_attach_does_not_trigger_mcp_read_source` (a spy on
`MCPRetrieveAdapter.read` asserts zero calls). No G4 attachment gold case over
the real fin object store plus live iknow-memory store exists yet; that is
SC12's end-of-round obligation.

## Blocked items (recorded, not substituted)

- **B1** `tests/retrieve/test_live_host_graph_hop.py` — error, not failure:
  it requires the iknow-memory `pg_es` cluster (`FATAL: role "iknow_memory"
  does not exist` on localhost:5432). Verified to fail identically in the
  untouched main checkout, so it is a missing local prerequisite, not a T1
  regression. Expected command: bring up the project docker-compose stack,
  then `uv run pytest -q -m mcp tests/retrieve/test_live_host_graph_hop.py`.
- **B2** `tests/retrieve/test_live_search_modes_pg_es.py` — not run: no
  Elasticsearch on `127.0.0.1:9200`. Same prerequisite family as B1.
- **B3** Not run: the 12 `tests/host/test_fin_launcher` cases (host lacks
  `jq`) and `tests/test_compile.py`'s compose case (no docker compose plugin).
  Both fail identically on the untouched main checkout; unrelated to T1.
- **B4** Not run: browser-level check of the Composer attachment chips. The
  web suite runs in a node environment with no DOM, so chip rendering,
  the withheld-attachment line, and the retry control are covered at the
  store/api seam only. Expected command: `cd web && npm run dev` plus a manual
  upload→parse→send pass.
- **B5** Not closed: SC12's committed G4 gold set (PDF-text, scanned PDF,
  image, 8,000-code-point continuation, cross-thread denial at retrieve,
  attachment/company comparison) — belongs to T2–T5 and the end-of-round run.

## PR base state

`origin/main` has moved ahead of the merge-base `2307e6b`: 10 commits on
`main`, 11 on this branch, and exactly two files collide —
`.claude/rules/test.md` and `.codex/rules/test.md`. The branch therefore has
to be rebased onto `origin/main` with those two resolved before the merge
request can open clean; the actual rebase result is reported in the PR
description rather than claimed here.

