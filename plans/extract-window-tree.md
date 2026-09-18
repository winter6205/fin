# Plan: extract window tree

**Goal:** One title-scoped **抽图窗** tree with overflow ~8% vs child overlap as separate identities.
**Approach:** Prove overlap identities with failing tests first, then pack children under the 1200-token parent without shrinking the window. Ranking and Fast extract stay out.
**Spec link:** `specs/extract-window-tree.md`
**Program:** `plans/extract-index-search-mode-program.md` wave 1
**ACR:** all-yes (from spec)

bounded-context-guardian: yes — 切分/抽图窗 stay fin corpus; retrieve/search-mode/Fast/Leiden out.
input-contract-tests: yes — Boundaries name empty / negative / overflow / concurrent / exception plus two overlap identities.
error-handling-enforcer: yes — named miss/fallback; ingest OCR EXIT reused; no second extract OCR.
complexity-anti-drift: yes — window overflow vs 父子 packing stay separate rules, not one god cutter.
minimal-change-verifier: yes — spec 1 of 4; siblings 2–4 out.

**Per-ticket loop (all bullets):** tdd → typecheck+tests → code-review → (GATE BLOCKED → review-report-repair) → verification-before-completion

## 待写入

empty

## Tasks (ordered by dependency)

1. **Short heading bodies stay one window** — tag: `[implementation]`
   - **Inherits:** ADR 0053; spec SC2 — body ≤1200 extract-model tokens → exactly one **抽图窗**, overlap 0.
   - **Surface:** `fin.corpus` 切分 / chunking
   - **Acceptance:** Fixture ≤ cap → one parent window; `uv run pytest -q` includes that case (or existing suite if already true).
   - Status: [x] done

2. **Overflow splits body only with ~8% neighbour overlap** — tag: `[implementation]`
   - **Inherits:** ADR 0053 / 0063; spec SC3.
   - **Surface:** `fin.corpus` 切分
   - **Acceptance:** Oversized heading body → ≥2 windows, same heading, adjacent overlap in the spec’s band; heading not a second body cutter.
   - `[blocks: T1]`
   - Status: [x] done

3. **Child overlap is not the window 8%** — tag: `[implementation]`
   - **Inherits:** ADR 0060 / 0063; spec SC4–SC5; narrative ≤1 sentence, tables/clauses 0.
   - **Surface:** `fin.corpus` params table + child packing
   - **Acceptance:** Same overflow fixture’s children default 0 overlap; children are substrings of one parent; density packs more/smaller children without shrinking the 1200 parent cap (SC8).
   - `[blocks: T2]`
   - Status: [x] done

4. **Deterministic 切分 + no extract OCR** — tag: `[implementation]`
   - **Inherits:** spec SC6–SC7; ADR 0054.
   - **Surface:** `fin.corpus` ingest/extract-record derivation
   - **Acceptance:** Same bytes+rule+table versions → byte-identical ids/text; extract does not call OCR or vision headings; empty/unknown triad still lands on a table row (named fallback, not model cut). Concurrent two 切分 identical. Exception: ingest `ocr_required_unavailable` unchanged.
   - `[parallel]` with T3
   - Status: [x] done
