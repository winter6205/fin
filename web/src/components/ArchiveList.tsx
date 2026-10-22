// question-workspace W5: the archive list and its explicit restore action
// (specs/question-workspace.md §"Active work, outcomes, and history"; SC-6).
//
// Three facts this surface must keep apart, all of which the pure module
// (``archiveRestore.ts``) decides and this file only renders:
//
//   * ARCHIVING is not completion: an archived task keeps whatever outcome
//     it had when it was archived, which may still be 进行中.
//   * RESTORE is explicit and host-authoritative: the button sends a named
//     archive, and while Runtime owes a confirmation the EXISTING current
//     task stays current. The click — and the send's own acceptance —
//     move nothing.
//   * RESTORED MATERIAL STAYS HISTORICAL: an archived answer and its
//     citations are evidence for the archive's own time, not current
//     support and not current authorization.

import {
  ARCHIVE_HISTORY_NOTICE,
  archiveOutcomeLabel,
  archiveRows,
  restorableArchiveIds,
  type ArchiveRow,
  type RestoreDecision,
  type RestoreState,
} from "../archiveRestore";
import { TASK_RUNTIME_REFUSAL_COPY, type TaskRuntimeDecodeRefusal } from "../capabilities/task/taskRuntime";
import { useWorkspaceStore } from "../store";

/** Which paragraph a verdict occupies, and its tone. Only ``refused`` and
 * ``unresolved`` are stated as a problem: ``awaiting_confirmation`` is a
 * request still owed an answer, not a failure. */
const VERDICT_PRESENTATION: Readonly<
  Record<RestoreState, { testid: string | null; tone: string }>
> = {
  idle: { testid: null, tone: "" },
  awaiting_confirmation: { testid: "archive-awaiting", tone: "text-gray-600" },
  unresolved: { testid: "archive-unresolved", tone: "text-red-700" },
  refused: { testid: "archive-refusal", tone: "text-red-700" },
  restored: { testid: "archive-restored", tone: "text-gray-800" },
};

/** What the section renders, resolved from the store by the wrapper
 * above. Props (not store hooks) so the render decision is testable
 * without a DOM — the same split as the pure modules this project
 * renders from. */
export interface ArchiveListViewProps {
  /** The thread this section is mounted on. A verdict computed for another
   * thread is dropped, not re-labelled. */
  threadId: string;
  rows: ArchiveRow[];
  restorable: Set<string>;
  /** THE restore decision, computed by the page from this thread's facts.
   * Nothing here derives a verdict from local state: the request record
   * names the thread it belongs to, and a verdict computed for another
   * thread is dropped rather than re-labelled. */
  decision: RestoreDecision | null;
  /** The thread's task-envelope decode refusal, if its last projection
   * read was refused. */
  projectionRefusal: TaskRuntimeDecodeRefusal | null;
  onRestore: (archiveId: string) => void;
}

function RestoreVerdict({ verdict }: { verdict: RestoreDecision }) {
  const shown = VERDICT_PRESENTATION[verdict.state];
  if (shown.testid === null || verdict.notice === null) return null;
  return (
    <p data-testid={shown.testid} className={`text-xs ${shown.tone}`}>
      {verdict.notice}
    </p>
  );
}

/** The rows and their one explicit action. A row that was never listed
 * with a usable id gets no button: an action the surface cannot see
 * through is worse than no action. */
