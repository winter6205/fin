// W4 read-side decoding of the ``task_runtime`` projection that
// ``GET /api/threads/{id}`` carries (spec
// specs/question-task-runtime-contract.md §Representation, wire SSOT
// ``fin.host.workspace_http.TaskRuntimeView`` /
// ``fin.host.task_runtime`` read surfaces).
//
// This is the read gate for reload recovery (W1/W2/W4/W5): the surface
// may render ONLY what this module has runtime-validated. Strict ``tsc``
// types are compile-time only — the GET body arrives as ``unknown``, so
// the same posture the evidence channel takes applies here: every
// decoder is a PURE function returning a TYPED outcome, and it never
// throws on data, never coerces, never silently drops, and never invents
// a default the wire did not carry.
//
// WHY an ABSENT key is not a refusal, while a PRESENT wrong-shaped one
// is: a host that predates the task channel simply has no envelope, and
// "no channel" is not "a broken channel" (contract §Inventory outcome:
// "No backfill. ``from_state_dict(None)`` reads as the empty envelope, so
// a legacy thread reads as taskless — unknown, never fabricated"). A key
// that IS present but does not have a contract shape is a different
// fact: the host meant to speak this channel and the bytes do not parse,
// so the surface must refuse rather than render a half-understood
// envelope as a real one.

import type {
  AcceptedInputRow,
  ArchivedTaskSummary,
  CurrentTask,
  PendingQuestionView,
  ReconciliationView,
  TaskOutcome,
  TaskRuntimeView,
} from "./types";

// ----- helpers (the house narrowing idiom, mirroring
// evidenceContract.ts / choiceWindow.ts) -----

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A version-counter test over the DECODED value (ADR 0093 decision 8):
 * ``Number.isInteger`` already excludes ``NaN`` / ``±Infinity`` / every
 * non-number type / the bool, so no separate ``typeof`` guard is needed
 * to narrow. */
function isInteger(value: unknown): value is number {
  return Number.isInteger(value);
}

/** Non-blank means "carries content", not "carries spaces": a
 * whitespace-only id or prompt is the absent thing in disguise. The
 * value is validated as-is and never trimmed — trimming would be a
 * coercion this seam forbids. */
function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/** A contract string field with a documented default: ``undefined``
 * means "present but wrong type" (the caller refuses) — kept distinct
 * from a legitimate ``null``/absent so the two never merge. */
function readString(value: unknown, fallback: string): string | undefined {
  if (value === undefined || value === null) return fallback;
  return typeof value === "string" ? value : undefined;
}

/** A contract integer field with a documented default (see each
 * narrower for WHY that default is the conservative reading). */
function readInteger(value: unknown, fallback: number): number | undefined {
  if (value === undefined || value === null) return fallback;
  return isInteger(value) ? value : undefined;
}

/** A contract ``string | null`` field: absent / ``null`` both read as
 * ``null``; ``undefined`` marks a wrong type the caller must refuse. */
function readOptionalString(value: unknown): string | null | undefined {
  if (value === undefined || value === null) return null;
  return typeof value === "string" ? value : undefined;
}

/** A contract ``string[]`` field: absent reads as the fallback, a
 * present non-array or a non-string element is off-contract and refuses
 * — the caller turns ``undefined`` into ``null``. */
function readStringArray(value: unknown, fallback: string[]): string[] | undefined {
  if (value === undefined || value === null) return fallback;
  if (!Array.isArray(value)) return undefined;
  for (const item of value) {
    if (typeof item !== "string") return undefined;
  }
  return value as string[];
}

// ----- the legacy-thread reading (contract §Inventory outcome) -----

/** The empty envelope the contract mandates a legacy thread read as.
 *
 * There is deliberately no backfill and no migration (contract
 * §Inventory outcome): a thread predating the channel has no
 * ``task_runtime`` key, and every field reads as its empty default
 * rather than raising. Taskless is a real, ordinary state — never a
 * fabricated task and never an error. */
export const EMPTY_TASK_RUNTIME: TaskRuntimeView = {
  current_task: null,
  archived_tasks: [],
  pending_question: null,
  reconciliation: null,
  task_outcome: null,
  input_version: 0,
  input_history: [],
};

