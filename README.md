# Agent Space

A multi-agent Telegram bridge. Run several personal AI agents as independent Telegram bots — each with its own personality, working directory, and memory — all driven by a single Python process built on [Pydantic AI](https://ai.pydantic.dev).

Comes with a web UI for managing agents, editing agent instructions, and scheduling reminders without touching config files.

---

## How it works

Each agent is a Telegram bot with:
- its own **`AGENTS.md`** defining its role and personality
- its own **working directory** where it can read/write files
- its own **model** and replayed conversation history

When a message arrives, the bridge runs a **Pydantic AI** agent in-process, scoped to the agent's directory. The model sees the shared + agent-specific instructions, any conversation history you've configured, and the message. Each agent has three tools: **`chat_respond`** (send a Telegram message), **`bash`** (run shell commands / the agent's CLI tools in its workdir), and **web search** (OpenAI native). Replies reach the user through `chat_respond`.

The bridge is one Python process with three threads per agent (poller, worker, scheduler) and a global concurrency cap so it runs comfortably on a Raspberry Pi.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| An OpenAI API key | — | powers the agent model (Responses API + web search) and voice transcription |
| A Telegram account | — | to create bots and find your chat ID |

---

## Installation

### 1. Clone

```bash
git clone https://github.com/your-username/agent-space.git
cd agent-space
```

### 2. Python dependencies

```bash
cd agent-telegram-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
```

### 3. OpenAI API key

The agent model and voice transcription use OpenAI. Put your key in `.env` (see Step 3 of Quick start below):

```
OPENAI_API_KEY=sk-...
```

### 4. Make tool wrappers executable (Linux / macOS)

```bash
chmod +x agent-tools/remind/remind
```

---

## Quick start

### Step 1 — Create a Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the token it gives you (format: `123456:ABC-...`).

### Step 2 — Find your Telegram chat ID

Send any message to your new bot, then open:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `"chat": {"id": 12345678}` in the response. That number is your chat ID.

### Step 3 — Configure secrets

```bash
cd agent-telegram-bridge
cp .env.example .env
```

Edit `.env` and set your bot token and OpenAI key:

```
EXAMPLE_BOT_TOKEN=123456:ABC-your-token-here
OPENAI_API_KEY=sk-...
```

### Step 4 — Configure agents

```bash
cp agents.yaml.example agents.yaml
```

Edit `agents.yaml`:

```yaml
concurrency: 1

agents:
  - name: example
    token_env: EXAMPLE_BOT_TOKEN
    allowed_ids: [12345678]          # your Telegram chat ID from Step 2
    workdir: /absolute/path/to/agent-space/agents/example-agent
    continue: false
    timeout: 120
    history: 10
```

The `workdir` must be an absolute path. Set it to where `agents/example-agent/` lives on your machine.

### Step 5 — Run

```bash
cd agent-telegram-bridge
.venv/bin/python telegram_agent.py
```

Send a message to your agent. You should get a reply.

---

## Web UI

A browser-based UI for managing agents, viewing chat history, editing agent instructions, and managing reminders and schedules.

```bash
cd agent-telegram-bridge
.venv/bin/python webui.py
```

Open **http://localhost:7860**

Features:
- Dashboard with per-agent stats
- Create, edit, and delete agents
- Start and stop the bridge process
- Chat history viewer
- Reminder management
- Edit `AGENTS.md` per agent
- Cron schedule management
- Shared tool editor
- Shared instructions editor (`agents-common.md`)
- Global settings

---

## Adding your own agent

### 1. Create the agent folder

```bash
mkdir -p agents/my-agent
```

### 2. Write `AGENTS.md`

This file defines the agent's role and personality. The shared Telegram mechanics (how to reply, how to send files, how to use `remind`) are inherited automatically — only write what's specific to this agent.

```markdown
# My Agent

You are a personal recipe assistant. You maintain a collection of recipes
in the `recipes/` folder of your working directory.

## What you do
- Save new recipes the user describes or pastes.
- Search and retrieve recipes on request.
- Suggest recipes based on available ingredients.
```

### 3. Add the agent to `agents.yaml`

```yaml
  - name: recipes
    token_env: RECIPES_BOT_TOKEN
    allowed_ids: [12345678]
    workdir: /absolute/path/to/agents/my-agent
    continue: false
    timeout: 300
    history: 15
```

### 4. Add the token to `.env`

```
RECIPES_BOT_TOKEN=123456:ABC-another-token
```

### 5. Restart the bridge

The bridge reads config once at startup, so a restart is needed when you add or change agents.

---

## Shared instructions

All agents inherit `agent-tools/shared/agents-common.md` automatically. It covers:

- Telegram reply rules (plain text, concise, no internal paths)
- The `[[send: path]]` file-send marker for returning files to the user
- How to call the `remind` tool
- Proactive (scheduled) message behaviour

Edit this file via the web UI (Shared Instructions) or directly. Changes take effect at the agent's next run — no restart needed.

---

## Shared tools

Tools in `agent-tools/` are available to every agent on their PATH. Each tool lives in its own subfolder:

```
agent-tools/
  remind/
    remind.py       main implementation
    remind          POSIX wrapper (Linux / macOS)
    remind.cmd      Windows wrapper
```

### Built-in: `remind`

Schedule a one-shot reminder back to the user:

```bash
remind "in 2 hours"   "Take the cake out of the oven"
remind "tomorrow 9am" "Send the grant draft"
remind "15:00"        "Stand-up meeting"
```

### Adding a new tool

Use the web UI (Tools → New Tool) or create a subfolder manually:

```
agent-tools/
  my-tool/
    my-tool.py      implementation
    my-tool         POSIX wrapper
    my-tool.cmd     Windows wrapper
```

The POSIX wrapper template:
```sh
#!/bin/sh
dir=$(dirname "$0")
exec python3 "$dir/my-tool.py" "$@"
```

Any new tool subfolder is picked up automatically — no bridge restart needed for PATH changes (takes effect on the agent's next run). Agents call these tools through the **`bash`** tool.

---

## Agent configuration reference

```yaml
concurrency: 1               # max agents running at once

agents:
  - name: example            # used in logs, history filenames, and the web UI
    token_env: EXAMPLE_BOT_TOKEN  # name of the env var holding the Telegram token
    allowed_ids: [12345678]  # whitelist of Telegram chat IDs; empty = bridge won't start
    workdir: /path/to/agent  # agent's working directory (must be outside bridge folder)
    continue: false          # retained for compatibility; no longer has an effect
    model: openai/gpt-4o-mini  # provider/model; OpenAI models use the Responses API
    timeout: 300             # per model-request timeout (seconds)
    history: 20              # replay last N messages as context (0 = off)
    schedules:               # optional cron-triggered proactive runs
      - cron: "0 8 * * *"
        prompt: "Morning check-in."
        chat_id: 12345678    # optional; defaults to the first entry in allowed_ids
```

Memory is provided by `history`: the bridge replays the last N Telegram messages (it logs the conversation itself) as context on each run. The `continue` field is kept so old configs still load, but it no longer does anything — sessions were an OpenCode concept. Use `history` to give an agent context. `model` should be an `openai/*` model to get web search (they run on the OpenAI Responses API); other providers work but without native web search.

---

## Deployment on a server (Raspberry Pi / Linux VPS)

### First-time setup

```bash
git clone https://github.com/your-username/agent-space.git
cd agent-space
./deploy/setup.sh    # creates venv, installs deps, sets chmod, prints next steps
```

Then manually:

```bash
# Secrets (bot tokens + OPENAI_API_KEY)
cp agent-telegram-bridge/.env.example agent-telegram-bridge/.env
nano agent-telegram-bridge/.env

# Machine-specific config
cp agent-telegram-bridge/agents.yaml.example agent-telegram-bridge/agents.yaml
nano agent-telegram-bridge/agents.yaml   # set absolute workdir paths for this machine
```

### systemd service

Edit `agent-telegram-bridge/telegram-agents.service` and update the four paths and the `User=` to match your server, then install:

```bash
sudo cp agent-telegram-bridge/telegram-agents.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-agents

# Follow logs
journalctl -u telegram-agents -f
```

### Updating after code changes

On your dev machine:
```bash
git add agent-telegram-bridge/ agent-tools/ agents/example-agent/ deploy/ README.md
git commit -m "describe what changed"
git push
```

On the server:
```bash
./deploy/update.sh   # git pull + pip install + systemctl restart
```

If `requirements.txt` did not change, the pip step is a no-op and adds only a few seconds.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named 'pydantic_ai'` | `pip install -r requirements.txt` into the interpreter that runs the bridge (the venv, if you use one) |
| `OPENAI_API_KEY is not set` at startup | Add `OPENAI_API_KEY` to `.env` — agent runs and transcription need it |
| Web search / recipe cron fails | Model must be an `openai/*` model (uses the Responses API); check `OPENAI_API_KEY` |
| Bot ignores messages | Chat ID not in `allowed_ids` — check logs for "ignored message from" |
| Bridge refuses to start | `workdir` is inside the bridge folder, or `allowed_ids` is empty |
| Voice transcription fails | `OPENAI_API_KEY` missing from `.env` |
| Reminders never arrive | `croniter` not installed, or malformed JSON in `reminders/` |
| `remind` not found | `chmod +x agent-tools/remind/remind` on Linux |

---

## Security notes

- **Tokens live in `.env` only.** `agents.yaml` never contains secrets.
- **`allowed_ids` is a hard whitelist.** The bridge drops all messages from unknown chat IDs.
- **Workdir isolation.** The bridge refuses to start if any agent's `workdir` is inside or equal to the bridge folder — this prevents agents from accessing `.env` via shell.
- **Shared tools** are kept outside the bridge folder deliberately, since agents have shell access to everything on their PATH.
