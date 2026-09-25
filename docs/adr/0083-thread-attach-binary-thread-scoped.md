# 0083. Thread attachments use binary upload, thread-scoped storage, and logical IDs

Date: 2026-09-23
Status: accepted

Workspace **thread attach** accepts binary files through the existing attachment door, stores them under fin's thread-scoped ownership, and gives the model a logical `attachment_id` rather than a filesystem path. Deleting the thread removes its attachments; attaching never pushes content into the permanent company corpus. This keeps file access bounded to a host-resolved identity and preserves the separate corpus-ingest door from ADR 0047.

The original choice exposed attachment text through `read_file` page reads. ADR 0085 revises that reading path to thread-scoped temporary indexing with `retrieve` and `read_file`; the upload, storage, addressing, and corpus boundaries above remain in force. ADRs 0087 and 0088 replace that historical model-facing read name and generic page locator with bounded `read_source` and typed `unit` locators.

Considered Options: model-visible paths, direct attachment-body injection into the prompt, and a second binary upload door. These would respectively expose filesystem access, bypass explicit evidence reads, or duplicate the existing attachment door.

Recovery source: `docs/会话备份.txt`, lines 68–84, 202–284, and 1793–1834. Restored to this checkout on 2026-09-24.
