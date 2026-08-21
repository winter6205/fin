# 0005. Agent loop with code citation verifier; LangGraph for durability only

Date: 2026-08-21
Status: accepted

fin v1 is an agent: the model selects tools (`list_filings`, `retrieve`, `read_locator`) in a bounded loop. Citation checks run in code against locators, not as a prompt. LangGraph supplies checkpoint, thread resume, and optional interrupt. Do not implement a fixed retrieve-then-generate path as the product, and do not fork another agent repository as the codebase.