function ArchiveRows({
  rows,
  restorable,
  verdict,
  onRestore,
}: {
  rows: ArchiveRow[];
  restorable: Set<string>;
  verdict: RestoreDecision | null;
  onRestore: (archiveId: string) => void;
}) {
  // Only the window in which the host has not answered at all holds the
  // listing still. A settled request — refused, unheard, or accepted and
  // still waiting for Runtime to prove it — keeps its sentence on screen but
  // must not park every other row indefinitely.
  const inFlight = verdict?.requestUnsettled === true;
  return (
    <ul className="space-y-1">
      {rows.map((row) => (
        <li
          key={row.archiveId}
          data-testid={`archive-row-${row.archiveId}`}
          className="flex items-center justify-between gap-2 text-xs"
        >
          <span className="min-w-0 break-words">
            {row.goal || "（未记录目标）"}
            <span className="ml-1 text-gray-500">
              {archiveOutcomeLabel(row.outcomeAtArchive)}
            </span>
          </span>
          {restorable.has(row.archiveId) ? (
            <button
              type="button"
              data-testid={`archive-restore-${row.archiveId}`}
              disabled={inFlight}
              onClick={() => onRestore(row.archiveId)}
              className="shrink-0 border border-black bg-card px-2 py-1 hover:bg-accent hover:text-white disabled:opacity-50"
            >
              {verdict?.pendingArchiveId === row.archiveId ? "复核中…" : "恢复"}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** Whether the section has anything to say. Nothing owed, no refusal, and no
 * rows is an empty history: it renders nothing. A refusal or an unresolved
 * read keeps it mounted — an empty list from a broken envelope must not read
 * as "no tasks recorded". */
function sectionIsVisible(
  rows: ArchiveRow[],
  projectionRefusal: TaskRuntimeDecodeRefusal | null,
  verdict: RestoreDecision | null,
): boolean {
  return (
    rows.length > 0 ||
    projectionRefusal !== null ||
    (verdict !== null && verdict.state !== "idle")
  );
}

/** The history notice rides whatever listing is on screen, whether or not a
 * verdict was computed: an archive exists, so history stays history. */
function resolvedHistoryNotice(
  verdict: RestoreDecision | null,
  rows: ArchiveRow[],
): string | null {
  return (
    verdict?.historyNotice ??
    (rows.length > 0 ? ARCHIVE_HISTORY_NOTICE : null)
  );
}

export function ArchiveListView({
  threadId,
  rows,
  restorable,
  decision,
  projectionRefusal,
  onRestore,
}: ArchiveListViewProps) {
  const verdict =
    decision !== null && decision.threadId === threadId ? decision : null;
  const historyNotice = resolvedHistoryNotice(verdict, rows);

  if (!sectionIsVisible(rows, projectionRefusal, verdict)) return null;

  return (
    <section
      data-testid="archive-list"
      className="border border-black bg-tool px-3 py-2 space-y-1"
    >
      <p className="text-xs font-semibold text-gray-800">历史任务</p>
      {historyNotice ? (
        <p className="text-xs text-gray-500">{historyNotice}</p>
      ) : null}

      {/* One paragraph, whichever state the decision named. The copy comes
          from the decision itself, so a claim of adoption cannot be
          re-worded into existence here. */}
      {verdict ? <RestoreVerdict verdict={verdict} /> : null}

      {/* Stated apart from the verdict on purpose: a refused ENVELOPE is a
          broken channel, not a failed restore, and conflating them would
          tell the operator a recheck happened when nothing could be read. */}
      {projectionRefusal ? (
        <p data-testid="archive-projection-refusal" className="text-xs text-red-700">
          {TASK_RUNTIME_REFUSAL_COPY[projectionRefusal]}
        </p>
      ) : null}

      <ArchiveRows
        rows={rows}
        restorable={restorable}
        verdict={verdict}
        onRestore={onRestore}
      />
    </section>
  );
}

export interface ArchiveListProps {
  threadId: string;
  /** Thread-scoped dynamic state, supplied by the page. When a prop is
   * omitted the section falls back ONLY to this thread's own projection
   * (never to a global slot), so an unwired mount still lists the archives
   * and claims nothing about a restore. */
  rows?: ArchiveRow[];
  restorable?: Set<string>;
  decision?: RestoreDecision | null;
  onRestore?: (archiveId: string) => void;
}

/** The store-resolving wrapper. ThreadPage's TaskRegion mounts it
 * unconditionally, so the refusal notice rides this section — the task
 * region's only always-present surface. */
export default function ArchiveList({
  threadId,
  rows,
  restorable,
  decision,
  onRestore,
}: ArchiveListProps) {
  const archives = useWorkspaceStore((s) => s.taskRuntime[threadId]?.archived_tasks);
  const projectionRefusal = useWorkspaceStore(
    (s) => s.taskProjectionRefusal[threadId] ?? null,
  );
  const restoreArchivedTask = useWorkspaceStore((s) => s.restoreArchivedTask);

  const resolvedRows = rows ?? archiveRows(archives);
  // Only a non-blank id can be honoured, so only those rows get a button:
  // the host would refuse a blank id as task_restore_unknown, and a button
  // the surface cannot see through is worse than no button.
  const resolvedRestorable =
    restorable ?? new Set(restorableArchiveIds(archives));

  return ArchiveListView({
    threadId,
    rows: resolvedRows,
    restorable: resolvedRestorable,
    decision: decision ?? null,
    projectionRefusal,
    onRestore:
      onRestore ?? ((archiveId) => void restoreArchivedTask(threadId, archiveId)),
  });
}
