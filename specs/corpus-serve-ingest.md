# Spec: corpus-serve-ingest

> **See also (2026-09-16):** live empty iknow + closed-loop ingest is [`first-closed-loop.md`](./first-closed-loop.md) (does not replace this spec’s two-door contract).

> **Amendment (2026-09-25, permanent corpus upload):** `plans/permanent-corpus-upload.md` T1 is a `[decision]` ticket: amend this spec before any code. The amendment below fixes the **permanent upload → asynchronous parse → push** contract: the accept time point, the stable `document_id` derivation, the durable job identity and its states, the `content_identity` handoff to iknow-memory, the upload-size cap, `format` / `doc_type` validation staging, duplicate/concurrent submission, the status polling contract, and the rule that no fin API may claim corpus-wide graph/C readiness from a successful push alone. It replaces the synchronous `POST /api/corpus/ingest` contract recorded earlier in this file. The original 2026-09-04 assumption gate, Focus 1–3 boundary text, and Success Criteria 1–9 remain in force.

> **Amendment (2026-09-26, content-identity authority):** corrects Decision 2 and every earlier sentence that put `body_content_id` / `content_identity` on the push wire. iknow-memory that pull request landed: its strict push envelope rejects unknown top-level keys, so **no identity field crosses the wire**; iknow computes `content_identity` **authoritatively over the whole pushed body** — `sha256(JSON([body, "normalization-v1", "parse-v1", "parameters-v1"]))` — stores it in `source_documents.content_identity` (unique jointly with `source_ref`), and **returns that stored value in the push response**. fin reads the response value as its **only authoritative** `content_identity` and is **forbidden from recomputing it at T4**; the local `body_content_id` is downgraded to **fin-side bookkeeping** (idempotency / ledger key) that never goes on the wire. Everything else in the 2026-09-25 amendment stays in force.

## Goal

An operator can submit a supported source outside any research thread, and fin **durably accepts** it, then parses, chunk-pushes, and reports per-source text readiness **asynchronously**, surviving process restart, without ever claiming corpus-wide graph/C readiness that only iknow-memory's index job can establish.

This spec is fin-side. fin owns the accepted job, its durable identity, its states, and per-source text readiness. iknow-memory owns the `content_identity` column (decision 2), the graph-index job, and corpus-wide graph/C readiness; that half is the cross-repo contract pinned by `specs/corpus-index-job-orchestration.md` (to be created by iknow-memory T1) and is **referenced, not defined**, here.

## Assumption gate (confirmed 2026-09-04)

Operator confirmed scope A in-thread. These are in force:

1. Serve-time product must stop using a local extracted tree as company-corpus SSOT; **retrieve** evidence is MCP-returned text; ordinary **read_file** reads the stored object (MCP when that is where it lives).
2. Workspace has **two doors**: drag into the current **research thread** = this-turn read only (no push); a separate **corpus ingest** control = ingest endpoint then MCP push.
3. **切分** runs only on the ingest endpoint (deterministic format table, not the research model). Version close/replace is an MCP call.
4. Default `uv run pytest -q` stays green with Fake retrieve; no live MCP required.
5. Connectors (mail, calendar, people) and multi-user source isolation stay out; they are tracked as GitHub issues, not as this spec's tickets.
6. This spec's plan and spec files use **local markdown only** — no GitHub issue tracker for tracer bullets.

## Objective

Ship the host/workspace contract so **company corpus** is stored and queried on the **retrieve** backend (typically **iknow-memory**), fin only produces bytes at ingest and gates citations on this-run tool text, and operators can attach a file to a turn without accidentally ingesting it.

Success is: a scripted serve path no longer needs a second on-disk cite tree; drag-to-thread does not push; the ingest door does push; pytest default stays green.

Quoted from `docs/CONTEXT.md` (exact copy, not redefinition) — after persist these names match the glossary:

- **retrieve port**, **citation verifier**, **read_file**, **切分**, **场景适配颗粒度**, **入库推送**, **iknow-memory**, **research thread** as defined there.

## Boundaries

