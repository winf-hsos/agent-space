"""
Multi-bot Telegram -> Pydantic AI bridge.

Runs several Telegram bots from a single process. Each bot is backed by its own
Pydantic AI agent: its own working directory (with its own AGENTS.md), its own
allowed users, its own model. Bots are declared in agents.yaml; secrets (tokens)
stay in environment variables / a .env file.

Each agent has three tools:
  - chat_respond(text)  native tool that delivers a message to Telegram (markers
                        processed in-process; replaces the old shell script +
                        internal HTTP server).
  - bash(command)       runs a shell command in the agent's workdir with the
                        shared + agent-local tool folders on PATH (food-add,
                        remind, schedule, etc.). Preserves the folder/markdown
                        workflow unchanged.
  - web search          OpenAI's native web search (Responses API), used e.g. by
                        the food agent's morning recipe cron.

Design:
  - One *poller* thread per bot long-polls Telegram (each token needs its own
    getUpdates consumer) and enqueues messages.
  - One *worker* thread per bot processes that bot's messages strictly in order.
  - A single global semaphore (`concurrency` in agents.yaml) caps how many agents
    run at the same time across ALL bots. This is the key guard on small
    hardware like a Raspberry Pi 5.

Setup:
  1. pip install -r requirements.txt
  2. Create each bot via @BotFather; put its token in .env.
  3. Put OPENAI_API_KEY in .env.
  4. Edit agents.yaml, then run:  python telegram_agent.py

Env vars (user-set):
  <token_env>           - one per agent, named in agents.yaml
  AGENTS_CONFIG         - optional, path to agents.yaml (default: next to this file)
  OPENAI_API_KEY        - required; agent model + voice transcription
  AGENT_REQUEST_LIMIT   - optional, max model requests per run (default: 20)

Env vars injected into the bash tool's subprocess:
  TELEGRAM_CHAT_ID      - current chat ID
  TELEGRAM_BOT_TOKEN    - this bot's token (for remind/schedule/food tools)
"""

import ast
import asyncio
import contextlib
import html
import json
import os
import queue
import re
import sys
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

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
# Shared instructions prepended to every agent's system prompt. Maintain once
# here; all agents pick it up. Read fresh each run so webui edits take effect
# without a restart.
SHARED_INSTRUCTIONS = TOOLS_DIR / "shared" / "agents-common.md"
TELEGRAM_LIMIT = 4096
RUN_TIMEOUT = 600  # seconds an agent may work on a single message
REMINDER_PREFIX = "Erinnerung"  # bold label prepended to delivered reminders

# Max model requests (tool round-trips) per run — guards against runaway loops.
AGENT_REQUEST_LIMIT = int(os.environ.get("AGENT_REQUEST_LIMIT", "20"))
# Per-command timeout for the bash tool (seconds).
BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "120"))

# Agent invocation logging. Set AGENT_LOG=1 in .env to enable.
# Logs full prompt + final output + usage per run to logs/<agent>.log
AGENT_LOG = os.environ.get("AGENT_LOG", "0").strip().lower() in ("1", "true", "yes")
AGENT_LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", BRIDGE_DIR / "logs"))
# Max characters shown per tool arg / tool result / text snippet in the trace.
AGENT_LOG_MAXLEN = int(os.environ.get("AGENT_LOG_MAXLEN", "800"))

# Voice transcription (OpenAI Whisper API). Needs OPENAI_API_KEY in the env.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")

# Caps concurrent agent runs across all bots; replaced in main() from the config.
AGENT_SLOTS = threading.Semaphore(1)

_history_lock = threading.Lock()        # serialises all history file writes


# --------------------------------------------------------------------------- #
# Agent (Pydantic AI)
# --------------------------------------------------------------------------- #

@dataclass
class Deps:
    """Per-run context handed to the agent's tools."""
    bot: "Bot"
    chat_id: int
    responded: bool = False              # True once chat_respond delivers a message
    sent: set = field(default_factory=set)     # texts already delivered (dedupe)
    delivered: list = field(default_factory=list)  # texts actually sent, in order (for the log)


