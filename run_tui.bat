@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 exit /b 1

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import textual" >nul 2>&1
if errorlevel 1 (
  echo [TUI] Optional dependency is not installed.
  echo Run: "%PY%" -m pip install ".[tui]"
  if not defined CI pause
  exit /b 1
)
"%PY%" scripts\tui_run.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%
