# 0021. Citations are filled from gate-passed evidence, not model prose

Date: 2026-08-22
Status: accepted

Locators come from this run's `retrieve` / `read_file` results. The citation verifier, still on the path to END (not after `invoke` returns), treats a supporting span as a contiguous substring of the answer (v1 minimum 32 characters) that appears on an evidenced page. Passed spans are what the user-visible citation list contains. Recoverable failures bounce with a fixed template hidden from CLI/HTTP rendering; unknown locators still refuse immediately (ADR 0008). Do not let the model mint `document_id` / `version` / `page`, do not add a `verify` tool, and do not use an LLM to classify bounce reasons.

Amended 2026-09-04 (ADR 0046): evidenced text is the payload this run received from retrieve/read, not a second serve-time extracted tree.
