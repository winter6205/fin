# 0028. Live EDGAR cite unit is official primary HTML, not PDF page

Date: 2026-08-25
Status: accepted

Live 10-Q/10-K source of record is the official primary iXBRL `.htm`; `version` hashes those bytes; the verifier still checks a contiguous substring of one frozen locator unit this run evidenced (ADR 0021). Product vs eval trees share this grain (ADR 0029); ADR 0006 is only historical PDF-page identity, not a second live path. Do not ingest unofficial PDFs as live identity, do not verify HTML quotes against PDF pages, and do not treat catalog-miss / on-demand fetch as a new host-graph node in this increment (later topology work under ADR 0027). How HTML is partitioned into `unit` is not this ADR.

Considered Options: PDF-only live ingest (coverage collapses when EDGAR ships no PDF); dual HTML+PDF locators in one gate (cross-render false pass/refuse).
