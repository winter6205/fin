# 0070. C searches community-report text with lexical and vectors in parallel

Date: 2026-09-17
Status: accepted

**检索档 C** retrieves **community** summary rows written at index time (ADR 0057 / 0059): lexical/ES on the report text runs in parallel with the report embedding (ADR 0058). Hits are those reports; the path may then drop to citable **抽图窗**. C must not become “ES the windows and ignore reports.” In-window **父子** children stay A’s hit keys (ADR 0060 / 0063).

Considered Options: C vectors only; C BM25 on windows with reports as fallback.
