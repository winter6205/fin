# SQL date-input contract: how a user request supplies the expiry window

Basis: the tracked requirement recorded in `docs/handoff/2026-10-02-pre330-repairs.md:20` ("Define and implement the user-facing SQL date-window path before attempting B3 … the adapter requires `sql_window_start` and `sql_window_end`; the graph currently supplies neither … do not introduce an implicit date window"), inheriting `specs/search-mode-business-sql-contract.md` §2.3 and §9 leg-1. The task text itself ("T1 — SQL: settle how user requests supply the date window") lives in `plans/pre330-remaining-prerequisites.md`, which is tracked on the `docs/pre330-remaining-plan` branch and is deliberately not carried into this branch; the tracked handoff line above states the same requirement. Recorded 2026-10-02 on branch `pre330/s1-sql`, revised the same day after the operator ruled the seam. T1 is a CONTRACT decision: it fixes the behavior, not the UI, so a later implementation may present the same contract through different controls. B3 is not claimed here and was not executed.

## The decision

A user's request supplies the inclusive window through **two explicit, model-visible tool arguments on the `retrieve` tool: `sql_window_start` and `sql_window_end`**. The model fills them on the tool call by copying the dates the user stated. A complete pair — both bounds, spelled in full — is required, and it is required together with `search_mode="sql"`.

The window is never a defaulted, inferred, or product-chosen quantity.

| # | Decision | Consequence at the seam |
| --- | --- | --- |
| 1 | The model supplies the pair as two tool arguments, `sql_window_start` / `sql_window_end`, on `retrieve`. | One pair, both bounds, copied from what the user said. |
| 2 | A complete inclusive pair is required, together with `search_mode="sql"`. | Both bounds or neither; a single bound is refused. |
| 3 | Missing, incomplete, malformed, non-ISO, or reversed bounds are refused with the named token `invalid_business_query` (existing token, existing operator copy at `src/fin/host/stream_map.py:777`), **before any transport round trip**. Ambiguous and relative requests are NOT in this list: nothing in the code can observe them, so they are instruction-dependent (see the taxonomy below). | A named refusal, never a silent default and never an implicit window. |
| 4 | **Relative dates are out of scope.** | "the next 90 days" is not accepted, see below. |
| 5 | **No date may be inferred** from the current date, locale, or timezone. | The window is only ever what the user wrote. This constraint is load-bearing — see below. |
| 6 | Inclusive semantics are exact at the SQL call. | `WHERE expiry_date BETWEEN $1::date AND $2::date`, `$1` = start, `$2` = end, both inclusive. |
| 7 | The arguments' **descriptions are part of the contract surface**. | The model must be told how to fill them, or rule 5 has no enforcement. |

### No inference, and why the argument text is load-bearing (rule 5 → rule 7)

Because the model now supplies these values, the constraint "no date may be inferred from the current date, locale, or timezone" is not a formality — it is the only thing standing between a user question and a well-formed, entirely fictional date range. A model that supplies "today through today + 90 days" produces a request that is syntactically perfect, binds cleanly, and answers a question nobody asked.

The `retrieve` tool description therefore states, as contract and not as a hint, that the model must:

- copy the user's own stated dates and change nothing;
- pass both bounds or neither;
- **omit both** when the user named no dates, named them relatively, or named more than one range — letting the named refusal fire;
- never substitute today's date, a relative expression, or a plausible default.

This is the mitigation for the seam's known weakness. The 检索档 is host-owned and deliberately absent from the model's arguments; the window deliberately is not, by operator ruling. That asymmetry is a considered trade — a model that can read dates off a user's own sentence is a better path to a real ledger answer than a host-side extractor that has to guess which dates in a sentence bound a window — and the cost is that the guard against an invented window is instructional rather than structural.

**What rule 3 bounds, stated exactly.** `resolve_sql_window` proves a window is *malformed or unbindable*: it rejects a missing bound, a bound that is not a spelled ISO calendar day, and `end < start`. It cannot prove a window is the window the operator meant. A model that invented a **well-formed** pair — `"2026-01-01"` … `"2026-04-30"` for a user who named no dates — passes every check, binds cleanly, and answers a question nobody asked. That is a known limit of a model-supplied window, not a guarantee the code makes: **no rule here refuses an invented-but-bindable window**, and no test claims to. The rule bounds the failure to an unanswerable request (a named refusal) rather than preventing the fabricated one; only the model obeying the instruction stands between the two.

### Why relative dates are out of scope (deciding by not supporting them)

