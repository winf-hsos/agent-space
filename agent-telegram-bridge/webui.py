#!/usr/bin/env python3
"""Web UI for managing the Telegram agent bridge.

Run:  python webui.py
Then open:  http://localhost:7860
"""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

BRIDGE_DIR = Path(__file__).resolve().parent
load_dotenv(BRIDGE_DIR / ".env")  # populate os.environ from .env before reading any env vars
REPO_DIR = BRIDGE_DIR.parent                          # agent-space/
_agents_dir_env = os.environ.get("AUTO_UPDATE_AGENTS_DIR", "")
MY_AGENTS_DIR: Optional[Path] = Path(_agents_dir_env) if _agents_dir_env else None
CONFIG_PATH = BRIDGE_DIR / "agents.yaml"
ENV_PATH = BRIDGE_DIR / ".env"
HISTORY_DIR = BRIDGE_DIR / "history"
TOOLS_DIR = Path(os.environ.get("AGENT_TOOLS_DIR", BRIDGE_DIR.parent / "agent-tools")).resolve()
SHARED_INSTRUCTIONS_PATH = TOOLS_DIR / "shared" / "agents-common.md"
AUTO_UPDATE_INTERVAL = int(os.environ.get("AUTO_UPDATE_INTERVAL", "0"))  # 0 = disabled

_bridge_proc: Optional[subprocess.Popen] = None
_bridge_lock = threading.Lock()
_last_update_check: Optional[str] = None
_last_update_result: Optional[str] = None  # "up-to-date" | "updated" | "error"


# ── Auto-update ────────────────────────────────────────────────────────────────

def _git_head(repo: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _git_pull(repo: Path, force: bool = False) -> bool:
    """Pull repo. Returns True if HEAD changed (new commits arrived).

    force=True: fetch + reset --hard so remote always wins over local changes.
    """
    before = _git_head(repo)
    if not before:
        return False
    try:
        if force:
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "origin"],
                capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", "FETCH_HEAD"],
                capture_output=True, text=True, timeout=30,
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo), "pull", "--quiet"],
                capture_output=True, text=True, timeout=60,
            )
    except Exception as e:
        print(f"[webui] git pull failed for {repo.name}: {e}")
        return False
    after = _git_head(repo)
    return bool(after) and before != after


def _auto_update_loop() -> None:
    global _last_update_check, _last_update_result
    while True:
        time.sleep(AUTO_UPDATE_INTERVAL)
        _last_update_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            changed = _git_pull(REPO_DIR)
            if MY_AGENTS_DIR and MY_AGENTS_DIR.exists():
                changed |= _git_pull(MY_AGENTS_DIR, force=True)
            if changed:
                print("[webui] New commits — updating deps and restarting bridge")
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "-r",
                     str(BRIDGE_DIR / "requirements.txt")],
                    timeout=120,
                )
                was_running = _bridge_running()
                _do_stop_bridge()
                if was_running:
                    time.sleep(2)
                    _do_start_bridge()
                    print("[webui] Bridge restarted with updated code")
                _last_update_result = "updated"
            else:
                _last_update_result = "up-to-date"
        except Exception as e:
            print(f"[webui] Auto-update error: {e}")
            _last_update_result = "error"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if AUTO_UPDATE_INTERVAL > 0:
        t = threading.Thread(target=_auto_update_loop, daemon=True)
        t.start()
        print(f"[webui] Auto-update enabled — polling every {AUTO_UPDATE_INTERVAL}s")
    yield


app = FastAPI(title="Agent Manager", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BRIDGE_DIR / "templates"))


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {"concurrency": 1, "agents": []}
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("concurrency", 1)
    data.setdefault("agents", [])
    return data


