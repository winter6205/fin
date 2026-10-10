// Shared types mirroring the backend contract (see
// src/fin/host/workspace_http.py for the SSOT of each field).

export interface ThreadListItem {
  id: string;
  title: string;
  created_at: string;
}

// How the UI labels one source unit: "page" only when the unit is a
// physical PDF page; every other unit reads "unit" (spec
// thread-attachment-source-read). "record" is a ledger row: a record
// has no page and no ordinal (ADR 0091).
export type SourceDisplay = "page" | "unit" | "record";

/** The quotable evidence kinds. `database-row` is a ledger row from the
 * `sql` 档 — a record, not a document passage, verified by its record
 * key and this round's returned fields rather than by a text span
 * (ADR 0091). */
export type CitationKind = "company" | "attachment" | "database-row";

/** One JSON scalar of a snapshot (spec §4.4 value-text rule; R2 of
 * the E1 reconcile notes: ingress values are scalars and the consumer
 * renders each to text — strings as-is, numbers as shortest round-trip
 * decimal, booleans lowercase, null as ""). */
export type DatabaseScalar = string | number | boolean | null;

/** The retained snapshot a `database-row` citation opens.
 *
 * It is the evidence THIS answer was built from, with the `queried_at`
 * of the query that produced it. Opening a citation shows this and
 * issues NO query; a refresh is a new query with a new time, and it
 * never rewrites a prior answer (spec 8.1). Nothing here states that
 * the data is unchanged — the product does not claim that without a
 * fresh query, so a reader cannot mistake silence for stability.
 *
 * ``schema`` names the shape and ``schema_version`` versions it
 * (spec §2.1); E5 mints both on live emissions. Snapshots stored before
 * the token are legacy v0 and simply lack the key (spec §7) — absent is
 * valid, an UNKNOWN value never decodes. Both stay optional together:
 * a row declares the shape, or it declares nothing. */
export interface DatabaseRowSnapshot {
  schema?: "fin.evidence.database_row_snapshot";
  schema_version?: number;
  // Which permitted view answered, so the read scope is auditable from
  // the citation alone.
  source: string;
  // The row's own identity: a claim traces to a record, not to a
  // position in a text.
  record_key: Record<string, DatabaseScalar>;
  // When the values were true. A ledger changes; a value with no time
  // is a claim about a moving target.
  queried_at: string;
  // The values this round actually returned.
  fields: Record<string, DatabaseScalar>;
  // One display line, minted by the host from those fields.
  summary: string;
}

export interface Citation {
  kind: CitationKind;
  // Company filings carry document_id; an attachment citation sends "",
  // and so does a database-row citation — a ledger row has no filing
  // behind it and naming one would point at a document the query never
  // read.
  document_id: string;
  // Attachment identity; a company citation sends "". The filename is
  // display-only — grouping and DOM keys use this id.
  attachment_id: string;
  version: string;
  // 1-based source unit (a PDF page for physical filings, a parsed
  // chunk otherwise). A ledger row has no ordinal and sends 1.
  unit: number;
  display: SourceDisplay;
  // Safe display name for an attachment; a company citation sends "".
  filename: string;
  quote: string;
  // The retained snapshot a database-row citation opens. Absent on a
  // document citation, which is why the host dumps it with
  // `exclude_none` — every stored filing citation keeps the exact wire
  // shape it had before this kind existed.
  database_row?: DatabaseRowSnapshot;
}

// The attachment leg of a ``source_ref`` — the locator ``read_source``
// resolves for an attachment source (spec
// thread-attachment-source-read: ready attachment hits carry
// ``source_ref={kind:"attachment",attachment_id,version,unit}``).
export interface AttachmentSourceRef {
  kind: "attachment";
  attachment_id: string;
  version: string;
  // One-based frozen-unit ordinal.
  unit: number;
}

// ----- The visual-observation lane -----
//
// One row of the ``visual_claims`` SSE event, repeated verbatim on the
// history endpoint's turn object. Mirrors
// ``fin.host.visual_evidence.VisualEvidence.as_claim_row()``: that key
// set IS the contract (VISUAL_CLAIM_FIELDS), and a consumer reading a
// field not listed here is guessing.
//
// There is deliberately NO answer text in the row. The observation body
// stays with the model, so this surface shows provenance and can never
// be rendered as a verbatim source quote (spec
// thread-attachment-source-read lines 58 and 65).

