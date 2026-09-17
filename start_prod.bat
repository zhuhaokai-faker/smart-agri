@echo off
cd /d "%~dp0"

echo ============================================================
echo   Smart Agri Analytics - PRODUCTION MODE (multi-process)
echo ============================================================
echo.
echo   NOTE: this file is deliberately pure ASCII.
echo   A .bat containing non-ASCII text is parsed unreliably by cmd
echo   (the parser loses its byte offset on multi-byte characters and
echo   starts executing from the middle of a line - intermittently).
echo   Same root cause as requirements.txt having to stay ASCII.
echo   Chinese documentation for this script lives in README.md.
echo.
echo   This starts nginx + several waitress worker processes.
echo   For everyday local development use start.bat instead - it is
echo   simpler and reloads on code changes.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run start.bat once first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found. Copy .env.example to .env and set
    echo         SECRET_KEY plus the database password first.
    echo.
    echo         Production mode refuses to start with the placeholder
    echo         SECRET_KEY: it is public, so anyone could forge a
    echo         session cookie and become an admin.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" deploy\serve_cluster.py %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
    echo serve_cluster.py exited with code %RC%
)
pause
exit /b %RC%