- **Does:**

  **Focus 1 — Serve evidence from the retrieve backend**
  - Product HTTP/CLI research path uses **retrieve port** hit text as citation evidence (ADR 0046 / 0047).
  - **read_file** is an ordinary read of a known locator; when the object is on MCP, the tool fetches that body (not a parallel extracted tree).
  - **list_files** catalogs ingested/pushed filings, not a leftover HTML tree as SSOT.

  **Focus 2 — Ingest endpoint**
  - One ingest entry: parse + **切分** via the format×section×density table (**场景适配颗粒度**), then **入库推送** / invalidate through MCP.
  - Root **文档类型** is file format (PDF, Word, PPT, Excel, HTML, Markdown, plain text), not EDGAR form names.
  - Unknown format: named failure or documented dumb fallback row — not model-chosen split points.
  - Research-track tools must not expose “chunk any string”.

  **Focus 3 — Two upload doors on workspace**
  - Drag/drop (or equivalent) into the open thread: **thread attach** — model may read this turn; default no MCP push.
  - Separate control: **corpus ingest** — same ingest endpoint as Focus 2.

  **Test allocation (5 classes):** empty (no file, empty retrieve hits); negative (unsupported type, ingest reject); overflow (oversize upload, push batch cap); concurrent (409 concurrent_run unchanged); exception (MCP down typed, ingest named failure, serve without local tree still typed).

- **Confirms with human:** none remaining after gate A.

- **Out of this spec:** mail/calendar/people connectors; multi-user OAuth sources; dream/extract as corpus ingest; session compaction wiring; thumbs UI; observability/rewrite retry; hybrid/RRF inside fin; Redis fair-queue SLA; 切分表具体数值（implementer / existing corpus-retrieval headroom）.

## Permanent upload contract (amendment 2026-09-25)

The subsections below are the **settled contract** for `plans/permanent-corpus-upload.md` T1. They define fin's side of the permanent upload → asynchronous parse → push path. Each decision is written as a contract, not a suggestion; where a decision was made from alternatives the alternatives are named so a later reader cannot silently reopen them.

### Decision 1 — stable `document_id` derivation

The first-page **corpus-ingest** form **requires** `company` and `period`. The derived identity is:

```
document_id = "{TICKER}-{FYQ}"
```

`TICKER` is the operator-supplied `company` value **upper-cased and whitespace-normalized** (`aapl` → `AAPL`); `FYQ` is `period` with surrounding whitespace removed (`2022q3` → `2022Q3`). Worked example: `company="aapl", period="2022q3"` → `document_id="AAPL-2022Q3"`.

Precedence and prohibitions:

1. An **explicit** `document_id` (for example an EDGAR accession number) has the **highest** precedence and takes the explicit branch verbatim; the form's `company` / `period` are then metadata only.
2. With no explicit `document_id`, the `{TICKER}-{FYQ}` rule above produces the identity. Both `company` and `period` are required on this path.
3. The **filename is metadata only and never participates in identity**. There is no filename-pattern parser and none may be added: `Path(filename).stem` is not a derivation step.
4. Explicit `document_id` absent **and** `company`/`period` absent or blank → the typed failure `document_id_missing` (`IngestDocumentIdMissingError`). The endpoint **must not** fall back to the filename stem.

This **supersedes** the current `resolve_document_id` precedence in `src/fin/corpus/ingest_endpoint.py`, whose third step (filename stem) is removed by this decision. The explicit-`document_id` branch and the typed `document_id_missing` failure are already implemented and are retained.

Rationale: the identity must be stable across re-uploads, independent of the operator's local filename, and derivable from operator-supplied metadata before any bytes are parsed. A filename-derived id breaks all three (two operators upload `10q.pdf` for different issuers; the same issuer re-exports with a different name).

### Decision 2 — `content_identity` authority and fin's job-identity pair (amended 2026-09-26)

This is the **fin-side interface contract** of the cross-repo decision. fin does not implement iknow-memory's storage and does **not** put any identity on the push wire; it pins **who computes `content_identity`, where fin reads it, and how fin keys idempotency**.

