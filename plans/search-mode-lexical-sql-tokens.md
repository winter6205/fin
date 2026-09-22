# Plan: search-mode lexical / sql (word-form tokens, zero letter coexistence)

**Goal:** The lasting closed set on both wires becomes `lexical | graph | global | sql`; every bare letter (`A`/`B`/`C`/`D`) is retired outright — no alias map, no widened entry-input set.
**Approach:** iknow swaps its enum first (the server must never refuse a token the host sends). fin then retokenizes SSOT → entries → port/live. The operator locked the spec 2026-09-22 with an explicit override: pre-release, no live clients, so retired-spelling growth to 4 aliases (spec assumption 4) and `TIER_INPUT_TOKENS` widening (assumption 10) are **cancelled**, and the iknow retire ticket is **merged into T1**. Existing `B`/`C` aliases are deleted in the same stroke.
**Spec link:** `specs/search-mode-lexical-sql-tokens.md` (+ sibling `../../iknow-memory/specs/search-mode-lexical-sql-tokens.md`)
**ACR:** all-yes (pre-implementation gate 2026-09-22, after operator lock)

bounded-context-guardian: yes — both sides change only their own SSOT leaf; port→leaf direction unchanged.
input-contract-tests: yes — empty / negative / overflow / concurrent / exception classes per spec Boundaries, retargeted to "letters are now refused everywhere".
error-handling-enforcer: yes — `invalid_search_mode` / `tier_not_open` / `real_db_not_wired` keep meanings; refusal stays typed, never empty `chunks[]`.
complexity-anti-drift: yes — constant tables + regex alternation; values-only, no identifier churn.
minimal-change-verifier: yes — one wire vocabulary swap; assignment cookbook, confidence floor, graph cache untouched.

**Per-ticket loop (all bullets):** tdd → tests (+ live where named) → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion.

## Operator rulings closing the spec's open questions

- OQ1: moot — no widening, no narrowing window; letters leave the input enum in the same commit that swaps the tuple.
- OQ2: `filters.c_drop_windows` stays out of this increment (its own graph-global ticket owns it).
- OQ3: values-only — `RETRIEVE_TIERS` / `LASTING_RETRIEVE_TIERS` / `RetrieveTier` / arm identifiers keep their names; only their VALUES move to word forms.
- OQ4: ADR 0081 gets one token line inside this round as a separate docs commit; `docs/CONTEXT.md` stays untouched in the main checkout (operator working copy owns it).

## Tasks (ordered by dependency)

External blocker: **T1 (iknow)** — fin T3's live leg may not run green before T1 is at least branch-complete locally.

1. **T1 — iknow: enum swap + full letter retire** — repo `../iknow-memory`, branch `feat/search-mode-word-tokens`, tag `[implementation]`
   - `SEARCH_MODES` → `["lexical","graph","global","sql"]`; `SEARCH_MODE_ALIASES` and the alias branch of `parseSearchMode` deleted (B/C included); `SEARCH_MODE_ARMS` dispatch keys retitled (`lexical`/`sql`); wire copy quotes the four word forms only.
   - Acceptance: `npx vitest run` on search-mode / defensive / retrieve / arm-fuse suites exits 0; `A`/`B`/`C`/`D` → typed `invalid_search_mode`; missing key still `graph`; typecheck/build script exits 0.
   - Status: [x] done (`feat/search-mode-word-tokens`, iknow that pull request; expand and retire in one ticket)

2. **T-fin-1 — host SSOT + usage copy + criteria glosses rekey** — fin branch stacked on `feat/jev-criteria-slot`, tag `[implementation]`
   - `tiers.py`: tuple → `("lexical","graph","global","sql")`; `TIER_LABELS` keys retitled (labels unchanged); `RETIRED_TIER_TOKENS` deleted; `TIER_INPUT_TOKENS` / `TierInputToken` deleted with it (the repair round removed the middle man rather than leaving it equal to `RETRIEVE_TIERS`); `TIER_UNAVAILABLE_CODE` keys → `global`/`sql`; `_TIER_BY_SPELLING` loses letter rows.
   - `agent_instructions.SEARCH_MODE_USAGE_COPY` keys + 查找/真数据库 copy spell `filters.search_mode=lexical` / `=sql`.
   - `_JEV_MODE_TIER_GLOSSES` / `_JEV_MODE_INSTRUCTIONS` and the gray-zone pins rekey off the SSOT tuple (they are derived — the rename must ride the derivation, not re-spell lists).
   - Acceptance: hermetic `tests/retrieve` + `tests/host` green; the KeyError pin still fires if a tier lacks its gloss.
   - `[blocks: T1]` (for the live leg only; hermetic work may proceed)
   - Status: [x] done (`2532d9d`)