// ----- closed refusal vocabulary -----

/** The two ways this seam can refuse a PRESENT envelope.
 *
 * ``malformed_task_runtime`` — a present key with a shape the contract
 * does not define.
 * ``unsupported_task_runtime`` — a recognised key whose content this
 * client cannot honour. This module has no version gate to emit it yet
 * (the frozen envelope carries no version token this client must check),
 * so it is currently reserved; it stays in the closed set because the
 * refusal vocabulary is one thing and a surface branches on it. */
export type TaskRuntimeDecodeRefusal =
  | "malformed_task_runtime"
  | "unsupported_task_runtime";

export interface TaskRuntimeDecode {
  view: TaskRuntimeView;
  /** ``null`` on a clean decode, AND on an ABSENT key: an older host
   * with no task channel is not a broken host (contract §Inventory
   * outcome). */
  refusal: TaskRuntimeDecodeRefusal | null;
}

// ----- element narrowers (each ``unknown -> T | null``, refusing
// rather than repairing) -----

/** ``fin.host.task_runtime_types.TASK_OUTCOMES``: the three frozen
 * tokens, and nothing else.
 *
 * WHY an unknown token must NEVER be widened to ``"active"``: the task
 * outcome is a decision, and presenting an unrecognised one as a live
 * task would assert the host committed a decision the bytes do not name.
 * Refusing (``null``) drops the field, which the aggregate records as a
 * refusal — an honest "unrecorded", never a fabricated "active". */
export function parseTaskOutcome(value: unknown): TaskOutcome | null {
  return value === "active" || value === "complete" || value === "cancelled"
    ? value
    : null;
}

/** The still-pending clarification (contract §Representation
 * ``pending_question``; ``task_runtime.pending_question_payload``).
 *
 * ``prompt`` must be non-blank: the spec requires the COMPLETE question,
 * so a prompt-less question is not a question and must not render as one
 * (a blank prompt would show an operator a clarification with nothing to
 * answer).
 *
 * ``choices`` must be a non-empty array of non-blank strings. The wire
 * list already ends with exactly one trailing ``"other"`` (the host
 * appends it), so this decoder neither adds one nor strips one — it
 * carries the list verbatim and never guesses which option was Other.
 *
 * Per-field defaults, each the CONSERVATIVE reading:
 * - ``task_version`` missing → ``1``: version 1 is the contract's
 *   initial version, so a missing counter claims NO material change
 *   happened — the reading that preserves applicable work.
 * - ``input_version`` missing → ``0``: 0 is the contract's "no accepted
 *   input yet", so a missing counter claims no input — it never invents
 *   an advance.
 * Both mirror ``task_runtime_types.from_state_dict`` (``… or 1`` /
 * ``… or 0``). A PRESENT non-integer counter is not defaulted: it is
 * off-contract and fails the whole question. */
/** ``choices`` must be a non-empty array of non-blank strings — a
 * choice-less or blank-entry list is not a question (the spec requires
 * the COMPLETE question with its options). Extracted from
 * ``parsePendingQuestion`` so the list contract is one named rule. */
function parseQuestionChoices(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  for (const choice of value) {
    if (!isNonBlankString(choice)) return null;
  }
  return value as string[];
}

export function parsePendingQuestion(value: unknown): PendingQuestionView | null {
  if (!isRecord(value)) return null;
  if (!isNonBlankString(value.question_id)) return null;
  if (!isNonBlankString(value.task_id)) return null;
  if (!isNonBlankString(value.prompt)) return null;
  const choices = parseQuestionChoices(value.choices);
  if (choices === null) return null;
  const task_version = readInteger(value.task_version, 1);
  if (task_version === undefined) return null;
  const input_version = readInteger(value.input_version, 0);
  if (input_version === undefined) return null;
  return {
    question_id: value.question_id,
    task_id: value.task_id,
    task_version,
    prompt: value.prompt,
    choices,
    input_version,
  };
}

