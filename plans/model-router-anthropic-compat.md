# Plan: Add model router so 订阅 key (Anthropic-compatible) works alongside current ChatOpenAI path

## 1. Summary

When `FIN_BASE_URL` ends in `/anthropic`, build the chat model with `ChatAnthropic`
so the MiniMax Token Plan 订阅 key (which only authorizes the Anthropic-compatible
endpoint) is accepted. Otherwise keep the existing `ChatOpenAI` path for 按量付费
keys and the legacy local proxy. Dispatch is auto by `FIN_BASE_URL` shape; an explicit
`FIN_MODEL_PROVIDER=anthropic|openai|auto` env wins.

Triggered by: a real `/ask` against the current code returned
`openai.AuthenticationError 401 (1004) login fail` even though the key was valid
— because Token Plan 订阅 keys are scoped to the Anthropic-compatible route.

## 2. Affected files (>= 3)

- `pyproject.toml` — add `langchain-anthropic>=0.3`
- `src/fin/host/model_router.py` (new) — `resolve_chat_model` + typed errors
- `src/fin/host/compile.py` — `_build_default_model` delegates to the router
- `tests/test_model_router.py` (new) — 5 boundary classes + positive paths
- `.env.example` — comment blocks for both key shapes
- `docs/CONTEXT.md` — add router section + dispatch rule
- `docs/adr/0033-model-router-anthropic-compat.md` (new) — record the decision

## 3. 5-line verdict block

bounded-context-guardian: yes — router lives in `fin.host.*` next to `compile.py` /
`stream_map.py` / `graph.py` (host is the chat-model wiring context per ADR 0014).
No new top-level context, no reverse dep on `corpus/` or `retrieve/`.

defensive-contract-validator: yes — tests cover empty (base_url=None → default
ChatOpenAI), negative (FIN_MODEL_PROVIDER=bogus → ModelRouterError), overflow (N/A,
no size limits in router), concurrent (N/A, router is process-local pure function
called once at boot), exception (ANTHROPIC_API_KEY missing + base_url=.../anthropic
→ typed ModelRouterError; reasoning_details absent under ChatAnthropic is logged
once and falls through to inline-strip, not a 500).

error-handling-enforcer: yes — `ModelRouterError` is a typed exception
(HostCompileError subclass), never returns None/silent fallback on unknown
base_url or provider/key mismatch. Router raises before any saver or graph
work — failures fail fast at boot, not at first `/ask`.

complexity-anti-drift: yes — one function `resolve_chat_model` + 2-3 small
helpers (`_is_anthropic_compat`, `_resolve_provider`); each helper stays at one
abstraction level; no god-function or god-module planned; expected < 100 LoC.

minimal-change-verifier: yes — 1 logical task ("route by base_url"), 1 commit.
Dep + router + tests + docs land together because the router without docs is
not onboarding-safe (the dispatch rule must be discoverable).

## 4. Cross-skill conflicts

None. bounded-context (no new context) and minimal-change (one task) agree.

## 5. Known risks (recorded, not blocking)

- `stream_map.py:reasoning_delta_from_chunk` reads
  `chunk.additional_kwargs["reasoning_details"]` — OpenAI-flavored field name.
  Under ChatAnthropic the same field MAY not be populated; if so the UI's
  thinking region goes blank, but `/ask` main flow is unaffected (citation
  gate reads `content`, not `reasoning_details`). Verification step covers this.
- `ChatAnthropic` defaults to `anthropic-version: 2023-06-01`; MiniMax
  Anthropic-compatible endpoint is documented to accept it.
- `MiniMax-M3` model id is reused across OpenAI and Anthropic paths per
  MiniMax docs; no model-id translation needed.
