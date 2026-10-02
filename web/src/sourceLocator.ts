// Source-locator presentation rules, shared by the citation chips, the
// comparison table and the visual-observation rows so the three never
// disagree on how a unit reads or on what identity a source carries.
//
// A PDF presentation may label a physical unit as a page; every other
// unit is displayed as a unit (spec thread-attachment-source-read).

import type { AttachmentSourceRef, Citation, SourceDisplay } from "./types";

/**
 * The locator suffix for a 1-based source unit. Only an explicit
 * ``page`` display may render as a page — a locator from an older peer
 * without a display must not silently claim page numbering.
 *
 * A ledger row renders as neither: a record has no page and no
 * ordinal, so `p.1` / `u.1` would name a position in a document that
 * does not exist. It shows `record` and the row's own identity beside
 * it (ADR 0091).
 */
export function formatUnitLocator(
  display: SourceDisplay | undefined,
  unit: number,
): string {
  if (display === "record") return "record";
  return display === "page" ? `p.${unit}` : `u.${unit}`;
}

/**
 * The citation's stable identity: an attachment is identified by its
 * attachment_id (the filename is display-only and may repeat), company
 * rows keep document_id, and a ledger row is identified by the view
 * that answered plus the record it answered about.
 *
 * WHY the third case is an explicit return rather than the old
 * `attachment ? attachment_id : document_id` tail: that tail defaulted
 * every unrecognised kind into the document path, so a `database-row`
 * would have grouped under a `document_id` it does not have. A kind the
 * host does not send has no identity here, and a stable empty string
 * keeps it in one bucket instead of inventing one per unknown value
 * (ADR 0091).
 */
export function citationSourceId(citation: Citation): string {
  if (citation.kind === "attachment") return citation.attachment_id;
  if (citation.kind === "company") return citation.document_id;
  const row = citation.database_row;
  if (!row) return "";
  const key = Object.keys(row.record_key)
    .sort()
    .map((name) => `${name}=${row.record_key[name]}`)
    .join(",");
  return `${row.source}#${key}`;
}

/** The attachment's safe display name: the filename the host sent, else
 * the id it stands for. Shared by citation rows and visual-claim rows so
 * neither one invents its own identity text. */
export function attachmentDisplayName(attachmentId: string, filename: string): string {
  return filename || attachmentId;
}

/**
 * The one address of one attachment on one thread: the host's thread
 * attachment door (GET for its status, DELETE for its removal). Both the
 * network doors in ``api.ts`` and the turn's source links are built here,
 * because a link that spells the path a second way can point at an address
 * the client's own fetch would not recognize.
 *
 * There is no durable HTTP seam yet for the rendered page image itself, so
 * this attachment door is the closest real "original location" a source link
 * can name; a row that needs to say WHICH page carries its ``source_ref``
 * alongside the link instead of inventing an image route.
 */
export function threadAttachmentUrl(
  threadId: string,
  attachmentId: string,
): string {
  // Both ids are opaque host-minted text that may contain ``/`` or a space;
  // unencoded they would escape the path segment.
  return (
    `/api/threads/${encodeURIComponent(threadId)}/attachments/` +
    encodeURIComponent(attachmentId)
  );
}

/** The locator that names an attachment source: the ``source_ref`` shape
 * ``read_source`` resolves. Built from the row's own identity fields, so
 * a citation and a visual claim naming one unit produce one locator
 * rather than two spellings of it. */
export function attachmentSourceRef(source: {
  attachment_id: string;
  version: string;
  unit: number;
}): AttachmentSourceRef {
  return {
    kind: "attachment",
    attachment_id: source.attachment_id,
    version: source.version,
    unit: source.unit,
  };
}

/** The safe display name: the attachment's filename, the ledger row's
 * own view-plus-key identity, else document_id. */
export function citationSourceLabel(citation: Citation): string {
  if (citation.kind === "attachment") {
    return attachmentDisplayName(citation.attachment_id, citation.filename);
  }
  if (citation.kind === "database-row") {
    return citationSourceId(citation) || citation.kind;
  }
  return citation.document_id;
}
