@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if exist "%VPY%" "%VPY%" "%~dp0companion_cli.py" stop
if exist "gem_web.pid" (
  set /p P=<"gem_web.pid"
  taskkill /PID !P! /T /F >nul 2>&1
  del /q "gem_web.pid" >nul 2>&1
)
echo GemOps workers and dashboard stopped.
if /I not "%~1"=="/quiet" pause