def _save_raw(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _find_bot(cfg: dict, name: str) -> Optional[dict]:
    for b in cfg.get("agents", []):
        if b.get("name") == name:
            return b
    return None


def _parse_ids(raw: str) -> list[int]:
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    return ids


# ── .env ───────────────────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _set_env(key: str, value: str) -> None:
    ENV_PATH.touch()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── History ────────────────────────────────────────────────────────────────────

def _list_chats(bot_name: str) -> list[dict]:
    if not HISTORY_DIR.exists():
        return []
    prefix = f"{bot_name}_"
    results = []
    for f in HISTORY_DIR.glob(f"{prefix}*.jsonl"):
        chat_id = f.stem[len(prefix):]
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        last_t: Optional[int] = None
        if lines:
            try:
                last_t = json.loads(lines[-1]).get("t")
            except Exception:
                pass
        results.append({"chat_id": chat_id, "count": len(lines), "last_t": last_t})
    results.sort(key=lambda x: x["last_t"] or 0, reverse=True)
    return results


def _read_history(bot_name: str, chat_id: str, limit: int = 300) -> list[dict]:
    path = HISTORY_DIR / f"{bot_name}_{chat_id}.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


# ── Reminders ──────────────────────────────────────────────────────────────────

def _list_reminders(bot: dict) -> list[dict]:
    rem_dir = Path(bot["workdir"]) / "reminders"
    if not rem_dir.exists():
        return []
    items = []
    for f in rem_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            items.append(data)
        except Exception:
            pass
    items.sort(key=lambda x: x.get("due", ""))
    return items


# ── Bridge ─────────────────────────────────────────────────────────────────────

def _bridge_running() -> bool:
    return _bridge_proc is not None and _bridge_proc.poll() is None


def _bridge_status() -> dict:
    running = _bridge_running()
    return {
        "running": running,
        "pid": _bridge_proc.pid if running else None,
        "last_update_check": _last_update_check,
        "last_update_result": _last_update_result,
    }


def _do_start_bridge() -> None:
    global _bridge_proc
    with _bridge_lock:
        if _bridge_running():
            return
        load_dotenv(BRIDGE_DIR / ".env", override=True)
        _bridge_proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_DIR / "telegram_agent.py")],
            cwd=str(BRIDGE_DIR),
        )


def _do_stop_bridge() -> None:
    global _bridge_proc
    with _bridge_lock:
        if _bridge_proc and _bridge_proc.poll() is None:
            _bridge_proc.terminate()
            try:
                _bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bridge_proc.kill()
        _bridge_proc = None


# ── Template globals ───────────────────────────────────────────────────────────

def _parse_ts(ts) -> "float | None":
    if ts is None:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def _fmt_ts(ts) -> str:
    t = _parse_ts(ts)
    if t is None:
        return "—"
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _fmt_rel(ts) -> str:
    t = _parse_ts(ts)
    if t is None:
        return "never"
    try:
        delta = datetime.now().timestamp() - t
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except Exception:
        return "—"


templates.env.filters["fmt_ts"] = _fmt_ts
templates.env.filters["fmt_rel"] = _fmt_rel


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = _load_raw()
    bots_view = []
    for bot in cfg.get("agents", []):
        chats = _list_chats(bot["name"])
        reminders = _list_reminders(bot)
        bots_view.append({
            **bot,
            "chat_count": len(chats),
            "msg_count": sum(c["count"] for c in chats),
            "last_t": chats[0]["last_t"] if chats else None,
            "reminder_count": len(reminders),
        })
    return templates.TemplateResponse(request, "dashboard.html", {
        "agents": bots_view,
        "bridge": _bridge_status(),
        "concurrency": cfg.get("concurrency", 1),
    })


# ── Bridge control ─────────────────────────────────────────────────────────────

@app.post("/api/bridge/start")
async def bridge_start():
    if _bridge_running():
        return JSONResponse({"ok": True, "status": "already_running", "pid": _bridge_proc.pid})
    _do_start_bridge()
    return JSONResponse({"ok": True, "status": "started", "pid": _bridge_proc.pid})


@app.post("/api/bridge/stop")
async def bridge_stop():
    _do_stop_bridge()
    return JSONResponse({"ok": True, "status": "stopped"})


@app.post("/api/bridge/restart")
async def bridge_restart():
    _do_stop_bridge()
    time.sleep(1)
    _do_start_bridge()
    return JSONResponse({"ok": True, "status": "restarted", "pid": _bridge_proc.pid if _bridge_running() else None})


@app.get("/api/bridge/status")
async def api_bridge_status():
    return JSONResponse(_bridge_status())


# ── Bot create ─────────────────────────────────────────────────────────────────

@app.get("/agents/new", response_class=HTMLResponse)
async def bot_new_form(request: Request):
    return templates.TemplateResponse(request, "agent_form.html", {
        "agent": None, "env": _load_env(), "error": None,
    })


