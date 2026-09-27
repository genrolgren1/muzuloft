@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title RoK Gem Bot v3.5.1 - Headless LDPlayer
color 0A

echo ============================================================
echo          RISE OF KINGDOMS GEM BOT v3.5.1
echo            MAIN LDPLAYER + REQUIRED COMPANION
echo ============================================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [ERROR] Python 3 was not found.
  pause
  exit /b 1
)

set "KS_HOME=%LOCALAPPDATA%\KingdomServices"
set "VENV=%KS_HOME%\venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%KS_HOME%" mkdir "%KS_HOME%" >nul 2>&1

if not exist "%VPY%" (
  echo [SETUP] Creating Python environment...
  %PY% -m venv "%VENV%"
  if errorlevel 1 goto :fail
)

"%VPY%" -c "import PIL,flask,waitress,cv2,numpy" >nul 2>&1
if errorlevel 1 (
  echo [SETUP] Installing Python packages...
  "%VPY%" -m pip install --disable-pip-version-check "Pillow>=10.4,<13" "Flask>=3.1,<4" "waitress>=3,<4" "opencv-python-headless>=4.10,<5"
  if errorlevel 1 goto :fail
)

set "KS_LD_HEADLESS=0"
set "KS_LD_TUNE=1"
if not defined KS_LD_CPU set "KS_LD_CPU=2"
if not defined KS_LD_MEMORY set "KS_LD_MEMORY=2048"
if not defined KS_LD_RESOLUTION set "KS_LD_RESOLUTION=1280,720,240"

rem Normal GemOps must never attach to the isolated Frida Test Lab.
echo [FRIDA] Nexus starts the companion automatically.

rem Reuse an existing normal LDPlayer. Do NOT blindly create index 0.
set "KS_LD_INDEX="
set "KS_INDEX_FILE=%TEMP%\ks_ld_index_%RANDOM%_%RANDOM%.txt"
set "KS_INDEX_ERR=%TEMP%\ks_ld_index_err_%RANDOM%_%RANDOM%.txt"

"%VPY%" "%~dp0ldplayer_backend.py" --select-main >"%KS_INDEX_FILE%" 2>"%KS_INDEX_ERR%"
if errorlevel 1 (
  if exist "%KS_INDEX_ERR%" type "%KS_INDEX_ERR%"
  del /q "%KS_INDEX_FILE%" "%KS_INDEX_ERR%" >nul 2>&1
  echo [ERROR] Could not select an LDPlayer instance.
  goto :fail
)

if exist "%KS_INDEX_FILE%" set /p KS_LD_INDEX=<"%KS_INDEX_FILE%"
del /q "%KS_INDEX_FILE%" "%KS_INDEX_ERR%" >nul 2>&1

if not defined KS_LD_INDEX (
  echo [ERROR] LDPlayer selector returned no index.
  goto :fail
)

echo(%KS_LD_INDEX%| findstr /r /x "[0-9][0-9]*" >nul
if errorlevel 1 (
  echo [ERROR] Invalid LDPlayer index returned: %KS_LD_INDEX%
  goto :fail
)

echo [LDPLAYER] Reusing main RoK index %KS_LD_INDEX%...
echo [ROK] Forcing Rise of Kingdoms to foreground...
echo [DISPLAY] LDPlayer window will stay visible.

"%VPY%" -c "import ldplayer_backend as x; x.ensure_ldplayer(%KS_LD_INDEX%,launch_game=True)" >"%TEMP%\ks_ld_prepare.txt" 2>&1
if errorlevel 1 (
  findstr /C:"LDPLAYER_ADB_SETUP_REQUIRED" "%TEMP%\ks_ld_prepare.txt" >nul 2>&1
  if not errorlevel 1 goto :adbsetup
  type "%TEMP%\ks_ld_prepare.txt"
  goto :fail
)

type "%TEMP%\ks_ld_prepare.txt"
del /q "%TEMP%\ks_ld_prepare.txt" >nul 2>&1


"%VPY%" "%~dp0companion_cli.py" stop >nul 2>&1
if exist "gem_web.pid" (
  set /p OLD_PID=<"gem_web.pid"
  if defined OLD_PID taskkill /PID !OLD_PID! /F >nul 2>&1
)
del /q "gem_web.pid" >nul 2>&1

echo [WEB] Starting dashboard...
"%VPY%" -c "import subprocess,sys,os; o=open('gem_web.log','a',encoding='utf-8'); p=subprocess.Popen([sys.executable,'web_server.py','--host','127.0.0.1','--port','8080'],cwd=os.getcwd(),stdout=o,stderr=o,env=os.environ.copy(),creationflags=0x08000000|0x00000008); open('gem_web.pid','w').write(str(p.pid))"
if errorlevel 1 goto :fail

set "READY="
for /L %%I in (1,1,30) do (
  if not defined READY (
    "%VPY%" -c "import urllib.request,json; x=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=1)); raise SystemExit(0 if x.get('ok') else 1)" >nul 2>&1
    if not errorlevel 1 set "READY=1"
    if not defined READY timeout /t 1 /nobreak >nul
  )
)
if not defined READY goto :fail

echo.
echo [READY] Main LDPlayer index %KS_LD_INDEX%
echo [WEB]   http://127.0.0.1:8080
start "" "http://127.0.0.1:8080/"
exit /b 0

:adbsetup
"%VPY%" "%~dp0ldplayer_backend.py" --show --index %KS_LD_INDEX% >nul 2>&1
cls
echo ============================================================
echo        LDPLAYER CONNECTION NOT READY
echo ============================================================
echo.
echo Local ADB was enabled automatically for index %KS_LD_INDEX%.
echo The selected emulator has not established its ADB connection.
echo Run LDPLAYER_DIAGNOSTICS.bat, then retry the launcher.
echo.

pause
exit /b 2

:fail
echo.
echo [ERROR] Startup failed.
echo Run LDPLAYER_DIAGNOSTICS.bat and send the output if needed.
pause
exit /b 1
