#!/usr/bin/env python3
"""Send a message to the current Telegram chat from within an agent run.

Usage: chat_respond <message text>
       echo "text" | chat_respond

Routes through the bridge's internal server so that all marker processing
(buttons, inline keyboards, file sends) works exactly as in regular replies.
HTML formatting is supported: <b>bold</b>, <i>italic</i>, <code>code</code>.
Markers work too: include [[buttons: A | B]] or [[inline: A | B]] in the text.

Environment variables injected by the bridge:
  TELEGRAM_BOT_TOKEN    — identifies which bot is sending
  TELEGRAM_CHAT_ID      — destination chat
  BRIDGE_INTERNAL_URL   — http://127.0.0.1:<port> of the bridge's send endpoint
"""
import os
import sys

import requests


def main() -> None:
    token    = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id  = os.environ.get("TELEGRAM_CHAT_ID")
    bridge   = os.environ.get("BRIDGE_INTERNAL_URL")

    if not token or not chat_id:
        sys.exit("chat_respond: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read().strip()
    # Shell double-quotes don't expand \n — convert escape sequences so the
    # agent can write multi-line messages naturally without $'...' syntax.
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    if not text:
        sys.exit("chat_respond: no message text provided")

    if bridge:
        resp = requests.post(
            f"{bridge}/send",
            json={"token": token, "chat_id": int(chat_id), "text": text},
            timeout=15,
        )
        if resp.status_code != 200:
            sys.exit(f"chat_respond: bridge error {resp.status_code}: {resp.text[:200]}")
    else:
        # Fallback: direct Telegram API when bridge URL is not available
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": int(chat_id), "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            sys.exit(f"chat_respond: Telegram error: {data}")


if __name__ == "__main__":
    main()
