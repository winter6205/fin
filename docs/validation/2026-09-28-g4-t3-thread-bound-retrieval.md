# G4 A.T3 — thread-bound retrieval and mixed company/attachment source selection

Date: 2026-09-28
Branch: `feat/g4-attachment-t3-thread-retrieval` (base `main` `e60f824`).
Commits, in order: `0982ef3` the T3 implementation snapshot taken for the
first review, `9259651` the live-suite expectation fixes (committed-fixture
facts, not product behavior), `940d59e` the round-1 repair, and `a1cd981`
the round-2 repair — the tip every "at `a1cd981`" row ran at.

Inherited contract: `specs/thread-attachment-source-read.md` (source
selection, thread-bound attachment search, typed exits) and ADR 0088 (typed
`unit` locators, host-bound scope). Records inherited and exercised here:
[2026-09-28-g4-t2f-graph-job-gate.md](2026-09-28-g4-t2f-graph-job-gate.md)
(the gate whose wait/resume this ticket's clause leans on and whose live
harness this suite reuses) and
[2026-09-27-g4-t2c-single-attachment-tombstone.md](2026-09-27-g4-t2c-single-attachment-tombstone.md)
(the `fin-g4-t2c-s3` byte store).

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | T3 tip (see "Review rounds" for the exact sha every command ran at) |
| Python | 3.12.14 (`uv run --no-sync pytest`); default suite stays hermetic |
| Durable metadata | Postgres 16 (`fin-postgres-1`, published `localhost:5433/fin`), `FIN_CHECKPOINT_URL` loaded from the repo's gitignored `.env` (the variable NAME is the only identifier used here) |
| Byte store (live suite) | SeaweedFS S3 gateway 4.43, container `fin-g4-t2c-s3` at `127.0.0.1:8333`, bucket `fin-g4-t2c`, `S3CompatibleObjectStore` through `create_object_store()`; local throwaway test credentials exported in the shell only |
| Company source (live suite) | live `iknow-memory` stdio MCP server built from sibling checkout `08cedb1` (`dist/index.js`, node v24.21.0) through the product's `MCPPushPort`/`MCPRetrieveAdapter`; `STORAGE=memory` harness mode, disclosed in the module docstring |
| Model transport (live suite) | no provider credential on this host: scripted model transport per the T2f live-module convention (real `ChatOpenAI` against a scripted OpenAI-compatible server) |
| Gold data | committed `data/corpus/dataset/t1/compliance-001.md` + `engineering-001.md` (attachment lane), committed `data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md` (company leg, same doc id as the SC8 live suite) |

## What was claimed, and the evidence for each clause

Acceptance clauses are the plan's T3 item
(`plans/thread-attachment-source-read.md`:135-140).

