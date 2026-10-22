"""Safe preflight and lifecycle contract for the real-browser demo launcher."""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DB_CHECK = REPO_ROOT / "scripts" / "demo_acceptance_db_check.py"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "demo_acceptance_release_instance.py"
LAUNCHER = REPO_ROOT / "scripts" / "demo-browser-acceptance.sh"
EXPECTED_QUESTIONS = REPO_ROOT / "fixtures" / "demo-corpus" / "expected.json"
BROWSER_RUNNER = REPO_ROOT / "scripts" / "demo-browser-acceptance.mjs"
MODE_WATCH = REPO_ROOT / "scripts" / "demo-mode-watch.mjs"

# The harness runs one check per plan entry: the question groups contribute
# 17 rows, the stateful worker 6, the two run-wide aggregates 2, and the reset
# 1. The corrected suite adds the routing/navigation checks on top of the
# original 17, and the restored q5 revenue guard is its own row.
EXPECTED_ACCEPTANCE_CHECK_COUNT = 26


def _function_source(runner: str, declaration: str) -> str:
    """The source of the top-level harness function that starts with ``declaration``."""
    assert declaration in runner, f"the harness no longer defines {declaration}"
    tail = runner.split(declaration, maxsplit=1)[1]
    return tail.split("\nasync function ", maxsplit=1)[0]


def _function_source_until(source: str, declaration: str, terminator: str) -> str:
    """Source from ``declaration`` up to the next explicit declaration.

    ``_function_source`` cuts at the next ``async function``; the plain
    ``function`` declarations (the request watch, the page-side scanner) need
    an explicit end marker so their block does not swallow a neighbour.
    """
    assert declaration in source, f"the source no longer defines {declaration}"
    return source.split(declaration, maxsplit=1)[1].split(terminator, maxsplit=1)[0]


def _serialized_helper_names(runner: str) -> list[str]:
    """The identifiers listed in the ``SERIALIZED_HELPERS`` block."""
    block = runner.split("const SERIALIZED_HELPERS = [", maxsplit=1)[1].split("\n];", maxsplit=1)[0]
    names = re.findall(r"^\s*([A-Za-z_$][\w$]*),\s*$", block, re.MULTILINE)
    assert names, "the harness no longer lists serialized helpers"
    return names


def _declared_runner_functions(runner: str) -> dict[str, str]:
    """Map each top-level runner function to its declaration prefix.

    ``_function_source`` splits on the exact declaration text, so an async
    helper needs the ``async`` prefix to resolve to its own declaration.
    """
    return {
        name: f"{kind}function"
        for kind, name in re.findall(
            r"^(async )?function ([A-Za-z_$][\w$]*)\(", runner, re.MULTILINE
        )
    }


NODE = shutil.which("node")


def _run_node(script: str, *, env_extra: dict[str, str] | None = None) -> dict:
    """Run an ESM meta script against the real harness module and parse JSON."""
    assert NODE is not None, "node is unavailable"
    env = {
        **os.environ,
        "HARNESS_PATH": str(BROWSER_RUNNER),
        **(env_extra or {}),
    }
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


PLAN_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const harness = await import(pathToFileURL(process.env.HARNESS_PATH).href);
const names = (checks) => checks.map((check) => check.name);
const groups = harness.acceptanceGroups().map((group) => ({
  id: group.id,
  stateful: Boolean(group.stateful),
  publishesSample: Boolean(group.publishesSample),
  checks: names(group.checks),
}));
const serial = names(harness.serialSequence());
console.log(JSON.stringify({
  expected: harness.EXPECTED_CHECK_COUNT,
  maxParallel: harness.MAX_PARALLEL_WORKERS,
  groups,
  aggregate: names(harness.acceptanceAggregateChecks()),
  reset: names(harness.acceptanceResetChecks()),
  serial,
}));
"""


@lru_cache(maxsize=1)
def _acceptance_plan() -> dict:
    """The harness's own plan, so grouping and count assertions cannot drift."""
    return _run_node(PLAN_SCRIPT)


def _assert_harness_runs_check(runner: str, check_name: str) -> None:
    """The named check must be part of the sequence the harness actually executes."""
    assert "for (const check of serialSequence())" in runner, (
        "the harness no longer iterates its serial plan"
    )
    assert "await check(ctx)" in runner, "the harness no longer awaits each sequenced check"
    assert check_name in _acceptance_plan()["serial"], f"the harness no longer runs {check_name}"


def _assert_pinned_check_count(runner: str) -> None:
    """The plan and the merged evidence must agree on exactly 26 checks."""
    plan = _acceptance_plan()
    group_checks = sum(len(group["checks"]) for group in plan["groups"])
    assert group_checks + len(plan["aggregate"]) + len(plan["reset"]) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert len(plan["serial"]) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert plan["expected"] == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert f"EXPECTED_CHECK_COUNT = {EXPECTED_ACCEPTANCE_CHECK_COUNT}" in runner
    assert "checks.length !== EXPECTED_CHECK_COUNT" in runner


