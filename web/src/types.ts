// Shared types mirroring the backend contract (see
// src/fin/host/workspace_http.py for the SSOT of each field).

export interface ThreadListItem {
  id: string;
  title: string;
  created_at: string;
}

export interface Citation {
  document_id: string;
  version: string;
  page: number;
  quote: string;
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
export type SoftTrack = "dialogue" | "clarify" | "off_topic" | "research";

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

// turn-trace-folds (#250): one sibling burst on the per-turn trace
// timeline — a thinking burst or a tool burst, in arrival order.
// ``live`` is true while that burst is still accepting frames; at
// burst end it flips false and the fold auto-collapses (the UI keeps
// a manual override so an operator-opened fold stays open).
export type TraceItem =
  | { kind: "thinking"; text: string; live: boolean }
  | { kind: "tools"; calls: ToolSummary[]; live: boolean };

// ----- T3 comparison (enterprise-kb Focus 1) -----
//
// The ``fin.comparison.v1`` envelope produced by the
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
  document_id: string;
  version: string;
  page: number;
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
  kind: "issuer" | "document";
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
  direction: "up" | "down" | "flat";
  display: string;
}

export interface ComparisonMissing {
  row: string;
  column: string;
  reason: string;
}

export interface Comparison {
  schema: "fin.comparison.v1";
  kind: "table";
  axis: "period" | "peer" | "matrix" | "single";
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
  // Present on wire entries (both SSE and persisted); correlates
  // the table with the tool row that produced it.
  tool_call_id?: string;
}

export interface Turn {
  run_id: string;
  question: string;
  status: RunStatus;
  status_reason?: string;
  answer: string;
  citations: Citation[];
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
  // turn-trace-folds (#250): the burst timeline this session folded
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
  // T8: the operator's latest thumb for this run ("up" / "down").
  // Null / absent when no thumb was ever set. A side record keyed by
  // run_id (mirrors ``fin.host.workspace_http.TurnRow.feedback``) —
  // setting or flipping it never changes the run's citations.
  feedback?: "up" | "down" | null;
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
}

// ----- T4 workflow (#138): watchlist + in-app notices -----
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
  // valid value, not an error.
  corpus_filing_count: number;
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

// Success body of POST /api/corpus/ingest (the corpus-ingest door).
// Mirrors fin.host.workspace_http.corpus_ingest's 200 payload. The
// push is observable here as written / duplicates / block_ids;
// ``invalidation`` is non-null only on a re-ingest that closed stale
// blocks. Every ingest failure is a named ``error`` code instead of
// a partial-success 200 (SC7/SC8) — see api.IngestFailedError.
export interface IngestResult {
  status: "ingested";
  document_id: string;
  version: string;
  fmt: string;
  doc_type: string;
  unit_count: number;
  block_count: number;
  written: number;
  duplicates: number;
  block_ids: string[];
  invalidation: {
    block_ids: string[];
    closed: number;
    already_closed: number;
    closed_at: [string, string][];
  } | null;
}

// Success body of POST /api/threads/{id}/attachments (the
// thread-attach door). ``scope`` is always "thread_attach": the model
// may read the text on THIS turn only; nothing is pushed, nothing is
// ingested (SC4). Mirrors the route's return dict.
export interface ThreadAttachment {
  attachment_id: string;
  filename: string;
  size_bytes: number;
  scope: "thread_attach";
}
