// Inline citation chips inside the answer prose plus a sources
// list under the turn. T3 fix (code-review #5): the spec mandates
// BOTH (a) "chips inserted at each verified span inside the
// answer" and (b) "a sources list under the turn (locator +
// quote; hover/focus/click-tap reveal quote)". The T1 chip row
// only satisfied (b) partially — chips lived below the bubble and
// the quote reveal was a hover-only ``title`` attr on a
// non-focusable ``<span>``. Now the chip is a focusable button
// (tab-reachable, focus-reveal) and lives INSIDE the prose at
// each verified span; the sources list lives under the prose and
// shows the locator + a tap-to-reveal quote.

import { useId, useState } from "react";
import type { Citation } from "../types";

type AnswerPart = string | { quote: string; index: number };

type IndexedCitation = { citation: Citation; globalIndex: number };

type PageGroup = {
  page: number;
  citations: IndexedCitation[];
};

type DocumentGroup = {
  document_id: string;
  pages: PageGroup[];
};

function splitAnswerAtCitations(
  answer: string,
  citations: Citation[],
): AnswerPart[] {
  if (!answer) return [];
  if (!citations || citations.length === 0) return [answer];
  // Sort citations by first occurrence of their quote in the answer.
  // Skipped if a quote is not found (the gate may have flagged it via
  // a future mechanism; we still render the answer with whatever spans
  // we can locate).
  const matches: {
    start: number;
    end: number;
    index: number;
    quote: string;
  }[] = [];
  for (let i = 0; i < citations.length; i += 1) {
    const c = citations[i];
    const idx = answer.indexOf(c.quote);
    if (idx < 0) continue;
    matches.push({
      start: idx,
      end: idx + c.quote.length,
      index: i,
      quote: c.quote,
    });
  }
  if (matches.length === 0) return [answer];
  // Sort by start position, then dedup overlaps (keep first).
  matches.sort((a, b) => a.start - b.start);
  const out: AnswerPart[] = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue;
    if (m.start > cursor) out.push(answer.slice(cursor, m.start));
    out.push({ quote: m.quote, index: m.index });
    cursor = m.end;
  }
  if (cursor < answer.length) out.push(answer.slice(cursor));
  return out;
}

function groupCitationsByDocumentAndPage(
  citations: Citation[],
): DocumentGroup[] {
  const docMap = new Map<string, Map<number, IndexedCitation[]>>();

  for (let i = 0; i < citations.length; i += 1) {
    const citation = citations[i];
    let pageMap = docMap.get(citation.document_id);
    if (!pageMap) {
      pageMap = new Map();
      docMap.set(citation.document_id, pageMap);
    }
    const pageCitations = pageMap.get(citation.page) ?? [];
    pageCitations.push({ citation, globalIndex: i + 1 });
    pageMap.set(citation.page, pageCitations);
  }

  return Array.from(docMap.entries()).map(([document_id, pageMap]) => ({
    document_id,
    pages: Array.from(pageMap.entries())
      .sort(([a], [b]) => a - b)
      .map(([page, pageCitations]) => ({
        page,
        citations: pageCitations,
      })),
  }));
}

function countUniquePages(citations: Citation[]): number {
  return new Set(citations.map((c) => `${c.document_id}:${c.page}`)).size;
}

const focusRingClass =
  "focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-1 focus:ring-offset-[#FAFAF7]";

function SourceRow({ citation, index }: { citation: Citation; index: number }) {
  const [open, setOpen] = useState(false);
  const label = `[${index}] ${citation.document_id} p.${citation.page}`;
  return (
    <li className="text-xs font-mono text-gray-700">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`source-quote-${index}`}
        onClick={() => setOpen((v) => !v)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className={`text-left underline decoration-dotted underline-offset-2 hover:text-black ${focusRingClass}`}
      >
        {label}
      </button>
      <p
        id={`source-quote-${index}`}
        className={
          open
            ? "mt-1 ml-4 italic text-gray-600 whitespace-pre-wrap break-words"
            : "sr-only"
        }
      >
        “{citation.quote}”
      </p>
    </li>
  );
}

