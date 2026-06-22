#!/usr/bin/env python3
"""Save task state so the bridge resumes it on the next user reply.

Usage:
  task_pause "What you were doing and what still needs doing"
  task_pause --done    # clear saved state when the task is complete

The bridge injects the saved state into the next prompt automatically,
so the agent wakes up knowing exactly where it left off.

TELEGRAM_CHAT_ID must be set (injected by the bridge). The state file
is per-chat so multiple users talking to the same bot don't interfere.
"""
import os
import sys
from pathlib import Path


def main() -> None:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "default")
    task_file = Path(f".task_{chat_id}")

    args = sys.argv[1:]

    if args == ["--done"]:
        task_file.unlink(missing_ok=True)
        return

    text = " ".join(args) if args else sys.stdin.read().strip()
    if not text:
        sys.exit("task_pause: provide state text, or --done to clear")

    task_file.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
