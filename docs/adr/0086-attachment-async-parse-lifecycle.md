# 0086. Thread attachments parse and index asynchronously after upload

Date: 2026-09-24
Status: accepted

After a thread attachment is stored and classified, the host starts parsing and temporary indexing asynchronously. The attachment is `parsing` until its parsed text and index are usable, then `ready`; on failure it becomes `failed` with a named reason and can be retried. A query that requires a still-parsing attachment receives `attachment_not_ready`, while unrelated queries may use other available sources.

For image and scanned-page parsing, selected failure reasons are `render_failed`, `vision_call_failed`, and `vision_empty_text`; unsupported types are rejected earlier under ADR 0084. Parsed page text is the shared thread-scoped source for `retrieve` and `read_file`, rather than a separate lazy page cache. The model-facing tool contract remains a separate G4 decision.

ADRs 0087 and 0088 settle that G4 contract as bounded `read_source` with typed `unit` locators; the asynchronous preparation and failure states above remain in force.

Considered Options: parse on first read or question, which avoids work for unused attachments but makes the first dependent question wait for parsing and indexing. Background parsing after attach spends resources earlier and requires visible readiness and retry states.

Decision source: operator acceptance of the G3 Wayfinder recommendation on 2026-09-24. This decision narrows the earlier proposal's `attachment_not_ready` rule to questions that require a pending attachment.
