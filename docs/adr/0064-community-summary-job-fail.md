# 0064. Failed community-summary jobs do not mark C ready

Date: 2026-09-17
Status: accepted

Index-time community summaries (ADR 0057) are all-or-nothing for **检索档 C**: if the summary job errors, times out, or emits empty text for any community, the job is a **typed fail**; C stays not-ready (**档未开** / index failure). The entity graph from extract+merge remains usable for **B**. Per-community retry is allowed. Partial corpora must not ship empty summaries as C hits, and query time must not backfill missing summaries (that path stays rejected in ADR 0057).

Considered Options: skip failed communities and still open C; write missing summaries on the first C question.