// The region actually looked at: normalized ``[left, top, right, bottom]``
// bounds in the page image's own coordinate space. ``null`` = no reliable
// box existed, so the whole rendered page was inspected.
export type VisualObservationScope = [number, number, number, number];

export interface VisualClaim {
  kind: "visual_observation";
  attachment_id: string;
  // The parsed-content version the observation was produced from.
  version: string;
  // One-based frozen-unit ordinal this page maps to.
  unit: number;
  // Physical page of the rendered image the model read.
  page: number;
  // Which persisted observation version answered the ask.
  observation_version: number;
  scope: VisualObservationScope | null;
  provider: string;
  model_version: string;
  // SHA-256 of the raster the observation was made from — what ties the
  // claim back to this exact page image.
  page_image_sha256: string;
  // The exact chart / series / category target the observation answers.
  detail: string;
  // Safe display name; identity stays ``attachment_id``.
  filename: string;
  // True = this run read a stored observation instead of asking the
  // model again; no re-upload and no vision call happened.
  reused: boolean;
  // Outbound attempts this read cost the run (0 on a pure reuse).
  outbound_calls: number;
}

// The closed §6 name set of specs/enterprise-evidence-contract.md (plus
// the delegated verifier exits the gate owns and the refusal lanes
// legitimately carry). A refusal code outside this set is itself a
// malformed entry — prose never rides this lane (plan E5: "per-row codes
// are the §6 names, never prose").
export type EvidenceRefusalCode =
  | "malformed_evidence_contract"
  | "unsupported_evidence_contract"
  | "citation_overflow"
  | "visual_claim_overflow"
  | "database_row_overflow"
  | "business_field_overflow"
  | "comparison_overflow"
  | "corpus_not_ready"
  | "insufficient_evidence"
  | "sql_no_matching_rows"
  | "sql_query_denied"
  | "citation_trust_denied"
  | "visual_claim_trust_denied"
  | "business_field_trust_denied"
  | "comparison_source_unverified"
  | "attachment_unavailable"
  | "observation_stale"
  | "visual_lane_unwired"
  | "retrieve_unavailable"
  | "persistence_unavailable"
  | "tier_not_open"
  | "real_db_not_wired"
  | "unknown_locator"
  | "citation_repair_cap";

// One per-row evidence refusal as the wire names it: the SSE
// ``evidence_refusals`` entry and the history read's
// ``rejected_citations`` row share this shape (E5 carrier rule), and the
// web decoder keeps both in the one ``Turn.evidence_refusals`` field so
// a reloaded turn renders identically to a streamed one.
export interface EvidenceRefusal {
  citation_index: number;
  error: EvidenceRefusalCode;
}

export type RunStatus = "done" | "refuse" | "step_cap";

// Stream error taxonomy for mid-flight failures (model raise,
// network drop, etc.). Maps to a small "原因" label the SPA
// renders on refuse rows so an auth / rate_limit / timeout reads
// distinctly instead of as one opaque English sentence.
export type StreamErrorKind =
  | "auth"
  | "network"
  | "timeout"
  | "rate_limit"
  | "not_found"
  | "server"
  | "unknown";

// Soft-Track (specs/soft-track.md): per-turn host contract. Optional
// on the wire until the host peer exposes SSE/GET ``track``.
export type SoftTrack = "dialogue" | "clarify" | "off_topic" | "grounded";

// Soft-Track RunRecord audit blob. Fields beyond ``track`` stay
// optional so older rows / partial peers still typecheck.
export interface TrackDecision {
  track: SoftTrack;
  confidence?: number;
  decision_tier?: string;
  signals?: Record<string, unknown>;
}

// T3 (spec line 32): one human-readable row per tool call observed
// in the run; the SPA renders `summary` in the tools region and
// correlates with the live SSE frames by `tool_call_id`. Mirrors
// fin.host.workspace_http.ToolSummaryPayload.
export interface ToolSummary {
  name: string;
  summary: string;
  tool_call_id: string;
}

