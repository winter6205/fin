# Turn receipt tracing

The session record — thread messages plus receipts — is always on. The host can also export a small set of execution spans through standard OpenTelemetry instrumentation; that trace layer is off by default. See [ADR 0102](../adr/0102-opentelemetry-standard-instrumentation-for-turn-receipt-tracing.md) for why this replaced the earlier hand-rolled OTLP encoder.

## Configuration

Tracing has two layers. The **session-record layer** — thread messages plus receipts — is always on, content-complete, and authoritative for single-turn troubleshooting. The **trace layer** — spans carrying structure, timing and allowlisted attributes, never raw text — is off by default; when enabled it may write to local PostgreSQL or export over OTLP. The layers are correlated by `thread_id`, `turn_id` and `trace_id`. Cross-turn aggregation and alerting belong to the middle platform (中台).

Select the trace exporter with the standard `OTEL_TRACES_EXPORTER` setting; the `OTEL_EXPORTER_OTLP_*` variables configure the OTLP endpoint.

- `OTEL_TRACES_EXPORTER` — `none` (default), `pg`, `console` or `otlp`; every exporter stays off unless explicitly selected. `pg` writes spans to local PostgreSQL, `console` writes them to the process output, and `otlp` exports them to the configured OTLP/HTTP endpoint.
- `FIN_TRACE_SPAN_DSN` — the local database `pg` writes its spans to. Required when `pg` is selected; startup refuses `pg` without it instead of discovering the gap in the export worker, and every other choice ignores it. No `.env` file is needed, and the value is never written to a message or a log line.
- `FIN_TRACE_SPAN_RETENTION_DAYS` — how long span rows are kept. Defaults to 14; `0` keeps them indefinitely. Pruning runs in the export worker at most once an hour, and a failed prune is logged and retried on the next window rather than failing a turn.
- `OTEL_EXPORTER_OTLP_ENDPOINT` — the OTLP/HTTP endpoint base URL. Any backend that accepts OTLP/HTTP works, including Langfuse.
- `OTEL_EXPORTER_OTLP_HEADERS` — optional backend headers, for example `Authorization=Basic <base64 of public:secret>`.

There is no vendor-specific exporter code; the PostgreSQL sink is fin's own store. Credentials belong in the process environment or the deployment's secret injection mechanism, never in repository files, command output or logs.

Application startup binds the exporter to that application's receipt projector. The shared executor publishes send/resume segment identity, and the existing bounded worker sends completed root/child batches off the execution thread. Application shutdown unbinds the sink; disabled tracing does not use another application's global provider.

## Spans

When the trace layer is enabled, exactly five spans are written by hand over the existing execution seams: **classify_node**, **retrieve_tool**, **model_node**, **citation_gate_node** and **admit_decision**. There is no automatic instrumentation and no general callback handler.

Trace and span identifiers are derived deterministically rather than inherited from ambient context, so a turn is always one trace: the trace id derives from (**thread_id**, **turn_id**), and each span id from (**thread_id**, **turn_id**, **segment_seq**). Each execution segment is a root span with no parent, and the receipt-projection span is its sibling. **segment_seq** is a durable receipt column; it advances when an accepted reply resumes a clarification, and nothing inside a segment changes it otherwise.

Retrieval runs in a worker thread through `asyncio.run` in the MCP adapter, so the trace context is passed into that thread explicitly rather than relied on to propagate.

## Content and privacy

Spans carry only allowlisted scalar attributes: opaque thread and turn correlations, receipt state, outcome, accepted-input projection, stage counters, timing, failure and cancellation tokens, provider token metadata when supplied, and the retrieval rerank fields the upstream receipt carries. They never carry prompts, answers, source content, source labels or ids, raw tool arguments, exception text or credentials.

## Delivery semantics

The **session-record layer** stays authoritative for accounting and for single-turn troubleshooting; the trace layer is a structural view and never a content source. A successful export advances only `exported_revision`; it never changes the receipt content revision or its timestamp, and an older acknowledgement cannot move the cursor backward. The worker scans unacknowledged rows at startup and once per second, coalesces duplicate queued turn identities, and retries a captured payload up to three times. A failed, malformed or partial response leaves the receipt pending for a later scan.

A span count that disagrees with receipt `calls` is exporter diagnostics. It does not void a trace and is not a reason to fail an evaluation.

These observations have different scopes: each seam is coalesced to one span per segment, while receipt `calls` account for twelve work stages across the turn. Inspect the exported spans alongside the receipt counters when diagnosing a difference. There is no automatic equality alarm between those totals; treating them as the same denominator would report expected coalescing as a defect.

Delivery can repeat: the receiver may accept a request while its response is lost, so the same span can be sent again. The exporter does not promise exactly-once or guaranteed delivery. Queue saturation, retry, scan and response failures are retained as exporter counters without raw exception details and do not block answer or status requests. Shutdown allows at most two seconds for a bounded drain; remaining revisions stay durable and are eligible for the next startup scan.

The two delivery modes differ in what they keep. The local `pg` sink writes one row per (`trace_id`, `span_id`) with `ON CONFLICT DO NOTHING`, so a retried or later delivery is a no-op; because the receipt-projection span id is fixed per (`thread_id`, `turn_id`, `segment_seq`), the stored projection row is the first delivered revision. An OTLP export sends each exported revision as its own request, so an OTLP backend may retain several projection spans that share one span id; the local dedupe rule does not travel with the payload. Intermediate receipt revisions may be coalesced by the queue and scan in either mode, and the receipt layer — not either trace store — remains the authority on a turn's current state.

## Operator view

TaskPanel's trace view reads only `GET /api/threads/{thread}/turns/{turn}/spans`; it never fetches the receipt route. The route always answers 200 with `thread_id`, `turn_id`, `spans` and a four-value `trace_state` that replaced the earlier boolean `trace_enabled`. The state comes from the exporter selection the application boot resolved once, not from the route re-reading `OTEL_TRACES_EXPORTER`, so the view and the export worker cannot disagree:

| `trace_state` | Cause | Panel hint |
| --- | --- | --- |
| `off` | the boot selected no exporter, so nothing is written | "trace 未开启" |
| `local` | the boot selected the `pg` sink and the read succeeded | "trace 已开启，但本轮没有 span" |
| `external` | the boot selected `console` or `otlp`, so spans leave the process | "trace 已开启（非本地存储）" |
| `unreadable` | the `pg` sink is selected and the store read failed | "trace 状态不可读，显示回执" |

Only `local` reads the local `fin_trace_span` table; `external` does not read it at all, so rows an earlier `pg` run left behind are not returned. The route returns at most 200 spans per turn, oldest first. A failed request or a payload carrying an unknown state token renders the same unreadable sentence rather than a state this build does not understand, and the hint while the read is in flight is "trace 状态读取中…". Clearing the turn's in-flight flag and committing the segment's rows are separate writes, so a settled `local` read that returns no spans is repeated once after one second; the re-read is cancelled on a turn change or unmount and is never polled. The empty-`local` hint can therefore be transient for up to one second.

When the turn has spans, the panel renders them as a waterfall. When it has none, the panel renders the fallback rows: the run status and turn result from the live task state, the turn status through the shared label (the raw `status_reason` token is never rendered), and the thinking and tool folds the turn already holds from `thinking_text` and `tool_summaries`; a turn with no recorded stage says so. It does not fetch or render the receipt's `stage_metrics`. Neither branch is an error state: with no exporter selected or an external one, the receipt fallback is the normal view, and single-turn troubleshooting reads the session-record layer in the host.