- **The identity never crosses the wire.** iknow-memory's push envelope is **strict** and rejects unknown top-level keys, so fin sends **no** identity field on push. `content_identity` is computed **authoritatively by iknow** over the whole pushed body — `sha256(JSON([body, "normalization-v1", "parse-v1", "parameters-v1"]))` — and stored in `source_documents.content_identity`, **unique jointly with `source_ref`**. fin never treats a locally computed hash as authoritative and **must not recompute it at T4**.
- **fin reads the stored value from the push response:** inside the job's `cataloging` stage, on every push **including an idempotent re-push**. iknow returns the stored `content_identity` in the push response — top level for a single source, and `content_identities[]` aligned in the same order as `source_doc_ids[]` for a batch. **This returned stored value is the only authoritative `content_identity` fin uses.** fin does **not** obtain any identity at accept time (accept persists bytes + pointer + job record only) and never at query time.
- **`body_content_id` is fin-side bookkeeping only:** the value `src/fin/corpus/push.py` computes locally (the same hashing construction, separator `\x1f`) stays fin's **idempotency / ledger key**. It never went onto the push wire and never will, and it is **not** the authority behind iknow's `content_identity` column.
- **`source_ref` stays exactly** the three-key `{document_id, version, unit}` canonical JSON and **never** contains `content_identity`.
- **fin's idempotency / index pair = `(source_ref, content_identity)`,** where `content_identity` is the iknow **stored value read back from the push response**, not a locally recomputed hash. An unchanged body/identity re-push returns the **same** `content_identity` and the **same batch of chunk ids** (push is idempotent, zero new writes). The per-child pair `(source_ref, content_id)` remains the invalidation key (§B4 of `specs/corpus-retrieval.md`).
- **Different `content_identity` with the same `source_ref`** = a **new content version** for that source: iknow-memory **OVERWRITE**s it (iknow-side behavior, defined in its own spec). fin's obligation on the same event is the existing **内容失效** contract: the stale chunks are closed through the invalidation port. A re-push of an unchanged pair must not trigger invalidation.
- **Cross-repo:** iknow-memory stores `content_identity` in a **first-class column with a uniqueness constraint on `(source_ref, content_identity)`**; that schema and the OVERWRITE semantics are written in the iknow-memory spec (`specs/corpus-index-job-orchestration.md`), not here. fin's own contract is limited to **reading the stored value from the push response** and **keying the `(source_ref, content_identity)` pair on it**; the value's derivation is iknow-memory's.

### Decision 3 — fin job states (seven-state vocabulary, reused)

fin **reuses** its existing seven `IngestState` values and **does not introduce** `queued`, `running`, or `stale` as new persisted states:

| State        | Fin-side meaning on the permanent-upload path                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------ |
| `pending`    | **The queued job state.** Raw bytes are durable in the object store, the pointer and the `pending` job record are durable, and the stable source identity exists; parser/chunker/pusher work has not begun. Reached at **accept**. |
| `fetching`   | The worker has picked the job up and re-reads the stored raw bytes from the object store.              |
| `extracting` | Parsing raw bytes into frozen units.                                                                    |
| `ocr`        | The OCR branch (scanned/imageless units); may be skipped when OCR is not required.                     |
| `cataloging` | 切分 + push + invalidation + catalog (`record_filings`) registration.                                   |
| `ready`      | **Per-source TEXT readiness only:** the pushed chunks are retrievable (block ids returned). This is **not** corpus-wide graph/C readiness (see "Graph/C readiness"). |
| `failed`     | Typed terminal failure carrying `error_type` / `error_message`; retry re-enters at `fetching`.           |

The transitions are the existing ones (`src/fin/corpus/ingest_state.py`): `pending → fetching → extracting → {ocr →} cataloging → ready`, each non-terminal state `→ failed`, and `failed → fetching` on retry. `ready` is terminal.

Consequence for T2's wording: T2's "**排队中的 job 状态**持久化" **lands on `pending`**. `accepted` is reported **only after** all four of the following are durable:

