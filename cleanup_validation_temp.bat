@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Cannot enter the project directory.
  pause
  exit /b 1
)

set "DRY_RUN="
set "ASSUME_YES="
if /I "%~1"=="--dry-run" set "DRY_RUN=1"
if /I "%~1"=="--yes" set "ASSUME_YES=1"

echo This removes only generated test and validation directories:
echo   .constraints-verify
echo   outputs\constraint_smoke
echo   outputs\form_validation_smoke
echo   outputs\_pytest_*
echo   outputs\_wheel_smoke_20260718
echo   outputs\_release_build_verify
echo   outputs\_quick_demo_verify
echo   outputs\_tui_final_verify
echo   outputs\_media_health_actual_check
echo   outputs\_final_architecture_demo
echo   outputs\_final_wheel_check
echo   outputs\.audit-probe-history
echo   outputs\tui_events
echo   outputs\.tui-events
echo   outputs\_preview
echo   outputs\quick_demo
echo.
echo It keeps real media, acceptance outputs, .tui-history, diagnostics,
echo and outputs\.gitkeep.
echo.

if defined DRY_RUN goto :remove_targets
if defined ASSUME_YES goto :remove_targets
choice /C YN /N /M "Continue? [Y/N] "
if errorlevel 2 (
  echo Cancelled. Nothing was removed.
  pause
  exit /b 0
)

:remove_targets
set "FAILED=0"
call :remove_one ".constraints-verify"
call :remove_one "outputs\constraint_smoke"
call :remove_one "outputs\form_validation_smoke"
for /d %%D in ("outputs\_pytest_*") do call :remove_one "%%~D"
call :remove_one "outputs\_wheel_smoke_20260718"
call :remove_one "outputs\_release_build_verify"
call :remove_one "outputs\_quick_demo_verify"
call :remove_one "outputs\_tui_final_verify"
call :remove_one "outputs\_media_health_actual_check"
call :remove_one "outputs\_final_architecture_demo"
call :remove_one "outputs\_final_wheel_check"
call :remove_one "outputs\.audit-probe-history"
call :remove_one "outputs\tui_events"
call :remove_one "outputs\.tui-events"
call :remove_one "outputs\_preview"
call :remove_one "outputs\quick_demo"

echo.
if "%FAILED%"=="0" (
  if defined DRY_RUN (
    echo [OK] Dry run complete. Nothing was removed.
  ) else (
    echo [OK] Generated validation directories were removed.
  )
) else (
  echo [FAIL] One or more directories could not be removed.
)

if not defined DRY_RUN if not defined ASSUME_YES pause
endlocal & exit /b %FAILED%

:remove_one
set "TARGET=%~1"
if not exist "%TARGET%" (
  echo [SKIP] %TARGET% does not exist.
  exit /b 0
)
if defined DRY_RUN (
  echo [DRY RUN] Would remove %TARGET%
  exit /b 0
)
echo [REMOVE] %TARGET%
rmdir /s /q "%TARGET%"
if exist "%TARGET%" (
  echo [FAIL] %TARGET% still exists.
  set "FAILED=1"
) else (
  echo [OK] %TARGET%
)
exit /b 0
