@echo off
chcp 65001 >nul 2>&1
echo.
echo   _____   ____  _____  _______ ______ _____
echo  |  __ \ / __ \|  __ \|__   __|  ____/ ____|
echo  | |  | | |  | | |__) |  | |  | |__ | |
echo  | |  | | |  | |  _  /   | |  |  __|| |
echo  | |__| | |__| | | \ \   | |  | |___| |____
echo  |_____/ \____/|_|  \_\  |_|  |______\_____|
echo.
echo   一键部署 - Web 可视化部署工具
echo.

echo [1/3] 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python 已安装

echo [2/3] 安装依赖...
pip install flask requests >nul 2>&1
echo [OK] 依赖已就绪

echo [3/3] 启动服务...
echo.
echo ========================================
echo   服务已启动: http://localhost:5000
echo   按 Ctrl+C 停止服务
echo ========================================
echo.

python "%~dp0server.py"
