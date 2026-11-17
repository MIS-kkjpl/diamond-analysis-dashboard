@echo off
rem Start (or restart) the Stockwise web app on port WEB_PORT from .env (default 8600).
rem Double-click it, or run "scripts\web.bat" from the project folder. Leave the window open; close it to stop the app.
rem "scripts\web.bat reload" also restarts the app by itself whenever a code file changes (for development).
rem "scripts\web.bat phone" also opens the phone link (scripts\phone.bat) in a second window.
setlocal
set "PROJECT=%~dp0.."
cd /d "%PROJECT%" || exit /b 1

set "PORT=8600"
for /f "tokens=2 delims== " %%v in ('findstr /b /c:"WEB_PORT=" .env 2^>nul') do set "PORT=%%v"

rem stop whatever is already serving the port (an earlier copy of the app)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    echo Stopping the running app (process %%p^)...
    taskkill /PID %%p /F >nul 2>&1
)

if /i "%~1"=="phone" start "Stockwise phone link" cmd /c "%PROJECT%\scripts\phone.bat"
if /i "%~2"=="phone" start "Stockwise phone link" cmd /c "%PROJECT%\scripts\phone.bat"

set "RELOAD="
if /i "%~1"=="reload" set "RELOAD=--reload --reload-dir src"
echo Starting Stockwise on http://localhost:%PORT% ...
".venv\Scripts\uvicorn.exe" kkj.web.app:app --host 0.0.0.0 --port %PORT% %RELOAD%
