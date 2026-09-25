# 0084. Attachment document types are selected by host rules, not Jev

Date: 2026-09-23
Status: superseded by ADR-0089

The host classifies each attachment once while preparing it, records the result on the attachment, and selects from the closed set `text`, `pdf-text`, `pdf-scanned`, `image`, and `unsupported`. Format bytes and PDF text-layer characteristics provide the classification facts; `unsupported` fails with `attachment_type_unsupported` rather than guessing a parser. Jev does not gain a document-type Choice lane because document format is an inspectable input property, unlike the intent choices in ADRs 0080 and 0081.

ADR 0085 places this classification in the thread-scoped temporary-ingest path. ADR 0086 records when asynchronous parsing starts and how its availability and failures are exposed.

Considered Options: an additional Jev classification lane and asking the operator to classify every attachment. Both add an avoidable decision step for formats the host can inspect.

Recovery source: `docs/会话备份.txt`, lines 1203–1266, 1369–1378, and 1793–1834. Restored to this checkout on 2026-09-24.

ADR 0089 supersedes the whole-file `pdf-text` / `pdf-scanned` dispatch rule. Attachments are inspected during graph-triggered preparation, per page and region; Jev remains outside document handling.