The plan-branch task text's clause — *"if relative dates are supported, define their timezone and mapping"* — is satisfied by declining to support them, which is a decision rather than a deferral. Supporting "the next 90 days" requires two things that do not exist in this codebase:

- a **defined timezone**, so that "today" resolves to one calendar day rather than to a per-request instant that shifts with the host clock or the caller's locale; and
- a **defined mapping** from the relative phrase onto a closed date-pair, including how a phrase crossing a month or year boundary is folded.

Neither exists. There is no date-parsing dependency in `pyproject.toml` and no relative-date handling anywhere in `src/fin/`. Adding one to satisfy the phrase would be a new capability with its own contract, not a reading of this one. A relative request is therefore not answered with a window fin chose: the model is instructed to omit both, and the request is then refused **by name** under rule 3 as a missing window — the same honest outcome. What the code guarantees is only that half; a model that converts the phrase itself would supply a well-formed pair that binds (see the taxonomy below for that boundary).

The cost is explicit: an operator must state absolute dates. That is accepted in exchange for a window that is never one the operator did not ask for.

### Refusal taxonomy (rule 3)

The gate is the existing `resolve_sql_window` (`src/fin/retrieve/port.py`), invoked pre-transport at the adapter seam; it is the single owner of what a bindable window is, and it is neither duplicated nor weakened here. It sees exactly two strings and the assigned 档 — nothing else about the request. So the shapes divide by **what the code can actually observe**, not by what is undesirable.

**PORT-ENFORCED — reaches `resolve_sql_window` and is refused `invalid_business_query` before any transport round trip.** All five are the same named outcome and the same actionable message — state the two required dates — regardless of which mistake produced it. They are not separate tokens by choice, not by omission.

| Shape | Example | Why refused |
| --- | --- | --- |
| missing | "which contracts expire?" — model omits both | no window was named |
| incomplete | start only | one bound is not a window |
| malformed | `"later"`, `"2026-01-32"` | not a spelled ISO calendar day the ledger binds |
| non-ISO | `"20260101"`, `"01/01/2026"` | not the `YYYY-MM-DD` shape the producer casts |
| reversed | `2026-04-30` … `2026-01-01` | `end < start` is not a question |

Reversal and ordering are deliberately **not** silently repaired: sorting a reversed pair into a valid range would answer a question the operator did not ask, which is the same defect as defaulting a window.

**INSTRUCTION-DEPENDENT — no code mechanism observes these, so they are NOT refused by name.** They are listed here because the contract depends on them, not because the port enforces them. `resolve_sql_window` receives two strings; it cannot see how many ranges the user named, and it has no clock, timezone, or relative-date parser to compare against (rule 4 declines to add one).

| Shape | Example | What actually happens |
| --- | --- | --- |
| ambiguous | two separate ranges in one request | nothing is refused. If the model copies one of them, that well-formed pair binds and runs; if the model obeys the instruction and omits both, the row above refuses it as *missing*. |
| relative | "in the next 90 days" | nothing is refused. There is no mapping from the phrase to a day (rule 4), so an obedient model omits both and the request is refused as *missing*; a model that converts the phrase itself supplies a well-formed invented pair that binds. |

The honest statement of the boundary: the token that reaches the operator for a relative or ambiguous request is `invalid_business_query` **as a missing window**, and the difference between "refused" and "answered with a window fin chose" is made by the model, not by this gate. No test in this branch claims otherwise.

### Inclusive semantics (rule 6)

The pair the model supplies is what the adapter binds, unmodified, as `filters.sql_window_start` / `filters.sql_window_end`. The producer's own permitted template (`contracts_expiring`, `specs/search-mode-business-sql-contract.md:201`) is `WHERE expiry_date BETWEEN $1::date AND $2::date` with `$1` = start and `$2` = end, and `BETWEEN` is inclusive on both ends in SQL. A contract expiring exactly on the start day or exactly on the end day is inside the window. No day is added or dropped in translation, and the values are not re-formatted, re-ordered, or normalized on the way. A document 档 resolves the window to `None`, so the keys never ride a document wire even when the model passed them.

## Implementation consequence

T2 wires this contract to the real producer path: the two arguments are added to the `retrieve_tool` signature (so LangChain derives them into `tool.args` and the model sees them), each carrying an `Annotated` description with the rule 7 guidance so the guidance rides **in the tool-call schema next to the argument the model is filling in**, and the rule as contract in the tool docstring. Both arguments ride the ONE existing `retrieve` port call. Validation stays where it already was — `resolve_sql_window` is still the only gate, still pre-transport. No new date-parsing dependency, no second parser, no weakened ISO-day validation, no additional `retrieve` tool surface, no `SendRequest` field (the model fills these on the tool call, so no HTTP surface change is needed), no `AgentState` change, and no `real_db_not_wired` routing gate — the merged producer path already exists and the routing table is empty by design.

