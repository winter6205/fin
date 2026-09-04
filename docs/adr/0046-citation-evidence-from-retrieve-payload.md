# 0046. Citation evidence is this-run retrieve/read payload; no second serve-time cite tree

Date: 2026-09-04
Status: accepted

The **citation verifier** stays a host code gate on the research path to END (ADR 0021): a supporting span is a contiguous substring of the answer (v1 minimum 32 characters) that appears in text this run actually received from **retrieve** / **read_file**. That text is the body returned through the **retrieve port** (and the read tool), typically from **iknow-memory** after ingest/push (ADR 0045). Serve-time product must not keep a second on-disk extracted tree as a parallel company-corpus SSOT for the gate. Ingest still freezes and **切分**s in fin, then pushes; chunks remain byte-identical to what was pushed (ADR 0037). The gate does not move into iknow-memory, and a source marker without a matching span is not a citation.

Considered Options: keep a serve-time extracted tree solely for the gate (second copy of the same pushed bytes; discarded); verify only that an answer names a `source_ref` (no substring check; discarded); run the substring gate inside the retrieve MCP (host would no longer own the product refuse path; discarded).
