# 0076. Fast noun-phrase regex is mixed Chinese/English, not English-only

Date: 2026-09-17
Status: accepted

**Fast 抽图** extracts noun-phrases with a **regex (and related NLP) tuned for mixed Chinese and English**, not the English-only `regex_english` default. Shipping that English-only extractor as the product Fast default is rejected; a Chinese 资料 run must not silently yield an empty co-occurrence graph. spaCy or another parser may be added later; v1 Fast still has to work on 中英窗 text. **Standard** LLM extract is unchanged.

Considered Options: English-only regex as Fast default; Fast allowed only on English 资料; Fast requires spaCy before any regex work.
