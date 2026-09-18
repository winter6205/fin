# 0053. Extract windows are title-scoped; overlap only inside an oversized body

Date: 2026-09-17
Status: accepted; superseded-by ADR-0060 §retrieve hit grain only

**抽图窗** is one method: each extract call sees the current heading plus a body span. If that heading’s body is at most 1200 tokens (extract-model tokenizer), the whole body is one window and overlap is zero. If the body is larger, the same heading is kept and only the body is split to the 1200 cap with about 8% overlap — the overflow branch of the same rule, not a second splitter. Table rows and clauses are title-scoped bodies that almost always fit in one window. This ADR does not choose **检索档** hit grain (ADR 0055). **资料主体** injection stays ADR 0052.

Considered Options: never overlap (split every long section to sentences only); document-long 1200-token slide with no heading reset; using retrieve parent/child blocks as extract windows.
