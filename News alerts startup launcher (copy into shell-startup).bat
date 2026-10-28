@echo off
rem Copy this file into your Windows Startup folder (Win+R, type shell:startup, Enter) so the news alerts start when you log in.
timeout /t 60 /nobreak >nul
start "News alerts" /min "C:\Users\Edwin\Desktop\nat-gas-dashboard-complete\nat-gas-dashboard\Run News Alerts (Windows).bat"
