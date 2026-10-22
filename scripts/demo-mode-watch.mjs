// The mode watch: the continuous page-side scanner of the demo run.
//
// WHAT THIS IS: ADR 0104 and spec search-mode-host moved the retrieval
// mode out of the operator's view. The UI may show where a decision came
// from, never which internal tier ran. The vocabulary (which tokens must
// never reach a rendered surface), the classification (is this text a
// routing disclosure?) and the scanner that watches the rendered page
// are ONE concern, so they live here and the acceptance runner imports
// them.
//
// THE SERIALIZATION CONSTRAINT: the runner builds its browser snippet by
// serializing these functions with ``Function.prototype.toString`` — the
// page context has NO module system, so the page-side scanner must stay
// a SELF-CONTAINED function body. It is installed with
// ``page.addInitScript``, which evaluates that body in EVERY document of
// the session. The scanner therefore keeps its own state in one object
// and calls no sibling: the vocabulary it needs arrives in the config
// argument.

/**
 * The internal retrieval vocabulary that must never reach the rendered UI.
 * The ASCII tokens match case-insensitively on word boundaries; the Chinese
 * operator labels match as exact substrings.
 */
export function modeTokenRules() {
  return [
    { id: "lexical", source: "\\blexical\\b", ascii: true },
    { id: "graph", source: "\\bgraph\\b", ascii: true },
    { id: "global", source: "\\bglobal\\b", ascii: true },
    { id: "sql", source: "\\bsql\\b", ascii: true },
    { id: "查找", source: "查找", ascii: false },
    { id: "知识问答", source: "知识问答", ascii: false },
    { id: "综述", source: "综述", ascii: false },
    { id: "真数据库", source: "真数据库", ascii: false },
  ];
}

/** A routing word next to a token turns that token into a routing disclosure. */
export function routingContextPattern() {
  return /(?:检索档|搜索档|档位|模式|search[_\s-]?mode|\bmode\b)/i;
}

/**
 * Find internal routing tokens in a text.
 *
 * `prose` relaxes exactly one rule: a bare "sql" inside model prose is
 * ordinary database language ("SQL query"), not a routing disclosure, so it
 * only counts when a routing word sits next to it. Every UI surface is read
 * with `prose: false`, where the token list is absolute.
 */
export function findModeTokens(text, options) {
  const prose = Boolean(options && options.prose);
  const found = [];
  if (!text) return found;
  for (const rule of modeTokenRules()) {
    if (prose && rule.id === "sql") {
      const pattern = new RegExp(rule.source, "gi");
      let match = pattern.exec(text);
      while (match !== null) {
        const start = Math.max(0, match.index - 24);
        const end = Math.min(text.length, match.index + match[0].length + 24);
        if (routingContextPattern().test(text.slice(start, end))) {
          found.push(rule.id);
          break;
        }
        match = pattern.exec(text);
      }
      continue;
    }
    if (new RegExp(rule.source, rule.ascii ? "i" : "").test(text)) found.push(rule.id);
  }
  return found;
}

/**
 * Install the continuous page watcher.
 *
 * The init script runs in EVERY document of the session, so a routing window
 * that opens and closes between two harness assertions is still recorded.
 * The findings live in sessionStorage, so they survive a client-side
 * navigation, and the per-run storage key keeps a previous run's findings
 * out of this one.
 *
 * A choice window is a routing window only when it offers retrieval-mode
 * choices: its title carries 检索档, or its options carry a routing label.
 * A Fast extract-method proposal ("选择抽图方法") and a content clarification
 * card are recorded as their own kinds and never count as a routing window.
 */
