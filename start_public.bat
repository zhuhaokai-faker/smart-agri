@echo off
cd /d "%~dp0"

echo ============================================================
echo   Smart Agri Analytics - PUBLIC DEMO MODE
echo ============================================================
echo.
echo   NOTE: this file is deliberately pure ASCII.
echo   A .bat containing non-ASCII text is parsed unreliably by cmd
echo   (the parser loses its byte offset on multi-byte characters and
echo   starts executing from the middle of a line - intermittently).
echo   Same root cause as requirements.txt having to stay ASCII.
echo   Chinese documentation for this script lives in README.md.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run start.bat once first.
    pause
    exit /b 1
)

if not exist "tools\cloudflared.exe" (
    echo [ERROR] tools\cloudflared.exe not found.
    echo.
    echo   Download cloudflared-windows-amd64.exe from:
    echo     https://github.com/cloudflare/cloudflared/releases/latest
    echo   If GitHub is slow, prefix that URL with a mirror, e.g.
    echo     https://ghfast.top/https://github.com/cloudflare/...
    echo   Then save it as tools\cloudflared.exe
    pause
    exit /b 1
)

REM ---- Port check ----
REM  If port 5000 is already taken by a leftover DEV instance (debug ON),
REM  the tunnel would point at THAT instance and expose the Flask debugger
REM  to the internet. "Thought I was running safe mode but the tunnel
REM  points elsewhere" is the easiest mistake to make here, so refuse.
netstat -ano | findstr ":5000 " | findstr "LISTENING" > nul
if not errorlevel 1 (
    echo [ERROR] Port 5000 is already in use. Refusing to continue.
    echo.
    echo   A leftover instance is probably still running in DEBUG mode.
    echo   The tunnel would point at it and expose the Flask debugger.
    echo.
    echo   Find it and stop it:
    echo     netstat -ano ^| findstr :5000
    echo     taskkill /F /PID ^<pid^>
    pause
    exit /b 1
)

REM ---- 1/2 start the app ----
REM  --prod is a SECURITY requirement, not just a mode switch: the default
REM  dev mode runs debug=True, and the Flask debugger lets any visitor
REM  execute arbitrary Python from the browser.
echo [1/2] Starting app (public mode, debug OFF)...
start "smart-agri-app" /min ".venv\Scripts\python.exe" run.py --prod

echo       waiting for the app to come up...
ping -n 7 127.0.0.1 > nul

REM ---- 2/2 open the tunnel ----
echo.
echo [2/2] Opening Cloudflare tunnel...
echo.
echo   ------------------------------------------------------------
echo    A public URL like https://xxxx.trycloudflare.com appears
echo    below. Share that URL. It CHANGES on every restart, so
echo    start this first, then send the URL out.
echo.
echo    Closing this window drops the tunnel immediately.
echo    Also close the minimized app window when done.
echo   ------------------------------------------------------------
echo.

"tools\cloudflared.exe" tunnel --url http://127.0.0.1:5000

echo.
echo Tunnel closed. Remember to close the minimized app window too.
pause
