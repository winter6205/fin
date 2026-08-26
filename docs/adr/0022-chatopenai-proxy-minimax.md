# 0022. ChatOpenAI against MiniMax official Chat Completions; default MiniMax-M3

Date: 2026-08-22
Updated: 2026-08-27
Status: accepted

ADR 0013 selected `init_chat_model` with default `anthropic:claude-sonnet-4-5-20250929`. Slash-style model ids are misparsed as a provider separator, so v1 constructs `ChatOpenAI(model=, base_url=)` from `FIN_MODEL` and `FIN_BASE_URL` (the URL includes `/v1`; the OpenAI SDK appends `/chat/completions`).

Default `FIN_MODEL` is `MiniMax-M3` and default `FIN_BASE_URL` is `https://api.minimaxi.com/v1` (CN official Chat Completions). The Bearer key is `FIN_API_KEY` in the process environment only — never committed. Overseas Chat Completions is `https://api.minimax.io/v1`. A local 9router remains an opt-in override (`FIN_BASE_URL=http://localhost:20128/v1` and `FIN_MODEL=fin/MiniMax-M3`). The historical `minimax-cn/MiniMax-M3` id returned HTTP 400 `invalid tool type (2013)` on that proxy; do not revive it as the default.

Do not use `init_chat_model` for this id, and do not put a raw vendor HTTP client in graph nodes. Anthropic Messages (`/anthropic/v1/messages`) is out of scope for this default; boot stays on Chat Completions + `ChatOpenAI`.

## Update 2026-08-27: rename key env var (hard switch)

`ANTHROPIC_AUTH_TOKEN` was the env-var name carried over from before
the MiniMax cutover — it kept the Anthropic brand even though the
runtime now talks to a non-Anthropic provider. The name confused
operators: a MiniMax key configured under `FIN_API_KEY` looked
correct, but a stale `ANTHROPIC_AUTH_TOKEN` shell variable held a
local-gateway key, so requests actually went through the legacy value
while the operator believed the new key was active.

**Decision:** rename the project-owned env var to `FIN_API_KEY` and
HARD-SWITCH — there is NO fallback to the legacy name. A missing
`FIN_API_KEY` falls through to the placeholder
`sk-no-key-required`; the provider returns 401 and the operator sees
a typed auth error, not a silent use of an unexpected credential.

The earlier draft considered a soft-cutover with `DeprecationWarning`
on the legacy name; that was rejected because the warning alone did
not stop the legacy value from being read — operators ignore stderr
during scripted boots, and the misleading "successful" boot is worse
than a loud 401.

**Surface touched (all in this commit):**

- `src/fin/host/compile.py` — `_build_default_model` reads
  `FIN_API_KEY` only. `FIN_API_KEY_ENV` exported as the public name.
- `src/fin/host/workspace_http.py` — auth-failure user copy names
  `FIN_API_KEY`.
- `.env.example` — primary key line is `FIN_API_KEY=...`. No
  deprecated-name comment.
- `README.md` — three references (scripted-lane credential removal,
  live-provider smoke requirement, exit-code 3 wording) updated to
  name `FIN_API_KEY`.
- `scripts/cursor-start.sh` — comment refers to `FIN_API_KEY`.
- `scripts/test_minimax_key.sh` — comment refers to the project-owned
  `FIN_API_KEY` (the script's own `MINIMAX_TEST_KEY` is intentionally
  a distinct name so it cannot be mistaken for the active credential).
- `specs/workspace.md` — live-provider smoke lane names `FIN_API_KEY`.
- `docs/CONTEXT.md` — agent-loop key paragraph names `FIN_API_KEY`.
- Existing tests in `tests/host/test_compile.py`,
  `test_cli.py`, `test_scripted_boot.py`,
  `test_workspace_http.py`, `test_soft_track_classify_model_injection.py`
  — adjusted to set/delete `FIN_API_KEY` instead of the legacy name.
- New `tests/host/test_compile_env.py` covers the hard-switch
  contract (only `FIN_API_KEY` is read; legacy value is ignored;
  missing key falls through to the placeholder with no warning).

**Migration (operators):**

1. Set `FIN_API_KEY=<your-key>` in `.env` / shell / CI / secret
   manager.
2. Remove `ANTHROPIC_AUTH_TOKEN` from those same places. The
   variable is no longer read; any remaining value is dead weight
   and a troubleshooting hazard.
3. No code-side fallback to remove later — the fallback was never
   shipped.

Out of scope: historical handoff docs under `docs/handoff/` and the
2026-08-26 verification report under `docs/verify/` keep their
original `ANTHROPIC_AUTH_TOKEN` text on purpose — they are
date-stamped records of what was true at the time.
