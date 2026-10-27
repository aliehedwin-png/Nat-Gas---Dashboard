@echo off
cd /d "%~dp0"
title News alerts (CL and NG)
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if defined PY goto havepy
python --version >nul 2>nul && set PY=python
if defined PY goto havepy
echo Python is not installed. Install it from https://www.python.org/downloads/ then start this file again.
pause
exit /b
:havepy
echo Crude oil and natural gas NEWS ALERTS. Information only: this never places or changes trades.
echo Important headlines pop up as Windows notifications; all headlines are on the dashboard News tab.
echo If it stops for any reason it restarts after 60 seconds. Close this window to stop the alerts.
:loop
%PY% news_alerts.py
echo %date% %time% news alerts exited with code %errorlevel%; restarting in 60 s>> data\news\restarts.log
echo The news checker stopped. Restarting in 60 seconds (close this window to cancel)...
timeout /t 60 /nobreak >nul
goto loop
