#!/usr/bin/env python3
"""
remind -- let an agent schedule a one-shot reminder.

Usage:
    remind <when> <message>

<when> may be:
    in 30m  /  in 2 hours  /  in 1 day  /  in 3 weeks   relative to now
    today 17:00  /  tomorrow 9am  /  tomorrow            a clock time on a day
    15:00  /  9am  /  9:30pm                             today (or tomorrow if past)
    2026-06-12T15:00  /  "2026-06-12 15:00"  /  2026-06-12   an explicit date-time

Quote both arguments. Examples:
    remind "in 2 hours" "Take the cake out of the oven"
    remind "tomorrow 9am" "Email the grant draft to Sabine"

The reminder is written as a small JSON file into ./reminders/ (relative to the
current working directory, which the bridge sets to the calling bot's folder).
The bridge's scheduler delivers it on Telegram at the due time and deletes it.
The target chat comes from the TELEGRAM_CHAT_ID env var the bridge injects, so
the reminder goes back to whoever asked for it.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def try_parse_clock(s):
    """Parse a clock time like '17:00', '9am', '9:30pm' -> (hour, minute) or None."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip().lower())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_when(s: str) -> datetime:
    """Turn a human time expression into an absolute, server-local datetime."""
    s = s.strip()
    low = s.lower()
    now = datetime.now()

    # "in 2 hours", "in 30m", "in 1 day", "in 3 weeks"
    m = re.fullmatch(r"in\s+(\d+)\s*([a-z]+)", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit in ("m", "min", "mins", "minute", "minutes"):
            return now + timedelta(minutes=n)
        if unit in ("h", "hr", "hrs", "hour", "hours"):
            return now + timedelta(hours=n)
        if unit in ("d", "day", "days"):
            return now + timedelta(days=n)
        if unit in ("w", "week", "weeks"):
            return now + timedelta(weeks=n)
        raise ValueError(f"unknown time unit: {unit!r}")

    # "today 17:00", "tomorrow 9am", "tomorrow" (defaults to 09:00)
    m = re.fullmatch(r"(today|tomorrow)(?:\s+(.+))?", low)
    if m:
        base = now if m.group(1) == "today" else now + timedelta(days=1)
        hour, minute = (try_parse_clock(m.group(2)) or (None, None)) if m.group(2) else (9, 0)
        if hour is None:
            raise ValueError(f"could not understand the time: {m.group(2)!r}")
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # bare clock: "15:00", "9am" -> today, or tomorrow if already past
    clock = try_parse_clock(low)
    if clock:
        cand = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand

    # explicit date-time
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"could not understand the time: {s!r}")


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: remind <when> <message>", file=sys.stderr)
        return 2
    # Everything before the last argument is the time expression (so an unquoted
    # "in 2 hours" still works); the last argument is the message.
    when_str = " ".join(args[:-1])
    message = args[-1].strip()
    if not message:
        print("remind: empty message", file=sys.stderr)
        return 2
    try:
        due = parse_when(when_str)
    except ValueError as e:
        print(f"remind: {e}", file=sys.stderr)
        return 1
    if due <= datetime.now():
        print(f"remind: that time is in the past ({due:%Y-%m-%d %H:%M})", file=sys.stderr)
        return 1

    entry = {"due": due.isoformat(timespec="seconds"), "text": message}
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if chat:
        try:
            entry["chat_id"] = int(chat)
        except ValueError:
            pass

    out_dir = Path.cwd() / "reminders"
    out_dir.mkdir(exist_ok=True)
    # One file per reminder => no shared-file write race with the scheduler.
    fname = f"{int(due.timestamp())}_{int(time.time() * 1000) % 100000}.json"
    (out_dir / fname).write_text(json.dumps(entry), encoding="utf-8")
    print(f"Reminder set for {due:%Y-%m-%d %H:%M}: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
