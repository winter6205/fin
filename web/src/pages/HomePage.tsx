// /: minimal home. The workbench has no 对话总览 — research threads
// live in the sidebar 会话 disclosure (specs/workbench-sidebar-rail.md
// T2), so this page renders only an empty placeholder pointing at the
// composer. What stays is the last-thread restore for returning visits
// (decideLastThreadRestore): a restorable thread bounces straight to
// /t/{id}; the retired /kb and /monitor session gates are gone.
//
// The corpus-ingest door also lives here: it uploads to the permanent
// corpus WITHOUT opening or naming a thread, which is what keeps it a
// separate door from the composer's 附加文件 (ADR 0047).

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { EMPTY_SESSIONS_COPY } from "../components/Sidebar";
import {
  describeIngestFailure,
  type IngestFailureNotice,
} from "../ingestFailure";
import {
  decideLastThreadRestore,
  forgetLastThread,
  readLastThread,
} from "../lastThread";
import { useWorkspaceStore } from "../store";
import type { IngestJobState, IngestResult, JobStatus } from "../types";

// The polled state in ordinary words. ``ready`` is said as TEXT
// readiness only — it is never corpus-wide graph/C readiness.
const STATE_LABELS: Record<IngestJobState, string> = {
  pending: "排队中",
  fetching: "读取原文",
  extracting: "解析中",
  ocr: "识别扫描件",
  cataloging: "切分入库",
  ready: "正文可检索",
  failed: "失败",
};

export default function HomePage() {
  const threads = useWorkspaceStore((s) => s.threads);
  const loadingThreads = useWorkspaceStore((s) => s.loadingThreads);
  const navigate = useNavigate();

  // T5 (#35, spec line 42): a RETURNING visit restores the last
  // opened `/t/{id}` when that id still exists. First visit (no
  // stored id, or the thread was deleted / is unknown) stays on
  // home — never a blank chat. One-shot per mount so a deliberate
  // later navigation home does not bounce the operator away again.
  const threadsLoaded = useWorkspaceStore((s) => s.threadsLoaded);
  const restoreAttempted = useRef(false);
  useEffect(() => {
    if (restoreAttempted.current) return;
    const decision = decideLastThreadRestore({
      threadsLoaded,
      lastThreadId: readLastThread(),
      threadIds: threads.map((t) => t.id),
    });
    if (decision.action === "wait") return;
    restoreAttempted.current = true;
    if (decision.action === "restore") {
      navigate(`/t/${decision.threadId}`, { replace: true });
    } else if (decision.action === "forget") {
      forgetLastThread();
    }
  }, [threadsLoaded, threads, navigate]);

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">研究对话</h2>
      <p className="text-sm text-gray-600 mb-4">
        就本地语料库里的 SEC filing 提问。
      </p>
      {/* No thread-card wall here (the 对话总览 the spec retired):
          threads are listed and opened in the sidebar 会话 group.
          The empty state keeps the dashed-border copy so a fresh
          visit is never a blank pane. */}
      {threads.length === 0 && !loadingThreads ? (
        <p className="text-sm text-gray-500 border border-dashed border-black p-4 bg-card">
          {EMPTY_SESSIONS_COPY}
        </p>
      ) : null}
      {loadingThreads && <p className="text-xs text-gray-400 mt-2">加载中…</p>}
      <CorpusIngestDoor />
    </main>
  );
}

/**
 * The corpus-ingest door: file + company + period, submitted outside any
 * thread. The POST only durably accepts, so what the operator watches
 * afterwards is the polled job state — and because the submitted job id
 * is persisted, a reload lands back on that same job rather than on an
 * empty form.
 */
