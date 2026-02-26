@echo off
chcp 65001 >nul
title Smart Group Bot

echo.
echo ========================================
echo   Smart Group Bot - 一键启动
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.12+
    pause
    exit /b 1
)

:: 检查依赖是否已安装
python -c "import aiogram" >nul 2>&1
if errorlevel 1 (
    echo [安装] 首次运行，正在安装依赖...
    pip install -e . --quiet
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
    echo [安装] 依赖安装完成
)

:: 启动
python start.py
pause
