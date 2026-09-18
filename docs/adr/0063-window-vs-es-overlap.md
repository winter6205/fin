# 0063. Window overflow overlap is not ES child overlap

Date: 2026-09-17
Status: accepted

**抽图窗** overflow (ADR 0053) keeps about **8%** overlap only between adjacent windows of the same heading body. **父子** children indexed for lexical/ES search are a second identity: default **no overlap**; narrative rows in the density table may overlap **at most one sentence**; table rows and atomic clauses stay 0. Children remain contiguous substrings of their parent window (ADR 0060). Do not copy the 8% onto ES keys, and do not use a global overlap percent for both layers. Frozen locator text is the source string, not a third overlap policy.

Considered Options: one overlap percent for windows and ES; ES also 8%; ES 10–15% word overlap as if the child were the LLM context.
