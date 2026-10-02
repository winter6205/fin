// T4 workflow (#138): the thread's export links.
//
// The download is a plain GET the browser performs itself (an anchor
// with ``download``), so this module only has to build the URL and say
// which kinds the thread can offer. Route + typed 404 codes are owned
// by src/fin/host/workspace_http.py; the file body — including the
// ``fin.comparison`` binding — is owned by src/fin/host/export.py.
// Nothing here reads a comparison cell, and nothing here computes a
// delta (docs/CONTEXT.md, compare_facts).

import type { Turn } from "./types";

export type ExportKind = "comparison" | "citations";
export type ExportFormat = "csv" | "json";

export interface ExportOptions {
  runId?: string;
  format?: ExportFormat;
}

export function exportUrl(
  threadId: string,
  kind: ExportKind,
  options: ExportOptions = {},
): string {
  const params = new URLSearchParams({
    kind,
    format: options.format ?? "csv",
  });
  if (options.runId) params.set("run_id", options.runId);
  return `/api/threads/${encodeURIComponent(threadId)}/export?${params.toString()}`;
}

export interface ExportAvailability {
  comparison: boolean;
  citations: boolean;
}

/**
 * Which exports this thread can offer right now.
 *
 * A still-running turn is excluded: its RunRecord is appended only
 * once the stream completes, so the server would answer the download
 * with a typed 404 while the SPA showed an enabled button.
 */
export function exportAvailability(turns: Turn[]): ExportAvailability {
  const settled = turns.filter((t) => t.in_flight !== true);
  return {
    comparison: settled.some((t) => (t.comparisons?.length ?? 0) > 0),
    citations: settled.some((t) => t.citations.length > 0),
  };
}