1. the **raw bytes** (object-store PUT succeeded);
2. the **pointer** (object key + `sha256` version + byte size, in fin's metadata store);
3. the **`pending` job record**;
4. the **stable source identity** — `document_id` (decision 1) plus `version = sha256(raw bytes)`.

### Accept time point

`POST` on the corpus-ingest door returns `accepted` **if and only if** all four durable items above are committed, then returns a `job_id` and the source `version` (see "Status polling contract"). Any failure while committing them yields a **typed** error and leaves **no accepted job**. In particular, when the object PUT has succeeded but the pointer or job record commit fails, the compensating object delete runs; **if that compensation fails, the failure is observable and retryable** — it is never swallowed and never reported as a clean rejection.

Validation that needs no bytes (metadata, format name, doc_type, size) runs **before** the object PUT (see "format / doc_type validation staging"); a metadata rejection therefore persists nothing and needs no compensation.

### Upload-size cap

The cap is the existing **50 MiB**, written exactly as **`52428800` bytes**. Both a **declarative** request (`Content-Length == 52428800`) and a **streamed** request measuring exactly `52428800` bytes are **accepted**. A request of `52428801` bytes — declared or streamed — is rejected with the typed overflow error `ingest_upload_too_large` (413-class at the HTTP route). The cap is enforced twice from **one** value: the route's pre-buffer gate and the endpoint's own check (`MAX_INGEST_BYTES`) read the same number, so the two boundaries cannot drift. An over-cap upload is rejected **before** any parse; it is never truncated into a parsed prefix.

### Synchronous endpoint retirement

`POST /api/corpus/ingest` changes from a synchronous parse → chunk → push endpoint into an **accept-and-persist** endpoint: it persists the four accept-time items and returns `job_id`, then parse / 切分 / push / invalidate run **asynchronously** off the request. The response no longer reports `"status": "ingested"` as the terminal outcome of the upload; `ingested` was the synchronous-completion token and is retired in favor of `accepted` + polled job state. A client that needs the outcome polls the status contract below. The endpoint keeps parsing and 切分 in fin (ADR 0037); only the **timing** changes, not the ownership, the chunk table, or the push contract.

### format / doc_type validation staging (explicit decision)

**Decision:** an **unknown format is rejected before acceptance**; a **recognized-format parse failure is a typed job result after acceptance**.

- **Before acceptance (typed rejection, nothing persisted):** format **recognition** — declared format outside `SUPPORTED_FORMATS`, or a filename whose extension is unknown, or no format name at all (`format_unspecified`); an explicit `doc_type` outside the params-table `DOC_TYPES` vocabulary (`invalid_doc_type`); the `document_id` derivation failure (`document_id_missing`); and the size cap (`ingest_upload_too_large`). None of these needs the bytes, all are deterministic functions of the operator's metadata and the byte count, and all are correctable by the operator without changing a source version.
- **After acceptance (typed job result):** parser and downstream failures that can only be discovered from the bytes — `empty_source` / `malformed_source` / `encrypted_source` / `format_mismatch` / `no_units` / `metadata_incomplete` (the existing `ParseError` family), OCR-unavailable (`ocr_required_unavailable`), empty extraction, and push/invalidation dependency failures. Each is a **named typed outcome** on the job (`failed` with `error_type` / `error_message`), never an empty success and never a silent skip.

**Reason:** format **recognition** is deterministic from the declared name and the filename extension alone; it decides whether the source is even in fin's supported set, so it belongs with the other metadata validation and must not consume a durable job and its identity. A **parsing** failure needs the bytes and can only be known after acceptance; forcing it before acceptance would mean buffering and parsing inline, which is exactly the synchronous behavior this amendment retires. Splitting the two keeps the durable job meaningful (only recognizable sources get one) while still making every byte-dependent failure observable as job state.

**Consequence:** a supported-format upload whose bytes are malformed is **accepted** and later `failed`; an unsupported-format upload is **rejected** and never appears as a job.

### Duplicate and concurrent submission

- **Same `(document_id, version)`** — identical raw bytes re-submitted, or two concurrent submissions of the identical bytes — **idempotently joins** the existing job: the response returns the **same `job_id`**, the same source version, and the existing job's current state. It creates **no second object, no second job row, and no second logical source version**. When the joined job is already `ready`, the response is the existing job's `ready` state (a successful no-op), not a new accept.
- **Different `version`, same `document_id`, no in-flight job** — this is **replacement**: the new version is accepted, and fin closes the stale chunks of the prior version through the existing invalidation port (**内容失效**), with iknow-memory performing its OVERWRITE per decision 2. Replacement does **not** create a second logical version coexisting with the first.
- **Different `version`, same `document_id`, while a job for that `document_id` is non-terminal** — a typed **conflict** (the repository's existing `concurrent_run`, 409-class). No second logical version is created and the in-flight job is not disturbed.
- **Never**: two logical versions of one `(document_id, version)`, or a replacement racing an in-flight job into a second accepted job.

### Status polling contract

- The HTTP gate **must** be wired to `IngestStateStore`; `GET` on the job must return the **real** persisted `IngestState` (`pending` / `fetching` / `extracting` / `ocr` / `cataloging` / `ready` / `failed`), **not** a constant `"ingested"` / `"accepted"`. This terminates the current state where `src/fin/host/workspace_http.py` never consults the state store and only echoes `"status": "ingested"`.
- `GET` returns at least: `job_id`, `document_id`, `version`, the current state, `error_type` / `error_message` when `failed`, the per-source **text-readiness** flag derived from `ready`, and a **separate** graph/C sub-object (see next subsection). It must never merge the two readiness meanings into one boolean.
- An unknown `job_id` / `document_id` is a typed `unknown_job` miss (`UnknownJobError`), not an empty success.
- A `failed` job is **retryable** from `fetching`; the retry reuses the durable raw bytes and the same source identity and does not require a re-upload.
- **`job_id`:** for v1 the store's key is `document_id` (the existing `IngestStateStore` protocol), so `job_id == document_id`; the versioned logical identity carried alongside is `(document_id, version)`. A job that cannot be created because one already exists for that `document_id` is resolved by the duplicate/replacement rules above, not by a duplicate `create_job`.

### Graph/C readiness

**No fin API may claim corpus-wide graph/C readiness from a successful push.** fin's `ready` state means per-source **text** readiness only (chunks pushed and retrievable). Corpus-wide graph/C readiness is established by iknow-memory's graph-index job and reported by fin **only** as a pass-through of that job's confirmed result.

- The status response exposes graph/C readiness in a **separate** field sourced from the iknow index-job contract, in states that never collapse to fin's text readiness. A successful push or a partial/stale index job produces `pending` / `unknown` / `failed` — never `ready`.
- A push-success must never be rendered as graph-ready; a failed or absent index-job result must never be rendered as graph-ready.
- The cross-repo contract is **`specs/corpus-index-job-orchestration.md`**, which **does not yet exist in this repository** — it is to be created by iknow-memory T1 (per `plans/permanent-corpus-upload.md`'s sibling-plan pointer). Until it lands, fin's graph/C field is defined as "not reported"; fin must not invent the semantics. Where this spec references graph/C readiness it **depends on** that sibling contract and does not restate it.

### Error and state vocabulary (fin side)

**Accept-time typed errors (nothing persisted):**

| Code                     | Trigger                                                                    |
| ------------------------ | -------------------------------------------------------------------------- |
| `ingest_upload_too_large`| body exceeds `52428800` bytes (declared or streamed; one byte over rejects) |
| `unsupported_format`     | declared format or filename extension outside `SUPPORTED_FORMATS`           |
| `format_unspecified`     | no declared format and no recognized filename extension                     |
| `document_id_missing`    | no explicit `document_id` and no derivable `{TICKER}-{FYQ}` pair             |
| `invalid_doc_type`       | explicit `doc_type` outside the params-table `DOC_TYPES` vocabulary         |

**Accept-time infrastructure typed errors (no accepted job; compensation visible):**

| Code                                   | Trigger                                                        |
| -------------------------------------- | -------------------------------------------------------------- |
| object-store write failure             | the raw-byte PUT fails                                         |
| pointer / metadata commit failure      | the pointer cannot be persisted after a successful PUT         |
| job-record commit failure              | the `pending` job record cannot be persisted                   |
| `object_cleanup_failed` (observable)   | the compensating delete of an already-PUT object fails         |

**Job-result typed outcomes (accepted, then `failed`):** the `ParseError` family, OCR-unavailable, empty-extraction, and push/invalidation dependency failures, each with `error_type` / `error_message` on the job.

**Conflict:**

| Code             | Trigger                                                                                      |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `concurrent_run` | a different `version` of a `document_id` is submitted while that `document_id`'s job is non-terminal |

**Unknown lookup:** `unknown_job` for a status poll of an id that has no job.

### Acceptance criteria (this amendment)

Each line is binary and maps to `plans/permanent-corpus-upload.md` T1/T2.

1. `document_id` is `"{TICKER}-{FYQ}"` for a form-typed `company` + `period`, with `TICKER` upper-cased; an explicit `document_id` wins; a missing explicit id with missing/blank `company`+`period` returns typed `document_id_missing` and **never** a filename-stem id. The filename never changes the identity.
2. `accepted` is returned **only** after raw bytes + pointer + `pending` job record + stable source identity are all durable. A fault in any of them returns a typed error and leaves no accepted job; a failed compensating delete is observable and retryable.
3. A declarative or streamed upload of exactly `52428800` bytes is accepted; `52428801` bytes is rejected `ingest_upload_too_large`. One cap value backs both the route gate and the endpoint check.
4. `POST /api/corpus/ingest` returns `accepted` + `job_id` and performs no synchronous parse/chunk/push; the parse/push chain runs asynchronously and its outcome is observed through the status poll.
5. The status `GET` returns the **real** persisted `IngestState` from `IngestStateStore` and never a constant `"ingested"`. `ready` means per-source text readiness; graph/C readiness is a **separate** field that is never `ready` on a push alone and is defined by `specs/corpus-index-job-orchestration.md` (iknow T1). No fin API renders a push success as graph/C ready.
6. An unknown format is rejected **before** acceptance and creates no job; a recognized-format parse failure is **accepted** and then a typed `failed` job. Neither is an empty success.
7. No push envelope carries an identity field — iknow's strict envelope rejects one — and `source_ref` remains the exact three-key canonical JSON and never contains `content_identity`. fin's authoritative `content_identity` is the stored value read from the push response (top level for a single source, `content_identities[]` aligned with `source_doc_ids[]` for a batch); the locally computed `body_content_id` is fin-side bookkeeping only and fin does not recompute the identity at T4. Re-pushing an unchanged `(source_ref, content_identity)` yields zero new writes and the same chunk ids; a changed `content_identity` on the same `source_ref` closes the stale chunks via invalidation.
8. Same `(document_id, version)` duplicate or concurrent submission idempotently joins one job and returns the same `job_id`; a replacement closes stale chunks; a different-version submission during an in-flight job returns typed `concurrent_run`. No path produces two logical source versions.
9. `GET` of an unknown id returns typed `unknown_job`; a `failed` job is retryable from `fetching` without re-upload.

## Success Criteria

Each line is binary. Wave plans map 1:1 onto a subset.

1. `uv run pytest -q` exits 0; `web/` `tsc --noEmit` exits 0 when web surfaces change.
2. A scripted research turn with Fake or live retrieve can **done** with citations whose spans occur only in this-run retrieve/`read_file` payloads — not by reading a serve-time extracted tree SSOT.
3. With the local extracted tree absent or unused on the serve path, retrieve/`read_file` still return typed misses or MCP/Fake bodies (no silent empty success that implies a local tree).
4. Thread attach of a fixture file makes that text available to the model on that turn and does **not** create a new pushed chunk on the retrieve backend (Fake or assert no ingest call).
5. Corpus-ingest of a fixture file of a supported format results in a push (or Fake ingest record) and a subsequent retrieve can hit that text.
6. Calling 切分 as a research tool is impossible (no such tool on the graph) or is rejected by contract test.
7. Unsupported format on corpus ingest is a named failure, not an empty 200 with silent skip.
8. MCP/ingest failure on corpus ingest is typed; the foreground thread attach path still completes.
9. Citation verifier still refuses unknown locators; memory/advisory text still cannot satisfy the gate (ADR 0035).

## Open Questions

(none)

## Inherits / Changes

**Inherits:** retrieve port ADR 0032; 切分归 fin ADR 0037; citations from this-run evidence ADR 0021 as amended 0046; company corpus via retrieve not dream ADR 0045; workspace ADR 0023–0026; Soft-Track ADR 0027; `specs/corpus-retrieval.md` format table and 父子分块 contract (numerical table values remain headroom); pytest Fake retrieve.

**Changes this spec adds:** serve-time extracted tree is not company SSOT (ADR 0047); two upload doors; ingest endpoint is the only 切分 caller from product UI; version mutate via MCP.

**Changes this spec adds (amendment 2026-09-25):**

1. **Async accept contract** — `POST /api/corpus/ingest` becomes accept-and-persist (raw bytes + pointer + `pending` job record + stable identity) and returns `job_id`; parse / 切分 / push / invalidate run asynchronously. Supersedes the synchronous endpoint contract recorded earlier in this file.
2. **`document_id` derivation** — `"{TICKER}-{FYQ}"` from a required `company` + `period`; explicit `document_id` wins; the filename never participates; missing both is typed `document_id_missing`. Supersedes the filename-stem fallback in `resolve_document_id`.
3. **`content_identity` authority and fin's job-identity pair** — iknow computes `content_identity` over the whole pushed body, stores it in a first-class unique column (jointly with `source_ref`), and returns the stored value in the push response; fin puts no identity field on the wire (strict envelope), reads that stored value as its only authoritative `content_identity`, and treats local `body_content_id` as bookkeeping only; fin's idempotency pair is `(source_ref, content_identity)` on the value read back, with OVERWRITE on change.
4. **Seven-state job vocabulary reused** — the queued job state is `pending`; no `queued` / `running` / `stale`; the HTTP gate is wired to `IngestStateStore` and the status `GET` exposes the real state.
5. **Size cap pinned** — retain 50 MiB as exactly `52428800` bytes; exact-cap accepted, one-byte-over typed-rejected.
6. **Validation staging pinned** — unknown format / invalid doc_type / missing identity rejected before acceptance; byte-dependent parse and push failures are typed job results after acceptance.
7. **Duplicate / replacement / conflict rule** — same `(document_id, version)` idempotently joins one job; replacement invalidates stale chunks; a different version during an in-flight job is typed `concurrent_run`; never two logical versions.
8. **Graph/C boundary** — no fin API claims corpus-wide graph/C readiness from a successful push; graph/C readiness is a separate pass-through defined by `specs/corpus-index-job-orchestration.md` (iknow-memory T1, not yet in this repo).

## Cross-repo contract dependencies

- **`specs/corpus-index-job-orchestration.md`** — fin-side graph/C readiness states and the push → index-job handoff semantics. **Does not exist in this repository yet**; created by iknow-memory T1. fin depends on it and must not restate or invent its states.
- **iknow-memory `content_identity` column** — the first-class column plus uniqueness constraint on `(source_ref, content_identity)` and OVERWRITE-on-change behavior behind decision 2. Defined in the iknow-memory spec; fin commits to **reading the stored `content_identity` from the push response** and keying the `(source_ref, content_identity)` idempotency pair on that value — it sends no identity field on the wire and treats local `body_content_id` as bookkeeping only.
- **iknow-memory bounded `read_source`** — the exact-`source_ref` bounded read contract consumed by fin's `read_source`; inherited, not redefined here.

## 待写入 (persist after ACR)

empty — flushed 2026-09-04: CONTEXT **ingest endpoint** / **thread attach** / **corpus ingest** + **read_file** / **list_files**; ADR 0047 accepted.

Flushed 2026-09-25 (amendment) — to persist after T1:

- **ADR (new, number after the current max):** *Asynchronous permanent corpus ingest: accept-time durability, stable `document_id`, job identity handoff, and the graph/C readiness boundary.* It records: (a) `POST /api/corpus/ingest` is accept-and-persist and returns `job_id`; `accepted` requires raw bytes + pointer + `pending` job record + `(document_id, version)` identity to be durable; (b) `document_id = "{TICKER}-{FYQ}"` from a required `company`+`period`, filename is metadata only, explicit id wins, missing is typed `document_id_missing`; (c) iknow computes `content_identity` over the whole pushed body, stores it in a first-class unique column, and returns it in the push response; fin reads that stored value (no identity field crosses the wire; local `body_content_id` is bookkeeping only, no T4 recompute), keys idempotency on the `(source_ref, content_identity)` pair, and a different content identity on the same `source_ref` invalidates stale chunks and makes iknow OVERWRITE; (d) the seven `IngestState` values are reused, the queued state is `pending`, and the HTTP gate is wired to `IngestStateStore`; (e) the 50 MiB cap is `52428800` bytes, exact accepted / one-over rejected; (f) format recognition is rejected before acceptance and byte-dependent parse/push failures are typed job results after; (g) same `(document_id, version)` joins idempotently, replacement invalidates, a concurrent different version is typed `concurrent_run`; (h) no fin API claims corpus-wide graph/C readiness from a push — that state is a pass-through of iknow-memory's index job.
- **CONTEXT glossary (needs `domain-modeling` to author):** extend **ingest endpoint** so the queued state is `pending` and acceptance is asynchronous with a durable `job_id`; add the identity rule (`"{TICKER}-{FYQ}"`, filename is metadata only) and the fin-side split of per-source **text** readiness (`ready`) from corpus-wide **graph/C** readiness; add the `(source_ref, content_identity)` job-identity pair and that fin reads the authoritative `content_identity` from the push response rather than sending it on the wire.
- **iknow-memory spec (sibling repo, not written here):** the `content_identity` first-class column + uniqueness constraint, the OVERWRITE-on-change semantics, and the graph-index job states behind corpus-wide graph/C readiness (`specs/corpus-index-job-orchestration.md`).

## architecture-change-reviewer

bounded-context-guardian: yes — ingest and 切分 stay a corpus/host capability; ranking stays behind **retrieve port**; web only offers attach vs ingest; no `controllers/` layer.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–8.
error-handling-enforcer: yes — named ingest/MCP/unsupported-type failures; no silent skip-as-success; attach failure must not look like ingest success.
complexity-anti-drift: yes — three foci (serve evidence, ingest endpoint, two doors); writing-plans must not dump connectors or memory-dream into this plan.
minimal-change-verifier: yes — one logical program, not one commit; writing-plans sequences one wave = one tracer bullet.

**Amendment 2026-09-25 (pre-implementation, permanent corpus upload T1):**

bounded-context-guardian: yes — the async job, `document_id` derivation, `content_identity` handoff, and size/validation rules stay inside the corpus/host capability; graph/C readiness and the `content_identity` column stay in iknow-memory via `specs/corpus-index-job-orchestration.md`; fin adds no second splitter and no graph node; the two-door contract is unchanged.
defensive-contract-validator: yes — the amendment names empty / negative / overflow / concurrent / exception for the async path (missing identity, unknown format, exact/over-cap bytes, duplicate/concurrent submissions, dependency faults, compensating-cleanup failure) and binds each to a typed error or job state in the error/state vocabulary and acceptance criteria 1–9.
error-handling-enforcer: yes — accept-time rejections persist nothing; byte-dependent failures are typed `failed` state, never empty success; a failed compensating delete is observable and retryable; `unknown_job` is typed; no push implies graph/C readiness.
complexity-anti-drift: yes — seven existing states reused (no `queued`/`running`/`stale`); the amendment retires the synchronous path rather than adding a parallel one; readiness meanings are split, not duplicated; the graph/C half is referenced, not restated.
minimal-change-verifier: yes — reuses the existing `IngestEndpoint` chain, `IngestStateStore`, object-store pointer split, push envelopes, and invalidation path; the only removals are the filename-stem id fallback and the synchronous completion token; no new library, ranking stack, or second job store.

**OVERALL: PASS — hand to writing-plans.** Spec path: `specs/corpus-serve-ingest.md`.

Affected surfaces (declared): `src/fin/host/` (retrieve/read wiring, ingest HTTP), `src/fin/corpus/` (endpoint, table), `web/src/` (attach vs ingest), `tests/host/` / `tests/corpus/`.
