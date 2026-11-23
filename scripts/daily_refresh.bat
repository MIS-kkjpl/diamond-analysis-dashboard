@echo off
rem Stockwise daily refresh: backup, yesterday's + today's sales, today's stock (also in Business Studio's categories), recommendations, accuracy audit.
rem Scheduled by Windows Task Scheduler (task "Stockwise daily refresh"); log in logs\daily_<stamp>.log.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\daily_refresh.py
