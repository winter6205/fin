# 0025. Ordinary-person workspace is local, sparse, and Claude Code-style in the transcript

Date: 2026-08-22
Status: accepted

The workspace (ADR 0024) is a local web UI server the person starts and opens in a browser — not hosted accounts this increment. The transcript follows a Claude Code web UI pattern: thinking streams when the model emits it and then collapses; tool calls are visible while running and collapse after. Consult that UI for the collapse/stream feel; do not copy it, and do not tell fin's product story as based on it. Keep only needed chrome: create, list, open, resume, delete (title from the first question); empty-thread corpus bound (the frozen 12 AAPL/MSFT/AMZN 10-Qs); `refuse` / `step_cap` as normal assistant turns with empty citations. No rename, folders, search, or other extra surfaces. Pixel layout remains deferred.