/** The current task (contract §Representation ``current_task``;
 * ``task_runtime.current_task_summary``).
 *
 * ``task_id`` and ``goal`` must be non-blank — a task with no goal is
 * not a task. ``expected_result`` may be ``""``: a task with no stated
 * expected result is task-SHAPED, not malformed, so an empty string is
 * carried through rather than refused.
 *
 * ``outcome`` / ``task_outcome`` must parse or be absent. A missing task
 * ``outcome`` defaults to ``"active"`` (contract §Ownership: with no
 * assessment recorded the outcome boundary leaves the task ACTIVE —
 * silence is never a licence to complete). A PRESENT unknown token is
 * refused: it must not be widened to ``"active"`` (see
 * ``parseTaskOutcome``).
 *
 * The nested ``pending_question`` is the same value the envelope also
 * carries at top level; here it narrows best-effort — a malformed nested
 * copy is omitted rather than failing the task, because the top-level
 * field is the authoritative record and carries its own refusal. */
/** The task's scalar fields with their conservative defaults (docstring
 * of ``parseCurrentTask``): ``expected_result``/``outcome_reason`` may be
 * ``""``; a PRESENT off-contract value refuses via ``undefined``.
 * Extracted so the scalar contract is one family, separate from the
 * outcome/nested-object decisions. */
function readTaskScalars(value: Record<string, unknown>):
  | {
      expected_result: string;
      task_version: number;
      outcome_reason: string;
      created_at_input_version: number;
      necessary_conditions: string[];
    }
  | null {
  const expected_result = readString(value.expected_result, "");
  if (expected_result === undefined) return null;
  const task_version = readInteger(value.task_version, 1);
  if (task_version === undefined) return null;
  const outcome_reason = readString(value.outcome_reason, "");
  if (outcome_reason === undefined) return null;
  const created_at_input_version = readInteger(
    value.created_at_input_version,
    1,
  );
  if (created_at_input_version === undefined) return null;
  const necessary_conditions = readStringArray(value.necessary_conditions, []);
  if (necessary_conditions === undefined) return null;
  return {
    expected_result,
    task_version,
    outcome_reason,
    created_at_input_version,
    necessary_conditions,
  };
}

/** The task's outcome: a missing one defaults to ``"active"`` (contract
 * §Ownership: with no assessment recorded the outcome boundary leaves the
 * task ACTIVE — silence is never a licence to complete). A PRESENT
 * unknown token is refused (``null``), never widened to ``"active"``. */
function resolvedTaskOutcome(raw: unknown): TaskOutcome | null {
  if (raw === undefined || raw === null) return "active";
  return parseTaskOutcome(raw);
}

/** The envelope-level ``task_outcome`` (optional field): absent reads as
 * not-set; a PRESENT unknown token refuses the whole task. */
function optionalParsedOutcome(
  raw: unknown,
  task: CurrentTask,
): CurrentTask | null {
  if (raw === undefined || raw === null) return task;
  const parsed = parseTaskOutcome(raw);
  if (parsed === null) return null;
  task.task_outcome = parsed;
  return task;
}

/** The nested ``pending_question`` copy narrows best-effort: a malformed
 * nested copy is omitted rather than failing the task, because the
 * top-level field is the authoritative record and carries its own
 * refusal. */
function attachBestEffortQuestion(
  value: Record<string, unknown>,
  task: CurrentTask,
): void {
  if (value.pending_question === undefined || value.pending_question === null) {
    return;
  }
  const question = parsePendingQuestion(value.pending_question);
  if (question !== null) task.pending_question = question;
}

export function parseCurrentTask(value: unknown): CurrentTask | null {
  if (!isRecord(value)) return null;
  if (!isNonBlankString(value.task_id)) return null;
  if (!isNonBlankString(value.goal)) return null;

  const scalars = readTaskScalars(value);
  if (scalars === null) return null;

  const outcome = resolvedTaskOutcome(value.outcome);
  if (outcome === null) return null;

  const task: CurrentTask = {
    task_id: value.task_id,
    goal: value.goal,
    ...scalars,
    outcome,
  };
  attachBestEffortQuestion(value, task);
  return optionalParsedOutcome(value.task_outcome, task);
}