| Clause | Evidence | Actual result |
| --- | --- | --- |
| later-turn hit carries `{kind:"attachment",attachment_id,version,unit}` | `tests/host/test_attachment_retrieval.py` locator/version/1-based-unit pins; `tests/host/test_graph_attachment_retrieve.py` envelope pin; live case 1 cross-checks each hit's text against the frozen-unit row read over the same service and the version against the SQL row | hermetic + live pass; live envelope carried `kind=attachment` with `unit=1`, text byte-equal to the unit row |
| a query in another thread cannot retrieve it | retriever re-filters `record.thread_id` after `list_for_thread` (leaky-store defense pinned by test); live case 2 runs thread B's grounded retrieve | live: B's envelope has no attachment hit carrying A's id and none of A's text; no typed error (legal empty) |
| a model-supplied thread ID cannot widen scope | no `thread_id` model argument exists (asserted on the tool schema); scope is read from the run's `configurable`; live case 3 runs A's id under B's config | live: `attachment_error {"error":"unknown_locator"}`, `attachment_chunks` empty, zero text leak |
| naming one attachment excludes others; comparison returns it alongside company hits | hermetic narrowing + live case 5 (one thread, two ready attachments sharing exactly one boilerplate phrase: unnamed → both ids, named → only the named id, the excluded file's unique sentence absent); live case 1 answers company + attachment branches in ONE retrieve over the real MCP wire | pass; company leg proved wire-crossing (the live server recorded the `retrieve` call) and returned pushed-filing text ("Apple Inc.") |
| required `uploaded`/`parsing` attachment: same run waits, resumes automatically | inherited T2f gate (node untouched by this diff); NEW hermetic seam test `test_a_run_suspended_by_the_gate_resumes_into_locator_publishing` pins interrupt → resume-without-resend → resumed retrieve publishes locator-bearing `attachment_chunks`; zero retriever calls while suspended | pass — the seam held with no product change |
| parse failures exit with their named typed failure | round-1 repair: a named `failed` row exits with its STORED `failure_reason` code (`test_named_failed_row_exits_with_its_stored_failure_code`); round-2 repair added the live gold for that exit (case 3c, row 11) and reused the existing no-code fallback token; live case 3b pins the gate-owned transient residual as `attachment_not_ready` | pass; residual choice for `uploaded`/`parsing` at the search surface is pinned in the plan's T3 status note |
| index outage is typed unavailable, never empty success | hermetic unusable-postings + ordinal-corruption pins; live case 4 deletes real postings rows over SQL while the row stays `ready` | live: `attachment_error {"error":"attachment_index_unavailable"}` — not an empty hit set, no text leak |
| company ranking remains behind the existing port | zero changes under `src/fin/retrieve/`; the attachment leg never touches the company port; rewrite-retry stays company-only (pinned hermetically) | pass |

## Commands

| # | Command | Exit | Actual output |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/host/test_attachment_retrieval.py tests/host/test_graph_attachment_retrieve.py tests/host/test_agent_instructions.py tests/host/test_attachment_prepare_door.py tests/host/test_graph_attachment_gate.py` (at `0982ef3`) | 0 | `96 passed in 7.55s` |
| 2 | `uv run --no-sync pytest -q tests/host -k "attachment or retrieve or graph or citation or evidence or agent_instructions"` (at `0982ef3`) | 0 | `1135 passed, 48 skipped, 1734 deselected in 69.66s` |
| 3 | live T3 suite, first run (see row 4 note) | 1 | `2 failed, 5 passed in 12.93s` — both failures were the NEW suite's own expectation bugs against committed fixtures (invented company phrase; `_prepare_to_ready` minted a second thread), the product answered correctly in both; fixed in `9259651` |
| 4 | `FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=http://127.0.0.1:8333 FIN_OBJECT_STORE_BUCKET=fin-g4-t2c … uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_retrieve_mcp.py` (at `9259651`) | 0 | `7 passed in 23.47s` |
| 5 | same live command re-run at the repair tip | 0 | `7 passed in 15.60s` |
| 6 | repair-loop command `uv run --no-sync pytest -q tests/host/test_attachment_retrieval.py tests/host/test_graph_attachment_retrieve.py tests/host/test_graph_attachment_gate.py` | 0 | `74 passed in 8.82s` (RED first: 3 named failures) |
| 7 | `uv run --no-sync pytest -q tests/host` at the repair tip | 1 | `2572 passed, 94 skipped, 1 failed` — the single failure is the docker-permission launcher case already named in the T2f record (`test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory`); it fails identically without this branch's changes |
| 8 | `git log --oneline e60f824..HEAD` (read-only pin check when "Review rounds" was written) | 0 | three commits — `940d59e` (round-1 repair), `9259651`, `0982ef3`; the round-2 repair commit landed after this row was drafted and is recorded in row 10 |
| 9 | round-2 repair sweep `uv run --no-sync pytest -q tests/host -k "attachment_retrieval or graph_attachment or attachment_prepare or degradation or send_wait or prepare_door"` (at `a1cd981`) | 0 | `218 passed, 1 skipped, 2707 deselected in 11.56s` (the skip is the named `FIN_CHECKPOINT_URL` opt-in Postgres twin) |
| 10 | `uv run --no-sync pytest -q tests/host/test_attachment_retrieval.py tests/host/test_graph_attachment_retrieve.py tests/host/test_graph_attachment_gate.py tests/host/test_attachment_send_wait.py tests/host/test_attachment_prepare_door.py` and `uv run --no-sync pytest -q tests/host -k "attachment or retrieve or graph or citation or evidence or agent_instructions or degradation"`, both at `a1cd981` | 0 | `100 passed in 14.55s`; `1159 passed, 48 skipped, 1719 deselected in 64.28s` |
| 11 | live T3 suite re-run at `a1cd981` (same command as row 4, now EIGHT cases including the new durable failed-row gold) | 0 | `8 passed in 28.82s` — the new case answers `attachment_error {"error":"attachment_empty_text"}` from a real `mark_failed` write over the same store door the preparation executor uses, and never `attachment_not_ready` |

## Accepted findings that stayed, with reasons

- `AttachmentPreparationService.search` delegating to
  `ThreadAttachmentRetriever` is a Middle Man by the letter of the smell
  list; it stays as the design note because the service is the single
  object `compile_for_runtime` already resolves and hands to the graph,
  so the delegation is the wiring seam, not a layer that could be removed
  without a second resolution site.
- `tests/host/test_live_attachment_retrieve_mcp.py` exceeds the 1000-line
  soft review trigger; it stays whole because every case shares one live
  fixture stack (Postgres + SeaweedFS + iknow MCP), and splitting would
  duplicate that expensive setup per file.
- `graph.py` growth is pre-existing (the file was already past the trigger
  at the base commit); this diff adds the attachment leg only.
- `test_graph_attachment_gate.py::test_run_id_persists_across_interrupt_and_resume`
  observed order-flakiness when Python GC reuses `id()` values used as
  graph-cache keys. Pre-existing pattern from T2f, not introduced here;
  closed in this branch by issue #318's cache fix (identity holders that
  retain the object for the entry's life).

## Known environment limits, named not hidden

- The live suite substitutes two boundaries, disclosed in its module
  docstring exactly as the T2f record does: scripted model transport (no
  provider credential on this host) and iknow-memory's `STORAGE=memory`
  harness mode. Everything else — real MCP stdio wire, real Postgres
  attachment/job/thread stores, real SeaweedFS bytes, the product graph,
  gate, retrieve tool, and citation gate — is the shipped code path.
- Checkpointing in the live T3 suite rides `InMemorySaver`: pause/resume
  over a real `PostgresSaver` checkpoint is the T2f live module's claimed
  evidence and is unchanged by this diff; T3's new seam test is hermetic.
- This shell user is not in the `docker` group; service liveness was
  probed over the published ports instead of the docker CLI.
- Full default suite (`uv run pytest -q`) was not run this round; the
  targeted sweep (rows 2, 6, 7) covers every module this diff touches.

## Review rounds

- Round 1, pinned `e60f824..0982ef3`: Standards 0 High / 1 Medium / 3 Low,
  Spec 0 High / 1 Medium / 2 Low → `GATE: PASS`. Both Mediums and the three
  actionable Lows were repaired in `940d59e` (fix(attachments): repair T3
  review findings on the search branch): a named `failed` row exits with its
  stored `failure_reason` instead of a generic not-ready code, one constant
  resolves the limit default, the gate-suspend-to-resume retrieve seam gained
  its hermetic case, and the unwired `attachment_ids` inertness is stated on
  the tool docstring.
- Round 2, pinned `9259651..940d59e`: Standards 0 High / 5 Medium / 6 Low,
  Spec 0 High / 1 Medium / 1 Low → `GATE: PASS` (no unresolved High on either
  axis). The shared-root Medium: the round-1 repair minted a new wire code
  `attachment_failed` that is not in the typed-exit enumeration at
  `specs/thread-attachment-source-read.md`:67; it was fixed by reusing the
  existing token `attachment_preparation_failed` — the gate's own no-code
  exit — as a single public Final in the leaf module both the gate and the
  search surface import, so no code outside the spec's list exists. The
  remaining round-2 Medium clusters (the ClassVar-shadowing code override,
  the falsified sentinel comment, the fake port's stale `limit` default, and
  the missing live gold for the changed exit) were repaired in `a1cd981`
  (fix(attachments): close round-2 review clusters on the search surface). Deferred round-2 Lows,
  each with its reason: test-double dedup across the gate and retrieve
  suites went to issue #318; the
  68-line seam test over the soft 60-line review trigger stays as one
  narrative read; the unannotated test-store helper returns were the third
  hand-rolled delegating store, same ticket as the dedup.
  Issue #318 was then implemented (operator's plan `c3cd7e6` + fix
  `0aa2bdb`, reviewed and recorded in
  [`docs/validation/2026-09-28-318-host-test-wiring.md`](2026-09-28-318-host-test-wiring.md))
  and merged into this branch: identity holders replace the `id()` cache
  keys, the shared `tests/host/attachment_fakes.py` closes the dedup and
  store-annotation Lows, and `tests/host/test_graph_cache.py` pins the
  collision/reuse regressions. The 68-line seam test remains deferred by
  choice.
- Gate after round-2 repair: no unresolved High and the shared-root Medium
  closed at its root, so `GATE: PASS` stands for the landing round at
  `a1cd981`.
