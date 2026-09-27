@echo off
setlocal
title RoK Gem Bot - Prepare Local Vision
where ollama >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Ollama is not installed or not on PATH.
  echo Install Ollama first, then run this BAT again.
  echo The Gem Bot uses qwen3-vl:2b locally to understand RoK screenshots.
  pause
  exit /b 1
)
echo [VISION] Starting Ollama if needed...
start "" /min ollama serve
timeout /t 2 >nul
echo [VISION] Downloading/checking qwen3-vl:2b...
ollama pull qwen3-vl:2b
if errorlevel 1 (
  echo [ERROR] Model setup failed.
  pause
  exit /b 1
)
echo [DONE] Local Gem Bot vision is ready.
pause
