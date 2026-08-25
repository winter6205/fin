# 0033. Model router — dispatch ChatOpenAI vs ChatAnthropic by `FIN_BASE_URL`

Date: 2026-08-25
Status: accepted

## Context

Token Plan 订阅 keys only authorize the Anthropic-compatible endpoint
on MiniMax (`/anthropic/v1/messages`, `x-api-key` header). The
existing `compile_for_runtime` / `compile_for_runtime_dev` knew only
how to wire `ChatOpenAI`, which routes to `/v1/chat/completions`
with `Authorization: Bearer`. A `POST /ask` against the prod runtime
with a Token Plan key returned `401 (1004) login fail` even when the
key was valid — the endpoint shape did not match the key's
authorization scope.

Two options were considered:

1. Force operators onto pay-as-you-go keys on the OpenAI path.
   Zero code change but breaks the subscription-key UX and the
   Token Plan procurement contract.
2. Add a router that dispatches by `FIN_BASE_URL` shape.

This ADR picks (2) so Token Plan keys work without operator action.

## Decision

`fin.host.model_router.resolve_chat_model(env)` is the single
chat-model wiring entry point. It dispatches:

- `FIN_BASE_URL` ending in `/anthropic` (case-insensitive, with or
  without trailing slash) → `ChatAnthropic`. Reads `ANTHROPIC_API_KEY`.
  No `extra_body` (Anthropic routes thinking through `betas`, not
  OpenAI-shaped extras).
- Otherwise → `ChatOpenAI`. Reads `ANTHROPIC_AUTH_TOKEN` (legacy
  alias kept for backward compat with pre-router configs) and passes
  `extra_body={thinking:{type:adaptive}, reasoning_split:true}` per
  ADR 0022 / ADR 0026.

Explicit `FIN_MODEL_PROVIDER=anthropic|openai|auto` overrides the
URL-shape detection. `auto` (and unset) defers to URL-shape.
Unknown values raise `ModelRouterError` (no silent fallback that
would surprise operators who mistyped).

`compile_for_runtime` / `compile_for_runtime_dev` delegate to the
router. Caller-supplied `model_name` / `base_url` arguments still
win — the helper merges them into the env mapping before the router
reads it, so tests pinning a URL keep working.

## Consequences

- `langchain-anthropic>=0.3` is now a runtime dependency alongside
  `langchain-openai`.
- Two parallel SDKs coexist; each builder imports its own lazily so
  a test fixture that swaps `langchain_openai` for a fake still works
  (and `langchain_anthropic` is only imported on the Anthropic path).
- `stream_map.reasoning_delta_from_chunk` reads
  `chunk.additional_kwargs["reasoning_details"]`. Under
  `ChatAnthropic` the MiniMax Anthropic-compatible endpoint may not
  populate this field; the UI's thinking region may go blank on
  that path. `/ask` main flow is unaffected — the citation gate
  reads `content`, not `reasoning_details`.
- The router does NOT detect key region. An operator whose key was
  issued on the overseas platform (`platform.minimax.io`) must
  still change `FIN_BASE_URL` to `https://api.minimax.io/anthropic`;
  the URL-shape rule does not cross-check the host against the
  issuing platform.
- `tests/host/test_model_router.py` covers the dispatch matrix
  (URL-shape auto, explicit override, `auto`, missing
  `ANTHROPIC_API_KEY` on the Anthropic path, legacy
  `ANTHROPIC_AUTH_TOKEN` alias on the OpenAI path,
  `extra_body` propagation, `FIN_MODEL` forwarding).
