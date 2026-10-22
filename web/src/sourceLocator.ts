// Source-locator presentation rules, shared by the citation chips, the
// comparison table and the visual-observation rows so the three never
// disagree on how a unit reads or on what identity a source carries.
//
// A PDF presentation may label a physical unit as a page; every other
// unit is displayed as a unit (spec thread-attachment-source-read).
//
// IDENTITY vs LABEL, the split this file owns: :func:`citationSourceId`
// names WHICH source a row is and therefore keeps the host's raw ids —
// grouping keys, React keys and comparison locators all depend on it.
// :func:`citationSourceLabel` is what a reader SEES, and in the default
// view it may not carry an internal id (specs/qa-chain-grounded-experience.md
// "Internal names, engine/tool names, IDs, and slot labels must not leak
// through default source labels").

import type {
  AttachmentSourceRef,
  Citation,
  CitationKind,
  SourceDisplay,
} from "./types";
import { demoSourceUrl, type DemoSourceSummary } from "./demoMode";

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

/** Section plus page/unit for a citation source row. History records
 * written before section labels existed state that absence explicitly. */
export function formatCitationLocator(
  citation: Pick<Citation, "section" | "display" | "unit">,
): string {
  if (citation.display === "record") return "record";
  return `${citation.section ?? "章节未注明"} · ${formatUnitLocator(
    citation.display,
    citation.unit,
  )}`;
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
 *
 * WHY this stays raw while :func:`citationSourceLabel` does not: identity
 * keys the sources-list grouping and the comparison locators, so it must
 * stay one-to-one with the stored record. What a reader sees is the
 * label, and the label is the only half that has to be readable.
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

/** The attachment's display name: the filename the host sent, else a
 * readable non-identifying name. Shared by citation rows and visual-claim
 * rows so neither one invents its own identity text.
 *
 * WHY the id is no longer the fallback: a raw ``attachment_id`` is an
 * internal handle no reader can use, and the default view may not show
 * one. The number keeps the row distinguishable from another attachment
 * without being an identity. */
export function attachmentDisplayName(
  attachmentId: string,
  filename: string,
): string {
  return filename || `对话附件 ${attachmentId}`;
}

/** The readable name per evidence kind, used when no real title arrived.
 * A company filing, a ledger record and a thread attachment are three
 * different kinds of source and must not collapse into one generic noun. */
const SOURCE_KIND_NAMES: Readonly<Record<CitationKind, string>> = {
  company: "公司资料",
  "database-row": "记录",
  attachment: "对话附件",
};

/** The source's locator in ordinary-person wording: 第 N 页 / 第 N 单元 /
 * 一条记录. WHY not the ``p.N`` / ``u.N`` / ``record`` forms: those are
 * the machine-facing spelling shared with the comparison export, and a
 * default-view source row has to read as Chinese. The row keeps its
 * number, so two rows of one source stay distinguishable. */
function readableUnitLocator(display: SourceDisplay, unit: number): string {
  if (display === "record") return "一条记录";
  return display === "page" ? `第 ${unit} 页` : `第 ${unit} 单元`;
}

/** The citation's LABEL — the safe, readable name a sources-list row
 * shows. It is the readable noun for its kind plus the locator the row
 * already carries, so two rows of the same source remain visually
 * distinguishable without either one naming an id.
 *
 * ``markerIndex`` is the 1-based row number the sources list prints for
 * this citation (``groupCitationsBySourceAndUnit`` numbers rows
 * ``i + 1``, and the inline ``[n]`` markers read from the same index).
 * It is the last resort that keeps two untitled rows of one source
 * apart — the ``[n]`` and the row label therefore always agree.
 *
 * ``developerMode`` restores the raw identity: a developer diagnosing a
 * producer bug needs the document id and the ledger view/key spelling,
 * and specs/demo-seed.md item 6 keeps diagnostics access-controlled
 * rather than deleted. It is the ONE switch every presentation surface
 * reads (``readDeveloperModeEnabled``) — there is no second flag. */
export function citationSourceLabel(
  citation: Citation,
  markerIndex?: number,
  developerMode = false,
): string {
  if (citation.kind === "attachment") {
    return attachmentDisplayName(citation.attachment_id, citation.filename);
  }
  if (developerMode) return citationSourceId(citation);
  if (citation.kind === "database-row") {
    return SOURCE_KIND_NAMES["database-row"];
  }
  const locator = readableUnitLocator(citation.display, citation.unit);
  return markerIndex === undefined
    ? `${SOURCE_KIND_NAMES.company} · ${locator}`
    : `${SOURCE_KIND_NAMES.company} · ${locator} · 出处 ${markerIndex}`;
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

/**
 * Where a citation's row JUMPS TO, or ``null`` when it has nowhere to
 * jump (spec demo-seed item 6: each sources entry "is clickable to jump
 * to that location").
 *
 * This is the normal attachment path. Demo company rows use the separate
 * ``demoCitationJumpUrl`` only after an exact fixture-index match, so this
 * helper never turns an arbitrary document id into a source-viewer link.
 *
 * The three refusals, each because the row names no attachment:
 * a ledger row is a RECORD, not a place in a document (ADR 0091); a
 * company citation has no target here; and an attachment id with no
 * thread is half an address. This decides only whether
 * ``threadAttachmentUrl`` can express the target, and lets it build it.
 */
export function citationJumpUrl(
  threadId: string | undefined,
  citation: Citation,
): string | null {
  if (!threadId) return null;
  if (citation.kind === "database-row") return null;
  if (!citation.attachment_id) return null;
  return threadAttachmentUrl(threadId, citation.attachment_id);
}

/** Match a company citation to the fixed demo corpus by its full locator. */
export function demoSourceForCitation(
  citation: Citation,
  sources: readonly DemoSourceSummary[],
): DemoSourceSummary | null {
  if (citation.kind !== "company" || citation.display !== "page") return null;
  return (
    sources.find(
      (source) =>
        source.document_id === citation.document_id &&
        source.version === citation.version &&
        source.unit === citation.unit &&
        source.page === citation.unit,
    ) ?? null
  );
}

/** A demo source link carries only a locator; the host resolves its allowlist. */
export function demoCitationJumpUrl(source: DemoSourceSummary): string {
  return demoSourceUrl(source);
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
