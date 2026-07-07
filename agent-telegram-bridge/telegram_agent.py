"""
Multi-bot Telegram -> OpenCode bridge.

Runs several Telegram bots from a single process. Each bot is backed by its own
OpenCode agent: its own working directory (with its own AGENTS.md), its own
allowed users, and its own session behavior. Bots are declared in agents.yaml;
secrets (tokens) stay in environment variables / a .env file.

Design:
  - One *poller* thread per bot long-polls Telegram (each token needs its own
    getUpdates consumer) and enqueues messages.
  - One *worker* thread per bot processes that bot's messages strictly in order
    -- which keeps `continue` sessions and wiki writes consistent.
  - A single global semaphore (`concurrency` in agents.yaml) caps how many agents
    run at the same time across ALL bots. This is the key guard on small
    hardware like a Raspberry Pi 5.

Setup:
  1. npm install -g opencode-ai   &&   opencode auth login
  2. Create each bot via @BotFather; put its token in .env.
  3. pip install -r requirements.txt
  4. Edit agents.yaml, then run:  python telegram_agent.py

Env vars (user-set):
  <token_env>           - one per agent, named in agents.yaml
  AGENTS_CONFIG         - optional, path to agents.yaml (default: next to this file)
  OPENCODE_BIN          - optional, explicit path to opencode executable
  BRIDGE_INTERNAL_PORT  - optional, port for internal HTTP server (default: 7861)

Env vars injected into agent subprocesses:
  TELEGRAM_CHAT_ID      - current chat ID
  TELEGRAM_BOT_TOKEN    - this bot's token (for chat_respond)
  BRIDGE_INTERNAL_URL   - http://127.0.0.1:<port> (for chat_respond routing)
"""

import ast
import contextlib
import html
import json
import os
import queue
import re
import sys
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

try:
    from croniter import croniter  # for cron `schedules` in agents.yaml
except ImportError:
    croniter = None

load_dotenv()  # read variables from a .env file next to this script

BRIDGE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", BRIDGE_DIR / "agents.yaml"))
# Shared toolbox put on every agent's PATH (see run_agent), e.g. the `remind`
# command. Kept OUTSIDE the bridge folder on purpose: agents have shell access,
# so anything reachable via PATH must not sit next to the .env with the tokens.
TOOLS_DIR = Path(
    os.environ.get("AGENT_TOOLS_DIR", BRIDGE_DIR.parent / "agent-tools")
).resolve()
# Shared instructions appended to every agent (via each workdir's opencode.json
# `instructions`). Maintain once here; all agents pick it up. See
# ensure_shared_instructions().
SHARED_INSTRUCTIONS = TOOLS_DIR / "shared" / "agents-common.md"
TELEGRAM_LIMIT = 4096
RUN_TIMEOUT = 600  # seconds an agent may work on a single message
REMINDER_PREFIX = "Erinnerung"  # bold label prepended to delivered reminders

# Agent invocation logging. Set AGENT_LOG=1 in .env to enable.
# Logs full prompt + raw opencode output per run to logs/<agent>.log
AGENT_LOG = os.environ.get("AGENT_LOG", "0").strip().lower() in ("1", "true", "yes")
AGENT_LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", BRIDGE_DIR / "logs"))

# Voice transcription (OpenAI Whisper API). Needs OPENAI_API_KEY in the env.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")

# Caps concurrent agent runs across all bots; replaced in main() from the config.
AGENT_SLOTS = threading.Semaphore(1)

# Internal HTTP server for chat_respond tool routing (localhost only).
# chat_respond POSTs here so the bridge can process markers before sending.
INTERNAL_PORT = int(os.environ.get("BRIDGE_INTERNAL_PORT", "7861"))
BRIDGE_INTERNAL_URL: str | None = None  # set in main() after server starts
_bots_by_token: dict = {}               # token → Bot, populated in main()
_history_lock = threading.Lock()        # serialises all history file writes
_chat_responded: dict = {}             # (token, chat_id) → bool; reset each run
_chat_responded_lock = threading.Lock()


def _resolve_opencode():
    """Locate the opencode executable, preferring the real binary over shims.

    On Windows, shutil.which() returns npm's opencode.cmd/.ps1 shim. Running a
    .cmd routes through cmd.exe, which treats a newline in an argument as a
    command separator and truncates multi-line prompts at the first line. The
    shim itself just launches a real opencode.exe, so we resolve to that exe
    (mirroring the shim's own path) to pass arguments untouched.
    """
    override = os.environ.get("OPENCODE_BIN")
    if override:
        return override
    found = shutil.which("opencode")
    if found and os.name == "nt" and found.lower().endswith((".cmd", ".bat", ".ps1")):
        exe = Path(found).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if exe.exists():
            return str(exe)
    return found


OPENCODE = _resolve_opencode()


# --------------------------------------------------------------------------- #
# OpenCode
# --------------------------------------------------------------------------- #

def extract_answer(stdout: str) -> str:
    """Reconstruct just the assistant's final answer from opencode's JSONL events.

    `opencode run --format json` emits one JSON event per line: step_start,
    tool_use, text, step_finish, error. The reply lives in `text` events. A model
    often writes text BEFORE/BETWEEN tool calls ("I'll set the reminder...") and
    again AFTER it ("Done, I'll remind you..."), which would otherwise reach the
    chat as two messages. So we keep only the text emitted after the LAST tool
    call -- the actual conclusion -- and fall back to all text when no tool ran.
    Each part id is deduped to its latest snapshot; distinct parts join with a
    blank line so boundaries don't glue words together.
    """
    parts: dict = {}      # part id -> latest text snapshot
    order: list = []      # part ids in first-appearance order
    last_tool_pos = -1    # how many text parts had appeared when the last tool ran
    errors = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        part = event.get("part") or {}
        if etype == "text":
            text = part.get("text")
            if text:
                pid = part.get("id", f"_{len(order)}")
                if pid not in parts:
                    order.append(pid)
                parts[pid] = text
        elif etype.startswith("tool"):
            last_tool_pos = len(order)  # text appearing after this is the conclusion
        elif etype == "error":
            errors.append(str(part.get("text") or event.get("error") or "unknown error"))

    # Prefer text emitted after the final tool call; if there is none, use all of
    # it (covers plain answers with no tool, or a tool with no trailing text).
    final = [pid for pid in order[last_tool_pos:] if parts[pid].strip()] if last_tool_pos >= 0 else []
    if not final:
        final = [pid for pid in order if parts[pid].strip()]
    answer = "\n\n".join(parts[pid].strip() for pid in final).strip()
    if not answer and errors:
        return "[agent error] " + " ".join(errors)
    return answer


def _log_invocation(agent_name: str, chat_id: int | None,
                    prompt: str, stdout: str, stderr: str, duration: float) -> None:
    """Append one agent run to logs/<agent_name>.log (no-op when AGENT_LOG is off)."""
    if not AGENT_LOG:
        return
    AGENT_LOG_DIR.mkdir(exist_ok=True)
    log_file = AGENT_LOG_DIR / f"{agent_name}.log"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bar = "=" * 72
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{bar}\n")
        fh.write(f"{ts}  agent={agent_name}  chat={chat_id}  duration={duration:.1f}s\n")
        fh.write(f"{bar}\n")
        fh.write("PROMPT\n------\n")
        fh.write(prompt.rstrip() + "\n")
        fh.write("\nOUTPUT (raw opencode JSONL)\n---------------------------\n")
        fh.write((stdout or "(empty)").rstrip() + "\n")
        if stderr and stderr.strip():
            fh.write("\nSTDERR\n------\n")
            fh.write(stderr.strip() + "\n")


