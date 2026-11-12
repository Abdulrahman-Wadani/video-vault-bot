#!/usr/bin/env bash
# Writes .env without echoing secrets to the screen or shell history.
# Re-asks until each value looks valid.
set -euo pipefail
cd "$(dirname "$0")"

ask_secret() {  # $1 prompt, $2 regex
  local v
  while true; do
    read -rsp "$1: " v; echo >&2
    v="${v//[$'\r\t ']/}"
    if [[ "$v" =~ $2 ]]; then printf '%s' "$v"; return; fi
    echo "  -> That doesn't look right (got ${#v} characters). Paste it again (Ctrl+Shift+V)." >&2
  done
}

BOT_TOKEN=$(ask_secret "Bot token from @BotFather (hidden)" '^[0-9]+:[A-Za-z0-9_-]{30,}$')
echo "  token OK (${#BOT_TOKEN} characters)"
while true; do
  read -rp "api_id (numbers): " API_ID; API_ID="${API_ID//[$'\r\t ']/}"
  [[ "$API_ID" =~ ^[0-9]+$ ]] && break
  echo "  -> api_id must be numbers only."
done
API_HASH=$(ask_secret "api_hash (hidden)" '^[0-9a-fA-F]{32}$')
echo "  api_hash OK"

umask 077
cat > .env <<ENV
BOT_TOKEN=${BOT_TOKEN}
API_ID=${API_ID}
API_HASH=${API_HASH}
ALLOWED_USER_IDS=

ENV
echo "Saved .env (readable only by you)."
