# Spec: Harness Runtime deployment and rollout

**Status:** Draft specification; architecture review and the stated freeze prerequisites are separate from implementation acceptance.
**Date:** 2026-10-21

## Objective

Define S6 for P11 and P12: operate the accepted Runtime and parser processes on one Lighthouse host
behind Baota-managed host Nginx, measure deployment capacity and restore readiness, then migrate and
cut over through additive, fenced steps. Keep accepted IDs and data recoverable, and retire the
current single-process guard only after the full evidence gate passes.

## Assumptions (grounded in closed decisions)

1. ADR 0101's existing single-process guard remains until the rollout gate passes; ADR 0103 defines
   Runtime, checkpoint, parser, PostgreSQL, and Redis ownership.
2. Tencent Cloud Lighthouse, Baota-managed host Nginx, and single-host Docker Compose are the
   selected operating profile; exact host size and capacity remain unmeasured.
3. P11 owns host entry, application image, resource evidence, and backup/restore; P12 owns additive
   migration, cutover, compatibility, and guard retirement, as allocated in the [reliability
   map](../docs/wayfinder/harness-runtime-reliability-map.md).
4. Session records remain always-on and authoritative; OpenTelemetry traces are opt-in, diagnostic,
   and never replace session records or the tool outcome ledger.
5. QA Plan 05 T12 already supplies the local PostgreSQL span sink and receipt-export lifecycle. P11
   configures and deploys that existing sink rather than implementing another one; current evidence
   is database/API/render-seam only, with browser TaskPanel and remote Langfuse evidence still open
   ([plan](../plans/qa-chain-05-recovery-trace.md), [closeout](../docs/verification/2026-10-19-qa-plan05-closeout.md)).
6. A production deployment, host purchase, or exact Lighthouse SKU is not authorized by this
   specification.

## Boundaries

- **Does:** Specify the host entry, production process topology, image and artifact readiness,
  health/degradation reporting, capacity/restore evidence, and compatibility-preserving rollout
  gates for P11/P12.
- **Confirms with human:** Exact Lighthouse plan and worker counts are selected from measured
  evidence. Host purchase, provisioning, production deployment, and any later topology change remain
  explicit operator actions.
- **Out of this spec:** Multi-host high availability, tenant isolation, autoscaling, replacing the
  retrieval/index backend, or claiming a deployment or capacity test has already passed.

## Success Criteria

1. **Topology evidence:** Baota's host Nginx serves the built SPA and is the sole public TLS/API/SSE
   entry. Compose initially runs one Runtime API, one graph worker, one parser API, and one parser
   worker with one active conversion, plus one PostgreSQL and one Redis service. Worker counts rise
   only after the evidence gates; Runtime, checkpoint, and parser authorities retain separate
   schemas/roles, and existing retrieval/index dependencies remain present. Vite is not a production
   process, and no competing Nginx, duplicate database/Redis, or second scheduler is introduced.
2. **Network/proxy evidence:** Host-published application ports bind to `127.0.0.1`, while Compose
   peers use normal service networking. SSE is not buffered or cached, heartbeats are forwarded, and
   the proxy read timeout exceeds the heartbeat interval.
3. **Upload-limit evidence:** Ingress allows at least the 50 MiB source payload plus multipart
   framing overhead while enforcing the same 50 MiB source limit at the application boundary;
   over-limit source bytes receive a typed rejection.
4. **Release-readiness evidence:** Application images, parser/provider versions, OCR language data,
   and model/system artifacts are pinned and recorded before startup. Startup does not fetch models
   or packages; missing required parser assets keep the worker unready with a visible diagnosis.
5. **Health/degradation evidence:** PostgreSQL is required for readiness and durable admission.
   Redis loss is reported as degraded live delivery while PostgreSQL work/outbox state remains
   available for recovery. Parser-worker loss leaves accepted parser jobs visible with durable
   status.
6. **Observability contract evidence:** Session messages and receipts remain content-complete and
   always-on under existing access, retention, and deletion rules. Trace export is off by default,
   includes no raw session text, and remains diagnostic. Deployment trace destinations are local
   `pg` or `otlp`; `console` remains developer diagnostic output. `pg` startup requires its
   configured fin-specific DSN.
