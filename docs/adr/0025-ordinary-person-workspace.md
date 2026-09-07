# 0025. Ordinary-person workspace is local, sparse, and Claude Code-style in the transcript

Date: 2026-08-22
Status: accepted

The workspace (ADR 0024) is a local web UI server the person starts and opens in a browser — not hosted accounts this increment. The transcript streams thinking and tools then collapses. Burst grain (per-burst **thinking fold** / **tool fold**, tail preview on collapsed thinking) is `specs/turn-trace-folds.md` (2026-09-07). Keep only needed chrome: create, list, open, resume, delete (title from the first question); empty-thread corpus bound (the frozen 12 AAPL/MSFT/AMZN 10-Qs); `refuse` / `step_cap` as normal assistant turns with empty citations. No rename, folders, search, or other extra surfaces. Pixel layout remains deferred.
