// Sources list for an assistant turn. T1 renders the plain list;
// inline chips inside the answer prose are T3 polish per spec.

import type { Citation } from "../types";

export default function CitationList({ citations }: { citations: Citation[] }) {
  if (!citations || citations.length === 0) return null;
  return (
    <div className="mt-2 border-t border-dashed border-black pt-2">
      <p className="text-xs font-semibold uppercase tracking-wide mb-1">
        Sources
      </p>
      <ul className="space-y-1">
        {citations.map((c, i) => (
          <li
            key={`${c.document_id}-${c.page}-${i}`}
            title={c.quote}
            className="text-xs text-gray-700"
          >
            <span className="font-mono">{c.document_id}</span> p.{c.page}:{" "}
            {c.quote.length > 80 ? c.quote.slice(0, 80) + "…" : c.quote}
          </li>
        ))}
      </ul>
    </div>
  );
}
