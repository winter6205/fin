# 0019. Host graph is authored in fin

Date: 2026-08-22
Status: accepted

ADR 0010 took an external ReAct template as the loop skeleton. v1 authors the host graph in this repo instead: model node, tool node, citation verifier on the path to END, and the step cap in fin state. Compile with the in-process checkpointer in fin; do not assume Studio injects it. Map `recursion_limit` separately from the 32-step cap. Do not switch runtime to OpenAI Agents, Pydantic AI, smolagents, or AutoGen, and do not copy an external agent tree into `src/fin`.