7. **Backup/restore evidence:** An off-host PostgreSQL backup is paired with a manifest of
   schema/ownership epoch, app image, parser/provider build, model/OCR/config revisions, and
   raw/derived artifact hashes and references. Restoring into an isolated `_test` database verifies
   required thread, checkpoint, receipt, attachment, parser-result, and locator references.
8. **Capacity evidence:** On the selected Lighthouse plan, record per-process RSS/cgroup memory,
   CPU, disk pressure, model/OCR cold start, queue growth, and representative API/SSE and
   concurrent-thread behavior before increasing worker counts or claiming fit. No unmeasured
   developer-machine result qualifies.
9. **Rollout evidence:** Additive schema expansion remains compatible with old readers. Before data
   migration/backfill, old admission stops, in-flight work drains to a safe terminal or explicit
   interrupted state, and the old writer generation is fenced; only then do checkpointed migration
   and backfill complete before new processes start. Each backfill step is restartable; identities,
   checkpoints, receipts, attachments, locators, tombstones, accepted IDs, and retrieval dependencies
   are preserved.
10. **Rollback evidence:** Before any new operation is accepted, an old writer may restart only if
    its epoch is compatible. After new acceptance, recovery uses a compatible Runtime/worker or
    forward fix and reconciles every accepted ID; it never restarts an incompatible old writer,
    resets data, or deletes accepted state.
11. **Guard-retirement evidence:** Retire the guard only after P8/P9/P10 acceptance, the full 50+
    case Fin business campaign, 2 Runtime API × 2 graph-worker fencing/restart evidence (including
    stale checkpoint `put` and `put_writes` rejection), parser restart evidence, browser and SSE
    evidence, isolated restore verification, and measured target-resource headroom all pass.
    Otherwise the guard remains.

## Open Questions

- Which Lighthouse plan and resulting production worker counts fit the measured workload remain open
  until P11 measurements exist; no numerical capacity or latency target is adopted here.
- The operator schedules any eventual host purchase, provisioning, and production cutover after the
  evidence gates; this spec does not claim those actions are approved or completed.

## Inherits / Changes

Quoted definitions from [docs/CONTEXT.md](../docs/CONTEXT.md):

> The application-owned authority that accepts durable client operations, orders work per **research thread**, manages run reservations and fenced **worker attempts**, and exposes thread snapshots and subscriptions. It dispatches the host graph but does not own graph checkpoints or **parser job** state.

> The optional, best-effort five-span OpenTelemetry view adopted in ADR 0102 for diagnosing selected execution seams; it may be sampled or incomplete and does not count all tool attempts or establish evaluation outcomes.

Inherits [ADR 0101](../docs/adr/0101-server-owns-runtime-lifetime.md), [ADR
0102](../docs/adr/0102-opentelemetry-standard-instrumentation-for-turn-receipt-tracing.md), [ADR
0103](../docs/adr/0103-durable-runtime-and-parser-ownership.md), the accepted operating profile and
rollout gates restated in Success Criteria above and allocated in the [reliability
map](../docs/wayfinder/harness-runtime-reliability-map.md), and the [context map](../docs/context-map.md).
The local `hr-deployment-operating-profile.md` and `hr-rollout-guard-retirement.md` tickets record provenance.

Changes: assigns the accepted host topology, deployment evidence, and compatibility gates to
S6/P11/P12; records the two-layer observability deployment contract already amended in ADR 0102. It
adds no new domain term or architecture decision. **Persist list:** empty.

## Architecture Change Review

Design-time independent review completed on 2026-10-21 against the six specifications, canonical map, ADRs, and tickets. These verdicts assess the contract, not executed code or runtime evidence.

- bounded-context-guardian: **yes**
- input-contract-tests: **yes**
- error-handling-enforcer: **yes**
- complexity-anti-drift: **yes**
- minimal-change-verifier: **yes**

**OVERALL: PASS — 0 unresolved applicable design concerns.** The stated documentation, dependency, evidence, and contract-freeze prerequisites remain in force; no implementation plan is opened by this review.
