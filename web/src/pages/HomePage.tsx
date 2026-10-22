// /: minimal home. The workbench has no 对话总览 — research threads
// live in the sidebar 会话 disclosure (specs/workbench-sidebar-rail.md
// T2), so this page renders only an empty placeholder pointing at the
// composer. The last-thread restore is NOT owned here: it belongs to
// the application-lifetime owner in ``App.tsx``, so a later visit to
// this page — browser Back included — stays home; the retired /kb and
// /monitor session gates are gone.
//
// The corpus-ingest door also lives here: it uploads to the permanent
// corpus WITHOUT opening or naming a thread, which is what keeps it a
// separate door from the composer's 附加文件 (ADR 0047).

import {
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { useNavigate } from "react-router-dom";
import * as api from "../api";
import { EMPTY_SESSIONS_COPY } from "../components/Sidebar";
import {
  describeIngestFailure,
  type IngestFailureNotice,
} from "../ingestFailure";
import {
  describeDemoError,
  resetDemoData,
} from "../demoMode";
import { useWorkspaceStore } from "../store";
import { describeCatalogOverview, describeCatalogWarning } from "../catalogOverview";
import type {
  CatalogWarning,
  GraphIndexJob,
  GraphIndexReport,
  IngestJobState,
  IngestResult,
  JobStatus,
} from "../types";

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

// Per-entry words for the sibling index job's own states; the
// readiness RULE lives in describeGraphReadiness, not here.
const GRAPH_JOB_LABELS: Record<GraphIndexJob["status"], string> = {
  in_progress: "进行中",
  ready: "就绪",
  failed: "失败",
  stale: "过期",
};

/** What the graph/C panel shows: one 图谱索引就绪 label, the compact
 * per-source list, and a named error line when the sibling report
 * carries one. */
export interface GraphReadinessView {
  communityLabel: string;
  jobs: { source_ref: string; label: string }[];
  errorLabel: string | null;
}

/**
 * Pure view of the pass-through graph/C report, exported so the
 * never-merge rule is testable in the node-env suite (no DOM).
 * 就绪 comes ONLY from ``community_index_state.status === "ready"``;
 * a missing state is 未确认 even when every ``jobs[]`` entry reads
 * ready, because per-source entries never establish corpus-wide
 * readiness (spec corpus-serve-ingest "Graph/C readiness").
 */
export function describeGraphReadiness(
  graph: GraphIndexReport | null,
): GraphReadinessView {
  if (!graph) {
    return { communityLabel: "未报告", jobs: [], errorLabel: null };
  }
  const state = graph.community_index_state;
  let communityLabel: string;
  if (!state) communityLabel = "未确认";
  else if (state.status === "ready") communityLabel = "就绪";
  else if (state.status === "in_progress") communityLabel = "进行中";
  else if (state.status === "failed") {
    communityLabel = `失败：${state.failure_code ?? "未命名"}`;
  } else communityLabel = "未确认";
  return {
    communityLabel,
    jobs: graph.jobs.map((job) => ({
      source_ref: job.source_ref,
      label:
        job.status === "failed" && job.failure_code
          ? `${GRAPH_JOB_LABELS[job.status]}：${job.failure_code}`
          : GRAPH_JOB_LABELS[job.status],
    })),
    // A named sibling-job error is always shown; hiding it would read
    // as "nothing wrong".
    errorLabel: graph.error
      ? graph.error.message
        ? `${graph.error.code} — ${graph.error.message}`
        : graph.error.code
      : null,
  };
}

export default function HomePage({
  demoModeEnabled = false,
  demoModeStatusLoaded = false,
}: {
  demoModeEnabled?: boolean;
  demoModeStatusLoaded?: boolean;
}) {
  const threads = useWorkspaceStore((s) => s.threads);
  const loadingThreads = useWorkspaceStore((s) => s.loadingThreads);

  // The last-thread restore is NOT owned here (T3). It belongs to the
  // application-lifetime owner in ``App.tsx``: a mount-local ref dies
  // with the route, so every later visit to "/" re-armed the restore
  // and browser Back bounced the operator back into the thread. The
  // homepage now only renders.

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">研究对话</h2>
      <p className="text-sm text-gray-600 mb-4">
        就本地资料库提问。
      </p>
      <DemoExperience
        enabled={demoModeEnabled}
        statusLoaded={demoModeStatusLoaded}
      />
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
      <CorpusOverview />
    </main>
  );
}