def run_agent(workdir: Path, keep_context: bool, prompt: str,
              model: str | None = None, timeout: int = RUN_TIMEOUT,
              chat_id: int | None = None,
              token: str | None = None,
              agent_name: str = "agent") -> str:
    """Run one prompt through the opencode CLI and return only the final answer."""
    if OPENCODE is None:
        return "opencode CLI not found on PATH. Run: npm install -g opencode-ai"
    cmd = [OPENCODE, "run", "--format", "json"]
    if keep_context:
        cmd.append("-c")  # resume previous session => the agent remembers context
    if model:
        cmd += ["-m", model]  # provider/model, e.g. anthropic/claude-sonnet-4-6
    cmd.append(prompt)
    # Build PATH: shared tool subfolders (agent-tools/*) for all agents,
    # plus workdir/tools/ for agent-local tools (e.g. food-specific scripts).
    env = os.environ.copy()
    tool_dirs = []
    if TOOLS_DIR.exists():
        tool_dirs += sorted(
            str(d) for d in TOOLS_DIR.iterdir()
            if d.is_dir() and d.name != "shared"
        )
    local_tools = workdir / "tools"
    if local_tools.is_dir():
        tool_dirs.insert(0, str(local_tools))  # agent-local tools take priority
    if tool_dirs:
        env["PATH"] = os.pathsep.join(tool_dirs) + os.pathsep + env.get("PATH", "")
    if chat_id is not None:
        env["TELEGRAM_CHAT_ID"] = str(chat_id)
    if token is not None:
        env["TELEGRAM_BOT_TOKEN"] = token
    if BRIDGE_INTERNAL_URL:
        env["BRIDGE_INTERNAL_URL"] = BRIDGE_INTERNAL_URL
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",  # opencode emits UTF-8; don't decode as cp1252 on Windows
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _log_invocation(agent_name, chat_id, prompt, "", "", time.monotonic() - t0)
        return f"Agent timed out after {timeout}s."
    _log_invocation(agent_name, chat_id, prompt,
                    result.stdout or "", result.stderr or "",
                    time.monotonic() - t0)
    answer = extract_answer(result.stdout or "")
    if answer:
        return answer
    err = (result.stderr or "").strip()
    return f"[agent error]\n{err}" if err else "(no output)"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send(api: str, chat_id: int, text: str, parse_mode: str | None = "HTML",
         buttons: list[str] | None = None,
         reply_keyboard: "list[str] | str | None" = None,
         inline_buttons: list[str] | None = None) -> None:
    """Send a reply, splitting on Telegram's 4096-char limit.

    Defaults to HTML so agents can use <b>bold</b>, <i>italic</i>, <code>text</code>.
    Falls back to plain text automatically if Telegram rejects the formatting.
    buttons attaches a one-time reply keyboard to the final chunk.
    reply_keyboard sets/replaces the persistent bottom keyboard; pass 'remove' to dismiss it.
    """
    text = text or "(no output)"
    # Strip unsupported <a href="tel:..."> links — Telegram only accepts http/https.
    # Replace with the visible link text so the number still appears.
    text = re.sub(
        r'<a\s+href=["\']tel:[^"\']*["\'][^>]*>(.*?)</a>',
        r'\1', text, flags=re.IGNORECASE | re.DOTALL
    )
    reply_markup = None
    if inline_buttons:
        # All labels in a single row; callback_data prefixed so poller can route them.
        reply_markup = {"inline_keyboard": [[
            {"text": lbl, "callback_data": f"inline:{lbl}"}
            for lbl in inline_buttons
        ]]}
    elif buttons:
        rows = []
        for i in range(0, len(buttons), 3):
            rows.append([{"text": b} for b in buttons[i:i+3]])
        reply_markup = {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True}
    elif reply_keyboard == "remove":
        reply_markup = {"remove_keyboard": True}
    elif reply_keyboard:
        rows = []
        for i in range(0, len(reply_keyboard), 2):
            rows.append([{"text": b} for b in reply_keyboard[i:i+2]])
        reply_markup = {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": False}
    chunks = range(0, len(text), TELEGRAM_LIMIT)
    for i in chunks:
        chunk = text[i : i + TELEGRAM_LIMIT]
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup and i + TELEGRAM_LIMIT >= len(text):
            payload["reply_markup"] = reply_markup
        resp = requests.post(f"{api}/sendMessage", json=payload, timeout=30)
        # If MarkdownV2 parsing failed, retry as plain text so the message
        # always gets through — the user sees content, not silence.
        if parse_mode and not resp.json().get("ok"):
            payload.pop("parse_mode")
            requests.post(f"{api}/sendMessage", json=payload, timeout=30)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# The agent emits this marker to have the bridge send a file; it is removed from
# the visible reply. e.g.  [[send: wiki/files/chart.png]]
SEND_MARKER = re.compile(r"\[\[send:\s*(.+?)\]\]")
# One-time reply keyboard — tap appears as a visible message in chat.
# e.g.  [[buttons: Yes | No]]
BUTTON_MARKER = re.compile(r"\[\[buttons:\s*(.+?)\]\]", re.DOTALL)
# Persistent reply keyboard at the bottom of the screen.
# e.g.  [[keyboard: Quick action]]  or  [[keyboard: remove]]
KEYBOARD_MARKER = re.compile(r"\[\[keyboard:\s*(.*?)\]\]", re.DOTALL)
# Inline keyboard attached to the message — tap is silent, bridge routes it to agent.
# e.g.  [[inline: 🛒 Combi | 💊 DM | 🏪 Markt]]
INLINE_MARKER = re.compile(r"\[\[inline:\s*(.+?)\]\]", re.DOTALL)


def http(method: str, url: str, **kwargs):
    """requests with a few retries for transient connection resets on flaky
    networks (e.g. a firewall that intermittently drops connections)."""
    last = None
    for attempt in range(3):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def download_file(bot: "Bot", file_id: str, suggested_name: str | None = None) -> Path:
    """Download a Telegram file into the bot's inbox/ folder and return its path."""
    meta = http("GET", f"{bot.api}/getFile", params={"file_id": file_id}, timeout=30).json()
    file_path = meta["result"]["file_path"]  # e.g. "documents/file_5.pdf"
    data = http(
        "GET", f"https://api.telegram.org/file/bot{bot.token}/{file_path}", timeout=120
    ).content
    name = Path(suggested_name).name if suggested_name else os.path.basename(file_path)
    inbox = bot.workdir / "inbox"
    inbox.mkdir(exist_ok=True)
    dest = inbox / f"{int(time.time())}_{name or file_id}"
    dest.write_bytes(data)
    return dest


def transcribe(path: Path) -> str:
    """Transcribe an audio file to text via the OpenAI Whisper API.

    The API accepts OGG/Opus (Telegram voice notes) directly, so no conversion
    is needed. Returns "" if no API key is configured.
    """
    if not OPENAI_API_KEY:
        return ""
    audio = path.read_bytes()  # read once so retries can resend it
    resp = http(
        "POST",
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        data={"model": TRANSCRIBE_MODEL},
        files={"file": (path.name, audio, "audio/ogg")},
        timeout=120,
    )
    return resp.json().get("text", "").strip()


def history_path(bot: "Bot", chat_id: int) -> Path:
    return bot.workdir / "history" / f"{chat_id}.jsonl"


def read_history(bot: "Bot", chat_id: int, n: int) -> list:
    """Return up to the last n logged messages for this chat (oldest first)."""
    if n <= 0:
        return []
    p = history_path(bot, chat_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def log_turn(bot: "Bot", chat_id: int, user_text, agent_text) -> None:
    """Append a user message and/or agent reply to this chat's history file.

    No-op unless the bot has history enabled. Either side may be empty (e.g. a
    proactive run logs only the agent's message).
    """
    if bot.history <= 0:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries = []
    if user_text and user_text.strip():
        entries.append({"t": now, "role": "user", "text": user_text.strip()})
    if agent_text and agent_text.strip():
        entries.append({"t": now, "role": "agent", "text": agent_text.strip()})
    if not entries:
        return
    p = history_path(bot, chat_id)
    with _history_lock:
        p.parent.mkdir(exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def history_block(entries: list) -> str:
    """Format logged messages as the full prompt for the agent."""
    if not entries:
        return ""
    body = "\n".join(
        f"[{e.get('t', '')}] {'User' if e.get('role') == 'user' else 'You'}: {e.get('text', '')}"
        for e in entries
    )
    return (
        "You are invoked because the user sent you a message. "
        "The conversation history is listed below, oldest first — "
        "the last entry is the message you should respond to.\n\n"
        f"{body}\n"
        "[End of conversation]"
    )


def scheduled_prompt(task: str, entries: list) -> str:
    """Wrap a scheduled/proactive task with recent conversation context.

    The task is the primary instruction; history is appended as read-only
    context so the agent has situational awareness without being confused
    about what it should do.
    """
    if not entries:
        return task
    body = "\n".join(
        f"[{e.get('t', '')}] {'User' if e.get('role') == 'user' else 'You'}: {e.get('text', '')}"
        for e in entries
    )
    return (
        f"{task}\n\n"
        "[Recent conversation context — for background only. "
        "Do not reply to these messages; focus on the scheduled task above.]\n"
        f"{body}\n"
        "[End of context]"
    )


def build_prompt(text: str, files: list) -> str:
    """Combine the user's text/caption with a note about any attached files."""
    if not files:
        return text
    listing = "\n".join(f"- {p}" for p in files)
    note = (
        "[The user attached files, saved to disk for you to open and process "
        f"according to your instructions:\n{listing}\n]"
    )
    return f"{text}\n\n{note}" if text else note


def split_outgoing(reply: str, workdir: Path):
    """Pull [[send: path]] markers out of a reply; return (clean_text, [paths])."""
    paths = []
    for m in SEND_MARKER.finditer(reply):
        raw = m.group(1).strip().strip("\"'")
        p = Path(raw)
        if not p.is_absolute():
            p = workdir / p
        paths.append(p)
    clean = SEND_MARKER.sub("", reply).strip()
    return clean, paths


def split_buttons(reply: str):
    """Pull [[buttons: A | B | C]] marker out of a reply; return (clean_text, [labels])."""
    m = BUTTON_MARKER.search(reply)
    if not m:
        return reply, []
    labels = [l.strip() for l in m.group(1).split("|") if l.strip()]
    clean = BUTTON_MARKER.sub("", reply).strip()
    return clean, labels


def split_inline(reply: str):
    """Pull [[inline: A | B | C]] marker; return (clean_text, [labels] or [])."""
    m = INLINE_MARKER.search(reply)
    if not m:
        return reply, []
    labels = [l.strip() for l in m.group(1).split("|") if l.strip()]
    clean = INLINE_MARKER.sub("", reply).strip()
    return clean, labels


def split_keyboard(reply: str):
    """Pull [[keyboard: A | B]] or [[keyboard: remove]] out of a reply.

    Returns (clean_text, labels) where labels is a list of strings,
    'remove' to dismiss the keyboard, or None if no marker present.
    """
    m = KEYBOARD_MARKER.search(reply)
    if not m:
        return reply, None
    raw = m.group(1).strip()
    clean = KEYBOARD_MARKER.sub("", reply).strip()
    if raw.lower() == "remove":
        return clean, "remove"
    labels = [l.strip() for l in raw.split("|") if l.strip()]
    return clean, labels if labels else None


def send_file(api: str, chat_id: int, path: Path) -> None:
    """Send a local file as a photo (if an image) or document."""
    if not path.exists():
        send(api, chat_id, f"(could not find file to send: {path.name})")
        return
    is_image = path.suffix.lower() in IMAGE_EXTS
    method, field = ("sendPhoto", "photo") if is_image else ("sendDocument", "document")
    with open(path, "rb") as fh:
        requests.post(
            f"{api}/{method}",
            data={"chat_id": chat_id},
            files={field: fh},
            timeout=120,
        )


@contextlib.contextmanager
def typing_indicator(api: str, chat_id: int):
    """Keep Telegram's 'typing...' visible for the whole block.

    A single sendChatAction only lasts ~5s, so we re-send it every 4s on a
    background thread until the block exits (i.e. until the reply is ready).
    """
    stop = threading.Event()

    def keep_alive():
        while not stop.is_set():
            try:
                requests.post(
                    f"{api}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                    timeout=30,
                )
            except requests.RequestException:
                pass
            stop.wait(4)  # refresh before the ~5s indicator expires

    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Bots
# --------------------------------------------------------------------------- #

class Bot:
    def __init__(self, name, token, allowed_ids, workdir, keep_context,
                 model=None, timeout=RUN_TIMEOUT, schedules=None, default_chat_id=None,
                 history=0):
        self.name = name
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.allowed_ids = set(allowed_ids)
        self.workdir = workdir
        self.keep_context = keep_context
        self.model = model
        self.timeout = timeout
        self.schedules = schedules or []          # cron entries from agents.yaml
        self.default_chat_id = default_chat_id     # for proactive msgs lacking a chat
        self.history = history                     # how many past messages to replay
        # each item: (chat_id, text, [(file_id, name), ...], [voice_id, ...], proactive)
        # proactive=True for cron-triggered runs (don't log their prompt as a user msg)
        self.queue: "queue.Queue[tuple]" = queue.Queue()


def worker(bot: Bot) -> None:
    """Process one bot's messages serially; the agent run is globally throttled."""
    while True:
        chat_id, text, attachments, voice_ids, proactive = bot.queue.get()
        try:
            with typing_indicator(bot.api, chat_id):
                # Voice notes: transcribe to text and treat as the user's message.
                for voice_id in voice_ids:
                    try:
                        transcript = transcribe(download_file(bot, voice_id))
                        if transcript:
                            text = f"{text}\n\n{transcript}" if text else transcript
                    except Exception as e:
                        print(f"[{bot.name}] transcription failed:", e)
                if voice_ids and not text:
                    send(bot.api, chat_id, "Sorry, I couldn't transcribe that voice message.")
                    continue

                files = []
                for file_id, suggested in attachments:
                    try:
                        files.append(download_file(bot, file_id, suggested))
                    except Exception as e:
                        print(f"[{bot.name}] download failed:", e)
                prompt = build_prompt(text, files)
                if proactive:
                    # Scheduled/cron run: keep the task as the primary instruction
                    # and append recent history as background context only.
                    history = read_history(bot, chat_id, bot.history)
                    if history:
                        prompt = scheduled_prompt(prompt, history)
                else:
                    # Interactive run: log user message first so it's included
                    # when history is read, then use history as the full prompt.
                    log_turn(bot, chat_id, prompt, None)
                    history = read_history(bot, chat_id, bot.history)
                    if history:
                        prompt = history_block(history)
                with _chat_responded_lock:
                    _chat_responded[(bot.token, chat_id)] = False
                with AGENT_SLOTS:  # global cap: at most N agents running at once
                    reply = run_agent(
                        bot.workdir, bot.keep_context, prompt,
                        model=bot.model, timeout=bot.timeout, chat_id=chat_id,
                        token=bot.token, agent_name=bot.name,
                    )
            # Agents are instructed to use chat_respond for all output.
            # If the model skips the tool and outputs text directly (happens
            # with long contexts), fall back to sending that text so the user
            # is never left with silence.
            clean, to_send = split_outgoing(reply, bot.workdir)
            clean, button_labels = split_buttons(clean)
            clean, inline_labels = split_inline(clean)
            clean, kbd = split_keyboard(clean)
            with _chat_responded_lock:
                used_chat_respond = _chat_responded.get((bot.token, chat_id), False)
            if clean and not used_chat_respond and not proactive:
                send(bot.api, chat_id, clean,
                     buttons=button_labels or None,
                     inline_buttons=inline_labels or None,
                     reply_keyboard=kbd)
                log_turn(bot, chat_id, None, clean)
            for path in to_send:
                send_file(bot.api, chat_id, path)
        except Exception as e:  # never let one bad message kill the worker
            print(f"[{bot.name}] worker error:", e)
            try:
                send(bot.api, chat_id, f"Sorry, something went wrong: {e}")
            except Exception:
                pass
        finally:
            bot.queue.task_done()


_food_lock = threading.Lock()


def _food_active(bot: "Bot") -> bool:
    """True if this agent uses YAML food data (stores.yaml or list.yaml present)."""
    return (bot.workdir / "stores.yaml").exists() or (bot.workdir / "list.yaml").exists()


def _food_load(bot: "Bot", filename: str):
    p = bot.workdir / filename
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _food_save(bot: "Bot", filename: str, data) -> None:
    p = bot.workdir / filename
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _food_stores(bot: "Bot") -> list:
    return _food_load(bot, "stores.yaml") or []


def _food_catalog(bot: "Bot") -> list:
    return _food_load(bot, "catalog.yaml") or []


def _food_list(bot: "Bot") -> "dict | None":
    data = _food_load(bot, "list.yaml")
    if data is not None and "items" not in data:
        data["items"] = []
    return data


def _food_find(items: list, id_: int) -> "dict | None":
    return next((x for x in items if x.get("id") == id_), None)


def _food_active_items(lst: dict, store_id=None, hours: int = 6) -> list:
    """Unchecked items + recently-checked items (within `hours`), optionally for one store."""
    if not lst or not lst.get("items"):
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    result = []
    for item in lst["items"]:
        if store_id is not None and item.get("store_id") != store_id:
            continue
        if item.get("checked"):
            sc = item.get("status_changed_at")
            if not sc or datetime.fromisoformat(sc) < cutoff:
                continue
        result.append(item)
    return sorted(result, key=lambda x: (
        x.get("checked", False), x.get("store_id", 0), x.get("name", "").lower()
    ))


def _food_enrich(items: list, stores: list) -> list:
    """Add 'store' name field to each item dict."""
    store_map = {s["id"]: s["name"] for s in stores}
    return [{**item, "store": store_map.get(item.get("store_id"), "")} for item in items]


def _store_picker_keyboard(stores, prefix: str = "liststore") -> dict:
    """Inline keyboard: stores 2 per row, plus an 'Alle Geschäfte' button at the end."""
    rows, row = [], []
    for s in stores:
        row.append({"text": s["name"], "callback_data": f"{prefix}:{s['id']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Alle Geschäfte", "callback_data": f"{prefix}:0"}])
    return {"inline_keyboard": rows}


def _check_keyboard(items: list) -> dict:
    """Inline keyboard for a single-store list: one item per row, toggle on tap."""
    rows = []
    for r in items:
        qty = f" · {r['qty']}" if r.get("qty") else ""
        if r["checked"]:
            text = f"✅ {r['name']}{qty}"
            data = f"uncheck:{r['id']}"
        else:
            text = f"⬜ {r['name']}{qty}"
            data = f"check:{r['id']}"
        rows.append([{"text": text, "callback_data": data}])
    return {"inline_keyboard": rows}


def _check_all_keyboard(items: list) -> dict:
    """Inline keyboard for the all-stores list: includes store name in button text.
    Uses checkall:/uncheckall: prefixes so the callback rebuilds the full list."""
    rows = []
    for r in items:
        qty = f" · {r['qty']}" if r.get("qty") else ""
        store = f" · {r['store']}" if r.get("store") else ""
        if r["checked"]:
            text = f"✅ {r['name']}{qty}{store}"
            data = f"uncheckall:{r['id']}"
        else:
            text = f"⬜ {r['name']}{qty}{store}"
            data = f"checkall:{r['id']}"
        rows.append([{"text": text, "callback_data": data}])
    return {"inline_keyboard": rows}


def _catalog_keyboard(products, on_list: set) -> dict:
    """Inline keyboard for a single-store catalog. Uses cat: prefix."""
    sorted_products = sorted(products, key=lambda p: (p["name"].lower() not in on_list, p["name"].lower()))
    rows = []
    for p in sorted_products:
        on = p["name"].lower() in on_list
        text = f"🛒 {p['name']}" if on else p["name"]
        rows.append([{"text": text, "callback_data": f"cat:{p['id']}"}])
    return {"inline_keyboard": rows}


def _catalog_all_keyboard(products, on_list: set) -> dict:
    """Inline keyboard for the all-stores catalog: shows store name in button text.
    Uses catall: prefix so the callback rebuilds the full catalog."""
    sorted_products = sorted(products, key=lambda p: (p["name"].lower() not in on_list, p["name"].lower()))
    rows = []
    for p in sorted_products:
        on = p["name"].lower() in on_list
        store = f" · {p['store']}" if p["store"] else ""
        text = f"🛒 {p['name']}{store}" if on else f"{p['name']}{store}"
        rows.append([{"text": text, "callback_data": f"catall:{p['id']}"}])
    return {"inline_keyboard": rows}


def handle_command(bot: Bot, chat_id: int, text: str) -> bool:
    """Handle bridge-level slash commands without invoking the agent.

    Returns True if the command was handled (caller should skip enqueuing),
    False if it should be passed to the agent as a normal message.
    """
    cmd = text.strip().split()[0].lower()

    if cmd == "/status":
        rem_dir = bot.workdir / "reminders"
        n_reminders = n_scheduled = 0
        if rem_dir.exists():
            for f in rem_dir.glob("*.json"):
                try:
                    entry = json.loads(f.read_text(encoding="utf-8"))
                    if entry.get("type") == "schedule":
                        n_scheduled += 1
                    else:
                        n_reminders += 1
                except Exception:
                    pass
        lines = [
            f"Agent: {bot.name}",
            f"Modell: {bot.model or '(opencode Standard)'}",
            f"Sitzung: {'kontinuierlich' if bot.keep_context else 'zustandslos'}",
            f"Verlauf: {bot.history} Nachrichten" if bot.history else "Verlauf: aus",
            f"Ausstehende Erinnerungen: {n_reminders}",
            f"Geplante Ausführungen: {n_scheduled}",
        ]
        send(bot.api, chat_id, "\n".join(lines))
        return True

    if cmd == "/list":
        if not _food_active(bot):
            return False
        parts = text.strip().split(None, 1)
        store_filter = parts[1].strip() if len(parts) > 1 else None
        try:
            stores = _food_stores(bot)
            lst = _food_list(bot)
            if not lst:
                send(bot.api, chat_id, "Die Einkaufsliste ist leer.")
                return True
            if store_filter:
                store = next((s for s in stores if s["name"].lower() == store_filter.lower()), None)
                if not store:
                    send(bot.api, chat_id, f"Geschäft <b>{html.escape(store_filter)}</b> nicht gefunden.", parse_mode="HTML")
                    return True
                items = _food_enrich(_food_active_items(lst, store_id=store["id"]), stores)
                if not items:
                    send(bot.api, chat_id, f"Nichts auf der Liste für <b>{html.escape(store['name'])}</b>.", parse_mode="HTML")
                else:
                    requests.post(f"{bot.api}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": f"{store['name']} — antippen zum Ab- oder Abhaken:",
                        "reply_markup": _check_keyboard(items),
                    }, timeout=30)
            else:
                unchecked_store_ids = {i["store_id"] for i in lst.get("items", []) if not i.get("checked")}
                picker_stores = sorted([s for s in stores if s["id"] in unchecked_store_ids], key=lambda s: s["name"])
                if not picker_stores:
                    send(bot.api, chat_id, "Die Einkaufsliste ist leer.")
                else:
                    requests.post(f"{bot.api}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": "Welches Geschäft?",
                        "reply_markup": _store_picker_keyboard(picker_stores),
                    }, timeout=30)
        except Exception as e:
            send(bot.api, chat_id, f"Einkaufsliste konnte nicht gelesen werden: {e}")
        return True

    if cmd == "/reminders":
        rem_dir = bot.workdir / "reminders"
        items = []
        if rem_dir.exists():
            for f in sorted(rem_dir.glob("*.json")):
                try:
                    entry = json.loads(f.read_text(encoding="utf-8"))
                    if entry.get("type") == "schedule":
                        continue
                    due = datetime.fromisoformat(entry["due"])
                    items.append(f"• {due:%Y-%m-%d %H:%M}  {entry.get('text', '(no text)')}")
                except Exception:
                    pass
        send(bot.api, chat_id, ("Ausstehende Erinnerungen:\n" + "\n".join(items)) if items else "Keine ausstehenden Erinnerungen.")
        return True

    if cmd == "/scheduled":
        rem_dir = bot.workdir / "reminders"
        items = []
        if rem_dir.exists():
            for f in sorted(rem_dir.glob("*.json")):
                try:
                    entry = json.loads(f.read_text(encoding="utf-8"))
                    if entry.get("type") != "schedule":
                        continue
                    due = datetime.fromisoformat(entry["due"])
                    items.append(f"• {due:%Y-%m-%d %H:%M}  {entry.get('prompt', '(no prompt)')}")
                except Exception:
                    pass
        send(bot.api, chat_id, ("Geplante Ausführungen:\n" + "\n".join(items)) if items else "Keine geplanten Ausführungen.")
        return True

    if cmd == "/help":
        lines = [
            "Systembefehle (kein Agentaufruf):",
            "  /status — Agentinfo und ausstehende Aufgaben",
            "  /reminders — Ausstehende Erinnerungen anzeigen",
            "  /scheduled — Geplante Ausführungen anzeigen",
            "  /help — Diese Übersicht",
        ]
        local_cmds = sorted((bot.workdir / "commands").glob("*.py")) if (bot.workdir / "commands").is_dir() else []
        if local_cmds:
            lines.append("")
            lines.append("Agentspezifische Befehle (kein Agentaufruf):")
            for p in local_cmds:
                lines.append(f"  /{p.stem}")
        lines += [
            "",
            "Agentbefehle (vom Agent verarbeitet):",
            "  /remind <wann> \"<nachricht>\" — Erinnerung setzen",
            "  /schedule <wann> \"<aufgabe>\" — Ausführung planen",
            "",
            "Natürliche Sprache funktioniert für alles oben ebenfalls.",
        ]
        send(bot.api, chat_id, "\n".join(lines))
        return True

    if cmd == "/clear_list":
        if not _food_active(bot):
            return False
        try:
            lst = _food_list(bot)
            count = len(lst.get("items", [])) if lst else 0
        except Exception as e:
            send(bot.api, chat_id, f"Fehler: {e}")
            return True
        if not count:
            send(bot.api, chat_id, "Die Einkaufsliste ist bereits leer.")
            return True
        requests.post(f"{bot.api}/sendMessage", json={
            "chat_id": chat_id,
            "text": f"Alle {count} Artikel löschen?",
            "reply_markup": {"inline_keyboard": [[
                {"text": "Ja, leeren", "callback_data": "clearlist:confirm"},
                {"text": "Abbrechen",  "callback_data": "clearlist:cancel"},
            ]]},
        }, timeout=10)
        return True

    if cmd == "/stores":
        if not _food_active(bot):
            return False
        try:
            stores = sorted(_food_stores(bot), key=lambda s: s["name"])
        except Exception as e:
            send(bot.api, chat_id, f"Fehler: {e}")
            return True
        if not stores:
            send(bot.api, chat_id, "Noch keine Geschäfte konfiguriert.")
            return True
        requests.post(f"{bot.api}/sendMessage", json={
            "chat_id": chat_id,
            "text": "Welches Geschäft?",
            "reply_markup": _store_picker_keyboard(stores, prefix="catstore"),
        }, timeout=10)
        return True

    if cmd == "/catalog":
        if not _food_active(bot):
            return False
        parts = text.strip().split(None, 1)
        store_filter = parts[1].strip() if len(parts) > 1 else None
        try:
            stores  = _food_stores(bot)
            catalog = _food_catalog(bot)
            lst = _food_list(bot)
            on_list: set = {i["name"].lower() for i in (lst.get("items", []) if lst else []) if not i.get("checked")}
            if store_filter:
                store = next((s for s in stores if s["name"].lower() == store_filter.lower()), None)
                if not store:
                    send(bot.api, chat_id, f"Geschäft <b>{html.escape(store_filter)}</b> nicht gefunden.", parse_mode="HTML")
                    return True
                products = sorted([p for p in catalog if p.get("store_id") == store["id"]], key=lambda p: p["name"])
                if not products:
                    send(bot.api, chat_id, f"Keine Produkte für <b>{html.escape(store['name'])}</b> im Katalog.", parse_mode="HTML")
                    return True
                requests.post(f"{bot.api}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": f"Katalog — {store['name']}:",
                    "reply_markup": _catalog_keyboard(products, on_list),
                }, timeout=30)
            else:
                catalog_store_ids = {p.get("store_id") for p in catalog}
                picker_stores = sorted([s for s in stores if s["id"] in catalog_store_ids], key=lambda s: s["name"])
                if not picker_stores:
                    send(bot.api, chat_id, "Noch keine Produkte im Katalog.")
                    return True
                requests.post(f"{bot.api}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "Welches Geschäft?",
                    "reply_markup": _store_picker_keyboard(picker_stores, prefix="catstore"),
                }, timeout=30)
        except Exception as e:
            send(bot.api, chat_id, f"Fehler: {e}")
        return True

    # Agent-local commands: workdir/commands/<name>.py
    cmd_script = bot.workdir / "commands" / f"{cmd[1:]}.py"
    if cmd_script.exists():
        try:
            cmd_env = os.environ.copy()
            cmd_env["PYTHONUTF8"] = "1"
            extra_args = text.strip().split()[1:]
            result = subprocess.run(
                [sys.executable, str(cmd_script)] + extra_args,
                capture_output=True, env=cmd_env,
                cwd=bot.workdir, timeout=10,
            )
            stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
            stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            output = stderr if (result.returncode != 0 and stderr) else stdout
            send(bot.api, chat_id, output or "(keine Ausgabe)")
        except Exception as e:
            send(bot.api, chat_id, f"Befehlsfehler: {e}")
        return True

    return False


