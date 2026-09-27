@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title RoK Gem Bot - LDPlayer Diagnostics

set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Run RUN_GEM_BOT.bat once first.
  pause
  exit /b 1
)

set "IDX="
set "KS_INDEX_FILE=%TEMP%\ks_ld_diag_%RANDOM%_%RANDOM%.txt"
"%VPY%" "%~dp0ldplayer_backend.py" --select-main >"%KS_INDEX_FILE%" 2>nul
if exist "%KS_INDEX_FILE%" set /p IDX=<"%KS_INDEX_FILE%"
del /q "%KS_INDEX_FILE%" >nul 2>&1

if not defined IDX (
  echo [ERROR] Could not determine the main LDPlayer index.
  pause
  exit /b 1
)

echo Selected main LDPlayer index: %IDX%
echo.
"%VPY%" "%~dp0ldplayer_backend.py" --status --index "%IDX%"
echo.
pause