3. **T-fin-2 — entries, regex, send door** — tag `[implementation]`
   - `_TIER_NAMING_RE`: `lexical`/`sql` alternatives ahead of the label group; `[ABCD]` letter class deleted (zero-coexistence ruling — an `A档` utterance is now ordinary prose the judge reads, not a named tier).
   - `workspace_http` `tiers` enum is `list[RetrieveTier]` over the four word forms (a letter send is therefore a typed schema refusal — accepted by the operator as no-client-was-live); the pin takes the already-typed value directly, and choice-window ids plus the audit stay word-form; fast-propose ids ride the SSOT.
   - Acceptance: an `A档`-naming utterance resolves no lasting token by regex; every entry surface that once mapped a letter now has a named test for its refusal or judge-lane path.
   - Status: [x] done (`0f9c49e`)

4. **T-fin-3 — port closed set + live driver** — tag `[implementation]`, live 必选
   - `resolve_search_mode` exact set `lexical|graph|global|sql`; `InvalidSearchModeError` message lists it; Fake/mcp adapter record the accepted token; `test_live_search_modes_pg_es.py` params → `lexical`/`sql` and the letter parametrization asserts all four are typed `invalid_search_mode` on the port.
   - Acceptance (against T1-branch iknow): `uv run pytest -m mcp tests/retrieve/test_live_search_modes_pg_es.py` exits 0; `test_live_jev_tier_reaches_wire.py` + `tests/host -m jev_live` exit 0 with the judge landing `lexical`/`sql` wire tokens.
   - `[blocks: T-fin-1, T-fin-2]`
   - Status: [x] done (`a298034`; live legs re-run against the T1-branch build)

5. **T-docs — ADR 0081 token line** (separate docs commit on the fin rename branch) + this plan and the two spec files committed with status `locked`.
   - Status: [x] done (`92c9934`)

## End of round

One code-review phase per branch (criteria lane and rename lane are reviewed separately — they are separate PRs), then live verification, push, stacked PRs awaiting merge. The iknow retire is NOT a later ticket — it is T1. iknow's own `plans/` entry may be opened by the T1 owner if the repo convention requires one.

Landed state: `LASTING_RETRIEVE_TIERS` is `("lexical","graph","global","sql")` and is the ONLY 档 vocabulary on either wire — `RETIRED_TIER_TOKENS`, `TIER_INPUT_TOKENS` and `TierInputToken` are deleted, the send door is `tiers: list[RetrieveTier]`, and a bare-letter send is refused at the door as well as at the port. The utterance pattern is derived from the SSOT instead of spelling it. Default hermetic gate: `uv run pytest tests/retrieve tests/host -q` → 2289 passed, 47 skipped; the marker-owned opt-in classes are re-run explicitly, and after the repair round they measure `-m soft_track tests/host` → 202 passed, `-m soft_track` on the four touched host files → 103 passed, `npx vitest run` in `web/` → 308 passed (the rename's own `src/choiceWindow.test.ts` contributing 12). Live gate against the iknow branch build (`FIN_LIVE_IKNOW_ROOT=…`): `-m mcp tests/retrieve/test_live_search_modes_pg_es.py` → 12 passed (real ES answers `lexical` with the parent 抽图窗 body, `sql` refuses `real_db_not_wired` before the wire, all four letters typed `invalid_search_mode`), `-m "mcp or jev_live" tests/retrieve/test_live_jev_tier_reaches_wire.py` → 1 passed (the judged 档 reaches the real server, `label=graph confidence=0.950`), `-m jev_live tests/host/test_live_jev_track.py tests/host/test_live_jev_search_mode.py` → 8 passed (`raw` / `decide_search_mode` 0.940, seam #2 0.620, `opened_tier` 0.940 — every 档 leg above the 0.6 floor with labels unchanged — plus the track-lane A/B probe arms at 0.860 / 0.840). T1's own gate ran in the iknow repo (`npx vitest run` on the search-mode / defensive / retrieve / arm-fuse suites, that pull request).

The review round's own contribution was mostly documentary: the spec had been committed with the zero-coexistence override only in its header while its body still promised the alias map, the wider input tuple and the letter class. Those passages are now rewritten to the shipped contract (or named as cancelled), the open questions are closed with the operator's rulings, the ACR footer carries the verdict it actually returned, and the older specs and ADRs 0055 / 0056 / 0059 / 0062 got a one-line spelling pointer each instead of a rewrite. Its two code fixes: the derived utterance pattern reads its labels through the module's own `MODE_LABELS` alias (built once at import), and `named_failures.unavailable_tier` compares the door-typed selector exactly instead of normalizing a case/padding shape no seam produces any more — so the door's 422 body naming the closed set is now the tested contract for what a selector may spell.
