# 0085. Attachment text uses a thread-scoped temporary index

Date: 2026-09-23
Status: accepted

For the grounded knowledge assistant, attachment processing parses file content into text and builds a thread-scoped temporary index. The answering model uses `retrieve` to find relevant attachment text and `read_file` to inspect the source text; that text can enter this-run citation evidence under the existing verifier. Raw attachments, parsed text, and temporary index entries are removed with the thread and are never automatically pushed into the permanent company corpus. Explicit corpus ingest remains the separate door defined by ADR 0047.

This revises ADR 0083's original page-only attachment reading path while retaining its binary upload, logical `attachment_id`, and thread lifetime. ADR 0084's type selection now feeds attachment preparation. ADR 0086 records parsing start time, availability states, and failure behavior.

ADRs 0087 and 0088 supersede this record's historical `read_file` name and generic page locator with bounded `read_source` and typed `unit` locators; thread-scoped search and attachment lifetime remain in force.

Considered Options: direct page browsing without an attachment index and adding attachments to the permanent corpus. Direct browsing leaves long documents difficult to locate within; permanent ingest changes the attachment's intended lifetime and corpus boundary.

Recovery source: `docs/会话备份.txt`, lines 1793–1834 and 1902–1929. Restored to this checkout on 2026-09-24.
