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
 */
export function formatUnitLocator(
  display: SourceDisplay | undefined,
  unit: number,
): string {
  return display === "page" ? `p.${unit}` : `u.${unit}`;
}

/**
 * The citation's stable identity: an attachment is identified by its
 * attachment_id (the filename is display-only and may repeat), while
 * company rows keep document_id.
 */
export function citationSourceId(citation: Citation): string {
  return citation.kind === "attachment"
    ? citation.attachment_id
    : citation.document_id;
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

/** The safe display name: the attachment's filename, else document_id. */
export function citationSourceLabel(citation: Citation): string {
  if (citation.kind === "attachment") {
    return attachmentDisplayName(citation.attachment_id, citation.filename);
  }
  return citation.document_id;
}
