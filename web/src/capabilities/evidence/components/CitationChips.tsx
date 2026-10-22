// Inline citation markers inside the answer prose plus a sources
// list under the turn. T3 fix (code-review #5): the spec mandates
// BOTH (a) "chips inserted at each verified span inside the
// answer" and (b) "a sources list under the turn (locator +
// quote; hover/focus/click-tap reveal quote)". The T1 chip row
// only satisfied (b) partially — chips lived below the bubble.
// Now each marker follows the sentence or paragraph supported by its
// verified span inside the prose, and the sources list closes the prose
// with the locator and a tap-to-reveal quote.
//
// specs/demo-seed.md item 6 / ADR 0100 decision 6 sharpen this: the
// inline marker is a SUPERSCRIPT `[N]` placed after the sentence or
// paragraph it supports, and the sources list closes the answer. The
// marker is a real <button> — tab-reachable, focus-reveal, click to
// toggle — because a hover-only ``title`` on a non-focusable
// <mark> reached neither a keyboard nor a touch screen.
//
// Item 6 also requires each sources entry to be CLICKABLE TO JUMP to the
// cited location. Attachment rows use ``threadAttachmentUrl``. In demo
// mode, a company citation links only when it exactly matches the fixed
// fixture index; every other unsupported locator stays plain text.

import { useId, useState } from "react";
import { useParams } from "react-router-dom";
import {
  countUniqueSourceUnits,
  groupCitationsBySourceAndUnit,
  shouldCollapseSourcesList,
  sourceCountWord,
  type SourceGroup,
  type UnitGroup,
} from "../sourcesList";
import {
  citationJumpUrl,
  citationSourceId,
  citationSourceLabel,
  demoCitationJumpUrl,
  demoSourceForCitation,
  formatCitationLocator,
  formatUnitLocator,
} from "../../../sourceLocator";
import { useDemoPresentation } from "../../../components/DemoModeSurface";
import {
  databaseRowFieldLines,
  databaseRowFreshnessNote,
  databaseRowIdentityLabel,
  evidenceRefusalRowText,
} from "../../../evidenceContract";
import { readDeveloperModeEnabled } from "../../../devMode";
import type { DemoSourceSummary } from "../../../demoMode";
import type { Citation, EvidenceRefusal } from "../../../types";

type AnswerPart =
  | string
  | { quote: string; index: number }
  | { markerIndex: number };

interface CitationMatch {
  start: number;
  end: number;
  index: number;
  quote: string;
}

const SENTENCE_ENDINGS = new Set([".", "!", "?", "。", "！", "？", "…"]);
const CLOSING_PUNCTUATION = new Set([
  '"',
  "'",
  ")",
  "]",
  "}",
  "）",
  "】",
  "」",
  "』",
  "”",
  "’",
]);

function isSentenceEndingAt(answer: string, index: number): boolean {
  const punctuation = answer[index];
  if (!SENTENCE_ENDINGS.has(punctuation)) return false;
  if (punctuation !== ".") return true;
  // A decimal point is not a sentence boundary.
  return !(
    /\d/.test(answer[index - 1] ?? "") &&
    /\d/.test(answer[index + 1] ?? "")
  );
}

function quotedSentenceEnd(answer: string, quoteEnd: number): number | null {
  let preceding = quoteEnd - 1;
  while (preceding >= 0 && CLOSING_PUNCTUATION.has(answer[preceding])) {
    preceding -= 1;
  }
  if (preceding < 0 || !isSentenceEndingAt(answer, preceding)) return null;
  return includeClosingPunctuation(answer, quoteEnd);
}

function includeSentenceClosers(answer: string, end: number): number {
  while (
    SENTENCE_ENDINGS.has(answer[end] ?? "") ||
    CLOSING_PUNCTUATION.has(answer[end] ?? "")
  ) {
    end += 1;
  }
  return end;
}

function includeClosingPunctuation(answer: string, end: number): number {
  while (CLOSING_PUNCTUATION.has(answer[end] ?? "")) end += 1;
  return end;
}

function nextSentenceBoundary(answer: string, start: number): number | null {
  for (let index = start; index < answer.length; index += 1) {
    if (answer[index] === "\n" || answer[index] === "\r") return index;
    if (!isSentenceEndingAt(answer, index)) continue;
    return includeSentenceClosers(answer, index + 1);
  }
  return null;
}

