# Plan: that pull request merge follow-up

**Goal:** Resolve the material T5 gaps and produce an evidence-based merge recommendation for that pull request.
**Approach:** Establish the attachment preparation failure's cause, deliver reliable region inspection, then exercise the visual lane through real boundaries. Reconcile the acceptance record without treating incomplete G4 obligations as delivered.
**Spec:** [Thread attachment source read](../specs/thread-attachment-source-read.md), items 10/10a of [the implementation plan](thread-attachment-source-read.md), ADRs [0088](../docs/adr/0088-thread-private-attachment-retrieval.md) and [0089](../docs/adr/0089-lazy-page-region-attachment-preparation.md).
**PR:** <the tracked pull request>
**Read baseline:** PR head `42ebed4b4ab8bfe1c4bdc9e5ca382667cc4a5683`; base `231493b`. Verify the current head before executing; do not assume it is unchanged.
**Evidence source:** [The validation record at the read baseline](https://github.com/winter6205/fin/blob/42ebed4b4ab8bfe1c4bdc9e5ca382667cc4a5683/docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md). Its test results are prior-session evidence, not runs performed while writing this plan.

## Execution constraints

- The operator explicitly waived CI investigation and a second code-review round for this PR. Do not rerun either as a prerequisite for these tasks. Keep targeted tests, real-boundary acceptance, and verification-before-completion.
- Start in an isolated checkout of the verified PR head. The checkout where this plan was authored was `main` at `231493b`; do not implement PR fixes there. Routine isolated worktree creation needs no additional permission.
- Read the applicable project rules and spec at the execution head. Explore before implementation; delegate by module, with product tests owned by `test-engineer`. Do not revert unrelated changes.
- Use failing-first tests for behavior changes, then minimal implementation, relevant type checks/tests, and measured verification. Do not weaken assertions, convert failures to skips, or silently change acceptance.
- No lockfile or settings changes are planned. Keep fixes supported by a reproduction or the existing spec. Do not introduce unrelated cleanup.
- Use configured credentials through the existing loaders; never print credential values or attach private bytes to logs. Stop temporary test servers on exit.
- Commit the verified work with Conventional Commits. This plan does not authorize push, merge, release, or deployment.

## Architecture gate

This delivery creates documentation only; the following exemptions cover the plan artifact, not future implementation approval. Before a task introduces cross-module changes, run the architecture gate against its actual proposed scope and resolve every no/unclear verdict before coding.

```text
bounded-context-guardian: N/A — documentation delivery changes no module boundaries.
input-contract-tests: N/A — documentation delivery changes no public input contract.
error-handling-enforcer: N/A — documentation delivery changes no failure paths.
complexity-anti-drift: N/A — documentation delivery adds no executable logic.
minimal-change-verifier: yes — this delivery is limited to the PR follow-up plan and a local handoff pointer.
```

**Persist list:** Empty; no new domain terms or ADR changes are proposed.

## Tasks ordered by dependency

1. **T1 — Establish and resolve the live preparation failure (L1)** — tag: `[implementation]`
   - **Inherits:** The spec requires `ready` to mean usable attachment products; source failures must have typed outcomes. The validation record explicitly says the observed `source_unavailable` root cause is undetermined.
   - **Surface:** fin host attachment preparation, durable jobs, object-store integration, and the live attachment door tests.
   - **Acceptance:** Recover the originating exception using sanitized diagnostics and reproduce the queued child-process preparation path against real Postgres and SeaweedFS. Compare with base `231493b` using equivalent queued/drained behavior and prerequisites; the base test's immediate `ready` assertion is not an equivalent baseline. Record whether this is a branch regression, an inherited defect, or a concrete environment failure, with evidence. Fix the demonstrated cause and show the attachment reaches `ready` before the existing test proves single-attachment deletion removes the real bytes. Preserve tombstone and late-job safety; do not replace the readiness assertion with acceptance of `failed`.
   - **Existing command:** `uv run --no-sync pytest -q -m mcp tests/host/test_live_attachment_upload_door.py` after its real-service prerequisites are configured in the isolated checkout.
   - **Completion:** Named root cause, relevant reproduction/regression tests, and the real live door passing at the repair revision; missing prerequisites remain blocked evidence.
   - Status: [ ] pending

2. **T2 — Deliver reliable crop inspection and accurate provenance (L9)** — tag: `[implementation]`
   - **Inherits:** ADR 0089 allows detail inspection of the stored original; the spec requires the relevant reliable crop, otherwise the full page, and provenance describing the scope actually inspected.
   - **Surface:** fin host run-side visual read, existing corpus page/region extraction, observation persistence, and visual evidence projection.
   - **Acceptance:** A valid request with a reliable persisted table/figure region inspects that crop and emits matching scope provenance. An unavailable reliable crop uses the permitted whole-page fallback and reports that actual scope. Invalid targets/regions retain typed refusals. Reuse requires matching target, version, and inspected scope; refreshing proves the stored page raster hash, charges the existing run ledger, and persists a new observation version. Cover empty/malformed/out-of-bounds regions, absent reliable boxes, stale versions, and exhausted budgets at applicable boundaries. Generated descriptions remain outside quoted-text evidence and citations.
   - **Existing test modules:** `tests/host/test_attachment_visual_read.py`, `tests/host/test_graph_visual_lane.py`, and `tests/host/test_visual_claims_wire.py`. The implementer chooses any additional test locations.
   - **Completion:** Failing-first cases now pass for crop and whole-page behavior, including persistence and outward provenance; no invented crop is reported as observed.
   - **Alternative:** Reducing scope requires an explicit operator decision and recorded spec/plan changes. A limitations paragraph alone does not satisfy the existing crop contract.
   - Status: [ ] pending
   - [blocks: T1]

3. **T3 — Prove the run-side visual lane against real boundaries (L8)** — tag: `[implementation]`
   - **Inherits:** Item 10a requires current-run reads, reuse without another upload or vision call, and persisted refresh under the run ceiling. The adapter/probe results do not establish this lane.
   - **Surface:** fin grounded graph and `read_visual_detail`, attachment observations and objects, real vision adapter, run persistence, and SSE/response projections.
   - **Acceptance:** Use the committed versioned G4 attachment gold cases with a real provider, real SeaweedFS bytes, and real Postgres. Drive the model-facing tool through the graph; show an insufficient observation causes the expected inspection, a charged ledger entry, and a real observation write with accurate provenance. On a later turn, read and reuse sufficient evidence without re-upload, an additional provider call, or a new version; prove the re-rendered raster hash against real stored bytes. Show the resulting claim reaches persisted run state and the outward visual-claim channel without becoming a source quote. Preserve deterministic coverage of hash mismatch, provider failure, exhausted budget, cross-thread access, and tombstones; exercise applicable failure legs live where the acceptance crosses a real boundary. Capture actual commands, dataset/provider versions, dimensions/scope, calls, versions, expected versus observed results, and exit codes.
   - **Completion:** A real end-to-end refresh-and-reuse record at the repair revision. Fake-only tests, a direct adapter call, a probe, or a skipped live test cannot substitute for it.
   - Status: [ ] pending
   - [blocks: T1, T2]

4. **T4 — Reconcile the handoff and produce the merge decision** — tag: `[decision]`
   - **Inherits:** Items 10/10a remain pending until their actual acceptance is met; the End-of-round contract also includes live iknow storage and the 4 GB device. ADR 0090 forbids tracked documents from citing ignored session-only paths as reachable authority.
   - **Surface:** tracked implementation plan, validation record, and PR handoff description.
   - **Acceptance:** Update the tracked record to name the tested revision and distinguish hermetic tests, each real-boundary result, unresolved defects, and unavailable evidence. Replace the tracked plan's unreachable `docs/handoff/` citation with a tracked artifact while preserving its substantive handoff information. Correct stale wording about the web surface still being in the working tree. Do not add an ignored handoff file to Git as the repair. Remove unsupported claims that L1 is already proven unrelated to branch changes. Resolve inconsistencies in counts and references, including the record's one-real-JSONB-case distinction.
   - **Local validation:** Run the default suite and the relevant host/web checks at the repair head; retain exact commands and results. The spec's default-suite exit-zero requirement remains unmet if L3 persists, even when its inherited cause is established. Any proposal to accept that exception must be explicit; do not silently mark T5 complete. This is local verification, not CI investigation or another code-review round.
   - **Merge recommendation:** Recommend squash merge only with an accurate bounded scope and evidence. If the operator chooses a staged merge while full G4 evidence is outstanding, describe it as partial delivery and keep 10/10a pending. Do not equate a merge recommendation with permission to merge.
   - **Completion:** Every remaining limitation has a named follow-up scope and observable acceptance; the recommendation states which product defects and evidence gaps remain. No push or merge is performed without explicit authorization.
   - Status: [ ] pending
   - [blocks: T3]

## Remaining obligations and scope boundaries

| Item | Treatment |
| --- | --- |
| L2: live graph-gate flakiness | If it blocks the real lane, isolate order/timing and repair in a separate narrow slice before declaring that leg passed. A clean isolated rerun does not establish module stability. Otherwise record a separate follow-up. |
| L3: default-suite `pytesseract` collection leak | Preserve the known failure and reproduction. Fix it separately if needed for full-suite exit zero; do not weaken assertions or call the existing condition satisfied. |
| L4: no real iknow `pg_es` store leg | Follow-up for full G4 acceptance with actual PG/ES/Neo4j storage. In-memory MCP is insufficient. |
| L5: no dedicated 4 GB device | Follow-up for full G4 acceptance. A swap-enforced cgroup run is distinct from a device measurement. |
| L7: pipeline image drift | Follow-up for mechanically reproducible dependency/image pinning with rebuild evidence. |
| Jev live round-trip | Existing search-mode obligation. Run when changing that product path; do not make it a new prerequisite for unrelated crop fixes. |
| global and real-SQL modes | Separate product work; not implementation scope for this PR's T5 repairs. |

Tracking is local markdown by default. This plan does not authorize creating GitHub issues or editing the PR remotely. Full T5 completion requires all applicable original acceptance, not just T1–T3 passing.

## Planning-session verification

- At local `main` revision `231493b`, `uv run --no-sync pytest -q -m eval tests/eval/test_gold.py` returned exit 0: **9 passed** (3.25 s), the committed hand-authored gold cases against ADR 0012's frozen corpus.
- This checks the documentation delivery's repository baseline only. No PR-head product tests, live acceptance, CI inspection, or second code-review round ran while writing this plan.
