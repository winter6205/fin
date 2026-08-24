# the tracked change: pending_checkpoint after mid-flight refuse

## Problem

Mid-flight exception emits terminal `refuse` SSE and persists `RunRecord`, but LangGraph checkpoint retains pending `next` nodes → `GET /threads/{id}` returns `pending_checkpoint: true` → CrashResume banner shows above terminal refuse turn.

## Fix (Approach A — backend SSOT)

After persisting mid-flight refuse `RunRecord`, finalize checkpoint via new `_LockedGraph` seam that calls `update_state(..., as_node="citation_gate")` with `status=refuse` to route to END. Preserves messages; clears `pending_nodes()`.

## Affected files

- `src/fin/host/graph.py` — add `finalize_abandoned_run(thread_id, status, status_reason)` seam
- `src/fin/host/workspace_http.py` — call seam after mid-flight refuse persistence
- `tests/host/test_workspace_http.py` — assert `pending_checkpoint` false + resume 204

## ACR verdicts

- bounded-context-guardian: yes — graph seam owns checkpoint mutation; HTTP layer calls documented public method (mirrors `delete_thread` / `pending_nodes`)
- defensive-contract-validator: yes — tests cover mid-flight refuse (empty/exception), crash resume regression (pending stays true), gate refuse regression
- error-handling-enforcer: yes — no new swallow paths; seam no-ops when `pending_nodes()` already empty; typed `ConcurrentRunError` if lock held
- complexity-anti-drift: yes — one small method + one call site; no god-function growth
- minimal-change-verifier: yes — single logical task (fix checkpoint inconsistency), one commit

## Tasks

1. [x] done — Add `finalize_abandoned_run` to `_LockedGraph`
2. [x] done — Call after mid-flight refuse `store.append_run` in send path
3. [x] done — Extend `test_scripted_model_raises_emits_terminal_status` + add resume 204 assertion
4. [x] done — Regression: crash resume test still has `pending_checkpoint: true`

**Shipped:** PR on `main`.