// turn-trace-folds (the tracked change): one sibling burst on the per-turn trace
// timeline — a thinking burst or a tool burst, in arrival order.
// ``live`` is true while that burst is still accepting frames; at
// burst end it flips false and the fold auto-collapses (the UI keeps
// a manual override so an operator-opened fold stays open).
export type TraceItem =
  | { kind: "thinking"; text: string; live: boolean }
  | { kind: "tools"; calls: ToolSummary[]; live: boolean };

// ----- T3 comparison (enterprise-kb Focus 1) -----
//
// The ``fin.comparison`` envelope produced by the
// ``compare_facts`` host tool. SSOT for the shape (including the
// typed EXITs this SPA never renders) is
// src/fin/host/tools/compare_facts.py; these interfaces mirror it
// field-for-field so the table renders STRUCTURE instead of
// re-parsing a prose table out of ``answer``.
//
// The envelope arrives twice by design: live on the ``comparison``
// SSE frame, and persisted on ``Turn.comparisons`` after a reload.
// Both carry byte-identical entries.

export interface ComparisonLocator {
  // The row's source discriminator, read by ``citationSourceId`` so the
  // table and the citation chips never resolve a source differently.
  // Optional because the v1 producer (``compare_facts._locator_dict``)
  // sends document_id / version / unit only: an absent ``kind`` is a
  // company row. An attachment fact row needs the producer to send this
  // field plus attachment_id before the table can name its source.
  kind?: "company" | "attachment";
  document_id: string;
  attachment_id?: string;
  version: string;
  // 1-based source unit.
  unit: number;
  display?: SourceDisplay;
}

export interface ComparisonColumn {
  key: string;
  label: string;
}

// One (row, column) intersection. ``value`` is null on a miss AND
// on a textual XBRL fact; ``display`` is always renderable, so the
// table reads ``display`` and never formats a number itself.
export interface ComparisonCell {
  row: string;
  column: string;
  status: "hit" | "missing";
  value: number | null;
  display: string;
  unit: string;
  scale: string;
  locator: ComparisonLocator | null;
}

export interface ComparisonRow {
  key: string;
  label: string;
  kind: string;
  cells: ComparisonCell[];
}

export interface ComparisonEnd {
  row: string;
  column: string;
}

// The redline between two cells. Computed by the host tool, never
// by the model and never by this client: an SPA that did its own
// arithmetic would be a second, ungated source of numbers.
export interface ComparisonDelta {
  from: ComparisonEnd;
  to: ComparisonEnd;
  absolute: number;
  // Null at a zero baseline (percent change is undefined there).
  percent: number | null;
  direction: string;
  display: string;
}

export interface ComparisonMissing {
  row: string;
  column: string;
  reason: string;
}

export interface Comparison {
  schema: "fin.comparison";
  // The envelope's version (spec §2.1). Required here exactly as the
  // token is: the spec's comparison schema lists it in ``required``, and
  // a required discriminant with an optional version is the half-versioned
  // state §2.1 exists to remove.
  schema_version: number;
  kind: string;
  axis: string;
  concept: string;
  unit: string;
  scale: string;
  columns: ComparisonColumn[];
  rows: ComparisonRow[];
  deltas: ComparisonDelta[];
  missing: ComparisonMissing[];
  hit_count: number;
  cell_count: number;
  locators: ComparisonLocator[];
}

// ----- search-mode-host T2: the choice window (ADR 0062 HITL) -----
//
// The clarify payload the host emits when the search-mode cascade
// cannot name a 档: a structured option list the operator picks from.
// Mirrors the SSE ``choice_window`` frame (SSOT:
// src/fin/host/workspace_http.py). The minimal contract is
// ``kind`` + non-empty ``options[{id,label}]``; ``title`` / ``prompt``
// / ``opened`` / ``rejected_picks`` are additive, so the same channel
// carries a future Fast-propose window (T4) without a new frame type.

export interface ChoiceOption {
  id: string;
  label: string;
  // False = the 档 exists but is not open: the button stays visible
  // and disabled. Absent on an older peer — treat as open.
  opened?: boolean;
}

export interface ChoiceWindow {
  // Payload-level dispatch key. This client renders any kind on this
  // channel the same way (options + pick); the label copy comes from
  // the host payload, never from a client-side per-kind table.
  kind: string;
  title?: string;
  prompt?: string;
  options: ChoiceOption[];
  // Ids the host refused on a previous resume; non-empty only when
  // the window is re-issued after an invalid pick.
  rejected_picks?: string[];
}