/** One locatable archive (contract §Representation ``archived_tasks``;
 * ``task_runtime.archived_task_summaries`` — already flattened to
 * ``task_id`` / ``goal`` / ``task_version`` on the wire).
 *
 * ``archive_id`` and ``task_id`` must be non-blank: an archive with no
 * identity cannot be restored, which is its whole purpose. ``run_id``
 * may be ``null`` and ``evidence_refs`` may be empty — an archived
 * result is kept locatable, and "no run ref" / "no evidence ref" are
 * real historical facts, not malformed rows. */
/** The archive row's scalar/optional fields (see ``parseArchivedTask``
 * docstring): identity already checked by the caller. Extracted so the
 * field contract is one family. */
function readArchiveScalars(value: Record<string, unknown>):
  | {
      goal: string;
      task_version: number;
      archived_at_input_version: number;
      run_id: string | null;
      evidence_refs: string[];
    }
  | null {
  const goal = readString(value.goal, "");
  if (goal === undefined) return null;
  const task_version = readInteger(value.task_version, 1);
  if (task_version === undefined) return null;
  const archived_at_input_version = readInteger(
    value.archived_at_input_version,
    0,
  );
  if (archived_at_input_version === undefined) return null;
  const run_id = readOptionalString(value.run_id);
  if (run_id === undefined) return null;
  const evidence_refs = readStringArray(value.evidence_refs, []);
  if (evidence_refs === undefined) return null;
  return {
    goal,
    task_version,
    archived_at_input_version,
    run_id,
    evidence_refs,
  };
}

export function parseArchivedTask(value: unknown): ArchivedTaskSummary | null {
  if (!isRecord(value)) return null;
  if (!isNonBlankString(value.archive_id)) return null;
  if (!isNonBlankString(value.task_id)) return null;
  const scalars = readArchiveScalars(value);
  if (scalars === null) return null;
  // The archived task's OWN outcome, retained because archiving is
  // separate from completing or cancelling (W5): an archive of a
  // still-active task reports ``active``. An ABSENT outcome stays
  // undefined rather than being defaulted, so the surface can say
  // 「未记录」 instead of asserting a task fact it was not given — and
  // an unrecognised token is dropped for the same reason.
  const outcome = parseTaskOutcome(value.outcome);
  const outcome_reason = readString(value.outcome_reason, "");
  if (outcome_reason === undefined) return null;
  return {
    archive_id: value.archive_id,
    task_id: value.task_id,
    ...scalars,
    ...(outcome !== null ? { outcome } : {}),
    ...(outcome_reason !== "" ? { outcome_reason } : {}),
  };
}

/** The committed decision about one accepted input (contract
 * §Representation ``reconciliation``; ``task_runtime.reconcile_marker``).
 *
 * ``reconciled_input_version`` must be a PRESENT integer and
 * ``disposition`` a non-blank string: the marker's identity is the input
 * version it reconciled, so a marker without one names no decision and
 * is refused rather than defaulted. ``task_id`` / ``task_version`` may
 * be ``null`` — a taskless reconciliation (a greeting, an off-topic
 * turn) has no task to name, and that absence is a real disposition. */
export function parseReconciliation(value: unknown): ReconciliationView | null {
  if (!isRecord(value)) return null;
  if (!isInteger(value.reconciled_input_version)) return null;
  if (!isNonBlankString(value.disposition)) return null;
  const task_id = readOptionalString(value.task_id);
  if (task_id === undefined) return null;
  let task_version: number | null;
  if (value.task_version === undefined || value.task_version === null) {
    task_version = null;
  } else if (isInteger(value.task_version)) {
    task_version = value.task_version;
  } else {
    return null;
  }
  const reason = readString(value.reason, "");
  if (reason === undefined) return null;
  return {
    reconciled_input_version: value.reconciled_input_version,
    task_id,
    task_version,
    disposition: value.disposition,
    reason,
  };
}

/** One entry of the bounded ORIGINAL-input window (contract
 * §Representation ``input_history``; ``task_runtime.bounded_input_history``).
 *
 * ``kind`` must be one of the three frozen tokens; an unknown kind is
 * REFUSED, never defaulted to ``"message"`` — defaulting would fabricate
 * which door the input came through (a restore is not a message). The
 * backend tolerates an unknown kind as ``message`` on the storage read,
 * but this is the DISPLAY boundary, where a fabricated provenance is a
 * visible lie rather than a tolerated default.
 *
 * ``payload`` may legitimately be ``null`` (contract §Inventory outcome:
 * an unrecoverable original reads as ``null`` and is NEVER
 * reconstructed). ``null`` payload is therefore VALID, not malformed —
 * this is the field most likely to be "helpfully" repaired, and doing so
 * would invent the very original the contract says to leave unknown. */
