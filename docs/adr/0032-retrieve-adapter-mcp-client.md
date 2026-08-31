# 0032. Retrieve adapter is MCP 2.0 client behind the port; Fake default; no lockstep memory repo

Date: 2026-08-25
Status: accepted

Filing retrieve stays behind the **retrieve port**. Default pytest uses a Fake port. When configured, the product adapter is an MCP 2.0 client (initialize → tools/call), protocol-testable against a compliant public or fixture server — not a joint release with any memory-server repo. In-process BM25 (ADR 0007) remains archived, not a fallback. Hits map to fin locators; the cite tree stays in fin; remote ranking/chunking stay outside fin. Host graph unchanged. Supersedes ADR 0030's binding of the product adapter exclusively to **iknow-memory** as a lockstep path; that server remains a later optional backend.

Amended 2026-08-31 (ADR 0037): the chunking clause above is re-ruled at the clause level — chunking is owned by **fin** and the retrieval service does not re-split pushed text; only remote ranking stays outside fin. The rest of this record's decisions are unchanged.

Considered Options: keep ADR 0030 exclusive iknow-memory binding (couples progress); restore BM25 as product fallback (two ranking stacks); implement hybrid/RRF inside fin (duplicates memory-server work).
