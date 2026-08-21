# 0005. Agent loop with code citation verifier; LangGraph for durability only

Date: 2026-08-21
Status: accepted

fin v1 is an agent: the model selects tools (`list_files`, `retrieve`, `read_file`; names in ADR 0009) in a bounded loop. Citation checks run in code against locators, not as a prompt. LangGraph is the host graph (loop, code gates, checkpoint, thread resume, optional interrupt). Do not implement a fixed retrieve-then-generate path as the product, and do not fork a research/RAG product repository as the codebase (host graph authored in fin: ADR 0019).
