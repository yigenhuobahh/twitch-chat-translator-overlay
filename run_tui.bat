@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter repository directory.
  if not defined CI pause
  exit /b 1
)

set "PY=python"
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo [FAIL] Python 3.10+ not found. Run install.bat first.
    if not defined CI pause
    exit /b 1
  )
)
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 (
  echo [FAIL] Selected Python must be runnable and version 3.10+: %PY%
  if not defined CI pause
  exit /b 1
)
"%PY%" -c "import textual" >nul 2>&1
if errorlevel 1 (
  echo [TUI] Textual is required. Run install.bat to install the standard dependencies.
  if not defined CI pause
  exit /b 1
)
"%PY%" scripts\tui_run.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%