def poller(bot: Bot) -> None:
    """Long-poll one bot's Telegram updates and enqueue allowed messages."""
    print(f"[{bot.name}] polling. workdir={bot.workdir} allowed={bot.allowed_ids}")
    offset = None
    while True:
        try:
            resp = requests.get(
                f"{bot.api}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=40,
            ).json()
        except requests.RequestException as e:
            print(f"[{bot.name}] poll error:", e)
            time.sleep(3)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1

            cb = update.get("callback_query")
            if cb:
                chat_id = cb["message"]["chat"]["id"]
                message_id = cb["message"]["message_id"]
                data = cb.get("data", "")

                def _ack(text=None):
                    payload = {"callback_query_id": cb["id"]}
                    if text:
                        payload["text"] = text
                    with contextlib.suppress(Exception):
                        requests.post(f"{bot.api}/answerCallbackQuery",
                                      json=payload, timeout=10)

                if chat_id not in bot.allowed_ids:
                    _ack()
                elif data.startswith("inline:"):
                    _ack()
                    label = data[7:].strip()
                    if label:
                        bot.queue.put((chat_id, label, [], [], False))
                elif data.startswith("cat:"):
                    try:
                        product_id = int(data.split(":")[1])
                        with _food_lock:
                            catalog = _food_catalog(bot)
                            product = _food_find(catalog, product_id)
                        if not product:
                            _ack()
                        else:
                            with _food_lock:
                                stores  = _food_stores(bot)
                                lst     = _food_list(bot) or {"items": []}
                                now     = datetime.now().isoformat(timespec="seconds")
                                existing = next((i for i in lst["items"] if i["name"].lower() == product["name"].lower()), None)
                                if not existing:
                                    lst["items"].append({"id": max((i.get("id", 0) for i in lst["items"]), default=0) + 1, "name": product["name"], "store_id": product["store_id"], "qty": "", "checked": False, "added_at": now, "status_changed_at": None})
                                    toast = f"{product['name']} hinzugefügt ✅"
                                elif existing["checked"]:
                                    existing["checked"] = False
                                    existing["status_changed_at"] = now
                                    toast = f"{product['name']} hinzugefügt ✅"
                                else:
                                    lst["items"] = [i for i in lst["items"] if i.get("id") != existing["id"]]
                                    toast = f"{product['name']} entfernt"
                                _food_save(bot, "list.yaml", lst)
                                store_prods = sorted([p for p in catalog if p.get("store_id") == product["store_id"]], key=lambda p: p["name"])
                                on_list: set = {i["name"].lower() for i in lst["items"] if not i.get("checked")}
                            _ack(toast)
                            requests.post(f"{bot.api}/editMessageReplyMarkup", json={
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "reply_markup": _catalog_keyboard(store_prods, on_list),
                            }, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] catalog callback error: {e}")
                        _ack()
                elif data.startswith("catstore:"):
                    _ack()
                    try:
                        store_id_val = int(data.split(":")[1])
                        stores  = _food_stores(bot)
                        catalog = _food_catalog(bot)
                        lst     = _food_list(bot)
                        on_list = {i["name"].lower() for i in (lst.get("items", []) if lst else []) if not i.get("checked")}
                        if store_id_val == 0:
                            enriched = _food_enrich(sorted(catalog, key=lambda p: (p.get("store_id", 0), p["name"].lower())), stores)
                            if not enriched:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Noch keine Produkte im Katalog."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Alle Geschäfte — Katalog:", "reply_markup": _catalog_all_keyboard(enriched, on_list)}, timeout=10)
                        else:
                            store = _food_find(stores, store_id_val)
                            products = sorted([p for p in catalog if p.get("store_id") == store_id_val], key=lambda p: p["name"])
                            store_name = store["name"] if store else "Katalog"
                            if not products:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"Keine Produkte für {store_name}."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"Katalog — {store_name}:", "reply_markup": _catalog_keyboard(products, on_list)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] catstore callback error: {e}")
                elif data.startswith("catall:"):
                    try:
                        product_id = int(data.split(":")[1])
                        with _food_lock:
                            stores  = _food_stores(bot)
                            catalog = _food_catalog(bot)
                            product = _food_find(catalog, product_id)
                        if not product:
                            _ack()
                        else:
                            with _food_lock:
                                lst  = _food_list(bot) or {"items": []}
                                now  = datetime.now().isoformat(timespec="seconds")
                                existing = next((i for i in lst["items"] if i["name"].lower() == product["name"].lower()), None)
                                if not existing:
                                    lst["items"].append({"id": max((i.get("id", 0) for i in lst["items"]), default=0) + 1, "name": product["name"], "store_id": product["store_id"], "qty": "", "checked": False, "added_at": now, "status_changed_at": None})
                                    toast = f"{product['name']} hinzugefügt ✅"
                                elif existing["checked"]:
                                    existing["checked"] = False
                                    existing["status_changed_at"] = now
                                    toast = f"{product['name']} hinzugefügt ✅"
                                else:
                                    lst["items"] = [i for i in lst["items"] if i.get("id") != existing["id"]]
                                    toast = f"{product['name']} entfernt"
                                _food_save(bot, "list.yaml", lst)
                                enriched = _food_enrich(sorted(catalog, key=lambda p: (p.get("store_id", 0), p["name"].lower())), stores)
                                on_list: set = {i["name"].lower() for i in lst["items"] if not i.get("checked")}
                            _ack(toast)
                            requests.post(f"{bot.api}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": _catalog_all_keyboard(enriched, on_list)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] catall callback error: {e}")
                        _ack()
                elif data.startswith("liststore:"):
                    _ack()
                    try:
                        store_id_val = int(data.split(":")[1])
                        stores = _food_stores(bot)
                        lst    = _food_list(bot)
                        if store_id_val == 0:
                            items = _food_enrich(_food_active_items(lst), stores) if lst else []
                            if not items:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Die Einkaufsliste ist leer."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Alle Geschäfte — antippen zum Ab- oder Abhaken:", "reply_markup": _check_all_keyboard(items)}, timeout=10)
                        else:
                            store = _food_find(stores, store_id_val)
                            items = _food_enrich(_food_active_items(lst, store_id=store_id_val), stores) if lst else []
                            store_name = store["name"] if store else "Einkaufsliste"
                            if not items:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"Nichts auf der Liste für {store_name}."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"{store_name} — antippen zum Ab- oder Abhaken:", "reply_markup": _check_keyboard(items)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] liststore callback error: {e}")
                elif data.startswith("checkall:") or data.startswith("uncheckall:"):
                    _ack()
                    try:
                        checking = data.startswith("checkall:")
                        item_id  = int(data.split(":")[1])
                        with _food_lock:
                            stores = _food_stores(bot)
                            lst    = _food_list(bot)
                            if lst:
                                item = _food_find(lst.get("items", []), item_id)
                                if item:
                                    item["checked"] = checking
                                    item["status_changed_at"] = datetime.now().isoformat(timespec="seconds")
                                    _food_save(bot, "list.yaml", lst)
                            items = _food_enrich(_food_active_items(lst), stores) if lst else []
                        requests.post(f"{bot.api}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": _check_all_keyboard(items)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] checkall callback error: {e}")
                elif data.startswith("clearlist:"):
                    action = data.split(":")[1]
                    if action == "cancel":
                        _ack()
                        requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Abgebrochen."}, timeout=10)
                    elif action == "confirm":
                        try:
                            with _food_lock:
                                lst = _food_list(bot)
                                if lst:
                                    lst["items"] = []
                                    _food_save(bot, "list.yaml", lst)
                            _ack()
                            requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Einkaufsliste geleert."}, timeout=10)
                        except Exception as e:
                            print(f"[{bot.name}] clearlist callback error: {e}")
                            _ack()
                    else:
                        _ack()
                elif data.startswith("check:") or data.startswith("uncheck:"):
                    _ack()
                    try:
                        checking = data.startswith("check:")
                        item_id  = int(data.split(":")[1])
                        with _food_lock:
                            stores = _food_stores(bot)
                            lst    = _food_list(bot)
                            store_id_of_item = None
                            if lst:
                                item = _food_find(lst.get("items", []), item_id)
                                if item:
                                    store_id_of_item = item.get("store_id")
                                    item["checked"] = checking
                                    item["status_changed_at"] = datetime.now().isoformat(timespec="seconds")
                                    _food_save(bot, "list.yaml", lst)
                            items = _food_enrich(_food_active_items(lst, store_id=store_id_of_item), stores) if lst and store_id_of_item is not None else []
                        requests.post(f"{bot.api}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": _check_keyboard(items)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] check callback error: {e}")
                else:
                    _ack()
                continue

            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            if chat_id not in bot.allowed_ids:
                print(f"[{bot.name}] ignored message from {chat_id}")
                continue

            text = msg.get("text") or msg.get("caption") or ""

            # Include context when the user quotes or replies to a message.
            # quote (partial text selection) takes priority over the full reply.
            reply_ctx = ""
            if "quote" in msg:
                q = msg["quote"].get("text", "").strip()
                if q:
                    reply_ctx = f'[Quoting: "{q}"]'
            elif "reply_to_message" in msg:
                r = msg["reply_to_message"]
                r_text = (r.get("text") or r.get("caption") or "").strip()
                if r_text:
                    reply_ctx = f'[Replying to: "{r_text}"]'
            if reply_ctx:
                text = f"{reply_ctx}\n{text}" if text else reply_ctx

            attachments = []  # (file_id, suggested_name) -> handed to the agent
            voice_ids = []    # transcribed to text before reaching the agent
            if "photo" in msg:
                attachments.append((msg["photo"][-1]["file_id"], None))  # largest size
            if "document" in msg:
                doc = msg["document"]
                attachments.append((doc["file_id"], doc.get("file_name")))
            if "voice" in msg:
                voice_ids.append(msg["voice"]["file_id"])
            if "audio" in msg:
                voice_ids.append(msg["audio"]["file_id"])

            if not text and not attachments and not voice_ids:
                continue  # ignore stickers, locations, etc.

            # Bridge-level slash commands are handled here without an agent call.
            raw_text = msg.get("text") or msg.get("caption") or ""
            if raw_text.startswith("/") and handle_command(bot, chat_id, raw_text):
                continue

            print(f"[{bot.name}] > {text!r} (+{len(attachments)} file, +{len(voice_ids)} audio)")
            bot.queue.put((chat_id, text, attachments, voice_ids, False))


