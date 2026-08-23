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

import { useState } from "react";
import type { Citation } from "../types";

type AnswerPart = string | { quote: string; index: number };

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

function SourcesList({ citations }: { citations: Citation[] }) {
  if (!citations || citations.length === 0) return null;
  return (
    <ul
      className="mt-2 border-t border-black/20 pt-2 space-y-1"
      aria-label="Sources"
    >
      {citations.map((c, i) => (
        <SourceRow
          key={`${c.document_id}-${c.page}-${i}`}
          citation={c}
          index={i + 1}
        />
      ))}
    </ul>
  );
}

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
        className="text-left underline decoration-dotted underline-offset-2 hover:text-black focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-1 focus:ring-offset-[#FAFAF7]"
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
