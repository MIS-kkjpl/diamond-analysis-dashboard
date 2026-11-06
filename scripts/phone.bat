@echo off
rem Open Stockwise to your phone through a Cloudflare quick tunnel (https://<random>.trycloudflare.com).
rem The app must be running (scripts\web.bat). Leave this window open; close it to shut the link.
rem The link changes every time this starts, and anyone who has it reaches the Stockwise sign-in page,
rem so share it only with people who have an account. Sign-in, lockout after 5 wrong passwords and
rem the 12-hour idle sign-out still apply.
setlocal
cd /d "%~dp0.."
if not exist "tools\cloudflared.exe" (
    echo tools\cloudflared.exe is missing. Download cloudflared-windows-amd64.exe from
    echo https://github.com/cloudflare/cloudflared/releases and save it as tools\cloudflared.exe
    exit /b 1
)
echo Starting the phone link... look for the line with https://....trycloudflare.com below and open it on your phone.
echo.
"tools\cloudflared.exe" tunnel --no-autoupdate --url http://localhost:8600
