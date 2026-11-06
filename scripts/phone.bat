@echo off
rem Open Stockwise to your phone through a Cloudflare quick tunnel (https://<random>.trycloudflare.com).
rem Usually started for you by "scripts\web.bat phone". Leave this window open; close it to shut the link.
rem The link changes every time this starts, and anyone who has it reaches the Stockwise sign-in page,
rem so share it only with people who have an account. Sign-in, lockout after 5 wrong passwords and
rem the 12-hour idle sign-out still apply. Everything cloudflared reports goes to logs\phone.log.
setlocal
cd /d "%~dp0.."
title Stockwise phone link
if not exist logs mkdir logs

if not exist "tools\cloudflared.exe" (
    echo tools\cloudflared.exe is missing. Download cloudflared-windows-amd64.exe from
    echo https://github.com/cloudflare/cloudflared/releases and save it as tools\cloudflared.exe
    goto :hold
)

rem stop an earlier phone link, if one is still running
taskkill /IM cloudflared.exe /F >nul 2>&1

echo Starting the phone link (takes up to 30 seconds)...
start "" /b "tools\cloudflared.exe" tunnel --no-autoupdate --metrics 127.0.0.1:20241 --logfile logs\phone.log --loglevel info --url http://localhost:8600 >nul 2>&1

powershell -NoProfile -Command ^
  "for ($i = 0; $i -lt 40; $i++) { try { $h = (Invoke-RestMethod http://127.0.0.1:20241/quicktunnel -TimeoutSec 2).hostname; if ($h) { Write-Host ''; Write-Host ('   Open on your phone:   https://' + $h) -ForegroundColor Green; Write-Host ''; exit 0 } } catch {}; Start-Sleep 1 }; exit 1"
if errorlevel 1 (
    echo.
    echo No link after 40 seconds. The office network may be blocking Cloudflare's tunnel.
    echo Details: logs\phone.log
    taskkill /IM cloudflared.exe /F >nul 2>&1
    goto :hold
)

netstat -ano | findstr /r /c:":8600 .*LISTENING" >nul || echo Note: the app itself is not running yet - start scripts\web.bat, or the link shows an error page.
echo Leave this window open while you use the link. Press any key here to close the link.
pause >nul
taskkill /IM cloudflared.exe /F >nul 2>&1
exit /b 0

:hold
echo.
pause
exit /b 1
