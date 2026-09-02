// T4 workflow (the tracked change): pure helpers for the watchlist + in-app notice
// panel. Parsing lives here (not in the components) so the wire
// boundary is unit-tested without a DOM, the same split
// ``comparison.ts`` / ``statusStrip.ts`` already use.
//
// Shape SSOT is src/fin/host/watchlist.py; a payload that does not
// match is DROPPED rather than half-rendered — a watch row the
// operator cannot delete (no id) is worse than no row.

import type { Notice, WatchEntry } from "./types";

/**
 * Split the operator's free-text form field into form types.
 * "10-k, 8-K 10-q" -> ["10-K", "8-K", "10-Q"]. Empty input means
 * "watch every form", which the host stores as an empty list.
 */
export function parseFormsInput(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,、]+/)) {
    const form = raw.trim().toUpperCase();
    if (form && !out.includes(form)) out.push(form);
  }
  return out;
}

/** One line describing what a watch covers. */
export function watchSummary(entry: WatchEntry): string {
  return entry.forms.length > 0 ? entry.forms.join("、") : "全部 form";
}

/** One line describing a filing detected by the monitor. */
export function noticeSummary(notice: Notice): string {
  const form = notice.form ? ` ${notice.form}` : "";
  const period = notice.period ? `（${notice.period}）` : "";
  return `${notice.issuer}${form} ${notice.document_id}${period} 已进入本地 catalog`;
}

export function unreadCount(notices: Notice[]): number {
  return notices.filter((n) => !n.read).length;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function parseWatchEntry(value: unknown): WatchEntry | null {
  const row = asRecord(value);
  if (row === null) return null;
  const id = asString(row.id);
  const issuer = asString(row.issuer);
  const createdAt = asString(row.created_at);
  if (id === null || issuer === null || createdAt === null) return null;
  const forms = Array.isArray(row.forms)
    ? row.forms.filter((f): f is string => typeof f === "string")
    : [];
  return { id, issuer, forms, created_at: createdAt };
}

export function parseWatchlist(value: unknown): WatchEntry[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(parseWatchEntry)
    .filter((entry): entry is WatchEntry => entry !== null);
}

export function parseNotice(value: unknown): Notice | null {
  const row = asRecord(value);
  if (row === null) return null;
  const id = asString(row.id);
  const issuer = asString(row.issuer);
  const documentId = asString(row.document_id);
  const createdAt = asString(row.created_at);
  if (id === null || issuer === null || documentId === null) return null;
  if (createdAt === null) return null;
  return {
    id,
    watch_id: asString(row.watch_id) ?? "",
    issuer,
    form: asString(row.form) ?? "",
    document_id: documentId,
    period: asString(row.period) ?? "",
    created_at: createdAt,
    read: row.read === true,
  };
}

export function parseNotices(value: unknown): Notice[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(parseNotice)
    .filter((notice): notice is Notice => notice !== null);
}