const DEMO_QUESTIONS = [
  "ZEUS 员工每年的年假是多少天，最多可结转几天？",
  "AEGIS 网关安装时机架前后需要留多少空间？最低固件版本是什么？",
  "ZEUS 生产变更请求缺少回滚计划时会怎样处理？",
  "ZEUS 董事会批准的季度股息是多少，何时支付？",
  "ZEUS 2024 年第三季度总收入和净利润分别是多少？",
  "ZEUS 供应商模板规定发票何时到期，逾期利息是多少？",
  "HELIOS 严重级别 1 支持请求多久确认，活动期间多久更新？",
  "平台项目当前有哪些未完成风险，下一检查点是什么时候？",
];

interface DemoActions {
  startingQuestion: string | null;
  confirmReset: boolean;
  resetting: boolean;
  resetNotice: string | null;
  questionError: string | null;
  askExample(question: string): Promise<void>;
  confirmDemoReset(): Promise<void>;
  requestReset(): void;
  cancelReset(): void;
}

function useDemoActions(): DemoActions {
  const createThread = useWorkspaceStore((s) => s.createThread);
  const openThread = useWorkspaceStore((s) => s.openThread);
  const sendQuestion = useWorkspaceStore((s) => s.sendQuestion);
  const navigate = useNavigate();
  const [startingQuestion, setStartingQuestion] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetNotice, setResetNotice] = useState<string | null>(null);
  const [questionError, setQuestionError] = useState<string | null>(null);

  async function askExample(question: string): Promise<void> {
    if (startingQuestion !== null || resetting) return;
    setStartingQuestion(question);
    setQuestionError(null);
    try {
      const threadId = await createThread(question);
      await openThread(threadId);
      const sending = sendQuestion(threadId, question);
      // An explicit PUSH: the homepage stays a history entry, so the
      // operator can come back to it with the browser's Back button.
      navigate(`/t/${threadId}`, { replace: false });
      await sending;
    } catch {
      setQuestionError("示例问题启动失败，请检查 fin 服务后重试。");
    } finally {
      setStartingQuestion(null);
    }
  }

  async function confirmDemoReset(): Promise<void> {
    if (resetting || startingQuestion !== null) return;
    setResetting(true);
    setResetNotice(null);
    try {
      await resetDemoData();
      setConfirmReset(false);
      setResetNotice("success");
    } catch (error) {
      setResetNotice(describeDemoError(error));
    } finally {
      setResetting(false);
    }
  }

  function requestReset(): void {
    setConfirmReset(true);
    setResetNotice(null);
  }

  function cancelReset(): void {
    setConfirmReset(false);
  }

  return {
    startingQuestion,
    confirmReset,
    resetting,
    resetNotice,
    questionError,
    askExample,
    confirmDemoReset,
    requestReset,
    cancelReset,
  };
}

function DemoResetButton({
  disabled,
  onRequest,
}: {
  disabled: boolean;
  onRequest(): void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <button
        type="button"
        data-testid="demo-reset-button"
        disabled={disabled}
        onClick={onRequest}
        className="border border-black px-3 py-1.5 text-xs font-medium hover:bg-tool disabled:opacity-50"
      >
        重置演示数据
      </button>
    </div>
  );
}

function DemoQuestions({
  disabled,
  error,
  onAsk,
}: {
  disabled: boolean;
  error: string | null;
  onAsk(question: string): void;
}) {
  return (
    <>
      <h3 className="mt-3 text-sm font-bold">试着问这些问题</h3>
      <ul className="mt-2 grid gap-2 sm:grid-cols-2">
        {DEMO_QUESTIONS.map((question) => (
          <li key={question}>
            <button
              type="button"
              data-testid="demo-question"
              aria-label={question}
              disabled={disabled}
              onClick={() => onAsk(question)}
              className="w-full border border-black bg-page p-2 text-left text-xs hover:bg-tool disabled:opacity-50"
            >
              {question}
            </button>
          </li>
        ))}
      </ul>
      {error ? (
        <p data-testid="demo-question-error" role="alert" className="mt-2 text-xs text-red-700">
          {error}
        </p>
      ) : null}
    </>
  );
}

