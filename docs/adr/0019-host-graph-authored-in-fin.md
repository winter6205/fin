# 0019. Host graph is authored in fin

Date: 2026-08-22
Status: accepted

fin authors and compiles its LangGraph host graph, with a model node, tool node, citation verifier on the path to END, and a step cap in fin state. The graph uses an in-process checkpointer. LangGraph's `recursion_limit` is configured separately from the 32-step agent-loop cap.
