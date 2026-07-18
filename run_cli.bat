@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter repository directory.
  if not defined CI pause
  exit /b 1
)

REM ASCII-only launcher. All Chinese UI is in Python (UTF-8).
REM This is the advanced/recovery CLI entry point. run.bat opens the TUI by default.

if not exist "scripts\render_cn_chat.py" (
  echo [FAIL] Run from repo root.
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

if /I "%~1"=="" goto MENU
if /I "%~1"=="menu" goto MENU
if /I "%~1"=="new" goto NEW
if /I "%~1"=="init-job" goto NEW
if /I "%~1"=="quick" goto QUICK
if /I "%~1"=="demo" goto DEMO
if /I "%~1"=="list" goto LIST
if /I "%~1"=="help" goto HELP
if /I "%~1"=="-h" goto PIPELINE
if /I "%~1"=="--help" goto PIPELINE
if /I "%~1"=="doctor" goto DOCTOR

set "FIRST=%~1"
if "%FIRST:~0,1%"=="-" goto PIPELINE

REM Preserve explicit media + CLI invocations. Dragging exactly video + HTML
REM stays on the safe 10-second original-preview route below.
if not "%~3"=="" if exist "%~1" goto PIPELINE

REM Drag video + chat HTML onto this file for a no-API 10-second preview.
"%PY%" scripts\job_wizard.py drop %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAIL] exit %RC%. Try: run_cli.bat doctor
  if not defined CI pause
)
exit /b %RC%

:PIPELINE
"%PY%" scripts\render_cn_chat.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAIL] exit %RC%. Try: run_cli.bat doctor
  if not defined CI pause
)
exit /b %RC%

:MENU
"%PY%" scripts\job_wizard.py menu
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%

:NEW
"%PY%" scripts\render_cn_chat.py --init-job
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%

:QUICK
"%PY%" scripts\job_wizard.py quick
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%

:DEMO
"%PY%" scripts\quick_demo.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%

:LIST
"%PY%" scripts\render_cn_chat.py --list-jobs
set "RC=%ERRORLEVEL%"
if not defined CI pause
exit /b %RC%

:DOCTOR
"%PY%" scripts\render_cn_chat.py --doctor
set "RC=%ERRORLEVEL%"
if not defined CI pause
exit /b %RC%

:HELP
echo run_cli.bat           Advanced Chinese CLI menu
echo run_cli.bat quick     First-run setup + job wizard
echo run_cli.bat demo      Offline demo (no translation API)
echo Drag video + chat HTML onto run_cli.bat for a 10-second preview
echo run_cli.bat NAME      Run jobs\NAME.yaml
echo run_cli.bat doctor    Environment check
if not defined CI pause
exit /b 0
