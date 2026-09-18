# 0054. Extract records are derived at extract time; OCR uses frozen write-back text

Date: 2026-09-17
Status: accepted

Extract does not persist a second section/row table at ingest. At extract time, records are derived from already frozen locator text: 资料主体, section title, body or table row, column names as fields (not extract-body text). HTML/Markdown headings are the title source. Extract does not consume retrieve **父子** child blocks (ADR 0055). Scanned or textless pages stay on the existing ingest OCR path (`OCREngine` write-back into the frozen unit, typed `ocr_required_unavailable` on failure). Extract reads that **same frozen text** as the body field — it does not OCR again and does not use a vision model to invent headings. If the frozen OCR page has no heading tags, title may be empty and overflow 1200-token splits still share that unit as source plus 资料主体 (ADR 0052 / 0053). Live EDGAR identity remains official HTML (ADR 0028); OCR is for image/no-text-layer 资料, not a second live 10-K path. Ingest `TesseractEngine` default language is `chi_sim+eng` so mixed Chinese/English scans are in-contract; the OS must ship those tessdata files.

Considered Options: skip OCR 资料 for extract (silent coverage hole); layout/LLM headings on OCR pages (non-deterministic cut); dual HTML+PDF extract of one filing (ADR 0028 already rejected for cites).