def test_browser_acceptance_covers_positive_sql_rows_and_keeps_boundary_refusal() -> None:
    questions = json.loads(EXPECTED_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    by_id = {question["id"]: question for question in questions}

    assert by_id["q14"]["expected_kind"] == "refusal"
    assert by_id["q14"]["search_mode"] == "sql"
    assert by_id["q15"]["expected_kind"] == "answerable"
    assert by_id["q15"]["search_mode"] == "sql"
    assert "2026-01-01" in by_id["q15"]["question"]
    assert "2026-04-30" in by_id["q15"]["question"]
    assert len(by_id["q15"]["rows"]) == 4
    assert all({"contract_id", "counterparty", "expiry_date", "notice_by"} == set(row) for row in by_id["q15"]["rows"])

    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    assert "async function checkQ15(ctx)" in runner
    _assert_harness_runs_check(runner, "checkQ15")
    _assert_pinned_check_count(runner)


def test_browser_acceptance_checks_q2_and_q5_with_default_receipts_hidden() -> None:
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    grounded_check = _function_source(runner, "async function answerGroundedQuestion")
    revenue_check = _function_source(runner, "async function checkQ5AmbiguousRevenue")
    clarification_check = _function_source(runner, "async function checkAmbiguousClarification")
    receipt_helper = _function_source(runner, "async function assertReceiptProvenance")

    assert "assertDefaultReceiptHidden(ctx" in grounded_check
    assert "ctx.byId.q2" in runner
    # The q5 row restores the guard the retargeted clarify check dropped: an
    # underspecified revenue ask must never publish ZEUS's quarterly figure,
    # and the clarified turn answers from the named quarterly summary.
    assert "checkQ5AmbiguousRevenue" in _acceptance_plan()["serial"]
    assert "What was revenue?" in revenue_check
    assert "!turnText.includes(\"412,300,000\")" in revenue_check
    assert "ZEUS Quarterly Financial Summary" in revenue_check
    # q5 owns the handoff's ONE click reply: it answers by clicking an
    # offered title.
    assert (
        'getByRole("button", { name: "ZEUS Quarterly Financial Summary", exact: true })'
        in revenue_check
    )
    assert "byId.q5.locator" in revenue_check
    assert "assertDefaultReceiptHidden(ctx, byId.q5.id)" in revenue_check
    assert clarification_check.index("removeItem(") < clarification_check.index(
        "fillAndSend(ctx, byId.q1.question)"
    )
    # The clarification is decided by real retrieval hits and offers the
    # catalog's own document titles, never the internal company/period slot.
    assert 'getByTestId("clarification-card")' in clarification_check
    assert "ZEUS Employee Handbook" in clarification_check
    assert "leaked internal slot names" in clarification_check
    # The handoff's ONE free-text reply: this row answers through Other's
    # text field, which only exists once Other is chosen, and never by
    # clicking the offered title.
    assert 'getByTestId("clarification-choice-other")' in clarification_check
    assert 'getByTestId("clarification-other-text")' in clarification_check
    assert "只看 ZEUS Employee Handbook" in clarification_check
    assert 'getByRole("button", { name: "ZEUS Employee Handbook"' not in clarification_check
    assert clarification_check.index(
        'getByTestId("clarification-choice-other")'
    ) < clarification_check.index('getByTestId("clarification-other-text")')
    assert clarification_check.index(
        'getByTestId("clarification-other-text")'
    ) < clarification_check.index('"clarification-submit"')
    assert "free-text clarification answer" in clarification_check
    # The reply continues in the SAME thread and carries a real answer
    # composed from the typed text.
    assert "clarification answer left the original thread" in clarification_check
    assert "annual-leave fact" in clarification_check
    assert "typed clarification answer" in clarification_check
    assert clarification_check.index("assertDefaultReceiptHidden(ctx") < clarification_check.index(
        'setItem("fin.developer-mode", "1")'
    )
    # The receipt is read only after the developer switch is on, and the shared
    # helper it now reads through must still wait for the receipt to appear.
    assert clarification_check.index('setItem("fin.developer-mode", "1")') < clarification_check.index(
        'assertReceiptProvenance(ctx, "clarify reply")'
    )
    # The reply is a NEW turn: the check waits for the row to appear, then
    # reads its answer and the original-question context.
    assert "clarification-submit" in clarification_check
    assert clarification_check.index('"clarification-submit"') < clarification_check.index(
        "terminalArticle(ctx)"
    )
    assert "did not carry the original question as context" in clarification_check
    assert "receipt.waitFor" in receipt_helper


def test_browser_acceptance_checks_expected_answers_and_sources_for_q7_through_q12() -> None:
    questions = json.loads(EXPECTED_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    by_id = {question["id"]: question for question in questions}
    assert all(by_id[f"q{index}"]["expected_kind"] == "answerable" for index in range(7, 13))
    assert len(by_id["q11"]["additional_sources"]) == 1
    q12_sources = [by_id["q12"], *by_id["q12"]["additional_sources"]]
    assert {source["source_ref"] for source in q12_sources} == {
        "zeus-platform-weekly-report",
        "zeus-aegis-weekly-report",
    }

    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    assert "async function checkExpectedDocumentQuestion(ctx, entry)" in runner
    plan = _acceptance_plan()
    documents = {
        group["id"]: group["checks"]
        for group in plan["groups"]
        if group["id"].startswith("documents-")
    }
    assert documents == {
        "documents-a": ["check-q7", "check-q8", "check-q9"],
        "documents-b": ["check-q10", "check-q11", "check-q12"],
    }
    assert "entry.additional_sources" in runner
    _assert_pinned_check_count(runner)


def test_browser_acceptance_measures_answer_visibility_against_the_send_response() -> None:
    """The live-answer claim is measured against the POST response, not the DOM.

    The answer card and the terminal status line share a React commit in most
    runs. The watcher must therefore record the first visible answer (even when
    terminal), and the runner must compare that timestamp with the closing of
    the send's POST response. The entry loader is a defect when it paints over
    an answer or replaces one on the same thread route.
    """
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    watcher = MODE_WATCH.read_text(encoding="utf-8")
    launcher = LAUNCHER.read_text(encoding="utf-8")
    # A bare Playwright command name is spawned through the WSL PATH and can
    # fail with EINVAL on the mounted pnpm shim; the launcher must export the
    # resolved absolute path the runner spawns.
    assert 'PLAYWRIGHT_CLI="$(type -P "$browser_cli"' in launcher
    assert "export PLAYWRIGHT_CLI" in launcher

    request_watch = _function_source_until(
        runner, "function installRequestWatch", "\nfunction isMessagePost"
    )
    assert 'page.on("response"' in request_watch
    assert 'page.on("requestfailed"' in request_watch
    assert "request.failure()" in request_watch
    assert "response.finished().then(" in request_watch
    assert "responseFinishedAt" in request_watch
    assert "responseFinishFailedAt" in request_watch
    assert "new Map()" in request_watch
    assert "byRequest.set(request, record)" in request_watch
    assert "byRequest.get(response.request())" in request_watch
    # The response updates the SAME record; a second record would double-count
    # every message POST.
    response_handler = request_watch.split('page.on("response"', maxsplit=1)[1]
    assert "requests.push" not in response_handler

    snapshot = _function_source(runner, "async function liveAnswerSnapshot")
    assert "events.slice(eventsBefore)" in snapshot
    assert "messagePosts(ctx).slice(postsBefore)" in snapshot

    live_wait = _function_source(runner, "async function waitForLiveAnswer")
    assert "eventsBefore" in live_wait
    assert "postsBefore" in live_wait
    assert "liveAnswerFailure" in live_wait
    assert "liveAnswerResult" in live_wait
    assert "liveAnswerUnobserved" in live_wait
    # A finished POST without an answer gets a short grace, not the whole
    # question budget.
    assert "5_000" in live_wait
    assert "the send finished and no answer followed" in live_wait

    live_failure = _function_source(runner, "function liveAnswerFailure")
    assert "responseStatus < 200 || post.responseStatus >= 300" in live_failure
    assert "responseFinishFailedAt" in live_failure
    assert "responseStatus" in live_failure
    assert "answerVisibleAt" in live_failure

    live_result = _function_source(runner, "function liveAnswerResult")
    assert "visibleBeforeResponse: deltaMs > 0" in live_result
    assert "answerVisibleAt" in live_result
    assert "responseFinishedAt" in live_result

    live_unobserved = _function_source(runner, "function liveAnswerUnobserved")
    assert "visibleBeforeResponse: false" in live_unobserved
    assert "answerVisibleAt" in live_unobserved
    assert "responseFinishedAt" in live_unobserved
    assert "deltaMs: null" in live_unobserved

    ask = _function_source(runner, "async function askSampleQuestion")
    assert "postsBefore = messagePostCount(ctx)" in ask
    assert "eventsBefore = (await readWatchEvents(ctx)).length" in ask
    assert "waitForLiveAnswer(ctx, ctx.cfg.questionTimeoutMs, eventsBefore, postsBefore)" in ask
    assert "live.visibleBeforeResponse" in ask
    assert "was not visible while the send was still in flight" in ask
    assert "loading_over_answer" in ask
    assert "loadingOverAnswer.length === 0" in ask

    # The return-home scenario must navigate while the POST is still in
    # flight. The answer/terminal DOM window is only tens of milliseconds
    # wide, so the stable in-flight signal is the POST whose response has
    # not finished; after the navigation it must still finish and answer.
    return_home = _function_source(runner, "async function checkReturnHomeWhileStreaming")
    assert "postsBefore = messagePostCount(ctx)" in return_home
    assert "waitForSendInFlight(ctx, postsBefore" in return_home
    assert "waitForSendResponse(ctx, sendPost" in return_home
    assert "sendInFlightAtNavigation: true" in return_home
    assert "terminal.answered" in return_home
    assert "returning home cancelled the send response" in return_home
    assert "waitForLiveAnswer" not in return_home
    # The navigation must happen after the in-flight POST is observed and
    # before its response is awaited; otherwise the scenario no longer
    # returns home while the send is in flight.
    assert return_home.index("waitForSendInFlight(ctx, postsBefore") < return_home.index("link.click()")
    assert return_home.index("link.click()") < return_home.index("waitForSendResponse(ctx, sendPost")
    # This scenario leaves before any answer exists, so a loader-over-answer
    # assertion here would be vacuous. Q1's askSampleQuestion owns it.
    assert "loading_over_answer" not in return_home

    helpers = runner.split("const SERIALIZED_HELPERS = [", maxsplit=1)[1]
    assert "liveAnswerSnapshot" in helpers
    assert "waitForSendInFlight" in helpers
    assert "waitForSendResponse" in helpers

    # The old whole-entry loader assertion is gone; the narrower one remains,
    # and the diagnostic count is still reported.
    assert "overlays.length === 0" not in runner
    assert "entering the example's thread painted the entry loading overlay" not in runner
    assert "entry_loading_overlay" in runner
    assert "entryLoadingOverlay" in runner

    disclosure = _function_source(runner, "function disclosureSummary")
    for kind in ("answer_visible", "live_answer", "loading_over_answer", "entry_loading_overlay"):
        assert f'count("{kind}")' in disclosure
    # The serial check and the parallel merge must share the same claim; a
    # second, weaker copy in the orchestrator would let one path drift.
    serial_disclosure = _function_source(runner, "async function checkNoModeDisclosureThroughout")
    assert "disclosureSummary(await readWatchEvents(ctx))" in serial_disclosure
    merge = _function_source(runner, "async function mergeWorkerReports")
    assert "disclosureSummary(watchEvents)" in merge
    assert "requestTiersSummary(requestSummaries)" in merge

    scan = _function_source_until(
        watcher, "function scanLiveAnswer", "\n    function scanChrome"
    )
    assert 'kind: "answer_visible"' in scan
    assert "terminal: isTerminal(article)" in scan
    # The terminal guard must not sit on the answer_visible path: the event is
    # recorded before any isTerminal call, and only the live_answer diagnostic
    # stays behind a non-terminal check.
    assert scan.index('kind: "answer_visible"') < scan.index("isTerminal(article)")
    assert "seenAnswers.has(article) && !isTerminal" not in scan
    assert "!isTerminal(article)" in scan
    # Per-article dedupe: a run-wide flag would leave every later caller with
    # no fresh event after its cursor.
    assert "state.seenAnswers.has(article)" in scan
    assert "state.seenAnswers.add(article)" in scan
    assert "state.seenLiveAnswers.has(article)" in scan
    assert 'kind: "live_answer"' in scan
    assert 'kind: "loading_over_answer"' in scan
    assert "replacedAnswer" in scan
    # The loading branch is a full-page early return, so an overlay-only test
    # cannot see the answer it replaced. The watcher must remember that an
    # answer was seen on this route and flag the loader on the same path.
    assert "state.answerEverSeen" in scan
    assert "state.answerPath = window.location.pathname" in scan
    assert "sameAnswerRoute" in scan
    assert "anyAnswerOnScreen() || sameAnswerRoute" in scan
    assert 'kind: "entry_loading_overlay"' in scan

    scan_body = _function_source_until(watcher, "function scan()", "\n    const observer")
    assert "scanLoading();" in scan_body


def test_watcher_reads_thinking_prose_without_mutating_the_live_document() -> None:
    """The page watcher measures the page; it never mutates the live DOM.

    ``scanChrome`` used to read the thinking panel and then remove those live
    React-managed nodes. React still owned them, so its next commit threw
    ``NotFoundError: Failed to execute 'removeChild' on 'Node'`` and unmounted
    the SPA: a demo question ended on a blank page with no answer card. The
    thinking text is read from the live panel, and only the CLONE used for the
    page-chrome scan has the panel pruned out of it.
    """
    watcher = MODE_WATCH.read_text(encoding="utf-8")
    install = _function_source(watcher, "export async function installModeWatch")
    scan_chrome = _function_source_until(install, "function scanChrome", "\n    function scan()")

    # The reasoning is still read from the live panel and classified as model
    # prose, and the page-chrome scan still reads the clone.
    assert "document.querySelectorAll('[data-testid=\"trace-thinking-full\"]')" in scan_chrome
    assert "proseTokensIn(element.textContent" in scan_chrome
    assert 'pushTokenEvent("model-thinking"' in scan_chrome
    assert 'pushTokenEvent("page-chrome", tokensIn(clone.textContent' in scan_chrome

    # The thinking panel is excluded from the clone only.
    clone_prune = re.compile(
        r"clone\s*\.\s*querySelectorAll\(\s*'\[data-testid=\"trace-thinking-full\"\]'\s*\)"
        r"\s*\.\s*forEach\(\s*\(\s*element\s*\)\s*=>\s*element\.remove\(\)\s*\)"
    )
    assert clone_prune.search(scan_chrome), (
        "the page-chrome clone no longer excludes the thinking panel"
    )

    # The exact regression was ``for (const element of thinking) element.remove()``.
    assert "for (const element of thinking) element.remove()" not in scan_chrome
    # No other removal may touch a live node: every prune starts at the clone.
    removals = [line.strip() for line in install.splitlines() if ".remove(" in line]
    assert removals, "the scanner no longer prunes any panel from its clone"
    for statement in removals:
        assert statement.startswith("clone.querySelectorAll("), (
            "the watcher removes a live node; only the clone may be pruned: " + statement
        )


def test_parallel_plan_groups_question_checks_and_orders_the_serial_fallback() -> None:
    """The plan is the single source of grouping for both modes."""
    plan = _acceptance_plan()
    groups = plan["groups"]
    by_id = {group["id"]: group for group in groups}

    assert plan["maxParallel"] == 6
    assert [group["id"] for group in groups] == [
        "sample",
        "documents-a",
        "documents-b",
        "grounded",
        "follow-ups",
        "stateful",
    ]
    assert sum(1 for group in groups if group["stateful"]) == 1
    assert by_id["sample"]["publishesSample"] is True
    assert by_id["sample"]["checks"] == ["checkDemoHome", "checkQ1"]
    assert by_id["grounded"]["checks"] == [
        "checkQ2",
        "checkQ4",
        "checkQ6",
        "checkQ14",
        "checkQ15",
    ]
    assert by_id["follow-ups"]["checks"] == [
        "checkQ13",
        "checkQ5AmbiguousRevenue",
        "checkAmbiguousClarification",
        "checkLexicalReceipt",
    ]
    # The default-view checks must precede the developer-mode checks in the
    # serial fallback; q13/q14/q15 turn the developer switch on.
    serial_names = plan["serial"]
    assert serial_names.index("checkQ6") < serial_names.index("checkQ13")
    assert serial_names.index("checkQ15") < serial_names.index("checkLexicalReceipt")
    # The stateful navigation/cold-start checks are one contiguous worker
    # group: exactly one session runs them, in this order.
    stateful = by_id["stateful"]
    assert stateful["checks"] == [
        "checkReturnHomeWhileStreaming",
        "checkSidebarHomeLink",
        "checkBrowserBackStaysHome",
        "checkDemoColdHomeDoesNotRestore",
        "checkNonDemoColdHomeRestores",
        "checkColdThreadDeepLink",
    ]
    assert plan["aggregate"] == [
        "checkNoModeDisclosureThroughout",
        "checkRequestBodiesCarryNoTiers",
    ]

    serial = plan["serial"]
    assert len(serial) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert len(set(serial)) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    group_check_count = sum(len(group["checks"]) for group in groups)
    assert serial[:group_check_count] == [
        name for group in groups for name in group["checks"]
    ]
    # Aggregates sit after every worker row and before the reset.
    assert serial[group_check_count:group_check_count + 2] == plan["aggregate"]
    assert serial[-1] == "checkDemoReset"
    assert serial[-3:] == [*plan["aggregate"], "checkDemoReset"]
    stateful_start = serial.index("checkReturnHomeWhileStreaming")
    assert serial[stateful_start:stateful_start + 6] == stateful["checks"]


CLI_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const harness = await import(pathToFileURL(process.env.HARNESS_PATH).href);
const parse = (args) => {
  try {
    const options = harness.parseArgs(args);
    return { ok: true, parallel: options.parallel, serial: options.serial };
  } catch (error) {
    return { ok: false, error: error.message };
  }
};
console.log(JSON.stringify({
  defaultRun: parse([]),
  parallelMax: parse(["--parallel=6"]),
  parallelZero: parse(["--parallel=0"]),
  parallelTooMany: parse(["--parallel=7"]),
  serial: parse(["--serial"]),
  conflict: parse(["--serial", "--parallel=2"]),
}));
"""


def test_parallel_cli_options_are_bounded_and_serial_is_the_direct_default() -> None:
    outcomes = _run_node(CLI_SCRIPT)

    assert outcomes["defaultRun"] == {"ok": True, "parallel": 0, "serial": False}
    assert outcomes["parallelMax"] == {"ok": True, "parallel": 6, "serial": False}
    assert outcomes["parallelZero"]["ok"] is False
    assert "positive integer" in outcomes["parallelZero"]["error"]
    assert outcomes["parallelTooMany"]["ok"] is False
    assert "between 1 and 6" in outcomes["parallelTooMany"]["error"]
    assert outcomes["serial"] == {"ok": True, "parallel": 0, "serial": True}
    assert outcomes["conflict"]["ok"] is False
    assert "mutually exclusive" in outcomes["conflict"]["error"]


def test_parallel_stateful_worker_starts_after_the_sample_and_runs_serially() -> None:
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    parallel = _function_source(runner, "async function runParallelBrowser")
    worker = _function_source(runner, "async function runDemoWorker")

    # The stateful worker is not in the independent fan-out; it is scheduled
    # with the sample thread the sample worker published.
    assert "const independent = groups.filter((group) => !group.stateful)" in parallel
    assert "const sampleReport = await running[sampleIndex]" in parallel
    assert "published: sampleReport.published" in parallel
    assert parallel.index("const sampleReport = await running[sampleIndex]") < (
        parallel.index("const statefulPromise = pool.run")
    )
    # All worker sessions close before the reset check (which rewrites demo
    # data) runs in its own final session.
    assert "await pool.drain()" in parallel
    assert parallel.index("await closeBrowser(opts, workDir)") < parallel.index(
        "const resetReport = await runWorkerGroup"
    )
    assert "mergeWorkerReports(reports, resetReport)" in parallel

    # One worker runs its group's checks sequentially in one browser profile.
    assert "for (const check of workerGroupChecks(cfg.workerGroup))" in worker
    assert "await check(ctx)" in worker
    helpers = runner.split("const SERIALIZED_HELPERS = [", maxsplit=1)[1]
    assert "runDemoWorker" in helpers
    assert "workerGroupChecks" in helpers


def test_worker_snippet_serializes_the_live_answer_helpers() -> None:
    """``waitForLiveAnswer`` is serialized, so its three callees must be too.

    ``snippetFor`` only carries the functions listed in
    ``SERIALIZED_HELPERS``; the serial and parallel paths share that list. An
    unlisted callee would throw ``ReferenceError`` inside the worker before
    any check runs, whichever path serialized it.
    """
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    serialized = _serialized_helper_names(runner)

    for name in (
        "liveAnswerSnapshot",
        "liveAnswerFailure",
        "liveAnswerResult",
        "liveAnswerUnobserved",
        "waitForLiveAnswer",
    ):
        assert name in serialized, f"the worker snippet does not serialize {name}"


def test_serialized_helpers_only_call_runner_helpers_that_are_serialized() -> None:
    """A serialized helper must never call a runner helper missing from the list.

    The browser snippet has no module system: each helper's source is injected
    on its own, so a call to an unlisted top-level helper is a runtime
    ``ReferenceError`` in the worker. This walks the serialized helpers'
    sources and fails on exactly that kind of dangling call.
    """
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    serialized = _serialized_helper_names(runner)
    declared = _declared_runner_functions(runner)

    dangling: dict[str, list[str]] = {}
    for name in serialized:
        declaration = declared.get(name)
        if declaration is None:
            # Imported from the mode-watch module: a separate module cannot
            # reach the runner's own helpers, so there is no closure to check.
            continue
        source = _function_source(runner, f"{declaration} {name}")
        # ``_function_source`` stops at the next async declaration; plain
        # functions declared between two async helpers still land in the tail.
        source = source.split("\nfunction ", maxsplit=1)[0]
        called = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", source))
        unlisted = sorted((called & set(declared)) - set(serialized) - {name})
        if unlisted:
            dangling[name] = unlisted

    assert not dangling, (
        "serialized helpers call runner helpers the snippet does not serialize: "
        + "; ".join(
            f"{name} -> {', '.join(callees)}" for name, callees in sorted(dangling.items())
        )
    )


def test_parallel_worker_navigates_to_the_app_root_before_its_checks() -> None:
    """A fresh worker browser starts at about:blank, not on the app origin.

    ``runDemoWorker`` must navigate to the app root after installing the
    origin route, the watcher and the request watch, and before its first
    check. The follow-ups worker's first check reads ``localStorage``, which
    about:blank denies with a ``SecurityError``; sample/stateful workers only
    passed because their own first check navigates first.
    """
    runner = BROWSER_RUNNER.read_text(encoding="utf-8")
    worker = _function_source(runner, "async function runDemoWorker")

    navigation = "await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });"
    assert navigation in worker, "the worker never navigates to the app root"
    goto_targets = re.findall(r"page\.goto\(([^,)]+)", worker)
    assert "`${cfg.baseUrl}/`" in goto_targets
    assert all("about:blank" not in target for target in goto_targets), (
        "the worker navigates to about:blank, where storage access is denied"
    )
    # The route must already cover the navigation, and the init script must be
    # installed before the first document that reads storage.
    assert worker.index("await restrictToAppOrigin(ctx);") < worker.index(navigation)
    assert worker.index("await installModeWatch(page") < worker.index(navigation)
    assert worker.index("ctx.requests = installRequestWatch(page);") < worker.index(navigation)
    assert worker.index(navigation) < worker.index(
        "for (const check of workerGroupChecks(cfg.workerGroup))"
    )


MERGE_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const harness = await import(pathToFileURL(process.env.HARNESS_PATH).href);
const evidence = {
  observedRequests: 1,
  messagePosts: 1,
  invalidMessagePosts: 0,
  messageKeys: ["question"],
  tiersFields: 0,
  tiersPaths: [],
};
const events = [
  { kind: "content_clarification", prompt: "slot" },
  { kind: "answer_visible", length: 10 },
  { kind: "live_answer", length: 10 },
  { kind: "loading_over_answer", replacedAnswer: false },
];
const reports = harness.acceptanceGroups().map((group) => ({
  worker: group.id,
  checks: group.checks.map((_check, index) => ({
    id: `${group.id}-${index}`,
    pass: true,
    ms: 1,
    detail: { worker: group.id },
  })),
  watchEvents: events,
  requestSummary: evidence,
}));
const resetReport = {
  worker: "reset",
  checks: [{ id: "demo-reset-control", pass: true, ms: 1, detail: { reset: true } }],
  watchEvents: [],
  requestSummary: { ...evidence, messagePosts: 0, messageKeys: [] },
};
const merged = await harness.mergeWorkerReports(reports, resetReport);
const broken = reports.map((report, index) =>
  index === 1 ? { ...report, watchEvents: null, requestSummary: null } : report
);
const mergedBroken = await harness.mergeWorkerReports(broken, resetReport);
const aggregateIds = [
  "no-mode-window-or-text-throughout-run",
  "request-bodies-carry-no-tiers",
];
console.log(JSON.stringify({
  ids: merged.checks.map((check) => check.id),
  aggregate: merged.checks
    .filter((check) => aggregateIds.includes(check.id))
    .map((check) => ({ id: check.id, pass: check.pass, detail: check.detail, error: check.error })),
  brokenAggregate: mergedBroken.checks
    .filter((check) => aggregateIds.includes(check.id))
    .map((check) => ({ id: check.id, pass: check.pass, error: check.error })),
}));
"""


def test_parallel_merge_keeps_26_rows_aggregates_and_runs_reset_last() -> None:
    merged = _run_node(MERGE_SCRIPT)
    ids = merged["ids"]

    assert len(ids) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert ids[-1] == "demo-reset-control"
    # 17 independent rows, 6 stateful rows, 2 aggregated rows, then reset.
    assert ids[:17] == [
        "sample-0", "sample-1",
        "documents-a-0", "documents-a-1", "documents-a-2",
        "documents-b-0", "documents-b-1", "documents-b-2",
        "grounded-0", "grounded-1", "grounded-2", "grounded-3", "grounded-4",
        "follow-ups-0", "follow-ups-1", "follow-ups-2", "follow-ups-3",
    ]
    assert ids[17:23] == [f"stateful-{index}" for index in range(6)]
    assert ids[23:25] == [
        "no-mode-window-or-text-throughout-run",
        "request-bodies-carry-no-tiers",
    ]
    assert ids[25:] == ["demo-reset-control"]

    aggregates = {entry["id"]: entry for entry in merged["aggregate"]}
    assert all(entry["pass"] for entry in aggregates.values())
    disclosure = aggregates["no-mode-window-or-text-throughout-run"]["detail"]
    # Every worker's watcher evidence is aggregated, not just one session's.
    assert disclosure["contentClarifications"] == 6
    assert disclosure["answerVisible"] == 6
    assert disclosure["liveAnswers"] == 6
    assert disclosure["loadingOverAnswers"] == 6
    request = aggregates["request-bodies-carry-no-tiers"]["detail"]
    assert request["messagePosts"] == 6
    assert request["tiersFields"] == 0

    # A worker with no evidence cannot silently satisfy the run-wide claims.
    broken = {entry["id"]: entry for entry in merged["brokenAggregate"]}
    assert set(broken) == {
        "no-mode-window-or-text-throughout-run",
        "request-bodies-carry-no-tiers",
    }
    assert all(entry["pass"] is False for entry in broken.values())
    assert "documents-a: no watcher evidence" in broken[
        "no-mode-window-or-text-throughout-run"
    ]["error"]
    assert "documents-a: no request evidence" in broken[
        "request-bodies-carry-no-tiers"
    ]["error"]


DISCLOSURE_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const harness = await import(pathToFileURL(process.env.HARNESS_PATH).href);
const outcome = (events) => {
  try {
    return { ok: true, summary: harness.disclosureSummary(events) };
  } catch (error) {
    return { ok: false, error: error.message };
  }
};
const base = [
  { kind: "content_clarification", prompt: "slot" },
  { kind: "answer_visible", length: 10 },
];
console.log(JSON.stringify({
  valid: outcome(base),
  window: outcome([...base, { kind: "mode_window", title: "检索档" }]),
  token: outcome([...base, { kind: "mode_token", surface: "page-chrome", tokens: ["sql"] }]),
  noClarification: outcome([{ kind: "answer_visible", length: 10 }]),
}));
"""


def test_parallel_disclosure_aggregation_keeps_the_whole_run_claim() -> None:
    outcomes = _run_node(DISCLOSURE_SCRIPT)

    assert outcomes["valid"]["ok"] is True
    assert outcomes["valid"]["summary"]["contentClarifications"] == 1
    assert outcomes["valid"]["summary"]["answerVisible"] == 1

    assert outcomes["window"]["ok"] is False
    assert "1 retrieval-mode choice window" in outcomes["window"]["error"]
    assert outcomes["token"]["ok"] is False
    assert "page-chrome" in outcomes["token"]["error"]
    assert outcomes["noClarification"]["ok"] is False
    assert "never showed a content clarification" in outcomes["noClarification"]["error"]


FACT_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const harness = await import(pathToFileURL(process.env.HARNESS_PATH).href);
const fact = "2024-10-01 at 13:30 UTC";
console.log(JSON.stringify({
  sourceForm: harness.answerStatesFact(
    "The checkpoint was 2024-10-01 at 13:30 UTC.", fact),
  modelForm: harness.answerStatesFact(
    "The checkpoint was 2024-10-01 13:30 UTC.", fact),
  separated: harness.answerStatesFact(
    "The date was 2024-10-01, and the cut was 13:30 UTC.", fact),
  dateOnly: harness.answerStatesFact("The date was 2024-10-01.", fact),
  timeOnly: harness.answerStatesFact("The cut was 13:30 UTC.", fact),
}));
"""


def test_q12_fact_matching_tolerates_the_connective_but_requires_contiguity() -> None:
    outcomes = _run_node(FACT_SCRIPT)

    assert outcomes == {
        "sourceForm": True,
        "modelForm": True,
        "separated": False,
        "dateOnly": False,
        "timeOnly": False,
    }


class _FakeDemoHandler(http.server.BaseHTTPRequestHandler):
    """The two preflight endpoints the runner fetches before opening workers."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/api/demo":
            body = b'{"enabled":true}'
            content_type = "application/json"
        elif self.path == "/":
            body = b"<html><body>fake demo</body></html>"
            content_type = "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture
def fake_demo_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeDemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


FAKE_PLAYWRIGHT = r'''#!__PYTHON__
import json
import os
import re
import sys
import time
from pathlib import Path

LOG = Path(os.environ["FAKE_PLAYWRIGHT_LOG"])
COUNTS = json.loads(os.environ["FAKE_PLAYWRIGHT_COUNTS"])
ARGS = sys.argv[1:]
SESSION = next((arg[3:] for arg in ARGS if arg.startswith("-s=")), "")


def log(line):
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def barrier_arrivals():
    """How many workers have reached the barrier, counted from the log."""
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line.startswith("barrier-wait "))


def await_concurrent_workers(group):
    """Block until the expected number of workers has arrived.

    Each worker records its arrival first and only finishes after the
    arrival count reaches the width, so no worker can complete before the
    whole width was concurrently waiting. A serial pool never reaches the
    width in time and fails instead of passing. The reset runs alone after
    every session closed and is exempt.
    """
    width = int(os.environ.get("FAKE_PLAYWRIGHT_BARRIER", "1"))
    deadline = time.monotonic() + float(
        os.environ.get("FAKE_PLAYWRIGHT_BARRIER_TIMEOUT", "10")
    )
    log(f"barrier-wait {group}")
    while barrier_arrivals() < width:
        if time.monotonic() >= deadline:
            log(f"barrier-timeout {group}")
            print(
                f"fake worker barrier timed out: {group} needs "
                f"{width} concurrent workers",
                file=sys.stderr,
            )
            sys.exit(5)
        time.sleep(0.01)
    log(f"barrier-reached {group}")


if "open" in ARGS:
    log(f"open {SESSION}")
    sys.exit(0)
if "close" in ARGS:
    log(f"close {SESSION}")
    sys.exit(0)
if "run-code" in ARGS:
    filename = next(arg.split("=", 1)[1] for arg in ARGS if arg.startswith("--filename="))
    match = re.search(r"worker-([a-z-]+)-snippet\.js$", filename)
    if not match:
        print(f"fake: unknown snippet {filename}", file=sys.stderr)
        sys.exit(3)
    group = match.group(1)
    if group == "stateful":
        # The stateful worker must receive the thread the sample published,
        # not an empty hand-off.
        snippet = Path(filename).read_text(encoding="utf-8")
        if '"sampleThreadUrl":"http://127.0.0.1:1/t/fake"' not in snippet:
            print("fake: stateful worker missing the sample thread", file=sys.stderr)
            sys.exit(4)
        if '"lastThreadMarker":"fake"' not in snippet:
            print("fake: stateful worker missing the last-thread marker", file=sys.stderr)
            sys.exit(4)
    log(f"run-start {group}")
    if group != "reset":
        await_concurrent_workers(group)
    failing = set(filter(None, os.environ.get("FAKE_PLAYWRIGHT_FAIL_GROUPS", "").split(",")))
    if group in failing:
        log(f"run-end {group}")
        print(f"fake worker failure: {group}", file=sys.stderr)
        sys.exit(3)
    checks = [
        {"id": f"{group}-{index}", "pass": True, "ms": 1, "detail": {"worker": group}}
        for index in range(COUNTS[group])
    ]
    payload = {
        "checks": checks,
        "watchEvents": [] if group == "reset" else [
            {"kind": "content_clarification", "prompt": group}
        ],
        "requestSummary": {
            "observedRequests": 1,
            "messagePosts": 0 if group == "reset" else 1,
            "invalidMessagePosts": 0,
            "messageKeys": [] if group == "reset" else ["question"],
            "tiersFields": 0,
            "tiersPaths": [],
        },
        "published": (
            {
                "sampleThreadUrl": "http://127.0.0.1:1/t/fake",
                "sampleThreadId": "fake",
                "lastThreadMarker": "fake",
            }
            if group == "sample"
            else None
        ),
    }
    log(f"run-end {group}")
    print(json.dumps({"result": payload}))
    sys.exit(0)
sys.exit(0)
'''


def _scripted_playwright(tmp_path: Path) -> tuple[Path, Path, dict[str, int]]:
    """A Playwright CLI double that reports scripted rows for each worker."""
    assert NODE is not None
    plan = _acceptance_plan()
    counts = {group["id"]: len(group["checks"]) for group in plan["groups"]}
    counts["reset"] = len(plan["reset"])

    fake_cli = tmp_path / "fake-playwright"
    fake_cli.write_text(
        FAKE_PLAYWRIGHT.replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    log = tmp_path / "playwright.log"
    return fake_cli, log, counts


def _run_scripted_parallel(
    fake_demo_server: str,
    fake_cli: Path,
    log: Path,
    counts: dict[str, int],
    *,
    fail_groups: str = "",
) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    env = {
        **os.environ,
        "FIN_DEMO_ACCEPTANCE_READY": "1",
        "FIN_DEMO_E2E_BASE_URL": fake_demo_server,
        "PLAYWRIGHT_CLI": str(fake_cli),
        "FAKE_PLAYWRIGHT_LOG": str(log),
        "FAKE_PLAYWRIGHT_COUNTS": json.dumps(counts),
        # The workers coordinate on the log they append to: each logs
        # `run-start` and a barrier arrival, then waits until three workers
        # have arrived. A serial pool never reaches that width and times
        # out, so the concurrency claim is a recorded handshake instead of
        # a sleep overlap.
        "FAKE_PLAYWRIGHT_BARRIER": "3",
        "FAKE_PLAYWRIGHT_BARRIER_TIMEOUT": "10",
        "FAKE_PLAYWRIGHT_FAIL_GROUPS": fail_groups,
    }
    return subprocess.run(
        [
            NODE,
            str(BROWSER_RUNNER),
            f"--base-url={fake_demo_server}",
            "--parallel=3",
            "--timeout-ms=30000",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_parallel_scripted_smoke_merges_workers_and_runs_reset_last(
    tmp_path: Path, fake_demo_server: str
) -> None:
    """Drive the real orchestrator with a scripted Playwright CLI.

    This exercises the session pool, per-worker report merge, the 26-row
    table, the cross-worker disclosure/request aggregation, and the
    close-all-then-reset ordering without a demo, a database, or a model.
    """
    fake_cli, log, counts = _scripted_playwright(tmp_path)
    result = _run_scripted_parallel(fake_demo_server, fake_cli, log, counts)

    assert result.returncode == 0, result.stderr + result.stdout
    pass_rows = [line for line in result.stdout.splitlines() if line.startswith("[PASS]")]
    assert len(pass_rows) == EXPECTED_ACCEPTANCE_CHECK_COUNT
    assert "[FAIL]" not in result.stdout
    assert f"verdict: PASS — {EXPECTED_ACCEPTANCE_CHECK_COUNT}/{EXPECTED_ACCEPTANCE_CHECK_COUNT}" in result.stdout

    events = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
    opened = [event[1] for event in events if event[0] == "open"]
    closed = [event[1] for event in events if event[0] == "close"]
    assert len(opened) == len(counts) == len(set(opened))
    assert sorted(opened) == sorted(closed)

    # The pool runs three sessions at a time, and the workers say so
    # themselves: the first barrier was recorded only after three run-starts
    # and before any run-end, and every worker that was not the reset reached
    # it. A serial pool times out at the barrier instead of passing.
    barrier_events = [
        (index, event)
        for index, event in enumerate(events)
        if event[0] == "barrier-reached"
    ]
    assert barrier_events, "no worker reached the concurrency barrier"
    assert not [event for event in events if event[0] == "barrier-timeout"], (
        "a worker timed out waiting for the pool to run three workers"
    )
    first_barrier = barrier_events[0][0]
    started_before_barrier = [
        event for event in events[:first_barrier] if event[0] == "run-start"
    ]
    assert len(started_before_barrier) >= 3, (
        "the barrier was reached before three workers started"
    )
    assert not any(event[0] == "run-end" for event in events[:first_barrier]), (
        "a worker finished before three workers had started"
    )
    assert {event[1] for _index, event in barrier_events} == set(counts) - {"reset"}

    starts = [index for index, event in enumerate(events) if event[0] == "run-start"]
    assert events[starts[-1]] == ["run-start", "reset"]
    for index, event in enumerate(events):
        if event[0] == "run-end" and event[1] != "reset":
            assert index < starts[-1]
        if event[0] == "close" and not event[1].endswith("-reset"):
            assert index < starts[-1]


def test_parallel_worker_failure_still_closes_sessions_and_runs_reset(
    tmp_path: Path, fake_demo_server: str
) -> None:
    """A dying worker is a harness failure, not an orphaned session."""
    fake_cli, log, counts = _scripted_playwright(tmp_path)
    result = _run_scripted_parallel(
        fake_demo_server,
        fake_cli,
        log,
        counts,
        fail_groups="documents-a",
    )

    # The failed worker contributes no rows, so the run is a harness failure
    # even though every session was closed and the reset still ran.
    assert result.returncode == 2, result.stderr + result.stdout
    assert "[FAIL] worker documents-a" in result.stderr
    rows = [
        line for line in result.stdout.splitlines()
        if line.startswith(("[PASS]", "[FAIL]"))
    ]
    assert len(rows) == EXPECTED_ACCEPTANCE_CHECK_COUNT - counts["documents-a"]
    # Both run-wide rows fail too: the dead worker left no request evidence.
    pass_rows = [line for line in rows if line.startswith("[PASS]")]
    assert len(pass_rows) == (
        EXPECTED_ACCEPTANCE_CHECK_COUNT - counts["documents-a"] - 2
    )
    assert "expected 26 checks, got 23" in result.stderr

    events = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
    opened = [event[1] for event in events if event[0] == "open"]
    closed = [event[1] for event in events if event[0] == "close"]
    assert sorted(opened) == sorted(closed)
    starts = [index for index, event in enumerate(events) if event[0] == "run-start"]
    assert events[starts[-1]] == ["run-start", "reset"]


FAKE_PSYCOPG = r'''
import os
from pathlib import Path

def _trace(line):
    Path(os.environ["TRACE_FILE"]).open("a", encoding="utf-8").write(line + "\n")

class Cursor:
    """The cursor surface of psycopg the acceptance scripts use."""

    def __init__(self, connection):
        self.connection = connection
        self.rowcount = -1
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def execute(self, sql):
        _trace(f"query:{sql}")
        statement = sql.lstrip().upper()
        self.rowcount = 1 if statement.startswith(("DELETE", "INSERT", "UPDATE")) else -1
        return self
    def fetchone(self):
        return (self.connection.database,)

class Connection:
    def __init__(self, requested):
        self.requested = requested
        self.database = os.environ.get("FAKE_DATABASE_NAME", requested)
    def __enter__(self):
        _trace("connected")
        _trace(f"database:{self.database}")
        return self
    def __exit__(self, *_args):
        return False
    def cursor(self):
        return Cursor(self)
    def execute(self, sql):
        return self.cursor().execute(sql)
    def fetchone(self):
        return (self.database,)
    def commit(self):
        _trace("commit")

def connect(dsn, **_kwargs):
    return Connection(dsn.split("=", 1)[1])
'''

FAKE_CONNINFO = r'''
def conninfo_to_dict(dsn):
    if not dsn.startswith("dbname="):
        raise ValueError("invalid connection info")
    return dict(part.split("=", 1) for part in dsn.split())
'''


def _fake_psycopg(tmp_path: Path) -> Path:
    package = tmp_path / "fake-python" / "psycopg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAKE_PSYCOPG, encoding="utf-8")
    (package / "conninfo.py").write_text(FAKE_CONNINFO, encoding="utf-8")
    return package.parent


def _db_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(_fake_psycopg(tmp_path)),
        "TRACE_FILE": str(tmp_path / "db-trace.log"),
        "FIN_CHECKPOINT_URL": "dbname=" + "fin_test",
        "GRAPHRAG_MEMORY_DB_URL": "dbname=" + "documents_test",
    }


def test_db_preflight_requires_both_targets_before_import_or_connect(tmp_path: Path) -> None:
    env = _db_env(tmp_path)
    env.pop("GRAPHRAG_MEMORY_DB_URL")

    result = subprocess.run(
        [sys.executable, str(DB_CHECK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "GRAPHRAG_MEMORY_DB_URL" in result.stderr
    assert not (tmp_path / "db-trace.log").exists()


@pytest.mark.parametrize(
    ("setting", "database"),
    (("FIN_CHECKPOINT_URL", "fin_dev"), ("GRAPHRAG_MEMORY_DB_URL", "documents_dev")),
)
def test_db_preflight_rejects_non_test_name_before_any_connection(
    tmp_path: Path, setting: str, database: str
) -> None:
    env = _db_env(tmp_path)
    env[setting] = "dbname=" + database

    result = subprocess.run(
        [sys.executable, str(DB_CHECK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert setting in result.stderr
    assert "_test" in result.stderr
    assert not (tmp_path / "db-trace.log").exists()


@pytest.mark.parametrize("connection_option", ["service=local", "passfile=/tmp/pgpass"])
def test_db_preflight_rejects_hidden_libpq_file_sources(
    tmp_path: Path, connection_option: str
) -> None:
    env = _db_env(tmp_path)
    env["FIN_CHECKPOINT_URL"] = "dbname=" + "fin_test " + connection_option

    result = subprocess.run(
        [sys.executable, str(DB_CHECK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "environment" in result.stderr
    assert not (tmp_path / "db-trace.log").exists()


def test_db_preflight_checks_connected_database_and_uses_only_identity_query(tmp_path: Path) -> None:
    env = _db_env(tmp_path)
    env["FAKE_DATABASE_NAME"] = "unexpected_database"

    result = subprocess.run(
        [sys.executable, str(DB_CHECK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "identity" in result.stderr
    trace = (tmp_path / "db-trace.log").read_text(encoding="utf-8")
    assert trace.count("connected\n") == 1
    assert trace.count("query:SELECT current_database()\n") == 1
    assert "unexpected_database" not in result.stdout + result.stderr


def test_db_preflight_verifies_both_test_targets_without_echoing_values(tmp_path: Path) -> None:
    env = _db_env(tmp_path)

    result = subprocess.run(
        [sys.executable, str(DB_CHECK)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout
    trace = (tmp_path / "db-trace.log").read_text(encoding="utf-8")
    assert trace.count("connected\n") == 2
    assert trace.count("query:SELECT current_database()\n") == 2
    assert "fin_test" not in result.stdout + result.stderr
    assert "documents_test" not in result.stdout + result.stderr


def _acceptance_checkout(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "fin"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(LAUNCHER, scripts / LAUNCHER.name)
    shutil.copy(DB_CHECK, scripts / DB_CHECK.name)
    shutil.copy(RELEASE_SCRIPT, scripts / RELEASE_SCRIPT.name)
    (scripts / "demo-browser-acceptance.mjs").write_text("// fake runner\n", encoding="utf-8")

    trace = tmp_path / "launcher-trace.log"
    fin = scripts / "fin"
    fin.write_text(
        """#!/usr/bin/env bash
printf 'fin %s isolated=%s settings=%s mode=%s skip=%s checkpoint=%s index=%s key=%s embed=%s adapter=%s scripted=%s ready=%s ledger=%s metadata=%s\\n' \\
  "$*" "${FIN_HOME:-}" "${FIN_SETTINGS_FILE:-}" "${FIN_DEMO_MODE:-}" \\
  "${FIN_DEMO_SKIP_BROWSER:-}" "${FIN_CHECKPOINT_URL:+present}" \\
  "${GRAPHRAG_MEMORY_DB_URL:+present}" "${FIN_API_KEY:+present}" \\
  "${GRAPHRAG_MEMORY_EMBED_API_KEY:+present}" "${FIN_RETRIEVE_ADAPTER:-}" \\
  "${FIN_SCRIPTED_MODEL:-}" "${FIN_DEMO_ACCEPTANCE_READY:-}" \\
  "${FIN_BUSINESS_LEDGER_URL:+present}" \\
  "${FIN_INGEST_STATE_DSN:+present}${FIN_METADATA_DSN:+present}${FIN_METADATA_URL:+present}" >>"$TRACE_FILE"
if [[ "${1:-}" == "demo" && "${2:-}" != "--stop" && "${FAKE_FIN_START_FAIL:-0}" == 1 ]]; then exit 7; fi
if [[ "${1:-}" == "demo" && "${2:-}" == "--stop" && "${FAKE_FIN_STOP_FAIL:-0}" == 1 ]]; then exit 8; fi
exit 0
""",
        encoding="utf-8",
    )
    fin.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = shutil.which("python3")
    assert python is not None
    tools = {
        "uv": f"""#!/usr/bin/env bash
printf 'verify\\n' >>\"$TRACE_FILE\"
shift 3
printf 'uv:%s\\n' "$(basename "$1")" >>"$TRACE_FILE"
TRACE_FILE=\"$DB_TRACE_FILE\" exec \"{python}\" \"$@\"
""",
        "node": """#!/usr/bin/env bash
shift
printf 'browser ready=%s url=%s leaked=%s sql_env=%s args=%s\\n' "${FIN_DEMO_ACCEPTANCE_READY:-}" \\
  "${FIN_DEMO_E2E_BASE_URL:-}" "${FIN_CHECKPOINT_URL:+yes}${GRAPHRAG_MEMORY_DB_URL:+yes}${FIN_API_KEY:+yes}${GRAPHRAG_MEMORY_EMBED_API_KEY:+yes}${PGPASSWORD:+yes}${OPENAI_API_KEY:+yes}" \\
  "${FIN_BUSINESS_LEDGER_URL:+yes}${FIN_INGEST_STATE_DSN:+yes}${FIN_METADATA_DSN:+yes}${FIN_METADATA_URL:+yes}" \\
  "$*" >>"$TRACE_FILE"
if [[ "${FAKE_BROWSER_FAIL:-0}" == 1 ]]; then exit 9; fi
[[ "${FIN_DEMO_ACCEPTANCE_READY:-}" == 1 && "${FIN_DEMO_E2E_BASE_URL:-}" == "http://127.0.0.1:8000" ]]
""",
        "playwright-cli": "#!/usr/bin/env bash\nexit 0\n",
        "docker": "#!/usr/bin/env bash\nexit 0\n",
        "curl": "#!/usr/bin/env bash\n[[ \"${FAKE_CURL_RESPONDS:-0}\" == 1 ]] && exit 0\nexit 7\n",
    }
    for name, content in tools.items():
        executable = bin_dir / name
        executable.write_text(content, encoding="utf-8")
        executable.chmod(0o755)

    fake_python = _fake_psycopg(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(fake_python),
        "TRACE_FILE": str(trace),
        "DB_TRACE_FILE": str(tmp_path / "db-trace.log"),
        "REAL_PYTHON": python,
        "FIN_CHECKPOINT_URL": "dbname=" + "fin_acceptance_test",
        "GRAPHRAG_MEMORY_DB_URL": "dbname=" + "docs_acceptance_test",
        "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
        "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fixture-embed-key",
        "FIN_API_KEY": "fixture-fin-key",
        "PGPASSWORD": "fixture-postgres-password",
        "OPENAI_API_KEY": "fixture-provider-key",
    }
    for name in (
        "FIN_HOME",
        "FIN_SETTINGS_FILE",
        "IKNOW_MEMORY_HOME",
        "FIN_DEMO_MODE",
        "FIN_DEMO_SKIP_BROWSER",
        "FIN_DEMO_ACCEPTANCE_READY",
        "FIN_DEMO_E2E_BASE_URL",
        "FIN_DEMO_ACCEPTANCE_PARALLEL",
    ):
        env.pop(name, None)
    env.update(
        {
            "FIN_SCRIPTED_MODEL": "1",
            "FIN_RETRIEVE_ADAPTER": "fake",
            "FIN_DEMO_ACCEPTANCE_READY": "stale",
            "FIN_DEMO_E2E_BASE_URL": "http://example.invalid",
        }
    )
    return repo, env, trace


def test_launcher_checks_test_databases_then_starts_browser_and_always_stops(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = trace.read_text(encoding="utf-8").splitlines()
    assert events[0] == "verify"
    assert events[1] == "uv:demo_acceptance_db_check.py"
    assert events[2] == "verify"
    assert events[3] == "uv:demo_acceptance_release_instance.py"
    assert events[4].startswith("fin demo ")
    assert "isolated=" in events[4]
    assert "settings=" in events[4]
    assert "mode=1 skip=1" in events[4]
    assert "checkpoint=present index=present key=present embed=present" in events[4]
    assert "adapter=mcp scripted= ready=" in events[4]
    assert events[4].endswith(" ledger= metadata=")
    assert events[5] == (
        "browser ready=1 url=http://127.0.0.1:8000 leaked= sql_env= "
        "args=--base-url=http://127.0.0.1:8000 --parallel=5"
    )
    assert events[6].startswith("fin demo --stop ")
    assert events[6].endswith(" ledger= metadata=")
    assert "127.0.0.1:8000" in result.stdout
    assert "fixture-fin-key" not in result.stdout + result.stderr
    isolated_fin_home = Path(events[4].split("isolated=", 1)[1].split(" ", 1)[0])
    assert not isolated_fin_home.parent.exists()
    db_trace = (tmp_path / "db-trace.log").read_text(encoding="utf-8")
    # The preflight still opens one connection per target and one identity
    # query each; the release step adds a third connection to the checkpoint
    # target and deletes the claim row.
    assert db_trace.splitlines()[:6] == [
        "connected",
        "database:fin_acceptance_test",
        "query:SELECT current_database()",
        "connected",
        "database:docs_acceptance_test",
        "query:SELECT current_database()",
    ]
    assert db_trace.count("connected\n") == 3
    assert db_trace.count("query:SELECT current_database()\n") == 3
    assert db_trace.count("query:DELETE FROM fin_instance\n") == 1
    assert "released the instance claim" in result.stdout


def test_launcher_drops_inherited_non_test_sql_connections_from_children(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env.update(
        {
            "FIN_BUSINESS_LEDGER_URL": "dbname=ledger_live",
            "FIN_INGEST_STATE_DSN": "dbname=ingest_live",
            "FIN_METADATA_DSN": "dbname=metadata_live",
            "FIN_METADATA_URL": "dbname=metadata_url_live",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = trace.read_text(encoding="utf-8").splitlines()
    assert events[4].endswith(" ledger= metadata=")
    assert " sql_env= args=" in events[5]
    assert events[5].endswith("--parallel=5")
    assert events[6].endswith(" ledger= metadata=")


def test_launcher_stops_after_browser_failure(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env["FAKE_BROWSER_FAIL"] = "1"

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    events = trace.read_text(encoding="utf-8").splitlines()
    assert events[-1].startswith("fin demo --stop ")
    db_trace = (tmp_path / "db-trace.log").read_text(encoding="utf-8")
    # Both preflight targets plus the release step's checkpoint connection.
    assert db_trace.count("connected\n") == 3
    assert db_trace.count("query:DELETE FROM fin_instance\n") == 1
    assert "fixture-fin-key" not in result.stdout + result.stderr


def test_launcher_retains_isolated_home_if_stop_fails_for_recovery(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env.update({"FAKE_BROWSER_FAIL": "1", "FAKE_FIN_STOP_FAIL": "1"})

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    events = trace.read_text(encoding="utf-8").splitlines()
    isolated_fin_home = Path(events[4].split("isolated=", 1)[1].split(" ", 1)[0])
    assert result.returncode != 0
    assert "stopping the isolated demo stack failed" in result.stderr
    assert "isolated home retained for recovery" in result.stderr
    assert isolated_fin_home.parent.exists()
    shutil.rmtree(isolated_fin_home.parent)


def test_launcher_stops_after_partial_fin_start_failure(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env["FAKE_FIN_START_FAIL"] = "1"

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    events = trace.read_text(encoding="utf-8").splitlines()
    assert events[3] == "uv:demo_acceptance_release_instance.py"
    assert events[-2].startswith("fin demo ")
    assert events[-1].startswith("fin demo --stop ")


@pytest.mark.parametrize(
    "missing",
    [
        "FIN_CHECKPOINT_URL",
        "GRAPHRAG_MEMORY_DB_URL",
        "FIN_API_KEY",
        "GRAPHRAG_MEMORY_EMBED_API_KEY",
        "GRAPHRAG_MEMORY_ES_URL",
        "GRAPHRAG_MEMORY_NEO4J_URI",
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS",
        "GRAPHRAG_MEMORY_EMBED_MODEL",
    ],
)
def test_launcher_missing_database_target_stops_before_verify_or_start(
    tmp_path: Path, missing: str
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env.pop(missing)

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert missing in result.stderr
    assert not trace.exists()


def test_launcher_rejects_arguments_before_database_or_service_work(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh"), "--help"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not trace.exists()


@pytest.mark.parametrize(
    ("arguments", "parallel_env", "expected_args"),
    [
        ([], None, "--base-url=http://127.0.0.1:8000 --parallel=5"),
        (["--parallel=3"], None, "--base-url=http://127.0.0.1:8000 --parallel=3"),
        (["--serial"], None, "--base-url=http://127.0.0.1:8000 --serial"),
        ([], "2", "--base-url=http://127.0.0.1:8000 --parallel=2"),
    ],
)
def test_launcher_forwards_the_parallel_mode_to_the_runner_once(
    tmp_path: Path,
    arguments: list[str],
    parallel_env: str | None,
    expected_args: str,
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    if parallel_env is not None:
        env["FIN_DEMO_ACCEPTANCE_PARALLEL"] = parallel_env

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh"), *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = trace.read_text(encoding="utf-8").splitlines()
    browser_events = [line for line in events if line.startswith("browser ")]
    assert len(browser_events) == 1
    assert browser_events[0].endswith(f" args={expected_args}")
    # The demo still starts and stops exactly once around that one runner.
    starts = [
        line for line in events
        if line.startswith("fin demo ") and "--stop" not in line
    ]
    stops = [line for line in events if line.startswith("fin demo --stop ")]
    assert len(starts) == 1
    assert len(stops) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["--parallel=0"],
        ["--parallel=7"],
        ["--parallel=abc"],
        ["--serial", "--parallel=3"],
        ["--unknown"],
    ],
)
def test_launcher_rejects_invalid_parallel_arguments_before_any_work(
    tmp_path: Path, arguments: list[str]
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh"), *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip()
    assert not trace.exists()


def test_launcher_rejects_an_invalid_parallel_environment_before_any_work(
    tmp_path: Path,
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env["FIN_DEMO_ACCEPTANCE_PARALLEL"] = "7"

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2
    assert "parallel worker count must be 1-6" in result.stderr
    assert not trace.exists()


def test_launcher_refuses_an_existing_loopback_service_before_database_work(tmp_path: Path) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    env["FAKE_CURL_RESPONDS"] = "1"

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "already serves a workspace" in result.stderr
    assert not trace.exists()
    assert not (tmp_path / "db-trace.log").exists()


def test_launcher_releases_the_instance_claim_after_the_preflight_and_before_the_demo(
    tmp_path: Path,
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = trace.read_text(encoding="utf-8").splitlines()
    preflight = events.index("uv:demo_acceptance_db_check.py")
    release = events.index("uv:demo_acceptance_release_instance.py")
    demo_start = next(index for index, line in enumerate(events) if line.startswith("fin demo "))
    assert preflight < release < demo_start
    # The release step addresses the checkpoint target alone and deletes the
    # claim row, then commits it.
    db_lines = (tmp_path / "db-trace.log").read_text(encoding="utf-8").splitlines()
    assert db_lines[-5:] == [
        "connected",
        "database:fin_acceptance_test",
        "query:SELECT current_database()",
        "query:DELETE FROM fin_instance",
        "commit",
    ]
    assert "docs_acceptance_test" not in "\n".join(db_lines[-5:])
    assert "released the instance claim" in result.stdout


def test_launcher_refuses_when_the_release_script_is_missing_from_the_checkout(
    tmp_path: Path,
) -> None:
    repo, env, trace = _acceptance_checkout(tmp_path)
    (repo / "scripts" / RELEASE_SCRIPT.name).unlink()

    result = subprocess.run(
        ["bash", str(repo / "scripts/demo-browser-acceptance.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1
    assert "missing its demo acceptance entrypoints" in result.stderr
    assert not trace.exists()
    assert not (tmp_path / "db-trace.log").exists()