export interface Turn {
  run_id: string;
  question: string;
  status: RunStatus;
  status_reason?: string;
  answer: string;
  citations: Citation[];
  // E6 (the tracked change): this run's per-row evidence refusals — the decoded
  // SSE ``evidence_refusals`` entries and the history read's
  // ``rejected_citations`` rows, folded into ONE field so a reloaded turn
  // renders identically. A refused citation is never silently dropped and
  // never an empty citation list: each entry is a visible named-code
  // indicator beside the answer (display policy, plan E6).
  evidence_refusals?: EvidenceRefusal[];
  // T3: the run's streamed thinking prose (header-only when the
  // thinking fold is collapsed). Empty when the model surfaced no reasoning.
  thinking_text?: string;
  // T4 (#37): total thinking duration in ms. After the run the
  // thinking region collapses to a one-line duration label
  // ("Thought for 4.2s"); 0 / undefined when no thinking streamed.
  thinking_duration_ms?: number;
  // T3: one entry per tool call observed this run (dicts, not
  // strings - T1's placeholder shape).
  tool_summaries?: ToolSummary[];
  // turn-trace-folds (the tracked change): the burst timeline this session folded
  // from the live stream. Optional — GET-restored rows build it in
  // the view layer (traceItemsFromTurn) since the server keeps no
  // burst history. Maintained in parallel with thinking_text /
  // tool_summaries, which stay the persisted SSOT.
  trace_items?: TraceItem[];
  // T2: ordinary-person copy on refuse / step_cap (empty on done).
  // Mirrored on the SSE status frame and the persisted turn row so
  // reload keeps the copy.
  assistant_message?: string;
  // Soft-Track: optional run track from SSE status / GET turn row.
  // Missing is OK (no hint) until the host peer populates the wire.
  track?: SoftTrack;
  // Soft-Track: optional audit blob from GET history / RunRecord.
  track_decision?: TrackDecision;
  // T4 (spec line 38): GET-while-running signal on the turn row.
  // Mirrors ``fin.host.workspace_http.TurnRow.in_flight``. True
  // only on the most-recent row of a thread whose lock is held,
  // which by construction can only be the latest turn. Drives the
  // T4 banner + Resume button in ThreadPage.
  in_flight?: boolean;
  // Mid-flight stream error taxonomy on refuse rows. Mirrors
  // ``fin.host.workspace_http.TurnRow.status_classified``. Empty
  // string on rows whose refusal came from the gate rather than
  // the stream engine; the SPA keys a small "原因" label off this
  // field so a 401 / 429 / 504 reads distinctly instead of as one
  // opaque English sentence.
  status_classified?: StreamErrorKind | "";
  // T3 (enterprise-kb Focus 1 / SC2): structured comparison tables
  // this turn's ``compare_facts`` calls produced. Mirrors
  // ``fin.host.workspace_http.TurnRow.comparisons``. Non-empty on
  // refuse rows too — on a ``numeric_gate_fail`` the table holds
  // the numbers the filing actually reported, which is the
  // evidence for the refusal.
  comparisons?: Comparison[];
  // The run's visual-claim rows (see ``VisualClaim``). Filled by
  // the ``visual_claims`` SSE frame while the run streams and by the
  // same array on the GET history row after a reload — one field, both
  // doors, so a reloaded turn renders identically. A turn that made no
  // visual claim carries an empty array (never missing on the wire);
  // optional here only so a peer predating the lane still typechecks.
  // NEVER part of ``citations``: a generated observation is not a source
  // quote, so it reaches the operator through its own surface
  // (components/VisualClaims.tsx) instead of a chip.
  visual_claims?: VisualClaim[];
  // T8: the operator's latest thumb for this run ("up" / "down").
  // Null / absent when no thumb was ever set. A side record keyed by
  // run_id (mirrors ``fin.host.workspace_http.TurnRow.feedback``) —
  // setting or flipping it never changes the run's citations.
  feedback?: "up" | "down" | null;
  // search-mode-host T2: the choice window this suspended run is
  // waiting on. Present = the run paused before any terminal frame
  // and the row is NOT done; a pick (or a terminal status frame)
  // clears it. Live-only on THIS row: a reloaded pause has no row yet,
  // so GET carries the same window on ``ThreadDetail.pending_turn``
  // instead (the per-turn field stays absent after a reload).
  choice_window?: ChoiceWindow;
  created_at: string;
}