def scheduler(bot: Bot) -> None:
    """Deliver due reminders and fire cron schedules for one bot.

    Two proactive sources, checked on a short tick:
      - reminders/   one JSON file per reminder, written by the shared `remind`
                     tool when the agent schedules one. Delivered as plain text
                     at its due time (no agent run needed), then deleted.
      - schedules    cron entries from agents.yaml. At fire time the prompt is
                     enqueued like a normal message, so the worker runs the
                     agent and its reply is sent -- reusing the whole pipeline
                     and counting against the global concurrency cap.
    """
    crons = []
    if bot.schedules and croniter is None:
        print(f"[{bot.name}] schedules configured but croniter is not installed; "
              f"cron disabled. Run: pip install croniter")
    elif croniter is not None:
        now = datetime.now()
        for sched in bot.schedules:
            expr = sched["cron"]
            try:
                itr = croniter(expr, now)
                crons.append({
                    "itr": itr,
                    "next": itr.get_next(datetime),  # don't backfill missed runs
                    "prompt": sched["prompt"],
                    "chat_id": sched.get("chat_id", bot.default_chat_id),
                })
            except (ValueError, KeyError) as e:
                print(f"[{bot.name}] bad cron {expr!r}: {e}")

    reminders_dir = bot.workdir / "reminders"
    while True:
        now = datetime.now()

        # Due reminders -> send the stored text directly (no model call).
        if reminders_dir.exists():
            for f in sorted(reminders_dir.glob("*.json")):
                try:
                    entry = json.loads(f.read_text(encoding="utf-8"))
                    due = datetime.fromisoformat(entry["due"])
                except (ValueError, KeyError, OSError) as e:
                    print(f"[{bot.name}] discarding bad reminder {f.name}: {e}")
                    f.unlink(missing_ok=True)
                    continue
                if due <= now:
                    chat_id = entry.get("chat_id", bot.default_chat_id)
                    if entry.get("type") == "schedule":
                        # Self-scheduled agent run: enqueue to the worker like a cron job.
                        user_prompt = entry.get("prompt", "").strip()
                        due_fmt = due.strftime("%Y-%m-%d at %H:%M")
                        invocation = (
                            f"You are invoked because you scheduled yourself to run on "
                            f"{due_fmt}. You wanted to do the following: \"{user_prompt}\""
                        )
                        bot.queue.put((chat_id, invocation, [], [], True))
                        print(f"[{bot.name}] scheduled run enqueued: {user_prompt!r}")
                    else:
                        text = entry.get("text") or "(reminder)"
                        try:
                            body = f"<b>{REMINDER_PREFIX}</b>: {html.escape(text, quote=False)}"
                            send(bot.api, chat_id, body, parse_mode="HTML")
                            print(f"[{bot.name}] reminder delivered: {text!r}")
                        except Exception as e:
                            print(f"[{bot.name}] reminder send failed:", e)
                    f.unlink(missing_ok=True)

        # Cron schedules -> enqueue a prompt for the worker.
        for c in crons:
            if c["next"] <= now:
                due_fmt = now.strftime("%Y-%m-%d at %H:%M")
                invocation = (
                    f"You are invoked because you were scheduled to run at {due_fmt}. "
                    f"Your task: \"{c['prompt']}\""
                )
                bot.queue.put((c["chat_id"], invocation, [], [], True))
                c["next"] = c["itr"].get_next(datetime)
                print(f"[{bot.name}] cron fired; next at {c['next']:%Y-%m-%d %H:%M}")

        time.sleep(15)


