# 0078. Graph changes rebuild Leiden and community reports

Date: 2026-09-17
Status: accepted

When extract/merge **changes the entity-relation graph** (new 资料, invalidate, or standard/Fast re-extract), the index job **re-runs hierarchical Leiden and community summaries** so **检索档 C** matches the current graph. Incremental extract of windows/entities may finish first; C stays **not-ready** until that rebuild succeeds (ADR 0064) — do not keep serving the previous report set for that corpus version. B may use the updated graph before C is ready. A manual-only rebuild with indefinitely stale C is rejected.

Considered Options: never re-cluster after first index; serve old community reports during rebuild; rebuild only when the operator clicks.
