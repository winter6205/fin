# Spec: Soft-Track judge routing

**Status:** Adopted QA-chain routing amendment, 2026-10-16; implementation and live acceptance are not claimed.

## Objective

Retain rules-primary classification on the existing four tracks (`dialogue`, `clarify`, `off_topic`, `grounded`). Jev is an optional closed-set hint on the ambiguous tier; missing, failed, malformed or low-confidence hints fall back to authorized retrieval. The operator adopted this change to ADR 0027/0080 on 2026-10-16.

## Boundaries

- **Does:** Preserve the routing fast paths and existing valid inheritance, consume a usable Jev hint, and record `route_source=rule|jev|fallback` separately from graph track and answer outcome.
- **Confirms with human:** The four presented QA-chain design groups were accepted. No new provider, second classifier or additional content-policy rules are selected.
- **Out of this spec:** Ranking, a new Track, model-based citation checking, evidence refusal before retrieval, or turning operational failure into clarification. The [canonical task contract](question-task-runtime.md), [shared wire](question-task-runtime-contract.md), and [routing/grounded contract](qa-chain-grounded-experience.md#routing-requirements) retain their respective authorities.

## Routing contract

Rules that already decide a route do not call Jev. Catalog discovery enters overview on the grounded path without financial slots. An ordinary knowledge question can retrieve without a company, period or document choice. Preserve the current `0.6` confidence threshold, finite `[0,1]` confidence and four-label validation for a usable hint; Jev's existing 20-second call timeout remains a bounded hint budget, separate from the five-second retrieval deadline.

Jev absence, exception, malformed/unknown output, out-of-range confidence or confidence below that threshold selects `route_intent=retrieve`, `track=grounded`, `route_source=fallback`. It does not claim missing evidence, choose another text-model classifier or manufacture a clarification. A `clarify` hint alone is insufficient: the canonical Runtime must identify an actionable user-held missing condition; otherwise retrieve. A genuine clarification retains its underlying intent (`retrieve` for a knowledge task), not a fifth product route. `off_topic` remains a soft capability-boundary response under chat, never a policy refusal.

Mandatory system/model policy and source authority checks remain. An explicit refusal under existing policy may set the `policy_refuse` disposition; permission/dependency errors, failed citation admission and evidence insufficiency keep their own meanings. Document ambiguity after retrieval uses Runtime's pending question and accepted reply, not the pre-retrieval clarify leaf.

## Success Criteria

- Rules resolve a greeting/catalog request with zero Jev calls and the existing appropriate leaf/product intent.
- A high-confidence valid fake Jev hint chooses its existing feasible track; a `clarify` hint without an actionable missing condition retrieves.
- Missing Jev, an exception, malformed label or low confidence on an ordinary corpus question attempts authorized retrieval with `route_source=fallback` and no fabricated evidence refusal.
- Existing policy/source checks still run; off-topic, policy refusal, dependency failure and evidence refusal remain distinct.
- Relevant `uv run pytest -q -m soft_track` checks pass after implementation. Configured Jev-path changes additionally require actual `jev_live` round-trip evidence; the deliberately missing-Jev product path is independently tested. A Fake pass is never a live pass.

## Open Questions

None for T0. Actual model/backend/browser outcomes belong to the existing execution and acceptance gates.

## Inherits / Changes

Inherits ADR 0027/0061's four tracks, rules-first orchestration and grounded-only tools/code citation verifier ([ADR 0082](../docs/adr/0082-citation-verifier-code-only.md)). Changes the old low-confidence/judge-down-to-clarify clause to retrieval fallback, with the trade-off recorded in the adopted ADR 0080/0027 amendments. No task/source authority or integrated acceptance gate is replaced.

**Selected persist list:** Adopt the ADR 0027/0080 amendments and reconcile the existing Soft-Track vocabulary through domain-modeling after fresh ACR; no new term beyond the selected route intent.

## Architecture Change Review

Fresh review is recorded with the three concrete QA-chain specs and program map. Earlier routing reviews do not certify these amended contracts; product tests remain Not run in this documentation-only task.