# --------------------------------------------------------------------------- #
# Config + startup
# --------------------------------------------------------------------------- #

def ensure_shared_instructions(bot: "Bot") -> None:
    """Make sure the bot's opencode.json references the shared instructions file.

    OpenCode auto-loads each workdir's AGENTS.md and *also* concatenates any files
    listed under `instructions` in opencode.json. So the common rules live in one
    shared file and every agent just points at it -- improvements propagate to all
    agents at once, and a new agent needs only its own personality AGENTS.md.

    The reference is stored as a path relative to the workdir (OpenCode resolves
    `instructions` relative to the config file's directory), which also keeps the
    whole tree portable to the Raspberry Pi. Existing config is merged, not
    clobbered; a config file we can't parse (e.g. JSONC with comments) is left
    untouched with a warning.
    """
    if not SHARED_INSTRUCTIONS.exists():
        print(f"[{bot.name}] shared instructions file not found: {SHARED_INSTRUCTIONS}")
    rel = os.path.relpath(SHARED_INSTRUCTIONS, bot.workdir).replace(os.sep, "/")
    if not rel.startswith("."):
        rel = "./" + rel
    cfg_path = bot.workdir / "opencode.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[{bot.name}] {cfg_path.name} unreadable; leaving it untouched. "
                  f"Add {rel!r} to its 'instructions' yourself. ({e})")
            return
        instr = cfg.get("instructions")
        instr = list(instr) if isinstance(instr, list) else ([] if instr is None else [instr])
        if rel in instr:
            return
        instr.append(rel)
        cfg["instructions"] = instr
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"[{bot.name}] linked shared instructions in {cfg_path.name}")
    else:
        cfg_path.write_text(json.dumps({"instructions": [rel]}, indent=2) + "\n",
                            encoding="utf-8")
        print(f"[{bot.name}] created {cfg_path.name} referencing shared instructions")


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Config not found: {CONFIG_PATH}")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    concurrency = int(cfg.get("concurrency", 1))
    bots = []
    for entry in cfg.get("agents", []):
        name = entry.get("name", "?")

        token_env = entry.get("token_env")
        token = os.environ.get(token_env) if token_env else None
        if not token:
            raise SystemExit(f"Bot '{name}': env var {token_env!r} is not set.")

        workdir = Path(entry["workdir"]).resolve()
        # Safety: the agent has file/shell access scoped to its workdir. Refuse
        # to start if that workdir equals or contains the bridge folder (which
        # holds this script and the .env with every bot token).
        if workdir == BRIDGE_DIR or workdir in BRIDGE_DIR.parents:
            raise SystemExit(
                f"Bot '{name}': workdir ({workdir}) is the same as, or a parent "
                f"of, the bridge folder ({BRIDGE_DIR}). The agent could then read "
                f"your .env / bot tokens. Point it at a separate folder."
            )
        if not workdir.exists():
            raise SystemExit(f"Bot '{name}': workdir does not exist: {workdir}")

        allowed = {int(x) for x in entry.get("allowed_ids", [])}
        if not allowed:
            raise SystemExit(
                f"Bot '{name}': allowed_ids is empty -- refusing to run an open bot."
            )

        keep_context = bool(entry.get("continue", False))
        model = entry.get("model")  # None => opencode uses its configured default
        timeout = int(entry.get("timeout", RUN_TIMEOUT))

        schedules = entry.get("schedules") or []
        for s in schedules:
            if not s.get("cron") or not s.get("prompt"):
                raise SystemExit(
                    f"Bot '{name}': each schedule needs both 'cron' and 'prompt'."
                )
        # Reminders/cron without an explicit chat default to the lowest allowed id.
        default_chat_id = sorted(allowed)[0]
        history = int(entry.get("history", 0))  # 0/absent = off
        bots.append(Bot(name, token, allowed, workdir, keep_context, model,
                        timeout, schedules, default_chat_id, history))

    if not bots:
        raise SystemExit(f"No bots configured in {CONFIG_PATH}.")
    return concurrency, bots


