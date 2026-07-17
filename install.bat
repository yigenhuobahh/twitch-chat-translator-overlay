@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter repository directory.
  if not defined CI pause
  exit /b 1
)

REM ASCII-only .bat (see run.bat header). Chinese UI lives in Python.

echo ======== Install ========
echo Repo: %CD%
echo.

set "PY="
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  goto :py_found
)

where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
    goto :py_found
  )
)

where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
  if not errorlevel 1 (
    set "PY=python"
    goto :py_found
  )
)

echo [FAIL] Python 3.10+ not found. Install from https://www.python.org/downloads/
echo        Enable: Add python.exe to PATH
if not defined CI pause
exit /b 1

:py_found
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
  if errorlevel 1 (
    echo [FAIL] Existing .venv must use Python 3.10+ and be runnable.
    echo        Remove .venv, then run install.bat again.
    if not defined CI pause
    exit /b 1
  )
)


echo [1/5] Python: %PY%
if not exist ".venv\Scripts\python.exe" (
  echo [2/5] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [FAIL] venv failed
    if not defined CI pause
    exit /b 1
  )
) else (
  echo [2/5] .venv already exists
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 (
  echo [FAIL] Created .venv is not runnable with Python 3.10+.
  if not defined CI pause
  exit /b 1
)

echo [3/5] Installing deps ...
"%PY%" -m pip install -U pip
if errorlevel 1 (
  echo [FAIL] pip upgrade failed
  if not defined CI pause
  exit /b 1
)
if exist "requirements.txt" (
  "%PY%" -m pip install -r requirements.txt
) else (
  "%PY%" -m pip install -e .
)
if errorlevel 1 (
  echo [FAIL] pip install failed
  if not defined CI pause
  exit /b 1
)

echo [4/5] --init + --doctor
"%PY%" scripts\render_cn_chat.py --init
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [FAIL] --init exit %RC%.
  if not defined CI pause
  exit /b %RC%
)
REM doctor prompts on TTY to help install FFmpeg when missing (default Yes).
"%PY%" scripts\render_cn_chat.py --doctor
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [WARN] Environment not fully ready after doctor.
  echo        If you skipped FFmpeg install, run again: run.bat doctor
  echo        Or: "%PY%" scripts\render_cn_chat.py --doctor --offer-fix
  if not defined CI pause
  exit /b %RC%
)
echo.
echo [5/5] Optional: TwitchDownloaderCLI (download VOD/chat without GUI)
if not defined CI (
  "%PY%" scripts\render_cn_chat.py --install-td-prompt
) else (
  echo   CI: skip optional TwitchDownloaderCLI prompt
)
echo.
echo ======== Install done ========
echo Next:
echo   1. Edit .env if you need translation API
echo   2. Double-click run.bat  -^> [1] New job  or  [3] Download media
echo   3. Or: run.bat new
echo   4. Reuse: run.bat example_job
echo   5. Optional download: python scripts\render_cn_chat.py --download ^<url^>
echo.
if not defined CI pause
endlocal
exit /b 0
