# 0068. Fast extract is an operator switch; AI may suggest, never silent-apply

Date: 2026-09-17
Status: accepted

**Fast 抽图** vs **standard** is chosen by an explicit **operator switch** on the index job (joint map; default **standard**). A model may **propose** Fast (choice window: Fast cheaper / noisier B vs standard) but must not flip the method without a pick. Ambiguous or no pick keeps standard. This is not a per-turn **检索档** and not a hidden query router (ADR 0067).

Considered Options: silent model-chosen extract method; config-only with no UI switch; query-time Fast.
