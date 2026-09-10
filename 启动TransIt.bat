@echo off
rem TransIt 启动脚本（UTF-8 内容以 GBK 保存，CRLF 换行）
chcp 936 >nul
cd /d "%~dp0"

if "%1"=="cli" (
    python transit_cli.py
    pause
    exit /b 0
)
if "%1"=="gui" (
    start "" pythonw transit_gui.py
    exit /b 0
)

rem 默认：WebUI（浏览器界面）。webui.py 自带延迟打开浏览器逻辑，
rem 这里不用 timeout/ping 做等待（Git usr/bin 会劫持这两个命令）。
start "TransIt WebUI" /min python -u webui.py
exit /b 0
