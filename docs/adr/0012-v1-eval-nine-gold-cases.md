# 0012. v1 eval gold is nine hand-authored cases in three classes

Date: 2026-08-21
Status: superseded by 0029

ADR 0003 took a thin eval slice, not the 40-question matrix. v1 gold is nine cases written by hand against the frozen 12 10-Qs after `list_files` / `retrieve` / `read_file` exist: three single-filing facts, three thin cross-period questions on 2023 Q1–Q3 only, three refuse (missing or unciteable). Do not bulk-import `data/upstream/qna_data.csv` as gold, and do not grow the harness to allowlist or broader retrieval evaluations in v1.
