# 0013. FIN_MODEL at runtime; CLI first; HTTP shares the compiled graph

Date: 2026-08-21
Status: accepted

The agent-loop chat model is `init_chat_model` selected by `FIN_MODEL` (default `anthropic:claude-sonnet-4-5-20250929`). API keys are environment variables only. The first ship binary is the CLI; v1 also mounts HTTP on the same compiled host graph and calls sync `invoke`. Swapping the in-process BM25 retrieve adapter for an iknow-memory MCP client stays post-v1, after this eval slice can run, and is not a v1 ticket. Do not put a raw vendor HTTP client in graph nodes (use the LangChain model client), and do not ship two graphs for CLI vs HTTP.
