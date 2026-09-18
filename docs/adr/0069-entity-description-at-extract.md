# 0069. Entity descriptions are summarized in the standard extract job

Date: 2026-09-17
Status: accepted

On the **standard** extract path, after **canonical merge** of that index run (duplicate check **is** that merge: normalized same name and type, then type and embedding gates — ADR 0052; not a second pass after descriptions exist), the job **summarizes each survivor entity’s (and relationship’s) description list into one string** before Leiden and community reports. That string is what ADR 0058 embeds. **Fast 抽图** skips this step. Trigger is the **index/extract job** (full run, or incremental re-summarize of entities whose description list changed) — not ingest/push, not query, not session memory/dream, not a second nightly “整理” unless that job _is_ the extract job. A single entity’s summary failure skips that description (no vector for that node); it does not fail the whole index the way C summaries do (ADR 0064). Query time must not write descriptions.

Considered Options: query-time biography; every-turn memory extract; describe only after Leiden; fail the whole index on one entity summary miss.
