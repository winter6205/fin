# 0026. Workspace streams on the sync host graph; resume is node-complete

Date: 2026-08-22
Status: accepted

ADR 0024 required streaming HTTP on the same compiled host graph; ADR 0015 still forbids `ainvoke` / `AsyncPostgresSaver` on the durable path. The workspace send path is POST `text/event-stream` wrapping sync `stream()` (threadpool), with `model_node` token-streaming. MiniMax-M3 thinking uses Chat Completions `extra_body` (`thinking.type` adaptive|disabled, `reasoning_split`) plus a converter so checkpointed messages replay `reasoning_details`; the citation gate sees thinking-stripped prose. Crash resume continues the same `run_id` from the last complete host-graph node (full model message including `tool_calls`, or full `ToolMessage`s) — it does not regenerate a completed node and does not delete the user turn. Do not use native EventSource as the send transport, and do not treat a mid-token buffer as a save point.
