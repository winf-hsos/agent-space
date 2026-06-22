#!/usr/bin/env python3
"""Send a message to the current Telegram chat from within an agent run.

Usage: chat_respond <message text>
       echo "text" | chat_respond

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment variables
injected by the bridge. Useful for proactive/scheduled runs that need to send
multiple separate messages during a single invocation.

Parse mode is HTML — wrap names in <b>…</b>, use &lt; and &gt; for literal
angle brackets. Emoji work as-is.
"""
import os
import sys

import requests


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("chat_respond: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read().strip()

    if not text:
        sys.exit("chat_respond: no message text provided")

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
