# 0006. v1 locator is page identity; quote span is not the locator

Date: 2026-08-21
Status: accepted

The current locator contract is recorded in ADR 0028/0029. This ADR records historical PDF-page identity only, not a second current path.

fin v1 cites with `{document_id, version, page}`: `document_id` from the corpus manifest, `version` that file's sha256, `page` the 1-based pdfplumber page. The model’s claimed span is a quote string checked as a contiguous substring of the frozen page text. Do not put `chunk_id`, character offsets, or bbox in the locator. Chunks are retrieve slices that carry this locator as `source_ref`; several chunks may share one page. Do not treat a whole page as the only allowed retrieve grain.