def _oneline(value, limit: int = AGENT_LOG_MAXLEN) -> str:
    """Collapse a value to a single truncated line for the log."""
    s = "" if value is None else str(value)
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\t", "\\t")
    if len(s) > limit:
        s = s[:limit] + f"… (+{len(s) - limit} chars)"
    return s


def _tail(text: str, limit: int) -> str:
    """Keep the last `limit` chars (the newest history / the triggering message)."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return f"…(+{len(text) - limit} chars above)…\n" + text[-limit:]


def _part_args(part) -> str:
    """Render a tool call's arguments as k=v, truncated."""
    try:
        d = part.args_as_dict()
    except Exception:
        return _oneline(getattr(part, "args", ""))
    return ", ".join(f"{k}={_oneline(v)}" for k, v in d.items())


def _render_message(msg, step: list) -> list:
    """Render one ModelRequest/ModelResponse into log lines. `step` is a 1-item
    list used as a mutable counter."""
    lines = []
    if isinstance(msg, ModelResponse):
        step[0] += 1
        u = msg.usage
        tok = (f"in={u.input_tokens} out={u.output_tokens}"
               f" cache_r={u.cache_read_tokens}") if u else "usage=?"
        fin = f" · stop={msg.finish_reason}" if msg.finish_reason else ""
        lines.append(f"  step {step[0]} · {msg.model_name or '?'} · {tok}{fin}")
        had_part = False
        for p in msg.parts:
            kind = getattr(p, "part_kind", "")
            if kind == "thinking":
                if (p.content or "").strip():
                    lines.append(f"      💭 {_oneline(p.content)}")
                    had_part = True
            elif kind in ("tool-call", "builtin-tool-call"):
                name = p.tool_name
                icon = "🔎" if "search" in name.lower() else "🔧"
                lines.append(f"      {icon} {name}({_part_args(p)})")
                had_part = True
            elif kind == "text":
                if (p.content or "").strip():
                    lines.append(f"      💬 text: {_oneline(p.content)}")
                    had_part = True
        if not had_part:
            lines.append("      (no actionable parts — empty response)")
    elif isinstance(msg, ModelRequest):
        for p in msg.parts:
            kind = getattr(p, "part_kind", "")
            if kind in ("tool-return", "builtin-tool-return"):
                lines.append(f"        → {p.tool_name}: {_oneline(p.content)}")
            elif kind == "retry-prompt":
                lines.append(f"      ↻ retry: {_oneline(p.content)}")
            # user-prompt / system-prompt handled in the header, skipped here
    return lines


def _sum_usage(messages) -> dict:
    """Aggregate token usage and counts across all ModelResponse messages."""
    tot = {"requests": 0, "tools": 0, "input": 0, "output": 0,
           "cache_read": 0, "cache_write": 0}
    for msg in messages:
        if isinstance(msg, ModelResponse):
            tot["requests"] += 1
            tot["tools"] += sum(
                1 for p in msg.parts
                if getattr(p, "part_kind", "") in ("tool-call", "builtin-tool-call")
            )
            u = msg.usage
            if u:
                tot["input"] += u.input_tokens or 0
                tot["output"] += u.output_tokens or 0
                tot["cache_read"] += u.cache_read_tokens or 0
                tot["cache_write"] += u.cache_write_tokens or 0
    return tot