function DemoResetFeedback({
  confirmReset,
  disabled,
  resetting,
  resetNotice,
  onConfirm,
  onCancel,
}: {
  confirmReset: boolean;
  disabled: boolean;
  resetting: boolean;
  resetNotice: string | null;
  onConfirm(): void;
  onCancel(): void;
}) {
  return (
    <>
      {confirmReset ? (
        <div
          data-testid="demo-reset-confirmation"
          className="mt-3 border border-black bg-tool p-3"
        >
          <p className="text-xs text-gray-700">
            这会重新生成演示语料，并覆盖当前演示库中的改动。
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              data-testid="demo-reset-confirm"
              disabled={disabled}
              onClick={onConfirm}
              className="border border-black bg-accent px-3 py-1 text-xs text-white disabled:opacity-50"
            >
              {resetting ? "重置中…" : "确认重置"}
            </button>
            <button
              type="button"
              data-testid="demo-reset-cancel"
              disabled={resetting}
              onClick={onCancel}
              className="border border-black px-3 py-1 text-xs disabled:opacity-50"
            >
              取消
            </button>
          </div>
        </div>
      ) : null}
      {resetNotice === "success" ? (
        <p data-testid="demo-reset-result" className="mt-2 text-xs text-green-800">
          演示数据已重置。
        </p>
      ) : resetNotice ? (
        <p data-testid="demo-reset-error" role="alert" className="mt-2 text-xs text-red-700">
          {resetNotice}
        </p>
      ) : null}
    </>
  );
}

function DemoExperience({
  enabled,
  statusLoaded,
}: {
  enabled: boolean;
  statusLoaded: boolean;
}) {
  const actions = useDemoActions();
  if (!statusLoaded || !enabled) return null;
  const disabled = actions.resetting || actions.startingQuestion !== null;

  return (
    <section
      data-testid="demo-experience"
      className="mb-6 border border-black bg-card p-4"
    >
      <DemoResetButton disabled={disabled} onRequest={actions.requestReset} />
      <DemoQuestions
        disabled={disabled}
        error={actions.questionError}
        onAsk={(question) => void actions.askExample(question)}
      />
      <DemoResetFeedback
        confirmReset={actions.confirmReset}
        disabled={disabled}
        resetting={actions.resetting}
        resetNotice={actions.resetNotice}
        onConfirm={() => void actions.confirmDemoReset()}
        onCancel={actions.cancelReset}
      />
    </section>
  );
}

/** The ingest form's optional descriptive metadata. The file alone is
 * always enough; every field here may stay blank. */
interface IngestFormFields {
  company: string;
  period: string;
  title: string;
}

/** Whether the door is worth a round trip: the file is the only required
 * input, so a picked file is always submittable. */
export function isIngestSubmittable(file: File | null): boolean {
  return file !== null;
}

/**
 * The metadata one submission carries: the non-blank optional fields only.
 * A blank string would reach the server as a present-but-blank value, so
 * it is omitted rather than sent.
 */
export function ingestSubmission(
  fields: IngestFormFields,
): api.IngestMeta {
  const submission: api.IngestMeta = {};
  if (fields.title.trim()) submission.title = fields.title.trim();
  if (fields.company.trim()) submission.company = fields.company.trim();
  if (fields.period.trim()) submission.period = fields.period.trim();
  return submission;
}

/** The one sentence above the form: the file is the whole requirement. */
const INGEST_DOOR_HINT = "选择文件即可入库；标题、公司和期间为可选说明信息。";

const INGEST_INPUT_CLASS = "border border-black bg-card px-2 py-1 text-sm";

interface IngestFormFieldsViewProps {
  fields: IngestFormFields;
  file: File | null;
  fileInputRef: RefObject<HTMLInputElement>;
  submitting: boolean;
  submittable: boolean;
  onPickFile(file: File | null): void;
  onChange(next: Partial<IngestFormFields>): void;
  onSubmit(): void;
}

/** The optional metadata boxes. They describe the document; none of them
 * affects the upload flow or the identity. */
function IngestMetadataFields({
  fields,
  onChange,
}: {
  fields: IngestFormFields;
  onChange(next: Partial<IngestFormFields>): void;
}) {
  return (
    <>
      <label className="flex flex-col gap-1 text-xs text-gray-700">
        标题
        <input
          data-testid="corpus-ingest-title"
          value={fields.title}
          placeholder="留空则用文件名"
          onChange={(e) => onChange({ title: e.target.value })}
          className={INGEST_INPUT_CLASS}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-gray-700">
        公司
        <input
          data-testid="corpus-ingest-company"
          value={fields.company}
          placeholder="选填"
          onChange={(e) => onChange({ company: e.target.value })}
          className={INGEST_INPUT_CLASS}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-gray-700">
        期间
        <input
          data-testid="corpus-ingest-period"
          value={fields.period}
          placeholder="选填"
          onChange={(e) => onChange({ period: e.target.value })}
          className={INGEST_INPUT_CLASS}
        />
      </label>
    </>
  );
}

