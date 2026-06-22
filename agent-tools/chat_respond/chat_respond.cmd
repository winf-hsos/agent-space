@echo off
rem Windows shim so agents can call `chat_respond ...` (resolved via PATHEXT).
python "%~dp0chat_respond.py" %*
