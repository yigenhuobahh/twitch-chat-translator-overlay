@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 exit /b 1

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import textual" >nul 2>&1
if errorlevel 1 (
  echo [TUI] First run needs the optional Textual dependency.
  set /p "INSTALL_TUI=Install it now? [Y/n] "
  if /I "%INSTALL_TUI%"=="n" goto TUI_MISSING
  "%PY%" -m pip install ".[tui]"
  if errorlevel 1 goto TUI_INSTALL_FAIL
)
"%PY%" scripts\tui_run.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not defined CI pause
exit /b %RC%

:TUI_MISSING
echo [TUI] Install later with: "%PY%" -m pip install ".[tui]"
if not defined CI pause
exit /b 1

:TUI_INSTALL_FAIL
echo [TUI] Installation failed. Check your network, then retry.
if not defined CI pause
exit /b 1
