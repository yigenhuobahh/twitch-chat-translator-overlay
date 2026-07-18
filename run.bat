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

REM Double-click entry: full Textual UI. Existing commands and drag/drop stay CLI-compatible.
if /I "%~1"=="" (
  call "%~dp0run_tui.bat"
  exit /b %ERRORLEVEL%
)

call "%~dp0run_cli.bat" %*
exit /b %ERRORLEVEL%
