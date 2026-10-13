#!/usr/bin/env bash
# quickstart.sh - Demonstrates shake agent pairing and messaging end-to-end
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Prefer virtualenv binaries if present, otherwise rely on PATH
if [ -f "$PROJECT_ROOT/.venv/bin/shake" ]; then
    SHAKE="$PROJECT_ROOT/.venv/bin/shake"
    SHAKED="$PROJECT_ROOT/.venv/bin/shaked"
else
    SHAKE="shake"
    SHAKED="shaked"
fi

echo "=== 1. Starting Shake Daemon in background ==="
export SHAKE_HOME="${SHAKE_HOME:-$HOME/.shake-demo}"
rm -rf "$SHAKE_HOME"
mkdir -p "$SHAKE_HOME"
"$SHAKED" &
DAEMON_PID=$!
trap "kill $DAEMON_PID 2>/dev/null || true; rm -rf $SHAKE_HOME" EXIT
sleep 0.8

echo "=== 2. Creating two agent identities ==="
ADA_ID=$("$SHAKE" agent create "Ada Lovelace" | tr -d '[:space:]')
BABBAGE_ID=$("$SHAKE" agent create "Charles Babbage" | tr -d '[:space:]')
echo "Created Ada ($ADA_ID) and Babbage ($BABBAGE_ID)"

echo "=== 3. Initiating Handshake ==="
CONN_OUT=$(SHAKE_AGENT="$ADA_ID" "$SHAKE" connect "$BABBAGE_ID")
echo "$CONN_OUT"
SAS_CODE=$(echo "$CONN_OUT" | grep -oE '[0-9]{6}' | head -n 1)
echo "Extracted SAS Code: $SAS_CODE"

echo "=== 4. Mutual Out-of-Band Verification ==="
SHAKE_AGENT="$ADA_ID" "$SHAKE" verify "$BABBAGE_ID" "$SAS_CODE"
SHAKE_AGENT="$BABBAGE_ID" "$SHAKE" verify "$ADA_ID" "$SAS_CODE"
echo "Mutual handshake complete. Both agents pinned."

echo "=== 5. Sending Authenticated 1:1 Message ==="
SHAKE_AGENT="$ADA_ID" "$SHAKE" text "$BABBAGE_ID" "Hello Charles, computing the Bernoulli numbers."

echo "=== 6. Reading Inbox as Babbage ==="
SHAKE_AGENT="$BABBAGE_ID" "$SHAKE" inbox

echo "=== 7. Reading Conversation History ==="
SHAKE_AGENT="$BABBAGE_ID" "$SHAKE" history "$ADA_ID"

echo "=== Demo completed successfully ==="
