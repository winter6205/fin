# 0044. Session compaction by token occupancy; `/compact` runs the same gate

Date: 2026-09-04
Status: accepted

Plan-default message-count windows are rejected. Occupancy is `count_tokens_approximately` on the model-facing payload versus a 256_000 estimated-token session budget (one constant; the operator may change it later). At ≥90% occupancy the host summarizes oldest complete tool pairs, writes the summary plus latest **status snapshot** slots, `RemoveMessage`s evicted ids from the checkpointer, and stops near 40% occupancy. `/compact` is intercepted before Soft-Track classify and runs that same gate without waiting for 90%. The summarizer is an injectable seam defaulting to the **v1 model**. Occupancy may map onto the open thread UI; it must not appear on `GET /api/status` (ADR 0033). `thread_store` turn rows are not the compact target. Trim-without-delete is not compaction. Summary failure is typed and must not refuse the operator turn.

Considered Options: message-count sliding window (looks unlike a context budget; discarded); trim_messages only (checkpoint stays fat); vendor 1M window as the 90% denominator (compaction never fires on ordinary research threads); a separate small summarizer at v1 (swap later via the seam instead).