export interface ThreadDetail extends ThreadListItem {
  turns: Turn[];
  // T4: thread-level live running signal (lock held, no RunRecord
  // persisted yet). The Composer reads this too so submission is
  // disabled BEFORE the optimistic turn surfaces. Mirrors
  // ``ThreadDetailResponse.in_flight``.
  in_flight?: boolean;
  // T4 fix (code-review H1): a checkpoint with ``next`` nodes is
  // present AND the lock is NOT held — i.e. the process died
  // mid-run and the operator should see the Resume banner. The
  // banner used to be keyed off the live ``in_flight`` flag,
  // which made it appear during a process-alive refresh (when
  // it is forbidden) and never appear after a real crash (when
  // it is needed). The server derives this from
  // ``pending_nodes(thread_id) and not is_locked``.
  pending_checkpoint?: boolean;
  // T2f (review C4): WHAT a paused run waits for while
  // ``pending_checkpoint`` is true — ``"attachment_preparation"``
  // (the durable prep job owns the auto-resume, no operator action)
  // or ``"choice_window"`` (the operator's pick owns it); ``null``
  // for a plain crash checkpoint. Mirrors
  // ``fin.host.workspace_http.ThreadDetailResponse.pending_interrupt_kind``.
  pending_interrupt_kind?: string | null;
  // A choice-window turn paused in the graph checkpoint is NOT a
  // persisted RunRecord, so a reload cannot find it in ``turns[]``.
  // The host projects the paused turn's exact checkpoint identity,
  // original question, and choices here so reopening the thread
  // restores the prompt WITHOUT synthesising history. Present only
  // while a choice interrupt is paused and the thread lock is not
  // held; absent / ``null`` otherwise. Mirrors
  // ``fin.host.workspace_http.ThreadDetailResponse.pending_turn``.
  pending_turn?: {
    run_id: string;
    question: string;
    choice_window: ChoiceWindow;
  } | null;
  // T3 (ADR 0044): token occupancy from the SAME host figure the
  // auto-compact gate computes. Undefined for a thread with no
  // checkpoint — the indicator renders nothing rather than a
  // fabricated 0%. Never mirrors onto ``WorkspaceStatus``:
  // ADR 0033 keeps ``GET /api/status`` boot-scoped.
  occupancy?: {
    estimated_tokens: number;
    budget_tokens: number;
    ratio: number;
  };
  // question-workspace W1/W4: the authoritative thread-scoped task
  // envelope Runtime persists. Present on every GET from a host that
  // carries the projection; ``undefined`` on an older peer, which
  // reads as TASKLESS rather than as a broken task (the contract's
  // own posture: "an absent envelope is genuinely empty"). Deliberately
  // NOT derived from ``turns`` — see ``TaskRuntimeView``.
  task_runtime?: TaskRuntimeView;
}

// ----- T4 workflow (the tracked change): watchlist + in-app notices -----
//
// Mirrors fin.host.watchlist (``fin.watchlist.v1``). ``forms`` empty
// means every form type — the operator watched the issuer, not a
// filing kind.

export interface WatchEntry {
  id: string;
  issuer: string;
  forms: string[];
  created_at: string;
}

// One watched filing that is now in the LOCAL catalog. In-app only:
// spec Focus 3 forbids outbound email / Slack, so ``read`` is the
// operator's own dismissal, not a delivery receipt.
export interface Notice {
  id: string;
  watch_id: string;
  issuer: string;
  form: string;
  document_id: string;
  period: string;
  created_at: string;
  read: boolean;
}

// T10 (ADR 0033, amended by workbench-retrieve-product-seam
// ADR 0051): the boot-scoped workspace status surface behind
// ``GET /api/status``. Boot-scoped means configuration plus
// dependency reachability — nothing run- or turn-scoped rides here
// (no step budget, no token usage, no soft-track confidence, no
// "% of context window"). Mirrors
// fin.host.workspace_http.WorkspaceStatusResponse.