function sentenceEndPosition(answer: string, quoteEnd: number): number {
  return (
    quotedSentenceEnd(answer, quoteEnd) ??
    nextSentenceBoundary(answer, quoteEnd) ??
    answer.length
  );
}

function findCitationMatches(
  answer: string,
  citations: Citation[],
): CitationMatch[] {
  const matches: CitationMatch[] = [];
  for (let index = 0; index < citations.length; index += 1) {
    const quote = citations[index].quote;
    const start = answer.indexOf(quote);
    if (start < 0) continue;
    matches.push({ start, end: start + quote.length, index, quote });
  }
  matches.sort((a, b) => a.start - b.start);

  const selected: CitationMatch[] = [];
  let end = 0;
  for (const match of matches) {
    if (match.start < end) continue;
    selected.push(match);
    end = match.end;
  }
  return selected;
}

function appendAnswerPart(parts: AnswerPart[], part: AnswerPart): void {
  const previous = parts[parts.length - 1];
  if (typeof part === "string" && typeof previous === "string") {
    parts[parts.length - 1] = previous + part;
    return;
  }
  if (
    typeof part !== "string" &&
    "quote" in part &&
    typeof previous !== "string" &&
    previous !== undefined &&
    "quote" in previous &&
    previous.index === part.index
  ) {
    parts[parts.length - 1] = { ...previous, quote: previous.quote + part.quote };
    return;
  }
  parts.push(part);
}

interface AnswerBoundaryPlan {
  markers: Map<number, number[]>;
  positions: number[];
}

function createAnswerBoundaryPlan(
  answer: string,
  matches: CitationMatch[],
): AnswerBoundaryPlan {
  const markers = new Map<number, number[]>();
  const boundaries = new Set([0, answer.length]);
  for (const match of matches) {
    boundaries.add(match.start);
    boundaries.add(match.end);
    const markerAt = sentenceEndPosition(answer, match.end);
    boundaries.add(markerAt);
    markers.set(markerAt, [...(markers.get(markerAt) ?? []), match.index]);
  }
  return { markers, positions: [...boundaries].sort((a, b) => a - b) };
}

function appendMarkersAt(
  parts: AnswerPart[],
  markers: Map<number, number[]>,
  position: number,
): void {
  for (const markerIndex of markers.get(position) ?? []) {
    appendAnswerPart(parts, { markerIndex });
  }
}

function appendAnswerSegment(
  parts: AnswerPart[],
  answer: string,
  matches: CitationMatch[],
  start: number,
  end: number,
  matchIndex: number,
): number {
  while (matches[matchIndex] && matches[matchIndex].end <= start) {
    matchIndex += 1;
  }
  const match = matches[matchIndex];
  const text = answer.slice(start, end);
  const isQuoted = match !== undefined && match.start <= start && match.end >= end;
  appendAnswerPart(parts, isQuoted ? { quote: text, index: match.index } : text);
  return matchIndex;
}

function buildAnswerParts(answer: string, matches: CitationMatch[]): AnswerPart[] {
  const plan = createAnswerBoundaryPlan(answer, matches);
  const parts: AnswerPart[] = [];
  let matchIndex = 0;
  for (let positionIndex = 0; positionIndex < plan.positions.length; positionIndex += 1) {
    const start = plan.positions[positionIndex];
    appendMarkersAt(parts, plan.markers, start);
    const end = plan.positions[positionIndex + 1];
    if (end === undefined || end <= start) continue;
    matchIndex = appendAnswerSegment(
      parts,
      answer,
      matches,
      start,
      end,
      matchIndex,
    );
  }
  return parts;
}

function splitAnswerAtCitations(
  answer: string,
  citations: Citation[],
): AnswerPart[] {
  if (!answer) return [];
  if (!citations || citations.length === 0) return [answer];
  const matches = findCitationMatches(answer, citations);
  return matches.length === 0 ? [answer] : buildAnswerParts(answer, matches);
}

const focusRingClass =
  "focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-1 focus:ring-offset-[#FAFAF7]";

