# Issue 318 repair and issue 319 merge recommendations

Date: 2026-09-28.
Basis: [issue #318](https://github.com/winter6205/fin/issues/318) and the
requested merge/follow-up analysis of
[issue #319](https://github.com/winter6205/fin/issues/319).
Repair plan: [318-host-test-wiring.md](../../plans/318-host-test-wiring.md).
Branch: `fix/318-host-test-wiring`, based on PR #316 tip
`0f65d6f6d496a66c3c0c1c8240f68af8d058d18b`.

## Root cause and repair boundary

The graph cache used integer IDs without retaining every object that
contributed its identity. A disabled memory port is not captured by the
compiled graph. Its caller can drop it while its obsolete cache key remains,
and ID reuse can make a different configuration receive the old proxy.

The repair keeps the existing identity dimensions, compares objects by `is`,
and retains their referents for the existing process-lifetime cache. It
supports unhashable dependencies without calling their value equality.
Plain object tuples or bare weak references would not provide those semantics.
No eviction mechanism, additional cache dimension, public schema, dependency,
or backend contract is added.

The shared preparation fake preserves gate scripting, last-state replay,
hydration, recorded calls, and scripted resolver errors. The delegating store
preserves the three independent fault scenarios: missing postings, a failed
row without a reason, and a leaky thread listing. Existing assertions remain
in place. The long resume-seam narrative is deliberately unchanged.

## Dataset and commands

The committed baseline is ADR 0012's nine cases in `tests/eval/test_gold.py`
against the frozen filing corpus. Dataset pins:

- `data/corpus/manifest.json` SHA-256:
  `148bf521f7ec2544ba960d1b5e4dfb4e3cdfa5a2584c73cb6d776ec81c2de81c`.
- `tests/eval/test_gold.py` SHA-256:
  `cb237b7badf9e897632953d31e64f5eddc235448401a11d28849440c95e3c178`.

Repair commands run from `/home/winner/projects/fin-wt-318` with
`UV_PROJECT_ENVIRONMENT=/home/winner/projects/fin-wt-t3/.venv PYTHONPATH=src`
and `uv run --no-sync`; no dependencies are synchronized. An import probe
confirmed `fin.host.graph.__file__` resolves to the repair worktree.
Baseline commands run in the unchanged T3 worktree at `0f65d6f`.

| Check | Exit | Observed result |
| --- | --- | --- |
| Base: `pytest -q tests/host -k "attachment or graph"` | 0 | 973 passed, 47 skipped, 1906 deselected, 42.48 s |
| Base: `pytest -q -m eval tests/eval/test_gold.py` | 0 | 9 passed; 6 done, 3 refuse, 0 step-cap/unknown, 2.65 s |
| RED: `pytest -q tests/host/test_graph_cache.py::test_distinct_equal_ports_do_not_alias_when_numeric_ids_collide` | 1 | Main independently reproduced 1 failed, 0.31 s: `assert other is not first` failed; two distinct value-equal, unhashable ports received the same proxy |
| Base reproduction of both final cache cases, using `PYTHONPATH=/home/winner/projects/fin-wt-t3/src` | 1 | 2 failed, 0.65 s; both distinct-instance and release/replacement assertions returned the stale proxy |
| GREEN: `pytest -q tests/host/test_graph_cache.py` | 0 | Main observed 2 passed, 0.38 s |
| Four changed suites: cache, gate, graph retrieval, attachment retrieval | 0 | Test-engineer observed 80 passed, 5.63 s, no skips |
| Graph and memory smoke: `pytest -q tests/host/test_graph_cache.py tests/host/test_graph.py tests/host/test_memory_*.py` | 0 | Implementer observed 324 passed, 34 existing Postgres skips, 10 deselected, 24.58 s |
| Repair: `pytest -q tests/host -k "attachment or graph"` | 0 | Main observed 975 passed, 47 skipped, 1906 deselected, 64.70 s; two added passing cases and exactly the same skip count as the base |
| Repair gold: `pytest -v -m eval tests/eval/test_gold.py` | 0 | Main observed 9 passed, 5.37 s; 6 done, 3 refuse, 0 step-cap/unknown |
| Base full default: `pytest -q` at `0f65d6f` | 1 | 4241 passed, 111 skipped, 340 deselected, 1 failed, 8 warnings, 1294.00 s; sole failure is the unchanged Docker launcher preflight |
| Repair full default: `pytest -q` | 1 | 4243 passed, 111 skipped, 340 deselected, 1 failed, 8 warnings, 1310.68 s; same sole Docker launcher preflight failure, no new failures |

Every gold case passed its unchanged, independently authored expectations:

| Case | Expected class | Actual |
| --- | --- | --- |
| `fact_01_aapl_2022q3_total_net_sales` | done, one read locator | PASS |
| `fact_02_msft_2023q1_cash_and_short_term_investments` | done, one read locator | PASS |
| `fact_03_amzn_2023q1_total_net_sales` | done, one read locator | PASS |
| `xperiod_01_aapl_total_net_sales_q1_to_q3_2023` | done, multiple read locators | PASS |
| `xperiod_02_msft_net_income_q1_q2_q3_2023` | done, multiple read locators | PASS |
| `xperiod_03_amzn_total_net_sales_q1_to_q3_2023` | done, multiple read locators | PASS |
| `refuse_01_unknown_locator_no_repair` | refuse, unknown locator | PASS |
| `refuse_02_unciteable_answer_hits_repair_cap` | refuse, repair cap | PASS |
| `refuse_03_cites_page_not_read_this_run` | refuse, unread evidence | PASS |

The full default suite is not green in this environment. Both runs fail only
`tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory`
with the same Docker preflight error; the repair adds exactly two passing
tests. The existing Postgres/vision skip prerequisites remain unavailable; they
are not live-service acceptance passes. No skips were introduced.

Type-checking limitation: no `pyright`, `basedpyright`, or `mypy` executable
was available in PATH or the supplied venv. `uv run --no-sync pyright --version`
exited 2 before analysis. The implementer's Serena diagnostics on changed
production lines were empty; this is not a substitute for a completed
type-checker run. No dependency or lockfile was changed to install one.

Validation:
- Not run: standalone static type checking; no checker executable available.
- Expected command: `uv run --no-sync pyright src/fin/host/graph.py`.
- Blocking issue: local tooling availability; not an added product dependency.

The two full-suite processes exited normally with status 1. No pytest
process remained at completion. The launcher command was check-only;
no deployment or Docker start/stop command was issued.

## Independent review

Pinned base: `0f65d6f6d496a66c3c0c1c8240f68af8d058d18b`.
Pinned review snapshot: `276cfabd08fff72938868cba4ddfaa813c4bba5c`
(object-only commit, not a branch-history landing).
Standards source: the installed `arthurpower:code-review` protocol,
sections 2.2 and 4, plus this repository's rules.
Spec source: issue #318 and the user's explicit issue #319 analysis request.
Later validation-table edits do not change the reviewed production or tests.

| Axis | Unresolved High | Medium | Low | Outcome |
| --- | --- | --- | --- | --- |
| Standards | 0 | 0 | 0 | No hard-rule violation or worsened Fowler smell |
| Spec | 0 | 0 | 0 | All three issue requirements covered; no assertion removed or weakened |

Standards OVERALL: 0 High / 0 Medium / 0 Low.
Spec OVERALL: 0 High / 0 Medium / 0 Low.
GATE: PASS.

Read-only exploration: dispatched 2, passed 2, failed 0.
Independent code-review axes: dispatched 2, passed 2, failed 0.
Test and production ownership were disjoint; only the main agent edited
the plan and validation record. No shared-file edit conflict occurred.

## Issue acceptance mapping

| Basis clause | State | Evidence |
| --- | --- | --- |
| #318.1: share preparation doubles | delivered | Both gate/retrieve suites import `attachment_fakes.FakeAttachmentService`; all original scenarios pass in the 80-case focused run |
| #318.2: share/annotate forwarding stores | delivered | `DelegatingStore` types all four read methods; each corruption wrapper retains its fault-specific override |
| #318.3: prevent object-ID cache aliasing | delivered | Actual dependencies are retained in identity keys; both deterministic cache regressions fail the original source and pass the repair |
| #318 verification bar: requested host sweep, no new skips or weakened assertions | delivered | 975 passed versus 973 at base, unchanged 47 skips; both review axes found no assertion removal or weakening |
| #318 exclusions: long seam narrative and launcher environment | preserved | No assertion split/loosening; launcher code untouched |
| User request: #319 analysis for merge and later tasks | delivered | Merge simulation, launcher rerun, deployment evidence boundary, integration order, and T5 interval design recorded below |

PASS = issue #318's three repair clauses and its stated host verification
bar. This is not a claim that the full default suite or #319 merge gates
are green. Full-suite and environment limitations are reported separately.

## Issue 319: merge gates versus subsequent acceptance

| Item | Decision | Required evidence / owner |
| --- | --- | --- |
| Launcher preflight | Still open before PR #317 merge under its stated gate | Run `uv run --no-sync pytest -q tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory` on a Docker-accessible runner; do not skip or weaken it |
| Backend bounded `read_source` | Code dependency is merged; deployed capability remains unverified | Before enabling the company read leg, deploy an iknow-memory build containing PR #274 and run live bounded-read acceptance against that build |
| Full durable store depth | G4 acceptance, not an implementation defect proved in T4 | Keep real fin thread/object stores and the company backend's durable path in the T5/end-of-round gold scope; do not equate `STORAGE=memory` MCP tests with deployed-store acceptance |
| `read_file`, generic `page`, citation DTOs | Deliberately T5-owned | Remove them end-to-end without a compatibility layer after both T3 and T4 are integrated |

The launcher case was rerun at T4 tip
`d0f1257132614069dd5d8d9b3c11a30cd7906c80`: exit 1, `1 failed in 1.79s`.
Its `fin check` calls `dev-up.sh --check`, not the service-start branch.
The script reports `docker daemon is not answering (docker info failed)`;
the explorer's direct Docker API probe reports socket `permission denied`.
This proves the current identity cannot access Docker, not that the daemon
has stopped. No daemon, group membership, deployment, or production
configuration was changed.

That runner also needs Docker Compose, node/uv/curl/jq, and a valid
iknow-memory checkout. If it is not a sibling checkout, set the non-secret
`FIN_IKNOW_ROOT` path explicitly before running the test.

The PR #317 evidence uses a real iknow-memory stdio process with in-memory
backend storage. That proves bounded wire behavior, but neither deployed
backend availability nor full durable-store behavior. GitHub reported no
checks for either PR during this investigation; a clean single-PR merge
status is not an integration test result.

An absent backend tool degrades to `source_unavailable`; an old response
missing required `next_position` is `source_backend_contract_error`. Neither
is evidence that the deployed company-read feature is usable.

## Integration risk and recommended order

PR #316 and PR #317 independently branch from
`e60f8249ba98843095ad371e9d76e5926175049a`. Neither includes the other.
The read-only merge simulation
`git merge-tree --write-tree feat/g4-attachment-t3-thread-retrieval worktree-t4-bounded-source-read`
exited 1 and reported a content conflict in `src/fin/host/graph.py`.
Conflict tree: `2c16c47ad761ed7dd1ccd5a269b34b46a17a2fc0`.

The same simulation was also run with the reviewed #318 repair snapshot
`276cfabd08fff72938868cba4ddfaa813c4bba5c` and T4 tip `d0f1257`.
It again exited 1 with only `graph.py` conflicted, in the same five regions;
the resulting tree is `de3f5f597063b005ece00ef9db42cb68cac16932`.

There are five conflicting regions: `compile_graph` parameters and their
documentation, the `_make_locked_graph` call, its parameters, and the cache
key. Preserve BOTH `attachment_retriever` and
`attachment_source_reader_factory`, their actual object identities, and
both tool paths. Apply this repair's identity-safe key rule to the added T4
factory as well; do not reintroduce an integer `attachment_source_reader_id`.
Concretely, pass `attachment_source_reader_factory` itself to the private
factory, retain that object parameter alongside `attachment_retriever`,
and add `_IdentityKey(attachment_source_reader_factory)` to the key. Test
that changing only that factory produces a distinct proxy while reusing
the same factory remains idempotent.
The auto-merged runtime composition passes the same preparation service to
T3 search, while T4 derives the read boundary from its store and the trusted
runtime thread. Matching typed attachment locators use the same 1-based unit.

Recommended sequence:

1. Move the issue #318 repair commits onto the T3 delivery branch, validate
   that combined tip, then squash PR #316 as one logical T3 delivery.
2. Update the T4 branch onto the resulting main and resolve the graph conflict
   by preserving both seams and the identity-safe cache, including the new
   source-reader factory. Verify auto-merged runtime/tool wiring too.
3. Run the issue #318 sweep and the T4 source-read suites on the combined
   revision, plus a same-thread `retrieve -> read_source` locator handoff.
   Pin an updated review and validation record to that revision.
4. Clear the launcher gate on a Docker-accessible runner. Record the backend
   capability/build and deployment order, then squash PR #317 separately.
5. Start T5 against the combined contract, followed by the full real G4 gold
   acceptance. Do not close #319 until each leftover has its promised evidence
   or remains explicitly tracked by its stated follow-up owner.

No integration/rebase, GitHub mutation, push, merge, release, or deployment
was performed in this run. The combined revision remains to be validated.

## T5 evidence design recommendation

T4 retains only `locator` and `page_text` in `EvidenceItem`; its
`_evidence_from_slice` does not preserve the slice's requested start offset.
`test_continuation_read_accumulates_both_slices` checks two returned evidence
entries, not cross-slice citation validity. T5's accepted criterion explicitly
requires a quote across adjacent returned slices to pass and a quote across
an unseen gap to fail.

Keep the requested code-point start and returned end/cursor in this-run
evidence. Join text only for identical typed source/version/unit provenance
whose intervals are contiguous. Add end-to-end cases for adjacent slices,
an unread gap, supplementary-plane offsets, cross-thread provenance, and
stale versions. This is a T5 prerequisite, not a reason to add a legacy
compatibility layer or silently widen T4's current evidence contract.