/** ``payload`` may legitimately be ``null`` (contract §Inventory outcome:
 * an unrecoverable original reads as ``null`` and is NEVER
 * reconstructed). ``null`` payload is therefore VALID, not malformed —
 * this is the field most likely to be "helpfully" repaired, and doing so
 * would invent the very original the contract says to leave unknown. */
function readRowPayload(raw: unknown): string | null | undefined {
  if (raw === undefined || raw === null) return null;
  return typeof raw === "string" ? raw : undefined;
}

/** The row's four ``string | null`` optional fields. Extracted from
 * ``parseAcceptedInput`` so the optional-field family is one named rule;
 * any off-contract value refuses the whole row (``undefined``). */
function readRowOptionalFields(value: Record<string, unknown>):
  | {
      question_id: string | null;
      archive_id: string | null;
      choice_id: string | null;
      run_id: string | null;
    }
  | null {
  const question_id = readOptionalString(value.question_id);
  if (question_id === undefined) return null;
  const archive_id = readOptionalString(value.archive_id);
  if (archive_id === undefined) return null;
  const choice_id = readOptionalString(value.choice_id);
  if (choice_id === undefined) return null;
  const run_id = readOptionalString(value.run_id);
  if (run_id === undefined) return null;
  return { question_id, archive_id, choice_id, run_id };
}

export function parseAcceptedInput(value: unknown): AcceptedInputRow | null {
  if (!isRecord(value)) return null;
  const kind = value.kind;
  if (kind !== "message" && kind !== "reply" && kind !== "restore") return null;
  const input_version = readInteger(value.input_version, 0);
  if (input_version === undefined) return null;
  const payload = readRowPayload(value.payload);
  if (payload === undefined) return null;
  const provenance = readString(value.provenance, "");
  if (provenance === undefined) return null;
  const optionalFields = readRowOptionalFields(value);
  if (optionalFields === null) return null;
  return {
    input_version,
    kind,
    payload,
    provenance,
    ...optionalFields,
  };
}

// ----- the aggregate -----

/** Narrow one OBJECT field: absent / ``null`` reads as ``null`` (a
 * legitimate "none"), a value that fails the narrower reads as ``null``
 * AND flags a refusal. The two are kept distinct so a surface can tell
 * "the host said none" from "the host sent something this client could
 * not honour". */
function narrowObjectField<T>(
  raw: unknown,
  parse: (value: unknown) => T | null,
  flagMalformed: () => void,
): T | null {
  if (raw === undefined || raw === null) return null;
  const parsed = parse(raw);
  if (parsed === null) flagMalformed();
  return parsed;
}

/** Narrow one ARRAY field: absent / ``null`` reads as ``[]``; each
 * entry that fails the narrower is DROPPED and flags a refusal.
 *
 * WHY drop the one bad row instead of failing the whole array: dropping
 * one bad row is honest — the good rows are the host's real data — while
 * serving a fabricated row would be, and refusing the whole list would
 * hide rows the host did commit. The recorded refusal is what keeps the
 * drop visible rather than silent. */
function narrowObjectRows<T>(
  raw: unknown,
  parse: (value: unknown) => T | null,
  flagMalformed: () => void,
): T[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) {
    flagMalformed();
    return [];
  }
  const rows: T[] = [];
  for (const entry of raw) {
    const row = parse(entry);
    if (row === null) {
      flagMalformed();
      continue;
    }
    rows.push(row);
  }
  return rows;
}

