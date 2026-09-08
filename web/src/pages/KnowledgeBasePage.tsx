// /kb: 知识库 (T1 chrome + /kb, specs/workbench-chrome.md).
//
// The workbench chrome's catalog surface. The corpus-ingest door
// (ADR 0047) and the corpus card grid both live here — this is the
// page the operator navigates to when they want to see what is in
// the library or push a new filing. Visiting this page sets the
// ``fin.kb`` session gate so a returning visit lands back here
// instead of bouncing the operator onto their last /t/{id}.

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import CorpusCard from "../components/CorpusCard";
import IngestCorpusControl from "../components/IngestCorpusControl";
import { corpusSummary } from "../corpusCards";
import { markKbVisit } from "../sessionKeys";
import { useWorkspaceStore } from "../store";

export default function KnowledgeBasePage() {
  // T1: visiting /kb this session lights the kb gate so a returning
  // visit lands back here instead of bouncing the operator to their
  // last /t/{id}. One-shot per mount so a SPA-internal navigation
  // from /kb back to / doesn't keep the gate fresh for a later
  // deliberate choice.
  useEffect(() => {
    markKbVisit();
  }, []);

  const corpus = useWorkspaceStore((s) => s.corpus);
  const corpusLoaded = useWorkspaceStore((s) => s.corpusLoaded);
  const navigate = useNavigate();
  const createThread = useWorkspaceStore((s) => s.createThread);

  const startThread = async (firstMessage: string | null) => {
    const id = await createThread(firstMessage);
    if (id) navigate(`/t/${id}`);
  };

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">知识库</h2>
      <p className="text-sm text-gray-600 mb-4">
        这里是已经放进来的文件。点一份开始提问，或添加新文件。
      </p>

      {!corpusLoaded ? (
        <>
          <h3 className="text-base font-semibold mb-1">正在加载知识库…</h3>
          <p className="text-sm text-gray-600 mb-3">正在读取文件列表</p>
          <div
            className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
            aria-busy="true"
          >
            <KbSkeletonCard />
            <KbSkeletonCard />
            <KbSkeletonCard />
          </div>
        </>
      ) : (
        <>
          <IngestCorpusControl />
          {corpus.length === 0 ? (
            <div className="border border-black bg-card p-4 max-w-md">
              <h3 className="text-base font-semibold mb-1">还没有文件</h3>
              <p className="text-sm text-gray-600 mb-3">
                请先添加文件，再提问。
              </p>
              <button
                onClick={() => void startThread(null)}
                className="border border-black px-3 py-1.5 text-sm hover:bg-accent hover:text-white"
              >
                先开一个空对话
              </button>
            </div>
          ) : (
            <>
              <h3 className="text-base font-semibold mb-2">
                知识库（{corpusSummary(corpus)}）
              </h3>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {corpus.map((f) => (
                  <CorpusCard key={f.document_id} filing={f} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </main>
  );
}

function KbSkeletonCard() {
  return (
    <div
      aria-hidden="true"
      className="border border-black bg-card p-3 animate-pulse"
    >
      <div className="h-4 bg-gray-200 border border-gray-300 mb-2 w-3/4" />
      <div className="h-3 bg-gray-200 border border-gray-300 mb-1 w-1/2" />
      <div className="h-3 bg-gray-200 border border-gray-300 w-1/4" />
    </div>
  );
}
