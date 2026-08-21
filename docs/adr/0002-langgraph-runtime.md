# 0002. LangGraph is fin's application runtime

Date: 2026-08-21
Status: accepted

fin's control flow is a LangGraph `StateGraph` (explicit state, conditional edges, retry cap, optional interrupt). Retrieve algorithms stay behind the retrieve port; they are not graph internals.
