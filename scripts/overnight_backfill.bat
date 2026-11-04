@echo off
rem One-off overnight ERP backfill; see scripts\overnight_backfill.py.
rem For Task Scheduler: runs from the project folder with the project's Python.
setlocal
set "PROJECT=C:\Users\kartik.riyal\Software\diamond-anaylysis-dashboard"
set "PYTHONIOENCODING=utf-8"
cd /d "%PROJECT%" || exit /b 1
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" scripts\overnight_backfill.py %* >> "logs\overnight_task.log" 2>&1
exit /b %ERRORLEVEL%