/** Decode the ``task_runtime`` envelope itself.
 *
 * ``undefined`` / ``null`` reads as ``EMPTY_TASK_RUNTIME`` with no
 * refusal — an ABSENT channel, not a broken one. A non-object value (an
 * array, a string, a number) is a present key the contract does not
 * define, so it refuses with an EMPTY view. Every field is validated
 * through the narrowers above; a field that fails reads as its absent
 * value (``null`` / ``[]`` / its documented default) with the refusal
 * recorded, so nothing half-built is ever presented as authoritative.
 *
 * Never throws on data and never returns a partly-defaulted object that
 * claims authority: a refusal is always accompanied by the unrecorded
 * reading, never by invented content. */
export function decodeTaskRuntime(value: unknown): TaskRuntimeDecode {
  if (value === undefined || value === null) {
    return { view: EMPTY_TASK_RUNTIME, refusal: null };
  }
  if (!isRecord(value)) {
    return { view: EMPTY_TASK_RUNTIME, refusal: "malformed_task_runtime" };
  }

  // Accumulate rather than assign a single variable: any malformed
  // element names the one refusal this seam can carry, and the array
  // makes "something was refused" explicit at the fold below.
  const refusals: TaskRuntimeDecodeRefusal[] = [];
  const flagMalformed = (): void => {
    refusals.push("malformed_task_runtime");
  };

  const current_task = narrowObjectField(value.current_task, parseCurrentTask, flagMalformed);
  const pending_question = narrowObjectField(
    value.pending_question,
    parsePendingQuestion,
    flagMalformed,
  );
  const reconciliation = narrowObjectField(
    value.reconciliation,
    parseReconciliation,
    flagMalformed,
  );
  const task_outcome = narrowObjectField(value.task_outcome, parseTaskOutcome, flagMalformed);
  const archived_tasks = narrowObjectRows(
    value.archived_tasks,
    parseArchivedTask,
    flagMalformed,
  );
  const input_history = narrowObjectRows(
    value.input_history,
    parseAcceptedInput,
    flagMalformed,
  );

  // A wrong-typed counter must not LEAK through as a number; it reads as
  // the contract's ``0`` default and refuses, so a caller never renders
  // ``"3"`` as the version 3.
  let input_version = 0;
  if (value.input_version !== undefined && value.input_version !== null) {
    if (isInteger(value.input_version)) {
      input_version = value.input_version;
    } else {
      flagMalformed();
    }
  }

  const refusal: TaskRuntimeDecodeRefusal | null =
    refusals.length > 0 ? "malformed_task_runtime" : null;

  return {
    view: {
      current_task,
      archived_tasks,
      pending_question,
      reconciliation,
      task_outcome,
      input_version,
      input_history,
    },
    refusal,
  };
}

/** A thin reader for the ``ThreadDetail`` envelope, so the caller need
 * not know the wire key name. A ``null`` / ``undefined`` body reads as
 * the empty envelope with no refusal, exactly like a body whose
 * ``task_runtime`` key is absent (the legacy-host case). */
export function decodeThreadTaskRuntime(
  body: { task_runtime?: unknown } | null | undefined,
): TaskRuntimeDecode {
  if (body === null || body === undefined) {
    return { view: EMPTY_TASK_RUNTIME, refusal: null };
  }
  return decodeTaskRuntime(body.task_runtime);
}

// ----- surface copy -----

/** Operator-facing copy for the two refusals.
 *
 * The copy states the task state is shown as UNRECORDED and that the
 * conversation is untouched — it never calls the state broken or lost.
 * An operational failure is a system outcome and is never converted into
 * a clarification (contract §Named failures: the shared red line), which
 * is why this copy carries no question and no question mark. */
export const TASK_RUNTIME_REFUSAL_COPY: Record<TaskRuntimeDecodeRefusal, string> = {
  malformed_task_runtime:
    "无法辨认这段对话的任务记录，本次按未记录显示；对话内容不受影响。",
  unsupported_task_runtime:
    "这段对话的任务记录来自本版本尚不支持的格式，本次按未记录显示；对话内容不受影响。",
};

/** Whether the envelope carries no CURRENT task to surface.
 *
 * Taskless is a legitimate, ordinary state — the surface renders it as
 * such rather than as an error. An archive is history, not a current
 * task, so archived rows do not make a thread non-taskless; a pending
 * question still binds to a task and does. */
export function isTaskless(view: TaskRuntimeView): boolean {
  return view.current_task === null && view.pending_question === null;
}