def _docstring_first_line(path: Path) -> str:
    """Return the first non-empty line of a Python file's module docstring, or ''."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree)
        if doc:
            return doc.strip().splitlines()[0].strip()
    except Exception:
        pass
    return ""


def register_commands(bot: "Bot") -> None:
    """Push the bot's slash-command list to Telegram via setMyCommands."""
    BUILTIN = sorted([
        ("status",    "Agentinfo und ausstehende Aufgaben"),
        ("reminders", "Ausstehende Erinnerungen anzeigen"),
        ("scheduled", "Geplante Ausführungen anzeigen"),
        ("help",      "Verfügbare Befehle anzeigen"),
    ])

    commands = []
    cmd_dir = bot.workdir / "commands"
    if cmd_dir.is_dir():
        for script in sorted(cmd_dir.glob("*.py")):
            desc = _docstring_first_line(script) or f"/{script.stem}"
            desc = re.sub(r"^Bridge command\s+/\w+\s*[—-]\s*", "", desc, flags=re.IGNORECASE)
            commands.append({"command": script.stem, "description": desc[:256]})
    commands += [{"command": cmd, "description": desc} for cmd, desc in BUILTIN]

    try:
        resp = requests.post(
            f"{bot.api}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        ).json()
        if resp.get("ok"):
            print(f"[{bot.name}] registered {len(commands)} command(s) with Telegram")
        else:
            print(f"[{bot.name}] setMyCommands failed: {resp.get('description')}")
    except Exception as e:
        print(f"[{bot.name}] setMyCommands error: {e}")


class _InternalHandler(BaseHTTPRequestHandler):
    """Handle POST /send requests from the chat_respond tool.

    chat_respond posts JSON {token, chat_id, text} here. The bridge looks up
    the bot by token, processes markers (buttons, inline keyboards, file sends),
    and delivers the message exactly as if it came from the worker.
    """

    def do_POST(self) -> None:
        if self.path != "/send":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        token   = body.get("token", "")
        text    = body.get("text", "")
        chat_id = body.get("chat_id")
        bot     = _bots_by_token.get(token)
        if not bot or chat_id is None:
            self.send_response(400)
            self.end_headers()
            return

        clean = ""
        try:
            chat_id = int(chat_id)
            clean, to_send = split_outgoing(text, bot.workdir)
            clean, button_labels = split_buttons(clean)
            clean, inline_labels = split_inline(clean)
            clean, kbd           = split_keyboard(clean)
            if clean:
                send(bot.api, chat_id, clean,
                     buttons=button_labels or None,
                     inline_buttons=inline_labels or None,
                     reply_keyboard=kbd)
            for path in to_send:
                send_file(bot.api, chat_id, path)
        except Exception as e:
            print(f"[internal] send error: {e}")
            self.send_response(500)
            self.end_headers()
            return

        # Log after confirming delivery — separate try so a logging failure
        # never causes a 500 that would break chat_respond in the agent.
        if clean:
            with _chat_responded_lock:
                _chat_responded[(bot.token, chat_id)] = True
            try:
                log_turn(bot, chat_id, None, clean)
            except Exception as e:
                print(f"[internal] log error: {e}")

        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass  # suppress HTTP access log


def start_internal_server() -> int:
    """Start the internal send server on localhost. Returns the bound port."""
    server = ThreadingHTTPServer(("127.0.0.1", INTERNAL_PORT), _InternalHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def main() -> None:
    global AGENT_SLOTS, BRIDGE_INTERNAL_URL, _bots_by_token
    concurrency, bots = load_config()
    AGENT_SLOTS = threading.Semaphore(concurrency)
    port = start_internal_server()
    BRIDGE_INTERNAL_URL = f"http://127.0.0.1:{port}"
    _bots_by_token = {bot.token: bot for bot in bots}
    print(
        f"Starting {len(bots)} bot(s); max {concurrency} agent(s) at once. "
        f"opencode={OPENCODE}  internal={BRIDGE_INTERNAL_URL}"
    )
    for bot in bots:
        ensure_shared_instructions(bot)
        register_commands(bot)
        threading.Thread(target=worker, args=(bot,), daemon=True).start()
        threading.Thread(target=poller, args=(bot,), daemon=True).start()
        threading.Thread(target=scheduler, args=(bot,), daemon=True).start()

    # Block forever; daemon threads do the work. Ctrl+C to stop.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Shutting down.")


if __name__ == "__main__":
    main()
