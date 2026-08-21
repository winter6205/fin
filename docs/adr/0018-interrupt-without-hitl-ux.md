# 0018. Interrupt may exist on the graph; v1 ships no HITL UX

Date: 2026-08-21
Status: accepted

LangGraph `interrupt` may be compiled into the host graph so a later approval UI can attach. The v1 CLI and HTTP do not expose approve/reject. Crash resume of the in-flight run is the durability path. Do not require LangGraph Studio as the v1 UI, and do not ship plan-approval as v1.
