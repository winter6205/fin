# 0008. Unknown locator refuses; recoverable cite fails use a repair cap; step cap is a fuse

Date: 2026-08-21
Status: accepted

ADR 0005 left citation-verifier failure as retry or refuse. v1 refuses immediately when the locator is not in the frozen corpus (unknown `document_id` / `version`, or page out of range). A known locator whose quote is not a contiguous substring of that page, or a non-refuse answer with no cites, returns to the agent loop until 3 such failures, then refuse. A separate step cap of 32 is a runaway fuse whose stop reason is `step_cap`, not citation failure. Do not treat all verifier fails the same, and do not use a tight shared step cap as the cite-repair policy.
