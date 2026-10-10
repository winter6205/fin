# 0098. MP15's collection outcome reaches fin on one absence-bearing internal read, and can only remove a fin claim

Date: 2026-10-07
Status: accepted
Authority: [`specs/structured-corpus-maintenance.md`](../../specs/structured-corpus-maintenance.md) §"Physical collection" and [`specs/structured-corpus-evidence.md`](../../specs/structured-corpus-evidence.md) §"Replay and failure" are the SSOT. This record decides the carrier the two of them leave unnamed.
Implements: `plans/structured-corpus-evidence-lifecycle-p2.md` §P2-D2. Partially unblocks P2-I4.

Accepted 2026-10-07 by the integrator, on the operator's request to clear the P3 prerequisites. §1–§5 are unchanged from the proposed text; the one substantive addition is the P3 agreement to the §2.2 token that §2.2 itself required, recorded below. Basis and overturn conditions: `plans/structured-corpus-p3-prerequisites.md` §0.

**P3's agreement to the §2.2 token, which this record required before implementing it:** agreed as tabulated, with no widening — method `corpus_collection_outcome`, request `iknow.collection_outcome_request`, response `iknow.collection_outcome_result`, both `schema_version: 1`, `evidence_refs` 1..20, `state` enum exactly `collected | expired | protected | purged | unknown`, and the same 32 KiB envelope and 60-second bound as every other action. `unknown` is load-bearing: it is what lets fin prove the §2.3 anti-laundering rule against a backend that has not implemented the method yet, which is why the fallback carrier below is not taken instead. The two fallbacks remain recorded and unused; either becomes the choice if the capability surface is ruled closed.

Citation anchor: every `src/` line reference is verified against base commit `49471ad`. `~/projects/iknow-wt-t13-ik` references are verified against that worktree as read on 2026-10-07.

## 1. Already decided by the specs, not decided here

- Fin's own outbound deadline is fixed and non-sliding; a snapshot stores `expires_at`, which equals `evidence_expires_at` (`specs/structured-corpus-evidence.md:288`, `:298`). Reads and replay never renew it (`:365`, `:368-371`).
- Collection is mark, wait 7 days, recheck, delete, and candidate time and physical purge time are separate (`specs/structured-corpus-maintenance.md:296-300`).
- The backend owns reference grants, so "collection never guesses whether fin still has an answer" (`:314-317`). That sentence is the reason this record does not build a success-bearing channel.
- Fin collects its own expired snapshot payloads on the same deadlines, and history keeps citation identifiers plus an unavailable reason (`:314-316`).
- "never records successful deletion before payload removal" for a collector failure (`:336`).
- Backend/network failure is `backend_unavailable`, never historical expiry (`specs/structured-corpus-evidence.md:353-354`).
- A count grant protects only its receipt and shared authorization metadata, and explicit purge or permission change still invalidates it (`:280-283`; `specs/structured-corpus-maintenance.md:280-283`).

## 2. Decision

### 2.1 Fin's expiry decision is local, and needs no channel at all

EP11's decision is computed from the snapshot fin already froze: `now >= expires_at` withholds the citation and the payload while retaining allowed non-content identity. The producer's `queried_at` and the expected expiry are already validated on the way in at `src/fin/host/structured_evidence_validation.py:218-224`.

MP15's mark, 7-day grace and locked recheck are **iknow's** schedule for **iknow's** payloads. Treating them as an input to fin's decision would make fin's own deadline non-authoritative, and would give fin a second, backend-driven way to claim collection. Rejected.

So the local half of EP11 has no MP15 dependency at all, and is implementable now.

### 2.2 The one channel: a host-internal, read-only, absence-bearing status read

For the cases where fin must learn what the backend did, the carrier is a single new **method token on the same MCP connection** as `corpus_evidence`, host-internal, transport-identity-bound, never model-supplied:

| Element | Value |
|---|---|
| JSON-RPC method | `corpus_collection_outcome` |
| request token | `iknow.collection_outcome_request`, `schema_version: 1` |
| response token | `iknow.collection_outcome_result`, `schema_version: 1` |
| request fields | `evidence_refs`: array of 1..20 non-empty strings |
| response fields | `outcomes`: array of `{evidence_ref, state, observed_at, schema_version}` |
| `state` enum | `collected`, `expired`, `protected`, `purged`, `unknown` |
| bounds | the same 32 KiB envelope and 60-second execution bound as every other action |

**It is not a fifth action of `iknow.evidence_request`.** The spec fixes that union at `specs/structured-corpus-evidence.md:220-227`; adding a member would reopen a normative table. A separate method token keeps that table intact.

