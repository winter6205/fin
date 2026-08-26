#!/usr/bin/env bash
# test_minimax_key.sh — probe a MiniMax API key against all four
# endpoint paths MiniMax publishes, in both regions and both protocols.
#
# Prints HTTP status + MiniMax error body but NEVER the key itself.
# Reads the key from the MINIMAX_TEST_KEY env var (deliberately a
# distinct name so the script does not silently use the project-owned
# FIN_API_KEY or anything in your .env).
#
# Usage:
#   MINIMAX_TEST_KEY=sk-... ./scripts/test_minimax_key.sh
#
# Exit codes:
#   0  at least one endpoint returned HTTP 200
#   1  every endpoint returned non-200 (key is rejected everywhere
#      OR every endpoint timed out / was unreachable)
#   2  missing MINIMAX_TEST_KEY

set -u
set -o pipefail

if [[ -z "${MINIMAX_TEST_KEY:-}" ]]; then
  echo "usage: MINIMAX_TEST_KEY=<key> $0" >&2
  exit 2
fi

# Local copy in a less-echoed variable. Avoid using $KEY in any
# command line that ends up in `set -x` output or a transcript.
K="$MINIMAX_TEST_KEY"
MODEL="MiniMax-M3"
PROMPT="Reply with one word: ok"

probe() {
  local label="$1" url="$2" auth_kind="$3"
  local -a hdr
  hdr=(-H "Content-Type: application/json")
  case "$auth_kind" in
    bearer)
      hdr+=(-H "Authorization: Bearer ${K}")
      ;;
    xapikey)
      hdr+=(-H "x-api-key: ${K}" -H "anthropic-version: 2023-06-01")
      ;;
    *)
      echo "internal: unknown auth_kind $auth_kind" >&2
      return 1
      ;;
  esac

  echo
  echo "=== $label ==="
  echo "URL  : $url"
  local raw http_status body
  raw=$(curl -sS -m 15 \
        -w $'\n__HTTP_STATUS__:%{http_code}' \
        "${hdr[@]}" \
        -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"${PROMPT}\"}],\"max_tokens\":32}" \
        "${url}" 2>&1) || true
  http_status=$(printf '%s' "${raw}" | sed -n 's/^__HTTP_STATUS__://p')
  body=$(printf '%s' "${raw}" | sed '/^__HTTP_STATUS__:/d')
  echo "STATUS: ${http_status}"
  # Truncate body; MiniMax error envelopes are short. Never echo K.
  echo "BODY  : $(printf '%s' "${body}" | head -c 400)"

  case "${http_status}" in
    200)
      echo "VERDICT: OK — this endpoint accepts the key"
      return 0
      ;;
    401)
      echo "VERDICT: REJECTED — MiniMax says login fail (key not valid for this endpoint)"
      return 1
      ;;
    403)
      echo "VERDICT: REJECTED — 403 forbidden (region / scope / quota)"
      return 1
      ;;
    404)
      echo "VERDICT: NOT_FOUND — model or route does not exist at this URL"
      return 1
      ;;
    429)
      echo "VERDICT: RATE_LIMITED — 429 (the key works but quota is hit)"
      return 1
      ;;
    000)
      echo "VERDICT: NETWORK_ERROR — could not reach the host"
      return 1
      ;;
    *)
      echo "VERDICT: UNKNOWN — see body (HTTP ${http_status})"
      return 1
      ;;
  esac
}

overall=0

probe "1/4 CN Chat Completions (Bearer)" \
      "https://api.minimaxi.com/v1/chat/completions" \
      bearer || overall=1

probe "2/4 Overseas Chat Completions (Bearer)" \
      "https://api.minimax.io/v1/chat/completions" \
      bearer || overall=1

probe "3/4 CN Anthropic Messages (x-api-key)" \
      "https://api.minimaxi.com/anthropic/v1/messages" \
      xapikey || overall=1

probe "4/4 Overseas Anthropic Messages (x-api-key)" \
      "https://api.minimax.io/anthropic/v1/messages" \
      xapikey || overall=1

echo
echo "============================================================"
echo "How to read the four verdicts:"
echo "  - 1 OK    → 按量付费 key on CN OpenAI Chat Completions"
echo "  - 2 OK    → 按量付费 key on Overseas OpenAI Chat Completions"
echo "  - 3 OK    → Token Plan 订阅 key on CN Anthropic Messages"
echo "  - 4 OK    → Token Plan 订阅 key on Overseas Anthropic Messages"
echo
echo "If ONLY one row returned OK, that is the exact URL+protocol"
echo "your key is scoped for. Set FIN_BASE_URL to that URL in your"
echo ".env (without the trailing /chat/completions or /v1/messages)"
echo "and fin.host.model_router will pick the right SDK class"
echo "(ChatOpenAI or ChatAnthropic) automatically."
echo
echo "If NO row returned OK:"
echo "  - The key may be expired / revoked / quota-exhausted."
echo "  - Or it was issued for a product other than these four (e.g."
echo "    a closed Codex-only scope, or a beta model id)."
echo "  - Confirm on platform.minimaxi.com (CN) or platform.minimax.io"
echo "    (overseas) under Interface Key / Token Plan that the key is"
echo "    active and matches one of the four row labels above."
echo "============================================================"

exit "${overall}"
