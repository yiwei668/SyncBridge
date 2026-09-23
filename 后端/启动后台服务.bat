@echo off
chcp 65001 >nul
title SyncBridge 后台服务
cd /d "%~dp0"

set "DB_PATH=%~dp0..\数据库\syncbridge.db"
set "PYTHONPATH=%~dp0"
set "SyncBridge_DB_PATH=%DB_PATH%"

echo ============================================
echo   SyncBridge 后台服务启动器
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 首次运行，正在创建 Python 虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo [错误] 创建虚拟环境失败。请确认已安装 Python 3.10 或更高版本，
        echo        并已勾选 "Add Python to PATH"。
        echo.
        pause
        exit /b 1
    )
    echo [2/3] 正在安装依赖（首次约需 1-2 分钟）...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络连接。
        echo.
        pause
        exit /b 1
    )
    echo [3/3] 准备完成。
) else (
    echo 虚拟环境已存在，跳过安装。
)

echo.
echo 服务地址：
echo    本机访问    http://127.0.0.1:8123
echo    局域网访问   http://本机IP:8123
echo    接口文档    http://127.0.0.1:8123/docs
echo    数据库      %DB_PATH%
echo.
echo 保持本窗口开启；关闭窗口即停止服务。
echo ============================================
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8123

echo.
echo 服务已停止。
pause
