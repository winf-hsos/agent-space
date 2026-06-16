#!/bin/sh
# Run this on the server to pull the latest code and restart the bridge.
# Usage:  ./deploy/update.sh [--no-restart]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/agent-telegram-bridge"
VENV="$BRIDGE_DIR/.venv/bin"

echo "==> Pulling latest code..."
cd "$SCRIPT_DIR"
git pull

echo "==> Installing/updating Python dependencies..."
"$VENV/pip" install -q -r "$BRIDGE_DIR/requirements.txt"

if [ "$1" = "--no-restart" ]; then
    echo "==> Skipping service restart (--no-restart)."
else
    echo "==> Restarting telegram-agents service..."
    sudo systemctl restart telegram-agents
    sleep 2
    sudo systemctl status telegram-agents --no-pager -l
fi

echo "==> Done."
