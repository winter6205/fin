# Official MiniMax Chat Completions boot

## Summary

Local and cloud boots both read process env (existing `apply_dotenv` setdefault). Defaults for `FIN_MODEL` / `FIN_BASE_URL` switch from the local 9router (`fin/MiniMax-M3` @ `http://localhost:20128/v1`) to official CN Chat Completions (`MiniMax-M3` @ `https://api.minimaxi.com/v1`). The key stays `ANTHROPIC_AUTH_TOKEN` and is never defaulted or committed. Cursor `start` exports the same URL/model fallbacks so a Cloud Agent that only injects the key still hits official chat.

affects: src/fin/host/compile.py
affects: tests/host/test_compile.py
affects: .env.example
affects: scripts/cursor-start.sh
affects: docs/adr/0022-chatopenai-proxy-minimax.md
affects: docs/CONTEXT.md
affects: README.md
affects: specs/v1-host-agent.md
affects: specs/workspace.md

## 5-line verdict block

bounded-context-guardian: yes — change stays in host compile + operator boot script + env template; no new technical-layer package, no retrieve/corpus imports reversed.
defensive-contract-validator: yes — existing compile tests cover empty env (fallback), negative/override URL, missing key placeholder; literals in `.env.example` and cursor-start stay aligned with constants.
error-handling-enforcer: yes — no new catch; missing key still uses `sk-no-key-required` so official API fails loudly; checkpoint URL contract unchanged.
complexity-anti-drift: yes — only constant + comment + start-script export; no new branches or helpers.
minimal-change-verifier: yes — one logical task (official Chat Completions as the boot default), one commit, docs limited to ADR/CONTEXT/README SSOT for that default.