@app.post("/agents/new", response_class=HTMLResponse)
async def bot_create(
    request: Request,
    name: str = Form(...),
    token_env: str = Form(...),
    token_value: str = Form(""),
    allowed_ids: str = Form(""),
    workdir: str = Form(...),
    model: str = Form(""),
    bot_continue: str = Form("false"),
    timeout: int = Form(600),
    history: int = Form(0),
    init_folder: str = Form("false"),
):
    cfg = _load_raw()
    if _find_bot(cfg, name):
        return templates.TemplateResponse(request, "agent_form.html", {
            "agent": None, "env": _load_env(),
            "error": f"An agent named '{name}' already exists.",
        })
    ids = _parse_ids(allowed_ids)
    if not ids:
        return templates.TemplateResponse(request, "agent_form.html", {
            "agent": None, "env": _load_env(),
            "error": "At least one Telegram chat ID is required in Allowed IDs.",
        })
    entry: dict = {
        "name": name,
        "token_env": token_env,
        "allowed_ids": ids,
        "workdir": workdir,
        "continue": bot_continue == "true",
        "timeout": timeout,
        "history": history,
    }
    if model.strip():
        entry["model"] = model.strip()
    cfg["agents"].append(entry)
    _save_raw(cfg)
    if token_value.strip():
        _set_env(token_env, token_value.strip())
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    if init_folder == "true":
        (wd / "reminders").mkdir(exist_ok=True)
        (wd / "inbox").mkdir(exist_ok=True)
        agents_md = wd / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(
                _STARTER_AGENTS_MD.format(name=name.capitalize()),
                encoding="utf-8",
            )
    return RedirectResponse(f"/agents/{name}", status_code=303)


# ── Bot detail ─────────────────────────────────────────────────────────────────

@app.get("/agents/{name}", response_class=HTMLResponse)
async def bot_detail(request: Request, name: str, tab: str = "overview", chat_id: str = ""):
    cfg = _load_raw()
    bot = _find_bot(cfg, name)
    if not bot:
        raise HTTPException(404, f"Agent '{name}' not found")
    chats = _list_chats(name)
    reminders = _list_reminders(bot)
    agents_md_path = Path(bot["workdir"]) / "AGENTS.md"
    agents_md = agents_md_path.read_text(encoding="utf-8") if agents_md_path.exists() else ""
    selected_chat = chat_id or (chats[0]["chat_id"] if chats else "")
    history_entries: list[dict] = []
    if selected_chat and tab == "history":
        history_entries = _read_history(name, selected_chat)
    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": bot,
        "tab": tab,
        "chats": chats,
        "chat_id": selected_chat,
        "history_entries": history_entries,
        "reminders": reminders,
        "agents_md": agents_md,
        "env": _load_env(),
        "bridge": _bridge_status(),
    })


# ── Bot edit ───────────────────────────────────────────────────────────────────

@app.get("/agents/{name}/edit", response_class=HTMLResponse)
async def bot_edit_form(request: Request, name: str):
    cfg = _load_raw()
    bot = _find_bot(cfg, name)
    if not bot:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "agent_form.html", {
        "agent": bot, "env": _load_env(), "error": None,
    })


@app.post("/agents/{name}/edit")
async def bot_update(
    name: str,
    token_env: str = Form(...),
    token_value: str = Form(""),
    allowed_ids: str = Form(""),
    workdir: str = Form(...),
    model: str = Form(""),
    bot_continue: str = Form("false"),
    timeout: int = Form(600),
    history: int = Form(0),
):
    cfg = _load_raw()
    ids = _parse_ids(allowed_ids)
    for i, bot in enumerate(cfg.get("agents", [])):
        if bot.get("name") == name:
            updated: dict = {
                "name": name,
                "token_env": token_env,
                "allowed_ids": ids,
                "workdir": workdir,
                "continue": bot_continue == "true",
                "timeout": timeout,
                "history": history,
            }
            if model.strip():
                updated["model"] = model.strip()
            if "schedules" in bot:
                updated["schedules"] = bot["schedules"]
            cfg["agents"][i] = updated
            break
    _save_raw(cfg)
    if token_value.strip():
        _set_env(token_env, token_value.strip())
    return RedirectResponse(f"/agents/{name}", status_code=303)


@app.post("/agents/{name}/delete")
async def bot_delete(name: str):
    cfg = _load_raw()
    cfg["agents"] = [b for b in cfg.get("agents", []) if b.get("name") != name]
    _save_raw(cfg)
    return RedirectResponse("/", status_code=303)


# ── AGENTS.md ─────────────────────────────────────────────────────────────────

@app.post("/agents/{name}/agents-md")
async def save_agents_md(name: str, content: str = Form(...)):
    cfg = _load_raw()
    bot = _find_bot(cfg, name)
    if not bot:
        raise HTTPException(404)
    Path(bot["workdir"]).mkdir(parents=True, exist_ok=True)
    (Path(bot["workdir"]) / "AGENTS.md").write_text(content, encoding="utf-8")
    return RedirectResponse(f"/agents/{name}?tab=instructions", status_code=303)


# ── Reminders ─────────────────────────────────────────────────────────────────

