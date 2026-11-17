@echo off
rem One-off morning catch-up (see scripts\morning_catchup.py). For Task Scheduler.
setlocal
set "PROJECT=%~dp0.."
set "PYTHONIOENCODING=utf-8"
cd /d "%PROJECT%" || exit /b 1
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" scripts\morning_catchup.py %* >> "logs\morning_catchup_task.log" 2>&1
exit /b %ERRORLEVEL%
