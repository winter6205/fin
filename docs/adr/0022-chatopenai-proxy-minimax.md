# 0022. ChatOpenAI against an OpenAI-compatible proxy; default fin/MiniMax-M3

Date: 2026-08-22
Status: accepted

ADR 0013 selected `init_chat_model` with default `anthropic:claude-sonnet-4-5-20250929`. Slash-style model ids are misparsed as a provider separator, so v1 constructs `ChatOpenAI(model=, base_url=)` from `FIN_MODEL` and `FIN_BASE_URL` (the URL includes `/v1`; the OpenAI SDK appends `/chat/completions`). Default `FIN_MODEL` is `fin/MiniMax-M3` because that id accepts OpenAI `function` tools; `minimax-cn/MiniMax-M3` returns HTTP 400 `invalid tool type (2013)`. Keys stay in environment variables. Do not use `init_chat_model` for this id, and do not put a raw vendor HTTP client in graph nodes.