/** The one line under the form: what the picked file contributes. */
function IngestFileNote({ file }: { file: File | null }) {
  if (!file) {
    return (
      <p className="mt-2 text-xs text-gray-400">尚未选择文件。</p>
    );
  }
  return (
    <p className="mt-2 text-xs text-gray-500">
      已选文件 <span className="font-mono">{file.name}</span>
      （文件名只作标题，不参与编号）
    </p>
  );
}

/**
 * The door's file picker and metadata boxes, lifted out so the component
 * above owns only the submit lifecycle.
 */
function IngestFormFieldsView({
  fields,
  file,
  fileInputRef,
  submitting,
  submittable,
  onPickFile,
  onChange,
  onSubmit,
}: IngestFormFieldsViewProps) {
  return (
    <>
      <p className="text-xs text-gray-600 mb-3">{INGEST_DOOR_HINT}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
        className="flex flex-wrap items-end gap-3"
      >
        <input
          ref={fileInputRef}
          type="file"
          data-testid="corpus-ingest-file"
          className="text-xs"
          onChange={(e) => onPickFile(e.target.files?.[0] ?? null)}
        />
        <IngestMetadataFields fields={fields} onChange={onChange} />
        <button
          type="submit"
          data-testid="corpus-ingest-submit"
          disabled={!submittable || submitting}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "入库"}
        </button>
      </form>
      <IngestFileNote file={file} />
    </>
  );
}

/**
 * The corpus-ingest door: one file, submitted outside any thread. The POST
 * only durably accepts, so what the operator watches afterwards is the
 * polled job state — and because the submitted job id is persisted, a
 * reload lands back on that same job rather than on an empty form.
 *
 * A file is the only required input; the metadata boxes are optional
 * descriptive text, and the identity is derived server-side from the
 * trusted corpus scope plus the content digest. The two rules are the pure
 * helpers above, and the markup is :func:`IngestFormFieldsView`, so this
 * component owns only the submit lifecycle and each rule stays unit-tested
 * without a DOM.
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
  const [fields, setFields] = useState<IngestFormFields>({
    company: "",
    period: "",
    title: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [rejection, setRejection] = useState<unknown>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const submittable = isIngestSubmittable(file);

  function changeFields(next: Partial<IngestFormFields>): void {
    setFields((current) => ({ ...current, ...next }));
  }

  async function submit(): Promise<void> {
    if (!file || !submittable || submitting) return;
    setSubmitting(true);
    setRejection(null);
    try {
      await ingestFile(file, ingestSubmission(fields));
      // The file is spent; the fields stay so a rejected upload can
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
      <IngestFormFieldsView
        fields={fields}
        file={file}
        fileInputRef={fileInputRef}
        submitting={submitting}
        submittable={submittable}
        onPickFile={setFile}
        onChange={changeFields}
        onSubmit={() => void submit()}
      />
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
 * The generic catalog surface (T6): what is in the library, named by
 * TITLE. An ordinary document has no company or period, so this is where
 * it becomes visible — the frozen financial ``/api/corpus`` projection
 * cannot show it at all.
 *
 * The grouping RULE lives in ``describeCatalogOverview`` beside the
 * catalog row contract; this component only renders the answer, so the
 * "only qualifying dimensions, 未分类 for missing, flat titles otherwise"
 * rules stay unit-tested in the node-env suite.
 */