// The product **知识 MCP** handshake outcome published at boot:
// 缺 (``missing``, no command) / 坏 (``fault``, handshake failed) /
// 可达 (``reachable``, ``initialize`` + ``tools/list`` succeeded).
// This REPLACES the retired two-state ``retrieve_reachability``.
export type KnowledgeMcpState = "missing" | "fault" | "reachable";

// Which port is behind the retrieve seam: the product iknow-memory
// client, or the hermetic fake that answers with no corpus. A fake
// boot publishes ``knowledge_mcp: "missing"`` — never 可达.
export type RetrieveAdapterKind = "mcp" | "fake";

export interface WorkspaceStatus {
  knowledge_mcp: KnowledgeMcpState;
  retrieve_adapter_kind: RetrieveAdapterKind;
  // The resolved ``FIN_MODEL`` slug only — the host never puts the
  // endpoint or key material on this wire.
  model_identity: string;
  // From the same catalog that serves ``GET /api/corpus``; zero is a
  // valid value, not an error. NOT a 就绪 signal (spec SC7) — read
  // ``bound_collection_name`` for the bind, and iknow's own 资料页 for
  // readiness.
  corpus_filing_count: number;
  // T4 (specs/first-closed-loop.md): the 资料集合 this process is bound
  // to, or the sentinel ``"unbound"`` when it is bound to none (a
  // test-only adapter, or a product boot whose 知识 MCP never reached
  // 可达). The v1 bind is the one process-wide collection ADR 0049
  // settles. The name itself is a documented MIRROR of iknow-memory's
  // ``SOURCE_DOC_COLLECTION_NAME``: no MCP tool returns a collection
  // identity, and iknow's separate dashboard HTTP process is not a
  // dependency fin may take. Carries no readiness claim.
  bound_collection_name: string;
}

export interface Filing {
  document_id: string;
  company: string;
  period: string;
  version: string;
  page_count: number;
  // Spec SC3: the EDGAR form type behind the row (``10-Q``, ``10-K``,
  // ``8-K``, ...). ``null`` on a corpus row the catalog was never
  // given a form for; optional so a peer predating SC3 still
  // typechecks.
  form?: string | null;

  // T1 does not render these; they exist in the payload.
  [key: string]: unknown;
}

// ----- corpus-serve-ingest T5: the two upload doors (ADR 0047) -----

// The observable states of a durable corpus job (mirrors
// fin.corpus.ingest_state.IngestState). ``pending`` is the QUEUED state
// the accept response reports — bytes, pointer, job record and stable
// identity are durable and nothing has been parsed yet. ``ready`` is
// PER-SOURCE TEXT readiness only; it is never corpus-wide graph/C
// readiness (that is the separate ``graph`` field below).
export type IngestJobState =
  | "pending"
  | "fetching"
  | "extracting"
  | "ocr"
  | "cataloging"
  | "ready"
  | "failed";

// Success body of POST /api/corpus/ingest (the corpus-ingest door).
// Mirrors fin.host.workspace_http.corpus_ingest's 200 payload. This is
// an ACCEPT, not a completion: 切分 / push / invalidation run off the
// request, so the outcome is followed through ``JobStatus`` below.
// Every ingest failure is a named ``error`` code instead of a
// partial-success 200 — see api.IngestFailedError.
export interface IngestResult {
  status: "accepted";
  // The store keys jobs by ``document_id``, so v1 answers
  // ``job_id === document_id``; the versioned identity is the pair
  // ``(document_id, version)``.
  job_id: string;
  document_id: string;
  version: string;
  state: IngestJobState;
}

// ----- T4: the corpus-wide graph/C report (sibling index job) -----

// One per-source entry of iknow's graph-index job, passed through by
// fin verbatim. These states belong to the SIBLING job and are not
// fin's ``IngestJobState``; an entry's ``ready`` is that source's own
// index state and never the corpus-wide graph/C claim — that is
// reported only by ``community_index_state`` below (spec
// corpus-serve-ingest "Graph/C readiness").
export interface GraphIndexJob {
  job_id: string;
  source_ref: string;
  status: "in_progress" | "ready" | "failed" | "stale";
  failure_code: string | null;
}