@app.delete("/api/agents/{name}/reminders/{filename}")
async def delete_reminder(name: str, filename: str):
    cfg = _load_raw()
    bot = _find_bot(cfg, name)
    if not bot:
        raise HTTPException(404)
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = Path(bot["workdir"]) / "reminders" / filename
    if path.exists():
        path.unlink()
    return JSONResponse({"ok": True})


# ── Schedules ─────────────────────────────────────────────────────────────────

@app.post("/api/agents/{name}/schedules")
async def add_schedule(name: str, request: Request):
    body = await request.json()
    cfg = _load_raw()
    for bot in cfg["agents"]:
        if bot.get("name") == name:
            entry: dict = {"cron": body["cron"], "prompt": body["prompt"]}
            if body.get("chat_id"):
                try:
                    entry["chat_id"] = int(body["chat_id"])
                except (ValueError, TypeError):
                    pass
            bot.setdefault("schedules", []).append(entry)
    _save_raw(cfg)
    return JSONResponse({"ok": True})


@app.delete("/api/agents/{name}/schedules/{idx}")
async def delete_schedule(name: str, idx: int):
    cfg = _load_raw()
    for bot in cfg["agents"]:
        if bot.get("name") == name:
            sched = bot.get("schedules", [])
            if 0 <= idx < len(sched):
                sched.pop(idx)
    _save_raw(cfg)
    return JSONResponse({"ok": True})


# ── History API (for JS chat loader) ──────────────────────────────────────────

@app.get("/api/agents/{name}/history/{chat_id}")
async def api_history(name: str, chat_id: str):
    return JSONResponse({"entries": _read_history(name, chat_id)})


# ── Tools ─────────────────────────────────────────────────────────────────────

def _list_tools() -> list[dict]:
    if not TOOLS_DIR.exists():
        return []
    tools = []
    for d in sorted(TOOLS_DIR.iterdir()):
        if d.is_dir() and d.name != "shared":
            files = sorted(f.name for f in d.iterdir() if f.is_file())
            tools.append({"name": d.name, "path": str(d), "files": files})
    return tools


def _tool_dir(name: str) -> Path:
    return TOOLS_DIR / name