**Flagged conflict, not resolved silently:** a new host-internal method token is an addition to the capability surface the evidence spec enumerates. It does not contradict any spec sentence, but the evidence spec does not name it either, so P3 must agree the token before implementing it and the spec should gain one line recording it. If P3 prefers, the alternative below is available and needs no new surface.

### 2.3 The anti-laundering rule

`state` is a statement about **the backend's** payload. It may only remove a fin claim; it may never create one. Concretely:

- Only `collected`, `expired` and `purged` justify deleting a fin snapshot, and each requires a positive record with an `observed_at` inside the retention window.
- `unknown` is the mandatory value for "no record", "record aged out", "never scheduled because a current dependency still protects it", and "the read failed". `unknown` is **not** collection. fin leaves the snapshot readable under its own local deadline and records no backend claim about it.
- `protected` is an explicit positive statement that the backend still holds the payload. It is the value that makes `unknown` honest rather than lazy.
- fin never infers backend collection from the absence of a complaint, from elapsed time, or from its own local deadline. A local deadline is a fin statement about fin's payload.
- A missing or unreadable channel is `backend_unavailable`. It is never `evidence_expired` (`specs/structured-corpus-evidence.md:353-354`).

### 2.4 Refusal codes, named

From `CONTRACT_REFUSAL_CODES` at `src/fin/host/evidence_contract.py:87-115`:

| Situation | Code | Constant |
|---|---|---|
| Local fixed deadline reached, or a positive `expired` outcome | `evidence_expired` | `EVIDENCE_EXPIRED`, `:76` |
| Grant gone for a non-deadline reason: purged, unknown reference, obligations stopped | `evidence_unavailable` | `EVIDENCE_UNAVAILABLE`, `:77` |
| Authorization revision changed, or the outcome read is not authorized for this binding | `access_denied` | `ACCESS_DENIED`, `:78` |
| Transport or backend failure | `backend_unavailable` (transport-level; see note) / `retrieve_unavailable` | `RETRIEVE_UNAVAILABLE`, `:70` |
| Malformed outcome envelope or an unknown `state` token | `malformed_evidence_contract` | `:57` |
| More than 20 references, or the envelope exceeds 32 KiB | `evidence_too_large` | `EVIDENCE_TOO_LARGE`, `:75` |
| A recognized token at another integer version | `unsupported_evidence_contract` | `:58` |

The `purged` versus expired separation is already the established mapping on the read path, where IK maps purge, unknown reference and stopped obligations to `evidence_unavailable` and expiry to `evidence_expired` (`docs/validation/2026-10-06-t13-ep3-private-evidence-read-freeze.md:82-87`, `docs/validation/2026-10-06-t14-ep4-support-continuation-freeze.md:76-79`). P2-D2 extends that same mapping to the outcome channel rather than minting a new vocabulary.

Note on `backend_unavailable`: fin raises it in production today at `src/fin/retrieve/evidence_route.py:335`, `:371`, `:396`, `:397`, but it is **not** a member of `CONTRACT_REFUSAL_CODES` (`src/fin/host/evidence_contract.py:87-115`). Decided: it stays outside that set. The registry is the evidence-contract failure vocabulary that `specs/structured-corpus-evidence.md:350-354` lists separately from `backend_unavailable`, and widening a closed set for a transport code is the move T13 declined to make (`~/projects/iknow-wt-t13-ik/src/core/storage/evidence-read.ts:127-133`). Recorded here because an implementer will notice the omission.

### 2.5 Verified: which codes have no production writer

Searched the committed tree at base `49471ad` for uses outside the module that defines them:

- `EVIDENCE_EXPIRED` and `EVIDENCE_UNAVAILABLE`: **zero** production uses in `src/`. They appear only at `src/fin/host/evidence_contract.py:76`, `:77`, their registry entries at `:110`, `:111`, and the export list at `:1376`, `:1380`. The plan's premise is verified.
- The same is true of `ACCESS_DENIED` (`:78`), `UNSUPPORTED_SOURCE_BINDING` (`:79`) and `RETRIEVE_UNAVAILABLE` (`:70`).
- `EVIDENCE_TOO_LARGE` is the only evidence-failure exit with production writers: `src/fin/host/structured_evidence_validation.py:426`, `:582`, `:602`, `:711`, and `src/fin/host/structured_evidence.py:188`.

Scope note: the claim is scoped to the committed tree. An untracked P2-I3 module, `src/fin/host/structured_evidence_invalidation.py`, appeared in this worktree during this session and is the first in-tree user of `EVIDENCE_UNAVAILABLE`; it is concurrent, uncommitted work and does not change the base-state finding.

Consequence: EP11 will be the first production writer of `evidence_expired`, and the first place fin itself, rather than iknow, chooses between expired, unavailable and denied. The E11 and E14 cases (`specs/structured-corpus-evidence.md:401`, `:404`) will be asserting exits that have no writer until then.

