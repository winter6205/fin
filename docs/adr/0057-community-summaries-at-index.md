# 0057. Community summaries are written at index time

Date: 2026-09-17
Status: accepted

After community clustering (ADR 0056), each community’s **summary text is generated and stored during indexing**, not on the first C 综述 question. Query-time C reads those rows. Leaving clusters without summaries is rejected. Generating the summary only when C is asked is rejected (cost and an incomplete index).

Considered Options: first C question writes the summary; store cluster ids only.