/** The standing caveat above the sources list, in ordinary-person
 * Chinese.
 *
 * WHY it exists at all: the matched excerpt is a verbatim span the host
 * located in the source, and a span can start or end mid-sentence, so the
 * reader who compares it with the visible page may see a half-sentence.
 * That is not a failed answer, and saying so is the whole point of the
 * note.
 *
 * WHY the old wording had to go: it named the internal component that
 * produces the match, which is exactly the implementation label the
 * default view may not show. */
export const SOURCES_MATCH_NOTE =
  "引文是原文中的匹配片段，可能只覆盖半句话，不代表回答出错";

/** The inline superscript marker for one cited span: a `[N]` rendered as
 * a superscript after the sentence or paragraph it supports (spec item 6), and
 * a real button so the excerpt it points at is reachable by keyboard and
 * by touch rather than by hover alone.
 *
 * ``index`` is the 0-based position in the citations array; the marker
 * shows the 1-based number the SOURCES LIST prints for the same row
 * (``groupCitationsBySourceAndUnit`` numbers rows ``i + 1``), so a `[2]`
 * in the prose and a `[2]` under the answer are one and the same citation.
 *
 * Closed, the excerpt stays in the markup as ``sr-only`` — the same
 * convention the sources-list rows use — so the control always discloses
 * something and never renders an empty panel when opened. */
function SuperscriptCitation({
  citation,
  index,
}: {
  citation: Citation | undefined;
  index: number;
}) {
  const [open, setOpen] = useState(false);
  const panelId = `inline-cite-${index + 1}`;
  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        data-superscript-index={index}
        className={`ml-0.5 inline-flex items-start align-super text-[0.7em] font-normal text-[#2563EB] hover:text-black ${focusRingClass}`}
      >
        {/* Tailwind preflight sets ``sup { line-height: 0 }``; as a flex
            item that collapses the button's box to zero height, so the
            marker stops being a visible, clickable target. */}
        <sup className="leading-[1.5]">[{index + 1}]</sup>
      </button>
      <span
        id={panelId}
        className={
          open
            ? "ml-1 italic text-gray-600 whitespace-pre-wrap break-words"
            : "sr-only"
        }
      >
        “{citation?.quote ?? ""}”
      </span>
    </>
  );
}

interface SourceRowPresentation {
  label: string;
  href: string | null;
  demoSource: boolean;
}

function sourceRowPresentation(
  citation: Citation,
  index: number,
  context: {
    threadId: string | undefined;
    demoEnabled: boolean;
    sources: readonly DemoSourceSummary[];
    developerMode: boolean;
  },
): SourceRowPresentation {
  const source = context.demoEnabled
    ? demoSourceForCitation(citation, context.sources)
    : null;
  const displayCitation = source
    ? { ...citation, section: source.section }
    : citation;
  // WHY the label is derived with the row's own marker index: the
  // default label must not carry the document id, so two untitled rows
  // of one source are told apart by the same ``[n]`` the inline markers
  // use (specs/qa-chain-grounded-experience.md "inline ``[n]`` markers
  // and a readable trailing sources list linked to the same references").
  const sourceName =
    source?.title ?? citationSourceLabel(citation, index, context.developerMode);
  return {
    label: `[${index}] ${sourceName} ${formatCitationLocator(displayCitation)}`,
    href: source
      ? demoCitationJumpUrl(source)
      : citationJumpUrl(context.threadId, citation),
    demoSource: source !== null,
  };
}

function SourceLink({
  label,
  href,
  demoSource,
}: SourceRowPresentation) {
  if (!href) return <span className="text-gray-700">{label}</span>;
  return (
    <a
      data-testid={demoSource ? "citation-source-link" : "source-jump"}
      href={href}
      aria-label={`${demoSource ? "查看演示原文" : "回到原始位置"}：${label}`}
      className={`underline decoration-dotted underline-offset-2 hover:text-black ${focusRingClass}`}
    >
      {label}
    </a>
  );
}

function CitationQuote({ id, quote, open }: { id: string; quote: string; open: boolean }) {
  return (
    <p
      id={id}
      className={
        open
          ? "mt-1 ml-4 italic text-gray-600 whitespace-pre-wrap break-words"
          : "sr-only"
      }
    >
      “{quote}”
    </p>
  );
}