// The community-level state of the same sibling job. ``status`` is an
// open string because fin does not define its vocabulary — it only
// forwards what iknow confirmed. ``null`` means no report yet.
export interface GraphCommunityIndexState {
  status: string;
  failure_code: string | null;
}

// Body of the ``graph`` field of GET /api/corpus/ingest/{job_id}: the
// pass-through of the sibling index job's confirmed result, or
// ``null`` while nothing has been reported. ``error`` is a named read
// failure and must be rendered, never silently hidden.
export interface GraphIndexReport {
  jobs: GraphIndexJob[];
  community_index_state: GraphCommunityIndexState | null;
  error: { code: string; message: string } | null;
}

// Body of GET /api/corpus/ingest/{job_id} — the polled job status.
// ``text_ready`` is fin's per-source readiness, derived from ``state``
// being ``ready``; it is a distinct field precisely so the two
// readiness meanings never merge into one boolean. ``graph`` carries
// the corpus-wide graph/C report from the sibling index job (see
// ``GraphIndexReport``); the client never derives graph/C readiness
// from ``text_ready`` / ``state`` or from ``jobs[].status``.
export interface JobStatus {
  job_id: string;
  document_id: string;
  version: string;
  state: IngestJobState;
  error_type: string | null;
  error_message: string | null;
  text_ready: boolean;
  graph: GraphIndexReport | null;
}

// ----- Thread attachment: the attachment lifecycle -----

// Closed status set of a thread attachment (ADR 0089): the upload door
// stores raw bytes only and answers ``uploaded``, so a record can be
// durable before any preparation exists.
//
// - ``uploaded``: the original bytes are durable; preparation has NOT
//   been requested, and the row will not change by itself.
// - ``parsing``: a grounded graph turn requested preparation and it is
//   running. The only state the SPA follows with a readiness poll.
// - ``ready``: parsed units, required visual derivatives and the
//   thread-private index are usable — the record is citable.
// - ``failed``: preparation stopped with a named reason retry can
//   clear.
export type AttachmentStatus =
  | "uploaded"
  | "parsing"
  | "ready"
  | "failed";

// Reasons the SPA names today. The wire may gain more (the parse
// exits in specs/thread-attachment-source-read.md), so display code
// must fall back to generic copy for an unrecognised reason rather
// than break.
export type AttachmentFailureReason =
  | "attachment_empty_text"
  | "attachment_index_unavailable"
  | "attachment_not_text"
  | "source_unavailable";

// One attachment of a thread (the thread-attach door). ``scope`` is
// always "thread_attach": the model may read the text, nothing is
// pushed to the permanent corpus (SC4). Unlike the old door the
// id is NOT single-use: it stays referenceable for the whole thread
// lifetime while ``status`` tells whether a run may cite it yet.
// Every attachment door — upload, list, status, retry — answers with
// exactly this shape, so one record never has two field sets.
export interface ThreadAttachment {
  attachment_id: string;
  filename: string;
  size_bytes: number;
  scope: "thread_attach";
  status: AttachmentStatus;
  // Parsed-content version; 0 until the first preparation publishes one.
  version: number;
  unit_count: number;
  searchable: boolean;
  failure_reason?: AttachmentFailureReason | string | null;
}

// ----- question-workspace: the task runtime wire projection -----
//
// SSOT: specs/question-task-runtime-contract.md. Mirrors
// ``fin.host.workspace_http.TaskRuntimeView`` — every spelling below is
// the frozen one, and this surface NEVER derives a task fact from a
// turn fact: ``TaskRuntimeView`` is deliberately a SEPARATE field from
// ``turns`` and ``status``, because a turn can end with an answer while
// its task stays active (contract §Representation).
//
// WHAT THIS SURFACE MAY NOT DO (contract §Ownership): change task state
// without an explicit user action, treat a saved restriction as a live
// grant, or infer task completion from a citation / run end / lookup.

/** ``fin.host.task_runtime_types.TASK_OUTCOMES``. ``null`` = taskless. */
export type TaskOutcome = "active" | "complete" | "cancelled";

/** The current task, as Runtime reports it. Mirrors
 * ``task_runtime.current_task`` (``current_task_summary``). */