def _log_invocation(bot: "Bot", chat_id: int | None, prompt: str, messages: list,
                    deps: "Deps", duration: float, proactive: bool,
                    end_status: str) -> None:
    """Append a detailed trace of one agent run to logs/<agent>.log.

    Walks the full Pydantic AI message history so the log shows every model step,
    the tools it called (with arguments), the truncated tool results, token usage
    per step and in total, and what was actually delivered to the user (from
    `deps.delivered`, so deduped re-sends aren't miscounted). No-op if AGENT_LOG
    is off. Never raises — logging must not break a run.
    """
    if not AGENT_LOG:
        return
    try:
        AGENT_LOG_DIR.mkdir(exist_ok=True)
        log_file = AGENT_LOG_DIR / f"{bot.name}.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        totals = _sum_usage(messages)
        trigger = "proactive" if proactive else "interactive"

        step = [0]
        trace_lines = []
        # System prompt is static per agent — summarise it once instead of dumping.
        sys_len = 0
        for msg in messages:
            if isinstance(msg, ModelRequest):
                for p in msg.parts:
                    if getattr(p, "part_kind", "") == "system-prompt":
                        sys_len = len(p.content or "")
            trace_lines += _render_message(msg, step)

        bar = "═" * 80
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{bar}\n")
            fh.write(f"{ts} · {bot.name} · chat={chat_id} · {trigger} · end={end_status}\n")
            fh.write(f"model={bot.model} · duration={duration:.1f}s · "
                     f"requests={totals['requests']} · tools={totals['tools']}\n")
            fh.write(f"tokens: input={totals['input']} output={totals['output']} "
                     f"cache_read={totals['cache_read']} cache_write={totals['cache_write']}\n")
            fh.write("─" * 80 + "\n")
            fh.write(f"TRIGGER (prompt {len(prompt)} chars"
                     f"{f', system prompt {sys_len} chars' if sys_len else ''}):\n")
            fh.write("  " + _tail(prompt, 1200).replace("\n", "\n  ") + "\n\n")
            fh.write("TRACE:\n")
            fh.write(("\n".join(trace_lines) if trace_lines else "  (no messages captured)") + "\n\n")
            fh.write(f"DELIVERED: {len(deps.delivered)} message(s) to the user\n")
            for i, d in enumerate(deps.delivered, 1):
                fh.write(f"  {i}. {_oneline(d)}\n")
            fh.write(f"OUTCOME: responded={deps.responded} · end={end_status}\n")
    except Exception as e:  # logging must never break the run
        print(f"[{bot.name}] logging error: {e}")


def _system_prompt(workdir: Path) -> str:
    """Shared instructions + the agent's own AGENTS.md, read fresh each run."""
    parts = []
    if SHARED_INSTRUCTIONS.exists():
        s = SHARED_INSTRUCTIONS.read_text(encoding="utf-8").strip()
        if s:
            parts.append(s)
    agents_md = workdir / "AGENTS.md"
    if agents_md.exists():
        a = agents_md.read_text(encoding="utf-8").strip()
        if a:
            parts.append(a)
    return "\n\n---\n\n".join(parts) or "You are a helpful assistant."


def _tool_env(bot: "Bot", chat_id: int | None) -> dict:
    """Environment for the bash tool: shared + agent-local tools on PATH, plus
    the TELEGRAM_* vars that remind/schedule/food tools rely on."""
    env = os.environ.copy()
    tool_dirs = []
    if TOOLS_DIR.exists():
        tool_dirs += sorted(
            str(d) for d in TOOLS_DIR.iterdir()
            if d.is_dir() and d.name != "shared"
        )
    local_tools = bot.workdir / "tools"
    if local_tools.is_dir():
        tool_dirs.insert(0, str(local_tools))  # agent-local tools take priority
    if tool_dirs:
        env["PATH"] = os.pathsep.join(tool_dirs) + os.pathsep + env.get("PATH", "")
    if chat_id is not None:
        env["TELEGRAM_CHAT_ID"] = str(chat_id)
    env["TELEGRAM_BOT_TOKEN"] = bot.token
    return env


def _build_model(model_str: str | None):
    """Map an agents.yaml model (e.g. 'openai/gpt-5.5') to a Pydantic AI model.

    OpenAI models use the Responses API so native web search is available.
    Returns (model, web_search_supported).
    """
    name = model_str or "openai/gpt-4o-mini"
    provider, _, rest = name.partition("/")
    if provider == "openai" and rest:
        return OpenAIResponsesModel(rest), True
    # Other providers: let Pydantic AI infer the model; no native web search.
    return name.replace("/", ":", 1), False


