@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter repository directory.
  if not defined CI pause
  exit /b 1
)

REM ASCII-only launcher. All Chinese UI is in Python (UTF-8).
REM Do NOT put Chinese text in this .bat (cmd uses system codepage / GBK).

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
  echo        If this is an existing .venv, recreate it with install.bat.
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
if /I "%~1"=="-h" goto HELP
if /I "%~1"=="--help" goto HELP
if /I "%~1"=="doctor" goto DOCTOR

REM Drag video + chat HTML onto this file for a no-API 10-second preview.
REM A dropped YAML or a legacy job name still uses the existing job wizard.
"%PY%" scripts\job_wizard.py drop %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAIL] exit %RC%. Try: run.bat doctor
  if not defined CI pause
)
exit /b %RC%

:MENU
REM Chinese interactive menu lives in job_wizard.py
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
echo.
if not "%RC%"=="0" (
  echo [FAIL] exit %RC%.
  if not defined CI pause
  exit /b %RC%
)
if not defined CI pause
exit /b %RC%

:DOCTOR
"%PY%" scripts\render_cn_chat.py --doctor
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [FAIL] exit %RC%.
  if not defined CI pause
  exit /b %RC%
)
if not defined CI pause
exit /b %RC%

:HELP
echo run.bat              Chinese menu
echo run.bat quick        First-run setup + job wizard
echo run.bat demo         Offline demo (no translation API)
echo Drag video + chat HTML onto run.bat for a 10-second preview
echo run.bat new          New job wizard
echo run.bat list         List jobs
echo run.bat NAME         Run jobs\NAME.yaml
echo run.bat NAME --args   Run with extra CLI args forwarded to pipeline
echo run.bat doctor       Environment check
echo install.bat          Install
if not defined CI pause
exit /b 0
