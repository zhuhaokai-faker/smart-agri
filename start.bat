@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo   智慧农业种植管理与产量分析平台
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没有找到虚拟环境 .venv
    echo        请先执行：
    echo            py -3.12 -m venv .venv
    echo            .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [提示] 没有找到 .env，正在从 .env.example 复制...
    copy .env.example .env > nul
    echo        请检查 .env 里的数据库密码是否正确。
    echo.
)

REM 检查数据库是否已初始化
".venv\Scripts\python.exe" -c "import pymysql,sys;sys.path.insert(0,'.');from config import DB_CONFIG;pymysql.connect(database='smart_agri',**DB_CONFIG)" 2>nul
if errorlevel 1 (
    echo [提示] 数据库 smart_agri 还没有初始化，正在自动初始化并造数...
    echo.
    ".venv\Scripts\python.exe" scripts\init_db.py --force
    ".venv\Scripts\python.exe" scripts\seed.py
    ".venv\Scripts\python.exe" scripts\gen_geojson.py
    ".venv\Scripts\python.exe" scripts\verify.py
    echo.
)

".venv\Scripts\python.exe" run.py
pause