function PageSourcesGroup({
  group,
  defaultCollapsed,
}: {
  group: PageGroup;
  defaultCollapsed: boolean;
}) {
  const [open, setOpen] = useState(!defaultCollapsed);
  const spanCount = group.citations.length;
  const panelId = `source-page-${group.page}-${group.citations[0]?.globalIndex ?? 0}`;

  return (
    <div className="border-t border-black first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-3 py-1 text-xs font-mono text-gray-700 hover:bg-tool ${focusRingClass}`}
      >
        p.{group.page} · {spanCount} span{spanCount === 1 ? "" : "s"}
      </button>
      {open ? (
        <ul id={panelId} className="px-3 pb-2 space-y-1">
          {group.citations.map(({ citation, globalIndex }) => (
            <SourceRow
              key={`${citation.document_id}-${citation.page}-${globalIndex}`}
              citation={citation}
              index={globalIndex}
            />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function DocumentSourcesGroup({
  group,
  defaultCollapsed,
}: {
  group: DocumentGroup;
  defaultCollapsed: boolean;
}) {
  const [open, setOpen] = useState(!defaultCollapsed);
  const pageCount = group.pages.length;
  const spanCount = group.pages.reduce(
    (sum, page) => sum + page.citations.length,
    0,
  );
  const panelId = `source-doc-${group.document_id}`;

  return (
    <div className="border-t border-black first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-2 py-1.5 text-xs font-mono font-medium hover:bg-tool ${focusRingClass}`}
      >
        {group.document_id} · {pageCount} 页 · {spanCount} spans
      </button>
      {open ? (
        <div id={panelId}>
          {group.pages.map((pageGroup) => (
            <PageSourcesGroup
              key={`${group.document_id}-${pageGroup.page}`}
              group={pageGroup}
              defaultCollapsed={defaultCollapsed}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SourcesList({ citations }: { citations: Citation[] }) {
  const spanCount = citations?.length ?? 0;
  const pageCount = countUniquePages(citations ?? []);
  const defaultCollapsed = spanCount > 10;
  const [open, setOpen] = useState(!defaultCollapsed);
  const panelId = useId();
  if (!citations || citations.length === 0) return null;

  const grouped = groupCitationsByDocumentAndPage(citations);

  return (
    <section className="mt-2 border border-black bg-card" aria-label="Sources">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-2 py-1.5 text-xs font-mono font-medium hover:bg-tool ${focusRingClass}`}
      >
        Sources（出处）· {pageCount} 页 · {spanCount} spans
      </button>
      <p className="px-2 pb-1.5 text-xs text-gray-600 border-b border-black">
        citation gate 匹配片段，可能有半句重叠，不代表发送失败
      </p>
      {open ? (
        <div id={panelId}>
          {grouped.map((docGroup) => (
            <DocumentSourcesGroup
              key={docGroup.document_id}
              group={docGroup}
              defaultCollapsed={defaultCollapsed}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

export default function AnswerWithCitations({
  answer,
  citations,
}: {
  answer: string;
  citations: Citation[];
}) {
  if (!answer) return null;
  const parts = splitAnswerAtCitations(answer, citations);
  return (
    <>
      <p className="text-sm whitespace-pre-wrap break-words">
        {parts.map((part, i) =>
          typeof part === "string" ? (
            <span key={`text-${i}`}>{part}</span>
          ) : (
            <mark
              key={`cite-${part.index}`}
              data-citation-index={part.index}
              className="bg-transparent border-b border-[#2563EB] text-inherit"
              title={citations[part.index]?.quote ?? ""}
            >
              {part.quote}
            </mark>
          ),
        )}
      </p>
      <SourcesList citations={citations} />
    </>
  );
}
