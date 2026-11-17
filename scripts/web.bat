@echo off
rem Start (or restart) the Stockwise web app on port 8600.
rem Double-click it, or run "scripts\web.bat" from the project folder. Leave the window open; close it to stop the app.
rem "scripts\web.bat reload" also restarts the app by itself whenever a code file changes (for development).
rem "scripts\web.bat phone" also opens the phone link (scripts\phone.bat) in a second window.
setlocal
set "PROJECT=%~dp0.."
cd /d "%PROJECT%" || exit /b 1

rem stop whatever is already serving port 8600 (an earlier copy of the app)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8600 .*LISTENING"') do (
    echo Stopping the running app (process %%p^)...
    taskkill /PID %%p /F >nul 2>&1
)

if /i "%~1"=="phone" start "Stockwise phone link" cmd /c "%PROJECT%\scripts\phone.bat"
if /i "%~2"=="phone" start "Stockwise phone link" cmd /c "%PROJECT%\scripts\phone.bat"

set "RELOAD="
if /i "%~1"=="reload" set "RELOAD=--reload --reload-dir src"
echo Starting Stockwise on http://localhost:8600 ...
".venv\Scripts\uvicorn.exe" kkj.web.app:app --host 0.0.0.0 --port 8600 %RELOAD%
