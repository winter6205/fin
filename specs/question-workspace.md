# Spec: Question experience workspace

> **Adopted QA-chain amendment (2026-10-16):** The operator accepted the routing, retrieval, lifecycle and scoped persistence proposals. This canonical module contract incorporates the relevant behaviour below; the [shared wire contract](question-task-runtime-contract.md) freezes representation. [Routing/grounded](qa-chain-grounded-experience.md), [server/receipts](qa-chain-server-runtime.md), and [catalog/retrieval](qa-chain-corpus-retrieval.md) own their deltas. The journey acceptance contract remains the sole integrated acceptance gate. No implementation or acceptance pass is claimed.

> **Adopted clarification-continuation correction (2026-10-20):** A content clarification is a terminal question plus a new-message continuation; only the document-choice interrupt remains an interactive resumable pause. The [host contract](search-mode-host.md#clarification-contract) owns the trigger, candidate and identity rules. This is implemented on `fix/qa-design-correction-20261020`; the evidence is still being recorded in the [implementation report](../docs/verification/2026-10-20-qa-design-correction-implementation.md), and no passing-check claim is made here.

> **Status:** Decision-level contract for one of four independently developed modules. It records product behavior and ownership, not an implementation design.
> **Architecture review:** Passed decision-only review; [review record](question-task-runtime.md#architecture-change-review). No implementation approval is implied.

## Objective

Define what the operator can see and do while pursuing a knowledge-QA task in the existing research-thread surface. The workspace presents current task state, current source restrictions and capabilities, sources actually used in an answer, clarification, material gaps, and task history. It preserves the operator's original input and makes task state recoverable after reload.

Workspace owns browser presentation and user input only. Runtime owns input ordering, task lifecycle, authoritative status, and persistence. Source Scope owns source eligibility, restriction, and current authority. Grounded Answer owns semantic support and sufficiency. One host integrator maps these module decisions to shared execution, persistence, and live-update boundaries; Workspace does not become a second authority.

## Boundaries

- **Does:**
  - Present task, source, answer, clarification, gap, status, and archive information in the existing research-thread surface.
  - Collect ordinary input and complete clarification replies while preserving original wording and the reply's association with its question.
  - Show only the current host-evaluated source selection and truthful operation capabilities; distinguish those from sources actually supporting an answer.
  - Keep compatible input available during active work and reflect authoritative accepted, rejected, stale, or failed outcomes.
  - Restore a task only through an explicit user action and the Runtime-owned lifecycle.
- **Confirms with human:** No additional product behavior remains open in the approved issue decisions.
- **Out of this spec:**
  - Task/input representation, source policy, grounding rules, authorization enforcement, persistence, and task-transition decisions; the owning modules define these contracts.
  - Exact screen layout, visual hierarchy, copy, component boundaries, accessibility implementation, and detailed source-control design. This spec sets observable behavior, not page design.
  - Exact browser-to-host payloads, transport, store mapping, retry mechanism, endpoints, and live-update strategy. One host integrator owns this mapping.
  - A new route, settings/navigation surface, retrieval-technique picker, permission system, or separate integration-acceptance specification.

## Workspace behavior

### State shown to the operator

Within the existing thread surface, the operator can observe:

- The current task goal, latest task outcome, latest turn result, and material unresolved gaps as reported by Runtime.
- The user's current source restriction and the host's freshly evaluated effective source set and actual operation capabilities. A saved restriction is user intent, not proof of current authorization. If evaluation is unavailable, the workspace says so and offers no source as currently available.
- The sources actually attached to the current answer, separately from sources merely eligible to be searched. A candidate source or citation alone does not establish semantic support.
- A pending clarification with its complete question and all options, plus archived task summaries with explicit restore actions.

Keep the task experience within the current research-thread surface and preserve its established chrome. This spec does not choose placement, ordering, visual treatment, or copy. Source and capability displays follow current host authority; they do not expose impossible selections or imply that an inaccessible or stale source is available.

### Clarification and original input

Show the complete question before its options. The operator can choose an offered option or select Other; free text appears inside the selected Other option. When the host offers only the reserved Other — no real document option — the card presents the free-text field directly, with no Other button to click first, and submits the non-blank text with the reserved `other` choice. Preserve the exact text, selected option, and association with the original question and its originating basis. Do not preselect an answer.

A content clarification is the turn's terminal question: the host asks it after retrieval and performs no further work on that turn. The reply is a new input, not a resume. The workspace submits it through the normal send door with `question_id` and the selected choice or free text; Runtime admits it once and opens a new turn in the same thread. The answered turn stays terminal, and the unchanged task goal plus the thread transcript carry the original question as context. No interrupt, paused run, or resume participates, and the workspace never presents a content clarification as resumable. The pending question must carry the turn's real `task_id`: the host never publishes a task clarification with an empty one, and the workspace decoder keeps rejecting a task clarification whose `task_id` is empty. A send carrying an unknown `question_id` but no answer value is submitted as an ordinary follow-up message and is never silently swallowed; an answer attempt naming an unknown or superseded question, or a question that already has an accepted answer, still follows the stale-reply refusal.

The document-choice question is the one interactive exception. Its interrupt pauses the graph, and an accepted reply (a candidate pick or Other with free text) continues that same paused run and receipt through the existing resume path (`Command(resume=...)`, also exposed as `POST /resume`). Reload displays it from the committed pending question without a bodyless resume. Its deterministic candidate labels and four-choice bound remain the shared wire contract's; the terminal content-clarification rules above do not apply to it. The two mechanisms must not be presented or wired as interchangeable.

The browser sends the original clarification response with its question association to Runtime. Runtime validates whether the question still applies, assigns accepted-input identity, and determines the resulting task state. The browser does not invent or change task identity, current basis, or revision.

Dismissing or collapsing a clarification is presentation-only. It supplies no default answer, does not answer or cancel the task, and leaves the pending question recoverable from authoritative state. A rejected stale reply does not mutate, reactivate, or answer another task. Preserve the original question association and the user's draft; never silently rebase the old reply onto a new question.

If a transport retry is needed, resend the identical reply with its original question association. A valid clarification continues the same task unless Runtime explicitly reports a task change: a content clarification continues it in a new turn, while a document-choice reply continues it in the paused run. Runtime's acceptance or rejection controls what the workspace displays; local selection alone does not count as an accepted answer.

### Active work, outcomes, and history

While work is active, the operator can submit compatible progress, added context, task-change, or cancellation input through Runtime's serialized acceptance path. Keep input available and reflect Runtime's acceptance outcome. Do not start concurrent graph work for the same thread or automatically cancel current work whenever new input arrives. Runtime decides whether unaffected work continues or affected work is interrupted.

Keep task outcome distinct from turn result, stream/run status, and archive status. A run ending, a citation appearing, a lookup succeeding, or a response being delivered does not by itself mean the task is complete. Show active, complete, and cancelled task outcomes only when Runtime reports them; show partial answers, pending clarification, and system failure as turn results without relabeling the task.

On opening or reloading a thread, the authoritative projection provides the current task, pending clarification, current source evaluation, outcome, and archive summaries needed for display. Reopening a pending question does not require an empty or bodyless resume request. Display recovery is separate from resuming graph execution.

Archiving is separate from completing or cancelling a task. An archived task may retain its prior active outcome. Restore is explicit; Runtime rechecks current permission, versions, and evidence applicability before making it current. Until Runtime confirms restoration, keep showing the existing current task. Historical text and citations remain historical evidence and are not promoted to current support by restoration.

### Input ordering and module seams

Runtime orders and reconciles accepted user inputs. The workspace distinguishes pending submission, accepted input, and rejected or failed input based on authoritative host responses; it does not optimistically mark a clarification answered or task changed. If a reply is stale, the returned current state governs the display while the original reply remains associated with its original question.

The conceptual UI seam is an authoritative current projection for display and a user-intent submission that preserves original text and question association. Source Scope supplies effective scope and capabilities; Grounded Answer supplies semantic assessment; Runtime supplies lifecycle truth. The host integrator owns shared mapping and persistence.

The [shared wire contract](question-task-runtime-contract.md) now freezes this seam for independent implementation; this module does not duplicate its field definitions.

### Adopted QA-chain presentation and replies

Document-choice clarification displays at most four admitted title/section labels and supported summaries, followed by the existing Other action labelled “换个问法”. Reload reads the complete question from Runtime's single pending question; it does not resume execution. A click is submitted to normal reply admission with the original question/choice IDs. Other opens its input first; actual submission preserves exact free text. Runtime's successful reply acceptance advances input history once and continues the same run/receipt; duplicate or stale clicks cannot launch another execution. The [shared wire contract](question-task-runtime-contract.md#document-clarification-and-accepted-input-scheduling) owns fields and bounds. This subsection describes the document-choice exception only; the terminal content clarification above keeps its new-turn continuation and is never rendered or wired as this resumable pick.

Show the accepted corpus/attachment restriction as saved intent and the fresh Source Scope evaluation separately. New questions and mode-only changes do not clear it. Only an explicit user scope change accepted by Runtime replaces/clears it. Attachments state “仅在本对话中可用”; inaccessible sources are never presented as usable merely because a saved selection names them.

All answer views use the same admitted readable projection, inline `[n]` markers and trailing clickable sources. The default surface omits internal tool/engine/slot identifiers and receipt panels while existing developer diagnostics remain available. Progress remains responsive without exposing unverified answer text. Use distinct index/busy/insufficient-evidence copy from the [grounded contract](qa-chain-grounded-experience.md), and generic Chinese copy rather than financial-slot prerequisites.

## Success Criteria

These are required observations, not implementation steps or a separate acceptance suite. Module-local workspace contract verification is distinct from the journey acceptance contract's final four-module H/M/B acceptance gate.

| Criterion | Pass condition | Acceptance mapping |
| --- | --- | --- |
| SC-1: Current task visibility | The thread surface shows Runtime-reported task outcome, turn result, and material gaps separately from run status. Citations and run completion never cause the workspace to infer task completion. | J01–J21 browser journeys, as applicable. |
| SC-2: Truthful source visibility | Current restrictions and capabilities reflect fresh host authority; a saved restriction is not displayed as a current grant. Actual answer sources are separate from eligible sources. No technique picker or impossible source menu appears. | J03–J05, J08–J12, J15, J20, and J22 source-related checks. |
| SC-3: Complete clarification | The full question and all options are available; a content clarification is terminal and its reply opens a new turn in the same thread, while a document-choice reply continues its paused run; Other text is inside the selected Other option, or, when the host offers only the reserved Other, the free-text field is presented directly with no Other button; exact original text and question association reach Runtime. Dismissal supplies no answer and leaves the question recoverable. | J06–J07 and mapped clarification journeys. |
| SC-4: Stale reply safety | A stale reply is rejected without mutating or reactivating a task, applying to another question, or losing its original association and user draft. | Applicable stale-input and lifecycle journeys. |
| SC-5: Input during active work | Compatible follow-up input can be accepted while work is visibly active. It does not trigger concurrent graph execution or automatic cancellation of unaffected work. | J14/J16 browser journeys. |
| SC-6: Recovery and restore | Reload displays persisted current task and pending question without a bodyless-resume workaround. Archive and completion remain distinct; explicit restore waits for Runtime revalidation and preserves historical evidence. | Applicable browser journeys through real persistence. |
| SC-7: Required browser coverage | Mapped browser journeys J01–J21 complete through the integrated application. J22 browser inspection is covered through J08/J21 as the manifest specifies. | The journey acceptance contract's B assignments and evidence rules; no separate acceptance spec. |

The final integrated gate retains the journey acceptance contract's full H/M/B assignments and repetitions across all four modules. Module-local UI verification may establish the presentation/input seam, but cannot be reported as integrated acceptance. No acceptance run is claimed here.

## Open Questions

No product or shared-wire decision remains open for the adopted scope. The shared contract selects reply admission, origin association, retry semantics and source/question projection; the QA-chain grounded contract selects the required copy and admitted presentation. Detailed layout, accessibility implementation and fixture/runner choices remain implementation work. A change to the approved behaviour or state authority requires a new operator decision.

## Inherits / Changes

### Confirmed inherited assumptions

1. The product remains a durable research thread in the current local workspace; the task experience does not become a new route or one-shot surface. **Confirmed by** [ADR 0025](../docs/adr/0025-ordinary-person-workspace.md), [ADR 0026](../docs/adr/0026-workspace-streaming-resume.md), [docs/context-map.md](../docs/context-map.md), and the user's four-module scope.
2. Users do not preselect documents or retrieval techniques for an ordinary question. Current effective scope comes from Source Scope and existing permission. **Confirmed by** [question-source-scope.md](question-source-scope.md).
3. Runtime owns input ordering, task lifecycle, persistence, pending-question truth, and stale-reply decisions; Workspace presents and submits operator intent. **Confirmed by** [question-task-runtime.md](question-task-runtime.md).
4. Source Scope owns permission-constrained selection; Grounded Answer judges semantic support and completion. Workspace presents both owners' current results without substituting its own decisions. **Confirmed by** [question-source-scope.md](question-source-scope.md), and [question-grounded-answer.md](question-grounded-answer.md).
5. The journey acceptance contract v1.0 is the sole integrated acceptance contract; this spec defines no fifth acceptance document. **Confirmed by** the journey acceptance contract and the user's direction.

The exact relevant glossary entry in [`docs/CONTEXT.md`](../docs/CONTEXT.md) is:

> **工作台**: The local web UI: **research thread**s live in a 会话 list (create, list, open, resume, delete); **thinking fold**s and **tool fold**s stream then collapse; citations sit on the assistant turn; **知识 MCP** health is one left-corner line.

The context map places the browser workspace inside the host-agent context and assigns it the research-thread surface and host composition role; see [`docs/context-map.md`](../docs/context-map.md). ADR 0025 keeps the workspace sparse and transcript-centered; ADR 0026 keeps streaming and crash recovery on the existing thread execution; ADR 0062's retrieval-mode clarification must not become a question-experience technique picker.

**Changes:** Adds a task-aware presentation/input decision in the existing thread surface; requires faithful source and task state, original clarification association, safe active-run input, persisted-question recovery, and explicit archive restore. It chooses no layout, copy, payload schema, transport, persistence design, or local test runner.

**Persist list:** empty. No glossary term or ADR decision is added by this spec.

## Architecture Change Review

Independent decision-only review passed on 2026-10-09; the five verdicts are recorded in
[Question and task runtime](question-task-runtime.md#architecture-change-review). This spec does not prescribe implementation planning.
