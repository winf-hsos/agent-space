@echo off
rem Windows shim so agents can call `remind ...` (resolved via PATHEXT).
python "%~dp0remind.py" %*