export async function installModeWatch(page, options) {
  await page.addInitScript((config) => {
    const TOKENS = config.tokens;
    const STORAGE_KEY = config.storageKey;
    const ROUTING_MARKERS = config.routingMarkers;
    const LOADING_TEXT = config.loadingText;
    const TERMINAL_MARKERS = config.terminalMarkers;
    // The watcher's own state: what it has already seen, plus the few
    // flags it keeps between scans. ONE object, so a new rendered surface
    // is a new field here and not a new captured variable.
    const state = {
      seenWindows: new WeakSet(),
      seenClarifications: new WeakSet(),
      seenReceipts: new WeakSet(),
      seenAnswers: new WeakSet(),
      seenLiveAnswers: new WeakSet(),
      reportedTokens: new Set(),
      loadingOverlaySeen: false,
      loadingOverAnswerSeen: false,
      answerEverSeen: false,
      answerPath: null,
      lastChromeScan: 0,
    };

    function readEvents() {
      try {
        const raw = window.sessionStorage.getItem(STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : null;
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }

    function writeEvents(events) {
      try {
        window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(events));
      } catch {
        /* storage unavailable: the watcher degrades to in-document findings */
      }
    }

    function pushEvent(event) {
      const events = readEvents();
      events.push({ ...event, at: Date.now() });
      writeEvents(events);
    }

    function pushTokenEvent(surface, tokens) {
      if (tokens.length === 0) return;
      const key = `${surface}:${tokens.join(",")}`;
      if (state.reportedTokens.has(key)) return;
      state.reportedTokens.add(key);
      pushEvent({ kind: "mode_token", surface, tokens });
    }

    function tokensIn(text) {
      const found = [];
      for (const rule of TOKENS) {
        if (new RegExp(rule.source, rule.ascii ? "i" : "").test(text)) found.push(rule.id);
      }
      return found;
    }

    /** The tokens in MODEL PROSE: the same list, with the one relaxation
     * prose mode allows. A bare "sql" in the model's own reasoning is
     * ordinary database language; it counts only when a routing word sits
     * next to it. */
    function proseTokensIn(text) {
      const found = [];
      for (const rule of TOKENS) {
        if (new RegExp(rule.source, rule.ascii ? "i" : "").test(text)) {
          if (rule.id !== "sql") {
            found.push(rule.id);
            continue;
          }
          const pattern = new RegExp(rule.source, "gi");
          let match = pattern.exec(text);
          while (match !== null) {
            const start = Math.max(0, match.index - 24);
            const end = Math.min(text.length, match.index + match[0].length + 24);
            if (/(?:检索档|搜索档|档位|模式|search[_\s-]?mode|\bmode\b)/i.test(text.slice(start, end))) {
              found.push(rule.id);
              break;
            }
            match = pattern.exec(text);
          }
        }
      }
      return found;
    }

    function isTerminal(article) {
      for (const paragraph of article.querySelectorAll("p")) {
        const text = (paragraph.textContent || "").trim();
        if (TERMINAL_MARKERS.some((marker) => text.startsWith(marker))) return true;
      }
      return false;
    }

    /** The answer prose currently painted in one article, or "" before the
     * card holds any text. */
    function visibleAnswerText(article) {
      const card = article.querySelector('[data-testid="turn-answer-card"]');
      if (!card) return "";
      const prose = card.querySelector("p.whitespace-pre-wrap");
      return ((prose ? prose.textContent : "") || "").trim();
    }

    function anyAnswerOnScreen() {
      for (const article of document.querySelectorAll("article")) {
        if (visibleAnswerText(article)) return true;
      }
      return false;
    }

    function scanReceipt() {
      for (const element of document.querySelectorAll('[data-testid="search-mode-receipt"]')) {
        if (state.seenReceipts.has(element)) continue;
        state.seenReceipts.add(element);
        pushTokenEvent("search-mode-receipt", tokensIn(element.textContent || ""));
      }
    }

    function scanClarification() {
      for (const element of document.querySelectorAll('[data-testid="clarification-card"], [data-testid="document-choice-card"]')) {
        if (state.seenClarifications.has(element)) continue;
        state.seenClarifications.add(element);
        const prompt = element.querySelector('[data-testid="clarification-prompt"], [data-testid="document-choice-prompt"]');
        pushEvent({
          kind: "content_clarification",
          prompt: ((prompt ? prompt.textContent : element.textContent) || "").trim().slice(0, 200),
        });
        pushTokenEvent("clarification", tokensIn(element.textContent || ""));
      }
    }

    function classifyChoiceWindow(element) {
      const text = element.textContent || "";
      const heading = element.querySelector("p");
      const title = ((heading ? heading.textContent : "") || "").trim();
      const tokens = tokensIn(text);
      const offersRouting = ROUTING_MARKERS.some((marker) => text.includes(marker));
      if (offersRouting || tokens.length > 0) {
        pushEvent({ kind: "mode_window", title, tokens });
      } else {
        pushEvent({ kind: "choice_window", title });
      }
    }

    function scanChoiceWindows() {
      for (const element of document.querySelectorAll('[data-testid="choice-window"]')) {
        if (state.seenWindows.has(element)) continue;
        state.seenWindows.add(element);
        classifyChoiceWindow(element);
      }
    }

    function scanRoutingSurfaces() {
      scanReceipt();
      scanClarification();
      scanChoiceWindows();
    }

    function scanLiveAnswer() {
      for (const article of document.querySelectorAll("article")) {
        const text = visibleAnswerText(article);
        if (!text) continue;
        // The answer has been on screen in THIS document. The route may
        // replace the whole page with the entry loader (the product's
        // loading branch is a full early return), so at the moment the
        // loader paints no answer card is left to inspect. Remembering the
        // route that carried the answer is what lets the loader rule catch
        // replacement, not just an overlay.
        if (!state.answerEverSeen) {
          state.answerEverSeen = true;
          state.answerPath = window.location.pathname;
        }
        // The FIRST visible answer of EACH article is recorded regardless of
        // its terminal state: React commits the answer card and the terminal
        // status line in the same commit in most runs, so a terminal guard
        // here would hide the only evidence that the answer ever appeared.
        // Per-article dedupe (not one flag per run) is what lets every caller
        // read a fresh event after its own cursor.
        if (!state.seenAnswers.has(article)) {
          state.seenAnswers.add(article);
          pushEvent({ kind: "answer_visible", length: text.length, terminal: isTerminal(article) });
        }
        // The diagnostic keeps its meaning: an answer painted while its
        // article was NOT terminal (live off the wire). One per answer.
        if (!state.seenLiveAnswers.has(article) && !isTerminal(article)) {
          state.seenLiveAnswers.add(article);
          pushEvent({ kind: "live_answer", length: text.length });
        }
      }
    }

    /** The loader is legitimate while the thread GET is in flight and no
     * answer exists yet. It is a defect when it paints over an answer OR
     * replaces one on the same thread route (the loading branch unmounts
     * the whole page, so an overlay-only test would miss that regression). */
    function scanLoading() {
      if (!document.body) return;
      const bodyText = document.body.textContent || "";
      if (!bodyText.includes(LOADING_TEXT)) return;
      if (!state.loadingOverlaySeen) {
        state.loadingOverlaySeen = true;
        pushEvent({ kind: "entry_loading_overlay" });
      }
      const sameAnswerRoute = state.answerEverSeen && state.answerPath === window.location.pathname;
      if (!state.loadingOverAnswerSeen && (anyAnswerOnScreen() || sameAnswerRoute)) {
        state.loadingOverAnswerSeen = true;
        pushEvent({ kind: "loading_over_answer", replacedAnswer: !anyAnswerOnScreen() });
      }
    }

    function scanChrome() {
      const now = Date.now();
      if (now - state.lastChromeScan < 300) return;
      state.lastChromeScan = now;
      if (!document.body) return;
      const clone = document.body.cloneNode(true);
      clone.querySelectorAll('[data-testid="turn-answer-card"]').forEach((element) => element.remove());
      // The model's own reasoning is model prose, exactly like the answer
      // card above: it is the MODEL's words, not the product's chrome. It is
      // read with the prose rule (a bare "sql" in prose is ordinary database
      // language) and a routing word next to a token still counts.
      const thinking = [...document.querySelectorAll('[data-testid="trace-thinking-full"]')];
      for (const element of thinking) {
        pushTokenEvent("model-thinking", proseTokensIn(element.textContent || ""));
      }
      // Prune the panel from the CLONE only: the live nodes are React-managed,
      // and removing one makes React's next commit throw NotFoundError and
      // unmount the SPA. The watcher measures the page; it never mutates it.
      clone.querySelectorAll('[data-testid="trace-thinking-full"]').forEach((element) => element.remove());
      pushTokenEvent("page-chrome", tokensIn(clone.textContent || ""));
    }

    function scan() {
      if (!document.body) return;
      scanRoutingSurfaces();
      scanLiveAnswer();
      scanLoading();
      scanChrome();
    }

    const observer = new MutationObserver(() => { scan(); });
    function start() {
      observer.observe(document, { childList: true, subtree: true, characterData: true });
      scan();
    }
    if (document.body) start();
    else document.addEventListener("DOMContentLoaded", start, { once: true });

    window.__finDemoWatch = {
      events: () => readEvents(),
      isTerminal: () => {
        const articles = document.querySelectorAll("article");
        if (articles.length === 0) return false;
        return isTerminal(articles[articles.length - 1]);
      },
    };
  }, {
    storageKey: options.storageKey,
    tokens: modeTokenRules(),
    routingMarkers: ["检索档", "搜索档"],
    loadingText: "加载对话中",
    terminalMarkers: ["完成", "未完成", "步数上限"],
  });
}
