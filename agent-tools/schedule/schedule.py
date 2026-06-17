#!/usr/bin/env python3
"""
schedule -- let an agent schedule its own future invocation.

Usage:
    schedule <when> <prompt>

<when> may be:
    in 30m  /  in 2 hours  /  in 1 day  /  in 3 weeks   relative to now
    today 17:00  /  tomorrow 9am  /  tomorrow            a clock time on a day
    15:00  /  9am  /  9:30pm                             today (or tomorrow if past)
    2026-06-12T15:00  /  "2026-06-12 15:00"  /  2026-06-12   explicit date-time

Quote both arguments. Examples:
    schedule "tomorrow 8am" "Tell Nicolas a joke"
    schedule "in 3 days" "Check whether the paper deadline has been extended"

The agent is invoked at the scheduled time with a prompt that includes both
the original instruction and the scheduled time, so it knows why it was run.
The target chat comes from the TELEGRAM_CHAT_ID env var the bridge injects.
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

    # explicit date-time (ISO and German formats)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"could not understand the time: {s!r}")


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: schedule <when> <prompt>", file=sys.stderr)
        return 2
    # Everything before the last argument is the time expression; the last is the prompt.
    when_str = " ".join(args[:-1])
    prompt = args[-1].strip()
    if not prompt:
        print("schedule: empty prompt", file=sys.stderr)
        return 2
    try:
        due = parse_when(when_str)
    except ValueError as e:
        print(f"schedule: {e}", file=sys.stderr)
        return 1
    if due <= datetime.now():
        print(f"schedule: that time is in the past ({due:%Y-%m-%d %H:%M})", file=sys.stderr)
        return 1

    entry = {
        "type": "schedule",
        "due": due.isoformat(timespec="seconds"),
        "prompt": prompt,
    }
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if chat:
        try:
            entry["chat_id"] = int(chat)
        except ValueError:
            pass

    out_dir = Path.cwd() / "reminders"
    out_dir.mkdir(exist_ok=True)
    fname = f"{int(due.timestamp())}_{int(time.time() * 1000) % 100000}.json"
    (out_dir / fname).write_text(json.dumps(entry), encoding="utf-8")
    print(f"Scheduled for {due:%Y-%m-%d %H:%M}: {prompt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