function DatabaseRowDetails({
  row,
  open,
  developerMode,
}: {
  row: Citation["database_row"];
  open: boolean;
  developerMode: boolean;
}) {
  if (!row) return null;
  return (
    <>
      <p data-testid="database-row-identity" className="mt-0.5 ml-4 text-gray-600">
        {databaseRowIdentityLabel(row, developerMode)}
      </p>
      <p className="mt-0.5 ml-4 text-gray-500">
        {databaseRowFreshnessNote(row, developerMode)}
      </p>
      {/* The retained row opens from its citation snapshot and issues no query. */}
      {open ? (
        <ul data-testid="database-row-fields" className="mt-1 ml-8 space-y-0.5">
          {databaseRowFieldLines(row, developerMode).map((line) => (
            <li key={line} className="text-gray-600 font-mono">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

/** One source label, its independent quote control, and retained row
 * details.
 *
 * ``developerMode`` is read here rather than passed down from the turn so
 * every surface built from one citation — the jump label, the ledger
 * identity and the field list — makes the same call. It defaults to the
 * stored switch, and the tests inject the other value. */
function SourceRow({
  citation,
  index,
  threadId,
  developerMode = readDeveloperModeEnabled(),
}: {
  citation: Citation;
  index: number;
  threadId: string | undefined;
  developerMode?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { enabled, sources } = useDemoPresentation();
  const presentation = sourceRowPresentation(citation, index, {
    threadId,
    demoEnabled: enabled,
    sources,
    developerMode,
  });
  const quoteId = `source-quote-${index}`;
  return (
    <li className="text-xs font-mono text-gray-700">
      <SourceLink {...presentation} />
      <button
        type="button"
        aria-expanded={open}
        aria-controls={quoteId}
        aria-label={`显示引文：${presentation.label}`}
        onClick={() => setOpen((value) => !value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className={`ml-2 align-baseline underline decoration-dotted underline-offset-2 hover:text-black ${focusRingClass}`}
      >
        引文
      </button>
      <DatabaseRowDetails
        row={citation.database_row}
        open={open}
        developerMode={developerMode}
      />
      <CitationQuote id={quoteId} quote={citation.quote} open={open} />
    </li>
  );
}

function UnitSourcesGroup({
  group,
  defaultCollapsed,
  threadId,
  developerMode,
}: {
  group: UnitGroup;
  defaultCollapsed: boolean;
  threadId: string | undefined;
  developerMode: boolean;
}) {
  const [open, setOpen] = useState(!defaultCollapsed);
  const spanCount = group.citations.length;
  const panelId = `source-unit-${group.unit}-${group.citations[0]?.globalIndex ?? 0}`;

  return (
    <div className="border-t border-black first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-3 py-1 text-xs font-mono text-gray-700 hover:bg-tool ${focusRingClass}`}
      >
        {formatUnitLocator(group.display, group.unit)} · {spanCount} span
        {spanCount === 1 ? "" : "s"}
      </button>
      {open ? (
        <ul id={panelId} className="px-3 pb-2 space-y-1">
          {group.citations.map(({ citation, globalIndex }) => (
            <SourceRow
              key={`${citationSourceId(citation)}-${citation.unit}-${globalIndex}`}
              citation={citation}
              index={globalIndex}
              threadId={threadId}
              developerMode={developerMode}
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
  threadId,
  developerMode,
}: {
  group: SourceGroup;
  defaultCollapsed: boolean;
  threadId: string | undefined;
  developerMode: boolean;
}) {
  const [open, setOpen] = useState(!defaultCollapsed);
  const unitCount = group.units.length;
  const spanCount = group.units.reduce(
    (sum, unitGroup) => sum + unitGroup.citations.length,
    0,
  );
  const panelId = `source-doc-${group.identity}`;

  return (
    <div className="border-t border-black first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-2 py-1.5 text-xs font-mono font-medium hover:bg-tool ${focusRingClass}`}
      >
        {group.label} · {unitCount}{" "}
        {sourceCountWord(group.units.map((unitGroup) => unitGroup.display))} ·{" "}
        {spanCount} spans
      </button>
      {open ? (
        <div id={panelId}>
          {group.units.map((unitGroup) => (
            <UnitSourcesGroup
              key={`${group.identity}-${unitGroup.unit}`}
              group={unitGroup}
              defaultCollapsed={defaultCollapsed}
              threadId={threadId}
              developerMode={developerMode}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SourcesList({
  citations,
  developerMode,
}: {
  citations: Citation[];
  developerMode: boolean;
}) {
  const spanCount = citations?.length ?? 0;
  const unitCount = countUniqueSourceUnits(citations ?? []);
  const defaultCollapsed = shouldCollapseSourcesList(spanCount);
  const [open, setOpen] = useState(!defaultCollapsed);
  const panelId = useId();
  // The thread a row may jump within is the thread whose turn is being
  // rendered, and that is the route this surface renders on — the wire
  // citation itself carries no thread id, so the route's own param is
  // the one place it can be read without a second copy of the state.
  const { thread_id: threadId } = useParams<{ thread_id?: string }>();
  if (!citations || citations.length === 0) return null;

  const groups = groupCitationsBySourceAndUnit(citations);

  return (
    <section className="mt-2 border border-black bg-card" aria-label="Sources">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`w-full text-left px-2 py-1.5 text-xs font-mono font-medium hover:bg-tool ${focusRingClass}`}
      >
        Sources（出处）· {unitCount}{" "}
        {sourceCountWord(citations.map((c) => c.display))} · {spanCount} spans
      </button>
      <p className="px-2 pb-1.5 text-xs text-gray-600 border-b border-black">
        {SOURCES_MATCH_NOTE}
      </p>
      {open ? (
        <div id={panelId}>
          {groups.map((group) => (
            <DocumentSourcesGroup
              key={group.identity}
              group={group}
              defaultCollapsed={defaultCollapsed}
              threadId={threadId}
              developerMode={developerMode}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** The visible refusal indicator: a decoded ``evidence_refusals`` entry
 * or a history ``rejected_citations`` row renders beside the answer —
 * never silently dropped, never an empty citation list standing in for a
 * refusal.
 *
 * ``data-code`` keeps the §6 code in the markup for both views. It is a
 * data attribute, not visible copy, which is how the developer-mode
 * surface stays derivable from the default DOM without printing a code
 * the reader cannot act on. */
function EvidenceRefusalNotice({
  refusals,
  developerMode = readDeveloperModeEnabled(),
}: {
  refusals: EvidenceRefusal[];
  developerMode?: boolean;
}) {
  if (refusals.length === 0) return null;
  return (
    <ul className="mt-2 space-y-0.5" aria-label="被拒绝的出处">
      {refusals.map((entry) => (
        <li
          key={`${entry.citation_index}-${entry.error}`}
          data-testid="evidence-refusal"
          data-code={entry.error}
          className="text-xs text-gray-500"
        >
          {evidenceRefusalRowText(entry, developerMode)}
        </li>
      ))}
    </ul>
  );
}

export default function AnswerWithCitations({
  answer,
  citations,
  refusals = [],
  developerMode = readDeveloperModeEnabled(),
}: {
  answer: string;
  citations: Citation[];
  refusals?: EvidenceRefusal[];
  developerMode?: boolean;
}) {
  if (!answer) return null;
  const parts = splitAnswerAtCitations(answer, citations);
  return (
    <>
      <p className="text-sm whitespace-pre-wrap break-words">
        {parts.map((part, i) => {
          if (typeof part === "string") {
            return <span key={`text-${i}`}>{part}</span>;
          }
          if ("markerIndex" in part) {
            return (
              <SuperscriptCitation
                key={`cite-marker-${i}`}
                citation={citations[part.markerIndex]}
                index={part.markerIndex}
              />
            );
          }
          return (
            <span key={`cite-span-${part.index}-${i}`}>
              <mark
                data-citation-index={part.index}
                className="bg-transparent border-b border-[#2563EB] text-inherit"
              >
                {part.quote}
              </mark>
            </span>
          );
        })}
      </p>
      <SourcesList citations={citations} developerMode={developerMode} />
      <EvidenceRefusalNotice refusals={refusals} developerMode={developerMode} />
    </>
  );
}
