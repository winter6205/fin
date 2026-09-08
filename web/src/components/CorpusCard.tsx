// One filing card. Click = create a thread + prefill the composer
// with the filing question; NEVER auto-send (spec).

import { useNavigate } from "react-router-dom";
import { cardMetaLine, cardThreadQuestion } from "../corpusCards";
import { useWorkspaceStore } from "../store";
import type { Filing } from "../types";

export default function CorpusCard({ filing }: { filing: Filing }) {
  const navigate = useNavigate();
  const createThread = useWorkspaceStore((s) => s.createThread);

  return (
    <button
      onClick={async () => {
        const id = await createThread(cardThreadQuestion(filing));
        if (id) {
          // Prefill via navigation state so ThreadPage's Composer
          // picks it up without auto-sending.
          navigate(`/t/${id}`, {
            state: {
              prefill: `Summarize ${filing.document_id} (${filing.period}).`,
            },
          });
        }
      }}
      className="text-left border border-black bg-card p-3 hover:bg-tool"
    >
      <p className="font-mono text-sm font-semibold">{filing.document_id}</p>
      <p className="text-xs text-gray-600 mt-1">{cardMetaLine(filing)}</p>
      <p className="text-xs text-gray-500">{filing.page_count} 页</p>
    </button>
  );
}
