@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Telegram 陪聊机器人
python --version >nul 2>&1
if errorlevel 1 (
    echo 请先安装 Python 3.10 或更新版本，并勾选 Add Python to PATH。
    pause
    exit /b 1
)
python -c "from importlib.metadata import version; assert tuple(map(int, version('python-telegram-bot').split('.')[:2])) >= (22, 8); from openai import AsyncOpenAI" >nul 2>&1
if errorlevel 1 (
    echo 安装或更新依赖中...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络。
        pause
        exit /b 1
    )
)
echo 机器人运行期间会代聊；回来后请运行停止脚本。
echo 私聊机器人可以测试，日志保存到 logs\bot.log。
python "%~dp0bot.py"
echo 机器人已停止。
pause
