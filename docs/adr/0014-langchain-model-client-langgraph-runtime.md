# 0014. LangGraph orchestrates; LangChain is the model client and tool protocol

Date: 2026-08-21
Status: accepted

ADR 0002 made LangGraph the application runtime (how the run proceeds). That does not strip LangChain's lowest layer: v1 calls the LLM through LangChain `ChatModel` (`ChatOpenAI`; ADR 0022) and exposes tools through the LangChain messages and `bind_tools` protocol so `ToolNode` can run. Pure-function tools (ADR 0009) stay testable without a model and are wrapped at that seam. Do not replace this with a homemade vendor HTTP client, do not require `init_chat_model` as the constructor, and do not treat LangGraph as a chat SDK.
