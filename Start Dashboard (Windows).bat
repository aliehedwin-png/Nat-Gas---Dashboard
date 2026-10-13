@echo off
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if defined PY goto havepy
python --version >nul 2>nul && set PY=python
if defined PY goto havepy
echo Python is not installed. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then start this file again.
pause
exit /b
:havepy
%PY% -c "import ib_async" >nul 2>nul
if errorlevel 1 (
  echo Installing the IBKR library ib_async, one time only...
  %PY% -m pip install ib_async
)
echo.
echo Starting the dashboard. It also starts the read-only IBKR feed. Leave this window open.
rem Open the browser as soon as the dashboard answers (waits up to 2 minutes; also works if it is already running)
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://localhost:8000/api/config | Out-Null; Start-Process 'http://localhost:8000'; break}catch{Start-Sleep 1}}"
%PY% server.py
echo.
echo The dashboard stopped. Read any message above, then press a key to close.
pause