## Verification

All three rows were run against **producer `e8165a704c7b0e733744c01de77322622e3be03a`** (`e8165a7`), `dist/index.js` sha256 `1beb2c1b550a868b0a0d5a349578ce22c2a469bc15cbb2ab73e6fd23c60510f6`. The fin revision at run time was the pre-squash commit `9fa5840`; the branch was then squashed, and `src/` and `tests/` are byte-identical between `9fa5840` and the squashed head, so these results transfer unchanged. Host is a 4-CPU / 7.6 GB WSL2 box; the full default suite was run separately on the squashed tree.

| # | Command | Result | Exit |
| --- | --- | --- | --- |
| 1 | `uv run --no-sync pytest -q tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` | `75 passed, 27 deselected in 0.24s` | 0 |
| 2 | `uv run --no-sync pytest -q -m soft_track tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_sql_wire.py` | `27 passed, 75 deselected in 1.70s` | 0 |
| 3 | `uv run --no-sync pytest -q -m sql_live tests/retrieve/test_live_sql_window_user_path.py` | `1 passed in 1.61s` | 0 |

Reading row 1 vs row 2: the bare command deselects the whole host file, which is expected and not a gap — that file is `soft_track` and the bare run selects the hermetic `test_sql_wire.py` gate set. The same 27 host cases are the ones that carry the window contract, so row 2 is the row that proves the host seam.

Reading row 3: the real producer was reached on ES `127.0.0.1:59200` (the `fin-pre330-es` container; the default `9200` is dead on this host — the pre330 mapping is the only one that answers) against the seeded dev ledger. Producer `e8165a7` is the merged head, built in a session-owned worktree at `/home/winner/projects/iknow-wt-s1r` and selected with `FIN_LIVE_IKNOW_ROOT`; the shared `iknow-memory` checkout was not modified. Row 3 is one invocation: the graph is compiled over the real `MCPRetrieveAdapter`, and the asserted row envelope is that call's answer, not a second query.

Marker routing, checked by collection rather than asserted in prose:

| Command | Collection |
| --- | --- |
| `pytest -q -m sql_live --collect-only tests/retrieve/test_live_sql_window_user_path.py` | `1 test collected` |
| `pytest -q -m mcp --collect-only tests/retrieve/test_live_sql_window_user_path.py` | `no tests collected (1 deselected)` |

Fail-closed behaviour of row 3's own guards, observed by breaking each precondition on purpose (both restored afterwards; the ledger run is the one where a missing prerequisite must be a named failure, not a skip):

| Broken precondition | Observed |
| --- | --- |
| `FIN_BUSINESS_LEDGER_URL` unset | `Failed: FIN_BUSINESS_LEDGER_URL is unset: the live sql-window user-path acceptance is blocked, not skipped` — exit 1 |
| producer `dist/index.js` moved aside | `Failed: the pinned iknow producer build is missing under /home/winner/projects/iknow-wt-s1r; the live sql-window user-path acceptance is blocked, not skipped` — exit 1 |

`uv run --no-sync pyright src/fin/host/graph.py tests/host/test_assigned_mode_reaches_port.py tests/retrieve/test_live_sql_window_user_path.py` → `3 errors`, all three pre-existing on this branch (`_CapturingMcpAdapter` passed where `_compile` annotates `FakeRetrievePort`, lines 306/323/343). None added by this slot.

## What this record does NOT prove

- It does not prove the browser journey (B3 owns it, `plans/post321-real-sql.md`); no B3 work was executed or claimed here.
- It does not prove the query-template allowlist is complete, or that a view's predicate is the intended scope.
- It does not say anything about the real ledger's own access controls; the seeded development ledger is not the operator's system of record.
- It does not prove a live LLM fills these arguments correctly. The tested model turn is the suite's `ScriptedChatModel` double, so what is proven is the **arg plumbing** — a tool call's two arguments through the real transport to the real producer's row envelope, in one invocation — and not model behavior in the field. The transport and the producer are real; only the model is a double. Rule 5's guard is instructional, and no automated test can prove an instruction is obeyed.
- The relative-date limitation in rule 4 is a scope boundary, not a defect fixed later in this slot; a future plan wanting relative dates must bring the timezone and the mapping with it.
