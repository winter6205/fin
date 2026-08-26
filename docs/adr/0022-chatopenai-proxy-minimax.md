# 0022. ChatOpenAI against MiniMax official Chat Completions; default MiniMax-M3

Date: 2026-08-22
Updated: 2026-08-26
Status: accepted

ADR 0013 selected `init_chat_model` with default `anthropic:claude-sonnet-4-5-20250929`. Slash-style model ids are misparsed as a provider separator, so v1 constructs `ChatOpenAI(model=, base_url=)` from `FIN_MODEL` and `FIN_BASE_URL` (the URL includes `/v1`; the OpenAI SDK appends `/chat/completions`).

Default `FIN_MODEL` is `MiniMax-M3` and default `FIN_BASE_URL` is `https://api.minimaxi.com/v1` (CN official Chat Completions). The Bearer key is `ANTHROPIC_AUTH_TOKEN` in the process environment only — never committed. Overseas Chat Completions is `https://api.minimax.io/v1`. A local 9router remains an opt-in override (`FIN_BASE_URL=http://localhost:20128/v1` and `FIN_MODEL=fin/MiniMax-M3`). The historical `minimax-cn/MiniMax-M3` id returned HTTP 400 `invalid tool type (2013)` on that proxy; do not revive it as the default.

Do not use `init_chat_model` for this id, and do not put a raw vendor HTTP client in graph nodes. Anthropic Messages (`/anthropic/v1/messages`) is out of scope for this default; boot stays on Chat Completions + `ChatOpenAI`.
