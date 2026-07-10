#!/bin/sh
# First-time server setup after git clone.
# Run once as the user that will run the bridge.
# Usage:  ./deploy/setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/agent-telegram-bridge"

echo "==> Creating Python virtual environment..."
python3 -m venv "$BRIDGE_DIR/.venv"
"$BRIDGE_DIR/.venv/bin/pip" install -q --upgrade pip
"$BRIDGE_DIR/.venv/bin/pip" install -r "$BRIDGE_DIR/requirements.txt"

echo "==> Setting executable bit on tool wrappers..."
find "$SCRIPT_DIR/agent-tools" -name "remind" -not -name "*.py" -not -name "*.cmd" \
    -exec chmod +x {} \;

echo "==> Creating agent runtime directories..."
for agent_dir in "$SCRIPT_DIR/agents"/*/; do
    mkdir -p "$agent_dir/reminders" "$agent_dir/inbox"
done

echo ""
echo "==> Next steps (manual):"
echo "  1. cp $BRIDGE_DIR/.env.example $BRIDGE_DIR/.env"
echo "     nano $BRIDGE_DIR/.env          # add real bot tokens and OPENAI_API_KEY"
echo "  2. cp $BRIDGE_DIR/bots.yaml.example $BRIDGE_DIR/bots.yaml"
echo "     nano $BRIDGE_DIR/bots.yaml     # set workdir paths for this machine"
echo "  3. Install the systemd service:"
echo "     sudo cp $BRIDGE_DIR/telegram-agents.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now telegram-agents"
echo ""
echo "==> Setup complete."