function CorpusOverview() {
  const overview = useWorkspaceStore((s) => s.catalogOverview);
  const loaded = useWorkspaceStore((s) => s.catalogOverviewLoaded);
  const loadCatalogOverview = useWorkspaceStore((s) => s.loadCatalogOverview);

  useEffect(() => {
    void loadCatalogOverview();
  }, [loadCatalogOverview]);

  const view = describeCatalogOverview(overview);
  if (!loaded) {
    return (
      <section
        data-testid="corpus-overview"
        className="mt-6 border border-black bg-card p-4"
      >
        <p className="text-xs text-gray-400">资料库加载中…</p>
      </section>
    );
  }
  return (
    <section
      data-testid="corpus-overview"
      className="mt-6 border border-black bg-card p-4"
    >
      <h3 className="text-sm font-bold mb-1">资料库</h3>
      <p className="text-xs text-gray-600 mb-2">
        共 <span className="font-medium">{view.count}</span> 份文档
      </p>
      {view.count === 0 ? (
        <p className="text-xs text-gray-400">资料库还是空的。</p>
      ) : null}
      {view.dimensions.map((dimension) => (
        <div key={dimension.key} className="mt-2">
          <p className="text-xs font-medium">{dimension.label}</p>
          <ul className="mt-1 space-y-0.5">
            {dimension.groups.map((group) => (
              <li key={group.value} className="text-xs text-gray-600">
                {group.value}
                <span className="ml-1 text-gray-400">
                  （{group.document_ids.length} 份）
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {/* The fallback list replaces a grouping, never joins one: with a
          qualifying dimension the titles are already implied by it. */}
      {view.titles.length > 0 ? (
        <ul className="mt-2 space-y-0.5" data-testid="corpus-overview-titles">
          {view.titles.map((title) => (
            <li key={title} className="text-xs text-gray-700">
              {title}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * Pure 正文就绪 label, kept out of the panel so the branch count stays
 * in the view helpers: 是/否 comes ONLY from the polled ``text_ready``;
 * before the first status read the accept body proves nothing, hence
 * 未确认.
 */
function describeTextReadiness(job: JobStatus | null): string {
  if (!job) return "未确认";
  return job.text_ready ? "是" : "否";
}

/**
 * The graph/C lines rendered from the pure describeGraphReadiness view.
 * Kept as one sub-view so IngestJobPanel stays a layout shell; the
 * readiness RULE itself never lives here.
 */
function GraphReadinessSection({ view }: { view: GraphReadinessView }) {
  return (
    <>
      <p className="mt-1 text-gray-500">
        图谱索引就绪：<span className="font-medium">{view.communityLabel}</span>
      </p>
      {view.jobs.length > 0 ? (
        <p className="mt-1 text-gray-500">
          来源索引：
          {view.jobs
            .map((entry) => `${entry.source_ref} ${entry.label}`)
            .join(" · ")}
        </p>
      ) : null}
      {view.errorLabel ? (
        <p className="mt-1 text-red-700">
          索引错误：
          <span className="font-mono">{view.errorLabel}</span>
        </p>
      ) : null}
    </>
  );
}

/**
 * The durable catalog-publication warning line (T6), or nothing once a
 * success is recorded. Kept as its own sub-view so the panel stays a
 * layout shell and the "safe copy, never a server string" rule stays in
 * :func:`describeCatalogWarning`, where it is unit-tested.
 */
function CatalogWarningSection({
  warning,
}: {
  warning: CatalogWarning | null | undefined;
}) {
  const view = describeCatalogWarning(warning);
  if (!view) return null;
  return (
    <p
      data-testid="catalog-warning"
      data-warning-code={view.code}
      className="mt-1 text-amber-800"
    >
      <span aria-hidden="true">⚠ </span>
      {view.message}
      {view.detail ? (
        <span className="ml-1 font-mono text-gray-500">{view.detail}</span>
      ) : null}
      <span className="ml-1 text-gray-500">（第 {view.attempts} 次尝试）</span>
    </p>
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
  // Before the first status read there is no sibling report to show,
  // which is the same 「未报告」 the null graph maps to.
  const graphView = describeGraphReadiness(job?.graph ?? null);
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
      <CatalogWarningSection warning={job?.catalog_warning} />
      {job?.state === "failed" ? (
        <p className="mt-1 text-red-700">
          失败原因：
          <span className="font-mono">{job.error_type ?? "未命名"}</span>
          {job.error_message ? ` — ${job.error_message}` : ""}
        </p>
      ) : null}
      {/* The two readiness lines stay separate by construction: text
          readiness is read ONLY from text_ready… */}
      <p className="mt-1">
        正文就绪：
        <span className="font-medium">{describeTextReadiness(job)}</span>
      </p>
      {/* …and graph/C readiness ONLY from the sibling report's
          community state, so a text ``ready`` can never read as
          corpus-wide graph readiness (spec "Graph/C readiness"). */}
      <GraphReadinessSection view={graphView} />
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
