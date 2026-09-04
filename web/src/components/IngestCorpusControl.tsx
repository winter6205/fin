// The corpus-ingest door (corpus-serve-ingest T5, ADR 0047): a file
// picker in the home corpus area — the ONLY UI that stores into the
// retrieve backend (parse + 切分 + MCP push via /api/corpus/ingest).
//
// Placement rationale (recorded for the plan's open point): ADR 0047
// keeps the two upload doors apart, so this control deliberately does
// NOT live in the Composer and is never a drag-to-thread affordance.
// The home corpus area is where the operator already sees what is in
// the corpus — and where the empty-corpus copy says "请运行 ingest
// 后再提问" — so the door sits here, above the corpus cards.
//
// Minimal on purpose: no metadata form. The endpoint derives
// document_id from company/period or the filename and classifies
// doc_type per unit; a file whose name names no supported format is
// the named format_unspecified failure, rendered below.

import { useRef, useState } from "react";
import {
  describeIngestFailure,
  type IngestFailureNotice,
} from "../ingestFailure";
import type { IngestResult } from "../types";
import { useWorkspaceStore } from "../store";

export default function IngestCorpusControl() {
  const ingestFile = useWorkspaceStore((s) => s.ingestFile);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<IngestFailureNotice | null>(null);
  const [ingested, setIngested] = useState<IngestResult | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const run = async (file: File) => {
    setBusy(true);
    setFailure(null);
    setIngested(null);
    try {
      setIngested(await ingestFile(file));
    } catch (error) {
      setFailure(describeIngestFailure(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="ingest-control" className="mb-3 max-w-md">
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          // Clear the selection so picking the SAME file again
          // retriggers onChange.
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) void run(file);
        }}
      />
      <button
        type="button"
        data-testid="ingest-file-button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        className="border border-black bg-card px-3 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
      >
        {busy ? "入库中…" : "Ingest 语料文件…"}
      </button>
      {ingested ? (
        <p data-testid="ingest-success" className="mt-1 text-xs text-gray-600">
          已入库 {ingested.document_id}：{ingested.block_count} 块（新增{" "}
          {ingested.written}）
        </p>
      ) : null}
      {failure ? (
        <p
          role="alert"
          data-testid="ingest-failure"
          data-failure-kind={failure.kind}
          className="mt-1 text-xs text-gray-800"
        >
          {failure.message}
        </p>
      ) : null}
    </div>
  );
}