def _is_text(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


_STARTER_AGENTS_MD = """\
# {name}

You are a friendly, general-purpose conversational assistant. You have no special domain, tools, or files to manage — your only job is to have a good conversation.

## Personality

- Warm and direct. No filler phrases like "Certainly!" or "Great question!".
- Curious. Ask a follow-up when something the user said is interesting.
- Honest about what you don't know. Don't make things up.
- Concise. One or two sentences is usually enough.

## What you can do

- Answer general knowledge questions.
- Help think through problems or decisions.
- Brainstorm, draft short texts, explain concepts.
- Have a casual chat.

## What you cannot do

- Access the internet or real-time information.
- Remember previous conversations (unless session memory is enabled in config).
- Run code or manage files — this agent has no special tools beyond `remind`.

## Tone

Match the user's tone. If they're casual, be casual. If they want a precise answer, be precise. Keep replies short unless depth is clearly needed.
"""

_TOOL_PY_STARTER = '''\
#!/usr/bin/env python3
"""
{name} -- describe what this tool does.

Usage:
    {name} <arg1> [arg2]

Called by agents from the shell; the bridge puts this folder on PATH.
"""
import sys


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: {name} <arg>", file=sys.stderr)
        return 2
    # TODO: implement
    print(f"Hello from {name}: {{args}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_TOOL_SH_STARTER = '''\
#!/bin/sh
# POSIX shim so agents can call `{name}` on Linux/macOS.
dir=$(dirname "$0")
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$dir/{name}.py" "$@"
fi
exec python "$dir/{name}.py" "$@"
'''

_TOOL_CMD_STARTER = '''\
@echo off
rem Windows shim so agents can call `{name}` (resolved via PATHEXT).
python "%~dp0{name}.py" %*
'''


@app.get("/tools", response_class=HTMLResponse)
async def tools_list(request: Request):
    return templates.TemplateResponse(request, "tools.html", {
        "tools": _list_tools(),
    })


@app.get("/tools/new", response_class=HTMLResponse)
async def tool_new_form(request: Request):
    return templates.TemplateResponse(request, "tool_detail.html", {
        "tool": None, "files": [], "active_file": None,
        "file_content": "", "error": None,
    })


@app.post("/tools/new")
async def tool_create(name: str = Form(...)):
    name = name.strip().lower().replace(" ", "-")
    if not name or "/" in name or "\\" in name or ".." in name:
        return RedirectResponse("/tools", status_code=303)
    d = _tool_dir(name)
    if d.exists():
        return RedirectResponse(f"/tools/{name}", status_code=303)
    d.mkdir(parents=True)
    (d / f"{name}.py").write_text(_TOOL_PY_STARTER.format(name=name), encoding="utf-8")
    (d / name).write_text(_TOOL_SH_STARTER.format(name=name), encoding="utf-8")
    (d / f"{name}.cmd").write_text(_TOOL_CMD_STARTER.format(name=name), encoding="utf-8")
    return RedirectResponse(f"/tools/{name}?file={name}.py", status_code=303)


@app.get("/tools/{tool_name}", response_class=HTMLResponse)
async def tool_detail(request: Request, tool_name: str, file: str = ""):
    d = _tool_dir(tool_name)
    if not d.exists() or not d.is_dir():
        raise HTTPException(404, f"Tool '{tool_name}' not found")
    files = sorted(f.name for f in d.iterdir() if f.is_file())
    # Pick which file to show in the editor
    active = file if file in files else (files[0] if files else "")
    content = ""
    is_text = False
    if active:
        p = d / active
        is_text = _is_text(p)
        if is_text:
            content = p.read_text(encoding="utf-8")
    return templates.TemplateResponse(request, "tool_detail.html", {
        "tool": {"name": tool_name, "path": str(d)},
        "files": files,
        "active_file": active,
        "file_content": content,
        "is_text": is_text,
        "error": None,
    })


@app.post("/tools/{tool_name}/save")
async def tool_save_file(tool_name: str, filename: str = Form(...), content: str = Form(...)):
    d = _tool_dir(tool_name)
    if not d.exists():
        raise HTTPException(404)
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    (d / filename).write_text(content, encoding="utf-8")
    return RedirectResponse(f"/tools/{tool_name}?file={filename}", status_code=303)


@app.post("/tools/{tool_name}/new-file")
async def tool_new_file(tool_name: str, filename: str = Form(...)):
    d = _tool_dir(tool_name)
    if not d.exists():
        raise HTTPException(404)
    filename = filename.strip()
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    p = d / filename
    if not p.exists():
        p.write_text("", encoding="utf-8")
    return RedirectResponse(f"/tools/{tool_name}?file={filename}", status_code=303)


@app.delete("/api/tools/{tool_name}/files/{filename}")
async def tool_delete_file(tool_name: str, filename: str):
    d = _tool_dir(tool_name)
    if not d.exists():
        raise HTTPException(404)
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    p = d / filename
    if p.exists():
        p.unlink()
    return JSONResponse({"ok": True})


@app.post("/tools/{tool_name}/delete")
async def tool_delete(tool_name: str):
    import shutil
    d = _tool_dir(tool_name)
    if d.exists() and d.is_dir():
        shutil.rmtree(d)
    return RedirectResponse("/tools", status_code=303)


# ── Shared instructions ───────────────────────────────────────────────────────

@app.get("/shared-instructions", response_class=HTMLResponse)
async def shared_instructions_page(request: Request):
    content = SHARED_INSTRUCTIONS_PATH.read_text(encoding="utf-8") if SHARED_INSTRUCTIONS_PATH.exists() else ""
    return templates.TemplateResponse(request, "shared_instructions.html", {
        "content": content,
        "path": str(SHARED_INSTRUCTIONS_PATH),
        "exists": SHARED_INSTRUCTIONS_PATH.exists(),
    })


@app.post("/shared-instructions")
async def save_shared_instructions(content: str = Form(...)):
    SHARED_INSTRUCTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_INSTRUCTIONS_PATH.write_text(content, encoding="utf-8")
    return RedirectResponse("/shared-instructions", status_code=303)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = _load_raw()
    return templates.TemplateResponse(request, "settings.html", {
        "concurrency": cfg.get("concurrency", 1),
        "env": _load_env(),
    })


@app.post("/settings")
async def save_settings(
    concurrency: int = Form(1),
    openai_api_key: str = Form(""),
    transcribe_model: str = Form(""),
    opencode_bin: str = Form(""),
):
    cfg = _load_raw()
    cfg["concurrency"] = max(1, concurrency)
    _save_raw(cfg)
    if openai_api_key.strip():
        _set_env("OPENAI_API_KEY", openai_api_key.strip())
    if transcribe_model.strip():
        _set_env("TRANSCRIBE_MODEL", transcribe_model.strip())
    if opencode_bin.strip():
        _set_env("OPENCODE_BIN", opencode_bin.strip())
    return RedirectResponse("/settings", status_code=303)


if __name__ == "__main__":
    uvicorn.run("webui:app", host="0.0.0.0", port=7860, reload=False)
