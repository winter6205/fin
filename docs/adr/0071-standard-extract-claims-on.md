# 0071. Standard extract enables claims; Fast does not

Date: 2026-09-17
Status: accepted

The **standard** extract/index job **extracts claims** (time-bounded factual assertions from **抽图窗**) by default, after canonical merge / graph finalize and before Leiden and community reports. Claims hang on entities and may feed standard community-report context. They are not XBRL fact-table rows, not `get_xbrl_fact`, and not **检索档 D**. **Fast 抽图** skips claim extract. A single claim miss may skip that row; it does not fail C the way missing community summaries do (ADR 0064). Query time does not extract claims.

Considered Options: claims off until prompt-tuned (reference default); claims on Fast; claims as the numeric fact SSOT.