function CorpusIngestDoor() {
  const ingestFile = useWorkspaceStore((s) => s.ingestFile);
  const loadIngestJob = useWorkspaceStore((s) => s.loadIngestJob);
  const ingestAccepted = useWorkspaceStore((s) => s.ingestAccepted);
  const ingestJob = useWorkspaceStore((s) => s.ingestJob);
  const ingestJobFailure = useWorkspaceStore((s) => s.ingestJobFailure);

  // A persisted submission means home owes the operator its state
  // again; with nothing ever submitted this reads nothing.
  useEffect(() => {
    void loadIngestJob();
  }, [loadIngestJob]);

  const [file, setFile] = useState<File | null>(null);
  const [company, setCompany] = useState("");
  const [period, setPeriod] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [rejection, setRejection] = useState<unknown>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Both fields feed the stable identity, so an empty one is never
  // worth a round trip: the endpoint would only answer the named
  // ``document_id_missing``.
  const submittable =
    file !== null && company.trim() !== "" && period.trim() !== "";

  async function submit(): Promise<void> {
    if (!file || !submittable || submitting) return;
    setSubmitting(true);
    setRejection(null);
    try {
      await ingestFile(file, {
        company: company.trim(),
        period: period.trim(),
      });
      // The file is spent; the two fields stay so a rejected upload can
      // be corrected and re-sent without retyping them.
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (error) {
      setRejection(error);
    } finally {
      setSubmitting(false);
    }
  }

  const submitNotice =
    rejection === null ? null : describeIngestFailure(rejection);
  const statusNotice = ingestJobFailure
    ? describeIngestFailure(ingestJobFailure)
    : null;

  return (
    <section className="mt-6 border border-black bg-card p-4">
      <h3 className="text-sm font-bold mb-1">语料入库</h3>
      <p className="text-xs text-gray-600 mb-3">
        选文件并填写公司与期间即可入库，不打开任何对话；解析与切分在后台进行。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="flex flex-wrap items-end gap-3"
      >
        <input
          ref={fileInputRef}
          type="file"
          data-testid="corpus-ingest-file"
          className="text-xs"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <label className="flex flex-col gap-1 text-xs text-gray-700">
          公司
          <input
            data-testid="corpus-ingest-company"
            value={company}
            placeholder="aapl"
            onChange={(e) => setCompany(e.target.value)}
            className="border border-black bg-card px-2 py-1 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-700">
          期间
          <input
            data-testid="corpus-ingest-period"
            value={period}
            placeholder="2022q3"
            onChange={(e) => setPeriod(e.target.value)}
            className="border border-black bg-card px-2 py-1 text-sm"
          />
        </label>
        <button
          type="submit"
          data-testid="corpus-ingest-submit"
          disabled={!submittable || submitting}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "入库"}
        </button>
      </form>
      {file ? (
        <p className="mt-2 text-xs text-gray-500">
          已选文件 <span className="font-mono">{file.name}</span>
          （文件名不参与 document_id）
        </p>
      ) : (
        <p className="mt-2 text-xs text-gray-400">
          选择文件并填写公司、期间后可入库。
        </p>
      )}
      <IngestJobPanel accepted={ingestAccepted} job={ingestJob} />
      {submitNotice ? (
        <FailureNotice testId="corpus-ingest-failure" notice={submitNotice} />
      ) : null}
      {statusNotice ? (
        <FailureNotice
          testId="corpus-ingest-status-failure"
          notice={statusNotice}
        />
      ) : null}
    </section>
  );
}

/**
 * The job the operator last submitted. Before the first status read the
 * accept body is what is known, so it is shown as-is; afterwards the
 * polled record replaces it.
 */
function IngestJobPanel({
  accepted,
  job,
}: {
  accepted: IngestResult | null;
  job: JobStatus | null;
}) {
  const shown = job ?? accepted;
  if (!shown) return null;
  return (
    <div
      data-testid="corpus-ingest-job"
      className="mt-3 border border-black bg-tool px-2 py-1.5 text-xs text-gray-800"
    >
      <p>
        状态：
        <span className="font-medium">{STATE_LABELS[shown.state]}</span>{" "}
        <span className="font-mono text-gray-500">{shown.state}</span>
      </p>
      <p>
        job_id <span className="font-mono">{shown.job_id}</span> · document_id{" "}
        <span className="font-mono">{shown.document_id}</span> · version{" "}
        <span className="font-mono">{shown.version}</span>
      </p>
      {job?.state === "failed" ? (
        <p className="mt-1 text-red-700">
          失败原因：
          <span className="font-mono">{job.error_type ?? "未命名"}</span>
          {job.error_message ? ` — ${job.error_message}` : ""}
        </p>
      ) : null}
      {/* Graph/C readiness belongs to a separate job and the status door
          does not report it yet; saying so keeps a text ``ready`` from
          ever reading as corpus-wide graph readiness. */}
      <p className="mt-1 text-gray-500">图谱索引就绪：未报告</p>
    </div>
  );
}

function FailureNotice({
  testId,
  notice,
}: {
  testId: string;
  notice: IngestFailureNotice;
}) {
  return (
    <div
      role="alert"
      data-testid={testId}
      data-failure-kind={notice.kind}
      className="mt-3 border border-black bg-tool px-2 py-1.5 text-sm text-gray-800"
    >
      {notice.message}
    </div>
  );
}