export interface CurrentTask {
  task_id: string;
  goal: string;
  expected_result: string;
  task_version: number;
  necessary_conditions: string[];
  outcome: TaskOutcome;
  outcome_reason: string;
  created_at_input_version: number;
  /** The clarification bound to this task, when one is pending. */
  pending_question?: PendingQuestionView | null;
  /** Runtime's task-level outcome. Distinct from ``outcome``, which is
   * the task record's own field; they agree by construction and are
   * carried separately because the contract publishes both. */
  task_outcome?: TaskOutcome | null;
}

/** One locatable archive. Mirrors ``archived_task_summaries``.
 *
 * ``outcome`` is the archived task's OWN outcome as of archiving — an
 * archive of a still-active task reports ``active``, because archiving
 * is separate from completing or cancelling (W5). Optional so an older
 * host that omits it still decodes; the surface then reads "未记录"
 * rather than guessing. */
export interface ArchivedTaskSummary {
  archive_id: string;
  task_id: string;
  goal: string;
  task_version: number;
  archived_at_input_version: number;
  run_id: string | null;
  evidence_refs: string[];
  outcome?: TaskOutcome;
  outcome_reason?: string;
}

/** The still-pending clarification. Mirrors
 * ``task_runtime.pending_question`` (``pending_question_payload``).
 *
 * ``choices`` already carries exactly one trailing ``"other"`` — the
 * host appends it, so this surface never adds a second Other and never
 * has to guess which option was Other. */
export interface PendingQuestionView {
  question_id: string;
  task_id: string;
  task_version: number;
  /** The COMPLETE question, authored by the host. Shown before the
   * options; never truncated, never replaced by a summary. */
  prompt: string;
  choices: string[];
  input_version: number;
}

/** The committed decision about one accepted input. Mirrors
 * ``reconcile_marker``; before the first reconciliation it is ``null``,
 * not a fabricated decision. */
export interface ReconciliationView {
  reconciled_input_version: number;
  task_id: string | null;
  task_version: number | null;
  disposition: string;
  reason: string;
}

/** One entry of the bounded ORIGINAL-input window. Mirrors
 * ``bounded_input_history``. ``payload: null`` means the original was
 * never recoverable — the surface must NOT reconstruct it. */
export interface AcceptedInputRow {
  input_version: number;
  kind: "message" | "reply" | "restore";
  payload: string | null;
  provenance: string;
  question_id: string | null;
  archive_id: string | null;
  choice_id: string | null;
  run_id: string | null;
}

/**
 * The closed named-failure vocabulary of the task runtime (contract
 * §Named failures; SSOT ``fin.host.task_runtime_types.TASK_FAILURE_KINDS``).
 *
 * A task door can refuse a request two ways, and both must read as a
 * refusal rather than as a transport hiccup: BEFORE any run, as an HTTP
 * 400/409 whose body is ``{"detail": {"error": <kind>}}``; and MID-STREAM,
 * as a terminal ``status`` frame carrying ``refuse`` with the same token in
 * ``reason`` / ``status_classified``. Listing the set here is what lets a
 * caller tell those two apart from an ordinary run refusal.
 */
export const TASK_FAILURE_KINDS: ReadonlySet<string> = new Set([
  "task_input_invalid",
  "task_input_unreconciled",
  "task_already_reconciled",
  "task_permission_denied",
  "task_dependency_failure",
  "task_capability_unavailable",
  "task_capability_unsupported",
  "task_insufficient_evidence",
  "task_applicability_unresolved",
  "task_intent_missing",
  "task_execution_limit",
  "task_checkpoint_failed",
  "task_history_failed",
  "task_reply_expired",
  "task_reply_unknown",
  "task_restore_unknown",
  "task_restore_not_adopted",
]);

/** Whether a terminal stream outcome names a task-door refusal. */
export function isTaskFailureKind(value: string | null | undefined): boolean {
  return typeof value === "string" && TASK_FAILURE_KINDS.has(value);
}

/** The thread-scoped task envelope on ``GET /api/threads/{id}``. */
export interface TaskRuntimeView {
  current_task: CurrentTask | null;
  archived_tasks: ArchivedTaskSummary[];
  pending_question: PendingQuestionView | null;
  reconciliation: ReconciliationView | null;
  task_outcome: TaskOutcome | null;
  input_version: number;
  input_history: AcceptedInputRow[];
}
