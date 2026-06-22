@echo off
rem Windows shim so agents can call `task_pause ...` (resolved via PATHEXT).
python "%~dp0task_pause.py" %*
