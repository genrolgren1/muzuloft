@echo off
setlocal EnableExtensions
title Kingdom Services - STOP EVERYTHING
cd /d "%~dp0"

echo ============================================
echo   KINGDOM SERVICES - STOP EVERYTHING
echo ============================================
echo.

set "STATE=%LOCALAPPDATA%\KingdomServices"
set "VPY=%STATE%\venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=py -3"

echo [1/5] Stopping Gem Bot and web dashboard...
if exist "%~dp0STOP_GEM_BOT.bat" (
    call "%~dp0STOP_GEM_BOT.bat" /quiet >nul 2>&1
)

echo [2/5] Stopping account-control and project Python workers...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$targets = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'account_controller\.py|web_server\.py|gem_bot\.py|frida_lab\.py|companion_setup\.py|companion_runtime\.py' }; foreach($p in $targets){ try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1

echo [3/5] Stopping Frida test lab...
if exist "%~dp0STOP_COMPANION.bat" (
    call "%~dp0STOP_COMPANION.bat" >nul 2>&1
)

echo [4/5] Stopping LDPlayer instances used by this project...
set "IDX="
set "IDXFILE=%TEMP%\ks_stop_idx_%RANDOM%_%RANDOM%.txt"

"%VPY%" "%~dp0ldplayer_backend.py" --select-main >"%IDXFILE%" 2>nul
if exist "%IDXFILE%" set /p IDX=<"%IDXFILE%"
del /q "%IDXFILE%" >nul 2>&1

if defined IDX (
    "%VPY%" "%~dp0ldplayer_backend.py" --stop --index "%IDX%" >nul 2>&1
)

rem Stop the dedicated Frida test LDPlayer if it still exists.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$roots=@('D:\LDPlayer\LDPlayer14','D:\LDPlayer\LDPlayer9','C:\LDPlayer\LDPlayer14','C:\LDPlayer\LDPlayer9','C:\Program Files\LDPlayer\LDPlayer9'); $c=$null; foreach($r in $roots){ $x=Join-Path $r 'ldconsole.exe'; if(Test-Path $x){$c=$x;break} }; if($c){ $lines=& $c list2 2>$null; foreach($line in $lines){ $parts=$line -split ','; if($parts.Count -ge 2 -and $parts[1] -eq 'KS_FRIDA_TEST'){ & $c quit --index $parts[0] 2>$null | Out-Null } } }" >nul 2>&1

echo [5/5] Cleaning stale project PID files...
del /q "%STATE%\gem_bot\gem_bot.pid" >nul 2>&1
del /q "%STATE%\gem_bot\web.pid" >nul 2>&1
del /q "%STATE%\frida_test_lab\*.pid" >nul 2>&1

echo.
echo ============================================
echo   EVERYTHING HAS BEEN STOPPED
echo ============================================
echo.
pause
endlocal
