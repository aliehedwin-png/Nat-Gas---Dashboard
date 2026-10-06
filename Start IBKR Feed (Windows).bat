@echo off
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if defined PY goto havepy
python --version >nul 2>nul && set PY=python
if defined PY goto havepy
echo Python is not installed. See START HERE.txt
pause
exit /b
:havepy
%PY% -c "import ib_async" >nul 2>nul
if errorlevel 1 (
  echo Installing the IBKR library ib_async, one time only...
  %PY% -m pip install ib_async
)
echo.
echo Reading bars from TWS / IB Gateway (read-only). Leave this window open.
%PY% ibkr_feed.py
echo.
echo The feed stopped. Read any message above, then press a key to close.
pause