def _build_agent(bot: "Bot") -> Agent:
    """Construct the agent for one run: model, system prompt, and tools.

    Built fresh per run so AGENTS.md / shared-instruction edits take effect
    without restarting the bridge (cheap next to the model call itself)."""
    model_obj, web_search = _build_model(bot.model)
    capabilities = [NativeTool(WebSearchTool())] if web_search else []
    agent = Agent(
        model_obj,
        deps_type=Deps,
        # Allow None so a truly empty final response is a valid end-of-run. Our
        # agents reply through the chat_respond tool and then have nothing left to
        # say; without this, Pydantic AI treats the empty response as non-actionable.
        output_type=str | None,
        system_prompt=_system_prompt(bot.workdir),
        model_settings=ModelSettings(timeout=float(bot.timeout)),
        capabilities=capabilities,
        # Kept low: after replying via chat_respond some models emit empty text
        # instead of ending cleanly, and each output retry is a wasted round-trip.
        # dedupe (in chat_respond) + the catch in run_agent handle the fallout.
        retries=1,
    )

    @agent.tool
    def bash(ctx: RunContext[Deps], command: str) -> str:
        """Run a shell command in your working directory and return its combined
        stdout/stderr. Use this for reading and writing your files and for your
        CLI tools (e.g. food-add, food-check, remind, schedule)."""
        env = _tool_env(ctx.deps.bot, ctx.deps.chat_id)
        try:
            r = subprocess.run(
                command, shell=True, cwd=ctx.deps.bot.workdir,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=BASH_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            return f"(command timed out after {BASH_TIMEOUT}s)"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return out or "(no output)"

    @agent.tool
    def chat_respond(ctx: RunContext[Deps], text: str) -> str:
        """Send a message to the user in Telegram — the ONLY way to reach them
        (your own final text is not shown). Call once per message; call again only
        for a genuinely different follow-up. Supports Telegram HTML and the
        [[buttons: ...]], [[inline: ...]], [[keyboard: ...]] and [[send: path]]
        markers. Use a literal \\n for line breaks. After sending, end your turn."""
        text = text.replace("\\n", "\n").replace("\\t", "\t")
        key = text.strip()
        # Guard against re-sending the same message: some models re-call this tool
        # instead of ending their turn, which would spam the user with duplicates.
        if key and key in ctx.deps.sent:
            return "You already sent that exact message. You are done — stop now."
        try:
            if deliver(ctx.deps.bot, ctx.deps.chat_id, text):
                ctx.deps.responded = True
                ctx.deps.sent.add(key)
                ctx.deps.delivered.append(text)
                return "Message sent. You are done — end your turn now."
            return "Nothing to send (message was empty after processing)."
        except Exception as e:
            return f"Failed to send message: {e}"

    return agent


def run_agent(bot: "Bot", prompt: str, chat_id: int,
              proactive: bool = False) -> tuple[str, bool]:
    """Run one prompt through the bot's Pydantic AI agent.

    Returns (final_output_text, responded). `responded` is True when the agent
    delivered at least one message via chat_respond; the worker uses the text as
    a fallback only when it didn't.
    """
    deps = Deps(bot=bot, chat_id=chat_id)
    t0 = time.monotonic()
    output, end_status = "", "clean"
    # capture_run_messages() gives us the full trace even when the run raises,
    # which is exactly the common "noisy end" case we want to see in the log.
    with capture_run_messages() as messages:
        try:
            agent = _build_agent(bot)
            result = asyncio.run(agent.run(
                prompt, deps=deps,
                usage_limits=UsageLimits(request_limit=AGENT_REQUEST_LIMIT),
            ))
            output = (result.output or "").strip()
        except UnexpectedModelBehavior as e:
            # Most commonly "Exceeded maximum output retries": the model kept
            # emitting empty text after finishing via chat_respond instead of
            # ending its turn. If it already delivered, that's benign — dedupe
            # stopped any duplicates.
            if deps.responded:
                end_status = f"noisy ({e})"
                print(f"[{bot.name}] agent ended noisily but delivered; ignoring: {e}")
            else:
                end_status = f"error ({e})"
                output = f"[agent error] {e}"
                print(f"[{bot.name}] agent run error: {e}")
        except Exception as e:
            end_status = f"error ({e})"
            output = f"[agent error] {e}"
            print(f"[{bot.name}] agent run error: {e}")
    _log_invocation(bot, chat_id, prompt, list(messages), deps,
                    time.monotonic() - t0, proactive, end_status)
    return output, deps.responded


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


def deliver(bot: "Bot", chat_id: int, text: str) -> bool:
    """Process outgoing markers in `text` and send it to Telegram.

    Shared by the native chat_respond tool and the worker's fallback path.
    Returns True if a text message was sent (files-only / empty returns False).
    """
    clean, to_send = split_outgoing(text, bot.workdir)
    clean, button_labels = split_buttons(clean)
    clean, inline_labels = split_inline(clean)
    clean, kbd = split_keyboard(clean)
    sent = False
    if clean:
        send(bot.api, chat_id, clean,
             buttons=button_labels or None,
             inline_buttons=inline_labels or None,
             reply_keyboard=kbd)
        log_turn(bot, chat_id, None, clean)
        sent = True
    for path in to_send:
        send_file(bot.api, chat_id, path)
    return sent


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
                with AGENT_SLOTS:  # global cap: at most N agents running at once
                    output, responded = run_agent(bot, prompt, chat_id, proactive)
            # Agents are instructed to reply via the chat_respond tool. If the
            # model skips it and returns text directly, fall back to delivering
            # that text so the user is never left with silence. On proactive
            # runs a silent agent is intentional — don't surface fallback text.
            if output and not responded and not proactive:
                deliver(bot, chat_id, output)
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
    """True if this agent uses markdown food data (.md or legacy .yaml present)."""
    wd = bot.workdir
    return (
        (wd / "stores.md").exists() or (wd / "list.md").exists() or
        (wd / "stores.yaml").exists() or (wd / "list.yaml").exists()
    )


def _food_parse_frontmatter(text: str):
    """Parse YAML frontmatter from a markdown file."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return yaml.safe_load(text[4:end])


def _food_load(bot: "Bot", filename: str):
    p = bot.workdir / filename
    if p.exists():
        text = p.read_text(encoding="utf-8")
        return _food_parse_frontmatter(text)
    # Migration fallback: old .yaml file
    yaml_p = p.with_suffix(".yaml")
    if yaml_p.exists():
        with yaml_p.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    return None


def _food_list_body(lst: dict, stores: list) -> str:
    store_map = {s["id"]: s["name"] for s in stores}
    date_str = lst.get("created_at", "")
    lines = [f"# Shopping List · {date_str}", ""]
    items = lst.get("items", [])
    if not items:
        return "\n".join(lines + ["*List is empty.*", ""])
    by_store: dict = {}
    for item in items:
        sname = store_map.get(item.get("store_id"), "Other")
        by_store.setdefault(sname, []).append(item)
    for sname in sorted(by_store):
        lines.append(f"## {sname}")
        for item in sorted(by_store[sname], key=lambda x: (x.get("checked", False), x["name"].lower())):
            check = "x" if item.get("checked") else " "
            qty = f" · {item['qty']}" if item.get("qty") else ""
            lines.append(f"- [{check}] {item['name']}{qty}")
        lines.append("")
    return "\n".join(lines)


def _food_save(bot: "Bot", filename: str, data) -> None:
    p = bot.workdir / filename
    fm = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    if "list" in filename:
        body = _food_list_body(data, _food_stores(bot))
    else:
        body = ""
    p.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")


def _food_stores(bot: "Bot") -> list:
    data = _food_load(bot, "stores.md")
    if isinstance(data, dict):
        return data.get("stores", [])
    return data or []


def _food_catalog(bot: "Bot") -> list:
    cat_dir = bot.workdir / "catalog"
    if not cat_dir.exists():
        return []
    products = []
    for f in sorted(cat_dir.glob("*.md")):
        data = _food_parse_frontmatter(f.read_text(encoding="utf-8"))
        if data and data.get("name"):
            products.append(data)
    return products


def _product_slug(name: str) -> str:
    """Convert a product name to a safe filename slug (matches food_data.py logic)."""
    slug = name.lower()
    for old, new in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        slug = slug.replace(old, new)
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def _catalog_on_list(lst: "dict | None") -> set:
    """Lowercase names of unchecked items currently on the list."""
    if not lst:
        return set()
    return {i["name"].lower() for i in lst.get("items", []) if not i.get("checked")}


def _catalog_keyboard(products: list, on_list: set, prefix: str = "cat") -> dict:
    """Inline keyboard for catalog: 🛒 for on-list items sorted to top."""
    on = sorted([p for p in products if p["name"].lower() in on_list], key=lambda p: p["name"].lower())
    off = sorted([p for p in products if p["name"].lower() not in on_list], key=lambda p: p["name"].lower())
    rows = []
    for p in on + off:
        slug = _product_slug(p["name"])[:56]  # Telegram callback_data max 64 bytes
        label = f"🛒 {p['name']}" if p["name"].lower() in on_list else p["name"]
        rows.append([{"text": label, "callback_data": f"{prefix}:{slug}"}])
    return {"inline_keyboard": rows}


def _food_list(bot: "Bot") -> "dict | None":
    data = _food_load(bot, "list.md")
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
            f"Modell: {bot.model or '(Standard)'}",
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
        stores = sorted(_food_stores(bot), key=lambda s: s["name"])
        if not stores:
            send(bot.api, chat_id, "Noch keine Geschäfte konfiguriert.")
        else:
            requests.post(f"{bot.api}/sendMessage", json={
                "chat_id": chat_id,
                "text": "Welches Geschäft?",
                "reply_markup": _store_picker_keyboard(stores, prefix="catstore"),
            }, timeout=30)
        return True

    if cmd == "/catalog":
        if not _food_active(bot):
            return False
        parts = text.strip().split(None, 1)
        store_filter = parts[1].strip() if len(parts) > 1 else None
        try:
            stores = _food_stores(bot)
            catalog = _food_catalog(bot)
            lst = _food_list(bot)
            on_list = _catalog_on_list(lst)
            if store_filter:
                store = next((s for s in stores if s["name"].lower() == store_filter.lower()), None)
                if not store:
                    send(bot.api, chat_id, f"Geschäft <b>{html.escape(store_filter)}</b> nicht gefunden.", parse_mode="HTML")
                    return True
                products = sorted([p for p in catalog if p.get("store", "").lower() == store["name"].lower()], key=lambda p: p["name"].lower())
                if not products:
                    send(bot.api, chat_id, f"Keine Produkte für <b>{html.escape(store['name'])}</b>.", parse_mode="HTML")
                    return True
                requests.post(f"{bot.api}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": f"{store['name']} — antippen zum Hinzufügen:",
                    "reply_markup": _catalog_keyboard(products, on_list, prefix="cat"),
                }, timeout=30)
            else:
                if not stores:
                    send(bot.api, chat_id, "Noch keine Geschäfte konfiguriert.")
                    return True
                requests.post(f"{bot.api}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "Welches Geschäft?",
                    "reply_markup": _store_picker_keyboard(stores, prefix="catstore"),
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
                                    _food_save(bot, "list.md", lst)
                            items = _food_enrich(_food_active_items(lst), stores) if lst else []
                        requests.post(f"{bot.api}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": _check_all_keyboard(items)}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] checkall callback error: {e}")
                elif data.startswith("catstore:"):
                    _ack()
                    try:
                        store_id_val = int(data.split(":")[1])
                        stores  = _food_stores(bot)
                        catalog = _food_catalog(bot)
                        lst     = _food_list(bot)
                        on_list = _catalog_on_list(lst)
                        if store_id_val == 0:
                            products = sorted(catalog, key=lambda p: (p.get("store", ""), p["name"].lower()))
                            if not products:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Kein Produkt im Katalog."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": "Alle Geschäfte — antippen zum Hinzufügen:", "reply_markup": _catalog_keyboard(products, on_list, prefix="catall")}, timeout=10)
                        else:
                            store = _food_find(stores, store_id_val)
                            store_name = store["name"] if store else ""
                            products = sorted([p for p in catalog if p.get("store", "").lower() == store_name.lower()], key=lambda p: p["name"].lower())
                            if not products:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"Keine Produkte für {store_name}."}, timeout=10)
                            else:
                                requests.post(f"{bot.api}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": f"{store_name} — antippen zum Hinzufügen:", "reply_markup": _catalog_keyboard(products, on_list, prefix="cat")}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] catstore callback error: {e}")
                elif data.startswith("cat:") or data.startswith("catall:"):
                    try:
                        all_view = data.startswith("catall:")
                        slug = data.split(":", 1)[1]
                        toast = ""
                        new_kb = None
                        with _food_lock:
                            stores  = _food_stores(bot)
                            catalog = _food_catalog(bot)
                            lst     = _food_list(bot)
                            if lst is None:
                                lst = {"items": []}
                            product = next((p for p in catalog if _product_slug(p["name"])[:56] == slug), None)
                            if product:
                                product_name = product["name"]
                                store_name   = product.get("store", "")
                                on_list      = _catalog_on_list(lst)
                                if product_name.lower() in on_list:
                                    lst["items"] = [i for i in lst.get("items", []) if i["name"].lower() != product_name.lower()]
                                    toast = f"✓ {product_name} entfernt"
                                else:
                                    store = next((s for s in stores if s["name"].lower() == store_name.lower()), None) if store_name else None
                                    store_id_v = store["id"] if store else None
                                    items = lst.get("items", [])
                                    existing = next((i for i in items if i["name"].lower() == product_name.lower()), None)
                                    if existing:
                                        existing["checked"] = False
                                        existing["status_changed_at"] = datetime.now().isoformat(timespec="seconds")
                                    else:
                                        max_id = max((i.get("id", 0) for i in items), default=0)
                                        items.append({"id": max_id + 1, "name": product_name, "store_id": store_id_v, "qty": None, "checked": False, "added_at": datetime.now().isoformat(timespec="seconds"), "status_changed_at": datetime.now().isoformat(timespec="seconds")})
                                        lst["items"] = items
                                    toast = f"✓ {product_name} hinzugefügt"
                                _food_save(bot, "list.md", lst)
                                on_list_after = _catalog_on_list(lst)
                                if all_view:
                                    products = sorted(catalog, key=lambda p: (p.get("store", ""), p["name"].lower()))
                                    new_kb = _catalog_keyboard(products, on_list_after, prefix="catall")
                                else:
                                    products = sorted([p for p in catalog if p.get("store", "").lower() == store_name.lower()], key=lambda p: p["name"].lower())
                                    new_kb = _catalog_keyboard(products, on_list_after, prefix="cat")
                        _ack(toast or None)
                        if new_kb:
                            requests.post(f"{bot.api}/editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": new_kb}, timeout=10)
                    except Exception as e:
                        print(f"[{bot.name}] cat callback error: {e}")
                        _ack()
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
                                    _food_save(bot, "list.md", lst)
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
                                    _food_save(bot, "list.md", lst)
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

        keep_context = bool(entry.get("continue", False))  # legacy; no longer used
        model = entry.get("model")  # None => _build_model falls back to a default
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


def main() -> None:
    global AGENT_SLOTS
    concurrency, bots = load_config()
    AGENT_SLOTS = threading.Semaphore(concurrency)
    if not OPENAI_API_KEY:
        print("WARNING: OPENAI_API_KEY is not set — agent runs and voice "
              "transcription will fail until it is added to .env.")
    print(
        f"Starting {len(bots)} bot(s); max {concurrency} agent(s) at once. "
        f"request_limit={AGENT_REQUEST_LIMIT}/run"
    )
    for bot in bots:
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