## 3. Alternatives considered

| Alternative | Why rejected |
|---|---|
| fin reads MP15's outcome to decide its own deadline | Makes a backend schedule authoritative for fin's fixed deadline, and creates a second path to a collection claim |
| A success-bearing channel: "this reference was collected" as a positive assertion | Cannot distinguish collected from never-scheduled from read-failed without an `unknown`, and the spec already forbids the backend guessing about fin's copies (`:314-317`). This is the laundering risk itself |
| Reuse the invalidation stream by adding a third `reason` for expiry | The spec fixes `reason` to two members (`specs/structured-corpus-evidence.md:241-243`) and P2-D1 §2.6 makes expiry structurally absent from that stream |
| A fifth action on `iknow.evidence_request` | Reopens the normative action-union table at `:220-227` |
| fin infers collection from "no invalidation event arrived" | An absent event means nothing: it may be unexpired, unread, filtered, or the backend may be down |
| fin polls iknow's Postgres directly | Two stores, two credentials, and an ownership inversion the specs do not permit |

Two fallbacks are recorded so the choice stays visible to P3 rather than being taken silently later.

**Fallback 1, reuse the maintenance summary.** Carry the outcome as extra non-content fields on the maintenance summary every grant already holds in `context.maintenance` (`src/fin/host/structured_evidence_validation.py:139-140`, `:225-267`), exposed through the existing `read` action. The anti-laundering rule in §2.3 is unchanged. Cost: the outcome read stops being a bounded batch query and rides the per-reference read path, and fin can only learn the outcome of a reference it can also read, which is not the case for a purged one.

**Fallback 2, derive it from the stamps that already exist.** iknow already carries source-level lifecycle stamps `retired_at`, `payload_collected_at` and `obligations_stopped_at` (`~/projects/iknow-wt-t13-ik/src/core/storage/schema-evidence.ts:63-65`), with a closed literal stamp writer (`~/projects/iknow-wt-t13-ik/src/core/storage/evidence-pg.ts:70-74`), and has indexed the grant-support side for a future collector (`~/projects/iknow-wt-t13-ik/src/core/storage/schema-evidence.ts:234-238`). A per-reference outcome could therefore be derived instead of stored. Acceptable **only** if the derivation happens backend-side and returns `collected` exclusively when every dependency pinned on that grant has a non-null `payload_collected_at`; anything short of that, including "no stamp on any dependency", is `unknown`. If fin performed the derivation itself, the rule would be rejected: it is the inference that laundered an unrecorded case into a success claim.

## 4. Not a running channel

MP15 does not exist in iknow today, and the distinction matters for what P3 still owes:

- There is no collector, no candidate mark, no 7-day grace step and no locked recheck anywhere in `~/projects/iknow-wt-t13-ik`. A generic stamp writer exists and can set `payload_collected_at` (`~/projects/iknow-wt-t13-ik/src/core/storage/evidence-pg.ts:70-74`, `:800-807`), but nothing calls it with that stamp: the only stamps in use are `retired_at` and `obligations_stopped_at` (`~/projects/iknow-wt-t13-ik/src/core/storage/memory-backend.ts:1568`, `:1575-1586`). The column exists as schema with no collection writing it.
- There is no per-reference outcome and no read that exposes one. iknow's evidence capability answers exactly two actions (`~/projects/iknow-wt-t13-ik/src/core/storage/evidence-read.ts:74-78`), and its evidence DDL is five tables: scope revisions, authorization receipts, grants, grant supports, grant extractions (`~/projects/iknow-wt-t13-ik/src/core/storage/schema-evidence.ts:147`, `:158`, `:177`, `:223`, `:245`). None carries an outcome for a collected reference.
- Nothing in fin reads any maintenance status from iknow; the only `maintenance*` token in fin's retrieve layer is the error code `maintenance_status_unavailable` (`src/fin/retrieve/mcp_adapter.py:576`).

So §2.2 is a contract for P3 to implement, not a description of a channel that exists. Until P3 lands it, fin proves §2.3 against a stub that can return `unknown`, and §2.1 needs no stub at all.

## 5. Consequences

- P2-I4 splits. The local deadline and withholding path is unblocked now; the outcome-consuming half waits for P3. That is the plan's own P2-I4 deferral, unchanged.
- E9, E10, E14 and M20 (`specs/structured-corpus-evidence.md:399-400`, `:404`; `specs/structured-corpus-maintenance.md:365`) cannot pass on the fin side alone. No record here should be read as partial acceptance of them.
- P3 must not invent a second outcome shape. If it lands `collect`/`mark`/`grace` before this token is agreed, the two shapes will diverge and this record will need superseding rather than patching.