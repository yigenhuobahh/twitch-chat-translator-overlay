@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter repository directory.
  if not defined CI pause
  exit /b 1
)

REM ASCII-only .bat

echo ======== Update ========
if exist ".git" (
  echo [1/3] git pull --ff-only
  git pull --ff-only
  if errorlevel 1 (
    echo [FAIL] git pull failed. Update stopped.
    echo        The remote history may have been rewritten.
    echo        1. Back up only your local .env, jobs, custom profiles,
    echo           and configs\launcher.local.yaml.
    echo        2. Create a fresh clone in a new directory.
    echo        3. Restore those local files into the fresh clone.
    if not defined CI pause
    exit /b 1
  )
) else (
  echo [FAIL] This directory is not a git checkout; source update is unavailable.
  echo        ZIP/source-archive copies cannot update themselves.
  echo        Download a fresh release or create a fresh clone instead.
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


echo [2/3] pip install
"%PY%" -m pip install -U pip
if errorlevel 1 (
  echo [FAIL] pip upgrade failed. Update stopped.
  if not defined CI pause
  exit /b 1
)
if exist "requirements.txt" (
  "%PY%" -m pip install -r requirements.txt
) else (
  "%PY%" -m pip install -e .
)
if errorlevel 1 (
  echo [FAIL] dependency install failed. Update stopped.
  if not defined CI pause
  exit /b 1
)

echo [3/3] doctor
"%PY%" scripts\render_cn_chat.py --doctor
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [FAIL] --doctor exit %RC%.
  if not defined CI pause
  exit /b %RC%
)
echo.
echo Update done.
if not defined CI pause
endlocal
exit /b 0
